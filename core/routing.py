# -*- coding: utf-8 -*-
"""Windows 统一域名分流控制层（Mihomo + WinSW）。"""
from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from core import config as cfgmod
from core import subscription_store
from core import vpn_os
from core.routing_support import attach_kill_on_close_job as _attach_kill_on_close_job
from core.routing_support import binary_path as _binary_path
from core.routing_support import provider_preview_error as _provider_preview_error
from core.routing_support import runtime_dir as _runtime_dir
from core.routing_support import sanitize_mihomo_error as _sanitize_mihomo_error
from core.routing_support import sha256_file as _sha256
from core.routing_support import stop_temporary_process as _stop_temporary_process
from core.routing_support import verify_runtime_files
from core import routing_selection as _selection
from core import routing_auto_policy as _auto_policy
from core import routing_rules as _routing_rules
from core import routing_service as _routing_service
from core.routing_tasks import commit_scope
from core.routing_environment import EnvironmentCache, bounded_calls
from core.routing_speedtest import (
    node_healthcheck_path as _node_healthcheck_path,
    test_group as _test_group,
    test_nodes as _test_nodes,
)


SERVICE_ID = 'CXVPNRoutingService'
# 服务数据目录本轮不迁移，保留旧内部路径以维持已安装服务兼容性。
LEGACY_SERVICE_DIR_NAME = 'CXVPNManager\\RoutingService'
MIHOMO_VERSION = 'v1.19.30'
MIHOMO_SHA256 = 'F55B3028D9160BEB9044F21B05DD7405B46524614A19642D6291492F5F985761'
GEOIP_DATABASE = 'Country.mmdb'
GEOIP_DATABASE_SHA256 = '4BF15C30737F7CC2807BCBE1ACE44149B18579BEA3B13BF7BEA935A3F2834052'
WINSW_VERSION = 'v2.12.0'
WINSW_SHA256 = '05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA'
DOMAIN_RE = re.compile(r'^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$', re.I)
WILDCARD_RE = re.compile(r'^[a-z0-9*?](?:[a-z0-9*?.-]{0,251}[a-z0-9*?])?$', re.I)
PROVIDER_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,39}$', re.I)
PROCESS_NAME_RE = re.compile(r'^[^\\/:*?"<>|\x00-\x1f]{1,128}$')
PROXY_STRATEGIES = {'url-test', 'fallback', 'select'}
SELECTION_MODES = {'auto', 'manual'}
TRAFFIC_MODES = {'rule', 'global'}
CAPTURE_MODES = {'system-proxy', 'tun'}
DOWNLOAD_ROUTES = {'auto', 'physical', 'system-proxy', 'custom-proxy'}
DNS_MODES = {'simple', 'advanced'}
DNS_ENHANCED_MODES = {'fake-ip', 'redir-host'}
DEFAULT_FAKE_IP_FILTER = ['+.lan', '+.local', 'localhost.ptlogin2.qq.com']
SIMPLE_DNS_FALLBACK_FILTER_IPCIDR = [
    '240.0.0.0/4',
    '0.0.0.0/32',
]
# 与 Clash Verge Rev v2.5.2 默认测速目标保持一致，避免因目标站点和 TLS
# 握手差异让同一节点在两个客户端中出现不可比较的延迟。
HEALTH_CHECK_URL = 'http://cp.cloudflare.com/generate_204'
CONNECTIVITY_CHECK_URLS = (
    'https://www.gstatic.com/generate_204',
    'https://cp.cloudflare.com/generate_204',
    'https://www.baidu.com/',
)
SUBSCRIPTION_USER_AGENT = 'Clash-Verge'
SUBSCRIPTION_SIZE_LIMIT = 10 * 1024 * 1024
PROVIDER_PREVIEW_TIMEOUT = 25
ELEVATED_ERROR_MESSAGES = {
    'Mihomo controller did not become ready':
        'Mihomo 控制端启动超时，系统服务已回滚',
    'Referenced proxy provider group did not become ready':
        '被引用的代理订阅节点加载超时，系统服务已回滚',
    'Selected manual proxy node did not become ready':
        '手动选择的代理节点未能加载，请更新订阅或重新选择节点',
    'Manual proxy selection could not be confirmed':
        '手动代理节点切换后无法确认，系统服务已回滚',
}


class RoutingError(RuntimeError):
    """可直接向 UI 展示的分流配置错误。"""


class ProviderFetchError(RoutingError):
    """订阅下载或解析未产出节点，可尝试安全的备用下载出口。"""


def default_config():
    return {
        'schema_version': _routing_rules.SCHEMA_VERSION,
        'enabled': False,
        'capture_mode': 'system-proxy',
        'traffic_mode': 'rule',
        'physical_interface': '',
        'proxy_strategy': 'url-test',
        'proxy_providers': [],
        'default_outbound': 'physical',
        'builtin_rule_pack': 'local-direct-v1',
        'system_proxy_bypass': {
            'lan': True,
            'include_cn_direct': False,
            'domains': [],
            'processes': [],
        },
        'mixed_port': 17890,
        'dns_mode': 'simple',
        'dns_enhanced_mode': 'fake-ip',
        'dns_respect_rules': True,
        'dns_servers': ['223.5.5.5', '119.29.29.29'],
        'default_nameserver': ['223.5.5.5', '119.29.29.29'],
        'proxy_server_nameserver': [
            'https://dns.alidns.com/dns-query',
            'https://doh.pub/dns-query',
        ],
        'direct_nameserver': ['223.5.5.5', '119.29.29.29'],
        'fake_ip_range': '198.18.0.1/16',
        'fake_ip_filter': list(DEFAULT_FAKE_IP_FILTER),
        'nameserver_policy': [],
        'controller_port': 19090,
        'controller_secret': '',
        'rules': [],
    }


def verify_runtime():
    verify_runtime_files(_runtime_dir(), {
        'mihomo.exe': MIHOMO_SHA256,
        GEOIP_DATABASE: GEOIP_DATABASE_SHA256,
        'WinSW-x64.exe': WINSW_SHA256,
    }, RoutingError)
    service_binary = _binary_path(_routing_service.SERVICE_BINARY)
    if not os.path.isfile(service_binary):
        raise RoutingError(
            f'统一分流运行时缺失：{_routing_service.SERVICE_BINARY}')
    return True


def _run_powershell_json(script, timeout=20):
    script = ('$OutputEncoding = [Console]::OutputEncoding = '
              '[Text.Encoding]::UTF8; ' + script)
    result = subprocess.run(
        ['powershell', '-NoProfile', '-Command', script],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode != 0:
        raise RoutingError((result.stderr or 'Windows 网络信息读取失败').strip())
    raw = (result.stdout or '').strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RoutingError('Windows 网络信息格式异常') from exc
    return data if isinstance(data, list) else [data]


def list_physical_interfaces():
    """按默认路由优先级返回可用于公网直连的物理接口。"""
    script = r'''
$routes = @(Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
  Sort-Object RouteMetric, InterfaceMetric)
$seen = @{}
$result = foreach ($route in $routes) {
  if ($seen.ContainsKey($route.InterfaceAlias)) { continue }
  $adapter = Get-NetAdapter -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue
  if (-not $adapter -or $adapter.Status -ne 'Up') { continue }
  $text = "$($route.InterfaceAlias) $($adapter.InterfaceDescription)"
  if ($text -match '(?i)vpn|wintun|wireguard|mihomo|clash|sing-box|tap|loopback') { continue }
  $seen[$route.InterfaceAlias] = $true
  [pscustomobject]@{
    name = [string]$route.InterfaceAlias
    description = [string]$adapter.InterfaceDescription
    index = [int]$route.InterfaceIndex
    metric = [int]($route.RouteMetric + $route.InterfaceMetric)
  }
}
@($result) | ConvertTo-Json -Compress
'''
    return _run_powershell_json(script)


def list_tun_conflicts():
    """查找正在承载默认路由的其他 TUN，避免两个全局 TUN 相互抢路由。"""
    script = rf'''
$routes = @(Get-NetRoute -DestinationPrefix '0.0.0.0/0','::/0' -ErrorAction SilentlyContinue)
$result = foreach ($route in $routes) {{
  $adapter = Get-NetAdapter -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue
  if (-not $adapter -or $adapter.Status -ne 'Up') {{ continue }}
  $text = "$($route.InterfaceAlias) $($adapter.InterfaceDescription)"
  if ($text -match '(?i)wintun|wireguard|mihomo|clash|sing-box|tun' -and
      $route.InterfaceAlias -ne 'CXVPN-TUN') {{
    [pscustomobject]@{{
      name = [string]$route.InterfaceAlias
      description = [string]$adapter.InterfaceDescription
      route = [string]$route.DestinationPrefix
    }}
  }}
}}
@($result | Sort-Object name,route -Unique) | ConvertTo-Json -Compress
'''
    return _run_powershell_json(script)


def _normalize_domain(value, match_type):
    value = str(value or '').strip().lower().rstrip('.')
    if '://' in value or '/' in value or ':' in value:
        raise RoutingError(f'域名规则不能包含协议、端口或路径：{value}')
    if match_type == 'suffix':
        value = value.removeprefix('*.').removeprefix('.')
    try:
        value = value.encode('idna').decode('ascii')
    except UnicodeError as exc:
        raise RoutingError(f'域名格式无效：{value}') from exc
    pattern = WILDCARD_RE if match_type == 'wildcard' else DOMAIN_RE
    if not value or not pattern.fullmatch(value):
        raise RoutingError(f'域名格式无效：{value or "（空）"}')
    if match_type != 'wildcard' and ('*' in value or '?' in value):
        raise RoutingError(f'{match_type} 规则不允许通配符：{value}')
    return value


def _parse_http_proxy_url(value, label='订阅下载代理', local_only=False):
    raw = str(value or '').strip()
    if not raw:
        return ''
    if '://' not in raw:
        raw = 'http://' + raw
    if len(raw) > 2048 or any(ord(char) < 32 for char in raw):
        raise RoutingError(f'{label}地址无效')
    parsed = urllib.parse.urlparse(raw)
    try:
        port = parsed.port
    except ValueError as exc:
        raise RoutingError(f'{label}端口无效') from exc
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or
            not port or parsed.path not in {'', '/'} or parsed.query or
            parsed.fragment):
        raise RoutingError(f'{label}必须是有效的 HTTP/HTTPS 代理地址')
    if local_only and (
            parsed.scheme != 'http' or parsed.hostname.lower() not in {
                '127.0.0.1', '::1', 'localhost'} or
            parsed.username is not None or parsed.password is not None):
        raise RoutingError(f'{label}仅支持不含账号密码的本机 HTTP 代理')
    return urllib.parse.urlunparse((
        parsed.scheme, parsed.netloc, '', '', '', ''))


def windows_system_proxy():
    """读取 Windows 手动系统代理；PAC/WPAD 不在此处隐式执行。"""
    if os.name != 'nt':
        return ''
    try:
        import winreg
        key_path = r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            enabled = int(winreg.QueryValueEx(key, 'ProxyEnable')[0] or 0)
            server = str(winreg.QueryValueEx(key, 'ProxyServer')[0] or '').strip()
        if not enabled or not server:
            return ''
        if ';' in server or '=' in server:
            values = {}
            for item in server.split(';'):
                if '=' not in item:
                    continue
                key, candidate = item.split('=', 1)
                values[key.strip().lower()] = candidate.strip()
            server = values.get('https') or values.get('http') or ''
        return _parse_http_proxy_url(server, 'Windows 系统代理') if server else ''
    except (OSError, ValueError, RoutingError):
        return ''


def _proxy_display(value):
    if not value:
        return ''
    parsed = urllib.parse.urlparse(value)
    host = parsed.hostname or ''
    if ':' in host and not host.startswith('['):
        host = f'[{host}]'
    return f'{host}:{parsed.port}' if parsed.port else host


