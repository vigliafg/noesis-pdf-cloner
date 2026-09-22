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

# Versione dello schema di cache: incrementarla invalida la cache esistente
# (evita di servire output prodotti da versioni diverse del motore).
CACHE_SCHEMA_VERSION = "1"


class TranslationCancelled(RuntimeError):
    """Traduzione annullata dall'utente (subprocess terminato)."""


def _looks_like_auth_error(text: str) -> bool:
    """True se lo stderr di pdf2zh_next indica una chiave OpenRouter invalida."""
    low = (text or "").lower()
    return (
        "401" in low
        or "unauthorized" in low
        or "invalid api key" in low
        or "invalid_api_key" in low
    )


def _is_windows() -> bool:
    return os.name == "nt"


def _bin_name(base: str) -> str:
    return f"{base}.exe" if _is_windows() else base


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
    # vicini utili in sviluppo (repo sorelli)
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
            return (
                [
                    "--openai",
                    "--openai-model", self.llm_model,
                    "--openai-base-url", self.llm_base_url,
                    "--openai-api-key", os.environ.get("OPENROUTER_API_KEY", ""),
                ],
                f"llm ({self.llm_model})",
            )
        if engine == "google":
            py = venv_python_for(self.pdf2zh_bin() or Path(sys.executable))
            cli = _gtranslate_cli_path()
            return (
                [
                    "--clitranslator",
                    "--clitranslator-command", f"{py} {cli}",
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
            env["CLONE_ENGINE_EVENTS"] = str(self.events_file)
            # Il fallback LLM della catena gratuita usa gli stessi modello e
            # base URL dell'engine, anche se cambiati via codice (come nel
            # servizio).
            env["PDF_LLM_MODEL"] = self.llm_model
            env["PDF_LLM_BASE_URL"] = self.llm_base_url
            try:
                log.info("traduzione pagina %d via %s...", page, t_name)
                r = self._run_engine(cmd, env, cancel_event)
                log.info("pdf2zh_next pagina %d: rc=%d", page, r.returncode)
                if r.returncode != 0:
                    stderr = r.stderr or ""
                    if engine == "llm" and _looks_like_auth_error(stderr):
                        raise RuntimeError("invalid_key")
                    raise RuntimeError(
                        f"pdf2zh_next exit {r.returncode}: {stderr[-400:]}"
                    )
                monos = glob.glob(str(out_dir / "*.mono.pdf"))
                if not monos:
                    raise RuntimeError("pdf2zh_next non ha prodotto il file mono")
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
