"""Archivio della chiave API OpenRouter (per-utente, cross-platform).

Scelta del progetto: **file per-utente** nella cartella dati dell'app
(``%APPDATA%\\noesis-pdf-cloner`` su Windows 11, ``~/.local/share/...`` su
Linux, ``~/Library/Application Support/...`` su macOS), con permessi ``0600``
dove supportati. Non è cifrato: su Windows la cartella profilo è già protetta
per utente; su Linux/macOS i permessi restano ristretti.

La chiave non viene mai scritta in ``config.json``, nei log o nei commit.
All'avvio l'app la carica in ``os.environ[OPENROUTER_API_KEY]``: la chiave
**salvata** dall'utente (Impostazioni) ha la precedenza, la variabile d'ambiente
di sistema resta come *fallback*. Così un'impostazione esplicita nell'app non
viene sovrascritta da una variabile di sistema (spesso non aggiornata).

Modulo **puro Python** (nessuna dipendenza da Qt): testabile senza QApplication.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

__all__ = [
    "KeyStore",
    "ENV_VAR",
    "DEFAULT_BASE_URL",
    "load_into_env",
    "registry_env_key",
]

ENV_VAR = "OPENROUTER_API_KEY"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class KeyStore:
    """Legge/scrive la chiave OpenRouter in un file JSON per-utente."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def get(self) -> str:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return str(data.get("openrouter_api_key", "") or "").strip()

    def has(self) -> bool:
        return bool(self.get())

    def set(self, key: str) -> None:
        key = (key or "").strip()
        if not key:
            self.clear()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps({"openrouter_api_key": key})
        # Scrittura atomica + permessi ristretti (0600) dove supportati.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
        finally:
            with _suppress(OSError):
                os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        with _suppress(OSError):
            os.chmod(self.path, 0o600)

    def clear(self) -> None:
        with _suppress(OSError):
            self.path.unlink()

    @staticmethod
    def mask(key: str) -> str:
        """Versione mascherata per la UI: ``sk-or-…abcd``."""
        key = (key or "").strip()
        if not key:
            return ""
        if len(key) <= 8:
            return "•" * len(key)
        return f"{key[:6]}…{key[-4:]}"

    @staticmethod
    def verify(
        key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 15.0,
    ) -> tuple[bool, str]:
        """Verifica la chiave su OpenRouter.

        Ritorna ``(ok, reason)`` con ``reason`` in ``{"ok", "missing",
        "invalid", "http N", "unreachable"}``.
        """
        key = (key or "").strip()
        if not key:
            return (False, "missing")
        url = base_url.rstrip("/") + "/key"
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {key}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return (response.status == 200, "ok" if response.status == 200
                        else f"http {response.status}")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return (False, "invalid")
            return (False, f"http {exc.code}")
        except Exception:
            return (False, "unreachable")


def load_into_env(path: str | Path) -> str:
    """Carica la chiave efficace in ``os.environ``.

    Precedenza: la chiave **salvata** (file per-utente) vince; se manca si usa
    la variabile d'ambiente ``OPENROUTER_API_KEY``. Ritorna da dove proviene la
    chiave attiva: ``"file"`` (archivio), ``"env"`` (variabile di sistema) o
    ``"none"``.
    """
    key = KeyStore(path).get()
    if key:
        os.environ[ENV_VAR] = key
        return "file"
    if os.environ.get(ENV_VAR):
        return "env"
    return "none"


def _registry_env_key() -> tuple[str, str]:
    """``(valore, ambito)`` di ``OPENROUTER_API_KEY`` nel registro di Windows.

    Serve a distinguere "variabile assente" da "variabile impostata nel sistema
    ma non ancora ereditata da questo processo" (su Windows una variabile creata
    di recente compare solo nei processi avviati dopo l'aggiornamento della
    sessione). ``("", "")`` su sistemi non Windows o se non è presente.
    """
    if os.name != "nt":  # pragma: no cover - dipende dall'OS
        return ("", "")
    try:  # pragma: no cover - solo Windows
        import winreg
    except Exception:  # pragma: no cover
        return ("", "")
    checks = (
        (winreg.HKEY_CURRENT_USER, r"Environment", "user"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            "system",
        ),
    )
    for root, subkey, scope in checks:  # pragma: no cover - solo Windows
        try:
            with winreg.OpenKey(root, subkey) as handle:
                value, _ = winreg.QueryValueEx(handle, ENV_VAR)
        except OSError:
            continue
        value = str(value or "").strip()
        if value:
            return (value, scope)
    return ("", "")


def registry_env_key() -> tuple[str, str]:
    """Espone :func:`_registry_env_key` (monkeypatch nei test)."""
    return _registry_env_key()


class _suppress:
    """``contextlib.suppress`` minimale (evita un import extra a livello top)."""

    def __init__(self, *exceptions):
        self._exceptions = exceptions

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, self._exceptions)
