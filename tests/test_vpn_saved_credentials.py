# -*- coding: utf-8 -*-
import ctypes
import os
import tempfile
import threading
import unittest
from unittest.mock import ANY, Mock, patch

from api import Api
from core import eap_connect, vpn_connect, vpn_service
from core.worker import Worker


class SavedCredentialDialTests(unittest.TestCase):
    @patch('core.vpn_connect.os.name', 'nt')
    @patch('core.vpn_connect.ctypes.WinDLL')
    def test_ras_watcher_registers_for_all_connections(self, win_dll):
        kernel32 = Mock()
        kernel32.CreateEventW = Mock(return_value=123)
        kernel32.CloseHandle = Mock(return_value=1)
        kernel32.WaitForSingleObject = Mock(
            return_value=vpn_connect.WAIT_TIMEOUT)
        rasapi = Mock()
        rasapi.RasConnectionNotificationW = Mock(return_value=0)
        win_dll.side_effect = lambda name, **_kwargs: (
            kernel32 if name == 'kernel32.dll' else rasapi)

        watcher = vpn_connect.RasConnectionWatcher()

        self.assertTrue(watcher.available)
        target = rasapi.RasConnectionNotificationW.call_args.args[0]
        self.assertEqual(target.value, ctypes.c_void_p(-1).value)
        watcher.close()
        kernel32.CloseHandle.assert_called_once_with(123)

    @patch('core.vpn_connect.time.sleep')
    @patch('core.vpn_connect.ctypes.WinDLL')
    def test_failed_ras_dial_releases_non_null_connection_handle(
            self, win_dll, sleep):
        rasapi = Mock()

        def fail_with_handle(*args):
            args[-1]._obj.value = 1234
            return 628

        rasapi.RasDialW = Mock(side_effect=fail_with_handle)
        rasapi.RasHangUpW = Mock(return_value=0)
        win_dll.return_value = rasapi

        code = vpn_connect._ras_dial_with_credentials(
            '工作 VPN', 'alice', 'secret')

        self.assertEqual(code, 628)
        rasapi.RasHangUpW.assert_called_once()
        sleep.assert_called_once_with(3)

    @patch('core.vpn_connect.time.sleep')
    @patch('core.vpn_connect.ctypes.WinDLL')
    def test_successful_ras_dial_keeps_connection_handle(self, win_dll, sleep):
        rasapi = Mock()

        def succeed_with_handle(*args):
            args[-1]._obj.value = 1234
            args[4](1234, 0, vpn_connect.RASCS_CONNECTED, 0, 0)
            return 0

        rasapi.RasDialW = Mock(side_effect=succeed_with_handle)
        rasapi.RasHangUpW = Mock(return_value=0)
        win_dll.return_value = rasapi

        code = vpn_connect._ras_dial_with_credentials(
            '工作 VPN', 'alice', 'secret')

        self.assertEqual(code, 0)
        rasapi.RasHangUpW.assert_not_called()
        sleep.assert_not_called()

    @patch('core.vpn_connect.time.sleep')
    @patch('core.vpn_connect.ctypes.WinDLL')
    def test_async_ras_dial_timeout_hangs_up_pending_connection(
            self, win_dll, sleep):
        rasapi = Mock()

        def never_finishes(*args):
            args[-1]._obj.value = 4321
            return 0

        rasapi.RasDialW = Mock(side_effect=never_finishes)
        rasapi.RasHangUpW = Mock(return_value=0)
        win_dll.return_value = rasapi

        code = vpn_connect._ras_dial_with_credentials(
            '工作 VPN', 'alice', 'secret', timeout=0.01)

        self.assertEqual(code, vpn_connect.ERROR_TIMEOUT)
        rasapi.RasHangUpW.assert_called_once()
        sleep.assert_called_once_with(3)

    @patch('core.vpn_connect._run')
    def test_status_parses_rasdial_multiline_connection_list(self, run):
        run.return_value = Mock(
            stdout='Connected to\r\n工作 VPN\r\n备用 VPN\r\n'
                   'Command completed successfully.\r\n')

        self.assertEqual(vpn_connect.status(), ['工作 VPN', '备用 VPN'])

    @patch('core.vpn_connect._run')
    def test_status_parses_chinese_multiline_connection_list(self, run):
        run.return_value = Mock(
            stdout='已连接到\r\n工作 VPN\r\n命令已成功完成。\r\n')

        self.assertEqual(vpn_connect.status(), ['工作 VPN'])

    def test_silence_pbk_updates_utf8_chinese_entry_without_bom(self):
        source = ("[其它 VPN]\r\nPreviewUserPw=1\r\n"
                  "[工作 VPN]\r\nPreviewUserPw=1\r\nPreviewDomain=1\r\n"
                  "ShowDialingProgress=1\r\nSkipDoubleDialDialog=0\r\n")
        with tempfile.TemporaryDirectory() as folder:
            pbk = os.path.join(folder, 'rasphone.pbk')
            with open(pbk, 'wb') as stream:
                stream.write(source.encode('utf-8'))

            with patch('core.vpn_connect._USER_PBK', pbk):
                vpn_connect._silence_pbk('工作 VPN')

            with open(pbk, 'rb') as stream:
                data = stream.read()

        self.assertFalse(data.startswith(b'\xef\xbb\xbf'))
        updated = data.decode('utf-8')
        self.assertIn('[其它 VPN]\r\nPreviewUserPw=1', updated)
        self.assertIn('[工作 VPN]\r\nPreviewUserPw=0', updated)
        self.assertIn('PreviewDomain=0', updated)
        self.assertIn('ShowDialingProgress=0', updated)
        self.assertIn('SkipDoubleDialDialog=1', updated)

    def test_rasdialparams_layout_keeps_username_password_and_domain_order(self):
        self.assertLess(vpn_connect._RASDIALPARAMS.szUserName.offset,
                        vpn_connect._RASDIALPARAMS.szPassword.offset)
        self.assertLess(vpn_connect._RASDIALPARAMS.szPassword.offset,
                        vpn_connect._RASDIALPARAMS.szDomain.offset)
        self.assertGreater(ctypes.sizeof(vpn_connect._RASDIALPARAMS), 2000)

    @patch('core.eap_connect.vpn_os._ps')
    def test_eap_credentials_are_encoded_and_stored_without_dialog(self, ps):
        ps.return_value = (True, 'CXVPN_EAP|PREPARED|0|', '')
        unsafe = "secret'\nWrite-Output injected"

        handled, ok, message = eap_connect.prepare(
            '工作 VPN', 'alice', unsafe)

        self.assertTrue(handled)
        self.assertTrue(ok)
        self.assertIn('写入 Windows', message)
        script = ps.call_args.args[0]
        process_env = ps.call_args.kwargs['env']
        self.assertNotIn(unsafe, script)
        self.assertEqual(
            process_env['CXVPN_EAP_PASSWORD_B64'],
            eap_connect._encoded(unsafe))
        self.assertNotIn(unsafe, str(ps.call_args))
        self.assertIn('EapHostPeerCredentialsXml2Blob', script)
        self.assertIn('RasSetEapUserData', script)
        self.assertIn('RasGetEapUserIdentity', script)
        self.assertNotIn('RasDialDlg', script)

    @patch('core.eap_connect.vpn_os._ps', return_value=(
        True, 'CXVPN_EAP|DIAL|0|', ''))
    def test_eap_connect_accepts_successful_rasdial_result(self, ps):
        handled, ok, message = eap_connect.connect(
            '工作 VPN', 'alice', 'secret')

        self.assertTrue(handled)
        self.assertTrue(ok)
        self.assertIn('EAP 凭据连接', message)

    @patch('core.eap_connect.vpn_os._ps', return_value=(
        True, 'CXVPN_EAP|IDENTITY|703|', ''))
    def test_eap_prepare_reports_noninteractive_identity_failure(self, ps):
        handled, ok, message = eap_connect.prepare(
            '工作 VPN', 'alice', 'secret')

        self.assertTrue(handled)
        self.assertFalse(ok)
        self.assertIn('703', message)

    @patch('core.vpn_connect.is_connected', return_value=False)
    @patch('core.vpn_connect._ras_dial_with_credentials', return_value=0)
    def test_pptp_uses_explicit_tool_credentials(self, dial, is_connected):
        ok, _ = vpn_connect.connect('工作 VPN', 'alice', 'secret')

        self.assertTrue(ok)
        dial.assert_called_once_with(
            '工作 VPN', 'alice', 'secret', timeout=90, progress=None,
            cancel_event=None)

    @patch('core.vpn_connect.is_connected', return_value=False)
    @patch('core.vpn_connect.eap_connect.connect', return_value=(
        True, True, '已连接'))
    @patch('core.vpn_connect._ras_dial_with_credentials', return_value=703)
    def test_703_switches_to_noninteractive_eap_data(
            self, dial, eap_dial, is_connected):
        ok, _ = vpn_connect.connect('工作 VPN', 'alice', 'secret')

        self.assertTrue(ok)
        eap_dial.assert_called_once_with(
            '工作 VPN', 'alice', 'secret', timeout=90)

    @patch('core.vpn_connect.is_connected', return_value=False)
    @patch('core.vpn_connect._recent_eap_error', return_value=691)
    @patch('core.vpn_connect._ras_dial_with_credentials', return_value=628)
    def test_628_with_recent_eap_691_is_promoted_to_auth_failure(
            self, dial, recent_error, is_connected):
        ok, message = vpn_connect.connect(
            '工作 VPN', 'alice', 'secret')

        self.assertFalse(ok)
        self.assertIn('691', message)
        self.assertNotIn('628', message)
        recent_error.assert_called_once()

    @patch('core.vpn_connect.is_connected', return_value=False)
    @patch('core.vpn_connect._recent_eap_error', return_value=0)
    @patch('core.vpn_connect._ras_dial_with_credentials', return_value=628)
    def test_628_without_matching_eap_event_remains_disconnect(
            self, dial, recent_error, is_connected):
        ok, message = vpn_connect.connect(
            '工作 VPN', 'alice', 'secret')

        self.assertFalse(ok)
        self.assertEqual(message, 'RAS 错误 628')

    @patch('core.vpn_connect.vpn_os._ps', return_value=(
        True, '[{"Code":"691","Domain":"alice","Username":""}]', ''))
    def test_recent_eap_error_reads_numeric_event_data(self, ps):
        code = vpn_connect._recent_eap_error('alice', 100.0)

        self.assertEqual(code, 691)
        script = ps.call_args.args[0]
        self.assertIn('Microsoft.PowerShell.Diagnostics.psd1', script)
        self.assertIn('FromUnixTimeMilliseconds(99000)', script)

    @patch('core.vpn_connect._ras_dial_with_credentials')
    def test_missing_tool_credentials_never_opens_windows_dialog(self, dial):
        ok, message = vpn_connect.connect('工作 VPN')

        self.assertFalse(ok)
        self.assertIn('NO_TOOL_CREDENTIALS', message)
        dial.assert_not_called()

    @patch('core.vpn_service.vpn_connect.connect', return_value=(True, '已连接'))
    @patch('core.vpn_service.sync_credentials', return_value=(True, 0))
    def test_service_passes_tool_credentials_to_dial(self, sync, dial):
        ok, _ = vpn_service.connect(
            '工作 VPN', {'user': 'alice', 'pass': 'secret'}, log=Mock())

        self.assertTrue(ok)
        sync.assert_called_once_with('工作 VPN', 'alice', 'secret')
        dial.assert_called_once_with(
            '工作 VPN', 'alice', 'secret', timeout=90, log=ANY,
            cancel_event=None)

    def test_missing_tool_credentials_prompts_in_app(self):
        message = vpn_service.friendly('NO_TOOL_CREDENTIALS')

        self.assertIn('软件尚未保存', message)
        self.assertIn('补录', message)


