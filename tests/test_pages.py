"""Test del parsing della specifica pagine (campo libero del wizard).

La semantica è allineata a ``app/pages.py`` del repository
``noesis-pdf-cloner-service``: indici 0-based, ordinati e senza duplicati,
estremi invertiti normalizzati, spazi ignorati, ``all``/``*`` = tutte.
In più verifichiamo i ``code`` di ``PageSpecError`` usati dalla UI per tradurre
il messaggio.
"""

import unittest

from pages import (
    PageSpecError,
    describe_pages,
    format_pages_label,
    parse_pages,
)


class ParsePagesTests(unittest.TestCase):
    def test_all_pages(self):
        self.assertEqual(parse_pages("all", 5), [0, 1, 2, 3, 4])
        self.assertEqual(parse_pages("", 3), [0, 1, 2])
        self.assertEqual(parse_pages(None, 3), [0, 1, 2])
        self.assertEqual(parse_pages("*", 2), [0, 1])

    def test_single_page(self):
        self.assertEqual(parse_pages("7", 10), [6])

    def test_range(self):
        self.assertEqual(parse_pages("100-103", 200), [99, 100, 101, 102])

    def test_range_reversed_is_normalised(self):
        self.assertEqual(parse_pages("103-100", 200), [99, 100, 101, 102])

    def test_list_with_spaces_and_duplicates(self):
        self.assertEqual(
            parse_pages(" 3, 5 , 10-12, 3 ", 20), [2, 4, 9, 10, 11]
        )

    def test_out_of_range_raises_with_bounds_code(self):
        for spec in ("11", "0", "1-11"):
            with self.assertRaises(PageSpecError) as ctx:
                parse_pages(spec, 10)
            self.assertEqual(ctx.exception.code, "bounds")
        # è comunque un ValueError (compatibilità col servizio)
        with self.assertRaises(ValueError):
            parse_pages("11", 10)

    def test_invalid_token_raises_with_codes(self):
        with self.assertRaises(PageSpecError) as ctx:
            parse_pages("abc", 10)
        self.assertEqual(ctx.exception.code, "token")
        with self.assertRaises(PageSpecError) as ctx:
            parse_pages("1-", 10)
        self.assertEqual(ctx.exception.code, "range")

    def test_no_document_pages(self):
        with self.assertRaises(PageSpecError) as ctx:
            parse_pages("1", 0)
        self.assertEqual(ctx.exception.code, "no_pages")

    def test_empty_token_code(self):
        with self.assertRaises(PageSpecError) as ctx:
            parse_pages("1,,2", 10)
        self.assertEqual(ctx.exception.code, "empty")

    def test_label_and_describe(self):
        self.assertEqual(format_pages_label([0]), "1")
        self.assertEqual(format_pages_label([99, 100, 101, 102]), "100-103")
        self.assertEqual(format_pages_label([2, 4, 9]), "3,5,10")
        self.assertEqual(format_pages_label([0, 2, 6, 7, 8]), "1,3,7-9")
        self.assertEqual(format_pages_label([]), "")
        self.assertEqual(describe_pages([155]), "pagina 156")
        self.assertIn("4 pagine", describe_pages([99, 100, 101, 102]))


if __name__ == "__main__":
    unittest.main()
