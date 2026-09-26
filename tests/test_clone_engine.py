"""Tests for the clone engine (clone_engine.py).

Covered without requiring pdf2zh_next or PyQt:
- binary auto-detection / venv python resolution;
- per-engine CLI flags;
- per-document / per-engine / per-language cache paths;
- the full split -> subprocess -> cache pipeline using a fake pdf2zh script;
- graceful errors (missing API key, missing engine).
"""

import os
import shlex
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

    def test_llm_flags_do_not_expose_api_key(self):
        # La chiave non deve finire in argv (leak via /proc/<pid>/cmdline):
        # viene passata solo via ambiente (PDF2ZH_OPENAI_API_KEY).
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "segreta"}):
            flags, _ = self.engine._translator_flags("llm")
        self.assertNotIn("--openai-api-key", flags)
        self.assertTrue(all("segreta" not in str(f) for f in flags))

    def test_openai_alias_is_normalized(self):
        self.assertEqual(clone_engine.normalize_engine("openai"), "llm")
        flags, _ = self.engine._translator_flags("openai")
        self.assertIn("--openai", flags)

    def test_llm_flags_skip_auto_glossary(self):
        # Punto 6: un giro LLM in meno per pagina.
        flags, _ = self.engine._translator_flags("llm")
        self.assertIn("--no-auto-extract-glossary", flags)
        self.assertIn("--openai-timeout", flags)

    def test_llm_flags_add_pool_workers(self):
        self.engine.llm_pool_workers = 6
        flags, _ = self.engine._translator_flags("llm")
        self.assertIn("--pool-max-workers", flags)
        self.assertEqual(flags[flags.index("--pool-max-workers") + 1], "6")

    def test_llm_flags_without_pool_workers(self):
        self.engine.llm_pool_workers = 1
        flags, _ = self.engine._translator_flags("llm")
        self.assertNotIn("--pool-max-workers", flags)

    def test_google_flags_do_not_get_llm_workers(self):
        self.engine.llm_pool_workers = 8
        flags, _ = self.engine._translator_flags("google")
        self.assertNotIn("--pool-max-workers", flags)

    def test_google_flags_use_clitranslator(self):
        flags, name = self.engine._translator_flags("google")
        self.assertIn("--clitranslator", flags)
        self.assertIn("--clitranslator-command", flags)
        self.assertIn("gtranslate_cli.py", " ".join(flags))
        self.assertIn("google", name)

    def test_google_clitranslator_command_roundtrips_windows_paths(self):
        """Il comando è interpretato da pdf2zh con shlex.split: deve reggere i
        percorsi Windows con backslash e spazi (bug delle finestre 'mono')."""
        win_py = r"C:\Users\mario rossi\.venv2\Scripts\python.exe"
        win_cli = r"C:\Users\mario rossi\AppData\Local\Temp\_MEI1\gtranslate_cli.py"
        with mock.patch.object(
            clone_engine, "venv_python_for", return_value=win_py
        ), mock.patch.object(
            clone_engine, "_gtranslate_cli_path", return_value=win_cli
        ):
            flags, _ = self.engine._translator_flags("google")
        command = flags[flags.index("--clitranslator-command") + 1]
        self.assertEqual(shlex.split(command), [win_py, win_cli])


