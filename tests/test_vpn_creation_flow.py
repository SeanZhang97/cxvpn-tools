# -*- coding: utf-8 -*-
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from api import Api
from core import ras_cred, vpn_os, vpn_repair, web_flow
from core.worker import Worker


class VpnProfileCommandTests(unittest.TestCase):
    @patch('core.vpn_os._ps', return_value=(True, '', ''))
    def test_ikev2_profile_remembers_credentials_and_uses_eap(self, ps):
        ok, _ = vpn_os.add_vpn('办公 VPN', 'vpn.example.com', 'Ikev2')

        self.assertTrue(ok)
        command = ps.call_args.args[0]
        self.assertIn('-RememberCredential', command)
        self.assertIn('-AuthenticationMethod Eap', command)

    @patch('core.vpn_os._ps', return_value=(True, '', ''))
    def test_l2tp_psk_is_safely_quoted(self, ps):
        ok, _ = vpn_os.add_vpn(
            '办公 VPN', 'vpn.example.com', 'L2tp', l2tp_psk="a'b")

        self.assertTrue(ok)
        command = ps.call_args.args[0]
        self.assertIn("-L2tpPsk 'a''b'", command)
        self.assertIn('-AuthenticationMethod MSChapv2', command)


class ApiVpnCreationTests(unittest.TestCase):
    def make_api(self, default=''):
        instance = Api.__new__(Api)
        instance._lock = threading.Lock()
        instance._vpn_action_lock = threading.Lock()
        instance.cfg = {
            'vpn_name': default, 'creds': {}, 'credential_status': {}}
        instance.worker = Mock()
        instance.worker.refresh_connections.return_value = []
        instance.routing_updates = Mock()
        instance.log = Mock()
        return instance

    @patch('api.cfgmod.save')
    @patch('api.vpn_os.add_vpn', return_value=(True, '已创建'))
    def test_first_created_profile_becomes_default(self, add, save):
        instance = self.make_api()

        result = instance.add_vpn('办公 VPN', 'vpn.example.com', 'Ikev2')

        self.assertTrue(result['ok'])
        self.assertEqual(result['default_name'], '办公 VPN')
        self.assertEqual(instance.cfg['vpn_name'], '办公 VPN')
        save.assert_called_once_with(instance.cfg)
        instance.worker.refresh_connections.assert_called_once_with()

    @patch('api.cfgmod.save')
    @patch('api.vpn_os.add_vpn', return_value=(True, '已创建'))
    def test_existing_default_is_preserved(self, add, save):
        instance = self.make_api('原 VPN')

        result = instance.add_vpn('新 VPN', 'vpn.example.com', 'Pptp')

        self.assertEqual(result['default_name'], '原 VPN')
        self.assertEqual(instance.cfg['vpn_name'], '原 VPN')
        save.assert_not_called()

    @patch('api.cfgmod.save')
    @patch('api.vpn_service.sync_credentials', return_value=(True, 0))
    @patch('api.vpn_service.connect')
    def test_save_credentials_syncs_without_dial_or_authorization(
            self, connect, sync, save):
        instance = self.make_api('办公 VPN')

        result = instance.save_cred('办公 VPN', 'alice', 'secret')

        self.assertTrue(result['ok'])
        self.assertFalse(result['authorization_started'])
        self.assertTrue(result['windows_saved'])
        self.assertNotIn('办公 VPN', instance.cfg['credential_status'])
        instance.worker.request_renew.assert_not_called()
        connect.assert_not_called()
        sync.assert_called_once_with('办公 VPN', 'alice', 'secret')
        save.assert_called_once_with(instance.cfg)

    @patch('api.cfgmod.save')
    @patch('api.vpn_service.sync_credentials',
           return_value=(False, 'VERIFY_FAILED'))
    @patch('api.vpn_service.connect')
    def test_save_reports_windows_sync_failure_without_dialing(
            self, connect, sync, save):
        instance = self.make_api('办公 VPN')

        result = instance.save_cred('办公 VPN', 'alice', 'secret')

        self.assertFalse(result['ok'])
        self.assertTrue(result['tool_saved'])
        self.assertFalse(result['windows_saved'])
        self.assertIn('Windows 同步失败', result['msg'])
        self.assertEqual(
            instance.cfg['credential_status']['办公 VPN'],
            'windows_sync_failed')
        connect.assert_not_called()
        save.assert_called_once_with(instance.cfg)

    @patch('api.vpn_service.connect', return_value=(
        False, '认证被拒 (691): 密码错误或授权到期'))
    def test_691_uses_normal_authorization_recovery_without_pending_state(
            self, connect):
        instance = self.make_api('办公 VPN')
        instance.cfg['creds']['办公 VPN'] = {
            'user': 'alice', 'pass': 'secret'}
        instance.worker.refresh_connections.return_value = [
            {'name': '办公 VPN', 'status': 'Disconnected'}]
        instance.worker.connection_snapshot.return_value = {
            'connections': [], 'profiles': [], 'route_conflicts': []}

        result = instance.connect_vpn('办公 VPN')

        self.assertFalse(result['ok'])
        instance.worker.request_renew.assert_called_once_with('recovery')
        self.assertNotIn('办公 VPN', instance.cfg['credential_status'])

    @patch('api.proxy_guard.startup_check', return_value=None)
    @patch('api.cfgmod.save')
    def test_start_removes_obsolete_pending_validation_state(self, save, check):
        instance = self.make_api('办公 VPN')
        instance.cfg['credential_status']['办公 VPN'] = \
            'pending_authorization'
        instance.worker.is_alive.return_value = False

        instance.start()

        self.assertNotIn('办公 VPN', instance.cfg['credential_status'])
        instance.worker.request_renew.assert_not_called()
        instance.worker.start.assert_called_once_with()
        save.assert_called_once_with(instance.cfg)
        check.assert_called_once_with(log=instance.log)

    @patch('api.proxy_guard.startup_check', return_value=None)
    @patch('api.vpn_service.sync_credentials', return_value=(True, 0))
    def test_start_resyncs_all_tool_credentials_without_dial(self, sync, check):
        instance = self.make_api('办公 VPN')
        instance.cfg['creds']['办公 VPN'] = {
            'user': 'alice', 'pass': 'secret'}
        instance.worker.is_alive.return_value = False

        instance.start()

        sync.assert_called_once_with('办公 VPN', 'alice', 'secret')
        check.assert_called_once_with(log=instance.log)


