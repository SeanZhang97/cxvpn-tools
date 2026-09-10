# -*- coding: utf-8 -*-
"""配置备份、应用历史与脱敏诊断导出。"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
import re
import tempfile

from core import config as cfgmod
from core import routing


BACKUP_VERSION = 1
HISTORY_LIMIT = 12
MAX_IMPORT_BYTES = 2 * 1024 * 1024
SAFE_CONFIG_FIELDS = (
    'vpn_name', 'renew_hours', 'auto_renew', 'auto_connect', 'close_to_tray',
    'global_hotkeys_enabled', 'lightweight_mode',
    'captcha_max_attempts', 'captcha_gate_max_refresh', 'sms_timeout',
    'browser_data_dir',
)
SECRET_KEYS = {
    'authorization', 'controller_secret', 'controller_port', 'creds',
    'credential_status', 'download_proxy', 'key', 'pass', 'password',
    'phone', 'secret', 'token', 'url', 'username',
}
URL_RE = re.compile(r'https?://[^\s"\']+', re.I)
BEARER_RE = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+')
QUERY_SECRET_RE = re.compile(
    r'(?i)([?&](?:token|key|secret|auth|password|pwd)=)[^&#\s]+')
INLINE_SECRET_RE = re.compile(
    r'(?i)(\b(?:token|key|secret|auth|password|pwd)\s*[=:]\s*)[^,;\s]+')
IPV4_TARGET_RE = re.compile(
    r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?(?![\w.])')
IPV6_TARGET_RE = re.compile(
    r'(?i)(?<![0-9a-f:])(?:\[[0-9a-f:]+\](?::\d{1,5})?|'
    r'(?:[0-9a-f]{1,4}:){2,}[0-9a-f:]*)(?![0-9a-f:])')
DOMAIN_TARGET_RE = re.compile(
    r'(?i)(?<![\w.-])(?:[a-z0-9-]{1,63}\.)+[a-z]{2,63}(?::\d{1,5})?(?![\w.-])')


def _clone(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _timestamp():
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(
        timespec='seconds')


def _filename_stamp():
    return datetime.datetime.now().strftime('%Y%m%d-%H%M%S')


def _atomic_json(path, value):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix='maintenance.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


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


def redact(value):
    """递归脱敏诊断数据；任何 URL 和常见凭据字段均不导出。"""
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            lowered = str(key).casefold()
            if lowered in SECRET_KEYS or any(
                    marker in lowered for marker in ('password', 'secret', 'token')):
                result[key] = '[REDACTED]'
            else:
                result[key] = redact(child)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        result = URL_RE.sub('[REDACTED_URL]', value)
        result = BEARER_RE.sub('Bearer [REDACTED]', result)
        result = QUERY_SECRET_RE.sub(r'\1[REDACTED]', result)
        result = INLINE_SECRET_RE.sub(r'\1[REDACTED]', result)
        home = os.path.expanduser('~')
        if home:
            result = result.replace(home, '%USERPROFILE%')
            result = result.replace(home.replace('\\', '/'), '%USERPROFILE%')
        return result
    return value


def redact_targets(value):
    """从诊断日志中移除可能代表实际连接目标的主机名与 IPv4。"""
    if isinstance(value, dict):
        return {key: redact_targets(child) for key, child in value.items()}
    if isinstance(value, list):
        return [redact_targets(item) for item in value]
    if isinstance(value, str):
        result = IPV6_TARGET_RE.sub('[REDACTED_TARGET]', value)
        result = IPV4_TARGET_RE.sub('[REDACTED_TARGET]', result)
        return DOMAIN_TARGET_RE.sub('[REDACTED_TARGET]', result)
    return value


def diagnostic_bundle(config, app_logs, core_logs=None, service=None,
                      activity=None, platform=None):
    normalized = routing.normalize_config((config or {}).get('routing') or {})
    rule_types = {}
    outbounds = {}
    for rule in normalized['rules']:
        rule_types[rule['match_type']] = rule_types.get(rule['match_type'], 0) + 1
        outbounds[rule['outbound'].split(':', 1)[0]] = (
            outbounds.get(rule['outbound'].split(':', 1)[0], 0) + 1)
    document = {
        'product': 'CXVPN Manager',
        'diagnostic_version': 1,
        'exported_at': _timestamp(),
        'platform': platform or {},
        'routing': {
            'enabled': normalized['enabled'],
            'capture_mode': normalized['capture_mode'],
            'traffic_mode': normalized['traffic_mode'],
            'dns_mode': normalized['dns_mode'],
            'builtin_rule_pack': normalized['builtin_rule_pack'],
            'provider_count': len(normalized['proxy_providers']),
            'enabled_provider_count': sum(
                1 for item in normalized['proxy_providers'] if item['enabled']),
            'rule_count': len(normalized['rules']),
            'rule_types': rule_types,
            'outbound_types': outbounds,
            'bypass_domain_count': len(
                normalized['system_proxy_bypass']['domains']),
            'include_cn_direct': bool(
                normalized['system_proxy_bypass']['include_cn_direct']),
            'effective_system_proxy_bypass_domain_count': len(
                routing.system_proxy_bypass_domains(normalized)),
            'bypass_process_count': len(
                normalized['system_proxy_bypass']['processes']),
        },
        'service': redact_targets(service or {}),
        'activity': activity or {},
        'recent_app_logs': list(app_logs or [])[-200:],
        'recent_core_logs': redact_targets(list(core_logs or [])[-200:]),
    }
    content = json.dumps(redact(document), ensure_ascii=False, indent=2)
    return {
        'ok': True,
        'filename': f'CXVPN-诊断包-{_filename_stamp()}.json',
        'content': content,
        'summary': '已脱敏；不包含订阅地址、凭据或连接目标',
    }


class ConfigHistory:
    def __init__(self, root=None):
        base = root or os.path.join(cfgmod.BASE, 'routing', 'history')
        self.root = base

    @staticmethod
    def _fingerprint(config):
        raw = json.dumps(config, ensure_ascii=False, sort_keys=True).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()[:12]

    def _paths(self):
        if not os.path.isdir(self.root):
            return []
        return sorted(
            (os.path.join(self.root, name) for name in os.listdir(self.root)
             if re.fullmatch(r'\d{8}T\d{12}-[0-9a-f]{8}\.json', name)),
            reverse=True)

    def ensure_baseline(self, config):
        fingerprint = self._fingerprint(config)
        for path in self._paths()[:1]:
            try:
                with open(path, encoding='utf-8') as stream:
                    if json.load(stream).get('fingerprint') == fingerprint:
                        return
            except (OSError, ValueError):
                pass
        self.record(config, True, 'baseline', '应用前的有效配置')

    def record(self, config, success, source, message):
        normalized = routing.normalize_config(config)
        now = datetime.datetime.now()
        fingerprint = self._fingerprint(normalized)
        record_id = f'{now:%Y%m%dT%H%M%S%f}-{hashlib.sha256((fingerprint + str(now.timestamp())).encode()).hexdigest()[:8]}'
        document = {
            'id': record_id,
            'created_at': now.astimezone().isoformat(timespec='seconds'),
            'success': bool(success),
            'source': str(source or 'apply')[:40],
            'message': redact(str(message or ''))[:300],
            'fingerprint': fingerprint,
            'config': normalized if success else None,
            'summary': {
                'enabled': normalized['enabled'],
                'capture_mode': normalized['capture_mode'],
                'traffic_mode': normalized['traffic_mode'],
                'dns_mode': normalized['dns_mode'],
                'provider_count': len(normalized['proxy_providers']),
                'rule_count': len(normalized['rules']),
                'builtin_rule_pack': normalized['builtin_rule_pack'],
            },
        }
        _atomic_json(os.path.join(self.root, record_id + '.json'), document)
        for path in self._paths()[HISTORY_LIMIT:]:
            try:
                os.unlink(path)
            except OSError:
                pass

    def list(self):
        rows = []
        for path in self._paths():
            try:
                with open(path, encoding='utf-8') as stream:
                    document = json.load(stream)
                rows.append({
                    'id': document.get('id'),
                    'created_at': document.get('created_at'),
                    'success': bool(document.get('success')),
                    'source': document.get('source'),
                    'message': document.get('message'),
                    'fingerprint': document.get('fingerprint'),
                    'restorable': bool(document.get('success') and document.get('config')),
                    **(document.get('summary') or {}),
                })
            except (OSError, ValueError):
                continue
        return rows

    def load(self, record_id):
        if not re.fullmatch(r'\d{8}T\d{12}-[0-9a-f]{8}', str(record_id or '')):
            raise routing.RoutingError('配置历史 ID 无效')
        path = os.path.join(self.root, str(record_id) + '.json')
        try:
            with open(path, encoding='utf-8') as stream:
                document = json.load(stream)
        except (OSError, ValueError) as exc:
            raise routing.RoutingError('配置历史不存在或已损坏') from exc
        if not document.get('success') or not isinstance(document.get('config'), dict):
            raise routing.RoutingError('该记录不是可恢复的有效配置')
        return routing.normalize_config(document['config'])
