#!/usr/bin/env python3
"""Banco di misura per una pagina del motore di clonazione.

Misura il tempo **reale** di una pagina usando lo stesso percorso di produzione
(``clone_engine.CloneEngine``), con cache ripulita a ogni run così da misurare
il lavoro del motore e non la cache.

Esempi:

    # baseline Mercury, 3 run
    .venv/bin/python tools/bench_page.py --page 3575 --runs 3

    # motore veloce + preset rapido
    .venv/bin/python tools/bench_page.py --page 3575 --runs 3 \
        --fast --fast-flags

    # confronto modello / pool
    .venv/bin/python tools/bench_page.py --page 3575 --runs 3 \
        --model openai/gpt-oss-120b --workers 8 --label "gpt-oss-8"

Scrive un JSONL (una riga per run) e stampa un riepilogo min/mediana/max.
Richiede ``OPENROUTER_API_KEY`` per il motore ``llm``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import clone_engine  # noqa: E402

_TOKEN_RE = re.compile(r"Total Token Usage:\s*Total\s*([\d,]+)", re.IGNORECASE)


def _child_cpu_seconds() -> float:
    """CPU (user+sys) consumata dai processi figli finora."""
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def _build_engine(args, cache_root: Path) -> clone_engine.CloneEngine:
    engine = clone_engine.CloneEngine(
        cache_root,
        pdf2zh_bin=args.pdf2zh or None,
        max_concurrent=1,
    )
    engine.set_document(args.pdf)
    engine.lang_in = args.lang_in
    engine.lang_out = args.lang_out
    if args.model:
        engine.llm_model = args.model
    if args.base_url:
        engine.llm_base_url = args.base_url
    engine.llm_pool_workers = max(1, int(args.workers))
    engine.fast_engine = bool(args.fast)
    engine.fast_flags = bool(args.fast_flags)
    engine.ignore_cache = bool(args.fresh)
    if args.reasoning_effort:
        engine.llm_reasoning_effort = args.reasoning_effort
    engine.llm_json_mode = bool(args.json_mode)
    return engine


def _instrument_tokens(engine: clone_engine.CloneEngine) -> dict:
    """Registra l'ultimo ``CompletedProcess`` per estrarre i token usati."""
    state: dict = {"last": None}
    original = engine._run_engine

    def wrapped(cmd, env, cancel_event=None):  # noqa: ANN001
        result = original(cmd, env, cancel_event)
        state["last"] = result
        return result

    engine._run_engine = wrapped  # type: ignore[method-assign]
    return state


def _tokens_from(state: dict) -> int | None:
    result = state.get("last")
    if result is None:
        return None
    blob = (getattr(result, "stdout", "") or "") + "\n" + (
        getattr(result, "stderr", "") or ""
    )
    match = _TOKEN_RE.search(blob)
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", default=str(ROOT / "ha22.pdf"))
    parser.add_argument("--page", type=int, required=True, help="indice 0-based")
    parser.add_argument("--engine", default="llm",
                        choices=list(clone_engine.ENGINES))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--model", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--lang-in", default="en")
    parser.add_argument("--lang-out", default="it")
    parser.add_argument("--pdf2zh", default="")
    parser.add_argument("--fast", action="store_true",
                        help="usa il wrapper con le patch runtime")
    parser.add_argument("--fast-flags", action="store_true",
                        help="preset 'traduzione rapida' (B2)")
    parser.add_argument("--fresh", action="store_true",
                        help="ignora la cache interna del motore (--ignore-cache)")
    parser.add_argument("--reasoning-effort", default="",
                        help="LLM: minimal|low|medium|high (fast_engine)")
    parser.add_argument("--json-mode", action="store_true",
                        help="LLM: abilita JSON mode (fast_engine)")
    parser.add_argument("--label", default="")
    parser.add_argument("--out", default=str(ROOT / "tools" / "bench_results.jsonl"))
    parser.add_argument("--cache-dir", default="")
    args = parser.parse_args()

    if not Path(args.pdf).is_file():
        print(f"PDF non trovato: {args.pdf}", file=sys.stderr)
        return 2
    if args.engine == "llm" and not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY assente: impossibile testare il motore llm",
              file=sys.stderr)
        return 2

    cache_root = (
        Path(args.cache_dir) if args.cache_dir
        else Path(tempfile.mkdtemp(prefix="noesis-bench-"))
    )
    engine = _build_engine(args, cache_root)
    if not engine.available():
        print("pdf2zh_next non trovato (usa --pdf2zh o installa il motore)",
              file=sys.stderr)
        return 2
    state = _instrument_tokens(engine)

    label = args.label or f"{args.engine}/{args.model or engine.llm_model}"
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    walls: list[float] = []
    cpus: list[float] = []
    ok = 0
    print(f"# bench {label} — pagina {args.page} — {args.runs} run "
          f"— fast={args.fast} flags={args.fast_flags} workers={args.workers}")
    for i in range(1, args.runs + 1):
        engine.purge_page_cache(args.engine, args.page)
        wall0, cpu0 = time.monotonic(), _child_cpu_seconds()
        out = engine.translate_page(args.page, args.engine)
        wall = time.monotonic() - wall0
        cpu = _child_cpu_seconds() - cpu0
        tokens = _tokens_from(state)
        success = bool(out and Path(out).is_file())
        if success:
            ok += 1
            walls.append(wall)
            cpus.append(cpu)
        record = {
            "label": label,
            "page": args.page,
            "engine": args.engine,
            "model": args.model or engine.llm_model,
            "workers": args.workers,
            "fast": args.fast,
            "fast_flags": args.fast_flags,
            "fresh": args.fresh,
            "reasoning_effort": args.reasoning_effort,
            "json_mode": args.json_mode,
            "run": i,
            "wall_s": round(wall, 3),
            "child_cpu_s": round(cpu, 3),
            "tokens": tokens,
            "success": success,
            "status": engine.status(args.page, args.engine),
            "ts": time.time(),
        }
        with out_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"  run {i}: wall={wall:6.2f}s cpu={cpu:6.2f}s "
              f"tokens={tokens} ok={success} [{record['status']}]")

    if not walls:
        print("Nessuna run riuscita.", file=sys.stderr)
        return 1

    def _fmt(values: list[float]) -> str:
        return (f"min={min(values):.2f} med={statistics.median(values):.2f} "
                f"max={max(values):.2f} mean={statistics.mean(values):.2f}")

    print(f"\n## {label}: {ok}/{args.runs} ok")
    print(f"   wall  {_fmt(walls)}")
    print(f"   cpu   {_fmt(cpus)}")
    print(f"   risultati in {out_path}")
    return 0 if ok == args.runs else 1


if __name__ == "__main__":
    raise SystemExit(main())
