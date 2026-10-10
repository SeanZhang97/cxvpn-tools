# -*- coding: utf-8 -*-
"""排序事务与 schema v4 兼容性；仅使用临时用户库。"""
from contextlib import closing
import sqlite3
import tempfile
import unittest

from core import state_store
from core.two_factor import AccountStore, TwoFactorError
from core.two_factor_api import TwoFactorApi
from tests.test_two_factor import account


class AccountOrderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = self.temporary.name
        self.store = AccountStore(self.root)
        self.ids = [self.store.save(account(account=f'排序用户 é 🇯🇵-{i}'))['id'] for i in range(3)]
        state_store.save_config({'probe': '保留配置 é 🇯🇵'}, self.root)

    def order(self):
        return [row['id'] for row in self.store.records()]

    def test_reorder_persists_without_changing_accounts_codes_or_config(self):
        records = {row['id']: row for row in self.store.records()}
        config = state_store.load_config(self.root)
        revision = state_store.current_revision(self.root)
        desired = self.ids[::-1]
        self.assertTrue(self.store.reorder(desired, self.ids)['ok'])
        self.assertEqual([r['id'] for r in AccountStore(self.root).listing()['accounts']], desired)
        self.assertEqual({r['id']: r for r in self.store.records()}, records)
        self.assertEqual(state_store.load_config(self.root), config)
        self.assertEqual(state_store.current_revision(self.root), revision)

    def test_v4_upgrade_keeps_existing_records_and_original_order(self):
        before = self.store.records()
        with closing(sqlite3.connect(state_store.database_path(self.root))) as conn:
            conn.execute('DROP TABLE otp_account_order')
            conn.execute('PRAGMA user_version=4')
            conn.commit()
        self.assertEqual(self.store.records(), before)
        with state_store._reader(self.root) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], state_store.SCHEMA_VERSION)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM otp_account_order').fetchone()[0], 0)
        self.assertEqual(state_store.load_config(self.root), {'probe': '保留配置 é 🇯🇵'})

    def test_invalid_partial_unknown_and_stale_requests_are_atomic(self):
        reversed_ids = self.ids[::-1]
        self.store.reorder(reversed_ids, self.ids)
        cases = [(None, reversed_ids), ([self.ids[0]] * 3, reversed_ids),
                 (['x', *reversed_ids[1:]], reversed_ids), (reversed_ids[:-1], reversed_ids),
                 (self.ids, self.ids), (reversed_ids, [1, 2, 3]),
                 (reversed_ids, 'not a list')]
        for desired, expected in cases:
            with self.subTest(desired=desired, expected=expected), self.assertRaises(TwoFactorError):
                self.store.reorder(desired, expected)
            self.assertEqual(self.order(), reversed_ids)

    def test_add_edit_import_and_delete_keep_saved_positions(self):
        desired = self.ids[::-1]
        self.store.reorder(desired, self.ids)
        added = self.store.save(account(account='追加账号'))['id']
        row = self.store.records()[1]
        self.store.save({**row, 'secret': '', 'issuer': '修改平台'})
        self.assertEqual(self.order(), desired + [added])
        prepared = self.store.prepare_import([account(account='导入账号')])
        imported = prepared['additions'][0]['id']
        self.store.apply_import(prepared)
        self.assertEqual(self.order(), desired + [added, imported])
        row = self.store.records()[1]
        self.store.delete(row['id'], row['revision'])
        self.assertEqual(self.order(), [id_ for id_ in desired + [added, imported] if id_ != row['id']])
        with state_store._reader(self.root) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM otp_account_order WHERE account_id=?', (row['id'],)).fetchone()[0], 0)

    def test_failed_insert_rolls_back_deleted_order(self):
        desired = self.ids[::-1]
        self.store.reorder(desired, self.ids)
        with state_store.transaction(self.root) as conn:
            conn.execute("CREATE TRIGGER order_failure BEFORE INSERT ON otp_account_order BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(state_store.StoreError):
            self.store.reorder(self.ids, desired)
        self.assertEqual(self.order(), desired)

    def test_import_preview_survives_order_only_change(self):
        prepared = self.store.prepare_import([account(account='预览账号')])
        self.store.reorder(self.ids[::-1], self.ids)
        self.store.apply_import(prepared)
        self.assertEqual(self.order(), self.ids[::-1] + [prepared['additions'][0]['id']])

    def test_bridge_error_and_logging_do_not_expose_account_or_code(self):
        bridge = TwoFactorApi()
        messages = []
        bridge.log = messages.append
        bridge._init_two_factor(self.root)
        result = bridge.reorder_otp_accounts(self.ids[::-1], self.ids)
        self.assertTrue(result['ok'])
        self.assertFalse(bridge.reorder_otp_accounts(self.ids, self.ids)['ok'])
        combined = '\n'.join(messages)
        self.assertIn('保存账号顺序', combined)
        for row in self.store.listing()['accounts']:
            for sensitive in (row['id'], row['account'], row['code']):
                self.assertNotIn(sensitive, combined)


if __name__ == '__main__':
    unittest.main()
