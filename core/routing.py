# -*- coding: utf-8 -*-
"""Windows 统一域名分流控制层（Mihomo + WinSW）。"""
from __future__ import annotations

import fnmatch
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

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
from core import routing_rules as _routing_rules
from core import routing_service as _routing_service
from core.routing_speedtest import (
    node_healthcheck_path as _node_healthcheck_path,
    test_group as _test_group,
    test_nodes as _test_nodes,
)


SERVICE_ID = 'CXVPNRoutingService'
SERVICE_DIR_NAME = 'CXVPNManager\\RoutingService'
MIHOMO_VERSION = 'v1.19.30'
MIHOMO_SHA256 = 'F55B3028D9160BEB9044F21B05DD7405B46524614A19642D6291492F5F985761'
WINSW_VERSION = 'v2.12.0'
WINSW_SHA256 = '05B82D46AD331CC16BDC00DE5C6332C1EF818DF8CEEFCD49C726553209B3A0DA'
DOMAIN_RE = re.compile(r'^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$', re.I)
WILDCARD_RE = re.compile(r'^[a-z0-9*?](?:[a-z0-9*?.-]{0,251}[a-z0-9*?])?$', re.I)
PROVIDER_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,39}$', re.I)
PROXY_STRATEGIES = {'url-test', 'fallback', 'select'}
SELECTION_MODES = {'auto', 'manual'}
TRAFFIC_MODES = {'rule', 'global'}
CAPTURE_MODES = {'system-proxy', 'tun'}
DOWNLOAD_ROUTES = {'auto', 'physical', 'system-proxy', 'custom-proxy'}
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
        'mixed_port': 17890,
        'dns_servers': ['223.5.5.5', '1.1.1.1'],
        'default_nameserver': ['223.5.5.5', '1.1.1.1'],
        'proxy_server_nameserver': ['223.5.5.5', '1.1.1.1'],
        'direct_nameserver': ['223.5.5.5', '119.29.29.29'],
        'controller_port': 19090,
        'controller_secret': '',
        'rules': [],
    }


