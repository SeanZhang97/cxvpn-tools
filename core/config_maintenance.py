# -*- coding: utf-8 -*-
"""配置导入、导出与本机凭据保护。"""
from __future__ import annotations

import copy
import datetime
import json
import os
import tempfile

from core import routing


BACKUP_VERSION = 1
MAX_IMPORT_BYTES = 2 * 1024 * 1024
SAFE_CONFIG_FIELDS = (
    'vpn_name', 'renew_hours', 'auto_renew', 'auto_connect', 'close_to_tray',
    'captcha_max_attempts', 'captcha_gate_max_refresh', 'sms_timeout',
    'browser_data_dir',
)


def _clone(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _timestamp():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(
        timespec='seconds')


def _filename_stamp():
    return datetime.datetime.now().strftime('%Y%m%d-%H%M%S')


def _parse_document(content):
    if not isinstance(content, str):
        raise routing.RoutingError('备份文件内容无效')
    if len(content.encode('utf-8')) > MAX_IMPORT_BYTES:
        raise routing.RoutingError('备份文件不能超过 2 MB')
    try:
        document = json.loads(content)
    except json.JSONDecodeError as exc:
        raise routing.RoutingError('备份文件不是有效 JSON') from exc
    if not isinstance(document, dict):
        raise routing.RoutingError('备份文件结构无效')
    if document.get('product') != 'CXVPN Manager':
        raise routing.RoutingError('该文件不是 CXVPN Manager 配置备份')
    if document.get('backup_version') != BACKUP_VERSION:
        raise routing.RoutingError('不支持该备份文件版本')
    if not isinstance(document.get('config'), dict):
        raise routing.RoutingError('备份文件缺少配置数据')
    return document


def create_backup(config, include_subscription_urls=False):
    source = _clone(config or {})
    public = {
        key: source[key] for key in SAFE_CONFIG_FIELDS if key in source}
    routing_config = routing.normalize_config(source.get('routing') or {})
    routing_config.pop('controller_port', None)
    routing_config.pop('controller_secret', None)
    for provider in routing_config['proxy_providers']:
        provider.pop('download_proxy', None)
        if not include_subscription_urls:
            provider.pop('url', None)
    public['routing'] = routing_config
    document = {
        'product': 'CXVPN Manager',
        'backup_version': BACKUP_VERSION,
        'exported_at': _timestamp(),
        'includes_subscription_urls': bool(include_subscription_urls),
        'credentials_included': False,
        'config': public,
    }
    content = json.dumps(document, ensure_ascii=False, indent=2)
    return {
        'ok': True,
        'filename': f'CXVPN-配置备份-{_filename_stamp()}.json',
        'content': content,
        'summary': {
            'provider_count': len(routing_config['proxy_providers']),
            'rule_count': len(routing_config['rules']),
            'includes_subscription_urls': bool(include_subscription_urls),
            'credentials_included': False,
        },
    }


def write_backup_file(path, content):
    """先完整写入同目录临时文件，失败时保留用户原有导出文件。"""
    path = os.path.abspath(os.fspath(path))
    descriptor, temporary = tempfile.mkstemp(
        prefix='.cxvpn-config-', suffix='.tmp', dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def read_backup_file(path):
    if os.path.splitext(path)[1].lower() != '.json':
        raise routing.RoutingError('请选择 JSON 配置文件')
    with open(path, 'rb') as stream:
        content = stream.read(MAX_IMPORT_BYTES + 1)
    if len(content) > MAX_IMPORT_BYTES:
        raise routing.RoutingError('配置文件不能超过 2 MB')
    return content.decode('utf-8-sig')


def prepare_restore(content, current_config):
    document = _parse_document(content)
    backed = document['config']
    candidate = _clone(current_config or {})
    for key in SAFE_CONFIG_FIELDS:
        if key in backed:
            candidate[key] = copy.deepcopy(backed[key])

    incoming_routing = copy.deepcopy(backed.get('routing') or {})
    current_routing = routing.normalize_config(
        candidate.get('routing') or routing.default_config())
    current_providers = {
        item['id']: item for item in current_routing['proxy_providers']}
    missing_urls = []
    for provider in incoming_routing.get('proxy_providers') or []:
        if not isinstance(provider, dict):
            continue
        current = current_providers.get(str(provider.get('id') or '').lower())
        if not provider.get('url'):
            if current and current.get('url'):
                provider['url'] = current['url']
            else:
                missing_urls.append(str(provider.get('name') or provider.get('id') or '未命名订阅'))
        # 自定义订阅上游属于凭据，只保留本机现值。
        provider['download_proxy'] = current.get('download_proxy', '') if current else ''
    incoming_routing['enabled'] = current_routing['enabled']
    incoming_routing['controller_port'] = current_routing['controller_port']
    incoming_routing['controller_secret'] = current_routing['controller_secret']
    normalized = routing.normalize_config(incoming_routing)
    candidate['routing'] = normalized

    old_ids = {item['id'] for item in current_routing['proxy_providers']}
    new_ids = {item['id'] for item in normalized['proxy_providers']}
    changed_fields = [
        key for key in SAFE_CONFIG_FIELDS
        if candidate.get(key) != (current_config or {}).get(key)]
    routing_changed = [
        key for key in (
            'capture_mode', 'traffic_mode', 'physical_interface',
            'proxy_strategy', 'aggregate_selection', 'default_outbound', 'builtin_rule_pack',
            'mixed_port', 'dns_servers', 'default_nameserver',
            'proxy_server_nameserver', 'direct_nameserver',
            'dns_mode', 'dns_enhanced_mode', 'dns_respect_rules',
            'fake_ip_range', 'fake_ip_filter', 'nameserver_policy',
            'system_proxy_bypass', 'rules',
        ) if normalized.get(key) != current_routing.get(key)]
    warnings = ['代理启用状态保持当前值，不随备份切换']
    if missing_urls:
        warnings.append(
            f'{len(missing_urls)} 个新订阅未包含地址，恢复后需手动补充')
    return {
        'ok': True,
        'candidate': candidate,
        'summary': {
            'general_fields': changed_fields,
            'routing_fields': routing_changed,
            'providers_added': sorted(new_ids - old_ids),
            'providers_removed': sorted(old_ids - new_ids),
            'provider_count': len(normalized['proxy_providers']),
            'rule_count': len(normalized['rules']),
            'missing_subscription_urls': missing_urls,
            'warnings': warnings,
            'credentials_preserved': True,
        },
    }