class RasCredentialPersistenceTests(unittest.TestCase):
    @patch('core.ras_cred._get_entry_dial_params',
           return_value=(0, 'alice', '********', True))
    @patch('core.ras_cred._set_entry_dial_params', return_value=0)
    @patch('core.ras_cred._get_credentials', return_value=(0, '', ''))
    @patch('core.ras_cred._set_credentials', return_value=0)
    def test_set_falls_back_to_entry_dial_params_and_verifies(
            self, set_primary, get_primary, set_fallback, get_fallback):
        ok, error = ras_cred.set('办公 VPN', 'alice', 'secret')

        self.assertTrue(ok)
        self.assertEqual(error, 0)
        set_fallback.assert_called_once_with('办公 VPN', 'alice', 'secret')

    @patch('core.ras_cred._get_entry_dial_params',
           return_value=(0, '', '', False))
    @patch('core.ras_cred._set_entry_dial_params', return_value=0)
    @patch('core.ras_cred._get_credentials', return_value=(0, '', ''))
    @patch('core.ras_cred._set_credentials', return_value=0)
    def test_set_never_reports_success_without_roundtrip_password(
            self, set_primary, get_primary, set_fallback, get_fallback):
        ok, error = ras_cred.set('办公 VPN', 'alice', 'secret')

        self.assertFalse(ok)
        self.assertIn('回读校验失败', error)


