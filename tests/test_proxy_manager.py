"""Tests per ``proxy_manager`` (avvio proxy + probe provider)."""

import io
import json
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import proxy_manager  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ProbeTests(unittest.TestCase):
    def test_probe_reports_provider(self):
        with mock.patch.object(
            proxy_manager.urllib.request,
            "urlopen",
            return_value=_FakeResponse({"provider": "Groq", "model": "openai/gpt-oss-120b"}),
        ):
            out = proxy_manager.probe("http://127.0.0.1:8790/v1", "m", "k")
        self.assertTrue(out["ok"])
        self.assertEqual(out["provider"], "Groq")

    def test_probe_http_error_includes_message(self):
        err = urllib.error.HTTPError(
            "http://x", 404, "Not Found", {}, io.BytesIO(
                json.dumps({"error": {"message": "no allowed providers"}}).encode()
            ),
        )
        with mock.patch.object(
            proxy_manager.urllib.request, "urlopen", side_effect=err
        ):
            out = proxy_manager.probe("http://x/v1", "m", "k")
        self.assertFalse(out["ok"])
        self.assertIn("404", out["error"])
        self.assertIn("no allowed providers", out["error"])

    def test_probe_connection_error(self):
        with mock.patch.object(
            proxy_manager.urllib.request,
            "urlopen",
            side_effect=OSError("connection refused"),
        ):
            out = proxy_manager.probe("http://127.0.0.1:1/v1", "m", "k")
        self.assertFalse(out["ok"])
        self.assertIn("connection refused", out["error"])


class ManagerTests(unittest.TestCase):
    def test_ensure_started_noop_when_already_healthy(self):
        mgr = proxy_manager.ProxyManager()
        with mock.patch.object(proxy_manager, "_health_ok", return_value=True), \
             mock.patch.object(proxy_manager.subprocess, "Popen") as popen:
            self.assertTrue(mgr.ensure_started(8790))
            popen.assert_not_called()

    def test_ensure_started_false_without_script(self):
        mgr = proxy_manager.ProxyManager()
        with mock.patch.object(proxy_manager, "_health_ok", return_value=False), \
             mock.patch.object(
                 proxy_manager, "_proxy_script", return_value=proxy_manager.Path("/nope")
             ):
            self.assertFalse(mgr.ensure_started(8790))

    def test_stop_without_proc_is_safe(self):
        proxy_manager.ProxyManager().stop()  # non deve sollevare

    def test_proxy_python_not_frozen_is_current_interpreter(self):
        with mock.patch.object(
            proxy_manager.sys, "frozen", False, create=True
        ):
            self.assertEqual(proxy_manager._proxy_python(), proxy_manager.sys.executable)

    def test_proxy_python_frozen_uses_engine_venv(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            py = Path(tmp) / "python.exe"
            py.write_text("")
            with mock.patch.object(
                proxy_manager.sys, "frozen", True, create=True
            ), mock.patch(
                "clone_engine.find_pdf2zh_bin", return_value=Path(tmp) / "pdf2zh_next"
            ), mock.patch(
                "clone_engine.venv_python_for", return_value=str(py)
            ):
                self.assertEqual(proxy_manager._proxy_python(), str(py))

    def test_console_kwargs_empty_on_non_windows(self):
        with mock.patch.object(proxy_manager.os, "name", "posix"):
            self.assertEqual(proxy_manager._console_kwargs(), {})

    def test_console_kwargs_minimizes_console_on_windows(self):
        """Su Windows il proxy parte ridotto a icona, senza rubare il focus."""

        class _FakeStartupInfo:
            def __init__(self):
                self.dwFlags = 0
                self.wShowWindow = None

        with mock.patch.object(proxy_manager.os, "name", "nt"), mock.patch.object(
            proxy_manager.subprocess, "STARTUPINFO", _FakeStartupInfo, create=True
        ), mock.patch.object(
            proxy_manager.subprocess, "STARTF_USESHOWWINDOW", 1, create=True
        ):
            kwargs = proxy_manager._console_kwargs()
        startupinfo = kwargs.get("startupinfo")
        self.assertIsNotNone(startupinfo)
        self.assertEqual(startupinfo.wShowWindow, proxy_manager._SW_SHOWMINNOACTIVE)
        self.assertTrue(startupinfo.dwFlags & 1)
        # Niente CREATE_NO_WINDOW: la console deve esistere, ma minimizzata.
        self.assertNotIn("creationflags", kwargs)


if __name__ == "__main__":
    unittest.main()