def verify_runtime():
    verify_runtime_files(_runtime_dir(), {
        'mihomo.exe': MIHOMO_SHA256,
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

    def normalize_dns(field, fallback):
        values = source.get(field)
        if values is None and field != 'dns_servers':
            values = source.get('dns_servers')
        if isinstance(values, str):
            values = re.split(r'[,\s]+', values)
        if not isinstance(values, list):
            values = []
        cleaned = [str(item).strip() for item in values if str(item).strip()][:8]
        return cleaned or list(fallback)

    result['dns_servers'] = normalize_dns(
        'dns_servers', default_config()['dns_servers'])
    result['default_nameserver'] = normalize_dns(
        'default_nameserver', default_config()['default_nameserver'])
    result['proxy_server_nameserver'] = normalize_dns(
        'proxy_server_nameserver', default_config()['proxy_server_nameserver'])
    result['direct_nameserver'] = normalize_dns(
        'direct_nameserver', default_config()['direct_nameserver'])

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


def _active_rules(config):
    if config.get('traffic_mode') == 'global':
        return []
    return [rule for rule in config.get('rules', []) if rule.get('enabled', True)]


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
    matched = None
    for index, rule in enumerate(_active_rules(config), start=1):
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
    if not matched and config['traffic_mode'] != 'global':
        for raw in _routing_rules.rules_for(config['builtin_rule_pack']):
            parts = raw.split(',')
            if parts[0] == 'DOMAIN-SUFFIX' and (
                    normalized_domain == parts[1] or
                    normalized_domain.endswith('.' + parts[1])):
                builtin_match = parts
                break
    outbound = (matched[1]['outbound'] if matched else
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

    return {
        'domain': normalized_domain,
        'matched': bool(matched or builtin_match),
        'source': 'user' if matched else 'builtin' if builtin_match else 'default',
        'rule_index': matched[0] if matched else 0,
        'match_type': matched[1]['match_type'] if matched else 'suffix' if builtin_match else 'default',
        'rule_domain': matched[1]['domain'] if matched else builtin_match[1] if builtin_match else '',
        'outbound': outbound,
        'outbound_name': outbound_name,
        'detail': detail,
        'available': available,
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
    routes = set()
    warnings = []
    for profile in vpns:
        if profile.get('name') not in selected_names:
            continue
        server = str(profile.get('server') or '').strip()
        if not server:
            continue
        try:
            address = ipaddress.ip_address(server)
            routes.add(f'{address}/{32 if address.version == 4 else 128}')
            continue
        except ValueError:
            pass
        try:
            for info in socket.getaddrinfo(server, None, type=socket.SOCK_STREAM):
                address = ipaddress.ip_address(info[4][0])
                routes.add(f'{address}/{32 if address.version == 4 else 128}')
        except OSError:
            warnings.append(f'未能预解析 VPN 服务器“{server}”，VPN 重连时可能需要暂停统一分流')
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
        'dns': {
            'enable': True,
            'ipv6': True,
            'enhanced-mode': 'fake-ip',
            'fake-ip-range': '198.18.0.1/16',
            'fake-ip-range6': 'fdfe:dcba:9876::1/64',
            'use-hosts': True,
            'nameserver': config['dns_servers'],
            'default-nameserver': config['default_nameserver'],
            'proxy-server-nameserver': config['proxy_server_nameserver'],
            'direct-nameserver': config['direct_nameserver'],
            'fake-ip-filter': ['+.lan', '+.local', 'localhost.ptlogin2.qq.com'],
        },
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
            generated['proxy-groups'].append(_proxy_group_config(
                provider_groups[provider['id']],
                _selection.provider_group_strategy(provider),
                [provider_keys[provider['id']]]))
        generated['proxy-groups'].append(_proxy_group_config(
            'PROXY', config['proxy_strategy'],
            [provider_keys[item['id']] for item in enabled_providers]))
    rules = []
    if standby:
        rules.append('MATCH,PROXY' if enabled_providers else 'MATCH,PHYSICAL')
    else:
        rule_kind = {'exact': 'DOMAIN', 'suffix': 'DOMAIN-SUFFIX',
                     'wildcard': 'DOMAIN-WILDCARD'}
        for rule in _active_rules(config):
            rules.append(
                f'{rule_kind[rule["match_type"]]},{rule["domain"]},'
                f'{target_name(rule["outbound"])}')
        if config['traffic_mode'] != 'global':
            rules.extend(_routing_rules.rules_for(config['builtin_rule_pack']))
        rules.append(f'MATCH,{target_name(config["default_outbound"])}')
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


def _proxy_group_config(name, strategy, provider_keys):
    group = {
        'name': name,
        'type': strategy,
        'use': provider_keys,
        'empty-fallback': 'REJECT',
    }
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
            if not node_name:
                continue
            catalog[node_name] = {
                **node, 'provider_name': str(provider_name or '')}
    return catalog


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
  <description>由 CXVPN 管理器维护的 Mihomo 域名分流服务</description>
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
        self._service_dir = os.path.join(program_data, SERVICE_DIR_NAME)
        self._native_service = _routing_service.RoutingServiceClient()

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
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
        return json.loads(raw.decode('utf-8')) if raw else {}

    def _controller_version(self, config):
        try:
            data = self._controller_request(config, '/version', timeout=1.5)
            return data.get('version', '')
        except (OSError, ValueError, urllib.error.URLError):
            return ''

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
            for node_name in group.get('all') or []:
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
                    'type': str(node.get('type') or ''),
                    'provider_name': str(
                        node.get('provider-name') or
                        node.get('provider_name') or ''),
                })
            result.append({
                'id': group_id,
                'name': display_name,
                'strategy': strategy,
                'selected': group.get('now') or '',
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
        builtin_payloads = {
            item.split(',')[1] for item in
            _routing_rules.rules_for(normalized['builtin_rule_pack'])
            if ',' in item
        }
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
        try:
            _selection.persist_provider_nodes(
                normalized, provider_id, group.get('nodes') or [])
        except (OSError, ValueError):
            self.log('[routing] 订阅已更新，但节点快照保存失败')
        selection_invalid = _selection.selection_missing(
            provider, group.get('nodes') or [])
        cache = None
        cache_warning = ''
        try:
            service_cache = self._native_service.read_provider(
                subscription_store.provider_filename(provider))
            cache = subscription_store.persist_bytes(
                provider, service_cache['content'],
                len(group.get('nodes') or []), '常驻核心远端更新',
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
        """获取订阅；自动模式失败时复用可用的 Windows 手动代理。"""
        source = dict(value) if isinstance(value, dict) else {}
        route = str(source.get('download_route') or 'auto').strip().lower()
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
        try:
            return self._preview_proxy_provider_once(source, network, **kwargs)
        except ProviderFetchError:
            can_fallback = (
                route == 'auto' and not _cache_only and _seed_payload is None)
            system_proxy = windows_system_proxy() if can_fallback else ''
            if not system_proxy:
                raise
            provider_name = str(source.get('name') or '未命名订阅')
            started_at = time.monotonic()
            self.log(
                f'[routing] 订阅“{provider_name}”内置更新未产出节点，'
                f'开始通过 Windows 系统代理自动回退（超时 25 秒）')
            retry_source = dict(source)
            retry_source['download_route'] = 'system-proxy'
            try:
                result = self._preview_proxy_provider_once(
                    retry_source, network, **kwargs)
            except RoutingError as fallback_error:
                elapsed = time.monotonic() - started_at
                self.log(
                    f'[routing] 订阅“{provider_name}”Windows 系统代理回退失败，'
                    f'耗时 {elapsed:.1f} 秒，错误类型 '
                    f'{type(fallback_error).__name__}')
                raise RoutingError(
                    '无法获取订阅：内置代理、物理网络和 Windows 系统代理均失败，'
                    '请检查订阅地址、代理状态或导入现有 YAML') from fallback_error
            elapsed = time.monotonic() - started_at
            self.log(
                f'[routing] 订阅“{provider_name}”Windows 系统代理回退成功，'
                f'耗时 {elapsed:.1f} 秒')
            result['download_route'] = (
                f'Windows 系统代理自动回退 {_proxy_display(system_proxy)}')
            result['auto_fallback'] = 'system-proxy'
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
        download = ({'target': 'PHYSICAL', 'proxy_url': '',
                     'upstream': '', 'self_bootstrap': False}
                    if _cache_only else _provider_download_route(
                        provider, windows_system_proxy()))

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
                    deadline = time.monotonic() + 25
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
                                            refreshed = True
                                            latest = self._controller_request(
                                                controller_config,
                                                '/providers/proxies/preview',
                                                timeout=2)
                                            if isinstance(latest, dict) and isinstance(
                                                    latest.get('proxies'), list) and latest['proxies']:
                                                provider_payload = latest
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
            self._controller_request(
                normalized, path, method='PUT', payload={'name': node_name})
        except RoutingError:
            raise
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise RoutingError('无法连接正在运行的 Mihomo 控制端') from exc
        return {'ok': True, 'msg': f'“{display_name}”已切换节点',
                'groups': self.proxy_overview(normalized)}

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
            'service_state': service.get('state', 'Unknown'),
            'service_backend': service.get('backend', 'unknown'),
            'service_version': service.get('service_version', ''),
            'mihomo_version': version or MIHOMO_VERSION,
            'winsw_version': WINSW_VERSION,
            'rule_count': len(normalized['rules']),
            'builtin_rule_pack': _routing_rules.summary(
                normalized['builtin_rule_pack']),
            'actual_rule_count': (1 if normalized['traffic_mode'] == 'global' else
                                  len(_active_rules(normalized)) +
                                  len(_routing_rules.rules_for(
                                      normalized['builtin_rule_pack'])) + 1),
            'provider_count': len([
                item for item in normalized['proxy_providers'] if item['enabled']]),
            'physical_interface': normalized['physical_interface'],
            'mixed_port': normalized['mixed_port'],
            'system_proxy_active': bool(service.get('system_proxy_active')),
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
        vpns = vpn_os.list_vpns()
        interfaces = list_physical_interfaces()
        conflicts = list_tun_conflicts()
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
  Copy-Item -LiteralPath {_ps_literal(source_winsw)} -Destination $wrapper -Force
  Copy-Item -LiteralPath {_ps_literal(config_path)} -Destination (Join-Path $target 'config.json') -Force
  if ((Get-FileHash -LiteralPath (Join-Path $target 'mihomo.exe') -Algorithm SHA256).Hash -ne $expectedMihomoHash) {{
    throw 'Copied Mihomo checksum validation failed'
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
        """提交前验证真实节点及显式本地链路/TUN 系统链路。"""
        groups = self._traffic_proxy_groups(config)
        if groups:
            self.log(
                f'[routing] 开始验证实际代理出口，共 {len(groups)} 个流量组，单次超时 8 秒')
            try:
                provider_payload = self._controller_request(
                    config, '/providers/proxies', timeout=5)
                provider_catalog = _provider_proxy_catalog(provider_payload)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                raise RoutingError(
                    '无法读取代理节点健康状态，已恢复 Windows 原始路由') from exc
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
                node = dict(provider_catalog.get(selected) or {})
                node['name'] = selected
                path = _node_healthcheck_path(
                    node, HEALTH_CHECK_URL, timeout=5000)
                delay = 0
                last_error = None
                for attempt in range(2):
                    try:
                        payload = self._controller_request(config, path, timeout=8)
                        value = payload.get('delay') if isinstance(payload, dict) else 0
                        delay = value if isinstance(value, int) and value > 0 else 0
                    except (OSError, ValueError, urllib.error.URLError) as exc:
                        last_error = exc
                    if delay:
                        break
                    if attempt == 0:
                        time.sleep(0.5)
                if not delay:
                    raise RoutingError(
                        f'当前代理节点的启动实时检测未通过（已检测 2 次，'
                        f'并非“未测速”状态）。请在节点列表重新测速并选择'
                        f'当前可用节点；已自动恢复 Windows 原始路由') from last_error
                self.log(
                    f'[routing] 代理组 {group_name} 当前节点健康检查通过，延迟 {delay} ms')

        if config['capture_mode'] == 'system-proxy':
            address = f'http://127.0.0.1:{config["mixed_port"]}'
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({
                'http': address, 'https': address}))
            self._probe_connectivity(opener, '显式本地 mixed-port 链路')
        else:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            self._probe_connectivity(opener, 'TUN 系统链路')

    def _probe_connectivity(self, opener, source):
        errors = []
        for endpoint in CONNECTIVITY_CHECK_URLS:
            host = urllib.parse.urlparse(endpoint).hostname or 'endpoint'
            self._log_best_effort(
                f'[routing] 开始验证{source}：目标={host}，超时=8 秒')
            request = urllib.request.Request(
                endpoint,
                headers={'User-Agent': 'CXVPN-Connectivity-Check/1.0'})
            started_at = time.monotonic()
            try:
                with opener.open(request, timeout=8) as response:
                    status = int(getattr(response, 'status', 0) or 0)
                    response.read(64)
                if 200 <= status < 400:
                    self._log_best_effort(
                        f'[routing] {source}验证成功：目标={host}，状态={status}，'
                        f'耗时={time.monotonic() - started_at:.1f} 秒')
                    return endpoint
                errors.append(f'{host}:HTTP_{status}')
            except Exception as exc:
                reason = getattr(exc, 'reason', exc)
                category = ('timeout' if isinstance(reason, (TimeoutError, socket.timeout)) else
                            'dns' if isinstance(reason, socket.gaierror) else
                            'proxy' if isinstance(exc, urllib.error.HTTPError) and
                            exc.code in {407, 502, 503, 504} else 'network')
                errors.append(f'{host}:{category}')
                self._log_best_effort(
                    f'[routing] {source}验证失败：目标={host}，分类={category}，'
                    f'异常={type(exc).__name__}，耗时={time.monotonic() - started_at:.1f} 秒')
        raise RoutingError(
            f'{source}多端点自检失败（{", ".join(errors)}），已自动回滚；'
            '请确认节点、系统 DNS 与本机代理端口可用')

    def _install(self, config_path, config, runtime_mode='active'):
        started_at = time.monotonic()
        mode_label = '待机核心' if runtime_mode == 'standby' else '系统接管'
        self.log(
            f'[routing] {mode_label}配置事务已提交到原生服务，'
            '等待后台领取（安装/IPC 超时 120 秒）')
        try:
            self._native_service.ensure_installed()
            transaction = self._native_service.apply(
                config_path, self._native_provider_files(config), runtime_mode)
        except _routing_service.ServiceError as exc:
            raise RoutingError(
                f'统一分流服务操作失败：{_sanitize_mihomo_error(exc)}') from exc
        transaction_id = str(transaction.get('transaction_id') or '')
        if not transaction_id:
            raise RoutingError('统一分流服务未返回配置事务标识')
        self.log('[routing] 原生服务已领取配置事务并启动候选运行时')
        try:
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
                self._probe_connectivity(
                    urllib.request.build_opener(urllib.request.ProxyHandler({
                        'http': active_proxy, 'https': active_proxy})),
                    'Windows 系统代理链路')
            self._native_service.commit(transaction_id)
            self.log(
                f'[routing] {mode_label}配置事务提交成功，总耗时 '
                f'{time.monotonic() - started_at:.1f} 秒')
        except Exception as original:
            self._log_best_effort(
                f'[routing] 配置事务执行失败，开始回滚：{type(original).__name__}: '
                f'{_sanitize_mihomo_error(original)}')
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
            try:
                self._native_service.rollback(transaction_id)
                self._log_best_effort(
                    '[routing] 配置事务回滚成功，Windows 原始路由已恢复')
            except _routing_service.ServiceError as rollback_error:
                raise RoutingError(
                    f'{_sanitize_mihomo_error(original)}；且服务回滚失败：'
                    f'{_sanitize_mihomo_error(rollback_error)}') from original
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
        self._log_best_effort(
            '[routing] 待机核心配置开始生成：TUN=off，系统代理=off，监听仅限回环')
        generated = build_mihomo_config(
            config, [], system_proxy=windows_system_proxy(), standby=True)
        _write_json(config_path, generated)
        self._test_config(config_path, test_data)
        self._install(config_path, config, runtime_mode='standby')

    def apply(self, value):
        config = normalize_config(value)
        self.log(
            f'[routing] 收到分流应用请求：enabled={config["enabled"]}，'
            f'mode={config["traffic_mode"]}，规则 {len(config["rules"])} 条')
        if not config['enabled']:
            service_state = self._service_state()
            if service_state.get('state') == 'Unknown':
                raise RoutingError(
                    '无法确认统一分流服务是否仍在运行，未保存关闭状态；请稍后重试')
            installed = bool(service_state.get('installed'))
            warnings = []
            if installed:
                if service_state.get('backend') == 'native':
                    try:
                        self._native_service.stop_runtime()
                    except _routing_service.ServiceError as exc:
                        raise RoutingError(
                            f'关闭统一分流失败：{_sanitize_mihomo_error(exc)}') from exc
                    self.log('[routing] 系统流量接管已停止，Windows 原始路由已恢复')
                    enabled_providers = [
                        item for item in config['proxy_providers']
                        if item.get('enabled')]
                    if enabled_providers:
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
                    'msg': ('代理已关闭，节点核心保持待机'
                            if standby_started else
                            '统一分流已关闭' if installed else
                            '分流配置已保存'),
                    'config': config,
                    'warnings': warnings, 'status': self.status(config)}

        verify_runtime()
        self.log('[routing] 开始执行启用前校验：运行时、节点偏好、接口、VPN 与 TUN 冲突')
        referenced_provider_ids = _referenced_provider_ids(config)
        selection_error = _selection.manual_selection_error(
            config, referenced_provider_ids)
        if selection_error:
            raise RoutingError(selection_error)
        vpns = vpn_os.list_vpns()
        interfaces = list_physical_interfaces()
        conflicts = list_tun_conflicts()
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
        self._install(config_path, config)
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
        vpns = vpn_os.list_vpns()
        warnings = validate_environment(
            config, vpns, list_physical_interfaces(), list_tun_conflicts())
        generated = build_mihomo_config(
            config, vpns, system_proxy=windows_system_proxy())
        verify_runtime()
        with tempfile.TemporaryDirectory(prefix='cxvpn-routing-preview-') as root:
            path = os.path.join(root, 'config.json')
            _write_json(path, generated)
            self._test_config(path, os.path.join(root, 'data'))
        return {'ok': True, 'msg': '配置预检通过', 'warnings': warnings,
                'rule_count': len(config['rules'])}
