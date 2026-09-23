"""Test della CLI gratuita (``gtranslate_cli``): stdio sempre UTF-8.

Riproduce il caso Windows/cp1252: pdf2zh_next esegue lo script come subprocess
con ``encoding="utf-8"``, quindi l'output deve essere UTF-8 valido (altrimenti
le accentate diventano U+FFFD «�»).
"""

import io
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import gtranslate_cli as g  # noqa: E402

ACCENTED = "città è però già perché"


class StdioUtf8Tests(unittest.TestCase):
    def test_force_utf8_reconfigures_all_streams(self):
        class _Stream:
            def __init__(self):
                self.encoding = None

            def reconfigure(self, **kwargs):
                self.encoding = kwargs.get("encoding")

        stream = _Stream()
        saved = (sys.stdin, sys.stdout, sys.stderr)
        sys.stdin = sys.stdout = sys.stderr = stream
        try:
            g._force_utf8_stdio()
        finally:
            sys.stdin, sys.stdout, sys.stderr = saved
        self.assertEqual(stream.encoding, "utf-8")

    def test_output_is_utf8_even_with_cp1252_stdout(self):
        raw_out = io.BytesIO()
        stdout = io.TextIOWrapper(raw_out, encoding="cp1252")
        stdin = io.TextIOWrapper(io.BytesIO(b"Hello world"), encoding="cp1252")
        saved = (sys.stdin, sys.stdout, sys.stderr)
        sys.stdin, sys.stdout, sys.stderr = stdin, stdout, io.StringIO()
        try:
            with mock.patch.object(g, "translate", return_value=ACCENTED):
                code = g.main()
        finally:
            sys.stdin, sys.stdout, sys.stderr = saved
        self.assertEqual(code, 0)
        text = raw_out.getvalue().decode("utf-8")
        self.assertEqual(text.strip(), ACCENTED)
        self.assertNotIn("\ufffd", text)

    def test_input_is_decoded_as_utf8_with_cp1252_stdin(self):
        stdin = io.TextIOWrapper(
            io.BytesIO(ACCENTED.encode("utf-8")), encoding="cp1252"
        )
        raw_out = io.BytesIO()
        stdout = io.TextIOWrapper(raw_out, encoding="cp1252")
        seen = {}

        def fake(text):
            seen["text"] = text
            return text

        saved = (sys.stdin, sys.stdout, sys.stderr)
        sys.stdin, sys.stdout, sys.stderr = stdin, stdout, io.StringIO()
        try:
            with mock.patch.object(g, "translate", side_effect=fake):
                g.main()
        finally:
            sys.stdin, sys.stdout, sys.stderr = saved
        self.assertEqual(seen["text"], ACCENTED)
        self.assertEqual(raw_out.getvalue().decode("utf-8").strip(), ACCENTED)

    def test_empty_input_emits_just_newline(self):
        raw_out = io.BytesIO()
        stdout = io.TextIOWrapper(raw_out, encoding="cp1252")
        stdin = io.TextIOWrapper(io.BytesIO(b"   \n"), encoding="cp1252")
        saved = (sys.stdin, sys.stdout, sys.stderr)
        sys.stdin, sys.stdout, sys.stderr = stdin, stdout, io.StringIO()
        try:
            code = g.main()
        finally:
            sys.stdin, sys.stdout, sys.stderr = saved
        self.assertEqual(code, 0)
        self.assertEqual(raw_out.getvalue(), b"\n")


if __name__ == "__main__":
    unittest.main()
