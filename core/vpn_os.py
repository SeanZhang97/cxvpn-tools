# -*- coding: utf-8 -*-
"""
core/vpn_os.py - 通过 PowerShell 管理 Windows 系统 VPN 配置
(Get/Add/Set/Remove-VpnConnection, 用户级无需管理员)
"""
import json
import base64
import ipaddress
import os
import subprocess
import tempfile

TYPES = ['Automatic', 'Pptp', 'L2tp', 'Ikev2']
_USER_PBK = os.path.join(
    os.environ.get('APPDATA', ''), 'Microsoft', 'Network', 'Connections',
    'Pbk', 'rasphone.pbk')
_GATEWAY_FIELDS = {
    'IpPrioritizeRemote': 'ipv4_default_gateway',
    'Ipv6PrioritizeRemote': 'ipv6_default_gateway',
}


def _ps(script, timeout=30, env=None):
    process_env = None
    if env:
        process_env = os.environ.copy()
        process_env.update(env)
    # EncodedCommand avoids the system code page on Windows PowerShell; set both
    # streams explicitly so Chinese adapter and VPN names stay UTF-8 end to end.
    utf8_setup = (
        "$utf8 = New-Object System.Text.UTF8Encoding($false); "
        "[Console]::InputEncoding = $utf8; "
        "[Console]::OutputEncoding = $utf8; "
        "$OutputEncoding = $utf8; "
    )
    encoded = base64.b64encode(
        (utf8_setup + str(script or '')).encode('utf-16le')).decode('ascii')
    r = subprocess.run(
        ['powershell', '-NoProfile', '-EncodedCommand', encoded],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW, env=process_env)
    return r.returncode == 0, (r.stdout or '').strip(), (r.stderr or '').strip()


def _quote(value):
    """PowerShell 单引号字面量，系统 VPN 名称和服务器地址均走此入口。"""
    return "'" + str(value or '').replace("'", "''") + "'"


def _normalize_routes(value):
    if not value:
        return []
    rows = value if isinstance(value, list) else [value]
    return sorted({str(row).strip() for row in rows if str(row).strip()})


def _decode_phonebook(raw):
    """按 Windows 电话簿原编码解码，返回 BOM、编码和文本。"""
    if raw.startswith(b'\xff\xfe'):
        bom, encoding = b'\xff\xfe', 'utf-16-le'
    elif raw.startswith(b'\xfe\xff'):
        bom, encoding = b'\xfe\xff', 'utf-16-be'
    elif raw.startswith(b'\xef\xbb\xbf'):
        bom, encoding = b'\xef\xbb\xbf', 'utf-8'
    else:
        bom = b''
        try:
            raw.decode('utf-8')
            encoding = 'utf-8'
        except UnicodeDecodeError:
            encoding = 'mbcs'
    return bom, encoding, raw[len(bom):].decode(encoding, errors='replace')


def _read_gateway_settings(phonebook=None):
    """一次读取用户电话簿中各 VPN 的 IPv4/IPv6 默认网关配置。"""
    path = phonebook or _USER_PBK
    try:
        with open(path, 'rb') as stream:
            _, _, text = _decode_phonebook(stream.read())
    except OSError:
        return {}

    profiles = {}
    current = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith('[') and line.endswith(']'):
            current = line[1:-1]
            profiles.setdefault(current.casefold(), {})
            continue
        if current is None or '=' not in line:
            continue
        key, value = line.split('=', 1)
        field = _GATEWAY_FIELDS.get(key)
        if field:
            profiles[current.casefold()][field] = value.strip() == '1'
    return profiles


