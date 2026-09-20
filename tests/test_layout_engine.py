"""Tests for the adaptive layout-fix engine (``layout_engine.py``).

Covers the profiler (``profile_page``), the fix registry (``FIX_REGISTRY``),
the scheduler (``plan_fixes``, incl. the ``fix_rules.json`` override) and the
pipeline (``apply_plan``). Synthetic pages are built with pymupdf, mirroring
``test_fixes.py``; the real-PDF equivalence checks are skipped when
``harrison2025.pdf`` is absent.

Run with (from the project root):

    .venv/bin/python -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest

# Make the single-file `main` module importable from the project root.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if os.path.join(_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "tests"))

import pymupdf  # noqa: E402

import layout_engine  # noqa: E402
from layout_engine import (  # noqa: E402
    FIX_REGISTRY,
    LayoutProfile,
    _load_overrides,
    apply_plan,
    plan_fixes,
    profile_page,
)

from test_fixes import _add_table, _new_page  # noqa: E402

# The real corpus PDFs live in the parent repo (noesis-pdf-reader/).
_PARENT_REPO = os.path.normpath(os.path.join(_ROOT, "..", "noesis-pdf-reader"))
REAL_PDF = os.path.join(_ROOT, "harrison2025.pdf")
if not os.path.exists(REAL_PDF):
    REAL_PDF = os.path.join(_PARENT_REPO, "harrison2025.pdf")


def _profile(
    columns=1,
    overlap=False,
    tables=False,
    index=False,
    refs=False,
    small=False,
) -> LayoutProfile:
    """A LayoutProfile built directly (scheduler tests in isolation)."""
    return LayoutProfile(
        columns=columns,
        splits=tuple(float(i) for i in range(columns)),
        columns_overlap=overlap,
        has_tables=tables,
        full_width_tables=1 if tables else 0,
        has_small_text=small,
        has_references=refs,
        has_index=index,
        body_blocks=10,
    )


def _ids(plan) -> list[str]:
    return [f.id for f in plan]


class ProfilePageTests(unittest.TestCase):
    def test_two_columns_side_by_side(self):
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Left one.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(50, 220, 250, 300), "Left two.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 120, 520, 200), "Right one.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 220, 520, 300), "Right two.", fontsize=10)
        p = profile_page(page)
        self.assertEqual(p.columns, 2)
        self.assertTrue(p.columns_overlap)
        doc.close()

    def test_single_column(self):
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Only left.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(50, 220, 250, 300), "More left.", fontsize=10)
        p = profile_page(page)
        self.assertEqual(p.columns, 1)
        self.assertFalse(p.columns_overlap)
        doc.close()

    def test_watermark_header_does_not_collapse_columns(self):
        # A top-margin banner spanning the column gap must not bridge the two
        # columns into one (the profiler must still report 2 columns).
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
        p = profile_page(page)
        self.assertEqual(p.columns, 2)
        self.assertTrue(p.columns_overlap)
        doc.close()

    def test_four_column_index(self):
        # Real index entries carry page numbers ("term, 1125f" / "term, 86, 87").
        # Scattered y positions so find_tables() does not see a data grid.
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(48, 120, 173, 160), "Adrenal adenoma, 1125f", fontsize=8)
        page.insert_textbox(pymupdf.Rect(48, 400, 173, 440), "Anemia, 86, 87, 90", fontsize=8)
        page.insert_textbox(pymupdf.Rect(190, 200, 319, 240), "Beta blocker, 1125–1126", fontsize=8)
        page.insert_textbox(pymupdf.Rect(190, 320, 319, 360), "Biopsy, 14b", fontsize=8)
        page.insert_textbox(pymupdf.Rect(332, 140, 458, 180), "Candida, 1115", fontsize=8)
        page.insert_textbox(pymupdf.Rect(332, 280, 458, 320), "Cancer staging, 23", fontsize=8)
        page.insert_textbox(pymupdf.Rect(473, 360, 600, 400), "Dyspnea, 7, 9", fontsize=8)
        page.insert_textbox(pymupdf.Rect(473, 450, 600, 490), "DVT, 1122t", fontsize=8)
        p = profile_page(page)
        self.assertEqual(p.columns, 4)
        self.assertTrue(p.has_index)
        doc.close()

    def test_full_width_table(self):
        # Table whose cells span the page width: find_tables bbox is wide.
        doc, page = _new_page()
        _add_table(
            page,
            (50, 80, 545, 160),
            [["A", "B", "C", "D"], ["1", "2", "3", "4"], ["5", "6", "7", "8"]],
        )
        p = profile_page(page)
        self.assertTrue(p.has_tables)
        self.assertEqual(p.full_width_tables, 1)
        self.assertEqual(p.columns, 1)  # no column text outside the table
        doc.close()

    def test_small_font_references(self):
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 160), "1. Whelan JS, Davis LE.", fontsize=7)
        page.insert_textbox(pymupdf.Rect(50, 180, 250, 220), "2. Meltzer PS, Helman LJ.", fontsize=7)
        page.insert_textbox(pymupdf.Rect(320, 120, 520, 160), "12. von Mehren M, Kane JM.", fontsize=7)
        page.insert_textbox(pymupdf.Rect(320, 180, 520, 220), "16. Farag S, Smith MJ.", fontsize=7)
        p = profile_page(page)
        self.assertEqual(p.columns, 2)
        self.assertTrue(p.has_small_text)
        self.assertTrue(p.has_references)
        doc.close()


class PlanFixesTests(unittest.TestCase):
    def test_auto_two_columns_pymupdf4llm(self):
        plan = plan_fixes(_profile(2, overlap=True), "PyMuPDF4LLM ⚡", mode="auto")
        self.assertEqual(_ids(plan), ["reorder_columns", "spacing"])

    def test_auto_two_columns_docling(self):
        plan = plan_fixes(_profile(2, overlap=True), "Docling 🧠", mode="auto")
        self.assertEqual(_ids(plan), ["dehyphenate", "split_glued", "spacing"])

    def test_auto_single_column(self):
        plan = plan_fixes(_profile(1), "PyMuPDF4LLM ⚡", mode="auto")
        self.assertEqual(_ids(plan), ["spacing"])

    def test_auto_two_columns_without_overlap_no_reorder(self):
        plan = plan_fixes(_profile(2, overlap=False), "PyMuPDF4LLM ⚡", mode="auto")
        self.assertEqual(_ids(plan), ["spacing"])

    def test_manual_mode_returns_single_fix(self):
        # mode = fix id applies that fix regardless of `when`.
        plan = plan_fixes(_profile(1), "PyMuPDF4LLM ⚡", mode="reorder_columns")
        self.assertEqual(_ids(plan), ["reorder_columns"])

    def test_unknown_manual_mode_returns_empty(self):
        self.assertEqual(plan_fixes(_profile(1), "PyMuPDF4LLM ⚡", mode="nope"), [])

    def test_override_disable(self):
        plan = plan_fixes(
            _profile(2, overlap=True), "PyMuPDF4LLM ⚡",
            overrides={"disable": ["spacing"]},
        )
        self.assertEqual(_ids(plan), ["reorder_columns"])

    def test_override_custom_rule_replaces_default_plan(self):
        overrides = {
            "rules": [
                {"when": {"columns": {"gte": 2}}, "apply": ["reorder_columns"], "order": 5}
            ]
        }
        plan = plan_fixes(_profile(2, overlap=True), "PyMuPDF4LLM ⚡", overrides=overrides)
        self.assertEqual(_ids(plan), ["reorder_columns"])

    def test_override_custom_rule_not_matching_keeps_default(self):
        overrides = {
            "rules": [
                {"when": {"columns": {"gte": 5}}, "apply": ["reorder_columns"]}
            ]
        }
        plan = plan_fixes(_profile(2, overlap=True), "PyMuPDF4LLM ⚡", overrides=overrides)
        self.assertEqual(_ids(plan), ["reorder_columns", "spacing"])


class ApplyPlanTests(unittest.TestCase):
    def test_two_column_reading_order(self):
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "Left intro para.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(50, 300, 250, 380), "Left after para.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 120, 520, 200), "Right intro para.", fontsize=10)
        page.insert_textbox(pymupdf.Rect(320, 300, 520, 380), "Right after para.", fontsize=10)
        profile = profile_page(page)
        plan = plan_fixes(profile, "PyMuPDF4LLM ⚡", mode="auto")
        md = apply_plan("", page, profile, plan)
        self.assertLess(md.index("Left intro para."), md.index("Right intro para."))
        self.assertLess(md.index("Left after para."), md.index("Right after para."))
        doc.close()

    def test_single_column_applies_spacing_only(self):
        doc, page = _new_page()
        page.insert_textbox(pymupdf.Rect(50, 120, 250, 200), "**134**and pathogen", fontsize=10)
        profile = profile_page(page)
        plan = plan_fixes(profile, "PyMuPDF4LLM ⚡", mode="auto")
        md = apply_plan("**134**and pathogen", page, profile, plan)
        self.assertIn("**134** and pathogen", md)
        doc.close()


class OverridesFileTests(unittest.TestCase):
    def test_load_overrides_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "fix_rules.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"disable": ["spacing"]}, f)
            data = _load_overrides(path)
            self.assertEqual(data.get("disable"), ["spacing"])

    def test_missing_file_returns_empty(self):
        self.assertEqual(_load_overrides("/nonexistent/fix_rules.json"), {})

    def test_invalid_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "fix_rules.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("not json{{{")
            self.assertEqual(_load_overrides(path), {})


@unittest.skipUnless(os.path.exists(REAL_PDF), "harrison2025.pdf not present")
@unittest.skipUnless(os.path.exists(REAL_PDF), "harrison2025.pdf not present")
class RealPdfEngineTests(unittest.TestCase):
    """Equivalence / behaviour of the engine on the real motivating pages."""

    @classmethod
    def setUpClass(cls):
        cls.doc = pymupdf.open(REAL_PDF)

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()

    def test_auto_pymupdf4llm_1007_reading_order(self):
        page = self.doc[1006]
        profile = profile_page(page)
        plan = plan_fixes(profile, "PyMuPDF4LLM ⚡", mode="auto")
        md = apply_plan("", page, profile, plan)
        self.assertLess(md.index("corresponding human"), md.index("cellular responses"))

    def test_docling_glue_split_via_engine(self):
        # The plan for Docling on a two-column page includes dehyphenate +
        # split_glued; the glued paragraph from TABLE 17-2 must be re-split.
        page = self.doc[156]
        profile = profile_page(page)
        plan = plan_fixes(profile, "Docling 🧠", mode="auto")
        self.assertIn("split_glued", _ids(plan))
        glued = (
            "Pain associated with local tenderness, e.g., region of temporal "
            "artery treatment of both tension-type headache and migraine, each "
            "symptom must be treated optimally."
        )
        md = apply_plan(glued, page, profile, plan)
        self.assertNotIn("artery treatment", md)
        self.assertIn("treatment of both tension-type headache", md)


if __name__ == "__main__":
    unittest.main()
