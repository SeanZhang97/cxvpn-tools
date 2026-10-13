# -*- coding: utf-8 -*-
"""命名代理出口与业务域名规则；不持有网络或用户数据库连接。"""
import re


ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,39}$')


def _rows(value, label, error):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 32:
        raise error(f'{label}必须为列表且最多 32 项')
    return value


def _identity(row, ids, names, label, error):
    if not isinstance(row, dict):
        raise error(f'{label}格式无效')
    key = str(row.get('id') or '').strip().lower()
    name = str(row.get('name') or '').strip()
    if not ID_RE.fullmatch(key) or key in ids:
        raise error(f'{label} ID 无效或重复')
    if (not name or len(name) > 80 or any(ord(c) < 32 for c in name)
            or name.casefold() in names):
        raise error(f'{label}名称为空、过长或重复')
    ids.add(key)
    names.add(name.casefold())
    return key, name


def normalize_outbounds(value, providers, error):
    result, ids, names = [], set(), set()
    enabled = {row['id'] for row in providers if row['enabled']}
    for row in _rows(value, '代理出口', error):
        key, name = _identity(row, ids, names, '代理出口', error)
        owner = str(row.get('provider_id') or '').strip().lower()
        node = str(row.get('node_name') or '').strip()
        if owner not in enabled:
            raise error(f'代理出口“{name}”引用了不存在或未启用的订阅，请先改绑或删除出口')
        if not node or len(node) > 512 or any(ord(c) < 32 for c in node):
            raise error(f'代理出口“{name}”尚未选择有效节点')
        result.append({'id': key, 'name': name, 'provider_id': owner, 'node_name': node})
    return result


def normalize_sets(value, outbounds, normalize_domain, error):
    result, ids, names = [], set(), set()
    targets = {'outbound:' + row['id'] for row in outbounds}
    for row in _rows(value, '业务规则集', error):
        key, name = _identity(row, ids, names, '业务规则集', error)
        target = str(row.get('outbound') or '').strip().lower()
        if target not in targets:
            raise error(f'业务规则集“{name}”需要选择已存在的代理出口')
        if not isinstance(row.get('enabled', True), bool):
            raise error(f'业务规则集“{name}”启用状态无效')
        item = {'id': key, 'name': name, 'enabled': row.get('enabled', True), 'outbound': target}
        for field, maximum in [('domains', 200), ('exclude_domains', 50)]:
            values = row.get(field, [])
            if not isinstance(values, list) or len(values) > maximum:
                raise error(f'业务规则集“{name}”的 {field} 最多 {maximum} 项')
            item[field] = list(dict.fromkeys(normalize_domain(v, 'suffix') for v in values))
        if not item['domains']:
            raise error(f'业务规则集“{name}”至少需要一个域名')
        result.append(item)
    expanded = expanded_rules({'business_rules': result})
    if len(expanded) > 1000 or sum(len(rule_expression(r).encode('utf-8')) for r in expanded) > 512 * 1024:
        raise error('启用的业务规则展开后超过 1000 条或 512 KB，请减少域名或排除项')
    return result


def expanded_rules(config):
    return [
        {'id': item['id'], 'enabled': True, 'match_type': 'suffix', 'domain': domain,
         'outbound': item['outbound'], 'exclude_domains': item['exclude_domains'],
         'business_name': item['name']}
        for item in config.get('business_rules', []) if item['enabled']
        for domain in item['domains']
    ]


def rule_expression(rule):
    kind = {'exact': 'DOMAIN', 'suffix': 'DOMAIN-SUFFIX', 'wildcard': 'DOMAIN-WILDCARD'}
    expression = f'{kind[rule["match_type"]]},{rule["domain"]}'
    exclusions = rule.get('exclude_domains', [])
    if exclusions:
        terms = [f'({expression})'] + [f'(NOT,((DOMAIN-SUFFIX,{d})))' for d in exclusions]
        expression = 'AND,(' + ','.join(terms) + ')'
    return f'{expression},{rule["outbound"]}'


def build_groups(config, provider_keys):
    providers = {p['id']: p for p in config['proxy_providers']}
    result = []
    for item in config.get('proxy_outbounds', []):
        node = f'[{providers[item["provider_id"]]["name"]}] {item["node_name"]}'
        literal = re.sub(r'([\\.^$|?*+()\[\]{}])', r'\\\1', node)
        result.append({'name': 'OUTBOUND-' + item['id'], 'type': 'select',
                       'use': [provider_keys[item['provider_id']]],
                       'filter': '^' + literal + '$', 'empty-fallback': 'REJECT'})
    return result


