"""Test del modulo OCR delle scansioni (``ocr_scans``).

Nessuna rete e nessun Tesseract: il provider è iniettato e le righe OCR
passano direttamente a ``build_text_layer``.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pymupdf  # noqa: E402

import ocr_scans  # noqa: E402


def _blank_pdf(path: Path, text_lines: list[str] | None = None) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=400)
    for index, line in enumerate(text_lines or []):
        page.insert_text((40, 60 + index * 20), line, fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def _lines() -> list[ocr_scans.OcrLine]:
    return [
        ocr_scans.OcrLine((40.0, 45.0, 360.0, 60.0), "the quick brwn fox", 11.0),
        ocr_scans.OcrLine((40.0, 65.0, 360.0, 80.0), "jumps over the lazy dog", 11.0),
    ]


class TextCleanupTests(unittest.TestCase):
    def test_plain_text_strips_markdown_and_images(self):
        md = (
            "<file name=\"page.pdf\">\n## Titolo\n\n**grassetto** e "
            "![img](img-0.jpeg)\n\n| a | b |\n| - | - |\n| 1 | 2 |\n"
        )
        text = ocr_scans.plain_text(md)
        self.assertIn("Titolo", text)
        self.assertIn("grassetto", text)
        self.assertNotIn("#", text)
        self.assertNotIn("**", text)
        self.assertNotIn("img-0", text)
        self.assertNotIn("file name", text)
        self.assertNotIn("|", text)

    def test_tesseract_lang(self):
        self.assertEqual(ocr_scans.tesseract_lang("it"), "ita")
        self.assertEqual(ocr_scans.tesseract_lang("en"), "eng")
        self.assertIn("eng", ocr_scans.tesseract_lang("auto"))
        self.assertEqual(ocr_scans.tesseract_lang("xx"), "eng")


class MatchLineTests(unittest.TestCase):
    def test_line_with_ocr_typo_is_replaced_from_mistral(self):
        mistral = "The quick brown fox jumps over the lazy dog."
        tokens = ocr_scans._tokenize(mistral)
        index = ocr_scans._word_index(tokens)
        match = ocr_scans._match_line("the quick brwn fox", tokens, index)
        self.assertEqual(match, "the quick brown fox")

    def test_garbage_line_is_not_replaced(self):
        tokens = ocr_scans._tokenize("The quick brown fox jumps over the lazy dog.")
        index = ocr_scans._word_index(tokens)
        self.assertIsNone(
            ocr_scans._match_line("zzz qqq xxx", tokens, index)
        )

    def test_short_line_is_ignored(self):
        tokens = ocr_scans._tokenize("hello world")
        self.assertIsNone(ocr_scans._match_line("hello", tokens, {} ))


class AnnotationsTests(unittest.TestCase):
    def test_success_annotation(self):
        payload = {"choices": [{"message": {"annotations": [{
            "type": "file",
            "file": {"content": [
                {"type": "text", "text": "<file name=\"page.pdf\">"},
                {"type": "text", "text": "Ciao mondo"},
            ]},
        }]}}]}
        self.assertEqual(ocr_scans._annotations_text(payload), "<file name=\"page.pdf\">Ciao mondo")

    def test_error_annotation_is_reused(self):
        """Il testo OCR arriva anche quando il modello fallisce (429)."""
        payload = {"error": {"metadata": {"file_annotations": [{
            "type": "file", "file": {"content": [
                {"type": "text", "text": "Testo riconosciuto"},
            ]},
        }]}}}
        self.assertEqual(ocr_scans._annotations_text(payload), "Testo riconosciuto")

    def test_missing_annotations(self):
        self.assertEqual(ocr_scans._annotations_text({"choices": []}), "")


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.pdf = _blank_pdf(Path(self._tmp.name) / "page.pdf")

    def tearDown(self):
        self._tmp.cleanup()

    def test_ocr_pdf_page_reads_annotations(self):
        calls = {}

        def fake_post(url, payload, key, timeout):
            calls["url"] = url
            calls["payload"] = payload
            return 200, {"choices": [{"message": {"annotations": [{
                "file": {"content": [
                    {"type": "text", "text": "Testo OCR"},
                ]},
            }]}}]}

        config = ocr_scans.OcrConfig("segreto")
        text = ocr_scans.ocr_pdf_page(self.pdf, config, post=fake_post)
        self.assertEqual(text, "Testo OCR")
        self.assertIn("/chat/completions", calls["url"])
        self.assertEqual(calls["payload"]["plugins"][0]["pdf"]["engine"], "mistral-ocr")
        body = json.dumps(calls["payload"])
        self.assertNotIn("segreto", body)  # la chiave va solo nell'header

    def test_missing_key(self):
        with self.assertRaises(ocr_scans.OcrError) as ctx:
            ocr_scans.ocr_pdf_page(self.pdf, ocr_scans.OcrConfig(""))
        self.assertEqual(ctx.exception.code, "missing_key")

    def test_http_error_without_annotations(self):
        def fake_post(url, payload, key, timeout):
            return 402, {"error": {"message": "Payment required"}}

        with self.assertRaises(ocr_scans.OcrError) as ctx:
            ocr_scans.ocr_pdf_page(self.pdf, ocr_scans.OcrConfig("k"), post=fake_post)
        self.assertEqual(ctx.exception.code, "no_credits")

    def test_rate_limit_with_annotations_is_success(self):
        """Con 429 il testo OCR c'è comunque: si usa, senza errore."""
        def fake_post(url, payload, key, timeout):
            return 429, {"error": {"metadata": {"file_annotations": [{
                "file": {"content": [{"type": "text", "text": "Testo"}]},
            }]}}}

        text = ocr_scans.ocr_pdf_page(
            self.pdf, ocr_scans.OcrConfig("k"), post=fake_post
        )
        self.assertEqual(text, "Testo")

    def test_config_tag_is_stable_and_versioned(self):
        tag = ocr_scans.OcrConfig("k").tag()
        self.assertTrue(tag.startswith(f"ocr{ocr_scans.OCR_SCHEMA_VERSION}-"))
        self.assertEqual(tag, ocr_scans.OcrConfig("altro").tag())
        other = ocr_scans.OcrConfig("k", model="altro/modello").tag()
        self.assertNotEqual(tag, other)


class LayerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.src = _blank_pdf(Path(self._tmp.name) / "scan.pdf")
        self.dst = Path(self._tmp.name) / "layer.pdf"

    def tearDown(self):
        self._tmp.cleanup()

    def test_text_layer_uses_mistral_text(self):
        markdown = "the quick brown fox\n\njumps over the lazy dog"
        coverage = ocr_scans.build_text_layer(
            self.src, markdown, self.dst, lines=_lines()
        )
        self.assertGreaterEqual(coverage, 0.9)
        with pymupdf.open(str(self.dst)) as doc:
            text = doc[0].get_text()
        self.assertIn("brown fox", text)
        self.assertIn("lazy dog", text)

    def test_dedupe_lines(self):
        duplicated = _lines() + [
            ocr_scans.OcrLine((42.0, 46.0, 358.0, 59.0), "dup", 11.0)
        ]
        self.assertEqual(len(ocr_scans._dedupe_lines(duplicated)), 2)

    def test_native_page_contains_text(self):
        markdown = "## Titolo\n\nUn paragrafo di prova."
        ocr_scans.build_native_page(self.src, markdown, self.dst)
        with pymupdf.open(str(self.dst)) as doc:
            text = doc[0].get_text()
        self.assertIn("Titolo", text)
        self.assertIn("paragrafo", text)

    def test_build_ocr_page_falls_back_to_native(self):
        import ocr_scans as module
        from unittest import mock

        with mock.patch.object(
            module, "ocr_pdf_page", return_value="Testo OCR riconosciuto"
        ), mock.patch.object(
            module,
            "build_text_layer",
            side_effect=module.OcrError("ocr_tesseract", "assente"),
        ):
            method = ocr_scans.build_ocr_page(
                self.src, self.dst, ocr_scans.OcrConfig("k")
            )
        self.assertEqual(method, "native")
        with pymupdf.open(str(self.dst)) as doc:
            self.assertIn("Testo OCR riconosciuto", doc[0].get_text())


if __name__ == "__main__":
    unittest.main()
