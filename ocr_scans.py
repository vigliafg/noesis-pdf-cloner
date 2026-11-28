"""OCR delle pagine scansionate: Mistral OCR (OpenRouter) → Markdown.

Una pagina **senza testo** non è traducibile da BabelDOC. Invece di provare a
ricostruire il layout (fragile su tabelle e figure), il percorso OCR produce
un **documento Markdown**:

1. la pagina viene inviata a **Mistral OCR** tramite il plugin ``file-parser``
   di OpenRouter, che restituisce il Markdown e le immagini estratte;
2. il Markdown (senza immagini) e le immagini vengono messi in cache: l'OCR si
   paga una volta sola;
3. il testo viene tradotto con ``text_translate`` (Google/Microsoft/LLM) e le
   figure vengono aggiunte **in coda** con didascalia;
4. il PDF di export è un "hardcopy" del Markdown completo (testo + figure),
   su una o più pagine.

Modulo **puro Python** (nessuna dipendenza da Qt), testabile senza rete: il
provider accetta un ``post`` iniettabile.

Allineato a ``noesis-pdf-cloner-service/app/ocr_scans.py``.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import text_translate

log = logging.getLogger("ocr_scans")

# Versione dello schema OCR: un cambiamento del formato/cache invalida i
# documenti OCR già prodotti.
OCR_SCHEMA_VERSION = "1"

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
# Modello "di appoggio" per la richiesta: il testo arriva dalle annotazioni del
# file-parser, non dalla risposta. Deve solo essere un modello testuale
# economico e disponibile.
DEFAULT_MODEL = "google/gemini-2.5-flash-lite"
DEFAULT_ENGINE = "mistral-ocr"
OCR_TIMEOUT = 180.0
# Oltre questa dimensione la pagina viene ricompressa prima dell'invio.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
# Prezzo Mistral OCR via OpenRouter ($2 / 1000 pagine): solo per la stima
# mostrata nell'avviso. I token del modello di appoggio sono trascurabili.
COST_PER_PAGE_USD = 0.002

_IMAGE_PLACEHOLDER_RE = re.compile(r"!\[[^\]]*\]\(\s*(img-[\w.-]+)\s*\)", re.I)
_FIGURE_RE = re.compile(r"(?:Figure|Fig\.?)\s*(\d+[^\n]*)", re.I)
_FILE_TAG_RE = re.compile(r"</?file[^>]*>")
_IMAGE_MD_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")


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


@dataclass
class OcrImage:
    """Immagine estratta dalla pagina (bytes + didascalia)."""

    filename: str
    data: bytes = b""
    mime: str = "image/png"
    caption: str = ""
    source_name: str = ""


@dataclass
class OcrPage:
    """Risultato dell'OCR di una pagina."""

    markdown: str
    images: list[OcrImage] = field(default_factory=list)


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


def _annotations(payload: dict) -> list:
    """Annotazioni del file-parser (successo **o** errore)."""
    choices = payload.get("choices") or []
    annotations = []
    if choices:
        message = choices[0].get("message") or {}
        annotations = message.get("annotations") or []
    if not annotations:
        metadata = ((payload.get("error") or {}).get("metadata") or {})
        annotations = metadata.get("file_annotations") or []
    return annotations


def _content_parts(payload: dict) -> list:
    """Parti (testo/immagini) del file analizzato, nell'ordine restituito."""
    for annotation in _annotations(payload):
        content = (annotation.get("file") or {}).get("content")
        if isinstance(content, list):
            return content
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
    return []


def _annotations_text(payload: dict) -> str:
    """Testo OCR concatenato (compatibilità con i test/versioni precedenti)."""
    return "".join(
        part.get("text", "")
        for part in _content_parts(payload)
        if isinstance(part, dict) and part.get("type") == "text"
    )


