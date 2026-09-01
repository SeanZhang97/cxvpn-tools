# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timezone
from unittest.mock import patch

from core import config, sms_receiver


class SmsReceiverTests(unittest.TestCase):
    def test_extracts_chaoxing_shortcut_mail_code(self):
        text = '【超星学习通】您的验证码为：730507,有效期为5分钟。'
        self.assertEqual(sms_receiver.find_code(text), '730507')

    def test_extracts_plain_text_from_multipart_mail(self):
        message = EmailMessage()
        message['Subject'] = '超星验证码'
        message.set_content('【超星学习通】您的验证码为：730507,有效期为5分钟。')
        message.add_alternative('<p>备用 HTML</p>', subtype='html')
        self.assertIn('730507', sms_receiver.email_text(message))
        self.assertNotIn('备用 HTML', sms_receiver.email_text(message))

    def test_partial_sms_config_keeps_nested_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'config.json')
            with open(path, 'w', encoding='utf-8') as stream:
                json.dump({'sms': {'method': 'email', 'email': {
                    'provider': 'qq', 'username': 'user@qq.com'}}}, stream)
            with patch.object(config, 'CFG_PATH', path):
                loaded = config.load()
        self.assertEqual(loaded['sms']['method'], 'email')
        self.assertEqual(loaded['sms']['email']['host'], '')
        self.assertEqual(loaded['sms']['email']['port'], 993)
        self.assertEqual(loaded['sms']['email']['subject'], '超星验证码')

    @patch('core.sms_receiver.imaplib.IMAP4_SSL')
    def test_imap_connection_test_is_read_only(self, imap_ssl):
        client = imap_ssl.return_value
        client.select.return_value = ('OK', [b'0'])
        settings = {'email': {
            'host': 'imap.qq.com', 'port': 993,
            'username': 'user@qq.com', 'password': 'app-code',
            'mailbox': 'INBOX'}}
        ok, _ = sms_receiver.test_imap(settings)
        self.assertTrue(ok)
        client.login.assert_called_once_with('user@qq.com', 'app-code')
        client.select.assert_called_once_with('INBOX', readonly=True)

    def test_email_catcher_uses_existing_uid_as_baseline(self):
        class Client:
            def __init__(self, messages):
                self.messages = messages

            def uid(self, command, value, *_args):
                if command == 'search':
                    return 'OK', [' '.join(map(str, self.messages)).encode()]
                return 'OK', [(b'RFC822', self.messages[int(value)])]

        catcher = sms_receiver.Catcher(settings={'method': 'email', 'email': {}})
        self.assertTrue(catcher.wait_ready(1))
        catcher._settings = {'email': {'subject': '超星验证码'}}
        first = Client({1: b'old'})
        catcher._scan_email(first, initial=True)
        self.assertEqual(catcher._last_email_uid, 1)

        message = EmailMessage()
        message['Date'] = format_datetime(datetime.now(timezone.utc))
        message['Subject'] = '超星验证码'
        message.set_content('【超星学习通】您的验证码为：730507,有效期为5分钟。')
        second = Client({1: b'old', 2: message.as_bytes()})
        catcher._scan_email(second)
        self.assertEqual(catcher.poll()[0], '730507')


if __name__ == '__main__':
    unittest.main()
