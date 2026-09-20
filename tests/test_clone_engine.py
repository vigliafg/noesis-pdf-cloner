"""Tests for the clone engine (clone_engine.py).

Covered without requiring pdf2zh_next or PyQt:
- binary auto-detection / venv python resolution;
- per-engine CLI flags;
- per-document / per-engine / per-language cache paths;
- the full split -> subprocess -> cache pipeline using a fake pdf2zh script;
- graceful errors (missing API key, missing engine).
"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import clone_engine  # noqa: E402

_FAKE_PDF2ZH = """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
out = None
for i, a in enumerate(args):
    if a == "--output":
        out = args[i + 1]
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "page.mono.pdf"), "wb") as f:
    f.write(b"%PDF-1.4 fake translated")
"""


class FindBinaryTests(unittest.TestCase):
    def test_override_is_used_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "pdf2zh_next"
            fake.write_text("#!/bin/sh\n")
            self.assertEqual(clone_engine.find_pdf2zh_bin(fake), fake)

    def test_missing_override_falls_back_to_none(self):
        # With no override, env and .venv2 candidates may or may not exist;
        # the function must never raise.
        result = clone_engine.find_pdf2zh_bin("/nonexistent/pdf2zh_next")
        self.assertTrue(result is None or result.is_file())

    def test_venv_python_for_falls_back_to_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "bin" / "pdf2zh_next"
            fake.parent.mkdir(parents=True)
            fake.write_text("#!/bin/sh\n")
            self.assertEqual(clone_engine.venv_python_for(fake), sys.executable)

    def test_venv_python_prefers_sibling_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir(parents=True)
            fake = bin_dir / "pdf2zh_next"
            fake.write_text("#!/bin/sh\n")
            py = bin_dir / "python"
            py.write_text("#!/bin/sh\n")
            self.assertEqual(clone_engine.venv_python_for(fake), str(py))


class FlagsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = clone_engine.CloneEngine(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_engines_tuple(self):
        self.assertEqual(clone_engine.ENGINES, ("google", "bing", "openai"))

    def test_bing_flags(self):
        flags, name = self.engine._translator_flags("bing")
        self.assertIn("--bing", flags)
        self.assertEqual(name, "bing")

    def test_openai_flags(self):
        flags, name = self.engine._translator_flags("openai")
        self.assertIn("--openai", flags)
        self.assertIn("--openai-model", flags)
        self.assertIn(self.engine.llm_model, flags)
        self.assertTrue(name.startswith("llm"))

    def test_google_flags_use_clitranslator(self):
        flags, name = self.engine._translator_flags("google")
        self.assertIn("--clitranslator", flags)
        self.assertIn("--clitranslator-command", flags)
        self.assertIn("gtranslate_cli.py", " ".join(flags))
        self.assertIn("google", name)


class CachePathTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = clone_engine.CloneEngine(Path(self._tmp.name))
        self.src = Path(self._tmp.name) / "book.pdf"
        self.src.write_bytes(b"%PDF-1.4 source")
        self.engine.set_document(self.src)

    def tearDown(self):
        self._tmp.cleanup()

    def test_paths_are_per_document_engine_and_language(self):
        self.engine.lang_in, self.engine.lang_out = "en", "it"
        p1 = self.engine.translated_path(3, "google")
        p2 = self.engine.translated_path(3, "bing")
        self.engine.lang_out = "fr"
        p3 = self.engine.translated_path(3, "google")
        self.assertNotEqual(p1, p2)   # engine
        self.assertNotEqual(p1, p3)   # target language
        self.assertIn("book_", str(p1))  # document key
        self.assertEqual(p1.name, "page_000003.pdf")

    def test_is_cached_false_initially(self):
        self.assertFalse(self.engine.is_cached(1, "google"))

    def test_set_document_none_resets(self):
        self.engine.set_document(None)
        self.assertEqual(self.engine._doc_key, "")
        self.assertIsNone(self.engine._src_pdf)


class SplitIndexTests(unittest.TestCase):
    """ensure_split must use the same 0-based index as the rest of the app."""

    def _make_pdf(self, path: Path, n: int = 3) -> None:
        try:
            import pymupdf
        except ImportError:  # pragma: no cover
            self.skipTest("pymupdf non disponibile")
        doc = pymupdf.open()
        for i in range(n):
            pg = doc.new_page()
            pg.insert_text((72, 72), f"PAGE{i}", fontsize=20)
        doc.save(str(path))
        doc.close()

    def test_ensure_split_extracts_the_same_zero_based_page(self):
        try:
            import pymupdf
        except ImportError:  # pragma: no cover
            self.skipTest("pymupdf non disponibile")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "book.pdf"
            self._make_pdf(src, 3)
            eng = clone_engine.CloneEngine(Path(tmp) / "cache")
            eng.set_document(src)
            sp = eng.ensure_split(1)          # pagina 2 (0-based)
            self.assertIsNotNone(sp)
            d = pymupdf.open(str(sp))
            text = d[0].get_text()
            d.close()
            self.assertIn("PAGE1", text)
            self.assertNotIn("PAGE0", text)
            self.assertNotIn("PAGE2", text)


class PipelineTests(unittest.TestCase):
    """Full pipeline with a fake pdf2zh_next (no real translation)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.fake = tmp / "pdf2zh_next"
        self.fake.write_text(_FAKE_PDF2ZH)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)

        self.engine = clone_engine.CloneEngine(tmp / "cache", pdf2zh_bin=self.fake)
        self.src = tmp / "book.pdf"
        self.src.write_bytes(b"%PDF-1.4 source")
        self.engine.set_document(self.src)
        self.engine.lang_in, self.engine.lang_out = "en", "it"
        # Pre-create the split so ensure_split does not need pymupdf.
        split = self.engine.split_path(1)
        split.parent.mkdir(parents=True, exist_ok=True)
        split.write_bytes(b"%PDF-1.4 page 1")

    def tearDown(self):
        self._tmp.cleanup()

    def test_translate_page_produces_cache(self):
        out = self.engine.translate_page(1, "google")
        self.assertIsNotNone(out)
        self.assertTrue(out.is_file())
        self.assertTrue(self.engine.is_cached(1, "google"))
        self.assertEqual(self.engine.status(1, "google"), "done")

    def test_second_call_is_cached_and_fast(self):
        self.engine.translate_page(1, "bing")
        with mock.patch("clone_engine.subprocess.run") as run:
            again = self.engine.translate_page(1, "bing")
            run.assert_not_called()
        self.assertTrue(again.is_file())

    def test_openai_without_key_reports_error(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            out = self.engine.translate_page(1, "openai")
        self.assertIsNone(out)
        self.assertTrue(self.engine.status(1, "openai").startswith("error:"))

    def test_missing_engine_reports_error(self):
        engine = clone_engine.CloneEngine(
            Path(self._tmp.name) / "cache2", pdf2zh_bin="/nonexistent/pdf2zh"
        )
        engine.set_document(self.src)
        split = engine.split_path(1)
        split.parent.mkdir(parents=True, exist_ok=True)
        split.write_bytes(b"%PDF-1.4 page 1")
        with mock.patch(
            "clone_engine.find_pdf2zh_bin", return_value=None
        ):
            out = engine.translate_page(1, "bing")
        self.assertIsNone(out)
        self.assertIn("pdf2zh_next non trovato", engine.status(1, "bing"))


if __name__ == "__main__":
    unittest.main()
