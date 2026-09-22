"""Test dell'espansione delle etichette di pagina (/PageLabels).

Allineato a ``noesis-pdf-cloner-service/tests/test_pagelabels.py``.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from pagelabels import build_page_labels, format_number  # noqa: E402


class FormatNumberTests(unittest.TestCase):
    def test_styles(self):
        self.assertEqual(format_number(3, "D"), "3")
        self.assertEqual(format_number(4, "R"), "IV")
        self.assertEqual(format_number(4, "r"), "iv")
        self.assertEqual(format_number(1, "A"), "A")
        self.assertEqual(format_number(27, "a"), "aa")
        self.assertEqual(format_number(5, ""), "")


class BuildPageLabelsTests(unittest.TestCase):
    def test_fallback_to_physical_numbers(self):
        self.assertEqual(build_page_labels([], 3), ["1", "2", "3"])
        self.assertEqual(build_page_labels(None, 2), ["1", "2"])

    def test_roman_then_decimal(self):
        spec = [
            {"startpage": 0, "style": "r", "firstpagenum": 1},
            {"startpage": 3, "style": "D", "firstpagenum": 1},
        ]
        self.assertEqual(
            build_page_labels(spec, 5), ["i", "ii", "iii", "1", "2"]
        )

    def test_prefix_and_no_style(self):
        spec = [
            {"startpage": 0, "style": "", "prefix": "Cop"},
            {"startpage": 1, "style": "D", "prefix": "A-", "firstpagenum": 1},
        ]
        self.assertEqual(build_page_labels(spec, 3), ["Cop", "A-1", "A-2"])

    def test_missing_style_means_prefix_only(self):
        """Regressione (pa19): senza ``style`` l'etichetta è solo il prefisso."""
        spec = [
            {"startpage": 0, "prefix": "Cover", "firstpagenum": 1},
            {"startpage": 1, "prefix": "i", "firstpagenum": 1},
            {"startpage": 2, "prefix": "70", "firstpagenum": 1},
            {"startpage": 3, "prefix": "71", "firstpagenum": 1},
        ]
        self.assertEqual(
            build_page_labels(spec, 4), ["Cover", "i", "70", "71"]
        )

    def test_uncovered_pages_use_physical(self):
        spec = [{"startpage": 2, "style": "D", "firstpagenum": 10}]
        self.assertEqual(build_page_labels(spec, 4), ["1", "2", "10", "11"])


class EnginePageLabelsTests(unittest.TestCase):
    """``CloneEngine.page_labels`` legge gli intervalli dal PDF (o fallback)."""

    def test_reads_labels_from_pdf(self):
        try:
            import pymupdf
        except ImportError:  # pragma: no cover
            self.skipTest("pymupdf non disponibile")
        import clone_engine

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lab.pdf"
            doc = pymupdf.open()
            for _ in range(4):
                doc.new_page()
            try:
                doc.set_page_labels(
                    [
                        {"startpage": 0, "prefix": "R", "style": "r",
                         "firstpagenum": 1},
                        {"startpage": 2, "prefix": "", "style": "D",
                         "firstpagenum": 1},
                    ]
                )
            except Exception:  # pragma: no cover — versione senza supporto
                self.skipTest("set_page_labels non disponibile")
            doc.save(str(path))
            doc.close()
            labels = clone_engine.CloneEngine.page_labels(path)
            self.assertEqual(labels, ["Ri", "Rii", "1", "2"])

    def test_fallback_without_spec(self):
        try:
            import pymupdf
        except ImportError:  # pragma: no cover
            self.skipTest("pymupdf non disponibile")
        import clone_engine

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.pdf"
            doc = pymupdf.open()
            doc.new_page()
            doc.new_page()
            doc.save(str(path))
            doc.close()
            self.assertEqual(
                clone_engine.CloneEngine.page_labels(path), ["1", "2"]
            )


if __name__ == "__main__":
    unittest.main()
