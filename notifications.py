"""Avvisi di fine lavoro: suono discreto e notifica di sistema.

Il modulo è **puro Python** (nessuna dipendenza da PyQt) per restare testabile
senza GUI. Il suono usa gli strumenti **nativi del sistema operativo**, così
non si aggiungono dipendenze pesanti (QtMultimedia/GStreamer) al bundle:

- Windows: ``winsound.MessageBeep`` (se disponibile) o ``winsound.Beep``;
- macOS: ``afplay`` (nostro WAV, fallback suoni di sistema);
- Linux: ``pw-play``/``paplay``/``aplay``/``canberra-gtk-play``/``ffplay`` col
  **nostro WAV** generato al volo (PipeWire, PulseAudio, ALSA…);
- fallback universale: il "bell" del terminale (``\\a``).

Prima si cercava solo un file audio *freedesktop*: su molte distribuzioni non
esiste (o cambia percorso) e non suonava nulla. Ora il WAV viene **generato da
noi** (due note brevi) e riprodotto col primo player disponibile.

Tutte le chiamate sono silenziose in caso di errore: un computer senza audio
non deve mai far fallire l'app. Il suono parte in un thread daemon per non
bloccare la GUI.
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import wave

log = logging.getLogger("notifications")

# Note del campanello: due note brevi e discrete (A5 → D6), con inviluppo
# per evitare click.
_CHIME_NOTES: tuple[tuple[float, float], ...] = ((880.0, 0.10), (1174.66, 0.14))
_CHIME_RATE = 44100
_CHIME_AMPLITUDE = 0.28

_MACOS_SOUNDS: tuple[str, ...] = (
    "/System/Library/Sounds/Glass.aiff",
    "/System/Library/Sounds/Tink.aiff",
    "/System/Library/Sounds/Pop.aiff",
)

_wav_path: str | None = None
_wav_lock = threading.Lock()


def _spawn(cmd: list[str]) -> bool:
    """Avvia un comando di riproduzione senza attenderlo; True se partito."""
    try:
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return True
    except (OSError, ValueError):
        return False


# ── WAV generato da noi ──────────────────────────────────────────────────────

def build_wav_bytes(
    notes: tuple[tuple[float, float], ...] = _CHIME_NOTES,
    rate: int = _CHIME_RATE,
) -> bytes:
    """Costruisce un WAV mono PCM16 con le note date (frequenza Hz, durata s)."""
    frames = bytearray()
    for freq, dur in notes:
        total = max(1, int(rate * float(dur)))
        fade_in = max(1, int(rate * 0.008))
        fade_out = max(1, int(rate * 0.025))
        for i in range(total):
            env = min(1.0, i / fade_in, (total - i) / fade_out)
            value = int(
                _CHIME_AMPLITUDE * 32767.0 * env
                * math.sin(2.0 * math.pi * float(freq) * i / rate)
            )
            frames += struct.pack("<h", max(-32768, min(32767, value)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(frames))
    return buf.getvalue()


def wav_path() -> str | None:
    """Scrive (una sola volta) il WAV in un file temporaneo e ne ritorna il path."""
    global _wav_path
    if _wav_path and os.path.exists(_wav_path):
        return _wav_path
    with _wav_lock:
        if _wav_path and os.path.exists(_wav_path):
            return _wav_path
        try:
            data = build_wav_bytes()
            digest = hashlib.sha1(data).hexdigest()[:8]
            path = os.path.join(
                tempfile.gettempdir(), f"noesis-pdf-cloner-chime-{digest}.wav"
            )
            if not os.path.exists(path):
                tmp = f"{path}.{os.getpid()}.tmp"
                with open(tmp, "wb") as handle:
                    handle.write(data)
                os.replace(tmp, path)
            _wav_path = path
            return path
        except Exception:  # noqa: BLE001 — l'avviso non deve mai fallire
            log.debug("scrittura WAV campanello fallita", exc_info=True)
            return None


# ── riproduzione per piattaforma ─────────────────────────────────────────────

def _play_windows() -> bool:
    try:
        import winsound  # type: ignore
    except ImportError:
        return False
    try:
        # Il "message beep" è discreto e non richiede file audio.
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
        return True
    except Exception:  # noqa: BLE001
        try:
            winsound.Beep(880, 120)
            return True
        except Exception:  # noqa: BLE001
            return False


def _play_macos() -> bool:
    player = shutil.which("afplay")
    if not player:
        return False
    path = wav_path()
    if path and _spawn([player, path]):
        return True
    for system_sound in _MACOS_SOUNDS:
        if os.path.exists(system_sound) and _spawn([player, system_sound]):
            return True
    return False


def _linux_commands(path: str | None) -> list[list[str]]:
    """Comandi di riproduzione disponibili, in ordine di preferenza."""
    commands: list[list[str]] = []
    if path:
        if shutil.which("pw-play"):
            commands.append(["pw-play", path])
        if shutil.which("paplay"):
            commands.append(["paplay", path])
        if shutil.which("aplay"):
            commands.append(["aplay", "-q", path])
        if shutil.which("ffplay"):
            commands.append(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path]
            )
        if shutil.which("mpv"):
            commands.append(["mpv", "--really-quiet", "--no-video", path])
    # Suoni di sistema "canberra" (GNOME): indicano il nome dell'evento.
    if shutil.which("canberra-gtk-play"):
        commands.append(["canberra-gtk-play", "-i", "complete"])
        if path:
            commands.append(["canberra-gtk-play", "-f", path])
    return commands


def _play_linux() -> bool:
    path = wav_path()
    for cmd in _linux_commands(path):
        if _spawn(cmd):
            log.debug("campanello riprodotto con %s", cmd[0])
            return True
    # Ultima spiaggia: cerca un suono freedesktop con i player disponibili.
    for sound in (
        "/usr/share/sounds/freedesktop/stereo/complete.oga",
        "/usr/share/sounds/freedesktop/stereo/message.oga",
    ):
        if not os.path.exists(sound):
            continue
        for player in ("paplay", "aplay"):
            if shutil.which(player):
                cmd = [player, sound] if player == "paplay" else [player, "-q", sound]
                if _spawn(cmd):
                    return True
    return False


def play_once() -> bool:
    """Riproduce subito il suono d'avviso; ritorna True se qualcosa ha suonato.

    Non solleva mai: in assenza di audio ricade sul bell del terminale (che può
    essere silenzioso, ma non rompe nulla).
    """
    try:
        if sys.platform.startswith("win"):
            ok = _play_windows()
        elif sys.platform == "darwin":
            ok = _play_macos()
        else:
            ok = _play_linux()
        if ok:
            return True
    except Exception:  # noqa: BLE001 — l'avviso non deve mai far fallire il job
        log.debug("riproduzione suono nativa fallita", exc_info=True)
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    return False


def chime(enabled: bool = True) -> None:
    """Riproduce il suono d'avviso in background (no-op se disabilitato)."""
    if not enabled:
        return
    threading.Thread(target=play_once, daemon=True).start()


def available_player() -> str | None:
    """Nome del primo player audio trovato (per diagnostica/Impostazioni)."""
    names = [
        "pw-play", "paplay", "aplay", "ffplay", "mpv",
        "afplay", "canberra-gtk-play",
    ]
    if sys.platform.startswith("win"):
        return "winsound"
    for name in names:
        if shutil.which(name):
            return name
    return None
