"""Tests for the collapsible engine bar of the right panel (Option A).

Verifica che:
- il collasso nasconda la barra e restituisca spazio al viewport;
- il glifo/tooltip del pulsante flottante cambi;
- lo stato sia persistito nel config.

Gira headless (QT_QPA_PLATFORM=offscreen) e viene saltato se PyQt6 manca.
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

try:
    from PyQt6.QtWidgets import QApplication
    _HAS_QT = True
except ImportError:  # pragma: no cover
    _HAS_QT = False

import i18n  # noqa: E402


@unittest.skipUnless(_HAS_QT, "PyQt6 non disponibile")
class CollapsibleBarTests(unittest.TestCase):
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
        self.panel = main.TranslatedPagePanel()
        self.panel.resize(400, 600)
        self.panel.show()
        self._app.processEvents()

    def tearDown(self):
        self.panel.close()
        i18n.set_language(self._prev_lang)
        self._tmp.cleanup()

    def test_starts_expanded(self):
        self.assertFalse(self.panel.is_collapsed())
        self.assertFalse(self.panel._bar.isHidden())

    def test_collapse_hides_bar_and_returns_space(self):
        h_expanded = self.panel.scroll_area.height()
        self.panel.set_collapsed(True)
        self._app.processEvents()
        h_collapsed = self.panel.scroll_area.height()
        self.assertTrue(self.panel.is_collapsed())
        self.assertTrue(self.panel._bar.isHidden())
        self.assertGreater(h_collapsed, h_expanded)
        self.assertEqual(self.panel._collapse_btn.text(), "▸")

    def test_expand_restores_bar(self):
        self.panel.set_collapsed(True)
        self.panel.set_collapsed(False)
        self._app.processEvents()
        self.assertFalse(self.panel._bar.isHidden())
        self.assertEqual(self.panel._collapse_btn.text(), "▾")

    def test_state_is_persisted(self):
        self.panel.set_collapsed(True)
        self.assertTrue(bool(i18n.get_setting("clone_bar_collapsed")))
        self.panel.set_collapsed(False)
        self.assertFalse(bool(i18n.get_setting("clone_bar_collapsed")))

    def test_tooltip_is_translated(self):
        self.panel.set_collapsed(True)
        self.assertEqual(self.panel._collapse_btn.toolTip(), i18n.T("clone.bar.expand"))
        self.panel.set_collapsed(False)
        self.assertEqual(self.panel._collapse_btn.toolTip(), i18n.T("clone.bar.collapse"))

    def test_export_menu_actions_emit_signals(self):
        received = []
        self.panel.export_requested.connect(lambda: received.append("wizard"))
        self.panel.export_current_requested.connect(
            lambda: received.append("current")
        )
        actions = self.panel.btn_export.menu().actions()
        self.assertEqual(len(actions), 2)
        actions[0].trigger()
        actions[1].trigger()
        self.assertEqual(received, ["wizard", "current"])

    def test_export_tooltip_is_translated(self):
        self.assertEqual(self.panel.btn_export.toolTip(), i18n.T("clone.export.tip"))

    def test_translate_button_emits_request(self):
        received = []
        self.panel.translate_requested.connect(lambda: received.append(True))
        self.panel.btn_translate.click()
        self.assertEqual(received, [True])

    def test_translate_button_is_translated(self):
        self.assertIn(i18n.T("clone.translate"), self.panel.btn_translate.text())
        self.assertEqual(
            self.panel.btn_translate.toolTip(), i18n.T("clone.translate.tip")
        )

    def test_pending_shows_hint_over_original(self):
        self.panel.show_pending(None, i18n.T("clone.pending_page"))
        self.assertTrue(self.panel._spinner.isVisible())
        self.assertEqual(self.panel._spinner.text(), i18n.T("clone.pending_page"))
        self.assertEqual(self.panel._lbl_status.text(), i18n.T("clone.status_todo"))


if __name__ == "__main__":
    unittest.main()
