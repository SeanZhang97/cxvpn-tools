# -*- coding: utf-8 -*-
"""隔离临时主库、真实 Windows DPAPI 与离线二维码/备份回归。"""
import base64
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from PIL import Image
import zxingcpp

from core import state_store, two_factor_backup as backup, two_factor_qr as qr
from core.two_factor import AccountStore, TwoFactorError, current_code, normalize, _dpapi
from core.two_factor_api import TwoFactorApi

SECRET = base64.b32encode(b'12345678901234567890').decode('ascii')
PASSWORD = '测试备份密码-e\u0301-\U0001f1ef\U0001f1f5'


def account(**changes):
    return {'issuer': '平台 e\u0301 \U0001f1ef\U0001f1f5', 'account': '用户@example.com',
            'secret': SECRET, 'algorithm': 'SHA1', 'digits': 6, 'period': 30, **changes}


def qr_bytes(value):
    pixels = memoryview(zxingcpp.write_barcode_to_image(
        zxingcpp.create_barcode(value, zxingcpp.BarcodeFormat.QRCode), scale=5))
    picture = Image.frombytes('L', (pixels.shape[1], pixels.shape[0]), pixels.tobytes())
    stream = io.BytesIO()
    picture.save(stream, format='PNG')
    return stream.getvalue()


