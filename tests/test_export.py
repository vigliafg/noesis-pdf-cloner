"""Tests for the translated-page export UI (ExportWizardDialog + CloneExportThread).

Gira headless (QT_QPA_PLATFORM=offscreen) e viene saltato se PyQt6 manca.
Non richiede pdf2zh_next: il motore è sostituito da un fake in-memory.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from PyQt6.QtWidgets import QApplication, QDialog
    _HAS_QT = True
except ImportError:  # pragma: no cover
    _HAS_QT = False

import i18n  # noqa: E402


class _FakeEngine:
    """Minimal CloneEngine stand-in: cache set + controlled outcomes."""

    def __init__(self, cached=(), fail=(), running_once=()):
        self._cached = set(cached)
        self._fail = set(fail)
        self._running_once = set(running_once)
        self.calls: list[int] = []
        self._status: dict[tuple, str] = {}
        # (engine, lang_in, lang_out, page) seen by cache queries.
        self.query_langs: list[tuple] = []

    def is_cached(self, page, engine, lang_in=None, lang_out=None):
        self.query_langs.append((engine, lang_in, lang_out, page))
        return page in self._cached

    def cached_pages(
        self, page_from, page_to, engine, lang_in=None, lang_out=None
    ):
        self.query_langs.append((engine, lang_in, lang_out, "range"))
        if page_from > page_to:
            page_from, page_to = page_to, page_from
        return [p for p in range(page_from, page_to + 1) if p in self._cached]

    def translated_path_for(
        self, page, engine, lang_in=None, lang_out=None
    ) -> Path:
        return Path(f"/fake/page_{page:06d}.pdf")

    def translate_page(self, page: int, engine: str, cancel_event=None):
        self.calls.append(page)
        if page in self._running_once:
            self._running_once.discard(page)
            self._status[(page, engine)] = "running"
            return None
        if page in self._fail:
            self._status[(page, engine)] = "error:boom"
            return None
        self._cached.add(page)
        self._status[(page, engine)] = "done"
        return self.translated_path_for(page, engine)

    def status(self, page: int, engine: str) -> str:
        return self._status.get((page, engine), "none")

    def export_pdf(self, pages, engine, dest, lang_in=None, lang_out=None):
        Path(dest).write_bytes(b"%PDF-1.4 fake merged")
        return len(list(pages))

    def export_zip(
        self, pages, engine, dest, lang_in=None, lang_out=None, stem=None
    ):
        import zipfile

        with zipfile.ZipFile(dest, "w") as archive:
            for page in pages:
                archive.writestr(
                    f"{stem or 'page'}_p{page + 1:04d}.pdf", b"%PDF-1.4 fake"
                )
        return len(list(pages))


@unittest.skipUnless(_HAS_QT, "PyQt6 non disponibile")
class ExportWizardDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        cfg = Path(self._tmp.name) / "config.json"
        self._prev_lang = i18n.get_language()
        i18n.init_config(cfg, defaults={**i18n.DEFAULTS, "lang": "it"})
        i18n.set_language("it")

        import main
        self.main = main
        self.engine = _FakeEngine(cached=[155])  # pagina 156 (1-based)

    def tearDown(self):
        i18n.set_language(self._prev_lang)
        self._tmp.cleanup()

    def _dialog(
        self, current=155, total=300, engine=None,
        engine_name="google", target="it", source="auto",
    ):
        return self.main.ExportWizardDialog(
            engine or self.engine, engine_name, target, source, current, total
        )

    def test_wizard_has_five_steps(self):
        dlg = self._dialog()
        self.assertEqual(len(dlg.STEPS), 5)
        self.assertEqual(dlg._step, 0)
        self.assertEqual(dlg._stack.currentIndex(), 0)

    def test_default_is_current_page(self):
        dlg = self._dialog()
        self.assertTrue(dlg.is_current())
        self.assertEqual(dlg.chosen_pages(), [155])

    def test_default_engine_and_target_from_current_settings(self):
        dlg = self._dialog(engine_name="bing", target="fr", source="en")
        self.assertEqual(dlg.chosen_engine(), "bing")
        self.assertEqual(dlg.chosen_target(), "fr")
        self.assertEqual(dlg.chosen_source(), "en")

    def test_navigation_back_and_next(self):
        dlg = self._dialog()
        self.assertTrue(dlg._btn_back.isHidden())
        dlg._go_next()
        self.assertEqual(dlg._step, 1)
        self.assertEqual(dlg._stack.currentIndex(), 1)
        self.assertFalse(dlg._btn_back.isHidden())
        dlg._go_back()
        self.assertEqual(dlg._step, 0)

    def test_last_step_button_says_start(self):
        dlg = self._dialog()
        dlg._set_step(len(dlg.STEPS) - 1)
        self.assertEqual(dlg._btn_next.text(), i18n.T("export.wizard.start"))

    def test_changing_engine_changes_ready_count(self):
        engine = _FakeEngine(cached=[155, 156])
        dlg = self._dialog(engine=engine)
        dlg._rad_range.setChecked(True)
        dlg._from_spin.setValue(156)
        dlg._to_spin.setValue(159)
        self._app.processEvents()
        self.assertEqual(
            dlg._ready_lbl.text(),
            i18n.T("export.ready", cached=2, total=4, missing=2),
        )
        # Il cambio motore passa il nuovo engine alle query di cache.
        engine.query_langs.clear()
        dlg._engine_buttons["bing"].setChecked(True)
        self._app.processEvents()
        self.assertTrue(any(q[0] == "bing" for q in engine.query_langs))

    def test_changing_target_language_is_used_for_cache_query(self):
        engine = _FakeEngine(cached=[155])
        dlg = self._dialog(engine=engine)
        dlg._rad_range.setChecked(True)
        dlg._from_spin.setValue(156)
        dlg._to_spin.setValue(159)
        engine.query_langs.clear()
        dlg._dst_combo.setCurrentIndex(dlg._dst_combo.findData("fr"))
        self._app.processEvents()
        self.assertTrue(any(q[2] == "fr" for q in engine.query_langs))
        self.assertEqual(dlg.chosen_target(), "fr")

    def test_range_maps_one_based_to_zero_based(self):
        dlg = self._dialog()
        dlg._rad_range.setChecked(True)
        dlg._from_spin.setValue(156)
        dlg._to_spin.setValue(159)
        self.assertFalse(dlg.is_current())
        self.assertEqual(dlg.chosen_pages(), [155, 156, 157, 158])
        self.assertEqual(dlg.range_label(), "156-159")

    def test_translate_missing_auto_checked_for_uncached_range(self):
        # Regressione: con range non in cache e casella non spuntata non
        # partiva né il salvataggio né la traduzione.
        dlg = self._dialog()
        dlg._rad_range.setChecked(True)
        dlg._from_spin.setValue(156)
        dlg._to_spin.setValue(159)
        self._app.processEvents()
        self.assertTrue(dlg._chk_translate.isChecked())
        self.assertTrue(dlg.translate_missing())

    def test_manual_uncheck_is_respected(self):
        dlg = self._dialog()
        dlg._rad_range.setChecked(True)
        dlg._from_spin.setValue(156)
        dlg._to_spin.setValue(159)
        self._app.processEvents()
        self.assertTrue(dlg._chk_translate.isChecked())
        dlg._chk_translate.setChecked(False)  # scelta esplicita dell'utente
        dlg._from_spin.setValue(157)
        self._app.processEvents()
        self.assertFalse(dlg._chk_translate.isChecked())
        self.assertFalse(dlg.translate_missing())

    def test_ready_label_counts_cached_pages(self):
        dlg = self._dialog()
        dlg._rad_range.setChecked(True)
        dlg._from_spin.setValue(156)
        dlg._to_spin.setValue(159)
        self._app.processEvents()
        self.assertEqual(
            dlg._ready_lbl.text(),
            i18n.T("export.ready", cached=1, total=4, missing=3),
        )
        # Con pagine mancanti l'opzione è attiva.
        self.assertTrue(dlg._chk_translate.isEnabled())

    def test_translate_missing_disabled_when_all_cached(self):
        engine = _FakeEngine(cached=[155, 156, 157, 158])
        dlg = self._dialog(engine=engine)
        dlg._rad_range.setChecked(True)
        dlg._from_spin.setValue(156)
        dlg._to_spin.setValue(159)
        self._app.processEvents()
        self.assertFalse(dlg._chk_translate.isEnabled())

    def test_spins_are_independent(self):
        # Regressione: modificare un campo non deve toccare l'altro.
        dlg = self._dialog()
        dlg._rad_range.setChecked(True)
        dlg._from_spin.setValue(159)
        dlg._to_spin.setValue(160)
        dlg._to_spin.setValue(150)  # es. un backspace che abbassa "a"
        self.assertEqual(dlg._from_spin.value(), 159)
        self.assertEqual(dlg._to_spin.value(), 150)
        # L'intervallo viene comunque normalizzato al momento dell'uso.
        self.assertEqual(dlg.chosen_pages(), list(range(149, 159)))
        self.assertEqual(dlg.range_label(), "150-159")

    def test_range_label_reflects_typed_values(self):
        # Regressione "1 di 21": con keyboard tracking disattivato il testo
        # digitato non veniva committato in value(), così la label usava un
        # valore precedente. Ora testo e valore restano sincronizzati.
        from PyQt6.QtTest import QTest

        dlg = self._dialog(current=139, total=4132)  # pagina corrente 140
        dlg._rad_range.setChecked(True)
        self._app.processEvents()
        self.assertTrue(dlg._from_spin.keyboardTracking())

        dlg._from_spin.setFocus()
        dlg._from_spin.selectAll()
        QTest.keyClicks(dlg._from_spin, "120")
        dlg._to_spin.setFocus()
        dlg._to_spin.selectAll()
        QTest.keyClicks(dlg._to_spin, "122")
        self._app.processEvents()

        self.assertEqual(dlg.chosen_pages(), [119, 120, 121])
        self.assertEqual(dlg.range_label(), "120-122")
        self.assertEqual(
            dlg._ready_lbl.text(),
            i18n.T("export.ready", cached=0, total=3, missing=3),
        )

    def test_default_output_format_is_merged_pdf(self):
        dlg = self._dialog()
        self.assertFalse(dlg.chosen_zip())
        self.assertTrue(dlg.chosen_path().endswith(".pdf"))

    def test_zip_output_format_can_be_chosen(self):
        dlg = self._dialog()
        dlg._btn_zip.setChecked(True)
        self.assertTrue(dlg.chosen_zip())
        self.assertTrue(dlg.chosen_path().endswith(".zip"))

    def test_default_path_uses_choices(self):
        dlg = self._dialog(engine_name="llm", target="it")
        path = dlg.chosen_path()
        self.assertTrue(path.endswith("_pag156_llm_it.pdf"), path)

    def test_chosen_path_keeps_explicit_path(self):
        dlg = self._dialog()
        custom = str(Path(self._tmp.name) / "custom.pdf")
        dlg._path_edit.setText(custom)
        dlg._path_touched = True
        self.assertEqual(dlg.chosen_path(), custom)

    def test_qss_styles_the_wizard_controls(self):
        # Lo stile è ricalcato dal wizard del servizio: radio e schede motore
        # devono avere le regole dedicate (altrimenti testo scuro su scuro).
        self.assertIn("QRadioButton", self.main._WIZARD_QSS)
        self.assertIn("wizCardOpt", self.main._WIZARD_QSS)
        self.assertIn("wizStep", self.main._WIZARD_QSS)


@unittest.skipUnless(_HAS_QT, "PyQt6 non disponibile")
class PreviewAndLiquidTests(unittest.TestCase):
    """Anteprime dello step Pagine e overlay liquido."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        cfg = Path(self._tmp.name) / "config.json"
        self._prev_lang = i18n.get_language()
        i18n.init_config(cfg, defaults={**i18n.DEFAULTS, "lang": "it"})
        i18n.set_language("it")
        import main
        self.main = main
        self.engine = _FakeEngine(cached=[0])

    def tearDown(self):
        i18n.set_language(self._prev_lang)
        self._tmp.cleanup()

    def _make_pdf(self, n: int = 5) -> Path:
        import pymupdf

        path = Path(self._tmp.name) / "doc.pdf"
        doc = pymupdf.open()
        for _ in range(n):
            doc.new_page()
        doc.save(str(path))
        doc.close()
        return path

    def _wizard(self, pdf, current=1, total=5):
        return self.main.ExportWizardDialog(
            self.engine, "google", "it", "auto", current, total, pdf
        )

    def test_spin_sits_above_its_own_thumbnail(self):
        pdf = self._make_pdf(5)
        try:
            import pymupdf  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("pymupdf non disponibile")
        dlg = self._wizard(pdf)
        # Lo spin "Da" sta nella colonna sinistra, lo spin "A" nella destra.
        self.assertTrue(dlg._col_from.isAncestorOf(dlg._from_spin))
        self.assertTrue(dlg._col_to.isAncestorOf(dlg._to_spin))
        # Nelle colonne l'header (spin) precede il riquadro della miniatura.
        for col, head, prev, thumb in (
            (dlg._col_from, dlg._from_head, dlg._from_prev, dlg._thumb_first),
            (dlg._col_to, dlg._to_head, dlg._to_prev, dlg._thumb_last),
        ):
            self.assertTrue(col.isAncestorOf(thumb))
            self.assertTrue(prev.isAncestorOf(thumb))
            lay = col.layout()
            self.assertLess(lay.indexOf(head), lay.indexOf(prev))
        dlg.done(0)

    def test_current_and_range_boxes_share_one_row(self):
        pdf = self._make_pdf(5)
        dlg = self._wizard(pdf)
        # Radio "Pagina corrente" e i due box Da/A sono sulla stessa riga.
        for widget in (dlg._rad_current, dlg._from_head, dlg._to_head):
            self.assertTrue(dlg._preview_row.isAncestorOf(widget))
        # E ognuno ha la propria miniatura sotto l'header.
        self.assertTrue(dlg._col_current.isAncestorOf(dlg._thumb_current))
        self.assertLess(
            dlg._col_current.layout().indexOf(dlg._rad_current),
            dlg._col_current.layout().indexOf(dlg._cur_prev),
        )
        dlg.done(0)

    def test_thumbnail_box_matches_page_aspect(self):
        import pymupdf

        pdf = Path(self._tmp.name) / "tall.pdf"
        doc = pymupdf.open()
        doc.new_page(width=200, height=400)  # rapporto 0.5
        doc.save(str(pdf))
        doc.close()
        dlg = self.main.ExportWizardDialog(
            self.engine, "google", "it", "auto", 0, 1, pdf
        )
        dlg._update_page_preview()
        # In modalità "Pagina corrente" la miniatura corrente ha l'aspetto reale.
        self.assertEqual(dlg._thumb_current.width(), 150)  # 300 * 0.5
        self.assertEqual(dlg._thumb_current.height(), self.main._PREVIEW_H)
        dlg.done(0)

    def test_preview_shows_first_and_last_of_range(self):
        try:
            import pymupdf  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("pymupdf non disponibile")
        pdf = self._make_pdf(5)
        dlg = self._wizard(pdf)
        dlg._rad_range.setChecked(True)
        dlg._from_spin.setValue(2)
        dlg._to_spin.setValue(4)
        dlg._update_page_preview()
        self.assertFalse(dlg._thumb_first.pixmap().isNull())
        self.assertFalse(dlg._from_prev.isHidden())
        self.assertFalse(dlg._to_prev.isHidden())
        self.assertIn("2", dlg._cap_first.text())
        self.assertIn("4", dlg._cap_last.text())
        dlg.done(0)

    def test_preview_single_thumbnail_for_current_page(self):
        try:
            import pymupdf  # noqa: F401
        except ImportError:  # pragma: no cover
            self.skipTest("pymupdf non disponibile")
        pdf = self._make_pdf(5)
        dlg = self._wizard(pdf)
        dlg._rad_current.setChecked(True)
        dlg._update_page_preview()
        self.assertTrue(dlg._from_prev.isHidden())
        self.assertTrue(dlg._to_prev.isHidden())
        self.assertFalse(dlg._cur_prev.isHidden())
        dlg.done(0)

    def test_preview_caption_reports_printed_label(self):
        try:
            import pymupdf
        except ImportError:  # pragma: no cover
            self.skipTest("pymupdf non disponibile")
        pdf = Path(self._tmp.name) / "labeled.pdf"
        doc = pymupdf.open()
        for _ in range(3):
            doc.new_page()
        try:
            doc.set_page_labels([
                {"startpage": 0, "prefix": "", "style": "D", "firstpagenum": 100}
            ])
        except Exception:  # pragma: no cover
            self.skipTest("set_page_labels non disponibile")
        doc.save(str(pdf))
        doc.close()
        dlg = self.main.ExportWizardDialog(
            self.engine, "google", "it", "auto", 0, 3, pdf
        )
        dlg._update_page_preview()
        self.assertIn("100", dlg._cap_current.text())
        dlg.done(0)

    def test_preview_is_debounced(self):
        pdf = self._make_pdf(5)
        dlg = self._wizard(pdf)
        dlg._from_spin.setValue(3)
        self.assertTrue(dlg._preview_timer.isActive())
        dlg.done(0)

    def test_liquid_overlay_progress_caps_and_finishes(self):
        overlay = self.main.LiquidOverlay()
        overlay.start(None, "Traduzione…", duration_ms=1000)
        for _ in range(4):
            overlay._tick()
        self.assertGreater(overlay.progress(), 0.0)
        self.assertLessEqual(overlay.progress(), 90.0)
        overlay.finish(True)
        self.assertEqual(overlay.progress(), 100.0)
        overlay.stop()
        self.assertFalse(overlay.isVisible())

    def test_liquid_cancel_button_emits(self):
        overlay = self.main.LiquidOverlay()
        seen = []
        overlay.cancel_requested.connect(lambda: seen.append(True))
        overlay.start(None, "x", cancelable=True)
        self.assertFalse(overlay._btn_cancel.isHidden())
        overlay._btn_cancel.click()
        self.assertEqual(seen, [True])
        self.assertFalse(overlay._btn_cancel.isEnabled())
        overlay.stop()

    def test_liquid_without_cancel_hides_button(self):
        overlay = self.main.LiquidOverlay()
        overlay.start(None, "x", cancelable=False)
        self.assertTrue(overlay._btn_cancel.isHidden())
        overlay.stop()

    def test_translate_thread_emits_cancelled(self):
        class _CancelEngine:
            def translate_page(self, page, engine, cancel_event=None):
                return None

            def status(self, page, engine):
                return "cancelled"

        thread = self.main.CloneTranslateThread(_CancelEngine(), 3, "google", 1)
        got = []
        thread.cancelled.connect(lambda *a: got.append(a))
        thread.run()
        self.assertEqual(got, [(1, 3, "google")])

    def test_translate_thread_exposes_page(self):
        thread = self.main.CloneTranslateThread(self.engine, 41, "google", 1)
        self.assertEqual(thread.page(), 41)

    def test_translate_thread_waits_for_concurrent_running(self):
        """Un worker già attivo sulla stessa pagina non è un errore."""
        from unittest import mock

        engine = _FakeEngine(running_once=[0])
        thread = self.main.CloneTranslateThread(engine, 0, "google", 1)
        done, error = [], []
        thread.done.connect(lambda *a: done.append(a))
        thread.error.connect(lambda *a: error.append(a))
        with mock.patch.object(self.main.time, "sleep", return_value=None):
            thread.run()
        self.assertEqual(error, [])
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0][:3], (1, 0, "google"))


