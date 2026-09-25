"""Motore di clonazione pagina-per-pagina con pdf2zh_next v2 (PDFMathTranslate/BabelDOC).

Adattamento desktop di ``pdfcloner/translator.py``:

- più documenti aperti a runtime (nessuna variabile globale ``SRC_PDF``);
- cache nella cartella dati dell'app, per documento + engine + coppia linguistica;
- tre motori di traduzione:
    * ``google``  = catena gratuita ``gtranslate_cli.py`` via ``--clitranslator``
                    (dict-chrome-ex -> translate-pa -> gtx -> microsoft -> LLM)
    * ``bing``    = traduttore Bing built-in di pdf2zh_next
    * ``llm``     = LLM via OpenRouter (flag pdf2zh ``--openai``; modello Mercury)

Il modulo è puro Python (nessuna dipendenza da PyQt): l'app lo guida da un
thread di background e ne mostra il risultato.

Flusso per ogni pagina ed engine:
  1. split: estrazione della singola pagina in un PDF leggero (cache/split/)
  2. traduzione della pagina via pdf2zh_next CLI
  3. il ``*.mono.pdf`` prodotto viene spostato in cache/translated/<engine>/
"""

from __future__ import annotations

import contextlib
import glob
import hashlib
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

from pagelabels import build_page_labels

log = logging.getLogger("clone_engine")

# Engine selezionabili nella UI (label via i18n). Ordine = ordine dei radio.
# ``llm`` usa il percorso "OpenAI-compatibile" (flag --openai) di pdf2zh_next
# puntato a OpenRouter (Mercury), NON OpenAI. L'alias ``openai`` è accettato.
ENGINES: tuple[str, ...] = ("google", "bing", "llm")
ENGINE_ALIASES: dict[str, str] = {"openai": "llm"}


def normalize_engine(engine: str) -> str:
    """Normalizza l'id motore (accetta l'alias storico ``openai`` → ``llm``)."""
    code = (engine or "").strip().lower()
    return ENGINE_ALIASES.get(code, code)

DEFAULT_MODEL = "inception/mercury-2.5"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
PAGE_TIMEOUT = 900  # secondi: pdf2zh_next + BabelDOC su una pagina densa
LLM_TIMEOUT = 90  # secondi: timeout per singola chiamata LLM

# Versione dello schema di cache: incrementarla invalida la cache esistente
# (evita di servire output prodotti da versioni diverse del motore).
CACHE_SCHEMA_VERSION = "1"


class TranslationCancelled(RuntimeError):
    """Traduzione annullata dall'utente (subprocess terminato)."""


def _looks_like_auth_error(text: str) -> bool:
    """True se lo stderr di pdf2zh_next indica una chiave OpenRouter invalida."""
    return classify_engine_failure(text) == "invalid_key"


# Marcatore → codice, in ordine di priorità. Il primo che compare vince.
_FAILURE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "invalid_key",
        (
            "401",
            "unauthorized",
            "invalid api key",
            "invalid_api_key",
            "no auth credentials",
            "authentication",
        ),
    ),
    ("forbidden", ("403", "forbidden", "permission denied")),
    (
        "no_credits",
        (
            "402",
            "payment required",
            "insufficient credit",
            "insufficient_quota",
            "quota exceeded",
            "out of credits",
        ),
    ),
    ("rate_limited", ("429", "rate limit", "too many requests")),
    (
        "model_not_found",
        ("404", "model not found", "no endpoints found", "not a valid model"),
    ),
    (
        "network",
        (
            "connection",
            "timed out",
            "timeout",
            "getaddrinfo",
            "name or service not known",
            "temporary failure in name resolution",
            "ssl",
            "proxy",
            "network is unreachable",
            "max retries exceeded",
            "nodename nor servname",
        ),
    ),
)


def classify_engine_failure(text: str) -> str:
    """Classifica un fallimento di ``pdf2zh_next`` a partire dallo stderr.

    Ritorna uno tra: ``invalid_key``, ``forbidden``, ``no_credits``,
    ``rate_limited``, ``model_not_found``, ``network``, ``unknown``.
    """
    low = (text or "").lower()
    for code, markers in _FAILURE_MARKERS:
        if any(marker in low for marker in markers):
            return code
    return "unknown"


def _engine_failure_code(engine: str, stderr: str, stdout: str) -> str | None:
    """Codice classificato per il motore, oppure ``None`` se non riconosciuto.

    Guarda **sia stderr sia stdout**: con alcune chiavi non valide
    ``pdf2zh_next`` esce con codice **0**, stderr vuoto e l'errore (401) su
    stdout, quindi la sola stderr non basta.
    """
    code = classify_engine_failure((stderr or "") + "\n" + (stdout or ""))
    if code != "unknown" and (engine == "llm" or code == "network"):
        return code
    return None