def normalize_config(value):
    source = value if isinstance(value, dict) else {}
    result = default_config()
    try:
        schema_version = int(source.get('schema_version') or 1)
    except (TypeError, ValueError) as exc:
        raise RoutingError('routing schema 版本无效') from exc
    if schema_version < 1 or schema_version > _routing_rules.SCHEMA_VERSION:
        raise RoutingError(f'不支持的 routing schema 版本：{schema_version}')
    # 未迁移的旧对象语义固定为 TUN；config.load 会持久化同一保守迁移。
    result['schema_version'] = _routing_rules.SCHEMA_VERSION
    capture_mode = str(source.get('capture_mode') or (
        'tun' if schema_version < 2 else 'system-proxy')).strip().lower()
    if capture_mode not in CAPTURE_MODES:
        raise RoutingError('网络接管方式无效')
    result['capture_mode'] = capture_mode
    result['enabled'] = bool(source.get('enabled', False))
    traffic_mode = str(source.get('traffic_mode') or 'rule').strip().lower()
    if traffic_mode not in TRAFFIC_MODES:
        raise RoutingError('代理连接模式无效')
    result['traffic_mode'] = traffic_mode
    result['physical_interface'] = str(source.get('physical_interface') or '').strip()
    proxy_strategy = str(source.get('proxy_strategy') or 'url-test').strip().lower()
    if proxy_strategy not in PROXY_STRATEGIES:
        raise RoutingError('全部订阅的节点策略无效')
    result['proxy_strategy'] = proxy_strategy

    providers = source.get('proxy_providers')
    if not isinstance(providers, list):
        providers = []
    # 兼容首版单订阅字段；保存应用后会自然迁移到 proxy_providers。
    legacy_url = str(source.get('proxy_provider_url') or '').strip()
    if not providers and legacy_url:
        providers = [{
            'id': 'default', 'name': '默认订阅', 'url': legacy_url,
            'enabled': True, 'strategy': proxy_strategy,
            'interval': source.get('proxy_provider_interval', 3600),
        }]
    if len(providers) > 16:
        raise RoutingError('代理订阅最多配置 16 个')
    normalized_providers = []
    provider_ids = set()
    provider_names = set()
    for index, provider in enumerate(providers):
        if not isinstance(provider, dict):
            raise RoutingError(f'第 {index + 1} 个代理订阅配置无效')
        provider_id = str(provider.get('id') or f'provider-{index + 1}').strip().lower()
        if not PROVIDER_ID_RE.fullmatch(provider_id):
            raise RoutingError(f'第 {index + 1} 个代理订阅 ID 无效')
        if provider_id in provider_ids:
            raise RoutingError(f'代理订阅 ID 重复：{provider_id}')
        name = str(provider.get('name') or f'订阅 {index + 1}').strip()
        if not name or len(name) > 40:
            raise RoutingError(f'第 {index + 1} 个代理订阅名称必须为 1～40 个字符')
        if any(ord(char) < 32 for char in name):
            raise RoutingError(f'第 {index + 1} 个代理订阅名称不能包含换行或控制字符')
        if name.casefold() in provider_names:
            raise RoutingError(f'代理订阅名称重复：{name}')
        url = str(provider.get('url') or '').strip()
        if len(url) > 4096:
            raise RoutingError(f'代理订阅“{name}”的 URL 过长')
        strategy = str(provider.get('strategy') or 'url-test').strip().lower()
        if strategy not in PROXY_STRATEGIES:
            raise RoutingError(f'代理订阅“{name}”的节点策略无效')
        raw_selection_mode = provider.get('selection_mode')
        selection_mode = str(raw_selection_mode or (
            'manual' if strategy == 'select' else 'auto')).strip().lower()
        if selection_mode not in SELECTION_MODES:
            raise RoutingError(f'代理订阅“{name}”的节点选择模式无效')
        if selection_mode == 'auto' and strategy == 'select':
            strategy = 'url-test'
        selected_node = str(provider.get('selected_node') or '').strip()
        if (len(selected_node) > 512 or
                any(ord(char) < 32 for char in selected_node)):
            raise RoutingError(f'代理订阅“{name}”的首选节点无效')
        try:
            interval = int(provider.get('interval') or 3600)
        except (TypeError, ValueError):
            interval = 3600
        include_filter = str(provider.get('filter') or '').strip()
        exclude_filter = str(provider.get('exclude_filter') or '').strip()
        if len(include_filter) > 200 or len(exclude_filter) > 200:
            raise RoutingError(f'代理订阅“{name}”的节点筛选表达式过长')
        download_route = str(
            provider.get('download_route') or 'auto').strip().lower()
        if download_route not in DOWNLOAD_ROUTES:
            raise RoutingError(f'代理订阅“{name}”的下载出口无效')
        download_proxy = str(provider.get('download_proxy') or '').strip()
        if download_route == 'custom-proxy':
            if not download_proxy:
                raise RoutingError(f'代理订阅“{name}”必须填写自定义下载代理')
            download_proxy = _parse_http_proxy_url(
                download_proxy, f'代理订阅“{name}”的自定义下载代理',
                local_only=True)
        elif download_proxy:
            download_proxy = _parse_http_proxy_url(
                download_proxy, f'代理订阅“{name}”的自定义下载代理')
        user_agent = str(
            provider.get('user_agent') or SUBSCRIPTION_USER_AGENT).strip()
        if (not user_agent or len(user_agent) > 200 or
                any(ord(char) < 32 for char in user_agent)):
            raise RoutingError(f'代理订阅“{name}”的 User-Agent 无效')
        normalized_providers.append({
            'id': provider_id,
            'name': name,
            'url': url,
            'enabled': bool(provider.get('enabled', True)),
            'strategy': strategy,
            'selection_mode': selection_mode,
            'selected_node': selected_node,
            'auto_policy': _auto_policy.normalize_policy(
                provider.get('auto_policy'), name, RoutingError),
            'auto_update': bool(provider.get('auto_update', False)),
            'interval': min(86400, max(300, interval)),
            'filter': include_filter,
            'exclude_filter': exclude_filter,
            'download_route': download_route,
            'download_proxy': download_proxy,
            'user_agent': user_agent,
        })
        provider_ids.add(provider_id)
        provider_names.add(name.casefold())
    result['proxy_providers'] = normalized_providers
    result['default_outbound'] = str(source.get('default_outbound') or 'physical').strip()
    if result['default_outbound'].startswith('proxy:'):
        result['default_outbound'] = 'proxy:' + result['default_outbound'][6:].lower()
    default_outbound = result['default_outbound']
    if (default_outbound not in {'physical', 'proxy', 'block'} and
            not default_outbound.startswith(('vpn:', 'proxy:'))):
        raise RoutingError('未命中规则的默认出口无效')
    if default_outbound.startswith('vpn:') and not default_outbound[4:].strip():
        raise RoutingError('未命中规则的默认出口未选择 VPN')
    builtin_rule_pack = str(source.get('builtin_rule_pack') or (
        'off' if schema_version < 2 else 'local-direct-v1')).strip().lower()
    if builtin_rule_pack not in _routing_rules.BUILTIN_PACKS:
        raise RoutingError('内置规则包无效')
    result['builtin_rule_pack'] = builtin_rule_pack
    raw_bypass = source.get('system_proxy_bypass')
    raw_bypass = raw_bypass if isinstance(raw_bypass, dict) else {}

    def normalize_bypass_list(field, maximum):
        values = raw_bypass.get(field)
        if isinstance(values, str):
            values = re.split(r'[,\r\n]+', values)
        if not isinstance(values, list):
            values = []
        cleaned = []
        seen_values = set()
        for raw in values:
            candidate = str(raw or '').strip()
            if not candidate:
                continue
            if field == 'domains':
                candidate = _normalize_domain(candidate.lstrip('.'), 'suffix')
            elif not PROCESS_NAME_RE.fullmatch(candidate):
                raise RoutingError(f'绕过进程名称无效：{candidate}')
            key = candidate.casefold()
            if key not in seen_values:
                cleaned.append(candidate)
                seen_values.add(key)
        if len(cleaned) > maximum:
            label = '域名' if field == 'domains' else '进程'
            raise RoutingError(f'系统代理绕过{label}最多配置 {maximum} 个')
        return cleaned

    result['system_proxy_bypass'] = {
        # 回环与局域网是防止本机服务和内网设备被代理误伤的保护项，不能关闭。
        'lan': True,
        'include_cn_direct': bool(raw_bypass.get('include_cn_direct', False)),
        'domains': normalize_bypass_list('domains', 100),
        'processes': normalize_bypass_list('processes', 100),
    }
    try:
        mixed_port = int(source.get('mixed_port') or 17890)
    except (TypeError, ValueError) as exc:
        raise RoutingError('本地 mixed-port 必须是数字') from exc
    if not 1024 <= mixed_port <= 65535:
        raise RoutingError('本地 mixed-port 必须在 1024～65535 之间')
    result['mixed_port'] = mixed_port
    try:
        port = int(source.get('controller_port') or 19090)
    except (TypeError, ValueError) as exc:
        raise RoutingError('控制端口必须是数字') from exc
    if not 1024 <= port <= 65535:
        raise RoutingError('控制端口必须在 1024～65535 之间')
    result['controller_port'] = port
    if mixed_port == port:
        raise RoutingError('本地 mixed-port 不能与 Controller 端口重复')
    secret = str(source.get('controller_secret') or '').strip()
    result['controller_secret'] = secret or secrets.token_urlsafe(24)

    dns_mode = str(source.get('dns_mode') or (
        'advanced' if schema_version < 4 else 'simple')).strip().lower()
    if dns_mode not in DNS_MODES:
        raise RoutingError('DNS 配置模式无效')
    result['dns_mode'] = dns_mode
    enhanced_mode = str(
        source.get('dns_enhanced_mode') or 'fake-ip').strip().lower()
    if enhanced_mode not in DNS_ENHANCED_MODES:
        raise RoutingError('DNS enhanced-mode 无效')
    result['dns_enhanced_mode'] = enhanced_mode
    result['dns_respect_rules'] = bool(source.get('dns_respect_rules', False))

    def normalize_dns_value(value, label):
        candidate = str(value or '').strip()
        if (not candidate or len(candidate) > 512 or
                any(ord(char) < 32 for char in candidate)):
            raise RoutingError(f'{label}包含无效 DNS 地址')
        if candidate == 'system':
            return candidate
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            if '://' not in candidate:
                try:
                    return _normalize_domain(candidate, 'exact')
                except RoutingError:
                    pass
        parsed = urllib.parse.urlparse(candidate)
        if (parsed.scheme not in {'https', 'tls', 'quic', 'dhcp', 'udp', 'tcp'} or
                not parsed.hostname or parsed.username or parsed.password or
                parsed.query or parsed.fragment):
            raise RoutingError(
                f'{label}仅支持 IP、system 或 https/tls/quic/dhcp/udp/tcp DNS 地址')
        return candidate

    def normalize_dns_value_list(values, label):
        cleaned = []
        seen_dns = set()
        for item in values:
            if not str(item).strip():
                continue
            candidate = normalize_dns_value(item, label)
            if candidate.casefold() not in seen_dns:
                cleaned.append(candidate)
                seen_dns.add(candidate.casefold())
        if len(cleaned) > 8:
            raise RoutingError(f'{label}最多配置 8 个 DNS 地址')
        return cleaned

    def normalize_dns(field, fallback, label=None):
        values = source.get(field)
        if values is None and field != 'dns_servers':
            values = source.get('dns_servers')
        if isinstance(values, str):
            values = re.split(r'[,\s]+', values)
        if not isinstance(values, list):
            values = []
        cleaned = normalize_dns_value_list(values, label or field)
        return cleaned or list(fallback)

    result['dns_servers'] = normalize_dns(
        'dns_servers', default_config()['dns_servers'])
    result['default_nameserver'] = normalize_dns(
        'default_nameserver', default_config()['default_nameserver'])
    result['proxy_server_nameserver'] = normalize_dns(
        'proxy_server_nameserver', default_config()['proxy_server_nameserver'])
    result['direct_nameserver'] = normalize_dns(
        'direct_nameserver', default_config()['direct_nameserver'])
    fake_ip_range = str(
        source.get('fake_ip_range') or '198.18.0.1/16').strip()
    try:
        fake_network = ipaddress.ip_network(fake_ip_range, strict=False)
        reserved = ipaddress.ip_network('198.18.0.0/15')
    except ValueError as exc:
        raise RoutingError('fake-IP 地址池必须是有效 IPv4 CIDR') from exc
    if (fake_network.version != 4 or not fake_network.subnet_of(reserved) or
            not 16 <= fake_network.prefixlen <= 30):
        raise RoutingError('fake-IP 地址池必须位于 198.18.0.0/15，前缀长度为 16～30')
    result['fake_ip_range'] = str(fake_network)

    raw_filter = source.get('fake_ip_filter')
    if isinstance(raw_filter, str):
        raw_filter = re.split(r'[,\r\n]+', raw_filter)
    if not isinstance(raw_filter, list):
        raw_filter = list(DEFAULT_FAKE_IP_FILTER)
    filters = []
    seen_filters = set()
    for raw in raw_filter:
        candidate = str(raw or '').strip().lower()
        if not candidate:
            continue
        check = candidate[2:] if candidate.startswith(('+.', '*.')) else candidate
        _normalize_domain(check, 'suffix')
        if candidate not in seen_filters:
            filters.append(candidate)
            seen_filters.add(candidate)
    if len(filters) > 100:
        raise RoutingError('fake-IP 排除域名最多配置 100 个')
    result['fake_ip_filter'] = filters

    policies = source.get('nameserver_policy')
    if not isinstance(policies, list):
        policies = []
    normalized_policies = []
    policy_domains = set()
    for index, policy in enumerate(policies):
        if not isinstance(policy, dict):
            raise RoutingError(f'第 {index + 1} 条 DNS 策略无效')
        raw_domain = str(policy.get('domain') or '').strip().lower()
        domain = _normalize_domain(
            raw_domain[2:] if raw_domain.startswith(('+.', '*.')) else raw_domain,
            'suffix')
        if domain in policy_domains:
            raise RoutingError(f'DNS 策略域名重复：{domain}')
        servers = policy.get('servers')
        if isinstance(servers, str):
            servers = re.split(r'[,\s]+', servers)
        if not isinstance(servers, list) or not any(str(item).strip() for item in servers):
            raise RoutingError(f'DNS 策略“{domain}”必须配置服务器')
        normalized_policies.append({
            'domain': domain,
            'servers': normalize_dns_value_list(
                servers, f'DNS 策略“{domain}”'),
        })
        policy_domains.add(domain)
    if len(normalized_policies) > 100:
        raise RoutingError('DNS 策略最多配置 100 条')
    result['nameserver_policy'] = normalized_policies

    normalized_rules = []
    seen = set()
    rules = source.get('rules') if isinstance(source.get('rules'), list) else []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue
        enabled = bool(rule.get('enabled', True))
        match_type = str(rule.get('match_type') or 'suffix').strip().lower()
        if match_type not in {'exact', 'suffix', 'wildcard'}:
            raise RoutingError(f'第 {index + 1} 条规则的匹配方式无效')
        domain = _normalize_domain(rule.get('domain'), match_type)
        outbound = str(rule.get('outbound') or 'physical').strip()
        if outbound.startswith('proxy:'):
            outbound = 'proxy:' + outbound[6:].lower()
        if (outbound not in {'physical', 'proxy', 'block'} and
                not outbound.startswith(('vpn:', 'proxy:'))):
            raise RoutingError(f'第 {index + 1} 条规则的出口无效')
        if outbound.startswith('vpn:') and not outbound[4:].strip():
            raise RoutingError(f'第 {index + 1} 条规则未选择 VPN')
        key = (match_type, domain)
        if key in seen:
            raise RoutingError(f'存在重复域名规则：{domain}')
        seen.add(key)
        normalized_rules.append({
            'id': str(rule.get('id') or f'rule-{index + 1}'),
            'enabled': enabled,
            'match_type': match_type,
            'domain': domain,
            'outbound': outbound,
        })
    result['rules'] = normalized_rules
    enabled_provider_ids = {
        item['id'] for item in result['proxy_providers'] if item['enabled']}
    outbound_values = [result['default_outbound']]
    outbound_values.extend(
        rule['outbound'] for rule in result['rules'] if rule['enabled'])
    for outbound in outbound_values:
        if outbound == 'proxy' and not enabled_provider_ids:
            raise RoutingError('聚合代理出口至少需要一个已启用的代理订阅')
        if outbound.startswith('proxy:'):
            provider_id = outbound[6:]
            if provider_id not in enabled_provider_ids:
                raise RoutingError(f'出口引用了不存在或未启用的代理订阅：{provider_id}')
    return result


def validate_auto_policy_nodes(provider):
    snapshot = subscription_store.load_node_snapshot(provider)
    _auto_policy.validate_preferred_nodes(
        provider, snapshot.get('nodes') or [], RoutingError)


def _active_rules(config):
    if config.get('traffic_mode') == 'global':
        return []
    return [rule for rule in config.get('rules', []) if rule.get('enabled', True)]


def _builtin_rules(pack_id):
    try:
        return _routing_rules.rules_for(pack_id)
    except _routing_rules.RulePackError as exc:
        raise RoutingError(str(exc)) from exc


def _bypass_rules(config):
    """生成始终优先于用户规则和 MATCH 的安全直连规则。"""
    bypass = config.get('system_proxy_bypass') or {}
    # 局域网和回环由 Windows ProxyOverride 在系统代理入口保护；这里只生成
    # 必须经过 Mihomo 判断的用户域名与进程规则，避免和内置包重复。
    rules = []
    rules.extend(
        f'DOMAIN-SUFFIX,{domain},PHYSICAL'
        for domain in bypass.get('domains', []))
    rules.extend(
        f'PROCESS-NAME,{process},PHYSICAL'
        for process in bypass.get('processes', []))
    return rules


def system_proxy_bypass_domains(config):
    """合并手动域名与国内规则包，供 Windows ProxyOverride 使用。"""
    bypass = config.get('system_proxy_bypass') or {}
    domains = list(bypass.get('domains') or [])
    if bypass.get('include_cn_direct'):
        try:
            domains.extend(_routing_rules.cn_direct_suffixes())
        except _routing_rules.RulePackError as exc:
            raise RoutingError(str(exc)) from exc
    result = []
    seen = set()
    for domain in domains:
        key = domain.casefold()
        if key not in seen:
            result.append(domain)
            seen.add(key)
    return result


def _effective_rules(config):
    rules = []
    seen = set()

    def append(raw):
        key = raw.casefold()
        if key not in seen:
            rules.append(raw)
            seen.add(key)

    for raw in _bypass_rules(config):
        append(raw)
    if config.get('traffic_mode') != 'global':
        rule_kind = {'exact': 'DOMAIN', 'suffix': 'DOMAIN-SUFFIX',
                     'wildcard': 'DOMAIN-WILDCARD'}
        for rule in _active_rules(config):
            append(
                f'{rule_kind[rule["match_type"]]},{rule["domain"]},'
                f'{rule["outbound"]}')
        for raw in _builtin_rules(config['builtin_rule_pack']):
            append(raw)
    if (config.get('system_proxy_bypass') or {}).get('include_cn_direct'):
        # 已知国内域名在 Windows ProxyOverride 层直接绕过；仍进入 Mihomo 的
        # 未知域名在用户规则和规则包之后按目标 IP 兜底，显式规则始终优先。
        append('GEOIP,CN,PHYSICAL')
    append(f'MATCH,{config["default_outbound"]}')
    return rules


def _target_vpns(config):
    values = [config.get('default_outbound', '')]
    values.extend(rule['outbound'] for rule in _active_rules(config))
    return sorted({value[4:] for value in values if value.startswith('vpn:')})


def _uses_proxy(config):
    values = [config.get('default_outbound', '')]
    values.extend(rule.get('outbound', '') for rule in _active_rules(config))
    return any(value == 'proxy' or value.startswith('proxy:') for value in values)


def _target_provider_ids(config):
    values = [config.get('default_outbound', '')]
    values.extend(rule.get('outbound', '') for rule in _active_rules(config))
    return {value[6:] for value in values if value.startswith('proxy:')}


def _referenced_provider_ids(config):
    """返回实际承载路由流量的 provider；聚合出口引用全部启用订阅。"""
    values = [config.get('default_outbound', '')]
    values.extend(rule.get('outbound', '') for rule in _active_rules(config))
    targets = _target_provider_ids(config)
    if 'proxy' in values:
        targets.update(item['id'] for item in config.get('proxy_providers', [])
                       if item.get('enabled'))
    return targets


def explain_domain(value, domain, vpns=None):
    """解释一个域名最终命中的规则和出口，不修改系统状态。"""
    config = normalize_config(value)
    normalized_domain = _normalize_domain(domain, 'exact')
    bypass_domains = (config['system_proxy_bypass']['domains']
                      if config['capture_mode'] != 'system-proxy'
                      else system_proxy_bypass_domains(config))
    bypass_match = next((item for item in bypass_domains
                         if normalized_domain == item or
                         normalized_domain.endswith('.' + item)), None)
    if not bypass_match and config['capture_mode'] == 'system-proxy':
        for raw in _builtin_rules('local-direct-v1'):
            parts = raw.split(',')
            if parts[0] == 'DOMAIN-SUFFIX' and (
                    normalized_domain == parts[1] or
                    normalized_domain.endswith('.' + parts[1])):
                bypass_match = parts[1]
                break
    matched = None
    for index, rule in enumerate([] if bypass_match else _active_rules(config), start=1):
        pattern = rule['domain']
        match_type = rule['match_type']
        if match_type == 'exact':
            hit = normalized_domain == pattern
        elif match_type == 'suffix':
            hit = (normalized_domain == pattern or
                   normalized_domain.endswith('.' + pattern))
        else:
            hit = fnmatch.fnmatchcase(normalized_domain, pattern)
        if hit:
            matched = (index, rule)
            break

    builtin_match = None
    if not bypass_match and not matched and config['traffic_mode'] != 'global':
        for raw in _builtin_rules(config['builtin_rule_pack']):
            parts = raw.split(',')
            if parts[0] == 'DOMAIN-SUFFIX' and (
                    normalized_domain == parts[1] or
                    normalized_domain.endswith('.' + parts[1])):
                builtin_match = parts
                break
    outbound = ('physical' if bypass_match else matched[1]['outbound'] if matched else
                'physical' if builtin_match else config['default_outbound'])
    providers = {item['id']: item for item in config['proxy_providers']}
    vpn_rows = vpns if vpns is not None else []
    vpn_profiles = {str(item.get('name') or ''): item for item in vpn_rows}
    available = True
    if outbound == 'physical':
        outbound_name = config['physical_interface'] or '物理网络'
        detail = '通过已选物理接口直连'
    elif outbound == 'proxy':
        enabled = [item for item in config['proxy_providers'] if item['enabled']]
        outbound_name = '聚合代理池'
        detail = f'{len(enabled)} 个已启用订阅，策略：{config["proxy_strategy"]}'
        available = bool(enabled)
    elif outbound.startswith('proxy:'):
        provider = providers.get(outbound[6:])
        outbound_name = provider['name'] if provider else outbound[6:]
        available = bool(provider and provider['enabled'])
        detail = (f'独立订阅代理组，策略：{provider["strategy"]}' if provider
                  else '引用的订阅不存在')
    elif outbound.startswith('vpn:'):
        name = outbound[4:]
        profile = vpn_profiles.get(name)
        outbound_name = name
        available = bool(profile and profile.get('status') == 'Connected')
        detail = ('Windows VPN 已连接' if available else
                  'Windows VPN 当前未连接或不存在')
    else:
        outbound_name = '阻止访问'
        detail = '请求将在本机被拒绝'

    runtime_geoip_fallback = bool(
        not bypass_match and not matched and not builtin_match and
        (config.get('system_proxy_bypass') or {}).get('include_cn_direct'))
    return {
        'domain': normalized_domain,
        'matched': bool(bypass_match or matched or builtin_match),
        'source': ('bypass' if bypass_match else 'user' if matched else
                   'builtin' if builtin_match else 'default'),
        'rule_index': matched[0] if matched else 0,
        'match_type': (matched[1]['match_type'] if matched else 'suffix'
                       if bypass_match or builtin_match else 'default'),
        'rule_domain': (matched[1]['domain'] if matched else bypass_match or
                        (builtin_match[1] if builtin_match else '')),
        'outbound': outbound,
        'outbound_name': outbound_name,
        'detail': detail,
        'available': available,
        'runtime_geoip_fallback': runtime_geoip_fallback,
        'runtime_detail': (
            f'解析为中国 IP 时通过{config["physical_interface"] or "物理网络"}直连；'
            f'否则使用默认出口 {outbound_name}'
            if runtime_geoip_fallback else ''),
    }


