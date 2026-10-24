import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from echo_app import setup


class EngineUpgradeTests(unittest.TestCase):
    def test_check_does_not_download_or_replace_files(self):
        with patch.object(setup, "installed_engine_version", return_value="0.40.7"), \
                patch.object(setup, "latest_engine_version", return_value="0.40.8"), \
                patch.object(setup, "download") as download:
            result = setup.upgrade_engine(check=True)
        self.assertEqual(result["target"], "0.40.8")
        self.assertFalse(result["updated"])
        download.assert_not_called()

    def test_upgrade_preserves_old_engine_as_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data = root / "data"
            old = data / "engine"
            old.mkdir(parents=True)
            (old / "ollama.exe").write_bytes(b"old")
            (old / "lib.dll").write_bytes(b"old-lib")

            def fake_download(_url, archive, *args, **kwargs):
                with zipfile.ZipFile(archive, "w") as zf:
                    zf.writestr("ollama.exe", b"new")
                    zf.writestr("lib/ollama/libllama.dll", b"new-lib")

            offline = Mock(side_effect=setup.httpx.ConnectError("offline"))
            with patch.object(setup.config, "DATA_DIR", data), \
                    patch.object(setup.config, "ENGINE_DIR", old), \
                    patch.object(setup.config, "ENGINE_EXE", old / "ollama.exe"), \
                    patch.object(setup.config, "ENGINE_URL", "http://127.0.0.1:1"), \
                    patch.object(setup, "installed_engine_version", side_effect=["0.40.7", "0.40.8"]), \
                    patch.object(setup, "download", side_effect=fake_download), \
                    patch.object(setup.httpx, "get", offline):
                result = setup.upgrade_engine("0.40.8")

            self.assertTrue(result["updated"])
            self.assertEqual((old / "ollama.exe").read_bytes(), b"new")
            self.assertEqual((old / "lib" / "ollama" / "libllama.dll").read_bytes(), b"new-lib")
            self.assertTrue(Path(result["backup"]).is_dir())
            self.assertEqual((Path(result["backup"]) / "ollama.exe").read_bytes(), b"old")

    def test_upgrade_refuses_a_running_private_engine(self):
        with patch.object(setup, "installed_engine_version", return_value="0.40.7"), \
                patch.object(setup.httpx, "get", return_value=Mock(status_code=200)), \
                patch.object(setup, "download") as download:
            with self.assertRaisesRegex(RuntimeError, "仍在运行"):
                setup.upgrade_engine("0.40.8")
        download.assert_not_called()
