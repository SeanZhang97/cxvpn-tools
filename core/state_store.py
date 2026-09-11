# -*- coding: utf-8 -*-
"""本地状态事务；网络/服务调用必须在事务外，文件导出在提交后进行。"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import json
import logging
import os
import sqlite3
import threading
import time
import traceback

from core import app_paths

DB_FILENAME = 'state.sqlite3'
SCHEMA_VERSION = 3
_local = threading.local()
_log = logging.getLogger(__name__)
_reporter = None


def configure_logger(reporter):
    global _reporter
    _reporter = reporter


def _report(message):
    if _reporter is not None:
        try:
            _reporter(message)
        except Exception:
            pass


class StoreError(OSError):
    """对外只暴露阶段和异常类型，不包含 SQL 绑定参数或敏感配置。"""


class StaleWriteError(StoreError):
    pass


def database_path(root=None):
    return os.path.abspath(os.path.join(root or app_paths.user_data_root(), DB_FILENAME))


def _schema(conn):
    version = conn.execute('PRAGMA user_version').fetchone()[0]
    if version > SCHEMA_VERSION:
        raise StoreError('数据库版本高于当前程序，请使用新版程序')
    if version == SCHEMA_VERSION:
        return
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS kv_config (id INTEGER PRIMARY KEY CHECK(id=1), '
                     'revision INTEGER NOT NULL, payload TEXT NOT NULL, updated_at INTEGER NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS node_snapshots (fingerprint TEXT PRIMARY KEY, '
                     'payload TEXT NOT NULL, revision INTEGER NOT NULL, updated_at INTEGER NOT NULL)')
        for table, column in (('kv_config', 'batch_id'), ('node_snapshots', 'batch_id'),
                              ('node_snapshots', 'config_revision')):
            # 表名/列名来自固定清单，不接收外部输入。
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
            if column not in columns:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0')
        conn.execute('CREATE TABLE IF NOT EXISTS store_clock (id INTEGER PRIMARY KEY CHECK(id=1), '
                     'batch_id INTEGER NOT NULL)')
        conn.execute('INSERT OR IGNORE INTO store_clock VALUES(1,0)')
        conn.execute('CREATE TABLE IF NOT EXISTS provider_cache (cache_key TEXT PRIMARY KEY, '
                     'definition TEXT NOT NULL, body BLOB NOT NULL, metadata TEXT NOT NULL, '
                     'snapshot_key TEXT NOT NULL, batch_id INTEGER NOT NULL, config_revision INTEGER NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS config_providers (provider_id TEXT PRIMARY KEY, '
                     'cache_key TEXT NOT NULL, snapshot_key TEXT NOT NULL, '
                     'config_revision INTEGER NOT NULL, batch_id INTEGER NOT NULL)')
        conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _connect(root=None):
    path = database_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('PRAGMA foreign_keys=ON')
        _schema(conn)
        return conn
    except BaseException:
        conn.close()
        raise


def _revision(conn):
    row = conn.execute('SELECT revision FROM kv_config WHERE id=1').fetchone()
    return int(row[0]) if row else 0


@contextmanager
def transaction(root=None, expected_revision=None):
    """同线程嵌套提交共用连接及批次；内层失败即使被捕获也不允许外层部分提交。"""
    path = database_path(root)
    active = getattr(_local, 'active', None)
    if active is not None:
        if active['path'] != path:
            raise StoreError('禁止跨数据库嵌套事务')
        try:
            if expected_revision is not None and _revision(active['conn']) != expected_revision:
                raise StaleWriteError('配置版本已变化，旧任务结果未写入')
            yield active['conn']
        except BaseException:
            active['failed'] = True
            raise
        return
    conn = None
    callbacks = []
    started = time.monotonic()
    try:
        conn = _connect(root)
        conn.execute('BEGIN IMMEDIATE')
        if expected_revision is not None and _revision(conn) != expected_revision:
            raise StaleWriteError('配置版本已变化，旧任务结果未写入')
        active = {'path': path, 'conn': conn, 'batch': None, 'failed': False,
                  'callbacks': callbacks}
        _local.active = active
        yield conn
        if active['failed']:
            raise StoreError('事务内有失败操作，全部修改已撤销')
        conn.commit()
    except BaseException as exc:
        if conn is not None:
            conn.rollback()
        _log.warning('state transaction failed: type=%s elapsed=%.3fs',
                     type(exc).__name__, time.monotonic() - started)
        frames = ''.join(traceback.format_tb(exc.__traceback__))
        _report(f'[storage] 事务失败，已回滚: type={type(exc).__name__}，耗时={time.monotonic()-started:.3f}秒\n{frames}')
        if isinstance(exc, sqlite3.Error):
            raise StoreError(f'本地数据库提交失败：{type(exc).__name__}') from exc
        raise
    finally:
        _local.active = None
        if conn is not None:
            conn.close()
    for callback in callbacks:
        try:
            callback()
        except Exception as exc:
            _log.warning('state export failed after commit: type=%s', type(exc).__name__)
            _report(f'[storage] 数据库已提交，兼容文件导出失败: type={type(exc).__name__}')


def after_commit(callback):
    active = getattr(_local, 'active', None)
    if active is None:
        raise StoreError('文件导出必须绑定状态事务')
    active['callbacks'].append(callback)


def _batch(conn):
    active = _local.active
    if active['batch'] is None:
        conn.execute('UPDATE store_clock SET batch_id=batch_id+1 WHERE id=1')
        active['batch'] = conn.execute('SELECT batch_id FROM store_clock WHERE id=1').fetchone()[0]
    return active['batch']


@contextmanager
def _reader(root=None):
    active = getattr(_local, 'active', None)
    if active is not None and active['path'] == database_path(root):
        yield active['conn']
        return
    conn = None
    try:
        conn = _connect(root)
        yield conn
    except sqlite3.Error as exc:
        raise StoreError(f'本地数据库读取失败：{type(exc).__name__}') from exc
    finally:
        if conn is not None:
            conn.close()


def _decode(payload):
    try:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError('not an object')
        return value
    except (TypeError, ValueError) as exc:
        raise StoreError('数据库内容损坏，已停止覆盖现有数据，请从备份恢复') from exc


def current_revision(root=None):
    with _reader(root) as conn:
        return _revision(conn)


def load_config(root=None):
    with _reader(root) as conn:
        row = conn.execute('SELECT payload FROM kv_config WHERE id=1').fetchone()
    return _decode(row[0]) if row else None


def save_config(value, root=None, expected_revision=None, links=()):
    payload = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    with transaction(root, expected_revision) as conn:
        revision, batch = _revision(conn) + 1, _batch(conn)
        conn.execute('INSERT INTO kv_config(id,revision,payload,updated_at,batch_id) VALUES(1,?,?,?,?) '
                     'ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload,'
                     'updated_at=excluded.updated_at,batch_id=excluded.batch_id',
                     (revision, payload, int(time.time()), batch))
        conn.execute('DELETE FROM config_providers')
        conn.executemany('INSERT INTO config_providers VALUES(?,?,?,?,?)',
                         [(provider_id, cache_key, snapshot_key, revision, batch)
                          for provider_id, cache_key, snapshot_key in links])
        return revision


def load_snapshot(fingerprint, root=None):
    with _reader(root) as conn:
        row = conn.execute('SELECT payload FROM node_snapshots WHERE fingerprint=?',
                           (str(fingerprint),)).fetchone()
    return _decode(row[0]) if row else None


def bind_config_data(links, root=None):
    """配置引用及其当前缓存归入同一提交批次，不伪造新的测速时间。"""
    with transaction(root) as conn:
        revision, batch = _revision(conn), _batch(conn)
        for _provider_id, cache_key, snapshot_key in links:
            conn.execute('UPDATE provider_cache SET config_revision=?,batch_id=? WHERE cache_key=?',
                         (revision, batch, cache_key))
            conn.execute('UPDATE node_snapshots SET config_revision=?,batch_id=? WHERE fingerprint=?',
                         (revision, batch, snapshot_key))


def ensure_config_links(links, root=None):
    """首次升级补齐既有配置的引用表；不增加配置版本或改变配置内容。"""
    with transaction(root) as conn:
        existing = conn.execute('SELECT provider_id,cache_key,snapshot_key FROM config_providers').fetchall()
        if sorted(existing) == sorted(links):
            return
        revision, batch = _revision(conn), _batch(conn)
        conn.execute('DELETE FROM config_providers')
        conn.executemany('INSERT INTO config_providers VALUES(?,?,?,?,?)',
                         [(provider_id, cache_key, snapshot_key, revision, batch)
                          for provider_id, cache_key, snapshot_key in links])
        conn.execute('UPDATE kv_config SET batch_id=? WHERE id=1', (batch,))
        bind_config_data(links, root)


def save_snapshot(fingerprint, value, root=None, expected_revision=None):
    with transaction(root, expected_revision) as conn:
        # 未测速快照/迟到探测不得清掉仍在列表中的较新延迟；消失的节点不保留。
        value = copy.deepcopy(value)
        old = load_snapshot(fingerprint, root) or {}
        prior = {node['name']: node for node in old.get('nodes', [])}
        for node in value.get('nodes', []):
            previous = prior.get(node['name'], {})
            if previous.get('tested') and (not node.get('tested') or
                    previous.get('tested_at', 0) > node.get('tested_at', 0)):
                for key in ('tested', 'tested_at', 'delay', 'alive'):
                    node[key] = previous.get(key)
        payload = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        batch = _batch(conn)
        row = conn.execute('SELECT revision FROM node_snapshots WHERE fingerprint=?',
                           (str(fingerprint),)).fetchone()
        revision = int(row[0]) + 1 if row else 1
        conn.execute('INSERT INTO node_snapshots '
                     '(fingerprint,payload,revision,updated_at,config_revision,batch_id) VALUES(?,?,?,?,?,?) '
                     'ON CONFLICT(fingerprint) DO UPDATE SET payload=excluded.payload,revision=excluded.revision,'
                     'updated_at=excluded.updated_at,config_revision=excluded.config_revision,batch_id=excluded.batch_id',
                     (str(fingerprint), payload, revision, int(time.time()), _revision(conn), batch))
        return revision


def load_cache(key, root=None):
    with _reader(root) as conn:
        row = conn.execute('SELECT definition,body,metadata,snapshot_key FROM provider_cache WHERE cache_key=?',
                           (str(key),)).fetchone()
    if row is None:
        return None
    return {'definition': _decode(row[0]), 'body': bytes(row[1]),
            'metadata': _decode(row[2]), 'snapshot_key': row[3]}


def save_cache(key, definition, body, metadata, snapshot_key, root=None):
    with transaction(root) as conn:
        conn.execute('INSERT INTO provider_cache VALUES(?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET '
                     'definition=excluded.definition,body=excluded.body,metadata=excluded.metadata,'
                     'snapshot_key=excluded.snapshot_key,batch_id=excluded.batch_id,config_revision=excluded.config_revision',
                     (str(key), json.dumps(definition, ensure_ascii=False), bytes(body),
                      json.dumps(metadata, ensure_ascii=False), str(snapshot_key), _batch(conn), _revision(conn)))
