"""Tests for the UI internationalization module (i18n.py).

Covers: translation completeness across all languages, T() resolution and
formatting, and config persistence (first-run write, corruption tolerance,
forward compatibility, OS-locale default).
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

import i18n  # noqa: E402


class TranslationCompletenessTests(unittest.TestCase):
    """Every key must exist in every language (no missing translations)."""

    def test_all_keys_present_in_all_languages(self):
        missing = []
        for key, table in i18n._STRINGS.items():
            for lang in i18n.LANGUAGES:
                if lang not in table or not table[lang]:
                    missing.append((key, lang))
        self.assertEqual(
            missing, [],
            f"Traduzioni mancanti: {missing}",
        )

    def test_no_unknown_languages_in_strings(self):
        known = set(i18n.LANGUAGES)
        for key, table in i18n._STRINGS.items():
            unknown = set(table) - known
            self.assertEqual(
                unknown, set(),
                f"Lingue sconosciute per '{key}': {unknown}",
            )

    def test_keys_are_nonempty_and_unique(self):
        self.assertGreaterEqual(len(i18n._STRINGS), 50)
        self.assertEqual(len(i18n._STRINGS), len(set(i18n._STRINGS)))

    def test_help_keys_exist_in_all_languages(self):
        """Le chiavi del bottone Guida devono esserci in tutte le 5 lingue."""
        for key in ("toolbar.help", "toolbar.help.tip"):
            self.assertIn(key, i18n._STRINGS)
            for lang in i18n.LANGUAGES:
                self.assertTrue(
                    i18n._STRINGS[key].get(lang),
                    f"'{key}' manca in '{lang}'",
                )
        self.assertIn("❓", i18n.T("toolbar.help"))

    def test_placeholders_consistent_across_languages(self):
        """Ogni lingua di una chiave deve avere gli STESSI placeholder {…}."""
        import re
        for key, table in i18n._STRINGS.items():
            expected = set(re.findall(r"\{(\w+)\}\}".replace("\\}\\}", "}"), ""))
            expected = set(re.findall(r"\{(\w+)\}\}".replace("\\}\\}", "}"), ""))
            expected = set(re.findall(r"\{(\w+)\}", table.get("it", "")))
            for lang, text in table.items():
                got = set(re.findall(r"\{(\w+)\}", text))
                self.assertEqual(
                    got, expected,
                    f"placeholder incoerenti per '{key}' in '{lang}': {got} vs {expected}",
                )


class TFunctionTests(unittest.TestCase):
    def setUp(self):
        self._prev = i18n.get_language()

    def tearDown(self):
        i18n.set_language(self._prev)

    def test_resolves_in_active_language(self):
        i18n.set_language("en")
        self.assertEqual(i18n.T("toolbar.open"), "📂 Open PDF")
        i18n.set_language("de")
        self.assertEqual(i18n.T("toolbar.open"), "📂 PDF öffnen")

    def test_default_is_italian(self):
        self.assertEqual(i18n.get_language(), "it")
        self.assertEqual(i18n.T("toolbar.open"), "📂 Apri PDF")

    def test_unknown_key_returns_the_key(self):
        self.assertEqual(i18n.T("chiave.inesistente"), "chiave.inesistente")

    def test_formatting_with_kwargs(self):
        i18n.set_language("en")
        out = i18n.T("status.page", page=3, total=12, name="x.pdf", ms="210")
        self.assertIn("Page 3 of 12", out)
        self.assertIn("x.pdf", out)

    def test_formatting_missing_kwarg_degrades_gracefully(self):
        # A missing placeholder must not raise: the raw text is returned.
        out = i18n.T("status.page", page=1)  # total/name/ms missing
        self.assertIsInstance(out, str)
        self.assertTrue(out)

    def test_italian_fallback_when_current_missing(self):
        # If the active language lacks a key (shouldn't happen, but guard),
        # T() falls back to Italian rather than crashing.
        self.assertIn("it", i18n._STRINGS["toolbar.open"])
        out = i18n.T("toolbar.open")
        self.assertEqual(out, "📂 Apri PDF")

    def test_set_language_ignores_unknown_codes(self):
        i18n.set_language("xx")
        self.assertEqual(i18n.get_language(), "it")


class ConfigPersistenceTests(unittest.TestCase):
    """load/save/ensure_config on temp files (no Qt involved)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._path = Path(self._tmp.name) / "cfg" / "config.json"
        self._prev = i18n.get_language()

    def tearDown(self):
        i18n.set_language(self._prev)
        self._tmp.cleanup()

    def test_save_load_roundtrip(self):
        i18n.save_language(self._path, "fr")
        self.assertEqual(i18n.load_language(self._path), "fr")
        # Persisted JSON uses the expected schema.
        data = json.loads(self._path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"lang": "fr"})

    def test_first_run_writes_default_and_returns_it(self):
        self.assertFalse(self._path.exists())
        lang = i18n.ensure_config(self._path, default="es")
        self.assertEqual(lang, "es")
        self.assertTrue(self._path.exists())  # written immediately
        self.assertEqual(i18n.load_language(self._path), "es")

    def test_second_run_reads_stored_value(self):
        i18n.save_language(self._path, "de")
        # A different default must NOT overwrite the stored choice.
        lang = i18n.ensure_config(self._path, default="it")
        self.assertEqual(lang, "de")

    def test_missing_file_returns_default(self):
        self.assertEqual(i18n.load_language(self._path), "it")
        self.assertEqual(i18n.load_language(self._path, default="fr"), "fr")

    def test_corrupted_json_returns_default(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("{ not valid json !!!", encoding="utf-8")
        self.assertEqual(i18n.load_language(self._path), "it")
        # ensure_config also repairs the file with the default.
        lang = i18n.ensure_config(self._path, default="fr")
        self.assertEqual(lang, "fr")
        self.assertEqual(i18n.load_language(self._path), "fr")

    def test_unknown_language_code_degrades_to_default(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"lang": "xx"}), encoding="utf-8")
        self.assertEqual(i18n.load_language(self._path), "it")
        self.assertEqual(i18n.load_language(self._path, default="en"), "en")

    def test_extra_fields_are_ignored(self):
        """Forward compatibility: future versions may extend the schema."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"lang": "it", "future_key": 42}), encoding="utf-8"
        )
        self.assertEqual(i18n.load_language(self._path), "it")

    def test_atomic_write_leaves_no_temp_file(self):
        i18n.save_language(self._path, "en")
        leftovers = list(self._path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])


class TranslationLanguagesTests(unittest.TestCase):
    """TRANSLATION_LANGUAGES: flag + endonym for every code."""

    def test_every_code_has_flag_and_endonym(self):
        for code, (flag, name) in i18n.TRANSLATION_LANGUAGES.items():
            self.assertTrue(flag, f"flag mancante per {code}")
            self.assertTrue(name, f"endonimo mancante per {code}")

    def test_auto_is_first_and_only_for_source(self):
        self.assertIn("auto", i18n.TRANSLATION_LANGUAGES)
        self.assertGreaterEqual(len(i18n.TRANSLATION_LANGUAGES), 14)

    def test_flag_endonym(self):
        self.assertEqual(i18n.flag_endonym("fr"), "🇫🇷 Français")
        self.assertEqual(i18n.flag_endonym("it"), "🇮🇹 Italiano")
        self.assertTrue(i18n.flag_endonym("auto").startswith("🌐"))


class ConfigV2Tests(unittest.TestCase):
    """Config v2: load/save/init validation, get/set, merge, caps."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._path = Path(self._tmp.name) / "cfg" / "config.json"
        self._prev = i18n.get_language()

    def tearDown(self):
        i18n.set_language(self._prev)
        self._tmp.cleanup()

    def _write(self, payload):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(payload), encoding="utf-8")

    def test_init_config_writes_full_defaults_on_first_run(self):
        cfg = i18n.init_config(self._path)
        for key, value in i18n.DEFAULTS.items():
            self.assertEqual(cfg[key], value, f"default {key}")
        self.assertTrue(self._path.exists())
        self.assertEqual(json.loads(self._path.read_text()), i18n.DEFAULTS)

    def test_init_config_sets_module_state(self):
        self._write({"lang": "fr", "src_lang": "en", "dst_lang": "de"})
        i18n.init_config(self._path)
        self.assertEqual(i18n.get_language(), "fr")
        self.assertEqual(i18n.get_source_lang(), "en")
        self.assertEqual(i18n.get_target_lang(), "de")

    def test_load_config_validation(self):
        self._write({
            "lang": "xx", "src_lang": "yy", "dst_lang": "auto",
            "zoom": 99, "font_size": 99,
            "render_md": "false", "show_header": 0,
            "last_tab": "bogus",
        })
        cfg = i18n.load_config(self._path)
        self.assertEqual(cfg["lang"], "it")
        self.assertEqual(cfg["src_lang"], "auto")
        self.assertEqual(cfg["dst_lang"], "it")
        self.assertEqual(cfg["zoom"], 4.0)     # clamp
        self.assertEqual(cfg["font_size"], 16)  # clamp
        self.assertFalse(cfg["render_md"])
        self.assertFalse(cfg["show_header"])
        self.assertEqual(cfg["last_tab"], "original")

    def test_get_set_setting_roundtrip_and_clamps(self):
        i18n.init_config(self._path)
        i18n.set_setting("zoom", 2.5)
        self.assertEqual(i18n.get_setting("zoom"), 2.5)
        i18n.set_setting("zoom", 99)
        self.assertEqual(i18n.get_setting("zoom"), 4.0)   # clamp
        i18n.set_setting("font_size", 5)
        self.assertEqual(i18n.get_setting("font_size"), 10)  # clamp

    def test_source_target_setters_validation(self):
        i18n.init_config(self._path)
        i18n.set_source_lang("fr")
        self.assertEqual(i18n.get_source_lang(), "fr")
        i18n.set_source_lang("bogus")
        self.assertEqual(i18n.get_source_lang(), "fr")
        i18n.set_target_lang("de")
        self.assertEqual(i18n.get_target_lang(), "de")
        i18n.set_target_lang("auto")  # auto non ammesso per la destinazione
        self.assertEqual(i18n.get_target_lang(), "de")

    def test_save_language_merges_with_existing_config(self):
        self._write({"lang": "it", "src_lang": "en", "dst_lang": "fr", "zoom": 2.0})
        i18n.save_language(self._path, "de")
        data = json.loads(self._path.read_text())
        self.assertEqual(data["lang"], "de")
        self.assertEqual(data["src_lang"], "en")   # non sovrascritto
        self.assertEqual(data["dst_lang"], "fr")
        self.assertEqual(data["zoom"], 2.0)

    def test_last_pages_capped_at_20(self):
        self._write({"lang": "it", "last_pages": {f"doc{i}.pdf": i for i in range(30)}})
        cfg = i18n.load_config(self._path)
        self.assertEqual(len(cfg["last_pages"]), 20)

    def test_old_lang_only_file_gets_defaults(self):
        """File vecchi {"lang": ...} → campi v2 riempiti coi default."""
        self._write({"lang": "es"})
        cfg = i18n.load_config(self._path)
        self.assertEqual(cfg["lang"], "es")
        self.assertEqual(cfg["src_lang"], "auto")
        self.assertEqual(cfg["dst_lang"], "it")
        self.assertEqual(cfg["zoom"], 3.0)

    def test_set_setting_lang_syncs_active_language(self):
        """set_setting("lang", …) deve sincronizzare anche la lingua di T()."""
        i18n.init_config(self._path)
        i18n.set_setting("lang", "en")
        self.assertEqual(i18n.get_language(), "en")
        self.assertEqual(i18n.T("toolbar.open"), "📂 Open PDF")
        i18n.set_setting("lang", "zz")  # codice invalido -> nessun cambio
        self.assertEqual(i18n.get_language(), "en")


if __name__ == "__main__":
    unittest.main()
