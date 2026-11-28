"""Traduzione di testo/Markdown con i motori gratuiti e LLM (stdlib only).

Estratto dal percorso "lettore" di ``main.py`` per poterlo usare anche nel
motore di clonazione e nel servizio (che non importano Qt):

- ``google``    : catena di endpoint gratuiti Google (dict-chrome-ex → translate-pa
                  → gtx);
- ``microsoft`` : endpoint gratuito Microsoft Edge (usato dal motore ``bing``
                  per il testo);
- ``llm``       : OpenRouter (OpenAI-compatibile), modello/prompt dal motore.

Markdown: i paragrafi sono tradotti separatamente, le tabelle cella per cella,
le immagini/URL non vengono inviati. Il modulo è thread-safe (un pool per
richiesta) e non dipende da PyQt.

Nota: ``main.py`` conserva ancora la sua copia storica per il pannello testo;
le due vanno tenute allineate finché non verranno unificate.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import urllib.parse
import urllib.request

# ═══════════════════════════════════════════════════════════════════════════════
#  Translation engines — Google Translate and Microsoft Edge (stdlib only, no API key)
# ═══════════════════════════════════════════════════════════════════════════════

_GT_URL = "https://translate.googleapis.com/translate_a/single"
_GT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


# Google progressively blocks the legacy "gtx" client (HTTP 429 "Sorry...")
# on many networks/IPs. Three *free, no-key* endpoints are tried in order:
#   1. the Chrome-extension client "dict-chrome-ex" on the classic endpoint;
#   2. Google's "translate-pa" endpoint with its embedded public browser key
#      (the same endpoint the bookfere calibre plugin ships as
#      "Google (Free) - New");
#   3. the legacy "gtx" client as a last-resort safety net.
_GT_PA_URL = "https://translate-pa.googleapis.com/v1/translate"
_GT_PA_KEY = "AIzaSyDLEeFI5OtFBwYBIoK_jj5m32rZK5CkCXA"


def _gt_request(url: str, params: dict) -> object:
    """GET a Google endpoint and return the parsed JSON (raises on failure)."""
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full_url, headers=_GT_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _gt_segments(text: str, source: str, target: str, client: str) -> str:
    """Classic translate_a/single response: list of [text, ...] segments."""
    result = _gt_request(_GT_URL, {
        "client": client, "sl": source, "tl": target, "dt": "t", "q": text,
    })
    if result and result[0]:
        return "".join(item[0] for item in result[0] if item[0])
    return ""


def _gt_pa(text: str, source: str, target: str) -> str:
    """translate-pa v1/translate response: {"translation": "..."}."""
    result = _gt_request(_GT_PA_URL, {
        "params.client": "gtx",
        "query.source_language": source,
        "query.target_language": target,
        "query.display_language": "en-US",
        "data_types": "TRANSLATION",
        "key": _GT_PA_KEY,
        "query.text": text,
    })
    if isinstance(result, dict) and result.get("translation"):
        return result["translation"]
    return ""


def _gt_translate_one(text: str, source: str, target: str) -> str:
    """Call a free Google Translate endpoint for a single chunk of text.

    Only network/HTTP failures move to the next endpoint; an answered-but-
    empty response stops the chain (the engine is reachable, there is simply
    nothing to return).
    """
    attempts = (
        lambda: _gt_segments(text, source, target, "dict-chrome-ex"),
        lambda: _gt_pa(text, source, target),
        lambda: _gt_segments(text, source, target, "gtx"),
    )
    last_error: Exception | None = None
    for attempt in attempts:
        try:
            out = attempt()
            if out:
                return out
            return text
        except Exception as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    return text


# Free Microsoft Edge endpoint (same scheme as the Ebook Translator calibre
# plugin): POST a JSON array of strings, get translations back in order.
_MS_URL = "https://edge.microsoft.com/translate/translatetext"
# Microsoft uses different codes than Google for a few languages.
_MS_LANG_CODES = {
    "zh": "zh-Hans",  # Google "zh" = Simplified Chinese
}


def _ms_lang_code(code: str) -> str:
    """Map an app language code to the Microsoft Edge API code."""
    return _MS_LANG_CODES.get(code, code)


def _ms_translate_one(text: str, source: str, target: str) -> str:
    """Call the free Microsoft Edge Translate API for a single chunk."""
    params = {"isEnterpriseClient": "False", "to": _ms_lang_code(target)}
    if source and source != "auto":
        params["from"] = _ms_lang_code(source)
    full_url = f"{_MS_URL}?{urllib.parse.urlencode(params)}"
    body = json.dumps([text]).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(
        full_url, data=body, headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    try:
        return result[0]["translations"][0]["text"]
    except (IndexError, KeyError, TypeError):
        return text


# ── LLM (OpenRouter, API OpenAI-compatibile) ────────────────────────────────
DEFAULT_LLM_MODEL = "openai/gpt-oss-120b"
DEFAULT_LLM_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_LLM_TIMEOUT = 90
_LLM_PROMPT = (
    "You are a professional translator. Translate from {src} to {dst}. "
    "Return ONLY the translated text, preserving Markdown structure "
    "(headings, lists, tables, emphasis, links). Do not add notes."
)


def _llm_translate_one(
    text: str,
    source: str,
    target: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    system_prompt: str = "",
    timeout: float = DEFAULT_LLM_TIMEOUT,
) -> str:
    """Traduce un chunk con OpenRouter (percorso OpenAI-compatibile)."""
    key = (api_key or os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY non impostata")
    model = (model or os.environ.get("PDF_LLM_MODEL") or DEFAULT_LLM_MODEL).strip()
    base = (
        base_url or os.environ.get("PDF_LLM_BASE_URL") or DEFAULT_LLM_BASE_URL
    ).rstrip("/")
    system = (system_prompt or os.environ.get("PDF_LLM_SYSTEM_PROMPT") or "").strip()
    if not system:
        system = _LLM_PROMPT.format(src=source, dst=target)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )
    with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("risposta LLM vuota")
    return content.strip()


def _translate_one(
    engine: str, text: str, source: str, target: str,
    _stats: _TranslateStats | None = None,
    llm: dict | None = None,
) -> str:
    """Translate a single chunk (google|microsoft|llm)."""
    if _stats is not None:
        _stats.attempted()
    if engine == "microsoft" or engine == "bing":
        out = _ms_translate_one(text, source, target)
    elif engine == "llm":
        out = _llm_translate_one(text, source, target, **(llm or {}))
    else:
        out = _gt_translate_one(text, source, target)
    if _stats is not None:
        _stats.succeeded()
    return out




# Markdown structural patterns protected during translation.
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_TABLE_RE = re.compile(
    r"^\s*\|[^\n]*\|\s*\n\s*\|[\s:|-]+\|\s*\n(?:\s*\|[^\n]*\|\s*\n?)+",
    re.MULTILINE,
)
_SEP_CELL_RE = re.compile(r":?-{3,}:?")


_MAX_WORKERS = 8  # concurrent translation requests (I/O-bound)


class TranslationError(RuntimeError):
    """Raised when a translation engine fails on every chunk of a request.

    Individual chunk failures still degrade gracefully to the original text;
    only a *total* failure (engine unreachable/blocked on this network) is
    raised so the UI can surface it instead of silently showing untranslated
    text.
    """


class _TranslateStats:
    """Attempt/ok counters shared across the concurrent translation chunks.

    ``list.append`` is atomic under the GIL, so no lock is needed even though
    paragraphs (and their nested table-cell/sub-chunk calls) run in worker
    threads.
    """

    def __init__(self) -> None:
        self._attempts: list[int] = []
        self._ok: list[int] = []

    @property
    def attempts(self) -> int:
        return len(self._attempts)

    @property
    def ok(self) -> int:
        return len(self._ok)

    def attempted(self) -> None:
        self._attempts.append(1)

    def succeeded(self) -> None:
        self._ok.append(1)


def _translate_cell(
    cell: str, source: str, target: str, engine: str = "google",
    _stats: _TranslateStats | None = None, llm: dict | None = None,
) -> str:
    """Translate one table cell, falling back to the original on failure."""
    try:
        return _translate_one(
            engine, cell, source, target, _stats=_stats, llm=llm
        ).strip()
    except Exception:
        return cell


def _translate_table(
    table: str, source: str, target: str, engine: str = "google",
    _stats: _TranslateStats | None = None, llm: dict | None = None,
) -> str:
    """Translate the cell contents of a markdown table, keeping its structure.

    All distinct translatable cells are fetched concurrently (one request per
    cell), then placed back in their original positions.
    """
    lines = [ln.strip() for ln in table.strip().splitlines()]
    out: list[str] = []

    def _cells(ln: str) -> list[str]:
        return [c.strip() for c in ln.strip().strip("|").split("|")]

    # Collect the distinct cells that actually need translation.
    unique: list[str] = []
    seen: set[str] = set()
    for ln in lines:
        if not ln.startswith("|"):
            continue
        cells = _cells(ln)
        if all(_SEP_CELL_RE.fullmatch(c) for c in cells):
            continue  # separator row kept verbatim
        for c in cells:
            if c and re.search(r"[A-Za-z]", c) and c not in seen:
                seen.add(c)
                unique.append(c)

    cache: dict[str, str] = {}
    if unique:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(_MAX_WORKERS, len(unique))
        ) as pool:
            futures = {
                pool.submit(_translate_cell, c, source, target, engine, _stats, llm): c
                for c in unique
            }
            for fut in concurrent.futures.as_completed(futures):
                cache[futures[fut]] = fut.result()

    for ln in lines:
        if not ln.startswith("|"):
            out.append(ln)
            continue
        cells = _cells(ln)
        if all(_SEP_CELL_RE.fullmatch(c) for c in cells):
            out.append(ln)  # separator row kept verbatim
            continue
        translated: list[str] = []
        for c in cells:
            # Numbers/symbol-only cells are left untouched (faster, safer).
            if not c or not re.search(r"[A-Za-z]", c):
                translated.append(c)
            else:
                translated.append(cache.get(c, c))
        out.append("| " + " | ".join(translated) + " |")
    return "\n".join(out)


def _translate_paragraph(
    para: str,
    source: str,
    target: str,
    chunk_size: int,
    engine: str = "google",
    _stats: _TranslateStats | None = None,
    llm: dict | None = None,
) -> str:
    """Translate one paragraph, protecting markdown tables and image links."""
    protected: dict[str, str] = {}

    def _protect(kind: str, value: str) -> str:
        tok = f"@@{kind}{len(protected)}@@"
        protected[tok] = value
        return tok

    # Image links: never send URLs to the translator.
    para = _MD_IMAGE_RE.sub(lambda m: _protect("IMG", m.group(0)), para)

    # Tables: translate their cells, then protect the rebuilt table.
    def _table_repl(m):
        return _protect(
            "TBL", _translate_table(m.group(0), source, target, engine, _stats, llm)
        )

    para = _MD_TABLE_RE.sub(_table_repl, para)

    # Nothing left to translate (only protected tokens) → skip the API call.
    if not re.search(r"[^\W\d_]", re.sub(r"@@[A-Z]+\d+@@", "", para)):
        out = para
    elif len(para) <= chunk_size:
        try:
            out = _translate_one(engine, para, source, target, _stats=_stats, llm=llm)
        except Exception:
            out = para
    else:
        # Long paragraph → split at sentence-ish boundaries.
        sub_paras = re.split(r"(?<=[.!?])\s+", para)
        sub_chunks: list[str] = []
        current: list[str] = []
        cur_len = 0
        for sub in sub_paras:
            if cur_len + len(sub) > chunk_size and current:
                sub_chunks.append(" ".join(current))
                current = []
                cur_len = 0
            current.append(sub)
            cur_len += len(sub)
        if current:
            sub_chunks.append(" ".join(current))
        sub_translated: list[str] = []
        for ch in sub_chunks:
            try:
                sub_translated.append(
                    _translate_one(engine, ch, source, target, _stats=_stats, llm=llm)
                )
            except Exception:
                sub_translated.append(ch)
        out = " ".join(sub_translated)

    # Google may add spaces around/inside tokens; normalize them back.
    out = re.sub(r"@@\s*([A-Z]+)\s*(\d+)\s*@@", r"@@\1\2@@", out)
    for tok, value in protected.items():
        out = out.replace(tok, value)
    return out


def translate_text(
    text: str,
    source: str = "en",
    target: str = "it",
    engine: str = "google",
    chunk_size: int = 1500,
    llm: dict | None = None,
) -> str:
    """Translate text using the chosen engine's public API.

    ``engine`` is "google", "microsoft" (anche "bing") o "llm".
    Per ``llm`` passare ``llm={model, base_url, api_key, system_prompt, timeout}``.  Translates **each paragraph
    independently** (split on ``\n\n``) so paragraph breaks never pass
    through the API, and fetches those paragraphs **concurrently** so a short
    page doesn't wait on many sequential round-trips.  Markdown tables and
    image links are protected so the translator doesn't mangle their syntax;
    table cell contents are translated individually (also concurrently).
    """
    if not text or not text.strip():
        return text

    paragraphs = text.split("\n\n")
    results: list[str] = [""] * len(paragraphs)
    tasks = [(i, p) for i, p in enumerate(paragraphs) if p.strip()]
    if not tasks:
        return text

    stats = _TranslateStats()
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(_MAX_WORKERS, len(tasks))
    ) as pool:
        futures = {
            pool.submit(
                _translate_paragraph, p, source, target, chunk_size, engine, stats, llm
            ): i
            for i, p in tasks
        }
        for fut in concurrent.futures.as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception:
                results[i] = paragraphs[i]

    # Restore blank paragraphs (the ``\n\n`` separators) in their positions.
    for i, p in enumerate(paragraphs):
        if not p.strip():
            results[i] = p

    if stats.attempts > 0 and stats.ok == 0:
        raise TranslationError(
            f"engine '{engine}' failed on all {stats.attempts} request(s)"
        )
    return "\n\n".join(results)


def translate_text_google(
    text: str, source: str = "en", target: str = "it", chunk_size: int = 1500
) -> str:
    """Backward-compatible wrapper: translate text with Google Translate."""
    return translate_text(
        text, source=source, target=target, engine="google", chunk_size=chunk_size
    )


