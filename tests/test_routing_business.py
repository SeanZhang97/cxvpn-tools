# -*- coding: utf-8 -*-
"""独立出口及业务规则的离线契约测试。"""
import copy
import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch

from core import config as cfgmod, routing, routing_business as business, subscription_store
from core import config_maintenance


NODE = '中文 e\u0301 \U0001f1ef\U0001f1f5 [a].+?(x)|$^\\'


def sample():
    return routing.normalize_config({
        **routing.default_config(), 'builtin_rule_pack': 'off',
        'proxy_providers': [{'id': 'alpha', 'name': '订阅甲', 'enabled': True,
                             'url': 'https://example.test/sub', 'selection_mode': 'manual',
                             'selected_node': '无关订阅节点'}],
        'proxy_outbounds': [
            {'id': 'chat', 'name': 'ChatGPT 专用', 'provider_id': 'alpha', 'node_name': NODE},
            {'id': 'git', 'name': 'GitHub 专用', 'provider_id': 'alpha', 'node_name': '节点 B'},
        ],
        'business_rules': [
            {'id': 'chat', 'name': 'ChatGPT', 'outbound': 'outbound:chat',
             'domains': ['chatgpt.com', 'openai.com'], 'exclude_domains': ['api.openai.com']},
            {'id': 'git', 'name': 'GitHub', 'outbound': 'outbound:git', 'domains': ['github.com']},
        ],
    })


class BusinessRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cxvpn-business-')
        self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, {'LOCALAPPDATA': self.temp.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_old_config_migrates_without_new_routes(self):
        old = {**routing.default_config(), 'schema_version': 9}
        old.pop('proxy_outbounds'); old.pop('business_rules')
        value = routing.normalize_config(old)
        self.assertEqual(value['schema_version'], 10)
        self.assertEqual(value['proxy_outbounds'], [])
        self.assertEqual(value['business_rules'], [])
        self.assertEqual(value['default_outbound'], 'physical')

    def test_two_groups_share_provider_but_not_selection(self):
        value = sample()
        generated = routing.build_mihomo_config(value, [])
        groups = {g['name']: g for g in generated['proxy-groups']}
        for name, node in [('OUTBOUND-chat', NODE), ('OUTBOUND-git', '节点 B')]:
            group = groups[name]
            self.assertEqual(group['use'], ['provider-alpha'])
            self.assertEqual(group['empty-fallback'], 'REJECT')
            self.assertTrue(re.fullmatch(group['filter'], '[订阅甲] ' + node))
            self.assertFalse(re.fullmatch(group['filter'], '[订阅甲] 其它节点'))
        self.assertIn('DOMAIN-SUFFIX,github.com,OUTBOUND-git', generated['rules'])
        self.assertIn('AND,((DOMAIN-SUFFIX,openai.com),(NOT,((DOMAIN-SUFFIX,api.openai.com)))),OUTBOUND-chat', generated['rules'])
        original = copy.deepcopy(groups['OUTBOUND-git'])
        value['proxy_outbounds'][0]['node_name'] = '新节点'
        changed = routing.build_mihomo_config(value, [])
        self.assertEqual(next(g for g in changed['proxy-groups'] if g['name'] == 'OUTBOUND-git'), original)
        self.assertEqual(value['proxy_providers'][0]['selected_node'], '无关订阅节点')
        self.assertEqual(routing._referenced_group_ids(value), set())
        self.assertTrue(routing._uses_proxy(value))

    def test_matching_priority_exclusions_and_global(self):
        value = sample()
        def hit(domain):
            return routing.explain_domain(value, domain)['outbound']
        self.assertEqual(hit('ws.chatgpt.com'), 'outbound:chat')
        self.assertEqual(hit('raw.github.com'), 'outbound:git')
        self.assertEqual(hit('evilchatgpt.com'), 'physical')
        self.assertEqual(hit('api.openai.com'), 'physical')
        self.assertEqual(hit('v1.api.openai.com'), 'physical')
        value['business_rules'].append({'id': 'later', 'name': '后续规则集', 'outbound': 'outbound:git',
                                        'domains': ['api.openai.com']})
        self.assertEqual(hit('api.openai.com'), 'outbound:git')
        value['rules'] = [{'domain': 'chatgpt.com', 'outbound': 'block'}]
        self.assertEqual(hit('ws.chatgpt.com'), 'block')
        value['system_proxy_bypass']['domains'] = ['chatgpt.com']
        self.assertEqual(hit('ws.chatgpt.com'), 'physical')
        self.assertTrue(business.warnings(routing.normalize_config(value)))
        value['traffic_mode'] = 'global'
        self.assertEqual(hit('github.com'), 'physical')
        self.assertFalse(any('OUTBOUND-' in rule for rule in routing.build_mihomo_config(value, [])['rules']))

    def test_disabled_set_and_order(self):
        value = sample()
        value['business_rules'][0]['enabled'] = False
        self.assertEqual(routing.explain_domain(value, 'chatgpt.com')['source'], 'default')
        value['business_rules'][0]['enabled'] = True
        value['business_rules'][1]['domains'].append('chatgpt.com')
        self.assertEqual(routing.explain_domain(value, 'chatgpt.com')['outbound'], 'outbound:chat')
        value['business_rules'].reverse()
        self.assertEqual(routing.explain_domain(value, 'chatgpt.com')['outbound'], 'outbound:git')

    def test_invalid_references_and_domain_inputs_rejected(self):
        mutations = [
            lambda c: c['proxy_providers'][0].update(enabled=False),
            lambda c: c.update(proxy_providers=[]),
            lambda c: c.update(proxy_outbounds=[]),
            lambda c: c['proxy_outbounds'][0].update(id='../bad'),
            lambda c: c['proxy_outbounds'][0].update(node_name='bad\nnode'),
            lambda c: c['business_rules'][0].update(domains=['https://chatgpt.com/path']),
            lambda c: c['business_rules'][0].update(exclude_domains=['*,REJECT']),
            lambda c: c['business_rules'][0].update(enabled='false'),
            lambda c: c['business_rules'][0].update(domains=[]),
            lambda c: c['proxy_outbounds'][1].update(name='ChatGPT 专用'),
        ]
        for mutate in mutations:
            value = sample(); mutate(value)
            with self.subTest(mutation=mutate), self.assertRaises(routing.RoutingError):
                routing.normalize_config(value)
        value = sample(); value['business_rules'] = []
        value['rules'] = [{'domain': 'test.example', 'enabled': False, 'outbound': 'outbound:missing'}]
        with self.assertRaises(routing.RoutingError):
            routing.normalize_config(value)

    def test_node_missing_preserves_identity_and_fails_closed(self):
        value = sample()
        snapshot = [{'name': '节点 B'}]
        with patch.object(subscription_store, 'load_node_snapshot', return_value={'nodes': snapshot}):
            normalized = routing.normalize_config(value)
            self.assertEqual(normalized['proxy_outbounds'][0]['node_name'], NODE)
            self.assertFalse(routing.explain_domain(normalized, 'chatgpt.com')['available'])
            self.assertTrue(routing.explain_domain(normalized, 'github.com')['available'])
        payload = {'proxies': {'OUTBOUND-chat': {'all': ['REJECT'], 'now': 'REJECT'},
                               'OUTBOUND-git': {'all': ['[订阅甲] 节点 B'], 'now': '[订阅甲] 节点 B'}}}
        business.verify_runtime(value, payload, routing.RoutingError)
        payload['proxies']['OUTBOUND-chat'] = {'all': ['[订阅甲] 节点 B'], 'now': '[订阅甲] 节点 B'}
        with self.assertRaises(routing.RoutingError):
            business.verify_runtime(value, payload, routing.RoutingError)

    def test_persistence_and_export_restore_unicode(self):
        value = sample()
        path = os.path.join(self.temp.name, 'config.json')
        with patch.object(cfgmod, 'CFG_PATH', path):
            cfgmod.save({'routing': value})
            self.assertEqual(cfgmod.load()['routing']['proxy_outbounds'][0]['node_name'], NODE)
            with open(path, encoding='utf-8') as stream:
                self.assertEqual(json.load(stream)['routing']['proxy_outbounds'][0]['node_name'], NODE)
        current = {'routing': value}
        backup = config_maintenance.create_backup(current)
        restored = config_maintenance.prepare_restore(backup['content'], current)
        self.assertEqual(restored['candidate']['routing']['business_rules'], value['business_rules'])
        self.assertEqual(restored['candidate']['routing']['proxy_outbounds'], value['proxy_outbounds'])

    def test_cn_bypass_does_not_override_business_rules(self):
        value = sample(); value['system_proxy_bypass']['include_cn_direct'] = True
        with patch('core.routing._routing_rules.cn_direct_suffixes', return_value=['chatgpt.com', 'other.test']):
            self.assertEqual(routing.system_proxy_bypass_domains(value), ['other.test'])

    def test_templates_are_optional_copies_and_bounded(self):
        templates = business.templates()
        templates[0]['domains'].clear()
        self.assertTrue(business.templates()[0]['domains'])
        value = sample()
        value['business_rules'][0]['domains'] = ['x.test'] * 201
        with self.assertRaises(routing.RoutingError):
            routing.normalize_config(value)

    def test_chatgpt_template_dependencies_match_without_platform_wide_routes(self):
        template = business.templates()[0]
        domains = template['domains']
        self.assertEqual(len(domains), len(set(domains)))
        required = {'challenges.cloudflare.com', 'cdn.auth0.com', 'cdn.workos.com',
                    'chatgpt.livekit.cloud', 'host.livekit.cloud', 'turn.livekit.cloud',
                    'openaiassets.blob.core.windows.net', 'openai.com.cdn.cloudflare.net',
                    'js.stripe.com', 'o33249.ingest.sentry.io', 'openai.qualtrics.com',
                    'chat.com', 'gpt.com', 'sora.com', 'crixet.com'}
        self.assertTrue(required.issubset(domains))
        value = sample()
        original = copy.deepcopy(value)
        value['business_rules'][0].update(domains=domains, exclude_domains=[])
        normalized = routing.normalize_config(value)
        generated = routing.build_mihomo_config(normalized, [])['rules']
        for domain in domains:
            with self.subTest(domain=domain):
                self.assertIn(f'DOMAIN-SUFFIX,{domain},OUTBOUND-chat', generated)
                self.assertEqual(routing.explain_domain(normalized, domain)['outbound'], 'outbound:chat')
        for domain in ['cloudflare.com', 'other.cloudflare.com', 'other.livekit.cloud',
                       'other.auth0.com', 'other.workos.com', 'other.azureedge.net',
                       'other.blob.core.windows.net', 'other.azurefd.net',
                       'other.ingest.sentry.io', 'other.qualtrics.com', 'stripe.com',
                       'other.stripecdn.com', 'other.stripe.network', 'chatgpt.site',
                       'hcaptcha.com', 'arkoselabs.com', 'evilchatgpt.com',
                       'chatgpt-async-webps-prod-test.webpubsub.azure.com']:
            with self.subTest(unrelated=domain):
                self.assertEqual(routing.explain_domain(normalized, domain)['outbound'], 'physical')
        self.assertEqual(routing.normalize_config(original)['business_rules'], original['business_rules'],
                         '模板升级不能自动合并或重写用户已经保存的规则')

    def test_saved_business_change_uses_existing_rollback_transaction(self):
        from tests.test_routing_commit import RoutingCommitTests
        api = RoutingCommitTests().make_api()
        before = sample()
        api.cfg['routing'] = before
        candidate = copy.deepcopy(before)
        candidate['proxy_outbounds'][0]['node_name'] = '新节点 e\u0301 \U0001f1ef\U0001f1f5'
        api.routing.apply.side_effect = [
            {'ok': True, 'config': candidate, 'status': {}, 'warnings': []},
            {'ok': True, 'config': before, 'status': {}, 'warnings': []},
        ]
        with patch('api.cfgmod.save', side_effect=OSError('disk full')), \
                patch('api.routing.subscription_store.prune_cache') as prune:
            result = api.apply_routing(candidate)
        self.assertFalse(result['ok'])
        self.assertEqual(api.cfg['routing'], before)
        api.routing.apply.assert_called_with(before)
        prune.assert_not_called()


if __name__ == '__main__':
    unittest.main()
