"""Test del suono d'avviso nativo (punto 3). Modulo puro: nessuna GUI."""

import os
import sys
import threading
import time
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import notifications  # noqa: E402


class ChimeTests(unittest.TestCase):
    def test_chime_disabled_does_not_play(self):
        called = []
        with mock.patch.object(
            notifications, "play_once", side_effect=lambda: called.append(1)
        ):
            notifications.chime(enabled=False)
            time.sleep(0.05)
        self.assertEqual(called, [])

    def test_chime_enabled_plays_in_background(self):
        done = threading.Event()
        with mock.patch.object(
            notifications, "play_once",
            side_effect=lambda: done.set(),
        ):
            notifications.chime(enabled=True)
            self.assertTrue(done.wait(2.0))

    def test_play_once_never_raises(self):
        # Nessuna piattaforma disponibile: deve degradare senza sollevare.
        with mock.patch.object(notifications, "_play_linux", side_effect=OSError):
            with mock.patch.object(notifications, "_play_macos", side_effect=OSError):
                with mock.patch.object(notifications, "_play_windows", side_effect=OSError):
                    with mock.patch.object(notifications.sys, "platform", "linux"):
                        self.assertFalse(notifications.play_once())

    def test_play_once_returns_true_when_native_works(self):
        with mock.patch.object(notifications, "_play_linux", return_value=True):
            with mock.patch.object(notifications.sys, "platform", "linux"):
                self.assertTrue(notifications.play_once())


class WavTests(unittest.TestCase):
    def test_build_wav_bytes_is_a_valid_wav(self):
        import io
        import wave

        data = notifications.build_wav_bytes()
        with wave.open(io.BytesIO(data), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), notifications._CHIME_RATE)
            self.assertGreater(wav.getnframes(), 0)

    def test_wav_path_is_written_once(self):
        first = notifications.wav_path()
        self.assertIsNotNone(first)
        self.assertTrue(os.path.exists(first))
        self.assertEqual(notifications.wav_path(), first)

    def test_linux_commands_prefer_pipewire(self):
        def which(name):
            return f"/usr/bin/{name}" if name in ("pw-play", "paplay") else None

        with mock.patch.object(notifications.shutil, "which", side_effect=which):
            commands = notifications._linux_commands("/tmp/x.wav")
        players = [c[0] for c in commands]
        self.assertIn("pw-play", players)
        self.assertLess(players.index("pw-play"), players.index("paplay"))


if __name__ == "__main__":
    unittest.main()
