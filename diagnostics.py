"""Diagnostica di preflight (pura Python, senza Qt).

Risponde a: *"questo sistema può tradurre, e con quali motori?"* separando i
tre assi — **motore** (assente/presente/funzionante), **chiave**
(assente/presente/valida), **modello** (risponde/non risponde).

Ogni controllo è una funzione che ritorna un :class:`CheckResult`; il
:class:`DiagnosticsRunner` li esegue in sequenza. Il modulo non dipende da Qt,
quindi è testabile a parte e **riusabile nel servizio** (`app/diagnostics.py`):
stessi `id`, stessi `code`, stessi suggerimenti (`fix`).

La UI traduce `id`/`code` con i18n; i codici di chiave/modello coincidono con
quelli di :func:`clone_engine.classify_engine_failure`, così il linguaggio della
diagnostica e quello degli errori a runtime restano coerenti.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import clone_engine
import keystore

__all__ = [
    "Status",
    "CheckResult",
    "DiagnosticsContext",
    "DiagnosticsRunner",
    "run_all",
    "overall_status",
    "check_data_dir",
    "check_disk",
    "check_pymupdf",
    "check_uv",
    "check_engine_bin",
    "check_engine_run",
    "check_openrouter_reachable",
    "check_key_present",
    "check_key_valid",
    "check_key_credits",
    "check_llm_model",
    "check_free_chain",
]

DEFAULT_BASE_URL = clone_engine.DEFAULT_BASE_URL
DEFAULT_MODEL = clone_engine.DEFAULT_MODEL

# Spazio minimo stimato per motore (.venv2 ~1,1 GB) + cache.
ENGINE_NEED_BYTES = 1_500_000_000


class Status:
    """Esiti possibili di un controllo."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class CheckResult:
    """Esito di un singolo controllo."""

    id: str
    status: str
    code: str = "ok"
    message: str = ""          # dettaglio breve, non tradotto (log/test)
    fix: str = ""              # azione suggerita: install_engine/enter_key/...
    duration_ms: int = 0
    data: dict = field(default_factory=dict)


@dataclass
class DiagnosticsContext:
    """Parametri dei controlli (dipendenze già risolte dal chiamante)."""

    data_dir: Path
    uv_bin: Path | None = None
    engine_bin: Path | None = None
    key: str = ""
    key_source: str = "none"          # "file" | "env" | "none"
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    engine_name: str = "llm"          # motore selezionato
    timeout_net: float = 8.0
    timeout_proc: float = 20.0
    want_llm_checks: bool = True
    want_free_checks: bool = True
    free_translator: Callable[[str], str | None] | None = None


