#!/usr/bin/env python3
"""CLI translator per pdf2zh_next (``--clitranslator``): stdin -> stdout.

Catena di endpoint gratuiti, importata da pdfcloner (a sua volta da
noesis-pdf-reader-lite) e usata dall'engine ``google`` di noesis-pdf-cloner:

  1. translate.googleapis.com/translate_a/single?client=dict-chrome-ex
  2. translate-pa.googleapis.com/v1/translate (chiave browser pubblica)
  3. translate_a/single?client=gtx (spesso 429)
  4. fallback Microsoft: edge.microsoft.com/translate/translatetext
  5. fallback LLM: OpenRouter, solo se OPENROUTER_API_KEY è impostata.

Le lingue arrivano da PDF_LANG_IN / PDF_LANG_OUT; ogni fallback degradato
(Microsoft/LLM) è registrato in un file JSONL (``CLONE_ENGINE_EVENTS`` o
``cache/engine_events.jsonl`` accanto allo script) per l'avviso in UI.
"""
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GT_URL = "https://translate.googleapis.com/translate_a/single"
GT_PA_URL = "https://translate-pa.googleapis.com/v1/translate"
GT_PA_KEY = "AIzaSyDLEeFI5OtFBwYBIoK_jj5m32rZK5CkCXA"
MS_URL = "https://edge.microsoft.com/translate/translatetext"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

LANG_IN = os.environ.get("PDF_LANG_IN", "en")
LANG_OUT = os.environ.get("PDF_LANG_OUT", "it")
EVENTS = os.environ.get("CLONE_ENGINE_EVENTS") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cache", "engine_events.jsonl"
)

HTTP_TIMEOUT = 15
ATTEMPTS = 2


def _force_utf8_stdio() -> None:
    """Mette stdin/stdout/stderr in UTF-8.

    ``pdf2zh_next`` esegue questo script come subprocess con
    ``encoding="utf-8"`` (``translator/translator_impl/clitranslator.py``): se
    scrivessimo con l'encoding di default del sistema (su Windows la codepage
    ANSI, es. cp1252) le lettere accentate uscirebbero come byte non-UTF-8 e
    pdf2zh le sostituirebbe con U+FFFD (�). Vale per l'italiano e per ogni
    carattere non-ASCII.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _read_input() -> str:
    """Legge tutto stdin come UTF-8 (fallback: testo già decodificato)."""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        try:
            return buffer.read().decode("utf-8", "replace")
        except (OSError, AttributeError, ValueError):
            pass
    return sys.stdin.read()


def _emit(text: str) -> None:
    """Scrive ``text`` su stdout come UTF-8 + newline."""
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        try:
            buffer.write(text.encode("utf-8") + b"\n")
            buffer.flush()
            return
        except (OSError, AttributeError, ValueError):
            pass
    print(text)


def _emit_error(text: str) -> None:
    """Scrive ``text`` su stderr come UTF-8 + newline."""
    buffer = getattr(sys.stderr, "buffer", None)
    if buffer is not None:
        try:
            buffer.write(text.encode("utf-8") + b"\n")
            buffer.flush()
            return
        except (OSError, AttributeError, ValueError):
            pass
    print(text, file=sys.stderr)


def http_get(url: str) -> str | None:
    for _ in range(ATTEMPTS):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                if r.status == 200:
                    return r.read().decode("utf-8", "replace")
        except Exception:
            time.sleep(0.5)
    return None


def http_post_json(url: str, data: bytes, headers: dict) -> str | None:
    for _ in range(ATTEMPTS):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                if r.status == 200:
                    return r.read().decode("utf-8", "replace")
        except Exception:
            time.sleep(0.5)
    return None


def gt_single(client: str, text: str) -> str | None:
    q = urllib.parse.urlencode(
        {"client": client, "sl": LANG_IN, "tl": LANG_OUT, "dt": "t", "q": text}
    )
    body = http_get(f"{GT_URL}?{q}")
    if body is None:
        return None
    try:
        result = json.loads(body)
        out = "".join(item[0] for item in result[0] if item and item[0])
        return out or None
    except Exception:
        return None


def gt_translate_pa(text: str) -> str | None:
    q = urllib.parse.urlencode({
        "params.client": "gtx",
        "query.source_language": LANG_IN,
        "query.target_language": LANG_OUT,
        "query.display_language": "en-US",
        "data_types": "TRANSLATION",
        "key": GT_PA_KEY,
        "query.text": text,
    })
    body = http_get(f"{GT_PA_URL}?{q}")
    if body is None:
        return None
    try:
        return json.loads(body).get("translation") or None
    except Exception:
        return None


def ms_translate(text: str) -> str | None:
    q = urllib.parse.urlencode({"isEnterpriseClient": "False", "to": LANG_OUT})
    body = http_post_json(
        f"{MS_URL}?{q}",
        json.dumps([text]).encode(),
        {"Content-Type": "application/json"},
    )
    if body is None:
        return None
    try:
        data = json.loads(body)
        out = "".join(tr.get("text", "") for tr in data[0]["translations"])
        return out or None
    except Exception:
        return None


def llm_translate(text: str) -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    model = os.environ.get("PDF_LLM_MODEL", "inception/mercury-2.5")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content":
                f"You are a professional, authentic machine translation engine. "
                f"Translate the text from {LANG_IN} to {LANG_OUT}. "
                "Do NOT add explanations. Preserve placeholders like [[1]] or %d. "
                "Translate every paragraph."},
            {"role": "user", "content": text},
        ],
    }
    url = (
        os.environ.get("PDF_LLM_BASE_URL", "https://openrouter.ai/api/v1")
        + "/chat/completions"
    )
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # 401/403 = chiave assente/non valida: evento distinto dalla rete.
        if exc.code in (401, 403):
            log_event("llm_unauthorized")
        return None
    except Exception:
        return None
    try:
        return json.loads(body)["choices"][0]["message"]["content"].strip() or None
    except Exception:
        return None


def log_event(engine: str) -> None:
    try:
        os.makedirs(os.path.dirname(EVENTS), exist_ok=True)
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "engine": engine}) + "\n")
    except OSError:
        pass


def translate(text: str) -> str | None:
    for fn, name in (
        (lambda t: gt_single("dict-chrome-ex", t), "google (dict-chrome-ex)"),
        (gt_translate_pa, "google (translate-pa)"),
        (lambda t: gt_single("gtx", t), "google (gtx)"),
        (ms_translate, "microsoft"),
    ):
        out = fn(text)
        if out is not None:
            if name not in ("google (dict-chrome-ex)",):
                log_event(name)
            return out
    if os.environ.get("OPENROUTER_API_KEY"):
        out = llm_translate(text)
        if out is not None:
            log_event("llm")
            return out
    return None


def main() -> int:
    _force_utf8_stdio()
    text = _read_input()
    if not text.strip():
        _emit("")
        return 0
    text = text[:5000]
    out = translate(text)
    if out is None:
        _emit_error(
            f"TRANSLATION-ERROR: nessun endpoint disponibile per '{text[:50]}'"
        )
        return 1
    _emit(html.unescape(out).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
