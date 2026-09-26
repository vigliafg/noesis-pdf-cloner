#!/usr/bin/env python3
"""Worker **persistente** del motore (Fase 2, sperimentale).

Mantiene un processo ``.venv2`` caldo tra una pagina e l'altra: import una volta,
patch di ``engine_patch`` una volta, warmup asset una volta. Per ogni job esegue
il percorso di ``pdf2zh_next`` **in-process** (``do_translate_file_async``), che
internamente usa il proprio subprocess per la singola pagina ma eredita dal
worker gli import e le cache già calde.

Protocollo: JSON-lines su un socket TCP locale.

- Avvio: il client passa ``--ready-file`` e ``--log-file``. Appena pronto il
  worker scrive ``<porta>`` nel file di ready e resta in ascolto.
- Job (una riga JSON): ``{"id": N, "argv": ["<split>", "--lang-in", ...]}``.
- Risposta (una riga JSON): ``{"id": N, "rc": 0|1, "error": str|null,
  "log_tail": "..."}``.

Il worker non conosce il livello applicativo (cache, status): è il client a
globbare l'output. Va avviato **solo** dal client, con il Python di ``.venv2``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import multiprocessing
import os
import socket
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("engine_worker")


def _inproc_enabled() -> bool:
    """Traduzione in-process nel worker.

    Default: **solo su Windows**, dove il child multiprocessing usa ``spawn`` e
    riparte freddo a ogni pagina (~15-25s in più). Su Linux/macOS il default
    ``fork`` eredita lo stato caldo, quindi si mantiene (memoria più contenuta).
    Override: ``NOESIS_INPROC=1`` / ``=0``.
    """
    raw = (os.environ.get("NOESIS_INPROC") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return os.name == "nt"


def _load_engine():
    """Importa il motore, applica le patch e fa il warmup degli asset."""
    import engine_patch  # noqa: PLC0415

    applied = engine_patch.apply_all()
    if _inproc_enabled():
        # Su Windows evita il child multiprocessing per pagina (spawn freddo):
        # la traduzione gira nel worker caldo. ~15s in meno per pagina.
        applied[engine_patch.INPROC_TRANSLATE] = (
            engine_patch.apply_inproc_translate()
        )
    log.warning("engine_patch applicate: %s", applied)
    import babeldoc.assets.assets as assets  # noqa: PLC0415
    from pdf2zh_next.config.main import ConfigManager  # noqa: PLC0415
    from pdf2zh_next.high_level import do_translate_file_async  # noqa: PLC0415

    with contextlib.suppress(Exception):
        assets.warmup()
    return ConfigManager, do_translate_file_async


def _flush_logs() -> None:
    for handler in logging.root.handlers:
        with contextlib.suppress(Exception):
            handler.flush()


def _collect_garbage() -> None:
    """Libera la memoria accumulata dalla traduzione in-process."""
    import gc  # noqa: PLC0415

    with contextlib.suppress(Exception):
        gc.collect()


def _log_tail(path: Path | None, limit: int = 8192) -> str:
    if path is None:
        return ""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", "replace")


def _run_one_job(argv: list[str], refs) -> dict:
    """Esegue un job nel processo corrente. Non solleva: ritorna l'esito."""
    ConfigManager, do_translate_file_async = refs
    sys.argv = ["pdf2zh_next", *argv]
    try:
        settings = ConfigManager().initialize_config()
        rc = asyncio.run(do_translate_file_async(settings, ignore_error=False))
        return {"rc": int(rc or 0), "error": None}
    except SystemExit as exc:  # la CLI non deve terminare il worker
        return {"rc": int(exc.code or 0), "error": "SystemExit"}
    except BaseException as exc:  # noqa: BLE001
        log.exception("job fallito")
        return {"rc": 1, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        _flush_logs()
        _collect_garbage()


def _serve(listener: socket.socket, idle_timeout: float, refs, log_path: Path | None) -> int:
    listener.settimeout(idle_timeout)
    conn: socket.socket | None = None
    buffer = b""
    try:
        while True:
            if conn is None:
                try:
                    conn, _addr = listener.accept()
                except TimeoutError:
                    log.warning("idle timeout, esco")
                    return 0
                conn.settimeout(None)
                buffer = b""
            try:
                chunk = conn.recv(65536)
            except OSError:
                chunk = b""
            if not chunk:
                with contextlib.suppress(OSError):
                    conn.close()
                conn = None
                buffer = b""
                continue
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if request.get("cmd") == "shutdown":
                    return 0
                result = _run_one_job(list(request.get("argv") or []), refs)
                result["id"] = request.get("id")
                result["log_tail"] = _log_tail(log_path)
                with contextlib.suppress(OSError):
                    conn.sendall((json.dumps(result) + "\n").encode("utf-8"))
    finally:
        with contextlib.suppress(OSError):
            listener.close()
        if conn is not None:
            with contextlib.suppress(OSError):
                conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--log-file", default="")
    parser.add_argument("--idle-timeout", type=float, default=300.0)
    args = parser.parse_args()

    # Diagnostica/parità Windows: su Linux il default è "fork" e il child
    # eredita gli import caldi del worker. Forzando "spawn" si riproduce il
    # comportamento di Windows (child freddo) per misurare il divario.
    if (os.environ.get("NOESIS_MP_SPAWN") or "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        with contextlib.suppress(RuntimeError):
            multiprocessing.set_start_method("spawn", force=True)
        log.warning("multiprocessing start method forzato a spawn")

    log_path = Path(args.log_file) if args.log_file else None
    refs = _load_engine()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    Path(args.ready_file).write_text(str(port), encoding="utf-8")
    log.warning("worker pronto sulla porta %d", port)
    return _serve(listener, float(args.idle_timeout), refs, log_path)


if __name__ == "__main__":
    raise SystemExit(main())
