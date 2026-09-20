"""Tests for the generic layout fixes (columns, title, markdown tables).

These tests exercise the pure helpers (``_spacing_fixes``,
``_detect_column_split``, ``_block_to_md``) directly, and the page-level
functions (``_table_to_md``, ``_column_aware_markdown``) against small
synthetic PDF pages built with pymupdf.  A regression test against the real
``harrison2025.pdf`` is included but skipped when the file is absent.

Run with (from the project root):

    .venv/bin/python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

# Make the single-file `main` module importable from the project root.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pymupdf  # noqa: E402
import markdown  # noqa: E402

import main  # noqa: E402
from main import (  # noqa: E402
    _MD_EXTENSIONS,
    _block_to_md,
    _collect_blocks,
    _column_aware_markdown,
    _detect_column_split,
    _detect_column_splits,
    _inclusion_order_markdown,
    _page_needs_column_reorder,
    _spacing_fixes,
    _split_cross_column_paragraphs,
    _table_to_md,
)

PAGE_W = 595
PAGE_H = 842

# The two text columns used across synthetic fixtures.
LEFT = (50, 250)
RIGHT = (320, 520)

# The real corpus PDFs live in the parent repo (noesis-pdf-reader/); fall back
# to this project's own directory so the tests still run if they are copied here.
_PARENT_REPO = os.path.normpath(os.path.join(_ROOT, "..", "noesis-pdf-reader"))


def _corpus_pdf(name: str) -> str:
    here = os.path.join(_ROOT, name)
    there = os.path.join(_PARENT_REPO, name)
    return here if os.path.exists(here) else there


REAL_PDF = _corpus_pdf("harrison2025.pdf")
REAL_CECIL_PDF = _corpus_pdf("cecil2024.pdf")
REAL_ROSEN_PDF = _corpus_pdf("rosen2022.pdf")
REAL_DAVID_PDF = _corpus_pdf("david2027.pdf")


def _new_page(width=PAGE_W, height=PAGE_H):
    """Return a fresh (document, page) pair backed by an in-memory PDF."""
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    return doc, page


def _add_table(page, rect, rows, fontsize=9):
    """Draw a grid + its cell text so ``find_tables`` detects a real table."""
    x0, y0, x1, y1 = rect
    nrows, ncols = len(rows), len(rows[0])
    col_w = (x1 - x0) / ncols
    row_h = (y1 - y0) / nrows
    page.draw_rect(pymupdf.Rect(x0, y0, x1, y1), color=(0, 0, 0), width=1)
    for c in range(1, ncols):
        page.draw_line(
            pymupdf.Point(x0 + c * col_w, y0),
            pymupdf.Point(x0 + c * col_w, y1),
            color=(0, 0, 0),
            width=1,
        )
    for r in range(1, nrows):
        page.draw_line(
            pymupdf.Point(x0, y0 + r * row_h),
            pymupdf.Point(x1, y0 + r * row_h),
            color=(0, 0, 0),
            width=1,
        )
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            cell_rect = pymupdf.Rect(
                x0 + c * col_w + 3,
                y0 + r * row_h + 3,
                x0 + (c + 1) * col_w - 3,
                y0 + (r + 1) * row_h - 3,
            )
            page.insert_textbox(cell_rect, str(cell), fontsize=fontsize)
    return page


def _add_captioned_table(page, rect, caption, header, data_rows, fontsize=9):
    """Draw a table whose first (full-width) row is a caption."""
    x0, y0, x1, y1 = rect
    ncols = len(header)
    # Rows: caption (0), header (1), then one per data row.
    nrows = 2 + len(data_rows)
    row_h = (y1 - y0) / nrows
    row_ys = [y0 + i * row_h for i in range(nrows + 1)]
    page.draw_rect(pymupdf.Rect(x0, y0, x1, y1), color=(0, 0, 0), width=1)
    for y in row_ys[1:-1]:
        page.draw_line(pymupdf.Point(x0, y), pymupdf.Point(x1, y), color=(0, 0, 0), width=1)
    # Vertical rules only below the caption row.
    col_w = (x1 - x0) / ncols
    for c in range(1, ncols):
        page.draw_line(
            pymupdf.Point(x0 + c * col_w, row_ys[1]),
            pymupdf.Point(x0 + c * col_w, y1),
            color=(0, 0, 0),
            width=1,
        )
    page.insert_textbox(
        pymupdf.Rect(x0 + 3, row_ys[0] + 3, x1 - 3, row_ys[1] - 3),
        caption,
        fontsize=fontsize,
    )
    for c, h in enumerate(header):
        page.insert_textbox(
            pymupdf.Rect(x0 + c * col_w + 3, row_ys[1] + 3, x0 + (c + 1) * col_w - 3, row_ys[2] - 3),
            str(h),
            fontsize=fontsize,
        )
    for r, row in enumerate(data_rows):
        for c, cell in enumerate(row):
            page.insert_textbox(
                pymupdf.Rect(
                    x0 + c * col_w + 3,
                    row_ys[2 + r] + 3,
                    x0 + (c + 1) * col_w - 3,
                    row_ys[3 + r] - 3,
                ),
                str(cell),
                fontsize=fontsize,
            )
    return page


class SpacingFixesTests(unittest.TestCase):
    def test_bold_cross_reference_glued_to_word(self):
        self.assertEqual(_spacing_fixes("**134**and pathogen"), "**134** and pathogen")

    def test_italic_comma_glued_to_next_word(self):
        self.assertEqual(
            _spacing_fixes("_S. aureus_,_Streptococcus_"),
            "_S. aureus_, _Streptococcus_",
        )

    def test_clean_text_is_unchanged(self):
        text = "Normal **bold** text with spaces, and _italics_."
        self.assertEqual(_spacing_fixes(text), text)


class DetectColumnSplitTests(unittest.TestCase):
    @staticmethod
    def _block(x0, x1):
        return {"x0": x0, "y0": 0, "x1": x1, "y1": 10, "max_size": 10.0, "lines": []}

    def test_two_columns_detected(self):
        blocks = [
            self._block(50, 200),
            self._block(55, 190),
            self._block(320, 470),
            self._block(330, 460),
        ]
        split = _detect_column_split(blocks, PAGE_W)
        self.assertIsNotNone(split)
        self.assertTrue(190 < split < 320)

    def test_single_column_returns_none(self):
        blocks = [self._block(50, 200), self._block(55, 190)]
        self.assertIsNone(_detect_column_split(blocks, PAGE_W))

    def test_four_columns_return_three_splits(self):
        # A four-column index page: three column boundaries.
        blocks = [
            self._block(48, 173), self._block(50, 170),
            self._block(190, 319), self._block(195, 315),
            self._block(332, 458), self._block(335, 455),
            self._block(473, 600), self._block(475, 598),
        ]
        splits = _detect_column_splits(blocks, PAGE_W)
        self.assertEqual(len(splits), 3)
        self.assertLess(splits[0], splits[1])
        self.assertLess(splits[1], splits[2])
        # Boundaries sit in the gaps between the columns.
        self.assertTrue(173 < splits[0] < 190)
        self.assertTrue(319 < splits[1] < 332)
        self.assertTrue(458 < splits[2] < 473)

    def test_margin_label_is_not_a_column(self):
        # A narrow side label next to two real columns must not produce a split
        # before the left column (the widest-gap wrapper returns the real one).
        blocks = [
            self._block(14, 29),   # narrow vertical margin label
            self._block(39, 295), self._block(45, 290),
            self._block(304, 560), self._block(310, 555),
        ]
        self.assertEqual(_detect_column_split(blocks, PAGE_W), (295 + 304) / 2)
        splits = _detect_column_splits(blocks, PAGE_W)
        self.assertEqual(len(splits), 1)


class PageNeedsColumnReorderTests(unittest.TestCase):
    def test_two_column_side_by_side(self):
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Left one.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(50, 220, 250, 300), "Left two.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 120, 520, 200), "Right one.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 220, 520, 300), "Right two.", fontsize=10)
        self.assertTrue(_page_needs_column_reorder(page))
        doc.close()

    def test_single_column(self):
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Only left.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(50, 220, 250, 300), "More left.", fontsize=10)
        self.assertFalse(_page_needs_column_reorder(page))
        doc.close()

    def test_columns_without_vertical_overlap(self):
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "L1", fontsize=10)
        page.insert_textbox(pymupdf.Rect(50, 220, 250, 300), "L2", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 500, 520, 580), "R1", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 600, 520, 680), "R2", fontsize=10)
        self.assertFalse(_page_needs_column_reorder(page))
        doc.close()

    def test_watermark_header_does_not_bridge_columns(self):
        # A decorative full-ish-width banner in the top margin that spans the
        # gap between the two columns (e.g. "Made with Xodo") must not merge
        # them into one column — otherwise the page stays single-column and the
        # columns get interleaved.
        doc, page = _new_page()
        page.insert_textbox(
            pymupdf.Rect(180, 5, 430, 20),
            "Made with Xodo PDF Reader and Editor",
            fontsize=14,
        )
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Left one.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(50, 220, 250, 300), "Left two.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 120, 520, 200), "Right one.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 220, 520, 300), "Right two.", fontsize=10)
        self.assertTrue(_page_needs_column_reorder(page))
        doc.close()


class InclusionOrderMarkdownTests(unittest.TestCase):
    def test_zones_emitted_in_numbered_order(self):
        doc, page = _new_page()
        # "Bottom" è più in basso ma nel box 1; "Top" è più in alto ma nel
        # box 2: è il NUMERO del box a dettare l'ordine, non la posizione.
        page.insert_textbox(pymupdf.Rect(50, 500, 250, 580), "Bottom text.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Top text.", fontsize=10)
        zone_bottom = (40, 450, 260, 620)   # box 1
        zone_top = (40, 80, 260, 260)       # box 2
        out12 = _inclusion_order_markdown(page, (zone_bottom, zone_top))
        out21 = _inclusion_order_markdown(page, (zone_top, zone_bottom))
        self.assertLess(out12.index("Bottom"), out12.index("Top"))
        self.assertLess(out21.index("Top"), out21.index("Bottom"))
        doc.close()

    def test_whitelist_drops_text_outside_zones(self):
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Keep me.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 400, 520, 480), "Drop me.", fontsize=10)
        out = _inclusion_order_markdown(page, ((40, 100, 260, 260),))
        self.assertIn("Keep me.", out)
        self.assertNotIn("Drop me.", out)
        doc.close()

    def test_exclusion_wins_over_inclusion(self):
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Noise.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(50, 240, 250, 320), "Content.", fontsize=10)
        out = _inclusion_order_markdown(
            page, ((40, 100, 260, 360),), exclude=((40, 100, 260, 220),)
        )
        self.assertNotIn("Noise.", out)
        self.assertIn("Content.", out)
        doc.close()

    def test_small_zone_captures_only_its_lines(self):
        # Un box piccolo (2-3 righe) dentro un paragrafo più grande deve
        # includere SOLO quelle righe, non l'intero paragrafo.
        doc, page = _new_page()
        para = (
            "Prima frase. Seconda frase. Terza frase. Quarta frase. Quinta "
            "frase. Sesta frase. Settima frase. Ottava frase. Nona frase. "
            "Decima frase. Undicesima frase. Dodicesima frase. Tredicesima "
            "frase. Quattordicesima frase."
        )
        page.insert_textbox(pymupdf.Rect(50, 100, 300, 400), para, fontsize=11)
        out = _inclusion_order_markdown(page, ((40, 95, 320, 132),))
        self.assertIn("Prima frase", out)
        self.assertNotIn("Quattordicesima", out)  # righe fuori dal box escluse
        doc.close()


class BlockToMdTests(unittest.TestCase):
    @staticmethod
    def _block(size, lines):
        return {"x0": 0, "y0": 0, "x1": 100, "y1": 10, "max_size": size, "lines": lines}

    def test_heading_levels(self):
        b = self._block(16, [[{"text": "Chapter One", "size": 16, "bold": False, "italic": False}]])
        self.assertEqual(_block_to_md(b, as_column=True), "# Chapter One")

        b = self._block(12, [[{"text": "Section", "size": 12, "bold": False, "italic": False}]])
        self.assertEqual(_block_to_md(b, as_column=True), "## Section")

    def test_bold_and_italic_spans(self):
        b = self._block(
            10,
            [[
                {"text": "a", "size": 10, "bold": True, "italic": False},
                {"text": " ", "size": 10, "bold": False, "italic": False},
                {"text": "b", "size": 10, "bold": False, "italic": True},
            ]],
        )
        self.assertEqual(_block_to_md(b, as_column=False), "**a** *b*")


class TableToMdTests(unittest.TestCase):
    def test_table_rendered_as_markdown(self):
        doc, page = _new_page()
        _add_table(
            page,
            (50, 80, 300, 160),
            [["Name", "Value"], ["Alpha", "42"], ["Beta", "7"]],
        )
        tabs = page.find_tables()
        self.assertEqual(len(tabs.tables), 1)
        md = _table_to_md(page, tabs.tables[0])
        self.assertIn("| Name | Value |", md)
        self.assertIn("| --- | --- |", md)
        self.assertIn("| Alpha | 42 |", md)
        self.assertIn("| Beta | 7 |", md)
        doc.close()

    def test_caption_keeps_blank_line_so_table_renders(self):
        # Regression: a caption glued to the header row (no blank line) made
        # python-markdown's "tables" extension skip the whole table.
        doc, page = _new_page()
        _add_captioned_table(
            page,
            (50, 80, 300, 190),
            "TABLE 1 My Table",
            ["A", "B"],
            [["1", "2"]],
        )
        tabs = page.find_tables()
        self.assertEqual(len(tabs.tables), 1)
        md = _table_to_md(page, tabs.tables[0])
        self.assertIn("**TABLE 1 My Table**\n\n| A | B |", md)
        self.assertIn("<table>", markdown.markdown(md, extensions=_MD_EXTENSIONS))
        doc.close()

    def test_two_cell_caption_row_does_not_truncate_columns(self):
        # Regression (cecil p.1905): pymupdf splits a caption row like
        # "TABLE 166-3 | EXAMPLES OF TARGETED CANCER THERAPIES" into two
        # cells. The old code used that row as the header, so ncols was 2
        # and every data row was truncated to 2 columns (TARGET, DISEASE,
        # INDICATION, COMPANION TEST were lost).
        doc, page = _new_page()
        x0, y0, x1, y1 = 50, 80, 400, 200
        row_h = 40
        # Caption row split by a vertical rule at x0+80 (as in the real PDF).
        page.draw_rect(pymupdf.Rect(x0, y0, x1, y1), color=(0, 0, 0), width=1)
        page.draw_line(
            pymupdf.Point(x0 + 80, y0), pymupdf.Point(x0 + 80, y0 + row_h),
            color=(0, 0, 0), width=1,
        )
        page.draw_line(
            pymupdf.Point(x0, y0 + row_h), pymupdf.Point(x1, y0 + row_h),
            color=(0, 0, 0), width=1,
        )
        page.draw_line(
            pymupdf.Point(x0, y0 + 2 * row_h), pymupdf.Point(x1, y0 + 2 * row_h),
            color=(0, 0, 0), width=1,
        )
        for c in (1, 2):
            page.draw_line(
                pymupdf.Point(x0 + c * (x1 - x0) / 3, y0 + row_h),
                pymupdf.Point(x0 + c * (x1 - x0) / 3, y1),
                color=(0, 0, 0), width=1,
            )
        page.insert_textbox(
            pymupdf.Rect(x0 + 3, y0 + 3, x0 + 77, y0 + row_h - 3),
            "TABLE 166-3", fontsize=9,
        )
        page.insert_textbox(
            pymupdf.Rect(x0 + 83, y0 + 3, x1 - 3, y0 + row_h - 3),
            "EXAMPLES OF TARGETED CANCER THERAPIES", fontsize=9,
        )
        cw = (x1 - x0) / 3
        for c, h in enumerate(["THERAPEUTIC AGENT", "TYPE", "TARGET"]):
            page.insert_textbox(
                pymupdf.Rect(x0 + c * cw + 3, y0 + row_h + 3,
                             x0 + (c + 1) * cw - 3, y0 + 2 * row_h - 3),
                h, fontsize=9,
            )
        for c, v in enumerate(["Imatinib", "Small molecule", "BCR-ABL"]):
            page.insert_textbox(
                pymupdf.Rect(x0 + c * cw + 3, y0 + 2 * row_h + 3,
                             x0 + (c + 1) * cw - 3, y0 + 3 * row_h - 3),
                v, fontsize=9,
            )
        tabs = page.find_tables()
        self.assertEqual(len(tabs.tables), 1)
        md = _table_to_md(page, tabs.tables[0])
        # Caption is recognized (even though it spans two cells)…
        self.assertIn("**TABLE 166-3 EXAMPLES OF TARGETED CANCER THERAPIES**", md)
        # …and the data columns are no longer truncated to the caption's 2.
        self.assertIn("| THERAPEUTIC AGENT | TYPE | TARGET |", md)
        self.assertIn("| Imatinib | Small molecule | BCR-ABL |", md)
        doc.close()

    def test_merged_header_cells_do_not_truncate_data_columns(self):
        # A header row with fewer cells than the data rows (e.g. one merged
        # "all columns" cell) must not truncate the data either: ncols comes
        # from the widest row.
        doc, page = _new_page()
        _add_captioned_table(
            page,
            (50, 80, 300, 190),
            "Spans everything",
            ["A", "B", "C"],
            [["1", "2", "3"]],
        )
        tabs = page.find_tables()
        self.assertEqual(len(tabs.tables), 1)
        md = _table_to_md(page, tabs.tables[0])
        self.assertIn("**Spans everything**\n\n| A | B | C |", md)
        self.assertIn("| 1 | 2 | 3 |", md)
        doc.close()


class ColumnAwareTests(unittest.TestCase):
    @staticmethod
    def _two_column_page():
        """Left intro, a chapter title in the middle, then more left text.

        The title sits mid-column so ``move_title`` has an observable effect.
        """
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Left intro para.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(50, 240, 250, 270), "Chapter Title", fontsize=16)
        page.insert_textbox(pymupdf.Rect(50, 300, 250, 380), "Left after para.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 120, 520, 200), "Right intro para.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 300, 520, 380), "Right after para.", fontsize=10)
        return page

    def test_left_column_read_before_right(self):
        page = self._two_column_page()
        md = _column_aware_markdown(page, move_title=False)
        self.assertLess(md.index("Left intro para."), md.index("Right intro para."))
        self.assertLess(md.index("Left after para."), md.index("Right after para."))

    def test_title_moved_to_top(self):
        page = self._two_column_page()
        md = _column_aware_markdown(page, move_title=True)
        self.assertTrue(md.startswith("# Chapter Title"))
        # Without the fix the title stays in the middle of the left column.
        md_plain = _column_aware_markdown(page, move_title=False)
        self.assertLess(md_plain.index("Left intro para."), md_plain.index("Chapter Title"))

    def test_middle_table_is_kept_between_bands(self):
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Left above.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 120, 520, 200), "Right above.", fontsize=10)
        _add_table(page, (50, 400, 545, 460), [["Name", "Value"], ["Alpha", "42"]])
        page.insert_textbox(pymupdf.Rect(50, 500, 250, 580), "Left below.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 500, 520, 580), "Right below.", fontsize=10)

        md = _column_aware_markdown(page, move_title=False)
        self.assertIn("| --- | --- |", md)
        self.assertIn("| Alpha | 42 |", md)
        # The table sits after the "above" band and before the "below" band.
        self.assertLess(md.index("Left above."), md.index("| --- | --- |"))
        self.assertLess(md.index("| Alpha | 42 |"), md.index("Left below."))
        doc.close()

    def test_single_column_table_does_not_split_other_column(self):
        # A table inside the LEFT column only: it must stay in the left
        # column's flow, not split the right column into two bands.
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 90, 250, 140), "Left above.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 150, 520, 200), "Right above.", fontsize=10)
        _add_table(page, (50, 300, 250, 390), [["Name", "Value"], ["Alpha", "42"]])
        page.insert_textbox(pymupdf.Rect(50, 440, 250, 490), "Left below.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 500, 520, 550), "Right below.", fontsize=10)

        md = _column_aware_markdown(page, move_title=False)
        # The whole left column (incl. its table) is emitted before the right
        # column, which must stay in one piece (top-to-bottom).
        self.assertLess(md.index("Left below."), md.index("Right above."))
        self.assertLess(md.index("Right above."), md.index("Right below."))
        self.assertIn("| Name | Value |", md)  # the table itself is kept
        doc.close()

    def test_small_font_references_are_kept(self):
        # References/index entries are 7pt: the font filter must not drop them.
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 160), "1. Whelan JS, Davis LE.", fontsize=7)
        page.insert_textbox(pymupdf.Rect(50, 180, 250, 220), "2. Meltzer PS, Helman LJ.", fontsize=7)
        page.insert_textbox(pymupdf.Rect(320, 120, 520, 160), "12. von Mehren M, Kane JM.", fontsize=7)
        page.insert_textbox(pymupdf.Rect(320, 180, 520, 220), "16. Farag S, Smith MJ.", fontsize=7)

        md = _column_aware_markdown(page, move_title=False)
        self.assertIn("Whelan JS, Davis LE.", md)
        self.assertIn("von Mehren M, Kane JM.", md)
        doc.close()

    def test_line_break_hyphenation_is_fused(self):
        # A word hyphenated across a line break must come out as one word,
        # not "un- common".
        doc, page = _new_page()
        page.insert_textbox(
            pymupdf.Rect(50, 120, 250, 200),
            "severe thrombocytopenia or hypoprothrombinaemia is un-\ncommon.",
            fontsize=9,
        )
        page.insert_textbox(pymupdf.Rect(50, 240, 250, 320), "More text here.", fontsize=9)
        page.insert_textbox(pymupdf.Rect(320, 120, 520, 200), "Right column text.", fontsize=9)
        page.insert_textbox(pymupdf.Rect(320, 240, 520, 320), "More right text.", fontsize=9)

        md = _column_aware_markdown(page, move_title=False)
        self.assertIn("uncommon", md)
        self.assertNotIn("un- common", md)
        doc.close()

    def test_four_column_index_read_left_to_right(self):
        doc, page = _new_page()
        # Scattered y positions so find_tables() does not mistake the layout
        # for a data table (real index entries are not grid-aligned).
        page.insert_textbox(pymupdf.Rect(48, 120, 173, 160), "Alpha entry one.", fontsize=8)
        page.insert_textbox(pymupdf.Rect(48, 400, 173, 440), "Alpha entry two.", fontsize=8)
        page.insert_textbox(pymupdf.Rect(190, 200, 319, 240), "Beta entry one.", fontsize=8)
        page.insert_textbox(pymupdf.Rect(190, 320, 319, 360), "Beta entry two.", fontsize=8)
        page.insert_textbox(pymupdf.Rect(332, 140, 458, 180), "Gamma entry one.", fontsize=8)
        page.insert_textbox(pymupdf.Rect(332, 280, 458, 320), "Gamma entry two.", fontsize=8)
        page.insert_textbox(pymupdf.Rect(473, 360, 600, 400), "Delta entry one.", fontsize=8)
        page.insert_textbox(pymupdf.Rect(473, 450, 600, 490), "Delta entry two.", fontsize=8)

        md = _column_aware_markdown(page, move_title=False)
        self.assertLess(md.index("Alpha entry one."), md.index("Alpha entry two."))
        self.assertLess(md.index("Alpha entry two."), md.index("Beta entry one."))
        self.assertLess(md.index("Beta entry two."), md.index("Gamma entry one."))
        self.assertLess(md.index("Gamma entry two."), md.index("Delta entry one."))
        doc.close()

    def test_sidebar_box_rendered_as_markdown_table(self):
        # A bordered box (sidebar) must be rendered as a markdown table, not
        # flattened into the body text with glued bullets. find_tables() does
        # not detect a single bordered rectangle, so the box path (get_drawings)
        # has to catch it.
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Left text above.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(50, 500, 250, 580), "Left text below.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 300, 520, 380), "Right text.", fontsize=10)
        box = pymupdf.Rect(320, 90, 520, 220)
        page.draw_rect(box, color=(0, 0, 0), width=1)
        page.insert_textbox(pymupdf.Rect(325, 95, 515, 120), "Box item one", fontsize=9)
        page.insert_textbox(pymupdf.Rect(325, 125, 515, 150), "Box item two", fontsize=9)
        page.insert_textbox(pymupdf.Rect(325, 155, 515, 180), "Box item three", fontsize=9)

        md = _column_aware_markdown(page, move_title=False)
        self.assertIn("| Box item one |", md)
        self.assertIn("| --- |", md)
        self.assertIn("| Box item two |", md)
        self.assertIn("| Box item three |", md)
        # Box content must not be duplicated as plain body text.
        self.assertEqual(md.count("Box item one"), 1)
        self.assertEqual(md.count("Box item two"), 1)
        doc.close()

    def test_title_strip_box_does_not_duplicate_title(self):
        # A thin border strip holding the box title (which continues inside
        # the box below) must not be rendered as its own table: the title is
        # already emitted as the **bold heading** of the box below (rosen
        # p.3032 pattern).
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Left text above.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(50, 500, 250, 580), "Left text below.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 400, 520, 480), "Right text.", fontsize=10)
        strip = pymupdf.Rect(320, 221, 570, 261)
        page.draw_rect(strip, color=(0, 0, 0), width=1)
        page.insert_textbox(
            pymupdf.Rect(325, 224, 565, 258),
            "BOX 9.2 Title of the\nExample Box Continues",
            fontsize=9,
        )
        box = pymupdf.Rect(320, 261, 570, 380)
        page.draw_rect(box, color=(0, 0, 0), width=1)
        page.insert_textbox(pymupdf.Rect(325, 265, 565, 290), "Box item one", fontsize=9)
        page.insert_textbox(pymupdf.Rect(325, 295, 565, 320), "Box item two", fontsize=9)

        md = _column_aware_markdown(page, move_title=False)
        self.assertIn("**BOX 9.2 Title of the Example Box Continues**", md)
        self.assertIn("| Box item one |", md)
        self.assertIn("| Box item two |", md)
        # Title emitted exactly once: as the heading, not also as a strip table.
        self.assertEqual(md.count("BOX 9.2"), 1)
        self.assertNotIn("| BOX 9.2 Title of the", md)
        doc.close()

    def test_off_page_strip_is_not_rendered_as_box(self):
        # A border rectangle drawn outside the page top (a decorative edge
        # strip clipping the chapter heading, robbins p.426 pattern) must not
        # become a box table duplicating the heading.
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Left text above.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(50, 500, 250, 580), "Left text below.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 400, 520, 480), "Right text.", fontsize=10)
        page.draw_rect(pymupdf.Rect(123, -9, 570, 42), color=(0, 0, 0), width=1)
        page.insert_textbox(
            pymupdf.Rect(200, 5, 560, 40),
            "Environmental and Nutritional Diseases",
            fontsize=10,
        )

        md = _column_aware_markdown(page, move_title=False)
        self.assertEqual(md.count("Environmental and Nutritional Diseases"), 1)
        self.assertNotIn("| Environmental and Nutritional Diseases |", md)
        doc.close()

    def test_table_legend_below_table_kept_despite_small_font(self):
        # Abbreviation keys / footnotes directly below a data table are real
        # content even at 6pt (hockberg p.1430 pattern); free-floating small
        # text like figure sub-labels is noise and must stay dropped.
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Left text above.", fontsize=10)
        _add_table(
            page,
            (50, 250, 400, 340),
            [["Drug", "Dose"], ["AAV therapy", "500 mg"], ["Maintenance", "250 mg"]],
        )
        page.insert_textbox(
            pymupdf.Rect(54, 345, 396, 375),
            "AAV, Antineutrophil cytoplasm antibody; BVAS, Birmingham Vasculitis "
            "Activity Score.",
            fontsize=6,
        )
        page.insert_textbox(pymupdf.Rect(450, 120, 560, 140), "(a) (b)", fontsize=6)

        md = _column_aware_markdown(page, move_title=False)
        self.assertIn("Birmingham Vasculitis Activity Score", md)
        self.assertNotIn("(a) (b)", md)
        doc.close()


@unittest.skipUnless(os.path.exists(REAL_PDF), "harrison2025.pdf not present")
class CrossColumnSplitTests(unittest.TestCase):
    def test_glued_paragraph_is_split_at_column_boundary(self):
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Alpha beta gamma delta", fontsize=10)
        page.insert_textbox(pymupdf.Rect(50, 220, 250, 300), "Epsilon zeta eta theta", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 120, 520, 200), "iota kappa lambda mu", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 220, 520, 300), "nu xi omicron pi", fontsize=10)

        md = "Alpha beta gamma delta iota kappa lambda mu\n\nA short para."
        out = _split_cross_column_paragraphs(md, page)
        self.assertIn("Alpha beta gamma delta\n\niota kappa lambda mu", out)
        self.assertIn("A short para.", out)  # short paragraphs pass through
        doc.close()

    def test_single_column_is_unchanged(self):
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Only one column here", fontsize=10)
        page.insert_textbox(pymupdf.Rect(50, 220, 250, 300), "More text below it", fontsize=10)
        md = "Only one column here More text below it"
        self.assertEqual(_split_cross_column_paragraphs(md, page), md)
        doc.close()


@unittest.skipUnless(os.path.exists(REAL_PDF), "harrison2025.pdf not present")
class RealPdfRegressionTests(unittest.TestCase):
    """Regression checks on the pages that originally motivated the fixes."""

    @classmethod
    def setUpClass(cls):
        cls.doc = pymupdf.open(REAL_PDF)

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()

    def test_page_1007_intro_paragraph_in_reading_order(self):
        md = _column_aware_markdown(self.doc[1006], move_title=False)
        self.assertLess(
            md.index("corresponding human"), md.index("cellular responses")
        )

    def test_page_156_table_rendered_as_markdown(self):
        md = _column_aware_markdown(self.doc[155], move_title=False)
        self.assertIn("| ---", md)

    def test_page_157_top_of_right_column_paragraph_in_order(self):
        md = _column_aware_markdown(self.doc[156], move_title=False)
        target = "treatment of both tension-type headache"
        self.assertIn(target, md)
        # Correct reading order: the left column (incl. its final list item)
        # ends, then the right column starts with this paragraph.
        self.assertLess(md.index("region of temporal artery"), md.index(target))
        self.assertLess(
            md.index(target), md.index("Underlying recurrent headache disorders")
        )

    def test_page_157_docling_glue_is_split(self):
        # Docling's layout model glues the last cell of TABLE 17-2 to the right
        # column's opening sentence; the fix must re-split them.
        glued = (
            "Pain associated with local tenderness, e.g., region of temporal "
            "artery treatment of both tension-type headache and migraine, each "
            "symptom must be treated optimally."
        )
        out = _split_cross_column_paragraphs(glued, self.doc[156])
        self.assertNotIn("artery treatment", out)
        self.assertIn("treatment of both tension-type headache", out)


@unittest.skipUnless(os.path.exists(REAL_CECIL_PDF), "cecil2024.pdf not present")
class CecilPdfRegressionTests(unittest.TestCase):
    """Regression checks on Cecil layouts: references and multi-column index."""

    @classmethod
    def setUpClass(cls):
        cls.doc = pymupdf.open(REAL_CECIL_PDF)

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()

    def test_page_2117_references_are_not_dropped(self):
        # 7pt reference entries must survive the font filter.
        md = _column_aware_markdown(self.doc[2116], move_title=False)
        self.assertIn("Whelan JS", md)
        self.assertIn("von Mehren M", md)

    def test_page_4382_index_columns_in_order(self):
        # Four-column index: column 1 must be read before column 4.
        md = _column_aware_markdown(self.doc[4381], move_title=False)
        self.assertLess(md.index("Dermatomyositis"), md.index("Diazepam"))
        self.assertIn("nordiazepam", md)


@unittest.skipUnless(os.path.exists(REAL_ROSEN_PDF), "rosen2022.pdf not present")
class RosenPdfRegressionTests(unittest.TestCase):
    """Regression checks on Rosen (sidebar boxes)."""

    @classmethod
    def setUpClass(cls):
        cls.doc = pymupdf.open(REAL_ROSEN_PDF)

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()

    def test_page_59_sidebar_box_rendered_as_table(self):
        # The "BOX 3.2" sidebar must come out as a markdown table (as
        # pymupdf4llm renders it), not as glued plain-text bullets.
        md = _column_aware_markdown(self.doc[58], move_title=False)
        self.assertIn("BOX 3.2", md)
        self.assertIn("Ill appearance or altered mental status", md)
        self.assertIn("| --- |", md)
        self.assertIn("Heart rate >100 beats/min", md)

    def test_page_3032_box_title_not_duplicated_by_strip_box(self):
        # The title of BOX E15.4 sits in a thin border strip of its own; it
        # must be emitted once (as the bold heading), not twice (also as a
        # strip table).
        md = _column_aware_markdown(self.doc[3031], move_title=False)
        self.assertEqual(md.count("BOX E15.4"), 1)
        self.assertIn("**BOX E15.4 Recommendations for Prevention of", md)
        self.assertIn("Isolate patient in single room", md)


@unittest.skipUnless(os.path.exists(REAL_DAVID_PDF), "david2027.pdf not present")
class DavidPdfRegressionTests(unittest.TestCase):
    """Regression checks on David (Xodo watermark must not bridge columns)."""

    @classmethod
    def setUpClass(cls):
        cls.doc = pymupdf.open(REAL_DAVID_PDF)

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()

    def test_page_27_watermark_does_not_collapse_columns(self):
        # The "Made with Xodo" banner in the top margin spans the column gap;
        # it must not merge the two columns into one (which interleaved the
        # left column into the right column's flow).
        self.assertTrue(_page_needs_column_reorder(self.doc[26]))

    def test_page_28_left_column_read_before_right_column(self):
        # Left column's last paragraph precedes the right column's first
        # heading — the exact order the watermark bridge was breaking.
        md = _column_aware_markdown(self.doc[27], move_title=False)
        self.assertLess(
            md.index("Almost half of doctors"), md.index("Bat and ball problem")
        )


if __name__ == "__main__":
    unittest.main()
