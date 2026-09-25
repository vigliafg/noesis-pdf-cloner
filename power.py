"""Gestione della sospensione del sistema durante i job lunghi.

Due responsabilità, entrambe **senza dipendenze da PyQt**:

1. ``SleepInhibitor``: impedisce allo standby automatico di interrompere un
   batch. Usa i meccanismi nativi della piattaforma:
   - Linux: ``systemd-inhibit`` (child ``sleep infinity`` tenuto vivo);
   - Windows: ``SetThreadExecutionState`` (ctypes);
   - macOS: ``caffeinate -i -w <pid>``.

2. Rilevamento del risveglio: ``CHECK_INTERVAL_S`` e ``resume_gap`` permettono
   al chiamante (un ``QTimer`` in ``main``) di accorgersi che il computer è
   andato in standby confrontando il tempo "da parete" con quello monotono.
   ``time.monotonic()`` non avanza durante la sospensione su Windows/Linux,
   mentre ``time.time()`` sì: un salto oltre la soglia indica un risveglio.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import shutil
import subprocess
import sys
import time

log = logging.getLogger("power")


def _death_with_parent():
    """``preexec_fn`` POSIX: il figlio muore se il processo padre termina.

    Evita di lasciare orfano ``systemd-inhibit`` (e quindi lo standby inibito)
    se l'app viene chiusa in modo anomalo.
    """
    def _set() -> None:
        import ctypes

        PR_SET_PDEATHSIG = 1
        with contextlib.suppress(Exception):
            ctypes.CDLL("libc.so.6", use_errno=True).prctl(
                PR_SET_PDEATHSIG, 15  # SIGTERM
            )

    return _set

# Ogni quanto il watchdog deve controllare (secondi).
CHECK_INTERVAL_S = 5.0
# Un salto fra orologio da parete e monotono oltre questa soglia è una sospensione.
RESUME_GAP_S = 60.0


def _no_window_kwargs() -> dict:
    """Evita finestre console su Windows durante i subprocess (app GUI)."""
    if not sys.platform.startswith("win"):
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startupinfo,
    }


class SleepInhibitor:
    """Impedisce lo standby finché è attivo (context manager o acquire/release)."""

    def __init__(self, reason: str = "Noesis PDF Cloner: traduzione in corso"):
        self._reason = reason
        self._child: subprocess.Popen | None = None
        self._windows = False
        self._active = False

    # ── API ────────────────────────────────────────────────────────────────

    def acquire(self) -> bool:
        """Attiva l'inibizione; ritorna True se è stato possibile."""
        if self._active:
            return True
        try:
            if sys.platform.startswith("win"):
                ok = self._acquire_windows()
            elif sys.platform == "darwin":
                ok = self._acquire_macos()
            else:
                ok = self._acquire_linux()
        except Exception:  # noqa: BLE001 — non deve mai bloccare il job
            log.debug("inibizione standby non disponibile", exc_info=True)
            ok = False
        self._active = bool(ok)
        if ok:
            # Rete di sicurezza: rilascia l'inibizione se l'app termina.
            atexit.register(self.release)
            log.info("standby inibito (%s)", self._reason)
        return self._active

    def release(self) -> None:
        """Rilascia l'inibizione (idempotente)."""
        if not self._active and self._child is None and not self._windows:
            return
        self._active = False
        child, self._child = self._child, None
        if child is not None:
            with contextlib.suppress(Exception):
                child.terminate()
            try:
                child.wait(timeout=3)
            except Exception:  # noqa: BLE001
                with contextlib.suppress(Exception):
                    child.kill()
        if self._windows:
            self._release_windows()
        log.info("standby reinserito")

    @property
    def active(self) -> bool:
        return self._active

    def __enter__(self) -> "SleepInhibitor":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    # ── piattaforme ────────────────────────────────────────────────────────

    def _acquire_linux(self) -> bool:
        if shutil.which("systemd-inhibit"):
            self._child = subprocess.Popen(
                [
                    "systemd-inhibit",
                    "--what=idle:sleep",
                    "--mode=block",
                    f"--why={self._reason}",
                    "sleep",
                    "infinity",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                preexec_fn=_death_with_parent() if not sys.platform.startswith("win") else None,
                **_no_window_kwargs(),
            )
            return True
        # Fallback: nessuna inibizione (meglio che rompere l'app).
        return False

    def _acquire_macos(self) -> bool:
        if not shutil.which("caffeinate"):
            return False
        # --w <pid>: caffeinate resta vivo finché il processo indicato esiste.
        self._child = subprocess.Popen(
            ["caffeinate", "-i", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return True

    def _acquire_windows(self) -> bool:
        import ctypes

        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        # ES_AWAYMODE_REQUIRED non è impostato: lo schermo può spegnersi, il
        # sistema resta svegliо (comportamento discreto).
        res = ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        )
        self._windows = bool(res)
        return self._windows

    def _release_windows(self) -> None:
        import ctypes

        ES_CONTINUOUS = 0x80000000
        with contextlib.suppress(Exception):
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        self._windows = False


class ResumeWatch:
    """Rileva un risveglio da standby confrontando orologio da parete e monotono.

    Uso tipico (in ``main``): chiamare ``poll()`` da un ``QTimer``; ritorna il
    numero di secondi "persi" (0.0 se non c'è stata sospensione).
    """

    def __init__(self, threshold_s: float = RESUME_GAP_S):
        self._threshold_s = float(threshold_s)
        self._wall = time.time()
        self._mono = time.monotonic()

    def reset(self) -> None:
        self._wall = time.time()
        self._mono = time.monotonic()

    def poll(self) -> float:
        """Ritorna la durata della sospensione rilevata (0.0 se nessuna)."""
        wall = time.time()
        mono = time.monotonic()
        wall_elapsed = wall - self._wall
        mono_elapsed = mono - self._mono
        self._wall = wall
        self._mono = mono
        gap = wall_elapsed - mono_elapsed
        if gap > self._threshold_s:
            log.info("risveglio da sospensione rilevato (gap %.1f s)", gap)
            return gap
        return 0.0