@unittest.skipUnless(_HAS_QT, "PyQt6 non disponibile")
class CloneExportThreadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        import main
        self.thread_cls = main.CloneExportThread
        self._signals: dict = {}

    def _run(self, engine, pages):
        thread = self.thread_cls(engine, pages, "google")
        self._signals = {"progress": [], "errors": [], "finished": []}
        thread.progress.connect(
            lambda d, t, p: self._signals["progress"].append((d, t, p))
        )
        thread.page_error.connect(
            lambda p, m: self._signals["errors"].append((p, m))
        )
        thread.batch_finished.connect(
            lambda d, f, t: self._signals["finished"].append((d, f, t))
        )
        thread.run()  # synchronous: signals delivered directly
        return thread

    def test_successful_batch_counts_and_progress(self):
        engine = _FakeEngine()
        self._run(engine, [0, 1, 2])
        self.assertEqual(engine.calls, [0, 1, 2])
        self.assertEqual(self._signals["finished"], [(3, 0, 3)])
        self.assertEqual(
            [p[2] for p in self._signals["progress"]], [0, 1, 2]
        )
        self.assertEqual(self._signals["errors"], [])

    def test_failures_are_reported(self):
        engine = _FakeEngine(fail=[1])
        self._run(engine, [0, 1, 2])
        self.assertEqual(self._signals["finished"], [(2, 1, 3)])
        self.assertEqual(self._signals["errors"], [(1, "boom")])

    def test_waits_for_concurrent_running_then_succeeds(self):
        engine = _FakeEngine(running_once=[0])
        self._run(engine, [0])
        self.assertEqual(engine.calls, [0, 0])  # primo None, poi riuscito
        self.assertEqual(self._signals["finished"], [(1, 0, 1)])

    def test_cancel_stops_before_next_page(self):
        engine = _FakeEngine()

        thread = self.thread_cls(engine, [0, 1, 2], "google")
        finished: list = []
        thread.batch_finished.connect(lambda *a: finished.append(a))
        # Annulla dopo la prima pagina, emulando la pressione del pulsante
        # mentre la coda è in corso.
        orig = engine.translate_page

        def _translate(page, eng, cancel_event=None):
            res = orig(page, eng)
            if page == 0:
                thread.cancel()
            return res

        engine.translate_page = _translate  # type: ignore[assignment]
        thread.run()
        self.assertEqual(engine.calls, [0])
        self.assertEqual(finished, [(1, 0, 3)])


