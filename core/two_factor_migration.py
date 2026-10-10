# -*- coding: utf-8 -*-
"""Google Authenticator 迁移格式的有界解析与加密分页暂存。"""
import base64
import hashlib
import time
import uuid
from urllib.parse import parse_qsl, urlsplit

from core.two_factor import MAX_ACCOUNTS, TwoFactorError, normalize, _seal, _unseal

MAX_PAGES = 100
MAX_PAYLOAD = 64 * 1024
MAX_URI = 96 * 1024


def _invalid():
    return TwoFactorError('Google 导出二维码数据损坏或格式无效，请重新截图')


def _varint(data, offset):
    value = 0
    for index in range(10):
        if offset >= len(data):
            raise _invalid()
        byte = data[offset]
        offset += 1
        if index == 9 and byte > 1:
            raise _invalid()
        value |= (byte & 127) << (7 * index)
        if not byte & 128:
            return value, offset
    raise _invalid()


def _fields(data, schema, repeated=()):
    """只处理所需 protobuf 类型；未知普通字段可跳过，拒绝递归 group。"""
    values, offset, count = {}, 0, 0
    while offset < len(data):
        count += 1
        if count > 4096:
            raise _invalid()
        tag, offset = _varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not 0 < number < 2**29:
            raise _invalid()
        if wire == 0:
            value, offset = _varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, offset = _varint(data, offset)
            else:
                size = 8 if wire == 1 else 4
            if size > len(data) - offset:
                raise _invalid()
            value = data[offset:offset + size]
            offset += size
        else:
            raise _invalid()
        if number not in schema:
            continue
        if wire != schema[number] or (number in values and number not in repeated):
            raise _invalid()
        if number in repeated:
            values.setdefault(number, []).append(value)
        else:
            values[number] = value
    return values


def _account(data):
    fields = _fields(data, {1: 2, 2: 2, 3: 2, 4: 0, 5: 0, 6: 0, 7: 0})
    try:
        name = fields.get(2, b'').decode('utf-8').strip()
        issuer = fields.get(3, b'').decode('utf-8').strip()
    except UnicodeError:
        raise _invalid() from None
    if len(name) > 512 or len(issuer) > 128 or any(ord(c) < 32 for c in name + issuer):
        raise _invalid()
    prefix, separator, suffix = name.partition(':')
    if separator and (not issuer or prefix.strip() == issuer):
        issuer = issuer or prefix.strip()
        name = suffix.strip()
    metadata = {'issuer': issuer or '未命名平台', 'account': name}
    algorithm = {0: 'SHA1', 1: 'SHA1', 2: 'SHA256', 3: 'SHA512'}.get(fields.get(4, 0))
    digits = {0: 6, 1: 6, 2: 8}.get(fields.get(5, 0))
    kind = fields.get(6, 0)
    if kind not in (0, 2):
        reason = 'HOTP 计数验证码暂不支持' if kind == 1 else '验证码类型暂不支持'
    elif algorithm is None or digits is None:
        reason = '算法或验证码位数暂不支持'
    else:
        try:
            return normalize({**metadata, 'secret': base64.b32encode(fields.get(1, b'')).decode('ascii'),
                              'algorithm': algorithm, 'digits': digits, 'period': 30}), None
        except TwoFactorError as exc:
            reason = str(exc)
    return None, {**metadata, 'reason': reason}


def parse_export(uri):
    if not isinstance(uri, str) or len(uri) > MAX_URI:
        raise _invalid()
    try:
        parsed = urlsplit(uri)
        if (parsed.scheme.lower(), parsed.netloc.lower()) != ('otpauth-migration', 'offline'):
            raise TwoFactorError('请选择 Google Authenticator 导出账号的二维码')
        pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=4)
        if parsed.path not in ('', '/') or parsed.fragment or len(pairs) != 1 or pairs[0][0] != 'data':
            raise _invalid()
        # 容忍未转义的 Base64 加号；标准导出会使用百分号转义。
        encoded = pairs[0][1].replace(' ', '+')
        payload = base64.b64decode(encoded + '=' * (-len(encoded) % 4), validate=True)
        if not payload or len(payload) > MAX_PAYLOAD:
            raise _invalid()
        fields = _fields(payload, {1: 2, 2: 0, 3: 0, 4: 0, 5: 0}, repeated=(1,))
        # iOS 导出版本 2 沿用现有账号及分页字段；附加字段由有界读取器跳过。
        if fields.get(2, 0) not in (1, 2):
            raise TwoFactorError('暂不支持此 Google 导出版本，请更新本软件后重试')
        size, index = fields.get(3, 1), fields.get(4, 0)
        if not 1 <= size <= MAX_PAGES or not 0 <= index < size:
            raise _invalid()
        records = fields.get(1, [])
        if not records or len(records) > MAX_ACCOUNTS:
            raise _invalid()
        accounts, skipped = [], []
        for data in records:
            value, error = _account(data)
            (accounts if value else skipped).append(value or error)
        return {'id': fields.get(5, 0), 'size': size, 'index': index,
                'digest': hashlib.sha256(payload).hexdigest(), 'accounts': accounts, 'skipped': skipped}
    except TwoFactorError:
        raise
    except (ValueError, TypeError, UnicodeError):
        raise _invalid() from None


class MigrationBatch:
    def __init__(self, previous=None):
        self.pages = dict(previous.pages) if previous else {}
        self.identity = previous.identity if previous else None
        self.deadline = time.monotonic() + 300

    def add(self, uris):
        for uri in uris:
            page = parse_export(uri)
            identity = (page['id'], page['size'])
            if self.identity is not None and self.identity != identity:
                raise TwoFactorError('图片来自不同导出批次，请点击“重新开始”后选择同一次导出的全部二维码')
            self.identity = identity
            previous = self.pages.get(page['index'])
            if previous:
                if previous['digest'] != page['digest']:
                    raise TwoFactorError('同一页二维码的内容不一致，请重新选择同一次导出的图片')
                continue
            encrypted = []
            for value in page['accounts']:
                identifier = uuid.uuid4().hex
                encrypted.append({**value, 'id': identifier, 'secret': _seal(value, identifier)})
            self.pages[page['index']] = {**page, 'accounts': encrypted}
            count = sum(len(p['accounts']) + len(p['skipped']) for p in self.pages.values())
            if count > MAX_ACCOUNTS:
                raise TwoFactorError('本批次超过 1000 个账号，请分次导出')

    def status(self):
        missing = [i + 1 for i in range(self.identity[1]) if i not in self.pages]
        pages = [self.pages[i] for i in sorted(self.pages)]
        return {'received': len(pages), 'total': self.identity[1], 'missing': missing,
                'accounts': [{'issuer': r['issuer'], 'account': r['account']}
                             for page in pages for r in page['accounts']],
                'skipped': [r for page in pages for r in page['skipped']]}

    def accounts(self):
        return [_unseal(row) for i in sorted(self.pages) for row in self.pages[i]['accounts']]
