# -*- coding: utf-8 -*-
"""采集本机、国内探测点和海外探测点看到的 IPv4 出口信息。"""
import json
import ipaddress
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import vpn_os


_REQUEST_TIMEOUT = 6
_USER_AGENT = 'CXVPN-Manager/1.0'
_OVERSEAS_TRACE_URLS = (
    'https://cloudflare.com/cdn-cgi/trace',
    'https://www.cloudflare.com/cdn-cgi/trace',
)


def _empty_entry(error=''):
    return {
        'ok': False,
        'ip': '',
        'location': '',
        'provider': '',
        'detail': '',
        'error': str(error or ''),
    }


def _text(value):
    return str(value or '').strip()


def _join_unique(values, separator=' · '):
    result = []
    for value in values:
        item = _text(value)
        if item and item not in result:
            result.append(item)
    return separator.join(result)


def _open_url(request, bypass_proxy=False):
    if bypass_proxy:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request, timeout=_REQUEST_TIMEOUT)
    return urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT)


def _request_text(url, bypass_proxy=False):
    request = urllib.request.Request(
        url, headers={'Accept': '*/*', 'User-Agent': _USER_AGENT})
    with _open_url(request, bypass_proxy=bypass_proxy) as response:
        raw = response.read(128 * 1024)
    return raw.decode('utf-8', errors='replace')


def _request_json(url, bypass_proxy=False):
    return json.loads(_request_text(url, bypass_proxy=bypass_proxy))


def _parse_domestic(payload):
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError('国内探测点返回格式异常')
    address = _text(data.get('ip'))
    if not address:
        raise ValueError('国内探测点未返回 IP')
    location_parts = data.get('location')
    if not isinstance(location_parts, list):
        location_parts = [
            data.get('country'), data.get('province'), data.get('city')]
    location_parts = [_text(item) for item in location_parts]
    provider = location_parts[-1] if len(location_parts) > 3 else ''
    location = _join_unique(location_parts[:3], ' ')
    return {
        'ok': True,
        'ip': address,
        'location': location or '位置未知',
        'provider': provider or _text(data.get('isp')) or '运营商未知',
        'detail': '',
        'error': '',
    }


def _parse_overseas(payload):
    if not isinstance(payload, dict):
        raise ValueError('海外探测点返回格式异常')
    address = _text(payload.get('ip'))
    if not address:
        raise ValueError('海外探测点未返回 IP')
    return {
        'ok': True,
        'ip': address,
        'location': _join_unique([
            payload.get('country_name'), payload.get('region'),
            payload.get('city')]) or '位置未知',
        'provider': _text(payload.get('org')) or '运营商未知',
        'detail': '',
        'error': '',
    }


def _parse_cloudflare_trace(payload):
    fields = {}
    for line in _text(payload).splitlines():
        key, separator, value = line.partition('=')
        if separator:
            fields[key.strip()] = value.strip()
    address = _text(fields.get('ip'))
    try:
        if ipaddress.ip_address(address).version != 4:
            raise ValueError
    except ValueError as error:
        raise ValueError('海外探测点未返回有效 IPv4 地址') from error
    country = _text(fields.get('loc')).upper()
    return {
        'ok': True,
        'ip': address,
        'location': country or '位置未知',
        'provider': '运营商未知',
        'detail': _text(fields.get('colo')),
        'error': '',
    }


