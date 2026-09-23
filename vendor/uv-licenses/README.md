# uv (bundled third-party)

`noesis-pdf-cloner` include il binario ufficiale **`uv`** per installare il
motore di traduzione `pdf2zh_next` senza richiedere che l'utente lo abbia già.

- Progetto: <https://github.com/astral-sh/uv>
- Licenza: **MIT** *oppure* **Apache-2.0** (a scelta) — testi in questa cartella.
- Versione inclusa: vedi `UV_VERSION` in [`../fetch_uv.py`](../fetch_uv.py).
- Il binario **non** è versionato: viene scaricato a build time da
  `vendor/fetch_uv.py` (versione pinnata + verifica `sha256`) e incluso nel
  bundle PyInstaller.