class FastEngineTests(unittest.TestCase):
    """Feature "motore veloce" (sperimentale, default OFF, reversibile)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.pdf2zh = tmp / "pdf2zh_next"
        self.pdf2zh.write_text("#!/bin/sh\n")
        self.engine = clone_engine.CloneEngine(
            tmp / "cache", pdf2zh_bin=self.pdf2zh
        )

    def tearDown(self):
        os.environ.pop("NOESIS_FAST_ENGINE", None)
        os.environ.pop("NOESIS_FAST_FLAGS", None)
        self._tmp.cleanup()

    def test_default_off_uses_binary(self):
        self.assertFalse(self.engine._fast_engine_active())
        self.assertEqual(
            self.engine._engine_launch_prefix(self.pdf2zh), [str(self.pdf2zh)]
        )

    def test_env_killswitch_overrides_setting(self):
        self.engine.fast_engine = True
        os.environ["NOESIS_FAST_ENGINE"] = "0"
        self.assertFalse(self.engine._fast_engine_active())
        self.assertEqual(
            self.engine._engine_launch_prefix(self.pdf2zh), [str(self.pdf2zh)]
        )

    def test_env_enables_without_setting(self):
        os.environ["NOESIS_FAST_ENGINE"] = "1"
        self.assertTrue(self.engine._fast_engine_active())

    def test_wrapper_used_when_enabled_and_present(self):
        self.engine.fast_engine = True
        wrapper = Path(self._tmp.name) / "engine_wrapper.py"
        wrapper.write_text("")
        with mock.patch.object(
            clone_engine, "_engine_wrapper_path", return_value=wrapper
        ), mock.patch.object(
            clone_engine, "venv_python_for", return_value="/venv/python"
        ):
            prefix = self.engine._engine_launch_prefix(self.pdf2zh)
        self.assertEqual(prefix, ["/venv/python", str(wrapper)])

    def test_missing_wrapper_falls_back_to_binary(self):
        self.engine.fast_engine = True
        missing = Path(self._tmp.name) / "missing_wrapper.py"
        with mock.patch.object(
            clone_engine, "_engine_wrapper_path", return_value=missing
        ):
            prefix = self.engine._engine_launch_prefix(self.pdf2zh)
        self.assertEqual(prefix, [str(self.pdf2zh)])

    def test_pool_workers_explicit_only_when_fast(self):
        self.engine.llm_pool_workers = 1
        flags, _ = self.engine._translator_flags("llm")
        self.assertNotIn("--pool-max-workers", flags)  # comportamento storico
        self.engine.fast_engine = True
        flags, _ = self.engine._translator_flags("llm")
        self.assertEqual(flags[flags.index("--pool-max-workers") + 1], "1")
        self.assertEqual(flags[flags.index("--qps") + 1], "1")

    def test_quality_flags_gated_by_fast_flags(self):
        self.engine.fast_engine = True
        self.engine.fast_flags = False
        self.assertEqual(self.engine._quality_flags(self.pdf2zh), [])
        self.engine.fast_flags = True
        with mock.patch.object(clone_engine, "page_has_text", return_value=True):
            flags = self.engine._quality_flags(self.pdf2zh)
        self.assertIn("--skip-scanned-detection", flags)
        self.assertIn("--skip-formula-offset-calculation", flags)

    def test_skip_scanned_omitted_for_textless_page(self):
        self.engine.fast_engine = True
        self.engine.fast_flags = True
        with mock.patch.object(clone_engine, "page_has_text", return_value=False):
            flags = self.engine._quality_flags(self.pdf2zh)
        self.assertNotIn("--skip-scanned-detection", flags)

    def test_fast_flags_env_killswitch(self):
        self.engine.fast_engine = True
        self.engine.fast_flags = True
        os.environ["NOESIS_FAST_FLAGS"] = "0"
        self.assertEqual(self.engine._quality_flags(self.pdf2zh), [])

    def test_version_tag_marker(self):
        tag_off = self.engine._version_tag()
        self.assertNotIn(clone_engine.FAST_ENGINE_TAG, tag_off)
        self.engine.fast_engine = True
        tag_on = self.engine._version_tag()
        self.assertIn(clone_engine.FAST_ENGINE_TAG, tag_on)
        self.assertNotEqual(tag_off, tag_on)

    def test_translate_page_uses_wrapper_and_quality_flags(self):
        import subprocess

        tmp = Path(self._tmp.name)
        src = tmp / "book.pdf"
        src.write_bytes(b"%PDF-1.4 source")
        self.engine.set_document(src)
        self.engine.lang_in, self.engine.lang_out = "en", "it"
        split = self.engine.split_path(1)
        split.parent.mkdir(parents=True, exist_ok=True)
        split.write_bytes(b"%PDF-1.4 page 1")
        self.engine.fast_engine = True
        self.engine.fast_flags = True
        wrapper = tmp / "engine_wrapper.py"
        wrapper.write_text("")
        captured: dict = {}

        def fake_run(cmd, env, cancel_event=None):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(
            clone_engine, "_engine_wrapper_path", return_value=wrapper
        ), mock.patch.object(
            clone_engine, "venv_python_for", return_value="/venv/python"
        ), mock.patch.object(
            clone_engine, "page_has_text", return_value=True
        ), mock.patch.object(self.engine, "_run_engine", fake_run):
            self.engine.translate_page(1, "google")

        cmd = captured["cmd"]
        self.assertEqual(cmd[:2], ["/venv/python", str(wrapper)])
        self.assertIn("--skip-scanned-detection", cmd)

    def test_llm_reasoning_and_json_flags_only_when_fast(self):
        self.engine.llm_reasoning_effort = "minimal"
        self.engine.llm_json_mode = True
        flags, _ = self.engine._translator_flags("llm")
        self.assertNotIn("--openai-reasoning-effort", flags)  # feature OFF
        self.assertNotIn("--openai-enable-json-mode", flags)
        self.engine.fast_engine = True
        flags, _ = self.engine._translator_flags("llm")
        self.assertEqual(
            flags[flags.index("--openai-reasoning-effort") + 1], "minimal"
        )
        self.assertIn("--openai-enable-json-mode", flags)

    def test_ignore_cache_flag_for_benchmark(self):
        import subprocess

        tmp = Path(self._tmp.name)
        src = tmp / "book.pdf"
        src.write_bytes(b"%PDF-1.4 source")
        self.engine.set_document(src)
        self.engine.lang_in, self.engine.lang_out = "en", "it"
        split = self.engine.split_path(1)
        split.parent.mkdir(parents=True, exist_ok=True)
        split.write_bytes(b"%PDF-1.4 page 1")
        self.engine.ignore_cache = True

        captured: dict = {}

        def fake_run(cmd, env, cancel_event=None):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(self.engine, "_run_engine", fake_run):
            self.engine.translate_page(1, "google")
        self.assertIn("--ignore-cache", captured["cmd"])


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

    def test_export_folder_writes_single_pages(self):
        self._page_pdf(0, "ZERO")
        self._page_pdf(2, "TWO")
        dest_dir = Path(self._tmp.name) / "pages_out"
        count = self.engine.export_folder(
            [0, 2], "google", dest_dir, stem="book"
        )
        self.assertEqual(count, 2)
        self.assertEqual(
            sorted(p.name for p in dest_dir.iterdir()),
            ["book_p0001.pdf", "book_p0003.pdf"],
        )

    def test_export_folder_skips_missing_and_raises_when_empty(self):
        dest_dir = Path(self._tmp.name) / "empty_out"
        with self.assertRaises(ValueError):
            self.engine.export_folder([0, 1], "google", dest_dir)
        # la cartella può esistere ma è vuota
        self.assertEqual(list(dest_dir.iterdir()), [])


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

    def test_no_mono_without_text_falls_back_to_original(self):
        """Pagina senza testo: BabelDOC esce 0 senza mono → originale."""
        self.fake.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
        with mock.patch.object(clone_engine, "page_has_text", return_value=False):
            out = self.engine.translate_page(1, "google")
        self.assertIsNotNone(out)
        self.assertTrue(out.is_file())
        self.assertEqual(
            out.read_bytes(), self.engine.split_path(1).read_bytes()
        )
        self.assertEqual(self.engine.status(1, "google"), "empty")
        self.assertTrue(self.engine.is_cached(1, "google"))

    def test_no_mono_with_text_is_a_real_error(self):
        """Pagina con testo ma nessun output: errore vero, non fallback."""
        self.fake.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
        with mock.patch.object(clone_engine, "page_has_text", return_value=True):
            out = self.engine.translate_page(1, "google")
        self.assertIsNone(out)
        self.assertIn("non ha tradotto", self.engine.status(1, "google"))

    def test_llm_without_key_reports_error(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENROUTER_API_KEY", None)
            out = self.engine.translate_page(1, "llm")
        self.assertIsNone(out)
        self.assertTrue(self.engine.status(1, "llm").startswith("error:"))
        self.assertIn("missing_key", self.engine.status(1, "llm"))

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


class KeyErrorDetectionTests(unittest.TestCase):
    """Riconoscimento dell'errore di autenticazione dal subprocess."""

    def test_auth_error_markers(self):
        for text in ("HTTP 401", "Unauthorized", "Invalid API key",
                     "invalid_api_key"):
            self.assertTrue(clone_engine._looks_like_auth_error(text))
        self.assertFalse(clone_engine._looks_like_auth_error("timeout"))

    def test_classify_engine_failure_codes(self):
        cases = {
            "HTTP 401 Unauthorized": "invalid_key",
            "invalid_api_key": "invalid_key",
            "403 Forbidden": "forbidden",
            "402 Payment Required": "no_credits",
            "insufficient_quota": "no_credits",
            "429 Too Many Requests": "rate_limited",
            "404 model not found": "model_not_found",
            "getaddrinfo failed": "network",
            "Connection timed out": "network",
            "qualcosa di strano": "unknown",
        }
        for text, code in cases.items():
            self.assertEqual(
                clone_engine.classify_engine_failure(text), code, text
            )

    def test_llm_invalid_key_reports_invalid_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = tmp_path / "pdf2zh_next"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stderr.write('401 Unauthorized\\n')\n"
                "sys.exit(1)\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
            engine = clone_engine.CloneEngine(
                tmp_path / "cache", pdf2zh_bin=fake
            )
            src = tmp_path / "book.pdf"
            src.write_bytes(b"%PDF-1.4 source")
            engine.set_document(src)
            engine.lang_in, engine.lang_out = "en", "it"
            split = engine.split_path(1)
            split.parent.mkdir(parents=True, exist_ok=True)
            split.write_bytes(b"%PDF-1.4 page 1")
            with mock.patch.dict(
                os.environ, {"OPENROUTER_API_KEY": "sk-or-bad"}
            ):
                out = engine.translate_page(1, "llm")
            self.assertIsNone(out)
            self.assertEqual(engine.status(1, "llm"), "error:invalid_key")

    def test_llm_invalid_key_exit0_stdout_is_classified(self):
        """pdf2zh_next può uscire con 0 e scrivere il 401 su stdout: va classificato."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = tmp_path / "pdf2zh_next"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stdout.write("
                "'openai.AuthenticationError: Error code: 401 - Unauthorized\\n')\n"
                "sys.exit(0)\n"
            )
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
            engine = clone_engine.CloneEngine(
                tmp_path / "cache", pdf2zh_bin=fake
            )
            src = tmp_path / "book.pdf"
            src.write_bytes(b"%PDF-1.4 source")
            engine.set_document(src)
            engine.lang_in, engine.lang_out = "en", "it"
            split = engine.split_path(1)
            split.parent.mkdir(parents=True, exist_ok=True)
            split.write_bytes(b"%PDF-1.4 page 1")
            with mock.patch.object(
                clone_engine, "page_has_text", return_value=True
            ), mock.patch.dict(
                os.environ, {"OPENROUTER_API_KEY": "sk-or-bad"}
            ):
                out = engine.translate_page(1, "llm")
            self.assertIsNone(out)
            self.assertEqual(engine.status(1, "llm"), "error:invalid_key")


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


class EngineInstallTests(unittest.TestCase):
    """Installer del motore (.venv2) tramite uv: path e flusso, senza uv reale."""

    def test_engine_base_dir_is_module_dir_in_development(self):
        self.assertEqual(
            clone_engine.engine_base_dir(),
            Path(clone_engine.__file__).resolve().parent,
        )

    def test_engine_venv_dir_appends_venv2(self):
        self.assertEqual(
            clone_engine.engine_venv_dir("/tmp/base"),
            Path("/tmp/base/.venv2"),
        )

    def test_engine_venv_python_layout(self):
        py = clone_engine.engine_venv_python(Path("/base/.venv2"))
        expected = (
            Path("/base/.venv2/Scripts/python.exe")
            if os.name == "nt"
            else Path("/base/.venv2/bin/python")
        )
        self.assertEqual(py, expected)

    def test_find_uv_prefers_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "uv"
            fake.write_text("#!/bin/sh\n")
            with mock.patch.dict(os.environ, {"UV": str(fake)}):
                self.assertEqual(clone_engine.find_uv(), str(fake))

    def test_find_uv_none_when_missing(self):
        env = {k: v for k, v in os.environ.items() if k != "UV"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(clone_engine.sys, "_MEIPASS", None, create=True):
                with mock.patch.object(
                    clone_engine.shutil, "which", return_value=None
                ):
                    self.assertIsNone(clone_engine.find_uv())

    def test_find_uv_prefers_bundled_over_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            meipass = Path(tmp)
            bundled = meipass / clone_engine._bin_name("uv")
            bundled.write_text("")
            env = {k: v for k, v in os.environ.items() if k != "UV"}
            with mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch.object(
                     clone_engine.sys, "_MEIPASS", str(meipass), create=True
                 ), \
                 mock.patch.object(
                     clone_engine.shutil, "which", return_value="/usr/bin/uv"
                 ):
                self.assertEqual(clone_engine.find_uv(), str(bundled))

    def test_find_uv_env_override_beats_bundled(self):
        with tempfile.TemporaryDirectory() as tmp:
            meipass = Path(tmp)
            (meipass / clone_engine._bin_name("uv")).write_text("")
            fake = Path(tmp) / "my-uv"
            fake.write_text("")
            with mock.patch.dict(os.environ, {"UV": str(fake)}), \
                 mock.patch.object(
                     clone_engine.sys, "_MEIPASS", str(meipass), create=True
                 ):
                self.assertEqual(clone_engine.find_uv(), str(fake))

    def test_find_uv_falls_back_to_path_without_bundle(self):
        env = {k: v for k, v in os.environ.items() if k != "UV"}
        with mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(clone_engine.sys, "_MEIPASS", None, create=True), \
             mock.patch.object(
                 clone_engine.shutil, "which", return_value="/usr/bin/uv"
             ):
            self.assertEqual(clone_engine.find_uv(), "/usr/bin/uv")

    def test_bundled_uv_none_without_meipass(self):
        with mock.patch.object(clone_engine.sys, "_MEIPASS", None, create=True):
            self.assertIsNone(clone_engine._bundled_uv())

    def test_bundled_uv_gets_exec_bit(self):
        if os.name == "nt":
            self.skipTest("bit di esecuzione non applicabile su Windows")
        with tempfile.TemporaryDirectory() as tmp:
            meipass = Path(tmp)
            bundled = meipass / "uv"
            bundled.write_text("")
            bundled.chmod(0o600)
            with mock.patch.object(
                clone_engine.sys, "_MEIPASS", str(meipass), create=True
            ):
                path = clone_engine._bundled_uv()
            self.assertEqual(path, bundled)
            self.assertTrue(os.access(bundled, os.X_OK))

    def test_install_engine_without_uv_raises(self):
        with mock.patch.object(clone_engine, "find_uv", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                clone_engine.install_engine(base="/tmp/does-not-matter")
        self.assertEqual(str(ctx.exception), "no_uv")

    def test_install_engine_creates_venv_and_returns_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            calls = []

            class _FakeProc:
                def __init__(self, cmd, **_kw):
                    calls.append(list(cmd))
                    self.stdout = iter([f"fake: {' '.join(cmd[1:3])}\n"])

                def wait(self, timeout=None):
                    return 0

                def terminate(self):
                    pass

                def kill(self):
                    pass

            def _fake_popen(cmd, **_kw):
                # Simula l'effetto collaterale di uv: crea l'interprete del venv
                # al primo comando, l'eseguibile del motore dopo il pip install.
                if cmd[1] == "venv":
                    py = clone_engine.engine_venv_python(Path(cmd[-1]))
                    py.parent.mkdir(parents=True, exist_ok=True)
                    py.write_text("")
                elif cmd[1] == "pip":
                    py = Path(cmd[cmd.index("--python") + 1])
                    (py.parent / clone_engine._bin_name("pdf2zh_next")).write_text("")
                return _FakeProc(cmd)

            lines = []
            with mock.patch.object(clone_engine, "find_uv", return_value="uv"), \
                 mock.patch.object(clone_engine, "_is_windows", return_value=False), \
                 mock.patch.object(
                     clone_engine.subprocess, "Popen", side_effect=_fake_popen
                 ):
                found = clone_engine.install_engine(base=base, log_line=lines.append)

            self.assertTrue(found.is_file())
            self.assertEqual(found.name, clone_engine._bin_name("pdf2zh_next"))
            self.assertTrue(any(c[1] == "venv" for c in calls))
            self.assertTrue(any(c[1] == "pip" for c in calls))
            self.assertTrue(any("pdf2zh_next" in c for c in calls))
            self.assertTrue(lines)  # il log ha ricevuto le righe di uv

    def test_app_data_dir_honours_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            clone_engine.set_app_data_dir(tmp)
            try:
                self.assertEqual(clone_engine.app_data_dir(), Path(tmp))
                self.assertEqual(
                    clone_engine.user_engine_base(), Path(tmp) / "engine"
                )
            finally:
                clone_engine.set_app_data_dir(None)

    def test_candidate_paths_include_user_engine_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            clone_engine.set_app_data_dir(tmp)
            try:
                expected = (
                    Path(tmp) / "engine" / ".venv2" / "bin"
                    / clone_engine._bin_name("pdf2zh_next")
                )
                self.assertIn(expected, clone_engine._candidate_pdf2zh())
            finally:
                clone_engine.set_app_data_dir(None)

    def test_engine_base_dir_falls_back_when_app_dir_unwritable(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "not-a-dir"
            blocker.write_text("x")  # un file: mkdir sotto di esso fallisce
            with mock.patch.object(
                clone_engine,
                "_engine_roots",
                return_value=[blocker / "sub", Path(tmp)],
            ):
                self.assertEqual(clone_engine.engine_base_dir(), Path(tmp))

    def test_is_writable_detects_unwritable_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("x")
            self.assertFalse(clone_engine._is_writable(blocker / "sub"))
            self.assertTrue(clone_engine._is_writable(Path(tmp)))

    def test_engine_base_dir_frozen_uses_exe_dir_when_writable(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "App.exe"
            exe.write_text("")
            with mock.patch.object(clone_engine.sys, "frozen", True, create=True), \
                 mock.patch.object(clone_engine.sys, "executable", str(exe)):
                self.assertEqual(clone_engine.engine_base_dir(), Path(tmp))
                self.assertIn(
                    Path(tmp) / ".venv2" / "bin"
                    / clone_engine._bin_name("pdf2zh_next"),
                    clone_engine._candidate_pdf2zh(),
                )

    def test_engine_base_dir_frozen_falls_back_to_user_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("x")  # la "cartella" dell'exe è sotto un file
            exe = blocker / "sub" / "App.exe"
            user_dir = Path(tmp) / "engine"
            with mock.patch.object(clone_engine.sys, "frozen", True, create=True), \
                 mock.patch.object(clone_engine.sys, "executable", str(exe)), \
                 mock.patch.object(
                     clone_engine, "user_engine_base", return_value=user_dir
                 ):
                self.assertEqual(clone_engine.engine_base_dir(), user_dir)


class EngineCacheManagementTests(unittest.TestCase):
    """Purga/statistiche della cache per pagina e motore (una traduzione per pagina)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.engine = clone_engine.CloneEngine(tmp / "cache")
        self.src = tmp / "book.pdf"
        self.src.write_bytes(b"%PDF-1.4 source")
        self.engine.set_document(self.src)

    def tearDown(self):
        self._tmp.cleanup()

    def _fake_cache(
        self, engine: str, page: int, lang_in: str = "en",
        lang_out: str = "it", size: int = 10,
    ) -> Path:
        path = self.engine.translated_path(page, engine, lang_in, lang_out)
        path.write_bytes(b"x" * size)
        return path

    def test_cached_engines_for_page_lists_only_that_page(self):
        self.assertEqual(self.engine.cached_engines_for_page(0), [])
        self._fake_cache("google", 0)
        self._fake_cache("bing", 0)
        self._fake_cache("google", 1)  # altra pagina: non conta per la 0
        self.assertEqual(
            self.engine.cached_engines_for_page(0), ["google", "bing"]
        )
        self.assertEqual(self.engine.cached_engines_for_page(1), ["google"])
        self.assertEqual(self.engine.cached_engines_for_page(2), [])

    def test_cached_engines_for_page_ignores_empty_dirs(self):
        # translated_path crea la cartella: da sola non conta come cache.
        self.engine.translated_path(0, "llm", "en", "it")
        self.assertEqual(self.engine.cached_engines_for_page(0), [])

    def test_page_cache_stats_counts_files_and_bytes(self):
        self._fake_cache("google", 0, size=10)
        other = self.engine.translated_path(0, "google", "en", "fr")
        other.write_bytes(b"x" * 5)
        self.assertEqual(self.engine.page_cache_stats("google", 0), (2, 15))
        self.assertEqual(self.engine.page_cache_stats("google", 1), (0, 0))

    def test_purge_removes_only_that_page_and_engine(self):
        self._fake_cache("google", 0)
        self._fake_cache("google", 1)
        self._fake_cache("bing", 0)
        files, size = self.engine.purge_page_cache("google", 0)
        self.assertEqual((files, size), (1, 10))
        self.assertFalse(self.engine.is_cached(0, "google"))
        self.assertTrue(self.engine.is_cached(1, "google"))  # altra pagina resta
        self.assertTrue(self.engine.is_cached(0, "bing"))     # altro motore resta
        self.assertEqual(self.engine.cached_engines_for_page(0), ["bing"])

    def test_purge_all_languages_of_that_page(self):
        self._fake_cache("google", 0, "en", "it")
        other = self.engine.translated_path(0, "google", "en", "fr")
        other.write_bytes(b"x" * 5)
        files, size = self.engine.purge_page_cache("google", 0)
        self.assertEqual((files, size), (2, 15))
        self.assertEqual(self.engine.cached_engines_for_page(0), [])

    def test_purge_unknown_page_is_empty(self):
        self.assertEqual(self.engine.purge_page_cache("google", 3), (0, 0))

    def test_purge_keeps_split_cache(self):
        split = self.engine.split_path(0)
        split.parent.mkdir(parents=True, exist_ok=True)
        split.write_bytes(b"%PDF-1.4 split")
        self._fake_cache("google", 0)
        self.engine.purge_page_cache("google", 0)
        self.assertTrue(split.is_file())


