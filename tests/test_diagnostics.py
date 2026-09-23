"""Test della diagnostica di preflight (diagnostics.py) con situazioni simulate.

Nessuna rete esterna e nessun motore reale: un piccolo server HTTP locale
simula OpenRouter (chiave valida/invalida/credito esaurito, modello ok/402/429/
404/timeout) e dei binari finti simulano ``uv`` e ``pdf2zh_next``.
"""

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import diagnostics as diag  # noqa: E402


class _FakeOpenRouter(BaseHTTPRequestHandler):
    """Simula gli endpoint ``/key`` e ``/chat/completions`` di OpenRouter."""

    def log_message(self, *args):  # silenzia il server
        pass

    def _token(self) -> str:
        auth = self.headers.get("Authorization", "")
        return auth.replace("Bearer", "").strip()

    def _send(self, status: int, payload: dict | None = None):
        body = json.dumps(payload or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path != "/key":
            return self._send(404, {"error": "not found"})
        token = self._token()
        if token == "good":
            return self._send(200, {"data": {
                "limit": 10, "usage": 1.2, "limit_remaining": 8.8,
                "is_free_tier": False,
            }})
        if token == "low":
            return self._send(200, {"data": {
                "limit": 5, "usage": 5, "limit_remaining": 0,
            }})
        if token == "slow":
            time.sleep(2.0)
            return self._send(200, {"data": {}})
        return self._send(401, {"error": {"message": "No auth credentials found"}})

    def do_POST(self):  # noqa: N802
        if self.path != "/chat/completions":
            return self._send(404, {"error": "not found"})
        if self._token() != "good":
            return self._send(401, {"error": {"message": "Invalid API key"}})
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            model = json.loads(self.rfile.read(length) or b"{}").get("model", "")
        except Exception:
            model = ""
        if model == "model/ok":
            return self._send(200, {"choices": [
                {"message": {"content": "pong"}}
            ]})
        if model == "model/paid":
            return self._send(402, {"error": {"message": "Insufficient credits"}})
        if model == "model/busy":
            return self._send(429, {"error": {"message": "Rate limit exceeded"}})
        if model == "model/slow":
            time.sleep(2.0)
            return self._send(200, {"choices": [{"message": {"content": "pong"}}]})
        return self._send(404, {"error": {"message": "model not found"}})


def _make_exec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _ServerMixin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOpenRouter)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()


class EngineChecksTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_engine_missing(self):
        r = diag.check_engine_bin(None)
        self.assertEqual(r.status, diag.Status.FAIL)
        self.assertEqual(r.code, "engine_missing")
        self.assertEqual(r.fix, "install_engine")

    def test_engine_present(self):
        fake = _make_exec(self.tmp / "pdf2zh_next", "#!/bin/sh\nexit 0\n")
        r = diag.check_engine_bin(fake)
        self.assertEqual(r.status, diag.Status.OK)

    def test_engine_run_ok(self):
        fake = _make_exec(self.tmp / "pdf2zh_next", "#!/bin/sh\necho usage\nexit 0\n")
        self.assertEqual(diag.check_engine_run(fake).status, diag.Status.OK)

    def test_engine_run_broken(self):
        fake = _make_exec(self.tmp / "pdf2zh_next", "#!/bin/sh\nexit 3\n")
        r = diag.check_engine_run(fake)
        self.assertEqual(r.status, diag.Status.WARN)
        self.assertEqual(r.code, "engine_not_runnable")

    def test_engine_run_skips_without_bin(self):
        self.assertEqual(diag.check_engine_run(None).status, diag.Status.SKIP)

    def test_uv_ok(self):
        fake = _make_exec(self.tmp / "uv", "#!/bin/sh\necho 'uv 0.12.17'\n")
        r = diag.check_uv(fake)
        self.assertEqual(r.status, diag.Status.OK)
        self.assertIn("uv 0.12.17", r.data.get("version", ""))

    def test_uv_missing(self):
        r = diag.check_uv(None)
        self.assertEqual(r.status, diag.Status.WARN)
        self.assertEqual(r.code, "uv_missing")

    def test_uv_not_runnable(self):
        fake = _make_exec(self.tmp / "uv", "#!/bin/sh\nexit 1\n")
        self.assertEqual(diag.check_uv(fake).status, diag.Status.WARN)

    def test_data_dir_writable(self):
        r = diag.check_data_dir(self.tmp / "appdata")
        self.assertEqual(r.status, diag.Status.OK)

    @unittest.skipIf(os.geteuid() == 0, "root ignora i permessi")
    def test_data_dir_unwritable(self):
        blocked = self.tmp / "blocked"
        blocked.mkdir()
        blocked.chmod(0o500)
        try:
            r = diag.check_data_dir(blocked / "sub")
            self.assertEqual(r.status, diag.Status.FAIL)
            self.assertEqual(r.code, "data_unwritable")
        finally:
            blocked.chmod(0o700)

    def test_disk_low(self):
        r = diag.check_disk(self.tmp, required_bytes=10**18)
        self.assertEqual(r.status, diag.Status.WARN)
        self.assertEqual(r.code, "disk_low")

    def test_pymupdf_available(self):
        self.assertEqual(diag.check_pymupdf().status, diag.Status.OK)


