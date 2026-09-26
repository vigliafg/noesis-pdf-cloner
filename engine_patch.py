"""Patch runtime **sperimentali** del motore ``pdf2zh_next`` / BabelDOC.

- ``font_metadata_cache`` e ``memory_monitor`` (default ON quando il wrapper è
  attivo): eliminano lavoro ridondante (hash dei font, monitor memoria) **senza
  cambiare l'output**.
- ``numeric_lists`` (**opt-in**, env ``NOESIS_NUMERIC_LISTS=1``): fa riconoscere
  a BabelDOC le **liste numerate/alfabetiche** (``1.``, ``a)`` …) come nuovi
  paragrafi. BabelDOC riconosce di default solo i bullet grafici, quindi le
  liste numerate finiscono fuse in un unico paragrafo. Questa patch cambia
  l'output (in meglio) ed è quindi disattivata per default.

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
import os
import re

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
NUMERIC_LISTS = "numeric_lists"

# Riconosce un marcatore di lista a inizio riga: "1." "12)" "a." "b)" …
_LIST_MARKER_RE = re.compile(r"^\s*(?:\d{1,3}[.)]|[A-Za-z][.)])\s+\S")


def _env_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _looks_like_list_marker(text: str) -> bool:
    """True se la riga inizia con un marcatore di lista numerico/alfabetico."""
    return bool(_LIST_MARKER_RE.match(text or ""))


def apply_all() -> dict[str, bool]:
    """Applica le patch e ritorna ``{nome: applicata}``.

    Idempotente: dalla seconda chiamata ritorna lo stato già calcolato. Non
    solleva mai: un fallimento di una singola patch viene loggato e la patch
    risulta ``False`` (il motore continua a funzionare normalmente).
    ``numeric_lists`` è applicata solo se ``NOESIS_NUMERIC_LISTS`` è attiva.
    """
    global _STATE
    if _STATE is not None:
        return dict(_STATE)
    _STATE = {
        FONT_METADATA_CACHE: _patch_font_metadata_cache(),
        MEMORY_MONITOR: _patch_memory_monitor(),
        NUMERIC_LISTS: (
            _patch_numeric_lists()
            if _env_truthy(os.environ.get("NOESIS_NUMERIC_LISTS"))
            else False
        ),
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


def _patch_numeric_lists() -> bool:
    """Fa riconoscere a BabelDOC le liste numerate/alfabetiche.

    Avvolge ``ParagraphFinder.process_independent_paragraphs``: prima di
    lasciar fare a BabelDOC, spezza i paragrafi dove una riga inizia con un
    marcatore di lista (``1.``, ``a)`` …), che BabelDOC di default non
    riconosce (gestisce solo i bullet grafici).
    """
    try:
        from babeldoc.format.pdf.document_il.midend import (  # noqa: PLC0415
            paragraph_finder as pf,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("numeric_lists: babeldoc non disponibile (%s)", exc)
        return False
    cls = getattr(pf, "ParagraphFinder", None)
    if cls is None:
        log.warning("numeric_lists: ParagraphFinder assente")
        return False
    original = getattr(cls, "process_independent_paragraphs", None)
    if original is None:
        log.warning("numeric_lists: process_independent_paragraphs assente")
        return False
    if getattr(original, "_noesis_numeric_lists", False):
        return True  # già applicata

    def _wrapped(self, paragraphs, median_width):  # noqa: ANN001
        try:
            _split_numbered_lists(self, paragraphs, pf)
        except Exception:  # noqa: BLE001 — mai bloccare il motore
            log.debug("numeric_lists: split fallito, proseguo", exc_info=True)
        return original(self, paragraphs, median_width)

    _wrapped._noesis_numeric_lists = True
    cls.process_independent_paragraphs = _wrapped
    return True


def _split_numbered_lists(finder, paragraphs, pf):  # noqa: ANN001
    """Spezza i paragrafi sui marcatori di lista, **mutando la lista in place**.

    BabelDOC muta la lista ``paragraphs`` in place (``insert``): restituire una
    nuova lista la farebbe ignorare al chiamante (i nuovi paragrafi sparirebbero).
    """
    box_cls = pf.Box
    paragraph_cls = pf.PdfParagraph
    new_id = pf.generate_base58_id
    index = 0
    while index < len(paragraphs):
        paragraph = paragraphs[index]
        compositions = paragraph.pdf_paragraph_composition
        if len(compositions) <= 1:
            index += 1
            continue
        cut = None
        for j in range(1, len(compositions)):
            line = getattr(compositions[j], "pdf_line", None)
            if line is None:
                continue
            text = "".join(c.char_unicode for c in line.pdf_character)
            if _looks_like_list_marker(text):
                cut = j
                break
        if cut is None:
            index += 1
            continue
        new_paragraph = paragraph_cls(
            box=box_cls(0, 0, 0, 0),
            pdf_paragraph_composition=compositions[cut:],
            unicode="",
            debug_id=new_id(),
            layout_label=paragraph.layout_label,
            layout_id=paragraph.layout_id,
        )
        paragraph.pdf_paragraph_composition = compositions[:cut]
        finder.update_paragraph_data(paragraph)
        finder.update_paragraph_data(new_paragraph)
        paragraphs.insert(index + 1, new_paragraph)
        index += 1  # processa il nuovo paragrafo (può avere altri marcatori)
    return paragraphs
