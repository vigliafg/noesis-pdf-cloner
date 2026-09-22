"""Tests for the translated-page export UI (ExportDialog + CloneExportThread).

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
class ExportDialogTests(unittest.TestCase):
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
        return self.main.ExportDialog(
            engine or self.engine, engine_name, target, source, current, total
        )

    def test_default_is_current_page(self):
        dlg = self._dialog()
        self.assertTrue(dlg.is_current())
        self.assertEqual(dlg.chosen_pages(), [155])
        self.assertFalse(dlg.translate_missing())

    def test_default_engine_and_target_from_current_settings(self):
        dlg = self._dialog(engine_name="bing", target="fr", source="en")
        self.assertEqual(dlg.chosen_engine(), "bing")
        self.assertEqual(dlg.chosen_target(), "fr")
        self.assertEqual(dlg.chosen_source(), "en")

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
        dlg._engine_combo.setCurrentIndex(
            dlg._engine_combo.findData("bing")
        )
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

    def test_translate_missing_only_in_range_mode(self):
        dlg = self._dialog()
        dlg._chk_translate.setChecked(True)
        self.assertFalse(dlg.translate_missing())  # current mode
        dlg._rad_range.setChecked(True)
        self.assertTrue(dlg.translate_missing())

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
        dlg._to_spin.setValue(159)
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

    def test_radio_labels_are_visible(self):
        # Regressione: senza regola QSS i radio ereditavano testo scuro su
        # sfondo scuro (label invisibili).
        self.assertIn("QRadioButton", self.main._SETTINGS_QSS)

    def test_default_output_format_is_merged_pdf(self):
        dlg = self._dialog()
        self.assertFalse(dlg.chosen_zip())

    def test_zip_output_format_can_be_chosen(self):
        dlg = self._dialog()
        dlg._rad_zip.setChecked(True)
        self.assertTrue(dlg.chosen_zip())


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


if __name__ == "__main__":
    unittest.main()
