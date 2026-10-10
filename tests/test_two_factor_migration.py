# -*- coding: utf-8 -*-
"""Google 迁移二维码：公开测试密钥、临时主库与真实隔离解码。"""
import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from PIL import Image

from core import state_store, two_factor_qr as qr
from core.two_factor import TwoFactorError, current_code
from core.two_factor_api import TwoFactorApi
from core.two_factor_migration import MigrationBatch, parse_export
from tests.test_two_factor import account, qr_bytes, SECRET


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    if isinstance(value, str):
        value = value.encode('utf-8')
    if isinstance(value, bytes):
        return varint(number * 8 + 2) + varint(len(value)) + value
    return varint(number * 8) + varint(value)


def otp(name='Google:user@example.com', issuer='Google', secret=b'12345678901234567890',
        algorithm=1, digits=1, kind=2):
    return b''.join(field(k, v) for k, v in ((1, secret), (2, name), (3, issuer),
                                           (4, algorithm), (5, digits), (6, kind)))


def migration_uri(records=None, size=1, index=0, batch=123, version=1, extra=b''):
    payload = b''.join(field(1, r) for r in (records if records is not None else [otp()]))
    payload += b''.join(field(k, v) for k, v in ((2, version), (3, size), (4, index), (5, batch))) + extra
    return 'otpauth-migration://offline?data=' + quote(base64.b64encode(payload).decode('ascii'), safe='')


def raw_uri(payload):
    return 'otpauth-migration://offline?data=' + quote(base64.b64encode(payload).decode('ascii'), safe='')


def data_url(uri):
    return 'data:image/png;base64,' + base64.b64encode(qr_bytes(uri)).decode('ascii')


