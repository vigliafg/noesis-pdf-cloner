"""Test dell'archivio per-utente della chiave OpenRouter (keystore.py)."""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import keystore  # noqa: E402


class KeyStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "secrets.json"
        self.store = keystore.KeyStore(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_set_get_clear_roundtrip(self):
        self.assertEqual(self.store.get(), "")
        self.assertFalse(self.store.has())
        self.store.set("sk-or-secret-1234")
        self.assertEqual(self.store.get(), "sk-or-secret-1234")
        self.assertTrue(self.store.has())
        self.store.clear()
        self.assertEqual(self.store.get(), "")
        self.assertFalse(self.path.exists())

    def test_empty_set_clears(self):
        self.store.set("abc")
        self.store.set("   ")
        self.assertFalse(self.path.exists())

    @unittest.skipIf(os.name == "nt", "permessi POSIX")
    def test_file_permissions_are_0600(self):
        self.store.set("sk-or-secret-1234")
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(oct(mode), "0o600")

    def test_mask(self):
        self.assertEqual(keystore.KeyStore.mask(""), "")
        self.assertEqual(keystore.KeyStore.mask("short"), "•••••")
        masked = keystore.KeyStore.mask("sk-or-abcdefgh1234")
        self.assertTrue(masked.startswith("sk-or-"))
        self.assertTrue(masked.endswith("1234"))
        self.assertIn("…", masked)

    def test_load_into_env_file_wins_over_env(self):
        """La chiave salvata ha la precedenza sulla variabile di sistema."""
        self.store.set("from-file")
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "from-env"}):
            source = keystore.load_into_env(self.path)
            self.assertEqual(source, "file")
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], "from-file")

    def test_load_into_env_env_is_fallback(self):
        """Senza chiave salvata si usa la variabile di sistema."""
        env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        env["OPENROUTER_API_KEY"] = "from-env"
        with mock.patch.dict(os.environ, env, clear=True):
            source = keystore.load_into_env(self.path)
            self.assertEqual(source, "env")
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], "from-env")

    def test_registry_env_key_empty_off_windows(self):
        if os.name == "nt":
            self.skipTest("comportamento Windows")
        self.assertEqual(keystore.registry_env_key(), ("", ""))

    def test_load_into_env_from_file(self):
        self.store.set("from-file")
        env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            source = keystore.load_into_env(self.path)
            self.assertEqual(source, "file")
            self.assertEqual(os.environ["OPENROUTER_API_KEY"], "from-file")

    def test_load_into_env_none(self):
        env = {k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(keystore.load_into_env(self.path), "none")

    def test_verify_missing(self):
        self.assertEqual(keystore.KeyStore.verify(""), (False, "missing"))

    def test_verify_invalid_401(self):
        import urllib.error

        error = urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/key", 401, "Unauthorized", {}, None
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            ok, reason = keystore.KeyStore.verify("sk-or-bad")
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid")

    def test_verify_unreachable(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            ok, reason = keystore.KeyStore.verify("sk-or-x")
        self.assertFalse(ok)
        self.assertEqual(reason, "unreachable")


if __name__ == "__main__":
    unittest.main()
