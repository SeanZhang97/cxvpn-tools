# -*- coding: utf-8 -*-
"""core/config.py - 配置读写（config.json 位于当前用户 LocalAppData）。"""
import copy
import hashlib
import json
import os
import shutil
import tempfile

from core import app_paths
from core import state_store


BASE = app_paths.user_data_root()
CFG_PATH = os.path.join(BASE, 'config.json')
CFG_BACKUP_PATH = CFG_PATH + '.bak'
CFG_HISTORY_DIR = os.path.join(BASE, 'config-history')
CFG_HISTORY_LIMIT = 20

DEFAULT = {
    'app_update': {
        'auto_check': True,
        'last_check': 0,
    },
    'phone': '',
    'vpn_name': '',
    'creds': {},
    'credential_status': {},
    'renew_hours': 7,
    'authorization': {
        'last_success_at': 0,
        'expires_at': 0,
        'source': '',
        'vpn_expiries': {},
    },
    'auto_renew': False,
    'auto_connect': False,
    'close_to_tray': False,
    'captcha_max_attempts': 10,
    'captcha_gate_max_refresh': 4,
    'sms_timeout': 120,
    'sms': {
        'method': 'phone_link',
        'email': {
            'provider': 'custom',
            'host': '',
            'port': 993,
            'username': '',
            'password': '',
            'mailbox': 'INBOX',
            'subject': '超星验证码',
            'sender': '',
            'poll_interval': 3,
        },
    },
    'browser_data_dir': './browser_data',
    'vlm': {'base': '', 'key': '', 'model': ''},
    'codex_api': {'base_url': '', 'api_key': ''},
    'routing': {
        'schema_version': 8,
        'enabled': False,
        'capture_mode': 'system-proxy',
        'traffic_mode': 'rule',
        'physical_interface': '',
        'proxy_strategy': 'url-test',
        'aggregate_selection': {'mode': 'auto', 'provider_id': '', 'node_name': ''},
        'proxy_providers': [],
        'default_outbound': 'physical',
        'builtin_rule_pack': 'cn-direct-v1',
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
        'fake_ip_filter': ['+.lan', '+.local', 'localhost.ptlogin2.qq.com'],
        'nameserver_policy': [],
        'controller_port': 19090,
        'controller_secret': '',
        'rules': [],
    },
}


def _merge_dict(target, source):
    """递归合并配置，保留新增字段的默认值。"""
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_dict(target[key], value)
        else:
            target[key] = value


def load():
    # 数据库损坏不能静默回退旧 JSON 再覆盖新数据；JSON 只用于首次迁移。
    root = os.path.dirname(CFG_PATH)
    stored = state_store.load_config(root)
    if isinstance(stored, dict):
        cfg = json.loads(json.dumps(DEFAULT))
        _merge_dict(cfg, stored)
        if os.path.normcase(root) == os.path.normcase(app_paths.user_data_root()):
            from core import subscription_store
            with state_store.transaction(root):
                providers = cfg.get('routing', {}).get('proxy_providers', [])
                for provider in providers:
                    subscription_store.migrate_provider(provider)
                state_store.ensure_config_links([
                    (provider['id'], subscription_store.provider_filename(provider),
                     subscription_store.snapshot_key(provider)) for provider in providers], root)
        return cfg
    cfg = json.loads(json.dumps(DEFAULT))
    candidates = [CFG_PATH, CFG_BACKUP_PATH]
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding='utf-8-sig') as f:
                user = json.load(f)
            if not isinstance(user, dict):
                raise ValueError('配置根节点必须是对象')
            # 旧版“enabled”唯一对应 TUN。迁移时必须显式保留这一流量路径，
            # 不能因新安装默认改为系统代理而静默改变已有用户的接管方式。
            routing = user.get('routing') if isinstance(user, dict) else None
            if isinstance(routing, dict) and 'schema_version' not in routing:
                routing['schema_version'] = 2
                routing['capture_mode'] = 'tun'
                routing['builtin_rule_pack'] = 'off'
            _merge_dict(cfg, user)
        except (OSError, ValueError, TypeError):
            continue
        break
    save(cfg)
    return cfg


def save(cfg):
    from core import subscription_store
    value = copy.deepcopy(cfg)
    root = os.path.dirname(CFG_PATH)
    providers = (value.get('routing') or {}).get('proxy_providers') or []
    links = [(provider['id'], subscription_store.provider_filename(provider),
              subscription_store.snapshot_key(provider)) for provider in providers]
    with state_store.transaction(root):
        state_store.save_config(value, root, links=links)
        if os.path.normcase(root) == os.path.normcase(app_paths.user_data_root()):
            for provider in providers:
                subscription_store.migrate_provider(provider)
        state_store.bind_config_data(links, root)
        state_store.after_commit(lambda: _export_json(value))
    return True


def _export_json(cfg):
    """兼容导出不是主提交点；失败不能触发运行时回滚到已过期配置。"""
    # Codex API Key 只保存在 SQLite 主库，不复制到兼容 JSON 及其历史。
    exported = copy.deepcopy(cfg)
    exported.pop('codex_api', None)
    directory = os.path.dirname(CFG_PATH)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix='config.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as f:
            json.dump(exported, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if os.path.isfile(CFG_PATH):
            _preserve_config_history(CFG_PATH)
            backup_path = CFG_PATH + '.bak'
            backup_temp = backup_path + '.tmp'
            with open(CFG_PATH, 'rb') as source, open(backup_temp, 'wb') as target:
                target.write(source.read())
                target.flush()
                os.fsync(target.fileno())
            os.replace(backup_temp, backup_path)
        os.replace(temporary, CFG_PATH)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        try:
            if os.path.exists(CFG_PATH + '.bak.tmp'):
                os.unlink(CFG_PATH + '.bak.tmp')
        except OSError:
            pass
        raise
    return True


def _preserve_config_history(source):
    """在覆盖兼容导出前保存可恢复快照，并限制历史数量。"""
    with open(source, 'rb') as stream:
        body = stream.read()
    # 损坏的兼容文件不进入恢复历史；SQLite 主存储仍不受影响。
    try:
        valid = isinstance(json.loads(body.decode('utf-8-sig')), dict)
    except (UnicodeError, json.JSONDecodeError):
        valid = False
    if not valid:
        return
    digest = hashlib.sha256(body).hexdigest()
    modified_ns = os.stat(source).st_mtime_ns
    os.makedirs(CFG_HISTORY_DIR, exist_ok=True)
    target = os.path.join(
        CFG_HISTORY_DIR, f'config-{modified_ns}-{digest[:12]}.json')
    if not os.path.exists(target):
        shutil.copy2(source, target)
    snapshots = sorted(
        (entry for entry in os.scandir(CFG_HISTORY_DIR)
         if entry.is_file(follow_symlinks=False) and
         entry.name.startswith('config-') and entry.name.endswith('.json')),
        key=lambda entry: (entry.stat().st_mtime_ns, entry.name),
        reverse=True)
    for entry in snapshots[CFG_HISTORY_LIMIT:]:
        os.unlink(entry.path)
