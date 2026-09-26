"""Client del worker persistente del motore (Fase 2, sperimentale).

Avvia una volta ``engine_worker.py`` con il Python di ``.venv2`` e gli invia i
job via socket TCP locale (JSON-lines). Con la feature attiva ``clone_engine``
usa questo client al posto di un nuovo subprocess per pagina: il costo di import
del motore e di warmup degli asset viene pagato una sola volta.

**Fail-safe**: qualunque errore di avvio/IO riporta l'eccezione al chiamante,
che ricade sul percorso a subprocess. Il worker è un processo figlio in una
sessione separata: viene terminato a ``close()``, alla cancellazione, al timeout
o quando cambia l'ambiente rilevante (es. chiave LLM).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

# Chiavi d'ambiente che, se cambiano, richiedono il riavvio del worker.
_ENV_KEYS = (
    "PDF2ZH_OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "PDF_LLM_MODEL",
    "PDF_LLM_BASE_URL",
    "PDF_LANG_IN",
    "PDF_LANG_OUT",
)


class WorkerError(RuntimeError):
    """Avvio/IO del worker fallito."""


class WorkerCancelled(RuntimeError):
    """Job annullato dall'utente (worker terminato)."""


class WorkerTimeout(RuntimeError):
    """Job oltre il timeout (worker terminato)."""


def _env_fingerprint(env: dict) -> str:
    blob = "\0".join(f"{k}={env.get(k, '')}" for k in _ENV_KEYS)
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()


# SW_SHOWMINNOACTIVE: minimizza la finestra senza attivarla (niente focus).
_SW_SHOWMINNOACTIVE = 7


def minimized_console_kwargs() -> dict:
    """Windows: avvia un processo console con la finestra **ridotta a icona**.

    Serve ``CREATE_NEW_CONSOLE``: lo show-state di ``STARTUPINFO`` è applicato
    alla console **solo quando ne viene creata una nuova**; senza, la finestra
    compare normale. ``SW_SHOWMINNOACTIVE`` evita anche di rubare il focus.
    """
    if os.name != "nt":
        return {}
    kwargs: dict = {}
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    if flags:
        kwargs["creationflags"] = flags
    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is not None:
        startupinfo = startupinfo_cls()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        startupinfo.wShowWindow = _SW_SHOWMINNOACTIVE
        kwargs["startupinfo"] = startupinfo
    return kwargs


def hidden_console_kwargs() -> dict:
    """Windows: nessuna finestra console (per processi interni, es. worker)."""
    if os.name != "nt":
        return {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flags} if flags else {}


