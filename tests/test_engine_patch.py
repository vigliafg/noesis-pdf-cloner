"""Tests for ``engine_patch`` (patch runtime sperimentali del motore).

Girano senza BabelDOC installato: le patch "reali" sono verificate con moduli
finti iniettati in ``sys.modules``.
"""

import os
import sys
import types
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import engine_patch  # noqa: E402


class _FakeMemoryMonitor:
    def __init__(self, *args, **kwargs):
        self.peak_memory_usage = 123

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_babeldoc_modules():
    """Costruisce moduli ``babeldoc`` finti per verificare le patch."""
    assets = types.ModuleType("babeldoc.assets.assets")
    calls = {"count": 0}

    def get_font_and_metadata(name):
        calls["count"] += 1
        return name, {"sha3_256": "x"}

    assets.get_font_and_metadata = get_font_and_metadata

    high_level = types.ModuleType("babeldoc.format.pdf.high_level")
    high_level.MemoryMonitor = _FakeMemoryMonitor

    modules = {
        "babeldoc": types.ModuleType("babeldoc"),
        "babeldoc.assets": types.ModuleType("babeldoc.assets"),
        "babeldoc.assets.assets": assets,
        "babeldoc.format": types.ModuleType("babeldoc.format"),
        "babeldoc.format.pdf": types.ModuleType("babeldoc.format.pdf"),
        "babeldoc.format.pdf.high_level": high_level,
    }
    return modules, assets, high_level, calls


class EnginePatchTests(unittest.TestCase):
    def setUp(self):
        engine_patch._STATE = None

    def tearDown(self):
        engine_patch._STATE = None

    def test_apply_all_is_safe_without_babeldoc(self):
        # Nessuna eccezione anche se BabelDOC non è installato.
        state = engine_patch.apply_all()
        self.assertEqual(
            set(state),
            {engine_patch.FONT_METADATA_CACHE, engine_patch.MEMORY_MONITOR},
        )
        self.assertTrue(all(isinstance(v, bool) for v in state.values()))

    def test_apply_all_is_idempotent(self):
        first = engine_patch.apply_all()
        second = engine_patch.apply_all()
        self.assertEqual(first, second)

    def test_apply_all_with_fake_babeldoc(self):
        modules, assets, high_level, calls = _fake_babeldoc_modules()
        with mock.patch.dict(sys.modules, modules):
            state = engine_patch.apply_all()

        self.assertTrue(state[engine_patch.FONT_METADATA_CACHE])
        self.assertTrue(state[engine_patch.MEMORY_MONITOR])

        # La memoizzazione evita il secondo calcolo (hash).
        assets.get_font_and_metadata("f1")
        assets.get_font_and_metadata("f1")
        self.assertEqual(calls["count"], 1)

        # Il MemoryMonitor è lo stub no-op.
        with high_level.MemoryMonitor() as monitor:
            self.assertEqual(monitor.peak_memory_usage, 0)

    def test_memory_monitor_stub_is_marked(self):
        modules, _assets, high_level, _calls = _fake_babeldoc_modules()
        with mock.patch.dict(sys.modules, modules):
            engine_patch.apply_all()
        self.assertTrue(getattr(high_level.MemoryMonitor, "_noesis_stub", False))

    def test_patch_version_is_exposed(self):
        self.assertIsInstance(engine_patch.PATCH_VERSION, str)
        self.assertTrue(engine_patch.PATCH_VERSION)


if __name__ == "__main__":
    unittest.main()
