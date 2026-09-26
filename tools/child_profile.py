#!/usr/bin/env python3
"""Profila il **processo figlio** in cui BabelDOC svolge il lavoro pesante.

``pdf2zh_next`` esegue la traduzione in un ``multiprocessing.Process``: il
profiler applicato al padre misura solo l'attesa. Questo wrapper sostituisce
``pdf2zh_next.high_level._translate_wrapper`` con una versione profilata
(solo a runtime, nessuna modifica al motore) e delega alla CLI.

Esempio:

    .venv2/bin/python tools/child_profile.py --patch --out child.prof -- \
        page.pdf --lang-in en --lang-out it --output out/ ... \
        --openai --openai-model inception/mercury-2.5 --pool-max-workers 4

Poi:

    .venv2/bin/python -c "import pstats; \\
        pstats.Stats('child.prof').sort_stats('cumulative').print_stats(30)"
"""

from __future__ import annotations

import argparse
import cProfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="child.prof",
                        help="file di output del profilo")
    parser.add_argument("--patch", action="store_true",
                        help="applica engine_patch prima di profilare")
    parser.add_argument("args", nargs=argparse.REMAINDER,
                        help="argomenti per pdf2zh_next (dopo '--')")
    ns = parser.parse_args()

    passthrough = list(ns.args)
    if passthrough and passthrough[0] == "--":
        passthrough = passthrough[1:]
    if not passthrough:
        parser.error("servono gli argomenti di pdf2zh_next dopo '--'")

    if ns.patch:
        import engine_patch  # noqa: PLC0415

        state = engine_patch.apply_all()
        sys.stderr.write(f"[child_profile] engine_patch: {state}\n")

    import pdf2zh_next.high_level as high_level  # noqa: PLC0415

    original = high_level._translate_wrapper
    prof_out = ns.out

    def _profiled(*call_args, **call_kwargs):  # noqa: ANN001
        cProfile.runctx(
            "original(*call_args, **call_kwargs)",
            {"original": original},
            {"call_args": call_args, "call_kwargs": call_kwargs},
            prof_out,
        )

    high_level._translate_wrapper = _profiled

    import pdf2zh_next.main as main_module  # noqa: PLC0415

    sys.argv = ["pdf2zh_next", *passthrough]
    main_module.cli()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
