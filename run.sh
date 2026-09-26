#!/usr/bin/env bash
# Noesis PDF Cloner — launcher con percorsi relativi allo script.
# Crea (se mancano) i due ambienti virtuali e avvia la GUI:
#   .venv   → app (PyQt6 + PyMuPDF)
#   .venv2  → motore di clonazione pdf2zh_next v2 (richiede Python <3.14)
set -e
cd "$(dirname "$0")"

UV="${UV:-uv}"
if ! command -v "$UV" >/dev/null 2>&1; then
  echo "ERRORE: 'uv' non trovato. Installa uv (https://docs.astral.sh/uv/)" >&2
  echo "oppure crea a mano i venv .venv e .venv2 (vedi README)." >&2
  exit 1
fi

# venv app (PyQt6 + PyMuPDF + markdown)
if [ ! -d .venv ]; then
  "$UV" venv --python python3.12 .venv
fi
"$UV" pip install --python .venv/bin/python -q -r requirements.txt

# venv motore di clonazione (pdf2zh_next v2: typesetting BabelDOC)
if [ ! -d .venv2 ]; then
  "$UV" venv --python python3.12 .venv2
fi
"$UV" pip install --python .venv2/bin/python -q pdf2zh_next==2.9.0

exec .venv/bin/python main.py "$@"
