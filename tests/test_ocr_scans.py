"""Test del percorso OCR → Markdown (``ocr_scans``).

Nessuna rete: il provider usa un ``post`` iniettato e i traduttori sono
sostituiti. Le immagini sono un PNG 1×1 valido.
"""

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pymupdf  # noqa: E402

import ocr_scans  # noqa: E402

_PIX = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 2, 2), 0)
_PIX.clear_with(200)
_PNG = _PIX.tobytes("png")
_DATA_URL = "data:image/png;base64," + base64.b64encode(_PNG).decode()


def _blank_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    doc.new_page(width=400, height=400)
    doc.save(str(path))
    doc.close()
    return path


def _payload(text: str, images: int = 0) -> tuple[int, dict]:
    parts = [{"type": "text", "text": text}]
    parts += [{"type": "image_url", "image_url": {"url": _DATA_URL}} for _ in range(images)]
    return 200, {"choices": [{"message": {"annotations": [{
        "type": "file", "file": {"content": parts},
    }]}}]}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.pdf = _blank_pdf(Path(self._tmp.name) / "page.pdf")

    def tearDown(self):
        self._tmp.cleanup()

    def test_ocr_pdf_page_reads_annotations(self):
        with mock.patch.object(
            ocr_scans, "_post_json", return_value=_payload("Testo OCR")
        ):
            text = ocr_scans.ocr_pdf_page(
                self.pdf, ocr_scans.OcrConfig("k")
            )
        self.assertEqual(text, "Testo OCR")

    def test_missing_key(self):
        with self.assertRaises(ocr_scans.OcrError) as ctx:
            ocr_scans.ocr_pdf_page(self.pdf, ocr_scans.OcrConfig(""))
        self.assertEqual(ctx.exception.code, "missing_key")

    def test_http_error_without_annotations(self):
        with mock.patch.object(
            ocr_scans,
            "_post_json",
            return_value=(402, {"error": {"message": "Payment required"}}),
        ):
            with self.assertRaises(ocr_scans.OcrError) as ctx:
                ocr_scans.ocr_pdf_page(
                    self.pdf, ocr_scans.OcrConfig("k")
                )
        self.assertEqual(ctx.exception.code, "no_credits")

    def test_rate_limit_with_annotations_is_success(self):
        def fake_post(*args, **kwargs):
            return _payload("Testo")

        with mock.patch.object(ocr_scans, "_post_json", side_effect=fake_post):
            text = ocr_scans.ocr_pdf_page(
                self.pdf, ocr_scans.OcrConfig("k")
            )
        self.assertEqual(text, "Testo")

    def test_config_tag_is_stable_and_versioned(self):
        tag = ocr_scans.OcrConfig("k").tag()
        self.assertTrue(tag.startswith(f"ocr{ocr_scans.OCR_SCHEMA_VERSION}-"))
        self.assertEqual(tag, ocr_scans.OcrConfig("altro").tag())
        self.assertNotEqual(tag, ocr_scans.OcrConfig("k", model="altro/modello").tag())


class PageOcrTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.pdf = _blank_pdf(Path(self._tmp.name) / "page.pdf")

    def tearDown(self):
        self._tmp.cleanup()

    def test_text_images_caption_and_body(self):
        text = (
            "<file name=\"page.pdf\">\n# Title\n\nSee "
            "![img-0.jpeg](img-0.jpeg)\n\nFigure 3 A useful figure\n"
        )
        with mock.patch.object(
            ocr_scans, "_post_json", return_value=_payload(text, images=1)
        ):
            page = ocr_scans.ocr_page(
                self.pdf, ocr_scans.OcrConfig("k"), page_label="12"
            )
        self.assertNotIn("![", page.markdown)
        self.assertNotIn("file name", page.markdown)
        self.assertIn("# Title", page.markdown)
        self.assertEqual(len(page.images), 1)
        self.assertEqual(page.images[0].filename, "img_0.png")
        self.assertIn("Figure 3", page.images[0].caption)
        self.assertEqual(page.images[0].data, _PNG)

    def test_image_without_placeholder_goes_to_tail(self):
        with mock.patch.object(
            ocr_scans, "_post_json", return_value=_payload("Solo testo", images=1)
        ):
            page = ocr_scans.ocr_page(
                self.pdf, ocr_scans.OcrConfig("k"), page_label="7"
            )
        self.assertEqual(len(page.images), 1)
        self.assertIn("pagina 7", page.images[0].caption.lower())

    def test_write_and_read_roundtrip(self):
        page = ocr_scans.OcrPage(
            markdown="# Titolo\n\nTesto",
            images=[ocr_scans.OcrImage(
                filename="img_0.png", data=_PNG, caption="Figura 1",
                source_name="img-0.jpeg",
            )],
        )
        md = Path(self._tmp.name) / "page.md"
        assets = Path(self._tmp.name) / "assets"
        ocr_scans.write_ocr_page(page, md, assets)
        body, images = ocr_scans.read_ocr_page(md, assets)
        self.assertEqual(body, "# Titolo\n\nTesto")
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].data, _PNG)
        self.assertEqual(images[0].caption, "Figura 1")

    def test_append_figures_and_base64(self):
        md = "# Titolo\n\nTesto"
        images = [ocr_scans.OcrImage(
            filename="img_0.png", data=_PNG, caption="Figura 1 — pagina 2"
        )]
        out = ocr_scans.append_figures(md, images)
        self.assertIn("## Figure", out)
        self.assertIn("assets/img_0.png", out)
        self.assertIn("*Figura 1 — pagina 2*", out)
        inlined = ocr_scans.inline_figure_images(out, images)
        self.assertIn("data:image/png;base64,", inlined)
        self.assertNotIn("assets/img_0.png", inlined)

    def test_pdf_from_markdown_multi_page_with_figure(self):
        text = ("# Titolo\n\n" + ("Parola " * 1200)).strip()
        images = [ocr_scans.OcrImage(
            filename="img_0.png", data=_PNG, caption="Figura 1 — pagina 2"
        )]
        dst = Path(self._tmp.name) / "hardcopy.pdf"
        pages = ocr_scans.build_pdf_from_markdown(
            text, images, dst, width=400, height=400, page_label="2"
        )
        self.assertGreaterEqual(pages, 2)
        with pymupdf.open(str(dst)) as doc:
            all_text = "".join(p.get_text() for p in doc)
            images_found = sum(len(p.get_images()) for p in doc)
        self.assertIn("Titolo", all_text)
        self.assertIn("Figura 1", all_text)
        self.assertGreaterEqual(images_found, 1)

    def test_translate_markdown_maps_bing_to_microsoft(self):
        with mock.patch.object(
            ocr_scans.text_translate, "translate_text", return_value="X"
        ) as tr:
            self.assertEqual(
                ocr_scans.translate_markdown("A", "en", "it", "bing"), "X"
            )
        self.assertEqual(tr.call_args.kwargs["engine"], "microsoft")


if __name__ == "__main__":
    unittest.main()
