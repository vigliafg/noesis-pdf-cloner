#!/usr/bin/env python3
"""Noesis PDF Cloner — PyQt6 split view: pagina originale (sinistra) + clone tradotto (destra).

Derivata da noesis-pdf-reader-lite: la UI, la navigazione (numero pagina, TOC,
prec/succ), lo zoom e le Impostazioni restano; al posto del testo estratto in
markdown il pannello destro mostra la **pagina tradotta con layout preservato**,
prodotta da pdf2zh_next v2 (BabelDOC) tramite ``clone_engine``. I tre motori di
traduzione sono google (catena gratuita), bing e llm (LLM/OpenRouter).

Il codice di estrazione testo + engine di layout è mantenuto dormiente (UI
nascosta) in vista dell'integrazione futura del motore Docling.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import markdown as _md_lib

# Engine adattativo dei fix di layout (puro, senza PyQt): profilo → piano →
# pipeline. Importa lazy alcuni helper puri da questo modulo.
import layout_engine

# Motore di clonazione della pagina tradotta (pdf2zh_next v2 + cache per engine).
import clone_engine
from clone_engine import ENGINES as CLONE_ENGINES

# Internazionalizzazione della sola interfaccia (dict T(), nessuna dipendenza
# Qt): le stringhe del chrome UI passano da qui, la lingua si cambia al volo
# e il config (lingue + preferenze) è gestito da questo modulo.
from i18n import (
    LANGUAGES, TRANSLATION_LANGUAGES, TRANSLATION_ENGINES, DEFAULTS, T,
    get_language, set_language, get_source_lang, set_source_lang,
    get_target_lang, set_target_lang,
    get_translation_engine, set_translation_engine,
    flag_endonym, get_config, get_setting, set_setting,
    init_config, save_config,
)

_MD_EXTENSIONS = ["tables", "fenced_code", "codehilite"]

try:
    import pymupdf4llm
    _has_pymupdf4llm = True
except ImportError:
    pymupdf4llm = None  # type: ignore
    _has_pymupdf4llm = False

try:
    import pymupdf
    _has_pymupdf = True
except ImportError:
    pymupdf = None  # type: ignore
    _has_pymupdf = False

from PyQt6.QtCore import (
    Qt, QThread, QTimer, QEvent, pyqtSignal, QUrl, QStandardPaths, QRectF,
    QLocale, QEventLoop,
)
from PyQt6.QtGui import (
    QImage, QPixmap, QFont, QKeySequence, QShortcut,
    QPen, QBrush, QColor, QPainter, QDesktopServices,
)
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QSizePolicy,
    QHBoxLayout,
    QMainWindow,
    QSplitter,
    QScrollArea,
    QLabel,
    QLineEdit,
    QTextEdit,
    QToolBar,
    QFileDialog,
    QSpinBox,
    QRadioButton,
    QButtonGroup,
    QStackedWidget,
    QDoubleSpinBox,
    QPushButton,
    QCheckBox,
    QDialog,
    QProgressBar,
    QPlainTextEdit,
    QMessageBox,
    QStatusBar,
    QWidget,
    QVBoxLayout,
    QFormLayout,
    QGroupBox,
    QDockWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QGraphicsView,
    QGraphicsScene,
    QGraphicsPixmapItem,
    QGraphicsRectItem,
    QGraphicsEllipseItem,
    QGraphicsTextItem,
    QFrame,
)

# URL della guida online (sito statico pubblicato su GitHub Pages).
# Aprire con QDesktopServices.openUrl apre il browser di sistema: nessuna
# dipendenza QtWebEngine, nessuna pagina incorporata.
HELP_URL = (
    "https://vigliafg.github.io/noesis-pdf-cloner/help/"
)

# ═══════════════════════════════════════════════════════════════════════════════
#  helpers
# ═══════════════════════════════════════════════════════════════════════════════


def clean_text(text: str) -> str:
    """Remove end-of-line hyphenation: "com-\npany" -> "company"."""
    return re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)


def _image_as_png(doc, xref: int) -> tuple[bytes, str] | None:
    """Return an embedded image as ``(png_bytes, "png")`` — always Qt-decodable.

    Raw embedded images may use formats Qt cannot display (JPEG2000/``jpx``,
    JBIG2/``jb2``, …), which would render as a null pixmap in the gallery.
    Rendering through a PyMuPDF ``Pixmap`` normalizes any source format to PNG.
    CMYK images are converted to RGB first (PNG cannot encode CMYK). Falls back
    to the raw bytes (best effort) if that fails.
    """
    try:
        pix = pymupdf.Pixmap(doc, xref)
        if pix.n == 4 and not pix.alpha:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)  # CMYK → RGB
        return pix.tobytes("png"), "png"
    except Exception:
        pass
    try:
        info = doc.extract_image(xref)
    except Exception:
        return None
    data = info.get("image")
    if not data:
        return None
    return data, (info.get("ext") or "png").lower()


def _region_image(
    doc, page_num: int, clip, zoom: float, embedded_only: bool = False
) -> tuple[bytes, str] | None:
    """Extract the image under a PDF-points rect as ``(png_bytes, "png")``.

    Prefers the original embedded raster; otherwise renders the region (whole
    figure, also for composite/vector figures). Returns None when nothing can
    be extracted.

    When ``embedded_only`` is True the zone is treated as a *capture zone*
    (exclude gesture): if the selection targets an embedded image (at least
    half of it inside), the WHOLE selected zone is rendered and captured —
    so composite figures (several embedded images, image + caption/vector
    parts) are kept in full instead of a single fragment. Pure-text zones
    (header/footer/caption, no embedded image) are skipped, so excluding
    them never dumps a rendered PNG into the gallery.
    """
    if not _has_pymupdf:
        return None
    try:
        page = doc[page_num]
        rect = pymupdf.Rect(clip)
        if rect.width < 1.0 or rect.height < 1.0:
            return None

        # embedded_only (exclude gesture): if the selection targets an
        # embedded image (at least half of it inside), render and capture
        # the WHOLE selected zone — composite figures (several embedded
        # images, image + caption/vector parts) are kept in full instead of
        # a single fragment. Pure-text zones (no embedded image) are
        # skipped, so excluding a header/footer/caption never dumps a
        # rendered PNG into the gallery.
        if embedded_only:
            for img in page.get_images(full=True):
                for r in page.get_image_rects(img[0]):
                    area = r.get_area()
                    if area > 0 and (r & rect).get_area() / area >= 0.5:
                        pix = page.get_pixmap(
                            clip=rect, matrix=pymupdf.Matrix(zoom, zoom)
                        )
                        return pix.tobytes("png"), "png"
            return None

        # 1) embedded image fully inside the selection → original raster
        for img in page.get_images(full=True):
            xref = img[0]
            for r in page.get_image_rects(xref):
                if not (
                    r.x0 >= rect.x0 and r.y0 >= rect.y0
                    and r.x1 <= rect.x1 and r.y1 <= rect.y1
                ):
                    continue
                converted = _image_as_png(doc, xref)
                if converted is not None:
                    return converted

        # 2) fallback: render the selected region at high resolution
        pix = page.get_pixmap(clip=rect, matrix=pymupdf.Matrix(zoom, zoom))
        return pix.tobytes("png"), "png"
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  layout fixes (generic corrections for two-column / chapter-open pages)
# ═══════════════════════════════════════════════════════════════════════════════


def _collect_blocks(page, exclude: tuple = ()) -> list[dict]:
    """Extract text blocks (with per-span formatting) from a pymupdf page.

    ``exclude`` is a tuple of (x0, y0, x1, y1) rects (PDF points); blocks
    mostly inside one of them are dropped so the adaptive engine can rebuild
    the reading order on a manually-cleaned source.
    """
    blocks: list[dict] = []
    for blk in page.get_text("dict")["blocks"]:
        if blk.get("type") != 0:
            continue
        if _is_excluded(tuple(blk["bbox"]), exclude):
            continue
        lines: list[list[dict]] = []
        max_size = 0.0
        for line in blk["lines"]:
            spans: list[dict] = []
            for s in line["spans"]:
                t = s["text"]
                if not t.strip():
                    continue
                spans.append(
                    {
                        "text": t,
                        "size": s["size"],
                        "bold": bool(s["flags"] & 16),
                        "italic": bool(s["flags"] & 2),
                    }
                )
                max_size = max(max_size, s["size"])
            if spans:
                lines.append(spans)
        # De-hyphenate words split across a line break ("un-" + "common" →
        # "uncommon"). Only when the next line starts lowercase, so real
        # hyphenated compounds at line ends are left alone.
        if lines:
            fused: list[list[dict]] = [lines[0]]
            for nxt in lines[1:]:
                cur = fused[-1]
                if (
                    cur and nxt
                    and cur[-1]["text"].endswith("-")
                    and len(cur[-1]["text"]) > 1
                    and nxt[0]["text"][:1].islower()
                ):
                    cur[-1]["text"] = cur[-1]["text"][:-1] + nxt[0]["text"]
                    nxt = nxt[1:]
                if nxt:
                    fused.append(nxt)
            lines = fused
        if not lines:
            continue
        x0, y0, x1, y1 = blk["bbox"]
        blocks.append(
            {
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "max_size": max_size, "lines": lines,
            }
        )
    return blocks


def _strip_margin_blocks(blocks: list[dict], page_height: float) -> list[dict]:
    """Drop blocks that lie entirely in the top/bottom page margins.

    Running headers, footers and watermarks (e.g. a "Made with Xodo" banner)
    are decorative. If left in, a full-ish-width header spanning the gap
    between two columns acts as a bridge in ``_merged_column_intervals`` and
    collapses the page to a single column, breaking the reading order. The
    band is the top/bottom 7% of the page (capped at 70pt), which removes
    page chrome but keeps real body text.
    """
    band = min(0.07 * page_height, 70.0)
    if band <= 0:
        return blocks
    return [
        b for b in blocks
        if b["y1"] > band and b["y0"] < page_height - band
    ]


def _merged_column_intervals(blocks: list[dict], page_width: float) -> list[list[float]]:
    """Merge narrow blocks into per-column x-intervals, dropping margin labels.

    Margin labels, page numbers and vertical side labels are narrow (<40pt)
    and must not be mistaken for a text column.
    """
    col = [b for b in blocks if (b["x1"] - b["x0"]) < 0.6 * page_width]
    if len(col) < 4:
        return []
    intervals = sorted((b["x0"], b["x1"]) for b in col)
    merged = [list(intervals[0])]
    for x0, x1 in intervals[1:]:
        if x0 <= merged[-1][1] + 3:
            merged[-1][1] = max(merged[-1][1], x1)
        else:
            merged.append([x0, x1])
    # Drop narrow labels that sit in the outer page margins (page numbers,
    # vertical side labels); narrow lines in the middle are real content.
    return [
        m for m in merged
        if not ((m[1] - m[0]) < 40 and (m[0] < 30 or m[1] > page_width - 30))
    ]


def _detect_column_splits(blocks: list[dict], page_width: float) -> list[float]:
    """Return the x boundaries between text columns (N-1 splits for N columns).

    Handles any number of columns (two-column prose, three/four-column
    indexes). Every gap between merged column intervals that is comparable to
    the widest gap is a column boundary; narrow indentation/margin gaps are
    dropped, so they never split a column.
    """
    merged = _merged_column_intervals(blocks, page_width)
    if len(merged) < 2:
        return []
    gaps = [merged[i + 1][0] - merged[i][1] for i in range(len(merged) - 1)]
    widest = max(gaps)
    return [
        (merged[i][1] + merged[i + 1][0]) / 2
        for i, gap in enumerate(gaps)
        if gap >= 8 and gap >= 0.55 * widest
    ]


def _detect_column_split(blocks: list[dict], page_width: float):
    """Return the widest column boundary, or None if single-column."""
    merged = _merged_column_intervals(blocks, page_width)
    if len(merged) < 2:
        return None
    best_gap = 0.0
    split = page_width / 2
    for i in range(len(merged) - 1):
        gap = merged[i + 1][0] - merged[i][1]
        if gap > best_gap:
            best_gap = gap
            split = (merged[i][1] + merged[i + 1][0]) / 2
    return split if best_gap >= 8 else None


def _block_to_md(block: dict, as_column: bool) -> str:
    """Render a block to markdown, marking headings when it's a column block."""
    lines: list[str] = []
    for line in block["lines"]:
        parts: list[str] = []
        for s in line:
            t = s["text"]
            if s["bold"] and s["italic"]:
                parts.append(f"***{t}***")
            elif s["bold"]:
                parts.append(f"**{t}**")
            elif s["italic"]:
                parts.append(f"*{t}*")
            else:
                parts.append(t)
        lines.append("".join(parts).strip())

    if not as_column:
        return " ".join(lines)

    size = block["max_size"]
    level = 0
    if size >= 14:
        level = 1
    elif size >= 12:
        level = 2
    elif size >= 9.8 and any(s["bold"] for l in block["lines"] for s in l):
        level = 3

    if level == 0:
        return " ".join(lines)
    if level == 1:
        return "# " + " ".join(lines)

    heading = lines[0]
    body = " ".join(lines[1:])
    if body:
        return f"{'#' * level} {heading}\n\n{body}"
    return f"{'#' * level} {heading}"


# A caption row starts with one of these markers (e.g. "TABLE 166-3",
# "Table 5.6 Lysosomal Storage Diseases") — not a column header.
_CAPTION_RE = re.compile(r"^\s*(table|fig(?:ure)?|box|exhibit|chart)\b", re.IGNORECASE)


def _table_to_md(page, table) -> str:
    """Render a pymupdf table as a markdown table using clean per-cell text."""
    cells = sorted(table.cells, key=lambda c: (c[1], c[0]))  # by y0 then x0
    if not cells:
        return ""

    def _cell_text(cell) -> str:
        x0, y0, x1, y1 = cell
        clip = (x0 + 1.5, y0 + 1.5, max(x0 + 1.5, x1 - 1.5), max(y0 + 1.5, y1 - 1.5))
        return " ".join(page.get_textbox(clip).split()).strip()

    # Group cells into rows by their top y-coordinate.
    rows: list[tuple[float, list[tuple[float, float, str]]]] = []
    for cell in cells:
        txt = _cell_text(cell)
        if not txt:
            continue
        x0, y0, x1, _y1 = cell
        if rows and abs(rows[-1][0] - y0) < 5.0:
            rows[-1][1].append((x0, x1, txt))
        else:
            rows.append((y0, [(x0, x1, txt)]))
    for _, row in rows:
        row.sort(key=lambda c: c[0])

    if not rows:
        return ""

    def _row_xrange(row) -> tuple[float, float]:
        return min(c[0] for c in row), max(c[1] for c in row)

    max_cells = max(len(r) for _, r in rows)
    tw = table.bbox[2] - table.bbox[0]

    out: list[str] = []
    # A leading row that is not a real header is the table caption: either a
    # single full-width cell, or a row with fewer cells than the widest row
    # that spans the table width and starts with a caption marker. The latter
    # is the classic "TABLE 166-3 | EXAMPLES OF TARGETED CANCER THERAPIES",
    # which pymupdf splits into 2 cells — using it as the header would
    # truncate every data row to 2 columns.
    first_y, first = rows[0]
    x0, x1 = _row_xrange(first)
    caption_text = " ".join(t.replace("\n", " ") for _, _, t in first)
    is_caption = (
        len(first) == 1
        or (
            len(first) < max_cells
            and (x1 - x0) >= 0.9 * tw
            and bool(_CAPTION_RE.match(caption_text))
        )
    )
    if is_caption:
        out.append(f"**{caption_text}**")
        out.append("")  # blank line so the markdown renderer sees the table
        rows = rows[1:]

    if not rows:
        return "\n".join(out)

    # Column count comes from the widest row, never from the header alone:
    # a caption row (or a merged header) must not truncate the data columns.
    ncols = max(len(r) for _, r in rows)
    header = [txt.replace("\n", " ") for _, _, txt in rows[0][1]]
    header = (header + [""] * ncols)[:ncols]
    out.append("| " + " | ".join(header) + " |")
    out.append("| " + " | ".join("---" for _ in range(ncols)) + " |")
    for _, row in rows[1:]:
        cell_md = [txt.replace("\n", "<br>") for _, _, txt in row]
        cell_md = (cell_md + [""] * ncols)[:ncols]
        out.append("| " + " | ".join(cell_md) + " |")
    return "\n".join(out)


def _box_to_md(text: str) -> str:
    """Render a box's text as a single-column markdown table (first line = header)."""
    lines = []
    for ln in text.splitlines():
        ln = re.sub(r"[\x07\t]+", " ", ln)
        ln = re.sub(r"\s+", " ", ln).strip()
        if ln:
            lines.append(ln)
    if not lines:
        return ""
    out = [f"| {lines[0]} |", "| --- |"]
    out += [f"| {ln} |" for ln in lines[1:]]
    return "\n".join(out)


def _box_title(page, rect: tuple, exclude: tuple = ()) -> tuple[str, tuple | None]:
    """Text block directly above a box (same x-range) → (title, bbox or None)."""
    x0, y0, x1, y1 = rect
    for b in _collect_blocks(page, exclude):
        if not (b["y1"] <= y0 and b["y1"] >= y0 - 30):
            continue
        if not (b["x0"] >= x0 - 40 and b["x1"] <= x1 + 40):
            continue
        t = " ".join(s["text"] for line in b["lines"] for s in line)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            return t, (b["x0"], b["y0"], b["x1"], b["y1"])
    return "", None


def _is_table_legend(b: dict, table_regions: list[tuple]) -> bool:
    """Small-text legend/footnote directly below a data table.

    Abbreviation keys and footnotes under tables are real content even at
    <6.5pt (e.g. hockberg p.1430's 6.0pt key under TABLE 164.3). Free-floating
    small text (figure sub-labels like "(a) (b)", watermarks) is not content
    and stays filtered out by the 6.5pt floor.
    """
    if b["max_size"] < 5.0:
        return False
    for r in table_regions:
        if b["y0"] >= r[3] - 4 and b["y0"] - r[3] <= 45:
            if b["x0"] >= r[0] - 30 and b["x1"] <= r[2] + 30:
                return True
    return False


def _rect_overlap_area(r1: tuple, r2: tuple) -> float:
    """Intersection area of two (x0, y0, x1, y1) rects."""
    ox = max(0.0, min(r1[2], r2[2]) - max(r1[0], r2[0]))
    oy = max(0.0, min(r1[3], r2[3]) - max(r1[1], r2[1]))
    return ox * oy


def _is_excluded(rect: tuple, exclude: tuple = ()) -> bool:
    """True when ``rect`` is mostly covered by one of the excluded zones.

    A user-drawn exclusion (header, footer, figure, caption…) hides any
    block/table/box whose area is ≥50% inside it, so the adaptive engine
    rebuilds the reading order on the remaining content only.
    """
    if not exclude:
        return False
    area = (rect[2] - rect[0]) * (rect[3] - rect[1])
    if area <= 0:
        return False
    return any(_rect_overlap_area(rect, ex) / area >= 0.5 for ex in exclude)


