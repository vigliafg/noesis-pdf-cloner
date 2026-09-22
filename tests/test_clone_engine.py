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
import threading
import time
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

# pdf2zh_next fittizio che "appende": serve a verificare che il cancel termini
# davvero il subprocess invece di attendere la fine della pagina.
_FAKE_PDF2ZH_SLOW = """#!/usr/bin/env python3
import os
import sys
import time

time.sleep(60)
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
        self.assertEqual(clone_engine.ENGINES, ("google", "bing", "llm"))

    def test_bing_flags(self):
        flags, name = self.engine._translator_flags("bing")
        self.assertIn("--bing", flags)
        self.assertEqual(name, "bing")

    def test_llm_flags_use_openai_path(self):
        # il motore "llm" usa i flag "OpenAI-compatibili" di pdf2zh
        flags, name = self.engine._translator_flags("llm")
        self.assertIn("--openai", flags)
        self.assertIn("--openai-model", flags)
        self.assertIn(self.engine.llm_model, flags)
        self.assertTrue(name.startswith("llm"))

    def test_openai_alias_is_normalized(self):
        self.assertEqual(clone_engine.normalize_engine("openai"), "llm")
        flags, _ = self.engine._translator_flags("openai")
        self.assertIn("--openai", flags)

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

    def test_cache_path_includes_version_and_model(self):
        p = self.engine.translated_path_for(1, "google")
        self.assertIn(f"cs{clone_engine.CACHE_SCHEMA_VERSION}-", str(p))
        self.engine.llm_model = "altro/modello-xyz"
        self.assertNotEqual(p, self.engine.translated_path_for(1, "google"))


class CacheQueryTests(unittest.TestCase):
    """cached_pages scans the on-disk cache (cross-session) without side effects."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name) / "cache"
        self.engine = clone_engine.CloneEngine(self.cache)
        self.src = Path(self._tmp.name) / "book.pdf"
        self.src.write_bytes(b"%PDF-1.4 source")
        self.engine.set_document(self.src)
        self.engine.lang_in, self.engine.lang_out = "en", "it"

    def tearDown(self):
        self._tmp.cleanup()

    def _touch(self, page: int, engine: str = "google") -> Path:
        p = self.engine.translated_path(page, engine)
        p.write_bytes(b"%PDF-1.4 fake")
        return p

    def test_translated_path_for_does_not_create_dirs(self):
        p = self.engine.translated_path_for(7, "google")
        self.assertFalse(p.parent.exists())
        self.assertEqual(p.name, "page_000007.pdf")

    def test_cached_pages_returns_present_sorted(self):
        self._touch(2)
        self._touch(0)
        self._touch(5)
        self.assertEqual(self.engine.cached_pages(0, 5, "google"), [0, 2, 5])

    def test_cached_pages_handles_reversed_range(self):
        self._touch(3)
        self.assertEqual(self.engine.cached_pages(4, 1, "google"), [3])

    def test_cached_pages_is_per_engine_and_language(self):
        self._touch(1, engine="bing")
        self.assertEqual(self.engine.cached_pages(0, 3, "google"), [])
        self.assertEqual(self.engine.cached_pages(0, 3, "bing"), [1])
        self.engine.lang_out = "fr"
        self.assertEqual(self.engine.cached_pages(0, 3, "bing"), [])

    def test_cached_pages_survives_new_instance(self):
        self._touch(4)
        other = clone_engine.CloneEngine(self.cache)
        other.set_document(self.src)
        other.lang_in, other.lang_out = "en", "it"
        self.assertEqual(other.cached_pages(0, 9, "google"), [4])

    def test_explicit_languages_override_current(self):
        self._touch(1)  # en-it
        self.assertEqual(
            self.engine.cached_pages(0, 3, "google", "en", "it"), [1]
        )
        self.assertEqual(
            self.engine.cached_pages(0, 3, "google", "en", "fr"), []
        )
        self.assertNotEqual(
            self.engine.translated_path_for(1, "google", "en", "it"),
            self.engine.translated_path_for(1, "google", "en", "fr"),
        )
        self.assertTrue(self.engine.is_cached(1, "google", "en", "it"))
        self.assertFalse(self.engine.is_cached(1, "google", "en", "fr"))


