import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from api import Api
from core import config, config_maintenance


class VpnOrderTests(unittest.TestCase):
    def test_storage_reload_and_backup(self):
        names = ['机房 e\u0301 \U0001f1e8\U0001f1f3', '公司 VPN']
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'config.json')
            with patch.object(config, 'CFG_PATH', path), \
                    patch.object(config, 'CFG_BACKUP_PATH', path + '.bak'), \
                    patch.object(config, 'CFG_HISTORY_DIR', str(Path(folder) / 'history')):
                data = copy.deepcopy(config.DEFAULT)
                data['vpn_order'] = names
                config.save(data)
                self.assertEqual(config.load()['vpn_order'], names)
                exported = config_maintenance.create_backup(data)
                restored = config_maintenance.prepare_restore(exported['content'], config.DEFAULT)
                self.assertEqual(restored['candidate']['vpn_order'], names)
                self.assertIn(names[0], Path(path).read_text(encoding='utf-8'))

    def test_invalid_order_rejected(self):
        for value in (None, 'vpn', ['A', 'a'], [''], [1], ['a' * 257]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.validate_vpn_order(value)

    def make_api(self):
        instance = Api.__new__(Api)
        instance.cfg = {'vpn_name': 'A', 'vpn_order': ['A', 'B'], 'creds': {'A': {}}}
        instance._lock = threading.Lock()
        instance.worker = Mock()
        instance.log = Mock()
        instance._poke_ui_state = Mock()
        return instance

    def test_save_patch_preserves_other_fields_and_failure_preserves_order(self):
        instance = self.make_api()
        with patch('api.cfgmod.save') as save:
            self.assertTrue(instance.save_config({'vpn_order': ['B', 'A']}))
            self.assertEqual(instance.cfg['vpn_name'], 'A')
            self.assertEqual(instance.cfg['creds'], {'A': {}})
            self.assertEqual(save.call_args.args[0]['vpn_order'], ['B', 'A'])
            instance.worker.update_default_target.assert_not_called()
        with patch('api.cfgmod.save', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                instance.save_config({'vpn_order': ['A', 'B']})
        self.assertEqual(instance.cfg['vpn_order'], ['B', 'A'])

    def test_remove_only_prunes_deleted_name(self):
        instance = self.make_api()
        with patch('api.vpn_os.remove_vpn', return_value=(True, 'ok')), patch('api.cfgmod.save'):
            instance.remove_vpn('A')
        self.assertEqual(instance.cfg['vpn_order'], ['B'])


if __name__ == '__main__':
    unittest.main()
