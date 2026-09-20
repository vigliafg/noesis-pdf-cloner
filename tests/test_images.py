"""Tests for the PyMuPDF manual region image extraction.

These exercise the pure module-level helper ``_region_image`` (used by the
🖱️ Seleziona zona feature) against a small synthetic PDF page, so no GUI is
required. A regression test on the real ``porth2014.pdf`` (page 44 holds two
JPEG2000 figures) is included but skipped when the file is absent.
"""

import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pymupdf  # noqa: E402

from main import _region_image  # noqa: E402

PNG_SIG = b"\x89PNG"


def _make_doc_with_image():
    """Return (doc, page, png_bytes) with one embedded PNG at (50,50,150,150)."""
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 30, 20))
    pix.clear_with(250)
    png = pix.tobytes("png")
    page.insert_image(pymupdf.Rect(50, 50, 150, 150), stream=png)
    return doc, page, png


def _make_doc_with_jpeg_image():
    """Return (doc, page, jpg_bytes) with one embedded JPEG at (40,40,160,160)."""
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30))
    pix.clear_with(120)
    jpg = pix.tobytes("jpg")
    page.insert_image(pymupdf.Rect(40, 40, 160, 160), stream=jpg)
    return doc, page, jpg


def _make_doc_with_cmyk_jpeg_image():
    """Return (doc, page, jpg_bytes) with one embedded CMYK JPEG at (50,50,150,150)."""
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=200)
    pix = pymupdf.Pixmap(pymupdf.csCMYK, pymupdf.IRect(0, 0, 30, 20))
    pix.clear_with(0)
    jpg = pix.tobytes("jpg")  # JPEG keeps the CMYK colorspace
    page.insert_image(pymupdf.Rect(50, 50, 150, 150), stream=jpg)
    return doc, page, jpg


# The real corpus PDFs live in the parent repo (noesis-pdf-reader/); fall back
# to this project's own directory so the tests still run if copied here.
_PARENT_REPO = os.path.normpath(os.path.join(_ROOT, "..", "noesis-pdf-reader"))


def _corpus_pdf(name: str) -> str:
    here = os.path.join(_ROOT, name)
    there = os.path.join(_PARENT_REPO, name)
    return here if os.path.exists(here) else there


PORTH_PDF = _corpus_pdf("porth2014.pdf")
ANDREW_PDF = _corpus_pdf("andrew2020.pdf")


class RegionImageTests(unittest.TestCase):
    def test_full_cover_returns_embedded_bytes(self):
        doc, _page, _png = _make_doc_with_image()
        try:
            out = _region_image(doc, 0, (40, 40, 160, 160), 2.0)
            self.assertIsNotNone(out)
            data, ext = out
            self.assertEqual(ext, "png")
            self.assertTrue(data.startswith(PNG_SIG))
        finally:
            doc.close()

    def test_subregion_falls_back_to_render(self):
        doc, _page, _png = _make_doc_with_image()
        try:
            out = _region_image(doc, 0, (60, 60, 80, 80), 2.0)
            self.assertIsNotNone(out)
            data, ext = out
            self.assertEqual(ext, "png")
            self.assertTrue(data.startswith(PNG_SIG))
        finally:
            doc.close()

    def test_empty_region_returns_none(self):
        doc, _page, _png = _make_doc_with_image()
        try:
            self.assertIsNone(_region_image(doc, 0, (10, 10, 5, 5), 2.0))
        finally:
            doc.close()