# ── helper di rete ──────────────────────────────────────────────────────────


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    payload: dict | None = None,
    timeout: float = 8.0,
) -> tuple[int | None, str]:
    """Ritorna ``(status, body)``; ``status=None`` se non raggiungibile."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = ""
        with contextlib.suppress(Exception):
            body = exc.read().decode("utf-8", "replace")
        return exc.code, body
    except Exception:
        return None, ""


def _timed(fn: Callable[[], CheckResult]) -> CheckResult:
    started = time.monotonic()
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 — un check non deve mai crashare
        result = CheckResult("?", Status.FAIL, code="exception", message=str(exc))
    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result


# ── ambiente locale ─────────────────────────────────────────────────────────


def check_data_dir(path: Path) -> CheckResult:
    """La cartella dati è scrivibile (config, cache, log)?"""
    def run() -> CheckResult:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".diag-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            return CheckResult(
                "env.data", Status.FAIL, code="data_unwritable",
                message=str(exc), data={"path": str(path)},
            )
        return CheckResult(
            "env.data", Status.OK, code="ok", data={"path": str(path)}
        )

    return _timed(run)


def check_disk(path: Path, required_bytes: int = ENGINE_NEED_BYTES) -> CheckResult:
    """Spazio libero sufficiente per installare il motore e la cache?"""
    def run() -> CheckResult:
        try:
            usage = shutil.disk_usage(str(path))
        except OSError as exc:
            return CheckResult(
                "env.disk", Status.WARN, code="disk_unknown", message=str(exc)
            )
        free = usage.free
        info = {"free_bytes": free, "required_bytes": required_bytes}
        if free < required_bytes:
            return CheckResult(
                "env.disk", Status.WARN, code="disk_low",
                message=f"libero {free} < richiesto {required_bytes}", data=info,
            )
        return CheckResult("env.disk", Status.OK, code="ok", data=info)

    return _timed(run)


def check_pymupdf() -> CheckResult:
    """PyMuPDF è importabile (rendering/estrazione)? Fa parte del bundle."""
    def run() -> CheckResult:
        try:
            import pymupdf  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                "env.pymupdf", Status.FAIL, code="pymupdf_missing", message=str(exc)
            )
        return CheckResult("env.pymupdf", Status.OK)

    return _timed(run)


def check_uv(uv_bin: Path | None) -> CheckResult:
    """``uv`` è presente e funzionante (serve a installare il motore)?"""
    def run() -> CheckResult:
        if uv_bin is None:
            return CheckResult(
                "uv", Status.WARN, code="uv_missing", fix="install_uv"
            )
        try:
            probe = subprocess.run(
                [str(uv_bin), "--version"],
                capture_output=True, text=True, timeout=20,
            )
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                "uv", Status.WARN, code="uv_not_runnable", message=str(exc)
            )
        version = (probe.stdout or probe.stderr or "").strip()
        if probe.returncode != 0:
            return CheckResult(
                "uv", Status.WARN, code="uv_not_runnable",
                message=version[:200],
            )
        return CheckResult(
            "uv", Status.OK, code="ok", message=version,
            data={"version": version},
        )

    return _timed(run)


# ── motore ──────────────────────────────────────────────────────────────────


def check_engine_bin(engine_bin: Path | None) -> CheckResult:
    """``pdf2zh_next`` è stato trovato (auto-rilevamento o override)?"""
    if engine_bin is None:
        return CheckResult(
            "engine.bin", Status.FAIL, code="engine_missing",
            fix="install_engine",
        )
    return CheckResult(
        "engine.bin", Status.OK, code="ok", data={"path": str(engine_bin)}
    )


def check_engine_run(
    engine_bin: Path | None, timeout: float = 20.0
) -> CheckResult:
    """Il motore risponde (``--help``) e non è corrotto?"""
    def run() -> CheckResult:
        if engine_bin is None:
            return CheckResult(
                "engine.run", Status.SKIP, code="engine_missing",
                fix="install_engine",
            )
        try:
            probe = subprocess.run(
                [str(engine_bin), "--help"],
                capture_output=True, text=True, timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                "engine.run", Status.WARN, code="engine_not_runnable",
                message=str(exc), fix="install_engine",
            )
        out = (probe.stdout or "") + (probe.stderr or "")
        if probe.returncode != 0:
            return CheckResult(
                "engine.run", Status.WARN, code="engine_not_runnable",
                message=f"exit {probe.returncode}: {out.strip()[:200]}",
                fix="install_engine",
            )
        return CheckResult("engine.run", Status.OK, code="ok")

    return _timed(run)


# ── rete / chiave / modello ─────────────────────────────────────────────────


def check_openrouter_reachable(
    base_url: str = DEFAULT_BASE_URL, timeout: float = 8.0
) -> CheckResult:
    """OpenRouter è raggiungibile? (GET /key senza auth: 401 = raggiungibile)."""
    def run() -> CheckResult:
        status, _ = _http_json(base_url.rstrip("/") + "/key", timeout=timeout)
        if status is None:
            return CheckResult(
                "net.openrouter", Status.FAIL, code="network",
                fix="check_network",
            )
        if status >= 500:
            return CheckResult(
                "net.openrouter", Status.WARN, code="network",
                message=f"HTTP {status}", fix="check_network",
            )
        return CheckResult(
            "net.openrouter", Status.OK, code="ok", data={"http": status}
        )

    return _timed(run)


def check_key_present(key: str, source: str = "none") -> CheckResult:
    """C'è una chiave e da dove proviene (file salvato / variabile di sistema)?"""
    if not (key or "").strip():
        return CheckResult(
            "key.present", Status.WARN, code="key_missing", fix="enter_key"
        )
    return CheckResult(
        "key.present", Status.OK, code=f"key_{source}",
        data={"source": source, "masked": keystore.KeyStore.mask(key)},
    )