def validate_environment(config, vpns=None, interfaces=None, conflicts=None):
    vpns = vpn_os.list_vpns() if vpns is None else vpns
    interfaces = list_physical_interfaces() if interfaces is None else interfaces
    conflicts = list_tun_conflicts() if conflicts is None else conflicts
    warnings = []
    if config.get('capture_mode') == 'tun' and conflicts:
        names = '、'.join(sorted({item.get('name', '') for item in conflicts}))
        raise RoutingError(f'检测到其他 TUN 正在接管默认路由（{names}），请先关闭其 TUN 模式')
    available = {row.get('name'): row for row in interfaces}
    selected = config.get('physical_interface')
    if not selected:
        if not interfaces:
            raise RoutingError('未找到可用的物理默认网络接口')
        selected = interfaces[0].get('name', '')
        config['physical_interface'] = selected
    elif selected not in available:
        raise RoutingError(f'物理网络接口不存在或未连接：{selected}')

    profiles = {row.get('name'): row for row in vpns}
    for name in _target_vpns(config):
        profile = profiles.get(name)
        if not profile:
            raise RoutingError(f'域名规则引用了不存在的 Windows VPN：{name}')
        if profile.get('ipv4_default_gateway', True) or profile.get('ipv6_default_gateway', True):
            raise RoutingError(f'VPN“{name}”仍启用了远程默认网关，请先在 VPN 配置中关闭 IPv4 和 IPv6 默认网关')
        if profile.get('status') != 'Connected':
            warnings.append(f'VPN“{name}”当前未连接，命中它的请求会失败，连接后无需重启分流')

    if _uses_proxy(config):
        enabled = [item for item in config['proxy_providers'] if item['enabled']]
        if not enabled:
            raise RoutingError('使用代理出口时必须至少启用一个代理订阅')
    for provider in config['proxy_providers']:
        if not provider['enabled']:
            continue
        parsed = urllib.parse.urlparse(provider['url'])
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
            raise RoutingError(f'代理订阅“{provider["name"]}”必须填写有效的 http/https URL')
    return warnings


def _resolve_vpn_server_routes(vpns, selected_names):
    routes, warnings, lookups = set(), [], {}
    for profile in vpns:
        if profile.get('name') not in selected_names:
            continue
        server = str(profile.get('server') or '').strip()
        if not server:
            continue
        try:
            address = ipaddress.ip_address(server)
            routes.add(f'{address}/{32 if address.version == 4 else 128}')
        except ValueError:
            lookups[server] = lambda server=server: socket.getaddrinfo(server, None, type=socket.SOCK_STREAM)
    results, errors = bounded_calls(lookups, 5)
    for rows in results.values():
        for info in rows:
            address = ipaddress.ip_address(info[4][0])
            routes.add(f'{address}/{32 if address.version == 4 else 128}')
    for server in errors:
        warnings.append(f'未能在期限内预解析 VPN 服务器“{server}”，VPN 重连时可能需要暂停统一分流')
    return sorted(routes), warnings


def _subscription_proxy_node(name, proxy_url, physical_interface=''):
    parsed = urllib.parse.urlparse(proxy_url)
    node = {
        'name': name,
        'type': 'http',
        'server': parsed.hostname,
        'port': parsed.port,
    }
    if parsed.scheme == 'https':
        node['tls'] = True
    if parsed.username is not None:
        node['username'] = urllib.parse.unquote(parsed.username)
    if parsed.password is not None:
        node['password'] = urllib.parse.unquote(parsed.password)
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == 'localhost'
    if physical_interface and not loopback:
        node['interface-name'] = physical_interface
    return node


def _provider_download_route(provider, system_proxy=''):
    mode = provider.get('download_route', 'auto')
    if mode == 'physical':
        return {'mode': mode, 'target': 'PHYSICAL', 'proxy_url': ''}
    if mode == 'custom-proxy':
        return {
            'mode': mode,
            'target': f'SUBSCRIPTION-UPSTREAM-{provider["id"]}',
            'proxy_url': provider['download_proxy'],
        }
    if mode == 'system-proxy':
        if not system_proxy:
            raise RoutingError(
                f'代理订阅“{provider["name"]}”要求使用 Windows 系统代理，但当前未检测到可用的手动代理')
        return {
            'mode': mode,
            'target': f'SUBSCRIPTION-UPSTREAM-{provider["id"]}',
            'proxy_url': system_proxy,
        }
    return {
        'mode': 'auto',
        'target': f'SUBSCRIPTION-UPDATE-{provider["id"]}',
        'proxy_url': '',
        'self_bootstrap': True,
    }


def _preview_provider_download_route(provider, system_proxy='',
                                     cache_available=False):
    """首次获取不能让订阅下载出口依赖尚不存在的同源节点。"""
    download = _provider_download_route(provider, system_proxy)
    if download.get('self_bootstrap') and not cache_available:
        return {
            'mode': 'auto',
            'target': 'PHYSICAL',
            'proxy_url': '',
            'self_bootstrap': False,
        }
    return download


def build_mihomo_config(config, vpns, excluded_routes=None, system_proxy='',
                        standby=False):
    """生成 JSON（YAML 的合法子集），避免额外引入 YAML 依赖。"""
    targets = [] if standby else _target_vpns(config)
    proxy_names = {name: f'VPN-{index + 1}' for index, name in enumerate(targets)}
    proxies = [{
        'name': 'PHYSICAL', 'type': 'direct', 'udp': True,
        'interface-name': config['physical_interface'],
    }]
    for name in targets:
        proxies.append({
            'name': proxy_names[name], 'type': 'direct', 'udp': True,
            'interface-name': name,
        })

    enabled_providers = [
        item for item in config.get('proxy_providers', []) if item['enabled']]
    provider_keys = {
        item['id']: f'provider-{item["id"]}' for item in enabled_providers}
    provider_groups = {
        item['id']: f'PROXY-{item["id"]}' for item in enabled_providers}

    def target_name(value):
        if value == 'physical':
            return 'PHYSICAL'
        if value == 'proxy':
            return 'PROXY'
        if value.startswith('proxy:'):
            return provider_groups[value[6:]]
        if value == 'block':
            return 'REJECT'
        return proxy_names[value[4:]]

    dns_source = config if config.get('dns_mode') == 'advanced' else default_config()
    dns_config = {
        'enable': True,
        'ipv6': True,
        'enhanced-mode': dns_source['dns_enhanced_mode'],
        'use-hosts': True,
        'respect-rules': bool(dns_source.get('dns_respect_rules')),
        'nameserver': dns_source['dns_servers'],
        'default-nameserver': dns_source['default_nameserver'],
        'proxy-server-nameserver': dns_source['proxy_server_nameserver'],
        'direct-nameserver': dns_source['direct_nameserver'],
    }
    if dns_source['dns_enhanced_mode'] == 'fake-ip':
        dns_config.update({
            'fake-ip-range': dns_source['fake_ip_range'],
            'fake-ip-range6': 'fdfe:dcba:9876::1/64',
            'fake-ip-filter': dns_source['fake_ip_filter'],
        })
    if dns_source.get('nameserver_policy'):
        dns_config['nameserver-policy'] = {
            f'+.{item["domain"]}': item['servers']
            for item in dns_source['nameserver_policy']
        }
    if config.get('dns_mode') != 'advanced':
        dns_config.update({
            'fallback': list(dns_source['proxy_server_nameserver']),
            'fallback-filter': {
                'geoip': True,
                'geoip-code': 'CN',
                'ipcidr': list(SIMPLE_DNS_FALLBACK_FILTER_IPCIDR),
            },
        })

    generated = {
        'mixed-port': (config['mixed_port']
                       if standby or config['capture_mode'] == 'system-proxy'
                       else 0),
        'allow-lan': False,
        'bind-address': '127.0.0.1',
        'mode': 'rule',
        'unified-delay': True,
        'tcp-concurrent': True,
        'log-level': 'info',
        'ipv6': True,
        'external-controller': f'127.0.0.1:{config["controller_port"]}',
        # Controller 只监听回环且需要随机 secret；浏览器不再直连，
        # 因此无需开放任何 CORS Origin。
        'secret': config['controller_secret'],
        'profile': {'store-selected': True, 'store-fake-ip': True},
        'find-process-mode': 'strict',
        'dns': dns_config,
        'tun': {
            'enable': not standby and config['capture_mode'] == 'tun',
            'device': 'CXVPN-TUN',
            'stack': 'mixed',
            'dns-hijack': ['any:53', 'tcp://any:53'],
            'auto-route': True,
            # strict-route 会接管系统流量；必须让 Mihomo 自动识别真实出口，
            # 否则其 DNS 和控制连接可能再次进入 TUN，形成路由递归。
            'auto-detect-interface': True,
            'strict-route': True,
            'route-exclude-address': excluded_routes or [],
        },
        'proxies': proxies,
    }
    if standby or _uses_proxy(config):
        generated['proxy-providers'] = {}
        generated['proxy-groups'] = []
        for provider in enabled_providers:
            download = _provider_download_route(provider, system_proxy)
            if download['proxy_url']:
                upstream = download.get('upstream') or download['target']
                if not any(item['name'] == upstream for item in generated['proxies']):
                    generated['proxies'].append(
                        _subscription_proxy_node(
                            upstream, download['proxy_url'],
                            config['physical_interface']))
            if download.get('upstream'):
                generated['proxy-groups'].append({
                    'name': download['target'],
                    'type': 'fallback',
                    'proxies': [download['upstream'], 'PHYSICAL'],
                    'url': HEALTH_CHECK_URL,
                    'interval': 300,
                    'timeout': 5000,
                    'lazy': True,
                })
            if download.get('self_bootstrap'):
                generated['proxy-groups'].append({
                    'name': download['target'],
                    'type': 'fallback',
                    'use': [provider_keys[provider['id']]],
                    'url': HEALTH_CHECK_URL,
                    'interval': 300,
                    'timeout': 5000,
                    'lazy': True,
                    'empty-fallback': 'PHYSICAL',
                })
            provider_config = _proxy_provider_config(
                provider,
                f'./providers/{subscription_store.provider_filename(provider)}',
                prefix=f'[{provider["name"]}] ',
                interface_name=config['physical_interface'],
                download_proxy=download['target'])
            generated['proxy-providers'][provider_keys[provider['id']]] = provider_config
            if _auto_policy.is_enabled(provider):
                generated['proxy-groups'].extend(_auto_policy.build_groups(
                    provider, provider_keys[provider['id']],
                    provider_groups[provider['id']], HEALTH_CHECK_URL,
                    subscription_store.load_node_snapshot(provider).get(
                        'nodes') or []))
            else:
                generated['proxy-groups'].append(_proxy_group_config(
                    provider_groups[provider['id']],
                    _selection.provider_group_strategy(provider),
                    provider_keys=[provider_keys[provider['id']]]))
        generated['proxy-groups'].append(_proxy_group_config(
            'PROXY', config['proxy_strategy'],
            proxy_names=[provider_groups[item['id']]
                         for item in enabled_providers]))
    rules = []
    if standby:
        rules.append('MATCH,PROXY' if enabled_providers else 'MATCH,PHYSICAL')
    else:
        for raw in _effective_rules(config):
            parts = raw.split(',')
            if parts[0] == 'MATCH':
                parts[1] = target_name(parts[1])
            elif len(parts) >= 3 and parts[2] != 'PHYSICAL':
                parts[2] = target_name(parts[2])
            rules.append(','.join(parts))
    generated['rules'] = rules
    return generated


def _proxy_provider_config(provider, path, prefix='', health_check=True,
                           interface_name='', download_proxy=''):
    """生成统一的 provider 配置，保持运行态与独立预览行为一致。"""
    result = {
        'type': 'http',
        'url': provider['url'],
        'path': path,
        'size-limit': SUBSCRIPTION_SIZE_LIMIT,
        'header': {'User-Agent': [provider.get(
            'user_agent') or SUBSCRIPTION_USER_AGENT]},
        'health-check': {
            'enable': bool(health_check),
            'url': HEALTH_CHECK_URL,
            'interval': 300,
            'timeout': 5000,
            'lazy': True,
        },
    }
    if provider.get('auto_update'):
        result['interval'] = provider['interval']
    if prefix:
        result['override'] = {'additional-prefix': prefix}
    if interface_name:
        result.setdefault('override', {})['interface-name'] = interface_name
    if download_proxy:
        result['proxy'] = download_proxy
    if provider['filter']:
        result['filter'] = provider['filter']
    if provider['exclude_filter']:
        result['exclude-filter'] = provider['exclude_filter']
    return result


def _proxy_group_config(name, strategy, *, provider_keys=None,
                        proxy_names=None):
    """生成订阅节点组或上层组合组，避免跨层直接展开订阅节点。"""
    if (provider_keys is None) == (proxy_names is None):
        raise ValueError('代理组必须且只能指定一种成员来源')
    group = {
        'name': name,
        'type': strategy,
        'empty-fallback': 'REJECT',
    }
    if provider_keys is not None:
        group['use'] = provider_keys
    else:
        group['proxies'] = proxy_names
    if strategy in {'url-test', 'fallback'}:
        group.update({
            'url': HEALTH_CHECK_URL,
            'interval': 300,
            'timeout': 5000,
            'lazy': True,
        })
    if strategy == 'url-test':
        group['tolerance'] = 80
    return group


def _provider_proxy_catalog(payload):
    """把新版 Controller 的 provider 节点合并为按运行态名称索引的目录。"""
    providers = payload.get('providers') if isinstance(payload, dict) else {}
    if not isinstance(providers, dict):
        return {}
    catalog = {}
    for provider_name, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        for node in provider.get('proxies') or []:
            if not isinstance(node, dict):
                continue
            node_name = str(node.get('name') or '')
            # Controller 的 /providers/proxies 在新版 Mihomo 中会同时返回
            # provider 节点和策略组对象（AUTO-*、PROXY-* 等）。策略组不是
            # 可选代理节点，必须在目录归一化阶段排除，避免聚合组把它们混入
            # 全部节点列表或持久化快照。
            if (not node_name or node_name.upper() in _NON_PROXY_NAMES
                    or _is_proxy_group(node)):
                continue
            catalog[node_name] = {
                **node, 'provider_name': str(provider_name or '')}
    return catalog


def _resolve_selected_proxy(proxies, selected, maximum_depth=8):
    """沿嵌套策略组解析到最终节点，避免 UI 只看到内部组名。"""
    current = str(selected or '')
    seen = set()
    for _index in range(maximum_depth):
        if not current or current in seen:
            break
        seen.add(current)
        item = proxies.get(current)
        if not isinstance(item, dict) or not item.get('now'):
            break
        current = str(item.get('now') or current)
    return current


_PROXY_GROUP_TYPES = {
    'select', 'url-test', 'fallback', 'load-balance', 'relay', 'smart',
    'compatibility',
}
_NON_PROXY_NAMES = frozenset({
    'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'PHYSICAL',
})


def _is_proxy_group(value):
    """判断 Controller 返回的代理对象是否为策略组而非真实节点。"""
    if not isinstance(value, dict):
        return False
    return ('all' in value or str(value.get('type') or '').strip().lower()
            in _PROXY_GROUP_TYPES)


def _expand_proxy_names(proxies, names, maximum_depth=8):
    """展开运行态策略组，只保留叶子节点名称并去重。"""
    result = []
    emitted = set()

    def visit(raw_name, depth, ancestors):
        name = str(raw_name or '').strip()
        if not name or name.upper() in _NON_PROXY_NAMES or name in ancestors:
            return
        item = proxies.get(name)
        if (_is_proxy_group(item) and depth < maximum_depth):
            children = item.get('all')
            if isinstance(children, list):
                next_ancestors = ancestors | {name}
                for child in children:
                    visit(child, depth + 1, next_ancestors)
                return
        if _is_proxy_group(item):
            return
        if name not in emitted:
            emitted.add(name)
            result.append(name)

    for name in names or []:
        visit(name, 0, set())
    return result


