"""同步 cxvpn-tools 本机代理到 Codex 用户配置。"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import tomllib

from core import app_paths


SNAPSHOT_FILE_NAME = 'codex_proxy_snapshot.json'
SNAPSHOT_VERSION = 1
_LOCK = threading.RLock()

_NO_PROXY = 'localhost,127.0.0.1,::1,mcp.mh.chaoxing.com,*.local,*.lan,10.*,192.168.*,172.16.*,172.17.*,172.18.*,172.19.*,172.20.*,172.21.*,172.22.*,172.23.*,172.24.*,172.25.*,172.26.*,172.27.*,172.28.*,172.29.*,172.30.*,172.31.*'

_TARGETS = {
    'features': {'respect_system_proxy': True},
    'mcp_servers.node_repl.env': {
        'HTTP_PROXY': None, 'HTTPS_PROXY': None, 'ALL_PROXY': None,
        'http_proxy': None, 'https_proxy': None, 'all_proxy': None,
        'NO_PROXY': _NO_PROXY, 'no_proxy': _NO_PROXY,
    },
    'shell_environment_policy.set': {
        'HTTP_PROXY': None, 'HTTPS_PROXY': None, 'ALL_PROXY': None,
        'http_proxy': None, 'https_proxy': None, 'all_proxy': None,
        'NO_PROXY': _NO_PROXY, 'no_proxy': _NO_PROXY,
    },
}


def codex_home(environ=None):
    env = os.environ if environ is None else environ
    value = str(env.get('CODEX_HOME') or '').strip()
    if value:
        return os.path.abspath(os.path.expanduser(value))
    profile = str(env.get('USERPROFILE') or os.path.expanduser('~')).strip()
    return os.path.abspath(os.path.join(profile, '.codex'))


def config_path(environ=None):
    return os.path.join(codex_home(environ), 'config.toml')


def snapshot_path(data_root=None):
    root = os.path.abspath(data_root or app_paths.user_data_root())
    return os.path.join(root, SNAPSHOT_FILE_NAME)


def _log(logger, message):
    if logger:
        try:
            logger(message)
        except Exception:
            pass


def _read_text(path):
    with open(path, 'r', encoding='utf-8-sig') as stream:
        return stream.read()


def _parse(text):
    return tomllib.loads(text) if text else {}


def _literal(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    escaped = str(value).replace('\\', '\\\\').replace('"', '\\"')
    escaped = escaped.replace('\r', '\\r').replace('\n', '\\n').replace('\t', '\\t')
    return f'"{escaped}"'


def _section_header(line):
    match = re.match(r'^\s*(\[\[?)([^\]]+)(\]\]?)\s*(?:#.*)?(?:\r?\n)?$', line)
    if not match or match.group(1) != '[' or match.group(3) != ']':
        return None
    return match.group(2).strip()


def _comment_start(value):
    quoted = False
    escaped = False
    for index, char in enumerate(value):
        if char == '"' and not escaped:
            quoted = not quoted
        if char == '#' and not quoted and (index == 0 or value[index - 1].isspace()):
            return index
        escaped = char == '\\' and not escaped
        if char != '\\':
            escaped = False
    return len(value)


def _replace_line(line, key, value):
    newline = '\n' if line.endswith('\n') else ''
    body = line[:-1] if newline else line
    match = re.match(rf'^(\s*{re.escape(key)}\s*=\s*)(.*)$', body)
    if not match:
        return line
    prefix, old = match.groups()
    split = _comment_start(old)
    suffix = old[split:]
    if suffix and not suffix.startswith(' '):
        suffix = ' ' + suffix
    return f'{prefix}{_literal(value)}{suffix}{newline}'


def _patch(text, desired, remove=None):
    """仅修改目标 section 的顶层键，返回保留注释的候选文本。"""
    remove = remove or set()
    lines = text.splitlines(keepends=True)
    for section, values in desired.items():
        sections = {}
        current = None
        for index, line in enumerate(lines):
            header = _section_header(line)
            if header is not None:
                current = header
                sections.setdefault(header, [index, index])
                sections[current][1] = index + 1
            elif current in sections:
                sections[current][1] = index + 1
        if section not in sections:
            if not any((section, key) not in remove for key in values):
                continue
            if lines and not lines[-1].endswith(('\n', '\r')):
                lines[-1] += '\n'
            if lines and lines[-1].strip():
                lines.append('\n')
            lines.append(f'[{section}]\n')
            for key, value in values.items():
                if (section, key) not in remove:
                    lines.append(f'{key} = {_literal(value)}\n')
            continue
        start, end = sections[section]
        present = set()
        nested = False
        for index in range(start, end):
            line = lines[index]
            if _section_header(line) is not None:
                continue
            for key, value in values.items():
                match = re.match(rf'^\s*{re.escape(key)}\s*=', line)
                if not match or key in present:
                    continue
                present.add(key)
                if (section, key) in remove:
                    lines[index] = ''
                else:
                    lines[index] = _replace_line(line, key, value)
        additions = [f'{key} = {_literal(value)}\n' for key, value in values.items()
                     if key not in present and (section, key) not in remove]
        if additions:
            insert_at = end
            lines[insert_at:insert_at] = additions
    return ''.join(lines)


def _field_key(section, key):
    return f'{section}.{key}'


def _current_values(parsed):
    result = {}
    for section, values in _TARGETS.items():
        node = parsed
        for part in section.split('.'):
            node = node.get(part) if isinstance(node, dict) else None
        for key in values:
            result[_field_key(section, key)] = (
                node[key] if isinstance(node, dict) and key in node else None,
                isinstance(node, dict) and key in node)
    return result


def _load_snapshot(path, logger=None):
    try:
        with open(path, encoding='utf-8-sig') as stream:
            value = json.load(stream)
        if value.get('version') != SNAPSHOT_VERSION or not isinstance(value.get('fields'), dict):
            raise ValueError('快照版本无效')
        return value
    except FileNotFoundError:
        return {'version': SNAPSHOT_VERSION, 'config_path': '', 'fields': {}}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        _log(logger, f'[codex] 读取代理快照失败：{type(exc).__name__}，将重新建立快照')
        return None


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')


def _atomic_write(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.codex.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_existing(path):
    if not os.path.exists(path):
        return '', {}
    text = _read_text(path)
    return text, _parse(text)


def _proxy_values(mixed_port):
    proxy = f'http://127.0.0.1:{int(mixed_port)}'
    return {
        section: {key: (proxy if value is None else value)
                  for key, value in values.items()}
        for section, values in _TARGETS.items()
    }


def sync(mixed_port, *, environ=None, data_root=None, logger=None):
    """同步当前端口；失败返回 warning，不抛出路由事务异常。"""
    started = time.monotonic()
    path = config_path(environ)
    snap_path = snapshot_path(data_root)
    _log(logger, f'[codex] 代理配置同步请求已提交：目标={os.path.basename(path)}')
    with _LOCK:
        try:
            text, parsed = _read_existing(path)
            desired = _proxy_values(mixed_port)
            current = _current_values(parsed)
            snapshot = _load_snapshot(snap_path, logger)
            if snapshot is None:
                raise ValueError('代理快照不可恢复，已跳过配置覆盖')
            fields = snapshot.setdefault('fields', {})
            changed = False
            for section, values in desired.items():
                for key, value in values.items():
                    field = _field_key(section, key)
                    current_value, exists = current[field]
                    if current_value == value and exists:
                        if field in fields:
                            fields[field]['last_written'] = value
                        continue
                    changed = True
                    if field not in fields:
                        fields[field] = {
                            'section': section, 'key': key,
                            'exists': exists, 'value': current_value,
                        }
                    fields[field]['last_written'] = value
            candidate = _patch(text, desired) if changed else text
            _parse(candidate)
            if changed:
                old_snapshot = None
                try:
                    with open(snap_path, 'rb') as stream:
                        old_snapshot = stream.read()
                except FileNotFoundError:
                    pass
                snapshot['config_path'] = path
                snapshot['mixed_port'] = int(mixed_port)
                _atomic_write(snap_path, _json_bytes(snapshot))
                try:
                    _atomic_write(path, candidate.encode('utf-8'))
                except Exception:
                    if old_snapshot is None:
                        try:
                            os.unlink(snap_path)
                        except OSError:
                            pass
                    else:
                        _atomic_write(snap_path, old_snapshot)
                    raise
            elif fields:
                snapshot['config_path'] = path
                snapshot['mixed_port'] = int(mixed_port)
                _atomic_write(snap_path, _json_bytes(snapshot))
            _log(logger, f'[codex] 代理配置同步成功：字段={len(desired)}，耗时={time.monotonic() - started:.2f}秒；已运行 Codex 需重启后加载')
            return {'ok': True, 'changed': changed, 'path': path}
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError, TypeError) as exc:
            _log(logger, f'[codex] 代理配置同步失败：{type(exc).__name__}，原文件未覆盖')
            return {'ok': False, 'warning': str(exc), 'path': path}


def restore(*, environ=None, data_root=None, logger=None):
    started = time.monotonic()
    path = config_path(environ)
    snap_path = snapshot_path(data_root)
    _log(logger, '[codex] 代理关闭恢复请求已提交')
    with _LOCK:
        snapshot = _load_snapshot(snap_path, logger)
        if snapshot is None:
            return {'ok': False, 'warning': '代理快照不可恢复，已跳过配置恢复', 'path': path}
        fields = snapshot.get('fields') or {}
        if not fields:
            return {'ok': True, 'changed': False, 'path': path}
        try:
            saved_path = str(snapshot.get('config_path') or '')
            if saved_path and os.path.normcase(os.path.abspath(saved_path)) != os.path.normcase(os.path.abspath(path)):
                _log(logger, '[codex] CODEX_HOME 已变化，跳过旧快照恢复')
                return {'ok': False, 'warning': 'CODEX_HOME 已变化', 'path': path}
            text, parsed = _read_existing(path)
            current = _current_values(parsed)
            desired = {section: dict(values) for section, values in _TARGETS.items()}
            remove = set()
            managed = {}
            warnings = []
            for field, record in fields.items():
                section = str(record.get('section') or '')
                key = str(record.get('key') or '')
                if not section or not key:
                    continue
                value, exists = current.get(field, (None, False))
                last = record.get('last_written')
                if exists and value == last:
                    if record.get('exists'):
                        managed.setdefault(section, {})[key] = record.get('value')
                    else:
                        managed.setdefault(section, {})[key] = None
                        remove.add((section, key))
                else:
                    warnings.append(field)
            if managed:
                candidate = _patch(text, managed, remove=remove)
                _parse(candidate)
                _atomic_write(path, candidate.encode('utf-8'))
            remaining = {field: record for field, record in fields.items() if field in warnings}
            if remaining:
                snapshot['fields'] = remaining
                _atomic_write(snap_path, _json_bytes(snapshot))
                _log(logger, f'[codex] 用户已修改字段，保留未恢复内容：{len(warnings)} 个')
            else:
                try:
                    os.unlink(snap_path)
                except FileNotFoundError:
                    pass
            _log(logger, f'[codex] 代理关闭恢复完成：已恢复={len(managed)}，警告={len(warnings)}，耗时={time.monotonic() - started:.2f}秒')
            return {'ok': True, 'changed': bool(managed), 'warnings': warnings, 'path': path}
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError, TypeError) as exc:
            _log(logger, f'[codex] 代理关闭恢复失败：{type(exc).__name__}，原文件未覆盖')
            return {'ok': False, 'warning': str(exc), 'path': path}


def managed_fields():
    return tuple(_field_key(section, key) for section, values in _TARGETS.items() for key in values)