@unittest.skipUnless(_HAS_QT, "PyQt6 non disponibile")
class ExportProgressDialogTests(unittest.TestCase):
    """Il dialog di attesa mostra contesto, avanzamento, conteggi, ETA, log."""

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        cfg = Path(self._tmp.name) / "config.json"
        self._prev_lang = i18n.get_language()
        i18n.init_config(cfg, defaults={**i18n.DEFAULTS, "lang": "it"})
        i18n.set_language("it")
        import main
        self.main = main

    def tearDown(self):
        i18n.set_language(self._prev_lang)
        self._tmp.cleanup()

    def _dialog(self):
        return self.main.ExportProgressDialog(
            engine_label="Google",
            lang_label="🇮🇹 Italiano",
            page_from=510,
            page_to=520,
            missing=11,
            cached=0,
            total=11,
        )

    def test_context_and_progress(self):
        dlg = self._dialog()
        self.assertIn("Google", dlg._ctx_lbl.text())
        self.assertIn("510", dlg._range_lbl.text())
        self.assertIn("11", dlg._range_lbl.text())
        dlg.begin(512)
        self.assertIn("512", dlg._page_lbl.text())
        self.assertIn("512", dlg._activity_lbl.text())  # feedback immediato
        self.assertTrue(dlg._log.toPlainText().strip())  # log non vuoto
        self.assertEqual(dlg._bar.minimum(), 0)
        self.assertEqual(dlg._bar.maximum(), 0)  # busy durante la pagina
        dlg.set_stats(2, 0, 11)
        self.assertEqual(dlg._bar.value(), 2)
        self.assertEqual(dlg._bar.maximum(), 11)

    def test_log_success_and_failure(self):
        dlg = self._dialog()
        dlg.log_ok(509)
        dlg.log_fail(510, "boom")
        text = dlg._log.toPlainText()
        self.assertIn("✓", text)
        self.assertIn("510", text)
        self.assertIn("boom", text)
        self.assertIn("511", dlg._activity_lbl.text())  # pagina 510 -> 1-based 511

    def test_stats_show_counts_elapsed_and_eta(self):
        dlg = self._dialog()
        dlg.begin(510)
        dlg.set_stats(done=2, failed=1, total=11)
        stats = dlg._stats_lbl.text()
        self.assertIn("2", stats)
        self.assertIn("1", stats)
        self.assertRegex(stats, r"\d{2}:\d{2}")
        self.assertRegex(dlg._eta_lbl.text(), r"\d{2}:\d{2}")

    def test_completed_state_shows_path_and_close(self):
        dlg = self._dialog()
        dlg.set_completed("/tmp/out.pdf", count=11, failed=0)
        self.assertIn("completata", dlg._activity_lbl.text().lower())
        self.assertFalse(dlg._path_lbl.isHidden())
        self.assertIn("/tmp/out.pdf", dlg._path_lbl.text())
        self.assertFalse(dlg._btn_close.isHidden())
        self.assertTrue(dlg._btn_cancel.isHidden())
        self.assertTrue(dlg._finished)

    def test_error_state_keeps_window_with_close(self):
        dlg = self._dialog()
        dlg.set_error("boom")
        self.assertEqual(dlg._activity_lbl.text(), "boom")
        self.assertFalse(dlg._btn_close.isHidden())
        self.assertTrue(dlg._btn_cancel.isHidden())

    def test_preview_follows_translating_page(self):
        import pymupdf

        pdf = Path(self._tmp.name) / "src.pdf"
        doc = pymupdf.open()
        for _ in range(3):
            doc.new_page()
        doc.save(str(pdf))
        doc.close()
        dlg = self.main.ExportProgressDialog(
            engine_label="Google", lang_label="It",
            page_from=1, page_to=3, missing=3, cached=0, total=3,
            pdf_path=pdf,
        )
        dlg.begin(2)
        self.assertFalse(dlg._preview.isHidden())
        self.assertIn("2", dlg._preview._caption)
        dlg.set_completed("/tmp/out.pdf", 3, 0)
        self.assertTrue(dlg._preview.isHidden())

    def test_log_records_each_page_in_progress(self):
        dlg = self._dialog()
        dlg.begin(510)
        dlg.set_translating(511, position=2)
        text = dlg._log.toPlainText()
        self.assertIn("510", text)
        self.assertIn("511", text)

    def test_page_label_shows_position(self):
        dlg = self._dialog()
        dlg.begin(510)
        dlg.set_translating(511, position=2)
        self.assertIn("2 di 11", dlg._page_lbl.text())

    def test_completed_range_label_switches_to_translated(self):
        dlg = self._dialog()
        dlg.set_completed("/tmp/out.pdf", count=11, failed=0)
        self.assertIn("Tradotte", dlg._range_lbl.text())

    def test_save_to_download_copies_file(self):
        from unittest import mock

        src_dir = Path(self._tmp.name) / "src"
        src_dir.mkdir()
        src = src_dir / "out.pdf"
        src.write_bytes(b"%PDF-1.4 x")
        dlg = self._dialog()
        dlg.set_completed(str(src), 1, 0)
        with mock.patch.object(
            self.main.QStandardPaths,
            "writableLocation",
            return_value=self._tmp.name,
        ):
            dlg._save_to_download()
        self.assertTrue((Path(self._tmp.name) / "out.pdf").is_file())

    def test_open_folder_uses_export_destination(self):
        from unittest import mock

        dlg = self._dialog()
        dlg.set_completed("/tmp/export_dir/out.pdf", 1, 0)
        opened = []
        with mock.patch.object(
            self.main.QDesktopServices,
            "openUrl",
            side_effect=lambda url: opened.append(url.toLocalFile()),
        ):
            dlg._open_folder()
        self.assertEqual(opened, ["/tmp/export_dir"])

    def test_open_folder_follows_download_after_save(self):
        from unittest import mock

        src_dir = Path(self._tmp.name) / "src"
        src_dir.mkdir()
        src = src_dir / "out.pdf"
        src.write_bytes(b"%PDF-1.4 x")
        dlg = self._dialog()
        dlg.set_completed(str(src), 1, 0)
        with mock.patch.object(
            self.main.QStandardPaths,
            "writableLocation",
            return_value=self._tmp.name,
        ):
            dlg._save_to_download()
        opened = []
        with mock.patch.object(
            self.main.QDesktopServices,
            "openUrl",
            side_effect=lambda url: opened.append(url.toLocalFile()),
        ):
            dlg._open_folder()
        self.assertEqual(opened, [self._tmp.name])

    def test_cancel_emits_once_and_blocks_after_finish(self):
        dlg = self._dialog()
        received = []
        dlg.cancelled.connect(lambda: received.append(True))
        dlg._btn_cancel.click()
        self.assertEqual(received, [True])
        dlg.set_cancelling()
        dlg._btn_cancel.click()  # disabilitato: nessun secondo segnale
        self.assertEqual(received, [True])
        dlg2 = self._dialog()
        received2 = []
        dlg2.cancelled.connect(lambda: received2.append(True))
        dlg2.finish()
        dlg2._on_cancel()
        self.assertEqual(received2, [])


