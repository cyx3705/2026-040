import unittest
from unittest.mock import patch
from echo_app import setup


class DictionarySetupTests(unittest.TestCase):
    def test_setup_only_installs_dictionary(self):
        with patch.object(setup, 'install_dictionary') as install:
            setup.main()
        install.assert_called_once()
        for retired in ['install_engine', 'upgrade_engine', 'pull_model', 'import_local_model']:
            self.assertFalse(hasattr(setup, retired))

    def test_native_command_is_disabled_before_loading_model(self):
        from echo_app.native_kv import main
        with self.assertRaisesRegex(SystemExit, 'EXT-001'):
            main([])

    def test_stale_explicit_data_path_fails_before_creating_empty_database(self):
        import os
        import subprocess
        import sys
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / 'moved-away'
            env = {**os.environ, 'ECHO_DATA_DIR':str(missing), 'PYTHONUTF8':'1'}
            result = subprocess.run([sys.executable,'-c','import echo_app.server'],
                env=env, capture_output=True, text=True, encoding='utf-8', timeout=10)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('ECHO_DATA_DIR', result.stderr)
            self.assertFalse(missing.exists())