def warnings(config):
    result = []
    bypass = config.get('system_proxy_bypass', {})
    for rule in expanded_rules(config):
        domain = rule['domain']
        if any(domain == d or domain.endswith('.' + d) or d.endswith('.' + domain)
               for d in bypass.get('domains', [])):
            result.append(f'业务规则集“{rule["business_name"]}”与手动绕过域名重叠，绕过优先')
    if config.get('business_rules') and config.get('traffic_mode') == 'global':
        result.append('全局模式不执行业务规则集，切回规则模式后生效')
    if config.get('business_rules') and bypass.get('processes'):
        result.append('手动绕过进程优先于业务规则集')
    return list(dict.fromkeys(result))


def referenced_outbounds(config):
    if config.get('traffic_mode') == 'global':
        return []
    referenced = {r['outbound'] for r in config.get('rules', []) if r.get('enabled', True)}
    referenced.update(r['outbound'] for r in config.get('business_rules', []) if r['enabled'])
    return [item for item in config.get('proxy_outbounds', [])
            if 'outbound:' + item['id'] in referenced]


def missing_nodes(config, provider_id, nodes):
    names = {node.get('display_name') or node.get('name') for node in nodes}
    return [item['name'] for item in config.get('proxy_outbounds', [])
            if item['provider_id'] == provider_id and item['node_name'] not in names]


def verify_runtime(config, payload, error):
    providers = {p['id']: p for p in config['proxy_providers']}
    proxies = payload.get('proxies', {}) if isinstance(payload, dict) else {}
    unavailable = []
    for item in referenced_outbounds(config):
        state = proxies.get('OUTBOUND-' + item['id'], {})
        expected = f'[{providers[item["provider_id"]]["name"]}] {item["node_name"]}'
        members, selected = state.get('all'), state.get('now')
        if not ((members == [expected] and selected == expected)
                or (members == ['REJECT'] and selected == 'REJECT')):
            raise error(f'代理出口“{item["name"]}”运行态与固定节点不一致，本次应用失败')
        if selected == 'REJECT':
            unavailable.append(item)
    return unavailable


def templates():
    # 域名均为后缀规则；第三方只列具体主机，不扩大到整个平台。
    # 官方网络清单与用户补充的兼容主机分组维护，不承诺完整覆盖。
    return [
        {'id': 'chatgpt', 'name': 'ChatGPT',
         'domains': [
             'chatgpt.com', 'openai.com', 'oaistatic.com', 'oaiusercontent.com',
             'oaistatsig.com', 'cdn.openaimerge.com',
             # 用户补充的关联产品与入口兼容项。
             'chat.com', 'gpt.com', 'sora.com', 'crixet.com',
             # 官方网络清单中的验证、登录和支付主机。
             'challenges.cloudflare.com', 'cdn.workos.com', 'forwarder.workos.com',
             'frontend-apps-multi-region.workos.com', 'images.workoscdn.com',
             'setup.workos.com', 'workos.imgix.net', 'js.stripe.com',
             # 用户补充的旧登录组件和语音主机；不使用 livekit.cloud 公共后缀。
             'cdn.auth0.com', 'chatgpt.livekit.cloud', 'host.livekit.cloud', 'turn.livekit.cloud',
             # 用户补充的 OpenAI 专用 CDN 主机。
             'openaiapi-site.azureedge.net', 'production-openaicom-storage.azureedge.net',
             'openaiassets.blob.core.windows.net', 'openaicomproductionae4b.blob.core.windows.net',
             'openaicom.imgix.net', 'openaicom-api-bdcpf8c6d2e9atf6.z01.azurefd.net',
             'openai.com.cdn.cloudflare.net',
             # 官方列出的遥测主机，以及用户补充的专用问卷主机。
             'o207216.ingest.sentry.io', 'o33249.ingest.sentry.io',
             'rum.browser-intake-datadoghq.com', 'openai.qualtrics.com',
         ],
         'note': '包含 OpenAI API、关联入口及部分登录、验证、语音、CDN 和支付主机；共享主机也会影响其他网站。'
                 '不保证完整支付或纯 IP/UDP 语音覆盖，不包含 Azure 动态通配域名；可按实际连接补充或排除。',
         'source': 'https://help.openai.com/en/articles/9247338-network-recommendations-for-chatgpt-errors-on-web-and-apps'},
        {'id': 'github', 'name': 'GitHub',
         'domains': ['github.com', 'githubusercontent.com', 'githubassets.com'],
         'note': '覆盖主要网页、API 和下载域名；不保证覆盖 Copilot、第三方站点及全部扩展服务。Git SSH 需进入代理或 TUN。',
         'source': 'https://docs.github.com/en/get-started/using-github/allowing-access-to-githubs-services-from-a-restricted-network'},
    ]
