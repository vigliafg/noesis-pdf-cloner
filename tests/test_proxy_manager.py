"""Tests per ``proxy_manager`` (avvio proxy + probe provider)."""

import io
import json
import os
import sys
import unittest
import urllib.error
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


if __name__ == "__main__":
    unittest.main()
