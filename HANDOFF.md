# Handoff — Noesis PDF Cloner

*Data: 2026-09-20 · Progetto: app desktop PyQt6 che apre un PDF e mostra, a sinistra la pagina originale e a destra la stessa pagina tradotta con il layout preservato (pdf2zh_next v2), con i tre motori di traduzione di pdfcloner.*

---

## 1. Stato del progetto

**FUNZIONANTE e operativo.** App derivata da **noesis-pdf-reader-lite** (UI, navigazione,
zoom, TOC, i18n, Impostazioni), il cui scopo è cambiato: non più esportazione del testo in
markdown, ma **clonazione della pagina tradotta** tramite **pdf2zh_next v2 (BabelDOC)**.

- Repository **pubblico**: `git@github.com:vigliafg/noesis-pdf-cloner.git` (remote **SSH**, branch `main`).
- Commit di oggi: `63ce410` (app iniziale), `9ac52f8` (fix off-by-one), `337c90e` (barra collassabile).
- Guida pubblicata su GitHub Pages (build da workflow): <https://vigliafg.github.io/noesis-pdf-cloner/help/>.
- Test: **164 OK** (17 skip, regressioni su PDF reali non versionati).

### Cosa NON è ancora fatto
- Nessuna release compilata (il workflow esiste ma non è mai stato eseguito: serve un tag `v*`).
- I contenuti di `docs/help/` sono **rebrandati** ma descrivono ancora l'estrazione markdown di lite.
- Il motore **Docling** e gli strumenti a zone (dormienti) non sono reintegrati.

---

## 2. Avvio rapido

### Da sorgente (venv già presenti)
```bash
cd /home/vigliafg/Documenti/GitHub/noesis-pdf-cloner
.venv/bin/python main.py ha22.pdf     # oppure senza argomento e poi 📂 Apri PDF
```

### Bootstrap completo (crea/aggiorna i due venv e avvia)
```bash
./run.sh ha22.pdf
```
`run.sh` usa `uv` + Python 3.12 e crea:
- `.venv` → app: PyQt6 + PyMuPDF (+ markdown/pymupdf4llm per il codice dormiente)
- `.venv2` → motore: `pdf2zh_next` v2 (BabelDOC)

### Motore di traduzione
- Rilevato automaticamente: override in ⚙️ Impostazioni → env `PDF2ZH_BIN` → `.venv2` accanto ad app/repo → `.venv2` di `../pdfcloner` (utile in sviluppo).
- Chiave **`OPENROUTER_API_KEY`** necessaria solo per il motore **LLM** e per il fallback LLM della catena gratuita.

### Release standalone (non ancora generate)
`git tag v0.1.0 && git push --tags` → workflow `.github/workflows/release.yml` compila su
Windows x64, macOS x64/arm64 e Linux (AppImage) e allega anche `setup_engine.sh`/`.ps1`.
Dopo il download, il motore si installa accanto all'eseguibile:
```bash
./setup_engine.sh                                          # Linux/macOS
powershell -ExecutionPolicy Bypass -File .\setup_engine.ps1  # Windows
```

---

## 3. Architettura

```
utente → MainWindow (PyQt6)
   ├─ sinistra: PdfPageView  ← render PyMuPDF della pagina originale
   └─ destra:   TranslatedPagePanel
         ├─ barra motori (radio Google/Bing/LLM) — collassabile
         └─ PdfPageView (read-only) ← render del clone tradotto (*.mono.pdf)
   │
   ├─ clone_engine.CloneEngine
   │     set_document(pdf) → ensure_split(page) → subprocess pdf2zh_next → cache
   │     ├─ engine google → --clitranslator → gtranslate_cli.py
   │     │     catena: dict-chrome-ex → translate-pa → gtx → microsoft → LLM
   │     ├─ engine bing   → --bing
   │     └─ engine openai → --openai (OpenRouter)
   │
   └─ cache app-data: ~/.local/share/noesis-pdf-cloner/clones/
         ├─ split/<doc_key>/page_XXXXXX.pdf
         ├─ translated/<doc_key>/<engine>/<lang_in>-<lang_out>/page_XXXXXX.pdf
         └─ engine_events.jsonl   (fallback Microsoft/LLM della catena)
```