def _is_windows() -> bool:
    return os.name == "nt"


def _bin_name(base: str) -> str:
    return f"{base}.exe" if _is_windows() else base


def _no_window_kwargs() -> dict:
    """Kwargs extra per ``Popen``/``run``: niente console su Windows.

    L'app è GUI (``--windowed``): senza ``CREATE_NO_WINDOW`` ogni subprocess
    (``uv``, ``pdf2zh_next``, ``taskkill``) farebbe comparire una finestra
    console davanti all'app. Vengono creati anche i figli senza finestra.
    """
    if not _is_windows():
        return {}
    kwargs: dict = {}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if flags:
        kwargs["creationflags"] = flags
    startupinfo_cls = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_cls is not None:
        startupinfo = startupinfo_cls()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        kwargs["startupinfo"] = startupinfo
    return kwargs


def _candidate_pdf2zh() -> list[Path]:
    """Percorsi candidati per l'eseguibile pdf2zh_next, in ordine di priorità.

    Include il venv2 accanto al modulo (sviluppo), accanto all'eseguibile
    congelato, e i venv di pdfcloner (comodo in sviluppo sulla stessa macchina).
    """
    here = Path(__file__).resolve().parent
    roots = [here, Path.cwd()]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        # Windows/Linux: cartella dell'eseguibile. macOS .app:
        # Contents/MacOS -> Contents -> la cartella accanto al bundle.
        roots.insert(0, exe_dir)
        roots.extend([exe_dir.parent, exe_dir.parent.parent])
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass))
    # basi preferite (accanto all'app oppure per-utente) + vicini di sviluppo
    roots.extend(_engine_roots())
    roots.extend([here.parent / "pdfcloner", here.parent / "noesis-pdf-cloner"])

    exe = _bin_name("pdf2zh_next")
    candidates: list[Path] = []
    for root in roots:
        candidates.append(root / ".venv2" / "bin" / exe)
        candidates.append(root / ".venv2" / "Scripts" / exe)
    return candidates


def find_pdf2zh_bin(override: str | Path | None = None) -> Path | None:
    """Localizza pdf2zh_next.

    Priorità: override esplicito (Impostazioni) -> env ``PDF2ZH_BIN`` ->
    candidati automatici (``.venv2`` accanto all'app/repo).
    """
    seen: set[str] = set()
    ordered: list[Path] = []
    if override:
        ordered.append(Path(override).expanduser())
    env = os.environ.get("PDF2ZH_BIN")
    if env:
        ordered.append(Path(env).expanduser())
    ordered.extend(_candidate_pdf2zh())

    for cand in ordered:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        if cand.is_file():
            return cand
    return None


def venv_python_for(pdf2zh_bin: Path) -> str:
    """Python del venv che possiede ``pdf2zh_next`` (per ``--clitranslator``)."""
    bin_dir = pdf2zh_bin.parent
    for name in (_bin_name("python"), _bin_name("python3")):
        candidate = bin_dir / name
        if candidate.is_file():
            return str(candidate)
    # fallback: l'interprete corrente
    return sys.executable


_APP_DATA_OVERRIDE: Path | None = None


def set_app_data_dir(path: str | Path | None) -> None:
    """Imposta la cartella dati per-utente (la GUI passa ``QStandardPaths``)."""
    global _APP_DATA_OVERRIDE
    _APP_DATA_OVERRIDE = Path(path) if path else None


def app_data_dir() -> Path:
    """Cartella dati per-utente, coerente con ``QStandardPaths.AppDataLocation``.

    Senza Qt: su Windows ``%APPDATA%`` (Roaming), su macOS
    ``~/Library/Application Support``, su Linux ``$XDG_DATA_HOME``/``~/.local/share``.
    """
    if _APP_DATA_OVERRIDE is not None:
        return _APP_DATA_OVERRIDE
    if _is_windows():
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "noesis-pdf-cloner"


def user_engine_base() -> Path:
    """Base per-utente del motore (fallback se la cartella dell'app non è scrivibile)."""
    return app_data_dir() / "engine"


def _is_writable(path: Path) -> bool:
    """True se ``path`` esiste (o è creabile) ed è scrivibile."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".noesis-write-test"
        probe.write_text("")
        probe.unlink()
        return True
    except OSError:
        return False


def _engine_roots() -> list[Path]:
    """Basi candidate per l'installazione del motore, in ordine di preferenza."""
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if sys.platform == "darwin":
            # Dentro un .app non si scrive: la cartella che contiene il bundle.
            roots.append(exe_dir.parent.parent)
        else:
            roots.append(exe_dir)
    else:
        roots.append(Path(__file__).resolve().parent)
    roots.append(user_engine_base())
    return roots


