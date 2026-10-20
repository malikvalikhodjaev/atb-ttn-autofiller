import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from app import check_installation
from settings import DEFAULT_SETTINGS, SettingsError, load_settings, save_settings, validate_settings
from tools.package_release import FILES, RELEASE, build_release


class SettingsTests(unittest.TestCase):
    def test_missing_file_returns_defaults_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            self.assertEqual(load_settings(path), DEFAULT_SETTINGS)
            self.assertFalse(path.exists())

    def test_atomic_roundtrip_preserves_text_and_leading_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            value = {"smartup_url": "https://smartup.example.test/app/", "company_tin": "012345678", "loading_address": "Ташкент, адрес из справочника"}
            saved = save_settings(path, value)
            self.assertEqual(load_settings(path), saved)
            self.assertEqual(saved["company_tin"], "012345678")
            self.assertEqual(saved["loading_address"], value["loading_address"])
            self.assertFalse(path.with_name(path.name + ".tmp").exists())

    def test_unsafe_or_malformed_links_are_rejected(self):
        for url in ("javascript:alert(1)", "file:///data", "https://user:password@example.test", "https://example.test/?token=secret", "https://example.test/#token", "https://example.test:99999/", "https://exam ple.test", "https://example.test\\path", "https://example.test:0/"):
            with self.subTest(url=url), self.assertRaises(SettingsError):
                validate_settings({"smartup_url": url})

    def test_unknown_fields_control_characters_and_wrong_tin_are_rejected(self):
        for value in ({"password": "secret"}, {"schema_version": True}, {"company_tin": "123"}, {"loading_address": "a\nb"}, {"didox_url": ""}, {"smartup_url": None}):
            with self.subTest(value=value), self.assertRaises(SettingsError):
                validate_settings(value)

    def test_broken_file_is_reported_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            path.write_text("broken", encoding="utf-8")
            with self.assertRaises(SettingsError):
                load_settings(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "broken")

    def test_preflight_detects_invalid_settings_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(check_installation(path), 0)
                path.write_text(json.dumps({"company_tin": "123"}), encoding="utf-8")
                self.assertEqual(check_installation(path), 1)


class ReleaseTests(unittest.TestCase):
    def test_clean_distribution_starts_without_workspace_or_codex_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            archive_path = build_release(path)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(set(archive.namelist()), {RELEASE + "/" + name for name in FILES})
                self.assertFalse(any("materials/" in name or "config.local.json" in name or "node_modules" in name for name in archive.namelist()))
                archive.extractall(path / "unpacked")
            result = subprocess.run([sys.executable, "-E", "-S", "app.py", "--check"], cwd=path / "unpacked" / RELEASE, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(archive_path.with_suffix(".zip.sha256").is_file())


if __name__ == "__main__":
    unittest.main()
