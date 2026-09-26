# Handoff — Noesis PDF Cloner

*Data: 2026-09-20 · Progetto: app desktop PyQt6 che apre un PDF e mostra, a sinistra la pagina originale e a destra la stessa pagina tradotta con il layout preservato (pdf2zh_next v2), con i tre motori di traduzione di pdfcloner.*

---

## 1. Stato del progetto

**FUNZIONANTE e operativo.** App derivata da **noesis-pdf-reader-lite** (UI, navigazione,
zoom, TOC, i18n, Impostazioni), il cui scopo è cambiato: non più esportazione del testo in
markdown, ma **clonazione della pagina tradotta** tramite **pdf2zh_next v2 (BabelDOC)**.

- Repository **pubblico**: `git@github.com:vigliafg/noesis-pdf-cloner.git` (remote **SSH**, branch `main`).
- Ultima release: **v0.1.11** (Windows x64 + macOS x64/arm64 + Linux AppImage).
- Sito del progetto su GitHub Pages (build da workflow): landing a
  <https://vigliafg.github.io/noesis-pdf-cloner/> e guida multilingue a
  <https://vigliafg.github.io/noesis-pdf-cloner/help/>.
- Test: **432 OK** (24 skip, regressioni su PDF reali non versionati).

### Cosa NON è ancora fatto
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

### Release standalone
`git tag v0.1.3 && git push origin v0.1.3` → workflow `.github/workflows/release.yml` compila su
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
8. **Export guidato (wizard a 5 passi)**: i parametri di esportazione si raccolgono in
   `ExportWizardDialog` (File → Pagine → Lingue → Motore → Output), sul modello del
   wizard del servizio `noesis-pdf-cloner-service` (palette scura, step numerati,
   footer Annulla/Indietro/Avanti). Il pulsante 💾 ha un menu a tendina con
   *procedura guidata* e *pagina corrente (rapido, senza wizard)*; il flusso di
   traduzione/export a valle resta invariato.
9. **Anteprime live + numerazione**: lo step Pagine mostra anteprime grandi (prima/ultima
   della selezione) renderizzate da un documento tenuto aperto con debounce; la didascalia
   espone numero **fisico** e **stampato** (`/PageLabels`, port di `app/pagelabels.py`).
   Il numero fisico resta l'unico input (box "vai a pagina" e TOC invariati).
10. **Progresso "liquido"**: overlay condiviso (`LiquidOverlay`) nel pannello destro
    durante la traduzione on-demand (progresso stimato: asintoto ~90% → 100% al done)
    con pulsante **Annulla** (ferma solo il thread attivo; i thread ritirati continuano
    a riempire la cache in background), e nella finestra di progresso export, dove
    mostra la pagina in lavorazione che avanza. Il pannello mostra una targhetta
    "in lavorazione: pagina N" se la pagina tradotta non è quella visualizzata.
11. **Chiave OpenRouter**: archivio per-utente `keystore.py` (file `secrets.json`
    0600 in app-data; nessun keyring per scelta) con priorità all'env esterno;
    campo in Impostazioni (Mostra/Verifica/Rimuovi) e prompt automatico quando si
    sceglie l'engine LLM senza chiave. Errori "chiave assente" (`missing_key`) e
    "chiave non valida" (401, `invalid_key`) sono codici motore tradotti in UI;
    `gtranslate_cli` registra `llm_unauthorized` nel log eventi.
12. **Finestra di progresso export**: log per **ogni** pagina (▶ in lavorazione →
    ✓/✗), indicatore "Pagina X — pos di N" durante la traduzione, intervallo che
    a fine processo passa a "Tradotte: N · Non riuscite: M", e pulsante
    **Salva in Download** (copia il file nella cartella Download, con nome unico).
13. **Passo "Pagine" e "Apri cartella"**: **una sola riga** con i radio
    "Pagina corrente"/"Intervallo di pagine" e i due box `Da pagina` / `A pagina`;
    sotto ogni header la **miniatura live** della sua pagina (corrente sotto il
    radio, Da/A sotto i rispettivi box). Miniatura **intera** con box calcolato
    dall'aspetto reale della pagina, contenuto in un `QScrollArea` (su schermi
    bassi si scorre senza tagliare). Toccare uno spin passa da solo a
    "Intervallo". "Apri cartella" apre l'**ultima destinazione** (Download dopo
    "Salva in Download").
14. **Installa motore dall'app**: in ⚙️ Impostazioni → *Motore di clonazione* il
    pulsante **Installa motore** crea `.venv2` accanto all'app e vi installa
    `pdf2zh_next` via `uv` (`clone_engine.engine_base_dir`/`install_engine`,
    `EngineInstallThread`/`EngineInstallDialog`), con log e Annulla. Se la
    cartella dell'app non è scrivibile (es. Program Files) il motore va nella
    cartella dati per-utente (`clone_engine.app_data_dir`) e l'auto-rilevamento
    lo trova lo stesso. L'installer NSIS include `setup_engine.ps1`. Senza il
    binario del motore **nessun** motore traduce (anche google/bing: sono
    traduttori *dentro* `pdf2zh_next`).
15. **Traduzione on demand**: al cambio pagina il pannello destro **non**
    traduce più da solo: mostra il clone in cache del motore selezionato oppure
    l'originale con l'invito *"Premi ▶ Traduci per tradurre questa pagina"*.
    La traduzione parte solo dal pulsante **▶ Traduci** nella barra del
    pannello destro (accanto alle radio google/bing/llm). Le radio cambiano solo
    il motore selezionato e la vista. Se la pagina è **già in cache** per il
    motore scelto, ▶ Traduci non rilancia nulla e dà un feedback esplicito
    (*"La pagina N tradotta da X è già in cache"*).
16. **Una traduzione per pagina**: premendo ▶ Traduci con un motore diverso da
    quello con cui **quella pagina** è già in cache, un dialog di conferma
    (motore/i, pagina, MB) chiede se eliminare la cache precedente della sola
    pagina; su conferma `CloneEngine.purge_page_cache` la rimuove (tutte le
    lingue di quella pagina, senza toccare le altre pagine, `split/` o gli altri
    motori) e annulla le traduzioni in background di quella pagina/motore.