class ConnectApiTests(unittest.TestCase):
    def make_api(self):
        instance = Api.__new__(Api)
        instance._lock = threading.Lock()
        instance._vpn_action_lock = threading.Lock()
        instance.cfg = {'vpn_name': '工作 VPN', 'creds': {}}
        instance.worker = Mock()
        instance.worker.state = {}
        instance.worker.refresh_connections.return_value = [
            {'name': '工作 VPN', 'status': 'Disconnected'}]
        instance.worker.connection_snapshot.return_value = {
            'connections': [], 'connected_names': [],
            'connected_count': 0, 'route_conflicts': []}
        instance.log = Mock()
        return instance

    @patch('api.vpn_service.connect', return_value=(
        False, 'Windows 未保存此 VPN 的可用登录密码，请补录一次'))
    def test_missing_windows_password_requests_credentials_without_renewal(
            self, connect):
        instance = self.make_api()

        result = instance.connect_now()

        self.assertFalse(result['ok'])
        self.assertTrue(result['needs_credentials'])
        self.assertEqual(result['category'], 'credentials')
        self.assertFalse(result['retryable'])
        instance.worker.request_renew.assert_not_called()
        instance.worker.record_connection_failure.assert_called_once_with(
            'Windows 未保存此 VPN 的可用登录密码，请补录一次', '工作 VPN')
        connect.assert_called_once_with('工作 VPN', None, log=instance.log)

    @patch('api.vpn_service.connect', return_value=(True, '已提交'))
    def test_manual_connect_uses_shared_confirmation_gate(self, connect):
        instance = self.make_api()
        instance.cfg['creds']['工作 VPN'] = {
            'user': 'alice', 'pass': 'secret'}

        result = instance.connect_now()

        self.assertTrue(result['ok'])
        self.assertTrue(result['pending'])
        self.assertIn('正在完成连接', result['msg'])
        instance.worker.begin_connection_confirmation.assert_called_once_with(
            '工作 VPN', 'manual')

    @patch('api.vpn_service.connect', return_value=(
        False, '认证被拒 (691): 密码错误或授权到期'))
    def test_saved_password_auth_failure_still_triggers_renewal(self, connect):
        instance = self.make_api()
        instance.cfg['creds']['工作 VPN'] = {'user': 'alice', 'pass': 'secret'}

        result = instance.connect_now()

        self.assertFalse(result['ok'])
        self.assertFalse(result['needs_credentials'])
        self.assertEqual(result['category'], 'authentication')
        self.assertTrue(result['retryable'])
        instance.worker.request_renew.assert_called_once_with('recovery')