class EngineWorkerClient:
    """Wrapper sul worker persistente, con riavvio e fallback."""

    def __init__(
        self,
        python: str,
        script: str | Path,
        work_dir: str | Path,
        *,
        ready_timeout: float = 180.0,
        idle_timeout: float = 600.0,
    ) -> None:
        self.python = python
        self.script = str(script)
        self.work_dir = Path(work_dir)
        self.ready_timeout = float(ready_timeout)
        self.idle_timeout = float(idle_timeout)
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._log_handle = None
        self._fingerprint: str = ""
        self._counter = 0
        self._lock = threading.Lock()

    # ── ciclo di vita ───────────────────────────────────────────────────

    def _log_path(self) -> Path:
        return self.work_dir / "worker.log"

    def _ready_path(self) -> Path:
        return self.work_dir / "worker.ready"

    def _kill(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            with contextlib.suppress(Exception):
                self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                with contextlib.suppress(Exception):
                    self._proc.kill()
        self._proc = None
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
        self._sock = None
        if self._log_handle is not None:
            with contextlib.suppress(OSError):
                self._log_handle.close()
            self._log_handle = None

    def close(self) -> None:
        """Termina il worker (idempotente)."""
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.sendall(b'{"cmd":"shutdown"}\n')
        self._kill()

    def _start_locked(self, env: dict) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        ready = self._ready_path()
        log_path = self._log_path()
        with contextlib.suppress(OSError):
            ready.unlink()
        log_handle = open(log_path, "ab", buffering=0)
        self._log_handle = log_handle
        cmd = [
            self.python,
            self.script,
            "--ready-file", str(ready),
            "--log-file", str(log_path),
            "--idle-timeout", str(int(self.idle_timeout)),
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                cwd=str(Path(self.script).parent),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=(os.name != "nt"),
                **hidden_console_kwargs(),
            )
        except OSError as exc:
            log_handle.close()
            raise WorkerError(f"avvio worker fallito: {exc}") from exc

        port = self._wait_ready_locked(ready)
        self._fingerprint = _env_fingerprint(env)
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        except OSError as exc:
            self._stop_locked()
            raise WorkerError(f"connessione al worker fallita: {exc}") from exc
        self._sock = sock

    def _wait_ready_locked(self, ready: Path) -> int:
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                tail = ""
                with contextlib.suppress(OSError):
                    tail = self._log_path().read_text("utf-8", "replace")[-1000:]
                raise WorkerError(
                    f"worker terminato all'avvio (rc={self._proc.returncode}): {tail}"
                )
            if ready.is_file():
                text = ready.read_text(encoding="utf-8").strip()
                if text.isdigit():
                    return int(text)
            time.sleep(0.1)
        self._kill()
        raise WorkerError("timeout in attesa del worker")

    # ── esecuzione ──────────────────────────────────────────────────────

    def ensure_started(self, env: dict) -> None:
        """Avvia il worker se non attivo o se l'ambiente rilevante è cambiato."""
        with self._lock:
            if self._sock is None or _env_fingerprint(env) != self._fingerprint:
                self._stop_locked()
                self._start_locked(env)

    def run(
        self,
        argv: list[str],
        env: dict,
        *,
        cancel_event: threading.Event | None = None,
        timeout: float = 900.0,
    ) -> subprocess.CompletedProcess:
        """Invia un job e attende il risultato (o solleva)."""
        with self._lock:
            if self._sock is None or _env_fingerprint(env) != self._fingerprint:
                self._stop_locked()
                self._start_locked(env)
            assert self._sock is not None and self._proc is not None
            sock = self._sock
            self._counter += 1
            job_id = self._counter

        request = json.dumps({"id": job_id, "argv": argv}) + "\n"
        with contextlib.suppress(OSError):
            sock.sendall(request.encode("utf-8"))

        buffer = b""
        deadline = time.monotonic() + timeout
        while True:
            if cancel_event is not None and cancel_event.is_set():
                with self._lock:
                    self._stop_locked()
                raise WorkerCancelled("job annullato")
            if time.monotonic() > deadline:
                with self._lock:
                    self._stop_locked()
                raise WorkerTimeout("timeout del worker")
            sock.settimeout(0.5)
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                chunk = b""
            if not chunk:
                with self._lock:
                    self._stop_locked()
                raise WorkerError("worker terminato durante il job")
            buffer += chunk
            if b"\n" not in buffer:
                continue
            line, _rest = buffer.split(b"\n", 1)
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                raise WorkerError(f"risposta worker non valida: {line[:200]!r}")
            if response.get("id") != job_id:
                # risposta di un job precedente: la ignora
                buffer = _rest
                continue
            stderr = response.get("log_tail") or (response.get("error") or "")
            return subprocess.CompletedProcess(
                argv, int(response.get("rc", 1)), "", stderr
            )

    def __del__(self):  # pragma: no cover - best effort
        with contextlib.suppress(Exception):
            self._kill()


def default_work_dir(cache_root: str | Path, tag: str = "") -> Path:
    """Cartella per file di ready/log del worker (nella cache dell'app).

    Con ``tag`` (es. id dell'engine) si usa una sottocartella dedicata: engine
    diversi (pannello + export) non condividono ``worker.ready``/``worker.log``,
    evitando che un client si connetta al worker di un altro.
    """
    path = Path(cache_root) / "_tmp" / "worker"
    if tag:
        path = path / tag
    with contextlib.suppress(OSError):
        path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_work_dir(cache_root: str | Path) -> None:
    with contextlib.suppress(OSError):
        shutil.rmtree(Path(cache_root) / "_tmp" / "worker", ignore_errors=True)