class KeyAndModelTests(_ServerMixin):
    def test_reachable(self):
        self.assertEqual(
            diag.check_openrouter_reachable(self.base_url).status, diag.Status.OK
        )

    def test_unreachable(self):
        r = diag.check_openrouter_reachable("http://127.0.0.1:1")
        self.assertEqual(r.status, diag.Status.FAIL)
        self.assertEqual(r.code, "network")

    def test_key_present_missing(self):
        r = diag.check_key_present("", "none")
        self.assertEqual(r.code, "key_missing")
        self.assertEqual(r.fix, "enter_key")

    def test_key_present_from_file_masked(self):
        r = diag.check_key_present("sk-or-abcdefgh1234", "file")
        self.assertEqual(r.status, diag.Status.OK)
        self.assertEqual(r.code, "key_file")
        self.assertNotIn("abcdefgh1234", r.data["masked"])

    def test_key_valid_ok(self):
        r = diag.check_key_valid("good", self.base_url)
        self.assertEqual(r.status, diag.Status.OK)

    def test_key_valid_invalid(self):
        r = diag.check_key_valid("bad", self.base_url)
        self.assertEqual(r.status, diag.Status.FAIL)
        self.assertEqual(r.code, "invalid_key")
        self.assertEqual(r.fix, "enter_key")

    def test_key_valid_skip_when_empty(self):
        self.assertEqual(
            diag.check_key_valid("", self.base_url).status, diag.Status.SKIP
        )

    def test_key_valid_network(self):
        r = diag.check_key_valid("good", "http://127.0.0.1:1")
        self.assertEqual(r.status, diag.Status.WARN)
        self.assertEqual(r.code, "network")

    def test_key_credits_ok(self):
        r = diag.check_key_credits("good", self.base_url)
        self.assertEqual(r.status, diag.Status.OK)
        self.assertEqual(r.data["limit_remaining"], 8.8)

    def test_key_credits_low(self):
        r = diag.check_key_credits("low", self.base_url)
        self.assertEqual(r.status, diag.Status.WARN)
        self.assertEqual(r.code, "credits_low")

    def test_model_ok(self):
        r = diag.check_llm_model("good", "model/ok", self.base_url)
        self.assertEqual(r.status, diag.Status.OK)

    def test_model_invalid_key(self):
        r = diag.check_llm_model("bad", "model/ok", self.base_url)
        self.assertEqual(r.status, diag.Status.FAIL)
        self.assertEqual(r.code, "invalid_key")

    def test_model_no_credits(self):
        r = diag.check_llm_model("good", "model/paid", self.base_url)
        self.assertEqual(r.status, diag.Status.FAIL)
        self.assertEqual(r.code, "no_credits")
        self.assertEqual(r.fix, "add_credits")

    def test_model_rate_limited(self):
        r = diag.check_llm_model("good", "model/busy", self.base_url)
        self.assertEqual(r.status, diag.Status.WARN)
        self.assertEqual(r.code, "rate_limited")

    def test_model_not_found(self):
        r = diag.check_llm_model("good", "model/unknown", self.base_url)
        self.assertEqual(r.status, diag.Status.FAIL)
        self.assertEqual(r.code, "model_not_found")

    def test_model_timeout(self):
        r = diag.check_llm_model("good", "model/slow", self.base_url, timeout=0.5)
        self.assertEqual(r.status, diag.Status.WARN)
        self.assertEqual(r.code, "network")

    def test_model_skip_without_key(self):
        self.assertEqual(
            diag.check_llm_model("", "model/ok", self.base_url).status,
            diag.Status.SKIP,
        )