def _data_url_bytes(url: str) -> tuple[bytes, str, str]:
    """``(bytes, mime, estensione)`` da una data URL base64."""
    match = re.match(r"data:([^;,]+)?(;base64)?,(.*)", url or "", re.S)
    if not match:
        return b"", "image/png", ".png"
    mime = match.group(1) or "image/png"
    raw = match.group(3)
    data = base64.b64decode(raw) if match.group(2) else raw.encode("utf-8")
    ext = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "image/png": ".png",
    }.get(mime, ".png")
    return data, mime, ext


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
            return response.status, json.loads(
                response.read().decode("utf-8", "replace")
            )
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
    log.info("pagina OCR ricompressa: %d → %d byte", len(data), len(shrunk))
    return shrunk


def ocr_pdf_page(
    page_pdf: str | Path,
    config: OcrConfig,
    *,
    timeout: float = OCR_TIMEOUT,
    post=None,
) -> str:
    """Testo Markdown della pagina via Mistral OCR (OpenRouter)."""
    post = post or _post_json
    if not (config.api_key or "").strip():
        raise OcrError("missing_key", "OPENROUTER_API_KEY non impostata")
    data = _upload_bytes(Path(page_pdf))
    payload = {
        "model": config.model,
        "messages": [{
            "role": "user",
            "content": [
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
        raise OcrError(
            _classify_http(status, detail or f"HTTP {status}"),
            detail or f"HTTP {status}",
        )
    return text


# ── pagina OCR: Markdown + immagini ─────────────────────────────────────────


def _caption_for(text: str, start: int, end: int, index: int, label: str) -> str:
    """Didascalia vicina al placeholder, o ``Figura N — pagina X``."""
    window = text[max(0, start - 500): end + 300]
    matches = [m.group(0).strip(" .:—-") for m in _FIGURE_RE.finditer(window)]
    if matches:
        caption = matches[0]
        if caption:
            return caption
    return f"Figura {index} — pagina {label}" if label else f"Figura {index}"


def _strip_markdown_images(markdown: str) -> str:
    text = _IMAGE_MD_RE.sub(" ", markdown or "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def ocr_page(
    page_pdf: str | Path,
    config: OcrConfig,
    *,
    page_label: str = "",
    timeout: float = OCR_TIMEOUT,
    post=None,
) -> OcrPage:
    """OCR completo della pagina: Markdown (senza immagini) + figure."""
    post = post or _post_json
    if not (config.api_key or "").strip():
        raise OcrError("missing_key", "OPENROUTER_API_KEY non impostata")
    data = _upload_bytes(Path(page_pdf))
    payload = {
        "model": config.model,
        "messages": [{
            "role": "user",
            "content": [
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
    parts = _content_parts(response)
    text = "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    )
    if not text.strip():
        detail = ""
        with contextlib.suppress(Exception):
            detail = str((response.get("error") or {}).get("message") or "")
        raise OcrError(
            _classify_http(status, detail or f"HTTP {status}"),
            detail or f"HTTP {status}",
        )

    # Immagini: le parti ``image_url`` del file-parser, in ordine.
    raw_images = [
        part["image_url"]["url"]
        for part in parts
        if isinstance(part, dict)
        and part.get("type") == "image_url"
        and isinstance(part.get("image_url"), dict)
    ]
    decoded: list[tuple[bytes, str, str]] = []
    for url_data in raw_images:
        with contextlib.suppress(Exception):
            decoded.append(_data_url_bytes(url_data))

    # Didascalie vicino ai placeholder, poi rimozione dal corpo.
    images: list[OcrImage] = []
    placeholders = list(_IMAGE_PLACEHOLDER_RE.finditer(text))
    used: set[int] = set()
    for position, match in enumerate(placeholders):
        index = position
        name_match = re.search(r"(\d+)", match.group(1) or "")
        if name_match:
            candidate = int(name_match.group(1))
            if 0 <= candidate < len(decoded):
                index = candidate
        if index in used:
            index = next((i for i in range(len(decoded)) if i not in used), index)
        used.add(index)
        caption = _caption_for(text, match.start(), match.end(), position + 1, page_label)
        if index < len(decoded):
            data_bytes, mime, ext = decoded[index]
            images.append(OcrImage(
                filename=f"img_{index}{ext}", data=data_bytes, mime=mime,
                caption=caption, source_name=match.group(1),
            ))
        else:
            images.append(OcrImage(
                filename=f"img_{position}.png", caption=caption,
                source_name=match.group(1),
            ))
    # Immagini senza placeholder: in coda, con didascalia generica.
    for index, (data_bytes, mime, ext) in enumerate(decoded):
        if index not in used:
            images.append(OcrImage(
                filename=f"img_{index}{ext}", data=data_bytes, mime=mime,
                caption=_caption_for(text, len(text), len(text), len(images) + 1, page_label),
            ))

    body = _strip_markdown_images(
        _FILE_TAG_RE.sub(" ", text)
    )
    return OcrPage(markdown=body, images=images)


def write_ocr_page(
    page: OcrPage, markdown_path: str | Path, assets_dir: str | Path
) -> None:
    """Salva il Markdown e le immagini in cache (metadati didascalie inclusi)."""
    md_path = Path(markdown_path)
    assets = Path(assets_dir)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    assets.mkdir(parents=True, exist_ok=True)
    md_path.write_text(page.markdown, encoding="utf-8")
    meta = []
    for image in page.images:
        if image.data:
            (assets / image.filename).write_bytes(image.data)
        meta.append({
            "filename": image.filename,
            "caption": image.caption,
            "mime": image.mime,
            "source_name": image.source_name,
        })
    (md_path.with_suffix(".json")).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read_ocr_page(
    markdown_path: str | Path, assets_dir: str | Path
) -> tuple[str, list[OcrImage]]:
    """Rilegge il Markdown e i metadati delle immagini dalla cache."""
    md_path = Path(markdown_path)
    markdown = md_path.read_text(encoding="utf-8")
    assets = Path(assets_dir)
    images: list[OcrImage] = []
    meta_path = md_path.with_suffix(".json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = []
    for item in meta:
        name = str(item.get("filename") or "")
        path = assets / name
        mime = str(item.get("mime") or "image/png")
        data = b""
        with contextlib.suppress(OSError):
            data = path.read_bytes()
        images.append(OcrImage(
            filename=name, data=data, mime=mime,
            caption=str(item.get("caption") or ""),
            source_name=str(item.get("source_name") or ""),
        ))
    return markdown, images


def translate_markdown(
    markdown: str,
    source: str,
    target: str,
    engine: str,
    *,
    llm: dict | None = None,
) -> str:
    """Traduce il Markdown con il motore scelto (google/bing/llm)."""
    engine = (engine or "google").strip().lower()
    if engine == "bing":
        engine = "microsoft"
    if engine not in ("google", "microsoft", "llm"):
        engine = "google"
    return text_translate.translate_text(
        markdown, source=source, target=target, engine=engine, llm=llm
    )


def inline_figure_images(markdown: str, images: list[OcrImage]) -> str:
    """Sostituisce i riferimenti ``assets/<file>`` con data URL base64."""
    out = markdown
    for image in images:
        if not image.data:
            continue
        data_url = f"data:{image.mime or 'image/png'};base64," + base64.b64encode(
            image.data
        ).decode("ascii")
        out = out.replace(f"assets/{image.filename}", data_url)
    return out


def append_figures(
    markdown: str,
    images: list[OcrImage],
    *,
    base64_images: bool = False,
    assets_dir: str | Path | None = None,
    page_label: str = "",
) -> str:
    """Aggiunge le figure in coda, con didascalia (path o base64)."""
    if not images:
        return markdown
    lines = [markdown.rstrip(), "", "---", "", "## Figure", ""]
    for index, image in enumerate(images, 1):
        caption = image.caption or (
            f"Figura {index} — pagina {page_label}" if page_label else f"Figura {index}"
        )
        if base64_images and image.data:
            src = f"data:{image.mime or 'image/png'};base64," + base64.b64encode(
                image.data
            ).decode("ascii")
        else:
            src = f"assets/{image.filename}"
        lines.append(f"![{caption}]({src})")
        lines.append("")
        lines.append(f"*{caption}*")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── PDF hardcopy del Markdown ───────────────────────────────────────────────


def _plain_lines(markdown: str) -> list[tuple[str, int]]:
    """Righe del Markdown → ``(testo, livello)`` (0 corpo, 1..3 titoli, -1 tabella)."""
    out: list[tuple[str, int]] = []
    for raw in (markdown or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            out.append(("", 0))
            continue
        if line.strip().startswith("|"):
            if re.fullmatch(r"\s*\|?[\s:|-]+\|?\s*", line):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append(("   ".join(c for c in cells if c), -1))
            continue
        heading = re.match(r"^(#{1,6})\s*(.*)$", line)
        if heading:
            out.append((heading.group(2).strip(), min(3, len(heading.group(1)))))
            continue
        if re.match(r"^\s*[-*+]\s+", line):
            out.append(("• " + re.sub(r"^\s*[-*+]\s+", "", line), 0))
            continue
        text = re.sub(r"(\*\*|__|\*|_|`)", "", line)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        out.append((text, 0))
    return out


def build_pdf_from_markdown(
    markdown: str,
    images: list[OcrImage],
    dst: str | Path,
    *,
    width: float = 612.0,
    height: float = 792.0,
    page_label: str = "",
) -> int:
    """PDF testuale dal Markdown tradotto + figure in coda. Ritorna le pagine."""
    import pymupdf  # noqa: PLC0415

    margin = 42.0
    body_size = 9.5
    line_gap = 1.25
    font = pymupdf.Font("helv")
    out = pymupdf.open()
    page = out.new_page(width=width, height=height)
    y = margin
    usable = width - 2 * margin

    def new_page() -> None:
        nonlocal page, y
        page = out.new_page(width=width, height=height)
        y = margin

    def wrap(text: str, size: float) -> list[str]:
        words = text.split()
        lines: list[str] = []
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if not current or font.text_length(trial, fontsize=size) <= usable:
                current = trial
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines or [""]

    for text, level in _plain_lines(markdown):
        if not text:
            y += body_size * 0.7
            continue
        if level > 0:
            size = {1: 15.0, 2: 13.0, 3: 11.5}.get(level, 11.0)
            fontname = "hebo"
        elif level < 0:
            size = 8.5
            fontname = "helv"
        else:
            size = body_size
            fontname = "helv"
        for line in wrap(text, size):
            if y + size * line_gap > height - margin:
                new_page()
            page.insert_text(
                (margin, y + size), line, fontsize=size, fontname=fontname,
                color=(0, 0, 0),
            )
            y += size * line_gap
        y += size * 0.35

    # Figure in coda, con didascalia.
    for index, image in enumerate(images, 1):
        if not image.data:
            continue
        caption = image.caption or (
            f"Figura {index} — pagina {page_label}" if page_label else f"Figura {index}"
        )
        with contextlib.suppress(Exception):
            pix = pymupdf.Pixmap(image.data)
            max_w = usable
            max_h = height * 0.42
            scale = min(max_w / pix.width, max_h / pix.height, 1.0)
            w, h = pix.width * scale, pix.height * scale
            if y + h + 24 > height - margin:
                new_page()
            rect = pymupdf.Rect(
                margin + (usable - w) / 2, y, margin + (usable + w) / 2, y + h
            )
            page.insert_image(rect, stream=image.data)
            y += h + 12
            for line in wrap(caption, 8.0):
                if y + 10 > height - margin:
                    new_page()
                page.insert_text(
                    (margin, y + 8), line, fontsize=8.0, fontname="helv",
                    color=(0.25, 0.25, 0.25),
                )
                y += 10
            y += 10

    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(str(dst_path), garbage=4, deflate=True)
    pages = len(out)
    out.close()
    return pages
