# -*- coding: utf-8 -*-
"""节点名称驱动的分层自动优选策略。"""
from __future__ import annotations

import functools
import json
from pathlib import Path
import re
import unicodedata

from core.app_paths import resource_root


MAX_STAGES = 8
MAX_KEYWORDS = 12
MAX_KEYWORD_LENGTH = 40
FALLBACK_ACTIONS = {'reject', 'all'}
SELECTION_MODES = {'latency', 'failure'}
TOLERANCE_UNITS = {'ms', 'percent'}
PERCENT_REFERENCE_MS = 100

_REGION_DATA = json.loads(
    (Path(resource_root()) / 'ui/routing_regions.json').read_text(encoding='utf-8'))
REGION_CATALOG = {
    code: (label, tuple(aliases) + (''.join(chr(0x1F1E6 + ord(char) - 65)
                                        for char in code),) if code != 'OTHER' else ())
    for code, (label, aliases) in _REGION_DATA.items()
}
# 与 ui/routing_nodes.js 的 REGION_PRIORITY 保持一致。
REGION_PRIORITY = (
    'HK', 'TW', 'JP', 'SG', 'US', 'KR', 'GB', 'DE', 'FR', 'CA', 'AU',
    'RU', 'IN', 'BR', 'CN', 'OTHER')

def default_policy():
    return {
        'enabled': False,
        'stages': [],
        'fallback': 'reject',
        'latency_tolerance': 20,
        'latency_tolerance_unit': 'ms',
    }


def _normalize_keywords(values, label, error_type):
    if isinstance(values, str):
        values = re.split(r'[,，|\r\n]+', values)
    if values is None:
        values = []
    if not isinstance(values, list):
        raise error_type(f'{label}必须是关键词列表')
    result = []
    seen = set()
    for raw in values:
        value = str(raw or '').strip()
        if not value:
            continue
        if (len(value) > MAX_KEYWORD_LENGTH or
                any(ord(char) < 32 for char in value)):
            raise error_type(f'{label}包含无效关键词')
        folded = value.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        result.append(value)
        if len(result) > MAX_KEYWORDS:
            raise error_type(f'{label}最多配置 {MAX_KEYWORDS} 个关键词')
    return result


def normalize_policy(value, provider_name, error_type):
    source = value if isinstance(value, dict) else {}
    enabled = bool(source.get('enabled', False))
    stages = source.get('stages')
    if stages is None:
        stages = []
    if not isinstance(stages, list) or len(stages) > MAX_STAGES:
        raise error_type(f'代理订阅“{provider_name}”的优选地区最多配置 {MAX_STAGES} 个')
    normalized_stages = []
    seen_regions = set()
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            raise error_type(f'代理订阅“{provider_name}”第 {index + 1} 个优选地区无效')
        region = str(stage.get('region') or '').strip().upper()
        if region not in REGION_CATALOG:
            raise error_type(f'代理订阅“{provider_name}”第 {index + 1} 个优选地区无效')
        if region in seen_regions:
            raise error_type(f'代理订阅“{provider_name}”的优选地区不能重复')
        region_keywords = _normalize_keywords(
            stage.get('region_keywords'),
            f'代理订阅“{provider_name}”的地区补充关键词', error_type)
        preferred_keywords = _normalize_keywords(
            stage.get('preferred_keywords'),
            f'代理订阅“{provider_name}”的线路优先关键词', error_type)
        selection_mode = str(
            stage.get('selection_mode') or 'latency').strip().lower()
        if selection_mode not in SELECTION_MODES:
            raise error_type(
                f'代理订阅“{provider_name}”第 {index + 1} 个地区的组内切换方式无效')
        preferred_node = str(stage.get('preferred_node') or '').strip()
        if (len(preferred_node) > 512 or
                any(ord(char) < 32 for char in preferred_node)):
            raise error_type(
                f'代理订阅“{provider_name}”第 {index + 1} 个地区的首选节点无效')
        if region == 'OTHER' and not region_keywords:
            raise error_type('“其他”地区必须填写至少一个地区匹配关键词')
        if enabled and selection_mode == 'failure' and not preferred_node:
            raise error_type(
                f'代理订阅“{provider_name}”第 {index + 1} 个地区使用仅故障切换时必须选择首选节点')
        normalized_stages.append({
            'region': region,
            'region_keywords': region_keywords,
            'preferred_keywords': preferred_keywords,
            'selection_mode': selection_mode,
            'preferred_node': preferred_node,
        })
        seen_regions.add(region)
    fallback = str(source.get('fallback') or 'reject').strip().lower()
    if fallback not in FALLBACK_ACTIONS:
        raise error_type(f'代理订阅“{provider_name}”的最终回退方式无效')
    try:
        tolerance = int(source.get('latency_tolerance', 20))
    except (TypeError, ValueError) as exc:
        raise error_type(f'代理订阅“{provider_name}”的延迟容差无效') from exc
    tolerance_unit = str(
        source.get('latency_tolerance_unit') or 'ms').strip().lower()
    if tolerance_unit not in TOLERANCE_UNITS:
        raise error_type(f'代理订阅“{provider_name}”的延迟容差单位无效')
    tolerance_max = 100 if tolerance_unit == 'percent' else 500
    if tolerance < 0 or tolerance > tolerance_max:
        suffix = '%' if tolerance_unit == 'percent' else 'ms'
        raise error_type(
            f'代理订阅“{provider_name}”的延迟容差必须为 0～{tolerance_max} {suffix}')
    if enabled and not normalized_stages:
        raise error_type(f'代理订阅“{provider_name}”启用智能优选前至少添加一个地区')
    return {
        'enabled': enabled,
        'stages': normalized_stages,
        'fallback': fallback,
        'latency_tolerance': tolerance,
        'latency_tolerance_unit': tolerance_unit,
    }