class EmbeddedOnlyTests(unittest.TestCase):
    """The exclude-zone gesture captures the WHOLE selected zone (rendered)
    when the zone targets an embedded image; a pure-text zone never adds a
    rendered PNG to the gallery."""

    def test_full_cover_captures_whole_zone(self):
        doc, _page, _png = _make_doc_with_image()
        try:
            out = _region_image(doc, 0, (40, 40, 160, 160), 2.0, embedded_only=True)
            self.assertIsNotNone(out)
            data, ext = out
            self.assertEqual(ext, "png")
            self.assertTrue(data.startswith(PNG_SIG))
        finally:
            doc.close()

    def test_partial_overlap_captures_whole_zone(self):
        """A sloppy selection covering ≥50% of the image still captures it."""
        doc, _page, _png = _make_doc_with_image()
        try:
            out = _region_image(doc, 0, (40, 40, 120, 160), 2.0, embedded_only=True)
            self.assertIsNotNone(out)
            data, ext = out
            self.assertEqual(ext, "png")
            self.assertTrue(data.startswith(PNG_SIG))
        finally:
            doc.close()

    def test_composite_zone_captures_whole_zone_not_one_fragment(self):
        """Two embedded images side by side: the exclude zone must render the
        WHOLE selected region (both images), not just one raster."""
        import struct

        doc = pymupdf.open()
        page = doc.new_page(width=300, height=300)
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 30, 20))
        pix.clear_with(250)
        png = pix.tobytes("png")
        page.insert_image(pymupdf.Rect(50, 50, 150, 150), stream=png)
        page.insert_image(pymupdf.Rect(160, 50, 260, 150), stream=png)
        try:
            # The zone wraps both images (composite figure).
            out = _region_image(doc, 0, (40, 40, 270, 160), 2.0, embedded_only=True)
            self.assertIsNotNone(out)
            data, ext = out
            self.assertEqual(ext, "png")
            self.assertTrue(data.startswith(PNG_SIG))
            # Raw PNG pixel size (IHDR): whole zone = 230 x 120 pt at zoom
            # 2.0 -> 460 x 240 px. A single embedded fragment would be only
            # 200 x 200 px (one 100 pt image at zoom 2.0).
            width, height = struct.unpack(">II", data[16:24])
            self.assertEqual((width, height), (460, 240))
        finally:
            doc.close()

    def test_text_zone_skips_render_fallback(self):
        """A zone with no embedded image returns None in embedded_only mode,
        even though the render fallback would produce a PNG."""
        doc, _page, _png = _make_doc_with_image()
        try:
            # Empty corner of the page: render fallback would succeed,
            # embedded-only must return None.
            self.assertIsNone(
                _region_image(doc, 0, (10, 10, 30, 30), 2.0, embedded_only=True)
            )
            # Sanity check: the same zone without embedded_only renders a PNG.
            out = _region_image(doc, 0, (10, 10, 30, 30), 2.0)
            self.assertIsNotNone(out)
            data, ext = out
            self.assertEqual(ext, "png")
            self.assertTrue(data.startswith(PNG_SIG))
        finally:
            doc.close()


class PngNormalizationTests(unittest.TestCase):
    def test_region_embedded_image_is_normalized_to_png(self):
        """The embedded path of ``_region_image`` must always return PNG."""
        doc, _page, _jpg = _make_doc_with_jpeg_image()
        try:
            out = _region_image(doc, 0, (40, 40, 160, 160), 2.0)
            self.assertIsNotNone(out)
            data, ext = out
            self.assertEqual(ext, "png")
            self.assertTrue(data.startswith(PNG_SIG))
        finally:
            doc.close()

    def test_cmyk_embedded_image_is_normalized_to_png(self):
        """CMYK images must be converted to RGB (PNG cannot encode CMYK)."""
        doc, _page, _jpg = _make_doc_with_cmyk_jpeg_image()
        try:
            out = _region_image(doc, 0, (50, 50, 150, 150), 2.0)
            self.assertIsNotNone(out)
            data, ext = out
            self.assertEqual(ext, "png")
            self.assertTrue(data.startswith(PNG_SIG))
        finally:
            doc.close()


@unittest.skipUnless(os.path.exists(PORTH_PDF), "porth2014.pdf not present")
class PorthJpxRegressionTests(unittest.TestCase):
    """Page 44 holds two JPEG2000 (jpx) figures; manual capture must be PNG."""

    @classmethod
    def setUpClass(cls):
        cls.doc = pymupdf.open(PORTH_PDF)

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()

    def test_page_44_regions_are_png(self):
        # 0-based page 43 = PDF page 44. The two figures are embedded as
        # JPEG2000 (jpx), which Qt cannot display; _region_image must
        # normalize them to PNG via the embedded path.
        for clip in [(49, 59, 287, 356), (140, 543, 282, 726)]:
            out = _region_image(self.doc, 43, clip, 3.0)
            self.assertIsNotNone(out)
            data, ext = out
            self.assertEqual(ext, "png")
            self.assertTrue(data.startswith(PNG_SIG))


@unittest.skipUnless(os.path.exists(ANDREW_PDF), "andrew2020.pdf not present")
class AndrewCmykRegressionTests(unittest.TestCase):
    """Page 20 holds a CMYK JPEG2000 figure; manual capture must be PNG."""

    @classmethod
    def setUpClass(cls):
        cls.doc = pymupdf.open(ANDREW_PDF)

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()

    def test_page_20_region_is_png(self):
        # 0-based page 19 = PDF page 20. The figure is embedded as CMYK
        # JPEG2000 (jpx), which neither Qt nor PNG can represent directly.
        out = _region_image(self.doc, 19, (59, 65, 310, 254), 3.0)
        self.assertIsNotNone(out)
        data, ext = out
        self.assertEqual(ext, "png")
        self.assertTrue(data.startswith(PNG_SIG))


if __name__ == "__main__":
    unittest.main()
