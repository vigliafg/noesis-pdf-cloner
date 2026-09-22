# AGENTS.md — istruzioni per l'agente

Queste istruzioni valgono per il repository **`noesis-pdf-cloner`** (app **desktop**).

## Lingua
Rispondere in **italiano**.

## Cos'è questo repository

`noesis-pdf-cloner` è la **versione desktop (GUI PyQt6)** del progetto. Il repository
sorella è **[`noesis-pdf-cloner-service`](https://github.com/vigliafg/noesis-pdf-cloner-service)**
(tipicamente in `../noesis-pdf-cloner-service` sulla stessa macchina), che contiene le
altre due versioni dello stesso programma:

| Versione | Repository | Forma |
|---|---|---|
| **Desktop** (questa) | `noesis-pdf-cloner` | GUI PyQt6, traduzione **on-demand pagina per pagina** |
| **Server** | `noesis-pdf-cloner-service` | API FastAPI + frontend web, job asincroni |
| **Headless / CLI** | `noesis-pdf-cloner-service` | `./run-cli.sh` (batch, `tqdm`), senza server |

Tutte e tre condividono **lo stesso motore di clonazione**: `pdf2zh_next` v2
(BabelDOC) invocato come **subprocess** in un venv dedicato (`.venv2`, Python 3.12),
con i tre motori di traduzione `google` / `bing` / `llm`.

> Nota: il nome del repo sorella è `noesis-pdf-cloner-service` (non
> `noesis-pdf-cloner`, che è questo repo). Se il path `../noesis-pdf-cloner-service`
> non esiste, segnalalo invece di assumere che il repo sorella sia assente.

## Regola di allineamento con il servizio

Qualunque modifica che riguarda le **funzionalità del programma** (comportamento del
motore/pipeline, traduzione, gestione pagine/cache, output, catena dei traduttori,
elenco motori/lingue, convenzione degli indici pagina, ecc.) va **riverberata anche
nelle parti di codice interessate di `noesis-pdf-cloner-service`**, così le tre
versioni restano allineate. Vale anche al contrario: una modifica funzionale fatta nel
servizio va riportata qui nel desktop.

Procedura:
1. Individua la controparte nella tabella qui sotto.
2. Applica la modifica equivalente anche nell'altro repository, rispettando lo stile
   di quel repo (qui: PyQt6, `i18n.py`, `clone_engine.py`; là: FastAPI, `app/pipeline.py`).
3. Verifica/aggiorna i **test** in entrambi i repository.
4. Riporta esplicitamente cosa è stato allineato e cosa no (e perché).

### Mappa delle parti condivise

| `noesis-pdf-cloner` (desktop, qui) | `noesis-pdf-cloner-service` | Cosa tenere allineato |
|---|---|---|
| `clone_engine.py` | `app/engine.py` | flag `pdf2zh_next`, split, `doc_key`, schema/percorsi cache, cancel, auto-rilevamento binario |
| `gtranslate_cli.py` | `app/gtranslate_cli.py` | catena gratuita (endpoint, fallback Microsoft/LLM, log eventi) |
| `i18n.py` (`TRANSLATION_ENGINES`, `TRANSLATION_LANGUAGES`) | `app/models.py` (`ENGINES`, `LANGUAGES`) | elenco motori e lingue |
| logica pagine/selezione del desktop | `app/pages.py` | semantica intervalli e convenzione 0-based/1-based |
| gestione etichette/numerazione pagina | `app/pagelabels.py` | doppia numerazione |
| flusso export di `main.py` | `app/pipeline.py`, `app/cli.py` | ordine pagine, blocchi, PDF unito / pagine singole, nomi file |

**Attenzione**: le due implementazioni sono **adattamenti** l'una dell'altra, non un
modulo letteralmente condiviso: la superficie API e le parti di contorno (cancel,
lock, anteprima, export ZIP, chiavi di cache) possono divergere liberamente. Ciò che
**deve** restare allineato è il **motore in sé**: invocazione di `pdf2zh_next`, flag,
catena dei traduttori, elenco motori/lingue e convenzione degli indici. In caso di
dubbio su cosa sia "motore" e cosa sia "contorno", chiedere conferma.

### Cosa NON riverberare (specifico del desktop)
GUI PyQt6 (`main.py`), pannelli e widget, `i18n.py` runtime, finestra Impostazioni,
navigazione/zoom, rendering PyMuPDF nella UI, release PyInstaller/NSIS/AppImage,
workflow GitHub Pages della guida: non hanno controparte nel servizio.

## Convenzioni tecniche
- Due venv: `.venv` (app: PyQt6 + PyMuPDF) e `.venv2` (motore `pdf2zh_next`, **Python 3.12**).
  Non convivono: il motore è invocato come **subprocess CLI**.
- **Indici pagina 0-based** nel motore e nella UI (vedi il fix off-by-one in
  `clone_engine.ensure_split`).
- La **porta standard del servizio è 18080** (non 8000) — non usarla per il desktop.
- Cache in app-data (`QStandardPaths.AppDataLocation` → `~/.local/share/noesis-pdf-cloner/clones/`),
  **non** nel repo. Chiave per documento + engine + coppia linguistica.
- Segreti (es. `OPENROUTER_API_KEY`): da variabile d'ambiente oppure dall'archivio
  per-utente `secrets.json` (modulo `keystore.py`, permessi `0600`, in app-data);
  **mai** in `config.json`, nei log o nei commit. La chiave non va negli argv se
  evitabile.
- Il motore `llm` usa il percorso "OpenAI-compatibile" di `pdf2zh_next` (flag `--openai`)
  puntato a **OpenRouter** (modello Mercury), **non** OpenAI. L'alias `openai` è
  normalizzato a `llm`.

## Comandi utili
- Avvio GUI: `./run.sh` (crea/aggiorna i due venv e apre la GUI) oppure
  `.venv/bin/python main.py [file.pdf]`.
- Test: `python3 -m unittest discover -s tests -v`.
- Motore: `./setup_engine.sh` (Linux/macOS) · `setup_engine.ps1` (Windows) crea `.venv2`.
- Rilevamento binario: override in ⚙️ Impostazioni → env `PDF2ZH_BIN` → `.venv2`
  accanto ad app/repo.

## PDF di test
- `ha22.pdf` (≈300 MB, copyright McGraw-Hill) è il PDF di test principale ed è
  **escluso dal versioning** (`.gitignore`): resta solo in locale.
- Se manca (repository appena clonato), segnalalo: i file non sono versionati.
- Non spostare, rinominare o cancellare i PDF di test senza chiedere.

## Documentazione
- `README.md` — uso, installazione, release.
- `HANDOFF.md` — stato del progetto, architettura, bug risolti e TODO.
- `docs/help/` — guida utente pubblicata su GitHub Pages.
