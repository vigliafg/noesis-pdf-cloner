"""OCR delle pagine scansionate: Mistral OCR (OpenRouter) + livello testo.

Una pagina **senza testo** non è traducibile da BabelDOC (fallisce con *"The
document contains no paragraphs"*). Qui si ricostruisce il testo e lo si
riporta sulla scansione, così il motore la tratta come una pagina nativa:

1. la pagina (PDF di una pagina) viene inviata a **Mistral OCR** tramite il
   plugin ``file-parser`` di OpenRouter, che restituisce il testo in Markdown;
2. **Tesseract** (locale, offline) fornisce le posizioni: righe con bounding
   box e corpo del testo. Il testo Mistral viene distribuito su quelle righe
   (assegnazione per contenuto, non per ordine: Tesseract legge le colonne in
   ordine intrecciato);
3. si scrive un **livello testo invisibile** (``render_mode=3``) nelle aree
   delle righe: la scansione originale resta e BabelDOC può tradurre. Con
   ``--ocr-workaround`` il tradotto copre il testo originale con un fondo
   bianco;
4. se Tesseract non è disponibile o l'allineamento copre troppo poco testo si
   ripiega su una **pagina nativa** ricostruita dal Markdown (nessuna
   impaginazione, ma il contenuto è tradotto).

Modulo **puro Python** (nessuna dipendenza da Qt), testabile senza rete né
Tesseract: ``ocr_pdf_page`` accetta un ``post`` iniettabile e
``build_text_layer`` accetta le righe già estratte.

Allineato a ``noesis-pdf-cloner-service/app/ocr_scans.py``.
"""

from __future__ import annotations

import base64
import contextlib
import difflib
import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("ocr_scans")

# Versione dello schema OCR: un cambiamento del livello testo (o del formato
# della cache) invalida i PDF OCR già prodotti.
OCR_SCHEMA_VERSION = "1"

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
# Modello "di appoggio" per la richiesta: il testo arriva dalle annotazioni del
# file-parser, non dalla risposta. Deve solo essere un modello testuale
# economico e disponibile (il modello del preset LLM non serve qui).
DEFAULT_MODEL = "google/gemini-2.5-flash-lite"
DEFAULT_ENGINE = "mistral-ocr"
OCR_TIMEOUT = 180.0
# Prezzo Mistral OCR via OpenRouter ($2 / 1000 pagine): solo per la stima
# mostrata nell'avviso. I token del modello di appoggio sono trascurabili.
COST_PER_PAGE_USD = 0.002
# Oltre questa dimensione la pagina viene ricompressa prima dell'invio.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
# render_mode PDF per il testo invisibile (3 = né fill né stroke).
_INVISIBLE = 3
_TEXT_DPI = 300

# App language code → Tesseract traineddata (stessi 14 lingua del bundle).
_TESS_LANG_CODES = {
    "en": "eng", "it": "ita", "fr": "fra", "de": "deu", "es": "spa",
    "pt": "por", "nl": "nld", "pl": "pol", "ru": "rus", "zh": "chi_sim",
    "ja": "jpn", "ko": "kor", "ar": "ara", "tr": "tur",
}
# Sorgente "auto": combinazione latina di base (CJK escluso: peggiora la
# qualità, come nella UI del lettore).
_AUTO_TESS_LANGS = "eng+ita+fra+deu+spa+por"


class OcrError(RuntimeError):
    """Errore OCR con un codice stabile per la UI."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class OcrConfig:
    """Configurazione del provider OCR (OpenRouter + Mistral OCR)."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    engine: str = DEFAULT_ENGINE

    def tag(self) -> str:
        """Segmento di cache: schema + provider/modello (es. ``ocr1-3f2a1b``)."""
        raw = f"ocr{OCR_SCHEMA_VERSION}|{self.engine}|{self.model}"
        return f"ocr{OCR_SCHEMA_VERSION}-{hashlib.sha1(raw.encode()).hexdigest()[:6]}"


@dataclass(frozen=True)
class OcrLine:
    """Riga riconosciuta da Tesseract: bbox, testo e corpo del carattere."""

    bbox: tuple[float, float, float, float]
    text: str
    size: float


# ── testo / paragrafi ────────────────────────────────────────────────────────


_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_FILE_TAG_RE = re.compile(r"</?file[^>]*>")


def tesseract_lang(lang_in: str | None) -> str:
    """Mappa la lingua sorgente dell'app nel linguaggio Tesseract."""
    code = (lang_in or "").strip().lower()
    if not code or code == "auto":
        return _AUTO_TESS_LANGS
    return _TESS_LANG_CODES.get(code, "eng")


