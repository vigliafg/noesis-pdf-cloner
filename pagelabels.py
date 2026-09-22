"""Etichette di pagina (/PageLabels) del PDF — allineato a
``noesis-pdf-cloner-service/app/pagelabels.py``.

Un PDF può numerare le pagine in modo diverso dall'indice fisico (frontespizio
non numerato, numeri romani, prefissi "A-", ecc.). PyMuPDF espone l'elenco
grezzo di intervalli con ``Document.get_page_labels()``; qui lo si espande in
un'etichetta per ogni pagina fisica.

Formato di ciascun intervallo (chiavi PyMuPDF/PDF spec):

``{"startpage": 0, "prefix": "", "style": "D", "firstpagenum": 1}``

Stili: ``D`` decimale, ``R``/``r`` romano, ``A``/``a`` alfabetico.

**Attenzione**: ``style`` è opzionale nello standard. Se la chiave è **assente**
non c'è parte numerica: l'etichetta è **solo il ``prefix``** (es. prefix "70" →
label "70", non "701"). Alcuni PDF usano un intervallo per pagina con il numero
stampato dentro ``prefix``.
"""

from __future__ import annotations

__all__ = ["build_page_labels", "format_number"]

_ROMAN = (
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
    (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
    (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
)


def _to_roman(n: int) -> str:
    if n <= 0:
        return str(n)
    out: list[str] = []
    for value, symbol in _ROMAN:
        while n >= value:
            out.append(symbol)
            n -= value
    return "".join(out)


def _to_alpha(n: int, upper: bool) -> str:
    """1 → 'a', 26 → 'z', 27 → 'aa' (numerazione alfabetica PDF)."""
    if n <= 0:
        return str(n)
    out: list[str] = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out.append(chr((65 if upper else 97) + rem))
    return "".join(reversed(out))


def format_number(n: int, style: str) -> str:
    """Formatta ``n`` secondo lo stile PDF (D/R/r/A/a/'')."""
    if style == "D":
        return str(n)
    if style == "R":
        return _to_roman(n)
    if style == "r":
        return _to_roman(n).lower()
    if style == "A":
        return _to_alpha(n, upper=True)
    if style == "a":
        return _to_alpha(n, upper=False)
    return ""  # stile assente: nessun numero, solo eventuale prefisso


def build_page_labels(spec: list[dict] | None, page_count: int) -> list[str]:
    """Espande gli intervalli ``/PageLabels`` in una lista di etichette.

    Le pagine non coperte da alcun intervallo usano il numero fisico 1-based.
    """
    labels: list[str | None] = [None] * max(page_count, 0)
    if not spec or page_count <= 0:
        return [str(i + 1) for i in range(max(page_count, 0))]

    entries = sorted(
        (e for e in spec if isinstance(e, dict) and "startpage" in e),
        key=lambda e: int(e.get("startpage", 0)),
    )
    for i, entry in enumerate(entries):
        start = int(entry.get("startpage", 0))
        if start < 0 or start >= page_count:
            continue
        end = (
            int(entries[i + 1].get("startpage", page_count)) - 1
            if i + 1 < len(entries)
            else page_count - 1
        )
        end = min(max(end, start), page_count - 1)
        # ``style`` è opzionale: se assente NON c'è parte numerica (solo prefisso).
        style = str(entry.get("style", ""))
        prefix = str(entry.get("prefix", ""))
        first = int(entry.get("firstpagenum", 1))
        for page in range(start, end + 1):
            labels[page] = prefix + format_number(first + (page - start), style)

    return [
        labels[i] if labels[i] is not None else str(i + 1)
        for i in range(page_count)
    ]