17. **`uv` incluso nel bundle (motore senza prerequisiti)**: il binario `uv`
    (versione pinnata + `sha256`, licenza MIT/Apache-2.0) è scaricato a build
    time da `vendor/fetch_uv.py` e incluso nel bundle PyInstaller. `find_uv()`
    usa: `UV` esplicito → `uv` del bundle → `PATH`. Così "Installa motore"
    funziona senza che l'utente abbia `uv` installato.
18. **Striscia "motore non installato"**: nel pannello destro, finché il motore
    manca, compare una striscia ambra non bloccante con il pulsante **Installa
    motore** (apre il dialog di installazione); sparisce da sola appena il
    motore è rilevato. `setup_engine.sh`/`.ps1` ora installano `uv` da soli se
    assente.
19. **Disinstallazione a scelta (NSIS)**: l'uninstaller ha una pagina custom
    *"Cosa rimuovere"* con motore `.venv2` e dati app
    (`%APPDATA%\noesis-pdf-cloner`), più la casella **"Rimuovi tutto: nessuna
    traccia"** (default: tutto selezionato). `RMDir /r` solo per il motore;
    altrimenti `RMDir` semplice, così `$INSTDIR` sopravvive se il motore è
    accanto all'app. **La cache/dati di `uv` (`%LOCALAPPDATA%\uv`,
    `%APPDATA%\uv`) non si tocca**: è condivisa da tutte le app che usano `uv`
    (Python gestiti e tool installati inclusi) e rimuoverla rompe altre
    installazioni.
20. **Chiave OpenRouter: precedenza e feedback (v0.1.5)**: la chiave **salvata
    in ⚙️ Impostazioni vince** sulla variabile di sistema `OPENROUTER_API_KEY`
    (che resta il *fallback*; "Rimuovi" torna a usarla). In Impostazioni una
    riga indica **quale chiave è attiva** (file / variabile di sistema /
    nessuna) e avvisa se una variabile di sistema viene ignorata. Prima di far
    partire una pagina LLM la chiave viene **verificata** (endpoint `/key`, in
    un thread, senza bloccare la GUI) e, se invalida/irraggiungibile, si mostra
    un **consiglio mirato** senza avviare il lavoro. Gli errori del motore sono
    **classificati** (`invalid_key`, `forbidden`, `no_credits`, `rate_limited`,
    `model_not_found`, `network`) con messaggio e suggerimento (credito, attesa,
    rete, oppure usa Google/Bing). Su Windows, se la variabile esiste nel
    registro ma non è stata ereditata dal processo, l'app lo dice ("riavvia
    l'app"). La classificazione è **allineata** in `app/engine.py` del servizio.
    Inoltre, quando il motore LLM è selezionato e **manca la chiave**, nel
    pannello destro compare una **striscia "chiave mancante"** (blu, come quella
    del motore non installato) con i pulsanti **Inserisci chiave** (apre il
    dialog) e **Usa Google/Bing** (passa a un motore gratuito, che non richiede
    chiave). **Nota E2E (Linux)**: con chiave non valida `pdf2zh_next` può
    uscire con **codice 0**, `stderr` vuoto e l'errore 401 su **stdout**: la
    classificazione guarda **anche stdout**, quindi il caso resta `invalid_key`.
21. **Diagnostica di preflight (`diagnostics.py`)**: modulo **puro Python** (no
    Qt) che verifica ambiente, `uv`, motore (`pdf2zh_next`), rete, chiave
    OpenRouter (presenza/fonte/validità/credito) e **modello** (chat completion
    minima). Ogni check ritorna un `CheckResult` (`id`, `status`
    ok/warn/fail/skip, `code`, `fix`, `duration_ms`); il `DiagnosticsRunner` li
    esegue con callback e cancellazione. Stessi `id`/`code`/`fix` del servizio
    (`app/diagnostics.py`), che li usa in `/health?deep=1` e nel comando
    **`--doctor`**. La UI (wizard al primo avvio + voce in ⚙️ Impostazioni) è la
    fase successiva; il modulo è già testato e usando i codici di
    `classify_engine_failure` parla la stessa lingua degli errori a runtime.
22. **▶ Traduci spostato nella toolbar principale (v0.1.5)**: il pulsante era
    nella fascia motori del pannello destro, che il chevron `▸` può compattare:
    con la fascia chiusa il segnaposto diceva "premi ▶ Traduci" ma il pulsante
    era invisibile. Ora ▶ Traduci sta nella **toolbar principale, a sinistra di
    ⚙️ Impostazioni**, sempre raggiungibile; la fascia motori conserva lingua
    target, radio motori, stato, targhetta e 💾 Esporta. Il **tooltip** del
    pulsante mostra motore attivo e pagina (`clone.translate.tooltip`), e il
    segnaposto dice "nella barra in alto". Nessuna scorciatoia.
23. **Accentate corrette nel motore Google (v0.1.5)**: `pdf2zh_next` esegue il
    nostro `gtranslate_cli.py` come subprocess con `encoding="utf-8"` e
    `errors="replace"` (`clitranslator.py`); il CLI scriveva con l'encoding di
    default del processo (su Windows **cp1252**), quindi le accentate uscivano
    come byte non-UTF-8 e pdf2zh le sostituiva con **U+FFFD** («�»). Microsoft e
    LLM non erano coinvolti (usano i percorsi JSON UTF-8 interni di pdf2zh).
    Fix: `gtranslate_cli.py` forza **UTF-8** su stdin/stdout/stderr (lettura e
    scrittura via buffer), e il motore passa `PYTHONIOENCODING=utf-8` al
    subprocess (difesa in profondità). Vale per l'italiano **e** ogni non-ASCII.
    Allineato in `app/gtranslate_cli.py` e `app/engine.py` del servizio.

---

## 5. Verifiche fatte oggi

| Verifica | Esito |
|---|---|
| Suite `unittest discover -s tests` | **369 OK** (24 skip) |
| Diagnostica (simulata) | 38 test: server OpenRouter locale (ok/401/402/429/404/timeout), binari `uv`/`pdf2zh_next` finti, runner |
| Diagnostica reale su Linux | desktop e `--doctor` servizio: tutti i check `ok` (motore, chiave mascherata, modello HTTP 200, catena gratuita) |
| E2E reale su Linux (chiave OpenRouter) | valida → `done` (26,6 s) · non valida → `error:invalid_key` (5,4 s) · assente → `error:missing_key` |
| `uv` nel bundle | `vendor/fetch_uv.py` scarica uv 0.12.17, sha256 verificata, `./vendor/uv-bin/uv --version` OK |
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

