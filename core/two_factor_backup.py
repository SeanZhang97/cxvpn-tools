# -*- coding: utf-8 -*-
"""独立的可迁移加密备份；固定版本参数避免不可信文件指定任意 KDF 成本。"""
import json
import os
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from core.two_factor import MAX_ACCOUNTS, TwoFactorError, normalize

MAGIC = b'CXVPN-2FA\x00\x01'
MAX_BYTES = 4 * 1024 * 1024


def _key(password, salt):
    if not isinstance(password, str) or not 10 <= len(password) <= 512:
        raise TwoFactorError('备份密码长度需为 10–512 个字符')
    return Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(password.encode('utf-8'))


def encrypt(records, password):
    salt, nonce = os.urandom(16), os.urandom(12)
    payload = json.dumps({'accounts': records}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    key = _key(password, salt)
    return MAGIC + salt + nonce + AESGCM(key).encrypt(nonce, payload, MAGIC + salt)


def decrypt(content, password):
    if not isinstance(content, bytes) or not len(MAGIC) + 44 <= len(content) <= MAX_BYTES:
        raise TwoFactorError('备份文件大小或格式无效')
    if not content.startswith(MAGIC):
        raise TwoFactorError('不支持此备份格式或版本，请选择 CXVPNTools 的 .cx2fa 文件')
    offset = len(MAGIC)
    salt, nonce = content[offset:offset + 16], content[offset + 16:offset + 28]
    try:
        raw = AESGCM(_key(password, salt)).decrypt(nonce, content[offset + 28:], MAGIC + salt)
        document = json.loads(raw.decode('utf-8'))
        records = document['accounts']
        if not isinstance(records, list) or len(records) > MAX_ACCOUNTS:
            raise ValueError()
        return [normalize(record) for record in records]
    except InvalidTag:
        raise TwoFactorError('密码不正确或备份文件已损坏，未修改现有账号') from None
    except (ValueError, UnicodeError, KeyError, TypeError):
        raise TwoFactorError('备份内容无效，未修改现有账号') from None


def write_file(path, content):
    path = os.path.abspath(path)
    if not path.lower().endswith('.cx2fa'):
        raise TwoFactorError('备份文件必须使用 .cx2fa 扩展名')
    descriptor, temporary = tempfile.mkstemp(prefix='.cx2fa-', suffix='.tmp', dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def read_file(path):
    if not str(path).lower().endswith('.cx2fa') or not os.path.isfile(path):
        raise TwoFactorError('请选择 .cx2fa 备份文件')
    with open(path, 'rb') as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise TwoFactorError('备份文件超过 4 MB，已停止读取')
    return content
