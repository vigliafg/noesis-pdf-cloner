"""Patch runtime **sperimentali** del motore ``pdf2zh_next`` / BabelDOC.

Feature sperimentale (default OFF): queste patch non cambiano l'output della
traduzione, ma eliminano lavoro ridondante misurato nel profilo di una pagina
densa (vedi ``.opencode/plan/velocita-traduzione.md``):

- ``font_metadata_cache``: ``babeldoc.assets.assets.get_font_and_metadata``
  ricalcola l'hash SHA3 di tutti i font a **ogni** costruzione di ``FontMapper``
  (7 volte per pagina, ~6 s). I file in cache sono immutabili, quindi il
  risultato può essere memoizzato in sicurezza.
- ``memory_monitor``: il ``MemoryMonitor`` di BabelDOC avvia un thread che
  campiona ``/proc`` via psutil (~2-3 s/pagina). Il picco di memoria non serve
  al desktop, quindi lo si sostituisce con un context manager no-op.

Caratteristiche:
- **idempotente**: applicare due volte non cambia nulla;
- **difensivo**: se ``babeldoc`` non è installato o cambia forma, la patch
  corrispondente viene saltata e lo stato riporta ``False`` senza sollevare.

Il modulo è importabile anche senza BabelDOC (l'app gira in ``.venv`` senza il
motore): ``apply_all()`` ritorna semplicemente tutti ``False``.
"""

from __future__ import annotations

import functools
import logging

log = logging.getLogger("engine_patch")

# Versione dello schema di patch: va cambiata se si modifica il *significato*
# di una patch (non solo il nome), così il marker di cache del desktop resta
# coerente.
PATCH_VERSION = "1"

# Stato di applicazione, popolato la prima volta da ``apply_all()``.
_STATE: dict[str, bool] | None = None

# Nomi stabili delle patch (usati anche nei test e nei log).
FONT_METADATA_CACHE = "font_metadata_cache"
MEMORY_MONITOR = "memory_monitor"


def apply_all() -> dict[str, bool]:
    """Applica tutte le patch e ritorna ``{nome: applicata}``.

    Idempotente: dalla seconda chiamata ritorna lo stato già calcolato. Non
    solleva mai: un fallimento di una singola patch viene loggato e la patch
    risulta ``False`` (il motore continua a funzionare normalmente).
    """
    global _STATE
    if _STATE is not None:
        return dict(_STATE)
    _STATE = {
        FONT_METADATA_CACHE: _patch_font_metadata_cache(),
        MEMORY_MONITOR: _patch_memory_monitor(),
    }
    log.info("engine_patch v%s applicate: %s", PATCH_VERSION, _STATE)
    return dict(_STATE)


def applied() -> dict[str, bool]:
    """Stato corrente (senza applicare): ``{}`` se non ancora applicate."""
    return dict(_STATE or {})


def _patch_font_metadata_cache() -> bool:
    """Memoizza ``assets.get_font_and_metadata`` (hash dei font)."""
    try:
        import babeldoc.assets.assets as assets  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        log.debug("font_metadata_cache: babeldoc non disponibile (%s)", exc)
        return False
    original = getattr(assets, "get_font_and_metadata", None)
    if original is None:
        log.warning("font_metadata_cache: get_font_and_metadata assente")
        return False
    if getattr(original, "_noesis_cached", False):
        return True  # già applicata (idempotenza)
    cached = functools.lru_cache(maxsize=None)(original)
    setattr(cached, "_noesis_cached", True)
    assets.get_font_and_metadata = cached
    return True


def _patch_memory_monitor() -> bool:
    """Sostituisce ``MemoryMonitor`` con un context manager no-op."""
    try:
        import babeldoc.format.pdf.high_level as high_level  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        log.debug("memory_monitor: babeldoc non disponibile (%s)", exc)
        return False
    current = getattr(high_level, "MemoryMonitor", None)
    if current is None:
        log.warning("memory_monitor: MemoryMonitor assente")
        return False
    if getattr(current, "_noesis_stub", False):
        return True  # già applicata

    class _NoopMemoryMonitor:
        """Stub di ``MemoryMonitor``: nessun campionamento, picco a 0."""

        _noesis_stub = True
        peak_memory_usage = 0

        def __enter__(self) -> "_NoopMemoryMonitor":
            return self

        def __exit__(self, *_exc) -> bool:
            return False

    high_level.MemoryMonitor = _NoopMemoryMonitor
    return True