def is_enabled(provider):
    policy = provider.get('auto_policy') or {}
    return (provider.get('selection_mode') == 'auto' and
            bool(policy.get('enabled')) and bool(policy.get('stages')))


def region_label(code):
    return REGION_CATALOG.get(str(code or '').upper(), (str(code or ''), ()))[0]


def _keyword_pattern(values):
    parts = []
    for value in values:
        # Mihomo 使用 Go RE2；只转义表达式元字符，避免 Python re.escape
        # 生成 RE2 不接受的空格、连字符转义。
        escaped = re.sub(r'([\\.^$|?*+(){}\[\]])', r'\\\1', value)
        if value.isascii() and value.replace(' ', '').isalnum():
            escaped = rf'(?:^|[^A-Za-z0-9]){escaped}(?:[^A-Za-z0-9]|$)'
        parts.append(escaped)
    return '(?:' + '|'.join(parts) + ')' if parts else ''


def stage_region_pattern(stage):
    region = stage['region']
    builtins = REGION_CATALOG[region][1]
    return '(?i)' + _keyword_pattern((*builtins, *stage.get('region_keywords', [])))


def stage_preferred_pattern(stage):
    region = stage_region_pattern(stage)
    preferred = _keyword_pattern(stage.get('preferred_keywords') or [])
    if not preferred:
        return ''
    return f'(?i)(?:{region[4:]}[\\s\\S]*{preferred}|{preferred}[\\s\\S]*{region[4:]})'


def exact_node_pattern(node_name):
    escaped = re.sub(r'([\\.^$|?*+(){}\[\]])', r'\\\1', node_name)
    return f'(?:^|\\] ){escaped}$'


def _node_name(node):
    return str(node.get('display_name') or node.get('name') or '').strip()


def _matching_delays(nodes, pattern=''):
    delays = []
    for node in nodes or []:
        name = _node_name(node)
        if pattern and not re.search(pattern, name):
            continue
        delay = node.get('delay')
        if (node.get('alive') is not False and isinstance(delay, int)
                and delay > 0):
            delays.append(delay)
    return delays


def effective_tolerance(policy, nodes=None, pattern=''):
    """将用户百分比容差按最近测速最低值换算为 Mihomo 的毫秒值。"""
    value = policy['latency_tolerance']
    if policy.get('latency_tolerance_unit') != 'percent':
        return value
    delays = _matching_delays(nodes, pattern)
    if not delays and pattern:
        delays = _matching_delays(nodes)
    reference = min(delays) if delays else PERCENT_REFERENCE_MS
    return min(500, round(reference * value / 100))


def validate_preferred_nodes(provider, nodes, error_type):
    """保存时校验仅故障切换的锚定节点，运行时节点失效则自然回退。"""
    policy = provider.get('auto_policy') or {}
    candidates = {}
    for node in nodes or []:
        for name in {_node_name(node), str(node.get('name') or '').strip()}:
            if name:
                candidates[name] = node
    for index, stage in enumerate(policy.get('stages') or [], 1):
        if stage.get('selection_mode') != 'failure':
            continue
        preferred_node = stage.get('preferred_node') or ''
        node = candidates.get(preferred_node)
        if not node:
            raise error_type(
                f'第 {index} 个地区的首选节点已失效，请更新订阅后重新选择')
        if not re.search(stage_region_pattern(stage), _node_name(node)):
            raise error_type(
                f'首选节点“{preferred_node}”不属于{region_label(stage.get("region"))}，请重新选择')


def _health_group(name, group_type, health_url, **fields):
    result = {
        'name': name,
        'type': group_type,
        'url': health_url,
        'interval': 300,
        'timeout': 5000,
        'lazy': True,
        'empty-fallback': 'REJECT',
        'hidden': True,
        **fields,
    }
    return result


