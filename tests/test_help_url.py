"""Tests for the online-help URL used by the ❓ Guida toolbar button.

La guida è pubblicata su GitHub Pages come sito statico del progetto:
landing alla radice, guida multilingue in ``/help/<lingua>/``. Il pulsante
deve aprire la guida nella lingua dell'interfaccia, con fallback all'italiano.
Gira headless (QT_QPA_PLATFORM=offscreen) e viene saltato se PyQt6 manca.
"""

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from PyQt6.QtWidgets import QApplication
    _HAS_QT = True
except ImportError:  # pragma: no cover
    _HAS_QT = False

import i18n  # noqa: E402


class HelpSiteTests(unittest.TestCase):
    """Il sito del progetto esiste ed è brandizzato Noesis PDF Cloner."""

    def test_landing_and_help_pages_exist(self):
        docs = os.path.join(_ROOT, "docs")
        self.assertTrue(os.path.isfile(os.path.join(docs, "index.html")))
        for code in ("it", "en", "fr", "de", "es"):
            page = os.path.join(docs, "help", code, "index.html")
            self.assertTrue(os.path.isfile(page), page)

    def test_help_is_branded_for_the_cloner(self):
        """Nessun residuo del progetto padre nella guida pubblicata."""
        docs = os.path.join(_ROOT, "docs")
        pages = [os.path.join(docs, "index.html")]
        for code in ("it", "en", "fr", "de", "es"):
            pages.append(os.path.join(docs, "help", code, "index.html"))
        for page in pages:
            with open(page, encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn("Noesis PDF Reader", text, page)
            self.assertNotIn("PDFReaderLite", text, page)

    def test_help_explains_free_page_field(self):
        """Ogni guida spiega il campo libero con le miniature schematiche."""
        docs = os.path.join(_ROOT, "docs")
        for code in ("it", "en", "fr", "de", "es"):
            page = os.path.join(docs, "help", code, "index.html")
            with open(page, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn('id="export-pages"', text, page)
            self.assertIn("pagemock", text, page)
            self.assertIn("1,3,7-9", text, page)

    def test_help_explains_page_actions_fab(self):
        """Ogni guida descrive il pulsante flottante delle azioni pagina."""
        docs = os.path.join(_ROOT, "docs")
        for code in ("it", "en", "fr", "de", "es"):
            page = os.path.join(docs, "help", code, "index.html")
            with open(page, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn('id="page-actions"', text, page)
            self.assertIn("fabmock", text, page)


@unittest.skipUnless(_HAS_QT, "PyQt6 non disponibile")
class HelpUrlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])
        import main  # noqa: E402
        cls.main = main

    def test_base_url_is_project_pages_help(self):
        """L'URL base punta al sito del progetto (non a reader-lite)."""
        self.assertEqual(
            self.main.HELP_URL,
            "https://vigliafg.github.io/noesis-pdf-cloner/help/",
        )
        self.assertNotIn("reader-lite", self.main.HELP_URL)

    def test_help_url_uses_explicit_language(self):
        for code in ("it", "en", "fr", "de", "es"):
            self.assertEqual(
                self.main.help_url(code),
                f"https://vigliafg.github.io/noesis-pdf-cloner/help/{code}/",
            )

    def test_help_url_falls_back_to_italian(self):
        self.assertTrue(self.main.help_url("xx").endswith("/help/it/"))

    def test_help_url_follows_active_language(self):
        old = i18n.get_language()
        try:
            i18n.set_language("fr")
            self.assertTrue(self.main.help_url().endswith("/help/fr/"))
        finally:
            i18n.set_language(old)


if __name__ == "__main__":
    unittest.main()
