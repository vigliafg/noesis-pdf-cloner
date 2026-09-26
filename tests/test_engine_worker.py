"""Tests del client del worker persistente (``engine_client``).

Usa un worker fittizio (script Python che parla lo stesso protocollo JSON-lines):
nessuna dipendenza da ``pdf2zh_next``.
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import engine_client  # noqa: E402

_FAKE_WORKER = '''\
import json
import socket
import sys
import time


def _arg(name):
    args = sys.argv[1:]
    return args[args.index(name) + 1]


ready = _arg("--ready-file")
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", 0))
server.listen(1)
Path = __import__("pathlib").Path
Path(ready).write_text(str(server.getsockname()[1]))
conn, _ = server.accept()
buf = b""
while True:
    chunk = conn.recv(4096)
    if not chunk:
        break
    buf += chunk
    while b"\\n" in buf:
        line, buf = buf.split(b"\\n", 1)
        if not line.strip():
            continue
        req = json.loads(line)
        if req.get("cmd") == "shutdown":
            sys.exit(0)
        argv = req.get("argv") or []
        mode = argv[0] if argv else "ok"
        if mode == "sleep":
            time.sleep(60)
        if mode == "die":
            sys.exit(3)
        conn.sendall((json.dumps({
            "id": req.get("id"), "rc": 0, "error": None, "log_tail": "log"
        }) + "\\n").encode())
'''

_DEAD_WORKER = "import sys\nsys.exit(2)\n"


class WorkerClientTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.script = self.tmp / "fake_worker.py"
        self.script.write_text(_FAKE_WORKER)
        self.client = None

    def tearDown(self):
        if self.client is not None:
            self.client.close()
        self._tmp.cleanup()

    def _client(self, timeout=15.0):
        self.client = engine_client.EngineWorkerClient(
            sys.executable,
            self.script,
            self.tmp / "wd",
            ready_timeout=timeout,
        )
        return self.client

    def test_run_returns_completed_process(self):
        client = self._client()
        result = client.run(["ok"], dict(os.environ))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "log")

    def test_two_jobs_reuse_same_worker(self):
        client = self._client()
        client.run(["ok"], dict(os.environ))
        first_pid = client._proc.pid
        client.run(["ok"], dict(os.environ))
        self.assertEqual(client._proc.pid, first_pid)

    def test_cancel_raises_and_stops_worker(self):
        client = self._client()
        cancel = threading.Event()
        timer = threading.Timer(0.6, cancel.set)
        timer.start()
        try:
            with self.assertRaises(engine_client.WorkerCancelled):
                client.run(["sleep"], dict(os.environ), cancel_event=cancel)
        finally:
            timer.cancel()

    def test_worker_death_raises_worker_error(self):
        client = self._client()
        with self.assertRaises(engine_client.WorkerError):
            client.run(["die"], dict(os.environ))

    def test_dead_worker_at_startup_raises(self):
        dead = self.tmp / "dead_worker.py"
        dead.write_text(_DEAD_WORKER)
        client = engine_client.EngineWorkerClient(
            sys.executable, dead, self.tmp / "wd2", ready_timeout=10
        )
        self.client = client
        with self.assertRaises(engine_client.WorkerError):
            client.run(["ok"], dict(os.environ))

    def test_env_change_restarts_worker(self):
        client = self._client()
        env = dict(os.environ)
        client.run(["ok"], env)
        first_pid = client._proc.pid
        env2 = dict(env)
        env2["PDF_LLM_MODEL"] = "altro/modello"
        client.run(["ok"], env2)
        self.assertNotEqual(client._proc.pid, first_pid)


class WorkDirTests(unittest.TestCase):
    """Engine diversi non devono condividere ``worker.ready``/``worker.log``."""

    def test_tag_creates_isolated_subdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = engine_client.default_work_dir(tmp)
            a = engine_client.default_work_dir(tmp, "engine-a")
            b = engine_client.default_work_dir(tmp, "engine-b")
            self.assertTrue(a.is_dir() and b.is_dir())
            self.assertEqual(a.parent, base)
            self.assertNotEqual(a, b)

    def test_cleanup_removes_tagged_subdirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine_client.default_work_dir(tmp, "engine-a")
            engine_client.cleanup_work_dir(tmp)
            self.assertFalse((Path(tmp) / "_tmp" / "worker").exists())

    def test_clone_engines_get_distinct_worker_dirs(self):
        import clone_engine  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            first = clone_engine.CloneEngine(tmp)
            second = clone_engine.CloneEngine(tmp)
            ca = first._get_worker_client(Path(sys.executable))
            cb = second._get_worker_client(Path(sys.executable))
            self.assertNotEqual(ca.work_dir, cb.work_dir)


class ConsoleKwargsTests(unittest.TestCase):
    """Su Windows le console dei processi interni non devono disturbare."""

    def test_non_windows_returns_empty(self):
        with mock.patch.object(engine_client.os, "name", "posix"):
            self.assertEqual(engine_client.minimized_console_kwargs(), {})
            self.assertEqual(engine_client.hidden_console_kwargs(), {})

    def test_minimized_console_on_windows(self):
        class _StartupInfo:
            def __init__(self):
                self.dwFlags = 0
                self.wShowWindow = None

        with mock.patch.object(engine_client.os, "name", "nt"), mock.patch.object(
            engine_client.subprocess, "STARTUPINFO", _StartupInfo, create=True
        ), mock.patch.object(
            engine_client.subprocess, "STARTF_USESHOWWINDOW", 1, create=True
        ), mock.patch.object(
            engine_client.subprocess, "CREATE_NEW_CONSOLE", 0x10, create=True
        ):
            kwargs = engine_client.minimized_console_kwargs()
        # CREATE_NEW_CONSOLE rende effettivo lo show-state della nuova console.
        self.assertEqual(kwargs["creationflags"], 0x10)
        self.assertEqual(kwargs["startupinfo"].wShowWindow, 7)

    def test_hidden_console_on_windows(self):
        with mock.patch.object(engine_client.os, "name", "nt"), mock.patch.object(
            engine_client.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True
        ):
            kwargs = engine_client.hidden_console_kwargs()
        self.assertEqual(kwargs["creationflags"], 0x08000000)


if __name__ == "__main__":
    unittest.main()