class ErrorPolicyTests(unittest.TestCase):
    def test_corrects_692_and_suggests_service_repair(self):
        message = vpn_service.friendly('RAS 错误 692')
        policy = vpn_service.failure_policy(message)

        self.assertIn('硬件故障', message)
        self.assertEqual(policy['category'], 'service')
        self.assertTrue(policy['suggest_repair'])
        self.assertFalse(policy['retryable'])

    def test_transient_network_error_keeps_backoff_retry(self):
        policy = vpn_service.failure_policy('无法到达 VPN 服务器 (800)')

        self.assertEqual(policy['category'], 'network')
        self.assertTrue(policy['retryable'])

    def test_ras_timeout_is_retryable_network_error(self):
        message = vpn_service.friendly('RAS 拨号超时 (1460)')
        policy = vpn_service.failure_policy(message)

        self.assertIn('强制终止', message)
        self.assertEqual(policy['category'], 'network')
        self.assertTrue(policy['retryable'])

    def test_missing_tool_credentials_are_not_treated_as_unknown_error(self):
        message = vpn_service.friendly('NO_TOOL_CREDENTIALS')
        policy = vpn_service.failure_policy(message)

        self.assertEqual(policy['category'], 'credentials')
        self.assertFalse(policy['retryable'])

    def test_protocol_error_pauses_automatic_retry(self):
        policy = vpn_service.failure_policy('无法协商 PPP 控制协议 (720)')

        self.assertEqual(policy['category'], 'configuration')
        self.assertFalse(policy['retryable'])

    def test_unknown_error_remains_retryable_for_compatibility(self):
        policy = vpn_service.failure_policy('未知的连接失败')

        self.assertEqual(policy['category'], 'unknown')
        self.assertTrue(policy['retryable'])

    def test_756_is_short_busy_retry_without_authorization(self):
        message = vpn_service.friendly('RAS 错误 756')
        policy = vpn_service.failure_policy(message)

        self.assertEqual(policy['category'], 'busy')
        self.assertEqual(policy['retry_delay'], 30)
        self.assertFalse(policy['trigger_renew'])


