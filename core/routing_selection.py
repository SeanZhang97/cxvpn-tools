# -*- coding: utf-8 -*-
"""代理节点选择与安全展示快照的纯业务辅助函数。"""
from __future__ import annotations

from core import subscription_store


def provider_group_strategy(provider):
    return ('select' if provider.get('selection_mode') == 'manual'
            else provider['strategy'])


def runtime_node_name(provider, selected_node):
    return f'[{provider["name"]}] {selected_node}'


def manual_runtime_targets(config, provider_ids=None):
    targets = set(provider_ids) if provider_ids is not None else None
    return [(provider['id'], runtime_node_name(
        provider, provider.get('selected_node') or ''))
        for provider in config.get('proxy_providers') or []
        if provider.get('enabled') and
        (targets is None or provider.get('id') in targets) and
        provider.get('selection_mode') == 'manual']


def manual_selection_error(config, provider_ids=None):
    targets = set(provider_ids) if provider_ids is not None else None
    for provider in config.get('proxy_providers') or []:
        if (not provider.get('enabled') or
                (targets is not None and provider.get('id') not in targets) or
                provider.get('selection_mode') != 'manual'):
            continue
        selected = str(provider.get('selected_node') or '')
        if not selected:
            return f'代理订阅“{provider["name"]}”尚未选择节点'
        snapshot = subscription_store.load_node_snapshot(provider)
        names = {str(item.get('name') or '')
                 for item in snapshot.get('nodes') or []}
        if not names:
            return f'代理订阅“{provider["name"]}”尚无节点快照，请先获取节点'
        if selected not in names:
            return (f'代理订阅“{provider["name"]}”原选择节点已失效，'
                    '请重新选择后再开启代理')
    return ''


def selection_missing(provider, nodes):
    if provider.get('selection_mode') != 'manual':
        return False
    selected = str(provider.get('selected_node') or '')
    names = {str(item.get('display_name') or item.get('name') or '')
             for item in nodes or []}
    return bool(not selected or selected not in names)


def persist_provider_nodes(config, provider_id, nodes):
    provider = next((item for item in config.get('proxy_providers') or []
                     if item.get('id') == provider_id), None)
    if not provider:
        return None
    safe_nodes = []
    for node in nodes or []:
        display_name = str(
            node.get('display_name') or node.get('name') or '').strip()
        if not display_name:
            continue
        safe_nodes.append({
            **node,
            'name': display_name,
            'display_name': display_name,
        })
    return subscription_store.persist_node_snapshot(provider, safe_nodes)


def load_provider_nodes(config):
    return {
        provider['id']: subscription_store.load_node_snapshot(provider)
        for provider in config.get('proxy_providers') or []
    }


def reconcile_provider_nodes(config, provider_caches, proxy_groups):
    snapshots = load_provider_nodes(config)
    for group in proxy_groups:
        provider_id = str(group.get('id') or '')
        nodes = group.get('nodes') or []
        if provider_id not in provider_caches or not nodes:
            continue
        if not provider_caches[provider_id]['available']:
            provider_caches[provider_id] = {
                'available': True, 'node_count': len(nodes),
                'updated_at': 0, 'source': 'Windows 服务缓存'}
        try:
            snapshots[provider_id] = persist_provider_nodes(
                config, provider_id, nodes)
        except (OSError, ValueError):
            pass
    return snapshots