class SnapshotAndAuthorizationTests(unittest.TestCase):
    def test_connection_snapshot_contains_all_profiles(self):
        worker = Worker(lambda: {'vpn_name': '办公 VPN'}, Mock())
        worker._profiles = [
            {'name': '办公 VPN', 'status': 'Disconnected'},
            {'name': '备用 VPN', 'status': 'Disconnected'},
        ]
        worker._profiles_checked_at = 100

        snapshot = worker.connection_snapshot('办公 VPN')

        self.assertEqual([row['name'] for row in snapshot['profiles']],
                         ['办公 VPN', '备用 VPN'])

    def test_api_success_dom_sync_marks_requested_rows(self):
        page = Mock()
        page.evaluate.return_value = 2

        updated = web_flow._sync_authorization_result(page, [1], [2])

        self.assertEqual(updated, 2)
        script, payload = page.evaluate.call_args.args
        self.assertEqual(payload, {'auth_ids': [1], 'renew_ids': [2]})
        self.assertIn('cx-authorization-success', script)
        self.assertIn('position:fixed', script)
        self.assertIn('setTimeout(() => banner.remove()', script)
        self.assertNotIn('insertBefore(banner, list)', script)

class RepairStateTests(unittest.TestCase):
    def make_api(self):
        instance = Api.__new__(Api)
        instance._repair_lock = threading.Lock()
        instance._repairing = False
        instance._repair_result = None
        instance._repair_run_id = 0
        instance.worker = Mock()
        instance.log = Mock()
        return instance

    @patch('core.vpn_repair.repair', return_value=(True, '修复完成'))
    @patch('api.threading.Thread')
    def test_repair_exposes_completion_state(self, thread_cls, repair):
        instance = self.make_api()
        thread_cls.side_effect = lambda target, daemon: Mock(
            start=lambda: target())

        result = instance.repair_vpn()

        self.assertTrue(result['ok'])
        self.assertFalse(instance._repairing)
        self.assertTrue(instance._repair_result['ok'])
        self.assertEqual(instance._repair_result['msg'], '修复完成')

    @patch('api.threading.Thread')
    def test_repair_rejects_duplicate_run(self, thread_cls):
        instance = self.make_api()
        thread_cls.return_value = Mock(start=Mock())

        first = instance.repair_vpn()
        second = instance.repair_vpn()

        self.assertTrue(first['ok'])
        self.assertFalse(second['ok'])
        self.assertIn('请勿重复', second['msg'])


class RepairBomTests(unittest.TestCase):
    def test_powershell_utf8_bom_result_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            def fake_run(args, **kwargs):
                result_file = args[-1]
                with open(result_file, 'w', encoding='utf-8-sig') as stream:
                    json.dump({'ok': True, 'msg': '完成'}, stream,
                              ensure_ascii=False)
                return Mock(returncode=0)

            with patch('core.vpn_repair.tempfile.gettempdir',
                       return_value=folder), \
                    patch('core.vpn_repair._is_admin', return_value=True), \
                    patch('core.vpn_repair.subprocess.run', side_effect=fake_run):
                ok, message = vpn_repair.repair(log=None)

        self.assertTrue(ok)
        self.assertEqual(message, '完成')

    def test_elevated_repair_process_is_hidden_after_uac(self):
        with tempfile.TemporaryDirectory() as folder:
            calls = []

            def fake_run(args, **kwargs):
                calls.append((args, kwargs))
                result_file = os.path.join(
                    folder, 'cxvpn_repair_result.json')
                with open(result_file, 'w', encoding='utf-8') as stream:
                    json.dump({'ok': True, 'msg': '完成'}, stream,
                              ensure_ascii=False)
                return Mock(returncode=0)

            with patch('core.vpn_repair.tempfile.gettempdir',
                       return_value=folder), \
                    patch('core.vpn_repair._is_admin', return_value=False), \
                    patch('core.vpn_repair.subprocess.run', side_effect=fake_run):
                ok, _message = vpn_repair.repair(log=None)

        self.assertTrue(ok)
        self.assertIn('-WindowStyle Hidden', calls[0][0][-1])
        self.assertIn('-Verb RunAs', calls[0][0][-1])

    def test_repair_script_keeps_isolated_rasman_kill_guard(self):
        script = vpn_repair._REPAIR_PS

        self.assertIn("@('Running','Stop Pending')", script)
        self.assertIn("$hosted.Count -ne 1", script)
        self.assertIn("$pr.Name -ine 'svchost.exe'", script)
        self.assertIn("ProcessId -ne $oldPid", script)


if __name__ == '__main__':
    unittest.main()
