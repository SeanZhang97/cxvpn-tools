# -*- coding: utf-8 -*-
"""订阅 last-known-good 缓存。

缓存位于当前用户的 LocalAppData，不跟随便携版重打包目录被覆盖。文件名包含订阅 URL
指纹，避免修改 URL 后误用上一份订阅节点。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time

from core import app_paths


CACHE_VERSION = 1
NODE_SNAPSHOT_VERSION = 1
DEFAULT_SIZE_LIMIT = 10 * 1024 * 1024
CACHE_FILE_RE = re.compile(
    r'^[a-z0-9][a-z0-9_-]{0,39}-[0-9a-f]{16}\.yaml'
    r'(?:\.json|\.nodes\.json)?$', re.I)
MAX_NODE_COUNT = 1_000_000
MAX_TIMESTAMP = 4_102_444_800  # 2100-01-01，拒绝损坏元数据中的异常大整数。
MAX_NODE_SNAPSHOT_SIZE = 2 * 1024 * 1024
MAX_SNAPSHOT_NODE_COUNT = 10_000
MAX_NODE_NAME_LENGTH = 512
MAX_DISPLAY_NAME_LENGTH = 512
MAX_NODE_TYPE_LENGTH = 64
MAX_NODE_DELAY = 600_000
NODE_SNAPSHOT_KEYS = {
    'name', 'display_name', 'type', 'delay', 'alive', 'tested', 'tested_at'}
SNAPSHOT_DOCUMENT_KEYS = {
    'version', 'url_fingerprint', 'updated_at', 'nodes'}
def cache_root():
    return os.path.join(
        app_paths.user_data_root(), 'routing', 'providers')


def url_fingerprint(provider):
    url = str(provider.get('url') or '').strip().encode('utf-8')
    return hashlib.sha256(url).hexdigest()[:16]


def node_snapshot_fingerprint(provider):
    """节点展示快照同时绑定 URL 与筛选条件，防止旧选点越过新筛选。"""
    include_filter = str(provider.get('filter') or '').strip()
    exclude_filter = str(provider.get('exclude_filter') or '').strip()
    if not include_filter and not exclude_filter:
        return url_fingerprint(provider)
    signature = '\x00'.join([
        str(provider.get('url') or '').strip(),
        include_filter,
        exclude_filter,
    ]).encode('utf-8')
    return hashlib.sha256(signature).hexdigest()[:16]


def provider_filename(provider):
    provider_id = str(provider.get('id') or '').strip().lower()
    return f'{provider_id}-{url_fingerprint(provider)}.yaml'


def cache_path(provider, root=None):
    return os.path.join(root or cache_root(), provider_filename(provider))


def metadata_path(provider, root=None):
    return cache_path(provider, root) + '.json'


def node_snapshot_path(provider, root=None):
    """返回当前订阅 URL 指纹作用域内的安全节点快照路径。"""
    return cache_path(provider, root) + '.nodes.json'


def _empty_node_snapshot(provider):
    return {
        'version': NODE_SNAPSHOT_VERSION,
        'url_fingerprint': node_snapshot_fingerprint(provider),
        'updated_at': 0,
        'nodes': [],
    }


def _safe_int(value, default, minimum=0, maximum=None):
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if parsed < minimum or (maximum is not None and parsed > maximum):
        return default
    return parsed


def _strict_int(value, minimum=0, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError('整数类型无效')
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError('整数超出允许范围')
    return value


def _strict_string(value, field, maximum, allow_empty=True):
    if not isinstance(value, str):
        raise ValueError(f'{field} 类型无效')
    if (not allow_empty and not value) or len(value) > maximum:
        raise ValueError(f'{field} 长度无效')
    return value


def _normalize_snapshot_node(node):
    if not isinstance(node, dict):
        raise ValueError('节点快照条目类型无效')
    if set(node) - NODE_SNAPSHOT_KEYS:
        raise ValueError('节点快照包含未允许字段')
    if set(node) != NODE_SNAPSHOT_KEYS:
        raise ValueError('节点快照字段不完整')
    delay = node['delay']
    if delay is not None:
        delay = _strict_int(delay, maximum=MAX_NODE_DELAY)
    if (node['alive'] is not None and not isinstance(node['alive'], bool)) or \
            not isinstance(node['tested'], bool):
        raise ValueError('节点状态类型无效')
    return {
        'name': _strict_string(
            node['name'], 'name', MAX_NODE_NAME_LENGTH, allow_empty=False),
        'display_name': _strict_string(
            node['display_name'], 'display_name', MAX_DISPLAY_NAME_LENGTH),
        'type': _strict_string(
            node['type'], 'type', MAX_NODE_TYPE_LENGTH),
        'delay': delay,
        'alive': node['alive'],
        'tested': node['tested'],
        'tested_at': _strict_int(
            node['tested_at'], maximum=MAX_TIMESTAMP),
    }


def _normalize_node_snapshot(provider, value):
    if not isinstance(value, dict) or set(value) != SNAPSHOT_DOCUMENT_KEYS:
        raise ValueError('节点快照结构无效')
    if value['version'] != NODE_SNAPSHOT_VERSION:
        raise ValueError('节点快照版本无效')
    expected_fingerprint = node_snapshot_fingerprint(provider)
    if (not isinstance(value['url_fingerprint'], str) or
            value['url_fingerprint'] != expected_fingerprint):
        raise ValueError('节点快照指纹无效')
    updated_at = _strict_int(
        value['updated_at'], minimum=1, maximum=MAX_TIMESTAMP)
    nodes = value['nodes']
    if not isinstance(nodes, list) or len(nodes) > MAX_SNAPSHOT_NODE_COUNT:
        raise ValueError('节点快照数量无效')
    return {
        'version': NODE_SNAPSHOT_VERSION,
        'url_fingerprint': expected_fingerprint,
        'updated_at': updated_at,
        'nodes': [_normalize_snapshot_node(node) for node in nodes],
    }


def load_node_snapshot(provider, root=None,
                       size_limit=MAX_NODE_SNAPSHOT_SIZE):
    """加载安全展示快照；任何损坏或越界内容均返回当前指纹的空快照。"""
    empty = _empty_node_snapshot(provider)
    path = node_snapshot_path(provider, root)
    try:
        if (isinstance(size_limit, bool) or not isinstance(size_limit, int) or
                size_limit <= 0):
            return empty
        size_limit = min(size_limit, MAX_NODE_SNAPSHOT_SIZE)
        size = os.path.getsize(path)
        if size <= 0 or size > size_limit:
            return empty
        with open(path, 'rb') as stream:
            payload = stream.read(size_limit + 1)
        if len(payload) != size or len(payload) > size_limit:
            return empty
        value = json.loads(payload.decode('utf-8'))
        return _normalize_node_snapshot(provider, value)
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError):
        return empty


def cache_status(provider, root=None):
    path = cache_path(provider, root)
    if not os.path.isfile(path):
        return {
            'available': False,
            'node_count': 0,
            'updated_at': 0,
            'source': '',
        }
    size = os.path.getsize(path)
    if size <= 0 or size > DEFAULT_SIZE_LIMIT:
        return {
            'available': False,
            'node_count': 0,
            'updated_at': 0,
            'source': '',
        }
    metadata = {}
    try:
        with open(metadata_path(provider, root), encoding='utf-8') as stream:
            value = json.load(stream)
        if (isinstance(value, dict) and
                value.get('url_fingerprint') == url_fingerprint(provider)):
            metadata = value
    except (OSError, ValueError, TypeError):
        pass
    try:
        modified_at = int(os.path.getmtime(path))
    except (OSError, OverflowError, ValueError):
        modified_at = 0
    return {
        'available': True,
        'node_count': _safe_int(
            metadata.get('node_count'), 0, maximum=MAX_NODE_COUNT),
        'updated_at': _safe_int(
            metadata.get('updated_at'), modified_at, maximum=MAX_TIMESTAMP),
        'source': str(metadata.get('source') or '内置缓存'),
    }


def stage_cache(provider, target_path, root=None):
    source = cache_path(provider, root)
    status = cache_status(provider, root)
    if not status['available']:
        return False
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    shutil.copy2(source, target_path)
    return True


def _prepare_temporary(path, payload, prefix):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=prefix, suffix='.tmp', dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _snapshot(path):
    if not os.path.isfile(path):
        return None
    with open(path, 'rb') as stream:
        payload = stream.read()
    return _prepare_temporary(path, payload, 'subscription.backup.')


def _restore_snapshot(path, snapshot):
    if snapshot:
        os.replace(snapshot, path)
    else:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _atomic_write_pair(first_path, first_payload, second_path, second_payload):
    """提交正文与元数据；任一步失败都尽力恢复提交前的完整版本。"""
    first_temp = _prepare_temporary(
        first_path, first_payload, 'subscription.body.')
    second_temp = _prepare_temporary(
        second_path, second_payload, 'subscription.metadata.')
    first_snapshot = None
    second_snapshot = None
    committed_first = False
    committed_second = False
    try:
        first_snapshot = _snapshot(first_path)
        second_snapshot = _snapshot(second_path)
        os.replace(first_temp, first_path)
        first_temp = None
        committed_first = True
        os.replace(second_temp, second_path)
        second_temp = None
        committed_second = True
    except Exception as exc:
        rollback_errors = []
        if committed_second:
            try:
                _restore_snapshot(second_path, second_snapshot)
                second_snapshot = None
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if committed_first:
            try:
                _restore_snapshot(first_path, first_snapshot)
                first_snapshot = None
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            raise OSError(
                '订阅缓存提交失败，且旧版本恢复不完整：' +
                '；'.join(rollback_errors)) from exc
        raise
    finally:
        for temporary in (
                first_temp, second_temp, first_snapshot, second_snapshot):
            if not temporary:
                continue
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _atomic_write_many(entries):
    """原子提交多个文件；任一步失败时恢复全部提交前版本。"""
    prepared = []
    backups = []
    committed = []
    try:
        for path, payload, prefix in entries:
            prepared.append([path, _prepare_temporary(path, payload, prefix)])
        for path, _temporary in prepared:
            backups.append([path, _snapshot(path)])
        for path, temporary in prepared:
            os.replace(temporary, path)
            committed.append(path)
            for row in prepared:
                if row[0] == path:
                    row[1] = None
                    break
    except Exception as exc:
        rollback_errors = []
        backup_map = {path: backup for path, backup in backups}
        for path in reversed(committed):
            try:
                _restore_snapshot(path, backup_map.get(path))
                backup_map[path] = None
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        if rollback_errors:
            raise OSError(
                '多文件缓存提交失败，且旧版本恢复不完整：' +
                '；'.join(rollback_errors)) from exc
        raise
    finally:
        for _path, temporary in prepared:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        for _path, backup in backups:
            if backup:
                try:
                    os.unlink(backup)
                except OSError:
                    pass


def _atomic_write(path, payload, prefix):
    temporary = _prepare_temporary(path, payload, prefix)
    try:
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def persist_bytes(provider, payload, node_count, source,
                  root=None, size_limit=DEFAULT_SIZE_LIMIT):
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError('订阅缓存内容类型无效')
    if not payload or len(payload) > size_limit:
        raise ValueError('订阅缓存为空或超过大小限制')
    path = cache_path(provider, root)
    metadata = {
        'version': CACHE_VERSION,
        'url_fingerprint': url_fingerprint(provider),
        'node_count': _safe_int(
            node_count, 0, maximum=MAX_NODE_COUNT),
        'updated_at': int(time.time()),
        'source': str(source or '内置引擎'),
    }
    _atomic_write_pair(
        path, bytes(payload), metadata_path(provider, root),
        json.dumps(metadata, ensure_ascii=False, indent=2).encode('utf-8'))
    return cache_status(provider, root)


def persist_bytes_and_nodes(provider, payload, node_count, source, nodes,
                            root=None, size_limit=DEFAULT_SIZE_LIMIT,
                            snapshot_size_limit=MAX_NODE_SNAPSHOT_SIZE):
    """以同一回滚边界提交 provider 正文、元数据和安全节点快照。"""
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError('订阅缓存内容类型无效')
    if not payload or len(payload) > size_limit:
        raise ValueError('订阅缓存为空或超过大小限制')
    timestamp = int(time.time())
    metadata = {
        'version': CACHE_VERSION,
        'url_fingerprint': url_fingerprint(provider),
        'node_count': _safe_int(node_count, 0, maximum=MAX_NODE_COUNT),
        'updated_at': timestamp,
        'source': str(source or '内置引擎'),
    }
    snapshot, snapshot_payload = _serialize_node_snapshot(
        provider, nodes, timestamp, snapshot_size_limit)
    _atomic_write_many([
        (cache_path(provider, root), bytes(payload), 'subscription.body.'),
        (metadata_path(provider, root), json.dumps(
            metadata, ensure_ascii=False, indent=2).encode('utf-8'),
         'subscription.metadata.'),
        (node_snapshot_path(provider, root), snapshot_payload,
         'subscription.nodes.'),
    ])
    return cache_status(provider, root), snapshot


def persist_cache(provider, source_path, node_count, source,
                  root=None, size_limit=DEFAULT_SIZE_LIMIT):
    size = os.path.getsize(source_path)
    if size <= 0 or size > size_limit:
        raise ValueError('订阅缓存为空或超过大小限制')
    with open(source_path, 'rb') as stream:
        payload = stream.read(size_limit + 1)
    return persist_bytes(
        provider, payload, node_count, source, root=root,
        size_limit=size_limit)


def _serialize_node_snapshot(provider, nodes, updated_at=None,
                             size_limit=MAX_NODE_SNAPSHOT_SIZE):
    if not isinstance(nodes, (list, tuple)):
        raise ValueError('节点快照必须是列表')
    if len(nodes) > MAX_SNAPSHOT_NODE_COUNT:
        raise ValueError('节点快照数量超过限制')
    if (isinstance(size_limit, bool) or not isinstance(size_limit, int) or
            size_limit <= 0):
        raise ValueError('节点快照大小限制无效')
    size_limit = min(size_limit, MAX_NODE_SNAPSHOT_SIZE)
    safe_nodes = []
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError('节点快照条目类型无效')
        safe_nodes.append(_normalize_snapshot_node({
            'name': node.get('name'),
            'display_name': node.get('display_name', node.get('name', '')),
            'type': node.get('type', ''),
            'delay': node.get('delay'),
            'alive': node.get('alive'),
            'tested': node.get('tested', False),
            'tested_at': node.get('tested_at', 0),
        }))
    timestamp = int(time.time()) if updated_at is None else _strict_int(
        updated_at, minimum=1, maximum=MAX_TIMESTAMP)
    document = {
        'version': NODE_SNAPSHOT_VERSION,
        'url_fingerprint': node_snapshot_fingerprint(provider),
        'updated_at': timestamp,
        'nodes': safe_nodes,
    }
    payload = json.dumps(
        document, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if not payload or len(payload) > size_limit:
        raise ValueError('节点快照为空或超过大小限制')
    return document, payload


def persist_node_snapshot(provider, nodes, root=None, updated_at=None,
                          size_limit=MAX_NODE_SNAPSHOT_SIZE):
    """原子保存节点展示与测速状态，不持久化节点连接参数或订阅 URL。"""
    document, payload = _serialize_node_snapshot(
        provider, nodes, updated_at, size_limit)
    _atomic_write(
        node_snapshot_path(provider, root), payload, 'subscription.nodes.')
    return document


def prune_cache(providers, root=None):
    """删除已从配置移除或 URL 已变更的本应用订阅缓存。"""
    directory = root or cache_root()
    if not os.path.isdir(directory):
        return 0
    allowed = set()
    for provider in providers or []:
        name = provider_filename(provider)
        allowed.update({name, name + '.json', name + '.nodes.json'})
    removed = 0
    for name in os.listdir(directory):
        if name in allowed or not CACHE_FILE_RE.fullmatch(name):
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        try:
            os.unlink(path)
            removed += 1
        except OSError:
            pass
    return removed