def engine_base_dir() -> Path:
    """Cartella dove installare ``.venv2``: la prima scrivibile tra quelle note.

    Build congelato → accanto all'eseguibile se scrivibile, altrimenti la
    cartella dati per-utente; sviluppo → radice del repo.
    """
    for root in _engine_roots():
        if _is_writable(root):
            return root
    return user_engine_base()


def engine_venv_dir(base: str | Path | None = None) -> Path:
    """Percorso del venv del motore (``.venv2`` accanto all'app)."""
    root = Path(base) if base else engine_base_dir()
    return root / ".venv2"


def engine_venv_python(venv_dir: str | Path) -> Path:
    """Interprete Python dentro ``.venv2`` (Scripts su Windows, bin altrove)."""
    venv = Path(venv_dir)
    return (venv / "Scripts" / "python.exe") if _is_windows() else (venv / "bin" / "python")


def _bundled_uv() -> Path | None:
    """``uv`` incluso nel bundle PyInstaller (``sys._MEIPASS``), se presente."""
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return None
    path = Path(meipass) / _bin_name("uv")
    if not path.is_file():
        return None
    if not _is_windows():
        # Il bit di esecuzione può perdersi impacchettando i dati: lo rimettiamo.
        with contextlib.suppress(OSError):
            path.chmod(path.stat().st_mode | 0o111)
    return path


def find_uv() -> str | None:
    """Localizza ``uv``.

    Priorità: ``UV`` esplicito → ``uv`` incluso nel bundle (deterministico,
    versione che abbiamo testato) → ``PATH`` di sistema.
    """
    env = os.environ.get("UV")
    if env:
        candidate = Path(env).expanduser()
        if candidate.is_file():
            return str(candidate)
    bundled = _bundled_uv()
    if bundled is not None:
        return str(bundled)
    return shutil.which("uv")


def _terminate_process(proc: subprocess.Popen) -> None:
    """Termina un processo dell'installer (best effort, cross-platform)."""
    with contextlib.suppress(Exception):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        with contextlib.suppress(Exception):
            proc.kill()