class ParserTests(unittest.TestCase):
    def test_ios_version_two_known_fields_and_optional_extensions(self):
        record = otp('平台 é 🇯🇵:账号', '平台 é 🇯🇵') + field(8, 'synthetic-entry-id')
        parsed = parse_export(migration_uri([record], version=2, size=2, index=1,
                                           extra=field(10, b'optional-extension')))
        self.assertEqual((parsed['size'], parsed['index']), (2, 1))
        self.assertEqual(parsed['skipped'], [])
        value = parsed['accounts'][0]
        self.assertEqual(value['issuer'], '平台 é 🇯🇵')
        self.assertEqual(value['account'], '账号')
        self.assertEqual(value['secret'], SECRET)
        self.assertEqual(current_code(value, 59)['code'], '287082')

    def test_unicode_defaults_algorithms_and_label_rules(self):
        rows = [otp('平台 é 🇯🇵:账号', '平台 é 🇯🇵', algorithm=0, digits=0, kind=0),
                otp('SHA256:two', '', algorithm=2, digits=2),
                otp('label:kept', 'different', algorithm=3), otp('plain', '')]
        parsed = parse_export(migration_uri(rows, extra=field(20, b'future')))
        self.assertEqual(parsed['skipped'], [])
        accounts = parsed['accounts']
        self.assertEqual((accounts[0]['issuer'], accounts[0]['account']), ('平台 é 🇯🇵', '账号'))
        self.assertEqual((accounts[0]['algorithm'], accounts[0]['digits'], accounts[0]['period']), ('SHA1', 6, 30))
        self.assertEqual((accounts[1]['issuer'], accounts[1]['algorithm'], accounts[1]['digits']), ('SHA256', 'SHA256', 8))
        self.assertEqual((accounts[2]['account'], accounts[2]['algorithm']), ('label:kept', 'SHA512'))
        self.assertEqual(accounts[3]['issuer'], '未命名平台')
        self.assertEqual(accounts[0]['secret'], SECRET)

    def test_unescaped_base64_plus_and_missing_padding(self):
        uri = migration_uri([otp(secret=bytes(range(255, 235, -1)))])
        self.assertIn('%2B', uri)
        self.assertEqual(parse_export(uri), parse_export(uri.replace('%2B', '+').replace('%3D', '')))

    def test_unsupported_entries_are_visible_and_not_converted(self):
        result = parse_export(migration_uri([otp(kind=1), otp(algorithm=4), otp(digits=3), otp(secret=b'')]))
        self.assertEqual(result['accounts'], [])
        self.assertEqual(len(result['skipped']), 4)
        self.assertIn('HOTP', result['skipped'][0]['reason'])
        self.assertNotIn(SECRET, json.dumps(result['skipped']))

    def test_malformed_inputs_and_bounds(self):
        valid = migration_uri()
        invalid = ['otpauth://totp/a', valid + '&data=duplicate', valid + '#fragment',
                   'otpauth-migration://offline?data=invalid!', migration_uri(version=3),
                   migration_uri(version=0),
                   migration_uri(size=0), migration_uri(size=101), migration_uri(size=2, index=2),
                   migration_uri(records=[]), migration_uri(extra=field(2, 1)),
                   migration_uri([otp() + field(4, b'wrong-wire')]),
                   raw_uri(b'\x00'), raw_uri(b'\x0a\x80'), raw_uri(b'\x0a\x05short\x10'),
                   raw_uri(b'\x0b'), raw_uri(b'\x80' * 11),
                   migration_uri([otp(name='bad\nname')]),
                   migration_uri([field(1, b'12345678901234567890') + field(2, b'\xff')]),
                   'otpauth-migration://offline?data=' + 'a' * 100000]
        for uri in invalid:
            with self.subTest(case=invalid.index(uri)), self.assertRaises(TwoFactorError) as caught:
                parse_export(uri)
            self.assertNotIn(SECRET, str(caught.exception))
            self.assertNotIn('data=', str(caught.exception))

    def test_batch_order_duplicate_missing_and_encrypted_staging(self):
        pages = [migration_uri([otp(f'Google:{i}')], size=3, index=i) for i in range(3)]
        batch = MigrationBatch()
        batch.add([pages[2], pages[0], pages[0]])
        self.assertEqual(batch.status()['missing'], [2])
        self.assertEqual(batch.status()['received'], 2)
        self.assertNotIn(SECRET, repr(batch.pages))
        self.assertNotIn(SECRET, json.dumps(batch.status()))
        batch.add([pages[1]])
        self.assertEqual(batch.status()['missing'], [])
        self.assertEqual([r['account'] for r in batch.accounts()], ['0', '1', '2'])
        with self.assertRaises(TwoFactorError):
            batch.add([migration_uri(size=3, index=1, batch=999)])
        with self.assertRaises(TwoFactorError):
            batch.add([migration_uri([otp('Google:changed')], size=3, index=1)])


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = TwoFactorApi()
        self.logs = []
        self.api.log = self.logs.append
        self.api._init_two_factor(self.temp.name)

    def collect(self, *uris):
        # API 状态/事务测试绕过识别子进程，独立测试覆盖真实识别。
        with patch.object(qr, 'decode_isolated', side_effect=lambda content, **kw: [content.decode('utf-8')]):
            return self.api._otp_collect_google([uri.encode('utf-8') for uri in uris])

    def test_no_save_until_complete_and_confirm_then_single_use(self):
        first = self.collect(migration_uri(size=2, index=1))
        self.assertFalse(first['pending'])
        self.assertEqual(first['batch']['missing'], [1])
        self.assertEqual(self.api._otp_store.records(), [])
        self.assertFalse(self.api.confirm_otp_google_import('invalid')['ok'])
        preview = self.collect(migration_uri([otp('Cloudflare:admin', 'Cloudflare')], size=2, index=0))
        self.assertTrue(preview['pending'])
        self.assertEqual(len(preview['summary']['additions']), 2)
        self.assertEqual(self.api._otp_store.records(), [])
        self.assertNotIn(SECRET, repr(self.api._otp_pending))
        self.assertTrue(self.api.confirm_otp_google_import(preview['token'])['ok'])
        self.assertFalse(self.api.confirm_otp_google_import(preview['token'])['ok'])
        self.assertEqual(len(self.api._otp_store.records()), 2)
        self.assertEqual(self.api._otp_store.export_records()[0]['secret'], SECRET)
        self.assertNotIn(SECRET, '\n'.join(self.logs))

    def test_ios_second_page_first_requires_first_page_before_confirmation(self):
        second = self.collect(migration_uri([otp('Second:user', 'Second')], version=2, size=2, index=1))
        self.assertFalse(second['pending'])
        self.assertEqual(second['batch']['missing'], [1])
        self.assertEqual(self.api._otp_store.records(), [])
        complete = self.collect(migration_uri(version=2, size=2, index=0))
        self.assertTrue(complete['pending'])
        self.assertEqual(complete['batch']['missing'], [])
        self.assertEqual(len(complete['summary']['additions']), 2)
        self.assertEqual(self.api._otp_store.records(), [])
        self.assertTrue(self.api.confirm_otp_google_import(complete['token'])['ok'])
        self.assertEqual(len(self.api._otp_store.records()), 2)

    def test_duplicate_conflict_unsupported_and_unreadable_do_not_overwrite(self):
        self.api._otp_store.save(account(issuer='Google', account='user@example.com'))
        broken = self.api._otp_store.save(account(issuer='Unreadable', account='user'))['id']
        with state_store.transaction(self.temp.name) as conn:
            conn.execute('UPDATE otp_accounts SET secret=? WHERE id=?', (b'corrupt', broken))
        rows = [otp(), otp(secret=b'99999999999999999999'), otp('Unreadable:user', 'Unreadable'),
                otp('New:user', 'New'), otp('HOTP:user', 'HOTP', kind=1)]
        preview = self.collect(migration_uri(rows))
        self.assertEqual(preview['summary']['duplicates'], 1)
        self.assertEqual(len(preview['summary']['conflicts']), 2)
        self.assertEqual(preview['summary']['replacements'], [])
        self.assertEqual(len(preview['batch']['skipped']), 1)
        self.assertTrue(self.api.confirm_otp_google_import(preview['token'])['ok'])
        self.assertEqual(len(self.api._otp_store.records()), 3)
        with self.assertRaises(TwoFactorError):
            self.api._otp_store.code(broken)

    def test_invalid_add_is_atomic_and_cancelled_selection_preserves_preview(self):
        preview = self.collect(migration_uri(size=2))
        previous = self.api._otp_google
        with self.assertRaises(TwoFactorError):
            self.collect(migration_uri(index=1, size=2), migration_uri(batch=999))
        self.assertIs(self.api._otp_google, previous)
        self.assertEqual(previous.status(), preview['batch'])
        with patch.object(self.api, '_otp_choose', return_value=None):
            self.assertTrue(self.api.preview_otp_google_images()['cancelled'])
        self.assertIs(self.api._otp_google, previous)
        self.assertTrue(self.api.cancel_otp_restore()['ok'])
        self.assertIsNone(self.api._otp_google)

    def test_expired_preview_and_concurrent_change_reject_write(self):
        preview = self.collect(migration_uri())
        with patch('core.two_factor_api.time.monotonic', return_value=self.api._otp_pending[1] + 1):
            self.assertFalse(self.api.confirm_otp_google_import(preview['token'])['ok'])
        self.assertEqual(self.api._otp_store.records(), [])
        self.api.cancel_otp_restore()
        self.collect(migration_uri(size=2))
        self.api._otp_google.deadline = 0
        with self.assertRaises(TwoFactorError):
            self.collect(migration_uri(size=2, index=1))
        self.assertIsNone(self.api._otp_google)
        preview = self.collect(migration_uri())
        self.api._otp_store.save(account(issuer='Concurrent'))
        self.assertFalse(self.api.confirm_otp_google_import(preview['token'])['ok'])
        self.assertEqual(len(self.api._otp_store.records()), 1)

    def test_wrong_transfer_type_and_busy_lock(self):
        preview = self.collect(migration_uri())
        self.assertFalse(self.api.restore_otp_backup(preview['token'])['ok'])
        self.assertEqual(self.api._otp_store.records(), [])
        self.api._otp_transfer_lock.acquire()
        try:
            self.assertFalse(self.api.preview_otp_google_image(data_url(migration_uri()))['ok'])
        finally:
            self.api._otp_transfer_lock.release()

    def test_real_native_multiselect_and_clipboard_unicode_pipeline(self):
        uris = [migration_uri([otp('平台 é 🇯🇵:账号', '平台 é 🇯🇵')], size=2, index=0),
                migration_uri([otp('Second:second', 'Second')], size=2, index=1)]
        paths = []
        for index, uri in enumerate(uris):
            path = Path(self.temp.name) / f'二维码-{index}.png'
            path.write_bytes(qr_bytes(uri))
            paths.append(str(path))
        with patch.object(self.api, '_otp_choose', return_value=paths) as choose:
            preview = self.api.preview_otp_google_images()
        choose.assert_called_once_with('二维码图片 (*.png;*.jpg;*.jpeg;*.webp)', multiple=True)
        self.assertTrue(preview['pending'], preview.get('msg'))
        self.assertEqual(preview['summary']['additions'][0]['issuer'], '平台 é 🇯🇵')
        self.api.cancel_otp_restore()
        first = self.api.preview_otp_google_image(data_url(uris[0]))
        self.assertEqual(first['batch']['missing'], [2])
        second = self.api.preview_otp_google_image(data_url(uris[1]))
        self.assertTrue(second['pending'], second.get('msg'))
        self.assertNotIn(SECRET, json.dumps(second))
        self.assertNotIn(uris[0], '\n'.join(self.logs))
        self.assertTrue(self.api.confirm_otp_google_import(second['token'])['ok'])

    def test_editor_google_file_and_clipboard_reuse_batch_without_secret_response(self):
        second_uri = migration_uri(version=2, size=2, index=1)
        image = Path(self.temp.name) / 'google-export.png'
        image.write_bytes(qr_bytes(second_uri))
        with patch.object(self.api, '_otp_choose', return_value=str(image)):
            second = self.api.import_otp_qr()
        self.assertTrue(second['ok'], second.get('msg'))
        self.assertEqual(second['kind'], 'google')
        self.assertEqual(second['batch']['missing'], [1])
        self.assertNotIn('account', second)
        self.assertNotIn(SECRET, json.dumps(second))
        self.assertNotIn(second_uri, json.dumps(second))
        self.assertEqual(self.api._otp_store.records(), [])
        complete = self.api.preview_otp_google_image(data_url(migration_uri(
            [otp('Other:user', 'Other')], version=2, size=2)))
        self.assertTrue(complete['pending'], complete.get('msg'))
        self.assertEqual(len(complete['summary']['additions']), 2)
        self.api.cancel_otp_restore()
        pasted = self.api.decode_otp_image(data_url(migration_uri(version=2)))
        self.assertEqual(pasted['kind'], 'google')
        self.assertTrue(pasted['pending'])
        self.assertNotIn(SECRET, json.dumps(pasted))
        self.assertTrue(self.api.confirm_otp_google_import(pasted['token'])['ok'])
        self.assertEqual(len(self.api._otp_store.records()), 1)
        self.assertNotIn(SECRET, '\n'.join(self.logs))

    def test_editor_standard_qr_remains_a_form_value_and_errors_preserve_batch(self):
        uri = f'otpauth://totp/Standard:public?secret={SECRET}'
        result = self.api.decode_otp_image(data_url(uri))
        self.assertEqual(result['kind'], 'totp')
        self.assertEqual(result['account']['secret'], SECRET)
        self.assertEqual(result['account']['issuer'], 'Standard')
        self.assertIsNone(self.api._otp_google)
        self.assertEqual(self.api._otp_store.records(), [])
        previous = self.collect(migration_uri(size=2))
        batch = self.api._otp_google
        rejected = self.api.decode_otp_image(data_url(migration_uri(version=3)))
        self.assertFalse(rejected['ok'])
        self.assertIs(self.api._otp_google, batch)
        self.assertEqual(self.api._otp_google.status(), previous['batch'])
        with patch.object(self.api, '_otp_choose', return_value=None):
            self.assertTrue(self.api.import_otp_qr()['cancelled'])
        self.assertIs(self.api._otp_google, batch)
        self.assertFalse(self.api.decode_otp_image(data_url('https://example.com'))['ok'])


