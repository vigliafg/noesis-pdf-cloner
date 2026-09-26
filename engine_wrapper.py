"""Launcher sperimentale del motore: applica le patch e delega a ``pdf2zh_next``.

Uso (identico al binario, ma eseguito con il Python di ``.venv2``):

    .venv2/bin/python engine_wrapper.py <argomenti pdf2zh_next>

Il desktop lo usa al posto del binario quando la feature ``fast_engine`` è
attiva. **Fail-safe**: se le patch non sono applicabili (BabelDOC assente o
cambiato), lo script prosegue comunque eseguendo il motore normale; l'unico
effetto è un messaggio su stderr.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_local_import() -> None:
    """Rende importabile ``engine_patch`` accanto a questo file (anche frozen)."""
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))


def main() -> None:
    _ensure_local_import()
    try:
        import engine_patch  # noqa: PLC0415

        state = engine_patch.apply_all()
        sys.stderr.write(
            f"[noesis fast-engine] engine_patch v{engine_patch.PATCH_VERSION}: {state}\n"
        )
    except Exception as exc:  # noqa: BLE001 — un fallimento non deve bloccare
        sys.stderr.write(f"[noesis fast-engine] patch non applicate: {exc}\n")

    from pdf2zh_next.main import cli  # noqa: PLC0415

    cli()


if __name__ == "__main__":
    main()