def install_engine(
    base: str | Path | None = None,
    log_line=None,
    cancel: threading.Event | None = None,
) -> Path:
    """Installa ``pdf2zh_next`` in ``.venv2`` accanto all'app usando ``uv``.

    ``log_line`` riceve le righe di output; ``cancel`` interrompe. Ritorna il
    percorso dell'eseguibile installato. Solleva ``RuntimeError`` con codice
    macchina: ``"no_uv"`` (uv assente), ``"cancelled"``, ``"failed"``.
    """
    uv = find_uv()
    if not uv:
        raise RuntimeError("no_uv")

    venv = engine_venv_dir(base)
    workdir = venv.parent
    python = engine_venv_python(venv)

    def _emit(line: str) -> None:
        if log_line is not None:
            log_line(line.rstrip())

    _emit(f"(cartella del motore: {workdir})")

    def _run(cmd: list[str]) -> None:
        proc = subprocess.Popen(
            cmd,
            cwd=str(workdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            **_no_window_kwargs(),
        )
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if cancel is not None and cancel.is_set():
                    _terminate_process(proc)
                    raise RuntimeError("cancelled")
                _emit(line)
        finally:
            code = proc.wait()
        if code != 0:
            raise RuntimeError("failed")

    if venv.exists() and not python.is_file():
        # venv presente ma rotto/incompleto: lo si ricrea da zero.
        _emit(f"(rimuovo il venv incompleto: {venv})")
        shutil.rmtree(venv, ignore_errors=True)
    if not python.is_file():
        _emit(f"$ {uv} venv --python 3.12 {venv}")
        _run([uv, "venv", "--python", "3.12", str(venv)])
    _emit(f"$ {uv} pip install --python {python} pdf2zh_next")
    _run([uv, "pip", "install", "--python", str(python), "pdf2zh_next"])

    installed = engine_venv_dir(base) / ("Scripts" if _is_windows() else "bin")
    installed = installed / _bin_name("pdf2zh_next")
    if not installed.is_file():
        found = find_pdf2zh_bin()
        if found is None:
            raise RuntimeError("failed")
        return found
    return installed


def _gtranslate_cli_path() -> Path:
    """Percorso di ``gtranslate_cli.py`` (sorgente o bundle PyInstaller)."""
    here = Path(__file__).resolve().with_name("gtranslate_cli.py")
    if here.is_file():
        return here
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        bundled = Path(meipass) / "gtranslate_cli.py"
        if bundled.is_file():
            return bundled
    return here


def page_has_text(path: str | Path) -> bool:
    """True se la pagina contiene testo estraibile.

    Serve a distinguere due casi con lo stesso sintomo (nessun ``.mono.pdf``):
    pagina **senza testo** (copertina, scansione) → si usa l'originale; pagina
    con testo ma nessuna traduzione → è un errore vero da segnalare.
    """
    try:
        import pymupdf  # import locale: l'app può girare senza render
    except ImportError:  # pragma: no cover
        return True
    try:
        with pymupdf.open(str(path)) as doc:
            if len(doc) == 0:
                return False
            return bool(doc[0].get_text().strip())
    except Exception:  # noqa: BLE001 — in dubbio non mascherare un errore
        return True


class CloneEngine:
    """Pipeline split -> pdf2zh_next -> cache, per documento e per engine."""

    def __init__(
        self,
        cache_root: str | Path,
        pdf2zh_bin: str | Path | None = None,
        max_concurrent: int = 2,
    ) -> None:
        self.cache_root = Path(cache_root)
        self.split_root = self.cache_root / "split"
        self.translated_root = self.cache_root / "translated"
        self.events_file = self.cache_root / "engine_events.jsonl"
        self._pdf2zh_bin: Path | None = None
        self._pdf2zh_override = str(pdf2zh_bin) if pdf2zh_bin else ""

        # Lingue: impostate dall'app prima di tradurre.
        self.lang_in: str = "en"
        self.lang_out: str = "it"
        self.qps_google: int = 2
        self.qps_bing: int = 5
        self.llm_model: str = os.environ.get("PDF_LLM_MODEL", DEFAULT_MODEL)
        self.llm_base_url: str = os.environ.get(
            "PDF_LLM_BASE_URL", DEFAULT_BASE_URL
        )
        # Numero di richieste LLM in parallelo dentro una pagina (punto 6):
        # alza il throughput. Impostato dall'app da ``llm_pool_workers``.
        self.llm_pool_workers: int = 4
        self.llm_timeout: int = LLM_TIMEOUT

        self._src_pdf: Path | None = None
        self._doc_key: str = ""
        # stato per (pagina, engine, lang_in, lang_out)
        self._status: dict[tuple, str] = {}
        self._locks: dict[tuple, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._semaphore = threading.Semaphore(max(1, max_concurrent))

        self.split_root.mkdir(parents=True, exist_ok=True)
        self.translated_root.mkdir(parents=True, exist_ok=True)

    # ── configurazione ────────────────────────────────────────────────

    def set_pdf2zh_bin(self, pdf2zh_bin: str | Path | None) -> None:
        """Cambia l'override dell'eseguibile (dalle Impostazioni)."""
        self._pdf2zh_override = str(pdf2zh_bin) if pdf2zh_bin else ""
        self._pdf2zh_bin = None

    def pdf2zh_bin(self) -> Path | None:
        """Ritorna (risolvendo la prima volta) l'eseguibile pdf2zh_next."""
        if self._pdf2zh_bin is None or not self._pdf2zh_bin.is_file():
            self._pdf2zh_bin = find_pdf2zh_bin(self._pdf2zh_override)
        return self._pdf2zh_bin

    def available(self) -> bool:
        """True se l'eseguibile pdf2zh_next è stato trovato."""
        return self.pdf2zh_bin() is not None

    @staticmethod
    def page_labels(path: str | Path) -> list[str]:
        """Etichette ``/PageLabels`` per pagina (fallback: numero fisico).

        Allineato a ``app/engine.py::CloneEngine.page_labels`` del servizio.
        """
        import pymupdf

        with pymupdf.open(str(path)) as document:
            count = document.page_count
            spec = []
            if hasattr(document, "get_page_labels"):
                with contextlib.suppress(Exception):
                    spec = document.get_page_labels()
        return build_page_labels(spec, count)

    def set_document(self, path: str | Path | None) -> None:
        """Imposta il PDF sorgente; resetta lo stato dipendente dal documento."""
        self._status.clear()
        if path is None:
            self._src_pdf = None
            self._doc_key = ""
            return
        self._src_pdf = Path(path)
        try:
            st = self._src_pdf.stat()
            self._doc_key = f"{self._src_pdf.stem}_{st.st_size}"
        except OSError:
            self._doc_key = self._src_pdf.stem

    # ── percorsi cache ────────────────────────────────────────────────

    def _lang_key(self) -> str:
        return f"{self.lang_in}-{self.lang_out}"

    def _version_tag(self) -> str:
        """Segmento di cache: schema + modello LLM.

        Evita di riusare output prodotti da versioni diverse del motore o da un
        modello LLM diverso (come nella versione server).
        """
        tag = f"cs{CACHE_SCHEMA_VERSION}"
        model = (self.llm_model or "").strip()
        if model:
            tag += "-" + hashlib.sha1(model.encode()).hexdigest()[:8]
        return tag

    def split_path(self, page: int) -> Path:
        return self.split_root / self._doc_key / f"page_{page:06d}.pdf"

    def translated_path_for(
        self,
        page: int,
        engine: str,
        lang_in: str | None = None,
        lang_out: str | None = None,
    ) -> Path:
        """Percorso del clone tradotto **senza** creare la cartella.

        Getter puro, usato dagli scan di cache/export: non ha effetti
        collaterali sul filesystem. ``lang_in``/``lang_out`` permettono di
        interrogare una coppia linguistica diversa da quella corrente
        (es. anteprima export) senza mutare l'engine.
        """
        li = self.lang_in if lang_in is None else lang_in
        lo = self.lang_out if lang_out is None else lang_out
        return (
            self.translated_root
            / self._doc_key
            / engine
            / f"{li}-{lo}"
            / self._version_tag()
            / f"page_{page:06d}.pdf"
        )

    def translated_path(
        self,
        page: int,
        engine: str,
        lang_in: str | None = None,
        lang_out: str | None = None,
    ) -> Path:
        out = self.translated_path_for(page, engine, lang_in, lang_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        return out

    def is_cached(
        self,
        page: int,
        engine: str,
        lang_in: str | None = None,
        lang_out: str | None = None,
    ) -> bool:
        return self.translated_path_for(page, engine, lang_in, lang_out).is_file()

    def cached_pages(
        self,
        page_from: int,
        page_to: int,
        engine: str,
        lang_in: str | None = None,
        lang_out: str | None = None,
    ) -> list[int]:
        """Indici **0-based** in ``[page_from, page_to]`` già in cache.

        Legge solo il filesystem: la cache sopravvive alle sessioni, quindi il
        risultato comprende i cloni prodotti in esecuzioni precedenti. Gli
        estremi sono accettati in qualunque ordine.
        """
        if page_from > page_to:
            page_from, page_to = page_to, page_from
        return [
            p
            for p in range(page_from, page_to + 1)
            if self.translated_path_for(
                p, engine, lang_in, lang_out
            ).is_file()
        ]

    # ── gestione cache (per la UI: una traduzione per pagina) ─────────────

    def _engine_cache_dir(self, engine: str) -> Path:
        """Cartella di cache di un motore per il documento corrente."""
        return self.translated_root / self._doc_key / normalize_engine(engine)

    def _page_cache_files(self, engine: str, page: int) -> list[Path]:
        """File di cache della pagina per il motore, in ogni lingua/versione."""
        if not self._doc_key:
            return []
        base = self._engine_cache_dir(engine)
        if not base.is_dir():
            return []
        name = f"page_{page:06d}.pdf"
        return [p for p in base.rglob(name) if p.is_file()]

    def cached_engines_for_page(self, page: int) -> list[str]:
        """Motori che hanno **questa pagina** in cache per il documento.

        Ordine stabile (``ENGINES``); conta anche coppie linguistiche diverse
        da quella corrente.
        """
        return [
            engine
            for engine in ENGINES
            if self._page_cache_files(engine, page)
        ]

    def page_cache_stats(self, engine: str, page: int) -> tuple[int, int]:
        """``(numero di file, byte)`` della pagina in cache per il motore."""
        files = self._page_cache_files(engine, page)
        size = 0
        for path in files:
            with contextlib.suppress(OSError):
                size += path.stat().st_size
        return len(files), size

    def purge_page_cache(self, engine: str, page: int) -> tuple[int, int]:
        """Elimina dalla cache **solo questa pagina** del motore.

        Ritorna ``(file rimossi, byte liberati)``. Le altre pagine dello stesso
        motore, le pagine estratte (``split/``) e gli altri motori restano.
        """
        files, size = self.page_cache_stats(engine, page)
        for path in self._page_cache_files(engine, page):
            with contextlib.suppress(OSError):
                path.unlink()
        # Stato in memoria: quella pagina di quel motore non è più "done".
        norm = normalize_engine(engine)
        for key in [k for k in self._status if k[1] == norm and k[0] == page]:
            self._status.pop(key, None)
        return files, size

    def export_pdf(
        self,
        pages,
        engine: str,
        dest: str | Path,
        lang_in: str | None = None,
        lang_out: str | None = None,
    ) -> int:
        """Unisce i mono-PDF tradotti delle ``pages`` (0-based) in ``dest``.

        Le pagine non in cache sono saltate; l'ordine è quello di ``pages``.
        Ritorna il numero di pagine scritte. Solleva ``ValueError`` se nessuna
        delle pagine richieste è disponibile.
        """
        import pymupdf  # import locale, come ensure_split

        dest = Path(dest)
        merged = 0
        with pymupdf.open() as out:
            for page in pages:
                src = self.translated_path_for(page, engine, lang_in, lang_out)
                if not src.is_file():
                    continue
                with pymupdf.open(str(src)) as doc:
                    out.insert_pdf(doc)
                    merged += 1
            if merged == 0:
                raise ValueError("nessuna pagina tradotta da esportare")
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            out.save(str(tmp), garbage=4, deflate=True)
            os.replace(tmp, dest)  # salvataggio atomico dell'export
        return merged

    def export_zip(
        self,
        pages,
        engine: str,
        dest: str | Path,
        lang_in: str | None = None,
        lang_out: str | None = None,
        stem: str | None = None,
    ) -> int:
        """Raccoglie i mono-PDF tradotti delle ``pages`` (0-based) in un ZIP.

        Come nello "stile pagine singole" del servizio: ogni pagina diventa una
        voce ``<stem>_pNNNN.pdf``. Le pagine non in cache sono saltate; ritorna
        il numero di pagine scritte e solleva ``ValueError`` se nessuna è
        disponibile.
        """
        dest = Path(dest)
        prefix = stem or "page"
        written = 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            for page in pages:
                src = self.translated_path_for(page, engine, lang_in, lang_out)
                if not src.is_file():
                    continue
                archive.write(src, arcname=f"{prefix}_p{page + 1:04d}.pdf")
                written += 1
        if written == 0:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise ValueError("nessuna pagina tradotta da esportare")
        os.replace(tmp, dest)  # salvataggio atomico dell'export
        return written

    def export_folder(
        self,
        pages,
        engine: str,
        dest_dir: str | Path,
        lang_in: str | None = None,
        lang_out: str | None = None,
        stem: str | None = None,
    ) -> int:
        """Scrive i mono-PDF tradotti delle ``pages`` (0-based) in ``dest_dir``.

        Ogni pagina diventa ``<stem>_pNNNN.pdf`` (stessa convenzione di
        ``export_zip`` e del servizio). Le pagine non in cache sono saltate;
        ritorna il numero di file scritti e solleva ``ValueError`` se nessuna
        è disponibile.
        """
        dest_dir = Path(dest_dir)
        prefix = stem or "page"
        written = 0
        dest_dir.mkdir(parents=True, exist_ok=True)
        for page in pages:
            src = self.translated_path_for(page, engine, lang_in, lang_out)
            if not src.is_file():
                continue
            shutil.copy2(src, dest_dir / f"{prefix}_p{page + 1:04d}.pdf")
            written += 1
        if written == 0:
            raise ValueError("nessuna pagina tradotta da esportare")
        return written

    # ── stato ─────────────────────────────────────────────────────────

    def _key(self, page: int, engine: str) -> tuple:
        return (page, engine, self.lang_in, self.lang_out)

    def status(self, page: int, engine: str) -> str:
        return self._status.get(self._key(page, engine), "none")

    def _lock_for(self, key: tuple) -> threading.Lock:
        with self._locks_guard:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def fallback_events(self, since_ts: float) -> list[str]:
        """Engine degradati (microsoft/llm) usati negli eventi da ``since_ts``."""
        fallback: set[str] = set()
        try:
            lines = self.events_file.read_text(encoding="utf-8").splitlines()[-100:]
        except OSError:
            return []
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("ts", 0) >= since_ts and ev.get("engine") in {
                "microsoft",
                "llm",
            }:
                fallback.add(ev["engine"])
        return sorted(fallback)

    # ── split ─────────────────────────────────────────────────────────

    def ensure_split(self, page: int) -> Path | None:
        """Estrae la singola pagina (0.2-2 MB) dal PDF sorgente. Una volta sola.

        ``page`` è un indice **0-based** (stessa convenzione del resto dell'app
        e di ``translated_path``/``split_path``).
        """
        if self._src_pdf is None:
            return None
        path = self.split_path(page)
        if path.is_file():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        import pymupdf  # import locale: l'app può girare senza la parte render

        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            src = pymupdf.open(str(self._src_pdf))
        except Exception:
            log.exception("apertura PDF sorgente fallita")
            return None
        try:
            new = pymupdf.open()
            new.insert_pdf(src, from_page=page, to_page=page)
            new.save(str(tmp), garbage=4, deflate=True)
            new.close()
            os.replace(tmp, path)  # scrittura atomica: mai file parziali in cache
            return path
        except Exception:
            log.exception("split fallito pagina %d", page)
            try:
                tmp.unlink()
            except OSError:
                pass
            return None
        finally:
            src.close()

    # ── traduzione ────────────────────────────────────────────────────

    def _translator_flags(self, engine: str) -> tuple[list[str], str]:
        """Flag CLI di pdf2zh_next per l'engine scelto, + nome descrittivo."""
        if normalize_engine(engine) == "llm":
            # La chiave NON va passata come flag: finirebbe in argv
            # (/proc/<pid>/cmdline). Viene iniettata nell'ambiente qui sotto
            # (PDF2ZH_OPENAI_API_KEY).
            flags = [
                "--openai",
                "--openai-model", self.llm_model,
                "--openai-base-url", self.llm_base_url,
                "--openai-timeout", str(int(self.llm_timeout)),
                # Un giro LLM in meno per pagina (il glossary automatico
                # estrae i termini con una chiamata extra): punto 6.
                "--no-auto-extract-glossary",
            ]
            workers = max(1, int(getattr(self, "llm_pool_workers", 1) or 1))
            if workers > 1:
                # Più segmenti tradotti insieme dentro la stessa pagina.
                flags += ["--pool-max-workers", str(workers)]
            return (flags, f"llm ({self.llm_model})")
        if engine == "google":
            py = venv_python_for(self.pdf2zh_bin() or Path(sys.executable))
            cli = _gtranslate_cli_path()
            return (
                [
                    "--clitranslator",
                    "--clitranslator-command", shlex.join([str(py), str(cli)]),
                    "--clitranslator-timeout", "120",
                    "--qps", str(self.qps_google),
                ],
                "google (gratuito, catena)",
            )
        return (["--bing", "--qps", str(self.qps_bing)], "bing")

    def translate_page(
        self,
        page: int,
        engine: str,
        cancel_event: threading.Event | None = None,
    ) -> Path | None:
        """Traduce la pagina se serve e ritorna il path del PDF tradotto.

        ``cancel_event`` (opzionale) interrompe il ``pdf2zh_next`` in corso: il
        subprocess è avviato in un *process group* e viene terminato, così
        l'annullamento non attende la fine della pagina (come nel servizio).
        """
        engine = normalize_engine(engine)
        if engine not in ENGINES:
            engine = "google"
        out = self.translated_path(page, engine)
        key = self._key(page, engine)
        if cancel_event is not None and cancel_event.is_set():
            self._status[key] = "cancelled"
            return None
        with self._lock_for(key):
            if out.is_file():
                self._status[key] = "done"
                return out
            if self._status.get(key) == "running":
                return None  # un altro worker ci sta già lavorando

            if self._src_pdf is None:
                self._status[key] = "error:nessun documento aperto"
                return None

            if engine == "llm" and not os.environ.get("OPENROUTER_API_KEY"):
                self._status[key] = "error:missing_key"
                return None

            pdf2zh = self.pdf2zh_bin()
            if pdf2zh is None:
                self._status[key] = "error:pdf2zh_next non trovato (installa il motore)"
                return None

            sp = self.ensure_split(page)
            if sp is None:
                self._status[key] = "error:impossibile estrarre la pagina"
                return None

        t_flags, t_name = self._translator_flags(engine)
        self._status[key] = "running"
        with self._semaphore:
            out_dir = self.translated_root / "_tmp" / f"tmp_{page:06d}_{engine}"
            shutil.rmtree(out_dir, ignore_errors=True)
            out_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                str(pdf2zh),
                str(sp),
                "--lang-in", self.lang_in,
                "--lang-out", self.lang_out,
                "--output", str(out_dir) + os.sep,
                "--watermark-output-mode", "no_watermark",
                "--no-dual",
                "--only-include-translated-page",
                "--disable-config-auto-save",
                "--disable-gui-sensitive-input",
            ] + t_flags
            # L'ambiente porta le lingue alla catena gratuita (gtranslate_cli.py,
            # eseguito come figlio di pdf2zh_next) e il file eventi dei fallback.
            env = dict(os.environ)
            env["PDF_LANG_IN"] = self.lang_in
            env["PDF_LANG_OUT"] = self.lang_out
            # UTF-8 per i subprocess figli (gtranslate_cli.py): su Windows evita
            # che le accentate escano in cp1252 e diventino U+FFFD nel pipe.
            env["PYTHONIOENCODING"] = "utf-8"
            env["CLONE_ENGINE_EVENTS"] = str(self.events_file)
            # Il fallback LLM della catena gratuita usa gli stessi modello e
            # base URL dell'engine, anche se cambiati via codice (come nel
            # servizio).
            env["PDF_LLM_MODEL"] = self.llm_model
            env["PDF_LLM_BASE_URL"] = self.llm_base_url
            # Chiave LLM: passata solo via ambiente, mai in argv. `OPENROUTER_API_KEY`
            # serve al fallback LLM della catena gratuita (gtranslate_cli.py);
            # `PDF2ZH_OPENAI_API_KEY` è la variabile letta da pdf2zh_next (prefisso PDF2ZH_).
            llm_key = os.environ.get("OPENROUTER_API_KEY", "")
            if llm_key:
                env["PDF2ZH_OPENAI_API_KEY"] = llm_key
            try:
                log.info("traduzione pagina %d via %s...", page, t_name)
                r = self._run_engine(cmd, env, cancel_event)
                log.info("pdf2zh_next pagina %d: rc=%d", page, r.returncode)
                if r.returncode != 0:
                    code = _engine_failure_code(engine, r.stderr, r.stdout)
                    if code is not None:
                        raise RuntimeError(code)
                    raise RuntimeError(
                        f"pdf2zh_next exit {r.returncode}: {(r.stderr or '')[-400:]}"
                    )
                monos = glob.glob(str(out_dir / "*.mono.pdf"))
                if not monos:
                    if page_has_text(sp):
                        # La pagina ha testo ma il motore non ha prodotto nulla:
                        # la traduzione è fallita. Anche con exit 0 può essere un
                        # errore riconoscibile (es. 401 gestito internamente e
                        # scritto su stdout): classifica l'output per un
                        # consiglio mirato, altrimenti usa il messaggio generico.
                        code = _engine_failure_code(engine, r.stderr, r.stdout)
                        if code is not None:
                            raise RuntimeError(code)
                        detail = (r.stderr or "").strip().splitlines()
                        if not detail:
                            detail = [
                                ln.strip()
                                for ln in (r.stdout or "").splitlines()
                                if ln.strip()
                            ]
                        tail = detail[-1][:200] if detail else ""
                        raise RuntimeError(
                            "il motore non ha tradotto la pagina"
                            + (f": {tail}" if tail else "")
                        )
                    # Pagina senza testo (copertina, scansione): il clone è la
                    # pagina stessa, non è un errore.
                    log.info(
                        "pagina %d (%s): nessun testo da tradurre, uso l'originale",
                        page, engine,
                    )
                    tmp_out = out.with_name(out.name + ".tmp")
                    shutil.copyfile(sp, tmp_out)
                    os.replace(tmp_out, str(out))  # scrittura atomica in cache
                    self._status[key] = "empty"
                    return out
                os.replace(monos[0], str(out))  # scrittura atomica in cache
                self._status[key] = "done"
                return out
            except TranslationCancelled:
                log.info("traduzione annullata pagina %d (%s)", page, engine)
                self._status[key] = "cancelled"
                return None
            except Exception as e:  # noqa: BLE001 — riportato in UI
                log.exception("traduzione fallita pagina %d (%s)", page, engine)
                self._status[key] = f"error:{e}"
                return None
            finally:
                shutil.rmtree(out_dir, ignore_errors=True)

    def _run_engine(
        self,
        cmd: list[str],
        env: dict,
        cancel_event: threading.Event | None = None,
    ) -> subprocess.CompletedProcess:
        """Esegue ``pdf2zh_next`` onorando timeout e annullamento.

        Il processo è avviato in una nuova sessione (POSIX) così che alla
        cancellazione si possa terminare l'intero albero con un segnale al
        process group (su Windows: ``taskkill /T /F``).
        """
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=not _is_windows(),
                **_no_window_kwargs(),
            )
        except OSError as exc:
            raise RuntimeError(f"avvio pdf2zh_next fallito: {exc}") from exc

        deadline = time.monotonic() + PAGE_TIMEOUT
        while True:
            if cancel_event is not None and cancel_event.is_set():
                self._kill(process)
                self._close_pipes(process)
                raise TranslationCancelled("traduzione annullata")
            if time.monotonic() > deadline:
                self._kill(process)
                self._close_pipes(process)
                raise RuntimeError("timeout della traduzione")
            try:
                stdout, stderr = process.communicate(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                continue
        return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)

    @staticmethod
    def _close_pipes(process: subprocess.Popen) -> None:
        """Chiude le pipe di un processo terminato senza ``communicate``."""
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        """Termina ``process`` e i suoi figli (process group / taskkill)."""
        if process.poll() is not None:
            return
        try:
            if not _is_windows():
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:  # pragma: no cover - Windows
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    **_no_window_kwargs(),
                )
        except (ProcessLookupError, PermissionError):
            return
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            with contextlib.suppress(Exception):
                if not _is_windows():
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()

    def start(self, page: int, engine: str) -> None:
        """Avvia la traduzione in un thread di background (no-op se in cache)."""
        if self.is_cached(page, engine):
            self._status[self._key(page, engine)] = "done"
            return
        if self._status.get(self._key(page, engine)) == "running":
            return
        threading.Thread(
            target=self.translate_page, args=(page, engine), daemon=True
        ).start()

    def run_start_ts(self) -> float:
        return time.time()