def _system_proxy_for_active_tunnel():
    """仅在系统存在活跃隧道时使用代理，忽略断开后残留的本地代理。"""
    proxies = urllib.request.getproxies()
    if not (proxies.get('https') or proxies.get('http')):
        return False
    script = r"""
$candidates = Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue |
  Where-Object {
    $_.Status -eq 'Up' -and $_.Virtual -and
    $_.InterfaceDescription -notmatch 'WAN Miniport|Hyper-V|VMware|VirtualBox|Docker|WSL'
  }
$active = $false
foreach ($adapter in $candidates) {
  $ip = Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object {
      $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' -and
      $_.AddressState -ne 'Tentative'
    } | Select-Object -First 1
  $ipInterface = Get-NetIPInterface -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue
  $identity = "$($adapter.Name) $($adapter.InterfaceDescription)"
  $looksLikeTunnel = $identity -match 'WireGuard|Wintun|\bTUN\b|\bTAP\b|VPN|Tunnel|OpenVPN|ZeroTier|Tailscale|Clash|Mihomo|sing-box|v2ray'
  $lowMetricVirtual = $ipInterface -and [int]$ipInterface.InterfaceMetric -le 10
  if ($ip -and ($looksLikeTunnel -or $lowMetricVirtual)) {
    $active = $true
    break
  }
}
[PSCustomObject]@{ active = $active } | ConvertTo-Json -Compress
"""
    ok, stdout, _ = vpn_os._ps(script, timeout=8)
    if not ok or not stdout:
        return False
    try:
        return bool(json.loads(stdout).get('active'))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def query_local():
    """读取承载默认 IPv4 路由的物理网卡地址。"""
    script = r"""
$routes = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
  Sort-Object @{Expression={ $_.RouteMetric + $_.InterfaceMetric }}
$result = $null
foreach ($route in $routes) {
  $adapter = Get-NetAdapter -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue
  $address = Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' } |
    Select-Object -First 1
  if ($adapter -and $address -and $adapter.Status -eq 'Up' -and -not $adapter.Virtual) {
    $result = [PSCustomObject]@{ ip = $address.IPAddress; adapter = $adapter.Name }
    break
  }
}
if (-not $result) {
  $address = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' } |
    Select-Object -First 1
  if ($address) {
    $adapter = Get-NetAdapter -InterfaceIndex $address.InterfaceIndex -ErrorAction SilentlyContinue
    $result = [PSCustomObject]@{ ip = $address.IPAddress; adapter = $adapter.Name }
  }
}
if ($result) { $result | ConvertTo-Json -Compress }
"""
    ok, stdout, stderr = vpn_os._ps(script, timeout=8)
    if not ok or not stdout:
        return _empty_entry(stderr or '未找到可用的本机 IPv4 地址')
    try:
        data = json.loads(stdout)
        address = _text(data.get('ip'))
        if not address:
            raise ValueError('未找到可用的本机 IPv4 地址')
        return {
            'ok': True,
            'ip': address,
            'location': '',
            'provider': '',
            'detail': _text(data.get('adapter')) or '本机网卡',
            'error': '',
        }
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        return _empty_entry(error)


def query_domestic():
    try:
        return _parse_domestic(_request_json(
            'https://myip.ipip.net/json', bypass_proxy=True))
    except Exception as error:
        return _empty_entry(error)


def query_overseas():
    errors = []
    bypass_proxy = not _system_proxy_for_active_tunnel()
    for url in _OVERSEAS_TRACE_URLS:
        try:
            result = _parse_cloudflare_trace(
                _request_text(url, bypass_proxy=bypass_proxy))
            try:
                metadata = _parse_overseas(_request_json(
                    f'https://ipapi.co/{result["ip"]}/json/'))
                result['location'] = metadata['location']
                result['provider'] = metadata['provider']
            except Exception as error:
                result['error'] = f'位置查询失败: {error}'
            return result
        except Exception as error:
            errors.append(f'{url}: {error}')
    return _empty_entry('; '.join(errors) or '海外探测点不可用')


def collect_ip_info(on_result=None):
    """并发采集三个视角，并按实际完成顺序回传单项结果。"""
    queries = {
        'local': query_local,
        'domestic': query_domestic,
        'overseas': query_overseas,
    }
    results = {}
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix='ip-info') as pool:
        futures = {pool.submit(query): name for name, query in queries.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
            except Exception as error:
                result = _empty_entry(error)
            results[name] = result
            if on_result:
                on_result(name, result)
    return results