### Flusso di una pagina non in cache
split (~0.5 s) → `pdf2zh_next` (traduzione + typesetting BabelDOC) → cache per
(documento, engine, coppia linguistica). Richiedere la stessa pagina con lo stesso
engine è istantaneo; con un engine diverso si rigenera (cache separata).

---

## 4. Scelte tecniche e motivazioni

1. **Due venv separati** (`.venv` / `.venv2`): `pdf2zh_next` (con BabelDOC) non convive
   con PyQt6 per i conflitti di dipendenze. Il motore è invocato come **subprocess CLI**,
   esattamente come in pdfcloner.
2. **`pdf2zh_next` v2, non v1**: la v1 sostituiva il testo nei box senza adattarlo
   (righe sovrapposte con l'italiano più lungo); la v2 (typesetter BabelDOC) no.
3. **Cache in app-data**, non nel repo: `QStandardPaths.AppDataLocation` → sopravvive
   agli aggiornamenti e non sporca il repository. Chiave per **documento + engine + lingue**.
4. **Indici pagina 0-based** in tutto il motore e nella UI (vedi §6 bug risolto).
5. **UI**: la navigazione completa di lite è mantenuta (spin numero pagina, TOC dock,
   prec/succ, scorciatoie, zoom sincronizzato sui due pannelli).
6. **Codice di lite dormiente**: estrazione testo/PyMuPDF4LLM, `layout_engine.py`,
   pannello testo e strumenti a zone restano definiti ma **nascosti** (nessun widget nel
   layout), pronti per l'integrazione Docling.
7. **Barra motori collassabile (Opzione A)**: un pulsante-chevron flottante, figlio del
   pannello ma fuori dal layout, nasconde l'intera barra; i 36 px tornano al viewport e
   `PdfPageView.resizeEvent → _fit_to_view` riadatta la pagina. Stato persistito in
   `config.json` (`clone_bar_collapsed`) e ripristinato all'avvio.

---

## 5. Verifiche fatte oggi

| Verifica | Esito |
|---|---|
| Suite `unittest discover -s tests` | **164 OK** (17 skip) |
| Smoke test GUI headless (offscreen) | nav/TOC/zoom/engine OK |
| E2E reale pagina 156 di `ha22.pdf` | google 53.3 s · bing 44.1 s · openai 65.3 s — tutti `done` |
| Contenuto clone pagina 156 | layout preservato, **figura MRI preservata**, `FIGURA 16-1`/`TABELLA 16-4` |
| Report visivo (Playwright/Chromium) | originale vs 3 motori, layout fedele |
| Barra collassabile | `scroll_area` 1050 → 1086 → 1050 px (**+36 px** al collasso), chevron `▾`/`▸` |

Artefatti del test visivo (effimeri) in `/tmp/opencode/cloner_visual/`:
`report_report.png` (griglia originale+3 motori), `app_page156_*.png`, `collapse_*.png`,
`page156_{google,bing,openai}.pdf`. Nessun fallback Microsoft/LLM registrato (la catena
Google ha usato gli endpoint gratuiti).

---

## 6. Bug risolto (importante)

**Off-by-one nello split.** `CloneEngine.ensure_split` estraeva `from_page=page-1`
(convenzione 1-based di pdfcloner) mentre l'app passa indici **0-based**: a pagina 156 il
pannello destro mostrava la traduzione della **155** (pannelli disallineati, figura
assente). Corretto in `clone_engine.py` (`from_page=page`) + test di regressione
`tests/test_clone_engine.py::SplitIndexTests`. Commit `9ac52f8`.

---

## 7. Problemi noti / TODO

1. **Release non testate**: il workflow multipiattaforma è nuovo; il primo tag va
   verificato. Su macOS il `.app` cerca `.venv2` in `Contents/MacOS` e nelle cartelle
   superiori (euristica); da validare sul campo.
2. **Pagine senza testo** (es. copertina, pagina 1): pdf2zh_next può non produrre il
   `.mono.pdf` → status `error`. La UI mostra il messaggio senza bloccare la navigazione.
3. **`docs/help/`**: contenuti ancora orientati all'estrazione markdown di lite; da
   riscrivere per il flusso di clonazione.
4. **`NEXT_STEPS-cattura-manuale.md`**: nota di lite (cattura manuale immagini); valutare
   se rimuoverla (la feature è dormiente).
5. **Overlap residuo BabelDOC** in casi limite (noto da pdfcloner, es. pagina 449): bug
   upstream, non dipende dai motori.
6. ~~**Nessuna cancellazione di una traduzione in corso** (il subprocess non è interrompibile
   dall'UI)~~ → **risolto** (allineato al servizio): `pdf2zh_next` è avviato in un
   *process group* e il cancel (export o chiusura app) lo termina subito, senza
   attendere la fine della pagina; i risultati obsoleti restano scartati dal
   generation guard.
7. **Docling** e reintegrazione degli strumenti a zone: lavoro futuro previsto.

---

## 8. File chiave

| File | Ruolo |
|---|---|
| `main.py` | GUI PyQt6: `MainWindow`, `TranslatedPagePanel` (barra collassabile), `PdfPageView`, navigazione/zoom, Impostazioni, i18n runtime |
| `clone_engine.py` | `CloneEngine`: split, subprocess pdf2zh_next, flag per engine, cache, stato, fallback log, auto-rilevamento binario |
| `gtranslate_cli.py` | Catena gratuita Google→Microsoft→LLM per `--clitranslator` |
| `i18n.py` | Stringhe UI (it/en/fr/de/es), `TRANSLATION_ENGINES = (google, bing, openai)`, config/`DEFAULTS` |
| `layout_engine.py` | Engine adattativo dei fix di layout (dormiente) |
| `tests/test_clone_engine.py` | Split/pipeline/flags/cache + regressione off-by-one |
| `tests/test_clone_panel.py` | Collasso barra motori (offscreen) |
| `tests/test_i18n.py` | Completezza traduzioni + config |
| `run.sh`, `setup_engine.sh`, `setup_engine.ps1` | Bootstrap venv / installazione motore |
| `.github/workflows/release.yml` | Release multipiattaforma (PyInstaller + NSIS + AppImage) |
| `.github/workflows/pages.yml` | Pubblica `docs/help/` su GitHub Pages |
| `installer.nsi` | Installer Windows (rebrandato) |
| `ha22.pdf` | PDF di test (300 MB, **gitignored**, copyright McGraw-Hill) |

---

## 9. Repository, release e Pages

- Remote: `git@github.com:vigliafg/noesis-pdf-cloner.git` (SSH), branch `main`.
- Visibilità: **pubblica**.
- GitHub Pages: abilitato con `build_type=workflow`; guida a
  <https://vigliafg.github.io/noesis-pdf-cloner/help/> (HTTP 200).
- Release: workflow pronto; eseguire con un tag `v*`.

---

## 10. Contesto utente

- Lingua utente: **italiano** — rispondere in italiano.
- Requisito originale: "creare una app utilizzando il codice di noesis-pdf-reader-lite e
  incorporando codice di pdfcloner e noesis-pdf-reader"; scopo = produrre un **clone della
  pagina tradotta** che mantiene il layout, con i tre motori di pdfcloner.
- Richieste successive recepite: navigazione completa; release multipiattaforma; repo
  pubblico con remote SSH; barra motori collassabile con restituzione dello spazio e
  persistenza.
- `OPENROUTER_API_KEY` presente nell'ambiente (motore LLM operativo).
