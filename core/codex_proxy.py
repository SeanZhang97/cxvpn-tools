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
            if line is None:
                continue
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
        for index in range(start, end):
            line = lines[index]
            if line is None or _section_header(line) is not None:
                continue
            for key, value in values.items():
                if key in present:
                    continue
                if not re.match(rf'^\s*{re.escape(key)}\s*=', line):
                    continue
                present.add(key)
                if (section, key) in remove:
                    lines[index] = None
                else:
                    lines[index] = _replace_line(line, key, value)
        additions = [f'{key} = {_literal(value)}\n' for key, value in values.items()
                     if key not in present and (section, key) not in remove]
        if additions:
            insert_at = end
            lines[insert_at:insert_at] = additions
    return ''.join(line for line in lines if line is not None)


def _field_key(section, key):
    return f'{section}.{key}'


_LOOPBACK_PROXY_RE = re.compile(r'^http://127\.0\.0\.1:\d{1,5}$')


def _looks_managed(section, key, value):
    """无快照记录时，判断托管字段的值是否为可识别的本工具写入模式。"""
    if value is None:
        return False
    if section == 'features' and key == 'respect_system_proxy':
        return value is True
    if key in ('NO_PROXY', 'no_proxy'):
        return value == _NO_PROXY
    return isinstance(value, str) and bool(_LOOPBACK_PROXY_RE.match(value))


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
    """关闭代理：删除仍等于本工具最后写入值的托管字段；用户改过的保留。"""
    started = time.monotonic()
    path = config_path(environ)
    snap_path = snapshot_path(data_root)
    _log(logger, '[codex] 关闭代理请求已提交（删除托管字段）')
    with _LOCK:
        snapshot = _load_snapshot(snap_path, logger)
        if snapshot is None:
            return {'ok': False, 'warning': '代理快照不可恢复，已跳过配置删除', 'path': path}
        fields = snapshot.get('fields') or {}
        try:
            saved_path = str(snapshot.get('config_path') or '')
            if saved_path and os.path.normcase(os.path.abspath(saved_path)) != os.path.normcase(os.path.abspath(path)):
                _log(logger, '[codex] CODEX_HOME 已变化，跳过旧快照删除')
                return {'ok': False, 'warning': 'CODEX_HOME 已变化', 'path': path}
            text, parsed = _read_existing(path)
            current = _current_values(parsed)
            managed = {}
            remove = set()
            warnings = []
            for field, record in fields.items():
                section = str(record.get('section') or '')
                key = str(record.get('key') or '')
                if not section or not key:
                    continue
                value, exists = current.get(field, (None, False))
                last = record.get('last_written')
                if exists and value == last:
                    managed.setdefault(section, {})[key] = None
                    remove.add((section, key))
                else:
                    warnings.append(field)
            residual = 0
            for section, values in _TARGETS.items():
                for key in values:
                    field = _field_key(section, key)
                    if field in fields:
                        continue
                    value, exists = current.get(field, (None, False))
                    if exists and _looks_managed(section, key, value):
                        managed.setdefault(section, {})[key] = None
                        remove.add((section, key))
                        residual += 1
            if not managed:
                return {'ok': True, 'changed': False, 'warnings': warnings, 'path': path}
            candidate = _patch(text, managed, remove=remove)
            _parse(candidate)
            _atomic_write(path, candidate.encode('utf-8'))
            remaining = {field: record for field, record in fields.items() if field in warnings}
            if remaining:
                snapshot['fields'] = remaining
                _atomic_write(snap_path, _json_bytes(snapshot))
                _log(logger, f'[codex] 用户已修改字段，保留未删除内容：{len(warnings)} 个')
            else:
                try:
                    os.unlink(snap_path)
                except FileNotFoundError:
                    pass
            _log(logger, f'[codex] 关闭代理完成：已删除托管字段={len(remove)}（含无快照兜底={residual}），警告={len(warnings)}，耗时={time.monotonic() - started:.2f}秒；已运行 Codex 需重启后加载')
            return {'ok': True, 'changed': bool(managed), 'warnings': warnings, 'path': path}
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError, TypeError) as exc:
            _log(logger, f'[codex] 关闭代理删除失败：{type(exc).__name__}，原文件未覆盖')
            return {'ok': False, 'warning': str(exc), 'path': path}


