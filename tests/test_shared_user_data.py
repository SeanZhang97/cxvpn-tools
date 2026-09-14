# -*- coding: utf-8 -*-
"""不同安装目录共用用户主库；可用 --exe 验证真实打包代码，所有数据严格隔离。"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]
EXE = None


class SharedUserDataTests(unittest.TestCase):
    def test_two_install_directories_read_the_same_saved_config(self):
        with tempfile.TemporaryDirectory(prefix='cxvpn-recovery-') as folder:
            root = Path(folder)
            (root / 'probe-allowed').write_text('isolated test', encoding='utf-8')
            results = []
            for name, stage in (('installed', 'shared-config-save'),
                                ('portable', 'shared-config-load')):
                install = root / name
                install.mkdir()
                # 安装位置的旧配置即使存在，也不能覆盖用户主库。
                (install / 'config.json').write_text('{"phone":"wrong-directory"}', encoding='utf-8')
                if EXE:
                    executable = install / 'CXVPNTools.exe'
                    shutil.copy2(EXE, executable)
                    shutil.copytree(Path(EXE).parent / '_internal', install / '_internal')
                    command = [str(executable)]
                else:
                    command = [sys.executable, '-X', 'utf8', str(PROJECT / 'main.py')]
                subprocess.run(
                    command + ['--storage-recovery-probe', str(root), stage],
                    cwd=install, timeout=20, check=True, capture_output=True,
                    text=True, encoding='utf-8', errors='backslashreplace')
                results.append(json.loads((root / (stage + '.json')).read_text(encoding='utf-8')))
                self.assertFalse((install / 'state.sqlite3').exists())
                self.assertFalse((install / '_internal' / 'state.sqlite3').exists())
                self.assertEqual(json.loads((install / 'config.json').read_text(encoding='utf-8')),
                                 {'phone': 'wrong-directory'})
            self.assertEqual(results[0], results[1])
            self.assertEqual(Path(results[1]['root']), root / 'local' / 'CXVPNTools')
            self.assertEqual(results[1]['config']['phone'], '13800000000')
            self.assertEqual(results[1]['config']['vpn_name'], '测试 VPN e\u0301 🇨🇳')
            self.assertEqual(len(results[1]['config']['creds']), 1)
            self.assertTrue((root / 'local' / 'CXVPNTools' / 'state.sqlite3').is_file())


if __name__ == '__main__':
    if '--exe' in sys.argv:
        position = sys.argv.index('--exe')
        EXE = str(Path(sys.argv[position + 1]).resolve())
        del sys.argv[position:position + 2]
    unittest.main()