class FreeChainTests(unittest.TestCase):
    def test_free_chain_ok(self):
        r = diag.check_free_chain(translator=lambda t: "ciao")
        self.assertEqual(r.status, diag.Status.OK)

    def test_free_chain_unavailable(self):
        r = diag.check_free_chain(translator=lambda t: None)
        self.assertEqual(r.status, diag.Status.WARN)
        self.assertEqual(r.code, "free_unavailable")

    def test_free_chain_exception(self):
        def boom(_):
            raise RuntimeError("rete")

        self.assertEqual(
            diag.check_free_chain(translator=boom).status, diag.Status.WARN
        )


class RunnerTests(_ServerMixin):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _ctx(self, **over):
        engine = _make_exec(self.tmp / "pdf2zh_next", "#!/bin/sh\necho usage\n")
        uv = _make_exec(self.tmp / "uv", "#!/bin/sh\necho 'uv 0.12.17'\n")
        base = dict(
            data_dir=self.tmp / "appdata",
            uv_bin=uv,
            engine_bin=engine,
            key="good",
            key_source="file",
            base_url=self.base_url,
            model="model/ok",
            engine_name="llm",
            free_translator=lambda t: "ciao",
        )
        base.update(over)
        return diag.DiagnosticsContext(**base)

    def test_run_all_ok_and_callbacks(self):
        seen = []
        results = diag.run_all(self._ctx(), on_result=seen.append)
        ids = [r.id for r in results]
        for expected in ("engine.bin", "key.valid", "llm.model", "free.chain"):
            self.assertIn(expected, ids)
        self.assertEqual(len(seen), len(results))
        self.assertEqual(diag.overall_status(results), diag.Status.OK)

    def test_run_all_engine_missing_is_fail(self):
        results = diag.run_all(self._ctx(engine_bin=None))
        self.assertEqual(diag.overall_status(results), diag.Status.FAIL)

    def test_run_all_wrong_key_is_fail(self):
        results = diag.run_all(self._ctx(key="bad"))
        by_id = {r.id: r for r in results}
        self.assertEqual(by_id["key.valid"].code, "invalid_key")
        self.assertEqual(diag.overall_status(results), diag.Status.FAIL)

    def test_run_only_subset(self):
        results = diag.run_all(self._ctx(), only=["engine.bin", "uv"])
        # ``only`` è un filtro: l'ordine resta quello canonico dei controlli.
        self.assertEqual({r.id for r in results}, {"engine.bin", "uv"})

    def test_run_cancel_stops(self):
        calls = {"n": 0}

        def cancel():
            calls["n"] += 1
            return calls["n"] > 1

        results = diag.run_all(self._ctx(), cancel=cancel)
        self.assertLess(len(results), 6)

    def test_free_checks_toggle_off(self):
        results = diag.run_all(self._ctx(want_free_checks=False))
        self.assertNotIn("free.chain", [r.id for r in results])


if __name__ == "__main__":
    unittest.main()
