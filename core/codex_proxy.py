"""同步 cxvpn-tools 本机代理到 Codex 用户配置。"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import tomllib
from copy import deepcopy

from core import app_paths


SNAPSHOT_FILE_NAME = 'codex_proxy_snapshot.json'
SNAPSHOT_VERSION = 1
TRANSPORT_SNAPSHOT_FILE_NAME = 'codex_transport_snapshot.json'
TRANSPORT_SNAPSHOT_VERSION = 1
HTTP_PROVIDER_ID = 'cxvpn_openai_http'
HTTP_PROVIDER = {
    'name': 'OpenAI HTTP via CXVPN',
    'base_url': 'https://chatgpt.com/backend-api/codex',
    'wire_api': 'responses',
    'requires_openai_auth': True,
    'supports_websockets': False,
}
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


def transport_snapshot_path(data_root=None):
    root = os.path.abspath(data_root or app_paths.user_data_root())
    return os.path.join(root, TRANSPORT_SNAPSHOT_FILE_NAME)


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


def _transport_snapshot_default():
    return {
        'version': TRANSPORT_SNAPSHOT_VERSION,
        'config_path': '',
        'phase': '',
        'original_model_provider_exists': False,
        'original_model_provider': None,
        'provider_created': False,
        'last_written': {},
    }


def _load_transport_snapshot(path, logger=None):
    try:
        with open(path, encoding='utf-8-sig') as stream:
            value = json.load(stream)
        if (not isinstance(value, dict) or
                value.get('version') != TRANSPORT_SNAPSHOT_VERSION or
                not isinstance(value.get('last_written'), dict)):
            raise ValueError('传输快照版本无效')
        return value
    except FileNotFoundError:
        return _transport_snapshot_default()
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        _log(logger, f'[codex-transport] 读取恢复记录失败：{type(exc).__name__}')
        return None


def _same_path(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _root_region_end(lines):
    for index, line in enumerate(lines):
        if _section_header(line) is not None or re.match(r'^\s*\[\[', line):
            return index
    return len(lines)


def _simple_root_key_lines(text, key):
    lines = text.splitlines(keepends=True)
    end = _root_region_end(lines)
    matches = [index for index in range(end)
               if re.match(rf'^\s*{re.escape(key)}\s*=', lines[index])]
    return lines, end, matches


def _patch_root_key(text, key, value, *, remove=False):
    """只编辑根级 bare key；等价的 quoted/dotted 写法由调用方拒绝。"""
    lines, root_end, matches = _simple_root_key_lines(text, key)
    if len(matches) > 1:
        raise ValueError(f'根级 {key} 存在重复定义')
    if matches:
        index = matches[0]
        if remove:
            del lines[index]
        else:
            lines[index] = _replace_line(lines[index], key, value)
        return ''.join(lines)
    if remove:
        return text
    insertion = f'{key} = {_literal(value)}\n'
    if root_end and lines[root_end - 1].strip():
        insertion += '\n'
    lines[root_end:root_end] = [insertion]
    return ''.join(lines)


def _exact_provider_table_span(text):
    lines = text.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        header = _section_header(line)
        if header == f'model_providers.{HTTP_PROVIDER_ID}':
            if start is not None:
                raise ValueError('专用 HTTP Provider 存在重复 table')
            start = index
        elif start is not None and (header is not None or re.match(r'^\s*\[\[', line)):
            return lines, start, index
    return lines, start, len(lines) if start is not None else None


def _append_http_provider(text):
    if text and not text.endswith(('\n', '\r')):
        text += '\n'
    if text and text.strip():
        text += '\n'
    text += f'[model_providers.{HTTP_PROVIDER_ID}]\n'
    for key, value in HTTP_PROVIDER.items():
        text += f'{key} = {_literal(value)}\n'
    return text


def _remove_http_provider(text):
    lines, start, end = _exact_provider_table_span(text)
    if start is None:
        return text
    del lines[start:end]
    while start > 0 and start < len(lines) and not lines[start - 1].strip() and not lines[start].strip():
        del lines[start]
    return ''.join(lines)


def _provider_value(parsed):
    providers = parsed.get('model_providers')
    if not isinstance(providers, dict):
        return None, False
    return providers.get(HTTP_PROVIDER_ID), HTTP_PROVIDER_ID in providers


def _effective_provider(parsed):
    value = parsed.get('model_provider', 'openai')
    return value if isinstance(value, str) else None


def _validate_simple_transport_shape(text, parsed):
    """拒绝无法安全局部编辑的根键和 Provider 复杂写法。"""
    _lines, _end, root_matches = _simple_root_key_lines(text, 'model_provider')
    if 'model_provider' in parsed and len(root_matches) != 1:
        raise ValueError('model_provider 使用了 quoted/dotted 等复杂写法，无法安全修改')
    provider, exists = _provider_value(parsed)
    _table_lines, table_start, _table_end = _exact_provider_table_span(text)
    if exists and table_start is None:
        raise ValueError('专用 HTTP Provider 使用了 inline/quoted 等复杂写法，无法安全修改')
    if table_start is not None and not exists:
        raise ValueError('专用 HTTP Provider table 结构异常')
    return provider, exists


def _transport_expected(parsed, *, provider_value, provider_exists):
    expected = deepcopy(parsed)
    if provider_value is None:
        expected.pop('model_provider', None)
    else:
        expected['model_provider'] = provider_value
    providers = expected.get('model_providers')
    if provider_exists:
        if not isinstance(providers, dict):
            providers = {}
            expected['model_providers'] = providers
        providers[HTTP_PROVIDER_ID] = deepcopy(HTTP_PROVIDER)
    elif isinstance(providers, dict):
        providers.pop(HTTP_PROVIDER_ID, None)
        if not providers:
            expected.pop('model_providers', None)
    return expected


def _assert_candidate_matches(candidate, expected):
    parsed = _parse(candidate)
    if parsed != expected:
        raise ValueError('候选配置包含目标字段以外的结构变化，已拒绝写入')
    return parsed


def _write_config_if_unchanged(path, original_text, candidate):
    """写入前回读，避免用过期候选覆盖外部编辑器刚保存的内容。"""
    latest = _read_text(path) if os.path.exists(path) else ''
    if latest != original_text:
        raise ValueError('Codex 配置已被其他进程修改，请刷新后重试')
    _atomic_write(path, candidate.encode('utf-8'))


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


def _transport_summary(parsed, snapshot, *, snapshot_exists, path):
    active = _effective_provider(parsed)
    provider, provider_exists = _provider_value(parsed)
    providers = parsed.get('model_providers')
    base = {
        'transport_mode': 'unsupported',
        'websocket_enabled': None,
        'transport_managed': False,
        'transport_conflict': False,
        'active_provider': active,
        'transport_snapshot_exists': snapshot_exists,
        'transport_transaction_phase': '',
        'runtime_state_verified': False,
    }
    if active is None:
        return base
    if snapshot is None:
        if snapshot_exists:
            base.update(transport_mode='conflict', transport_conflict=True)
        return base
    phase = str(snapshot.get('phase') or '')
    base['transport_transaction_phase'] = phase
    if snapshot_exists:
        saved_path = str(snapshot.get('config_path') or '')
        if not saved_path or not _same_path(saved_path, path):
            base.update(transport_mode='conflict', transport_conflict=True)
            return base
    last = snapshot.get('last_written') or {}
    managed_target = (
        snapshot_exists and
        last.get('model_provider') == HTTP_PROVIDER_ID and
        last.get('provider') == HTTP_PROVIDER)
    invalid_provider_container = (
        providers is not None and not isinstance(providers, dict))
    built_in_override = isinstance(providers, dict) and 'openai' in providers
    if invalid_provider_container or built_in_override:
        if snapshot_exists:
            base.update(transport_mode='conflict', transport_conflict=True)
        return base
    original_matches = False
    if snapshot_exists and not provider_exists:
        original_exists = bool(snapshot.get('original_model_provider_exists'))
        original = snapshot.get('original_model_provider')
        original_matches = (
            (original_exists and active == original) or
            (not original_exists and 'model_provider' not in parsed and active == 'openai'))
    if managed_target and original_matches:
        base.update(transport_mode='wss_preferred', websocket_enabled=True)
        return base
    if active == HTTP_PROVIDER_ID:
        if managed_target and provider == HTTP_PROVIDER:
            base.update(
                transport_mode='http_only', websocket_enabled=False,
                transport_managed=True)
        else:
            base.update(transport_mode='conflict', transport_conflict=True)
        return base
    if snapshot_exists or provider_exists:
        base.update(transport_mode='conflict', transport_conflict=True)
        return base
    if active == 'openai':
        if 'openai_base_url' in parsed or 'profile' in parsed:
            return base
        base.update(transport_mode='wss_preferred', websocket_enabled=True)
        return base
    base['transport_mode'] = 'custom_provider'
    return base


def transport_status(*, environ=None, data_root=None, logger=None):
    """读取模型传输配置摘要；不把文件值表述为运行中进程状态。"""
    path = config_path(environ)
    snap_path = transport_snapshot_path(data_root)
    exists = os.path.isfile(path)
    try:
        _text, parsed = _read_existing(path)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        return {
            'transport_mode': 'invalid_config',
            'websocket_enabled': None,
            'transport_managed': False,
            'transport_conflict': False,
            'active_provider': None,
            'transport_snapshot_exists': os.path.isfile(snap_path),
            'transport_transaction_phase': '',
            'runtime_state_verified': False,
            'transport_error': f'{type(exc).__name__}: {exc}',
        }
    snapshot_exists = os.path.isfile(snap_path)
    snapshot = _load_transport_snapshot(snap_path, logger)
    result = _transport_summary(
        parsed, snapshot, snapshot_exists=snapshot_exists, path=path)
    result['transport_error'] = ''
    result['config_exists'] = exists
    return result


def set_websocket_enabled(enabled, *, environ=None, data_root=None, logger=None):
    """设置明确目标：True 恢复内置 openai；False 使用仅 HTTP/SSE Provider。"""
    if type(enabled) is not bool:
        return {'ok': False, 'warning': 'enabled 必须是布尔值'}
    started = time.monotonic()
    path = config_path(environ)
    snap_path = transport_snapshot_path(data_root)
    target_label = '优先 WSS' if enabled else '仅 HTTP/SSE'
    _log(logger, f'[codex-transport] 切换请求已提交：目标={target_label}')
    with _LOCK:
        _log(logger, f'[codex-transport] 请求已领取，开始执行：目标={target_label}')
        try:
            text, parsed = _read_existing(path)
            provider, provider_exists = _validate_simple_transport_shape(text, parsed)
            unsupported_source = (
                '检测到自定义 openai_base_url，未修改传输配置'
                if 'openai_base_url' in parsed else
                ('检测到活动 profile，无法确认有效 model_provider，未修改配置'
                 if 'profile' in parsed else ''))
            providers = parsed.get('model_providers')
            if not enabled and isinstance(providers, dict) and 'openai' in providers:
                raise ValueError('检测到对内置 openai Provider 的覆盖，未修改传输配置')
            if providers is not None and not isinstance(providers, dict):
                raise ValueError('model_providers 类型无效')
            active = _effective_provider(parsed)
            if active is None:
                raise ValueError('model_provider 类型无效')
            snapshot_exists = os.path.isfile(snap_path)
            snapshot = _load_transport_snapshot(snap_path, logger)
            if snapshot is None:
                raise ValueError('传输恢复记录损坏，已停止修改配置')
            if unsupported_source and (not enabled or not snapshot_exists):
                raise ValueError(unsupported_source)
            summary = _transport_summary(
                parsed, snapshot, snapshot_exists=snapshot_exists, path=path)
            if summary['transport_conflict']:
                last = snapshot.get('last_written') or {}
                recoverable_restore = (
                    enabled and snapshot_exists and
                    active == HTTP_PROVIDER_ID and
                    last.get('model_provider') == HTTP_PROVIDER_ID)
                if not recoverable_restore:
                    raise ValueError('传输配置与恢复记录冲突，请先检查配置文件')

            if not enabled:
                if active == HTTP_PROVIDER_ID and summary['transport_managed']:
                    return {'ok': True, 'changed': False, 'path': path,
                            'transport_mode': 'http_only'}
                if active != 'openai':
                    raise ValueError(f'当前 model_provider={active}，自定义 Provider 不会自动切换')
                if provider_exists:
                    raise ValueError(f'Provider ID {HTTP_PROVIDER_ID} 已存在且不受本工具托管')
                candidate = _patch_root_key(text, 'model_provider', HTTP_PROVIDER_ID)
                candidate = _append_http_provider(candidate)
                expected = _transport_expected(
                    parsed, provider_value=HTTP_PROVIDER_ID, provider_exists=True)
                _assert_candidate_matches(candidate, expected)
                record = {
                    'version': TRANSPORT_SNAPSHOT_VERSION,
                    'config_path': path,
                    'phase': 'prepared',
                    'original_model_provider_exists': 'model_provider' in parsed,
                    'original_model_provider': parsed.get('model_provider'),
                    'provider_created': True,
                    'last_written': {
                        'model_provider': HTTP_PROVIDER_ID,
                        'provider': deepcopy(HTTP_PROVIDER),
                    },
                }
                old_snapshot = None
                if snapshot_exists:
                    with open(snap_path, 'rb') as stream:
                        old_snapshot = stream.read()
                _atomic_write(snap_path, _json_bytes(record))
                try:
                    _write_config_if_unchanged(path, text, candidate)
                except Exception:
                    if old_snapshot is None:
                        try:
                            os.unlink(snap_path)
                        except OSError:
                            pass
                    else:
                        _atomic_write(snap_path, old_snapshot)
                    raise
                record['phase'] = 'committed'
                try:
                    _atomic_write(snap_path, _json_bytes(record))
                except OSError as exc:
                    _log(logger, f'[codex-transport] 配置已写入但恢复记录提交失败：{type(exc).__name__}')
                    return {
                        'ok': False, 'changed': True, 'path': path,
                        'warning': '配置已写入为仅 HTTP/SSE，但恢复记录仍处于 prepared；请勿手动删除恢复记录',
                    }
                _log(logger, f'[codex-transport] 切换成功：目标=仅 HTTP/SSE，耗时={time.monotonic() - started:.2f}秒；重启 Codex 后生效')
                return {'ok': True, 'changed': True, 'path': path,
                        'transport_mode': 'http_only'}

            if active == 'openai' and not provider_exists and not snapshot_exists:
                return {'ok': True, 'changed': False, 'path': path,
                        'transport_mode': 'wss_preferred'}
            if (enabled and snapshot_exists and not provider_exists and
                    summary['transport_mode'] == 'wss_preferred'):
                try:
                    os.unlink(snap_path)
                except FileNotFoundError:
                    pass
                _log(logger, '[codex-transport] 已清理未应用或恢复后残留的事务记录')
                return {'ok': True, 'changed': False, 'path': path,
                        'transport_mode': 'wss_preferred'}
            if not snapshot_exists:
                if active != 'openai':
                    raise ValueError(f'当前 model_provider={active}，没有可信恢复记录，未修改配置')
                raise ValueError(f'Provider ID {HTTP_PROVIDER_ID} 已存在且不受本工具托管')
            saved_path = str(snapshot.get('config_path') or '')
            if not saved_path or not _same_path(saved_path, path):
                raise ValueError('CODEX_HOME 已变化，未使用旧恢复记录')
            last = snapshot.get('last_written') or {}
            if active != last.get('model_provider'):
                raise ValueError('model_provider 已被用户修改，未自动恢复')
            original_exists = bool(snapshot.get('original_model_provider_exists'))
            original = snapshot.get('original_model_provider')
            if original_exists and original != 'openai':
                raise ValueError('恢复记录中的原 Provider 不在首版支持范围')
            candidate = _patch_root_key(
                text, 'model_provider', original,
                remove=not original_exists)
            warnings = []
            remove_provider = bool(
                snapshot.get('provider_created') and provider == last.get('provider'))
            if remove_provider:
                candidate = _remove_http_provider(candidate)
            elif provider_exists:
                warnings.append('专用 HTTP Provider 已被手动修改，已保留')
            else:
                warnings.append('专用 HTTP Provider 已被手动删除')
            expected = deepcopy(parsed)
            if original_exists:
                expected['model_provider'] = original
            else:
                expected.pop('model_provider', None)
            if remove_provider:
                providers = expected.get('model_providers')
                if isinstance(providers, dict):
                    providers.pop(HTTP_PROVIDER_ID, None)
                    if not providers:
                        expected.pop('model_providers', None)
            _assert_candidate_matches(candidate, expected)
            _write_config_if_unchanged(path, text, candidate)
            try:
                os.unlink(snap_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log(logger, f'[codex-transport] 配置已恢复但恢复记录删除失败：{type(exc).__name__}')
                return {
                    'ok': False, 'changed': True, 'path': path,
                    'warning': '配置已恢复为优先 WSS，但旧恢复记录删除失败，请重试恢复操作',
                }
            _log(logger, f'[codex-transport] 切换成功：目标=优先 WSS，警告={len(warnings)}，耗时={time.monotonic() - started:.2f}秒；重启 Codex 后生效')
            return {'ok': True, 'changed': True, 'warnings': warnings,
                    'path': path, 'transport_mode': 'wss_preferred'}
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError, TypeError) as exc:
            _log(logger, f'[codex-transport] 切换失败：目标={target_label}，阶段=配置校验或写入，类型={type(exc).__name__}，耗时={time.monotonic() - started:.2f}秒')
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
