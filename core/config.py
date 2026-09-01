# -*- coding: utf-8 -*-
"""core/config.py - 配置读写 (config.json 位于 app 根目录, 随程序便携)"""
import json
import os
import sys
import tempfile

if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)  # 打包后: exe 所在目录
else:
    BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(BASE, 'config.json')

DEFAULT = {
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
    'auto_renew': True,
    'auto_connect': False,
    'close_to_tray': True,
    'global_hotkeys_enabled': False,
    'lightweight_mode': False,
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
    'routing': {
        'schema_version': 6,
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
            'domains': [],
            'processes': [],
        },
        'mixed_port': 17890,
        'dns_mode': 'simple',
        'dns_enhanced_mode': 'fake-ip',
        'dns_respect_rules': False,
        'dns_servers': ['223.5.5.5', '1.1.1.1'],
        'default_nameserver': ['223.5.5.5', '1.1.1.1'],
        'proxy_server_nameserver': ['223.5.5.5', '1.1.1.1'],
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
    cfg = json.loads(json.dumps(DEFAULT))
    if os.path.exists(CFG_PATH):
        try:
            with open(CFG_PATH, encoding='utf-8') as f:
                user = json.load(f)
            # 旧版“enabled”唯一对应 TUN。迁移时必须显式保留这一流量路径，
            # 不能因新安装默认改为系统代理而静默改变已有用户的接管方式。
            routing = user.get('routing') if isinstance(user, dict) else None
            if isinstance(routing, dict) and 'schema_version' not in routing:
                routing['schema_version'] = 2
                routing['capture_mode'] = 'tun'
                routing['builtin_rule_pack'] = 'off'
            _merge_dict(cfg, user)
        except Exception:
            pass
    return cfg


def save(cfg):
    directory = os.path.dirname(CFG_PATH)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix='config.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, CFG_PATH)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return True