def _words(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _tokenize(text: str) -> list[str]:
    """Parole normalizzate (minuscole, senza punteggiatura) per l'allineamento."""
    return [token for token in re.findall(r"[^\W_]+", (text or "").lower())]


def plain_text(markdown: str) -> str:
    """Markdown → testo semplice (per la pagina nativa di fallback)."""
    text = _FILE_TAG_RE.sub(" ", markdown or "")
    text = _IMAGE_RE.sub(" ", text)
    # Tabelle Markdown: le celle diventano parole separate.
    text = re.sub(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", "", text, flags=re.MULTILINE)
    text = text.replace("|", " ")
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"(\*\*|__|\*|_|`)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # link
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _word_index(tokens: list[str]) -> dict[str, list[int]]:
    index: dict[str, list[int]] = {}
    for position, token in enumerate(tokens):
        index.setdefault(token, []).append(position)
    return index


def _match_line(
    line_text: str,
    tokens: list[str],
    index: dict[str, list[int]],
    min_score: float = 0.6,
) -> str | None:
    """Testo Mistral corrispondente a una riga Tesseract (o ``None``).

    L'ordine di lettura di Tesseract non è affidabile (colonne intrecciate) e
    quello di Mistral è a paragrafi: si cerca quindi, per **contenuto**, la
    finestra di parole Mistral più simile alla riga. Le parole rare fanno da
    indice, così la ricerca resta veloce anche su pagine dense.
    """
    words = _tokenize(line_text)
    if len(words) < 3:
        return None
    length = len(words)
    starts: set[int] = set()
    # Ancore ai bordi: la prima e l'ultima parola della riga devono coincidere
    # con gli estremi della finestra (evita finestre spostate di un token).
    for word, offset in ((words[0], 0), (words[-1], -(length - 1))):
        for position in index.get(word, ())[:50]:
            starts.add(position + offset)
    # Fallback: parole rare della riga (bordi OCR errati).
    for word in sorted(set(words), key=lambda w: len(index.get(w, ())))[:2]:
        starts.update(index.get(word, ()))
    starts = {start for start in starts if start >= 0}
    if not starts:
        return None
    best_score = 0.0
    best_span: tuple[int, int] | None = None
    for start in starts:
        for size in (length, length - 1, length + 1):
            window = tokens[start:start + size]
            if len(window) != size:
                continue
            score = difflib.SequenceMatcher(
                None, words, window, autojunk=False
            ).ratio()
            if window[0] == words[0]:
                score += 0.05
            if window[-1] == words[-1]:
                score += 0.05
            if score > best_score:
                best_score, best_span = score, (start, start + size)
    if best_span is None or best_score < min_score:
        return None
    return " ".join(tokens[best_span[0]:best_span[1]])


# ── provider OCR ────────────────────────────────────────────────────────────


def _classify_http(status: int, body: str) -> str:
    """Codice d'errore stabile a partire dallo status/dettaglio di OpenRouter."""
    if status in (401, 403):
        return "invalid_key"
    if status == 402:
        return "no_credits"
    if status == 429:
        return "rate_limited"
    if status == 404:
        return "model_not_found"
    if status >= 500:
        return "network"
    low = (body or "").lower()
    if "rate limit" in low or "temporarily rate-limited" in low:
        return "rate_limited"
    if "insufficient credit" in low or "payment required" in low:
        return "no_credits"
    return "ocr_failed"


def _annotations_text(payload: dict) -> str:
    """Testo OCR dalle annotazioni del file-parser (successo **o** errore).

    OpenRouter restituisce il file analizzato in ``message.annotations``; se
    l'inferenza fallisce (es. 429 del provider) le stesse annotazioni arrivano
    in ``error.metadata.file_annotations``. In entrambi i casi il testo OCR è
    già stato prodotto (e pagato), quindi lo si riusa.
    """
    choices = payload.get("choices") or []
    annotations = []
    if choices:
        message = choices[0].get("message") or {}
        annotations = message.get("annotations") or []
    if not annotations:
        metadata = ((payload.get("error") or {}).get("metadata") or {})
        annotations = metadata.get("file_annotations") or []
    for annotation in annotations:
        file_info = annotation.get("file") or {}
        content = file_info.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
            if text.strip():
                return text
    return ""


def _post_json(
    url: str, payload: dict, api_key: str, timeout: float
) -> tuple[int, dict]:
    """POST JSON con urllib; ritorna ``(status, payload)`` anche sugli errori."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        with contextlib.suppress(Exception):
            return exc.code, json.loads(exc.read().decode("utf-8", "replace"))
        return exc.code, {}
    except Exception as exc:  # noqa: BLE001 — rete/DNS/TLS
        raise OcrError("network", str(exc)) from exc


def _upload_bytes(page_pdf: Path) -> bytes:
    """PDF da inviare: l'originale o una versione ricompressa se enorme."""
    data = Path(page_pdf).read_bytes()
    if len(data) <= MAX_UPLOAD_BYTES:
        return data
    import pymupdf  # noqa: PLC0415 — solo per il caso "pagina enorme"

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        page = doc[0]
        rect = page.rect
        pix = page.get_pixmap(dpi=200, colorspace=pymupdf.csRGB)
        image = pix.tobytes("jpeg", jpg_quality=85)
    out = pymupdf.open()
    new = out.new_page(width=rect.width, height=rect.height)
    new.insert_image(new.rect, stream=image)
    shrunk = out.tobytes(garbage=4, deflate=True)
    out.close()
    log.info("pagina OCR ricompressa per l'invio: %d → %d byte", len(data), len(shrunk))
    return shrunk


def ocr_pdf_page(
    page_pdf: str | Path,
    config: OcrConfig,
    *,
    timeout: float = OCR_TIMEOUT,
    post=_post_json,
) -> str:
    """Testo Markdown della pagina via Mistral OCR (OpenRouter).

    Solleva :class:`OcrError` con codice stabile. La chiave API non finisce
    mai in un file o in un log.
    """
    if not (config.api_key or "").strip():
        raise OcrError("missing_key", "OPENROUTER_API_KEY non impostata")
    data = _upload_bytes(Path(page_pdf))
    payload = {
        "model": config.model,
        "messages": [{
            "role": "user",
            "content": [
                # Il contenuto della risposta non serve: si leggono le
                # annotazioni con il testo analizzato dal file-parser.
                {"type": "text", "text": "Reply with the single word: ok"},
                {"type": "file", "file": {
                    "filename": "page.pdf",
                    "file_data": "data:application/pdf;base64,"
                    + base64.b64encode(data).decode("ascii"),
                }},
            ],
        }],
        "plugins": [{"id": "file-parser", "pdf": {"engine": config.engine}}],
        "max_tokens": 4,
    }
    url = config.base_url.rstrip("/") + "/chat/completions"
    status, response = post(url, payload, config.api_key, timeout)
    text = _annotations_text(response)
    if not text.strip():
        detail = ""
        with contextlib.suppress(Exception):
            detail = str((response.get("error") or {}).get("message") or "")
        if not detail:
            detail = f"HTTP {status}"
        raise OcrError(_classify_http(status, detail), detail)
    return text


# ── livello testo / pagina nativa ───────────────────────────────────────────


def tesseract_lines(
    page, language: str = "eng", dpi: int = _TEXT_DPI
) -> list[OcrLine]:
    """Righe di testo (bbox + corpo) riconosciute da Tesseract sulla pagina."""
    textpage = page.get_textpage_ocr(dpi=dpi, full=True, language=language)
    data = page.get_text("dict", textpage=textpage)
    lines: list[OcrLine] = []
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans") or []
            text = " ".join(s.get("text", "") for s in spans).strip()
            if not text:
                continue
            size = max((s.get("size") or 0.0) for s in spans) or 10.0
            lines.append(OcrLine(tuple(line["bbox"]), text, float(size)))
    return lines


def _overlap_ratio(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    """Frazione dell'area di ``a`` coperta da ``b`` (0..1)."""
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    area = max(0.0, (a[2] - a[0]) * (a[3] - a[1]))
    return (width * height) / area if area else 0.0


def _dedupe_lines(lines: list[OcrLine]) -> list[OcrLine]:
    """Toglie le righe ridondanti (Tesseract a volte produce box sovrapposti)."""
    kept: list[OcrLine] = []
    for line in sorted(lines, key=lambda item: len(item.text), reverse=True):
        if any(_overlap_ratio(line.bbox, other.bbox) > 0.35 for other in kept):
            continue
        kept.append(line)
    return sorted(kept, key=lambda item: (item.bbox[1], item.bbox[0]))


def _draw_line(page, rect, text: str, size: float, font) -> bool:
    """Scrive ``text`` (invisibile) in **una sola riga** dentro ``rect``.

    Mai andare a capo: un testo che sborda sotto la riga finirebbe nella riga
    successiva e BabelDOC creerebbe paragrafi sovrapposti. Se il testo è più
    largo del box si riduce il corpo (il testo è invisibile: conta la posizione,
    non la leggibilità).
    """
    size = max(3.0, float(size))
    width = max(1.0, rect.width)
    while size > 3.0 and font.text_length(text, fontsize=size) > width:
        size *= 0.9
    baseline = rect.y0 + min(rect.height, size * 1.0)
    page.insert_text(
        (rect.x0, baseline), text, fontsize=size, fontname="helv",
        color=(0, 0, 0), render_mode=_INVISIBLE,
    )
    return True


def build_text_layer(
    src_pdf: str | Path,
    markdown: str,
    dst_pdf: str | Path,
    *,
    language: str = "eng",
    lines: list[OcrLine] | None = None,
) -> float:
    """Scrive la scansione + livello testo invisibile. Ritorna la copertura.

    Ogni riga di Tesseract riceve, se il contenuto è riconoscibile, il testo
    **Mistral** corrispondente; altrimenti resta il testo Tesseract (la
    geometria è comunque corretta: BabelDOC riordina per posizione). La
    copertura è la frazione di parole aggiornate con il testo Mistral.
    """
    import pymupdf  # noqa: PLC0415

    tokens = _tokenize(plain_text(markdown))
    index = _word_index(tokens)
    with pymupdf.open(str(src_pdf)) as doc:
        page = doc[0]
        try:
            detected = lines if lines is not None else tesseract_lines(page, language)
        except Exception as exc:  # noqa: BLE001 — Tesseract assente/rotto
            raise OcrError("ocr_tesseract", str(exc)) from exc
        if not detected:
            raise OcrError("ocr_tesseract", "nessuna riga riconosciuta")
        detected = _dedupe_lines(detected)
        font = pymupdf.Font("helv")
        replaced = 0
        total = 0
        for line in detected:
            words = len(_words(line.text))
            if not words:
                continue
            total += words
            text = _match_line(line.text, tokens, index) if tokens else None
            if text is None:
                text = line.text
            else:
                replaced += words
            x0, y0, x1, y1 = line.bbox
            rect = pymupdf.Rect(x0, y0, x1, y1)
            _draw_line(page, rect, text, line.size, font)
        coverage = replaced / total if total else 0.0
        dst = Path(dst_pdf)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if doc.is_pdf:
            doc.save(str(dst), garbage=4, deflate=True)
        else:
            doc.convert_to_pdf()
            doc.save(str(dst), garbage=4, deflate=True)
    return coverage


def _text_fits(text: str, width: float, height: float, size: float, font) -> bool:
    lines = 0
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split():
            trial = f"{current} {word}".strip()
            if font.text_length(trial, fontsize=size) <= width:
                current = trial
            else:
                lines += 1
                current = word
        lines += 1
    return lines * size * 1.25 <= height


def build_native_page(
    src_pdf: str | Path, markdown: str, dst_pdf: str | Path
) -> None:
    """Pagina nativa di fallback: solo testo (nessuna scansione)."""
    import pymupdf  # noqa: PLC0415

    text = plain_text(markdown)
    with pymupdf.open(str(src_pdf)) as src:
        rect = src[0].rect
    out = pymupdf.open()
    page = out.new_page(width=rect.width, height=rect.height)
    margin = 36.0
    box = pymupdf.Rect(margin, margin, rect.width - margin, rect.height - margin)
    font = pymupdf.Font("helv")
    size = 10.0
    while size > 4.0 and not _text_fits(text, box.width, box.height, size, font):
        size -= 0.5
    page.insert_textbox(
        box, text, fontsize=size, fontname="helv", color=(0, 0, 0)
    )
    dst = Path(dst_pdf)
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(str(dst), garbage=4, deflate=True)
    out.close()


def build_ocr_page(
    src_pdf: str | Path,
    dst_pdf: str | Path,
    config: OcrConfig,
    *,
    lang_in: str = "auto",
    timeout: float = OCR_TIMEOUT,
    post=_post_json,
    markdown_path: str | Path | None = None,
) -> str:
    """OCR + livello testo. Ritorna ``"layout"`` o ``"native"``.

    ``"layout"`` = scansione con livello testo (impaginazione conservata);
    ``"native"`` = pagina di testo ricostruita (fallback).

    Se ``markdown_path`` esiste il testo OCR viene riusato (nessuna nuova
    chiamata a pagamento): utile per rigenerare il livello dopo un aggiornamento
    del codice. Altrimenti il Markdown viene salvato lì per gli usi successivi.
    """
    md_file = Path(markdown_path) if markdown_path else None
    markdown = ""
    if md_file is not None and md_file.is_file():
        markdown = md_file.read_text(encoding="utf-8")
    if not markdown.strip():
        markdown = ocr_pdf_page(src_pdf, config, timeout=timeout, post=post)
        if md_file is not None:
            md_file.parent.mkdir(parents=True, exist_ok=True)
            md_file.write_text(markdown, encoding="utf-8")
    if not markdown.strip():
        raise OcrError("ocr_empty", "OCR senza testo")
    language = tesseract_lang(lang_in)
    try:
        coverage = build_text_layer(
            src_pdf, markdown, dst_pdf, language=language
        )
    except OcrError as exc:
        if exc.code != "ocr_tesseract":
            raise
        log.warning("Tesseract non disponibile (%s): pagina nativa", exc.detail)
        build_native_page(src_pdf, markdown, dst_pdf)
        return "native"
    log.info(
        "livello testo OCR: %.0f%% delle parole aggiornate con Mistral",
        coverage * 100,
    )
    return "layout"
