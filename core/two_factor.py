# -*- coding: utf-8 -*-
"""本地 TOTP 账号；不参与普通配置、兼容 JSON 或代理配置 revision。"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import re
import time
from urllib.parse import parse_qsl, unquote, urlsplit
import uuid

import pyotp

from core import state_store

MAX_ACCOUNTS = 1000
ALGORITHMS = {'SHA1': hashlib.sha1, 'SHA256': hashlib.sha256, 'SHA512': hashlib.sha512}
PUBLIC_FIELDS = ('id', 'issuer', 'account', 'algorithm', 'digits', 'period', 'revision')


class TwoFactorError(ValueError):
    """可向用户展示的固定错误，不包含输入密钥或二维码正文。"""


def _integer(value, label, minimum, maximum):
    if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)):
        raise TwoFactorError(f'{label}必须为整数')
    result = int(value)
    if not minimum <= result <= maximum:
        raise TwoFactorError(f'{label}超出允许范围（{minimum}–{maximum}）')
    return result


def _label(value, name, limit, required):
    if not isinstance(value, str):
        raise TwoFactorError(f'{name}必须是文本')
    value = value.strip()
    if len(value) > limit or any(ord(c) < 32 for c in value):
        raise TwoFactorError(f'{name}过长或包含控制字符')
    if required and not value:
        raise TwoFactorError(f'请填写{name}')
    return value


def parse_uri(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() != 'otpauth' or parsed.netloc.lower() != 'totp':
            raise TwoFactorError('仅支持标准 TOTP 密钥链接，不支持 HOTP 或批量迁移码')
        if parsed.fragment:
            raise TwoFactorError('密钥链接格式无效')
        pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=16)
        params = dict(pairs)
        if len(params) != len(pairs):
            raise TwoFactorError('密钥链接包含重复参数')
        label = unquote(parsed.path.lstrip('/'))
        prefix, account = label.split(':', 1) if ':' in label else ('', label)
        issuer = params.get('issuer', prefix).strip()
        if prefix and issuer != prefix.strip():
            raise TwoFactorError('密钥链接中的平台名称不一致')
        return {'issuer': issuer, 'account': account.strip(),
                'secret': params.get('secret', ''), 'algorithm': params.get('algorithm', 'SHA1'),
                'digits': params.get('digits', 6), 'period': params.get('period', 30)}
    except (ValueError, TypeError) as exc:
        if isinstance(exc, TwoFactorError):
            raise
        raise TwoFactorError('密钥链接格式无效') from None


def normalize(value, require_labels=True):
    if not isinstance(value, dict):
        raise TwoFactorError('账号数据格式无效')
    secret = value.get('secret', '')
    if not isinstance(secret, str) or len(secret) > 8192:
        raise TwoFactorError('密钥格式无效或过长')
    value = dict(value)
    if secret.strip().lower().startswith(('otpauth:', 'otpauth-migration:')):
        parsed = parse_uri(secret.strip())
        parsed.update({k: value[k] for k in ('issuer', 'account') if value.get(k)})
        value = parsed
        secret = value['secret']
    secret = re.sub(r'\s+', '', secret).upper().rstrip('=')
    if not re.fullmatch(r'[A-Z2-7]{16,256}', secret):
        raise TwoFactorError('请输入有效的 Base32 密钥（至少 16 个字符）')
    try:
        decoded = base64.b32decode(secret + '=' * (-len(secret) % 8))
        if len(decoded) < 10 or base64.b32encode(decoded).decode('ascii').rstrip('=') != secret:
            raise ValueError()
    except ValueError:
        raise TwoFactorError('Base32 密钥编码无效') from None
    algorithm = str(value.get('algorithm', 'SHA1')).upper()
    if algorithm not in ALGORITHMS:
        raise TwoFactorError('算法仅支持 SHA1、SHA256 和 SHA512')
    digits = _integer(value.get('digits', 6), '验证码位数', 6, 8)
    if digits not in (6, 8):
        raise TwoFactorError('验证码位数仅支持 6 或 8')
    return {'issuer': _label(value.get('issuer', ''), '平台名称', 128, require_labels),
            'account': _label(value.get('account', ''), '账号', 256, require_labels),
            'secret': secret, 'algorithm': algorithm, 'digits': digits,
            'period': _integer(value.get('period', 30), '更新周期（秒）', 1, 86400)}


def current_code(value, now=None):
    now = time.time() if now is None else now
    otp = pyotp.TOTP(value['secret'], digits=value['digits'],
                    digest=ALGORITHMS[value['algorithm']], interval=value['period'])
    return {'code': otp.at(now), 'expires_at': (int(now) // value['period'] + 1) * value['period']}


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data, account_id, decrypt=False):
    """当前 Windows 用户范围；条目 ID 作为额外熵，防止密文在记录间互换。"""
    raw = ctypes.create_string_buffer(data)
    entropy_raw = ctypes.create_string_buffer(('CXVPNTools.TOTP.v1:' + account_id).encode('utf-8'))
    source = _Blob(len(data), ctypes.cast(raw, ctypes.POINTER(ctypes.c_ubyte)))
    entropy = _Blob(len(entropy_raw) - 1, ctypes.cast(entropy_raw, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    try:
        if not function(ctypes.byref(source), None, ctypes.byref(entropy), None,
                        None, 1, ctypes.byref(target)):
            raise TwoFactorError('密钥无法解密，请使用原 Windows 用户或从加密备份恢复' if decrypt
                                 else 'Windows 密钥加密失败，账号未保存')
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.memset(raw, 0, len(raw))
        if target.data:
            ctypes.memset(target.data, 0, target.size)
            kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def _seal(value, account_id):
    payload = {key: value[key] for key in ('secret', 'algorithm', 'digits', 'period')}
    return _dpapi(json.dumps(payload, ensure_ascii=False).encode('utf-8'), account_id)


def _unseal(row):
    try:
        payload = json.loads(_dpapi(bytes(row['secret']), row['id'], decrypt=True).decode('utf-8'))
        return normalize({**row, **payload})
    except (ValueError, UnicodeError, TypeError, KeyError):
        raise TwoFactorError('密钥无法解密，请使用原 Windows 用户或从加密备份恢复') from None


def _rows(conn):
    cursor = conn.execute('SELECT * FROM otp_accounts ORDER BY created_at,id')
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _signature(rows):
    return hashlib.sha256(json.dumps(sorted((r['id'], r['revision']) for r in rows)).encode()).hexdigest()


def _identity(value):
    return (value['issuer'].casefold(), value['account'].casefold())


class AccountStore:
    def __init__(self, root=None):
        self.root = root

    def records(self):
        with state_store._reader(self.root) as conn:
            return _rows(conn)

    def listing(self):
        now = time.time()
        result = []
        for row in self.records():
            item = {key: row[key] for key in PUBLIC_FIELDS}
            try:
                item.update(current_code(_unseal(row), now))
            except TwoFactorError as exc:
                item.update(code='', expires_at=0, error=str(exc))
            result.append(item)
        return {'ok': True, 'accounts': result, 'now': now}

    def code(self, account_id):
        row = next((r for r in self.records() if r['id'] == account_id), None)
        if row is None:
            raise TwoFactorError('账号已删除，请刷新列表')
        return {'ok': True, **current_code(_unseal(row)), 'now': time.time()}

    def save(self, payload):
        if not isinstance(payload, dict):
            raise TwoFactorError('账号数据格式无效')
        account_id = payload.get('id') or uuid.uuid4().hex
        with state_store.transaction(self.root) as conn:
            rows = _rows(conn)
            previous = next((row for row in rows if row['id'] == account_id), None)
            if payload.get('id') and previous is None:
                raise TwoFactorError('账号已删除，请刷新列表')
            if previous and payload.get('revision') != previous['revision']:
                raise TwoFactorError('账号已被修改，请重新打开编辑窗口')
            value = dict(payload)
            if previous and not value.get('secret', '').strip():
                value['secret'] = _unseal(previous)['secret']
            value = normalize(value)
            if any(_identity(r) == _identity(value) and r['id'] != account_id for r in rows):
                raise TwoFactorError('相同平台和账号已存在，请编辑现有记录')
            if not previous and len(rows) >= MAX_ACCOUNTS:
                raise TwoFactorError('账号数量已达上限（1000）')
            now = time.time_ns()
            conn.execute('INSERT INTO otp_accounts VALUES(?,?,?,?,?,?,?,?,?,?) '
                         'ON CONFLICT(id) DO UPDATE SET issuer=excluded.issuer, account=excluded.account, '
                         'algorithm=excluded.algorithm, digits=excluded.digits, period=excluded.period, '
                         'secret=excluded.secret, revision=excluded.revision, updated_at=excluded.updated_at',
                         (account_id, value['issuer'], value['account'], value['algorithm'], value['digits'],
                          value['period'], _seal(value, account_id), previous['revision'] + 1 if previous else 1,
                          previous['created_at'] if previous else now, now))
        return {'ok': True, 'id': account_id, 'msg': '账号已保存'}

    def delete(self, account_id, revision):
        with state_store.transaction(self.root) as conn:
            result = conn.execute('DELETE FROM otp_accounts WHERE id=? AND revision=?', (account_id, revision))
            if result.rowcount != 1:
                raise TwoFactorError('账号已变化，请刷新后再删除')
        return {'ok': True, 'msg': '账号已删除'}

    def export_records(self):
        return [_unseal(row) for row in self.records()]

    def prepare_import(self, incoming):
        rows = self.records()
        existing = {_identity(row): row for row in rows}
        additions, replacements, conflicts, duplicates = [], [], [], 0
        for payload in incoming:
            value = normalize(payload)
            identity = _identity(value)
            previous = existing.get(identity)
            if previous:
                try:
                    original = _unseal(previous)
                except TwoFactorError:
                    # 当前 Windows 用户无法读取旧密文时，允许从已验证密码的备份恢复。
                    record = {**value, 'id': previous['id'], 'secret': _seal(value, previous['id'])}
                    replacements.append(record)
                    existing[identity] = record
                    continue
                if all(original[k] == value[k] for k in ('secret', 'algorithm', 'digits', 'period')):
                    duplicates += 1
                else:
                    conflicts.append({'issuer': value['issuer'], 'account': value['account']})
                continue
            account_id = uuid.uuid4().hex
            record = {**value, 'id': account_id, 'secret': _seal(value, account_id)}
            additions.append(record)
            existing[identity] = record
        if len(rows) + len(additions) > MAX_ACCOUNTS:
            raise TwoFactorError('导入后账号数量超过上限（1000），未修改现有数据')
        return {'signature': _signature(rows), 'additions': additions, 'replacements': replacements, 'duplicates': duplicates,
                'conflicts': conflicts}

    def apply_import(self, prepared):
        with state_store.transaction(self.root) as conn:
            if _signature(_rows(conn)) != prepared['signature']:
                raise TwoFactorError('预览后账号已变化，请重新选择备份并预览')
            now = time.time_ns()
            conn.executemany('UPDATE otp_accounts SET algorithm=?,digits=?,period=?,secret=?, '
                             'revision=revision+1,updated_at=? WHERE id=?', [
                (r['algorithm'], r['digits'], r['period'], r['secret'], now, r['id'])
                for r in prepared['replacements']])
            conn.executemany('INSERT INTO otp_accounts VALUES(?,?,?,?,?,?,?,?,?,?)', [
                (r['id'], r['issuer'], r['account'], r['algorithm'], r['digits'], r['period'],
                 r['secret'], 1, now + i, now + i) for i, r in enumerate(prepared['additions'])])
        restored = len(prepared['replacements'])
        suffix = f'，已恢复 {restored} 个不可读账号' if restored else ''
        return {'ok': True, 'msg': f"已导入 {len(prepared['additions'])} 个账号{suffix}，重复或冲突项已跳过"}
