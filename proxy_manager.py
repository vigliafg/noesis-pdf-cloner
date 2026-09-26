"""Gestione del proxy provider locale (avvio/stop automatico).

Avvia ``tools/provider_proxy.py`` come processo figlio dell'app e ne attende la
disponibilità. Idempotente: se il proxy risponde già su ``/healthz`` non ne
avvia un secondo. Pensato per il desktop; il proxy usa solo la stdlib.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PORT = 8790
DEFAULT_PROVIDER = "groq"
DEFAULT_MODELS = "openai/gpt-oss-120b"


def _proxy_script() -> Path:
    """Percorso di ``provider_proxy.py`` (sorgente o bundle PyInstaller)."""
    here = Path(__file__).resolve().parent / "tools" / "provider_proxy.py"
    if here.is_file():
        return here
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "tools" / "provider_proxy.py"
        if bundled.is_file():
            return bundled
    return here


def _proxy_python() -> str:
    """Interprete Python per il proxy.

    In una build **congelata** ``sys.executable`` è l'eseguibile dell'app, non
    Python: usarlo rilancierebbe la GUI e il proxy non partirebbe. Si usa quindi
    il Python del venv del motore (``.venv2``), che basta (il proxy è stdlib-only).
    """
    if not getattr(sys, "frozen", False):
        return sys.executable
    with contextlib.suppress(Exception):
        import clone_engine  # noqa: PLC0415

        binary = clone_engine.find_pdf2zh_bin()
        if binary is not None:
            candidate = Path(clone_engine.venv_python_for(binary))
            if candidate.is_file():
                return str(candidate)
    with contextlib.suppress(Exception):
        import clone_engine  # noqa: PLC0415

        for venv in (
            clone_engine.engine_venv_dir(),
            clone_engine.engine_venv_dir(clone_engine.user_engine_base()),
        ):
            candidate = clone_engine.engine_venv_python(venv)
            if candidate.is_file():
                return str(candidate)
    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if found:
            return found
    return sys.executable


# SW_SHOWMINNOACTIVE: mostra la finestra ridotta a icona senza attivarla.
_SW_SHOWMINNOACTIVE = 7


def _console_kwargs() -> dict:
    """Su Windows avvia il proxy con la console **ridotta a icona**.

    Delega a ``engine_client.minimized_console_kwargs`` (che aggiunge
    ``CREATE_NEW_CONSOLE``: senza, lo show-state non viene applicato alla nuova
    console e la finestra compare normale). Fallback locale se il modulo manca.
    """
    if os.name != "nt":
        return {}
    try:
        from engine_client import minimized_console_kwargs  # noqa: PLC0415

        return minimized_console_kwargs()
    except Exception:  # noqa: BLE001
        # Fallback: stessa logica, senza dipendere da engine_client.
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


def _health_ok(port: int, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz", timeout=timeout
        ) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


class ProxyManager:
    """Avvia e ferma il proxy provider locale (un processo per volta)."""

    def __init__(self, log_dir: str | Path | None = None) -> None:
        self._proc: subprocess.Popen | None = None
        self._port: int = 0
        self._log_dir = Path(log_dir) if log_dir else None
        self._log_handle = None
        self._lock = threading.Lock()

    def is_running(self, port: int | None = None) -> bool:
        return _health_ok(port or self._port or DEFAULT_PORT)

    def ensure_started(
        self,
        port: int = DEFAULT_PORT,
        provider: str = DEFAULT_PROVIDER,
        models: str = DEFAULT_MODELS,
        timeout: float = 25.0,
    ) -> bool:
        """Avvia il proxy se non è già attivo. Ritorna True se disponibile."""
        port = int(port or DEFAULT_PORT)
        with self._lock:
            if _health_ok(port):
                return True  # già attivo (avviato da noi o manualmente)
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
            script = _proxy_script()
            if not script.is_file():
                return False
            env = dict(os.environ)
            env["PROXY_PORT"] = str(port)
            env["PROXY_PROVIDER"] = provider
            env["PROXY_MODELS"] = models
            log_path = None
            if self._log_dir is not None:
                with contextlib.suppress(OSError):
                    self._log_dir.mkdir(parents=True, exist_ok=True)
                log_path = self._log_dir / "proxy.log"
            stdout = None
            if log_path is not None:
                self._log_handle = open(log_path, "ab", buffering=0)
                stdout = self._log_handle
            try:
                self._proc = subprocess.Popen(
                    [_proxy_python(), str(script)],
                    cwd=str(script.parent),
                    stdout=stdout,
                    stderr=subprocess.STDOUT,
                    env=env,
                    start_new_session=(os.name != "nt"),
                    **_console_kwargs(),
                )
            except OSError:
                return False
            self._port = port
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    return False
                if _health_ok(port, timeout=1.0):
                    return True
                time.sleep(0.2)
            return False

    def stop(self) -> None:
        """Termina il proxy avviato da questa istanza (idempotente)."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                with contextlib.suppress(Exception):
                    self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    with contextlib.suppress(Exception):
                        self._proc.kill()
            self._proc = None
            if self._log_handle is not None:
                with contextlib.suppress(OSError):
                    self._log_handle.close()
                self._log_handle = None


def probe(
    base_url: str,
    model: str,
    api_key: str,
    timeout: float = 60.0,
) -> dict:
    """Piccola richiesta di prova: ritorna provider/modello o l'errore.

    Utile per verificare il routing (es. Groq) dal pulsante "Prova provider".
    """
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
        "max_tokens": 5,
    }).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        return {
            "ok": True,
            "provider": data.get("provider"),
            "model": data.get("model"),
        }
    except urllib.error.HTTPError as exc:
        detail = ""
        with contextlib.suppress(Exception):
            body = json.loads(exc.read().decode("utf-8", "replace"))
            detail = str(body.get("error", {}).get("message", ""))
        return {"ok": False, "error": f"HTTP {exc.code}: {detail}".strip()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