class ExportPdfTests(unittest.TestCase):
    """export_pdf merges the cached single-page clones into one PDF."""

    def setUp(self):
        try:
            import pymupdf  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("pymupdf non disponibile")
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = clone_engine.CloneEngine(Path(self._tmp.name) / "cache")
        self.src = Path(self._tmp.name) / "book.pdf"
        self.src.write_bytes(b"%PDF-1.4 source")
        self.engine.set_document(self.src)
        self.engine.lang_in, self.engine.lang_out = "en", "it"
        self.dest = Path(self._tmp.name) / "out.pdf"

    def tearDown(self):
        self._tmp.cleanup()

    def _page_pdf(self, page: int, text: str) -> Path:
        import pymupdf
        out = self.engine.translated_path(page, "google")
        doc = pymupdf.open()
        pg = doc.new_page()
        pg.insert_text((72, 72), text, fontsize=20)
        doc.save(str(out))
        doc.close()
        return out

    def test_merges_in_the_given_order(self):
        import pymupdf
        self._page_pdf(3, "THREE")
        self._page_pdf(0, "ZERO")
        count = self.engine.export_pdf([0, 3], "google", self.dest)
        self.assertEqual(count, 2)
        with pymupdf.open(str(self.dest)) as doc:
            self.assertEqual(len(doc), 2)
            self.assertIn("ZERO", doc[0].get_text())
            self.assertIn("THREE", doc[1].get_text())

    def test_skips_pages_not_in_cache(self):
        import pymupdf
        self._page_pdf(1, "ONE")
        count = self.engine.export_pdf([0, 1, 2], "google", self.dest)
        self.assertEqual(count, 1)
        with pymupdf.open(str(self.dest)) as doc:
            self.assertEqual(len(doc), 1)

    def test_raises_when_nothing_is_available(self):
        with self.assertRaises(ValueError):
            self.engine.export_pdf([0, 1], "google", self.dest)
        self.assertFalse(self.dest.exists())

    def test_export_pdf_with_explicit_languages(self):
        import pymupdf
        # Clone under a different target language than the engine's current.
        out = self.engine.translated_path(2, "google", "en", "fr")
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), "FRENCH", fontsize=20)
        doc.save(str(out))
        doc.close()
        # Current language (en-it) does not see it…
        with self.assertRaises(ValueError):
            self.engine.export_pdf([2], "google", self.dest)
        # …but the explicit pair does.
        count = self.engine.export_pdf(
            [2], "google", self.dest, "en", "fr"
        )
        self.assertEqual(count, 1)
        with pymupdf.open(str(self.dest)) as merged:
            self.assertIn("FRENCH", merged[0].get_text())

    def test_export_zip_collects_single_pages(self):
        import zipfile
        self._page_pdf(0, "ZERO")
        self._page_pdf(1, "ONE")
        dest = Path(self._tmp.name) / "out.zip"
        count = self.engine.export_zip([0, 1], "google", dest, stem="book")
        self.assertEqual(count, 2)
        with zipfile.ZipFile(dest) as archive:
            self.assertEqual(
                sorted(archive.namelist()),
                ["book_p0001.pdf", "book_p0002.pdf"],
            )

    def test_export_zip_skips_missing_and_raises_when_empty(self):
        dest = Path(self._tmp.name) / "empty.zip"
        with self.assertRaises(ValueError):
            self.engine.export_zip([0, 1], "google", dest)
        self.assertFalse(dest.exists())


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

    def test_llm_without_key_reports_error(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            out = self.engine.translate_page(1, "llm")
        self.assertIsNone(out)
        self.assertTrue(self.engine.status(1, "llm").startswith("error:"))

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


class CancelTests(unittest.TestCase):
    """Il cancel deve terminare il subprocess, non attendere la pagina."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.fake = tmp / "pdf2zh_next"
        self.fake.write_text(_FAKE_PDF2ZH_SLOW)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
        self.engine = clone_engine.CloneEngine(tmp / "cache", pdf2zh_bin=self.fake)
        self.src = tmp / "book.pdf"
        self.src.write_bytes(b"%PDF-1.4 source")
        self.engine.set_document(self.src)
        self.engine.lang_in, self.engine.lang_out = "en", "it"
        split = self.engine.split_path(1)
        split.parent.mkdir(parents=True, exist_ok=True)
        split.write_bytes(b"%PDF-1.4 page 1")

    def tearDown(self):
        self._tmp.cleanup()

    def test_cancel_interrupts_running_subprocess(self):
        cancel = threading.Event()
        timer = threading.Timer(0.5, cancel.set)
        timer.start()
        try:
            t0 = time.perf_counter()
            out = self.engine.translate_page(1, "bing", cancel)
            elapsed = time.perf_counter() - t0
        finally:
            timer.cancel()
        self.assertIsNone(out)
        self.assertEqual(self.engine.status(1, "bing"), "cancelled")
        self.assertLess(elapsed, 15)  # il subprocess fittizio dorme 60 s
        self.assertFalse(self.engine.is_cached(1, "bing"))

    def test_pre_set_cancel_does_not_start_subprocess(self):
        cancel = threading.Event()
        cancel.set()
        with mock.patch("clone_engine.subprocess.Popen") as popen:
            out = self.engine.translate_page(1, "bing", cancel)
            popen.assert_not_called()
        self.assertIsNone(out)
        self.assertEqual(self.engine.status(1, "bing"), "cancelled")


class AtomicWriteTests(unittest.TestCase):
    """Le scritture in cache devono essere atomiche (nessun file parziale)."""

    def setUp(self):
        try:
            import pymupdf  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("pymupdf non disponibile")
        import pymupdf

        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.src = tmp / "book.pdf"
        doc = pymupdf.open()
        doc.new_page().insert_text((72, 72), "pagina di prova")
        doc.save(str(self.src))
        doc.close()
        self.engine = clone_engine.CloneEngine(tmp / "cache")
        self.engine.set_document(self.src)

    def tearDown(self):
        self._tmp.cleanup()

    def test_ensure_split_leaves_no_tmp_file(self):
        path = self.engine.ensure_split(0)
        self.assertIsNotNone(path)
        self.assertTrue(path.is_file())
        self.assertEqual(list(path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
