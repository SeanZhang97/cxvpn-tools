# -*- coding: utf-8 -*-
"""在 Codex 完全退出后同步可见历史任务的 Provider 元数据。"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import tomllib
import uuid
from datetime import datetime, timezone

from core import app_paths, codex_proxy


BACKUP_DIR_NAME = 'codex-session-provider-backups'
MAX_BACKUPS = 3
MAX_SESSION_META_BYTES = 2 * 1024 * 1024
SQLITE_BUSY_TIMEOUT_MS = 3000
_STATE_DB_RELATIVE_PATHS = (
    'state_5.sqlite',
    os.path.join('sqlite', 'state_5.sqlite'),
)
_LOCK = threading.RLock()


def _log(logger, message):
    if logger:
        try:
            logger(message)
        except Exception:
            pass


def _atomic_write(path, content):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f'.{os.path.basename(path)}.provider-sync.', dir=parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _codex_root(environ=None):
    return os.path.abspath(os.path.dirname(codex_proxy.config_path(environ)))


def _target_provider(codex_root):
    path = os.path.join(codex_root, 'config.toml')
    if not os.path.isfile(path):
        return 'openai'
    with open(path, 'rb') as stream:
        parsed = tomllib.load(stream)
    if 'profile' in parsed:
        raise ValueError('检测到活动 profile，无法确定历史任务的目标 Provider')
    provider = parsed.get('model_provider', 'openai')
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError('model_provider 类型无效')
    return provider.strip()


def _state_db_paths(codex_root):
    paths = []
    for relative in _STATE_DB_RELATIVE_PATHS:
        path = os.path.abspath(os.path.join(codex_root, relative))
        if os.path.isfile(path) and path not in paths:
            paths.append(path)
    return paths


def _table_columns(connection, table):
    escaped = table.replace('"', '""')
    return {
        row[1] for row in connection.execute(
            f'PRAGMA table_info("{escaped}")').fetchall()
    }


def _visible_thread_rows(db_path, target_provider):
    uri = f'file:{db_path.replace(os.sep, "/")}?mode=ro'
    connection = sqlite3.connect(uri, uri=True, timeout=3)
    try:
        columns = _table_columns(connection, 'threads')
        required = {'id', 'model_provider', 'rollout_path', 'archived'}
        visibility_columns = {'preview', 'first_user_message'} & columns
        if not required.issubset(columns) or not visibility_columns:
            return []
        predicates = [
            "COALESCE(model_provider, '') <> ?",
            "COALESCE(rollout_path, '') <> ''",
        ]
        predicates.append('COALESCE(archived, 0) = 0')
        if 'preview' in columns and 'first_user_message' in columns:
            predicates.append(
                "(COALESCE(preview, '') <> '' OR "
                "COALESCE(first_user_message, '') <> '')")
        elif 'preview' in columns:
            predicates.append("COALESCE(preview, '') <> ''")
        elif 'first_user_message' in columns:
            predicates.append("COALESCE(first_user_message, '') <> ''")
        if 'source' in columns:
            predicates.extend((
                "LOWER(COALESCE(source, '')) NOT LIKE '%subagent%'",
                "LOWER(COALESCE(source, '')) NOT LIKE '%sub_agent%'",
                "LOWER(COALESCE(source, '')) NOT LIKE '%internal%'",
            ))
        if 'thread_source' in columns:
            predicates.extend((
                "LOWER(COALESCE(thread_source, '')) <> 'subagent'",
                "LOWER(COALESCE(thread_source, '')) <> "
                "'memory_consolidation'",
                "LOWER(COALESCE(thread_source, '')) <> "
                "'ambient_suggestions'",
            ))
        sql = (
            'SELECT id, rollout_path, model_provider FROM threads WHERE ' +
            ' AND '.join(predicates))
        return [tuple(row) for row in connection.execute(
            sql, (target_provider,)).fetchall()]
    finally:
        connection.close()


def _is_within(path, root):
    try:
        return os.path.commonpath((
            os.path.normcase(os.path.realpath(path)),
            os.path.normcase(os.path.realpath(root)),
        )) == os.path.normcase(os.path.realpath(root))
    except ValueError:
        return False


def _resolve_rollout_path(codex_root, value):
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace('/', os.sep)
    if os.name == 'nt' and raw.startswith('\\\\?\\UNC\\'):
        raw = '\\\\' + raw[8:]
    elif os.name == 'nt' and raw.startswith('\\\\?\\'):
        raw = raw[4:]
    path = os.path.abspath(raw if os.path.isabs(raw)
                           else os.path.join(codex_root, raw))
    sessions_root = os.path.join(codex_root, 'sessions')
    if not _is_within(path, sessions_root):
        return None
    name = os.path.basename(path).lower()
    if not name.startswith('rollout-') or not name.endswith('.jsonl'):
        return None
    return path


def _source_is_non_root(source):
    if isinstance(source, dict):
        keys = {str(key).strip().lower() for key in source}
        return bool(keys & {'subagent', 'sub_agent', 'internal'})
    if isinstance(source, str):
        value = source.strip().lower()
        return (value in {'subagent', 'internal'} or
                value.startswith('subagent_') or
                value.startswith('internal_'))
    return False


def _read_first_line(path):
    with open(path, 'rb') as stream:
        line = stream.readline(MAX_SESSION_META_BYTES + 1)
    if len(line) > MAX_SESSION_META_BYTES:
        raise ValueError('session_meta 超过安全读取上限')
    if not line:
        raise ValueError('rollout 文件为空')
    ending = b''
    raw = line
    if raw.endswith(b'\r\n'):
        raw, ending = raw[:-2], b'\r\n'
    elif raw.endswith(b'\n'):
        raw, ending = raw[:-1], b'\n'
    record = json.loads(raw.decode('utf-8'))
    return record, ending


def _prepare_rollout(path, thread_id, target_provider):
    if not os.path.isfile(path):
        raise ValueError('rollout 文件不存在')
    record, ending = _read_first_line(path)
    if record.get('type') != 'session_meta':
        raise ValueError('rollout 首行不是 session_meta')
    payload = record.get('payload')
    if not isinstance(payload, dict):
        raise ValueError('session_meta.payload 无效')
    recorded_id = payload.get('id', payload.get('session_id'))
    if not isinstance(recorded_id, str) or recorded_id != thread_id:
        raise ValueError('rollout 与 SQLite 任务 ID 不一致')
    if _source_is_non_root(payload.get('source')):
        raise ValueError('子 Agent 或内部任务不参与 Provider 同步')
    current = payload.get('model_provider')
    if not isinstance(current, str):
        current = ''
    updated_line = None
    if current != target_provider:
        payload['model_provider'] = target_provider
        updated_line = json.dumps(
            record, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        updated_line += ending
    stat = os.stat(path)
    return {
        'path': path,
        'thread_id': thread_id,
        'source_provider': current,
        'updated_line': updated_line,
        'size': stat.st_size,
        'mtime_ns': stat.st_mtime_ns,
    }


def _collect_plan(codex_root, target_provider, logger):
    db_rows = {}
    rollout_changes = {}
    skipped = []
    for db_path in _state_db_paths(codex_root):
        selected = []
        for thread_id, rollout_value, source_provider in _visible_thread_rows(
                db_path, target_provider):
            rollout_path = _resolve_rollout_path(codex_root, rollout_value)
            if not rollout_path:
                skipped.append(thread_id)
                continue
            try:
                change = rollout_changes.get(rollout_path)
                if change is None:
                    change = _prepare_rollout(
                        rollout_path, thread_id, target_provider)
                    rollout_changes[rollout_path] = change
                elif change['thread_id'] != thread_id:
                    raise ValueError('多个任务引用同一个 rollout 文件')
            except (OSError, UnicodeError, ValueError, TypeError,
                    json.JSONDecodeError) as exc:
                skipped.append(thread_id)
                _log(
                    logger,
                    f'[codex-history] 跳过无法安全同步的任务：'
                    f'thread={thread_id}，类型={type(exc).__name__}')
                continue
            selected.append((thread_id, source_provider))
        if selected:
            db_rows[db_path] = selected
    return db_rows, rollout_changes, skipped


def _backup_root(data_root=None):
    root = os.path.abspath(data_root or app_paths.user_data_root())
    return os.path.join(root, BACKUP_DIR_NAME)


def _new_backup_dir(data_root=None):
    root = _backup_root(data_root)
    os.makedirs(root, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    path = os.path.join(root, f'{stamp}-{uuid.uuid4().hex[:8]}')
    os.makedirs(path)
    return path


def _relative_to_root(path, root):
    relative = os.path.relpath(path, root)
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        raise ValueError('目标文件不在 Codex 数据目录中')
    return relative


def _backup_database(source, target):
    os.makedirs(os.path.dirname(target), exist_ok=True)
    connection = sqlite3.connect(source, timeout=3)
    try:
        connection.execute(f'PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}')
        connection.execute('VACUUM main INTO ?', (target,))
    finally:
        connection.close()


def _create_backup(codex_root, db_rows, rollout_changes, target_provider,
                   data_root=None):
    backup_dir = _new_backup_dir(data_root)
    files_root = os.path.join(backup_dir, 'files')
    db_root = os.path.join(backup_dir, 'db')
    try:
        for path in rollout_changes:
            relative = _relative_to_root(path, codex_root)
            target = os.path.join(files_root, relative)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(path, target)
        for path in db_rows:
            relative = _relative_to_root(path, codex_root)
            _backup_database(path, os.path.join(db_root, relative))
        manifest = {
            'version': 1,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'codex_root': codex_root,
            'target_provider': target_provider,
            'thread_count': sum(len(rows) for rows in db_rows.values()),
            'rollout_files': [
                _relative_to_root(path, codex_root)
                for path in rollout_changes
            ],
            'sqlite_files': [
                _relative_to_root(path, codex_root) for path in db_rows
            ],
        }
        _atomic_write(
            os.path.join(backup_dir, 'manifest.json'),
            (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
            .encode('utf-8'))
        return backup_dir
    except Exception:
        shutil.rmtree(backup_dir, ignore_errors=True)
        raise


def _rewrite_rollout(change):
    stat = os.stat(change['path'])
    if stat.st_size != change['size'] or stat.st_mtime_ns != change['mtime_ns']:
        raise RuntimeError('rollout 在扫描后发生变化')
    if change['updated_line'] is None:
        return False
    path = change['path']
    parent = os.path.dirname(path)
    fd, temp_path = tempfile.mkstemp(
        prefix=f'.{os.path.basename(path)}.provider-sync.', dir=parent)
    try:
        with open(path, 'rb') as source, os.fdopen(fd, 'wb') as target:
            source.readline(MAX_SESSION_META_BYTES + 1)
            target.write(change['updated_line'])
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, path)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        return True
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def _update_database(db_path, rows, target_provider):
    connection = sqlite3.connect(db_path, timeout=3)
    try:
        connection.execute(f'PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}')
        connection.execute('BEGIN IMMEDIATE')
        updated = 0
        for thread_id, source_provider in rows:
            cursor = connection.execute(
                "UPDATE threads SET model_provider = ? "
                "WHERE id = ? AND COALESCE(model_provider, '') = ?",
                (target_provider, thread_id, source_provider or ''))
            if cursor.rowcount != 1:
                raise RuntimeError('SQLite 任务在扫描后发生变化')
            updated += cursor.rowcount
        connection.commit()
        integrity = connection.execute('PRAGMA integrity_check').fetchone()
        if not integrity or integrity[0] != 'ok':
            raise RuntimeError('SQLite 完整性检查失败')
        return updated
    except Exception:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def _remove_sqlite_sidecars(path):
    for suffix in ('-wal', '-shm'):
        try:
            os.unlink(path + suffix)
        except FileNotFoundError:
            pass


def _restore_backup(codex_root, backup_dir, db_rows, rollout_changes):
    files_root = os.path.join(backup_dir, 'files')
    db_root = os.path.join(backup_dir, 'db')
    for path in rollout_changes:
        relative = _relative_to_root(path, codex_root)
        source = os.path.join(files_root, relative)
        if os.path.isfile(source):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            shutil.copy2(source, path)
    for path in db_rows:
        relative = _relative_to_root(path, codex_root)
        source = os.path.join(db_root, relative)
        if not os.path.isfile(source):
            continue
        _remove_sqlite_sidecars(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=f'.{os.path.basename(path)}.restore.',
            dir=os.path.dirname(path))
        os.close(fd)
        try:
            shutil.copy2(source, temp_path)
            os.replace(temp_path, path)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
        _remove_sqlite_sidecars(path)


def _prune_backups(data_root=None):
    root = _backup_root(data_root)
    try:
        entries = [
            entry for entry in os.scandir(root)
            if entry.is_dir(follow_symlinks=False)
        ]
    except FileNotFoundError:
        return
    entries.sort(key=lambda item: item.name, reverse=True)
    for entry in entries[MAX_BACKUPS:]:
        shutil.rmtree(entry.path, ignore_errors=True)


def migrate_current_provider(*, environ=None, data_root=None, logger=None):
    """将官方侧边栏可见根任务同步为 config.toml 当前 Provider。"""
    started = time.monotonic()
    _log(logger, '[codex-history] Provider 同步请求已提交')
    with _LOCK:
        _log(logger, '[codex-history] 后台已领取，开始扫描可见历史任务')
        backup_dir = ''
        codex_root = ''
        db_rows = {}
        rollout_changes = {}
        try:
            codex_root = _codex_root(environ)
            target_provider = _target_provider(codex_root)
            db_rows, rollout_changes, skipped = _collect_plan(
                codex_root, target_provider, logger)
            thread_count = sum(len(rows) for rows in db_rows.values())
            if not thread_count:
                _log(
                    logger,
                    f'[codex-history] Provider 同步完成：目标={target_provider}，'
                    f'无需修改，跳过={len(skipped)}，'
                    f'耗时={time.monotonic() - started:.2f}秒')
                return {
                    'ok': True,
                    'changed': False,
                    'target_provider': target_provider,
                    'updated_threads': 0,
                    'updated_rollouts': 0,
                    'skipped_threads': len(skipped),
                    'backup_dir': '',
                }
            backup_dir = _create_backup(
                codex_root, db_rows, rollout_changes, target_provider,
                data_root=data_root)
            updated_rollouts = sum(
                1 for change in rollout_changes.values()
                if _rewrite_rollout(change))
            updated_threads = sum(
                _update_database(path, rows, target_provider)
                for path, rows in db_rows.items())
            _prune_backups(data_root)
            _log(
                logger,
                f'[codex-history] Provider 同步成功：目标={target_provider}，'
                f'任务={updated_threads}，rollout={updated_rollouts}，'
                f'跳过={len(skipped)}，耗时={time.monotonic() - started:.2f}秒')
            return {
                'ok': True,
                'changed': bool(updated_threads or updated_rollouts),
                'target_provider': target_provider,
                'updated_threads': updated_threads,
                'updated_rollouts': updated_rollouts,
                'skipped_threads': len(skipped),
                'backup_dir': backup_dir,
            }
        except (OSError, sqlite3.Error, UnicodeError, ValueError, TypeError,
                RuntimeError, tomllib.TOMLDecodeError) as exc:
            rollback_error = ''
            if backup_dir and codex_root:
                try:
                    _restore_backup(
                        codex_root, backup_dir, db_rows, rollout_changes)
                except (OSError, ValueError) as rollback_exc:
                    rollback_error = type(rollback_exc).__name__
            _log(
                logger,
                f'[codex-history] Provider 同步失败：阶段=写入或校验，'
                f'类型={type(exc).__name__}，回滚=' +
                ('失败' if rollback_error else '完成') +
                f'，耗时={time.monotonic() - started:.2f}秒')
            warning = f'历史任务 Provider 同步失败：{exc}'
            if rollback_error:
                warning += f'；自动回滚失败（{rollback_error}），备份位于 {backup_dir}'
            elif backup_dir:
                warning += '；已自动回滚'
            return {
                'ok': False,
                'changed': False,
                'warning': warning,
                'backup_dir': backup_dir,
            }