MAX_VIEW_BYTES = 512 * 1024


def read_config(*, environ=None, logger=None):
    """只读返回配置原文供 UI 查看；日志只记录存在性与大小，不记录正文。"""
    started = time.monotonic()
    path = config_path(environ)
    try:
        if not os.path.isfile(path):
            _log(logger, f'[codex] 读取配置文件：不存在，路径已返回，耗时={time.monotonic() - started:.2f}秒')
            return {'ok': True, 'exists': False, 'path': path, 'text': '', 'bytes': 0}
        size = os.path.getsize(path)
        if size > MAX_VIEW_BYTES:
            _log(logger, f'[codex] 读取配置文件：超出 {MAX_VIEW_BYTES} 字节查看上限，未传输内容')
            return {'ok': False, 'exists': True, 'path': path, 'text': '',
                    'warning': f'配置文件超过 {MAX_VIEW_BYTES} 字节查看上限，请用编辑器查看'}
        text = _read_text(path)
        _log(logger, f'[codex] 读取配置文件：成功，大小={len(text.encode("utf-8"))} 字节，耗时={time.monotonic() - started:.2f}秒')
        return {'ok': True, 'exists': True, 'path': path, 'text': text,
                'bytes': len(text.encode('utf-8'))}
    except UnicodeDecodeError as exc:
        _log(logger, f'[codex] 读取配置文件失败（编码不兼容）：UnicodeDecodeError')
        return {'ok': False, 'exists': True, 'path': path, 'text': '',
                'warning': f'配置文件不是 UTF-8 编码，无法在界面查看：{exc}'}
    except OSError as exc:
        _log(logger, f'[codex] 读取配置文件失败：{type(exc).__name__}')
        return {'ok': False, 'exists': True, 'path': path, 'text': '',
                'warning': f'读取 Codex 配置文件失败：{exc}'}


def status(*, environ=None, data_root=None, logger=None):
    """返回 Codex 配置同步状态摘要，不含配置正文与任何敏感值。"""
    path = config_path(environ)
    snap_path = snapshot_path(data_root)
    exists = os.path.isfile(path)
    parse_error = ''
    parsed = {}
    if exists:
        try:
            text, parsed = _read_existing(path)
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            parse_error = f'{type(exc).__name__}: {exc}'
    current = _current_values(parsed)
    snapshot = _load_snapshot(snap_path, logger)
    if snapshot is None:
        snapshot = {'version': SNAPSHOT_VERSION, 'config_path': '', 'fields': {}}
    fields = snapshot.get('fields') or {}
    synced_fields = []
    deviated_fields = []
    for field, record in fields.items():
        last = record.get('last_written')
        if last is None:
            continue
        synced_fields.append(field)
        value, present = current.get(field, (None, False))
        if not present or value != last:
            deviated_fields.append(field)
    residual_fields = []
    for section, values in _TARGETS.items():
        for key in values:
            field = _field_key(section, key)
            if field in fields:
                continue
            value, present = current.get(field, (None, False))
            if present and _looks_managed(section, key, value):
                residual_fields.append(field)
    return {
        'ok': True,
        'path': path,
        'config_exists': exists,
        'parse_error': parse_error,
        'snapshot_exists': os.path.isfile(snap_path),
        'snapshot_fields': len(fields),
        'last_mixed_port': int(snapshot.get('mixed_port') or 0),
        'synced_fields': len(synced_fields),
        'deviated_fields': deviated_fields,
        'residual_fields': residual_fields,
    }


def managed_fields():
    return tuple(_field_key(section, key) for section, values in _TARGETS.items() for key in values)