class PageHasTextTests(unittest.TestCase):
    """``page_has_text`` distingue le pagine senza testo (fallback) dagli errori."""

    def setUp(self):
        try:
            import pymupdf
        except ImportError:  # pragma: no cover
            self.skipTest("pymupdf non disponibile")
        self.pymupdf = pymupdf
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _pdf(self, name: str, text: str) -> Path:
        path = Path(self._tmp.name) / name
        doc = self.pymupdf.open()
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
        doc.save(str(path))
        doc.close()
        return path

    def test_text_page_is_detected(self):
        self.assertTrue(clone_engine.page_has_text(self._pdf("t.pdf", "hello")))

    def test_empty_page_is_detected(self):
        self.assertFalse(clone_engine.page_has_text(self._pdf("e.pdf", "")))

    def test_unreadable_path_defaults_to_true(self):
        # In dubbio non si maschera un possibile errore di traduzione.
        self.assertTrue(
            clone_engine.page_has_text(Path(self._tmp.name) / "missing.pdf")
        )


class NoWindowTests(unittest.TestCase):
    """I subprocess dell'app GUI non devono aprire console su Windows."""

    class _StartupInfo:
        def __init__(self):
            self.dwFlags = 0

    def test_empty_off_windows(self):
        with mock.patch.object(clone_engine, "_is_windows", return_value=False):
            self.assertEqual(clone_engine._no_window_kwargs(), {})

    def test_creationflags_and_startupinfo_on_windows(self):
        with mock.patch.object(clone_engine, "_is_windows", return_value=True), \
             mock.patch.object(
                 clone_engine.subprocess, "CREATE_NO_WINDOW", 0x08000000,
                 create=True,
             ), \
             mock.patch.object(
                 clone_engine.subprocess, "STARTUPINFO", self._StartupInfo,
                 create=True,
             ), \
             mock.patch.object(
                 clone_engine.subprocess, "STARTF_USESHOWWINDOW", 1, create=True
             ):
            kwargs = clone_engine._no_window_kwargs()
        self.assertEqual(kwargs["creationflags"], 0x08000000)
        self.assertTrue(kwargs["startupinfo"].dwFlags & 1)

    def test_popen_receives_no_window_flags(self):
        """``install_engine`` avvia ``uv`` senza finestra console."""
        captured = {}

        class _FakeProc:
            returncode = 0
            stdout = []

            def wait(self, timeout=None):
                return 0

        def _fake_popen(cmd, **kwargs):
            captured.update(kwargs)
            return _FakeProc()

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(clone_engine, "find_uv", return_value="/fake/uv"), \
                 mock.patch.object(clone_engine, "_is_windows", return_value=True), \
                 mock.patch.object(clone_engine.subprocess, "Popen", _fake_popen), \
                 mock.patch.object(
                     clone_engine.subprocess, "CREATE_NO_WINDOW", 0x08000000,
                     create=True,
                 ), \
                 mock.patch.object(
                     clone_engine.subprocess, "STARTUPINFO", self._StartupInfo,
                     create=True,
                 ), \
                 mock.patch.object(
                     clone_engine.subprocess, "STARTF_USESHOWWINDOW", 1, create=True
                 ):
                try:
                    clone_engine.install_engine(base=tmp)
                except RuntimeError:
                    pass
        self.assertEqual(captured.get("creationflags"), 0x08000000)
        self.assertIn("startupinfo", captured)


class EngineEnvTests(unittest.TestCase):
    """Il subprocess del motore riceve ``PYTHONIOENCODING=utf-8``.

    Su Windows, senza questo, la catena gratuita (``gtranslate_cli.py``) scrive
    le accentate in cp1252 e pdf2zh le sostituisce con U+FFFD.
    """

    def test_engine_passes_utf8_io_encoding(self):
        import subprocess

        import pymupdf

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src = tmp_path / "book.pdf"
            doc = pymupdf.open()
            doc.new_page()
            doc.save(src)
            doc.close()
            fake = tmp_path / "pdf2zh_next"
            fake.write_text("#!/bin/sh\n")
            engine = clone_engine.CloneEngine(
                tmp_path / "cache", pdf2zh_bin=fake
            )
            engine.set_document(src)
            engine.lang_in, engine.lang_out = "en", "it"
            captured: dict = {}

            def fake_run(cmd, env, cancel_event=None):
                captured.update(env)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(engine, "_run_engine", fake_run), \
                 mock.patch.object(
                     clone_engine, "page_has_text", return_value=False
                 ):
                engine.translate_page(0, "google")
            self.assertEqual(captured.get("PYTHONIOENCODING"), "utf-8")


if __name__ == "__main__":
    unittest.main()