class DecoderTests(unittest.TestCase):
    def test_auto_decoder_distinguishes_formats_and_rejects_non_otp(self):
        standard = f'otpauth://totp/Standard:public?secret={SECRET}'
        self.assertEqual(qr.decode_isolated(qr_bytes(standard), auto=True), {'kind': 'totp', 'uri': standard})
        google = migration_uri(version=2)
        self.assertEqual(qr.decode_isolated(qr_bytes(google), auto=True), {'kind': 'google', 'uris': [google]})
        with self.assertRaises(TwoFactorError):
            qr.decode_isolated(qr_bytes('https://example.com'), auto=True)

    def test_multiple_codes_in_one_image_and_single_editor_guidance(self):
        uris = [migration_uri(size=2, index=i) for i in range(2)]
        pictures = [Image.open(io.BytesIO(qr_bytes(uri))) for uri in uris]
        try:
            canvas = Image.new('L', (sum(p.width for p in pictures) + 60, max(p.height for p in pictures) + 40), 255)
            offset = 20
            for picture in pictures:
                canvas.paste(picture, (offset, 20))
                offset += picture.width + 20
            stream = io.BytesIO()
            canvas.save(stream, format='PNG')
            self.assertEqual(set(qr.decode_isolated(stream.getvalue(), migration=True)), set(uris))
            detected = qr.decode_isolated(stream.getvalue(), auto=True)
            self.assertEqual(detected['kind'], 'google')
            self.assertEqual(set(detected['uris']), set(uris))
        finally:
            for picture in pictures:
                picture.close()
        with self.assertRaisesRegex(TwoFactorError, 'Google 导入'):
            qr.decode_image(qr_bytes(uris[0]))
        with self.assertRaises(TwoFactorError):
            qr.decode_isolated(qr_bytes('https://example.com'), migration=True)


if __name__ == '__main__':
    unittest.main()
