"""Test dei ritocchi UI: riduzione a icona (punto 1), avvisi (punto 3),
blocco delle azioni durante il batch e inibizione standby (punto 4).

Gira headless (QT_QPA_PLATFORM=offscreen) e viene saltato se PyQt6 manca.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from PyQt6.QtWidgets import QApplication, QScrollArea
    from PyQt6.QtCore import Qt
    _HAS_QT = True
except ImportError:  # pragma: no cover
    _HAS_QT = False

import i18n  # noqa: E402


class _DummyEngine:
    def is_cached(self, *args, **kwargs):
        return False

    def cached_pages(self, *args, **kwargs):
        return []

    def available(self):
        return True


@unittest.skipUnless(_HAS_QT, "PyQt6 non disponibile")
class DialogWindowTests(unittest.TestCase):
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

    def test_wizard_has_minimize_button(self):
        dlg = self.main.ExportWizardDialog(
            _DummyEngine(), "google", "it", "en", 0, 3, None
        )
        self.assertTrue(
            dlg.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint
        )
        dlg.deleteLater()

    def test_progress_dialog_has_minimize_button(self):
        dlg = self.main.ExportProgressDialog(
            engine_label="Google",
            lang_label="Italiano",
            page_from=1,
            page_to=3,
            missing=2,
            cached=1,
            total=2,
        )
        self.assertTrue(
            dlg.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint
        )
        dlg.deleteLater()


@unittest.skipUnless(_HAS_QT, "PyQt6 non disponibile")
class SettingsDialogLayoutTests(unittest.TestCase):
    """Il dialog Impostazioni non deve comprimere i controlli."""

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

    def test_settings_content_scrolls_and_controls_keep_height(self):
        dlg = self.main.SettingsDialog()
        # Forza una finestra bassa: prima i controlli venivano schiacciati.
        dlg.resize(580, 360)
        dlg.show()
        self._app.processEvents()

        scroll = dlg.findChild(QScrollArea, "settingsScroll")
        self.assertIsNotNone(scroll)
        content = scroll.widget()
        self.assertIsNotNone(content)
        # Il contenuto è più alto del viewport: serve la scrollbar, non la
        # compressione dei widget.
        self.assertGreater(
            content.sizeHint().height(), scroll.viewport().height()
        )
        for combo in (dlg._ui_combo, dlg._engine_combo, dlg._theme_combo):
            self.assertGreaterEqual(
                combo.height(), combo.sizeHint().height() - 1
            )
        dlg.close()

    def test_test_sound_button_plays_chime(self):
        dlg = self.main.SettingsDialog()
        with mock.patch.object(self.main.notifications, "chime") as chime:
            dlg._btn_test_sound.click()
        chime.assert_called_once_with(True)
        dlg.close()


@unittest.skipUnless(_HAS_QT, "PyQt6 non disponibile")
class MainWindowRefinementsTests(unittest.TestCase):
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
        self.window = main.MainWindow()

    def tearDown(self):
        self.window.close()
        i18n.set_language(self._prev_lang)
        self._tmp.cleanup()

    def test_start_and_end_long_job(self):
        self.window._sleep_inhibitor = mock.MagicMock()
        i18n.set_setting("prevent_sleep", True)
        self.window._start_long_job()
        self.assertTrue(self.window._batch_busy)
        self.window._sleep_inhibitor.acquire.assert_called_once()
        self.window._end_long_job()
        self.assertFalse(self.window._batch_busy)
        self.window._sleep_inhibitor.release.assert_called_once()

    def test_start_long_job_respects_prevent_sleep_off(self):
        self.window._sleep_inhibitor = mock.MagicMock()
        i18n.set_setting("prevent_sleep", False)
        self.window._start_long_job()
        self.window._sleep_inhibitor.acquire.assert_not_called()
        self.window._end_long_job()

    def test_export_busy_guard(self):
        self.window._batch_busy = True
        self.window._pdf_path = Path(self._tmp.name) / "doc.pdf"
        self.window._mupdf_doc = mock.MagicMock()
        self.window._page_count = 3
        with mock.patch.object(
            self.main.QDialog, "exec"
        ) as dlg_exec:
            self.window._on_export_translated()
        dlg_exec.assert_not_called()
        self.assertIn(
            i18n.T("export.busy"), self.window.status_bar.currentMessage()
        )

    def test_translate_busy_guard(self):
        self.window._batch_busy = True
        self.window._pdf_path = Path(self._tmp.name) / "doc.pdf"
        self.window._mupdf_doc = mock.MagicMock()
        self.window._page_count = 3
        with mock.patch.object(self.window, "_request_translation") as req:
            self.window._on_translate_requested()
        req.assert_not_called()
        self.assertIn(
            i18n.T("export.busy"), self.window.status_bar.currentMessage()
        )

    def test_notify_respects_disabled(self):
        i18n.set_setting("notify_on_finish", False)
        with mock.patch.object(self.main.notifications, "chime") as chime:
            self.window._notify_batch("ciao")
        chime.assert_not_called()

    def test_notify_plays_sound_and_flashes(self):
        i18n.set_setting("notify_on_finish", True)
        i18n.set_setting("notify_sound", True)
        with mock.patch.object(self.main.notifications, "chime") as chime:
            with mock.patch.object(self.main.QApplication, "alert") as alert:
                self.window._notify_batch("fatto")
        chime.assert_called_once_with(True)
        alert.assert_called_once()

    def test_handle_resume_message_when_busy(self):
        self.window._batch_busy = True
        self.window._handle_resume(120.0)
        self.assertIn(
            i18n.T("power.resumed"), self.window.status_bar.currentMessage()
        )

    def test_notify_page_done_only_when_not_active(self):
        with mock.patch.object(
            self.window, "isActiveWindow", return_value=True
        ), mock.patch.object(
            self.window, "isMinimized", return_value=False
        ), mock.patch.object(self.window, "_notify_batch") as notify:
            self.window._notify_page_done(0)
        notify.assert_not_called()

        with mock.patch.object(
            self.window, "isActiveWindow", return_value=False
        ), mock.patch.object(
            self.window, "isMinimized", return_value=True
        ), mock.patch.object(self.window, "_notify_batch") as notify:
            self.window._notify_page_done(4)
        notify.assert_called_once()

    def test_clone_done_shows_page_actions(self):
        self.window._clone_generation = 7
        self.window._current_page = 0
        self.window._page_count = 3
        self.window._clone_engine = mock.MagicMock()
        self.window._clone_engine.status.return_value = "done"
        i18n.set_translation_engine("google")
        with mock.patch.object(
            self.window, "_display_translated_page"
        ), mock.patch.object(
            self.window, "_update_working_badge"
        ), mock.patch.object(
            self.window, "_notify_page_done"
        ), mock.patch.object(
            self.window.translated_panel, "show_page_actions"
        ) as show_actions:
            self.window._on_clone_done(7, 0, "google", "/tmp/x.pdf")
        show_actions.assert_called_once()

    def test_apply_theme_sets_stylesheet(self):
        self.main.theme.set_mode("light")
        self.window.apply_theme()
        qss = self.window.styleSheet()
        self.assertIn(self.main.theme.LIGHT["bg"], qss)
        self.main.theme.set_mode("dark")
        self.window.apply_theme()

    def test_tray_absent_when_unavailable(self):
        with mock.patch.object(
            self.main.QSystemTrayIcon, "isSystemTrayAvailable",
            return_value=False,
        ):
            self.window._tray = None
            self.window._setup_tray()
        self.assertIsNone(self.window._tray)


if __name__ == "__main__":
    unittest.main()
