"""Test del pulsante flottante "Azioni pagina" (pannello destro).

Verifica che:
- il FAB sia nascosto all'avvio e compaia solo a traduzione completata;
- il menu elenchi le sei azioni, tradotte, e ognuna emetta il segnale giusto;
- "Traduci la successiva" sia disabilitabile sull'ultima pagina;
- i gestori di MainWindow (salva in Download, ritraduci, apri, cancella cache)
  facciano la cosa giusta.

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
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QPoint, QAbstractAnimation
    _HAS_QT = True
except ImportError:  # pragma: no cover
    _HAS_QT = False

import i18n  # noqa: E402

_FAB_KEYS = (
    "save_download", "export", "next", "retranslate", "open_external", "purge",
)


class _FakeEngine:
    """Engine minimale per i gestori del FAB."""

    def __init__(self, cached=True, export_ok=True):
        self._cached = cached
        self._export_ok = export_ok
        self.purged: list[tuple] = []
        self.exported: list[tuple] = []

    def is_cached(self, page, engine, lang_in=None, lang_out=None):
        return self._cached

    def export_pdf(self, pages, engine, dest, lang_in=None, lang_out=None):
        if not self._export_ok:
            raise OSError("boom")
        Path(dest).write_bytes(b"%PDF-1.4 fake")
        self.exported.append((list(pages), str(dest)))
        return len(list(pages))

    def purge_page_cache(self, engine, page):
        self.purged.append((engine, page))
        return (1, 1234)

    def translated_path(self, page, engine, lang_in=None, lang_out=None):
        return Path(f"/nonexistent/page_{page:06d}.pdf")


@unittest.skipUnless(_HAS_QT, "PyQt6 non disponibile")
class PageActionsFabTests(unittest.TestCase):
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
        self.panel = main.TranslatedPagePanel()
        self.panel.resize(400, 600)
        self.panel.show()
        self._app.processEvents()

    def tearDown(self):
        self.panel.close()
        i18n.set_language(self._prev_lang)
        self._tmp.cleanup()

    def test_fab_hidden_by_default(self):
        self.assertFalse(self.panel.is_page_actions_visible())
        self.assertTrue(self.panel._fab.isHidden())

    def test_show_and_hide_page_actions(self):
        self.panel.show_page_actions()
        self._app.processEvents()
        self.assertTrue(self.panel.is_page_actions_visible())
        self.assertFalse(self.panel._fab.isHidden())
        self.panel.hide_page_actions()
        self.assertFalse(self.panel.is_page_actions_visible())

    def test_translating_hides_fab(self):
        self.panel.show_page_actions()
        self.panel.show_translating(None, "…")
        self.assertFalse(self.panel.is_page_actions_visible())

    def test_fab_is_independent_from_the_bar(self):
        # Il pulsante Azioni è un overlay del pannello, non della barra: resta
        # visibile anche collassando la fascia motori.
        self.panel.show_page_actions()
        self._app.processEvents()
        fab = self.panel._fab
        self.assertIs(fab.parent(), self.panel)
        self.panel.set_collapsed(True)
        self._app.processEvents()
        self.assertTrue(self.panel.is_page_actions_visible())
        self.assertFalse(fab.isHidden())
        top_left = fab.mapTo(self.panel, QPoint(0, 0))
        self.assertLess(top_left.y(), self.panel.height() // 2)
        self.panel.set_collapsed(False)
        self._app.processEvents()

    def test_fab_visible_when_panel_is_small(self):
        self.panel.resize(360, 300)
        self.panel.show_page_actions()
        self._app.processEvents()
        self.assertTrue(self.panel.is_page_actions_visible())
        fab = self.panel._fab
        top_left = fab.mapTo(self.panel, QPoint(0, 0))
        self.assertGreaterEqual(top_left.x(), 0)
        self.assertLessEqual(top_left.y() + fab.height(), self.panel.height())

    def test_badge_visible_on_show_and_cleared_on_menu_open(self):
        self.panel.show_page_actions()
        self._app.processEvents()
        self.assertTrue(self.panel._fab_dot.isVisible())
        self.panel._toggle_page_actions()  # apre il menu
        self.assertFalse(self.panel._fab_dot.isVisible())
        self.assertFalse(self.panel._fab_badge_pending)
        self.panel._fab_menu.hide()

    def test_badge_stays_after_repositioning(self):
        self.panel.show_page_actions()
        self._app.processEvents()
        self.panel.resize(520, 700)
        self._app.processEvents()
        self.assertTrue(self.panel._fab_dot.isVisible())
        self.assertGreater(self.panel._fab_dot.x(), 0)

    def test_hide_clears_badge(self):
        self.panel.show_page_actions()
        self.panel.hide_page_actions()
        self.assertFalse(self.panel._fab_dot.isVisible())
        self.assertFalse(self.panel._fab_badge_pending)

    def test_fab_has_intermittent_glow(self):
        # Glow intermittente: due animazioni in loop (blur + colore).
        self.panel.show_page_actions()
        self._app.processEvents()
        self.assertIsNotNone(self.panel._fab.graphicsEffect())
        anims = self.panel._fab_glow_anims
        self.assertEqual(len(anims), 2)
        for anim in anims:
            self.assertEqual(anim.loopCount(), -1)  # loop infinito
            self.assertEqual(
                anim.state(), QAbstractAnimation.State.Running
            )
        self.panel.hide_page_actions()
        self.assertEqual(self.panel._fab_glow_anims, ())

    def test_fab_glow_color_follows_theme(self):
        import theme
        self.panel.show_page_actions()
        theme.set_mode("dark")
        self.panel.apply_theme()
        dark = self.panel._fab_glow_effect.color().rgb()
        theme.set_mode("light")
        self.panel.apply_theme()
        light = self.panel._fab_glow_effect.color().rgb()
        theme.set_mode("dark")
        self.panel.apply_theme()
        self.assertNotEqual(dark, light)
        self.panel.hide_page_actions()

    def test_menu_has_six_translated_actions(self):
        labels = [a.text() for a in self.panel._fab_menu.actions()]
        self.assertEqual(
            labels, [i18n.T(f"clone.fab.{k}") for k in _FAB_KEYS]
        )

    def test_actions_emit_their_signals(self):
        received = []
        pairs = (
            ("save_download", self.panel.page_save_download_requested),
            ("export", self.panel.export_requested),
            ("next", self.panel.page_translate_next_requested),
            ("retranslate", self.panel.page_retranslate_requested),
            ("open_external", self.panel.page_open_external_requested),
            ("purge", self.panel.page_purge_requested),
        )
        for key, sig in pairs:
            sig.connect(lambda k=key: received.append(k))
        for act in self.panel._fab_menu.actions():
            act.trigger()
        self.assertEqual(received, [k for k, _ in pairs])

    def test_next_action_can_be_disabled(self):
        next_act = self.panel._fab_actions["next"]
        self.panel.set_page_actions_next_enabled(False)
        self.assertFalse(next_act.isEnabled())
        self.panel.set_page_actions_next_enabled(True)
        self.assertTrue(next_act.isEnabled())

    def test_labels_follow_language(self):
        i18n.set_language("en")
        self.panel.retranslate()
        self.assertEqual(self.panel._fab.text(), i18n.T("clone.fab.title"))
        self.assertEqual(
            self.panel._fab_actions["purge"].text(), i18n.T("clone.fab.purge")
        )


@unittest.skipUnless(_HAS_QT, "PyQt6 non disponibile")
class PageActionsHandlersTests(unittest.TestCase):
    """I gestori di MainWindow dietro i segnali del FAB."""

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
        self.window._pdf_path = Path(self._tmp.name) / "doc.pdf"
        self.window._page_count = 3
        self.window._current_page = 0
        self.window._clone_engine = _FakeEngine()

    def tearDown(self):
        self.window.close()
        i18n.set_language(self._prev_lang)
        self._tmp.cleanup()

    def test_translate_next_advances_and_translates(self):
        calls = []
        self.window._set_page = lambda p: calls.append(("page", p))
        self.window._on_translate_requested = lambda: calls.append(("tr", None))
        self.window._on_fab_translate_next()
        self.assertEqual(calls, [("page", 1), ("tr", None)])

    def test_translate_next_on_last_page_warns(self):
        self.window._current_page = 2
        calls = []
        self.window._set_page = lambda p: calls.append(p)
        self.window._on_translate_requested = lambda: calls.append("tr")
        self.window._on_fab_translate_next()
        self.assertEqual(calls, [])
        self.assertIn(
            i18n.T("clone.fab.last_page"),
            self.window.status_bar.currentMessage(),
        )

    def test_save_download_writes_file(self):
        with mock.patch.object(
            self.main.QStandardPaths,
            "writableLocation",
            return_value=self._tmp.name,
        ):
            self.window._on_fab_save_download()
        self.assertEqual(len(self.window._clone_engine.exported), 1)
        pages, dest = self.window._clone_engine.exported[0]
        self.assertEqual(pages, [0])
        self.assertTrue(Path(dest).is_file())
        self.assertTrue(dest.endswith("_pag1_google_it.pdf"), dest)

    def test_save_download_without_cache_warns(self):
        self.window._clone_engine = _FakeEngine(cached=False)
        self.window._on_fab_save_download()
        self.assertEqual(self.window._clone_engine.exported, [])
        self.assertIn(
            i18n.T("export.not_ready"),
            self.window.status_bar.currentMessage(),
        )

    def test_purge_calls_engine_after_confirm(self):
        self.window._confirm_page_action = lambda *a, **k: True
        shown = []
        self.window._show_clone_for_page = lambda p: shown.append(p)
        self.window._on_fab_purge()
        self.assertEqual(
            self.window._clone_engine.purged, [("google", 0)]
        )
        self.assertEqual(shown, [0])

    def test_purge_aborts_without_confirm(self):
        self.window._confirm_page_action = lambda *a, **k: False
        self.window._on_fab_purge()
        self.assertEqual(self.window._clone_engine.purged, [])

    def test_retranslate_purges_then_translates(self):
        self.window._confirm_page_action = lambda *a, **k: True
        calls = []
        self.window._on_translate_requested = lambda: calls.append("tr")
        self.window._on_fab_retranslate()
        self.assertEqual(self.window._clone_engine.purged, [("google", 0)])
        self.assertEqual(calls, ["tr"])

    def test_open_external_without_cache_warns(self):
        self.window._on_fab_open_external()
        self.assertIn(
            i18n.T("export.not_ready"),
            self.window.status_bar.currentMessage(),
        )

    def test_open_external_opens_translated_file(self):
        target = Path(self._tmp.name) / "clone.pdf"
        target.write_bytes(b"%PDF-1.4 x")
        self.window._clone_engine.translated_path = lambda *a, **k: target
        with mock.patch.object(self.main, "QDesktopServices") as desktop:
            self.window._on_fab_open_external()
        self.assertTrue(desktop.openUrl.called)


if __name__ == "__main__":
    unittest.main()