def _provider_catalog_names(catalog, provider_id, prefix=''):
    """按 provider key 优先筛选节点，兼容旧核心缺少名称前缀的返回。"""
    expected_provider = f'provider-{provider_id}'
    names = []
    for name, node in (catalog or {}).items():
        if not isinstance(node, dict):
            continue
        provider_name = str(node.get('provider_name') or '').strip()
        if provider_name == expected_provider or (
                prefix and str(name).startswith(prefix)):
            names.append(str(name))
    return names


def _history_tested_at(history_item):
    """把 Mihomo history.time 转为安全的 Unix 秒时间戳。"""
    if not isinstance(history_item, dict):
        return 0
    raw = str(history_item.get('time') or '').strip()
    if not raw:
        return 0
    try:
        value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        timestamp = int(value.timestamp())
    except (OverflowError, TypeError, ValueError):
        return 0
    return timestamp if 0 < timestamp <= subscription_store.MAX_TIMESTAMP else 0


def _write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(
        prefix='routing.', suffix='.tmp', dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _service_xml():
    return f'''<service>
  <id>{SERVICE_ID}</id>
  <name>CXVPN 统一分流服务</name>
  <description>由 CXVPNTools 维护的 Mihomo 域名分流服务</description>
  <executable>%BASE%\\mihomo.exe</executable>
  <arguments>-d &quot;%BASE%\\data&quot; -f &quot;%BASE%\\config.json&quot;</arguments>
  <workingdirectory>%BASE%</workingdirectory>
  <startmode>Automatic</startmode>
  <onfailure action="restart" delay="10 sec" />
  <resetfailure>1 hour</resetfailure>
  <stoptimeout>15 sec</stoptimeout>
  <log mode="roll-by-size">
    <sizeThreshold>1048576</sizeThreshold>
    <keepFiles>4</keepFiles>
  </log>
</service>
'''


def _run_elevated(script, timeout=120):
    import base64
    import gzip
    result_file = tempfile.NamedTemporaryFile(
        prefix='cxvpn-routing-', suffix='.result', delete=False)
    result_path = result_file.name
    result_file.close()
    wrapped = f'''
$ErrorActionPreference = 'Stop'
$resultPath = {_ps_literal(result_path)}
try {{
  & {{
{script}
  }}
  [IO.File]::WriteAllText($resultPath, 'OK', [Text.UTF8Encoding]::new($false))
}} catch {{
  $message = [string]$_.Exception.Message
  [IO.File]::WriteAllText($resultPath, ('ERROR:' + $message), [Text.UTF8Encoding]::new($false))
  exit 1
}}
'''
    compressed = base64.b64encode(
        gzip.compress(wrapped.encode('utf-8'), mtime=0)).decode('ascii')
    launcher = f'''
$data = [Convert]::FromBase64String('{compressed}')
$source = New-Object IO.MemoryStream(,$data)
$gzip = New-Object IO.Compression.GZipStream(
  $source, [IO.Compression.CompressionMode]::Decompress)
$reader = New-Object IO.StreamReader($gzip, [Text.Encoding]::UTF8)
try {{ $code = $reader.ReadToEnd() }}
finally {{ $reader.Dispose(); $gzip.Dispose(); $source.Dispose() }}
& ([ScriptBlock]::Create($code))
'''
    encoded = base64.b64encode(
        launcher.encode('utf-16-le')).decode('ascii')
    outer = (
        "$ErrorActionPreference='Stop'; "
        f"$p=Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru "
        f"-ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand','{encoded}'); "
        'exit $p.ExitCode')
    if len(outer) >= 30000:
        try:
            os.remove(result_path)
        except OSError:
            pass
        raise RoutingError('管理员操作脚本超过 Windows 命令行限制，分流配置未变更')
    try:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command', outer],
            capture_output=True, text=True, encoding='utf-8',
            errors='replace', timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            with open(result_path, encoding='utf-8-sig', errors='replace') as stream:
                child_result = stream.read(8192).strip()
        except OSError:
            child_result = ''
    except subprocess.TimeoutExpired as exc:
        raise RoutingError('Windows 管理员操作超时，分流配置未变更') from exc
    finally:
        try:
            os.remove(result_path)
        except OSError:
            pass
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip()
        if child_result.startswith('ERROR:'):
            detail = child_result[6:].strip()
        if 'canceled' in detail.lower() or '取消' in detail:
            raise RoutingError('已取消 Windows 管理员授权，分流配置未变更')
        safe_detail = ELEVATED_ERROR_MESSAGES.get(detail)
        if not safe_detail and detail:
            safe_detail = _sanitize_mihomo_error(detail)
        raise RoutingError(
            f'统一分流服务操作失败：{safe_detail or "未收到提权子进程的错误详情"}')


def _ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _free_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', 0))
        return int(listener.getsockname()[1])


class RoutingManager:
    def __init__(self, logger=None):
        self.log = logger or (lambda _message: None)
        self._data_dir = os.path.join(cfgmod.BASE, 'routing_data')
        program_data = os.environ.get('ProgramData', r'C:\ProgramData')
        self._service_dir = os.path.join(
            program_data, LEGACY_SERVICE_DIR_NAME)
        self._native_service = _routing_service.RoutingServiceClient()
        self._runtime_signature = ''
        self._runtime_sha256 = ''
        self._environment = EnvironmentCache(
            lambda: vpn_os.list_vpns(), lambda: list_physical_interfaces(),
            lambda: list_tun_conflicts(), self._log_best_effort)

    @staticmethod
    def _config_signature(config):
        current = normalize_config(config)
        current.pop('enabled', None)
        return hashlib.sha256(json.dumps(current, sort_keys=True,
            ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()

    def runtime_matches(self, config, service_state):
        return bool(self._runtime_signature and self._runtime_sha256 and
                    self._runtime_signature == self._config_signature(config) and
                    self._runtime_sha256 == str(service_state.get('config_sha256') or '').upper())

    def remember_selection(self, config, previous):
        if self._runtime_sha256 and self._runtime_signature == self._config_signature(previous):
            self._runtime_signature = self._config_signature(config)

    def _log_best_effort(self, message):
        """日志故障不得遮蔽原始错误或阻断配置事务回滚。"""
        try:
            self.log(message)
        except Exception:
            pass

    def _service_state(self, allow_powershell=True):
        try:
            native = self._native_service.status()
        except _routing_service.ServiceError:
            native = None
        if native:
            return {
                'installed': True,
                'state': 'Running',
                'backend': 'native',
                'runtime_running': bool(native.get('runtime_running')),
                'runtime_enabled': bool(native.get('runtime_enabled')),
                'runtime_mode': str(native.get('runtime_mode') or (
                    'active' if native.get('runtime_enabled') else 'stopped')),
                'service_version': str(native.get('service_version') or ''),
                'system_proxy_active': bool(native.get('system_proxy_active')),
                'fast_toggle_ready': bool(native.get('fast_toggle_ready')),
                'config_sha256': str(native.get('config_sha256') or ''),
                'crash_fused': bool(native.get('crash_fused')),
            }
        if not allow_powershell:
            return {'installed': None, 'state': 'Unknown',
                    'backend': 'unknown'}
        script = rf'''
$service = Get-Service -Name '{SERVICE_ID}' -ErrorAction SilentlyContinue
if ($service) {{
  $detail = Get-CimInstance Win32_Service -Filter "Name='{SERVICE_ID}'" -ErrorAction SilentlyContinue
  $backend = if ($detail.PathName -match 'CXVPNRoutingHost\.exe') {{ 'native' }} else {{ 'legacy' }}
  [pscustomobject]@{{ installed = $true; state = [string]$service.Status; backend = $backend }} | ConvertTo-Json -Compress
}} else {{
  [pscustomobject]@{{ installed = $false; state = 'NotInstalled'; backend = 'none' }} | ConvertTo-Json -Compress
}}
'''
        try:
            rows = _run_powershell_json(script)
            if not rows or not isinstance(rows[0], dict):
                return {'installed': None, 'state': 'Unknown'}
            row = rows[0]
            state = str(row.get('state') or '').strip()
            installed = row.get('installed')
            if installed is False and state == 'NotInstalled':
                return {'installed': False, 'state': 'NotInstalled',
                        'backend': 'none'}
            if installed is True and state and state != 'Unknown':
                return {'installed': True, 'state': state,
                        'backend': str(row.get('backend') or 'legacy')}
            return {'installed': None, 'state': 'Unknown'}
        except Exception:
            return {'installed': None, 'state': 'Unknown'}

    def _controller_request(self, config, path, method='GET', payload=None,
                            timeout=2.5):
        url = f'http://127.0.0.1:{config["controller_port"]}{path}'
        body = (json.dumps(payload, ensure_ascii=False).encode('utf-8')
                if payload is not None else None)
        headers = {'Authorization': f'Bearer {config["controller_secret"]}'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(
            url, data=body, headers=headers, method=method)
        # Controller 只监听回环地址。必须显式禁用系统/环境代理，避免关闭
        # Windows 系统代理后，urllib 的默认 opener 仍把就绪探测送入旧的
        # mixed-port，形成“本地 Controller 请求经代理组转发”的回环。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
        return json.loads(raw.decode('utf-8')) if raw else {}

    def _controller_version(self, config):
        try:
            data = self._controller_request(config, '/version', timeout=1.5)
            return data.get('version', '')
        except (OSError, ValueError, urllib.error.URLError):
            return ''

    def close_connections(self, config, connection_id=''):
        """关闭一个或全部 Mihomo 活动连接。"""
        normalized = normalize_config(config)
        target = str(connection_id or '').strip()
        path = '/connections'
        if target:
            path += '/' + urllib.parse.quote(target, safe='')
        self._controller_request(
            normalized, path, method='DELETE', timeout=3)
        return {'ok': True, 'closed': 'one' if target else 'all'}

    @staticmethod
    def _group_identity(config, group_id):
        group_id = str(group_id or '').strip().lower()
        if group_id == 'all':
            return 'PROXY', '全部代理订阅', config['proxy_strategy']
        provider = next((item for item in config['proxy_providers']
                         if item['enabled'] and item['id'] == group_id), None)
        if not provider:
            raise RoutingError('代理订阅不存在或未启用')
        return (f'PROXY-{provider["id"]}', provider['name'],
                _selection.provider_group_strategy(provider))

    def proxy_overview(self, config):
        """读取运行中 Mihomo 的代理组、当前节点和最近延迟。"""
        normalized = normalize_config(config)
        if not _uses_proxy(normalized):
            return []
        try:
            payload = self._controller_request(normalized, '/proxies')
        except (OSError, ValueError, urllib.error.URLError):
            return []
        proxies = payload.get('proxies') if isinstance(payload, dict) else {}
        if not isinstance(proxies, dict):
            return []
        provider_catalog = {}
        try:
            provider_payload = self._controller_request(
                normalized, '/providers/proxies')
            provider_catalog = _provider_proxy_catalog(provider_payload)
        except (OSError, ValueError, urllib.error.URLError):
            # 兼容尚未提供 provider 详情接口的旧 Controller。
            pass
        group_ids = ['all'] + [
            item['id'] for item in normalized['proxy_providers'] if item['enabled']]
        result = []
        for group_id in group_ids:
            internal_name, display_name, strategy = self._group_identity(
                normalized, group_id)
            group = proxies.get(internal_name)
            if not isinstance(group, dict):
                continue
            nodes = []
            prefix = '' if group_id == 'all' else f'[{display_name}] '
            provider = next((item for item in normalized['proxy_providers']
                             if item['id'] == group_id), None)
            # /proxies 的 all 可能暂时指向旧配置留下的 AUTO-* 策略组（例如
            # 用户刚从自动优选切到手动模式，常驻核心尚未重载配置）。先递归
            # 展开组成员，避免把策略组本身伪装成真实节点；provider 接口可用
            # 时再合并其完整节点目录，补回尚未出现在运行态组中的节点。
            node_names = _expand_proxy_names(
                proxies, group.get('all') or [])
            if provider:
                catalog_names = _provider_catalog_names(
                    provider_catalog, provider['id'], prefix)
                if _auto_policy.is_enabled(provider):
                    node_names = catalog_names
                elif catalog_names:
                    node_names = list(dict.fromkeys(
                        [*node_names, *catalog_names]))
            elif provider_catalog:
                # 全部代理组通常只列出各订阅的上层组；当其中某个订阅仍是
                # 旧 AUTO-* 结构时，直接从 provider 目录补齐真实叶子节点。
                node_names = list(dict.fromkeys(
                    [*node_names, *provider_catalog]))
            for node_name in node_names:
                generic_node = proxies.get(node_name)
                node = (generic_node if isinstance(generic_node, dict)
                        else provider_catalog.get(node_name, {}))
                history = node.get('history') if isinstance(node, dict) else []
                last = history[-1] if isinstance(history, list) and history else {}
                delay = last.get('delay') if isinstance(last, dict) else 0
                nodes.append({
                    'name': node_name,
                    'display_name': (node_name[len(prefix):]
                                     if prefix and node_name.startswith(prefix)
                                     else node_name),
                    'delay': delay if isinstance(delay, int) and delay > 0 else 0,
                    'alive': (node.get('alive')
                              if isinstance(node.get('alive'), bool) else None),
                    'tested': isinstance(node.get('alive'), bool),
                    'tested_at': _history_tested_at(last),
                    'type': str(node.get('type') or ''),
                    'provider_name': str(
                        node.get('provider-name') or
                        node.get('provider_name') or ''),
                })
            selected = _resolve_selected_proxy(
                proxies, group.get('now') or '')
            # 运行态选择仍可能停留在已淘汰的内部策略组。UI 只能把真实叶子
            # 节点标记为当前节点，避免显示 AUTO-…-FALLBACK 之类内部名称。
            if (selected not in node_names and
                    _is_proxy_group(proxies.get(selected))):
                selected = ''
            result.append({
                'id': group_id,
                'name': display_name,
                'strategy': strategy,
                'selected': selected,
                'nodes': nodes,
                'alive_count': len([
                    item for item in nodes if item['alive'] is True]),
            })
        return result

    def connection_observability(self, config):
        """返回去标识化连接与规则命中摘要，不暴露目标 IP、域名或凭据。"""
        normalized = normalize_config(config)
        try:
            payload = self._controller_request(normalized, '/connections', timeout=2)
        except (OSError, ValueError, urllib.error.URLError):
            return {'available': False, 'active': 0, 'upload': 0,
                    'download': 0, 'rule_hits': []}
        rows = payload.get('connections') if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            rows = []
        try:
            current_builtin_rules = _builtin_rules(
                normalized['builtin_rule_pack'])
        except RoutingError:
            current_builtin_rules = []
        builtin_payloads = {
            item.split(',')[1] for item in current_builtin_rules if ',' in item}
        user_payloads = {
            item['domain'] for item in _active_rules(normalized)}
        counts = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            rule = str(row.get('rule') or 'MATCH')
            payload_value = str(row.get('rulePayload') or '')
            source = ('user' if payload_value in user_payloads else
                      'builtin' if payload_value in builtin_payloads else
                      'default')
            key = (source, rule)
            counts[key] = counts.get(key, 0) + 1
        return {
            'available': True,
            'active': len(rows),
            'upload': int(payload.get('uploadTotal') or 0),
            'download': int(payload.get('downloadTotal') or 0),
            'rule_hits': [
                {'source': source, 'rule': rule, 'connections': count}
                for (source, rule), count in sorted(
                    counts.items(), key=lambda item: item[1], reverse=True)[:8]
            ],
        }

    def refresh_proxy_provider(self, config, provider_id):
        normalized = normalize_config(config)
        provider_id = str(provider_id or '').strip().lower()
        self._group_identity(normalized, provider_id)
        provider = next(item for item in normalized['proxy_providers']
                        if item['id'] == provider_id)
        provider_name = f'provider-{provider_id}'
        path = f'/providers/proxies/{urllib.parse.quote(provider_name, safe="")}'
        started_at = time.monotonic()
        self._log_best_effort(
            f'[routing] 订阅“{provider["name"]}”更新请求已提交，'
            '等待常驻核心领取（超时 15 秒）')
        try:
            self._controller_request(normalized, path, method='PUT', timeout=15)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            self._log_best_effort(
                f'[routing] 订阅“{provider["name"]}”常驻核心更新失败，'
                f'异常={type(exc).__name__}，'
                f'耗时={time.monotonic() - started_at:.1f} 秒')
            raise RoutingError(
                '订阅远端更新失败；当前节点缓存未被删除，请检查订阅地址和节点可用性') from exc
        self._log_best_effort(
            f'[routing] 订阅“{provider["name"]}”常驻核心已完成远端请求，'
            f'耗时={time.monotonic() - started_at:.1f} 秒，开始回读节点与完整缓存')
        groups = self.proxy_overview(normalized)
        group = next((item for item in groups
                      if item.get('id') == provider_id), None)
        if not group or not group.get('nodes'):
            raise RoutingError(
                '订阅更新请求已提交，但无法确认新节点列表；已保留当前界面数据，请稍后刷新')
        selection_invalid = _selection.selection_missing(
            provider, group.get('nodes') or [])
        cache = None
        cache_warning = ''
        try:
            service_cache = self._native_service.read_provider(
                subscription_store.provider_filename(provider))
            with commit_scope():
                cache, _snapshot = subscription_store.persist_bytes_and_nodes(
                    provider, service_cache['content'],
                    len(group.get('nodes') or []), '常驻核心远端更新',
                    [{**node, 'name': node.get('display_name') or node['name']}
                     for node in group.get('nodes') or []],
                    size_limit=SUBSCRIPTION_SIZE_LIMIT)
        except (_routing_service.ServiceError, OSError, ValueError):
            cache_warning = (
                '远端更新已完成，但完整节点缓存同步失败；常驻核心仍使用最新节点')
            self._log_best_effort(
                f'[routing] 订阅“{provider["name"]}”完整缓存同步失败，'
                '已保留服务侧最新缓存')
        base_message = ('代理订阅已更新，但原选择节点已失效，请重新选择'
                        if selection_invalid else '代理订阅远端更新成功')
        return {
            'ok': True,
            'msg': base_message + (f'；{cache_warning}' if cache_warning else ''),
            'groups': groups,
            'selection_invalid': selection_invalid,
            'cache': cache,
            'warning': cache_warning,
            'used_cache': False,
            'refreshed': True,
            'update_state': 'remote_updated',
        }

    def preview_proxy_provider(self, value, network=None, _seed_payload=None,
                               _persist=True, _cache_source='',
                               _run_delay_test=False, _refresh_cache=True,
                               _cache_only=False, _progress=None,
                               _cancel_event=None, _persist_snapshot=True):
        """获取订阅；自动模式按缓存状态选择可用的首次下载出口。"""
        source = dict(value) if isinstance(value, dict) else {}
        normalized = normalize_config({
            'enabled': False,
            'proxy_providers': [{**source, 'enabled': True}],
            'default_outbound': 'physical',
        })
        provider = normalized['proxy_providers'][0]
        route = provider['download_route']
        provider_name = provider['name']
        kwargs = {
            '_seed_payload': _seed_payload,
            '_persist': _persist,
            '_cache_source': _cache_source,
            '_run_delay_test': _run_delay_test,
            '_refresh_cache': _refresh_cache,
            '_cache_only': _cache_only,
            '_progress': _progress,
            '_cancel_event': _cancel_event,
            '_persist_snapshot': _persist_snapshot,
        }
        can_auto_route = (
            route == 'auto' and not _cache_only and _seed_payload is None)
        system_proxy = windows_system_proxy() if can_auto_route else ''
        cache_available = bool(
            subscription_store.cache_status(provider).get('available')) \
            if can_auto_route else False
        preferred_proxy_error = None
        preferred_system_proxy = bool(system_proxy and not cache_available)
        if preferred_system_proxy:
            started_at = time.monotonic()
            self.log(
                f'[routing] 订阅“{provider_name}”首次获取尚无节点缓存，'
                '优先通过 Windows 系统代理执行（超时 '
                f'{PROVIDER_PREVIEW_TIMEOUT} 秒）')
            retry_source = dict(source)
            retry_source['download_route'] = 'system-proxy'
            try:
                result = self._preview_proxy_provider_once(
                    retry_source, network, **kwargs)
            except RoutingError as exc:
                preferred_proxy_error = exc
                elapsed = time.monotonic() - started_at
                self.log(
                    f'[routing] 订阅“{provider_name}”Windows 系统代理优先路径失败，'
                    f'耗时 {elapsed:.1f} 秒，错误类型 {type(exc).__name__}；'
                    '继续尝试物理网络')
            else:
                elapsed = time.monotonic() - started_at
                self.log(
                    f'[routing] 订阅“{provider_name}”Windows 系统代理优先路径成功，'
                    f'耗时 {elapsed:.1f} 秒')
                result['download_route'] = (
                    f'Windows 系统代理自动选择 {_proxy_display(system_proxy)}')
                result['auto_route'] = 'system-proxy'
                return result
        try:
            return self._preview_proxy_provider_once(source, network, **kwargs)
        except ProviderFetchError as primary_error:
            fallback_error = preferred_proxy_error or primary_error
            if system_proxy and not preferred_system_proxy:
                started_at = time.monotonic()
                self.log(
                    f'[routing] 订阅“{provider_name}”内置更新未产出节点，'
                    '开始通过 Windows 系统代理自动回退（超时 '
                    f'{PROVIDER_PREVIEW_TIMEOUT} 秒）')
                retry_source = dict(source)
                retry_source['download_route'] = 'system-proxy'
                try:
                    result = self._preview_proxy_provider_once(
                        retry_source, network, **kwargs)
                except RoutingError as exc:
                    fallback_error = exc
                    elapsed = time.monotonic() - started_at
                    self.log(
                        f'[routing] 订阅“{provider_name}”Windows 系统代理回退失败，'
                        f'耗时 {elapsed:.1f} 秒，错误类型 {type(exc).__name__}')
                else:
                    elapsed = time.monotonic() - started_at
                    self.log(
                        f'[routing] 订阅“{provider_name}”Windows 系统代理回退成功，'
                        f'耗时 {elapsed:.1f} 秒')
                    result['download_route'] = (
                        f'Windows 系统代理自动回退 {_proxy_display(system_proxy)}')
                    result['auto_fallback'] = 'system-proxy'
                    return result

            if _cache_only or _seed_payload is not None:
                raise primary_error
            retained = self._retained_cache_preview(
                source, network, kwargs, provider_name)
            if retained:
                return retained
            if system_proxy:
                raise RoutingError(
                    '无法获取订阅：内置代理、物理网络和 Windows 系统代理均失败，'
                    '当前没有可保留的节点缓存；请检查订阅地址或导入现有 YAML') \
                    from fallback_error
            raise primary_error

    def _retained_cache_preview(self, source, network, kwargs, provider_name):
        """远端链路均失败时显式解析 last-known-good，且保留历史测速状态。"""
        normalized = normalize_config({
            'enabled': False,
            'proxy_providers': [{**source, 'enabled': True}],
            'default_outbound': 'physical',
        })
        provider = normalized['proxy_providers'][0]
        cache = subscription_store.cache_status(provider)
        if not cache.get('available'):
            return None
        cache_kwargs = dict(kwargs)
        cache_kwargs.update({
            '_persist': False,
            '_run_delay_test': False,
            '_refresh_cache': False,
            '_cache_only': True,
            '_persist_snapshot': False,
        })
        try:
            result = self._preview_proxy_provider_once(
                source, network, **cache_kwargs)
        except RoutingError as exc:
            self._log_best_effort(
                f'[routing] 订阅“{provider_name}”last-known-good 解析失败，'
                f'错误类型={type(exc).__name__}')
            return None
        snapshot = subscription_store.load_node_snapshot(provider)
        previous = {
            str(item.get('display_name') or item.get('name') or ''): item
            for item in snapshot.get('nodes') or []
        }
        nodes = []
        for item in result.get('nodes') or []:
            name = str(item.get('display_name') or item.get('name') or '')
            old = previous.get(name)
            nodes.append({
                **item,
                **({
                    'delay': old.get('delay'),
                    'alive': old.get('alive'),
                    'tested': old.get('tested', False),
                    'tested_at': old.get('tested_at', 0),
                } if old else {}),
            })
        result.update({
            'msg': f'远端更新失败，已保留上次成功缓存中的 {len(nodes)} 个节点',
            'nodes': nodes,
            'node_count': len(nodes),
            'download_route': '本地节点缓存（远端更新失败）',
            'cache': cache,
            'used_cache': True,
            'refreshed': False,
            'update_state': 'cache_retained',
            'warning': '远端更新失败，节点列表和历史测速结果均已保留',
        })
        self._log_best_effort(
            f'[routing] 订阅“{provider_name}”远端更新失败，'
            f'已从 last-known-good 恢复 {len(nodes)} 个节点')
        return result

    def _preview_proxy_provider_once(self, value, network=None,
                                     _seed_payload=None, _persist=True,
                                     _cache_source='', _run_delay_test=False,
                                     _refresh_cache=True, _cache_only=False,
                                     _progress=None, _cancel_event=None,
                                     _persist_snapshot=True):
        """用内置 Mihomo 获取订阅；成功内容写入 last-known-good 缓存。"""
        source = dict(value) if isinstance(value, dict) else {}
        source['enabled'] = True
        normalized = normalize_config({
            'enabled': False,
            'proxy_providers': [source],
            'default_outbound': 'physical',
        })
        provider = normalized['proxy_providers'][0]
        parsed = urllib.parse.urlparse(provider['url'])
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
            raise RoutingError('请填写有效的 http/https 订阅 URL')
        network_source = network if isinstance(network, dict) else {}
        physical_interface = str(
            network_source.get('physical_interface') or '').strip()
        dns_servers = network_source.get('dns_servers')
        if isinstance(dns_servers, str):
            dns_servers = re.split(r'[,\s]+', dns_servers)
        if not isinstance(dns_servers, list):
            dns_servers = []
        dns_servers = [str(item).strip() for item in dns_servers
                       if str(item).strip()][:8]
        verify_runtime()
        cache_available = bool(
            _seed_payload is not None or
            subscription_store.cache_status(provider).get('available'))
        download = ({'target': 'PHYSICAL', 'proxy_url': '',
                     'upstream': '', 'self_bootstrap': False}
                    if _cache_only else _preview_provider_download_route(
                        provider, windows_system_proxy(), cache_available))

        controller_port = _free_loopback_port()
        controller_secret = secrets.token_urlsafe(24)
        controller_config = {
            'controller_port': controller_port,
            'controller_secret': controller_secret,
        }
        physical_proxy = {'name': 'PHYSICAL', 'type': 'direct', 'udp': True}
        if physical_interface:
            physical_proxy['interface-name'] = physical_interface
        preview_proxies = [physical_proxy]
        preview_groups = []
        if not _cache_only and download['proxy_url']:
            upstream = download.get('upstream') or download['target']
            preview_proxies.append(
                _subscription_proxy_node(
                    upstream, download['proxy_url'], physical_interface))
        if not _cache_only and download.get('upstream'):
            preview_groups.append({
                'name': download['target'],
                'type': 'fallback',
                'proxies': [download['upstream'], 'PHYSICAL'],
                'url': HEALTH_CHECK_URL,
                'interval': 300,
                'timeout': 5000,
                'lazy': True,
            })
        if not _cache_only and download.get('self_bootstrap'):
            preview_groups.append({
                'name': download['target'],
                'type': 'fallback',
                'use': ['preview'],
                'url': HEALTH_CHECK_URL,
                'interval': 300,
                'timeout': 5000,
                'lazy': True,
                'empty-fallback': 'PHYSICAL',
            })
        preview_provider = _proxy_provider_config(
            provider, './providers/preview.yaml', health_check=False,
            interface_name=physical_interface,
            download_proxy=download['target'])
        if _cache_only:
            preview_provider = {
                'type': 'file', 'path': './providers/preview.yaml',
                'health-check': {'enable': False}}
            if physical_interface:
                preview_provider['override'] = {
                    'interface-name': physical_interface}
        generated = {
            'allow-lan': False,
            'bind-address': '127.0.0.1',
            'mode': 'rule',
            'log-level': 'info',
            'ipv6': True,
            'external-controller': f'127.0.0.1:{controller_port}',
            'secret': controller_secret,
            'proxies': preview_proxies,
            'proxy-providers': {'preview': preview_provider},
            'proxy-groups': preview_groups + [{
                'name': 'PREVIEW',
                'type': 'select',
                'use': ['preview'],
                'empty-fallback': 'REJECT',
            }],
            'rules': ['MATCH,DIRECT'],
        }
        if dns_servers:
            generated['dns'] = {
                'enable': True,
                'ipv6': True,
                'nameserver': dns_servers,
            }

        provider_payload = None
        provider_seen = False
        log_text = ''
        cache_payload = None
        cache_seeded = False
        candidate_seeded = _seed_payload is not None
        refreshed = False
        refresh_warning = ''
        preview_delays = None
        with tempfile.TemporaryDirectory(prefix='cxvpn-provider-preview-') as root:
            config_path = os.path.join(root, 'config.json')
            log_path = os.path.join(root, 'mihomo.log')
            provider_path = os.path.join(root, 'providers', 'preview.yaml')
            if candidate_seeded:
                if (not isinstance(_seed_payload, (bytes, bytearray)) or
                        not _seed_payload or
                        len(_seed_payload) > SUBSCRIPTION_SIZE_LIMIT):
                    raise RoutingError('导入候选缓存为空或超过大小限制')
                os.makedirs(os.path.dirname(provider_path), exist_ok=True)
                with open(provider_path, 'wb') as stream:
                    stream.write(bytes(_seed_payload))
                cache_seeded = True
            else:
                cache_seeded = subscription_store.stage_cache(
                    provider, provider_path)
            if _cache_only and not cache_seeded:
                raise RoutingError('尚无可测速的节点缓存，请先获取节点')
            _write_json(config_path, generated)
            with open(log_path, 'w', encoding='utf-8', errors='replace') as log_file:
                process = subprocess.Popen(
                    [_binary_path('mihomo.exe'), '-d', root, '-f', config_path],
                    stdout=log_file, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                close_job = None
                try:
                    try:
                        close_job = _attach_kill_on_close_job(process)
                    except Exception as exc:
                        raise RoutingError('无法安全启动临时订阅解析进程') from exc
                    deadline = time.monotonic() + PROVIDER_PREVIEW_TIMEOUT
                    controller_ready = False
                    while time.monotonic() < deadline:
                        if _cancel_event and _cancel_event.is_set():
                            raise RoutingError('测速已停止')
                        if process.poll() is not None:
                            break
                        try:
                            if not controller_ready:
                                version = self._controller_request(
                                    controller_config, '/version', timeout=1)
                                controller_ready = bool(
                                    isinstance(version, dict) and
                                    version.get('version'))
                                if not controller_ready:
                                    time.sleep(0.25)
                                    continue
                            payload = self._controller_request(
                                controller_config, '/providers/proxies/preview',
                                timeout=1.5)
                            if isinstance(payload, dict):
                                provider_seen = True
                                proxies = payload.get('proxies')
                                if isinstance(proxies, list) and proxies:
                                    provider_payload = payload
                                    if (cache_seeded and not candidate_seeded and
                                            _refresh_cache):
                                        try:
                                            self._controller_request(
                                                controller_config,
                                                '/providers/proxies/preview',
                                                method='PUT', timeout=15)
                                            latest = self._controller_request(
                                                controller_config,
                                                '/providers/proxies/preview',
                                                timeout=2)
                                            if isinstance(latest, dict) and isinstance(
                                                    latest.get('proxies'), list) and latest['proxies']:
                                                provider_payload = latest
                                                refreshed = True
                                            else:
                                                refresh_warning = (
                                                    '在线更新结果无法确认，已继续使用上次成功缓存')
                                        except (OSError, ValueError,
                                                urllib.error.URLError):
                                            refresh_warning = (
                                                '在线更新失败，已继续使用上次成功缓存')
                                    break
                        except (OSError, ValueError, urllib.error.URLError):
                            pass
                        time.sleep(0.25)
                    if provider_payload and _run_delay_test:
                        preview_nodes = [{
                            'name': str(item.get('name') or ''),
                            'display_name': str(item.get('name') or ''),
                            'type': str(item.get('type') or ''),
                            'provider_name': 'preview',
                        } for item in provider_payload.get('proxies') or []
                            if isinstance(item, dict) and
                            str(item.get('name') or '').strip()]
                        preview_delays = _test_nodes(
                            self._controller_request, controller_config,
                            preview_nodes, HEALTH_CHECK_URL, _progress,
                            _cancel_event, workers=8)
                finally:
                    _stop_temporary_process(process, close_job)
            try:
                with open(log_path, 'r', encoding='utf-8', errors='replace') as stream:
                    log_text = stream.read()[-12000:]
            except OSError:
                log_text = ''
            try:
                with open(provider_path, 'rb') as stream:
                    cache_payload = stream.read(SUBSCRIPTION_SIZE_LIMIT + 1)
            except OSError:
                cache_payload = None

        if not provider_payload:
            raise ProviderFetchError(
                _provider_preview_error(log_text, provider_seen))

        nodes = []
        for item in provider_payload.get('proxies') or []:
            if not isinstance(item, dict) or not str(item.get('name') or '').strip():
                continue
            tested_result = (preview_delays.get(str(item['name']))
                             if isinstance(preview_delays, dict) else None)
            delay = (tested_result.get('delay', 0)
                     if isinstance(tested_result, dict) else 0)
            tested = isinstance(tested_result, dict)
            nodes.append({
                'name': str(item['name']),
                'display_name': str(item['name']),
                'type': str(item.get('type') or ''),
                'alive': delay > 0 if tested else None,
                'tested': tested,
                'delay': delay,
                'tested_at': (tested_result.get('tested_at', 0)
                              if tested else 0),
            })
        if not nodes:
            raise RoutingError(_provider_preview_error(log_text, True))
        if not cache_payload or len(cache_payload) > SUBSCRIPTION_SIZE_LIMIT:
            raise RoutingError('订阅已解析，但内置节点缓存写入失败，请重试')
        if _cache_only:
            cache_source = '本地节点缓存'
        elif _cache_source:
            cache_source = str(_cache_source)
        elif provider['download_route'] == 'custom-proxy':
            cache_source = '自定义本地代理恢复'
        elif provider['download_route'] == 'system-proxy':
            cache_source = 'Windows 系统代理迁移'
        elif provider['download_route'] == 'physical':
            cache_source = '物理网络获取'
        elif cache_seeded:
            cache_source = '内置代理更新' if refreshed else '内置缓存'
        else:
            cache_source = '物理网络首次获取'
        with commit_scope():
            if _persist:
                if refresh_warning and cache_seeded and not candidate_seeded:
                    cache = subscription_store.cache_status(provider)
                else:
                    try:
                        cache = subscription_store.persist_bytes(
                            provider, cache_payload, len(nodes), cache_source,
                            size_limit=SUBSCRIPTION_SIZE_LIMIT)
                    except (OSError, ValueError) as exc:
                        raise RoutingError('订阅已解析，但内置节点缓存保存失败') from exc
            else:
                cache = subscription_store.cache_status(provider)
            snapshot_warning = ''
            if _persist_snapshot:
                try:
                    _selection.persist_provider_nodes(
                        normalized, provider['id'], nodes)
                except (OSError, ValueError):
                    snapshot_warning = '节点快照保存失败，下次打开需重新获取节点'
                    self.log('[routing] 订阅解析成功，但节点快照保存失败')
        route_label = (
            '本地节点缓存' if _cache_only
            else f'自定义代理 {_proxy_display(download["proxy_url"])}'
            if provider['download_route'] == 'custom-proxy'
            else f'Windows 系统代理 {_proxy_display(download["proxy_url"])}'
            if provider['download_route'] == 'system-proxy'
            else f'物理网络 {physical_interface or "自动"}'
            if provider['download_route'] == 'physical'
            else '智能自动更新（缓存节点、物理网络、Windows 系统代理）')
        used_cache = cache_seeded and not refreshed and not candidate_seeded
        if _cache_only:
            update_state = 'cache_tested'
            message = f'当前缓存节点测速完成，共检测 {len(nodes)} 个节点'
        elif candidate_seeded:
            update_state = 'cache_imported'
            message = f'节点文件导入成功，已解析并缓存 {len(nodes)} 个节点'
        elif used_cache:
            update_state = 'cache_retained'
            message = f'远端更新未完成，已载入上次成功缓存中的 {len(nodes)} 个节点'
        else:
            update_state = 'remote_updated'
            message = f'订阅远端更新成功，已解析并缓存 {len(nodes)} 个节点'
        return {
            'ok': True,
            'msg': (message +
                    (f'；{refresh_warning}' if refresh_warning else '') +
                    (f'；{snapshot_warning}' if snapshot_warning else '')),
            'provider_id': provider['id'],
            'provider_name': provider['name'],
            'nodes': nodes,
            'node_count': len(nodes),
            'checked_at': int(time.time()),
            'download_route': route_label,
            'cache': cache,
            'used_cache': used_cache,
            'refreshed': refreshed or not cache_seeded or candidate_seeded,
            'update_state': update_state,
            'warning': '；'.join(filter(None, [
                refresh_warning, snapshot_warning])),
            'tested': _run_delay_test,
            'selection_invalid': _selection.selection_missing(provider, nodes),
        }

    def test_preview_proxy_provider(self, value, network=None, progress=None, cancel_event=None):
        """只使用本地缓存，以无 TUN 临时进程测试预览节点。"""
        return self.preview_proxy_provider(
            value, network, _persist=False, _run_delay_test=True,
            _refresh_cache=False, _cache_only=True, _progress=progress,
            _cancel_event=cancel_event)

    def import_proxy_provider(self, value, content, network=None):
        """导入 Clash/Mihomo provider YAML，作为不依赖外部客户端的首次种子。"""
        source = dict(value) if isinstance(value, dict) else {}
        source['enabled'] = True
        normalized = normalize_config({
            'enabled': False,
            'proxy_providers': [source],
            'default_outbound': 'physical',
        })
        provider = normalized['proxy_providers'][0]
        if not isinstance(content, str):
            raise RoutingError('请选择 UTF-8 编码的 Clash/Mihomo YAML 文件')
        try:
            payload = content.encode('utf-8')
        except UnicodeError as exc:
            raise RoutingError('订阅文件不是有效的 UTF-8 文本') from exc
        if not payload or len(payload) > SUBSCRIPTION_SIZE_LIMIT:
            raise RoutingError('订阅文件为空或超过 10 MB 限制')
        verify_runtime()
        network_source = network if isinstance(network, dict) else {}
        physical_interface = str(
            network_source.get('physical_interface') or '').strip()
        with tempfile.TemporaryDirectory(prefix='cxvpn-provider-import-') as root:
            provider_path = os.path.join(root, 'providers', 'import.yaml')
            os.makedirs(os.path.dirname(provider_path), exist_ok=True)
            with open(provider_path, 'wb') as stream:
                stream.write(payload)
            direct = {'name': 'PHYSICAL', 'type': 'direct', 'udp': True}
            if physical_interface:
                direct['interface-name'] = physical_interface
            generated = {
                'mixed-port': 0,
                'allow-lan': False,
                'bind-address': '127.0.0.1',
                'mode': 'rule',
                'log-level': 'warning',
                'proxies': [direct],
                'proxy-providers': {
                    'imported': {
                        'type': 'file',
                        'path': './providers/import.yaml',
                        'health-check': {'enable': False},
                    },
                },
                'proxy-groups': [{
                    'name': 'IMPORTED',
                    'type': 'select',
                    'use': ['imported'],
                    'empty-fallback': 'REJECT',
                }],
                'rules': ['MATCH,DIRECT'],
            }
            config_path = os.path.join(root, 'config.json')
            _write_json(config_path, generated)
            self._test_config(config_path, root)
        preview_value = dict(provider)
        preview_value['download_route'] = 'auto'
        result = self.preview_proxy_provider(
            preview_value, network, _seed_payload=payload, _persist=False,
            _cache_source='导入 YAML', _persist_snapshot=False)
        try:
            with commit_scope():
                result['cache'], _snapshot = (
                    subscription_store.persist_bytes_and_nodes(
                        provider, payload, result['node_count'], '导入 YAML',
                        result.get('nodes') or [],
                        size_limit=SUBSCRIPTION_SIZE_LIMIT))
        except (OSError, ValueError) as exc:
            raise RoutingError('订阅文件解析通过，但缓存与节点快照保存失败') from exc
        result['msg'] = f'订阅文件已导入，解析并缓存 {result["node_count"]} 个节点'
        return result

    def test_proxy_group(self, config, group_id):
        return self.test_proxy_group_stream(config, group_id)

    def test_proxy_group_stream(self, config, group_id, progress=None, cancel_event=None):
        return _test_group(
            self, normalize_config(config), group_id, HEALTH_CHECK_URL,
            RoutingError, _selection.persist_provider_nodes, progress,
            cancel_event)

    def test_proxy_node(self, config, group_id, node_name):
        normalized = normalize_config(config)
        target = str(group_id or '').strip().lower()
        try:
            group = next((item for item in self.proxy_overview(normalized)
                          if item.get('id') == target), None)
            candidate = next((item for item in (group or {}).get('nodes') or []
                              if item.get('name') == node_name), None)
            if not candidate:
                raise RoutingError('所选节点不在当前代理组中，请刷新后重试')
            path = _node_healthcheck_path(
                candidate, HEALTH_CHECK_URL, timeout=5000)
            result = self._controller_request(normalized, path, timeout=8)
        except RoutingError:
            raise
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise RoutingError('节点测速失败，请确认统一分流服务正在运行') from exc
        delay = result.get('delay', 0) if isinstance(result, dict) else 0
        tested_node = {
            **candidate,
            'delay': delay if isinstance(delay, int) and delay > 0 else 0,
            'alive': isinstance(delay, int) and delay > 0,
            'tested': True,
            'tested_at': int(time.time()),
        }
        if target != 'all':
            try:
                merged = [
                    tested_node if item.get('name') == node_name else item
                    for item in group.get('nodes') or []]
                _selection.persist_provider_nodes(normalized, target, merged)
            except (OSError, ValueError):
                self.log('[routing] 单节点测速完成，但节点快照保存失败')
        message = (f'节点测速完成：{delay} ms' if delay else
                   '节点测速未通过：未取得有效延迟')
        return {'ok': True, 'msg': message,
                'delay': delay, 'groups': self.proxy_overview(normalized)}

    def select_proxy_node(self, config, group_id, node_name):
        normalized = normalize_config(config)
        internal_name, display_name, strategy = self._group_identity(
            normalized, group_id)
        if strategy != 'select':
            raise RoutingError(f'代理组“{display_name}”不是手动选择模式')
        path = f'/proxies/{urllib.parse.quote(internal_name, safe="")}'
        try:
            current = self._controller_request(normalized, path)
            candidates = current.get('all') if isinstance(current, dict) else []
            if node_name not in (candidates or []):
                raise RoutingError('所选节点不在当前代理组中，请刷新后重试')
            previous = str(current.get('now') or '')
            try:
                self._controller_request(
                    normalized, path, method='PUT', payload={'name': node_name})
                confirmed = self._controller_request(normalized, path)
                if not isinstance(confirmed, dict) or confirmed.get('now') != node_name:
                    raise RoutingError('节点切换后回读不一致')
            except (RoutingError, OSError, ValueError, urllib.error.URLError) as original:
                if previous and previous != node_name:
                    try:
                        self._controller_request(normalized, path, method='PUT', payload={'name': previous})
                        restored = self._controller_request(normalized, path)
                        if restored.get('now') != previous:
                            raise RoutingError('原节点回读不一致')
                    except Exception as rollback_error:
                        self._log_best_effort(f'[routing] 节点切换恢复失败: {type(rollback_error).__name__}')
                        raise RoutingError('节点切换失败且无法确认原节点，请刷新运行状态') from original
                raise RoutingError('节点切换未确认生效，已保留原节点偏好') from original
        except RoutingError:
            raise
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise RoutingError('无法连接正在运行的 Mihomo 控制端') from exc
        groups = self.proxy_overview(normalized)
        if not any(group.get('id') == group_id for group in groups):
            groups.append({'id': group_id, 'name': display_name, 'strategy': strategy,
                           'selected': node_name, 'nodes': [], 'partial': True})
        self._log_best_effort(f'[routing] 节点切换回读已确认: group={group_id}')
        return {'ok': True, 'msg': f'“{display_name}”已切换节点', 'groups': groups}

    def status(self, config, quick=False):
        normalized = normalize_config(config)
        service = self._service_state(allow_powershell=not quick)
        runtime_expected = (service.get('runtime_running')
                            if service.get('backend') == 'native'
                            else service.get('state') == 'Running')
        version = (MIHOMO_VERSION if quick and runtime_expected else
                   self._controller_version(normalized) if runtime_expected else '')
        runtime_mode = str(service.get('runtime_mode') or (
            'active' if runtime_expected else 'stopped'))
        core_running = bool(version)
        capture_ready = (normalized['capture_mode'] != 'system-proxy' or
                         bool(service.get('system_proxy_active')))
        running = bool(
            normalized['enabled'] and core_running and
            runtime_mode == 'active' and capture_ready)
        standby = bool(
            not normalized['enabled'] and core_running and
            runtime_mode == 'standby')
        return {
            'ok': True,
            'configured_enabled': normalized['enabled'],
            'installed': bool(service.get('installed')),
            'running': running,
            'core_running': core_running,
            'standby': standby,
            'runtime_mode': runtime_mode,
            'traffic_mode': normalized['traffic_mode'],
            'capture_mode': normalized['capture_mode'],
            'dns_mode': normalized['dns_mode'],
            'service_state': service.get('state', 'Unknown'),
            'service_backend': service.get('backend', 'unknown'),
            'service_version': service.get('service_version', ''),
            'service_update_required': bool(
                service.get('backend') == 'native' and
                service.get('service_version') !=
                _routing_service.SERVICE_VERSION),
            'mihomo_version': version or MIHOMO_VERSION,
            'winsw_version': WINSW_VERSION,
            'rule_count': len(normalized['rules']),
            'builtin_rule_pack': _routing_rules.summary(
                normalized['builtin_rule_pack']),
            'actual_rule_count': len(_effective_rules(normalized)),
            'provider_count': len([
                item for item in normalized['proxy_providers'] if item['enabled']]),
            'physical_interface': normalized['physical_interface'],
            'mixed_port': normalized['mixed_port'],
            'system_proxy_active': bool(service.get('system_proxy_active')),
            'fast_toggle_ready': bool(service.get('fast_toggle_ready')),
            'crash_fused': bool(service.get('crash_fused')),
            'msg': ('Mihomo 连续崩溃已熔断，系统接管已恢复，请查看诊断日志后重试'
                    if service.get('crash_fused') else
                    '统一分流正在运行' if running else
                    '代理未开启，节点核心正在待机' if standby else
                    '统一分流服务未运行' if normalized['enabled'] else
                    '统一分流未启用'),
        }

    def bootstrap(self, config):
        """仅读取本地配置、节点快照和原生服务 IPC，用于 UI 快速首屏。"""
        normalized = normalize_config(config)
        status = self.status(normalized, quick=True)
        provider_caches = {
            item['id']: subscription_store.cache_status(item)
            for item in normalized['proxy_providers']
        }
        provider_nodes = _selection.reconcile_provider_nodes(
            normalized, provider_caches, [])
        system_proxy = windows_system_proxy()
        return {
            'ok': True,
            'partial': True,
            'config': normalized,
            'vpns': [],
            'interfaces': [],
            'tun_conflicts': [],
            'status': status,
            'system_proxy': {
                'available': bool(system_proxy),
                'address': _proxy_display(system_proxy),
            },
            'builtin_rule_pack': _routing_rules.summary(
                normalized['builtin_rule_pack']),
            'builtin_rule_packs': _routing_rules.catalog(),
            'protected_fields': [
                'mixed-port', 'tun', 'external-controller',
                'secret', 'proxy-providers', 'proxy-groups', 'rules', 'dns'],
            'provider_caches': provider_caches,
            'provider_nodes': provider_nodes,
            'proxy_groups': [],
            'observability': {
                'available': False, 'active': 0, 'upload': 0,
                'download': 0, 'rule_hits': []},
        }

    def setup(self, config):
        normalized = normalize_config(config)
        environment = self._environment.snapshot(normalized['capture_mode'] == 'tun')
        vpns, interfaces, conflicts = (environment['vpns'], environment['interfaces'],
                                       environment['tun_conflicts'])
        status = self.status(normalized)
        system_proxy = windows_system_proxy()
        proxy_groups = (self.proxy_overview(normalized)
                        if status.get('core_running') else [])
        observability = (self.connection_observability(normalized)
                         if status['running'] else {
                             'available': False, 'active': 0, 'upload': 0,
                             'download': 0, 'rule_hits': []})
        provider_caches = {
            item['id']: subscription_store.cache_status(item)
            for item in normalized['proxy_providers']
        }
        provider_nodes = _selection.reconcile_provider_nodes(
            normalized, provider_caches, proxy_groups)
        return {
            'ok': True,
            'config': normalized,
            'vpns': vpns,
            'interfaces': interfaces,
            'tun_conflicts': conflicts,
            'status': status,
            'system_proxy': {
                'available': bool(system_proxy),
                'address': _proxy_display(system_proxy),
            },
            'builtin_rule_pack': _routing_rules.summary(
                normalized['builtin_rule_pack']),
            'builtin_rule_packs': _routing_rules.catalog(),
            'protected_fields': [
                'mixed-port', 'tun', 'external-controller',
                'secret', 'proxy-providers', 'proxy-groups', 'rules', 'dns'],
            'provider_caches': provider_caches,
            'provider_nodes': provider_nodes,
            'proxy_groups': proxy_groups,
            'observability': observability,
        }

    def _test_config(self, path, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        source_geoip = _binary_path(GEOIP_DATABASE)
        target_geoip = os.path.join(data_dir, GEOIP_DATABASE)
        if (not os.path.isfile(target_geoip) or
                _sha256(target_geoip).upper() != GEOIP_DATABASE_SHA256):
            shutil.copy2(source_geoip, target_geoip)
        if _sha256(target_geoip).upper() != GEOIP_DATABASE_SHA256:
            raise RoutingError('GeoIP 数据库完整性校验失败')
        try:
            result = subprocess.run(
                [_binary_path('mihomo.exe'), '-t', '-d', data_dir, '-f', path],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=45,
                creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            raise RoutingError('Mihomo 配置预检超时，请检查配置后重试') from exc
        except OSError as exc:
            raise RoutingError('无法启动 Mihomo 配置预检') from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or '').strip().splitlines()
            safe_detail = _sanitize_mihomo_error(
                detail[-1] if detail else '未知错误')
            raise RoutingError(f'Mihomo 配置预检失败：{safe_detail}')

    def _install_legacy(self, config_path, config):
        service_dir = self._service_dir
        wrapper = os.path.join(service_dir, f'{SERVICE_ID}.exe')
        xml_path = os.path.join(service_dir, f'{SERVICE_ID}.xml')
        source_mihomo = _binary_path('mihomo.exe')
        source_geoip = _binary_path(GEOIP_DATABASE)
        source_winsw = _binary_path('WinSW-x64.exe')
        expected_config_hash = _sha256(config_path)
        xml = _service_xml()
        controller_url = f'http://127.0.0.1:{config["controller_port"]}/version'
        authorization = f'Bearer {config["controller_secret"]}'
        referenced_provider_ids = _referenced_provider_ids(config)
        required_group_rows = []
        manual_targets = dict(_selection.manual_runtime_targets(
            config, referenced_provider_ids))
        for provider_id in sorted(referenced_provider_ids):
            group_name = f'PROXY-{provider_id}'
            selected_node = manual_targets.get(provider_id, '')
            group_url = (
                f'http://127.0.0.1:{config["controller_port"]}/proxies/'
                f'{urllib.parse.quote(group_name, safe="")}')
            body = (json.dumps({'name': selected_node}, ensure_ascii=False)
                    if selected_node else '')
            required_group_rows.append(
                '[pscustomobject]@{{ Url = {url}; Node = {node}; Body = {body} }}'.format(
                    url=_ps_literal(group_url), node=_ps_literal(selected_node),
                    body=_ps_literal(body)))
        required_groups_literal = '@(' + ','.join(required_group_rows) + ')'
        cache_copy_lines = []
        service_cache_names = []
        for provider in config.get('proxy_providers', []):
            if not provider.get('enabled'):
                continue
            service_cache_names.append(
                subscription_store.provider_filename(provider))
            source = subscription_store.cache_path(provider)
            if not subscription_store.cache_status(provider)['available']:
                continue
            destination = os.path.join(
                service_dir, 'data', 'providers',
                subscription_store.provider_filename(provider))
            cache_copy_lines.append(f'''
$cacheSource = {_ps_literal(os.path.realpath(source))}
$cacheTarget = {_ps_literal(destination)}
if (Test-Path -LiteralPath $cacheSource) {{
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $cacheTarget) | Out-Null
  $shouldCopy = -not (Test-Path -LiteralPath $cacheTarget)
  if (-not $shouldCopy) {{
    $shouldCopy = (Get-Item -LiteralPath $cacheSource).LastWriteTimeUtc -gt
      (Get-Item -LiteralPath $cacheTarget).LastWriteTimeUtc
  }}
  if ($shouldCopy) {{ Copy-Item -LiteralPath $cacheSource -Destination $cacheTarget -Force }}
}}
''')
        cache_copy_script = ''.join(cache_copy_lines)
        allowed_cache_literal = '@(' + ','.join(
            _ps_literal(name) for name in service_cache_names) + ')'
        script = f'''
$ErrorActionPreference = 'Stop'
$serviceId = {_ps_literal(SERVICE_ID)}
$target = {_ps_literal(service_dir)}
$wrapper = {_ps_literal(wrapper)}
$xmlPath = {_ps_literal(xml_path)}
$expectedMihomoHash = {_ps_literal(MIHOMO_SHA256)}
$expectedGeoIpHash = {_ps_literal(GEOIP_DATABASE_SHA256)}
$expectedWinSWHash = {_ps_literal(WINSW_SHA256)}
$expectedConfigHash = {_ps_literal(expected_config_hash)}
$parent = Split-Path -Parent $target
$backup = Join-Path $parent ('.RoutingService.rollback-' + [guid]::NewGuid().ToString('N'))
$targetFull = [IO.Path]::GetFullPath($target)
$parentFull = [IO.Path]::GetFullPath($parent)
$backupFull = [IO.Path]::GetFullPath($backup)
if (-not $backupFull.StartsWith(
    $parentFull.TrimEnd('\\') + '\\', [StringComparison]::OrdinalIgnoreCase)) {{
  throw 'Invalid rollback directory'
}}
$existing = Get-Service -Name $serviceId -ErrorAction SilentlyContinue
$hadExistingService = $null -ne $existing
$canRestore = $false
$oldConfigHash = ''
$requiredGroups = {required_groups_literal}
$authorization = {_ps_literal(authorization)}
function Invoke-CxvpnJson {{
  param(
    [Parameter(Mandatory=$true)][string]$Uri,
    [string]$Method = 'GET',
    [string]$Body = ''
  )
  $request = [Net.HttpWebRequest]::Create($Uri)
  $request.Method = $Method
  $request.Timeout = 3000
  $request.ReadWriteTimeout = 3000
  $request.Headers['Authorization'] = $authorization
  if ($Body) {{
    $bytes = [Text.Encoding]::UTF8.GetBytes($Body)
    $request.ContentType = 'application/json; charset=utf-8'
    $request.ContentLength = $bytes.Length
    $requestStream = $request.GetRequestStream()
    try {{ $requestStream.Write($bytes, 0, $bytes.Length) }}
    finally {{ $requestStream.Dispose() }}
  }}
  $response = $request.GetResponse()
  $text = ''
  try {{
    $stream = $response.GetResponseStream()
    if ($stream) {{
      $reader = New-Object IO.StreamReader(
        $stream, [Text.Encoding]::UTF8, $true)
      try {{ $text = $reader.ReadToEnd() }}
      finally {{ $reader.Dispose() }}
    }}
  }} finally {{ $response.Dispose() }}
  if ([string]::IsNullOrWhiteSpace($text)) {{ return $null }}
  return $text | ConvertFrom-Json
}}
New-Item -ItemType Directory -Force -Path $parent | Out-Null
New-Item -ItemType Directory -Force -Path $target | Out-Null
& icacls.exe $target /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) {{ throw "icacls target exit code $LASTEXITCODE" }}

if ($hadExistingService) {{
  $oldMihomo = Join-Path $target 'mihomo.exe'
  $oldConfig = Join-Path $target 'config.json'
  if (-not (Test-Path -LiteralPath $oldMihomo) -or
      -not (Test-Path -LiteralPath $wrapper) -or
      -not (Test-Path -LiteralPath $xmlPath) -or
      -not (Test-Path -LiteralPath $oldConfig)) {{
    throw 'Existing service files are incomplete; refusing an update without rollback data'
  }}
  if ((Get-FileHash -LiteralPath $oldMihomo -Algorithm SHA256).Hash -ne $expectedMihomoHash -or
      (Get-FileHash -LiteralPath $wrapper -Algorithm SHA256).Hash -ne $expectedWinSWHash) {{
    throw 'Existing service runtime checksum is invalid; refusing unsafe rollback'
  }}
  $oldConfigHash = (Get-FileHash -LiteralPath $oldConfig -Algorithm SHA256).Hash
  try {{
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    & icacls.exe $backup /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) {{ throw "icacls rollback exit code $LASTEXITCODE" }}
    Copy-Item -LiteralPath $oldMihomo -Destination (Join-Path $backup 'mihomo.exe') -Force
    Copy-Item -LiteralPath $wrapper -Destination (Join-Path $backup '{SERVICE_ID}.exe') -Force
    Copy-Item -LiteralPath $xmlPath -Destination (Join-Path $backup '{SERVICE_ID}.xml') -Force
    Copy-Item -LiteralPath $oldConfig -Destination (Join-Path $backup 'config.json') -Force
    $oldData = Join-Path $target 'data'
    if (Test-Path -LiteralPath $oldData) {{
      Copy-Item -LiteralPath $oldData -Destination $backup -Recurse -Force
    }}
    if ((Get-FileHash -LiteralPath (Join-Path $backup 'mihomo.exe') -Algorithm SHA256).Hash -ne $expectedMihomoHash -or
        (Get-FileHash -LiteralPath (Join-Path $backup '{SERVICE_ID}.exe') -Algorithm SHA256).Hash -ne $expectedWinSWHash -or
        (Get-FileHash -LiteralPath (Join-Path $backup 'config.json') -Algorithm SHA256).Hash -ne $oldConfigHash) {{
      throw 'Rollback backup checksum validation failed'
    }}
    $canRestore = $true
  }} catch {{
    if (Test-Path -LiteralPath $backup) {{
      Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
    }}
    throw
  }}
}}

try {{
  if ($existing -and $existing.Status -ne 'Stopped') {{
    Stop-Service -Name $serviceId -Force -ErrorAction Stop
  }}
  New-Item -ItemType Directory -Force -Path (Join-Path $target 'data') | Out-Null
  Copy-Item -LiteralPath {_ps_literal(source_mihomo)} -Destination (Join-Path $target 'mihomo.exe') -Force
  Copy-Item -LiteralPath {_ps_literal(source_geoip)} -Destination (Join-Path (Join-Path $target 'data') '{GEOIP_DATABASE}') -Force
  Copy-Item -LiteralPath {_ps_literal(source_winsw)} -Destination $wrapper -Force
  Copy-Item -LiteralPath {_ps_literal(config_path)} -Destination (Join-Path $target 'config.json') -Force
  if ((Get-FileHash -LiteralPath (Join-Path $target 'mihomo.exe') -Algorithm SHA256).Hash -ne $expectedMihomoHash) {{
    throw 'Copied Mihomo checksum validation failed'
  }}
  if ((Get-FileHash -LiteralPath (Join-Path (Join-Path $target 'data') '{GEOIP_DATABASE}') -Algorithm SHA256).Hash -ne $expectedGeoIpHash) {{
    throw 'Copied GeoIP database checksum validation failed'
  }}
  if ((Get-FileHash -LiteralPath $wrapper -Algorithm SHA256).Hash -ne $expectedWinSWHash) {{
    throw 'Copied WinSW checksum validation failed'
  }}
  if ((Get-FileHash -LiteralPath (Join-Path $target 'config.json') -Algorithm SHA256).Hash -ne $expectedConfigHash) {{
    throw 'Copied routing config checksum validation failed'
  }}
  $providerDir = Join-Path (Join-Path $target 'data') 'providers'
  New-Item -ItemType Directory -Force -Path $providerDir | Out-Null
  $allowedProviderCaches = {allowed_cache_literal}
  Get-ChildItem -LiteralPath $providerDir -File -Filter '*.yaml' -ErrorAction SilentlyContinue |
    Where-Object {{ $allowedProviderCaches -notcontains $_.Name }} |
    Remove-Item -Force -ErrorAction Stop
{cache_copy_script}
  Set-Content -LiteralPath $xmlPath -Value {_ps_literal(xml)} -Encoding UTF8
  if ($hadExistingService) {{
    & sc.exe delete $serviceId | Out-Null
    if ($LASTEXITCODE -ne 0) {{ throw "sc delete exit code $LASTEXITCODE" }}
    Start-Sleep -Milliseconds 800
  }}
  & $wrapper install
  if ($LASTEXITCODE -ne 0) {{ throw "WinSW install exit code $LASTEXITCODE" }}
  & $wrapper start
  if ($LASTEXITCODE -ne 0) {{ throw "WinSW start exit code $LASTEXITCODE" }}
  $ready = $false
  for ($attempt = 0; $attempt -lt 40; $attempt++) {{
    try {{
      Invoke-CxvpnJson -Uri {_ps_literal(controller_url)} | Out-Null
      $ready = $true
      break
    }} catch {{ Start-Sleep -Milliseconds 250 }}
  }}
  if (-not $ready) {{ throw 'Mihomo controller did not become ready' }}
  foreach ($required in $requiredGroups) {{
    $groupReady = $false
    for ($attempt = 0; $attempt -lt 80; $attempt++) {{
      try {{
        $groupState = Invoke-CxvpnJson -Uri $required.Url
        $members = @($groupState.all)
        if ($required.Node) {{
          $groupReady = $members -contains $required.Node
        }} else {{
          $groupReady = @($members | Where-Object {{ $_ -and $_ -ne 'REJECT' }}).Count -gt 0
        }}
        if ($groupReady) {{
          $groupReady = $true
          break
        }}
      }} catch {{}}
      Start-Sleep -Milliseconds 250
    }}
    if (-not $groupReady) {{
      if ($required.Node) {{ throw 'Selected manual proxy node did not become ready' }}
      throw 'Referenced proxy provider group did not become ready'
    }}
    if ($required.Node) {{
      Invoke-CxvpnJson -Uri $required.Url -Method Put -Body $required.Body | Out-Null
      $selectedState = Invoke-CxvpnJson -Uri $required.Url
      if (-not $selectedState -or $selectedState.now -ne $required.Node) {{
        throw 'Manual proxy selection could not be confirmed'
      }}
    }}
  }}
  if (Test-Path -LiteralPath $backup) {{
    Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
  }}
}} catch {{
  $originalError = $_
  Stop-Service -Name $serviceId -Force -ErrorAction SilentlyContinue
  & sc.exe delete $serviceId | Out-Null
  if ($canRestore) {{
    try {{
      $dataTarget = [IO.Path]::GetFullPath((Join-Path $target 'data'))
      if (-not $dataTarget.StartsWith(
          $targetFull.TrimEnd('\\') + '\\', [StringComparison]::OrdinalIgnoreCase)) {{
        throw 'Invalid service data directory'
      }}
      if (Test-Path -LiteralPath $dataTarget) {{
        Remove-Item -LiteralPath $dataTarget -Recurse -Force -ErrorAction Stop
      }}
      Copy-Item -LiteralPath (Join-Path $backup 'mihomo.exe') -Destination (Join-Path $target 'mihomo.exe') -Force
      Copy-Item -LiteralPath (Join-Path $backup '{SERVICE_ID}.exe') -Destination $wrapper -Force
      Copy-Item -LiteralPath (Join-Path $backup '{SERVICE_ID}.xml') -Destination $xmlPath -Force
      Copy-Item -LiteralPath (Join-Path $backup 'config.json') -Destination (Join-Path $target 'config.json') -Force
      if (Test-Path -LiteralPath (Join-Path $backup 'data')) {{
        Copy-Item -LiteralPath (Join-Path $backup 'data') -Destination $target -Recurse -Force
      }}
      & icacls.exe $target /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
      if ($LASTEXITCODE -ne 0) {{ throw "rollback icacls exit code $LASTEXITCODE" }}
      if ((Get-FileHash -LiteralPath (Join-Path $target 'mihomo.exe') -Algorithm SHA256).Hash -ne $expectedMihomoHash -or
          (Get-FileHash -LiteralPath $wrapper -Algorithm SHA256).Hash -ne $expectedWinSWHash -or
          (Get-FileHash -LiteralPath (Join-Path $target 'config.json') -Algorithm SHA256).Hash -ne $oldConfigHash) {{
        throw 'Restored service checksum validation failed'
      }}
      & $wrapper install
      if ($LASTEXITCODE -ne 0) {{ throw "rollback WinSW install exit code $LASTEXITCODE" }}
      & $wrapper start
      if ($LASTEXITCODE -ne 0) {{ throw "rollback WinSW start exit code $LASTEXITCODE" }}
      $restored = $false
      for ($attempt = 0; $attempt -lt 20; $attempt++) {{
        $restoredService = Get-Service -Name $serviceId -ErrorAction SilentlyContinue
        if ($restoredService -and $restoredService.Status -eq 'Running') {{
          $restored = $true
          break
        }}
        Start-Sleep -Milliseconds 250
      }}
      if (-not $restored) {{ throw 'Restored service did not reach Running state' }}
      Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue
    }} catch {{
      throw "Routing update failed: $($originalError.Exception.Message); rollback also failed: $($_.Exception.Message)"
    }}
  }}
  throw $originalError
}}
'''
        _run_elevated(script)

    def _uninstall_legacy(self):
        state = self._service_state()
        if state.get('state') == 'Unknown':
            raise RoutingError('无法确认统一分流服务状态，为避免代理继续运行，已取消关闭操作')
        if not state.get('installed'):
            return
        script = f'''
$ErrorActionPreference = 'Stop'
$serviceId = {_ps_literal(SERVICE_ID)}
$service = Get-Service -Name $serviceId -ErrorAction SilentlyContinue
if ($service -and $service.Status -ne 'Stopped') {{
  Stop-Service -Name $serviceId -Force -ErrorAction Stop
}}
if ($service) {{
  & sc.exe delete $serviceId | Out-Null
  if ($LASTEXITCODE -ne 0) {{ throw "sc delete exit code $LASTEXITCODE" }}
}}
'''
        _run_elevated(script)

    def _native_provider_files(self, config):
        rows = []
        for provider in config.get('proxy_providers', []):
            if not provider.get('enabled'):
                continue
            path = subscription_store.cache_path(provider)
            if not subscription_store.cache_status(provider).get('available'):
                continue
            if not os.path.isfile(path):
                continue
            rows.append({
                'name': subscription_store.provider_filename(provider),
                'path': os.path.realpath(path),
            })
        return rows

    def _wait_native_ready(self, config, runtime_mode='active'):
        started_at = time.monotonic()
        self.log('[routing] 原生服务已领取配置事务，开始等待 Mihomo Controller（超时 25 秒）')
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self._controller_version(config):
                break
            time.sleep(0.25)
        else:
            raise RoutingError(ELEVATED_ERROR_MESSAGES[
                'Mihomo controller did not become ready'])
        self.log(
            f'[routing] Mihomo Controller 已就绪，耗时 {time.monotonic() - started_at:.1f} 秒')

        if runtime_mode == 'standby':
            self.log('[routing] 待机核心已就绪，不执行系统接管与出口连通性门控')
            return

        referenced = _referenced_provider_ids(config)
        manual_targets = dict(_selection.manual_runtime_targets(
            config, referenced))
        pending = {
            provider_id: (f'PROXY-{provider_id}',
                          manual_targets.get(provider_id, ''))
            for provider_id in referenced
        }
        while pending and time.monotonic() < deadline:
            for provider_id, (group_name, selected_node) in list(pending.items()):
                path = f'/proxies/{urllib.parse.quote(group_name, safe="")}'
                try:
                    state = self._controller_request(config, path)
                except (OSError, ValueError, urllib.error.URLError):
                    continue
                members = state.get('all') if isinstance(state, dict) else []
                ready = (selected_node in (members or []) if selected_node else
                         any(item and item != 'REJECT' for item in (members or [])))
                if ready:
                    pending.pop(provider_id, None)
            if pending:
                time.sleep(0.25)
        if pending:
            if any(value[1] for value in pending.values()):
                raise RoutingError(ELEVATED_ERROR_MESSAGES[
                    'Selected manual proxy node did not become ready'])
            raise RoutingError(ELEVATED_ERROR_MESSAGES[
                'Referenced proxy provider group did not become ready'])
        if referenced:
            self.log(f'[routing] 被引用代理组已加载，共 {len(referenced)} 个订阅')

        for provider_id, selected_node in manual_targets.items():
            group_name = f'PROXY-{provider_id}'
            path = f'/proxies/{urllib.parse.quote(group_name, safe="")}'
            try:
                self._controller_request(
                    config, path, method='PUT', payload={'name': selected_node})
                state = self._controller_request(config, path)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                raise RoutingError(ELEVATED_ERROR_MESSAGES[
                    'Manual proxy selection could not be confirmed']) from exc
            if not isinstance(state, dict) or state.get('now') != selected_node:
                raise RoutingError(ELEVATED_ERROR_MESSAGES[
                    'Manual proxy selection could not be confirmed'])
        if manual_targets:
            self.log(f'[routing] 手动节点已应用并回读确认，共 {len(manual_targets)} 个')

        self._verify_runtime_egress(config)

    @staticmethod
    def _traffic_proxy_groups(config):
        values = [config.get('default_outbound', '')]
        values.extend(rule.get('outbound', '') for rule in _active_rules(config))
        result = []
        for outbound in values:
            group = ('PROXY' if outbound == 'proxy' else
                     f'PROXY-{outbound[6:]}' if outbound.startswith('proxy:')
                     else '')
            if group and group not in result:
                result.append(group)
        return result

    def _verify_runtime_egress(self, config):
        """提交前确认当前选择，并以真实接管链路作为唯一可用性门控。"""
        groups = self._traffic_proxy_groups(config)
        if groups:
            self.log(
                f'[routing] 开始确认实际代理出口，共 {len(groups)} 个流量组')
            for group_name in groups:
                group_path = f'/proxies/{urllib.parse.quote(group_name, safe="")}'
                try:
                    state = self._controller_request(config, group_path, timeout=5)
                except (OSError, ValueError, urllib.error.URLError) as exc:
                    raise RoutingError(
                        f'无法确认代理组“{group_name}”的当前节点，已恢复 Windows 原始路由') from exc
                selected = str(state.get('now') or '') if isinstance(state, dict) else ''
                if not selected or selected == 'REJECT':
                    raise RoutingError(
                        f'代理组“{group_name}”没有可用节点，已恢复 Windows 原始路由')
                members = state.get('all') if isinstance(state, dict) else []
                if isinstance(members, list) and members and selected not in members:
                    raise RoutingError(
                        f'代理组“{group_name}”的当前节点状态不一致，'
                        '已恢复 Windows 原始路由')
                self.log(f'[routing] 代理组 {group_name} 当前节点已确认')

        if config['capture_mode'] == 'system-proxy':
            address = f'http://127.0.0.1:{config["mixed_port"]}'
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({
                'http': address, 'https': address}))
            self._probe_connectivity(opener, '显式本地 mixed-port 链路')
        else:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            self._probe_connectivity(opener, 'TUN 系统链路')

    def _probe_connectivity(self, opener, source):
        deadline = time.monotonic() + 8
        def check(endpoint):
            host = urllib.parse.urlparse(endpoint).hostname or 'endpoint'
            started = time.monotonic()
            self._log_best_effort(f'[routing] 开始验证{source}：目标={host}，单请求超时=4秒，总期限=8秒')
            request = urllib.request.Request(endpoint, headers={'User-Agent': 'CXVPN-Connectivity-Check/1.0'})
            try:
                with opener.open(request, timeout=max(.1, min(4, deadline-time.monotonic()))) as response:
                    status = int(getattr(response, 'status', 0) or 0)
                    response.read(64)
                if not 200 <= status < 400:
                    raise OSError(f'HTTP_{status}')
                self._log_best_effort(f'[routing] {source}验证成功：目标={host}，状态={status}，耗时={time.monotonic()-started:.1f}秒')
                return endpoint
            except Exception as exc:
                reason = getattr(exc, 'reason', exc)
                category = ('timeout' if isinstance(reason, (TimeoutError, socket.timeout)) else
                            'dns' if isinstance(reason, socket.gaierror) else 'network')
                self._log_best_effort(f'[routing] {source}验证失败：目标={host}，分类={category}，异常={type(exc).__name__}，耗时={time.monotonic()-started:.1f}秒')
                raise
        for endpoints in (CONNECTIVITY_CHECK_URLS[:2], CONNECTIVITY_CHECK_URLS[2:]):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            results, _errors = bounded_calls(
                {url: lambda url=url: check(url) for url in endpoints},
                min(4.5, remaining), first_success=True)
            if results:
                return next(iter(results.values()))
        raise RoutingError(f'{source}当前无法建立可用连接，已恢复 Windows 原始路由。请检查当前节点或网络设置')

    def _install(self, config_path, config, runtime_mode='active',
                 fast_toggle_ready=False, allow_reload=True):
        started_at = time.monotonic()
        mode_label = '待机核心' if runtime_mode == 'standby' else '系统接管'
        self.log(
            f'[routing] {mode_label}配置事务已提交到原生服务，'
            '等待后台领取（安装超时 120 秒，单次配置 IPC 总期限 60 秒）')
        try:
            system_proxy_domains = system_proxy_bypass_domains(config)
            if config['capture_mode'] == 'system-proxy':
                manual_count = len(config['system_proxy_bypass']['domains'])
                self._log_best_effort(
                    f'[routing] Windows 系统代理绕过域名已合并：'
                    f'手动={manual_count}，规则包引用='
                    f'{"开启" if config["system_proxy_bypass"]["include_cn_direct"] else "关闭"}，'
                    f'实际={len(system_proxy_domains)}')
            self._native_service.ensure_installed()
            transaction = self._native_service.apply(
                config_path, self._native_provider_files(config), runtime_mode,
                fast_toggle_ready=fast_toggle_ready,
                system_proxy_bypass_domains=system_proxy_domains,
                allow_reload=allow_reload)
        except _routing_service.ServiceError as exc:
            raise RoutingError(
                f'统一分流服务操作失败：{_sanitize_mihomo_error(exc)}') from exc
        transaction_id = str(transaction.get('transaction_id') or '')
        if not transaction_id:
            raise RoutingError('统一分流服务未返回配置事务标识')
        self.log('[routing] 原生服务已领取配置事务，开始确认运行时')
        reload_failed = False
        try:
            if transaction.get('hot_reload'):
                self.log('[routing] 运行配置热重载开始执行，超时=10秒')
                try:
                    self._controller_request(config, '/configs?force=true', method='PUT',
                        payload={'path': transaction['config_path']}, timeout=10)
                except Exception:
                    reload_failed = True
                    raise
                self.log('[routing] 运行配置热重载成功，开始回读与出口验证')
            self._wait_native_ready(config, runtime_mode)
            if (runtime_mode == 'active' and
                    config['capture_mode'] == 'system-proxy'):
                self._log_best_effort(
                    '[routing] 显式本地链路已通过，开始在候选事务中启用 Windows 系统代理')
                self._native_service.activate_system_proxy(transaction_id)
                active_proxy = windows_system_proxy()
                expected_proxy = f'http://127.0.0.1:{config["mixed_port"]}'
                if active_proxy != expected_proxy:
                    raise RoutingError(
                        'Windows 系统代理写入后回读不一致，已自动恢复原设置')
                self._log_best_effort('[routing] Windows 系统代理回读已确认，与刚验证的 mixed-port 链路一致')
            signature = self._config_signature(config)
            self._native_service.commit(transaction_id)
            self._runtime_signature = signature
            self._runtime_sha256 = str(transaction.get('config_sha256') or '').upper()
            self._log_best_effort(
                f'[routing] {mode_label}配置事务提交成功，总耗时 '
                f'{time.monotonic() - started_at:.1f} 秒')
        except Exception as original:
            self._log_best_effort(
                f'[routing] 配置事务执行失败，开始回滚：{type(original).__name__}: '
                f'{_sanitize_mihomo_error(original)}')
            try:
                self._native_service.rollback(transaction_id)
                self._log_best_effort(
                    '[routing] 配置事务回滚成功，Windows 原始路由已恢复')
            except _routing_service.ServiceError as rollback_error:
                raise RoutingError(
                    f'{_sanitize_mihomo_error(original)}；且服务回滚失败：'
                    f'{_sanitize_mihomo_error(rollback_error)}') from original
            if reload_failed and allow_reload:
                self._log_best_effort('[routing] 热重载失败并已回滚，回退到完整配置事务')
                return self._install(config_path, config, runtime_mode,
                                     fast_toggle_ready, allow_reload=False)
            try:
                diagnostics = self._native_service.diagnostics()
                lines = diagnostics.get('mihomo_log') if isinstance(
                    diagnostics, dict) else []
                for line in (lines or [])[-3:]:
                    self._log_best_effort(
                        f'[routing] Mihomo 诊断: {_sanitize_mihomo_error(line)}')
            except Exception as diagnostic_error:
                self._log_best_effort(
                    f'[routing] Mihomo 诊断读取失败: {type(diagnostic_error).__name__}')
            if isinstance(original, RoutingError):
                raise
            raise RoutingError(_sanitize_mihomo_error(original)) from original

    def _start_standby_runtime(self, config):
        """在不接管系统流量时保留仅回环核心，供订阅更新与节点测速。"""
        verify_runtime()
        staging = os.path.join(self._data_dir, 'staging-standby')
        test_data = os.path.join(staging, 'data')
        config_path = os.path.join(staging, 'config.json')
        os.makedirs(test_data, exist_ok=True)
        fast_toggle_ready = config['capture_mode'] == 'system-proxy'
        if fast_toggle_ready:
            self._log_best_effort(
                '[routing] 快切待机配置开始生成：保留完整规则与节点组，系统代理=off')
            vpns = self._environment.snapshot(False)['vpns']
            excludes, _warnings = _resolve_vpn_server_routes(
                vpns, set(_target_vpns(config)))
            generated = build_mihomo_config(
                config, vpns, excludes, windows_system_proxy())
        else:
            self._log_best_effort(
                '[routing] 待机核心配置开始生成：TUN=off，系统代理=off，监听仅限回环')
            generated = build_mihomo_config(
                config, [], system_proxy=windows_system_proxy(), standby=True)
        _write_json(config_path, generated)
        self._test_config(config_path, test_data)
        self._install(
            config_path, config, runtime_mode='standby',
            fast_toggle_ready=fast_toggle_ready)

    @staticmethod
    def _can_fast_toggle_system_proxy(config, service_state):
        return bool(
            config['capture_mode'] == 'system-proxy' and
            service_state.get('installed') and
            service_state.get('backend') == 'native' and
            service_state.get('runtime_running') and
            service_state.get('fast_toggle_ready') and
            service_state.get('runtime_mode') in {'active', 'standby'} and
            not service_state.get('crash_fused'))

    def _fast_toggle_system_proxy(self, config, enabled):
        started_at = time.monotonic()
        action = '开启' if enabled else '关闭'
        self.log(
            f'[routing] Windows 系统代理快切请求已提交：target={action}，'
            '复用常驻 Mihomo，不重载配置')
        if enabled:
            for provider_id, target in _selection.manual_runtime_targets(
                    config, _referenced_provider_ids(config)):
                path = f'/proxies/PROXY-{provider_id}'
                state = self._controller_request(config, path, timeout=3)
                if not isinstance(state, dict) or state.get('now') != target:
                    self.select_proxy_node(config, provider_id, target)
            # 在写入 Windows 系统代理前验证当前常驻核心的真实 mixed-port
            # 链路；配置未变化时不重复扫描 VPN/网卡或执行 Mihomo -t。
            self._verify_runtime_egress(config)
        try:
            self._native_service.set_system_proxy_enabled(enabled)
        except _routing_service.ServiceError as exc:
            raise RoutingError(
                f'系统代理{action}失败：{_sanitize_mihomo_error(exc)}') from exc
        if enabled:
            expected = f'http://127.0.0.1:{config["mixed_port"]}'
            if windows_system_proxy() != expected:
                try:
                    self._native_service.set_system_proxy_enabled(False)
                except _routing_service.ServiceError:
                    pass
                raise RoutingError(
                    'Windows 系统代理开启后回读不一致，已自动恢复原设置')
        self.log(
            f'[routing] Windows 系统代理快切完成：target={action}，耗时 '
            f'{time.monotonic() - started_at:.2f} 秒')
        return {
            'ok': True,
            'msg': '代理已开启' if enabled else '代理已关闭，节点核心保持待机',
            'config': config,
            'warnings': [],
            'standby_pending': False,
            'status': self.status(config),
        }

    def apply(self, value, defer_standby=False, allow_fast_toggle=False):
        config = normalize_config(value)
        self.log(
            f'[routing] 收到分流应用请求：enabled={config["enabled"]}，'
            f'mode={config["traffic_mode"]}，规则 {len(config["rules"])} 条')
        service_state = self._service_state()
        if (allow_fast_toggle and
                self._can_fast_toggle_system_proxy(config, service_state) and
                (not config['enabled'] or self.runtime_matches(config, service_state))):
            return self._fast_toggle_system_proxy(config, config['enabled'])
        if not config['enabled']:
            if service_state.get('state') == 'Unknown':
                raise RoutingError(
                    '无法确认统一分流服务是否仍在运行，未保存关闭状态；请稍后重试')
            installed = bool(service_state.get('installed'))
            warnings = []
            enabled_providers = [
                item for item in config['proxy_providers']
                if item.get('enabled')]
            if installed:
                if service_state.get('backend') == 'native':
                    try:
                        self._native_service.stop_runtime()
                    except _routing_service.ServiceError as exc:
                        raise RoutingError(
                            f'关闭统一分流失败：{_sanitize_mihomo_error(exc)}') from exc
                    self.log('[routing] 系统流量接管已停止，Windows 原始路由已恢复')
                    if enabled_providers and not defer_standby:
                        try:
                            self._start_standby_runtime(config)
                            self.log(
                                f'[routing] 节点待机核心已启动，共加载 '
                                f'{len(enabled_providers)} 个订阅配置')
                        except Exception as exc:
                            warning = (
                                '代理已安全关闭，但节点待机核心启动失败；'
                                '订阅更新和测速将回退到临时核心')
                            warnings.append(warning)
                            self._log_best_effort(
                                f'[routing] 待机核心启动失败，保持系统接管关闭：'
                                f'{type(exc).__name__}: '
                                f'{_sanitize_mihomo_error(exc)}')
                    elif enabled_providers:
                        self.log(
                            '[routing] 系统接管关闭已完成，节点待机核心转入后台启动')
                else:
                    self._uninstall_legacy()
                    self.log('[routing] 旧版统一分流服务已停止并注销')
            else:
                self.log('[routing] 分流配置已保存，统一分流保持关闭')
            standby_started = bool(
                installed and not warnings and
                self._service_state(allow_powershell=False).get(
                    'runtime_mode') == 'standby')
            return {'ok': True,
                    'msg': ('代理已关闭，节点核心正在后台启动'
                            if defer_standby and installed and enabled_providers else
                            '代理已关闭，节点核心保持待机'
                            if standby_started else
                            '统一分流已关闭' if installed else
                            '分流配置已保存'),
                    'config': config,
                    'warnings': warnings,
                    'standby_pending': bool(
                        defer_standby and installed and enabled_providers),
                    'status': self.status(config)}

        verify_runtime()
        self.log('[routing] 开始执行启用前校验：运行时、节点偏好、接口、VPN 与 TUN 冲突')
        referenced_provider_ids = _referenced_provider_ids(config)
        selection_error = _selection.manual_selection_error(
            config, referenced_provider_ids)
        if selection_error:
            raise RoutingError(selection_error)
        environment = self._environment.snapshot(config['capture_mode'] == 'tun', fresh=True)
        vpns, interfaces, conflicts = (environment['vpns'], environment['interfaces'],
                                       environment['tun_conflicts'])
        warnings = validate_environment(config, vpns, interfaces, conflicts)
        excludes, resolve_warnings = _resolve_vpn_server_routes(
            vpns, set(_target_vpns(config)))
        warnings.extend(resolve_warnings)
        staging = os.path.join(self._data_dir, 'staging')
        test_data = os.path.join(staging, 'data')
        config_path = os.path.join(staging, 'config.json')
        os.makedirs(test_data, exist_ok=True)
        generated = build_mihomo_config(
            config, vpns, excludes, windows_system_proxy())
        _write_json(config_path, generated)
        self.log('[routing] Mihomo 候选配置已生成，开始离线语法预检（超时 45 秒）')
        self._test_config(config_path, test_data)
        self.log('[routing] Mihomo 候选配置离线预检通过，开始应用服务事务')
        self._install(
            config_path, config,
            fast_toggle_ready=config['capture_mode'] == 'system-proxy')
        self.log(f'[routing] 统一分流已启动，规则 {len(config["rules"])} 条')
        return {
            'ok': True,
            'msg': f'统一分流已启用，共加载 {len(config["rules"])} 条域名规则',
            'config': config,
            'warnings': warnings,
            'status': self.status(config),
        }

    def preview(self, value):
        config = normalize_config(value)
        system_proxy_domains = system_proxy_bypass_domains(config)
        environment = self._environment.snapshot(config['capture_mode'] == 'tun')
        vpns = environment['vpns']
        warnings = validate_environment(config, vpns, environment['interfaces'], environment['tun_conflicts'])
        generated = build_mihomo_config(
            config, vpns, system_proxy=windows_system_proxy())
        verify_runtime()
        with tempfile.TemporaryDirectory(prefix='cxvpn-routing-preview-') as root:
            path = os.path.join(root, 'config.json')
            _write_json(path, generated)
            self._test_config(path, os.path.join(root, 'data'))
        return {'ok': True, 'msg': '配置预检通过', 'warnings': warnings,
                'rule_count': len(config['rules']),
                'system_proxy_bypass_domain_count': len(system_proxy_domains)}
