"""Parsing della specifica pagine scelta dall'utente.

Formati accettati (1-based, come li vede l'utente):

- ``all``                     → tutte le pagine
- ``7``                       → la pagina 7
- ``100-103``                 → l'intervallo 100..103 (estremi invertiti ok)
- ``3,5,10-12``               → lista mista
- ``1, 3 , 5-7``              → spazi ignorati

La funzione ritorna indici **0-based**, ordinati e senza duplicati: è la
convenzione usata dal motore (vedi la lezione del bug off-by-one del desktop).

Questo modulo è l'adattamento desktop di ``app/pages.py`` del repository
``noesis-pdf-cloner-service``: la semantica (formati, estremi, ordinamento,
unicità) deve restare identica tra le due versioni. L'unica aggiunta è
``PageSpecError`` con un ``code`` stabile, usato dalla UI per tradurre il
messaggio d'errore; resta una sottoclasse di ``ValueError`` per compatibilità.
"""

from __future__ import annotations

__all__ = [
    "PageSpecError",
    "parse_pages",
    "format_pages_label",
    "describe_pages",
]

_MAX_PAGES = 100_000


class PageSpecError(ValueError):
    """Specifica pagine non valida.

    ``code`` è un identificatore stabile pensato per la UI (i18n); ``params``
    contiene i valori da interpolare nel messaggio tradotto. Essendo una
    sottoclasse di ``ValueError``, chi cattura ``ValueError`` continua a
    funzionare (allineato al servizio).
    """

    def __init__(self, code: str, message: str, **params):
        super().__init__(message)
        self.code = code
        self.params = params


def _parse_token(token: str, page_count: int) -> list[int]:
    token = token.strip()
    if not token:
        raise PageSpecError("empty", "specifica pagine vuota")
    if "-" in token:
        left, _, right = token.partition("-")
        try:
            start = int(left.strip())
            end = int(right.strip())
        except ValueError as exc:
            raise PageSpecError(
                "range", f"intervallo non valido: '{token}'", token=token
            ) from exc
        if start > end:
            start, end = end, start
        _check_bound(start, page_count)
        _check_bound(end, page_count)
        return list(range(start - 1, end))
    try:
        page = int(token)
    except ValueError as exc:
        raise PageSpecError(
            "token", f"numero di pagina non valido: '{token}'", token=token
        ) from exc
    _check_bound(page, page_count)
    return [page - 1]


def _check_bound(page: int, page_count: int) -> None:
    if page < 1:
        raise PageSpecError(
            "bounds", "le pagine partono da 1", page=page, total=page_count
        )
    if page_count and page > page_count:
        raise PageSpecError(
            "bounds",
            f"pagina {page} oltre il totale ({page_count})",
            page=page,
            total=page_count,
        )


def parse_pages(spec: str | None, page_count: int) -> list[int]:
    """Converte la specifica utente in indici 0-based ordinati e unici.

    Solleva ``PageSpecError`` per specifiche non valide o fuori intervallo.
    """
    if page_count <= 0:
        raise PageSpecError("no_pages", "il documento non contiene pagine")
    text = (spec or "all").strip().lower()
    if text in {"", "all", "*"}:
        return list(range(page_count))
    pages: set[int] = set()
    for chunk in text.split(","):
        pages.update(_parse_token(chunk, page_count))
    if not pages:
        raise PageSpecError("none", "nessuna pagina selezionata")
    if len(pages) > _MAX_PAGES:
        raise PageSpecError("too_many", "troppe pagine richieste")
    return sorted(pages)


def _format_run(start: int, end: int) -> str:
    """Un run contiguo 0-based → "N" oppure "a-b" (1-based)."""
    if start == end:
        return str(start + 1)
    return f"{start + 1}-{end + 1}"


def format_pages_label(pages: list[int]) -> str:
    """Etichetta compatta per il nome file: "156", "100-103", "1,3,7-9".

    Comprime ogni sequenza contigua (non solo l'intero elenco), così una
    selezione mista resta leggibile.
    """
    if not pages:
        return ""
    ordered = sorted(set(pages))
    parts: list[str] = []
    start = prev = ordered[0]
    for page in ordered[1:]:
        if page == prev + 1:
            prev = page
            continue
        parts.append(_format_run(start, prev))
        start = prev = page
    parts.append(_format_run(start, prev))
    return ",".join(parts)


def describe_pages(pages: list[int]) -> str:
    """Descrizione leggibile per il log/anteprima."""
    if not pages:
        return "nessuna pagina"
    if len(pages) == 1:
        return f"pagina {pages[0] + 1}"
    return f"{len(pages)} pagine ({format_pages_label(pages)})"