**Finestre console su Windows (v0.1.2).** Essendo l'app GUI (`--windowed`), ogni
subprocess console (``uv``, ``pdf2zh_next``, ``taskkill``) apriva una finestra
davanti all'app: una durante *Installa motore* e una per **ogni pagina** durante
export/traduzione. Risolto con ``clone_engine._no_window_kwargs()``
(``CREATE_NO_WINDOW`` + ``STARTUPINFO``/``STARTF_USESHOWWINDOW``) su tutti i
lanci. È un dettaglio GUI: nel servizio (headless) non serve.

**Cambio pagina durante una traduzione (v0.1.2).** Al cambio pagina la traduzione
precedente resta viva in background: se si torna/ricade sulla stessa pagina,
``translate_page`` ritorna ``None`` con stato ``running`` e il vecchio
``CloneTranslateThread`` lo trattava come **errore**. Ora attende che il worker
concorrente finisca (come fa l'export) e ``_request_translation`` non avvia un
secondo worker sulla stessa pagina.

**Log su file (v0.1.2).** ``main._setup_logging()`` scrive in
``<app-data>/logs/noesis-pdf-cloner.log`` (rotazione 2 MB × 3, ``clone_engine`` a
DEBUG): utile per diagnosticare su Windows, dove non c'è console.

**Serializzazione (v0.1.2).** Il motore condiviso dell'app usa
``max_concurrent=1``: una sola ``pdf2zh_next`` alla volta (meno processi
concorrenti su Windows). Le pagine in coda restano visibili con la targhetta.

**Pagine senza testo (v0.1.2).** Se ``pdf2zh_next`` esce 0 senza produrre il
``.mono.pdf`` **e la pagina non ha testo estraibile** (copertina, scansione) il
motore copia l'originale in cache e lo marca ``empty``; la UI mostra la nota
"nessun testo da tradurre". Se invece la pagina **ha testo**, resta un errore
vero (messaggio "il motore non ha tradotto la pagina: …"), così un guasto della
catena non viene mascherato. Allineato al servizio.

**Catena Google su Windows (v0.1.2).** Sintomo: *"pdf2zh_next non ha prodotto il
file mono"* — google falliva, bing no. Causa: `--clitranslator-command` è una
stringa che BabelDOC rilegge con `shlex.split`, che sui **percorsi Windows**
(`C:\Users\…`) toglie i backslash → comando ineseguibile → nessuna traduzione →
nessun `.mono.pdf` (rc=0). Risolto con `shlex.join([python, gtranslate_cli.py])`
(desktop + servizio). Test: round-trip di `shlex.split` su percorsi Windows.

---

## 7. Problemi noti / TODO

1. **Release**: v0.1.2 e **v0.1.3** pubblicate e verificate (Windows + Linux
   AppImage + macOS). Resta da validare **sul campo il `.app` macOS**: cerca
   `.venv2` in `Contents/MacOS` e nelle cartelle superiori (euristica).
2. ~~**Pagine senza testo** (es. copertina, pagina 1): pdf2zh_next può non produrre il
   `.mono.pdf` → status `error`. La UI mostra il messaggio senza bloccare la navigazione.~~
   **Risolto (v0.1.2)**: il motore copia l'originale in cache (`empty`) e la UI mostra
   la nota "nessun testo da tradurre" (allineato al servizio).
3. ~~**`docs/help/`**: contenuti ancora orientati all'estrazione markdown di lite; da
   riscrivere per il flusso di clonazione.~~ **Fatto (v0.1.5)**: riscritta per il
   cloner in 5 lingue, con landing del sito alla radice delle Pages
   (`docs/index.html`) e pulsante ❓ Guida che apre la lingua dell'interfaccia
   (`help_url()`).
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
8. **AppImage e motore `.venv2`** (rinviato): l'AppImage gira da un mount
   temporaneo in sola lettura, quindi il `.venv2` non può stare "accanto
   all'eseguibile". Da fare:
   - far cercare all'app anche `$APPIMAGE`/`$APPDIR`, così rileva un `.venv2`
     accanto al file `.AppImage` (oggi `setup_engine.sh` lì non basta);
   - nel README, spiegare che con l'AppImage conviene il pulsante **Installa
     motore** (installa in `~/.local/share/noesis-pdf-cloner/engine/.venv2`,
     già auto-rilevato) oppure impostare il percorso a mano in ⚙️ Impostazioni →
     *Eseguibile pdf2zh_next*.
9. ~~**Avviso "motore non installato"**~~ → **fatto (v0.1.5)**: striscia ambra
   non bloccante nel pannello destro con pulsante **Installa motore**, visibile
   finché il motore manca (`TranslatedPagePanel.set_engine_missing`,
   `MainWindow._update_engine_banner`).
10. **Robustezza AppImage su Linux** (rinviato): build su `ubuntu-22.04` per una
    glibc più vecchia (compatibilità distro); documentare `libfuse2` e
    `./NoesisPDFCloner-x86_64.AppImage --appimage-extract-and-run`; valutare in
    aggiunta un `.tar.gz` estraibile senza FUSE.
11. ~~**`uv` incluso nel bundle**~~ → **fatto (v0.1.5)**: `vendor/fetch_uv.py`
    scarica `uv` pinnato (`UV_VERSION = 0.12.17`) con verifica `sha256` a build
    time; incluso nel bundle PyInstaller (`--add-data`, come
    `gtranslate_cli.py`); `find_uv()` = `UV` → bundle (`sys._MEIPASS`) →
    `PATH`; licenze MIT/Apache-2.0 in `vendor/uv-licenses/`. Da ricordare:
    **aggiornare `UV_VERSION` + le hash** a ogni aggiornamento di `uv`.
12. ~~**Bootstrap di `uv` negli script**~~ → **fatto (v0.1.5)**:
    `setup_engine.sh`/`.ps1` installano `uv` da soli se assente. **Resta
    opzionale** (da valutare): integrare il motore nell'installer NSIS come
    **componente opzionale e skippabile** (`uv.exe` incluso, `nsExec::ExecToLog`,
    destinazione `%LOCALAPPDATA%\noesis-pdf-cloner\engine\.venv2`); attenzione a
    SmartScreen/Defender e ai tempi del download di ~1,1 GB. Meglio comunque:
    installer veloce → primo avvio → la app scarica il motore.
13. ~~**Disinstallazione del motore / dati**~~ → **fatto (v0.1.5)**:
    l'uninstaller NSIS mostra la pagina **"Cosa rimuovere"** (custom, nsDialogs)
    con due voci — motore `.venv2` e dati app in `%APPDATA%\noesis-pdf-cloner`
    (cache traduzioni, impostazioni, chiave, log) — più la casella **"Rimuovi
    tutto: nessuna traccia del programma sul sistema"**. Default: tutto
    selezionato. Deselezionando il motore, la `RMDir` non ricorsiva preserva
    `$INSTDIR`. **La cache/dati di `uv` NON viene toccata**: è condivisa da
    tutte le app che usano `uv` (Python gestiti e tool installati) e rimuoverla
    rompe altre installazioni (segnalato dall'utente: cancellava la cache di
    Hermes). Nota: l'uninstaller gira elevato (admin), quindi
    `$APPDATA`/`$LOCALAPPDATA` sono quelli dell'utente che eleva (coincide con
    l'utente tipico). Compilato/verificato con `makensis` 3.10.
14. ~~**Chiave OpenRouter: precedenza e feedback**~~ → **fatto (v0.1.5)**: vedi
    §4 punto 20. La chiave salvata ha la precedenza sulla variabile di sistema;
    indicatore della fonte attiva, verifica pre-volo non bloccante, errori del
    motore classificati con consigli. Classificazione allineata nel servizio
    (`app/engine.py`).

### Fatto il 23/09 (v0.1.5)

`uv` nel bundle → avviso "motore non installato" → bootstrap `uv` negli script
(ordine consigliato rispettato) → **opzioni di disinstallazione nell'uninstaller
NSIS** → **feedback completo sulla chiave OpenRouter** (precedenza, fonte,
verifica pre-volo, errori classificati). Restano in coda i punti 8 e 10
(AppImage).

### Fatto il 23/09 (v0.1.6)

- **Campo libero di scelta pagine** nel wizard di export (`pages.py`, allineato a
  `app/pages.py` del servizio): tre modi (corrente / intervallo / elenco
  `1,3,7-9`, `all`), validazione live, export esatto anche non contiguo. L'help
  lo spiega con **miniature schematiche** HTML/CSS.
- **Pulsante flottante "Azioni pagina"** nel pannello destro a traduzione
  completata: Salva in Download, Esporta, Traduci la successiva, Ritraduci, Apri
  col visualizzatore, Cancella cache. Entra con uno **slide-up con overshoot** e
  un glow dell'ombra una tantum (~180 ms di ritardo, ~280 ms di movimento,
  `QPropertyAnimation`); un **pallino azzurro** sull'angolo resta finché non apri
  il menu. Le animazioni si fermano su hide/resize.
