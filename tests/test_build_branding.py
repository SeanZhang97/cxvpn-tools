# -*- coding: utf-8 -*-
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_NAME = 'CXVPNTools'
LEGACY_APP_NAMES = ('CX VPN TOOLS', 'CXVPN管理器')


class BuildBrandingTest(unittest.TestCase):
    def test_packagers_use_new_product_name(self):
        for relative in ('build.py', 'build_protected.py', '打包纯净版.bat'):
            source = (ROOT / relative).read_text(encoding='utf-8')
            self.assertIn(APP_NAME, source, relative)

    def test_legacy_names_are_only_used_for_config_migration(self):
        build_source = (ROOT / 'build.py').read_text(encoding='utf-8')
        protected_source = (
            ROOT / 'build_protected.py').read_text(encoding='utf-8')
        batch_source = (ROOT / '打包纯净版.bat').read_text(
            encoding='utf-8')

        expected = "LEGACY_APP_NAMES = ('CX VPN TOOLS', 'CXVPN管理器')"
        self.assertIn(expected, build_source)
        self.assertIn(expected, protected_source)
        for legacy in LEGACY_APP_NAMES:
            self.assertNotIn(legacy, batch_source)

    def test_expected_output_paths_are_documented(self):
        build_source = (ROOT / 'build.py').read_text(encoding='utf-8')
        protected_source = (
            ROOT / 'build_protected.py').read_text(encoding='utf-8')

        self.assertIn("'--name', APP_NAME", build_source)
        self.assertIn(
            "SPEC_PATH = os.path.join(BASE, APP_NAME + '-protected.spec')",
            protected_source)
        self.assertIn("APP_NAME + '.exe'", protected_source)

    def test_desktop_identity_uses_exact_product_name(self):
        source = (ROOT / 'core' / 'windows_desktop.py').read_text(
            encoding='utf-8')

        self.assertIn("APP_NAME = 'CXVPNTools'", source)
        self.assertIn("WINDOW_TITLE = 'CXVPNTools'", source)
        self.assertIn(
            r"INSTANCE_MUTEX = r'Local\CXVPNTools.Singleton.v1'", source)

    def test_rule_pack_files_are_bundled_and_legacy_data_is_migrated(self):
        for filename in ('local-direct-v1.txt', 'cn-direct-v1.txt'):
            path = ROOT / 'rule-packs' / filename
            self.assertTrue(path.is_file(), filename)
            path.read_text(encoding='utf-8')
        for relative in ('build.py', 'build_protected.py'):
            source = (ROOT / relative).read_text(encoding='utf-8')
            self.assertIn('SOURCE_RULE_PACK_DIR', source, relative)
            self.assertIn('migrate_legacy_user_data', source, relative)
            self.assertNotIn('saved_rule_packs', source, relative)
            self.assertNotIn('DIST_RULE_PACK_DIR', source, relative)


if __name__ == '__main__':
    unittest.main()
