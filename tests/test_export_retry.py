"""Test del retry degli errori transitori nell'export (punto 4).

Dopo un risveglio da standby (o un 429/timeout) la pagina non va data per
persa: ``CloneExportThread`` la ritenta un paio di volte. Gli errori
"definitivi" (es. chiave non valida) non vengono ritentati.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class RetryTests(unittest.TestCase):
    """Gli errori transitori (rete/timeout) vengono ritentati (punto 4)."""

    class _Engine:
        def __init__(self, first_status):
            self.calls = 0
            self._first = first_status
            self._status = "none"

        def translate_page(self, page, engine, cancel_event=None):
            self.calls += 1
            if self.calls == 1:
                self._status = self._first
                return None
            self._status = "done"
            return Path("/fake/page.pdf")

        def status(self, page, engine):
            return self._status

    def _thread(self, engine):
        import main

        return main.CloneExportThread(engine, [0], "google")

    def test_transient_error_is_retried(self):
        import main

        engine = self._Engine("error:network")
        thread = self._thread(engine)
        with mock.patch.object(main.time, "sleep", return_value=None):
            path, message = thread._translate_one(0)
        self.assertIsNotNone(path)
        self.assertEqual(engine.calls, 2)

    def test_permanent_error_is_not_retried(self):
        import main

        engine = self._Engine("error:invalid_key")
        thread = self._thread(engine)
        with mock.patch.object(main.time, "sleep", return_value=None):
            path, message = thread._translate_one(0)
        self.assertIsNone(path)
        self.assertEqual(message, "invalid_key")
        self.assertEqual(engine.calls, 1)


if __name__ == "__main__":
    unittest.main()