class AlgorithmTests(unittest.TestCase):
    def test_rfc6238_all_algorithms_and_large_time(self):
        expected = [(59, '94287082', '46119246', '90693936'),
                    (1111111109, '07081804', '68084774', '25091201'),
                    (1111111111, '14050471', '67062674', '99943326'),
                    (1234567890, '89005924', '91819424', '93441116'),
                    (2000000000, '69279037', '90698825', '38618901'),
                    (20000000000, '65353130', '77737706', '47863826')]
        for index, (algorithm, size) in enumerate((('SHA1', 20), ('SHA256', 32), ('SHA512', 64)), 1):
            raw = (b'1234567890' * 7)[:size]
            value = normalize(account(algorithm=algorithm, digits=8, secret=base64.b32encode(raw).decode()))
            for vector in expected:
                with self.subTest(algorithm=algorithm, time=vector[0]):
                    self.assertEqual(current_code(value, vector[0])['code'], vector[index])

    def test_boundary_and_leading_zero(self):
        value = normalize(account())
        self.assertNotEqual(current_code(value, 29)['code'], current_code(value, 30)['code'])
        self.assertEqual(current_code(value, 1111111109)['code'], '081804')
        self.assertEqual(current_code(value, 30)['expires_at'], 60)

    def test_uri_unicode_and_parameters(self):
        uri = f'otpauth://totp/{quote("平台 é 🇯🇵:账号")}?secret={SECRET}&issuer={quote("平台 é 🇯🇵")}&digits=8&algorithm=SHA256&period=60'
        result = normalize({'secret': uri})
        self.assertEqual(result['issuer'], '平台 é 🇯🇵')
        self.assertEqual(result['digits'], 8)
        self.assertEqual(result['period'], 60)
        self.assertEqual(result['account'], '账号')
        self.assertEqual(result['algorithm'], 'SHA256')

    def test_invalid_inputs_do_not_echo_secret(self):
        for changes in ({'secret': 'bad-secret-do-not-echo'}, {'digits': 7}, {'period': 0},
                        {'period': 30.5}, {'algorithm': 'MD5'}, {'issuer': ''}, {'period': True}):
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(TwoFactorError) as caught:
                    normalize(account(**changes))
                self.assertNotIn('bad-secret-do-not-echo', str(caught.exception))
        for uri in (f'otpauth://hotp/GitHub:a?secret={SECRET}',
                    f'otpauth://totp/GitHub:a?secret={SECRET}&secret={SECRET}',
                    f'otpauth://totp/GitHub:a?secret={SECRET}&issuer=Google'):
            with self.assertRaises(TwoFactorError):
                normalize({'secret': uri})


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AccountStore(self.temp.name)

    def test_dpapi_unicode_and_record_binding(self):
        raw = '组合 é 与非 BMP 🇯🇵'.encode('utf-8')
        encrypted = _dpapi(raw, 'record-a')
        self.assertNotIn(raw, encrypted)
        self.assertEqual(_dpapi(encrypted, 'record-a', True), raw)
        with self.assertRaises(TwoFactorError):
            _dpapi(encrypted, 'record-b', True)

    def test_create_edit_delete_and_config_independence(self):
        original = {'phone': 'test-only', 'routing': {'enabled': False}}
        state_store.save_config(original, self.temp.name)
        revision = state_store.current_revision(self.temp.name)
        added = self.store.save(account())
        shown = self.store.listing()['accounts'][0]
        self.assertNotIn('secret', shown)
        self.assertEqual(len(shown['code']), 6)
        self.assertEqual(shown['issuer'], account()['issuer'])
        self.assertEqual(self.store.code(added['id'])['code'], shown['code'])
        self.store.save({**shown, 'issuer': '更新后', 'secret': ''})
        self.assertEqual(self.store.export_records()[0]['secret'], SECRET)
        with self.assertRaises(TwoFactorError):
            self.store.save({**shown, 'secret': ''})
        updated = self.store.listing()['accounts'][0]
        self.store.delete(updated['id'], updated['revision'])
        self.assertEqual(self.store.listing()['accounts'], [])
        self.assertEqual(state_store.load_config(self.temp.name), original)
        self.assertEqual(state_store.current_revision(self.temp.name), revision)
        self.assertFalse((Path(self.temp.name) / 'config.json').exists())

    def test_unique_account_and_encrypted_database(self):
        self.store.save(account())
        with self.assertRaises(TwoFactorError):
            self.store.save(account())
        self.assertEqual(len(self.store.records()), 1)
        for path in Path(self.temp.name).glob('state.sqlite3*'):
            self.assertNotIn(SECRET.encode(), path.read_bytes())

    def test_corruption_is_local_and_replaceable(self):
        identifier = self.store.save(account())['id']
        self.store.save(account(issuer='另一平台'))
        with state_store.transaction(self.temp.name) as conn:
            conn.execute('UPDATE otp_accounts SET secret=? WHERE id=?', (b'corrupt', identifier))
        rows = self.store.listing()['accounts']
        broken = next(row for row in rows if row['id'] == identifier)
        self.assertEqual(broken['code'], '')
        self.assertIn('error', broken)
        self.assertTrue(next(row for row in rows if row['id'] != identifier)['code'])
        self.store.save({**broken, 'secret': SECRET})
        self.assertTrue(self.store.code(identifier)['code'])

    def test_import_duplicate_conflict_and_atomic_revision(self):
        self.store.save(account())
        incoming = [account(), account(secret='JBSWY3DPEHPK3PXP'), account(issuer='新平台'), account(issuer='新平台')]
        prepared = self.store.prepare_import(incoming)
        self.assertEqual(prepared['duplicates'], 2)
        self.assertEqual(len(prepared['conflicts']), 1)
        self.assertEqual(len(prepared['additions']), 1)
        self.store.save(account(issuer='并发添加'))
        with self.assertRaises(TwoFactorError):
            self.store.apply_import(prepared)
        self.assertEqual(len(self.store.records()), 2)
        prepared = self.store.prepare_import(incoming)
        self.store.apply_import(prepared)
        self.assertEqual(len(self.store.records()), 3)
        self.assertEqual(self.store.export_records()[0]['secret'], SECRET)

    def test_import_failure_rolls_back_all_rows(self):
        prepared = self.store.prepare_import([account(issuer='A'), account(issuer='B')])
        prepared['additions'][1]['id'] = prepared['additions'][0]['id']
        with self.assertRaises(state_store.StoreError):
            self.store.apply_import(prepared)
        self.assertEqual(self.store.records(), [])

    def test_backup_restores_unreadable_keys_without_overwriting_readable_keys(self):
        identifier = self.store.save(account())['id']
        with state_store.transaction(self.temp.name) as conn:
            conn.execute('UPDATE otp_accounts SET secret=? WHERE id=?', (b'unreadable', identifier))
        prepared = self.store.prepare_import([account(), account()])
        self.assertEqual(len(prepared['replacements']), 1)
        self.assertEqual(prepared['duplicates'], 1)
        self.store.apply_import(prepared)
        self.assertEqual(self.store.export_records()[0]['secret'], SECRET)
        self.assertEqual(self.store.records()[0]['revision'], 2)
        self.assertTrue(self.store.code(identifier)['code'])

    def test_upgrade_v3_preserves_existing_config(self):
        state_store.save_config({'existing': '保留'}, self.temp.name)
        with closing(sqlite3.connect(state_store.database_path(self.temp.name))) as conn:
            conn.execute('DROP TABLE otp_accounts')
            conn.execute('PRAGMA user_version=3')
        self.assertEqual(self.store.records(), [])
        self.assertEqual(state_store.load_config(self.temp.name), {'existing': '保留'})
        with closing(sqlite3.connect(state_store.database_path(self.temp.name))) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 4)


