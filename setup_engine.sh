#!/usr/bin/env bash
# Installa il motore di clonazione pdf2zh_next v2 accanto all'app (.venv2).
# Usalo dopo aver scaricato una release standalone di Noesis PDF Cloner,
# eseguendolo nella stessa cartella dell'eseguibile.
#
#   ./setup_engine.sh
#
# Richiede 'uv' (https://docs.astral.sh/uv/): se manca, viene installato
# automaticamente con l'installer ufficiale.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# uv: usa quello presente, altrimenti scarica il binario ufficiale.
if ! command -v uv >/dev/null 2>&1; then
  echo "uv non trovato: lo installo (installer ufficiale astral.sh)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # L'installer mette uv in ~/.local/bin e aggiorna il profilo; nella sessione
  # corrente potrebbe non essere ancora nel PATH.
  export PATH="$HOME/.local/bin:$PATH"
fi

UV="${UV:-uv}"
if ! command -v "$UV" >/dev/null 2>&1; then
  echo "ERRORE: 'uv' non disponibile. Installalo da https://docs.astral.sh/uv/" >&2
  exit 1
fi

if [ ! -d .venv2 ]; then
  "$UV" venv --python python3.12 .venv2
fi
"$UV" pip install --python .venv2/bin/python -q pdf2zh_next

echo "Motore installato in: $HERE/.venv2/bin/pdf2zh_next"
echo "Noesis PDF Cloner lo rileverà automaticamente."
