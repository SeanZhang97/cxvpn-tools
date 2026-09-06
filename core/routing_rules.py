# -*- coding: utf-8 -*-
"""从可编辑 UTF-8 文件加载离线直连规则包。"""
from __future__ import annotations

import ipaddress
import os
import re
import sys
import unicodedata


SCHEMA_VERSION = 7
BUILTIN_PACKS = {'off', 'local-direct-v1', 'cn-direct-v1'}
BUILTIN_PACK_ORDER = ('off', 'local-direct-v1', 'cn-direct-v1')
RULE_PACK_DIR_NAME = 'rule-packs'
PACK_FILES = {
    'local-direct-v1': 'local-direct-v1.txt',
    'cn-direct-v1': 'cn-direct-v1.txt',
}
MAX_FILE_BYTES = 256 * 1024
MAX_LOCAL_RULES = 512
MAX_CN_SUFFIXES = 2000
DOMAIN_KINDS = {'DOMAIN', 'DOMAIN-SUFFIX', 'DOMAIN-WILDCARD'}
IP_KINDS = {'IP-CIDR': 4, 'IP-CIDR6': 6}
DOMAIN_LABEL_RE = re.compile(
    r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$', re.I)


class RulePackError(ValueError):
    """本地规则包缺失、编码错误或内容不合法。"""


def _app_root():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


RULE_PACK_DIR = os.path.join(_app_root(), RULE_PACK_DIR_NAME)


def _source_name(pack_id):
    filename = PACK_FILES.get(pack_id, '')
    return f'{RULE_PACK_DIR_NAME}/{filename}' if filename else ''


def _read_entries(pack_id, maximum):
    filename = PACK_FILES[pack_id]
    path = os.path.join(RULE_PACK_DIR, filename)
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise RulePackError(f'规则包文件缺失：{_source_name(pack_id)}') from exc
    if size > MAX_FILE_BYTES:
        raise RulePackError(
            f'规则包文件过大：{_source_name(pack_id)}，最大 256 KB')
    try:
        with open(path, 'r', encoding='utf-8-sig') as stream:
            lines = stream.readlines()
    except UnicodeError as exc:
        raise RulePackError(
            f'规则包必须使用 UTF-8 编码：{_source_name(pack_id)}') from exc
    except OSError as exc:
        raise RulePackError(
            f'无法读取规则包：{_source_name(pack_id)}') from exc
    entries = [(number, line.strip()) for number, line in enumerate(lines, 1)
               if line.strip() and not line.lstrip().startswith('#')]
    if len(entries) > maximum:
        raise RulePackError(
            f'规则包条目过多：{_source_name(pack_id)}，最多 {maximum} 条')
    return entries


def _normalize_domain(value, pack_id, line_number, allow_wildcard=False):
    raw = unicodedata.normalize(
        'NFC', str(value or '').strip().lower().rstrip('.'))
    if raw.startswith('+.'):
        raw = raw[2:]
    elif raw.startswith('.'):
        raw = raw[1:]
    if (not raw or any(char.isspace() for char in raw) or
            any(char in raw for char in '/\\:@?#') or
            any(ord(char) > 0xFFFF for char in raw)):
        raise RulePackError(
            f'{_source_name(pack_id)} 第 {line_number} 行域名格式无效')
    labels = []
    try:
        for label in raw.split('.'):
            if allow_wildcard and label == '*':
                labels.append(label)
            else:
                labels.append(label.encode('idna').decode('ascii').lower())
    except UnicodeError as exc:
        raise RulePackError(
            f'{_source_name(pack_id)} 第 {line_number} 行域名格式无效') from exc
    normalized = '.'.join(labels)
    if (len(normalized) > 253 or not any(label != '*' for label in labels) or
            any(not label or (label != '*' and not DOMAIN_LABEL_RE.fullmatch(label))
                for label in labels)):
        raise RulePackError(
            f'{_source_name(pack_id)} 第 {line_number} 行域名格式无效')
    return normalized


def _local_rules():
    result = []
    seen = set()
    for line_number, raw in _read_entries(
            'local-direct-v1', MAX_LOCAL_RULES):
        parts = [part.strip() for part in raw.split(',')]
        if len(parts) not in {2, 3}:
            raise RulePackError(
                f'{_source_name("local-direct-v1")} 第 {line_number} 行规则格式无效')
        kind = parts[0].upper()
        value = parts[1]
        option = parts[2].lower() if len(parts) == 3 else ''
        if kind in DOMAIN_KINDS:
            if option:
                raise RulePackError(
                    f'{_source_name("local-direct-v1")} 第 {line_number} 行域名规则不支持选项')
            value = _normalize_domain(
                value, 'local-direct-v1', line_number,
                allow_wildcard=kind == 'DOMAIN-WILDCARD')
        elif kind in IP_KINDS:
            if option not in {'', 'no-resolve'}:
                raise RulePackError(
                    f'{_source_name("local-direct-v1")} 第 {line_number} 行仅支持 no-resolve 选项')
            try:
                network = ipaddress.ip_network(value, strict=False)
            except ValueError as exc:
                raise RulePackError(
                    f'{_source_name("local-direct-v1")} 第 {line_number} 行网段格式无效') from exc
            if network.version != IP_KINDS[kind]:
                raise RulePackError(
                    f'{_source_name("local-direct-v1")} 第 {line_number} 行规则类型与网段版本不一致')
            value = str(network)
        else:
            raise RulePackError(
                f'{_source_name("local-direct-v1")} 第 {line_number} 行规则类型不受支持')
        rule = f'{kind},{value},PHYSICAL{"," + option if option else ""}'
        key = rule.casefold()
        if key not in seen:
            result.append(rule)
            seen.add(key)
    return result


def _cn_rules():
    result = []
    seen = set()
    for line_number, raw in _read_entries('cn-direct-v1', MAX_CN_SUFFIXES):
        domain = _normalize_domain(raw, 'cn-direct-v1', line_number)
        key = domain.casefold()
        if key not in seen:
            result.append(f'DOMAIN-SUFFIX,{domain},PHYSICAL')
            seen.add(key)
    return result


_RULES_BY_PACK = {'off': []}
_ERRORS_BY_PACK = {}
_CN_DIRECT_SUFFIXES = []


def load_rule_packs():
    """软件启动时加载一次规则文件，运行期间使用同一份内存快照。"""
    global _RULES_BY_PACK, _ERRORS_BY_PACK, _CN_DIRECT_SUFFIXES
    loaded = {'off': []}
    errors = {}
    cn_suffixes = []
    try:
        local = _local_rules()
    except RulePackError as exc:
        local = []
        errors['local-direct-v1'] = str(exc)
        errors['cn-direct-v1'] = str(exc)
    else:
        loaded['local-direct-v1'] = local
        try:
            cn_rules = _cn_rules()
            loaded['cn-direct-v1'] = local + cn_rules
            cn_suffixes = [raw.split(',', 2)[1] for raw in cn_rules]
        except RulePackError as exc:
            errors['cn-direct-v1'] = str(exc)
    _RULES_BY_PACK = loaded
    _ERRORS_BY_PACK = errors
    _CN_DIRECT_SUFFIXES = cn_suffixes


def rules_for(pack_id):
    if pack_id not in BUILTIN_PACKS:
        raise RulePackError(f'未知规则包：{pack_id}')
    if pack_id in _ERRORS_BY_PACK:
        raise RulePackError(_ERRORS_BY_PACK[pack_id])
    return list(_RULES_BY_PACK.get(pack_id, []))


def cn_direct_suffixes():
    """返回启动时加载的国内域名后缀，不包含 local-direct-v1 的本地规则。"""
    if 'cn-direct-v1' in _ERRORS_BY_PACK:
        raise RulePackError(_ERRORS_BY_PACK['cn-direct-v1'])
    return list(_CN_DIRECT_SUFFIXES)


def _metadata(pack_id):
    return {
        'id': pack_id,
        'version': 1 if pack_id != 'off' else 0,
        'readonly': True,
        'file_editable': pack_id != 'off',
        'source_file': _source_name(pack_id),
        'description': {
            'off': '不加载本地规则包',
            'local-direct-v1': '局域网、回环和私有地址直连',
            'cn-direct-v1': '本地网络与稳定国内域名直连',
        }[pack_id],
    }


def detail(pack_id):
    result = _metadata(pack_id)
    try:
        rules = rules_for(pack_id)
    except RulePackError as exc:
        rules = []
        result['error'] = str(exc)
    result['rule_count'] = len(rules)
    result['system_proxy_domain_count'] = (
        len(_CN_DIRECT_SUFFIXES) if pack_id == 'cn-direct-v1' else 0)
    result['system_proxy_domains'] = (
        list(_CN_DIRECT_SUFFIXES) if pack_id == 'cn-direct-v1' else [])
    result['rules'] = [{
        'index': index,
        'type': parts[0],
        'value': parts[1],
        'outbound': parts[2],
        'options': parts[3:],
    } for index, raw in enumerate(rules, 1) for parts in [raw.split(',')]]
    return result


def summary(pack_id):
    result = detail(pack_id)
    result.pop('rules', None)
    result.pop('system_proxy_domains', None)
    return result


def catalog():
    return {pack_id: detail(pack_id) for pack_id in BUILTIN_PACK_ORDER}


load_rule_packs()
