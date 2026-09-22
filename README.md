# noesis-pdf-cloner

App desktop **PyQt6** derivata da **noesis-pdf-reader-lite**: apre un PDF e mostra
**a sinistra la pagina originale**, **a destra la stessa pagina tradotta nella
lingua scelta con il layout preservato**. Il clone è prodotto da
**[pdf2zh_next v2](https://github.com/PDFMathTranslate/PDFMathTranslate-next)**
(typesetter BabelDOC), con i tre motori di traduzione di **pdfcloner**:

| Motore | Descrizione |
|---|---|
| **Google** | catena gratuita `gtranslate_cli.py`: `dict-chrome-ex` → `translate-pa` → `gtx` → Microsoft → LLM |
| **Bing** | traduttore Bing built-in di pdf2zh_next |
| **LLM** | OpenRouter (`--openai`), richiede `OPENROUTER_API_KEY` |

La traduzione è **on-demand pagina per pagina** (solo ciò che guardi) con
**cache su disco** separata per motore e coppia linguistica: al secondo accesso
la pagina è istantanea.

## Cosa include

- Vista affiancata: pagina originale (sinistra) + clone tradotto (destra).
- Navigazione completa sul PDF: **numero di pagina**, **indice (TOC)**,
  **pagina precedente / successiva** e scorciatoie da tastiera.
- Zoom sincronizzato sui due pannelli (`🔍+` / `🔍−`, Ctrl+±, Ctrl+0).
- Radio dei motori (Google / Bing / LLM) nel pannello destro, con spinner e
  avviso dei fallback (Microsoft/LLM) della catena gratuita. La barra dei
  motori è **collassabile** (chevron in alto a destra): al collasso lo spazio
  torna al viewport e la pagina si riadatta; lo stato è ricordato tra sessioni.
- ⚙️ Impostazioni: lingua UI (it/en/fr/de/es), lingua del documento (origine) e
  della traduzione (destinazione), motore, zoom di avvio, "riprendi dall'ultima
  pagina" e percorso dell'eseguibile `pdf2zh_next`.
- 💾 **Esporta** le pagine tradotte (corrente o intervallo) come **un unico PDF**
  oppure come **pagine singole in un archivio ZIP**; le pagine non ancora in
  cache possono essere tradotte prima, con annullamento immediato.
- ❓ Guida online (`docs/help/`, pubblicata su GitHub Pages).

> Il codice di estrazione testo/markdown e gli strumenti a zone di lite sono
> **mantenuti dormienti** (UI nascosta) e verranno riattivati con l'integrazione
> futura del motore **Docling**.

## Installazione da sorgente

Serve **Python 3.12** e [uv](https://docs.astral.sh/uv/). Lo script crea due
ambienti virtuali separati (motivo: `pdf2zh_next` non convive con PyQt6):

| venv | Contenuto |
|---|---|
| `.venv` | app: PyQt6 + PyMuPDF |
| `.venv2` | motore di clonazione: `pdf2zh_next` v2 (BabelDOC) |

```bash
./run.sh                 # crea i venv, installa tutto e apre la GUI
./run.sh /percorso/file.pdf
```

Oppure a mano:

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
uv venv --python 3.12 .venv2 && uv pip install --python .venv2/bin/python pdf2zh_next
.venv/bin/python main.py
```

La chiave `OPENROUTER_API_KEY` serve solo per il fallback LLM della catena
gratuita e per il motore **LLM**.

## Release standalone

Le release sono compilate con **PyInstaller** su runner nativi (Windows x64,
macOS x64/arm64, Linux AppImage) dal workflow
[`.github/workflows/release.yml`](.github/workflows/release.yml) (push di un tag
`v*` o avvio manuale).

Il binario contiene la GUI; il motore `pdf2zh_next` va installato **accanto
all'eseguibile** con lo script incluso nella release:

```bash
./setup_engine.sh                                        # Linux / macOS
powershell -ExecutionPolicy Bypass -File .\setup_engine.ps1   # Windows
```

L'app rileva automaticamente `.venv2` accanto all'eseguibile; in alternativa si
imposta il percorso in ⚙️ Impostazioni.

## Test

```bash
python3 -m unittest discover -s tests -v
```

## Struttura

- `main.py` — GUI PyQt6 (pannelli, navigazione, zoom, Impostazioni).
- `clone_engine.py` — pipeline split → `pdf2zh_next` → cache per engine.
- `gtranslate_cli.py` — catena gratuita Google→Microsoft→LLM per `--clitranslator`.
- `i18n.py` — stringhe UI (5 lingue) e config.
- `layout_engine.py` — engine adattativo dei fix di layout (dormiente).
- `tests/` — test di regressione.
