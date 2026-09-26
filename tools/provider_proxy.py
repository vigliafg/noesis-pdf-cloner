#!/usr/bin/env python3
"""Proxy OpenAI-compatibile → OpenRouter che **pinna il provider** (es. Groq).

`pdf2zh_next` non espone il routing del provider: per forzare Groq serve il campo
``provider`` nel body. Questo proxy ascolta in locale e lo inietta.

Uso:

    OPENROUTER_API_KEY=... PROXY_PORT=8790 .venv/bin/python tools/provider_proxy.py

Poi punta il motore al proxy:

    PDF_LLM_BASE_URL=http://127.0.0.1:8790/v1 \
    .venv/bin/python tools/bench_page.py --model openai/gpt-oss-120b \
        --base-url http://127.0.0.1:8790/v1 --fast --fast-flags --workers 8 \
        --reasoning-effort minimal --fresh --label gpt-oss-groq

Variabili: ``PROXY_PORT`` (default 8790), ``PROXY_PROVIDER`` (default ``groq``),
``PROXY_MODELS`` (lista di modelli da pinnare, default ``openai/gpt-oss-120b``;
``*`` = tutti), ``PROXY_UPSTREAM`` (default ``https://openrouter.ai/api/v1``),
``OPENROUTER_API_KEY`` (obbligatoria, salvo header Authorization in ingresso).
Solo stdlib: nessuna dipendenza.

**Model-aware**: il campo ``provider`` è iniettato solo per i modelli in
``PROXY_MODELS``; per gli altri (DeepSeek, Gemini, Mercury…) il body passa
invariato. Così un'unica base URL serve tutti i modelli.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PROXY_PORT = int(os.environ.get("PROXY_PORT", "8790"))
PROXY_PROVIDER = os.environ.get("PROXY_PROVIDER", "groq").strip()
PROXY_UPSTREAM = os.environ.get("PROXY_UPSTREAM", "https://openrouter.ai/api/v1").rstrip("/")
API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
# Modelli a cui applicare il pin (match per sottostringa, case-insensitive).
# "*" = pinna tutto (comportamento storico). Default: solo gpt-oss.
PROXY_MODELS: tuple[str, ...] = tuple(
    token.strip().lower()
    for token in os.environ.get("PROXY_MODELS", "openai/gpt-oss-120b").split(",")
    if token.strip()
)


def _should_pin(model: str) -> bool:
    """True se il modello va pinnato al provider configurato.

    Così un'unica base URL può servire tutti i modelli: gpt-oss → Groq, mentre
    DeepSeek/Gemini/Mercury passano invariati verso OpenRouter.
    """
    if "*" in PROXY_MODELS:
        return True
    low = (model or "").strip().lower()
    return any(token in low for token in PROXY_MODELS)


def _upstream_url(path: str) -> str:
    """Compone l'URL upstream evitando il doppio ``/v1``.

    Il client usa ``base_url = http://host:port/v1`` → path ``/v1/chat/...``;
    l'upstream è già ``.../api/v1``. Normalizziamo il prefisso.
    """
    base = PROXY_UPSTREAM.rstrip("/")
    if base.endswith("/v1"):
        if path.startswith("/v1/"):
            path = path[len("/v1"):]
        elif path == "/v1":
            path = ""
    elif not path.startswith("/v1") and not path.startswith("/api"):
        path = "/v1" + path
    return base + path


def _inject_provider(payload: dict[str, Any]) -> dict[str, Any]:
    """Aggiunge/forza ``provider.only`` nel body, **solo** per i modelli scelti.

    Per i modelli non in ``PROXY_MODELS`` il body è restituito invariato, così
    gli altri motori (DeepSeek, Gemini, Mercury…) funzionano normalmente.
    """
    model = str(payload.get("model", ""))
    if not _should_pin(model):
        return payload
    payload = dict(payload)
    existing = payload.get("provider")
    provider = dict(existing) if isinstance(existing, dict) else {}
    provider["only"] = [PROXY_PROVIDER]
    provider["allow_fallbacks"] = False
    payload["provider"] = provider
    return payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: ANN001
        sys.stderr.write("proxy: " + (fmt % args) + "\n")

    def _auth_header(self) -> str:
        return self.headers.get("Authorization") or (f"Bearer {API_KEY}" if API_KEY else "")

    def _send_json(self, status: int, obj: Any) -> None:
        body = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Il client si è disconnesso: non è un errore del proxy.
            pass

    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok", "provider": PROXY_PROVIDER})
            return
        # passthrough minimale (es. /v1/models)
        self._forward(b"", self.path)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        if self.path.endswith("/chat/completions"):
            try:
                payload = json.loads(raw or b"{}")
                model = str(payload.get("model", ""))
                pinned = _should_pin(model)
                raw = json.dumps(_inject_provider(payload)).encode("utf-8")
                self.log_message(
                    "chat model=%s pin=%s", model, PROXY_PROVIDER if pinned else "-"
                )
            except json.JSONDecodeError:
                self._send_json(400, {"error": "invalid json body"})
                return
        self._forward(raw, self.path)

    def _write_raw(self, status: int, content_type: str, data: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _forward(self, body: bytes, path: str) -> None:
        url = _upstream_url(path)
        request = urllib.request.Request(url, data=body or None, method=self.command)
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        auth = self._auth_header()
        if auth:
            request.add_header("Authorization", auth)
        # identificazione opzionale consigliata da OpenRouter
        request.add_header("HTTP-Referer", "https://github.com/vigliafg/noesis-pdf-cloner")
        request.add_header("X-Title", "noesis-pdf-cloner provider proxy")
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                data = response.read()
                self._write_raw(
                    response.status,
                    response.headers.get("Content-Type", "application/json"),
                    data,
                )
        except urllib.error.HTTPError as exc:
            self._write_raw(
                exc.code,
                "application/json",
                exc.read(),
            )
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, {"error": f"upstream failed: {exc}"})


def main() -> int:
    if not API_KEY and not os.environ.get("PROXY_ALLOW_NO_KEY"):
        print(
            "OPENROUTER_API_KEY assente (o imposta PROXY_ALLOW_NO_KEY=1 per "
            "inoltrare l'Authorization del client)",
            file=sys.stderr,
        )
        return 2
    server = ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), Handler)
    print(
        f"provider proxy su http://127.0.0.1:{PROXY_PORT}/v1 "
        f"→ {PROXY_UPSTREAM} (provider={PROXY_PROVIDER}, "
        f"modelli={','.join(PROXY_MODELS) or '-'})",
        file=sys.stderr,
    )
    print(f"NOESIS_PROXY_READY {PROXY_PORT}", file=sys.stdout, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