def check_key_valid(
    key: str, base_url: str = DEFAULT_BASE_URL, timeout: float = 8.0
) -> CheckResult:
    """La chiave è valida su OpenRouter (endpoint ``/key``)?"""
    def run() -> CheckResult:
        if not (key or "").strip():
            return CheckResult(
                "key.valid", Status.SKIP, code="key_missing", fix="enter_key"
            )
        status, _ = _http_json(
            base_url.rstrip("/") + "/key",
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        if status is None:
            return CheckResult(
                "key.valid", Status.WARN, code="network", fix="check_network"
            )
        if status == 200:
            return CheckResult("key.valid", Status.OK, code="key_valid")
        if status in (401, 403):
            return CheckResult(
                "key.valid", Status.FAIL, code="invalid_key",
                message=f"HTTP {status}", fix="enter_key",
            )
        return CheckResult(
            "key.valid", Status.FAIL, code=f"http_{status}",
            message=f"HTTP {status}",
        )

    return _timed(run)


def check_key_credits(
    key: str, base_url: str = DEFAULT_BASE_URL, timeout: float = 8.0
) -> CheckResult:
    """Legge il credito residuo da ``/key`` (warn se esaurito)."""
    def run() -> CheckResult:
        if not (key or "").strip():
            return CheckResult(
                "key.credits", Status.SKIP, code="key_missing", fix="enter_key"
            )
        status, body = _http_json(
            base_url.rstrip("/") + "/key",
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        if status is None:
            return CheckResult(
                "key.credits", Status.WARN, code="network", fix="check_network"
            )
        if status in (401, 403):
            return CheckResult(
                "key.credits", Status.SKIP, code="invalid_key", fix="enter_key"
            )
        if status != 200:
            return CheckResult(
                "key.credits", Status.WARN, code=f"http_{status}",
                message=f"HTTP {status}",
            )
        try:
            info = json.loads(body).get("data", {}) or {}
        except Exception:  # noqa: BLE001
            info = {}
        remaining = info.get("limit_remaining")
        data = {
            "limit": info.get("limit"),
            "usage": info.get("usage"),
            "limit_remaining": remaining,
            "is_free_tier": info.get("is_free_tier"),
        }
        if isinstance(remaining, (int, float)) and remaining <= 0:
            return CheckResult(
                "key.credits", Status.WARN, code="credits_low",
                message=f"credito residuo {remaining}", fix="add_credits",
                data=data,
            )
        return CheckResult("key.credits", Status.OK, code="ok", data=data)

    return _timed(run)


def check_llm_model(
    key: str,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 20.0,
) -> CheckResult:
    """Il modello LLM **risponde**? (una chat completion minima)."""
    def run() -> CheckResult:
        if not (key or "").strip():
            return CheckResult(
                "llm.model", Status.SKIP, code="key_missing", fix="enter_key"
            )
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        status, body = _http_json(
            base_url.rstrip("/") + "/chat/completions",
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            payload=payload,
            timeout=timeout,
        )
        if status is None:
            return CheckResult(
                "llm.model", Status.WARN, code="network", fix="check_network"
            )
        mapping = {
            200: (Status.OK, "model_ok"),
            401: (Status.FAIL, "invalid_key"),
            403: (Status.FAIL, "forbidden"),
            402: (Status.FAIL, "no_credits"),
            429: (Status.WARN, "rate_limited"),
            404: (Status.FAIL, "model_not_found"),
        }
        if status in mapping:
            level, code = mapping[status]
            fix = {
                "invalid_key": "enter_key",
                "forbidden": "enter_key",
                "no_credits": "add_credits",
                "rate_limited": "retry",
                "model_not_found": "choose_model",
            }.get(code, "")
            return CheckResult(
                "llm.model", level, code=code, message=f"HTTP {status}",
                fix=fix, data={"http": status, "model": model},
            )
        # Fallback: classifica dal corpo (es. errori senza codice HTTP utile).
        code = clone_engine.classify_engine_failure(body)
        if code != "unknown":
            return CheckResult(
                "llm.model", Status.FAIL, code=code,
                message=f"HTTP {status}", data={"http": status},
            )
        return CheckResult(
            "llm.model", Status.FAIL, code=f"http_{status}",
            message=f"HTTP {status}",
        )

    return _timed(run)


def check_free_chain(
    text: str = "hello",
    src: str = "en",
    dst: str = "it",
    timeout: float = 15.0,
    translator: Callable[[str], str | None] | None = None,
) -> CheckResult:
    """I motori gratuiti (catena Google + fallback) rispondono?"""
    def run() -> CheckResult:
        fn = translator
        if fn is None:
            try:
                import gtranslate_cli

                os.environ.setdefault("PDF_LANG_IN", src)
                os.environ.setdefault("PDF_LANG_OUT", dst)
                fn = gtranslate_cli.translate
            except Exception as exc:  # noqa: BLE001
                return CheckResult(
                    "free.chain", Status.WARN, code="free_unavailable",
                    message=str(exc),
                )
        try:
            out = fn(text)
        except Exception as exc:  # noqa: BLE001
            return CheckResult(
                "free.chain", Status.WARN, code="free_unavailable",
                message=str(exc),
            )
        if out:
            return CheckResult(
                "free.chain", Status.OK, code="ok", data={"sample": out[:60]}
            )
        return CheckResult(
            "free.chain", Status.WARN, code="free_unavailable",
            fix="retry",
        )

    return _timed(run)


# ── runner ──────────────────────────────────────────────────────────────────


def _build_checks(
    ctx: DiagnosticsContext,
) -> list[tuple[str, Callable[[], CheckResult]]]:
    checks: list[tuple[str, Callable[[], CheckResult]]] = [
        ("env.data", lambda: check_data_dir(ctx.data_dir)),
        ("env.disk", lambda: check_disk(ctx.data_dir)),
        ("env.pymupdf", check_pymupdf),
        ("uv", lambda: check_uv(ctx.uv_bin)),
        ("engine.bin", lambda: check_engine_bin(ctx.engine_bin)),
        ("engine.run", lambda: check_engine_run(ctx.engine_bin, ctx.timeout_proc)),
    ]
    if ctx.engine_name == "llm" and ctx.want_llm_checks:
        checks += [
            ("net.openrouter",
             lambda: check_openrouter_reachable(ctx.base_url, ctx.timeout_net)),
            ("key.present", lambda: check_key_present(ctx.key, ctx.key_source)),
            ("key.valid",
             lambda: check_key_valid(ctx.key, ctx.base_url, ctx.timeout_net)),
            ("key.credits",
             lambda: check_key_credits(ctx.key, ctx.base_url, ctx.timeout_net)),
            ("llm.model",
             lambda: check_llm_model(
                 ctx.key, ctx.model, ctx.base_url, ctx.timeout_net
             )),
        ]
    if ctx.want_free_checks:
        checks.append(
            ("free.chain", lambda: check_free_chain(
                translator=ctx.free_translator
            ))
        )
    return checks


class DiagnosticsRunner:
    """Esegue i controlli in sequenza, con callback per risultato."""

    def __init__(
        self,
        ctx: DiagnosticsContext,
        on_result: Callable[[CheckResult], None] | None = None,
        cancel: Callable[[], bool] | None = None,
    ):
        self.ctx = ctx
        self.on_result = on_result
        self.cancel = cancel

    def run(self, only: Iterable[str] | None = None) -> list[CheckResult]:
        wanted = set(only) if only else None
        results: list[CheckResult] = []
        for check_id, fn in _build_checks(self.ctx):
            if self.cancel is not None and self.cancel():
                break
            if wanted is not None and check_id not in wanted:
                continue
            result = fn()
            if not result.id or result.id == "?":
                result.id = check_id
            results.append(result)
            if self.on_result is not None:
                self.on_result(result)
        return results


def run_all(
    ctx: DiagnosticsContext,
    on_result: Callable[[CheckResult], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    only: Iterable[str] | None = None,
) -> list[CheckResult]:
    """Scorciatoia: esegue tutti i controlli per il contesto dato."""
    return DiagnosticsRunner(ctx, on_result, cancel).run(only)


def overall_status(results: Iterable[CheckResult]) -> str:
    """Esito complessivo: fail > warn > ok (skip ignorati)."""
    statuses = {r.status for r in results}
    if Status.FAIL in statuses:
        return Status.FAIL
    if Status.WARN in statuses:
        return Status.WARN
    if Status.OK in statuses:
        return Status.OK
    return Status.SKIP