class WorkerConnectionPolicyTests(unittest.TestCase):
    def make_worker(self):
        return Worker(lambda: {}, Mock())

    @patch('core.worker.time.time', return_value=1000)
    def test_transient_error_sets_incremental_backoff(self, now):
        worker = self.make_worker()

        policy, wait = worker.record_connection_failure(
            '无法到达 VPN 服务器 (800)')

        self.assertTrue(policy['retryable'])
        self.assertEqual(wait, 5)
        self.assertEqual(worker._retry_after, 1005)
        self.assertFalse(worker._conn_blocked)

    @patch('core.vpn_service.connect')
    def test_manual_disconnect_suspends_automatic_reconnect(self, connect):
        worker = self.make_worker()
        worker.prepare_connection_target('工作 VPN')
        worker.note_manual_disconnect(['工作 VPN'])
        worker.refresh_connections = Mock(return_value=[
            {'name': '工作 VPN', 'status': 'Disconnected'}])

        result = worker._attempt_auto_connect({
            'vpn_name': '工作 VPN', 'auto_connect': True, 'creds': {}})

        self.assertIsNone(result)
        self.assertTrue(worker._auto_connect_suspended)
        connect.assert_not_called()

    @patch('core.worker.time.time', return_value=1000)
    def test_enabling_auto_connect_preserves_inflight_confirmation(self, now):
        worker = self.make_worker()
        worker.begin_connection_confirmation('工作 VPN', 'manual')

        worker.enable_auto_connect()

        self.assertFalse(worker._auto_connect_suspended)
        self.assertEqual(worker._connect_confirm_until, 1030)
        self.assertEqual(worker._last_conn, 1000)

    @patch('core.worker.time.time', return_value=1000)
    def test_new_manual_target_replaces_previous_confirmation_action(self, now):
        worker = self.make_worker()
        worker.begin_connection_confirmation('VPN A', 'manual')

        worker.prepare_connection_target('VPN B')

        action = worker.state['connection_action']
        self.assertEqual(worker._connect_confirm_until, 0)
        self.assertTrue(action['active'])
        self.assertEqual(action['status'], 'connecting')
        self.assertEqual(action['name'], 'VPN B')
        self.assertNotIn('VPN A', action['message'])

    @patch('core.worker.time.time', return_value=1000)
    def test_ras_disconnect_queues_bounded_reconnect_without_renewal(self, now):
        worker = self.make_worker()
        worker.prepare_connection_target('工作 VPN')
        worker._observed_connected_names = {'工作 VPN'}
        worker.refresh_connections = Mock(return_value=[
            {'name': '工作 VPN', 'status': 'Disconnected'}])

        worker._handle_ras_event({
            'vpn_name': '工作 VPN', 'auto_connect': True})

        self.assertTrue(worker._event_reconnect_pending)
        self.assertEqual(worker._retry_after, 1005)
        self.assertFalse(worker.renew_requested.is_set())
        self.assertEqual(
            worker.state['connection_action']['source'], 'ras_event')

    @patch('core.worker.time.time', side_effect=[
        1000, 1000, 1000, 1060, 1060, 1120, 1120, 1180, 1180, 1240, 1240])
    def test_five_ras_disconnects_pause_reconnect(self, now):
        worker = self.make_worker()
        worker.prepare_connection_target('工作 VPN')
        worker.refresh_connections = Mock(return_value=[
            {'name': '工作 VPN', 'status': 'Disconnected'}])
        cfg = {'vpn_name': '工作 VPN', 'auto_connect': True}

        for _ in range(5):
            worker._observed_connected_names = {'工作 VPN'}
            worker._handle_ras_event(cfg)

        self.assertTrue(worker._conn_blocked)
        self.assertFalse(worker._event_reconnect_pending)
        self.assertEqual(
            worker.state['connection_error']['category'], 'flapping')

    def test_configuration_error_blocks_until_reset(self):
        worker = self.make_worker()

        policy, wait = worker.record_connection_failure(
            '无法协商 PPP 控制协议 (720)')

        self.assertFalse(policy['retryable'])
        self.assertIsNone(wait)
        self.assertTrue(worker._conn_blocked)
        self.assertEqual(worker.state['connection_error']['code'], '720')

        worker.reset_backoff()
        self.assertFalse(worker._conn_blocked)
        self.assertIsNone(worker.state['connection_error'])

    @patch('core.worker.time.time', return_value=1000)
    def test_busy_error_uses_short_retry_delay(self, now):
        worker = self.make_worker()

        policy, wait = worker.record_connection_failure(
            'VPN 正在拨号 (756)')

        self.assertEqual(policy['category'], 'busy')
        self.assertEqual(wait, 30)
        self.assertEqual(worker._retry_after, 1030)

    @patch('core.vpn_service.connect')
    def test_auto_connect_exposes_active_action_until_finished(self, connect):
        worker = self.make_worker()
        worker.refresh_connections = Mock(side_effect=[
            [{'name': '工作 VPN', 'status': 'Disconnected'}],
            [{'name': '工作 VPN', 'status': 'Connected'}],
        ])

        def complete(*args, **kwargs):
            action = worker.state['connection_action']
            self.assertTrue(action['active'])
            self.assertEqual(action['status'], 'connecting')
            self.assertEqual(action['source'], 'automatic')
            return True, '已连接'

        connect.side_effect = complete
        cfg = {
            'vpn_name': '工作 VPN', 'auto_connect': True,
            'creds': {'工作 VPN': {'user': 'alice', 'pass': 'secret'}},
        }

        result = worker._attempt_auto_connect(cfg)

        self.assertTrue(result)
        self.assertFalse(worker.state['connection_action']['active'])
        self.assertEqual(
            worker.state['connection_action']['status'], 'connected')

    @patch('core.worker.time.time', return_value=1000)
    @patch('core.vpn_service.connect', return_value=(True, '已提交'))
    def test_successful_dial_waits_for_windows_without_redial_storm(
            self, connect, now):
        worker = self.make_worker()
        worker.refresh_connections = Mock(return_value=[
            {'name': '工作 VPN', 'status': 'Disconnected'}])
        cfg = {
            'vpn_name': '工作 VPN', 'auto_connect': True,
            'creds': {'工作 VPN': {'user': 'alice', 'pass': 'secret'}},
        }

        first = worker._attempt_auto_connect(cfg)
        second = worker._attempt_auto_connect(cfg)

        self.assertTrue(first)
        self.assertIsNone(second)
        connect.assert_called_once()
        self.assertEqual(worker._last_conn, 1000)
        self.assertEqual(worker._connect_confirm_until, 1030)
        self.assertTrue(worker.state['connection_action']['active'])
        self.assertEqual(
            worker.state['connection_action']['status'], 'verifying')

    @patch('core.worker.time.time', return_value=1000)
    @patch('core.vpn_service.connect')
    def test_existing_windows_connecting_state_is_never_redialed(
            self, connect, now):
        worker = self.make_worker()
        worker.refresh_connections = Mock(return_value=[
            {'name': '工作 VPN', 'status': 'Connecting'}])
        cfg = {'vpn_name': '工作 VPN', 'auto_connect': True, 'creds': {}}

        result = worker._attempt_auto_connect(cfg)

        self.assertIsNone(result)
        connect.assert_not_called()
        self.assertEqual(worker._connect_confirm_until, 1030)
        self.assertEqual(
            worker.state['connection_action']['status'], 'verifying')

    @patch('core.worker.time.time', return_value=1000)
    @patch('core.vpn_service.connect')
    def test_existing_windows_disconnecting_state_is_not_redialed(
            self, connect, now):
        worker = self.make_worker()
        worker.refresh_connections = Mock(return_value=[
            {'name': '工作 VPN', 'status': 'Disconnecting'}])
        cfg = {'vpn_name': '工作 VPN', 'auto_connect': True, 'creds': {}}

        result = worker._attempt_auto_connect(cfg)

        self.assertIsNone(result)
        connect.assert_not_called()
        self.assertEqual(worker._retry_after, 1005)

    @patch('core.vpn_service.connect', return_value=(True, '已提交'))
    def test_unconfirmed_dial_enters_bounded_retry(self, connect):
        worker = self.make_worker()
        worker.refresh_connections = Mock(return_value=[
            {'name': '工作 VPN', 'status': 'Disconnected'}])
        cfg = {'vpn_name': '工作 VPN', 'auto_connect': True, 'creds': {}}

        with patch('core.worker.time.time', return_value=1000) as now:
            self.assertTrue(worker._attempt_auto_connect(cfg))
            now.return_value = 1031
            result = worker._check_connection_confirmation(cfg)

        self.assertFalse(result)
        self.assertEqual(worker._conn_fail, 1)
        self.assertTrue(worker._event_reconnect_pending)
        connect.assert_called_once()

    @patch('core.vpn_service.connect')
    def test_auto_connect_does_not_override_other_active_vpn(self, connect):
        worker = self.make_worker()
        worker.refresh_connections = Mock(return_value=[
            {'name': 'VPN A', 'status': 'Disconnected'},
            {'name': 'VPN B', 'status': 'Connected'},
        ])
        cfg = {'vpn_name': 'VPN A', 'auto_connect': True, 'creds': {}}

        result = worker._attempt_auto_connect(cfg)

        self.assertIsNone(result)
        connect.assert_not_called()

    @patch('core.vpn_service.connect')
    def test_authorization_reconnect_clears_queued_state_when_other_vpn_active(
            self, connect):
        worker = self.make_worker()
        worker.refresh_connections = Mock(return_value=[
            {'name': 'VPN A', 'status': 'Disconnected'},
            {'name': 'VPN B', 'status': 'Connected'},
        ])
        worker._set_connection_action(
            'queued', 'authorization', 'VPN A', '准备重新连接')
        cfg = {'vpn_name': 'VPN A', 'auto_connect': True, 'creds': {}}

        result = worker._attempt_auto_connect(cfg, source='authorization')

        self.assertIsNone(result)
        self.assertFalse(worker.state['connection_action']['active'])
        self.assertEqual(worker.state['connection_action']['status'], 'skipped')
        self.assertIn('VPN B', worker.state['connection_action']['message'])
        connect.assert_not_called()

    @patch('core.worker.time.time', return_value=1234)
    def test_manual_action_defers_next_auto_connect_check(self, now):
        worker = self.make_worker()
        worker._last_conn = 0

        with worker.manual_connection_action():
            worker._last_conn = 0

        self.assertEqual(worker._last_conn, 1234)

    def test_queued_auto_connect_never_blocks_worker_lock_probe(self):
        worker = self.make_worker()
        started = threading.Event()
        release = threading.Event()
        worker.refresh_connections = Mock(return_value=[
            {'name': '工作 VPN', 'status': 'Disconnected'}])

        def blocking_connect(*_args, **_kwargs):
            started.set()
            release.wait(1)
            return False, 'RAS 拨号超时 (90 秒)'

        cfg = {
            'vpn_name': '工作 VPN', 'auto_connect': True,
            'creds': {'工作 VPN': {'user': 'alice', 'pass': 'secret'}},
        }
        with patch('core.vpn_service.connect', side_effect=blocking_connect):
            queued = worker._queue_auto_connect(cfg, 'automatic')

            self.assertTrue(queued)
            self.assertTrue(started.wait(0.2))
            self.assertEqual(worker.dial_in_progress()['name'], '工作 VPN')
            self.assertFalse(worker._try_vpn_operation(lambda: None))
            worker.browser_requested.set()
            self.assertTrue(worker.browser_requested.is_set())
            release.set()
            worker._auto_connect_thread.join(1)

    @patch('core.worker.time.time', return_value=1000)
    def test_authorization_success_explicitly_requests_reconnect(self, now):
        worker = self.make_worker()
        worker._queue_auto_connect = Mock(return_value=True)
        cfg = {'vpn_name': '工作 VPN', 'auto_connect': True}

        result = worker._reconnect_after_authorization(cfg)

        self.assertTrue(result)
        self.assertEqual(worker._renew_trigger_after, 1600)
        worker._queue_auto_connect.assert_called_once_with(
            cfg, source='authorization')
        self.assertEqual(
            worker.state['connection_action']['status'], 'queued')

    @patch('core.worker.time.time', return_value=1000)
    def test_authorization_success_does_not_duplicate_pending_connection(
            self, now):
        worker = self.make_worker()
        worker._attempt_auto_connect = Mock()
        worker.begin_connection_confirmation('工作 VPN', 'manual')

        result = worker._reconnect_after_authorization({
            'vpn_name': '工作 VPN', 'auto_connect': True})

        self.assertIsNone(result)
        worker._attempt_auto_connect.assert_not_called()
        self.assertEqual(
            worker.state['connection_action']['status'], 'verifying')

    def test_ras_event_burst_keeps_first_refresh_deadline(self):
        worker = self.make_worker()

        worker._schedule_ras_refresh(1000)
        worker._schedule_ras_refresh(1000.8)

        self.assertEqual(worker._ras_refresh_due, 1001)

    def test_authorization_success_skips_reconnect_when_auto_connect_is_off(self):
        worker = self.make_worker()
        worker._attempt_auto_connect = Mock()

        result = worker._reconnect_after_authorization({
            'vpn_name': '工作 VPN', 'auto_connect': False})

        self.assertIsNone(result)
        worker._attempt_auto_connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