@unittest.skipUnless(_HAS_QT, "PyQt6 non disponibile")
class ExportTranslationWaitTests(unittest.TestCase):
    """Regressione: la chiusura del dialog di attesa non annulla l'export.

    Il segnale ``cancelled`` viene emesso anche alla chiusura del dialogo;
    senza la guardia ``thread.isRunning()`` un batch riuscito veniva marcato
    come annullato.
    """

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])
        cls._app.setApplicationName("noesis-pdf-cloner")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        cfg = Path(self._tmp.name) / "config.json"
        self._prev_lang = i18n.get_language()
        i18n.init_config(cfg, defaults={**i18n.DEFAULTS, "lang": "it"})
        i18n.set_language("it")
        import main
        self.main = main

    def tearDown(self):
        i18n.set_language(self._prev_lang)
        self._tmp.cleanup()

    def test_successful_wait_is_not_reported_as_cancelled(self):
        window = self.main.MainWindow()
        engine = _FakeEngine()
        dest = Path(self._tmp.name) / "out.pdf"
        original = self.main.ExportProgressDialog.exec
        self.main.ExportProgressDialog.exec = (
            lambda self_: QDialog.DialogCode.Accepted
        )
        try:
            ok, count, failed = window._run_export_with_progress(
                engine, [0], True, "google", "auto", "it", 0, 0, 0, str(dest)
            )
        finally:
            self.main.ExportProgressDialog.exec = original
            window.close()
        self.assertTrue(ok)
        self.assertEqual((count, failed), (1, 0))
        self.assertTrue(dest.is_file())

    def test_instant_export_without_translation_shows_completion(self):
        window = self.main.MainWindow()
        engine = _FakeEngine(cached=[0, 1])
        dest = Path(self._tmp.name) / "instant.pdf"
        original = self.main.ExportProgressDialog.exec
        self.main.ExportProgressDialog.exec = (
            lambda self_: QDialog.DialogCode.Accepted
        )
        try:
            ok, count, failed = window._run_export_with_progress(
                engine, [], False, "google", "auto", "it", 0, 1, 2, str(dest)
            )
        finally:
            self.main.ExportProgressDialog.exec = original
            window.close()
        self.assertTrue(ok)
        self.assertEqual((count, failed), (2, 0))
        self.assertTrue(dest.is_file())

    def test_progress_dialog_is_shown_during_wait(self):
        from PyQt6.QtCore import QTimer

        window = self.main.MainWindow()
        engine = _FakeEngine()
        orig = engine.translate_page
        # Traduzione "lenta" per lasciare il tempo al loop annidato di
        # mostrare il dialog di attesa.
        engine.translate_page = lambda p, e, cancel_event=None: (
            time.sleep(0.2), orig(p, e)
        )[1]

        seen: list = []
        timer = QTimer()
        timer.setInterval(0)
        timer.timeout.connect(
            lambda: seen.append(
                [
                    w
                    for w in QApplication.topLevelWidgets()
                    if isinstance(w, self.main.ExportProgressDialog)
                    and w.isVisible()
                ]
            )
        )
        timer.start()
        dest = Path(self._tmp.name) / "out.pdf"
        original = self.main.ExportProgressDialog.exec
        self.main.ExportProgressDialog.exec = (
            lambda self_: QDialog.DialogCode.Accepted
        )
        try:
            ok, _, _ = window._run_export_with_progress(
                engine, [0], True, "google", "auto", "it", 0, 0, 0, str(dest)
            )
        finally:
            self.main.ExportProgressDialog.exec = original
            timer.stop()
            window.close()
        self.assertTrue(ok)
        self.assertTrue(any(seen), "il dialog di attesa non è mai stato visibile")

    def test_zip_export_writes_archive(self):
        window = self.main.MainWindow()
        engine = _FakeEngine(cached=[0, 1])
        dest = Path(self._tmp.name) / "out.zip"
        original = self.main.ExportProgressDialog.exec
        self.main.ExportProgressDialog.exec = (
            lambda self_: QDialog.DialogCode.Accepted
        )
        try:
            ok, count, failed = window._run_export_with_progress(
                engine, [], False, "google", "auto", "it", 0, 1, 2, str(dest), True
            )
        finally:
            self.main.ExportProgressDialog.exec = original
            window.close()
        self.assertTrue(ok)
        self.assertEqual((count, failed), (2, 0))
        self.assertTrue(dest.is_file())