def _norm_strip_text(t: str) -> str:
    """Normalize text for title-strip matching.

    Strips markdown table furniture and hyphens (incl. soft hyphens \u00ad)
    and collapses whitespace, so the same words written as
    ``In-\xadHospital`` or ``In-Hospital`` compare equal.
    """
    t = t.replace("\u00ad", "")
    t = re.sub(r"[|\u2014\-]", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _is_title_strip(bx: dict, boxes: list[dict]) -> bool:
    """True when ``bx`` is a thin border strip holding the title (or its
    *start*, which continues inside the box) of the box directly below it.

    Such strips are drawn as a separate rectangle, so they are detected as
    their own box and the title ends up emitted twice: once as the strip's
    markdown table and once as the **bold heading** of the box below.
    """
    r = bx["rect"]
    w, h = r[2] - r[0], r[3] - r[1]
    if h > 0.5 * w:
        return False  # not strip-shaped
    text = _norm_strip_text(bx["md"])
    if len(text) < 8:
        return False
    for other in boxes:
        if other is bx:
            continue
        o = other["rect"]
        # The strip shares its bottom border with the box below (it is the
        # box's title band). A visible gap means a stacked box, not a strip:
        # e.g. stacked citation entries would otherwise look like strips.
        if not (r[3] - 2 <= o[1] <= r[3] + 6):
            continue
        if min(r[2], o[2]) - max(r[0], o[0]) < 0.6 * w:
            continue  # different column
        title = _norm_strip_text(other["title"])
        # The strip's text is the title itself (or its start) and the title
        # belongs to the box below, which emits it as its bold heading.
        if len(title) >= 12 and title.startswith(text):
            return True
    return False


def _dedup_boxes(boxes: list[dict]) -> list[dict]:
    """Drop boxes whose area is mostly covered by a larger kept box.

    Nested/overlapping rectangles (e.g. a chart drawn as several bordered
    cells) would otherwise be emitted as duplicate tables.
    """
    ordered = sorted(
        boxes,
        key=lambda b: (b["rect"][2] - b["rect"][0]) * (b["rect"][3] - b["rect"][1]),
        reverse=True,
    )
    kept: list[dict] = []
    for bx in ordered:
        r = bx["rect"]
        area = (r[2] - r[0]) * (r[3] - r[1])
        if area <= 0:
            continue
        if any(
            _rect_overlap_area(r, k["rect"]) / area >= 0.6
            for k in kept
        ):
            continue
        kept.append(bx)
    return kept


def _detect_boxes(page, page_width: float, table_regions: list[tuple], exclude: tuple = ()) -> list[dict]:
    """Detect bordered boxes (sidebars) and render them as markdown tables.

    A box is a closed rectangle from ``get_drawings()`` that contains text and
    is not part of a data table. Returns ``[{rect, title, title_bbox, md}]``.
    """
    pw, ph = page.rect.width, page.rect.height
    boxes: list[dict] = []
    for d in page.get_drawings():
        if d["type"] not in ("fs", "s", "f"):
            continue
        r = d["rect"]
        w, h = r[2] - r[0], r[3] - r[1]
        if w < 60 or h < 30:
            continue
        if w > 0.97 * pw or h > 0.97 * ph:
            continue  # full-page frame
        if r[1] < -2 or r[3] > ph + 2:
            continue  # drawn outside the page: decorative edge strip
        if w >= 0.9 * pw and (r[1] < 60 or r[3] > ph - 60):
            continue  # running header/footer, not a content box
        rect = tuple(r)
        if _is_excluded(rect, exclude):
            continue  # manually excluded zone
        # Skip boxes that are (mostly) inside a data table — their content is
        # rendered by the table path, not the box path.
        area = (rect[2] - rect[0]) * (rect[3] - rect[1])
        if area > 0 and any(
            _rect_overlap_area(rect, tr) / area >= 0.5
            for tr in table_regions
        ):
            continue
        text = page.get_text(clip=r).strip()
        if not text:
            continue
        # Skip trivial boxes: a lone page number / short label is not a sidebar.
        n_lines = len([ln for ln in text.splitlines() if ln.strip()])
        if n_lines < 2 and len(text) < 30:
            continue
        title, tbbox = _box_title(page, rect, exclude)
        boxes.append(
            {"rect": rect, "title": title, "title_bbox": tbbox, "md": _box_to_md(text)}
        )
    # A title strip (the start of a box title in its own thin border) would
    # duplicate the title, which is already emitted as the **heading** of the
    # box below.
    boxes = [b for b in boxes if not _is_title_strip(b, boxes)]
    return _dedup_boxes(boxes)


def _column_aware_markdown(page, move_title: bool = False, exclude: tuple = ()) -> str:
    """Reconstruct a page in correct reading order.

    Body text is decomposed into consecutive paragraphs, column by column:
    within each band the left column is emitted top-to-bottom and then the
    right column top-to-bottom. Only elements that span the full page width
    (titles, full-width tables) act as horizontal separators between bands;
    single-column tables stay inside their own column, so they never split
    the other column. Every body paragraph is emitted exactly once.
    """
    page_width = page.rect.width
    page_height = page.rect.height

    # Detect data tables (rendered as markdown) and their bboxes.
    table_regions: list[tuple] = []
    table_items: list[dict] = []  # {y0, x0, x1, md}
    try:
        tabs = page.find_tables()
    except Exception:
        tabs = None
    if tabs:
        for t in tabs.tables:
            if t.row_count <= 1 and t.col_count <= 2:
                continue  # likely a chapter-title block, not a data table
            bbox = tuple(t.bbox)
            if _is_excluded(bbox, exclude):
                continue
            table_regions.append(bbox)
            md = _table_to_md(page, t)
            if md:
                table_items.append(
                    {"y0": bbox[1], "x0": bbox[0], "x1": bbox[2], "md": md}
                )

    # Detect bordered boxes (sidebars) and render them as tables too.
    boxes = _detect_boxes(page, page_width, table_regions, exclude)
    box_regions = [b["rect"] for b in boxes]
    box_titles = {b["title"] for b in boxes if b["title"]}

    def _inside(b: dict, r: tuple) -> bool:
        return (
            b["x0"] >= r[0] - 2 and b["x1"] <= r[2] + 2
            and b["y0"] >= r[1] - 2 and b["y1"] <= r[3] + 2
        )

    blocks = [
        b for b in _collect_blocks(page, exclude)
        if b["max_size"] >= 6.5 or _is_table_legend(b, table_regions)
    ]

    full_width: list[dict] = []  # spans the page → separator
    body: list[dict] = []        # column paragraphs (outside tables/boxes)
    for b in blocks:
        if any(_inside(b, r) for r in table_regions):
            continue  # covered by the markdown table
        if any(_inside(b, r) for r in box_regions):
            continue  # covered by the box table
        if box_titles:
            t = " ".join(s["text"] for line in b["lines"] for s in line)
            if re.sub(r"\s+", " ", t).strip() in box_titles:
                continue  # box title rendered above the box table
        w = b["x1"] - b["x0"]
        if w >= 0.6 * page_width:
            full_width.append(b)
        elif w >= 25:
            body.append(b)

    # Robust column boundaries, computed from all non-table blocks (incl. box
    # content): a column that is entirely a box must still count as a column,
    # otherwise the layout collapses to single-column and the box is misordered.
    split_blocks = [
        b for b in blocks if not any(_inside(b, r) for r in table_regions)
    ]
    # Headers/footers/watermarks must not bridge the column gap (they would
    # collapse the page to a single column).
    splits = _detect_column_splits(
        _strip_margin_blocks(split_blocks, page_height), page_width
    )

    def _col_of(x: float) -> int:
        return sum(1 for s in splits if x > s)

    # Move chapter title(s) out of the column flow when requested.
    titles: list[dict] = []
    if move_title:
        first_split = splits[0] if splits else page_width + 1
        titles = [b for b in body if b["max_size"] >= 14 and b["x0"] < first_split]
        body = [b for b in body if b not in titles]
        titles.sort(key=lambda b: b["max_size"])

    # Full-width separators: full-width blocks + full-width tables + boxes.
    separators: list[tuple[float, str]] = []
    for b in full_width:
        md = _block_to_md(b, as_column=False)
        if md:
            separators.append((b["y0"], md))
    for ti in table_items:
        if (ti["x1"] - ti["x0"]) >= 0.6 * page_width:
            separators.append((ti["y0"], ti["md"]))
    for bx in boxes:
        if (bx["rect"][2] - bx["rect"][0]) >= 0.6 * page_width:
            md = f"**{bx['title']}**\n\n{bx['md']}" if bx["title"] else bx["md"]
            separators.append((bx["rect"][1], md))
    separators.sort(key=lambda s: s[0])

    # Column paragraphs, decomposed top-to-bottom: (y0, x0, md).
    n_cols = len(splits) + 1
    col_items: list[list[tuple[float, float, str]]] = [[] for _ in range(n_cols)]
    for b in body:
        md = _block_to_md(b, as_column=True)
        if not md:
            continue
        item = (b["y0"], b["x0"], md)
        col_items[_col_of((b["x0"] + b["x1"]) / 2)].append(item)
    # Single-column tables and in-column boxes belong to their column, at y.
    for ti in table_items:
        if (ti["x1"] - ti["x0"]) >= 0.6 * page_width:
            continue
        mid = (ti["x0"] + ti["x1"]) / 2
        item = (ti["y0"], ti["x0"], ti["md"])
        col_items[_col_of(mid)].append(item)
    for bx in boxes:
        if (bx["rect"][2] - bx["rect"][0]) >= 0.6 * page_width:
            continue
        mid = (bx["rect"][0] + bx["rect"][2]) / 2
        md = f"**{bx['title']}**\n\n{bx['md']}" if bx["title"] else bx["md"]
        item = (bx["rect"][1], bx["rect"][0], md)
        col_items[_col_of(mid)].append(item)
    for items in col_items:
        items.sort(key=lambda it: (it[0], it[1]))

    out: list[str] = []
    for t in titles:
        out.append(_block_to_md(t, as_column=True))

    sep_marks = [y0 for y0, _ in separators]

    def _band(y0: float) -> int:
        return sum(1 for sy in sep_marks if y0 >= sy)

    n_seps = len(separators)
    for band_idx in range(n_seps + 1):
        for items in col_items:
            out.extend(md for y0, _x, md in items if _band(y0) == band_idx)
        if band_idx < n_seps:
            out.append(separators[band_idx][1])

    return "\n\n".join(out)


def _collect_lines(page) -> list[dict]:
    """Text LINES with bbox + per-span formatting, in document order.

    Unlike ``_collect_blocks`` (which keeps whole paragraphs), this keeps the
    line granularity with each line's bbox and source-block index, so a small
    inclusion zone can capture just a few lines of a larger paragraph.
    """
    lines: list[dict] = []
    for bi, blk in enumerate(page.get_text("dict")["blocks"]):
        if blk.get("type") != 0:
            continue
        for line in blk["lines"]:
            spans: list[dict] = []
            size = 0.0
            for s in line["spans"]:
                t = s["text"]
                if not t.strip():
                    continue
                spans.append(
                    {
                        "text": t,
                        "size": s["size"],
                        "bold": bool(s["flags"] & 16),
                        "italic": bool(s["flags"] & 2),
                    }
                )
                size = max(size, s["size"])
            if not spans:
                continue
            x0, y0, x1, y1 = line["bbox"]
            lines.append(
                {
                    "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                    "max_size": size, "spans": spans, "blk": bi,
                }
            )
    return lines


def _lines_to_block(lns: list[dict]) -> dict:
    """Reassemble a group of lines into a block dict for ``_block_to_md``."""
    return {
        "x0": min(l["x0"] for l in lns), "y0": min(l["y0"] for l in lns),
        "x1": max(l["x1"] for l in lns), "y1": max(l["y1"] for l in lns),
        "max_size": max(l["max_size"] for l in lns),
        "lines": [[dict(s) for s in l["spans"]] for l in lns],
    }


def _line_rect(ln: dict) -> tuple:
    return (ln["x0"], ln["y0"], ln["x1"], ln["y1"])


def _inclusion_order_markdown(page, zones, exclude: tuple = ()) -> str:
    """Emit text following the numbered inclusion zones (whitelist + order).

    ``zones`` are (x0, y0, x1, y1) PDF rects in reading order: index 0 is box
    1, index 1 is box 2, etc. Works at LINE level: every line whose area is
    >=50% inside a zone is kept (so a small zone over two/three lines works
    even when the rest of the paragraph is outside). Lines are emitted zone by
    zone, top-to-bottom (then left-to-right) within each zone; consecutive
    lines of the same paragraph stay joined. ``exclude`` zones are dropped
    first (red wins), so the two manual tools compose: red removes noise,
    green sets the order.
    """
    lines = _collect_lines(page)
    if exclude:
        lines = [ln for ln in lines if not _is_excluded(_line_rect(ln), exclude)]

    emitted: set[int] = set()
    out: list[str] = []
    for zone in zones:
        inside = [
            (i, ln) for i, ln in enumerate(lines)
            if i not in emitted and _is_excluded(_line_rect(ln), (zone,))
        ]
        inside.sort(key=lambda il: (il[1]["y0"], il[1]["x0"]))
        # Raggruppa righe consecutive dello stesso blocco in un paragrafo.
        groups: list[tuple[int, list[int]]] = []
        for i, ln in inside:
            if groups and groups[-1][0] == ln["blk"]:
                groups[-1][1].append(i)
            else:
                groups.append((ln["blk"], [i]))
        for _blk, idxs in groups:
            emitted.update(idxs)
            md = _block_to_md(_lines_to_block([lines[i] for i in idxs]), as_column=True)
            if md:
                out.append(md)
    return "\n\n".join(out)


def _page_needs_column_reorder(page) -> bool:
    """Heuristic: does this page need the two-column reorder fix?

    True only when a clear column split exists, both columns are populated
    (≥2 blocks each) and they run side by side (their blocks overlap
    vertically) — the exact condition where a line-by-line extraction
    interleaves the two columns.
    """
    # Data tables are rendered separately; exclude their cells from the split
    # detection (same rule as _column_aware_markdown).
    table_regions: list[tuple] = []
    try:
        tabs = page.find_tables()
    except Exception:
        tabs = None
    if tabs:
        for t in tabs.tables:
            if t.row_count <= 1 and t.col_count <= 2:
                continue  # likely a chapter-title block, not a data table
            table_regions.append(tuple(t.bbox))

    def _inside(b: dict, r: tuple) -> bool:
        return (
            b["x0"] >= r[0] - 2 and b["x1"] <= r[2] + 2
            and b["y0"] >= r[1] - 2 and b["y1"] <= r[3] + 2
        )

    blocks = [
        b for b in _collect_blocks(page)
        if b["max_size"] >= 6.5 and not any(_inside(b, r) for r in table_regions)
    ]
    page_width = page.rect.width
    # Same column filter as _column_aware_markdown: narrow, but not stray marks.
    columns = [
        b for b in blocks
        if (b["x1"] - b["x0"]) < 0.6 * page_width and (b["x1"] - b["x0"]) >= 25
    ]
    # Headers/footers/watermarks must not bridge the column gap.
    columns = _strip_margin_blocks(columns, page.rect.height)
    splits = _detect_column_splits(columns, page_width)
    if not splits:
        return False

    left = [b for b in columns if b["x1"] <= splits[0]]
    right = [b for b in columns if b["x0"] >= splits[0]]
    if len(left) < 2 or len(right) < 2:
        return False

    return any(
        lb["y0"] <= rb["y1"] and rb["y0"] <= lb["y1"]
        for lb in left
        for rb in right
    )


def _reading_normalize(text: str) -> str:
    """Collapse whitespace + de-hyphenate line breaks, lowercased.

    Used to align extracted markdown against pymupdf's per-column raw text
    (the two may differ in line breaks and end-of-line hyphenation).
    """
    text = re.sub(r"-\s*\n\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.lower().strip()


def _longest_prefix_in(s: str, target: str) -> int:
    """Length of the longest word-aligned prefix of ``s`` that is in ``target``."""
    for i in range(len(s), -1, -1):
        if i < len(s) and s[i] != " ":
            continue  # not a word boundary
        if s[:i] in target:
            return i
    return 0


def _longest_suffix_in(s: str, target: str) -> int:
    """Length of the longest word-aligned suffix of ``s`` that is in ``target``."""
    for i in range(len(s), -1, -1):
        start = len(s) - i
        if start > 0 and s[start - 1] != " ":
            continue  # suffix doesn't start at a word boundary
        if s[-i:] in target:
            return i
    return 0


def _split_cross_column_paragraphs(md: str, page) -> str:
    """Re-split paragraphs that a backend glued across the two-column boundary.

    Docling's layout model occasionally merges a left-column element (e.g. the
    last cell of a side box) with the right column's opening sentence into a
    single paragraph. Using the detected column split and pymupdf's per-column
    raw text, a paragraph whose head lives in the left column and whose tail
    lives in the right column is split at that boundary.

    Degrades to ``md`` unchanged when no clear column split exists.
    """
    if not _has_pymupdf:
        return md
    # Headers/footers/watermarks must not bridge the column gap (they would
    # hide the real split and disable the re-split).
    blocks = _strip_margin_blocks(_collect_blocks(page), page.rect.height)
    split = _detect_column_split(blocks, page.rect.width)
    if split is None:
        return md
    width, height = page.rect.width, page.rect.height
    left_text = _reading_normalize(
        page.get_text(clip=pymupdf.Rect(0, 0, split, height))
    )
    right_text = _reading_normalize(
        page.get_text(clip=pymupdf.Rect(split, 0, width, height))
    )

    out: list[str] = []
    for para in md.split("\n\n"):
        stripped = para.strip()
        words = stripped.split()
        if len(words) < 8:
            out.append(stripped)
            continue
        norm = _reading_normalize(stripped)
        p = _longest_prefix_in(norm, left_text)
        s = _longest_suffix_in(norm, right_text)
        if not (p > 0 and s > 0 and p + s >= len(norm) - 1 and p < len(norm) - s):
            out.append(stripped)
            continue
        cut = len(norm[:p].split())
        if not (3 <= cut <= len(words) - 3):
            out.append(stripped)
            continue
        out.append(" ".join(words[:cut]))
        out.append(" ".join(words[cut:]))
    return "\n\n".join(p for p in out if p)


def _spacing_fixes(md: str) -> str:
    """Generic cosmetic spacing fixes for markdown artifacts."""
    # bold chapter cross-reference glued to the following word: **134**and
    md = re.sub(r"\*\*(\d+)\*\*(?=\S)", r"**\1** ", md)
    # underscore-italic word followed by a comma glued to the next word: _a_,_b_
    md = re.sub(r"(_[^_]+_),", r"\1, ", md)
    return md


# ═══════════════════════════════════════════════════════════════════════════════
#  Translation engines — Google Translate and Microsoft Edge (stdlib only, no API key)
# ═══════════════════════════════════════════════════════════════════════════════

_GT_URL = "https://translate.googleapis.com/translate_a/single"
_GT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


# Google progressively blocks the legacy "gtx" client (HTTP 429 "Sorry...")
# on many networks/IPs. Three *free, no-key* endpoints are tried in order:
#   1. the Chrome-extension client "dict-chrome-ex" on the classic endpoint;
#   2. Google's "translate-pa" endpoint with its embedded public browser key
#      (the same endpoint the bookfere calibre plugin ships as
#      "Google (Free) - New");
#   3. the legacy "gtx" client as a last-resort safety net.
_GT_PA_URL = "https://translate-pa.googleapis.com/v1/translate"
_GT_PA_KEY = "AIzaSyDLEeFI5OtFBwYBIoK_jj5m32rZK5CkCXA"


def _gt_request(url: str, params: dict) -> object:
    """GET a Google endpoint and return the parsed JSON (raises on failure)."""
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full_url, headers=_GT_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _gt_segments(text: str, source: str, target: str, client: str) -> str:
    """Classic translate_a/single response: list of [text, ...] segments."""
    result = _gt_request(_GT_URL, {
        "client": client, "sl": source, "tl": target, "dt": "t", "q": text,
    })
    if result and result[0]:
        return "".join(item[0] for item in result[0] if item[0])
    return ""


def _gt_pa(text: str, source: str, target: str) -> str:
    """translate-pa v1/translate response: {"translation": "..."}."""
    result = _gt_request(_GT_PA_URL, {
        "params.client": "gtx",
        "query.source_language": source,
        "query.target_language": target,
        "query.display_language": "en-US",
        "data_types": "TRANSLATION",
        "key": _GT_PA_KEY,
        "query.text": text,
    })
    if isinstance(result, dict) and result.get("translation"):
        return result["translation"]
    return ""


def _gt_translate_one(text: str, source: str, target: str) -> str:
    """Call a free Google Translate endpoint for a single chunk of text.

    Only network/HTTP failures move to the next endpoint; an answered-but-
    empty response stops the chain (the engine is reachable, there is simply
    nothing to return).
    """
    attempts = (
        lambda: _gt_segments(text, source, target, "dict-chrome-ex"),
        lambda: _gt_pa(text, source, target),
        lambda: _gt_segments(text, source, target, "gtx"),
    )
    last_error: Exception | None = None
    for attempt in attempts:
        try:
            out = attempt()
            if out:
                return out
            return text
        except Exception as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    return text


# Free Microsoft Edge endpoint (same scheme as the Ebook Translator calibre
# plugin): POST a JSON array of strings, get translations back in order.
_MS_URL = "https://edge.microsoft.com/translate/translatetext"
# Microsoft uses different codes than Google for a few languages.
_MS_LANG_CODES = {
    "zh": "zh-Hans",  # Google "zh" = Simplified Chinese
}


def _ms_lang_code(code: str) -> str:
    """Map an app language code to the Microsoft Edge API code."""
    return _MS_LANG_CODES.get(code, code)


def _ms_translate_one(text: str, source: str, target: str) -> str:
    """Call the free Microsoft Edge Translate API for a single chunk."""
    params = {"isEnterpriseClient": "False", "to": _ms_lang_code(target)}
    if source and source != "auto":
        params["from"] = _ms_lang_code(source)
    full_url = f"{_MS_URL}?{urllib.parse.urlencode(params)}"
    body = json.dumps([text]).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(
        full_url, data=body, headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    try:
        return result[0]["translations"][0]["text"]
    except (IndexError, KeyError, TypeError):
        return text


def _translate_one(
    engine: str, text: str, source: str, target: str,
    _stats: _TranslateStats | None = None,
) -> str:
    """Translate a single chunk with the chosen engine (google|microsoft)."""
    if _stats is not None:
        _stats.attempted()
    if engine == "microsoft":
        out = _ms_translate_one(text, source, target)
    else:
        out = _gt_translate_one(text, source, target)
    if _stats is not None:
        _stats.succeeded()
    return out


# ═══════════════════════════════════════════════════════════════════════════════
#  Tesseract OCR (scanned PDFs)
# ═══════════════════════════════════════════════════════════════════════════════

# App language code → Tesseract traineddata (tessdata_fast) name.
_TESS_LANG_CODES = {
    "en": "eng", "it": "ita", "fr": "fra", "de": "deu", "es": "spa",
    "pt": "por", "nl": "nld", "pl": "pol", "ru": "rus", "zh": "chi_sim",
    "ja": "jpn", "ko": "kor", "ar": "ara", "tr": "tur",
}
# Source "auto" → Tesseract language combination: best match per line among
# the Latin/Cyrillic/RTL languages bundled with the app. CJK is excluded
# because mixing scripts degrades accuracy — for zh/ja/ko documents the user
# sets the source language explicitly (and the resource is bundled anyway).
_AUTO_TESS_LANGS = "eng+deu+fra+ita+spa+por+nld+pol+rus+tur+ara"


def _tess_lang_code(code: str | None) -> str:
    """Map an app language code to the Tesseract OCR language string."""
    if not code or code == "auto":
        return _AUTO_TESS_LANGS
    return _TESS_LANG_CODES.get(code, "eng")


def _setup_bundled_tesseract() -> None:
    """Point PyMuPDF OCR at the Tesseract bundled in frozen (PyInstaller) builds.

    In a frozen app the ``tesseract`` binary, its shared libraries (``lib/``)
    and the ``tessdata/`` directory land next to the extracted payload
    (``sys._MEIPASS``).  Make them discoverable via PATH / TESSDATA_PREFIX and
    LD_LIBRARY_PATH / DYLD_LIBRARY_PATH (appended, so the app's own bundled
    libraries keep precedence) so MuPDF's OCR works without a system install.
    """
    if not getattr(sys, "frozen", False):
        return
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
    tess_exe = os.path.join(base, "tesseract.exe" if os.name == "nt" else "tesseract")
    if os.path.exists(tess_exe):
        os.environ["PATH"] = base + os.pathsep + os.environ.get("PATH", "")
    lib_dir = os.path.join(base, "lib")
    if os.path.isdir(lib_dir):
        if os.name == "nt":
            os.environ["PATH"] = (
                os.environ.get("PATH", "") + os.pathsep + lib_dir
            )
        else:
            existing = os.environ.get("LD_LIBRARY_PATH") or ""
            os.environ["LD_LIBRARY_PATH"] = (
                existing + os.pathsep + lib_dir if existing else lib_dir
            )
            existing = os.environ.get("DYLD_LIBRARY_PATH") or ""
            os.environ["DYLD_LIBRARY_PATH"] = (
                existing + os.pathsep + lib_dir if existing else lib_dir
            )
    tessdata = os.path.join(base, "tessdata")
    if os.path.isdir(tessdata):
        os.environ["TESSDATA_PREFIX"] = tessdata


def _extract_pymupdf4llm(
    path: str, page_num: int, ocr_language: str | None = None
) -> str:
    """Extract a page to Markdown (native text or Tesseract OCR for scans).

    ``ocr_language`` is a Tesseract language or "+"-joined combination.  If
    the requested language is unavailable (e.g. the auto combination on a
    system that bundles only English), it degrades to ``eng`` before giving
    up, so extraction never hard-fails on a language mismatch.
    """
    if not _has_pymupdf4llm:
        return T("extract.no_pymupdf4llm")
    attempts = [ocr_language] if ocr_language else ["eng"]
    if attempts[-1] != "eng":
        attempts.append("eng")
    last_error: Exception | None = None
    for lang in attempts:
        try:
            kwargs: dict = {}
            if lang:
                kwargs["ocr_language"] = lang
            md = pymupdf4llm.to_markdown(path, pages=[page_num], **kwargs)
            return md.strip() or T("extract.empty_page")
        except Exception as e:  # noqa: BLE001 — degrada al fallback, non crasha
            last_error = e
    return T("extract.error", e=last_error)


def _apply_engine_on_page(
    page, text: str, exclude: tuple = (), include: tuple = ()
) -> tuple[str, str]:
    """Apply the adaptive layout engine to ``text`` using a pymupdf page.

    ``include`` (numbered inclusion zones, reading order) takes precedence:
    it rebuilds the text as a whitelist ordered by zone number.  Otherwise
    the v1 pipeline applies: with ``exclude`` non-empty the page is rebuilt
    skipping those zones (manual cleaning) before the cosmetic fixes; with
    neither, the automatic plan is applied unchanged.  Returns
    ``(text, label)`` where label is "auto" or "manual".  This is CPU-bound
    (profile_page analyzes the page) and slow on scanned PDFs, so callers
    run it off the GUI thread.
    """
    label = "manual" if (include or exclude) else "auto"
    try:
        if include:
            return _inclusion_order_markdown(page, include, exclude=exclude) or text, label
        profile = layout_engine.profile_page(page, exclude=exclude)
        plan = layout_engine.plan_fixes(profile, "PyMuPDF4LLM ⚡", mode="auto")
        if exclude:
            cleaned = _column_aware_markdown(page, exclude=exclude) or text
            plan = [f for f in plan if f.id != "reorder_columns"]
            return layout_engine.apply_plan(cleaned, page, profile, plan) or cleaned, label
        return layout_engine.apply_plan(text, page, profile, plan) or text, label
    except Exception:
        return text, label


def _apply_engine_standalone(
    path: str, page_num: int, text: str, exclude: tuple = (), include: tuple = ()
) -> tuple[str, str]:
    """Apply the layout engine on a freshly opened document (background thread)."""
    label = "manual" if (include or exclude) else "auto"
    try:
        with pymupdf.open(path) as doc:
            return _apply_engine_on_page(doc[page_num], text, exclude=exclude, include=include)
    except Exception:
        return text, label


# Markdown structural patterns protected during translation.
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_TABLE_RE = re.compile(
    r"^\s*\|[^\n]*\|\s*\n\s*\|[\s:|-]+\|\s*\n(?:\s*\|[^\n]*\|\s*\n?)+",
    re.MULTILINE,
)
_SEP_CELL_RE = re.compile(r":?-{3,}:?")


_MAX_WORKERS = 8  # concurrent translation requests (I/O-bound)


class TranslationError(RuntimeError):
    """Raised when a translation engine fails on every chunk of a request.

    Individual chunk failures still degrade gracefully to the original text;
    only a *total* failure (engine unreachable/blocked on this network) is
    raised so the UI can surface it instead of silently showing untranslated
    text.
    """


class _TranslateStats:
    """Attempt/ok counters shared across the concurrent translation chunks.

    ``list.append`` is atomic under the GIL, so no lock is needed even though
    paragraphs (and their nested table-cell/sub-chunk calls) run in worker
    threads.
    """

    def __init__(self) -> None:
        self._attempts: list[int] = []
        self._ok: list[int] = []

    @property
    def attempts(self) -> int:
        return len(self._attempts)

    @property
    def ok(self) -> int:
        return len(self._ok)

    def attempted(self) -> None:
        self._attempts.append(1)

    def succeeded(self) -> None:
        self._ok.append(1)


def _translate_cell(
    cell: str, source: str, target: str, engine: str = "google",
    _stats: _TranslateStats | None = None,
) -> str:
    """Translate one table cell, falling back to the original on failure."""
    try:
        return _translate_one(
            engine, cell, source, target, _stats=_stats
        ).strip()
    except Exception:
        return cell


def _translate_table(
    table: str, source: str, target: str, engine: str = "google",
    _stats: _TranslateStats | None = None,
) -> str:
    """Translate the cell contents of a markdown table, keeping its structure.

    All distinct translatable cells are fetched concurrently (one request per
    cell), then placed back in their original positions.
    """
    lines = [ln.strip() for ln in table.strip().splitlines()]
    out: list[str] = []

    def _cells(ln: str) -> list[str]:
        return [c.strip() for c in ln.strip().strip("|").split("|")]

    # Collect the distinct cells that actually need translation.
    unique: list[str] = []
    seen: set[str] = set()
    for ln in lines:
        if not ln.startswith("|"):
            continue
        cells = _cells(ln)
        if all(_SEP_CELL_RE.fullmatch(c) for c in cells):
            continue  # separator row kept verbatim
        for c in cells:
            if c and re.search(r"[A-Za-z]", c) and c not in seen:
                seen.add(c)
                unique.append(c)

    cache: dict[str, str] = {}
    if unique:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(_MAX_WORKERS, len(unique))
        ) as pool:
            futures = {
                pool.submit(_translate_cell, c, source, target, engine, _stats): c
                for c in unique
            }
            for fut in concurrent.futures.as_completed(futures):
                cache[futures[fut]] = fut.result()

    for ln in lines:
        if not ln.startswith("|"):
            out.append(ln)
            continue
        cells = _cells(ln)
        if all(_SEP_CELL_RE.fullmatch(c) for c in cells):
            out.append(ln)  # separator row kept verbatim
            continue
        translated: list[str] = []
        for c in cells:
            # Numbers/symbol-only cells are left untouched (faster, safer).
            if not c or not re.search(r"[A-Za-z]", c):
                translated.append(c)
            else:
                translated.append(cache.get(c, c))
        out.append("| " + " | ".join(translated) + " |")
    return "\n".join(out)


def _translate_paragraph(
    para: str,
    source: str,
    target: str,
    chunk_size: int,
    engine: str = "google",
    _stats: _TranslateStats | None = None,
) -> str:
    """Translate one paragraph, protecting markdown tables and image links."""
    protected: dict[str, str] = {}

    def _protect(kind: str, value: str) -> str:
        tok = f"@@{kind}{len(protected)}@@"
        protected[tok] = value
        return tok

    # Image links: never send URLs to the translator.
    para = _MD_IMAGE_RE.sub(lambda m: _protect("IMG", m.group(0)), para)

    # Tables: translate their cells, then protect the rebuilt table.
    def _table_repl(m):
        return _protect(
            "TBL", _translate_table(m.group(0), source, target, engine, _stats)
        )

    para = _MD_TABLE_RE.sub(_table_repl, para)

    # Nothing left to translate (only protected tokens) → skip the API call.
    if not re.search(r"[^\W\d_]", re.sub(r"@@[A-Z]+\d+@@", "", para)):
        out = para
    elif len(para) <= chunk_size:
        try:
            out = _translate_one(engine, para, source, target, _stats=_stats)
        except Exception:
            out = para
    else:
        # Long paragraph → split at sentence-ish boundaries.
        sub_paras = re.split(r"(?<=[.!?])\s+", para)
        sub_chunks: list[str] = []
        current: list[str] = []
        cur_len = 0
        for sub in sub_paras:
            if cur_len + len(sub) > chunk_size and current:
                sub_chunks.append(" ".join(current))
                current = []
                cur_len = 0
            current.append(sub)
            cur_len += len(sub)
        if current:
            sub_chunks.append(" ".join(current))
        sub_translated: list[str] = []
        for ch in sub_chunks:
            try:
                sub_translated.append(
                    _translate_one(engine, ch, source, target, _stats=_stats)
                )
            except Exception:
                sub_translated.append(ch)
        out = " ".join(sub_translated)

    # Google may add spaces around/inside tokens; normalize them back.
    out = re.sub(r"@@\s*([A-Z]+)\s*(\d+)\s*@@", r"@@\1\2@@", out)
    for tok, value in protected.items():
        out = out.replace(tok, value)
    return out


def translate_text(
    text: str,
    source: str = "en",
    target: str = "it",
    engine: str = "google",
    chunk_size: int = 1500,
) -> str:
    """Translate text using the chosen engine's public API.

    ``engine`` is "google" or "microsoft".  Translates **each paragraph
    independently** (split on ``\n\n``) so paragraph breaks never pass
    through the API, and fetches those paragraphs **concurrently** so a short
    page doesn't wait on many sequential round-trips.  Markdown tables and
    image links are protected so the translator doesn't mangle their syntax;
    table cell contents are translated individually (also concurrently).
    """
    if not text or not text.strip():
        return text

    paragraphs = text.split("\n\n")
    results: list[str] = [""] * len(paragraphs)
    tasks = [(i, p) for i, p in enumerate(paragraphs) if p.strip()]
    if not tasks:
        return text

    stats = _TranslateStats()
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(_MAX_WORKERS, len(tasks))
    ) as pool:
        futures = {
            pool.submit(
                _translate_paragraph, p, source, target, chunk_size, engine, stats
            ): i
            for i, p in tasks
        }
        for fut in concurrent.futures.as_completed(futures):
            i = futures[fut]
            try:
                results[i] = fut.result()
            except Exception:
                results[i] = paragraphs[i]

    # Restore blank paragraphs (the ``\n\n`` separators) in their positions.
    for i, p in enumerate(paragraphs):
        if not p.strip():
            results[i] = p

    if stats.attempts > 0 and stats.ok == 0:
        raise TranslationError(
            f"engine '{engine}' failed on all {stats.attempts} request(s)"
        )
    return "\n\n".join(results)


def translate_text_google(
    text: str, source: str = "en", target: str = "it", chunk_size: int = 1500
) -> str:
    """Backward-compatible wrapper: translate text with Google Translate."""
    return translate_text(
        text, source=source, target=target, engine="google", chunk_size=chunk_size
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  app data / i18n bootstrap (Qt helpers)
# ═══════════════════════════════════════════════════════════════════════════════


def _app_data_base() -> Path:
    """User-writable app-data dir (version-independent, survives updates)."""
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    )
    if not base:
        base = str(Path.home() / ".noesis-pdf-reader")
    return Path(base)


def _config_file_path() -> Path:
    """Path of the UI-language config file (persists across versions)."""
    return _app_data_base() / "config.json"


def _detect_os_lang() -> str:
    """Best-matching UI language for the OS locale, or 'it'."""
    try:
        name = QLocale.system().name()  # e.g. "fr_FR", "en-US", "C"
    except Exception:
        return "it"
    code = name.split("_")[0].split("-")[0].lower()
    return code if code in LANGUAGES else "it"


# ═══════════════════════════════════════════════════════════════════════════════
#  widgets
# ═══════════════════════════════════════════════════════════════════════════════


class PdfPageView(QGraphicsView):
    """Left panel — displays the rendered PDF page.

    In "select mode" the user can drag a rubber-band rectangle; the selection
    is emitted in scene coordinates (full-resolution pixels of the rendered
    page), which the caller converts back to PDF points.
    """

    # x0, y0, x1, y1 in scene (full-res pixmap) coordinates
    region_selected = pyqtSignal(float, float, float, float)
    region_excluded = pyqtSignal(float, float, float, float)
    region_included = pyqtSignal(float, float, float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(300)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setBackgroundBrush(QColor(43, 43, 43))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self._full_pixmap: QPixmap | None = None
        self._pix_item: QGraphicsPixmapItem | None = None
        self._text_item: QGraphicsTextItem | None = None
        self._view_zoom: float = 1.0  # visible zoom; 1.0 = fit-to-view

        self._select_mode = False
        self._exclude_mode = False
        self._include_mode = False
        self._exclusion_items: list[QGraphicsRectItem] = []
        self._inclusion_items: list[QGraphicsRectItem] = []
        self._rubber_item: QGraphicsRectItem | None = None
        self._rubber_origin = None
        self._band_pen = QPen(QColor(74, 144, 217), 2, Qt.PenStyle.DashLine)
        self._band_brush = QBrush(QColor(74, 144, 217, 70))
        self._exclude_pen = QPen(QColor(217, 83, 79), 2, Qt.PenStyle.DashLine)
        self._exclude_brush = QBrush(QColor(217, 83, 79, 70))
        self._include_pen = QPen(QColor(92, 184, 92), 2, Qt.PenStyle.DashLine)
        self._include_brush = QBrush(QColor(92, 184, 92, 70))

        self._start_hint = True  # retranslate() re-shows it only while idle
        self._show_message_text(T("view.start_hint"))

    # ── scene management ──────────────────────────────────────────────

    def _clear_scene(self):
        self._scene.clear()
        self._pix_item = None
        self._text_item = None
        self._rubber_item = None
        self._rubber_origin = None
        self._exclusion_items = []
        self._inclusion_items = []

    def _show_message_text(self, text: str):
        self._clear_scene()
        item = QGraphicsTextItem(text)
        item.setDefaultTextColor(QColor(136, 136, 136))
        item.setFont(QFont("Segoe UI", 14))
        self._text_item = item
        self._scene.addItem(item)
        r = item.boundingRect()
        item.setPos(-r.width() / 2, -r.height() / 2)
        self._scene.setSceneRect(
            -r.width() / 2 - 20, -r.height() / 2 - 20,
            r.width() + 40, r.height() + 40,
        )

    def show_page(self, pixmap: QPixmap | None):
        """Store the full-resolution pixmap and scale it to fit the view."""
        if pixmap is None:
            self._full_pixmap = None
            self._start_hint = False
            self._show_message_text(T("view.page_unavailable"))
            return
        self._start_hint = False
        self._full_pixmap = pixmap
        self._clear_scene()
        self._pix_item = self._scene.addPixmap(pixmap)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self._fit_to_view()

    def show_message(self, text: str):
        """Show a plain status message instead of a page (e.g. while loading)."""
        self._full_pixmap = None
        self._start_hint = False
        self._show_message_text(text)

    def retranslate(self):
        """Re-apply UI strings after a language switch."""
        if self._full_pixmap is None and self._start_hint:
            self._show_message_text(T("view.start_hint"))

    # ── selection mode ───────────────────────────────────────────────────

    def set_select_mode(self, enabled: bool):
        """Enable/disable rubber-band region selection."""
        self._select_mode = enabled
        self._clear_rubber()
        if enabled:
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
        else:
            self.unsetCursor()

    def set_exclude_mode(self, enabled: bool):
        """Enable/disable rubber-band zone exclusion."""
        self._exclude_mode = enabled
        self._clear_rubber()
        if enabled:
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
        else:
            self.unsetCursor()

    def set_include_mode(self, enabled: bool):
        """Enable/disable rubber-band zone inclusion (numbered reading order)."""
        self._include_mode = enabled
        self._clear_rubber()
        if enabled:
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
        else:
            self.unsetCursor()

    def _interactive(self) -> bool:
        return self._select_mode or self._exclude_mode or self._include_mode

    def _rubber_style(self) -> tuple[QPen, QBrush]:
        if self._exclude_mode:
            return self._exclude_pen, self._exclude_brush
        if self._include_mode:
            return self._include_pen, self._include_brush
        return self._band_pen, self._band_brush

    def _clear_exclusion_overlay(self):
        for item in self._exclusion_items:
            self._scene.removeItem(item)
        self._exclusion_items = []

    def show_excluded_zones(self, zones):
        """Draw the excluded zones (scene coords) as red overlays."""
        self._clear_exclusion_overlay()
        pen = QPen(QColor(217, 83, 79), 2, Qt.PenStyle.SolidLine)
        brush = QBrush(QColor(217, 83, 79, 60))
        for x0, y0, x1, y1 in zones:
            item = QGraphicsRectItem(QRectF(x0, y0, x1 - x0, y1 - y0))
            item.setPen(pen)
            item.setBrush(brush)
            item.setZValue(10)
            self._scene.addItem(item)
            self._exclusion_items.append(item)

    def _clear_inclusion_overlay(self):
        for item in self._inclusion_items:
            self._scene.removeItem(item)
        self._inclusion_items = []

    def show_inclusion_zones(self, zones):
        """Draw the numbered inclusion zones (scene coords) as green overlays."""
        self._clear_inclusion_overlay()
        pen = QPen(QColor(92, 184, 92), 2, Qt.PenStyle.SolidLine)
        brush = QBrush(QColor(92, 184, 92, 60))
        for idx, (x0, y0, x1, y1) in enumerate(zones):
            item = QGraphicsRectItem(QRectF(x0, y0, x1 - x0, y1 - y0))
            item.setPen(pen)
            item.setBrush(brush)
            item.setZValue(10)
            self._scene.addItem(item)
            self._inclusion_items.append(item)
            # Badge circolare verde scuro con il numero, ben visibile su
            # qualunque sfondo (il verde traslucido del box non basta).
            cx = x0 + (x1 - x0) / 2
            cy = y0 + (y1 - y0) / 2
            badge = QGraphicsEllipseItem(QRectF(cx - 34, cy - 34, 68, 68))
            badge.setBrush(QBrush(QColor(46, 125, 50)))
            badge.setPen(QPen(QColor(255, 255, 255), 2))
            badge.setZValue(11)
            self._scene.addItem(badge)
            self._inclusion_items.append(badge)
            label = QGraphicsTextItem(str(idx + 1))
            label.setDefaultTextColor(QColor(255, 255, 255))
            label.setFont(QFont("Segoe UI", 48, QFont.Weight.Bold))
            r = label.boundingRect()
            label.setPos(cx - r.width() / 2, cy - r.height() / 2)
            label.setZValue(12)
            self._scene.addItem(label)
            self._inclusion_items.append(label)

    def _clear_rubber(self):
        if self._rubber_item is not None:
            self._scene.removeItem(self._rubber_item)
            self._rubber_item = None
        self._rubber_origin = None

    # ── sizing ──────────────────────────────────────────────────────────

    def set_view_zoom(self, zoom: float):
        """Set the visible zoom factor on top of the fitted page (1.0 = fit)."""
        self._view_zoom = zoom
        if self._pix_item is not None:
            self._fit_to_view()

    def _fit_to_view(self):
        """Scale the full-resolution pixmap to fit the current view size."""
        if self._pix_item is not None:
            self.fitInView(self._pix_item, Qt.AspectRatioMode.KeepAspectRatio)
            if self._view_zoom != 1.0:
                self.scale(self._view_zoom, self._view_zoom)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_to_view()

    # ── rubber band mouse handling ─────────────────────────────────────

    def mousePressEvent(self, event):
        if (
            self._interactive()
            and self._pix_item is not None
            and event.button() == Qt.MouseButton.LeftButton
        ):
            try:
                self._clear_rubber()
                pos = self.mapToScene(event.position().toPoint())
                self._rubber_origin = pos
                self._rubber_item = QGraphicsRectItem(QRectF(pos, pos))
                pen, brush = self._rubber_style()
                self._rubber_item.setPen(pen)
                self._rubber_item.setBrush(brush)
                self._scene.addItem(self._rubber_item)
            except Exception:
                # Never let an exception escape a virtual handler: in PyQt6
                # that aborts the whole process.
                self._clear_rubber()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (
            self._interactive()
            and self._rubber_item is not None
            and self._rubber_origin is not None
        ):
            try:
                cur = self.mapToScene(event.position().toPoint())
                self._rubber_item.setRect(
                    QRectF(self._rubber_origin, cur).normalized()
                )
            except Exception:
                self._clear_rubber()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if (
            self._interactive()
            and self._rubber_item is not None
            and self._rubber_origin is not None
            and event.button() == Qt.MouseButton.LeftButton
        ):
            try:
                cur = self.mapToScene(event.position().toPoint())
                rect = QRectF(self._rubber_origin, cur).normalized()
                self._clear_rubber()
                if rect.width() >= 4.0 and rect.height() >= 4.0:
                    if self._exclude_mode:
                        self.region_excluded.emit(
                            rect.left(), rect.top(), rect.right(), rect.bottom()
                        )
                    elif self._include_mode:
                        self.region_included.emit(
                            rect.left(), rect.top(), rect.right(), rect.bottom()
                        )
                    else:
                        self.region_selected.emit(
                            rect.left(), rect.top(), rect.right(), rect.bottom()
                        )
            except Exception:
                self._clear_rubber()
            event.accept()
            return
        super().mouseReleaseEvent(event)


_MIN_FONT_SIZE = 8
_MAX_FONT_SIZE = 24


def _clamp_font_size(px: int, lo: int = _MIN_FONT_SIZE, hi: int = _MAX_FONT_SIZE) -> int:
    """Clamp a font size in points to the runtime zoom range (8–24 pt)."""
    return max(lo, min(hi, int(px)))


def _text_key(text: str) -> str:
    """Deterministic short id of a text (stable across app restarts)."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _strip_header(body: str) -> str:
    """Return the body without the extraction header line.

    The header is a single line ``── … ──`` (it contains ``│`` separators
    and ends with a blank line).  It is excluded from the edit-cache key so
    that UI-language, engine and timing changes don't orphan saved edits.
    """
    first, sep, rest = body.partition("\n")
    if sep and first.startswith("──") and "│" in first:
        return rest.lstrip("\n")
    return body


class TextPanel(QTextEdit):
    """Editable text window with a live font zoom (A− / A+).

    Each instance owns its source buffer, font size and plain/rendered mode,
    so the Original and Translated tabs behave as independent editors.  The
    content is shown rendered from Markdown (HTML + CSS); once the user edits
    the text the window falls back to plain text — a re-render would
    reinterpret typed ``*``/``#``/``_`` and destroy the HTML formatting.
    """

    _CSS_TEMPLATE = """
    <style>
      body { font-family: 'Segoe UI', sans-serif; font-size: {size}px;
             color: #1a1a1a; line-height: 1.7; margin: 0; }
      h1 { font-size: 1.5em; border-bottom: 2px solid #4a90d9; padding-bottom: 4px; }
      h2 { font-size: 1.3em; color: #2c5f8a; margin-top: 1em; }
      h3 { font-size: 1.15em; color: #3a7ab5; }
      strong { color: #1a3a5c; }
      em { color: #555; }
      code { background: #f0f0f0; padding: 2px 6px; border-radius: 3px;
             font-family: 'Consolas', monospace; font-size: 0.9em; }
      pre { background: #f5f5f5; padding: 12px; border-radius: 6px;
            border: 1px solid #ddd; overflow-x: auto; }
      table { border-collapse: collapse; width: 100%; margin: 10px 0; }
      th { background: #4a90d9; color: #fff; padding: 8px 12px;
           text-align: left; font-weight: 600; }
      td { border: 1px solid #ddd; padding: 6px 12px; }
      tr:nth-child(even) { background: #f8f9fa; }
      blockquote { border-left: 4px solid #4a90d9; margin: 10px 0;
                   padding: 6px 16px; background: #f0f4f8; color: #444; }
      ul, ol { padding-left: 24px; }
      li { margin: 3px 0; }
      hr { border: none; border-top: 1px solid #ddd; margin: 16px 0; }
      a { color: #4a90d9; }
    </style>
    """

    font_size_changed = pyqtSignal(int)
    edited = pyqtSignal(str, str)  # (base_text, edited_text) from the user
    modified = pyqtSignal(bool)    # True: buffer differs from its base

    def __init__(self, parent=None):
        super().__init__(parent)
        self._base_font_size = 12
        self._font_size = 12
        self._render_source: str = ""     # markdown/raw-html source for re-renders
        self._buffer: str = ""            # current plain content (rendered or edited)
        self._as_markdown: bool = True
        self._plain_mode: bool = False    # True once the user has edited
        self._raw_html: bool = False
        self._programmatic: bool = False  # guard against our own setHtml calls
        self._shown_buffer: str | None = None  # last programmatic content
        self._shown_md: bool = True
        self._edit_base: str | None = None  # base the current buffer derives from
        self._rendered_text: str = ""  # plain text of the last programmatic render
        self.setReadOnly(False)
        self.setFont(QFont("Segoe UI", self._font_size))
        self.setStyleSheet(
            "QTextEdit { background: #ffffff; color: #1a1a1a; padding: 12px; }"
        )
        self.textChanged.connect(self._on_text_changed)

    def css(self) -> str:
        """Current HTML stylesheet (font size interpolated).

        ``replace`` instead of ``format``: the CSS itself contains braces.
        """
        return self._CSS_TEMPLATE.replace("{size}", str(self._font_size))

    # ── rendering ──────────────────────────────────────────────────────

    def _render(self) -> None:
        """Rebuild the document (from the render source or plain buffer).

        La dimensione del font NON passa dal ``body { font-size }`` del CSS:
        il motore rich-text di Qt lo ignora nei tag <style>, quindi il testo
        markdown non si sarebbe mai ridimensionato. Si applica invece
        ``document().setDefaultFont`` dopo il build del documento (i tag
        em/percentuali della CSS scalano rispetto al default font).
        """
        self._programmatic = True
        try:
            if self._raw_html:
                self.setHtml(self.css() + self._render_source)
            elif self._as_markdown and not self._plain_mode:
                html_body = _md_lib.markdown(
                    self._render_source, extensions=_MD_EXTENSIONS
                )
                self.setHtml(self.css() + html_body)
            else:
                self.setPlainText(self._buffer)
        finally:
            self._programmatic = False
        self.document().setDefaultFont(QFont("Segoe UI", self._font_size))
        # il contenuto corrente è sempre il testo renderizzato (i round-trip
        # HTML possono normalizzare gli spazi: confrontare col sorgente grezzo
        # darebbe falsi positivi di modifica)
        self._rendered_text = self.toPlainText()
        self._buffer = self._rendered_text

    def is_modified(self) -> bool:
        """True if the user changed the content beyond the last render."""
        return self._rendered_text != self._buffer

    def _on_text_changed(self) -> None:
        """A user edit flips the window to plain text and updates the buffer."""
        if self._programmatic:
            return
        self._plain_mode = True
        self._raw_html = False
        self._buffer = self.toPlainText()
        changed = self.is_modified()
        self.modified.emit(changed)
        if changed and self._edit_base is not None:
            self.edited.emit(self._edit_base, self._buffer)

    def show_text(
        self,
        text: str,
        as_markdown: bool = True,
        edited: str | None = None,
    ) -> None:
        """Display text, optionally rendering as Markdown → HTML.

        Idempotent: if the same content is shown again (tab switch, repeat
        display), the window is left untouched so edits, cursor and scroll
        position survive.  ``edited`` (the user's stored version of ``text``)
        is shown instead, in plain mode, while ``text`` stays the edit base.
        """
        if text == self._shown_buffer and as_markdown == self._shown_md:
            return
        self._shown_buffer = text
        self._shown_md = as_markdown
        self._edit_base = text
        if edited is not None and edited != text:
            self._render_source = edited
            self._buffer = edited
            self._as_markdown = as_markdown
            self._plain_mode = True
            self._raw_html = False
        else:
            self._render_source = text
            self._buffer = text
            self._as_markdown = as_markdown
            self._plain_mode = False
            self._raw_html = False
        self._render()

    def show_html(self, html_body: str) -> None:
        """Display raw HTML with CSS styling."""
        self._render_source = html_body
        self._buffer = html_body
        self._as_markdown = False
        self._plain_mode = False
        self._raw_html = True
        self._shown_buffer = html_body
        self._shown_md = False
        self._edit_base = html_body
        self._render()

    # ── font zoom ──────────────────────────────────────────────────────

    def font_size(self) -> int:
        return self._font_size

    def set_font_size(self, px: int) -> None:
        """Set the base size (from Settings) and apply it."""
        self._base_font_size = px
        self._apply_font_size(px)

    def zoom_in(self) -> None:
        self._apply_font_size(self._font_size + 1)

    def zoom_out(self) -> None:
        self._apply_font_size(self._font_size - 1)

    def reset_zoom(self) -> None:
        self._apply_font_size(self._base_font_size)

    def _apply_font_size(self, px: int) -> None:
        """Apply a clamped size; plain text resizes live (cursor kept)."""
        px = _clamp_font_size(px)
        if px == self._font_size:
            return
        self._font_size = px
        if self._plain_mode and not self._raw_html:
            self.document().setDefaultFont(QFont("Segoe UI", px))
        else:
            self._render()  # markdown / raw HTML: re-render with new CSS size
        self.font_size_changed.emit(px)


class TextToolbar(QWidget):
    """Mini toolbar (A−  size  A+  ↺  💾) acting on a single TextPanel.

    Each text window gets its own toolbar, so the Original and Translated
    tabs zoom and export independently.  The size label and button state
    follow the panel's ``font_size_changed`` signal.
    """

    export_requested = pyqtSignal()

    _BTN_STYLE = (
        "QPushButton { background: #f0f0f0; color: #1a1a1a;"
        " border: 1px solid #ccc; border-radius: 4px; padding: 0;"
        " font-size: 13px; }"
        "QPushButton:hover { background: #e0e8f0; }"
        "QPushButton:pressed { background: #d0d8e0; }"
        "QPushButton:disabled { color: #aaa; }"
    )

    # Dimensioni uniformi per tutti i bottoni della mini toolbar (A−, A+,
    # ↺, 💾): il glifo emoji del salvataggio avrebbe altezza/larghezza
    # diverse dai caratteri di testo.
    _BTN_FIXED = (36, 26)

    def __init__(self, panel: TextPanel, parent=None):
        super().__init__(parent)
        self._panel = panel

        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(4)

        self.btn_decrease = QPushButton("A−")
        self.btn_decrease.setToolTip(T("editor.decrease"))
        self.btn_decrease.setStyleSheet(self._BTN_STYLE)
        self.btn_decrease.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_decrease.clicked.connect(panel.zoom_out)

        self.lbl_size = QLabel("")
        self.lbl_size.setStyleSheet(
            "color: #aaa; font-size: 12px; min-width: 44px;"
        )
        self.lbl_size.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.btn_increase = QPushButton("A+")
        self.btn_increase.setToolTip(T("editor.increase"))
        self.btn_increase.setStyleSheet(self._BTN_STYLE)
        self.btn_increase.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_increase.clicked.connect(panel.zoom_in)

        self.btn_reset = QPushButton("↺")
        self.btn_reset.setToolTip(T("editor.reset"))
        self.btn_reset.setStyleSheet(self._BTN_STYLE)
        self.btn_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_reset.clicked.connect(panel.reset_zoom)

        self.btn_export = QPushButton("💾")
        self.btn_export.setToolTip(T("editor.export"))
        self.btn_export.setStyleSheet(self._BTN_STYLE)
        self.btn_export.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_export.clicked.connect(self.export_requested.emit)

        # Punto di stato: modifiche non ancora salvate su disco.
        self.lbl_dirty = QLabel("")
        self.lbl_dirty.setToolTip(T("editor.unsaved"))
        self.lbl_dirty.setStyleSheet(
            "color: #e67e22; font-size: 14px; min-width: 14px;"
        )
        self.lbl_dirty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        for b in (
            self.btn_decrease,
            self.btn_increase,
            self.btn_reset,
            self.btn_export,
        ):
            b.setFixedSize(*self._BTN_FIXED)

        lay.addStretch()
        lay.addWidget(self.btn_decrease)
        lay.addWidget(self.lbl_size)
        lay.addWidget(self.btn_increase)
        lay.addWidget(self.btn_reset)
        lay.addSpacing(6)
        lay.addWidget(self.btn_export)
        lay.addSpacing(4)
        lay.addWidget(self.lbl_dirty)
        lay.addStretch()

        panel.font_size_changed.connect(self._sync)
        self._sync(panel.font_size())

    def set_dirty(self, dirty: bool) -> None:
        """Show/hide the "unsaved edits" dot."""
        self.lbl_dirty.setText("●" if dirty else "")

    def _sync(self, px: int) -> None:
        self.lbl_size.setText(f"{px} pt")
        self.btn_decrease.setEnabled(px > _MIN_FONT_SIZE)
        self.btn_increase.setEnabled(px < _MAX_FONT_SIZE)

    def retranslate(self) -> None:
        self.btn_decrease.setToolTip(T("editor.decrease"))
        self.btn_increase.setToolTip(T("editor.increase"))
        self.btn_reset.setToolTip(T("editor.reset"))
        self.btn_export.setToolTip(T("editor.export"))
        self.lbl_dirty.setToolTip(T("editor.unsaved"))



class TranslateThread(QThread):
    """Background thread for translation to keep UI responsive."""

    result_ready = pyqtSignal(int, str, str)  # generation_id, kind, translated_text
    error_ready = pyqtSignal(int, str)        # generation_id, error message

    def __init__(
        self,
        text: str,
        generation: int,
        kind: str = "origin",
        source: str = "en",
        target: str = "it",
        engine: str = "google",
    ):
        super().__init__()
        self._text = text
        self._generation = generation
        self._kind = kind
        self._source = source
        self._target = target
        self._engine = engine

    def run(self):
        try:
            translated = translate_text(
                self._text,
                source=self._source,
                target=self._target,
                engine=self._engine,
            )
        except Exception as exc:
            self.error_ready.emit(self._generation, str(exc))
            return
        self.result_ready.emit(self._generation, self._kind, translated)


class ExtractThread(QThread):
    """Background thread for text extraction + layout engine.

    Both Tesseract OCR (scanned PDFs) and the adaptive layout engine's
    ``profile_page`` are CPU/IO bound and slow (seconds per page), so the
    whole pipeline runs here and the GUI thread only displays the result.
    """

    result_ready = pyqtSignal(int, int, str, str, str, float)
    # generation, page_num, text, label, raw, elapsed

    def __init__(
        self,
        path: str,
        page_num: int,
        generation: int,
        ocr_language: str = "eng",
        exclude: tuple = (),
        include: tuple = (),
        raw: str | None = None,
    ):
        super().__init__()
        self._path = path
        self._page_num = page_num
        self._generation = generation
        self._ocr_language = ocr_language
        self._exclude = exclude
        self._include = include
        self._raw = raw

    def run(self):
        t0 = time.perf_counter()
        if self._raw is not None:
            raw = self._raw  # già estratta (cache): solo engine layout
        else:
            raw = _extract_pymupdf4llm(
                self._path, self._page_num, ocr_language=self._ocr_language
            )
        text, label = _apply_engine_standalone(
            self._path, self._page_num, raw,
            exclude=self._exclude, include=self._include,
        )
        elapsed = time.perf_counter() - t0
        self.result_ready.emit(
            self._generation, self._page_num, text, label, raw, elapsed
        )


class CloneTranslateThread(QThread):
    """Background thread: one page of clone translation via pdf2zh_next.

    ``pdf2zh_next`` runs as a subprocess and can take tens of seconds per
    page, so it never touches the GUI thread. Emits the resulting PDF path on
    success (``done``) or the engine status string on failure (``error``).
    """

    done = pyqtSignal(int, int, str, str)   # generation, page, engine, pdf path
    error = pyqtSignal(int, int, str, str)  # generation, page, engine, message

    def __init__(
        self, engine: clone_engine.CloneEngine, page: int, engine_name: str,
        generation: int,
    ):
        super().__init__()
        self._engine = engine
        self._page = page
        self._engine_name = engine_name
        self._generation = generation
        self._cancel_event = threading.Event()

    def cancel(self):
        """Chiede l'interruzione del ``pdf2zh_next`` in corso."""
        self._cancel_event.set()

    def run(self):
        try:
            path = self._engine.translate_page(
                self._page, self._engine_name, self._cancel_event
            )
        except Exception as exc:  # noqa: BLE001 — riportato in UI
            self.error.emit(
                self._generation, self._page, self._engine_name, str(exc)
            )
            return
        if path is None:
            status = self._engine.status(self._page, self._engine_name)
            message = status.split("error:", 1)[-1] if status.startswith("error:") else status
            self.error.emit(
                self._generation, self._page, self._engine_name, message
            )
            return
        self.done.emit(
            self._generation, self._page, self._engine_name, str(path)
        )


class CloneExportThread(QThread):
    """Background thread: pre-translate the missing pages of an export range.

    Iterates ``pages`` (0-based) **sequentially** through
    ``CloneEngine.translate_page`` (cache-aware, per-page serialised) and emits
    progress after each page. ``cancel`` interrompe subito il ``pdf2zh_next``
    in corso (process group), non solo la pagina successiva.
    """

    progress = pyqtSignal(int, int, int)        # done, total, page
    page_error = pyqtSignal(int, str)           # page, message
    batch_finished = pyqtSignal(int, int, int)  # done, failed, total

    def __init__(
        self,
        engine: clone_engine.CloneEngine,
        pages: list[int],
        engine_name: str,
    ):
        super().__init__()
        self._engine = engine
        self._pages = list(pages)
        self._engine_name = engine_name
        self._cancelled = False
        self._cancel_event = threading.Event()

    def cancel(self):
        self._cancelled = True
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        return self._cancelled

    def _translate_one(self, page: int) -> tuple[Path | None, str]:
        """Traduce una pagina, attendendo un eventuale worker concorrente.

        Se un altro thread sta già traducendo la stessa pagina (es. la
        traduzione della pagina corrente ancora in corso) ``translate_page``
        ritorna ``None`` con stato ``running``: si attende che la cache
        compaia invece di segnarla come errore.
        """
        for _ in range(600):  # ~5 minuti al massimo
            path = self._engine.translate_page(
                page, self._engine_name, self._cancel_event
            )
            if path is not None or self._cancelled:
                return path, ""
            status = self._engine.status(page, self._engine_name)
            if status == "running":
                time.sleep(0.5)
                continue
            if status.startswith("error:"):
                return None, status.split("error:", 1)[-1]
            return None, status
        return None, "timeout"

    def run(self):
        done = failed = 0
        total = len(self._pages)
        for index, page in enumerate(self._pages, start=1):
            if self._cancelled:
                break
            path, message = self._translate_one(page)
            if path is not None:
                done += 1
            elif not self._cancelled:
                failed += 1
                self.page_error.emit(page, message)
            self.progress.emit(index, total, page)
        self.batch_finished.emit(done, failed, total)


class TranslatablePanel(QWidget):
    """Wraps TextPanel with tabs: original, Italian translation, and images."""

    # Emitted when the user removes a captured image from the gallery.
    image_removed = pyqtSignal(str)  # file:// URI
    # Transient message for the main window status bar (message, duration ms).
    toast = pyqtSignal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Tab bar ────────────────────────────────────────────────────
        self._tab_bar = QWidget()
        self._tab_bar.setFixedHeight(36)
        self._tab_bar.setStyleSheet("""
            QWidget#tabBar {
                background: #3a3a3a;
                border-bottom: 1px solid #555;
            }
        """)
        self._tab_bar.setObjectName("tabBar")

        tab_layout = QHBoxLayout(self._tab_bar)
        tab_layout.setContentsMargins(4, 2, 4, 2)
        tab_layout.setSpacing(2)

        self._btn_original = QPushButton(T("tab.original"))
        # La tab di traduzione mostra bandiera + nome della lingua di
        # destinazione scelta (endonimo): es. "🇫🇷 Français", "🇩🇪 Deutsch".
        self._target_lang: str = get_target_lang()
        self._source_lang: str = get_source_lang()
        self._engine: str = get_translation_engine()
        self._btn_translated = QPushButton(flag_endonym(self._target_lang))
        self._btn_images = QPushButton(T("tab.images"))

        tab_style = """
            QPushButton {
                background: #444; color: #aaa;
                border: 1px solid #555; border-bottom: none;
                border-radius: 6px 6px 0 0;
                padding: 4px 16px; font-size: 13px;
            }
            QPushButton:hover { background: #555; color: #ddd; }
            QPushButton:checked {
                background: #fff; color: #1a1a1a;
                border-color: #ddd; font-weight: bold;
            }
        """
        for btn in (self._btn_original, self._btn_translated, self._btn_images):
            btn.setCheckable(True)
            btn.setFlat(True)
            btn.setStyleSheet(tab_style)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            tab_layout.addWidget(btn)

        # Spinner label for translation in progress
        self._lbl_spinner = QLabel("")
        self._lbl_spinner.setStyleSheet("color: #aaa; font-size: 13px; padding: 4px 8px;")
        tab_layout.addWidget(self._lbl_spinner)

        tab_layout.addStretch()

        # ── Engine radios (live engine switch), far right of the tab bar ──
        self._radio_google = QRadioButton(T("engine.short.google"))
        self._radio_microsoft = QRadioButton(T("engine.short.microsoft"))
        radio_style = """
            QRadioButton { color: #bbb; font-size: 12px; background: transparent;
                           spacing: 5px; }
            QRadioButton:hover { color: #fff; }
            QRadioButton::indicator {
                width: 13px; height: 13px; border: 1px solid #888;
                border-radius: 7px; background: #444;
            }
            QRadioButton::indicator:checked {
                background: #3a6bc5; border-color: #3a6bc5;
            }
        """
        for rb in (self._radio_google, self._radio_microsoft):
            rb.setStyleSheet(radio_style)
            rb.setCursor(Qt.CursorShape.PointingHandCursor)
            tab_layout.addWidget(rb)
        self._engine_group = QButtonGroup(self)
        self._engine_group.addButton(self._radio_google)
        self._engine_group.addButton(self._radio_microsoft)
        self._radio_google.setChecked(self._engine == "google")
        self._radio_microsoft.setChecked(self._engine == "microsoft")
        self._radio_google.toggled.connect(self._on_engine_radio)
        self._radio_microsoft.toggled.connect(self._on_engine_radio)

        # ── Text windows (Original / Translated) ───────────────────────
        # Two independent editable windows, each with its own mini toolbar
        # (A− / A+): stacked, the active tab's window is shown.
        def _make_window() -> tuple[QWidget, TextPanel, TextToolbar]:
            win = QWidget()
            v = QVBoxLayout(win)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(0)
            panel = TextPanel()
            panel.set_font_size(get_setting("font_size", 12))
            toolbar = TextToolbar(panel)
            v.addWidget(toolbar)
            v.addWidget(panel)
            return win, panel, toolbar

        (
            self._origin_window,
            self.origin_panel,
            self.origin_toolbar,
        ) = _make_window()
        (
            self._translated_window,
            self.translated_panel,
            self.translated_toolbar,
        ) = _make_window()

        # ── Translating overlay: a toast floating over the translated text
        # while a translation is being produced (the old tiny spinner label
        # was barely visible). Shown on schedule/start, hidden when the
        # translated text appears (or on error). ─────────────────────────
        self._translating_toast = QLabel(
            T("status.translating_engine", engine=T(f"engine.option.{self._engine}"))
        )
        self._translating_toast.setStyleSheet("""
            QLabel {
                background: rgba(20, 20, 20, 230); color: #fff;
                border: 1px solid #666; border-radius: 12px;
                padding: 16px 34px; font-size: 20px; font-weight: bold;
            }
        """)
        self._translating_toast.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._translating_toast.hide()
        # Overlay child of the translated window: the layout does not manage
        # it, we position it manually (centered) and keep it on top.
        self._translating_toast.setParent(self._translated_window)
        self._translated_window.installEventFilter(self)

        # ── Images panel (gallery of extracted figures) ────────────────
        self.images_panel = QScrollArea()
        self.images_panel.setWidgetResizable(True)
        self.images_panel.setStyleSheet(
            "QScrollArea { background: #f5f5f5; border: none; }"
        )

        self._stack = QStackedWidget()
        self._stack.addWidget(self._origin_window)      # index 0
        self._stack.addWidget(self._translated_window)  # index 1
        self._stack.addWidget(self.images_panel)        # index 2

        layout.addWidget(self._tab_bar)
        layout.addWidget(self._stack)

        # ── State ──────────────────────────────────────────────────────
        self._page_text: str = ""          # the single text shown (auto or manual)
        self._page_body: str = ""          # page text without the extraction header
        self._header_prefix: str = ""      # header template before the engine token
        self._header_tail: str = ""        # header template after the engine token
        self._translated_text: str = ""
        self._render_md: bool = True
        # Cache per (pagina, destinazione): cambiare la lingua di destinazione
        # fa cache-miss naturale, senza invalidare nulla.
        # Cache per (pagina, engine, destinazione): cambiare lingua o motore
        # fa cache-miss naturale, senza invalidare nulla.
        self._page_translation_cache: dict[tuple[int, str, str], str] = {}
        self._current_page: int = -1
        self._images: list[str] = []  # file:// URIs of manually captured regions
        self._thread: TranslateThread | None = None
        # Thread sostituiti mentre erano ancora attivi: tenuti vivi finché non
        # terminano (i loro risultati sono scartati dal guard di generazione).
        self._retired_threads: list[TranslateThread] = []
        self._generation: int = 0
        self._cache_file: Path | None = None  # on-disk translation cache file
        self._doc_fingerprint: str = ""  # invalidates the cache if the PDF changes
        # Modifiche utente per finestra (origin/translated), chiavate per
        # contenuto base (senza header): sopravvivono alla navigazione e alla
        # riapertura del documento.
        self._edit_cache: dict[str, dict[str, str]] = {
            "origin": {}, "translated": {},
        }
        self._edits_cache_file: Path | None = None  # on-disk edits cache file
        self._doc_stem: str = ""  # PDF name (without extension), for exports
        self._save_edits: bool = bool(get_setting("save_edits", True))
        # Indicatore "modifiche non salvate" per finestra (toolbar dot).
        self._dirty: dict[str, bool] = {"origin": False, "translated": False}
        self._edits_save_timer = QTimer(self)
        self._edits_save_timer.setSingleShot(True)
        self._edits_save_timer.setInterval(1500)
        self._edits_save_timer.timeout.connect(self._save_edits_cache)
        # Deferral ibrido: traduzione ri-lanciata solo dopo una pausa di 600 ms.
        self._pending_translation: bool = False
        self._translate_timer = QTimer(self)
        self._translate_timer.setSingleShot(True)
        self._translate_timer.setInterval(600)
        self._translate_timer.timeout.connect(self._flush_translation)

        # ── Connections ────────────────────────────────────────────────
        self._btn_original.clicked.connect(self._on_show_original)
        self._btn_translated.clicked.connect(self._on_show_translated)
        self._btn_images.clicked.connect(self._on_show_images)
        self.origin_panel.edited.connect(self._on_text_edited)
        self.translated_panel.edited.connect(self._on_text_edited)
        self.origin_panel.modified.connect(
            lambda m: self._on_window_modified("origin", m)
        )
        self.translated_panel.modified.connect(
            lambda m: self._on_window_modified("translated", m)
        )
        self.origin_toolbar.export_requested.connect(
            lambda: self._export_window(self.origin_panel)
        )
        self.translated_toolbar.export_requested.connect(
            lambda: self._export_window(self.translated_panel)
        )

        # Riapri sull'ultima tab usata (se abilitato) — dopo che tutto lo
        # stato e i pannelli esistono.
        self._restore_initial_tab()
        self._rebuild_images_panel()

    def _restore_initial_tab(self):
        """Open the panel on the remembered tab (if enabled), else Originale."""
        if get_setting("remember_tab", True):
            last = get_setting("last_tab", "original")
            if last == "translated":
                self._set_active_tab(self._btn_translated)
            elif last == "images":
                self._set_active_tab(self._btn_images)
            else:
                self._set_active_tab(self._btn_original)
        else:
            self._set_active_tab(self._btn_original)

    def retranslate(self):
        """Re-apply tab labels and toolbar tooltips after a language switch."""
        self._btn_original.setText(T("tab.original"))
        self._btn_translated.setText(flag_endonym(self._target_lang))
        self._btn_images.setText(T("tab.images"))
        self._radio_google.setText(T("engine.short.google"))
        self._radio_microsoft.setText(T("engine.short.microsoft"))
        self._translating_toast.setText(
            T("status.translating_engine", engine=T(f"engine.option.{self._engine}"))
        )
        self.origin_toolbar.retranslate()
        self.translated_toolbar.retranslate()

    def set_font_size(self, px: int) -> None:
        """Apply the Settings font size as the base for both windows."""
        self.origin_panel.set_font_size(px)
        self.translated_panel.set_font_size(px)

    def set_translation_languages(
        self, src: str, dst: str, engine: str | None = None
    ) -> None:
        """Apply new source/target languages (and optionally engine)."""
        src = src or "auto"
        dst = dst or "it"
        engine = engine or self._engine
        changed = (
            src != self._source_lang
            or dst != self._target_lang
            or engine != self._engine
        )
        self._source_lang = src
        self._target_lang = dst
        self._engine = engine
        self._btn_translated.setText(flag_endonym(self._target_lang))
        # Keep the tab-bar engine radios in sync (Settings change → radios).
        for rb, code in (
            (self._radio_google, "google"),
            (self._radio_microsoft, "microsoft"),
        ):
            rb.blockSignals(True)
            rb.setChecked(engine == code)
            rb.blockSignals(False)
        if changed and self._btn_translated.isChecked():
            self._schedule_translation()

    def _on_engine_radio(self, checked: bool):
        """Live engine switch from the tab-bar radios.

        Persists the choice, shows a 2-second toast, and re-translates the
        current page when the translated tab is active (cache is keyed by
        engine, so switching engines naturally misses and re-translates).
        """
        if not checked:
            return  # toggled fires for both radios; act on the checked one
        code = "google" if self.sender() is self._radio_google else "microsoft"
        if code == self._engine:
            return
        self._engine = code
        set_translation_engine(code)
        save_config()
        # The origin window also shows the engine in its header: recompose it
        # so both windows agree after a live switch (same root cause as the
        # stale translated header).
        if self._header_prefix and not self._btn_translated.isChecked():
            self._page_text = self._recomposed_header() + self._page_body
            self._show_panel(self.origin_panel, self._page_text, self._render_md)
        self.toast.emit(
            T("toast.engine_changed", engine=T(f"engine.option.{code}")), 2000
        )
        if self._btn_translated.isChecked():
            self._maybe_show_translation()

    # ── translation header / translating toast ────────────────────────

    def _recomposed_header(self) -> str:
        """Header for the translated window, recomposed with the current engine.

        The header template (ms/chars/label/OCR + engine) is captured at
        ``show_text`` time; only the engine token is refreshed, so a live
        engine switch (radio buttons / Settings) is reflected above the
        translated text instead of keeping the stale name.
        """
        if not self._header_prefix:
            return ""
        return (
            f"{self._header_prefix} {T(f'engine.option.{self._engine}')}"
            f" ──{self._header_tail}"
        )

    def _show_translating_toast(self):
        """Center and show the 'translating…' toast over the translated text."""
        toast = self._translating_toast
        toast.setText(
            T("status.translating_engine", engine=T(f"engine.option.{self._engine}"))
        )
        toast.adjustSize()
        r = self._translated_window.rect()
        toast.move(
            r.center().x() - toast.width() // 2,
            r.center().y() - toast.height() // 2,
        )
        toast.show()
        toast.raise_()

    def _hide_translating_toast(self):
        self._translating_toast.hide()

    def _center_translating_toast(self):
        """Keep the toast centered after the translated window is resized."""
        if self._translating_toast.isVisible():
            self._show_translating_toast()

    def eventFilter(self, obj, event):
        if obj is self._translated_window and event.type() == QEvent.Type.Resize:
            self._center_translating_toast()
        return super().eventFilter(obj, event)

    def show_text(
        self,
        text: str,
        as_markdown: bool = True,
        page_num: int = -1,
        images: list[str] | None = None,
    ):
        """Display the page text (already the auto-or-manual result).

        ``page_num`` keys the per-page translation cache; ``images`` are the
        file:// URIs of the regions captured for the page.
        """
        self._render_md = as_markdown
        self._page_text = text
        self._current_page = page_num
        self._images = list(images or [])
        # The header line (``── … Trad: {engine} ──``) must never be sent to
        # the translator: it is baked at display time and would carry a stale
        # engine name. Translate only the body and recompose the header with
        # the *current* engine when the translated result is shown.
        body = _strip_header(text)
        self._page_body = body
        header = text[: len(text) - len(body)]
        if header:
            # The header template ends with the engine name right before the
            # closing "──": drop that baked name from the prefix, so the
            # recomposed header can swap in the *current* engine later.
            prefix, tail = header.rsplit("──", 1)
            old_engine = T(f"engine.option.{self._engine}")
            stripped = prefix.rstrip()  # the template ends "…{engine} ──"
            if stripped.endswith(old_engine):
                prefix = stripped[: -len(old_engine)].rstrip()
            self._header_prefix = prefix
            self._header_tail = tail
        else:
            self._header_prefix = ""
            self._header_tail = ""
        self._rebuild_images_panel()

        if self._btn_images.isChecked():
            return  # images tab active — gallery already rebuilt above

        if self._btn_translated.isChecked():
            self._maybe_show_translation()
            return

        # Original tab is active (default)
        self._set_active_tab(self._btn_original)
        self._show_panel(self.origin_panel, self._page_text, as_markdown)
        self._lbl_spinner.setText("")

    def _set_active_tab(self, active):
        for btn in (self._btn_original, self._btn_translated, self._btn_images):
            btn.setChecked(btn is active)
        if active is self._btn_images:
            self._stack.setCurrentWidget(self.images_panel)
            self._rebuild_images_panel()
        elif active is self._btn_translated:
            self._stack.setCurrentWidget(self._translated_window)
        else:
            self._stack.setCurrentWidget(self._origin_window)
        if get_setting("remember_tab", True):
            if active is self._btn_translated:
                tab_id = "translated"
            elif active is self._btn_images:
                tab_id = "images"
            else:
                tab_id = "original"
            if get_setting("last_tab", "") != tab_id:
                set_setting("last_tab", tab_id)
                save_config()

    def _show_panel(
        self, panel: TextPanel, text: str, as_markdown: bool
    ) -> None:
        """Show text in a window, re-applying the user's saved edits if any.

        Edits are keyed by the content without the extraction header, so they
        survive navigation, reopen and header re-renders (language/engine
        switches); the current header is recomposed on display.
        """
        window = "origin" if panel is self.origin_panel else "translated"
        body = _strip_header(text)
        saved = None
        if self._save_edits:
            saved = self._edit_cache.get(window, {}).get(_text_key(body))
        edited = None
        if saved is not None and saved != body:
            # ri-attacca l'header corrente al testo modificato salvato
            edited = text[: len(text) - len(body)] + saved
        panel.show_text(text, as_markdown=as_markdown, edited=edited)
        # indicatore: la finestra mostra una modifica ancora in attesa di
        # essere scritta su disco (timer di salvataggio attivo)
        pending = self._edits_save_timer.isActive()
        self._set_dirty(window, pending and edited is not None and edited != text)

    def _on_text_edited(self, base: str, edited: str) -> None:
        """Store a user edit (header-stripped) and schedule a disk save."""
        if not self._save_edits:
            return
        window = "origin" if self.sender() is self.origin_panel else "translated"
        body = _strip_header(base)
        if not body:
            return
        self._edit_cache.setdefault(window, {})[_text_key(body)] = _strip_header(edited)
        self._edits_save_timer.start()

    def _on_window_modified(self, window: str, modified: bool) -> None:
        """Track the unsaved-edits dot from the user's typing."""
        if not self._save_edits:
            self._set_dirty(window, False)
            return
        self._set_dirty(window, modified)

    def _set_dirty(self, window: str, dirty: bool) -> None:
        """Update the toolbar dot, avoiding redundant repaints."""
        if self._dirty.get(window) == dirty:
            return
        self._dirty[window] = dirty
        toolbar = self.origin_toolbar if window == "origin" else self.translated_toolbar
        toolbar.set_dirty(dirty)

    def _export_window(self, panel: TextPanel) -> None:
        """Export the window content (.md or .txt), edits included.

        The extraction header line is excluded: it is display metadata, not
        document content.
        """
        content = _strip_header(panel.toPlainText())
        window = "original" if panel is self.origin_panel else "translated"
        page = max(self._current_page + 1, 1)
        stem = self._doc_stem or "documento"
        dest, _ = QFileDialog.getSaveFileName(
            self,
            T("editor.export_dialog"),
            f"{stem}_pag{page}_{window}",
            T("editor.export_filter"),
        )
        if not dest:
            return
        if not dest.lower().endswith((".md", ".txt")):
            dest += ".md"
        try:
            Path(dest).write_text(content, encoding="utf-8")
            self._lbl_spinner.setText(T("status.exported"))
        except Exception:
            self._lbl_spinner.setText(T("editor.export_error"))

    def show_original(self):
        """Switch to the "Originale" text tab (keeps current content)."""
        self._set_active_tab(self._btn_original)
        self._lbl_spinner.setText("")

    def _on_show_original(self):
        self.show_original()
        self._show_panel(self.origin_panel, self._page_text, self._render_md)

    def _on_show_translated(self):
        self._set_active_tab(self._btn_translated)
        self._maybe_show_translation()

    def _on_show_images(self):
        self._set_active_tab(self._btn_images)
        self._lbl_spinner.setText("")

    def _maybe_show_translation(self):
        """Show the cached translation for the page, or schedule a new one."""
        key = (self._current_page, self._engine, self._target_lang)
        cached = self._page_translation_cache.get(key)
        if self._current_page >= 0 and cached is not None:
            self._translated_text = cached
            self._show_panel(self.translated_panel, cached, self._render_md)
            self._lbl_spinner.setText("")
            self._hide_translating_toast()
        else:
            self._schedule_translation()

    def _schedule_translation(self):
        """Debounce: re-translate only after the user pauses drawing."""
        self._lbl_spinner.setText("")
        self._show_translating_toast()
        self._pending_translation = True
        self._translate_timer.start()

    def _flush_translation(self):
        self._pending_translation = False
        if not self._btn_translated.isChecked():
            return
        key = (self._current_page, self._engine, self._target_lang)
        if self._current_page >= 0 and key in self._page_translation_cache:
            # Debounce obsoleto: la pagina corrente è già tradotta per questa
            # destinazione (es. sbirciata avanti e ritorno) → mostra la cache
            # invece di ritradurre inutilmente.
            self._show_panel(
                self.translated_panel,
                self._page_translation_cache[key],
                self._render_md,
            )
            self._lbl_spinner.setText("")
            self._hide_translating_toast()
            return
        self._start_translation()

    def show_images(self, images: list[str], activate: bool = True):
        """Set the current page's captured regions and (by default) activate
        the gallery tab. Pass ``activate=False`` to update the gallery without
        switching away from the text window (used by zone exclusion)."""
        self._images = list(images or [])
        self._rebuild_images_panel()
        if activate:
            self._set_active_tab(self._btn_images)

    # ── Images gallery ─────────────────────────────────────────────────

    def _rebuild_images_panel(self):
        """Rebuild the gallery from the current page's figure URIs."""
        container = QWidget()
        container.setObjectName("galleryContainer")
        # fondo esplicito: la galleria resta chiara su qualunque tema di
        # sistema (il viewport dello QScrollArea potrebbe ereditare il palette)
        container.setStyleSheet("#galleryContainer { background: #f5f5f5; }")
        lay = QVBoxLayout(container)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        if not self._images:
            hint = QLabel(T("gallery.empty"))
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #888; font-size: 14px; padding: 24px;")
            lay.addWidget(hint)
        else:
            for uri in self._images:
                lay.addWidget(self._make_image_card(uri))
        lay.addStretch()

        old = self.images_panel.takeWidget()
        if old is not None:
            old.deleteLater()
        self.images_panel.setWidget(container)

    def _make_image_card(self, uri: str) -> QWidget:
        path = str(QUrl(uri).toLocalFile())
        card = QWidget()
        card.setObjectName("imgCard")
        card.setStyleSheet(
            "QWidget#imgCard { background: #fff; border: 1px solid #ddd;"
            " border-radius: 6px; }"
        )
        v = QVBoxLayout(card)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        pix = QPixmap(path)
        thumb = QLabel()
        thumb.setPixmap(
            pix.scaledToWidth(340, Qt.TransformationMode.SmoothTransformation)
        )
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setCursor(Qt.CursorShape.PointingHandCursor)
        thumb.setToolTip(T("gallery.zoom_tip"))
        thumb.mousePressEvent = lambda _e, u=uri: self._show_image_full(u)
        v.addWidget(thumb)

        info = QLabel(f"{Path(path).name}  ·  {pix.width()}×{pix.height()} px")
        info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        info.setStyleSheet("border: none; color: #555; font-size: 12px;")
        v.addWidget(info)

        row = QHBoxLayout()
        btn_style = (
            "QPushButton { background: #4a90d9; color: #fff; border: none;"
            " border-radius: 4px; padding: 6px 12px; font-size: 12px; }"
            "QPushButton:hover { background: #3a7ab5; }"
        )
        btn_save = QPushButton(T("gallery.save"))
        btn_save.clicked.connect(lambda _=False, u=uri: self._save_image(u))
        btn_copy = QPushButton(T("gallery.copy"))
        btn_copy.clicked.connect(lambda _=False, u=uri: self._copy_image(u))
        for b in (btn_save, btn_copy):
            b.setStyleSheet(btn_style)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(b)
        btn_remove = QPushButton(T("gallery.remove"))
        btn_remove.clicked.connect(lambda _=False, u=uri: self._remove_image(u))
        btn_remove.setStyleSheet(
            "QPushButton { background: #d9534f; color: #fff; border: none;"
            " border-radius: 4px; padding: 6px 12px; font-size: 12px; }"
            "QPushButton:hover { background: #c9302c; }"
        )
        btn_remove.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(btn_remove)
        v.addLayout(row)
        return card

    def _show_image_full(self, uri: str):
        path = str(QUrl(uri).toLocalFile())
        dlg = QDialog(self)
        dlg.setWindowTitle(Path(path).name)
        dlg.resize(900, 720)
        # finestra top-level: tema scuro esplicito, indipendente dal sistema
        dlg.setStyleSheet(
            "QDialog { background: #2b2b2b; color: #ddd; }"
            " QScrollArea { background: #2b2b2b; border: none; }"
        )
        lay = QVBoxLayout(dlg)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        lbl = QLabel()
        lbl.setPixmap(QPixmap(path))
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll.setWidget(lbl)
        lay.addWidget(scroll)
        dlg.exec()

    def _copy_image(self, uri: str):
        img = QImage(QUrl(uri).toLocalFile())
        if not img.isNull():
            QApplication.clipboard().setImage(img)
            self._lbl_spinner.setText(T("status.copied"))

    def _save_image(self, uri: str):
        src = str(QUrl(uri).toLocalFile())
        dest, _ = QFileDialog.getSaveFileName(
            self,
            T("gallery.save_dialog"),
            Path(src).name,
            T("gallery.save_filter"),
        )
        if dest:
            shutil.copyfile(src, dest)
            self._lbl_spinner.setText(T("status.saved"))

    def _remove_image(self, uri: str):
        """Ask the main window to drop a captured image from the gallery."""
        self.image_removed.emit(uri)

    def _start_translation(self):
        """Fire a background translation for the current page text."""
        # Translate only the body: the extraction header is recomposed at
        # display time with the current engine (see ``_recomposed_header``),
        # so a live engine switch updates the name above the translated text.
        text = self._page_body
        self._lbl_spinner.setText("")
        self._show_translating_toast()
        self._generation += 1

        old = self._thread
        if old is not None:
            for sig in (old.result_ready, old.error_ready):
                try:
                    sig.disconnect()
                except TypeError:
                    pass  # already disconnected
            if old.isRunning():
                # Non distruggere un thread ancora attivo: lo si ritira e si
                # pulisce quando termina da solo.
                self._retired_threads.append(old)
                old.finished.connect(self._forget_retired_thread)

        thread = TranslateThread(
            text, generation=self._generation, kind="page",
            source=self._source_lang, target=self._target_lang,
            engine=self._engine,
        )
        thread.result_ready.connect(self._on_translation_done)
        thread.error_ready.connect(self._on_translation_error)
        self._thread = thread
        thread.start()

    def _forget_retired_thread(self):
        """Drop a retired thread from the keep-alive list once it finishes."""
        thread = self.sender()
        if thread in self._retired_threads:
            self._retired_threads.remove(thread)

    def _on_translation_done(self, generation: int, kind: str, translated: str):
        """Slot: background translation finished."""
        # Ignore stale results from superseded requests
        if generation != self._generation:
            return

        # Attach the header recomposed with the *current* engine, so the
        # engine name above the translated text always matches the active one
        # (e.g. after a live switch from Google to Microsoft).
        displayed = self._recomposed_header() + translated
        self._translated_text = displayed

        # Cache per (pagina, engine, destinazione)
        if self._current_page >= 0:
            self._page_translation_cache[
                (self._current_page, self._engine, self._target_lang)
            ] = displayed
            self._save_disk_cache()

        # Show if the translated tab is active
        if self._btn_translated.isChecked():
            self._show_panel(self.translated_panel, displayed, self._render_md)

        self._lbl_spinner.setText("✅")
        self._hide_translating_toast()

    def _on_translation_error(self, generation: int, message: str):
        """Slot: engine failed on every chunk — surface it instead of the
        silent no-op that used to leave untranslated text on screen."""
        if generation != self._generation:
            return
        engine = T(f"engine.option.{self._engine}")
        self._lbl_spinner.setText(
            T("status.translation_error", engine=engine, reason=message)
        )
        self._hide_translating_toast()

    def show_html(self, html_body: str):
        """Forward to the Original text window."""
        self.origin_panel.show_html(html_body)

    # ── persistent translation cache ───────────────────────────────────

    def set_document(self, path: Path | None):
        """Point the caches at this PDF and load saved translations + edits."""
        self._save_edits_cache()  # flush pending edits of the previous doc
        self._page_translation_cache.clear()
        self._cache_file = None
        self._edit_cache = {"origin": {}, "translated": {}}
        self._edits_cache_file = None
        self._doc_fingerprint = ""
        if path is None:
            self._doc_stem = ""
            return
        self._doc_stem = path.stem
        try:
            st = path.stat()
            self._doc_fingerprint = f"{st.st_size}-{st.st_mtime_ns}"
        except Exception:
            return
        cache_dir = _app_data_base() / "translation"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return
        self._cache_file = cache_dir / f"{path.stem}.json"
        self._load_disk_cache()
        edits_dir = _app_data_base() / "edits"
        try:
            edits_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return
        self._edits_cache_file = edits_dir / f"{path.stem}.json"
        if self._save_edits:
            self._load_edits_cache()

    def _load_disk_cache(self):
        """Load saved translations when they match the current PDF fingerprint."""
        if self._cache_file is None or not self._cache_file.exists():
            return
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
        except Exception:
            return
        if data.get("fingerprint") != self._doc_fingerprint:
            return
        pages = data.get("pages") or {}
        for key, value in pages.items():
            try:
                page = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, str):
                # vecchio formato {page: testo}: la destinazione era sempre it
                self._page_translation_cache[(page, "google", "it")] = value
            elif isinstance(value, dict):
                if "origin" in value or "cleaned" in value:
                    # formato pre-v2 {origin, cleaned} → migra a destinazione it
                    migrated = value.get("cleaned") or value.get("origin")
                    if isinstance(migrated, str):
                        self._page_translation_cache[(page, "google", "it")] = migrated
                else:
                    # formato v2 {page: {target: testo}} → engine google;
                    # formato v3 {page: {"engine:target": testo}}
                    for tgt, txt in value.items():
                        if not isinstance(txt, str):
                            continue
                        if ":" in tgt:
                            engine, lang = tgt.split(":", 1)
                            if (
                                engine in TRANSLATION_ENGINES
                                and lang in TRANSLATION_LANGUAGES
                                and lang != "auto"
                            ):
                                self._page_translation_cache[(page, engine, lang)] = txt
                        elif tgt in TRANSLATION_LANGUAGES and tgt != "auto":
                            self._page_translation_cache[(page, "google", tgt)] = txt

    def _save_disk_cache(self):
        """Persist the in-memory translation cache to disk."""
        if self._cache_file is None:
            return
        pages: dict[str, dict[str, str]] = {}
        for (page, engine, tgt), txt in self._page_translation_cache.items():
            pages.setdefault(str(page), {})[f"{engine}:{tgt}"] = txt
        payload = {
            "fingerprint": self._doc_fingerprint,
            "pages": pages,
        }
        try:
            self._cache_file.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    # ── persistent user-edits cache ─────────────────────────────────────

    def _load_edits_cache(self):
        """Load saved user edits when they match the current PDF fingerprint."""
        if self._edits_cache_file is None or not self._edits_cache_file.exists():
            return
        try:
            data = json.loads(self._edits_cache_file.read_text(encoding="utf-8"))
        except Exception:
            return
        if data.get("fingerprint") != self._doc_fingerprint:
            return
        for window in ("origin", "translated"):
            entries = data.get(window)
            if isinstance(entries, dict):
                for key, value in entries.items():
                    if isinstance(value, str):
                        self._edit_cache[window][key] = value

    def _save_edits_cache(self, force: bool = False):
        """Persist the user edits to disk.

        No-op while saving is disabled (``force`` overrides it, used by
        ``clear_saved_edits`` which must wipe the file on purpose).
        """
        if not self._save_edits and not force:
            return
        if self._edits_cache_file is None:
            return
        payload = {"fingerprint": self._doc_fingerprint, **self._edit_cache}
        try:
            self._edits_cache_file.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            return
        # tutto ciò che era in attesa è ora su disco: la baseline di
        # confronto delle finestre diventa il contenuto appena salvato
        self.origin_panel._rendered_text = self.origin_panel.toPlainText()
        self.translated_panel._rendered_text = self.translated_panel.toPlainText()
        self._set_dirty("origin", False)
        self._set_dirty("translated", False)

    def set_save_edits(self, enabled: bool) -> None:
        """Enable/disable the persistence (and application) of text edits.

        Disabling stops saving and drops the in-memory cache (the on-disk
        file is preserved); re-enabling reloads the saved edits.
        """
        enabled = bool(enabled)
        if enabled == self._save_edits:
            return
        self._save_edits = enabled
        self._edits_save_timer.stop()
        if enabled:
            self._load_edits_cache()
            # il contenuto mostrato non cambia: forza il re-render delle
            # finestre così le modifiche salvate tornano visibili subito
            self._force_reshow()
        else:
            # flush le modifiche pendenti fatte mentre era attivo, poi
            # abbandona la cache in memoria (il file su disco resta)
            self._save_edits_cache(force=True)
            self._edit_cache = {"origin": {}, "translated": {}}
            self._set_dirty("origin", False)
            self._set_dirty("translated", False)

    def _force_reshow(self) -> None:
        """Re-render the current page, bypassing the idempotent show.

        Used after the edit cache changes (re-enable saving / clear) so the
        current view reflects it immediately.
        """
        self.origin_panel._shown_buffer = None
        self.translated_panel._shown_buffer = None
        if self._current_page >= 0:
            if self._btn_translated.isChecked():
                self._maybe_show_translation()
            else:
                self._show_panel(
                    self.origin_panel, self._page_text, self._render_md
                )

    def clear_saved_edits(self) -> None:
        """Wipe the current document's saved edits (memory + disk)."""
        self._edits_save_timer.stop()
        self._edit_cache = {"origin": {}, "translated": {}}
        self._save_edits_cache(force=True)
        self._set_dirty("origin", False)
        self._set_dirty("translated", False)
        self._force_reshow()

    def invalidate_cache(self):
        """Clear per-page translation cache."""
        self._page_translation_cache.clear()

    def invalidate_page(self, page_num: int):
        """Drop the cached translations for one page (its content changed)."""
        for k in [k for k in self._page_translation_cache if k[0] == page_num]:
            del self._page_translation_cache[k]
        self._save_disk_cache()

    def shutdown(self):
        """Wait for any in-flight translation before the app closes."""
        self._translate_timer.stop()
        self._edits_save_timer.stop()
        self._save_edits_cache()
        for t in self._retired_threads:
            if t.isRunning():
                t.wait(3000)
        self._retired_threads.clear()
        if self._thread is not None and self._thread.isRunning():
            self._thread.wait(5000)
        self._save_disk_cache()


class TranslatedPagePanel(QWidget):
    """Right panel — clone of the current page, translated, layout preserved.

    Replaces lite's text panel: the ``PdfPageView`` here is read-only (no
    rubber-band modes) and shows the ``*.mono.pdf`` render produced by
    pdf2zh_next. The header carries the three engine radios (google/bing/llm)
    and the target-language badge; a centered spinner overlays the view while
    the page is being translated.
    """

    engine_changed = pyqtSignal(str)
    export_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Header bar: target language + engine radios + status ───────
        self._bar = QWidget()
        self._bar.setObjectName("cloneBar")
        self._bar.setFixedHeight(36)
        self._bar.setStyleSheet(
            "QWidget#cloneBar { background: #3a3a3a;"
            " border-bottom: 1px solid #555; }"
        )
        bar = QHBoxLayout(self._bar)
        # Margine destro riservato al pulsante flottante di collasso, così non
        # copre la label di stato.
        bar.setContentsMargins(10, 2, 36, 2)
        bar.setSpacing(10)

        self._lbl_target = QLabel("")
        self._lbl_target.setStyleSheet(
            "color: #ddd; font-size: 13px; font-weight: bold;"
        )
        bar.addWidget(self._lbl_target)

        bar.addStretch()

        radio_style = """
            QRadioButton { color: #bbb; font-size: 12px; background: transparent;
                           spacing: 5px; }
            QRadioButton:hover { color: #fff; }
            QRadioButton::indicator {
                width: 13px; height: 13px; border: 1px solid #888;
                border-radius: 7px; background: #444;
            }
            QRadioButton::indicator:checked {
                background: #3a6bc5; border-color: #3a6bc5;
            }
        """
        self._radios: dict[str, QRadioButton] = {}
        self._engine_group = QButtonGroup(self)
        for code in CLONE_ENGINES:
            rb = QRadioButton(T(f"engine.short.{code}"))
            rb.setStyleSheet(radio_style)
            rb.setCursor(Qt.CursorShape.PointingHandCursor)
            rb.toggled.connect(
                lambda checked, c=code: self._on_radio(c, checked)
            )
            self._engine_group.addButton(rb)
            self._radios[code] = rb
            bar.addWidget(rb)

        bar.addStretch()

        self._lbl_status = QLabel("")
        self._lbl_status.setStyleSheet("color: #aaa; font-size: 12px;")
        bar.addWidget(self._lbl_status)

        # Esporta la pagina tradotta: piccolo pulsante nella barra motori.
        self.btn_export = QPushButton("💾")
        self.btn_export.setToolTip(T("clone.export.tip"))
        self.btn_export.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_export.setFlat(True)
        self.btn_export.setStyleSheet(
            "QPushButton { background: transparent; color: #ddd; border: none;"
            " font-size: 14px; padding: 0 4px; }"
            "QPushButton:hover { color: #fff; }"
        )
        self.btn_export.clicked.connect(self.export_requested.emit)
        bar.addWidget(self.btn_export)

        # ── Page view (read-only) wrapped in a scroll area ────────────
        self.view = PdfPageView()
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(self.view)

        layout.addWidget(self._bar)
        layout.addWidget(self.scroll_area)

        # ── Spinner overlay (child, positioned manually) ──────────────
        self._spinner = QLabel("", self)
        self._spinner.setStyleSheet("""
            QLabel {
                background: rgba(20, 20, 20, 230); color: #fff;
                border: 1px solid #666; border-radius: 12px;
                padding: 16px 34px; font-size: 18px; font-weight: bold;
            }
        """)
        self._spinner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._spinner.hide()

        # ── Pulsante flottante di collasso della barra motori ─────────
        # Figlio del pannello ma FUORI dal layout: non consuma spazio, quindi
        # al collasso l'intera barra viene nascosta e i suoi pixel tornano al
        # viewport. Resta sempre visibile per riespandere.
        self._is_collapsed: bool = False
        self._collapse_btn = QPushButton("▾", self)
        self._collapse_btn.setFixedSize(26, 22)
        self._collapse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._collapse_btn.setFlat(True)
        self._collapse_btn.setStyleSheet(
            "QPushButton { background: rgba(58,58,58,220); color: #ddd;"
            " border: 1px solid #666; border-radius: 4px; font-size: 12px;"
            " padding: 0; }"
            "QPushButton:hover { background: #555; color: #fff; }"
        )
        self._collapse_btn.clicked.connect(self._toggle_collapsed)
        self._collapse_btn.raise_()

        self._view_zoom: float = 1.0

    # ── collapse / expand della barra motori ──────────────────────────

    def _toggle_collapsed(self):
        self.set_collapsed(not self._is_collapsed)

    def is_collapsed(self) -> bool:
        return self._is_collapsed

    def set_collapsed(self, collapsed: bool, persist: bool = True):
        """Collassa/espande la barra motori restituendo lo spazio al viewport.

        Con la barra nascosta il ``QVBoxLayout`` non le riserva più altezza:
        lo ``scroll_area`` cresce e ``PdfPageView.resizeEvent`` riadatta la
        pagina. Il pulsante flottante resta visibile per riespandere.
        """
        self._is_collapsed = bool(collapsed)
        self._bar.setVisible(not self._is_collapsed)
        self._collapse_btn.setText("▸" if self._is_collapsed else "▾")
        self._collapse_btn.setToolTip(
            T("clone.bar.expand") if self._is_collapsed else T("clone.bar.collapse")
        )
        self._reposition_collapse_btn()
        self._collapse_btn.raise_()
        # Re-fit immediato (sicuro anche senza pixmap).
        self.view._fit_to_view()
        if persist:
            set_setting("clone_bar_collapsed", self._is_collapsed)
            save_config()

    def _reposition_collapse_btn(self):
        b = self._collapse_btn
        x = max(0, self.width() - b.width() - 6)
        b.move(x, 6)

    # ── engine radios ─────────────────────────────────────────────────

    def _on_radio(self, code: str, checked: bool):
        if checked:
            self.engine_changed.emit(code)

    def set_engine(self, engine: str, emit: bool = False):
        rb = self._radios.get(engine)
        if rb is None:
            return
        if rb.isChecked():
            return
        if emit:
            rb.setChecked(True)
        else:
            rb.blockSignals(True)
            rb.setChecked(True)
            rb.blockSignals(False)

    # ── content ───────────────────────────────────────────────────────

    def set_target_language(self, target: str):
        self._lbl_target.setText(flag_endonym(target))

    def show_page(self, pixmap: QPixmap | None):
        self.hide_spinner()
        self.view.show_page(pixmap)
        self._lbl_status.setText("")

    def show_message(self, text: str):
        self.view.show_message(text)

    def set_view_zoom(self, zoom: float):
        self._view_zoom = zoom
        self.view.set_view_zoom(zoom)

    def show_spinner(self, text: str):
        self._spinner.setText(text)
        self._position_spinner()
        self._spinner.show()
        self._spinner.raise_()
        self._collapse_btn.raise_()

    def hide_spinner(self):
        self._spinner.hide()

    def set_status(self, text: str):
        self._lbl_status.setText(text)

    def _position_spinner(self):
        self._spinner.adjustSize()
        x = (self.width() - self._spinner.width()) // 2
        y = (self.height() - self._spinner.height()) // 2
        self._spinner.move(max(0, x), max(0, y))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_collapse_btn()
        if self._spinner.isVisible():
            self._position_spinner()

    def retranslate(self):
        self.set_target_language(get_target_lang())
        for code, rb in self._radios.items():
            rb.setText(T(f"engine.short.{code}"))
        self._collapse_btn.setToolTip(
            T("clone.bar.expand") if self._is_collapsed else T("clone.bar.collapse")
        )
        self.btn_export.setToolTip(T("clone.export.tip"))
        self.view.retranslate()


class TocPanel(QWidget):
    """Dockable multi-level table of contents navigator."""

    page_selected = pyqtSignal(int)  # 0-based page index

    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(16)
        self.tree.setStyleSheet(
            "QTreeWidget { background: #2b2b2b; color: #ddd; border: none;"
            " font-size: 13px; }"
            "QTreeWidget::item { padding: 2px 0; }"
            "QTreeWidget::item:selected { background: #3a6bc5; color: #fff; }"
        )
        layout.addWidget(self.tree)

        self._items: list[QTreeWidgetItem] = []  # flat, in document order
        self._syncing: bool = False

        self.tree.itemClicked.connect(self._on_item_clicked)

    def build_toc(self, doc) -> None:
        """Rebuild the tree from a pymupdf Document's bookmarks."""
        self.tree.clear()
        self._items = []

        stack: list[tuple[int, QTreeWidgetItem]] = []  # (level, item)
        try:
            bookmarks = doc.get_toc(simple=True)
        except Exception:
            bookmarks = []

        for level, title, page in bookmarks:
            page_idx = page - 1  # pymupdf is 1-based
            if page_idx < 0:
                continue
            title = (title or "").strip() or T("toc.no_title")
            item = QTreeWidgetItem(
                [T("toc.page_fmt", title=title, page=page_idx + 1)]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, page_idx)

            level = max(0, int(level))
            while stack and stack[-1][0] >= level:
                stack.pop()
            if stack:
                stack[-1][1].addChild(item)
            else:
                self.tree.addTopLevelItem(item)
            stack.append((level, item))
            self._items.append(item)

        self.tree.expandAll()

    def select_page(self, page_num: int) -> None:
        """Highlight the TOC entry that best matches the given 0-based page."""
        best: QTreeWidgetItem | None = None
        for item in self._items:
            p = item.data(0, Qt.ItemDataRole.UserRole)
            if p is None:
                continue
            if p <= page_num:
                best = item
            else:
                break

        if best is None:
            return
        self._syncing = True
        self.tree.setCurrentItem(best)
        self.tree.scrollToItem(best)
        self._syncing = False

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int):
        if self._syncing:
            return
        page_idx = item.data(0, Qt.ItemDataRole.UserRole)
        if page_idx is None:
            return
        self.page_selected.emit(int(page_idx))


# ═══════════════════════════════════════════════════════════════════════════════
#  settings dialog
# ═══════════════════════════════════════════════════════════════════════════════


_SETTINGS_QSS = """
QDialog { background: #2b2b2b; }
QGroupBox { color: #eee; border: 1px solid #555; border-radius: 6px;
            margin-top: 10px; padding-top: 8px; font-size: 13px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QLabel { color: #ccc; font-size: 13px; }
QComboBox, QSpinBox, QDoubleSpinBox {
    background: #444; color: #eee; border: 1px solid #555;
    border-radius: 4px; padding: 4px 8px; font-size: 13px;
    min-width: 220px;
}
QComboBox QAbstractItemView { background: #444; color: #eee;
    selection-background-color: #3a6bc5; selection-color: #fff; }
QCheckBox { color: #ddd; font-size: 13px; spacing: 8px; }
QRadioButton { color: #ddd; font-size: 13px; spacing: 8px; }
QRadioButton::indicator {
    width: 13px; height: 13px; border: 1px solid #888;
    border-radius: 7px; background: #444;
}
QRadioButton::indicator:checked {
    background: #3a6bc5; border-color: #3a6bc5;
}
QPushButton {
    background: #444; color: #eee; border: 1px solid #555;
    border-radius: 4px; padding: 6px 18px; font-size: 13px;
}
QPushButton:hover { background: #555; }
QPushButton:pressed { background: #666; }
"""


class SettingsDialog(QDialog):
    """Menu di configurazione: lingua UI, lingue traduzione, preferenze.

    Ogni stringa visibile passa da T(), quindi il dialogo è mostrato nella
    lingua UI corrente. Cambiare la lingua UI dentro il dialogo fa un'anteprima
    dal vivo (ri-traduce solo le label del dialogo); Annulla ripristina la
    lingua originale senza toccare il MainWindow.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._orig_ui_lang = get_language()
        self._cfg = get_config()

        self.setWindowTitle(T("settings.title"))
        self.setMinimumWidth(480)
        self.setStyleSheet(_SETTINGS_QSS)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        # ── Lingua ──────────────────────────────────────────────────────
        self._box_lang = QGroupBox(T("settings.group.lang"))
        lang_form = QFormLayout(self._box_lang)
        self._lbl_ui = QLabel(T("settings.lang.ui"))
        self._ui_combo = QComboBox()
        for code, name in LANGUAGES.items():
            self._ui_combo.addItem(name, code)
        lang_form.addRow(self._lbl_ui, self._ui_combo)
        self._lbl_src = QLabel(T("settings.lang.source"))
        self._src_combo = QComboBox()
        for code, (flag, name) in TRANSLATION_LANGUAGES.items():
            self._src_combo.addItem(f"{flag} {name}", code)
        lang_form.addRow(self._lbl_src, self._src_combo)
        self._lbl_dst = QLabel(T("settings.lang.target"))
        self._dst_combo = QComboBox()
        for code, (flag, name) in TRANSLATION_LANGUAGES.items():
            if code == "auto":
                continue
            self._dst_combo.addItem(f"{flag} {name}", code)
        lang_form.addRow(self._lbl_dst, self._dst_combo)
        root.addWidget(self._box_lang)

        # ── Traduzione ──────────────────────────────────────────────────
        self._box_translation = QGroupBox(T("settings.group.translation"))
        trans_form = QFormLayout(self._box_translation)
        self._lbl_engine = QLabel(T("settings.translation.engine"))
        self._engine_combo = QComboBox()
        for code in TRANSLATION_ENGINES:
            self._engine_combo.addItem(T(f"engine.option.{code}"), code)
        trans_form.addRow(self._lbl_engine, self._engine_combo)
        root.addWidget(self._box_translation)

        # ── Motore di clonazione (pdf2zh_next v2) ───────────────────────
        self._box_clone = QGroupBox(T("settings.group.clone"))
        clone_form = QFormLayout(self._box_clone)
        self._lbl_pdf2zh = QLabel(T("settings.clone.pdf2zh"))
        pdf2zh_row = QHBoxLayout()
        self._pdf2zh_edit = QLineEdit()
        self._pdf2zh_edit.setPlaceholderText(T("settings.clone.pdf2zh.ph"))
        self._btn_pdf2zh = QPushButton(T("settings.clone.browse"))
        self._btn_pdf2zh.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_pdf2zh.clicked.connect(self._on_browse_pdf2zh)
        pdf2zh_row.addWidget(self._pdf2zh_edit)
        pdf2zh_row.addWidget(self._btn_pdf2zh)
        clone_form.addRow(self._lbl_pdf2zh, pdf2zh_row)
        self._lbl_clone_hint = QLabel(T("settings.clone.hint"))
        self._lbl_clone_hint.setWordWrap(True)
        self._lbl_clone_hint.setStyleSheet("color: #999; font-size: 12px;")
        clone_form.addRow(self._lbl_clone_hint)
        root.addWidget(self._box_clone)

        # ── Testo ───────────────────────────────────────────────────────
        self._box_text = QGroupBox(T("settings.group.text"))
        text_form = QFormLayout(self._box_text)
        self._lbl_font = QLabel(T("settings.text.font"))
        self._font_spin = QSpinBox()
        self._font_spin.setRange(10, 16)
        text_form.addRow(self._lbl_font, self._font_spin)
        self._md_check = QCheckBox(T("settings.text.md"))
        text_form.addRow(self._md_check)
        self._header_check = QCheckBox(T("settings.text.header"))
        text_form.addRow(self._header_check)
        self._save_edits_check = QCheckBox(T("settings.edits.save"))
        text_form.addRow(self._save_edits_check)
        self._btn_clear_edits = QPushButton(T("settings.edits.clear"))
        self._btn_clear_edits.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_clear_edits.clicked.connect(self._on_clear_edits)
        text_form.addRow(self._btn_clear_edits)
        # Gruppo testo (markdown/header/edit) DORMIENTE: il pannello destro non
        # mostra testo estratto. Il widget esiste ma resta fuori dal layout.
        self._box_text.hide()

        # ── Visualizzazione ─────────────────────────────────────────────
        self._box_view = QGroupBox(T("settings.group.view"))
        view_form = QFormLayout(self._box_view)
        self._lbl_zoom = QLabel(T("settings.view.zoom"))
        self._zoom_spin = QDoubleSpinBox()
        self._zoom_spin.setRange(0.5, 4.0)
        self._zoom_spin.setSingleStep(0.25)
        self._zoom_spin.setDecimals(2)
        view_form.addRow(self._lbl_zoom, self._zoom_spin)
        root.addWidget(self._box_view)

        # ── Comportamento ───────────────────────────────────────────────
        self._box_beh = QGroupBox(T("settings.group.behavior"))
        beh_form = QFormLayout(self._box_beh)
        self._resume_check = QCheckBox(T("settings.behavior.resume"))
        beh_form.addRow(self._resume_check)
        self._tab_check = QCheckBox(T("settings.behavior.tab"))
        beh_form.addRow(self._tab_check)
        root.addWidget(self._box_beh)

        # ── Pulsanti ────────────────────────────────────────────────────
        btns = QHBoxLayout()
        btns.addStretch(1)
        self._btn_ok = QPushButton(T("settings.ok"))
        self._btn_ok.clicked.connect(self.accept)
        self._btn_cancel = QPushButton(T("settings.cancel"))
        self._btn_cancel.clicked.connect(self.reject)
        for b in (self._btn_ok, self._btn_cancel):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            btns.addWidget(b)
        root.addLayout(btns)

        self._load_values()

        # Anteprima lingua dal vivo: ri-traduce SOLO le label del dialogo.
        self._ui_combo.currentIndexChanged.connect(self._on_ui_preview)

    def _on_browse_pdf2zh(self):
        """Pick the pdf2zh_next executable (per-piattaforma)."""
        start = self._pdf2zh_edit.text().strip() or str(Path.home())
        path_str, _ = QFileDialog.getOpenFileName(
            self, T("settings.clone.pdf2zh"), start
        )
        if path_str:
            self._pdf2zh_edit.setText(path_str)

    def _on_clear_edits(self):
        """Ask confirmation, then wipe the current document's saved edits."""
        ret = QMessageBox.question(
            self,
            T("settings.title"),
            T("settings.edits.clear_confirm"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        parent = self.parent()
        if parent is not None and hasattr(parent, "clear_saved_edits"):
            parent.clear_saved_edits()
        QMessageBox.information(self, T("settings.title"), T("settings.edits.clear_done"))

    def _load_values(self):
        """Populate the widgets from the current config (bozza)."""
        cfg = self._cfg
        idx = self._ui_combo.findData(cfg.get("lang", "it"))
        self._ui_combo.setCurrentIndex(max(0, idx))
        idx = self._src_combo.findData(cfg.get("src_lang", "auto"))
        self._src_combo.setCurrentIndex(max(0, idx))
        idx = self._dst_combo.findData(cfg.get("dst_lang", "it"))
        self._dst_combo.setCurrentIndex(max(0, idx))
        idx = self._engine_combo.findData(cfg.get("engine", "google"))
        self._engine_combo.setCurrentIndex(max(0, idx))
        self._font_spin.setValue(int(cfg.get("font_size", 12)))
        self._md_check.setChecked(bool(cfg.get("render_md", True)))
        self._header_check.setChecked(bool(cfg.get("show_header", True)))
        self._save_edits_check.setChecked(bool(cfg.get("save_edits", True)))
        self._zoom_spin.setValue(float(cfg.get("zoom", 3.0)))
        self._resume_check.setChecked(bool(cfg.get("resume_last_page", True)))
        self._tab_check.setChecked(bool(cfg.get("remember_tab", True)))
        self._pdf2zh_edit.setText(str(cfg.get("pdf2zh_bin", "") or ""))

    def _on_ui_preview(self, index: int):
        """Live preview: re-label the dialog when the UI language changes."""
        code = self._ui_combo.itemData(index)
        if code and code != get_language():
            set_language(code)
            self.retranslate()

    def retranslate(self):
        """Re-apply the dialog's own labels (names in combos are endonyms)."""
        self.setWindowTitle(T("settings.title"))
        self._box_lang.setTitle(T("settings.group.lang"))
        self._box_translation.setTitle(T("settings.group.translation"))
        self._box_text.setTitle(T("settings.group.text"))
        self._box_view.setTitle(T("settings.group.view"))
        self._box_beh.setTitle(T("settings.group.behavior"))
        self._lbl_ui.setText(T("settings.lang.ui"))
        self._lbl_src.setText(T("settings.lang.source"))
        self._lbl_dst.setText(T("settings.lang.target"))
        self._lbl_engine.setText(T("settings.translation.engine"))
        self._box_clone.setTitle(T("settings.group.clone"))
        self._lbl_pdf2zh.setText(T("settings.clone.pdf2zh"))
        self._pdf2zh_edit.setPlaceholderText(T("settings.clone.pdf2zh.ph"))
        self._btn_pdf2zh.setText(T("settings.clone.browse"))
        self._lbl_clone_hint.setText(T("settings.clone.hint"))
        # Le etichette dei motori passano da T(): ricostruisci la combo
        # preservando la selezione corrente.
        cur = self._engine_combo.currentData()
        self._engine_combo.clear()
        for code in TRANSLATION_ENGINES:
            self._engine_combo.addItem(T(f"engine.option.{code}"), code)
        if cur is not None:
            idx = self._engine_combo.findData(cur)
            self._engine_combo.setCurrentIndex(max(0, idx))
        self._lbl_font.setText(T("settings.text.font"))
        self._md_check.setText(T("settings.text.md"))
        self._header_check.setText(T("settings.text.header"))
        self._save_edits_check.setText(T("settings.edits.save"))
        self._btn_clear_edits.setText(T("settings.edits.clear"))
        self._lbl_zoom.setText(T("settings.view.zoom"))
        self._resume_check.setText(T("settings.behavior.resume"))
        self._tab_check.setText(T("settings.behavior.tab"))
        self._btn_ok.setText(T("settings.ok"))
        self._btn_cancel.setText(T("settings.cancel"))

    def reject(self):
        """Cancel: restore the original UI language (main window untouched)."""
        set_language(self._orig_ui_lang)
        super().reject()

    def values(self) -> dict:
        """Return the dialog choices (applied by MainWindow on OK)."""
        return {
            "lang": self._ui_combo.currentData() or get_language(),
            "src_lang": self._src_combo.currentData() or "auto",
            "dst_lang": self._dst_combo.currentData() or "it",
            "engine": self._engine_combo.currentData() or "google",
            "zoom": float(self._zoom_spin.value()),
            "font_size": int(self._font_spin.value()),
            "render_md": bool(self._md_check.isChecked()),
            "show_header": bool(self._header_check.isChecked()),
            "save_edits": bool(self._save_edits_check.isChecked()),
            "resume_last_page": bool(self._resume_check.isChecked()),
            "remember_tab": bool(self._tab_check.isChecked()),
            "pdf2zh_bin": self._pdf2zh_edit.text().strip(),
        }


class ExportDialog(QDialog):
    """Choose what to export: the current page or a page range.

    The dialog also lets the user pick the **engine** and the **output
    language** for this export only (one-off, independent of the right
    panel's current settings). In range mode it can optionally queue the
    translation of pages that are not in cache yet ("translate missing pages
    first"); ``cached_pages`` is queried with the selected engine/languages so
    the "ready on disk" count is accurate. The dialog only collects the
    choice; MainWindow owns the wait/translation/export.
    """

    def __init__(
        self,
        engine: clone_engine.CloneEngine,
        engine_name: str,
        target_lang: str,
        source_lang: str,
        current_page: int,
        page_count: int,
        parent=None,
    ):
        super().__init__(parent)
        self._engine = engine
        self._engine_name = engine_name
        self._target_lang = target_lang
        self._source_lang = source_lang
        self._page_count = max(int(page_count), 1)
        self._current_page = min(max(int(current_page), 0), self._page_count - 1)

        self.setWindowTitle(T("export.title"))
        self.setMinimumWidth(460)
        self.setStyleSheet(_SETTINGS_QSS)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        self._mode_box = QGroupBox(T("export.title"))
        mode_lay = QVBoxLayout(self._mode_box)

        self._rad_current = QRadioButton(T("export.mode.current"))
        self._rad_range = QRadioButton(T("export.mode.range"))
        self._rad_current.setChecked(True)
        self._rad_current.setCursor(Qt.CursorShape.PointingHandCursor)
        self._rad_range.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._rad_current)
        self._mode_group.addButton(self._rad_range)
        mode_lay.addWidget(self._rad_current)
        mode_lay.addWidget(self._rad_range)

        # Range inputs (visible only in range mode).
        self._range_widget = QWidget()
        range_form = QFormLayout(self._range_widget)
        range_form.setContentsMargins(20, 0, 0, 0)
        self._lbl_from = QLabel(T("export.range.from"))
        self._lbl_to = QLabel(T("export.range.to"))
        self._from_spin = QSpinBox()
        self._to_spin = QSpinBox()
        for sp in (self._from_spin, self._to_spin):
            sp.setRange(1, self._page_count)
            sp.setValue(self._current_page + 1)
        range_form.addRow(self._lbl_from, self._from_spin)
        range_form.addRow(self._lbl_to, self._to_spin)
        mode_lay.addWidget(self._range_widget)

        self._ready_lbl = QLabel("")
        self._ready_lbl.setStyleSheet("color: #999; font-size: 12px;")
        self._ready_lbl.setContentsMargins(20, 0, 0, 0)
        mode_lay.addWidget(self._ready_lbl)

        self._chk_translate = QCheckBox(T("export.translate_missing"))
        self._chk_translate.setChecked(False)
        self._chk_translate.setContentsMargins(20, 0, 0, 0)
        # Finché l'utente non tocca la casella, viene attivata da sola quando
        # l'intervallo scelto contiene pagine non ancora tradotte (altrimenti
        # con range non in cache non partirebbe né salvataggio né traduzione).
        self._translate_touched = False
        self._chk_translate.toggled.connect(self._on_translate_toggled)
        mode_lay.addWidget(self._chk_translate)

        root.addWidget(self._mode_box)

        # ── Traduzione: motore + lingua di uscita (solo per questo export) ──
        self._trans_box = QGroupBox(T("export.group.translation"))
        trans_form = QFormLayout(self._trans_box)
        self._lbl_engine = QLabel(T("settings.translation.engine"))
        self._engine_combo = QComboBox()
        for code in CLONE_ENGINES:
            self._engine_combo.addItem(T(f"engine.option.{code}"), code)
        eidx = self._engine_combo.findData(engine_name)
        self._engine_combo.setCurrentIndex(max(0, eidx))
        trans_form.addRow(self._lbl_engine, self._engine_combo)

        self._lbl_dst = QLabel(T("settings.lang.target"))
        self._dst_combo = QComboBox()
        for code, (flag, name) in TRANSLATION_LANGUAGES.items():
            if code == "auto":
                continue
            self._dst_combo.addItem(f"{flag} {name}", code)
        didx = self._dst_combo.findData(target_lang)
        self._dst_combo.setCurrentIndex(max(0, didx))
        trans_form.addRow(self._lbl_dst, self._dst_combo)
        root.addWidget(self._trans_box)

        # ── Formato di uscita: PDF unito o pagine singole in ZIP ───────────
        self._format_box = QGroupBox(T("export.group.format"))
        format_lay = QVBoxLayout(self._format_box)
        self._rad_merged = QRadioButton(T("export.format.merged"))
        self._rad_zip = QRadioButton(T("export.format.zip"))
        self._rad_merged.setChecked(True)
        for rb in (self._rad_merged, self._rad_zip):
            rb.setCursor(Qt.CursorShape.PointingHandCursor)
        self._format_group = QButtonGroup(self)
        self._format_group.addButton(self._rad_merged)
        self._format_group.addButton(self._rad_zip)
        format_lay.addWidget(self._rad_merged)
        format_lay.addWidget(self._rad_zip)
        root.addWidget(self._format_box)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self._btn_ok = QPushButton(T("settings.ok"))
        self._btn_ok.clicked.connect(self.accept)
        self._btn_cancel = QPushButton(T("settings.cancel"))
        self._btn_cancel.clicked.connect(self.reject)
        for b in (self._btn_ok, self._btn_cancel):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            btns.addWidget(b)
        root.addLayout(btns)

        self._rad_range.toggled.connect(self._update_mode)
        self._from_spin.valueChanged.connect(self._on_spin_changed)
        self._to_spin.valueChanged.connect(self._on_spin_changed)
        self._engine_combo.currentIndexChanged.connect(self._update_ready)
        self._dst_combo.currentIndexChanged.connect(self._update_ready)
        self._update_mode()

    # ── stato del dialogo ─────────────────────────────────────────────

    def _update_mode(self):
        range_on = self._rad_range.isChecked()
        self._range_widget.setVisible(range_on)
        self._ready_lbl.setVisible(range_on)
        self._chk_translate.setVisible(range_on)
        self._update_ready()

    def _on_spin_changed(self):
        # I due campi sono indipendenti: NON si forza l'altro campo. Se
        # l'utente inserisce "da" > "a" l'intervallo viene normalizzato in
        # ``_range_pages`` (min/max) al momento dell'uso.
        self._update_ready()

    def _range_pages(self) -> tuple[int, int]:
        a = self._from_spin.value() - 1
        b = self._to_spin.value() - 1
        return (min(a, b), max(a, b))

    def _on_translate_toggled(self, _checked: bool):
        self._translate_touched = True

    def _update_ready(self):
        if not self._rad_range.isChecked():
            return
        a, b = self._range_pages()
        cached = self._engine.cached_pages(
            a, b, self.chosen_engine(), self._source_lang, self.chosen_target()
        )
        total = b - a + 1
        missing = total - len(cached)
        self._ready_lbl.setText(
            T(
                "export.ready",
                cached=len(cached),
                total=total,
                missing=missing,
            )
        )
        # L'opzione ha senso solo se manca qualcosa.
        self._chk_translate.setEnabled(missing > 0)
        # Attiva da sola la traduzione quando ci sono pagine mancanti, finché
        # l'utente non ha espresso una scelta esplicita.
        if missing > 0 and not self._translate_touched:
            self._chk_translate.blockSignals(True)
            self._chk_translate.setChecked(True)
            self._chk_translate.blockSignals(False)

    # ── API per MainWindow ────────────────────────────────────────────

    def is_current(self) -> bool:
        return self._rad_current.isChecked()

    def chosen_engine(self) -> str:
        return self._engine_combo.currentData() or "google"

    def chosen_target(self) -> str:
        return self._dst_combo.currentData() or self._target_lang

    def chosen_source(self) -> str:
        return self._source_lang

    def chosen_pages(self) -> list[int]:
        if self.is_current():
            return [self._current_page]
        a, b = self._range_pages()
        return list(range(a, b + 1))

    def translate_missing(self) -> bool:
        return self._rad_range.isChecked() and self._chk_translate.isChecked()

    def chosen_zip(self) -> bool:
        """True se l'utente vuole le pagine singole in un archivio ZIP."""
        return self._rad_zip.isChecked()

    def range_label(self) -> str:
        """Suffisso per il nome file: "156" oppure "156-159"."""
        if self.is_current():
            return str(self._current_page + 1)
        a, b = self._range_pages()
        return f"{a + 1}-{b + 1}"


def _fmt_duration(seconds: float) -> str:
    """Formatta secondi come MM:SS (o H:MM:SS se serve)."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class ExportProgressDialog(QDialog):
    """Modal wait shown while the missing range pages are translated and the
    PDF is written.

    It keeps the user informed with live feedback (engine/language, range,
    a working/done activity line, page i/N, a busy-then-determinate progress
    bar, running counts, elapsed + estimated remaining time and a per-page
    log) and holds the application modal so navigation/settings cannot change
    mid-queue. When the job finishes it stays open in a "completed" state
    showing the saved file path, so the export never closes abruptly. Cancel
    is cooperative: the in-flight pdf2zh_next subprocess cannot be
    interrupted, so the queue stops after the current page.
    """

    cancelled = pyqtSignal()

    def __init__(
        self,
        engine_label: str,
        lang_label: str,
        page_from: int,
        page_to: int,
        missing: int,
        cached: int,
        total: int,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle(T("export.progress.title"))
        self.setMinimumWidth(560)
        self.setStyleSheet(_SETTINGS_QSS)
        self._total = max(int(total), 1)
        self._start: float | None = None
        self._done = 0
        self._failed = 0
        self._phase = "idle"
        self._finished = False
        self._current_page: int | None = None
        self._dots = 0
        self._dest: Path | None = None

        root = QVBoxLayout(self)
        root.setSpacing(8)

        self._ctx_lbl = QLabel(
            T("export.progress.engine_lang", engine=engine_label, lang=lang_label)
        )
        root.addWidget(self._ctx_lbl)

        self._range_lbl = QLabel(
            T(
                "export.progress.range",
                **{
                    "from": page_from,
                    "to": page_to,
                    "missing": missing,
                    "cached": cached,
                },
            )
        )
        self._range_lbl.setStyleSheet("color: #aaa; font-size: 12px;")
        root.addWidget(self._range_lbl)

        # Riga di attività: sempre valorizzata, così la finestra non appare
        # mai "vuota" durante l'attesa.
        self._activity_lbl = QLabel("")
        self._activity_lbl.setStyleSheet(
            "color: #fff; font-size: 14px; font-weight: bold;"
        )
        root.addWidget(self._activity_lbl)

        self._page_lbl = QLabel("")
        self._page_lbl.setStyleSheet("color: #ddd;")
        root.addWidget(self._page_lbl)

        self._bar = QProgressBar()
        self._bar.setRange(0, self._total)
        self._bar.setValue(0)
        root.addWidget(self._bar)

        self._stats_lbl = QLabel("")
        self._stats_lbl.setStyleSheet("color: #999; font-size: 12px;")
        root.addWidget(self._stats_lbl)

        self._eta_lbl = QLabel("")
        self._eta_lbl.setStyleSheet("color: #999; font-size: 12px;")
        root.addWidget(self._eta_lbl)

        self._path_lbl = QLabel("")
        self._path_lbl.setStyleSheet("color: #7fd67f; font-size: 12px;")
        self._path_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._path_lbl.setWordWrap(True)
        self._path_lbl.setVisible(False)
        root.addWidget(self._path_lbl)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(110)
        self._log.setStyleSheet("font-family: monospace; font-size: 12px;")
        root.addWidget(self._log)

        btns = QHBoxLayout()
        btns.addStretch(1)
        self._btn_open = QPushButton(T("export.progress.open_folder"))
        self._btn_open.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_open.clicked.connect(self._open_folder)
        self._btn_open.setVisible(False)
        btns.addWidget(self._btn_open)
        self._btn_cancel = QPushButton(T("settings.cancel"))
        self._btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_cancel.clicked.connect(self._on_cancel)
        btns.addWidget(self._btn_cancel)
        self._btn_close = QPushButton(T("export.progress.close"))
        self._btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_close.clicked.connect(self.accept)
        self._btn_close.setVisible(False)
        btns.addWidget(self._btn_close)
        root.addLayout(btns)

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)

    # ── ciclo di vita del job ─────────────────────────────────────────

    def begin(self, page_1based: int):
        """Avvia la fase di traduzione mostrando subito l'attività."""
        self._start = time.perf_counter()
        self._current_page = page_1based
        self._phase = "translating"
        self._log.appendPlainText(
            T("export.progress.activity_page", page=page_1based)
        )
        self.set_translating(page_1based)
        self._timer.start()

    def set_translating(self, page_1based: int):
        self._current_page = page_1based
        self._phase = "translating"
        self._bar.setRange(0, 0)  # busy/indeterminata
        self._page_lbl.setText(
            T(
                "export.progress.label",
                page=page_1based,
                done=self._done,
                total=self._total,
            )
        )
        self._activity_lbl.setText(
            T("export.progress.activity_page", page=page_1based)
            + "." * self._dots
        )
        self._update_stats()

    def log_ok(self, page: int):
        self._log.appendPlainText(T("export.progress.page_ok", page=page + 1))
        self._activity_lbl.setText(
            T("export.progress.activity_done_page", page=page + 1)
        )

    def log_fail(self, page: int, reason: str):
        self._log.appendPlainText(
            T("export.progress.page_fail", page=page + 1, reason=reason)
        )
        self._activity_lbl.setText(
            T("export.progress.activity_done_page", page=page + 1)
        )

    def set_stats(self, done: int, failed: int, total: int):
        self._done = int(done)
        self._failed = int(failed)
        self._total = max(int(total), 1)
        self._bar.setRange(0, self._total)
        self._bar.setValue(self._done)
        self._update_stats()

    def set_phase_exporting(self):
        if self._start is None:
            self._start = time.perf_counter()
            self._timer.start()
        self._phase = "exporting"
        self._current_page = None
        self._bar.setRange(0, 0)  # busy mentre si scrive il PDF
        self._page_lbl.setText("")
        self._activity_lbl.setText(T("export.progress.activity_exporting"))
        self._update_stats()

    def set_completed(self, dest: str, count: int, failed: int):
        self._finished = True
        self._timer.stop()
        self._phase = "done"
        self._bar.setRange(0, self._total)
        self._bar.setValue(self._total)
        self._activity_lbl.setText(T("export.progress.completed_title"))
        self._page_lbl.setText("")
        self._stats_lbl.setText(
            T(
                "export.progress.completed_summary",
                count=count,
                failed=failed,
                elapsed=_fmt_duration(self._elapsed()),
            )
        )
        self._eta_lbl.setText("")
        self._dest = Path(dest)
        self._path_lbl.setText(
            T("export.progress.saved_path", path=str(dest))
        )
        self._path_lbl.setVisible(True)
        self._log.appendPlainText(T("export.progress.completed_title"))
        self._btn_cancel.setVisible(False)
        self._btn_open.setVisible(True)
        self._btn_close.setVisible(True)
        self._btn_close.setFocus()

    def set_error(self, message: str):
        """Termina in errore mantenendo la finestra aperta (feedback)."""
        self._finished = True
        self._timer.stop()
        self._phase = "error"
        self._bar.setRange(0, self._total)
        self._activity_lbl.setText(message)
        self._page_lbl.setText("")
        self._eta_lbl.setText("")
        self._btn_cancel.setVisible(False)
        self._btn_close.setVisible(True)
        self._btn_close.setFocus()

    def set_cancelling(self):
        self._btn_cancel.setEnabled(False)
        self._activity_lbl.setText(T("export.progress.cancelling"))

    def finish(self):
        self._finished = True
        self._timer.stop()

    # ── interni ───────────────────────────────────────────────────────

    def _elapsed(self) -> float:
        return (time.perf_counter() - self._start) if self._start else 0.0

    def _tick(self):
        self._dots = (self._dots + 1) % 4
        if self._phase == "translating" and self._current_page:
            self._activity_lbl.setText(
                T("export.progress.activity_page", page=self._current_page)
                + "." * self._dots
            )
        self._update_stats()

    def _update_stats(self):
        if self._phase not in ("translating", "exporting"):
            return
        elapsed = self._elapsed()
        self._stats_lbl.setText(
            T(
                "export.progress.stats",
                done=self._done,
                failed=self._failed,
                elapsed=_fmt_duration(elapsed),
            )
        )
        eta = None
        if self._phase == "translating" and 0 < self._done < self._total:
            eta = (elapsed / self._done) * (self._total - self._done)
        self._eta_lbl.setText(
            T("export.progress.eta", eta=_fmt_duration(eta))
            if eta is not None
            else ""
        )

    def _on_cancel(self):
        if self._finished:
            return
        self.cancelled.emit()

    def _open_folder(self):
        if self._dest is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._dest.parent)))


# ═══════════════════════════════════════════════════════════════════════════════
#  main window
# ═══════════════════════════════════════════════════════════════════════════════


class MainWindow(QMainWindow):
    # Motore di rendering (PyMuPDF) e di estrazione (PyMuPDF4LLM) fissi;
    # l'engine adattativo dei fix è sempre attivo (nessun dropdown).

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Noesis PDF Reader Lite")
        self.resize(1400, 900)
        self.setMinimumSize(800, 600)

        # Preferenze dal config (inizializzato in main() prima della UI).
        # ``_base_render_scale`` è la risoluzione di render persistita
        # (impostazione "zoom", 0.5–4.0); ``_view_zoom`` è lo zoom visibile
        # runtime (1.0 = adatta alla finestra). ``_render_scale`` è la
        # risoluzione effettiva usata per render + mappatura zone.
        self._base_render_scale: float = float(get_setting("zoom", 3.0))
        self._view_zoom: float = 1.0
        self._render_scale: float = self._base_render_scale
        self._render_md: bool = bool(get_setting("render_md", True))
        self._show_header: bool = bool(get_setting("show_header", True))
        self._remember_tab: bool = bool(get_setting("remember_tab", True))
        self._resume_last_page: bool = bool(get_setting("resume_last_page", True))
        self._last_result: tuple[str, str, float] | None = None  # (text, label, elapsed)
        self._last_elapsed: float = 0.0
        # Lingua con cui le stringhe dei widget sono state applicate: serve a
        # capire se serve una ri-traduzione dopo l'OK delle Impostazioni (la
        # anteprima del dialogo può aver già cambiato la lingua globale).
        self._ui_lang_applied: str = get_language()
        # Persistenza dell'ultima pagina con debounce (2 s dopo l'ultimo cambio).
        self._last_page_timer = QTimer(self)
        self._last_page_timer.setSingleShot(True)
        self._last_page_timer.setInterval(2000)
        self._last_page_timer.timeout.connect(save_config)

        # State
        self._pdf_path: Path | None = None
        self._current_page: int = 0
        self._page_count: int = 0
        self._mupdf_doc = None       # pymupdf Document (render + layout + images)
        self._images_dir: Path | None = None  # dir for extracted figures
        self._current_images: list[str] = []  # captured regions, kept until the book closes
        self._excluded_zones: dict[int, list[tuple]] = {}  # page → excluded PDF rects
        self._inclusion_zones: dict[int, list[tuple]] = {}  # page → numbered inclusion rects
        # Estrazione asincrona: cache del testo grezzo per (pagina, lingua OCR)
        # + thread in background. Sui PDF scansionati l'OCR richiede secondi:
        # niente lavoro sincrono sul thread GUI, e la cache evita di ri-OCRare.
        self._extraction_cache: dict[tuple[int, str], str] = {}
        # Testo finale per (pagina, lingua OCR, chiave zone): il caso "auto"
        # (nessuna zona) è la voce comune e rende la navigazione istantanea.
        self._final_text_cache: dict[tuple[int, str, str], tuple[str, str, float]] = {}
        self._extraction_cache_file: Path | None = None
        self._doc_fingerprint: str = ""
        self._extract_thread: ExtractThread | None = None
        self._retired_extract_threads: list[ExtractThread] = []
        self._extract_generation: int = 0

        # ── Motore di clonazione (pdf2zh_next v2) ──────────────────────
        # Un'istanza per tutta l'app; cambia documento con ``set_document``.
        self._clone_engine = clone_engine.CloneEngine(
            cache_root=_app_data_base() / "clones",
            pdf2zh_bin=get_setting("pdf2zh_bin", "") or None,
        )
        self._clone_thread: CloneTranslateThread | None = None
        self._retired_clone_threads: list[CloneTranslateThread] = []
        self._clone_generation: int = 0
        self._clone_run_start: dict[str, float] = {}

        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Toolbar
        self._build_toolbar(root_layout)

        # TOC dock (left, dockable)
        self.toc_panel = TocPanel()
        self.toc_panel.page_selected.connect(self._goto_toc_page)
        self.toc_dock = QDockWidget(T("dock.toc"), self)
        self.toc_dock.setObjectName("tocDock")
        self.toc_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea
            | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.toc_dock.setWidget(self.toc_panel)
        self.toc_dock.setMinimumWidth(220)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.toc_dock)
        self.toc_dock.visibilityChanged.connect(self.btn_toc.setChecked)

        # Splitter
        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left — mini toolbar + scroll area wrapping the page view
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.pdf_view = PdfPageView()
        self.pdf_view.region_selected.connect(self._on_region_selected)
        self.pdf_view.region_excluded.connect(self._on_region_excluded)
        self.pdf_view.region_included.connect(self._on_region_included)
        self.scroll_area.setWidget(self.pdf_view)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        left_layout.addWidget(self._build_page_toolbar())
        left_layout.addWidget(self.scroll_area)

        # Right — clone of the current page, translated (layout preserved)
        self.translated_panel = TranslatedPagePanel()
        self.translated_panel.engine_changed.connect(self._on_engine_selected)
        self.translated_panel.export_requested.connect(self._on_export_translated)
        self.translated_panel.set_target_language(get_target_lang())
        self.translated_panel.set_engine(get_translation_engine())
        # Ripristina lo stato collassato/espanso della barra motori (persistito).
        self.translated_panel.set_collapsed(
            bool(get_setting("clone_bar_collapsed", False)), persist=False
        )

        # Pannello testo di noesis-pdf-reader-lite: mantenuto dormiente
        # (nascosto, non aggiunto al layout) per la futura integrazione Docling.
        self.text_panel = TranslatablePanel(self)
        self.text_panel.hide()
        self.text_panel.image_removed.connect(self._on_image_removed)
        self.text_panel.toast.connect(self._show_toast)

        self.splitter.addWidget(left_panel)
        self.splitter.addWidget(self.translated_panel)
        self.splitter.setSizes([700, 700])

        root_layout.addWidget(self.splitter)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage(T("status.ready"))

        # Shortcuts
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, self._next_page)
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, self._prev_page)
        QShortcut(QKeySequence(Qt.Key.Key_PageDown), self, self._next_page)
        QShortcut(QKeySequence(Qt.Key.Key_PageUp), self, self._prev_page)
        QShortcut(QKeySequence.StandardKey.ZoomIn, self, self._zoom_in)
        QShortcut(QKeySequence.StandardKey.ZoomOut, self, self._zoom_out)
        QShortcut(
            QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_0), self, self._zoom_reset
        )
        QShortcut(
            QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_M), self, self._toggle_markdown
        )
        QShortcut(
            QKeySequence(Qt.Modifier.CTRL | Qt.Key.Key_E),
            self,
            self._on_export_translated,
        )

        # Dark theme
        self.setStyleSheet("""
            QMainWindow { background: #2b2b2b; }
            QToolBar {
                background: #333; padding: 4px; spacing: 6px;
                border-bottom: 1px solid #444;
            }
            QToolBar QPushButton {
                background: #444; color: #eee; border: 1px solid #555;
                border-radius: 4px; padding: 6px 14px; font-size: 13px;
            }
            QToolBar QPushButton:hover { background: #555; }
            QToolBar QPushButton:pressed { background: #666; }
            QToolBar QPushButton:checked { background: #3a6bc5; color: #fff; }
            QToolBar QSpinBox {
                background: #444; color: #eee; border: 1px solid #555;
                border-radius: 4px; padding: 4px 8px; font-size: 13px;
                min-width: 60px;
            }
            /* Page-number box: no up/down buttons (they made the widget look
               cluttered); navigation is via ◀ ▶ or by typing a page number. */
            QToolBar QSpinBox::up-button, QToolBar QSpinBox::down-button {
                width: 0px; border: none; background: transparent;
            }
            QToolBar QLabel { color: #ccc; font-size: 13px; }
            QStatusBar { background: #333; color: #aaa; }

            /* Punti che altrimenti ereditano il tema di sistema: colori
               espliciti così l'app resta scura anche sui temi chiari/scuri
               del sistema operativo. */
            QScrollArea { background: #2b2b2b; border: none; }
            QSplitter::handle { background: #333; }
            QDockWidget { color: #ddd; }
            QDockWidget::title {
                background: #333; color: #ddd; padding: 5px 8px;
                text-align: left;
            }
            QDockWidget::close-button, QDockWidget::float-button {
                background: #444; border: none; border-radius: 2px;
            }
            QDockWidget::close-button:hover,
            QDockWidget::float-button:hover { background: #555; }
            QScrollBar:vertical {
                background: #2f2f2f; width: 12px; margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #555; min-height: 24px;
                border-radius: 6px; margin: 2px;
            }
            QScrollBar::handle:vertical:hover { background: #666; }
            QScrollBar:horizontal {
                background: #2f2f2f; height: 12px; margin: 0;
            }
            QScrollBar::handle:horizontal {
                background: #555; min-width: 24px;
                border-radius: 6px; margin: 2px;
            }
            QScrollBar::handle:horizontal:hover { background: #666; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                height: 0; width: 0;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: none;
            }
        """)

        # L'apertura del PDF è gestita in main(): argomento da riga di comando
        # oppure harrison2025.pdf nella directory corrente.

    # ── toolbar ───────────────────────────────────────────────────────────

    def _build_toolbar(self, parent_layout: QVBoxLayout):
        bar = QToolBar(T("toolbar.nav"))
        bar.setMovable(False)
        parent_layout.addWidget(bar)

        # Apri
        self.btn_open = QPushButton(T("toolbar.open"))
        self.btn_open.clicked.connect(self._on_open)
        bar.addWidget(self.btn_open)

        # Esporta la pagina tradotta in PDF (Ctrl+E)
        self.btn_export = QPushButton(T("toolbar.export"))
        self.btn_export.setToolTip(T("toolbar.export.tip"))
        self.btn_export.clicked.connect(self._on_export_translated)
        bar.addWidget(self.btn_export)

        # TOC toggle
        self.btn_toc = QPushButton(T("toolbar.toc"))
        self.btn_toc.setCheckable(True)
        self.btn_toc.setChecked(True)
        self.btn_toc.setToolTip(T("toolbar.toc.tip"))
        self.btn_toc.clicked.connect(
            lambda checked: self.toc_dock.setVisible(checked)
        )
        bar.addWidget(self.btn_toc)

        bar.addSeparator()

        # Prev
        self.btn_prev = QPushButton(T("toolbar.prev"))
        self.btn_prev.clicked.connect(self._prev_page)
        bar.addWidget(self.btn_prev)

        # Page spin
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.setValue(1)
        # Keyboard tracking off: with it on, every keystroke committed a value
        # and fired valueChanged -> _set_page -> full page render + text
        # extraction (Docling ~2-6s), freezing the box while typing. Now the
        # page changes only on Enter / focus-out. The up/down spin buttons are
        # hidden via QSS (cleaner look); typing + Enter or ◀ ▶ do navigation.
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.valueChanged.connect(self._on_spin)
        self.page_spin.setEnabled(False)
        bar.addWidget(self.page_spin)

        self.lbl_of = QLabel(T("toolbar.of"))
        bar.addWidget(self.lbl_of)
        self.lbl_total = QLabel("0")
        bar.addWidget(self.lbl_total)

        # Next
        self.btn_next = QPushButton(T("toolbar.next"))
        self.btn_next.clicked.connect(self._next_page)
        bar.addWidget(self.btn_next)

        bar.addSeparator()

        # Zoom
        self.btn_zoom_out = QPushButton("🔍−")
        self.btn_zoom_out.setToolTip(T("toolbar.zoom_out.tip"))
        self.btn_zoom_out.clicked.connect(self._zoom_out)
        bar.addWidget(self.btn_zoom_out)

        self.zoom_label = QLabel(T("toolbar.zoom.scale", x=f"{self._view_zoom:.2f}"))
        bar.addWidget(self.zoom_label)

        self.btn_zoom_in = QPushButton("🔍+")
        self.btn_zoom_in.setToolTip(T("toolbar.zoom_in.tip"))
        self.btn_zoom_in.clicked.connect(self._zoom_in)
        bar.addWidget(self.btn_zoom_in)

        bar.addSeparator()

        # Markdown rendering toggle NASCOSO: il pannello destro non mostra più
        # il testo estratto. Il widget resta definito ma fuori dal layout.
        self.btn_md_toggle = QPushButton(T("toolbar.md.on"))
        self.btn_md_toggle.setToolTip(T("toolbar.md.tip"))
        self.btn_md_toggle.setCheckable(True)
        self.btn_md_toggle.setChecked(self._render_md)
        self.btn_md_toggle.clicked.connect(self._toggle_markdown)
        self.btn_md_toggle.hide()

        bar.addSeparator()

        # Impostazioni (lingua UI, lingue traduzione, preferenze) — spinto a
        # destra da uno spacer espanso. Il cambio lingua UI avviene SOLO qui.
        _spacer = QWidget()
        _spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        bar.addWidget(_spacer)
        self.btn_settings = QPushButton(T("settings.button"))
        self.btn_settings.setToolTip(T("settings.button.tip"))
        self.btn_settings.clicked.connect(self._on_open_settings)
        bar.addWidget(self.btn_settings)

        # Guida online (apre il sito help nel browser di sistema)
        self.btn_help = QPushButton(T("toolbar.help"))
        self.btn_help.setToolTip(T("toolbar.help.tip"))
        self.btn_help.clicked.connect(self._on_open_help)
        bar.addWidget(self.btn_help)

    def _build_page_toolbar(self):
        """Mini toolbar of lite (zone selection/exclusion/inclusion).

        Dormiente: i widget vengono creati (i metodi che li usano restano
        validi) ma NON aggiunti alla barra, che resta nascosta. Verranno
        riattivati quando l'app integrerà il motore Docling.
        """
        bar = QToolBar(T("page_toolbar.title"))
        bar.setMovable(False)

        # Region selection (extract the image under a mouse-drawn rectangle)
        self.btn_select_region = QPushButton(T("page_toolbar.select"))
        self.btn_select_region.setCheckable(True)
        self.btn_select_region.setToolTip(T("page_toolbar.select.tip"))
        self.btn_select_region.clicked.connect(self._on_select_region_toggled)

        # Zone exclusion (manual cleaning fed to the adaptive engine)
        self.btn_exclude = QPushButton(T("page_toolbar.exclude"))
        self.btn_exclude.setCheckable(True)
        self.btn_exclude.setToolTip(T("page_toolbar.exclude.tip"))
        self.btn_exclude.clicked.connect(self._on_exclude_toggled)

        # Zone inclusion (green): numbered reading-order boxes
        self.btn_include = QPushButton(T("page_toolbar.include"))
        self.btn_include.setCheckable(True)
        self.btn_include.setToolTip(T("page_toolbar.include.tip"))
        self.btn_include.clicked.connect(self._on_include_toggled)

        self.btn_reset_zones = QPushButton(T("page_toolbar.reset"))
        self.btn_reset_zones.setToolTip(T("page_toolbar.reset.tip"))
        self.btn_reset_zones.clicked.connect(self._on_reset_zones)

        for btn in (
            self.btn_select_region, self.btn_exclude,
            self.btn_include, self.btn_reset_zones,
        ):
            btn.hide()
        bar.hide()
        return bar

    def _display_text(
        self,
        text: str,
        page_num: int = -1,
        images: list[str] | None = None,
    ):
        """Display the page text (already the auto-or-manual result)."""
        if images is None:
            images = self._current_images
        self.text_panel.show_text(
            text, as_markdown=self._render_md, page_num=page_num, images=images,
        )

    def _zones_key(self, page_num: int) -> str:
        """Stable fingerprint of the page's manual zones (cache key suffix)."""
        exclude = tuple(self._excluded_zones.get(page_num, ()))
        include = tuple(self._inclusion_zones.get(page_num, ()))
        if not exclude and not include:
            return "()"  # caso auto: chiave canonica, persistita su disco
        return repr((exclude, include))

    def _request_extraction(self, page_num: int):
        """Show the page text, extracting in the background if not cached.

        The whole pipeline (OCR + adaptive layout engine) runs in
        ``ExtractThread`` so the GUI never freezes on scanned PDFs.  The raw
        markdown is cached per ``(page, OCR language)`` and the final text per
        ``(page, OCR language, zones key)``: a repeat view (or a reopen with a
        warm disk cache) is displayed instantly, and changing manual zones
        only re-runs the layout engine (fast path, raw already cached).
        """
        if not self._pdf_path:
            return
        ocr_lang = _tess_lang_code(get_source_lang())
        key = (page_num, ocr_lang, self._zones_key(page_num))
        cached = self._final_text_cache.get(key)
        if cached is not None:
            text, label, elapsed = cached
            self._last_result = (text, label, elapsed)
            self._last_elapsed = elapsed
            self._display_last_result()
            self._show_page_status(elapsed)
            return

        self.status_bar.showMessage(T("status.extracting"))
        self._extract_generation += 1

        old = self._extract_thread
        if old is not None:
            try:
                old.result_ready.disconnect()
            except TypeError:
                pass  # already disconnected
            if old.isRunning():
                # Non distruggere un thread ancora attivo: lo si ritira e si
                # pulisce quando termina da solo (risultati scartati dal guard).
                self._retired_extract_threads.append(old)
                old.finished.connect(self._forget_retired_extract_thread)

        thread = ExtractThread(
            str(self._pdf_path), page_num, self._extract_generation, ocr_lang,
            exclude=tuple(self._excluded_zones.get(page_num, ())),
            include=tuple(self._inclusion_zones.get(page_num, ())),
            raw=self._extraction_cache.get((page_num, ocr_lang)),
        )
        thread.result_ready.connect(self._on_extraction_done)
        self._extract_thread = thread
        thread.start()

    def _forget_retired_extract_thread(self):
        """Drop a retired extract thread once it finishes."""
        thread = self.sender()
        if thread in self._retired_extract_threads:
            self._retired_extract_threads.remove(thread)

    def _on_extraction_done(
        self,
        generation: int,
        page_num: int,
        text: str,
        label: str,
        raw: str,
        elapsed: float,
    ):
        """Slot: background pipeline finished (stale results are ignored)."""
        if generation != self._extract_generation:
            return
        ocr_lang = _tess_lang_code(get_source_lang())
        self._extraction_cache[(page_num, ocr_lang)] = raw
        self._final_text_cache[(page_num, ocr_lang, self._zones_key(page_num))] = (
            text, label, elapsed,
        )
        self._save_extraction_cache()
        if page_num == self._current_page:
            self._finish_extraction(page_num, text, label, elapsed)

    def _finish_extraction(
        self, page_num: int, text: str, label: str, elapsed: float = 0.0
    ):
        """Display a finished extraction (text + engine already computed)."""
        self._last_result = (text, label, elapsed)
        self._last_elapsed = elapsed
        self._display_last_result()
        self._show_page_status(elapsed)

    def _display_last_result(self):
        """Re-display the stored extraction (header only if enabled).

        Used after a language/settings change: no re-extraction needed.
        """
        if self._last_result is None:
            return
        text, label, elapsed = self._last_result
        body = text
        if self._show_header:
            body = self._extraction_header(text, elapsed, label) + text
        self._display_text(body, page_num=self._current_page)

    def _toggle_markdown(self):
        """Toggle Markdown rendering on/off and refresh display."""
        self._render_md = not self._render_md
        if self._render_md:
            self.btn_md_toggle.setText(T("toolbar.md.on"))
        else:
            self.btn_md_toggle.setText(T("toolbar.md.plain"))
        set_setting("render_md", self._render_md)
        save_config()
        # Dormiente: il pannello destro non mostra più testo estratto, quindi
        # niente ri-estrazione (la scorciatoia Ctrl+M resta inerte).

    # ── persistent raw-extraction cache ──────────────────────────────────

    def _set_extraction_cache(self, path: Path | None):
        """Point the raw-extraction cache at this PDF and load saved entries."""
        self._extraction_cache.clear()
        self._extraction_cache_file = None
        self._doc_fingerprint = ""
        if path is None:
            return
        try:
            st = path.stat()
            self._doc_fingerprint = f"{st.st_size}-{st.st_mtime_ns}"
        except Exception:
            return
        cache_dir = _app_data_base() / "extraction"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return
        self._extraction_cache_file = cache_dir / f"{path.stem}.json"
        self._load_extraction_cache()

    def _load_extraction_cache(self):
        """Load saved raw extraction when it matches the current fingerprint."""
        if (
            self._extraction_cache_file is None
            or not self._extraction_cache_file.exists()
        ):
            return
        try:
            data = json.loads(self._extraction_cache_file.read_text(encoding="utf-8"))
        except Exception:
            return
        if data.get("fingerprint") != self._doc_fingerprint:
            return
        pages = data.get("pages") or {}
        for page_str, langs in pages.items():
            try:
                page = int(page_str)
            except (TypeError, ValueError):
                continue
            if not isinstance(langs, dict):
                continue
            for lang, value in langs.items():
                if isinstance(value, str):
                    # formato v1: {lang: testo grezzo}
                    self._extraction_cache[(page, lang)] = value
                elif isinstance(value, dict):
                    # formato v2: {lang: {raw, final}}
                    raw = value.get("raw")
                    if isinstance(raw, str):
                        self._extraction_cache[(page, lang)] = raw
                    final = value.get("final")
                    if (
                        isinstance(final, (list, tuple))
                        and len(final) == 3
                        and isinstance(final[0], str)
                    ):
                        # il "final" persistito è il caso auto (nessuna zona)
                        self._final_text_cache[(page, lang, "()")] = (
                            final[0], final[1], float(final[2]),
                        )

    def _save_extraction_cache(self):
        """Persist raw + auto final text to disk."""
        if self._extraction_cache_file is None:
            return
        pages: dict[str, dict[str, object]] = {}
        for (page, lang), raw in self._extraction_cache.items():
            entry: dict[str, object] = {"raw": raw}
            final = self._final_text_cache.get((page, lang, "()"))
            if final is not None:
                entry["final"] = [final[0], final[1], final[2]]
            pages.setdefault(str(page), {})[lang] = entry
        payload = {"fingerprint": self._doc_fingerprint, "pages": pages}
        try:
            self._extraction_cache_file.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def _wait_extraction_threads(self):
        """Wait for any in-flight extraction before the app closes."""
        for t in self._retired_extract_threads:
            if t.isRunning():
                t.wait(3000)
        self._retired_extract_threads.clear()
        if self._extract_thread is not None and self._extract_thread.isRunning():
            self._extract_thread.wait(5000)

    # ── image extraction (manual region, PyMuPDF) ───────────────────────

    def _extract_image_region(
        self, page_num: int, clip, embedded_only: bool = False
    ) -> str | None:
        """Extract the image in a PDF-points rect and save it (file:// URI)."""
        doc = self._get_mupdf_doc()
        if doc is None or not _has_pymupdf:
            return None
        result = _region_image(
            doc, page_num, clip, max(self._render_scale, 4.0),
            embedded_only=embedded_only,
        )
        if result is None:
            return None
        data, ext = result
        images_dir = self._get_images_dir()
        prefix = f"page_{page_num + 1:04d}_region_"
        # Unique name: append after the regions already captured for this page.
        index = 0
        for old in images_dir.glob(prefix + "*"):
            try:
                index = max(index, int(old.stem.rsplit("_", 1)[-1]) + 1)
            except ValueError:
                index += 1
        path = images_dir / f"{prefix}{index}.{ext}"
        try:
            path.write_bytes(data)
        except Exception:
            return None
        return path.resolve().as_uri()

    def _on_region_selected(self, x0: float, y0: float, x1: float, y1: float):
        """Extract the user-selected page region and show it in the gallery."""
        self._set_select_mode(False)
        scale = self._render_scale or 1.0
        clip = (x0 / scale, y0 / scale, x1 / scale, y1 / scale)
        uri = self._extract_image_region(self._current_page, clip)
        if uri is None:
            self.status_bar.showMessage(T("status.no_image"))
            return
        self._current_images.append(uri)  # new captures accumulate in the gallery
        self.text_panel.show_images(self._current_images)
        name = Path(QUrl(uri).toLocalFile()).name
        self.status_bar.showMessage(T("status.image_extracted", name=name))

    def _on_image_removed(self, uri: str):
        """Drop a captured image from the gallery and delete its file."""
        if uri in self._current_images:
            self._current_images.remove(uri)
        try:
            Path(QUrl(uri).toLocalFile()).unlink(missing_ok=True)
        except Exception:
            pass
        self.text_panel.show_images(self._current_images)

    def _on_select_region_toggled(self, checked: bool):
        """Enable/disable rubber-band selection on the left panel."""
        self._set_select_mode(checked)

    def _set_select_mode(self, enabled: bool):
        """Update the select-zone toggle (and turn off the other modes)."""
        self.btn_select_region.setChecked(enabled)
        self.pdf_view.set_select_mode(enabled)
        if enabled:
            self.btn_exclude.setChecked(False)
            self.pdf_view.set_exclude_mode(False)
            self.btn_include.setChecked(False)
            self.pdf_view.set_include_mode(False)

    def _on_exclude_toggled(self, checked: bool):
        """Enable/disable rubber-band zone exclusion."""
        self._set_exclude_mode(checked)

    def _set_exclude_mode(self, enabled: bool):
        """Update the exclude-zone toggle (and turn off the other modes)."""
        self.btn_exclude.setChecked(enabled)
        self.pdf_view.set_exclude_mode(enabled)
        if enabled:
            self.btn_select_region.setChecked(False)
            self.pdf_view.set_select_mode(False)
            self.btn_include.setChecked(False)
            self.pdf_view.set_include_mode(False)

    def _on_include_toggled(self, checked: bool):
        """Enable/disable rubber-band zone inclusion."""
        self._set_include_mode(checked)

    def _set_include_mode(self, enabled: bool):
        """Update the include-zone toggle (and turn off the other modes)."""
        self.btn_include.setChecked(enabled)
        self.pdf_view.set_include_mode(enabled)
        if enabled:
            self.btn_select_region.setChecked(False)
            self.pdf_view.set_select_mode(False)
            self.btn_exclude.setChecked(False)
            self.pdf_view.set_exclude_mode(False)

    def _on_region_excluded(self, x0: float, y0: float, x1: float, y1: float):
        """Store an excluded zone and re-extract the cleaned page.

        The exclude mode stays active so the user can draw as many zones as
        needed; click 🚫 Escludi zona again to leave the mode.

        If the drawn zone contains an embedded image, the same gesture also
        captures it into the 🖼️ Immagini gallery (embedded rasters only, no
        render fallback), so excluding a figure and keeping it are one action.
        """
        scale = self._render_scale or 1.0
        rect = (
            min(x0, x1) / scale, min(y0, y1) / scale,
            max(x0, x1) / scale, max(y0, y1) / scale,
        )
        if (rect[2] - rect[0]) < 1.0 or (rect[3] - rect[1]) < 1.0:
            return
        zones = self._excluded_zones.setdefault(self._current_page, [])
        zones.append(rect)
        self.pdf_view.show_excluded_zones(self._scene_exclusions(self._current_page))
        self.text_panel.invalidate_page(self._current_page)
        self._refresh_current_page_text()

        msg = T("status.zone_excluded", count=len(zones))
        uri = self._extract_image_region(self._current_page, rect, embedded_only=True)
        if uri is not None:
            self._current_images.append(uri)
            # Aggiungi la figura alla gallery ma riporta il focus alla
            # finestra "Originale": l'utente sta lavorando sull'esclusione
            # della zona, non sulla gallery.
            self.text_panel.show_images(self._current_images, activate=False)
            self.text_panel.show_original()
            name = Path(QUrl(uri).toLocalFile()).name
            msg = T("status.zone_excluded_image", name=name)
        self.status_bar.showMessage(msg)

    def _on_region_included(self, x0: float, y0: float, x1: float, y1: float):
        """Store a numbered inclusion zone and re-extract in reading order.

        The include mode stays active so the user can draw the whole reading
        sequence; click 🟩 Includi zona again to leave the mode. The zone
        number is its position in the drawn sequence (shown on the box).
        """
        scale = self._render_scale or 1.0
        rect = (
            min(x0, x1) / scale, min(y0, y1) / scale,
            max(x0, x1) / scale, max(y0, y1) / scale,
        )
        if (rect[2] - rect[0]) < 1.0 or (rect[3] - rect[1]) < 1.0:
            return
        zones = self._inclusion_zones.setdefault(self._current_page, [])
        zones.append(rect)
        self.pdf_view.show_inclusion_zones(self._scene_inclusions(self._current_page))
        self.text_panel.invalidate_page(self._current_page)
        self._refresh_current_page_text()
        self.status_bar.showMessage(T("status.zone_included", count=len(zones)))

    def _on_reset_zones(self):
        """Remove all zones (exclusions + inclusions) for the current page."""
        self._excluded_zones.pop(self._current_page, None)
        self._inclusion_zones.pop(self._current_page, None)
        self.pdf_view.show_excluded_zones([])
        self.pdf_view.show_inclusion_zones([])
        self.text_panel.invalidate_page(self._current_page)
        self._refresh_current_page_text()
        self.status_bar.showMessage(T("status.zones_reset"))

    def _scene_exclusions(self, page_num: int) -> list[tuple]:
        """Convert the page's excluded zones (PDF points) to scene pixels."""
        scale = self._render_scale or 1.0
        return [
            tuple(v * scale for v in r)
            for r in self._excluded_zones.get(page_num, [])
        ]

    def _scene_inclusions(self, page_num: int) -> list[tuple]:
        """Convert the page's inclusion zones (PDF points) to scene pixels."""
        scale = self._render_scale or 1.0
        return [
            tuple(v * scale for v in r)
            for r in self._inclusion_zones.get(page_num, [])
        ]

    def _refresh_current_page_text(self):
        """Re-display the current page's text (cache-aware re-extraction)."""
        if not self._pdf_path or self._mupdf_doc is None or self._page_count == 0:
            return
        self._request_extraction(self._current_page)

    def _get_images_dir(self) -> Path:
        """Return (creating on first use) the per-document figures directory."""
        if self._images_dir is None:
            self._images_dir = _app_data_base() / "images" / self._pdf_path.stem
        self._images_dir.mkdir(parents=True, exist_ok=True)
        return self._images_dir

    # ── navigation ────────────────────────────────────────────────────────

    def _set_page(self, page_num: int):
        if self._mupdf_doc is None or self._page_count == 0:
            return
        count = self._page_count
        page_num = max(0, min(page_num, count - 1))
        self._current_page = page_num
        self._remember_last_page(page_num)

        # Render left
        self._display_page(page_num)

        # Clone translation right (async: pdf2zh_next non blocca la UI)
        if self._pdf_path:
            self._request_translation(page_num)

        # Update toolbar
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(page_num + 1)
        self.page_spin.blockSignals(False)

        # Sync TOC highlight
        self.toc_panel.select_page(page_num)

    def _remember_last_page(self, page_num: int):
        """Track the current page per document; persist with a 2 s debounce."""
        if not self._resume_last_page or not self._pdf_path:
            return
        pages = dict(get_setting("last_pages", {}) or {})
        pages[self._pdf_path.name] = int(page_num)
        set_setting("last_pages", pages)
        self._last_page_timer.start()

    def _show_page_status(self, elapsed: float = 0.0):
        """Status-bar message for the current page (or the ready hint)."""
        if not self._pdf_path:
            self.status_bar.showMessage(T("status.ready"))
            return
        self.status_bar.showMessage(
            T(
                "status.page",
                page=self._current_page + 1,
                total=self._page_count,
                name=self._pdf_path.name,
                ms=f"{elapsed*1000:.0f}",
            )
        )

    def _next_page(self):
        self._set_page(self._current_page + 1)

    def _prev_page(self):
        self._set_page(self._current_page - 1)

    def _on_spin(self, val: int):
        self._set_page(val - 1)

    def _goto_toc_page(self, page_idx: int):
        """Navigate to a page selected from the TOC."""
        self._set_page(page_idx)

    # ── settings dialog ────────────────────────────────────────────────────

    def _on_open_settings(self):
        """Open the config dialog; apply on OK."""
        dlg = SettingsDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._apply_settings(dlg.values())

    def _on_open_help(self):
        """Open the online help site in the system browser."""
        QDesktopServices.openUrl(QUrl(HELP_URL))

    def _apply_settings(self, values: dict):
        """Apply the settings dialog choices (languages + preferences)."""
        ui_changed = values.get("lang") != self._ui_lang_applied
        src = values.get("src_lang", get_source_lang())
        dst = values.get("dst_lang", get_target_lang())
        engine = values.get("engine", get_translation_engine())
        langs_changed = src != get_source_lang() or dst != get_target_lang()
        engine_changed = engine != get_translation_engine()

        if values.get("lang") in LANGUAGES:
            set_language(values["lang"])
        for key in ("zoom", "font_size", "render_md", "show_header",
                    "resume_last_page", "remember_tab", "save_edits",
                    "pdf2zh_bin"):
            if key in values:
                set_setting(key, values[key])
        set_source_lang(src)   # setters validati (auto solo in sorgente)
        set_target_lang(dst)
        set_translation_engine(engine)
        save_config()

        # Applicazione immediata delle preferenze
        zoom = float(values.get("zoom", self._base_render_scale))
        if abs(zoom - self._base_render_scale) > 1e-9:
            self._base_render_scale = zoom
            self._update_zoom()
        self._render_md = bool(values.get("render_md", self._render_md))
        self._show_header = bool(values.get("show_header", self._show_header))
        self._resume_last_page = bool(
            values.get("resume_last_page", self._resume_last_page)
        )
        self._remember_tab = bool(values.get("remember_tab", self._remember_tab))
        self.btn_md_toggle.setChecked(self._render_md)
        self.btn_md_toggle.setText(
            T("toolbar.md.on") if self._render_md else T("toolbar.md.plain")
        )
        self.text_panel.set_font_size(int(values.get("font_size", 12)))
        self.text_panel.set_translation_languages(src, dst, engine)
        self.text_panel.set_save_edits(bool(values.get("save_edits", True)))

        # Pannello clone: allinea lingua/engine e rigenera se sono cambiati.
        self._configure_clone_engine()
        self.translated_panel.set_target_language(dst)
        self.translated_panel.set_engine(engine)
        if langs_changed or engine_changed:
            self._clone_generation += 1
            self._retire_clone_thread()
            if self._pdf_path:
                self._request_translation(self._current_page)

        if ui_changed:
            self._retranslate_all()
        else:
            self._display_last_result()  # header on/off + nuovo font
        if (langs_changed or engine_changed) and self.text_panel._btn_translated.isChecked():
            self.text_panel._maybe_show_translation()

    def _show_toast(self, message: str, ms: int):
        """Show a transient message in the status bar (the app's 'toast')."""
        self.status_bar.showMessage(message, ms)

    def clear_saved_edits(self):
        """Wipe the current document's saved edits and re-show fresh text."""
        if self._mupdf_doc is None:
            return
        self.text_panel.clear_saved_edits()
        self._display_last_result()
        self.status_bar.showMessage(T("settings.edits.clear_done"), 3000)

    def _retranslate_all(self):
        """Re-apply every UI string after a language switch."""
        self.retranslate()
        self.text_panel.retranslate()
        self.translated_panel.retranslate()
        if self._mupdf_doc is not None:
            # Il TOC riporta "(senza titolo)" e il suffisso "p." per pagina:
            # ricostruirlo è economico e lo allinea alla lingua attiva.
            self.toc_panel.build_toc(self._mupdf_doc)
        self._display_last_result()
        self._show_page_status(self._last_elapsed)
        self._ui_lang_applied = get_language()

    def retranslate(self):
        """Re-apply the MainWindow's own chrome strings."""
        self.btn_settings.setText(T("settings.button"))
        self.btn_settings.setToolTip(T("settings.button.tip"))
        self.btn_help.setText(T("toolbar.help"))
        self.btn_help.setToolTip(T("toolbar.help.tip"))
        self.btn_open.setText(T("toolbar.open"))
        self.btn_export.setText(T("toolbar.export"))
        self.btn_export.setToolTip(T("toolbar.export.tip"))
        self.btn_toc.setText(T("toolbar.toc"))
        self.btn_toc.setToolTip(T("toolbar.toc.tip"))
        self.btn_prev.setText(T("toolbar.prev"))
        self.btn_next.setText(T("toolbar.next"))
        self.lbl_of.setText(T("toolbar.of"))
        self.btn_zoom_out.setToolTip(T("toolbar.zoom_out.tip"))
        self.btn_zoom_in.setToolTip(T("toolbar.zoom_in.tip"))
        self.zoom_label.setText(
            T("toolbar.zoom.scale", x=f"{self._view_zoom:.2f}")
        )
        self.btn_md_toggle.setText(
            T("toolbar.md.on") if self._render_md else T("toolbar.md.plain")
        )
        self.btn_md_toggle.setToolTip(T("toolbar.md.tip"))
        self.btn_select_region.setText(T("page_toolbar.select"))
        self.btn_select_region.setToolTip(T("page_toolbar.select.tip"))
        self.btn_exclude.setText(T("page_toolbar.exclude"))
        self.btn_exclude.setToolTip(T("page_toolbar.exclude.tip"))
        self.btn_include.setText(T("page_toolbar.include"))
        self.btn_include.setToolTip(T("page_toolbar.include.tip"))
        self.btn_reset_zones.setText(T("page_toolbar.reset"))
        self.btn_reset_zones.setToolTip(T("page_toolbar.reset.tip"))
        self.toc_dock.setWindowTitle(T("dock.toc"))
        self.pdf_view.retranslate()

    def _extraction_header(self, text: str, elapsed: float, label: str = "auto") -> str:
        """Build the header line shown above the extracted text.

        ``label`` is a key suffix ("auto"/"manual"): it is resolved through
        T() at display time, so a language switch re-renders it correctly.
        ``ocr`` is the OCR language derived from the source-language setting
        ("🌐 Auto" when detection is automatic) and ``engine`` is the active
        translation engine: both are resolved at display time too, so they
        stay in sync with settings/language changes.
        """
        src = get_source_lang()
        return T(
            "header.line",
            ms=f"{elapsed*1000:.1f}",
            chars=len(text),
            label=T(f"engine.label.{label}"),
            ocr=flag_endonym(src or "auto"),
            engine=T(f"engine.option.{get_translation_engine()}"),
        )

    # ── zoom ───────────────────────────────────────────────────────────────

    def _zoom_in(self):
        self._view_zoom = min(8.0, self._view_zoom * 1.25)
        self._update_zoom()

    def _zoom_out(self):
        self._view_zoom = max(0.25, self._view_zoom / 1.25)
        self._update_zoom()

    def _zoom_reset(self):
        self._view_zoom = 1.0  # 1.0 = adatta alla finestra
        self._update_zoom()

    def _update_zoom(self):
        """Apply the visible zoom: bump render resolution for sharpness,
        push the zoom to the view, and re-render the current page."""
        # Render at enough resolution so text stays crisp when zoomed in,
        # while keeping the persisted base as the "fit" quality level.
        self._render_scale = min(8.0, max(0.5, self._base_render_scale * self._view_zoom))
        self.zoom_label.setText(
            T("toolbar.zoom.scale", x=f"{self._view_zoom:.2f}")
        )
        set_setting("zoom", self._base_render_scale)
        save_config()
        if self._mupdf_doc is not None and self._page_count > 0:
            self.pdf_view.set_view_zoom(self._view_zoom)
            self.translated_panel.set_view_zoom(self._view_zoom)
            self._display_page(self._current_page)
            self._display_translated_page(self._current_page)

    # ── rendering engine ──────────────────────────────────────────────────

    def _get_mupdf_doc(self):
        """Open (lazily) the pymupdf document for rendering."""
        if self._mupdf_doc is None and self._pdf_path and _has_pymupdf:
            try:
                self._mupdf_doc = pymupdf.open(str(self._pdf_path))
            except Exception:
                self._mupdf_doc = None
        return self._mupdf_doc

    def _render_pymupdf(self, page_num: int) -> QPixmap | None:
        doc = self._get_mupdf_doc()
        if doc is None:
            return None
        try:
            page = doc[page_num]
            pix = page.get_pixmap(
                matrix=pymupdf.Matrix(self._render_scale, self._render_scale)
            )
            img = QImage(
                pix.samples, pix.width, pix.height, pix.stride,
                QImage.Format.Format_RGB888,
            )
            return QPixmap.fromImage(img)
        except Exception:
            return None

    def _render_page(self, page_num: int) -> QPixmap | None:
        """Render a page with PyMuPDF (single rendering engine)."""
        return self._render_pymupdf(page_num)

    def _display_page(self, page_num: int):
        """Render + show a page, with an informative fallback message."""
        pix = self._render_page(page_num)
        if pix is not None:
            self.pdf_view.show_page(pix)
            self.pdf_view.show_excluded_zones(self._scene_exclusions(page_num))
            self.pdf_view.show_inclusion_zones(self._scene_inclusions(page_num))
            return
        if not _has_pymupdf:
            self.pdf_view.show_message(T("view.no_pymupdf"))
        else:
            self.pdf_view.show_message(T("view.page_unavailable"))

    # ── clone translation (right panel) ───────────────────────────────────

    def _clone_langs(self) -> tuple[str, str]:
        return get_source_lang(), get_target_lang()

    def _configure_clone_engine(self):
        """Push languages + pdf2zh override from settings into the engine."""
        src, dst = self._clone_langs()
        self._clone_engine.lang_in = src
        self._clone_engine.lang_out = dst
        self._clone_engine.set_pdf2zh_bin(get_setting("pdf2zh_bin", "") or None)

    def _engine_display(self, engine: str) -> str:
        return T(f"engine.option.{engine}")

    def _request_translation(self, page_num: int):
        """Show the cached clone of ``page_num`` or start a background job."""
        if not self._pdf_path:
            return
        self._configure_clone_engine()
        engine = get_translation_engine()

        if self._clone_engine.is_cached(page_num, engine):
            self._display_translated_page(page_num)
            self.status_bar.showMessage(
                T("clone.status_cached", page=page_num + 1)
            )
            return

        if not self._clone_engine.available():
            self.translated_panel.hide_spinner()
            self.translated_panel.show_message(T("clone.no_engine"))
            self.translated_panel.set_status(T("clone.no_engine_short"))
            self.status_bar.showMessage(T("clone.no_engine"))
            return

        self._clone_generation += 1
        self._clone_run_start[engine] = time.time()
        self._retire_clone_thread()
        self.translated_panel.show_message(T("clone.page_pending"))
        self.translated_panel.set_status(T("clone.status_running"))
        self.translated_panel.show_spinner(
            T("clone.spinner", engine=self._engine_display(engine))
        )
        self.status_bar.showMessage(
            T(
                "clone.translating",
                page=page_num + 1,
                engine=self._engine_display(engine),
            )
        )

        thread = CloneTranslateThread(
            self._clone_engine, page_num, engine, self._clone_generation
        )
        thread.done.connect(self._on_clone_done)
        thread.error.connect(self._on_clone_error)
        self._clone_thread = thread
        thread.start()

    def _retire_clone_thread(self):
        """Detach the current clone thread; keep it alive until it finishes."""
        old = self._clone_thread
        self._clone_thread = None
        if old is None:
            return
        for sig in (old.done, old.error):
            try:
                sig.disconnect()
            except TypeError:
                pass
        if old.isRunning():
            self._retired_clone_threads.append(old)
            old.finished.connect(self._forget_retired_clone_thread)

    def _forget_retired_clone_thread(self):
        t = self.sender()
        if t in self._retired_clone_threads:
            self._retired_clone_threads.remove(t)

    def _on_clone_done(self, generation: int, page_num: int, engine: str, path: str):
        if generation != self._clone_generation:
            return
        if page_num != self._current_page or engine != get_translation_engine():
            return
        self._display_translated_page(page_num)
        self.translated_panel.set_status(T("clone.status_done"))
        self.status_bar.showMessage(T("clone.done", page=page_num + 1))

    def _on_clone_error(self, generation: int, page_num: int, engine: str, message: str):
        if generation != self._clone_generation:
            return
        self.translated_panel.hide_spinner()
        self.translated_panel.set_status(T("clone.status_error"))
        if page_num != self._current_page:
            return
        self.translated_panel.show_message(T("clone.failed", reason=message))
        self.status_bar.showMessage(T("clone.failed", reason=message))

    def _render_translated_pdf(self, page_num: int, engine: str) -> QPixmap | None:
        path = self._clone_engine.translated_path(page_num, engine)
        if not path.is_file() or not _has_pymupdf:
            return None
        try:
            with pymupdf.open(str(path)) as doc:
                if len(doc) == 0:
                    return None
                pix = doc[0].get_pixmap(
                    matrix=pymupdf.Matrix(self._render_scale, self._render_scale)
                )
                img = QImage(
                    pix.samples, pix.width, pix.height, pix.stride,
                    QImage.Format.Format_RGB888,
                )
                return QPixmap.fromImage(img)
        except Exception:
            return None

    def _display_translated_page(self, page_num: int):
        """Render the cached clone of ``page_num`` on the right panel."""
        engine = get_translation_engine()
        if not self._clone_engine.is_cached(page_num, engine):
            return
        pix = self._render_translated_pdf(page_num, engine)
        if pix is None:
            self.translated_panel.show_message(T("clone.render_error"))
            return
        self.translated_panel.show_page(pix)

    # ── export della pagina tradotta ──────────────────────────────────────

    def _on_export_translated(self):
        """Export current translated page, or a range, as a single PDF.

        The export dialog also lets the user pick the engine and the output
        language for this export only. In range mode the user may ask to
        translate the missing pages first: the app holds the user with a
        modal progress dialog until the queue finishes (or is cancelled),
        then merges the cached pages and shows a completion state with the
        saved file path.
        """
        if not self._pdf_path or self._mupdf_doc is None or self._page_count == 0:
            self.status_bar.showMessage(T("export.need_doc"), 4000)
            return
        self._configure_clone_engine()
        source = get_source_lang()

        dlg = ExportDialog(
            self._clone_engine,
            get_translation_engine(),
            get_target_lang(),
            source,
            self._current_page,
            self._page_count,
            self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        engine = dlg.chosen_engine()
        target = dlg.chosen_target()
        pages = dlg.chosen_pages()
        want_translate = dlg.translate_missing()
        as_zip = dlg.chosen_zip()
        lo, hi = min(pages), max(pages)
        cached = self._clone_engine.cached_pages(lo, hi, engine, source, target)
        missing = [
            p
            for p in pages
            if not self._clone_engine.is_cached(p, engine, source, target)
        ]

        if not cached and not want_translate:
            self.status_bar.showMessage(
                T("export.not_ready") if dlg.is_current() else T("export.none_ready"),
                5000,
            )
            return
        if want_translate and missing and not self._clone_engine.available():
            self.status_bar.showMessage(T("clone.no_engine"), 6000)
            return

        # Destinazione scelta PRIMA della traduzione: l'attesa termina con il
        # file già scritto, senza ulteriori interruzioni.
        ext = ".zip" if as_zip else ".pdf"
        default = (
            f"{self._pdf_path.stem}_pag{dlg.range_label()}_{engine}_{target}{ext}"
        )
        dest, _ = QFileDialog.getSaveFileName(
            self,
            T("export.dialog_zip") if as_zip else T("export.dialog"),
            default,
            T("export.filter_zip") if as_zip else T("export.filter"),
        )
        if not dest:
            return
        if not dest.lower().endswith(ext):
            dest += ext

        # Engine dedicato con motore/lingua scelti: non disturba l'engine
        # condiviso col pannello destro (che può avere un thread in corso).
        translate = bool(want_translate and missing)
        export_engine = self._clone_engine
        if translate:
            export_engine = clone_engine.CloneEngine(
                cache_root=self._clone_engine.cache_root,
                pdf2zh_bin=get_setting("pdf2zh_bin", "") or None,
            )
            export_engine.set_document(self._pdf_path)
            export_engine.lang_in = source
            export_engine.lang_out = target

        ok, count, failed = self._run_export_with_progress(
            export_engine,
            missing,
            translate,
            engine,
            source,
            target,
            lo,
            hi,
            len(cached),
            dest,
            as_zip,
        )
        if not ok:
            return
        self.status_bar.showMessage(
            T("export.done", count=count, failed=failed), 6000
        )

    def _run_export_with_progress(
        self,
        engine,
        missing: list[int],
        translate: bool,
        engine_name: str,
        source: str,
        target: str,
        page_from: int,
        page_to: int,
        cached_count: int,
        dest: str,
        as_zip: bool = False,
    ) -> tuple[bool, int, int]:
        """Translate (optionally), merge and save, with a modal progress dialog.

        The dialog is application-modal so the user cannot navigate away while
        the job runs; its event loop keeps the UI painting (activity line,
        busy/determinate bar, counts, elapsed/ETA, per-page log) without
        blocking the GUI thread. On success it stays open in a "completed"
        state showing the saved path until the user closes it. Cancel is
        cooperative and stops after the page in flight.

        Returns ``(ok, pages_written, pages_failed)``.
        """
        total = len(missing)
        prog = ExportProgressDialog(
            engine_label=T(f"engine.option.{engine_name}"),
            lang_label=flag_endonym(target),
            page_from=page_from + 1,
            page_to=page_to + 1,
            missing=total,
            cached=cached_count,
            total=max(total, 1),
            parent=self,
        )
        prog.setWindowModality(Qt.WindowModality.ApplicationModal)
        prog.show()

        state = {"cancelled": False, "failed": {}}
        if translate and missing:
            thread = CloneExportThread(engine, missing, engine_name)

            def _on_page_error(page: int, reason: str):
                state["failed"][page] = reason

            def _on_progress(done: int, tot: int, page: int):
                if page in state["failed"]:
                    prog.log_fail(page, state["failed"][page])
                else:
                    prog.log_ok(page)
                prog.set_stats(done, len(state["failed"]), tot)
                next_page = missing[done] if done < tot else None
                if next_page is not None:
                    prog.set_translating(next_page + 1)
                else:
                    prog.set_phase_exporting()

            def _on_cancel():
                # Lo slot è invocato anche alla chiusura del dialogo: ignoralo
                # se la coda è già terminata, altrimenti un export riuscito
                # verrebbe marcato come annullato.
                if not thread.isRunning():
                    return
                state["cancelled"] = True
                thread.cancel()
                prog.set_cancelling()

            thread.page_error.connect(_on_page_error)
            thread.progress.connect(_on_progress)
            prog.cancelled.connect(_on_cancel)

            prog.begin(missing[0] + 1)
            loop = QEventLoop()
            thread.batch_finished.connect(lambda *_: loop.quit())
            thread.start()
            loop.exec()
            thread.wait(2000)

            if state["cancelled"] or thread.is_cancelled():
                prog.set_error(T("export.cancelled"))
                prog.exec()
                self.status_bar.showMessage(T("export.cancelled"), 5000)
                return (False, 0, 0)
        else:
            prog.set_phase_exporting()

        ready = engine.cached_pages(page_from, page_to, engine_name, source, target)
        if not ready:
            prog.set_error(T("export.none_ready"))
            prog.exec()
            self.status_bar.showMessage(T("export.none_ready"), 5000)
            return (False, 0, 0)
        try:
            if as_zip:
                count = engine.export_zip(
                    ready,
                    engine_name,
                    dest,
                    source,
                    target,
                    stem=self._pdf_path.stem if self._pdf_path else None,
                )
            else:
                count = engine.export_pdf(ready, engine_name, dest, source, target)
        except Exception:  # noqa: BLE001 — riportato nella finestra
            log.exception("export PDF fallito: %s", dest)
            prog.set_error(T("export.error"))
            prog.exec()
            self.status_bar.showMessage(T("export.error"), 5000)
            return (False, 0, 0)
        failed = sum(
            1
            for p in missing
            if not engine.is_cached(p, engine_name, source, target)
        )
        prog.set_completed(str(dest), count, failed)
        prog.exec()  # resta aperta finché l'utente non preme "Chiudi"
        return (True, count, failed)

    def _wait_clone_threads(self):
        """Cancel and wait for in-flight clone translations before closing."""
        for t in list(self._retired_clone_threads):
            t.cancel()
        if self._clone_thread is not None:
            self._clone_thread.cancel()
        for t in self._retired_clone_threads:
            if t.isRunning():
                t.wait(3000)
        self._retired_clone_threads.clear()
        if self._clone_thread is not None and self._clone_thread.isRunning():
            self._clone_thread.wait(5000)

    def _on_engine_selected(self, engine: str):
        """Quick engine switch from the right-panel radios."""
        if engine not in CLONE_ENGINES or engine == get_translation_engine():
            return
        set_translation_engine(engine)
        save_config()
        self._clone_generation += 1
        self._retire_clone_thread()
        self.translated_panel.set_target_language(get_target_lang())
        if self._pdf_path:
            self._request_translation(self._current_page)

    # ── file open ─────────────────────────────────────────────────────────

    def _on_open(self):
        path_str, _ = QFileDialog.getOpenFileName(
            self, T("dlg.open"), "", T("dlg.open_filter")
        )
        if path_str:
            self._open_pdf(Path(path_str))

    def _open_pdf(self, path: Path):
        if not path.exists():
            QMessageBox.warning(self, T("dlg.error"), T("dlg.file_not_found", path=path))
            return

        if self._mupdf_doc is not None:
            self._mupdf_doc.close()
            self._mupdf_doc = None

        try:
            self._mupdf_doc = pymupdf.open(str(path))
            self._pdf_path = path
            self._page_count = len(self._mupdf_doc)

            self._images_dir = None
            self._current_images = []
            self._excluded_zones = {}
            self._inclusion_zones = {}
            self.text_panel.set_document(path)
            self._set_extraction_cache(path)

            # Clone engine: nuovo documento sorgente + pannello destro pulito.
            self._clone_generation += 1
            self._retire_clone_thread()
            self._clone_engine.set_document(path)
            self._configure_clone_engine()
            self.translated_panel.set_target_language(get_target_lang())
            self.translated_panel.set_engine(get_translation_engine())
            self.translated_panel.show_message(T("clone.page_pending"))
            self.translated_panel.set_status("")

            self.page_spin.setEnabled(True)
            self.page_spin.setMaximum(max(self._page_count, 1))
            self.lbl_total.setText(str(self._page_count))

            # Build the multi-level table of contents
            self.toc_panel.build_toc(self._mupdf_doc)

            if self._page_count > 0:
                start = 0
                if self._resume_last_page:
                    try:
                        start = int(
                            (get_setting("last_pages", {}) or {}).get(path.name, 0) or 0
                        )
                    except (TypeError, ValueError):
                        start = 0
                self._set_page(start)
            else:
                self.pdf_view.show_page(None)
                self._display_text(T("view.empty_pdf"))
                self.status_bar.showMessage(T("status.empty_pdf"))
        except Exception as e:
            QMessageBox.critical(self, T("dlg.pdf_error"), T("dlg.cannot_open", e=e))
            self._mupdf_doc = None
            self._pdf_path = None
            self._page_count = 0
            self._clone_engine.set_document(None)

    def closeEvent(self, event):
        if self._mupdf_doc is not None:
            self._mupdf_doc.close()
            self._mupdf_doc = None
        self._last_page_timer.stop()
        self._wait_clone_threads()
        self._wait_extraction_threads()
        self._save_extraction_cache()
        save_config()  # flush ultima pagina / ultima tab
        self.text_panel.shutdown()
        super().closeEvent(event)


# ═══════════════════════════════════════════════════════════════════════════════
#  entry point
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    # Nei build congelati (PyInstaller) punta l'OCR di PyMuPDF al Tesseract
    # incluso nel bundle (binario + librerie + tessdata) invece di richiederlo
    # come installazione di sistema.
    _setup_bundled_tesseract()

    app = QApplication(sys.argv)
    app.setApplicationName("noesis-pdf-cloner")

    # Config v2: al primo avvio vengono scritti i default (lingua UI = lingua
    # dell'OS o italiano); le scelte persistono tra gli aggiornamenti (la
    # cartella dati è ancorata al nome app, non alla versione).
    config_path = _config_file_path()
    cfg = init_config(config_path, defaults={**DEFAULTS, "lang": _detect_os_lang()})
    set_language(cfg["lang"])

    window = MainWindow()
    window.show()

    # Apri un PDF passato da riga di comando (percorso assoluto o relativo);
    # in mancanza, fallback su harrison2025.pdf nella directory corrente.
    if len(sys.argv) > 1:
        pdf = Path(sys.argv[1])
    else:
        default = Path("harrison2025.pdf")
        pdf = default if default.exists() else None
    if pdf is not None:
        QTimer.singleShot(100, lambda: window._open_pdf(pdf))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
