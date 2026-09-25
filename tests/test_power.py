"""Test di gestione standby/risveglio (punto 4). Modulo puro: nessuna GUI."""

import os
import sys
import time
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import power  # noqa: E402


class SleepInhibitorTests(unittest.TestCase):
    def test_acquire_returns_false_without_systemd_inhibit(self):
        inh = power.SleepInhibitor()
        with mock.patch.object(power.sys, "platform", "linux"):
            with mock.patch.object(power.shutil, "which", return_value=None):
                self.assertFalse(inh.acquire())
                self.assertFalse(inh.active)
        # release su inattivo è innocuo
        inh.release()

    def test_acquire_and_release_with_fake_child(self):
        fake = mock.MagicMock()
        fake.wait.return_value = 0
        inh = power.SleepInhibitor()
        with mock.patch.object(power.sys, "platform", "linux"):
            with mock.patch.object(power.shutil, "which", return_value="/usr/bin/x"):
                with mock.patch.object(power.subprocess, "Popen", return_value=fake):
                    self.assertTrue(inh.acquire())
                    self.assertTrue(inh.active)
        inh.release()
        self.assertFalse(inh.active)
        fake.terminate.assert_called_once()

    def test_context_manager(self):
        inh = power.SleepInhibitor()
        with mock.patch.object(inh, "acquire", return_value=True) as acq:
            with mock.patch.object(inh, "release") as rel:
                with inh:
                    pass
        acq.assert_called_once()
        rel.assert_called_once()

    def test_no_window_kwargs_empty_on_linux(self):
        with mock.patch.object(power.sys, "platform", "linux"):
            self.assertEqual(power._no_window_kwargs(), {})


class ResumeWatchTests(unittest.TestCase):
    def test_poll_returns_zero_without_gap(self):
        watch = power.ResumeWatch(threshold_s=5.0)
        self.assertEqual(watch.poll(), 0.0)

    def test_poll_detects_gap(self):
        watch = power.ResumeWatch(threshold_s=5.0)
        # Simula una sospensione: l'orologio da parete avanza di 120 s mentre
        # il monotono resta indietro di 1 s.
        watch._wall = time.time() - 121.0
        watch._mono = time.monotonic() - 1.0
        gap = watch.poll()
        self.assertGreater(gap, 100.0)

    def test_reset_forgets_gap(self):
        watch = power.ResumeWatch(threshold_s=5.0)
        watch._wall = time.time() - 500.0
        watch._mono = time.monotonic()
        watch.reset()
        self.assertEqual(watch.poll(), 0.0)


if __name__ == "__main__":
    unittest.main()
