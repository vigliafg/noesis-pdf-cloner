"""Real-corpus export tests: single page 156 and range 156-159.

Le pagine sono **1-based** come nella UI; nel motore diventano 0-based
(156 -> 155, 159 -> 158). I test usano la cache reale dell'app
(``~/.local/share/noesis-pdf-cloner/clones``): le pagine tradotte in una
sessione precedente sono quindi già disponibili (cache cross-sessione).

- ``RealExportTests`` legge solo la cache e viene saltato per le pagine non
  ancora tradotte (convenzione degli altri test di regressione su PDF reali).
- ``RealTranslateThenExportTests`` traduce davvero le pagine mancanti con
  pdf2zh_next: è un test E2E, quindi gira solo con ``NOESIS_E2E=1``.

Gira headless (QT_QPA_PLATFORM=offscreen); salta tutto se PyQt6, PyMuPDF o
``ha22.pdf`` mancano.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import clone_engine  # noqa: E402

try:
    import pymupdf  # noqa: E402,F401
    _HAS_PYMUPDF = True
except ImportError:  # pragma: no cover
    _HAS_PYMUPDF = False

try:
    from PyQt6.QtCore import QCoreApplication, QStandardPaths
    _HAS_QT = True
except ImportError:  # pragma: no cover
    _HAS_QT = False

HA22 = Path(_ROOT) / "ha22.pdf"
APP_NAME = "noesis-pdf-cloner"
ENGINE = "google"
LANG_IN, LANG_OUT = "auto", "it"

# pagina 156 (1-based) e intervallo 156-159 (1-based)
PAGE_156 = 155
RANGE_156_159 = [155, 156, 157, 158]

# intervallo 510-520 (1-based) -> indici 0-based 509..519
RANGE_510_520 = list(range(509, 520))

_E2E = os.environ.get("NOESIS_E2E") == "1"


def _cache_root() -> Path:
    QCoreApplication.setApplicationName(APP_NAME)
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    )
    return Path(base) / "clones"


def _make_engine() -> clone_engine.CloneEngine:
    eng = clone_engine.CloneEngine(_cache_root())
    eng.set_document(HA22)
    eng.lang_in, eng.lang_out = LANG_IN, LANG_OUT
    return eng


@unittest.skipUnless(_HAS_QT, "PyQt6 non disponibile")
@unittest.skipUnless(_HAS_PYMUPDF, "PyMuPDF non disponibile")
@unittest.skipUnless(HA22.exists(), f"{HA22.name} non presente")
class RealExportTests(unittest.TestCase):
    """Esporta le pagine reali già in cache (cross-sessione)."""

    @classmethod
    def setUpClass(cls):
        cls.engine = _make_engine()

    def _export(self, pages, dest):
        count = self.engine.export_pdf(pages, ENGINE, dest)
        self.assertEqual(count, len(pages))
        with pymupdf.open(str(dest)) as doc:
            self.assertEqual(len(doc), len(pages))
            for i in range(len(pages)):
                self.assertTrue(doc[i].get_text().strip())
        return count

    def test_range_156_159_cached_pages_detection(self):
        cached = self.engine.cached_pages(
            RANGE_156_159[0], RANGE_156_159[-1], ENGINE
        )
        # Sottoinsieme atteso: ogni pagina rilevata è davvero un file cache.
        for page in cached:
            self.assertTrue(self.engine.is_cached(page, ENGINE))
        if cached != RANGE_156_159:
            self.skipTest(
                f"intervallo 156-159 non tutto in cache: {cached}"
            )

    def test_single_page_156_export(self):
        if not self.engine.is_cached(PAGE_156, ENGINE):
            self.skipTest("pagina 156 non in cache")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "page156.pdf"
            self._export([PAGE_156], dest)

    def test_range_156_159_export(self):
        if not all(self.engine.is_cached(p, ENGINE) for p in RANGE_156_159):
            self.skipTest("intervallo 156-159 non tutto in cache")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "range_156_159.pdf"
            self._export(RANGE_156_159, dest)

    def test_range_510_520_cached_pages_detection(self):
        cached = self.engine.cached_pages(
            RANGE_510_520[0], RANGE_510_520[-1], ENGINE
        )
        for page in cached:
            self.assertTrue(self.engine.is_cached(page, ENGINE))
        if cached != RANGE_510_520:
            self.skipTest(f"intervallo 510-520 non tutto in cache: {cached}")

    def test_range_510_520_export(self):
        if not all(self.engine.is_cached(p, ENGINE) for p in RANGE_510_520):
            self.skipTest("intervallo 510-520 non tutto in cache")
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "range_510_520.pdf"
            self._export(RANGE_510_520, dest)


@unittest.skipUnless(_HAS_QT, "PyQt6 non disponibile")
@unittest.skipUnless(_HAS_PYMUPDF, "PyMuPDF non disponibile")
@unittest.skipUnless(HA22.exists(), f"{HA22.name} non presente")
@unittest.skipUnless(_E2E, "test E2E: impostare NOESIS_E2E=1")
class RealTranslateThenExportTests(unittest.TestCase):
    """E2E: traduce le pagine mancanti di un intervallo e poi le esporta."""

    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])
        import main

        cls.main = main
        cls.engine = _make_engine()

    def _translate_and_export(self, pages: list[int], label: str):
        if not self.engine.available():
            self.skipTest("pdf2zh_next non trovato")
        missing = [p for p in pages if not self.engine.is_cached(p, ENGINE)]
        if not missing:
            self.skipTest(f"intervallo {label} già tutto in cache")

        thread = self.main.CloneExportThread(self.engine, missing, ENGINE)
        outcome: list = []
        errors: list = []
        thread.batch_finished.connect(lambda *a: outcome.append(a))
        thread.page_error.connect(lambda p, m: errors.append((p, m)))
        thread.run()  # sincrono: siamo in un test, non serve un event loop

        self.assertEqual(errors, [], f"pagine fallite: {errors}")
        done, failed, total = outcome[0]
        self.assertEqual(failed, 0)
        self.assertEqual(done, total)
        self.assertEqual(
            self.engine.cached_pages(pages[0], pages[-1], ENGINE), pages
        )

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / f"range_{label}.pdf"
            count = self.engine.export_pdf(pages, ENGINE, dest)
            self.assertEqual(count, len(pages))
            with pymupdf.open(str(dest)) as doc:
                self.assertEqual(len(doc), len(pages))

    def test_translate_missing_then_export_range_156_159(self):
        self._translate_and_export(RANGE_156_159, "156_159")

    def test_translate_missing_then_export_range_510_520(self):
        self._translate_and_export(RANGE_510_520, "510_520")


if __name__ == "__main__":
    unittest.main()
