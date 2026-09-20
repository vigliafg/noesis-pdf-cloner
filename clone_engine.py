"""Motore di clonazione pagina-per-pagina con pdf2zh_next v2 (PDFMathTranslate/BabelDOC).

Adattamento desktop di ``pdfcloner/translator.py``:

- più documenti aperti a runtime (nessuna variabile globale ``SRC_PDF``);
- cache nella cartella dati dell'app, per documento + engine + coppia linguistica;
- tre motori di traduzione:
    * ``google``  = catena gratuita ``gtranslate_cli.py`` via ``--clitranslator``
                    (dict-chrome-ex -> translate-pa -> gtx -> microsoft -> LLM)
    * ``bing``    = traduttore Bing built-in di pdf2zh_next
    * ``openai``  = LLM via OpenRouter (``--openai``)

Il modulo è puro Python (nessuna dipendenza da PyQt): l'app lo guida da un
thread di background e ne mostra il risultato.

Flusso per ogni pagina ed engine:
  1. split: estrazione della singola pagina in un PDF leggero (cache/split/)
  2. traduzione della pagina via pdf2zh_next CLI
  3. il ``*.mono.pdf`` prodotto viene spostato in cache/translated/<engine>/
"""

from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("clone_engine")

# Engine selezionabili nella UI (label via i18n). Ordine = ordine dei radio.
ENGINES: tuple[str, ...] = ("google", "bing", "openai")

DEFAULT_MODEL = "inception/mercury-2.5"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
PAGE_TIMEOUT = 900  # secondi: pdf2zh_next + BabelDOC su una pagina densa


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

    def split_path(self, page: int) -> Path:
        return self.split_root / self._doc_key / f"page_{page:06d}.pdf"

    def translated_path(self, page: int, engine: str) -> Path:
        d = (
            self.translated_root
            / self._doc_key
            / engine
            / self._lang_key()
        )
        d.mkdir(parents=True, exist_ok=True)
        return d / f"page_{page:06d}.pdf"

    def is_cached(self, page: int, engine: str) -> bool:
        return self.translated_path(page, engine).is_file()

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
        """Estrae la singola pagina (0.2-2 MB) dal PDF sorgente. Una volta sola."""
        if self._src_pdf is None:
            return None
        path = self.split_path(page)
        if path.is_file():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        import pymupdf  # import locale: l'app può girare senza la parte render

        try:
            src = pymupdf.open(str(self._src_pdf))
        except Exception:
            log.exception("apertura PDF sorgente fallita")
            return None
        try:
            new = pymupdf.open()
            new.insert_pdf(src, from_page=page - 1, to_page=page - 1)
            new.save(str(path), garbage=4, deflate=True)
            new.close()
            return path
        except Exception:
            log.exception("split fallito pagina %d", page)
            try:
                path.unlink()
            except OSError:
                pass
            return None
        finally:
            src.close()

    # ── traduzione ────────────────────────────────────────────────────

    def _translator_flags(self, engine: str) -> tuple[list[str], str]:
        """Flag CLI di pdf2zh_next per l'engine scelto, + nome descrittivo."""
        if engine == "openai":
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

    def translate_page(self, page: int, engine: str) -> Path | None:
        """Traduce la pagina se serve e ritorna il path del PDF tradotto."""
        if engine not in ENGINES:
            engine = "google"
        out = self.translated_path(page, engine)
        key = self._key(page, engine)
        with self._lock_for(key):
            if out.is_file():
                self._status[key] = "done"
                return out
            if self._status.get(key) == "running":
                return None  # un altro worker ci sta già lavorando

            if self._src_pdf is None:
                self._status[key] = "error:nessun documento aperto"
                return None

            if engine == "openai" and not os.environ.get("OPENROUTER_API_KEY"):
                self._status[key] = "error:OPENROUTER_API_KEY non impostata"
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
            try:
                log.info("traduzione pagina %d via %s...", page, t_name)
                r = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=PAGE_TIMEOUT,
                    env=env,
                )
                log.info("pdf2zh_next pagina %d: rc=%d", page, r.returncode)
                if r.returncode != 0:
                    raise RuntimeError(
                        f"pdf2zh_next exit {r.returncode}: {r.stderr[-400:]}"
                    )
                monos = glob.glob(str(out_dir / "*.mono.pdf"))
                if not monos:
                    raise RuntimeError("pdf2zh_next non ha prodotto il file mono")
                shutil.move(monos[0], str(out))
                self._status[key] = "done"
                return out
            except Exception as e:  # noqa: BLE001 — riportato in UI
                log.exception("traduzione fallita pagina %d (%s)", page, engine)
                self._status[key] = f"error:{e}"
                return None
            finally:
                shutil.rmtree(out_dir, ignore_errors=True)

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
