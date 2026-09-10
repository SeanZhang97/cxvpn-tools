# -*- coding: utf-8 -*-
"""聚合代理出口的独立选点、失效保护与配置提交。"""
import copy
import re
import time
import traceback

from core import config as cfgmod, subscription_store


def default_selection():
    return {'mode': 'auto', 'provider_id': '', 'node_name': ''}


def is_manual(config):
    return (config.get('aggregate_selection') or {}).get('mode') == 'manual'


def normalize(value, providers, error_type):
    if value is None:
        return default_selection()
    if not isinstance(value, dict):
        raise error_type('全部代理订阅的选点配置无效')
    mode = str(value.get('mode') or 'auto').strip().lower()
    if mode not in {'auto', 'manual'}:
        raise error_type('全部代理订阅的选点模式无效')
    if mode == 'auto':
        return default_selection()
    provider_id = str(value.get('provider_id') or '').strip().lower()
    node_name = str(value.get('node_name') or '').strip()
    if (not node_name or len(node_name) > 512 or
            any(ord(char) < 32 for char in node_name)):
        raise error_type('全部代理订阅尚未选择有效节点')
    if not any(item['id'] == provider_id and item['enabled'] for item in providers):
        raise error_type('全部代理订阅的固定节点所属订阅不存在或未启用，请先重选节点或切回自动')
    return {'mode': mode, 'provider_id': provider_id, 'node_name': node_name}


def selected_provider(config):
    target = config.get('aggregate_selection') or {}
    return next((item for item in config.get('proxy_providers') or []
                 if item['id'] == target.get('provider_id') and item.get('enabled')), None)


def runtime_target(config):
    provider = selected_provider(config)
    target = config.get('aggregate_selection') or {}
    return f'[{provider["name"]}] {target["node_name"]}' if provider else ''


def selection_error(config):
    if not is_manual(config):
        return ''
    provider = selected_provider(config)
    if not provider:
        return '全部代理订阅的固定节点所属订阅不存在或未启用，请重选节点或切回自动'
    nodes = subscription_store.load_node_snapshot(provider).get('nodes') or []
    if not any(item.get('name') == config['aggregate_selection']['node_name'] for item in nodes):
        return '全部代理订阅的固定节点已失效或尚无节点快照，请更新订阅、重选节点或切回自动'
    return ''


def build_manual_group(config, provider_keys):
    target = config['aggregate_selection']
    # 只纳入精确固定的叶子节点；订阅更新删除节点后退化为 REJECT，不能另选首个节点。
    literal = re.sub(r'([\\.^$|?*+()\[\]{}])', r'\\\1', runtime_target(config))
    return {'name': 'PROXY', 'type': 'select',
            'use': [provider_keys[target['provider_id']]],
            'filter': '^' + literal + '$', 'empty-fallback': 'REJECT'}


class AggregateSelectionApi:
    def save_aggregate_proxy_preference(self, mode, provider_id='', node_name=''):
        from core import routing

        started = time.monotonic()
        self.log('[routing] 聚合出口选点请求已提交')
        with self._routing_lock:
            try:
                self.log('[routing] 后台已领取聚合出口选点请求，开始校验')
                current = routing.normalize_config(self._cfg_get().get('routing') or {})
                candidate = copy.deepcopy(current)
                candidate['aggregate_selection'] = normalize(
                    {'mode': mode, 'provider_id': provider_id, 'node_name': node_name},
                    current['proxy_providers'], routing.RoutingError)
                if not is_manual(candidate) and candidate['proxy_strategy'] == 'select':
                    candidate['proxy_strategy'] = 'url-test'
                problem = selection_error(candidate)
                if problem:
                    raise routing.RoutingError(problem)
                candidate = routing.normalize_config(candidate)
                status = self.routing.status(current)
                if status.get('running'):
                    # 精确过滤器随节点变化，复用完整事务处理组重载、回读、失败回滚及写盘。
                    self.log('[routing] 聚合出口选点开始应用，沿用路由事务超时与回滚边界')
                    result = self._apply_routing_locked(candidate, 'aggregate-preference')
                    if not result.get('ok'):
                        self.log(f'[routing] 聚合出口选点应用失败，耗时={time.monotonic()-started:.2f}秒')
                        return result
                    result = {**result, 'groups': self.routing.proxy_overview(candidate),
                              'requires_apply': False, 'msg': '全部代理订阅的选点已保存并应用'}
                else:
                    self._routing_changed()
                    with self._lock:
                        committed = copy.deepcopy(self.cfg)
                        committed['routing'] = candidate
                        cfgmod.save(committed)
                        self.cfg = committed
                    self._routing_changed()
                    result = {'ok': True, 'config': candidate, 'status': status,
                              'requires_apply': True,
                              'msg': '全部代理订阅的选点已保存，开启代理时应用'}
                    self._poke_ui_state()
                self.log(f'[routing] 聚合出口选点成功，耗时={time.monotonic()-started:.2f}秒')
                return self._routing_response(result)
            except routing.RoutingError as exc:
                self.log(f'[routing] 聚合出口选点失败: {exc}，耗时={time.monotonic()-started:.2f}秒')
                return {'ok': False, 'msg': str(exc)}
            except Exception as exc:
                self.log(f'[routing] 聚合出口选点失败: {type(exc).__name__}，'
                         f'耗时={time.monotonic()-started:.2f}秒\n'
                         f'{routing._sanitize_mihomo_error(traceback.format_exc())}')
                return {'ok': False, 'msg': '全部代理订阅的选点保存失败，请重试或查看日志'}