def set_gateway_settings(name, ipv4_enabled, ipv6_enabled, phonebook=None):
    """修改单个用户 VPN 的 IPv4/IPv6 远程默认网关开关。"""
    path = phonebook or _USER_PBK
    if not name:
        return False, '未指定 VPN 名称'
    try:
        with open(path, 'rb') as stream:
            raw = stream.read()
        bom, encoding, text = _decode_phonebook(raw)
        separator = '\r\n' if '\r\n' in text else '\n'
        lines = text.split(separator)
        header = f'[{name}]'.casefold()
        start = next((index for index, line in enumerate(lines)
                      if line.strip().casefold() == header), -1)
        if start < 0:
            return False, f'Windows VPN 电话簿中不存在“{name}”'
        end = next((index for index in range(start + 1, len(lines))
                    if lines[index].lstrip().startswith('[')), len(lines))
        desired = {
            'IpPrioritizeRemote': '1' if ipv4_enabled else '0',
            'Ipv6PrioritizeRemote': '1' if ipv6_enabled else '0',
        }
        found = set()
        for index in range(start + 1, end):
            key = lines[index].split('=', 1)[0].strip()
            if key in desired:
                lines[index] = f'{key}={desired[key]}'
                found.add(key)
        insert_at = end
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        missing = [f'{key}={value}' for key, value in desired.items()
                   if key not in found]
        if missing:
            lines[insert_at:insert_at] = missing

        data = bom + separator.join(lines).encode(encoding)
        folder = os.path.dirname(path)
        descriptor, temp_path = tempfile.mkstemp(
            prefix='rasphone.', suffix='.tmp', dir=folder)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(data)
            os.replace(temp_path, path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
        return True, '默认网关配置已更新，重新连接 VPN 后生效'
    except OSError as exc:
        return False, f'默认网关配置写入失败：{exc}'


def list_vpns():
    """返回系统 VPN，并附带双栈网关配置、地址与路由摘要。"""
    script = r'''
$vpns = @(Get-VpnConnection -ErrorAction SilentlyContinue)
$routes4 = @(Get-NetRoute -AddressFamily IPv4 -ErrorAction SilentlyContinue)
$routes6 = @(Get-NetRoute -AddressFamily IPv6 -ErrorAction SilentlyContinue)
$addresses4 = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue)
$addresses6 = @(Get-NetIPAddress -AddressFamily IPv6 -ErrorAction SilentlyContinue)
$result = foreach ($vpn in $vpns) {
  $vpnRoutes4 = @($routes4 | Where-Object { $_.InterfaceAlias -eq $vpn.Name })
  $vpnRoutes6 = @($routes6 | Where-Object { $_.InterfaceAlias -eq $vpn.Name })
  $vpnAddress4 = $addresses4 | Where-Object {
    $_.InterfaceAlias -eq $vpn.Name -and $_.IPAddress -notlike '169.254.*'
  } | Select-Object -First 1
  $vpnAddress6 = $addresses6 | Where-Object {
    $_.InterfaceAlias -eq $vpn.Name -and $_.IPAddress -notlike 'fe80:*'
  } | Select-Object -First 1
  [pscustomobject]@{
    Name = $vpn.Name
    ServerAddress = $vpn.ServerAddress
    TunnelType = [string]$vpn.TunnelType
    ConnectionStatus = [string]$vpn.ConnectionStatus
    SplitTunneling = [bool]$vpn.SplitTunneling
    IpAddress = if ($vpnAddress4) { [string]$vpnAddress4.IPAddress } else { '' }
    PrefixLength = if ($vpnAddress4) { [int]$vpnAddress4.PrefixLength } else { 0 }
    Ipv6Address = if ($vpnAddress6) { [string]$vpnAddress6.IPAddress } else { '' }
    Ipv6PrefixLength = if ($vpnAddress6) { [int]$vpnAddress6.PrefixLength } else { 0 }
    Routes4 = @($vpnRoutes4 | Select-Object -ExpandProperty DestinationPrefix -Unique)
    Routes6 = @($vpnRoutes6 | Select-Object -ExpandProperty DestinationPrefix -Unique)
  }
}
$result | ConvertTo-Json -Compress -Depth 4
'''
    ok, out, _ = _ps(script)
    if not ok or not out:
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    gateway_settings = _read_gateway_settings()
    result = []
    for row in data:
        routes4 = _normalize_routes(row.get('Routes4'))
        routes6 = _normalize_routes(row.get('Routes6'))
        fallback = not bool(row.get('SplitTunneling'))
        gateways = gateway_settings.get(
            str(row.get('Name', '')).casefold(), {})
        ipv4_gateway = gateways.get('ipv4_default_gateway', fallback)
        ipv6_gateway = gateways.get('ipv6_default_gateway', fallback)
        result.append({
            'name': row.get('Name', ''),
            'server': row.get('ServerAddress', ''),
            'type': str(row.get('TunnelType', '')),
            'status': str(row.get('ConnectionStatus', '')),
            'ip_address': row.get('IpAddress', '') or '',
            'prefix_length': int(row.get('PrefixLength') or 0),
            'ipv6_address': row.get('Ipv6Address', '') or '',
            'ipv6_prefix_length': int(row.get('Ipv6PrefixLength') or 0),
            'routes': routes4 + routes6,
            'ipv4_routes': routes4,
            'ipv6_routes': routes6,
            'ipv4_default_gateway': ipv4_gateway,
            'ipv6_default_gateway': ipv6_gateway,
            'default_route_ipv4': '0.0.0.0/0' in routes4,
            'default_route_ipv6': '::/0' in routes6,
            'default_route': ('0.0.0.0/0' in routes4 or '::/0' in routes6),
        })
    return result


def analyze_route_conflicts(vpns):
    """分析多条已连接 VPN 的默认路由和网段重叠，返回可直接展示的提示。"""
    connected = [row for row in vpns if row.get('status') == 'Connected']
    conflicts = []
    for family, flag in (
            ('IPv4', 'default_route_ipv4'),
            ('IPv6', 'default_route_ipv6')):
        defaults = [row['name'] for row in connected if row.get(flag)]
        if len(defaults) > 1:
            conflicts.append({
                'type': f'default_route_{family.lower()}',
                'names': defaults,
                'message': f'多个 VPN 同时提供 {family} 默认路由，'
                           'Windows 可能改变当前网络出口',
            })

    networks = []
    for row in connected:
        for prefix in row.get('routes') or []:
            if prefix in ('0.0.0.0/0', '::/0'):
                continue
            try:
                network = ipaddress.ip_network(prefix, strict=False)
            except ValueError:
                continue
            if network.prefixlen < network.max_prefixlen and \
                    not network.is_link_local:
                networks.append((row['name'], network))
    seen = set()
    for index, (left_name, left_net) in enumerate(networks):
        for right_name, right_net in networks[index + 1:]:
            if left_name == right_name or left_net.version != right_net.version \
                    or not left_net.overlaps(right_net):
                continue
            key = tuple(sorted((left_name, right_name)))
            if key in seen:
                continue
            seen.add(key)
            conflicts.append({
                'type': 'subnet_overlap',
                'names': list(key),
                'message': f'{key[0]} 与 {key[1]} 的路由网段存在重叠',
            })
    return conflicts


def _profile_options(vtype, l2tp_psk='', for_update=False):
    """返回与隧道类型匹配的安全选项，避免依赖 Windows 隐式默认值。"""
    options = ['-RememberCredential $true' if for_update
               else '-RememberCredential']
    if vtype == 'Pptp':
        options.append('-AuthenticationMethod MSChapv2')
    elif vtype == 'Ikev2':
        options.append('-AuthenticationMethod Eap')
    elif vtype == 'L2tp':
        options.append('-AuthenticationMethod MSChapv2')
        if l2tp_psk:
            options.append(f'-L2tpPsk {_quote(l2tp_psk)}')
    return options


def add_vpn(name, server, vtype='Automatic', all_user=False,
            l2tp_psk='', ipv4_default_gateway=None,
            ipv6_default_gateway=None):
    if vtype not in TYPES:
        return False, '不支持的 VPN 类型'
    scope = '-AllUsers' if all_user else ''
    parts = [
        'Add-VpnConnection', scope,
        f'-Name {_quote(name)}',
        f'-ServerAddress {_quote(server)}',
        f'-TunnelType {vtype}',
        '-EncryptionLevel Optional',
        *_profile_options(vtype, l2tp_psk),
        '-Force',
    ]
    ok, out, err = _ps(' '.join(part for part in parts if part))
    message = err or out
    if ok and ipv4_default_gateway is not None and \
            ipv6_default_gateway is not None:
        gateway_ok, gateway_msg = set_gateway_settings(
            name, bool(ipv4_default_gateway), bool(ipv6_default_gateway))
        if not gateway_ok:
            return False, f'VPN 已创建，但{gateway_msg}'
        message = (message + '；' if message else '') + gateway_msg
    return ok, message


def set_vpn(name, server=None, vtype=None, l2tp_psk='',
            ipv4_default_gateway=None, ipv6_default_gateway=None):
    if vtype and vtype not in TYPES:
        return False, '不支持的 VPN 类型'
    parts = [f"Set-VpnConnection -Name {_quote(name)}"]
    if server:
        parts.append(f"-ServerAddress {_quote(server)}")
    if vtype:
        parts.append(f"-TunnelType {vtype}")
        parts.extend(_profile_options(vtype, l2tp_psk, for_update=True))
        if l2tp_psk:
            parts.append('-Force')
    else:
        parts.append('-RememberCredential $true')
    ok, out, err = _ps(' '.join(parts))
    message = err or out
    if ok and ipv4_default_gateway is not None and \
            ipv6_default_gateway is not None:
        gateway_ok, gateway_msg = set_gateway_settings(
            name, bool(ipv4_default_gateway), bool(ipv6_default_gateway))
        if not gateway_ok:
            return False, f'基础配置已更新，但{gateway_msg}'
        message = (message + '；' if message else '') + gateway_msg
    return ok, message


def remove_vpn(name):
    ok, out, err = _ps(
        f"Remove-VpnConnection -Name {_quote(name)} -Force")
    return ok, err or out


if __name__ == '__main__':
    for v in list_vpns():
        print(v)
