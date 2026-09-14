# -*- coding: utf-8 -*-
"""隔离强杀与业务事务回归；可选 --exe 校验 PyInstaller 产物。"""
from contextlib import closing
import copy
import gc
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import state_store, config, subscription_store
from core.routing_tasks import operation_scope, commit_scope

EXE = None
if '--exe' in sys.argv:
    index = sys.argv.index('--exe')
    EXE = sys.argv[index + 1]
    del sys.argv[index:index + 2]

NAME = '节点 e\u0301 🇯🇵'
PROVIDER = {'id': 'alpha', 'name': '订阅 e\u0301 🇯🇵', 'url': 'https://example.test/sub'}
NODES = [{'name': NAME, 'display_name': NAME, 'type': 'ss', 'delay': 20,
          'alive': True, 'tested': True, 'tested_at': 2}]


class StoreRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cxvpn-recovery-')
        self.root = self.temp.name
        self.addCleanup(self.temp.cleanup)

    def test_schema_upgrade_retains_old_payload(self):
        path = state_store.database_path(self.root)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('CREATE TABLE kv_config(id INTEGER PRIMARY KEY,revision INTEGER,payload TEXT,updated_at INTEGER)')
            conn.execute('CREATE TABLE node_snapshots(fingerprint TEXT PRIMARY KEY,payload TEXT,revision INTEGER,updated_at INTEGER)')
            conn.execute('INSERT INTO kv_config VALUES(1,8,?,1)', (json.dumps({'probe': NAME}),))
            conn.execute('INSERT INTO node_snapshots VALUES(?,?,1,1)', ('fp', json.dumps({'nodes': NODES})))
            conn.commit()
        self.assertEqual(state_store.load_config(self.root), {'probe': NAME})
        self.assertEqual(state_store.load_snapshot('fp', self.root)['nodes'], NODES)
        self.assertEqual(state_store.current_revision(self.root), 8)

    def test_stale_revision_cannot_write(self):
        state_store.save_config({'version': 1}, self.root)
        old_revision = state_store.current_revision(self.root)
        state_store.save_config({'version': 2}, self.root)
        with self.assertRaises(state_store.StaleWriteError):
            state_store.save_snapshot('fp', {'nodes': NODES}, self.root, expected_revision=old_revision)
        self.assertIsNone(state_store.load_snapshot('fp', self.root))

    def test_nested_failure_is_rollback_only(self):
        state_store.save_config({'version': 1}, self.root)
        with self.assertRaises(state_store.StoreError):
            with state_store.transaction(self.root):
                state_store.save_config({'version': 2}, self.root)
                try:
                    with state_store.transaction(self.root):
                        raise ValueError('simulated')
                except ValueError:
                    pass
        self.assertEqual(state_store.load_config(self.root), {'version': 1})

    def test_mirror_failure_does_not_revert_database(self):
        with state_store.transaction(self.root):
            state_store.save_config({'version': 2}, self.root)
            state_store.after_commit(lambda: (_ for _ in ()).throw(OSError('locked')))
        self.assertEqual(state_store.load_config(self.root), {'version': 2})

    def test_delay_not_erased_by_unmeasured_or_older_snapshot(self):
        state_store.save_snapshot('fp', {'nodes': NODES}, self.root)
        for patch_node in ({'tested': False, 'tested_at': 0, 'delay': 0},
                           {'tested': True, 'tested_at': 1, 'delay': 80}):
            state_store.save_snapshot('fp', {'nodes': [{**NODES[0], **patch_node}]}, self.root)
            self.assertEqual(state_store.load_snapshot('fp', self.root)['nodes'][0]['delay'], 20)

    def test_kill_at_each_commit_boundary(self):
        for stage in ('after-config', 'after-cache', 'before-commit', 'after-commit'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory(prefix='cxvpn-recovery-') as root:
                with state_store.transaction(root):
                    state_store.save_config({'probe_version': 1}, root)
                    state_store.save_cache('cache', {'id': 'probe'}, b'old-body', {'node_count': 1}, 'probe:fp', root)
                    state_store.save_snapshot('probe:fp', {'nodes': [{**NODES[0], 'delay': 80, 'tested_at': 1}]}, root)
                Path(root, 'probe-allowed').write_text('isolated test', encoding='utf-8')
                if EXE:
                    command = [str(Path(EXE).resolve()), '--storage-recovery-probe', root, stage]
                else:
                    command = [sys.executable, '-c',
                               'import sys; from core.storage_probe import run; run(sys.argv[1],sys.argv[2])',
                               root, stage]
                process = subprocess.Popen(command, cwd=str(Path(__file__).resolve().parents[1]),
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    deadline = time.monotonic() + 12
                    while not Path(root, 'ready').exists() and time.monotonic() < deadline:
                        if process.poll() is not None:
                            self.fail('probe exited before commit barrier')
                        time.sleep(.02)
                    self.assertTrue(Path(root, 'ready').exists(), 'commit barrier timeout')
                    process.kill()
                    process.wait(timeout=5)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                # Windows 终止进程后，父进程持有的 HANDLE 会延长映射文件寿命，
                # SQLite 恢复截断 shm 可能返回 IOERR_TRUNCATE；先释放测试进程句柄。
                del process
                gc.collect()
                committed = stage == 'after-commit'
                self.assertEqual(state_store.load_config(root)['probe_version'], 2 if committed else 1)
                self.assertEqual(state_store.load_cache('cache', root)['body'], b'new-body' if committed else b'old-body')
                self.assertEqual(state_store.load_snapshot('probe:fp', root)['nodes'][0]['delay'], 20 if committed else 80)
                with closing(sqlite3.connect(state_store.database_path(root))) as conn:
                    self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    batches = [conn.execute(sql).fetchone()[0] for sql in (
                        'SELECT batch_id FROM kv_config', 'SELECT batch_id FROM provider_cache',
                        'SELECT batch_id FROM node_snapshots')]
                    self.assertEqual(len(set(batches)), 1)


class ProviderTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cxvpn-data-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.stack = __import__('contextlib').ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('core.app_paths.user_data_root', return_value=self.root))
        self.stack.enter_context(patch.object(config, 'CFG_PATH', os.path.join(self.root, 'config.json')))
        self.stack.enter_context(patch.object(config, 'CFG_BACKUP_PATH', os.path.join(self.root, 'config.json.bak')))
        self.stack.enter_context(patch.object(
            config, 'CFG_HISTORY_DIR', os.path.join(self.root, 'config-history')))
        self.cfg = {'routing': {'proxy_providers': [PROVIDER], 'enabled': False}}

    def bundle(self, body=b'proxies: []'):
        return subscription_store.persist_bytes_and_nodes(PROVIDER, body, 1, 'offline', NODES)

    def test_config_bundle_failure_rolls_back_everything(self):
        config.save(self.cfg)
        self.bundle()
        old_revision = state_store.current_revision()
        with self.assertRaises(OSError):
            with state_store.transaction():
                config.save({**self.cfg, 'phone': 'dummy'})
                self.bundle(b'new')
                raise OSError('simulated disk failure')
        self.assertNotIn('phone', state_store.load_config())
        self.assertEqual(state_store.current_revision(), old_revision)
        self.assertEqual(state_store.load_cache(subscription_store.provider_filename(PROVIDER))['body'], b'proxies: []')

    def test_config_export_keeps_recoverable_previous_versions(self):
        config.save({**self.cfg, 'phone': '13800000000'})
        config.save({**self.cfg, 'phone': ''})

        snapshots = list(Path(config.CFG_HISTORY_DIR).glob('config-*.json'))
        self.assertEqual(len(snapshots), 1)
        restored = json.loads(snapshots[0].read_text(encoding='utf-8'))
        self.assertEqual(restored['phone'], '13800000000')

    def test_unified_batch_and_restore_without_files_or_network(self):
        with state_store.transaction():
            config.save(self.cfg)
            self.bundle()
        # 文件只用于导出，丢失/损坏不改变主存储。
        Path(config.CFG_PATH).write_text('{}', encoding='utf-8')
        Path(subscription_store.node_snapshot_path(PROVIDER)).write_text('{}', encoding='utf-8')
        Path(subscription_store.cache_path(PROVIDER)).write_bytes(b'bad')
        self.assertEqual(config.load()['routing']['proxy_providers'][0]['id'], 'alpha')
        self.assertEqual(subscription_store.load_node_snapshot(PROVIDER)['nodes'], NODES)
        target = str(Path(self.root, 'stage', 'provider.yaml'))
        self.assertTrue(subscription_store.stage_cache(PROVIDER, target))
        self.assertEqual(Path(target).read_bytes(), b'proxies: []')
        with closing(sqlite3.connect(state_store.database_path())) as conn:
            batches = [conn.execute(sql).fetchone()[0] for sql in (
                'SELECT batch_id FROM kv_config', 'SELECT batch_id FROM provider_cache',
                'SELECT batch_id FROM node_snapshots', 'SELECT batch_id FROM config_providers')]
            self.assertEqual(len(set(batches)), 1)

    def test_same_url_different_provider_id_is_isolated(self):
        config.save(self.cfg)
        self.bundle()
        other = {**PROVIDER, 'id': 'beta'}
        subscription_store.persist_node_snapshot(other, [{**NODES[0], 'delay': 90}])
        self.assertEqual(subscription_store.load_node_snapshot(PROVIDER)['nodes'][0]['delay'], 20)
        self.assertEqual(subscription_store.load_node_snapshot(other)['nodes'][0]['delay'], 90)

    def test_operation_guard_rejects_old_task(self):
        from contextlib import nullcontext
        config.save(self.cfg)
        with operation_scope(nullcontext):
            config.save({**self.cfg, 'version': 2})
            with self.assertRaises(state_store.StaleWriteError):
                with commit_scope():
                    self.bundle()
        self.assertIsNone(state_store.load_cache(subscription_store.provider_filename(PROVIDER)))

    def test_update_path_reads_latest_sqlite_snapshot(self):
        config.save(self.cfg)
        self.bundle()
        subscription_store.persist_bytes_and_nodes(PROVIDER, b'new', 1, 'offline',
            [{**NODES[0], 'name': '新节点', 'display_name': '新节点', 'tested_at': 3}])
        self.assertEqual(subscription_store.load_node_snapshot(PROVIDER)['nodes'][0]['name'], '新节点')

    def test_bootstrap_restores_without_live_core(self):
        from core import routing
        config.save(self.cfg)
        self.bundle()
        manager = routing.RoutingManager()
        with patch.object(manager, 'status', return_value={'running': False}), \
                patch('core.routing.windows_system_proxy', return_value=''):
            result = manager.bootstrap(config.load()['routing'])
        self.assertEqual(result['provider_nodes']['alpha']['nodes'], NODES)
        self.assertTrue(result['provider_caches']['alpha']['available'])

    def test_existing_config_migration_binds_links_without_changing_revision(self):
        state_store.save_config(self.cfg)
        revision = state_store.current_revision()
        self.bundle()
        config.load()
        self.assertEqual(state_store.current_revision(), revision)
        with closing(sqlite3.connect(state_store.database_path())) as conn:
            self.assertEqual(conn.execute('SELECT provider_id FROM config_providers').fetchall(), [('alpha',)])
            batches = [conn.execute(sql).fetchone()[0] for sql in (
                'SELECT batch_id FROM kv_config', 'SELECT batch_id FROM provider_cache',
                'SELECT batch_id FROM node_snapshots', 'SELECT batch_id FROM config_providers')]
            self.assertEqual(len(set(batches)), 1)

    def test_speed_results_commit_in_guard_owner_thread(self):
        import threading
        from contextlib import nullcontext
        from core.routing_speedtest import test_nodes
        config.save(self.cfg)
        owner = threading.get_ident()
        completed = []

        def progress(event):
            if event['event'] == 'result':
                self.assertEqual(threading.get_ident(), owner)
                with commit_scope():
                    subscription_store.persist_node_snapshot(PROVIDER, [event['node']])
                completed.append(event['node'])

        with operation_scope(nullcontext):
            test_nodes(lambda *args, **kwargs: {'delay': 42}, {}, NODES,
                       'https://example.test', progress=progress)
        self.assertEqual(len(completed), 1)
        self.assertEqual(subscription_store.load_node_snapshot(PROVIDER)['nodes'][0]['delay'], 42)


if __name__ == '__main__':
    unittest.main()