- **Destinazione export**: cartella di destinazione (ricordata) + nome, terzo
  formato "pagine singole in una cartella" (`CloneEngine.export_folder`); il
  pulsante di fine export è diventato **⬇ Copia in Download** (è una copia).

### Fatto il 25/09 (rifiniture desktop — Fase 1 + Fase 2)

Implementati i 7 punti richiesti (solo **desktop**: allineamento al servizio
**rimandato**, vedi TODO sotto).

1. **Riduzione a icona**: tasto minimizza su wizard di export e finestra di
   avanzamento; la finestra di avanzamento **non è più application-modal** e il
   batch continua mentre l'app è ridotta. Le azioni in conflitto sono bloccate
   dal flag `MainWindow._batch_busy` ("esportazione già in corso").
2. **Pulsante "Azioni pagina"** come **overlay del pannello** in alto a destra,
   **indipendente dalla barra motori**: resta visibile anche con la fascia
   collassata (si affianca al chevron). Non più flottante in basso: così non
   può finire dietro le scrollbar. **Glow intermittente** (drop-shadow animato,
   colore dal tema: blu su chiaro, blu tenue su scuro) ben visibile su pagine
   chiare e scure; bordo 2px; resta il pallino-badge.
3. **Avvisi di fine batch** (`notifications.py`): notifica di sistema
   (`QSystemTrayIcon`), flash della taskbar (`QApplication.alert`) e **suono
   nativo** discreto (Windows `winsound`, macOS `afplay`, Linux
   `pw-play`/`paplay`/`aplay`/`ffplay` con un **WAV generato dall'app**,
   fallback `QApplication.beep`) in background. Icona nel system tray con
   Mostra/Esci. Opzioni: avviso e suono on/off + **🔔 Prova suono**. L'avviso
   scatta anche a fine **singola pagina** se la finestra non è in primo piano
   (`_notify_page_done`).
4. **Standby** (`power.py`): `SleepInhibitor` impedisce lo standby durante i job
   (`systemd-inhibit` / `SetThreadExecutionState` / `caffeinate`);
   `ResumeWatch` rileva il risveglio (orologio da parete vs monotono) e gli
   errori transitori (rete/timeout) vengono **ritentati** (`_is_transient_error`).
5. **Tema chiaro/scuro/sistema** (`theme.py`): due tavolozze di token e QSS
   generati; scelta in ⚙️ Impostazioni → Aspetto, default scuro, "come il
   sistema" segue `QStyleHints.colorScheme()`.
6. **Velocità LLM** (senza parallelizzare le pagine): `--no-auto-extract-glossary`
   (un giro LLM in meno per pagina), `--openai-timeout` e `--pool-max-workers`
   (setting `llm_pool_workers`, richieste LLM in parallelo *dentro* una pagina).
   La traduzione delle pagine resta **sequenziale, una alla volta**.
7. **Sequenziale**: `CloneExportThread` traduce le pagine una per una (nessun
   pool tra pagine, nessun batch a più pagine). La UI mostra ogni pagina con la
   sua anteprima, l'etichetta "Pagina N (pos di tot)" e la barra di avanzamento.

