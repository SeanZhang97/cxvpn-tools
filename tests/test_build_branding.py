# -*- coding: utf-8 -*-
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_NAME = 'CX VPN TOOLS'
LEGACY_APP_NAME = 'CXVPN管理器'


class BuildBrandingTest(unittest.TestCase):
    def test_packagers_use_new_product_name(self):
        for relative in ('build.py', 'build_protected.py', '打包纯净版.bat'):
            source = (ROOT / relative).read_text(encoding='utf-8')
            self.assertIn(APP_NAME, source, relative)

    def test_legacy_name_is_only_used_for_config_migration(self):
        build_source = (ROOT / 'build.py').read_text(encoding='utf-8')
        protected_source = (
            ROOT / 'build_protected.py').read_text(encoding='utf-8')
        batch_source = (ROOT / '打包纯净版.bat').read_text(
            encoding='utf-8')

        self.assertIn("LEGACY_APP_NAME = 'CXVPN管理器'", build_source)
        self.assertIn("LEGACY_APP_NAME = 'CXVPN管理器'", protected_source)
        self.assertNotIn(LEGACY_APP_NAME, batch_source)

    def test_expected_output_paths_are_documented(self):
        build_source = (ROOT / 'build.py').read_text(encoding='utf-8')
        protected_source = (
            ROOT / 'build_protected.py').read_text(encoding='utf-8')

        self.assertIn("'--name', APP_NAME", build_source)
        self.assertIn(
            "SPEC_PATH = os.path.join(BASE, APP_NAME + '-protected.spec')",
            protected_source)
        self.assertIn("APP_NAME + '.exe'", protected_source)

    def test_rule_pack_files_are_distributed_and_preserved(self):
        for filename in ('local-direct-v1.txt', 'cn-direct-v1.txt'):
            path = ROOT / 'rule-packs' / filename
            self.assertTrue(path.is_file(), filename)
            path.read_text(encoding='utf-8')
        for relative in ('build.py', 'build_protected.py'):
            source = (ROOT / relative).read_text(encoding='utf-8')
            self.assertIn('RULE_PACK_FILES', source, relative)
            self.assertIn('saved_rule_packs', source, relative)
            self.assertIn('DIST_RULE_PACK_DIR', source, relative)


if __name__ == '__main__':
    unittest.main()
