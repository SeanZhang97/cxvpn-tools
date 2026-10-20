"""核验 Controller 中代理规则的 UDP 拒绝兜底，不代表整机保护。"""
from collections import Counter


def proxy_target(target):
    if target == 'proxy':
        return 'PROXY'
    if target.startswith('proxy:'):
        return 'PROXY-' + target[6:]
    if target.startswith('outbound:'):
        return 'OUTBOUND-' + target[9:]
    return ''


def inspect_rules(payload, expected_targets):
    rules = payload.get('rules') if isinstance(payload, dict) else None
    if not isinstance(rules, list) or not rules or any(
            not isinstance(rule, dict) or not all(key in rule for key in ('type', 'payload', 'proxy'))
            for rule in rules):
        return {'state': 'unknown', 'message': '无法确认 UDP 拒绝规则，请重新检查'}
    targets = []
    for index, rule in enumerate(rules):
        target = str(rule['proxy'])
        if target != 'PROXY' and not target.startswith(('PROXY-', 'OUTBOUND-')):
            continue
        targets.append(target)
        guard = rules[index + 1] if index + 1 < len(rules) else {}
        if ((rule.get('extra') or {}).get('disabled') or (guard.get('extra') or {}).get('disabled') or
                guard.get('proxy') != 'REJECT' or
                (rule['type'], rule['payload']) != (guard.get('type'), guard.get('payload'))):
            return {'state': 'failed', 'message': 'UDP 拒绝规则缺失或已停用，请重新应用配置'}
    if Counter(targets) != Counter(expected_targets):
        return {'state': 'failed', 'message': '运行代理规则与保存配置不一致，请重新应用配置'}
    return {'state': 'verified' if targets else 'not_required', 'guard_count': len(targets),
            'message': 'UDP 拒绝兜底已核验；显式直连例外仍有效' if targets else '当前规则不使用代理出口'}