Extra: il dialog **Impostazioni** ora scorre (`QScrollArea`) invece di
comprimere i controlli quando la finestra è bassa; i pulsanti OK/Annulla
restano fissi in fondo. Nel registro degli export restano **solo gli esiti**
✓/✗ (niente righe "in lavorazione" fuorvianti).

Nuovi test: `test_theme.py`, `test_notifications.py`, `test_power.py`,
`test_ui_refinements.py`, `test_export_retry.py`, più flag LLM in
`test_clone_engine.py`. Suite: **477 OK** (24 skip).

> **TODO allineamento servizio (richiesto dall'utente)**: le modifiche al
> *motore* (`_translator_flags` LLM: `--no-auto-extract-glossary`,
> `--openai-timeout`, `--pool-max-workers`) **non** sono state riverberate in
> `noesis-pdf-cloner-service` (`app/engine.py`, `app/pipeline.py`). Da fare
> **dopo** il collaudo sul campo del desktop.
> I punti 1–5 e la scelta "tutto sequenziale" sono specifici del desktop:
> non da riverberare.

### Fatto il 26/09 (FAB — effetto "Scintilla")

Richiesta: rendere **più visibile** il pulsante "✓ Azioni pagina" a fine
traduzione; il glow da solo era insufficiente su tema chiaro e scuro.

- Creato il mockup interattivo **`fab_effects_mockup.html`** con **30 effetti**
  (12 cicli colore, 9 forma, 9 movimento), ognuno mostrato su entrambi i temi
  con sfondo pagina bianca, selezione singola/multipla e note di implementazione
  Qt. È un artefatto locale (come `glow_mockup.html`), **non** da versionare.
- Scelto e implementato l'effetto **09 Scintilla**: un riflesso luminoso che
  attraversa la superficie del FAB. Nuova classe `_SheenButton` in `main.py`
  (`paintEvent` + `QLinearGradient` ritagliato con `QPainterPath`; posizione
  animata da `QPropertyAnimation` sulla property `sheenPos`, ciclo 2,4 s in
  loop). Il colore arriva dal nuovo token di tema `fab_spark`: bianco
  traslucido sul tema scuro, blu traslucido su quello chiaro (il bianco sarebbe
  invisibile sul pulsante chiaro appoggiato a pagina bianca).
- La scintilla parte in `show_page_actions()` e si ferma in
  `hide_page_actions()`, in parallelo al glow; `apply_theme()` aggiorna colore e
  stato.

- Modifica collaterale: il **menu del FAB** aveva un bordo quasi invisibile
  (`menu_border` #3a3a3a su fondo #2b2b2b). Ora bordo **2px** con **rilievo 3D**
  (lato alto/sinistro chiaro, basso/destro scuro) su fondo a **gradiente
  verticale**; nuovi token `menu_bg_top/bottom`, `menu_border_hi/lo` in
  `theme.py`, raggio 9px e voci con più respiro.

Nuovi test in `tests/test_page_actions.py` (scintilla in loop, colore per tema,
property animabile, menu con bordo 3D) e in `tests/test_theme.py` (token del
rilievo). Suite: **482 OK** (24 skip).

> Solo desktop: il FAB non ha controparte nel servizio, nessuna riverberazione.

---

## 8. File chiave

| File | Ruolo |
|---|---|
| `main.py` | GUI PyQt6: `MainWindow`, `TranslatedPagePanel` (barra collassabile), `PdfPageView`, navigazione/zoom, Impostazioni, i18n runtime |
| `clone_engine.py` | `CloneEngine`: split, subprocess pdf2zh_next, flag per engine, cache, stato, fallback log, auto-rilevamento binario; `export_pdf`/`export_zip`/`export_folder` |
| `pages.py` | Parsing della specifica pagine (campo libero), allineato a `app/pages.py` del servizio |
| `theme.py` | Tavolozze (chiaro/scuro) e QSS generati per finestra/dialoghi/wizard |
| `notifications.py` | Suono d'avviso nativo (Windows/macOS/Linux) senza dipendenze pesanti |
| `power.py` | Inibizione standby cross-platform e rilevamento risveglio |
| `gtranslate_cli.py` | Catena gratuita Google→Microsoft→LLM per `--clitranslator` |
| `i18n.py` | Stringhe UI (it/en/fr/de/es), `TRANSLATION_ENGINES = (google, bing, openai)`, config/`DEFAULTS` |
| `layout_engine.py` | Engine adattativo dei fix di layout (dormiente) |
| `tests/test_clone_engine.py` | Split/pipeline/flags/cache + export (pdf/zip/folder) + regressione off-by-one |
| `tests/test_clone_panel.py` | Collasso barra motori (offscreen) |
| `tests/test_page_actions.py` | Pulsante flottante "Azioni pagina" (pannello + gestori) |
| `tests/test_pages.py` | Parsing del campo libero (semantica + codici errore) |
| `tests/test_i18n.py` | Completezza traduzioni + config |
| `run.sh`, `setup_engine.sh`, `setup_engine.ps1` | Bootstrap venv / installazione motore (auto-bootstrap di `uv`) |
| `vendor/fetch_uv.py` | Scarica `uv` pinnato + verifica `sha256` (incluso nel bundle) |
| `vendor/uv-licenses/` | Licenze MIT/Apache-2.0 di `uv` |
| `.github/workflows/release.yml` | Release multipiattaforma (PyInstaller + NSIS + AppImage) |
| `.github/workflows/pages.yml` | Pubblica il sito del progetto: landing (`docs/index.html`) alla radice e guida multilingue in `/help/` |
| `assets/` | Logo sorgente `assets/PDFCLONER.jpeg` e icone di release (`noesispdf.ico`/`.icns`/`-256.png`) generate da esso; l'icona è usata anche a runtime (`app.setWindowIcon`) |
| `installer.nsi` | Installer Windows (rebrandato) |
| `ha22.pdf` | PDF di test (300 MB, **gitignored**, copyright McGraw-Hill) |

---

## 9. Repository, release e Pages

- Remote: `git@github.com:vigliafg/noesis-pdf-cloner.git` (SSH), branch `main`.
- Visibilità: **pubblica**.
- GitHub Pages: abilitato con `build_type=workflow`; sito a
  <https://vigliafg.github.io/noesis-pdf-cloner/> (landing) e guida a
  <https://vigliafg.github.io/noesis-pdf-cloner/help/> (HTTP 200).
- Release: esistenti **v0.1.2**, **v0.1.3**, **v0.1.5**, **v0.1.6**; per
  pubblicarne una nuova creare un tag `v*` e pusharlo
  (`git tag -a v0.1.11 -m "v0.1.11" && git push origin v0.1.11`).
  Un `workflow_dispatch` su `main` produce solo artifact, senza release.

---

## 10. Feature sperimentale — "motore veloce" (26/09)

Obiettivo: ridurre la latenza **a freddo** (prima traduzione, LLM chiamato)
di una pagina tradotta verso i **≤ 30 s**. Le misure *a caldo* (cache interna
del motore) restano solo **diagnostiche** (isolano il pavimento fisso della
pipeline PDF). Analisi completa in `.opencode/plan/velocita-traduzione.md`
(fuori dal repo). Sintesi misurata: il collo di bottiglia non è solo l'LLM; una
pagina costa ~30 s anche a LLM saltato, per costi fissi pagati a ogni subprocess
(ri-hash dei font, monitor memoria, import Python, load modello).

### Cosa è stato aggiunto (feature **default OFF**, reversibile)

- `engine_patch.py` — patch runtime del motore:
  - memoizza `babeldoc.assets.assets.get_font_and_metadata` (i font in cache
    sono immutabili; oggi ri-hash 7×/pagina ≈ 6 s);
  - sostituisce `MemoryMonitor` con un no-op (~2-3 s/pagina).
  Idempotente e difensiva: se `babeldoc` manca/cambia, la patch è saltata.
- `engine_wrapper.py` — launcher che applica le patch e delega a `pdf2zh_next`.
- `clone_engine.py` — `fast_engine`/`fast_flags` (default OFF) + pool esplicito
  (`--pool-max-workers`/`--qps`, anche =1) + preset "traduzione rapida"
  (`--skip-scanned-detection` solo se la pagina ha testo,
  `--skip-formula-offset-calculation`, `--no-remove-non-formula-lines`) +
  marker cache `-fast1` + fallback automatico al binario.
- `i18n.py` / `main.py` — due toggle in **Impostazioni → Prestazioni**
  ("Motore veloce", "Traduzione rapida").
- `tools/bench_page.py` — banco di misura (usa `CloneEngine`, `--fresh` =
  `--ignore-cache`); `tools/child_profile.py` — profiler del processo figlio.

### Fase 2 — worker persistente
- `engine_worker.py` (lato `.venv2`): processo caldo (import + patch + warmup una
  volta), per job esegue `do_translate_file_async` **in-process**; protocollo
  JSON-lines su socket locale.
- `engine_client.py` (lato app): avvio/riavvio, cancel/timeout, riavvio se cambia
  l'ambiente rilevante; **fail-safe** verso il subprocess.
- `CloneEngine.warmup()/prewarm()/close()`; pre-avvio in background all'apertura
  (il costo di avvio non ricade sulla prima pagina). Toggle **"Worker
  persistente"** in Impostazioni.

### Fase 3 — LLM avanzato
- `--openai-reasoning-effort` e `--openai-enable-json-mode` (setting UI + env
  `PDF_LLM_REASONING_EFFORT`/`PDF_LLM_JSON_MODE`), gated da `fast_engine`.
- **Preset prestazioni** (Impostazioni → Prestazioni): tre **card radio**
  sempre visibili (Normale / Veloce / Massima velocità) con descrizione e tempo
  atteso inline:
  - *Normale*: tutto OFF, Mercury, OpenRouter (~40 s/pagina);
  - *Veloce (consigliato)*: patch + worker, Mercury, OpenRouter (~33 s);
  - *Massima velocità*: patch + worker + gpt-oss-120B + proxy locale (autostart)
    + reasoning minimal + pool 8 + **prompt di sistema di default** (traduce i
    nomi dei farmaci, non traduce citazioni/sigle) (~20-23 s).
  I campi che il preset governa stanno in **"Avanzate"** (collassato di default)
  e sono di sola lettura. **Stato provider inline** + "Prova provider" sotto le
  card. I preset valgono per tutti i motori (patch+worker anche su google/bing);
  la card *Massima velocità* e "Prova provider" sono attive **solo col motore
  LLM** (grigie altrove, con tooltip).
  **I preset NON attivano i flag B2** ("traduzione rapida":
  `--skip-formula-offset-calculation` ecc.): saltano elaborazioni di
  layout/formule, riducono la precisione e nel benchmark davano ~0,5 s. Restano
  come **opt-in manuale** (checkbox in Avanzate, non governata dal preset).
- **Liste numerate/alfabetiche** (Avanzate → "Rileva liste numerate", opt-in):
  BabelDOC riconosce solo i bullet grafici, quindi le liste `1. 2. 3.` finivano
  fuse in un paragrafo. Una patch runtime (`engine_patch`,
  `NOESIS_NUMERIC_LISTS`) spezza i paragrafi sui marcatori di lista. Validato
  sulla pagina 401 di `ha22.pdf` (8 voci separate). Richiede il wrapper (patch
  runtime); marker cache `-lists1`. Riverberato al service (`NUMERIC_LISTS`).
- **Modello e base URL** sono tendine (non testo libero): *Mercury* /
  *gpt-oss-120B* / *gpt-6-luna* / *Default*, e *OpenRouter* / *Proxy locale*.
- **Prompt di sistema LLM** (`llm_system_prompt`, campo in Avanzate):
  `--custom-system-prompt`, per guidare terminologia/stile (es. tradurre i nomi
  dei farmaci, non tradurre le citazioni). Marker cache dedicato.
- **Fix invio reasoning effort**: `--openai-reasoning-effort` da solo **non**
  veniva inviato a pdf2zh; ora si passa anche `--openai-send-reasoning-effort`.
- **Proxy provider automatico** (`proxy_manager.py`): da Impostazioni →
  Prestazioni, "Avvia automaticamente il proxy provider" + porta; l'app avvia
  `tools/provider_proxy.py` in background, punta la base URL al proxy e lo
  ferma alla chiusura. Idempotente (non ne avvia un secondo se è già attivo).
- **"Prova provider"**: pulsante che invia una piccola richiesta e mostra quale
  provider risponde (es. Groq) — verifica immediata del pin.
- `tools/provider_proxy.py`: proxy **model-aware** che pinna il provider (Groq)
  su OpenRouter **solo per i modelli scelti** (`PROXY_MODELS`, default
  `openai/gpt-oss-120b`); gli altri (DeepSeek, Gemini, Mercury) passano
  invariati. Serve perché `pdf2zh_next` non espone il routing provider.
- **Fix persistenza config** (`i18n._validate_config`): molte impostazioni
  (engine, theme, notify_*, `llm_pool_workers`, tutte le stringhe) **tornavano
  ai default a ogni riavvio**; ora un merge generico le conserva (con
  coercizione di tipo e validazione degli enum).
- **Allowlist account-wide OpenRouter** (Settings → Privacy → Allowed Providers)
  provata: pinna sì, ma è **globale** e blocca DeepSeek/Gemini → **scartata** a
  favore del proxy per-richiesta.

### Reversibilità (runbook)

1. Impostazioni → disattiva i due toggle (secondi).
2. `NOESIS_FAST_ENGINE=0` / `NOESIS_FAST_FLAGS=0` (kill-switch, senza UI).
3. `git checkout main` sul branch `experiment/fast-engine` (non mergiato).
4. `git revert -m 1 <merge>` se mergiato; oppure rimozione dei file nuovi.
Con feature OFF il comando al motore è **identico** a prima (test dedicati).

### Service

Allineato nello stesso branch `experiment/fast-engine` di
`noesis-pdf-cloner-service`: `app/engine_patch.py`, `app/engine_wrapper.py`,
`app/engine.py`, `app/config.py` (`llm_pool_workers`/`fast_engine`/`fast_flags`,
env `PDF_LLM_POOL_WORKERS`/`FAST_ENGINE`/`FAST_FLAGS`), `worker`/`cli`/`estimate`.
Default OFF; a OFF comportamento invariato (290 test verdi).

### Follow-up noti

- **Packaging**: `.github/workflows/release.yml` non bundle ancora
  `engine_wrapper.py`/`engine_patch.py`/`engine_worker.py` (come
  `gtranslate_cli.py`). Finché non lo si aggiunge, nelle build PyInstaller la
  feature degrada al binario (fail-safe). Il file era già modificato per pin di
  action non correlati: da aggiungere in un commit dedicato.
- **Groq/LLM**: per usare gpt-oss-120b serve il pin del provider via
  `tools/provider_proxy.py` (model-aware), puntando la base URL al proxy da
  Impostazioni. Prezzi e disponibilità del provider possono cambiare.
- **Upstream BabelDOC**: proporre memoizzazione di `get_font_and_metadata` e
  `MemoryMonitor` opzionale, così il wrapper diventa temporaneo.
- **`content = None`**: aggiungere retry (marker transitorio) — vedi
  `BENCH_LLM.md`. Il worker riduce ma non elimina questa classe di errori.
- **Worker**: valutare un pool di worker (uno per processo server) e la
  classificazione degli errori dal `log_tail` (oggi il worker restituisce solo
  rc + tail).

### Risultati benchmark (pagina 3575, EN→IT, 4 core, 3 run con `--fresh`)

Mediana dei tempi (`tools/bench_page.py`, `tools/bench_results.jsonl`):

| Config | run (s) | mediana | note |
|---|---:|---:|---|
| baseline (feature OFF) | 52,7 / 42,5 / 40,3 | **42,5** | ~coerente con `BENCH_LLM.md` |
| patch sole (worker 4) | 38,7 / 35,7 / 34,1 | **35,7** | −6,8 s |
| fast + preset rapido (4) | 34,7 / 35,9 / 35,2 | **35,2** | B2 quasi nullo su questa pagina |
| fast + preset rapido (12) | 33,9 / 33,8 / 36,1 | **33,9** | +2-3 worker ≈ −1,3 s |
| **cache calda** (fast+flags, 4) | 21,9 / 21,0 / 21,0 | **21,0** | scenario desktop (usa la cache) |
| cache calda (feature OFF, 4) | 31,4 / 30,2 / 29,7 | **30,2** | pavimento "puro" senza patch |
| google (fast+flags, 4) | 26,4 / 22,6 | ~24,5 | il wrapper vale anche sulla catena gratuita |
| Fase 2: Mercury + worker (4) | 36,1 / 32,4 | ~34,2 | worker: −2,8 s sul run a regime |
| **Fase 3: gpt-oss/Groq + minimal (8)** | 27,3 / 28,6 / 31,3 | **28,6** | senza worker |
| **Fase 1+2+3: gpt-oss + minimal + worker (8)** | 24,1 / 22,2 / 24,1 / 22,5 / 22,5 / 30,1 | **23,3** | min 22,2 — **≤ 30 s a freddo** |

Lettura:
- la Fase 1 riduce di ~7-9 s la traduzione fresca (≈ −17-20%); il grosso viene
  dalle **patch** (font cache + MemoryMonitor), non dai flag B2;
- il **pavimento CPU** resta ~21 s (run a cache calda: LLM quasi assente);
  senza patch il pavimento è ~30 s (quota fissa −9 s);
- **Fase 3** (gpt-oss-120b su Groq + `reasoning-effort minimal`) porta il caso
  freddo a ~28,6 s;
- **Fase 2** (worker persistente) toglie ~2,8 s di costi di avvio per pagina;
- **combinata Fase 1+2+3**: mediana **23,3 s a freddo** (min 22,2) → **obiettivo
  ≤ 30 s raggiunto** con margine, contro i 42,5 s di baseline (**−45%**).
- Il "worker persistente" si pre-avvia in background: la prima pagina non paga
  più l'avvio.

---

## 11. Contesto utente

- Lingua utente: **italiano** — rispondere in italiano.
- Requisito originale: "creare una app utilizzando il codice di noesis-pdf-reader-lite e
  incorporando codice di pdfcloner e noesis-pdf-reader"; scopo = produrre un **clone della
  pagina tradotta** che mantiene il layout, con i tre motori di pdfcloner.
- Richieste successive recepite: navigazione completa; release multipiattaforma; repo
  pubblico con remote SSH; barra motori collassabile con restituzione dello spazio e
  persistenza.
- `OPENROUTER_API_KEY` presente nell'ambiente (motore LLM operativo).

---

## 12. Release — preparazione (26/09)

### 0.1.11 (fix 50s su Windows)
- **Traduzione in-process nel worker** (`engine_worker.py`, `engine_patch.py`):
  `pdf2zh_next` lancia un child `multiprocessing` **per pagina**. Su Windows il
  default è `spawn` → il child riparte freddo e non eredita lo stato caldo del
  worker né le patch (misurato simulando lo spawn su Linux: 22.6s → 37.8s; su
  Windows ~50s). Nel worker la traduzione ora gira in-process (stessa strada di
  `--debug`, ma senza debug), **solo su Windows** (su Linux/macOS resta `fork`).
  Aggiunto `gc.collect()` dopo ogni pagina (memoria 3.0GB → 0.8-1.3GB).
  Kill-switch: `NOESIS_INPROC=0`.
- Misure (pag. 3575, gpt-oss+Groq): parità Windows (spawn) **26.9s**; Linux
  default 22.2s. Test: desktop **553 OK**, service **310**.
- Allineato il service (`app/engine_patch.py`, `app/engine_worker.py`).

### 0.1.10 (correzioni da test sul campo)
- **Worker senza finestra** (`engine_client.py`): il worker persistente era
  avviato **senza soppressione della console** → all'avvio compariva una
  finestra console muta (la "PowerShell" di Windows 11). Ora parte con
  `CREATE_NO_WINDOW` (nessuna finestra).
- **Proxy ridotto a icona** (`proxy_manager.py` + `engine_client.py`): lo
  show-state di `STARTUPINFO` vale solo se viene creata una **nuova** console;
  mancava `CREATE_NEW_CONSOLE`. Ora il proxy parte minimizzato e senza rubare
  il focus (`SW_SHOWMINNOACTIVE`).
- **Export lento / hang** (`clone_engine.py`): l'engine dedicato dell'export
  condivideva la `work_dir` (`_tmp/worker`) col worker del pannello → due worker
  sullo stesso `worker.ready`, client connesso a quello sbagliato (hang su
  Linux, riavvii + `SubprocessCrashError -15` su Windows). Ora ogni engine ha
  la sua `work_dir` (`_tmp/worker/<tag>`) e l'engine di export pre-avvia il
  worker. Export misurato: 23.1 / 21.8 / 20.9s.
- `installer.nsi` → `0.1.10`. Test: desktop **549 OK**, service **310**.
- Allineato il service (`app/engine.py`, `app/engine_client.py`).

### 0.1.9 (correzioni da test sul campo)
- **Preset non persistito** (`main.py`): `performance_preset` (e
  `numeric_lists`) non erano salvati da `_apply_settings`; riaprendo le
  impostazioni il preset tornava a "Normale" e, dando OK, `fast_engine` veniva
  azzerato → la traduzione (singola e batch) ripartiva lenta. Ora il preset è
  salvato; in `_load_values` i valori salvati vincono sui default del preset e,
  per le config delle versioni precedenti (senza preset ma con motore veloce
  attivo), il preset viene **inferito** invece di azzerare tutto.
- **Numbox del wizard** (`main.py` + `theme.py`): i campi "da/a pagina" erano
  limitati a 62px → si vedevano due cifre e le frecce coprivano il testo. Ora
  la larghezza minima è calcolata dalle metriche del font per tutte le cifre
  del numero di pagine, niente clamp fisso, e il QSS riserva lo spazio a destra
  per i pulsanti freccia.
- **Proxy su Windows** (`proxy_manager.py`): avviato **ridotto a icona** senza
  rubare il focus (`SW_SHOWMINNOACTIVE`), invece di `CREATE_NO_WINDOW`.
- `installer.nsi` → `0.1.9`. Test: **543 OK**.

### 0.1.8 (correzione)
- **Fix proxy nella build Windows/congelata**: `proxy_manager` usava
  `sys.executable` (che in PyInstaller è l'eseguibile dell'app, non Python) →
  il proxy non partiva e "Prova provider" dava errore. Ora usa il **Python del
  venv del motore** (`.venv2`), con fallback a `python`/`python3`/`py` dal PATH;
  aggiunto `CREATE_NO_WINDOW` (niente finestra console).
- `installer.nsi` → `0.1.8`.

### 0.1.7
- **Packaging** (`.github/workflows/release.yml`): aggiunti gli `--add-data`
  per `engine_patch.py`, `engine_wrapper.py`, `engine_worker.py`,
  `engine_client.py`, `proxy_manager.py` (→ `.`) e `tools/provider_proxy.py`
  (→ `tools`). Senza, nelle build PyInstaller il motore veloce/worker degradavano
  e il proxy provider non era disponibile.
- **Versione**: `installer.nsi` → `0.1.7`.
- **Motore pinnato**: `pdf2zh_next==2.9.0` (costante `ENGINE_PACKAGE` in
  `clone_engine.py`, `setup_engine.sh/.ps1`, `run.sh`) — versione testata con le
  patch runtime.
- **Proxy robusto**: gestione `BrokenPipeError` + **idle-timeout** (default
  1800 s, `PROXY_IDLE_TIMEOUT`) per evitare processi orfani.
- **Guida utente** (`docs/help/*`, 5 lingue): nuova sezione **Prestazioni**
  (Normale/Veloce/Massima, modello/base URL, prompt, proxy + Prova provider,
  liste numerate).
- **`.gitignore`**: esclusi `*_mockup.html` e `BENCH_LLM.md`.
- **File legali/comunità** versionati (LICENSE, NOTICE, CLA, CONTRIBUTING,
  SECURITY, TRADEMARK, ADDITIONAL_TERMS, dependabot).
- **Default OFF**: `fast_engine`/`fast_flags`/`fast_worker`/`numeric_lists`/
  `llm_proxy_autostart` = False, preset `normal` → comportamento invariato.
- Test: desktop **536 OK**, service **310**.

### Smoke post-build (da eseguire per piattaforma)
Normal (Mercury/google/bing) · Veloce (Mercury) · Massima (proxy autostart +
"Prova provider" → Groq) · liste numerate (pag. 401) · persistenza impostazioni
al riavvio · nessun processo orfano alla chiusura.