def build_groups(provider, provider_key, public_group_name, health_url,
                 nodes=None):
    """把地区顺序编译为隐藏测速/故障组与有序 fallback 组。"""
    policy = provider['auto_policy']
    speedtest_interval = provider.get('speedtest_interval', 300)
    groups = []
    stage_groups = []
    for index, stage in enumerate(policy['stages'], 1):
        base = f'AUTO-{provider["id"]}-{index}'
        region_name = f'{base}-REGION'
        region_pattern = stage_region_pattern(stage)
        group_type = ('fallback' if stage.get('selection_mode') == 'failure'
                      else 'url-test')
        region_fields = {
            'use': [provider_key],
            'filter': region_pattern,
        }
        if group_type == 'url-test':
            region_fields['tolerance'] = effective_tolerance(
                policy, nodes, region_pattern)
        region_group = _health_group(
            region_name, group_type, health_url, interval=speedtest_interval, **region_fields)
        preferred_pattern = stage_preferred_pattern(stage)
        sources = []
        if stage.get('selection_mode') == 'failure':
            pinned_name = f'{base}-PINNED'
            groups.append(_health_group(
                pinned_name, 'fallback', health_url,
                interval=speedtest_interval,
                use=[provider_key],
                filter=exact_node_pattern(stage.get('preferred_node') or '')))
            sources.append(pinned_name)
        if preferred_pattern:
            preferred_name = f'{base}-PREFERRED'
            preferred_fields = {
                'use': [provider_key],
                'filter': preferred_pattern,
            }
            if group_type == 'url-test':
                preferred_fields['tolerance'] = effective_tolerance(
                    policy, nodes, preferred_pattern)
            groups.append(_health_group(
                preferred_name, group_type, health_url,
                interval=speedtest_interval, **preferred_fields))
            sources.append(preferred_name)
        groups.append(region_group)
        sources.append(region_name)
        if len(sources) == 1:
            stage_groups.append(sources[0])
        else:
            stage_name = f'{base}-FALLBACK'
            groups.append(_health_group(
                stage_name, 'fallback', health_url,
                interval=speedtest_interval, proxies=sources))
            stage_groups.append(stage_name)
    if policy['fallback'] == 'all':
        all_name = f'AUTO-{provider["id"]}-ALL'
        all_failure = all(
            stage.get('selection_mode') == 'failure'
            for stage in policy['stages'])
        all_fields = {'use': [provider_key]}
        if not all_failure:
            all_fields['tolerance'] = effective_tolerance(policy, nodes)
        groups.append(_health_group(
            all_name, 'fallback' if all_failure else 'url-test',
            health_url, interval=speedtest_interval, **all_fields))
        stage_groups.append(all_name)
    groups.append(_health_group(
        public_group_name, 'fallback', health_url,
        interval=speedtest_interval, proxies=stage_groups, hidden=False))
    return groups


def flow_labels(policy):
    labels = []
    for stage in policy.get('stages') or []:
        region = region_label(stage.get('region'))
        if (stage.get('selection_mode') == 'failure' and
                stage.get('preferred_node')):
            labels.append(f'{region}首选节点')
        if stage.get('preferred_keywords'):
            labels.append(f'{region}优先线路')
        labels.append(f'{region}其他节点')
    labels.append('全部节点' if policy.get('fallback') == 'all' else '停止并提示')
    return labels


_FLAG_PAIR_RE = re.compile('([%s-%s]{2})' % (chr(0x1F1E6), chr(0x1F1FF)))
_region_matchers = None


def _fold_text(value):
    decomposed = unicodedata.normalize('NFKD', str(value or ''))
    return ''.join(char for char in decomposed
                   if unicodedata.category(char)[0] != 'M')


def _build_region_matchers():
    """预编译地区别名匹配器，规则镜像 ui/routing_nodes.js。"""
    hints, codes = [], []
    for code, (_label, aliases) in REGION_CATALOG.items():
        for alias in aliases:
            text = _fold_text(alias)
            if not text:
                continue
            escaped = re.sub(r'([\\.^$|?*+(){}\[\]])', r'\\\1', text)
            escaped = escaped.replace(' ', r'\s*')
            if all(ord(char) < 128 for char in text):
                pattern = rf'(?:^|[^A-Za-z0-9]){escaped}(?=$|[^A-Za-z0-9])'
            else:
                pattern = escaped
            flags = 0 if re.fullmatch(r'[A-Z]{2}', text) else re.IGNORECASE
            entry = (re.compile(pattern, flags), code, len(text))
            (codes if re.fullmatch(r'[A-Z]{2,3}', text)
             else hints).append(entry)
    # 先匹配完整长名称，避免短别名抢先命中。
    hints.sort(key=lambda entry: -entry[2])
    return hints, codes


@functools.lru_cache(maxsize=4096)
def node_region_code(name):
    """按节点名识别地区码（国旗 emoji 优先，其次别名，兜底 OTHER）。"""
    global _region_matchers
    if _region_matchers is None:
        _region_matchers = _build_region_matchers()
    hints, codes = _region_matchers
    stripped = re.sub(r'^\[[^\]]+\]\s*', '', str(name or '').strip())
    flag = _FLAG_PAIR_RE.search(stripped)
    if flag:
        code = ''.join(chr(ord(char) - 0x1F1E6 + 65)
                       for char in flag.group(1))
        if code in REGION_CATALOG:
            return code
    searchable = _fold_text(stripped)
    for pattern, code, _length in hints:
        if pattern.search(searchable):
            return code
    for pattern, code, _length in codes:
        if pattern.search(searchable):
            return code
    return 'OTHER'