@unittest.skipUnless(_HAS_QT, "PyQt6 non disponibile")
class OnDemandTranslationTests(unittest.TestCase):
    """Traduzione on demand: si avvia solo col pulsante, non al cambio pagina.

    Una traduzione per documento: se esiste la cache di un altro motore, la si
    elimina solo dopo conferma dell'utente.
    """

    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])
        cls._app.setApplicationName("noesis-pdf-cloner")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        cfg = Path(self._tmp.name) / "config.json"
        self._prev_lang = i18n.get_language()
        i18n.init_config(cfg, defaults={**i18n.DEFAULTS, "lang": "it"})
        i18n.set_language("it")
        import main
        self.main = main

    def tearDown(self):
        i18n.set_language(self._prev_lang)
        self._tmp.cleanup()

    class _Engine:
        def __init__(self, cached=(), engines=()):
            self.lang_in = "en"
            self.lang_out = "it"
            self._cached = set(cached)
            self._engines = list(engines)
            self.purged: list[str] = []

        def set_pdf2zh_bin(self, *args, **kwargs):
            pass

        def is_cached(self, page, engine, *args, **kwargs):
            return page in self._cached

        def available(self):
            return True

        def cached_engines_for_page(self, page):
            return list(self._engines)

        def page_cache_stats(self, engine, page):
            return (1, 1024)

        def purge_page_cache(self, engine, page):
            self.purged.append((engine, page))
            return (1, 1024)

        def status(self, page, engine):
            return "done"

        def translated_path(self, page, engine, *args, **kwargs):
            return Path("/fake/none.pdf")

    class _Doc:
        def close(self):
            pass

    def _window(self, engine):
        window = self.main.MainWindow()
        window._pdf_path = Path(self._tmp.name) / "doc.pdf"
        window._mupdf_doc = self._Doc()
        window._page_count = 10
        window._current_page = 0
        window._clone_engine = engine
        return window

    def test_set_page_does_not_auto_translate(self):
        engine = self._Engine()
        i18n.set_translation_engine("google")
        window = self._window(engine)
        calls: list = []
        window._request_translation = lambda page: calls.append(page)
        window._display_page = lambda page: None
        window._remember_last_page = lambda page: None
        window._show_clone_for_page = lambda page: calls.append(("show", page))
        try:
            window._set_page(0)
        finally:
            window.close()
        self.assertEqual(calls, [("show", 0)])

    def test_cached_page_is_shown_without_translating(self):
        engine = self._Engine(cached=[0], engines=["google"])
        i18n.set_translation_engine("google")
        window = self._window(engine)
        calls: list = []
        window._request_translation = lambda page: calls.append(page)
        try:
            window._on_translate_requested()
            message = window.status_bar.currentMessage()
        finally:
            window.close()
        self.assertEqual(calls, [])
        self.assertEqual(engine.purged, [])
        # Feedback esplicito: pagina già in cache per quel motore.
        self.assertEqual(
            message,
            i18n.T(
                "clone.already_cached",
                page=1,
                engine=window._engine_display("google"),
            ),
        )

    def test_other_engine_purged_after_confirm_then_translates(self):
        engine = self._Engine(engines=["bing"])
        i18n.set_translation_engine("google")
        window = self._window(engine)
        calls: list = []
        window._request_translation = lambda page: calls.append(page)
        window._confirm_purge = lambda olds, new, page: True
        try:
            window._on_translate_requested()
        finally:
            window.close()
        self.assertEqual(engine.purged, [("bing", 0)])
        self.assertEqual(calls, [0])

    def test_cancelled_confirm_aborts_translation(self):
        engine = self._Engine(engines=["bing"])
        i18n.set_translation_engine("google")
        window = self._window(engine)
        calls: list = []
        window._request_translation = lambda page: calls.append(page)
        window._confirm_purge = lambda olds, new, page: False
        try:
            window._on_translate_requested()
        finally:
            window.close()
        self.assertEqual(engine.purged, [])
        self.assertEqual(calls, [])

    def test_engine_switch_does_not_translate(self):
        engine = self._Engine(engines=["google"])
        i18n.set_translation_engine("google")
        window = self._window(engine)
        calls: list = []
        window._request_translation = lambda page: calls.append(page)
        try:
            window._on_engine_selected("bing")
        finally:
            window.close()
        self.assertEqual(calls, [])
        self.assertEqual(i18n.get_translation_engine(), "bing")


if __name__ == "__main__":
    unittest.main()
