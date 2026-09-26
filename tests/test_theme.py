"""Test del tema UI (chiaro/scuro/sistema) — punto 5.

Modulo puro: non serve PyQt. Verifica che le due tavolozze siano complete e
coerenti, che la risoluzione di ``system`` funzioni e che i tre fogli di stile
si costruiscano senza errori e cambino davvero tra chiaro e scuro.
"""

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import theme  # noqa: E402


class PaletteTests(unittest.TestCase):
    def test_two_palettes_with_same_tokens(self):
        self.assertEqual(set(theme.DARK), set(theme.LIGHT))

    def test_normalize_mode(self):
        self.assertEqual(theme.normalize_mode("light"), "light")
        self.assertEqual(theme.normalize_mode("SYSTEM"), "system")
        self.assertEqual(theme.normalize_mode(""), "dark")
        self.assertEqual(theme.normalize_mode("nope"), "dark")

    def test_mode_and_resolved(self):
        theme.set_mode("light")
        self.assertEqual(theme.mode(), "light")
        self.assertEqual(theme.resolved(), "light")
        self.assertFalse(theme.is_dark())
        theme.set_mode("system")
        # system conserva la richiesta; il risolto resta l'ultimo effettivo.
        self.assertEqual(theme.mode(), "system")
        self.assertEqual(theme.resolved(), "light")
        theme.set_resolved("dark")
        self.assertEqual(theme.resolved(), "dark")
        self.assertTrue(theme.is_dark())
        theme.set_resolved("bogus")
        self.assertEqual(theme.resolved(), "dark")

    def test_color_fallback(self):
        theme.set_mode("dark")
        self.assertEqual(theme.color("bg"), theme.DARK["bg"])
        self.assertEqual(theme.color("does-not-exist", "#123456"), "#123456")

    def test_menu_bevel_tokens_give_depth(self):
        # Il menu del FAB usa gradiente + bordo a rilievo: i token devono
        # esistere in entrambe le tavolozze e differire tra loro.
        for pal in (theme.DARK, theme.LIGHT):
            self.assertNotEqual(pal["menu_bg_top"], pal["menu_bg_bottom"])
            self.assertNotEqual(pal["menu_border_hi"], pal["menu_border_lo"])


class QssTests(unittest.TestCase):
    def tearDown(self):
        theme.set_mode("dark")

    def test_qss_are_built_for_both_themes(self):
        for mode in ("dark", "light"):
            theme.set_mode(mode)
            main = theme.main_qss()
            settings = theme.settings_qss()
            wizard = theme.wizard_qss()
            self.assertIn("QMainWindow", main)
            self.assertIn("QScrollBar::handle:vertical", main)
            self.assertIn("QGroupBox", settings)
            self.assertIn("QRadioButton", settings)
            self.assertIn("wizStep", wizard)
            self.assertIn("wizPrimary", wizard)
            # Nessun token non sostituito (Template.substitute solleva se manca,
            # ma difendiamoci anche da un "$" residuo).
            for qss in (main, settings, wizard):
                self.assertNotIn("$", qss)

    def test_themes_differ(self):
        theme.set_mode("dark")
        dark = theme.main_qss()
        theme.set_mode("light")
        light = theme.main_qss()
        self.assertNotEqual(dark, light)
        self.assertIn(theme.LIGHT["bg"], light)
        self.assertNotIn(theme.LIGHT["bg"], dark)


if __name__ == "__main__":
    unittest.main()