class BackupTests(unittest.TestCase):
    def test_portable_encrypted_roundtrip_and_tamper(self):
        content = backup.encrypt([normalize(account())], PASSWORD)
        self.assertNotIn(SECRET.encode(), content)
        self.assertNotIn('用户'.encode(), content)
        self.assertEqual(backup.decrypt(content, PASSWORD), [normalize(account())])
        for raw, password in ((content, 'a-wrong-password'), (content[:-1] + bytes([content[-1] ^ 1]), PASSWORD)):
            with self.assertRaises(TwoFactorError):
                backup.decrypt(raw, password)
        with tempfile.TemporaryDirectory() as other:
            store = AccountStore(other)
            store.apply_import(store.prepare_import(backup.decrypt(content, PASSWORD)))
            self.assertEqual(store.export_records(), [normalize(account())])

    def test_atomic_file_and_bounds(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / '备份 é 🇯🇵.cx2fa'
            target.write_bytes(b'previous')
            with patch('core.two_factor_backup.os.replace', side_effect=OSError('failure')):
                with self.assertRaises(OSError):
                    backup.write_file(target, b'new')
            self.assertEqual(target.read_bytes(), b'previous')
            self.assertEqual(list(Path(root).iterdir()), [target])
            backup.write_file(target, b'new')
            self.assertEqual(backup.read_file(target), b'new')
        for content in (b'bad', b'a' * (backup.MAX_BYTES + 1)):
            with self.assertRaises(TwoFactorError):
                backup.decrypt(content, PASSWORD)


class QRAndApiTests(unittest.TestCase):
    def test_qr_isolated_unicode_and_invalid(self):
        uri = f'otpauth://totp/{quote("平台 é 🇯🇵:账号")}?secret={SECRET}'
        content = qr_bytes(uri)
        self.assertEqual(qr.decode_isolated(content), uri)
        with self.assertRaises(TwoFactorError):
            qr.decode_image(qr_bytes('https://example.com'))
        with self.assertRaises(TwoFactorError):
            qr.decode_image(b'broken image')
        with patch('core.two_factor_qr.subprocess.run', side_effect=__import__('subprocess').TimeoutExpired('decoder', 10)):
            with self.assertRaises(TwoFactorError):
                qr.decode_isolated(content)

    def test_api_logs_and_preview_do_not_return_secrets(self):
        with tempfile.TemporaryDirectory() as root:
            api = TwoFactorApi()
            messages = []
            api.log = messages.append
            api._init_two_factor(root)
            self.assertTrue(api.save_otp_account(account())['ok'])
            target = str(Path(root) / 'test.cx2fa')
            api._otp_choose = lambda *args: target
            self.assertTrue(api.export_otp_backup(PASSWORD)['ok'])
            preview = api.preview_otp_restore(PASSWORD)
            self.assertTrue(preview['pending'])
            self.assertNotIn(SECRET, json.dumps(preview))
            self.assertNotIn(SECRET, '\n'.join(messages))
            self.assertNotIn(PASSWORD, '\n'.join(messages))
            self.assertTrue(api.restore_otp_backup(preview['token'])['ok'])
            self.assertFalse(api.restore_otp_backup(preview['token'])['ok'])
            api._otp_choose = lambda *args: None
            self.assertTrue(api.export_otp_backup(PASSWORD)['cancelled'])


if __name__ == '__main__':
    unittest.main()
