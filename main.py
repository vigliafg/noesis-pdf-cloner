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
import contextlib
import hashlib
import html as _html
import json
import logging
import logging.handlers
import math
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

# Proxy provider locale (avvio automatico + prova provider).
import proxy_manager

# Archivio per-utente della chiave OpenRouter (file, cross-platform).
import keystore

# Temi UI (chiaro/scuro/sistema) e avvisi di fine lavoro.
import theme

# Suono d'avviso nativo (nessuna dipendenza pesante) e gestione standby.
import notifications
import power

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
import pages as page_spec

_MD_EXTENSIONS = ["tables", "fenced_code", "codehilite"]

# Logger del modulo (la configurazione su file avviene in ``_setup_logging``).
log = logging.getLogger("main")

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
    Qt, QThread, QTimer, QEvent, pyqtSignal, pyqtProperty, QUrl,
    QStandardPaths, QRectF, QRect, QLocale, QEventLoop, QPoint,
    QPropertyAnimation, QEasingCurve,
)
from PyQt6.QtGui import (
    QImage, QPixmap, QFont, QKeySequence, QShortcut, QIcon,
    QPen, QBrush, QColor, QPainter, QPainterPath, QDesktopServices,
    QLinearGradient,
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
    QToolButton,
    QMenu,
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
    QGraphicsDropShadowEffect,
    QSystemTrayIcon,
)

# URL base della guida online (sito statico pubblicato su GitHub Pages).
# La landing del progetto è alla radice del sito; la guida è in /help/<lingua>/.
# Aprire con QDesktopServices.openUrl apre il browser di sistema: nessuna
# dipendenza QtWebEngine, nessuna pagina incorporata.
HELP_URL = (
    "https://vigliafg.github.io/noesis-pdf-cloner/help/"
)


def help_url(lang: str | None = None) -> str:
    """URL della guida nella lingua dell'interfaccia (fallback: italiano).

    ``lang`` esplicito è utile nei test; se assente si usa la lingua attiva.
    """
    code = lang or get_language()
    if code not in LANGUAGES:
        code = "it"
    return f"{HELP_URL}{code}/"

# Stima indicativa del tempo di traduzione per pagina (il desktop non ha un
# backend di stima): usata dal box del passo "Motore" e dall'overlay liquido.
_EXPORT_EST_MS_PER_PAGE = 45_000
# Anteprime grandi dello step "Pagine": altezza fissa, larghezza calcolata
# dall'aspetto reale della pagina (così l'immagine resta intera).
_PREVIEW_H = 300

# ═══════════════════════════════════════════════════════════════════════════════
#  helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _status_style(kind: str) -> str:
    """QSS per una label di stato: ``kind`` è un token colore del tema."""
    return "color: %s; font-size: 12px;" % theme.color(kind)


def _resolve_theme(mode: str) -> str:
    """Risolve ``system`` nel tema effettivo del sistema operativo."""
    mode = theme.normalize_mode(mode)
    if mode != "system":
        return mode
    try:
        scheme = QApplication.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Light:
            return "light"
        if scheme == Qt.ColorScheme.Dark:
            return "dark"
    except Exception:  # noqa: BLE001 — in dubbio resta scuro
        pass
    return "dark"


def apply_theme_mode(mode: str) -> None:
    """Imposta il tema attivo (risolvendo ``system``) nel modulo ``theme``."""
    theme.set_mode(mode)
    theme.set_resolved(_resolve_theme(mode))


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


def _keystore_path() -> Path:
    """Per-user secrets file (OpenRouter key), 0600 where supported."""
    return _app_data_base() / "secrets.json"


# Da dove proviene la chiave OpenRouter attiva all'avvio:
# "file" (salvata in Impostazioni) | "env" (variabile di sistema) | "none".
_API_KEY_SOURCE = "none"
# Valore della variabile di sistema all'avvio: fallback se si rimuove il file.
_API_KEY_ENV_AT_START = ""
# Variabile presente nel registro di sistema ma NON ereditata da questo processo
# (tipico su Windows quando la si crea con l'app/sessione già avviata).
_API_KEY_ENV_STALE = False

# Codici d'errore del motore → chiave i18n con il consiglio per l'utente.
_ENGINE_ERROR_KEYS = {
    "missing_key": "clone.no_key",
    "invalid_key": "clone.invalid_key",
    "forbidden": "clone.err.forbidden",
    "no_credits": "clone.err.no_credits",
    "rate_limited": "clone.err.rate_limited",
    "model_not_found": "clone.err.model_not_found",
    "network": "clone.err.network",
}


def _friendly_reason(message: str) -> str:
    """Traduce i codici d'errore del motore in messaggi utente con consiglio."""
    key = _ENGINE_ERROR_KEYS.get(message)
    if key is not None:
        return T(key)
    return T("clone.failed", reason=message)


# Errori ritentabili: dopo un risveglio da standby la rete può essere appena
# tornata, o il servizio può aver risposto 429/timeout. Non sono colpa della
# pagina, quindi vale la pena ritentarla (punto 4).
_TRANSIENT_ERROR_MARKERS = (
    "network", "timeout", "rate_limited", "rate limit", "connection",
    "temporarily", "503", "502", "504",
)


def _is_transient_error(message: str) -> bool:
    low = (message or "").lower()
    return any(marker in low for marker in _TRANSIENT_ERROR_MARKERS)


def _llm_key_source_text() -> str:
    """Descrive quale chiave OpenRouter è attiva (per Impostazioni)."""
    file_key = keystore.KeyStore(_keystore_path()).get()
    if file_key:
        base = T("apikey.source.file")
        if _API_KEY_ENV_AT_START:
            base += " " + T("apikey.source.env_shadowed")
        return base
    if _API_KEY_ENV_AT_START:
        return T("apikey.source.env")
    if _API_KEY_ENV_STALE:
        return T("apikey.source.env_stale")
    return T("apikey.source.none")


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
        self.setBackgroundBrush(QColor(theme.color("bg")))
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

    def apply_theme(self):
        """Sfondo della vista pagina coerente col tema attivo."""
        self.setBackgroundBrush(QColor(theme.color("bg")))

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


class ProviderTestThread(QThread):
    """Prova il provider LLM in background (una piccola richiesta)."""

    result = pyqtSignal(dict)

    def __init__(self, base_url: str, model: str, api_key: str, parent=None):
        super().__init__(parent)
        self._base_url = base_url
        self._model = model
        self._api_key = api_key

    def run(self):
        try:
            outcome = proxy_manager.probe(
                self._base_url, self._model, self._api_key
            )
        except Exception as exc:  # noqa: BLE001 — riportato in UI
            outcome = {"ok": False, "error": str(exc)}
        self.result.emit(outcome)


class CloneTranslateThread(QThread):
    """Background thread: one page of clone translation via pdf2zh_next.

    ``pdf2zh_next`` runs as a subprocess and can take tens of seconds per
    page, so it never touches the GUI thread. Emits the resulting PDF path on
    success (``done``) or the engine status string on failure (``error``).
    """

    done = pyqtSignal(int, int, str, str)   # generation, page, engine, pdf path
    error = pyqtSignal(int, int, str, str)  # generation, page, engine, message
    cancelled = pyqtSignal(int, int, str)   # generation, page, engine

    # Attesa di un worker concorrente sulla stessa pagina (es. la traduzione
    # rimasta attiva dopo un cambio pagina): ~10 minuti a passi di 0,5 s.
    _WAIT_SLICE = 0.5
    _WAIT_SLICES = 1200

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

    def page(self) -> int:
        """Indice 0-based della pagina in traduzione."""
        return self._page

    def engine_name(self) -> str:
        """Motore della traduzione in corso."""
        return self._engine_name

    def _translate(self) -> Path | None:
        """Traduce la pagina; se un altro worker la sta già facendo, attende.

        Su un cambio pagina rapido la traduzione precedente resta viva in
        background (``retire``): la nuova richiesta sulla **stessa** pagina
        riceve ``None`` con stato ``running``. Non è un errore — si attende che
        il worker finisca e la cache compaia (come fa l'export).
        """
        path = self._engine.translate_page(
            self._page, self._engine_name, self._cancel_event
        )
        for _ in range(self._WAIT_SLICES):
            if path is not None or self._cancel_event.is_set():
                return path
            if self._engine.status(self._page, self._engine_name) != "running":
                return path
            time.sleep(self._WAIT_SLICE)
            path = self._engine.translate_page(
                self._page, self._engine_name, self._cancel_event
            )
        return path

    def run(self):
        try:
            path = self._translate()
        except Exception as exc:  # noqa: BLE001 — riportato in UI
            self.error.emit(
                self._generation, self._page, self._engine_name, str(exc)
            )
            return
        if path is None:
            status = self._engine.status(self._page, self._engine_name)
            if status == "cancelled":
                self.cancelled.emit(
                    self._generation, self._page, self._engine_name
                )
                return
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

    Le pagine (0-based) passano da ``CloneEngine.translate_page`` in modo
    **sequenziale**, una alla volta (cache-aware, serializzato per pagina) e il
    progresso viene emesso dopo ogni pagina. ``cancel`` interrompe subito il
    ``pdf2zh_next`` in corso (process group).
    """

    progress = pyqtSignal(int, int, int)        # done, total, page
    page_started = pyqtSignal(int)              # page (0-based)
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
        compaia invece di segnarla come errore. Gli errori **transitori**
        (rete, timeout, rate limit — tipici dopo un risveglio da standby) sono
        ritentati un paio di volte.
        """
        last = ""
        for attempt in range(3):
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
                    last = status.split("error:", 1)[-1]
                    break
                return None, status
            else:
                return None, "timeout"
            if self._cancelled or not _is_transient_error(last):
                return None, last
            time.sleep(1.0)
        return None, last

    def run(self):
        done = failed = 0
        total = len(self._pages)
        for page in self._pages:
            if self._cancelled:
                break
            # Annuncia l'avvio della pagina: la UI mostra anteprima e barra.
            self.page_started.emit(page)
            path, message = self._translate_one(page)
            if path is not None:
                done += 1
            elif not self._cancelled:
                failed += 1
                self.page_error.emit(page, message)
            self.progress.emit(done + failed, total, page)
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


class LiquidOverlay(QWidget):
    """Overlay con riempimento "liquido" (stile servizio) per un pixmap.

    Mostra la pagina (sbiadita) e un gradiente blu che sale dal basso. Il
    progresso è **stimato** (nessun progresso reale dal subprocess
    ``pdf2zh_next``): sale asintoticamente verso ~90% in una durata tipica e
    chiude a 100% al completamento. Usato sia dal pannello destro (traduzione
    on-demand) sia dalla finestra di progresso export (pagina in lavorazione).
    """

    cancel_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._caption = ""
        self._progress = 0.0
        self._target = 90.0
        self._duration = _EXPORT_EST_MS_PER_PAGE
        self._elapsed = 0
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._cancelable = False
        self._btn_cancel = QPushButton(T("settings.cancel"), self)
        self._btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_cancel.clicked.connect(self._on_cancel_clicked)
        self._btn_cancel.hide()
        self.apply_theme()
        self.hide()

    def apply_theme(self):
        """Applica i colori del tema attivo all'overlay e al pulsante Annulla."""
        q = theme.color
        self._btn_cancel.setStyleSheet(
            "QPushButton { background: %s; color: %s;"
            " border: 1px solid %s; border-radius: 8px;"
            " padding: 7px 18px; font-size: 13px; }"
            "QPushButton:hover { background: %s; }"
            "QPushButton:disabled { color: %s; border-color: %s; }"
            % (
                q("wiz_panel"), q("wiz_title"), q("wiz_accent"),
                q("wiz_sel_bg"), q("wiz_muted"), q("wiz_line"),
            )
        )
        self.update()

    # ── ciclo di vita ────────────────────────────────────────────────
    def start(self, pixmap: QPixmap | None, caption: str,
              duration_ms: int = _EXPORT_EST_MS_PER_PAGE,
              cancelable: bool = False):
        self._pixmap = pixmap
        self._caption = caption or ""
        self._progress = 0.0
        self._target = 90.0
        self._elapsed = 0
        self._duration = max(1000, int(duration_ms))
        self._cancelable = bool(cancelable)
        self._btn_cancel.setVisible(self._cancelable)
        self._btn_cancel.setEnabled(True)
        self.show()
        self.raise_()
        self._position_cancel()
        self.update()
        self._timer.start()

    def _on_cancel_clicked(self):
        self._btn_cancel.setEnabled(False)
        self._caption = T("export.progress.cancelling")
        self.update()
        self.cancel_requested.emit()

    def set_target(self, target: float):
        self._target = max(0.0, min(100.0, float(target)))

    def finish(self, ok: bool = True):
        self._timer.stop()
        self._progress = 100.0 if ok else 0.0
        self._btn_cancel.hide()
        self.update()
        QTimer.singleShot(200, self._hide_when_idle)

    def stop(self):
        self._timer.stop()
        self._pixmap = None
        self._btn_cancel.hide()
        self.hide()

    def progress(self) -> float:
        return self._progress

    def _position_cancel(self):
        if not self._btn_cancel.isVisible():
            return
        self._btn_cancel.adjustSize()
        x = (self.width() - self._btn_cancel.width()) // 2
        y = (self.height() + 56) // 2
        y = min(y, self.height() - self._btn_cancel.height() - 8)
        self._btn_cancel.move(max(0, x), max(0, y))

    def resizeEvent(self, event):  # noqa: N802 — override Qt
        super().resizeEvent(event)
        self._position_cancel()

    # ── animazione ───────────────────────────────────────────────────
    def _tick(self):
        self._elapsed += self._timer.interval()
        tau = self._duration / 2.5
        self._progress = self._target * (1.0 - math.exp(-self._elapsed / tau))
        self.update()

    def _hide_when_idle(self):
        if not self._timer.isActive():
            self.hide()

    # ── rendering ────────────────────────────────────────────────────
    def paintEvent(self, event):  # noqa: N802 — override Qt
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        painter.fillRect(rect, QColor.fromString(theme.color("liquid_bg")))

        if self._pixmap is not None and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(
                rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (rect.width() - scaled.width()) // 2
            y = (rect.height() - scaled.height()) // 2
            painter.setOpacity(0.35)
            painter.drawPixmap(x, y, scaled)
            painter.setOpacity(1.0)

        height = int(rect.height() * self._progress / 100.0)
        if height > 0:
            top = rect.height() - height
            gradient = QLinearGradient(0, top, 0, rect.height())
            gradient.setColorAt(0.0, QColor(79, 140, 255, 77))
            gradient.setColorAt(1.0, QColor(79, 140, 255, 148))
            painter.fillRect(QRect(0, top, rect.width(), height), gradient)
            painter.setPen(QColor(79, 140, 255, 230))
            painter.drawLine(0, top, rect.width(), top)

        if self._caption:
            font = painter.font()
            font.setPointSize(13)
            font.setBold(True)
            painter.setFont(font)
            flags = int(Qt.AlignmentFlag.AlignCenter) | int(
                Qt.TextFlag.TextWordWrap
            )
            metrics = painter.fontMetrics()
            box = metrics.boundingRect(
                QRect(8, 0, max(40, rect.width() - 16), rect.height()),
                flags,
                self._caption,
            )
            box.adjust(-12, -8, 12, 8)
            box.moveCenter(rect.center())
            painter.fillRect(
                box, QColor.fromString(theme.color("liquid_caption_bg"))
            )
            painter.setPen(QColor.fromString(theme.color("liquid_text")))
            painter.drawText(box, flags, self._caption)


class _SheenButton(QPushButton):
    """QPushButton con un riflesso luminoso ("scintilla") che ne attraversa la superficie.

    Qt non supporta le animazioni dei fogli di stile, quindi il riflesso è
    disegnato in ``paintEvent`` e la sua posizione è animata da una
    ``QPropertyAnimation`` sulla property ``sheenPos`` (0→1). Il colore arriva
    dal token di tema ``fab_spark``: bianco sul tema scuro, blu su quello chiaro
    (dove un riflesso bianco sarebbe invisibile su pagina bianca).

    Il riflesso è pensato per il pulsante flottante "Azioni pagina": viene
    avviato alla comparsa e fermato alla scomparsa, in parallelo al glow.
    """

    # Durata del ciclo del riflesso (ms), coerente col mockup (2.4 s).
    SHEEN_MS = 2400
    # Raggio degli angoli: allineato al ``border-radius: 15px`` del QSS del FAB.
    SHEEN_RADIUS = 15.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sheen_pos = 0.0
        self._sheen_enabled = False
        self._sheen_color = QColor("#ffffff")
        self._sheen_anim = QPropertyAnimation(self, b"sheenPos", self)
        self._sheen_anim.setDuration(self.SHEEN_MS)
        self._sheen_anim.setLoopCount(-1)
        self._sheen_anim.setStartValue(0.0)
        # Raggiunge il bordo destro al 55% del ciclo, poi "riposa" fuori campo:
        # imita la pausa tra due passate del riflesso.
        self._sheen_anim.setKeyValueAt(0.55, 1.0)
        self._sheen_anim.setEndValue(1.0)
        self._sheen_anim.setEasingCurve(QEasingCurve.Type.Linear)

    # ── property animata ────────────────────────────────────────────────
    def _get_sheen_pos(self) -> float:
        return self._sheen_pos

    def _set_sheen_pos(self, value: float):
        self._sheen_pos = float(value)
        self.update()

    sheenPos = pyqtProperty(float, _get_sheen_pos, _set_sheen_pos)

    # ── API ─────────────────────────────────────────────────────────────
    def set_sheen_color(self, color) -> None:
        """Aggiorna il colore del riflesso (dal tema attivo)."""
        self._sheen_color = QColor(color)
        self.update()

    def start_sheen(self) -> None:
        """Avvia il riflesso in loop (pulsante visibile)."""
        self._sheen_enabled = True
        self._sheen_anim.start()

    def stop_sheen(self) -> None:
        """Ferma il riflesso e lo riporta fuori campo."""
        self._sheen_enabled = False
        self._sheen_anim.stop()
        self._sheen_pos = 0.0
        self.update()

    def is_sheen_running(self) -> bool:
        return self._sheen_enabled

    # ── disegno ─────────────────────────────────────────────────────────
    def paintEvent(self, event):  # noqa: N802 (API Qt)
        super().paintEvent(event)
        if not self._sheen_enabled:
            return
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Clip agli angoli arrotondati del pulsante (meno il bordo di 2px).
        path = QPainterPath()
        path.addRoundedRect(
            QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0),
            self.SHEEN_RADIUS, self.SHEEN_RADIUS,
        )
        painter.setClipPath(path)
        # Banda diagonale che scorre da sinistra a destra.
        band = max(24.0, w * 0.5)
        x = -band + (w + 2.0 * band) * self._sheen_pos
        grad = QLinearGradient(x, 0.0, x + band, float(h))
        edge = QColor(self._sheen_color)
        edge.setAlpha(0)
        grad.setColorAt(0.0, edge)
        grad.setColorAt(0.5, QColor(self._sheen_color))
        grad.setColorAt(1.0, edge)
        painter.fillRect(self.rect(), grad)
        painter.end()


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
    export_current_requested = pyqtSignal()
    translate_cancel_requested = pyqtSignal()
    install_engine_requested = pyqtSignal()
    enter_key_requested = pyqtSignal()
    use_free_engine_requested = pyqtSignal()
    page_save_download_requested = pyqtSignal()
    page_translate_next_requested = pyqtSignal()
    page_retranslate_requested = pyqtSignal()
    page_open_external_requested = pyqtSignal()
    page_purge_requested = pyqtSignal()

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

        # Pulsante di traduzione on demand: sta nella toolbar principale in
        # alto, così resta raggiungibile anche con la fascia motori chiusa.
        # (Nessun pulsante qui.)

        bar.addStretch()

        self._lbl_status = QLabel("")
        self._lbl_status.setStyleSheet("color: #aaa; font-size: 12px;")
        bar.addWidget(self._lbl_status)

        # Targhetta "in lavorazione: pagina N" (traduzione di una pagina che
        # non è quella attualmente mostrata).
        self._lbl_working = QLabel("")
        self._lbl_working.setStyleSheet(
            "color: #ffcc66; font-size: 12px; font-weight: bold;"
        )
        bar.addWidget(self._lbl_working)

        # Esporta la pagina tradotta: 💾 con menu a tendina (procedura guidata
        # oppure esportazione rapida della sola pagina corrente).
        self.btn_export = QToolButton()
        self.btn_export.setText("💾")
        self.btn_export.setToolTip(T("clone.export.tip"))
        self.btn_export.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_export.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_export.setStyleSheet(
            "QToolButton { background: transparent; color: #ddd; border: none;"
            " font-size: 14px; padding: 0 4px; }"
            "QToolButton:hover { color: #fff; }"
            "QToolButton::menu-indicator { width: 0; }"
        )
        export_menu = QMenu(self.btn_export)
        export_menu.addAction(
            T("export.menu.wizard"), self.export_requested.emit
        )
        export_menu.addAction(
            T("export.menu.current"), self.export_current_requested.emit
        )
        self.btn_export.setMenu(export_menu)
        bar.addWidget(self.btn_export)

        # ── Striscia "motore non installato" (nascosta se il motore c'è) ──
        # Non blocca nulla: informa e offre il pulsante Installa. Si nasconde
        # da sola appena il motore è rilevato.
        self._engine_banner = QFrame()
        self._engine_banner.setObjectName("engineBanner")
        self._engine_banner.setStyleSheet(
            "QFrame#engineBanner { background: #4a3f1c;"
            " border-bottom: 1px solid #6d5c28; }"
        )
        banner_lay = QHBoxLayout(self._engine_banner)
        banner_lay.setContentsMargins(10, 4, 10, 4)
        banner_lay.setSpacing(10)
        self._lbl_engine_banner = QLabel(T("clone.engine_missing"))
        self._lbl_engine_banner.setWordWrap(True)
        self._lbl_engine_banner.setStyleSheet(
            "color: #ffd873; font-size: 12px; background: transparent;"
        )
        banner_lay.addWidget(self._lbl_engine_banner, 1)
        self.btn_install_engine = QPushButton(T("clone.engine_missing.install"))
        self.btn_install_engine.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_install_engine.setStyleSheet(
            "QPushButton { background: #b5892a; color: #1a1a1a; border: none;"
            " border-radius: 4px; padding: 3px 12px; font-size: 12px;"
            " font-weight: bold; }"
            "QPushButton:hover { background: #d0a03a; }"
        )
        self.btn_install_engine.clicked.connect(
            self.install_engine_requested.emit
        )
        banner_lay.addWidget(self.btn_install_engine)
        self._engine_banner.hide()

        # ── Striscia "chiave OpenRouter mancante" (motore LLM selezionato) ──
        # Non blocca: informa e offre le due azioni (inserisci la chiave oppure
        # passa a un motore gratuito). Si nasconde quando la chiave c'è.
        self._key_banner = QFrame()
        self._key_banner.setObjectName("keyBanner")
        self._key_banner.setStyleSheet(
            "QFrame#keyBanner { background: #1c2a4a;"
            " border-bottom: 1px solid #28406d; }"
        )
        key_lay = QHBoxLayout(self._key_banner)
        key_lay.setContentsMargins(10, 4, 10, 4)
        key_lay.setSpacing(10)
        self._lbl_key_banner = QLabel(T("clone.key_missing"))
        self._lbl_key_banner.setWordWrap(True)
        self._lbl_key_banner.setStyleSheet(
            "color: #9dc0ff; font-size: 12px; background: transparent;"
        )
        key_lay.addWidget(self._lbl_key_banner, 1)
        self.btn_enter_key = QPushButton(T("clone.key_missing.enter"))
        self.btn_enter_key.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_enter_key.setStyleSheet(
            "QPushButton { background: #2a5db5; color: #fff; border: none;"
            " border-radius: 4px; padding: 3px 12px; font-size: 12px;"
            " font-weight: bold; }"
            "QPushButton:hover { background: #3a78d0; }"
        )
        self.btn_enter_key.clicked.connect(self.enter_key_requested.emit)
        key_lay.addWidget(self.btn_enter_key)
        self.btn_use_free = QPushButton(T("clone.key_missing.use_free"))
        self.btn_use_free.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_use_free.setStyleSheet(
            "QPushButton { background: #3a3a3a; color: #eaeaea;"
            " border: 1px solid #555; border-radius: 4px; padding: 3px 12px;"
            " font-size: 12px; }"
            "QPushButton:hover { background: #4a4a4a; }"
        )
        self.btn_use_free.clicked.connect(self.use_free_engine_requested.emit)
        key_lay.addWidget(self.btn_use_free)
        self._key_banner.hide()

        # ── Page view (read-only) wrapped in a scroll area ────────────
        self.view = PdfPageView()
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(self.view)

        layout.addWidget(self._bar)
        layout.addWidget(self._engine_banner)
        layout.addWidget(self._key_banner)
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

        # Overlay "liquido" (progresso stimato) sopra la pagina in traduzione.
        self._liquid = LiquidOverlay(self)
        self._liquid.cancel_requested.connect(
            self.translate_cancel_requested.emit
        )
        self._liquid.hide()

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

        # ── Pulsante "Azioni pagina" (appare a traduzione fatta) ────────
        # Overlay figlio del PANNELLO (fuori layout), indipendente dalla barra
        # motori: resta visibile anche quando la barra è collassata. Posizionato
        # in alto a destra (sotto la barra, o accanto al chevron se collassata).
        self._fab = _SheenButton(T("clone.fab.title"), self)
        self._fab.setObjectName("pageFab")
        self._fab.setCursor(Qt.CursorShape.PointingHandCursor)
        self._fab.setToolTip(T("clone.fab.tip"))
        self._fab.clicked.connect(self._toggle_page_actions)
        self._fab.hide()
        self._fab.raise_()

        # Glow intermittente (drop-shadow animato): rende il pulsante evidente
        # su qualunque sfondo, pagina bianca o scura. Il colore viene dal tema.
        self._fab_glow_effect = QGraphicsDropShadowEffect(self._fab)
        self._fab_glow_effect.setOffset(0, 0)
        self._fab_glow_effect.setBlurRadius(18.0)
        glow_base = QColor(theme.color("fab_glow"))
        glow_base.setAlpha(190)
        self._fab_glow_effect.setColor(glow_base)
        self._fab.setGraphicsEffect(self._fab_glow_effect)
        self._fab_glow_anims: tuple = ()

        # Pallino persistente: cue che resta finché il menu non viene aperto.
        # È figlio del pulsante, in alto a destra, e non intercetta i clic
        # (che devono aprire il menu).
        self._fab_dot = QLabel(self._fab)
        self._fab_dot.setObjectName("pageFabDot")
        self._fab_dot.setFixedSize(8, 8)
        self._fab_dot.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self._fab_dot.hide()
        self._fab_badge_pending = False

        self._fab_menu = QMenu(self)
        self._fab_menu.setObjectName("pageFabMenu")
        self._fab_actions: dict = {}
        for key, sig in (
            ("save_download", self.page_save_download_requested),
            ("export", self.export_requested),
            ("next", self.page_translate_next_requested),
            ("retranslate", self.page_retranslate_requested),
            ("open_external", self.page_open_external_requested),
            ("purge", self.page_purge_requested),
        ):
            act = self._fab_menu.addAction(T(f"clone.fab.{key}"))
            act.triggered.connect(lambda _checked=False, s=sig: s.emit())
            self._fab_actions[key] = act

        self._view_zoom: float = 1.0

        # Applica i colori del tema attivo a tutti gli stili inline del pannello.
        self.apply_theme()

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
        y = 6
        # A barra collassata il pulsante non ha più la barra sotto di sé: se
        # sono visibili le strisce informative le spostiamo sotto di esse per
        # non coprirne i pulsanti.
        if self._is_collapsed:
            offset = 0
            if not self._engine_banner.isHidden():
                offset += self._engine_banner.sizeHint().height()
            if not self._key_banner.isHidden():
                offset += self._key_banner.sizeHint().height()
            if offset:
                y = offset + 6
        b.move(x, y)
        self._reposition_fab()

    # ── pulsante "Azioni pagina" (overlay indipendente dalla barra) ───

    def show_page_actions(self):
        """Mostra il pulsante Azioni (a traduzione completata).

        Il pulsante è un overlay del pannello, indipendente dalla barra motori:
        resta visibile anche con la barra collassata. Il pallino-badge resta
        finché l'utente non apre il menu (nuovo esito da guardare).
        """
        self._fab_badge_pending = True
        self._fab.show()
        self._fab.raise_()
        self._fab_dot.show()
        self._reposition_fab()
        self._reposition_fab_dot()
        self._start_fab_glow()
        self._fab.start_sheen()

    def hide_page_actions(self):
        """Nasconde il pulsante Azioni (cambio pagina, nuova traduzione)."""
        self._stop_fab_glow()
        self._fab.stop_sheen()
        self._clear_fab_badge()
        self._fab_menu.hide()
        self._fab.hide()

    def is_page_actions_visible(self) -> bool:
        return not self._fab.isHidden()

    def set_page_actions_next_enabled(self, enabled: bool):
        """Abilita/disabilita la voce "Traduci la successiva" (ultima pagina)."""
        act = self._fab_actions.get("next")
        if act is not None:
            act.setEnabled(bool(enabled))

    def _clear_fab_badge(self):
        """Spegne il pallino: ha fatto il suo lavoro o il pulsante è nascosto."""
        self._fab_badge_pending = False
        if getattr(self, "_fab_dot", None) is not None:
            self._fab_dot.hide()

    def _reposition_fab_dot(self):
        """Posiziona il pallino nell'angolo alto-destro del pulsante."""
        d = self._fab_dot
        if d is None or d.isHidden():
            return
        d.move(max(0, self._fab.width() - d.width() - 2), 2)
        d.raise_()

    def _reposition_fab(self):
        """Posiziona l'overlay Azioni in alto a destra, sempre visibile.

        - barra espansa: subito **sotto** la barra (e le strisce), così non
          copre i controlli della barra;
        - barra collassata: **accanto al chevron**, così segue il chevron
          quando questo scende sotto le strisce informative.
        """
        b = getattr(self, "_fab", None)
        if b is None or b.isHidden():
            return
        b.adjustSize()
        cb = self._collapse_btn
        if self._is_collapsed:
            x = cb.x() - b.width() - 34
            y = cb.y() + max(0, (cb.height() - b.height()) // 2) - 6
        else:
            top = self._bar.height()
            if not self._engine_banner.isHidden():
                top += self._engine_banner.sizeHint().height()
            if not self._key_banner.isHidden():
                top += self._key_banner.sizeHint().height()
            x = self.width() - b.width() - 52
            y = top + 24
        b.move(max(0, x), max(0, y))
        self._reposition_fab_dot()

    # Durata del ciclo di glow intermittente (ms).
    _FAB_GLOW_MS = 1700

    def _start_fab_glow(self):
        """Avvia il glow intermittente (loop) attorno al pulsante Azioni."""
        self._stop_fab_glow()
        eff = getattr(self, "_fab_glow_effect", None)
        if eff is None:
            return
        base = QColor(theme.color("fab_glow"))
        low = QColor(base)
        low.setAlpha(171)
        high = QColor(base)
        high.setAlpha(255)
        eff.setColor(low)

        blur = QPropertyAnimation(eff, b"blurRadius", self)
        blur.setDuration(self._FAB_GLOW_MS)
        blur.setLoopCount(-1)
        blur.setStartValue(18.0)
        blur.setKeyValueAt(0.5, 52.0)
        blur.setEndValue(18.0)
        blur.setEasingCurve(QEasingCurve.Type.InOutSine)

        color = QPropertyAnimation(eff, b"color", self)
        color.setDuration(self._FAB_GLOW_MS)
        color.setLoopCount(-1)
        color.setStartValue(low)
        color.setKeyValueAt(0.5, high)
        color.setEndValue(low)
        color.setEasingCurve(QEasingCurve.Type.InOutSine)

        self._fab_glow_anims = (blur, color)
        blur.start()
        color.start()

    def _stop_fab_glow(self):
        """Ferma il glow e riporta l'ombra a un valore discreto."""
        for anim in getattr(self, "_fab_glow_anims", ()) or ():
            anim.stop()
        self._fab_glow_anims = ()
        eff = getattr(self, "_fab_glow_effect", None)
        if eff is not None:
            base = QColor(theme.color("fab_glow"))
            base.setAlpha(190)
            eff.setColor(base)
            eff.setBlurRadius(18.0)

    def _toggle_page_actions(self):
        if self._fab_menu.isVisible():
            self._fab_menu.hide()
            return
        # Il menu è stato aperto: il pallino ha fatto il suo lavoro.
        self._clear_fab_badge()
        self._fab_menu.adjustSize()
        pos = self._fab.mapToGlobal(QPoint(0, self._fab.height() + 4))
        x = pos.x() + self._fab.width() - self._fab_menu.width()
        self._fab_menu.popup(QPoint(max(0, x), pos.y()))


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

    def set_engine_missing(self, missing: bool):
        """Mostra/nasconde la striscia "motore di traduzione non installato"."""
        self._engine_banner.setVisible(bool(missing))
        self._reposition_collapse_btn()

    def set_engine_key_missing(self, missing: bool):
        """Mostra/nasconde la striscia "chiave OpenRouter mancante" (LLM)."""
        self._key_banner.setVisible(bool(missing))
        self._reposition_collapse_btn()

    def show_page(self, pixmap: QPixmap | None):
        self.hide_spinner()
        self._liquid.stop()
        self.view.show_page(pixmap)
        self._lbl_status.setText("")

    def show_pending(self, pixmap: QPixmap | None, hint: str):
        """Originale + invito a lanciare la traduzione (nessun processo attivo)."""
        self._liquid.stop()
        self.view.show_page(pixmap)
        self._lbl_status.setText(T("clone.status_todo"))
        self.show_spinner(hint)

    def show_translating(self, pixmap: QPixmap | None, caption: str,
                         cancelable: bool = True):
        """Mostra l'overlay liquido durante la traduzione della pagina."""
        self.hide_page_actions()
        self.hide_spinner()
        self.view.show_page(None)
        self._lbl_status.setText(T("clone.status_running"))
        self._liquid.setGeometry(self.scroll_area.geometry())
        self._liquid.start(pixmap, caption, cancelable=cancelable)

    def finish_translating(self, ok: bool = True):
        self._liquid.finish(ok)

    def hide_liquid(self):
        self._liquid.stop()

    def show_message(self, text: str):
        self._liquid.stop()
        self.hide_spinner()
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

    def set_working(self, text: str):
        self._lbl_working.setText(text)

    def _position_spinner(self):
        self._spinner.adjustSize()
        x = (self.width() - self._spinner.width()) // 2
        y = (self.height() - self._spinner.height()) // 2
        self._spinner.move(max(0, x), max(0, y))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_collapse_btn()
        if not self._fab.isHidden():
            self._reposition_fab()
        if self._spinner.isVisible():
            self._position_spinner()
        if self._liquid.isVisible():
            self._liquid.setGeometry(self.scroll_area.geometry())
            self._liquid.raise_()

    def apply_theme(self):
        """(Ri)applica i colori del tema attivo a tutti gli stili del pannello."""
        q = theme.color
        self._bar.setStyleSheet(
            "QWidget#cloneBar { background: %s; border-bottom: 1px solid %s; }"
            % (q("bar_bg"), q("bar_border"))
        )
        self._lbl_target.setStyleSheet(
            "color: %s; font-size: 13px; font-weight: bold;" % q("text2")
        )
        radio_style = """
            QRadioButton { color: %s; font-size: 12px; background: transparent;
                           spacing: 5px; }
            QRadioButton:hover { color: %s; }
            QRadioButton::indicator {
                width: 13px; height: 13px; border: 1px solid %s;
                border-radius: 7px; background: %s;
            }
            QRadioButton::indicator:checked {
                background: %s; border-color: %s;
            }
        """ % (
            q("text4"), q("text"), q("radio_border"), q("radio_bg"),
            q("accent"), q("accent"),
        )
        for rb in self._radios.values():
            rb.setStyleSheet(radio_style)
        self._lbl_status.setStyleSheet(
            "color: %s; font-size: 12px;" % q("text4")
        )
        self._lbl_working.setStyleSheet(
            "color: %s; font-size: 12px; font-weight: bold;" % q("warn")
        )
        self.btn_export.setStyleSheet(
            "QToolButton { background: transparent; color: %s; border: none;"
            " font-size: 14px; padding: 0 4px; }"
            "QToolButton:hover { color: %s; }"
            "QToolButton::menu-indicator { width: 0; }"
            % (q("text2"), q("text"))
        )
        self._engine_banner.setStyleSheet(
            "QFrame#engineBanner { background: %s; border-bottom: 1px solid %s; }"
            % (q("banner_engine_bg"), q("banner_engine_border"))
        )
        self._lbl_engine_banner.setStyleSheet(
            "color: %s; font-size: 12px; background: transparent;"
            % q("banner_engine_text")
        )
        self.btn_install_engine.setStyleSheet(
            "QPushButton { background: %s; color: %s; border: none;"
            " border-radius: 4px; padding: 3px 12px; font-size: 12px;"
            " font-weight: bold; }"
            "QPushButton:hover { background: %s; }"
            % (
                q("banner_engine_btn"), q("banner_engine_btn_text"),
                q("banner_engine_btn_hover"),
            )
        )
        self._key_banner.setStyleSheet(
            "QFrame#keyBanner { background: %s; border-bottom: 1px solid %s; }"
            % (q("banner_key_bg"), q("banner_key_border"))
        )
        self._lbl_key_banner.setStyleSheet(
            "color: %s; font-size: 12px; background: transparent;"
            % q("banner_key_text")
        )
        self.btn_enter_key.setStyleSheet(
            "QPushButton { background: %s; color: %s; border: none;"
            " border-radius: 4px; padding: 3px 12px; font-size: 12px;"
            " font-weight: bold; }"
            "QPushButton:hover { background: %s; }"
            % (
                q("banner_key_btn"), q("accent_text"),
                q("banner_key_btn_hover"),
            )
        )
        self.btn_use_free.setStyleSheet(
            "QPushButton { background: %s; color: %s;"
            " border: 1px solid %s; border-radius: 4px; padding: 3px 12px;"
            " font-size: 12px; }"
            "QPushButton:hover { background: %s; }"
            % (
                q("banner_key_btn2"), q("banner_key_btn2_text"),
                q("border2"), q("banner_key_btn2_hover"),
            )
        )
        self._spinner.setStyleSheet(
            "QLabel { background: %s; color: %s; border: 1px solid %s;"
            " border-radius: 12px; padding: 16px 34px; font-size: 18px;"
            " font-weight: bold; }"
            % (q("spinner_bg"), q("spinner_text"), q("spinner_border"))
        )
        self._collapse_btn.setStyleSheet(
            "QPushButton { background: %s; color: %s;"
            " border: 1px solid %s; border-radius: 4px; font-size: 12px;"
            " padding: 0; }"
            "QPushButton:hover { background: %s; color: %s; }"
            % (
                q("collapse_bg"), q("collapse_text"), q("collapse_border"),
                q("bg_hover"), q("text"),
            )
        )
        self._fab.setStyleSheet(
            "QPushButton#pageFab {"
            " background: %s; color: %s;"
            " border: 2px solid %s; border-radius: 15px;"
            " padding: 5px 14px; font-size: 13px; font-weight: 700; }"
            "QPushButton#pageFab:hover { background: %s;"
            " border-color: %s; }"
            % (
                q("fab_bg"), q("fab_text"), q("fab_border"),
                q("fab_hover_bg"), q("fab_hover_border"),
            )
        )
        # Glow: aggiorna il colore al tema e riavvia se il pulsante è visibile.
        if getattr(self, "_fab_glow_effect", None) is not None:
            if self._fab.isVisible():
                self._start_fab_glow()
            else:
                self._stop_fab_glow()
        # Scintilla: colore dipendente dal tema, avviata solo a pulsante visibile.
        if getattr(self, "_fab", None) is not None:
            self._fab.set_sheen_color(q("fab_spark"))
            if self._fab.isVisible():
                self._fab.start_sheen()
            else:
                self._fab.stop_sheen()
        self._fab_dot.setStyleSheet(
            "QLabel#pageFabDot { background: %s; border-radius: 4px; }"
            % q("badge")
        )
        # Menu del FAB: bordo più spesso e a rilievo (lato alto/sinistro chiaro,
        # basso/destro scuro) su fondo a gradiente verticale, per staccarlo
        # nettamente dalla pagina e dargli tridimensionalità. Qt non ha
        # box-shadow nei QSS: il rilievo è affidato a bordo + gradiente.
        self._fab_menu.setStyleSheet(
            "QMenu#pageFabMenu {"
            " background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
            " stop:0 %s, stop:1 %s);"
            " color: %s;"
            " border: 2px solid %s;"
            " border-top-color: %s; border-left-color: %s;"
            " border-bottom-color: %s; border-right-color: %s;"
            " border-radius: 9px; padding: 6px; }"
            "QMenu#pageFabMenu::item { padding: 8px 22px 8px 12px;"
            " border-radius: 6px; }"
            "QMenu#pageFabMenu::item:selected { background: %s;"
            " color: %s; }"
            "QMenu#pageFabMenu::item:disabled { color: %s; }"
            % (
                q("menu_bg_top"), q("menu_bg_bottom"), q("menu_text"),
                q("menu_border"),
                q("menu_border_hi"), q("menu_border_hi"),
                q("menu_border_lo"), q("menu_border_lo"),
                q("sel_bg"), q("sel_text"), q("menu_disabled"),
            )
        )
        if hasattr(self, "_liquid"):
            self._liquid.apply_theme()
        if hasattr(self, "view"):
            self.view.apply_theme()

    def retranslate(self):
        self._collapse_btn.setToolTip(
            T("clone.bar.expand") if self._is_collapsed else T("clone.bar.collapse")
        )
        self.btn_export.setToolTip(T("clone.export.tip"))
        self._lbl_engine_banner.setText(T("clone.engine_missing"))
        self.btn_install_engine.setText(T("clone.engine_missing.install"))
        self._lbl_key_banner.setText(T("clone.key_missing"))
        self.btn_enter_key.setText(T("clone.key_missing.enter"))
        self.btn_use_free.setText(T("clone.key_missing.use_free"))
        self._fab.setText(T("clone.fab.title"))
        self._fab.setToolTip(T("clone.fab.tip"))
        for key, act in self._fab_actions.items():
            act.setText(T(f"clone.fab.{key}"))
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
        self.apply_theme()

        layout.addWidget(self.tree)

        self._items: list[QTreeWidgetItem] = []  # flat, in document order
        self._syncing: bool = False

        self.tree.itemClicked.connect(self._on_item_clicked)

    def apply_theme(self):
        """Applica i colori del tema attivo al TOC."""
        self.tree.setStyleSheet(
            "QTreeWidget { background: %s; color: %s; border: none;"
            " font-size: 13px; }"
            "QTreeWidget::item { padding: 2px 0; }"
            "QTreeWidget::item:selected { background: %s; color: %s; }"
            % (
                theme.color("tree_bg"), theme.color("tree_text"),
                theme.color("tree_sel_bg"), theme.color("tree_sel_text"),
            )
        )

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


class ApiKeyDialog(QDialog):
    """Inserimento della chiave OpenRouter (cross-platform).

    Usata sia dal gruppo "Motore" delle Impostazioni sia dal prompt quando si
    sceglie l'engine LLM senza chiave. La chiave viene salvata dal chiamante
    (MainWindow) nell'archivio per-utente.
    """

    OPENROUTER_KEYS_URL = "https://openrouter.ai/keys"

    def __init__(self, parent=None, initial: str = ""):
        super().__init__(parent)
        self.setWindowTitle(T("apikey.title"))
        self.setMinimumWidth(520)
        self.setStyleSheet(theme.settings_qss())

        root = QVBoxLayout(self)
        root.setSpacing(10)

        intro_text = T("apikey.intro")
        if _API_KEY_ENV_STALE:
            intro_text += "\n\n" + T("apikey.source.env_stale")
        intro = QLabel(intro_text)
        intro.setWordWrap(True)
        root.addWidget(intro)

        self._edit = QLineEdit()
        self._edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._edit.setPlaceholderText(T("apikey.placeholder"))
        self._edit.setText(initial or "")
        root.addWidget(self._edit)

        row = QHBoxLayout()
        self._chk_show = QCheckBox(T("apikey.show"))
        self._chk_show.toggled.connect(self._on_show_toggled)
        row.addWidget(self._chk_show)
        row.addStretch(1)
        self._btn_verify = QPushButton(T("apikey.verify"))
        self._btn_verify.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_verify.clicked.connect(self._on_verify)
        row.addWidget(self._btn_verify)
        root.addLayout(row)

        self._result = QLabel("")
        self._result.setWordWrap(True)
        self._result.setStyleSheet(_status_style("text5"))
        root.addWidget(self._result)

        link = QLabel(
            f'<a href="{self.OPENROUTER_KEYS_URL}">{T("apikey.link")}</a>'
        )
        link.setOpenExternalLinks(True)
        root.addWidget(link)

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

    def value(self) -> str:
        return self._edit.text().strip()

    def _on_show_toggled(self, checked: bool):
        self._edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked
            else QLineEdit.EchoMode.Password
        )

    def _on_verify(self):
        key = self.value()
        ok, reason = keystore.KeyStore.verify(key)
        if ok:
            self._result.setText(T("apikey.verify_ok"))
            self._result.setStyleSheet(_status_style("ok"))
        elif reason == "invalid":
            self._result.setText(T("apikey.verify_invalid"))
            self._result.setStyleSheet(_status_style("err"))
        elif reason == "missing":
            self._result.setText(T("apikey.verify_missing"))
            self._result.setStyleSheet(_status_style("err"))
        else:
            self._result.setText(T("apikey.verify_unreachable"))
            self._result.setStyleSheet(_status_style("warn"))


class KeyVerifyThread(QThread):
    """Verifica non bloccante della chiave OpenRouter (endpoint ``/key``)."""

    verified = pyqtSignal(bool, str, str)  # ok, reason, key

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self._key = key

    def run(self):  # noqa: D102 — override QThread
        try:
            ok, reason = keystore.KeyStore.verify(self._key)
        except Exception:  # noqa: BLE001 — la verifica non deve mai crashare
            ok, reason = False, "unreachable"
        self.verified.emit(ok, reason, self._key)


class EngineInstallThread(QThread):
    """Installa ``pdf2zh_next`` in ``.venv2`` (via ``uv``) senza bloccare la GUI."""

    log_line = pyqtSignal(str)
    done = pyqtSignal(bool, str)  # successo, percorso oppure messaggio d'errore

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):  # noqa: D102 — override QThread
        try:
            found = clone_engine.install_engine(
                log_line=self.log_line.emit, cancel=self._cancel
            )
        except RuntimeError as exc:
            code = str(exc)
            if code == "cancelled":
                msg = T("engine.install.cancelled")
            elif code == "no_uv":
                msg = T("engine.install.no_uv")
            else:
                msg = T("engine.install.failed")
            self.done.emit(False, msg)
            return
        except Exception as exc:  # noqa: BLE001 — mostra l'errore, non crashare
            self.done.emit(False, T("engine.install.failed_detail", e=exc))
            return
        self.done.emit(True, str(found))


class EngineInstallDialog(QDialog):
    """Stato dell'installazione del motore: log di ``uv`` + barra di avanzamento."""

    installed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(T("engine.install.title"))
        self.setMinimumSize(660, 440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(10)

        intro = QLabel(T("engine.install.intro"))
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setPlaceholderText(T("engine.install.running"))
        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        self._log.setFont(mono)
        lay.addWidget(self._log, 1)

        self._bar = QProgressBar()
        self._bar.setRange(0, 0)  # indeterminata: uv non dà una percentuale
        lay.addWidget(self._bar)

        self._status = QLabel(T("engine.install.running"))
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self._btn_cancel = QPushButton(T("engine.install.cancel"))
        self._btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_cancel.clicked.connect(self._on_cancel)
        self._btn_close = QPushButton(T("engine.install.close"))
        self._btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_close.clicked.connect(self.accept)
        self._btn_close.setEnabled(False)
        buttons.addWidget(self._btn_cancel)
        buttons.addWidget(self._btn_close)
        lay.addLayout(buttons)

        self._thread = EngineInstallThread(self)
        self._thread.log_line.connect(self._append)
        self._thread.done.connect(self._on_done)
        self._thread.start()

    def _append(self, line: str):
        self._log.appendPlainText(line)
        bar = self._log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_cancel(self):
        self._thread.cancel()
        self._btn_cancel.setEnabled(False)
        self._status.setText(T("engine.install.cancelling"))

    def _on_done(self, ok: bool, info: str):
        self._bar.setRange(0, 1)
        self._bar.setValue(1 if ok else 0)
        self._btn_cancel.setEnabled(False)
        self._btn_close.setEnabled(True)
        self._btn_close.setFocus()
        if ok:
            self._status.setText(T("engine.install.done", path=info))
            self.installed.emit(info)
        else:
            self._status.setText(info)

    def closeEvent(self, event):  # noqa: N802 — override Qt
        if self._thread.isRunning():
            self._thread.cancel()
            self._thread.wait(3000)
        super().closeEvent(event)


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
        self.setMinimumWidth(520)
        self.resize(580, 720)
        self.setStyleSheet(theme.settings_qss())

        root = QVBoxLayout(self)
        root.setSpacing(10)

        # Le sezioni sono molte: per non comprimere i controlli (che
        # diventavano inusabili) il contenuto scorre dentro una QScrollArea,
        # mentre i pulsanti OK/Annulla restano fissi in fondo.
        scroll = QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        content = QWidget()
        content.setObjectName("settingsContent")
        inner = QVBoxLayout(content)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(10)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

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
        inner.addWidget(self._box_lang)

        # ── Traduzione ──────────────────────────────────────────────────
        self._box_translation = QGroupBox(T("settings.group.translation"))
        trans_form = QFormLayout(self._box_translation)
        self._lbl_engine = QLabel(T("settings.translation.engine"))
        self._engine_combo = QComboBox()
        for code in TRANSLATION_ENGINES:
            self._engine_combo.addItem(T(f"engine.option.{code}"), code)
        self._engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        trans_form.addRow(self._lbl_engine, self._engine_combo)
        inner.addWidget(self._box_translation)

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
        self._btn_install_engine = QPushButton(T("settings.clone.install"))
        self._btn_install_engine.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_install_engine.setToolTip(T("settings.clone.install.tip"))
        self._btn_install_engine.clicked.connect(self._on_install_engine)
        pdf2zh_row.addWidget(self._pdf2zh_edit)
        pdf2zh_row.addWidget(self._btn_pdf2zh)
        pdf2zh_row.addWidget(self._btn_install_engine)
        clone_form.addRow(self._lbl_pdf2zh, pdf2zh_row)

        # Chiave OpenRouter: salvata nell'archivio per-utente (non in config.json).
        self._lbl_api = QLabel(T("settings.clone.apikey"))
        api_row = QHBoxLayout()
        self._api_edit = QLineEdit()
        self._api_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_edit.setPlaceholderText(T("apikey.placeholder"))
        self._api_edit.setText(keystore.KeyStore(_keystore_path()).get())
        self._chk_api_show = QCheckBox(T("apikey.show"))
        self._chk_api_show.toggled.connect(self._on_api_show_toggled)
        self._btn_api_verify = QPushButton(T("apikey.verify"))
        self._btn_api_verify.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_api_verify.clicked.connect(self._on_verify_api)
        self._btn_api_clear = QPushButton(T("apikey.clear"))
        self._btn_api_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_api_clear.clicked.connect(self._on_clear_api)
        api_row.addWidget(self._api_edit)
        api_row.addWidget(self._chk_api_show)
        api_row.addWidget(self._btn_api_verify)
        api_row.addWidget(self._btn_api_clear)
        clone_form.addRow(self._lbl_api, api_row)
        self._api_source = QLabel(_llm_key_source_text())
        self._api_source.setWordWrap(True)
        self._api_source.setStyleSheet(_status_style("text_muted"))
        clone_form.addRow(self._api_source)
        self._api_result = QLabel("")
        self._api_result.setWordWrap(True)
        self._api_result.setStyleSheet(_status_style("text5"))
        clone_form.addRow(self._api_result)

        self._lbl_clone_hint = QLabel(T("settings.clone.hint"))
        self._lbl_clone_hint.setWordWrap(True)
        self._lbl_clone_hint.setStyleSheet(_status_style("text5"))
        clone_form.addRow(self._lbl_clone_hint)
        inner.addWidget(self._box_clone)

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
        inner.addWidget(self._box_view)

        # ── Comportamento ───────────────────────────────────────────────
        self._box_beh = QGroupBox(T("settings.group.behavior"))
        beh_form = QFormLayout(self._box_beh)
        self._resume_check = QCheckBox(T("settings.behavior.resume"))
        beh_form.addRow(self._resume_check)
        self._tab_check = QCheckBox(T("settings.behavior.tab"))
        beh_form.addRow(self._tab_check)
        inner.addWidget(self._box_beh)

        # ── Aspetto (tema) ──────────────────────────────────────────────
        self._box_appearance = QGroupBox(T("settings.group.appearance"))
        appear_form = QFormLayout(self._box_appearance)
        self._lbl_theme = QLabel(T("settings.appearance.theme"))
        self._theme_combo = QComboBox()
        for code in ("dark", "light", "system"):
            self._theme_combo.addItem(T(f"settings.theme.{code}"), code)
        appear_form.addRow(self._lbl_theme, self._theme_combo)
        inner.addWidget(self._box_appearance)

        # ── Avvisi (fine batch + standby) ───────────────────────────────
        self._box_notify = QGroupBox(T("settings.group.notifications"))
        notify_form = QFormLayout(self._box_notify)
        self._notify_check = QCheckBox(T("settings.notify.on_finish"))
        notify_form.addRow(self._notify_check)
        self._sound_check = QCheckBox(T("settings.notify.sound"))
        notify_form.addRow(self._sound_check)
        self._btn_test_sound = QPushButton(T("settings.notify.test"))
        self._btn_test_sound.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_test_sound.setToolTip(
            T(
                "settings.notify.player",
                player=notifications.available_player() or "—",
            )
        )
        self._btn_test_sound.clicked.connect(
            lambda: notifications.chime(True)
        )
        notify_form.addRow(self._btn_test_sound)
        self._sleep_check = QCheckBox(T("settings.notify.prevent_sleep"))
        notify_form.addRow(self._sleep_check)
        inner.addWidget(self._box_notify)

        # ── Prestazioni ──────────────────────────────────────────────────
        # ── Prestazioni ──────────────────────────────────────────────────
        # Tre preset come "card" radio (tutte visibili), con i campi
        # governati dal preset dentro "Avanzate" (collassato) e lo stato del
        # provider inline.
        self._box_perf = QGroupBox(T("settings.group.performance"))
        perf_root = QVBoxLayout(self._box_perf)
        self._preset_radios: dict = {}
        self._preset_descs: dict = {}
        preset_group = QButtonGroup(self._box_perf)
        for code in ("normal", "fast", "fastest"):
            radio = QRadioButton(T(f"settings.performance.preset.{code}.label"))
            radio.setCursor(Qt.CursorShape.PointingHandCursor)
            radio.toggled.connect(
                lambda checked, c=code: self._on_preset_toggled(c, checked)
            )
            preset_group.addButton(radio)
            desc = QLabel(T(f"settings.performance.preset.{code}.desc"))
            desc.setWordWrap(True)
            desc.setStyleSheet(f"color: {theme.color('text5')}; font-size: 11px;")
            perf_root.addWidget(radio)
            perf_root.addWidget(desc)
            self._preset_radios[code] = radio
            self._preset_descs[code] = desc

        status_row = QHBoxLayout()
        self._proxy_test_result = QLabel("")
        self._proxy_test_result.setWordWrap(True)
        self._proxy_test_result.setStyleSheet(_status_style("text5"))
        self._btn_proxy_test = QPushButton(T("settings.proxy.test"))
        self._btn_proxy_test.setToolTip(T("settings.proxy.test.tip"))
        self._btn_proxy_test.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_proxy_test.clicked.connect(self._on_test_provider)
        status_row.addWidget(self._proxy_test_result, 1)
        status_row.addWidget(self._btn_proxy_test)
        perf_root.addLayout(status_row)

        # Avanzate (nascoste di default): i campi governati dal preset.
        self._btn_advanced = QToolButton()
        self._btn_advanced.setText(T("settings.performance.advanced"))
        self._btn_advanced.setCheckable(True)
        self._btn_advanced.setArrowType(Qt.ArrowType.RightArrow)
        self._btn_advanced.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._btn_advanced.clicked.connect(self._on_advanced_toggled)
        perf_root.addWidget(self._btn_advanced)

        self._advanced_widget = QWidget()
        perf_form = QFormLayout(self._advanced_widget)
        perf_form.setContentsMargins(0, 0, 0, 0)
        self._lbl_llm_workers = QLabel(T("settings.performance.llm_workers"))
        self._llm_workers_spin = QSpinBox()
        self._llm_workers_spin.setRange(1, 16)
        self._llm_workers_spin.setToolTip(
            T("settings.performance.llm_workers.tip")
        )
        perf_form.addRow(self._lbl_llm_workers, self._llm_workers_spin)
        # Feature sperimentale "motore veloce" (reversibile, default OFF).
        self._fast_engine_check = QCheckBox(T("settings.performance.fast_engine"))
        self._fast_engine_check.setToolTip(
            T("settings.performance.fast_engine.tip")
        )
        self._fast_engine_check.toggled.connect(self._on_fast_engine_toggled)
        perf_form.addRow(self._fast_engine_check)
        self._fast_flags_check = QCheckBox(T("settings.performance.fast_flags"))
        self._fast_flags_check.setToolTip(T("settings.performance.fast_flags.tip"))
        perf_form.addRow(self._fast_flags_check)
        self._numeric_lists_check = QCheckBox(
            T("settings.performance.numeric_lists")
        )
        self._numeric_lists_check.setToolTip(
            T("settings.performance.numeric_lists.tip")
        )
        perf_form.addRow(self._numeric_lists_check)
        self._fast_worker_check = QCheckBox(T("settings.performance.fast_worker"))
        self._fast_worker_check.setToolTip(T("settings.performance.fast_worker.tip"))
        perf_form.addRow(self._fast_worker_check)
        self._lbl_reasoning = QLabel(T("settings.performance.reasoning_effort"))
        self._reasoning_combo = QComboBox()
        for value in ("", "minimal", "low", "medium", "high"):
            self._reasoning_combo.addItem(value or "—", value)
        self._reasoning_combo.setToolTip(
            T("settings.performance.reasoning_effort.tip")
        )
        perf_form.addRow(self._lbl_reasoning, self._reasoning_combo)
        self._json_mode_check = QCheckBox(T("settings.performance.json_mode"))
        self._json_mode_check.setToolTip(T("settings.performance.json_mode.tip"))
        perf_form.addRow(self._json_mode_check)
        self._lbl_llm_model = QLabel(T("settings.llm.model"))
        self._llm_model_combo = QComboBox()
        self._llm_model_combo.setToolTip(T("settings.llm.model.tip"))
        perf_form.addRow(self._lbl_llm_model, self._llm_model_combo)
        self._lbl_llm_base = QLabel(T("settings.llm.base_url"))
        self._llm_base_combo = QComboBox()
        self._llm_base_combo.setToolTip(T("settings.llm.base_url.tip"))
        perf_form.addRow(self._lbl_llm_base, self._llm_base_combo)
        self._lbl_llm_prompt = QLabel(T("settings.llm.system_prompt"))
        self._llm_prompt_edit = QLineEdit()
        self._llm_prompt_edit.setToolTip(T("settings.llm.system_prompt.tip"))
        perf_form.addRow(self._lbl_llm_prompt, self._llm_prompt_edit)
        self._populate_llm_combos()
        self._proxy_autostart_check = QCheckBox(T("settings.proxy.autostart"))
        self._proxy_autostart_check.setToolTip(T("settings.proxy.autostart.tip"))
        self._proxy_autostart_check.toggled.connect(
            self._on_proxy_autostart_toggled
        )
        perf_form.addRow(self._proxy_autostart_check)
        self._lbl_proxy_port = QLabel(T("settings.proxy.port"))
        self._proxy_port_spin = QSpinBox()
        self._proxy_port_spin.setRange(1024, 65535)
        self._proxy_port_spin.setToolTip(T("settings.proxy.port.tip"))
        perf_form.addRow(self._lbl_proxy_port, self._proxy_port_spin)
        perf_root.addWidget(self._advanced_widget)
        self._advanced_widget.setVisible(False)
        inner.addWidget(self._box_perf)

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

    def _on_install_engine(self):
        """Scarica e installa il motore pdf2zh_next in ``.venv2`` (via uv)."""
        dlg = EngineInstallDialog(self)

        def _installed(_path: str):
            # Il motore è accanto all'app: basta l'auto-rilevamento.
            self._pdf2zh_edit.setText("")

        dlg.installed.connect(_installed)
        dlg.exec()

    def _on_api_show_toggled(self, checked: bool):
        self._api_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked
            else QLineEdit.EchoMode.Password
        )

    def _on_verify_api(self):
        key = self._api_edit.text().strip()
        if not key:
            # Campo vuoto: verifica la chiave attiva (es. variabile di sistema).
            key = (os.environ.get(keystore.ENV_VAR) or "").strip()
        ok, reason = keystore.KeyStore.verify(key)
        if ok:
            self._api_result.setText(T("apikey.verify_ok"))
            self._api_result.setStyleSheet(_status_style("ok"))
        elif reason == "invalid":
            self._api_result.setText(T("apikey.verify_invalid"))
            self._api_result.setStyleSheet(_status_style("err"))
        elif reason == "missing":
            self._api_result.setText(T("apikey.verify_missing"))
            self._api_result.setStyleSheet(_status_style("err"))
        else:
            self._api_result.setText(T("apikey.verify_unreachable"))
            self._api_result.setStyleSheet(_status_style("warn"))

    def _on_clear_api(self):
        """Rimuove la chiave salvata (il salvataggio avviene in config col campo)."""
        self._api_edit.clear()
        keystore.KeyStore(_keystore_path()).clear()
        self._api_source.setText(_llm_key_source_text())
        self._api_result.setText(T("apikey.removed"))
        self._api_result.setStyleSheet(_status_style("text5"))

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

    def _on_fast_engine_toggled(self, enabled: bool):
        """Abilita/disabilita le opzioni dipendenti dal motore veloce."""
        self._fast_flags_check.setEnabled(enabled)
        self._fast_worker_check.setEnabled(enabled)
        self._reasoning_combo.setEnabled(enabled)
        self._json_mode_check.setEnabled(enabled)
        self._proxy_autostart_check.setEnabled(enabled)
        self._proxy_port_spin.setEnabled(enabled and self._proxy_autostart_check.isChecked())
        self._btn_proxy_test.setEnabled(enabled)
        if not enabled:
            self._fast_flags_check.setChecked(False)
            self._fast_worker_check.setChecked(False)
            self._proxy_autostart_check.setChecked(False)

    def _on_proxy_autostart_toggled(self, enabled: bool):
        self._proxy_port_spin.setEnabled(enabled and self._fast_engine_check.isChecked())
        if enabled:
            # Allinea la selezione alla base URL del proxy.
            idx = self._llm_base_combo.findData("__proxy__")
            if idx >= 0:
                self._llm_base_combo.setCurrentIndex(idx)

    # Valori dei preset: governano tutti i campi sottostanti.
    # NOTA precisione: i flag "traduzione rapida" (B2) NON sono attivi nei
    # preset — saltano elaborazioni di layout/formule e riducono la precisione
    # per un guadagno trascurabile. Restano come opt-in manuale in Avanzate.
    # Prompt di default per "Massima velocità": rende gpt-oss coerente nella
    # terminologia (nomi dei farmaci in italiano, citazioni non tradotte).
    _FASTEST_PROMPT = (
        "Sei un traduttore medico EN->IT. Traduci fedelmente mantenendo la "
        "formattazione e l'ordine. Traduci i nomi dei farmaci nella forma "
        "italiana quando esiste (es. daptomycin->daptomicina, polymyxins->"
        "polimixine). NON tradurre le citazioni bibliografiche, i nomi di "
        "riviste e le sigle tecniche (ABG, AG, MIC)."
    )
    _PRESETS = {
        "normal": {
            "fast": False, "flags": False, "worker": False,
            "model": "inception/mercury-2.5", "base": "", "reasoning": "",
            "json": False, "autostart": False, "pool": 4, "prompt": "",
        },
        "fast": {
            "fast": True, "flags": False, "worker": True,
            "model": "inception/mercury-2.5", "base": "", "reasoning": "",
            "json": False, "autostart": False, "pool": 4, "prompt": "",
        },
        "fastest": {
            "fast": True, "flags": False, "worker": True,
            "model": "openai/gpt-oss-120b", "base": "__proxy__",
            "reasoning": "minimal", "json": False, "autostart": True, "pool": 8,
            "prompt": _FASTEST_PROMPT,
        },
    }

    @staticmethod
    def _select_combo_data(combo, data: str) -> None:
        idx = combo.findData(data)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _set_preset_fields_enabled(self, enabled: bool) -> None:
        # NOTA: `_fast_flags_check` è escluso: è l'opt-in manuale (B2) e resta
        # modificabile in Avanzate per chi accetta il trade-off sulla precisione.
        for widget in (
            self._fast_engine_check, self._fast_worker_check,
            self._llm_model_combo, self._llm_base_combo,
            self._reasoning_combo, self._json_mode_check,
            self._proxy_autostart_check, self._proxy_port_spin,
            self._llm_workers_spin, self._lbl_llm_workers, self._lbl_llm_model,
            self._lbl_llm_base, self._lbl_reasoning, self._lbl_proxy_port,
        ):
            widget.setEnabled(enabled)
        self._btn_proxy_test.setEnabled(True)

    def _apply_preset(self, name: str) -> None:
        """Compila i campi secondo il preset e li rende di sola lettura."""
        preset = self._PRESETS.get(name) or self._PRESETS["normal"]
        self._fast_engine_check.setChecked(preset["fast"])
        self._fast_flags_check.setChecked(preset["flags"])
        self._fast_worker_check.setChecked(preset["worker"])
        self._select_combo_data(self._llm_model_combo, preset["model"])
        self._select_combo_data(self._llm_base_combo, preset["base"])
        self._select_combo_data(self._reasoning_combo, preset["reasoning"])
        self._json_mode_check.setChecked(preset["json"])
        self._proxy_autostart_check.setChecked(preset["autostart"])
        self._llm_workers_spin.setValue(preset["pool"])
        self._llm_prompt_edit.setText(preset.get("prompt", ""))
        self._set_preset_fields_enabled(False)

    def _on_preset_toggled(self, code: str, checked: bool) -> None:
        if checked:
            self._apply_preset(code)

    def _select_preset(self, name: str) -> None:
        radio = self._preset_radios.get(name) or self._preset_radios["normal"]
        radio.setChecked(True)
        self._apply_preset(name)

    def _on_advanced_toggled(self, checked: bool) -> None:
        self._advanced_widget.setVisible(checked)
        self._btn_advanced.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )

    def _on_engine_changed(self, index: int) -> None:
        self._update_perf_enabled()

    def _update_perf_enabled(self) -> None:
        """Preset per tutti i motori; 'Massima velocità' e test solo per LLM."""
        engine = self._engine_combo.currentData() or "google"
        llm = engine == "llm"
        box = getattr(self, "_box_perf", None)
        if box is not None:
            box.setEnabled(True)
        fastest = self._preset_radios.get("fastest")
        if fastest is not None:
            fastest.setEnabled(llm)
            fastest.setToolTip(
                "" if llm else T("settings.performance.preset.fastest.llm_only")
            )
            desc = self._preset_descs.get("fastest")
            if desc is not None:
                desc.setEnabled(llm)
        self._btn_proxy_test.setEnabled(llm)

    def _on_test_provider(self):
        """Avvia la prova provider in background e mostra l'esito."""
        parent = self.parent()
        autostart = self._proxy_autostart_check.isChecked()
        port = int(self._proxy_port_spin.value())
        if autostart:
            base = f"http://127.0.0.1:{port}/v1"
            ensure = getattr(parent, "ensure_proxy", None)
            if callable(ensure):
                ensure(port)
        else:
            base = self._selected_base_url() or clone_engine.DEFAULT_BASE_URL
        model = self._llm_model_combo.currentData() or clone_engine.DEFAULT_MODEL
        key = (os.environ.get(keystore.ENV_VAR) or "").strip()
        self._proxy_test_result.setText(T("settings.proxy.test.running"))
        self._proxy_test_result.setStyleSheet(_status_style("text5"))
        thread = ProviderTestThread(base, model, key, self)
        thread.result.connect(self._on_provider_result)
        self._proxy_test_thread = thread  # evita il garbage collector
        thread.start()

    def _on_provider_result(self, outcome: dict):
        if outcome.get("ok"):
            self._proxy_test_result.setText(
                T(
                    "settings.proxy.test.ok",
                    provider=outcome.get("provider") or "?",
                    model=outcome.get("model") or "?",
                )
            )
            self._proxy_test_result.setStyleSheet(_status_style("ok"))
        else:
            self._proxy_test_result.setText(
                T("settings.proxy.test.error", error=outcome.get("error") or "?")
            )
            self._proxy_test_result.setStyleSheet(_status_style("err"))

    def _populate_llm_combos(self, model_value=None, base_value=None):
        """Riempe i menu modello/base URL preservando la selezione e i custom."""
        model = (
            model_value
            if model_value is not None
            else self._llm_model_combo.currentData()
        )
        self._llm_model_combo.clear()
        self._llm_model_combo.addItem(
            T("settings.llm.model.mercury"), "inception/mercury-2.5"
        )
        self._llm_model_combo.addItem(
            T("settings.llm.model.gptoss"), "openai/gpt-oss-120b"
        )
        self._llm_model_combo.addItem(
            T("settings.llm.model.luna"), "openai/gpt-6-luna"
        )
        self._llm_model_combo.addItem(T("settings.llm.model.default"), "")
        if model and self._llm_model_combo.findData(model) < 0:
            self._llm_model_combo.addItem(model, model)
        self._llm_model_combo.setCurrentIndex(
            max(0, self._llm_model_combo.findData(model or ""))
        )

        base = (
            base_value
            if base_value is not None
            else self._llm_base_combo.currentData()
        )
        self._llm_base_combo.clear()
        self._llm_base_combo.addItem(T("settings.llm.base.openrouter"), "")
        self._llm_base_combo.addItem(T("settings.llm.base.proxy"), "__proxy__")
        if base and base != "__proxy__" and self._llm_base_combo.findData(base) < 0:
            self._llm_base_combo.addItem(base, base)
        self._llm_base_combo.setCurrentIndex(
            max(0, self._llm_base_combo.findData(base or ""))
        )

    def _selected_base_url(self) -> str:
        """Base URL effettiva dalla selezione corrente (proxy o preset)."""
        selected = self._llm_base_combo.currentData() or ""
        if selected == "__proxy__":
            port = int(self._proxy_port_spin.value())
            return f"http://127.0.0.1:{port}/v1"
        return selected

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
        idx = self._theme_combo.findData(cfg.get("theme", "dark"))
        self._theme_combo.setCurrentIndex(max(0, idx))
        self._notify_check.setChecked(bool(cfg.get("notify_on_finish", True)))
        self._sound_check.setChecked(bool(cfg.get("notify_sound", True)))
        self._sleep_check.setChecked(bool(cfg.get("prevent_sleep", True)))
        self._llm_workers_spin.setValue(
            int(cfg.get("llm_pool_workers", 4) or 4)
        )
        # Porta proxy dal config, poi applica il preset (che governa i campi).
        self._proxy_port_spin.setValue(int(cfg.get("llm_proxy_port", 8790) or 8790))
        preset = str(cfg.get("performance_preset", "normal") or "normal")
        self._select_preset(preset)
        self._numeric_lists_check.setChecked(bool(cfg.get("numeric_lists", False)))
        # Prompt: un valore salvato non vuoto vince sul default del preset.
        saved_prompt = str(cfg.get("llm_system_prompt", "") or "").strip()
        if saved_prompt:
            self._llm_prompt_edit.setText(saved_prompt)
        # I flag B2 sono seedati OFF dai preset (precisione): non si ripristina
        # un eventuale valore vecchio/errato salvato in config.
        self._update_perf_enabled()

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
        self._btn_install_engine.setText(T("settings.clone.install"))
        self._btn_install_engine.setToolTip(T("settings.clone.install.tip"))
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
        self._lbl_api.setText(T("settings.clone.apikey"))
        self._chk_api_show.setText(T("apikey.show"))
        self._btn_api_verify.setText(T("apikey.verify"))
        self._btn_api_clear.setText(T("apikey.clear"))
        self._api_source.setText(_llm_key_source_text())
        self._api_edit.setPlaceholderText(T("apikey.placeholder"))
        self._md_check.setText(T("settings.text.md"))
        self._header_check.setText(T("settings.text.header"))
        self._save_edits_check.setText(T("settings.edits.save"))
        self._btn_clear_edits.setText(T("settings.edits.clear"))
        self._lbl_zoom.setText(T("settings.view.zoom"))
        self._resume_check.setText(T("settings.behavior.resume"))
        self._tab_check.setText(T("settings.behavior.tab"))
        self._box_appearance.setTitle(T("settings.group.appearance"))
        self._lbl_theme.setText(T("settings.appearance.theme"))
        cur_theme = self._theme_combo.currentData()
        self._theme_combo.clear()
        for code in ("dark", "light", "system"):
            self._theme_combo.addItem(T(f"settings.theme.{code}"), code)
        if cur_theme is not None:
            tidx = self._theme_combo.findData(cur_theme)
            self._theme_combo.setCurrentIndex(max(0, tidx))
        self._box_notify.setTitle(T("settings.group.notifications"))
        self._notify_check.setText(T("settings.notify.on_finish"))
        self._sound_check.setText(T("settings.notify.sound"))
        self._btn_test_sound.setText(T("settings.notify.test"))
        self._sleep_check.setText(T("settings.notify.prevent_sleep"))
        self._box_perf.setTitle(T("settings.group.performance"))
        for code in ("normal", "fast", "fastest"):
            self._preset_radios[code].setText(
                T(f"settings.performance.preset.{code}.label")
            )
            self._preset_descs[code].setText(
                T(f"settings.performance.preset.{code}.desc")
            )
        self._btn_advanced.setText(T("settings.performance.advanced"))
        self._lbl_llm_workers.setText(T("settings.performance.llm_workers"))
        self._llm_workers_spin.setToolTip(
            T("settings.performance.llm_workers.tip")
        )
        self._fast_engine_check.setText(T("settings.performance.fast_engine"))
        self._fast_engine_check.setToolTip(
            T("settings.performance.fast_engine.tip")
        )
        self._fast_flags_check.setText(T("settings.performance.fast_flags"))
        self._fast_flags_check.setToolTip(
            T("settings.performance.fast_flags.tip")
        )
        self._numeric_lists_check.setText(
            T("settings.performance.numeric_lists")
        )
        self._numeric_lists_check.setToolTip(
            T("settings.performance.numeric_lists.tip")
        )
        self._fast_worker_check.setText(T("settings.performance.fast_worker"))
        self._fast_worker_check.setToolTip(
            T("settings.performance.fast_worker.tip")
        )
        self._lbl_reasoning.setText(T("settings.performance.reasoning_effort"))
        self._reasoning_combo.setToolTip(
            T("settings.performance.reasoning_effort.tip")
        )
        self._json_mode_check.setText(T("settings.performance.json_mode"))
        self._json_mode_check.setToolTip(T("settings.performance.json_mode.tip"))
        self._lbl_llm_model.setText(T("settings.llm.model"))
        self._llm_model_combo.setToolTip(T("settings.llm.model.tip"))
        self._lbl_llm_base.setText(T("settings.llm.base_url"))
        self._llm_base_combo.setToolTip(T("settings.llm.base_url.tip"))
        self._lbl_llm_prompt.setText(T("settings.llm.system_prompt"))
        self._llm_prompt_edit.setToolTip(T("settings.llm.system_prompt.tip"))
        self._populate_llm_combos()
        self._proxy_autostart_check.setText(T("settings.proxy.autostart"))
        self._proxy_autostart_check.setToolTip(T("settings.proxy.autostart.tip"))
        self._lbl_proxy_port.setText(T("settings.proxy.port"))
        self._proxy_port_spin.setToolTip(T("settings.proxy.port.tip"))
        self._btn_proxy_test.setText(T("settings.proxy.test"))
        self._btn_proxy_test.setToolTip(T("settings.proxy.test.tip"))
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
            "openrouter_api_key": self._api_edit.text().strip(),
            "theme": self._theme_combo.currentData() or "dark",
            "notify_on_finish": bool(self._notify_check.isChecked()),
            "notify_sound": bool(self._sound_check.isChecked()),
            "prevent_sleep": bool(self._sleep_check.isChecked()),
            "llm_pool_workers": int(self._llm_workers_spin.value()),
            "performance_preset": next(
                (code for code, radio in self._preset_radios.items() if radio.isChecked()),
                "normal",
            ),
            "fast_engine": bool(self._fast_engine_check.isChecked()),
            "fast_flags": bool(self._fast_flags_check.isChecked()),
            "numeric_lists": bool(self._numeric_lists_check.isChecked()),
            "fast_worker": bool(self._fast_worker_check.isChecked()),
            "llm_reasoning_effort": self._reasoning_combo.currentData() or "",
            "llm_json_mode": bool(self._json_mode_check.isChecked()),
            "llm_model": self._llm_model_combo.currentData() or "",
            "llm_base_url": self._selected_base_url(),
            "llm_system_prompt": self._llm_prompt_edit.text().strip(),
            "llm_proxy_autostart": bool(self._proxy_autostart_check.isChecked()),
            "llm_proxy_port": int(self._proxy_port_spin.value()),
        }


# ── Wizard di esportazione (stile del wizard del servizio) ──────────────────
#
# Ricalca il modal multi-step di ``noesis-pdf-cloner-service`` (5 passi
# numerati, footer Annulla/Indietro/Avanti). Il desktop non ha un backend di
# stima, quindi il box del passo "Motore" usa un tempo indicativo. I colori
# vengono dal tema attivo (``theme.wizard_qss()``): chiaro o scuro.


class ExportWizardDialog(QDialog):
    """Wizard multi-step per i parametri di esportazione.

    Stile e flusso sono ricalcati dal wizard del servizio
    (``noesis-pdf-cloner-service``): cinque passi numerati (File, Pagine,
    Lingue, Motore, Output), footer Annulla/Indietro/Avanti. Il dialogo
    raccoglie soltanto i parametri; MainWindow esegue traduzione ed export.
    """

    STEPS = ("file", "pages", "langs", "engine", "output")

    def __init__(
        self,
        engine: clone_engine.CloneEngine,
        engine_name: str,
        target_lang: str,
        source_lang: str,
        current_page: int,
        page_count: int,
        pdf_path=None,
        parent=None,
    ):
        super().__init__(parent)
        self._engine = engine
        self._engine_name = engine_name
        self._target_lang = target_lang
        self._source_lang = source_lang
        self._page_count = max(int(page_count), 1)
        self._current_page = min(max(int(current_page), 0), self._page_count - 1)
        self._pdf_path = Path(pdf_path) if pdf_path else None
        saved_dir = get_setting("export_dir", "") or ""
        self._folder: Path | None = (
            Path(saved_dir) if saved_dir and Path(saved_dir).is_dir() else None
        )
        self._step = 0
        self._translate_touched = False
        self._path_touched = False

        # Etichette /PageLabels (numero stampato) e anteprime.
        self._labels: list[str] = []
        if self._pdf_path is not None and _has_pymupdf:
            try:
                self._labels = clone_engine.CloneEngine.page_labels(self._pdf_path)
            except Exception:
                self._labels = []
        self._preview_doc = None
        self._preview_cache: dict[tuple[int, int], QPixmap] = {}
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(150)
        self._preview_timer.timeout.connect(self._update_page_preview)

        self.setWindowTitle(T("export.wizard.title"))
        self.setMinimumSize(820, 700)
        # Tasto "riduci a icona" sul wizard (utile sulle finestre ridotte).
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setStyleSheet(theme.wizard_qss())

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())
        root.addWidget(self._build_steps_bar())

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_step_file())
        self._stack.addWidget(self._build_step_pages())
        self._stack.addWidget(self._build_step_langs())
        self._stack.addWidget(self._build_step_engine())
        self._stack.addWidget(self._build_step_output())
        root.addWidget(self._stack, 1)
        root.addWidget(self._build_footer())

        # Ricalcolo globale quando cambia una scelta che incide su
        # cache/stima/nome file.
        for btn in self._engine_buttons.values():
            btn.toggled.connect(self._refresh_all)
        self._src_combo.currentIndexChanged.connect(self._refresh_all)
        self._dst_combo.currentIndexChanged.connect(self._refresh_all)

        self._set_step(0)

    # ── costruzione dei pezzi ─────────────────────────────────────────

    def _build_header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("wizHeader")
        lay = QHBoxLayout(header)
        lay.setContentsMargins(18, 14, 14, 14)
        title = QLabel(T("export.wizard.title"))
        title.setObjectName("wizTitle")
        lay.addWidget(title)
        lay.addStretch(1)
        close = QPushButton("✕")
        close.setObjectName("wizX")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.reject)
        lay.addWidget(close)
        return header

    def _build_steps_bar(self) -> QWidget:
        self._steps_bar = QWidget()
        self._steps_bar.setObjectName("wizSteps")
        self._steps_layout = QHBoxLayout(self._steps_bar)
        self._steps_layout.setContentsMargins(18, 12, 18, 12)
        self._steps_layout.setSpacing(8)
        return self._steps_bar

    def _render_steps(self):
        while self._steps_layout.count():
            item = self._steps_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, key in enumerate(self.STEPS):
            if index:
                sep = QFrame()
                sep.setObjectName("wizSep")
                sep.setFixedSize(16, 1)
                self._steps_layout.addWidget(sep)
            item = QWidget()
            item.setObjectName("wizStep")
            lay = QHBoxLayout(item)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(7)
            num = QLabel(str(index + 1))
            num.setObjectName("wizStepNum")
            num.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name = QLabel(T(f"export.wizard.step.{key}"))
            name.setObjectName("wizStepName")
            lay.addWidget(num)
            lay.addWidget(name)
            state = "on" if index == self._step else ("done" if index < self._step else "off")
            item.setProperty("state", state)
            item.style().unpolish(item)
            item.style().polish(item)
            self._steps_layout.addWidget(item)
        self._steps_layout.addStretch(1)

    def _build_step_file(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)
        title = QLabel(T("export.wizard.file.title"))
        title.setObjectName("wizH3")
        hint = QLabel(T("export.wizard.file.hint"))
        hint.setObjectName("wizHint")
        hint.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(hint)

        self._file_card = QWidget()
        self._file_card.setObjectName("wizCard")
        card_lay = QHBoxLayout(self._file_card)
        card_lay.setContentsMargins(14, 14, 14, 14)
        card_lay.setSpacing(14)
        self._thumb = QLabel()
        self._thumb.setObjectName("wizThumb")
        self._thumb.setFixedSize(94, 124)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_lay.addWidget(self._thumb)
        self._file_info = QLabel("")
        self._file_info.setWordWrap(True)
        card_lay.addWidget(self._file_info, 1)
        lay.addWidget(self._file_card)
        lay.addStretch(1)

        self._refresh_file_step()
        return w

    def _refresh_file_step(self):
        if self._pdf_path is None:
            self._file_info.setText(T("export.wizard.file.none"))
            return
        try:
            size = self._pdf_path.stat().st_size / 1048576.0
        except OSError:
            size = 0.0
        self._file_info.setText(
            T(
                "export.wizard.file.info",
                name=self._pdf_path.name,
                pages=self._page_count,
                size=f"{size:.1f}",
            )
        )
        pix = self._render_source_thumb(self._current_page)
        if pix is not None:
            self._thumb.setPixmap(pix)

    def _ensure_preview_doc(self):
        """Apre (una volta) il PDF per le anteprime e lo tiene aperto."""
        if self._preview_doc is not None:
            return self._preview_doc
        if not _has_pymupdf or self._pdf_path is None:
            return None
        try:
            self._preview_doc = pymupdf.open(str(self._pdf_path))
        except Exception:
            self._preview_doc = None
        return self._preview_doc

    def _page_pixmap(self, page: int, width: int):
        """Render della pagina ``page`` (0-based) larga ``width`` px.

        Usa il documento tenuto aperto e una piccola cache; renderizza alla
        ``devicePixelRatio`` per restare nitido su schermi HiDPI.
        """
        doc = self._ensure_preview_doc()
        if doc is None or len(doc) == 0:
            return None
        index = min(max(int(page), 0), len(doc) - 1)
        try:
            ratio = float(self.devicePixelRatioF())
        except Exception:
            ratio = 1.0
        px_width = max(1, int(round(width * ratio)))
        key = (index, px_width)
        cached = self._preview_cache.get(key)
        if cached is not None:
            return cached
        try:
            pdf_page = doc[index]
            zoom = max(0.05, px_width / max(pdf_page.rect.width, 1.0))
            pix = pdf_page.get_pixmap(
                matrix=pymupdf.Matrix(zoom, zoom), alpha=False
            )
            img = QImage(
                pix.samples, pix.width, pix.height, pix.stride,
                QImage.Format.Format_RGB888,
            )
            result = QPixmap.fromImage(img)
        except Exception:
            return None
        self._preview_cache[key] = result
        while len(self._preview_cache) > 6:
            self._preview_cache.pop(next(iter(self._preview_cache)))
        return result

    def _render_source_thumb(self, page: int, width: int = 94):
        return self._page_pixmap(page, width)

    def _close_preview_doc(self):
        if self._preview_doc is not None:
            with contextlib.suppress(Exception):
                self._preview_doc.close()
            self._preview_doc = None

    def closeEvent(self, event):  # noqa: N802 — override Qt
        self._close_preview_doc()
        super().closeEvent(event)

    def done(self, result):  # noqa: N802 — override Qt
        self._close_preview_doc()
        super().done(result)

    @staticmethod
    def _mode_sep() -> QFrame:
        """Separatore verticale sottile tra i gruppi della riga dei modi."""
        sep = QFrame()
        sep.setObjectName("wizModeSep")
        sep.setFixedWidth(1)
        return sep

    def _build_preview_column(self):
        """Colonna anteprima: miniatura grande + didascalia sotto."""
        col = QWidget()
        cl = QVBoxLayout(col)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(6)
        thumb = QLabel()
        thumb.setObjectName("wizThumbBig")
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cap = QLabel("")
        cap.setObjectName("wizHint")
        cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cap.setWordWrap(True)
        cl.addWidget(thumb, 0, Qt.AlignmentFlag.AlignLeft)
        cl.addWidget(cap)
        return col, thumb, cap

    def _build_step_pages(self) -> QWidget:
        pane = QWidget()
        pane.setObjectName("wizPane")
        lay = QVBoxLayout(pane)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(8)
        title = QLabel(T("export.wizard.pages.title"))
        title.setObjectName("wizH3")
        hint = QLabel(T("export.wizard.pages.hint"))
        hint.setObjectName("wizHint")
        hint.setWordWrap(True)
        numbering = QLabel(T("export.wizard.pages.numbering"))
        numbering.setObjectName("wizHint")
        numbering.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(hint)
        lay.addWidget(numbering)

        self._rad_current = QRadioButton(T("export.mode.current"))
        self._rad_range = QRadioButton(T("export.mode.range"))
        self._rad_free = QRadioButton(T("export.mode.free"))
        self._rad_current.setChecked(True)
        for rb in (self._rad_current, self._rad_range, self._rad_free):
            rb.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._rad_current)
        self._mode_group.addButton(self._rad_range)
        self._mode_group.addButton(self._rad_free)

        self._lbl_from = QLabel(T("export.range.from.short"))
        self._lbl_to = QLabel(T("export.range.to.short"))
        self._from_spin = QSpinBox()
        self._to_spin = QSpinBox()
        for sp in (self._from_spin, self._to_spin):
            sp.setObjectName("wizSpin")
            sp.setRange(1, self._page_count)
            sp.setValue(self._current_page + 1)
            sp.setAlignment(Qt.AlignmentFlag.AlignRight)
            sp.setMinimumWidth(56)
            sp.setMaximumWidth(62)
        self._from_label = QLabel("")
        self._from_label.setObjectName("wizHint")
        self._to_label = QLabel("")
        self._to_label.setObjectName("wizHint")

        self._free_edit = QLineEdit()
        self._free_edit.setPlaceholderText(T("export.free.placeholder"))
        self._free_edit.setClearButtonEnabled(True)
        self._free_edit.setMinimumWidth(160)

        # Cluster "Intervallo" (label + numbox): mostrato solo in quel modo.
        self._range_cluster = QWidget()
        rc = QHBoxLayout(self._range_cluster)
        rc.setContentsMargins(0, 0, 0, 0)
        rc.setSpacing(6)
        rc.addWidget(self._lbl_from)
        rc.addWidget(self._from_spin)
        rc.addWidget(self._lbl_to)
        rc.addWidget(self._to_spin)

        # ── Riga dei modi: compare SOLO il controllo del modo attivo, così
        # la riga non supera mai la larghezza e il campo libero ha spazio. ──
        self._mode_row = QWidget()
        mr = QHBoxLayout(self._mode_row)
        mr.setContentsMargins(0, 4, 0, 0)
        mr.setSpacing(8)
        mr.addWidget(self._rad_current)
        mr.addWidget(self._mode_sep())
        mr.addWidget(self._rad_range)
        mr.addWidget(self._range_cluster)
        mr.addWidget(self._mode_sep())
        mr.addWidget(self._rad_free)
        mr.addWidget(self._free_edit, 1)
        lay.addWidget(self._mode_row)

        self._free_hint = QLabel(T("export.free.hint"))
        self._free_hint.setObjectName("wizHint")
        self._free_hint.setWordWrap(True)
        lay.addWidget(self._free_hint)

        self._free_error = QLabel("")
        self._free_error.setObjectName("wizError")
        self._free_error.setWordWrap(True)
        self._free_error.hide()
        lay.addWidget(self._free_error)

        # ── Anteprime: 1 (corrente) oppure prima/ultima (intervallo/pagine) ─
        # A destra delle miniature una colonna info usa lo spazio vuoto.
        self._preview_row = QWidget()
        row = QHBoxLayout(self._preview_row)
        row.setContentsMargins(0, 6, 0, 0)
        row.setSpacing(18)

        self._col_current, self._thumb_current, self._cap_current = (
            self._build_preview_column()
        )
        self._col_first, self._thumb_first, self._cap_first = (
            self._build_preview_column()
        )
        self._col_last, self._thumb_last, self._cap_last = (
            self._build_preview_column()
        )
        row.addWidget(self._col_current, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self._col_first, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(self._col_last, 0, Qt.AlignmentFlag.AlignTop)

        self._count_lbl = QLabel("")
        self._count_lbl.setObjectName("wizHint")
        self._count_lbl.setWordWrap(True)
        self._preview_note = QLabel("")
        self._preview_note.setObjectName("wizHint")
        self._preview_note.setWordWrap(True)
        self._preview_info = QWidget()
        info = QVBoxLayout(self._preview_info)
        info.setContentsMargins(0, 0, 0, 0)
        info.setSpacing(6)
        info.addWidget(self._count_lbl)
        info.addWidget(self._from_label)
        info.addWidget(self._to_label)
        info.addWidget(self._preview_note)
        info.addStretch(1)
        row.addWidget(self._preview_info, 1, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self._preview_row)

        self._ready_lbl = QLabel("")
        self._ready_lbl.setObjectName("wizHint")
        self._ready_lbl.setContentsMargins(2, 4, 0, 0)
        self._ready_lbl.setWordWrap(True)
        lay.addWidget(self._ready_lbl)
        lay.addStretch(1)

        self._rad_current.toggled.connect(self._on_mode_changed)
        self._rad_range.toggled.connect(self._on_mode_changed)
        self._rad_free.toggled.connect(self._on_mode_changed)
        self._from_spin.valueChanged.connect(self._on_pages_value_changed)
        self._to_spin.valueChanged.connect(self._on_pages_value_changed)
        self._free_edit.textChanged.connect(self._on_free_changed)

        # Stato del campo libero (parse live, aggiornato in _refresh_all).
        self._free_pages: list[int] = []
        self._free_error_key: str = ""
        self._free_error_params: dict = {}

        # Rete di sicurezza: su schermi bassi si scorre, ma ogni pagina resta
        # INTERA (la miniatura ha l'aspetto reale della pagina).
        scroll = QScrollArea()
        scroll.setObjectName("wizScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.viewport().setObjectName("wizViewport")
        scroll.setWidget(pane)
        return scroll

    def _build_step_langs(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)
        title = QLabel(T("export.wizard.langs.title"))
        title.setObjectName("wizH3")
        hint = QLabel(T("export.wizard.langs.hint"))
        hint.setObjectName("wizHint")
        hint.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(12)
        src_col = QVBoxLayout()
        src_col.setSpacing(6)
        src_col.addWidget(QLabel(T("settings.lang.source")))
        self._src_combo = QComboBox()
        for code, (flag, name) in TRANSLATION_LANGUAGES.items():
            self._src_combo.addItem(f"{flag} {name}", code)
        sidx = self._src_combo.findData(self._source_lang)
        self._src_combo.setCurrentIndex(max(0, sidx))
        src_col.addWidget(self._src_combo)
        row.addLayout(src_col, 1)

        dst_col = QVBoxLayout()
        dst_col.setSpacing(6)
        dst_col.addWidget(QLabel(T("settings.lang.target")))
        self._dst_combo = QComboBox()
        for code, (flag, name) in TRANSLATION_LANGUAGES.items():
            if code == "auto":
                continue
            self._dst_combo.addItem(f"{flag} {name}", code)
        didx = self._dst_combo.findData(self._target_lang)
        self._dst_combo.setCurrentIndex(max(0, didx))
        dst_col.addWidget(self._dst_combo)
        row.addLayout(dst_col, 1)
        lay.addLayout(row)
        lay.addStretch(1)
        return w

    def _build_step_engine(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)
        title = QLabel(T("export.wizard.engine.title"))
        title.setObjectName("wizH3")
        hint = QLabel(T("export.wizard.engine.hint"))
        hint.setObjectName("wizHint")
        hint.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(hint)

        self._engine_group = QButtonGroup(self)
        self._engine_group.setExclusive(True)
        self._engine_buttons: dict[str, QPushButton] = {}
        for code in CLONE_ENGINES:
            btn = QPushButton(T(f"engine.option.{code}"))
            btn.setObjectName("wizCardOpt")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("code", code)
            if code == self._engine_name:
                btn.setChecked(True)
            self._engine_group.addButton(btn)
            self._engine_buttons[code] = btn
            lay.addWidget(btn)
        if not any(b.isChecked() for b in self._engine_buttons.values()):
            first = next(iter(self._engine_buttons.values()))
            first.setChecked(True)

        self._est_lbl = QLabel("")
        self._est_lbl.setObjectName("wizEst")
        self._est_lbl.setWordWrap(True)
        lay.addWidget(self._est_lbl)

        self._chk_translate = QCheckBox(T("export.translate_missing"))
        self._chk_translate.setChecked(False)
        # Finché l'utente non tocca la casella, viene attivata da sola quando
        # le pagine scelte non sono tutte in cache.
        self._translate_touched = False
        self._chk_translate.toggled.connect(self._on_translate_toggled)
        lay.addWidget(self._chk_translate)
        lay.addStretch(1)
        return w

    def _build_step_output(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(10)
        title = QLabel(T("export.wizard.output.title"))
        title.setObjectName("wizH3")
        hint = QLabel(T("export.wizard.output.hint"))
        hint.setObjectName("wizHint")
        hint.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(hint)

        fmt_lbl = QLabel(T("export.group.format"))
        fmt_lbl.setObjectName("wizHint")
        lay.addWidget(fmt_lbl)
        seg = QHBoxLayout()
        seg.setSpacing(8)
        self._btn_merged = QPushButton(T("export.format.merged"))
        self._btn_zip = QPushButton(T("export.format.zip"))
        self._btn_folder = QPushButton(T("export.format.folder"))
        self._format_group = QButtonGroup(self)
        self._format_group.setExclusive(True)
        for btn in (self._btn_merged, self._btn_zip, self._btn_folder):
            btn.setObjectName("wizSeg")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._format_group.addButton(btn)
            seg.addWidget(btn)
        self._btn_merged.setChecked(True)
        seg.addStretch(1)
        lay.addLayout(seg)

        # Cartella di destinazione: si sceglie col pulsante (campo in sola lettura).
        folder_lbl = QLabel(T("export.wizard.output.folder"))
        folder_lbl.setObjectName("wizHint")
        lay.addWidget(folder_lbl)
        frow = QHBoxLayout()
        frow.setSpacing(8)
        self._folder_edit = QLineEdit()
        self._folder_edit.setReadOnly(True)
        self._folder_edit.setText(str(self._output_dir()))
        choose_dir = QPushButton(T("export.wizard.output.choose_folder"))
        choose_dir.setObjectName("wizBtn")
        choose_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        choose_dir.clicked.connect(self._browse_folder)
        frow.addWidget(self._folder_edit, 1)
        frow.addWidget(choose_dir)
        lay.addLayout(frow)

        # Nome file (o nome cartella nel formato "pagine singole in una cartella").
        self._name_lbl = QLabel(T("export.wizard.output.filename"))
        self._name_lbl.setObjectName("wizHint")
        lay.addWidget(self._name_lbl)
        self._path_edit = QLineEdit()
        self._path_edit.textEdited.connect(self._on_path_edited)
        lay.addWidget(self._path_edit)

        self._summary = QLabel("")
        self._summary.setObjectName("wizSum")
        self._summary.setWordWrap(True)
        self._summary.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self._summary)
        lay.addStretch(1)

        self._btn_merged.toggled.connect(self._on_output_changed)
        self._btn_zip.toggled.connect(self._on_output_changed)
        self._btn_folder.toggled.connect(self._on_output_changed)
        self._update_path()
        return w

    def _build_footer(self) -> QWidget:
        footer = QWidget()
        footer.setObjectName("wizFooter")
        lay = QHBoxLayout(footer)
        lay.setContentsMargins(18, 12, 18, 12)
        lay.setSpacing(10)
        self._lbl_error = QLabel("")
        self._lbl_error.setObjectName("wizError")
        lay.addWidget(self._lbl_error)
        lay.addStretch(1)
        self._btn_cancel = QPushButton(T("settings.cancel"))
        self._btn_cancel.setObjectName("wizGhost")
        self._btn_cancel.clicked.connect(self.reject)
        self._btn_back = QPushButton(T("export.wizard.back"))
        self._btn_back.setObjectName("wizBtn")
        self._btn_back.clicked.connect(self._go_back)
        self._btn_next = QPushButton(T("export.wizard.next"))
        self._btn_next.setObjectName("wizPrimary")
        self._btn_next.clicked.connect(self._go_next)
        for btn in (self._btn_cancel, self._btn_back, self._btn_next):
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            lay.addWidget(btn)
        return footer

    # ── navigazione ───────────────────────────────────────────────────

    def _set_step(self, step: int):
        self._step = max(0, min(int(step), len(self.STEPS) - 1))
        self._stack.setCurrentIndex(self._step)
        self._render_steps()
        self._lbl_error.setText("")
        self._btn_back.setVisible(self._step > 0)
        self._btn_next.setText(
            T("export.wizard.start")
            if self._step == len(self.STEPS) - 1
            else T("export.wizard.next")
        )
        if self._step == 0:
            self._refresh_file_step()
        elif self._step == 1:
            self._update_page_preview()
        self._refresh_all()

    def _go_back(self):
        self._set_step(self._step - 1)

    def _go_next(self):
        if self._step == 1 and self.is_free() and self._free_error_key:
            self._lbl_error.setText(self._free_error_text())
            return
        if self._step == len(self.STEPS) - 1:
            if not self._path_edit.text().strip():
                self._path_edit.setText(self._default_filename())
            # Ricorda la cartella scelta per i prossimi export.
            set_setting("export_dir", str(self._output_dir()))
            save_config()
            self.accept()
            return
        self._set_step(self._step + 1)

    # ── stato / ricalcoli ─────────────────────────────────────────────

    def _on_mode_changed(self, *_):
        # La riga resta la stessa: cambia solo quale miniatura è visibile.
        self._refresh_all()

    def _on_pages_value_changed(self, *_):
        # Toccare uno spin significa voler esportare un intervallo.
        if not self._rad_range.isChecked():
            self._rad_range.setChecked(True)  # → _on_mode_changed → refresh
            return
        self._refresh_all()

    def _on_free_changed(self, *_):
        # Scrivere nel campo libero significa voler esportare un elenco.
        if self._free_edit.text().strip() and not self._rad_free.isChecked():
            self._rad_free.setChecked(True)  # → _on_mode_changed → refresh
            return
        self._refresh_all()

    def _parse_free(self):
        """Aggiorna ``_free_pages`` e l'eventuale errore del campo libero."""
        if not hasattr(self, "_free_edit"):
            return
        try:
            self._free_pages = page_spec.parse_pages(
                self._free_edit.text(), self._page_count
            )
            self._free_error_key = ""
            self._free_error_params = {}
        except page_spec.PageSpecError as exc:
            self._free_pages = []
            self._free_error_key = f"export.free.err.{exc.code}"
            self._free_error_params = dict(exc.params)

    def _free_error_text(self) -> str:
        if not self._free_error_key:
            return ""
        return T(self._free_error_key, **self._free_error_params)

    def _update_free_feedback(self):
        if not hasattr(self, "_free_error"):
            return
        self._free_error.setText(self._free_error_text())
        self._free_error.setVisible(bool(self._free_error_key))
        if hasattr(self, "_free_hint"):
            self._free_hint.setVisible(self.is_free())

    def _update_mode_controls(self):
        """Mostra solo il controllo del modo attivo (riga compatta).

        In "Pagina corrente" non c'è nessun controllo; in "Intervallo" solo i
        due numbox; in "Pagine" solo il campo libero, che prende tutto lo spazio.
        """
        if not hasattr(self, "_range_cluster"):
            return
        self._range_cluster.setVisible(self._rad_range.isChecked())
        self._free_edit.setVisible(self._rad_free.isChecked())

    def _update_next_enabled(self):
        if not hasattr(self, "_btn_next"):
            return
        blocked = self._step == 1 and self.is_free() and bool(self._free_error_key)
        self._btn_next.setEnabled(not blocked)

    def _page_label(self, page: int) -> str:
        """Etichetta stampata (/PageLabels) della pagina 0-based, o numero fisico."""
        if 0 <= page < len(self._labels):
            return self._labels[page]
        return str(page + 1)

    def _caption(self, page: int) -> str:
        physical = page + 1
        label = self._page_label(page)
        if label and label != str(physical):
            return T("export.wizard.preview.caption", n=physical, label=label)
        return T("export.wizard.preview.caption_plain", n=physical)

    def _set_thumb(self, label: QLabel, pix: QPixmap | None):
        if pix is None:
            label.clear()
            return
        label.setPixmap(
            pix.scaled(
                label.width(),
                label.height(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _update_numbering_hints(self):
        """Etichette live accanto agli spin + nota quando la numerazione differisce."""
        if not hasattr(self, "_from_label"):
            return
        pages = self.chosen_pages()
        if not pages:
            self._from_label.setText("")
            self._to_label.setText("")
            self._preview_note.setText("")
            return
        lo, hi = min(pages), max(pages)

        def _note(page: int) -> str:
            label = self._page_label(page)
            if label and label != str(page + 1):
                return T("export.wizard.preview.spin_label", label=label)
            return ""

        if self.is_current():
            # In "Pagina corrente" gli spin non rappresentano la selezione.
            self._from_label.setText("")
            self._to_label.setText("")
        else:
            self._from_label.setText(_note(lo))
            self._to_label.setText(_note(hi))
        differs = any(
            self._page_label(p) and self._page_label(p) != str(p + 1)
            for p in pages
        )
        self._preview_note.setText(
            T("export.wizard.preview.label_diff") if differs else ""
        )

    def _page_box(self, page: int) -> tuple[int, int]:
        """Box miniatura (larghezza, altezza) con l'aspetto reale della pagina.

        La larghezza deriva dall'altezza fissa ``_PREVIEW_H`` e dal rapporto
        della pagina, così l'immagine riempie il box e resta **intera**.
        """
        height = _PREVIEW_H
        doc = self._ensure_preview_doc()
        width = int(round(height * 0.707))  # A4 di fallback
        if doc is not None and len(doc) > 0:
            try:
                rect = doc[min(max(int(page), 0), len(doc) - 1)].rect
                width = int(round(height * rect.width / max(rect.height, 1.0)))
            except Exception:
                pass
        width = max(120, min(width, 430))
        return (width, height)

    def _paint_thumb(self, thumb: QLabel, cap: QLabel, page: int):
        """Dimensiona il box sull'aspetto della pagina e disegna miniatura+didascalia."""
        width, height = self._page_box(page)
        thumb.setFixedSize(width, height)
        cap.setFixedWidth(width)
        self._set_thumb(
            thumb, self._page_pixmap(page, width) if _has_pymupdf else None
        )
        cap.setText(
            self._caption(page) if _has_pymupdf
            else T("export.wizard.preview.none")
        )

    def _update_page_preview(self):
        """Anteprime grandi: la corrente (1) oppure prima/ultima (intervallo/pagine)."""
        if not hasattr(self, "_thumb_first"):
            return
        current = self._rad_current.isChecked()
        self._col_current.setVisible(current)
        self._col_first.setVisible(not current)
        self._col_last.setVisible(not current)

        if current:
            self._paint_thumb(
                self._thumb_current, self._cap_current, self._current_page
            )
            self._count_lbl.setText("")
            return

        pages = self.chosen_pages()
        if not pages:
            self._thumb_first.clear()
            self._cap_first.setText("")
            self._thumb_last.clear()
            self._cap_last.setText("")
            self._count_lbl.setText("")
            return
        first, last = min(pages), max(pages)
        self._paint_thumb(self._thumb_first, self._cap_first, first)
        self._paint_thumb(self._thumb_last, self._cap_last, last)
        if len(pages) > 1:
            self._count_lbl.setText(
                T(
                    "export.wizard.preview.count",
                    n=len(pages),
                    label=page_spec.format_pages_label(pages),
                )
            )
        else:
            self._count_lbl.setText("")

    def _on_translate_toggled(self, _checked: bool):
        self._translate_touched = True

    def _on_path_edited(self, _text: str):
        self._path_touched = True

    def _on_output_changed(self, *_):
        self._update_path()
        self._refresh_all()

    def _range_pages(self) -> tuple[int, int]:
        a = self._from_spin.value() - 1
        b = self._to_spin.value() - 1
        return (min(a, b), max(a, b))

    def _chosen_langs(self) -> tuple[str, str]:
        src = self._src_combo.currentData() if hasattr(self, "_src_combo") else None
        dst = self._dst_combo.currentData() if hasattr(self, "_dst_combo") else None
        return (src or self._source_lang, dst or self._target_lang)

    def _cache_counts(self) -> tuple[int, int, int]:
        """(cached, total, missing) per le pagine e le lingue scelte.

        Conta **esattamente** le pagine selezionate (anche non contigue), non
        l'intervallo fra la prima e l'ultima.
        """
        pages = self.chosen_pages()
        if not pages:
            return (0, 0, 0)
        source, target = self._chosen_langs()
        engine = self.chosen_engine()
        cached = sum(
            1
            for p in pages
            if self._engine.is_cached(p, engine, source, target)
        )
        total = len(pages)
        return cached, total, total - cached

    def _refresh_all(self, *_):
        self._parse_free()
        self._update_free_feedback()
        self._update_mode_controls()
        cached, total, missing = self._cache_counts()
        if hasattr(self, "_ready_lbl"):
            self._ready_lbl.setText(
                T("export.ready", cached=cached, total=total, missing=missing)
            )
        if hasattr(self, "_est_lbl"):
            eta = _fmt_duration(missing * _EXPORT_EST_MS_PER_PAGE / 1000.0)
            self._est_lbl.setText(
                T(
                    "export.wizard.est",
                    cached=cached,
                    total=total,
                    missing=missing,
                    time=eta,
                )
            )
        self._update_translate_option(missing)
        self._update_numbering_hints()
        self._update_next_enabled()
        self._preview_timer.start()
        self._update_path()
        self._update_summary()

    def _update_translate_option(self, missing: int):
        if not hasattr(self, "_chk_translate"):
            return
        self._chk_translate.setEnabled(missing > 0)
        if missing > 0 and not self._translate_touched:
            self._chk_translate.blockSignals(True)
            self._chk_translate.setChecked(True)
            self._chk_translate.blockSignals(False)
        elif missing == 0:
            self._chk_translate.blockSignals(True)
            self._chk_translate.setChecked(False)
            self._chk_translate.blockSignals(False)

    def _output_dir(self) -> Path:
        if self._folder is not None:
            return self._folder
        if self._pdf_path is not None:
            return self._pdf_path.parent
        docs = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation
        )
        return Path(docs) if docs else Path.home()

    def _default_filename(self) -> str:
        stem = self._pdf_path.stem if self._pdf_path else "export"
        base = (
            f"{stem}_pag{self.range_label()}_{self.chosen_engine()}"
            f"_{self.chosen_target()}"
        )
        fmt = self.chosen_format()
        if fmt == "folder":
            return base  # è il nome della sottocartella
        return base + (".zip" if fmt == "zip" else ".pdf")

    def _default_path(self) -> str:
        return str(self._output_dir() / self._default_filename())

    def _update_name_label(self):
        if not hasattr(self, "_name_lbl"):
            return
        key = (
            "export.wizard.output.folder_name"
            if self.chosen_format() == "folder"
            else "export.wizard.output.filename"
        )
        self._name_lbl.setText(T(key))

    def _update_path(self):
        if not hasattr(self, "_path_edit"):
            return
        self._update_name_label()
        if not self._path_touched:
            self._path_edit.setText(self._default_filename())

    def _update_summary(self):
        if not hasattr(self, "_summary"):
            return
        source, target = self._chosen_langs()
        name = self._pdf_path.name if self._pdf_path else "—"
        fmt = T(f"export.format.{self.chosen_format()}")
        path = self.chosen_path()
        lines = [
            (T("export.wizard.sum.file"), f"{name} · {self.range_label()}"),
            (
                T("export.wizard.sum.langs"),
                f"{flag_endonym(source)} → {flag_endonym(target)}",
            ),
            (T("export.wizard.sum.engine"), T(f"engine.option.{self.chosen_engine()}")),
            (T("export.wizard.sum.output"), f"{Path(path).name} · {fmt}"),
        ]
        self._summary.setText(
            "<br>".join(
                f"<span style='color:{theme.color('wiz_muted')}'>"
                f"{_html.escape(k)}</span>"
                f"&nbsp;&nbsp;&nbsp;{_html.escape(v)}"
                for k, v in lines
            )
        )

    def _browse_folder(self):
        """Sceglie la cartella di destinazione (ricordata in config)."""
        chosen = QFileDialog.getExistingDirectory(
            self,
            T("export.wizard.output.choose_folder"),
            str(self._output_dir()),
        )
        if not chosen:
            return
        self._folder = Path(chosen)
        self._folder_edit.setText(chosen)
        set_setting("export_dir", chosen)
        save_config()
        self._update_path()
        self._update_summary()

    # ── API per MainWindow ────────────────────────────────────────────

    def is_current(self) -> bool:
        return self._rad_current.isChecked()

    def is_free(self) -> bool:
        return self._rad_free.isChecked()

    def chosen_engine(self) -> str:
        btn = self._engine_group.checkedButton()
        return (btn.property("code") if btn is not None else None) or "google"

    def chosen_target(self) -> str:
        return self._chosen_langs()[1]

    def chosen_source(self) -> str:
        return self._chosen_langs()[0]

    def chosen_pages(self) -> list[int]:
        if self.is_current():
            return [self._current_page]
        if self.is_free():
            return list(self._free_pages)
        a, b = self._range_pages()
        return list(range(a, b + 1))

    def translate_missing(self) -> bool:
        return self._chk_translate.isChecked()

    def chosen_format(self) -> str:
        """Formato scelto: ``merged`` (PDF unico), ``zip`` o ``folder``."""
        if self._btn_folder.isChecked():
            return "folder"
        if self._btn_zip.isChecked():
            return "zip"
        return "merged"

    def chosen_zip(self) -> bool:
        """True se l'utente vuole le pagine singole in un archivio ZIP."""
        return self.chosen_format() == "zip"

    def chosen_folder(self) -> bool:
        """True se l'utente vuole le pagine singole in una cartella."""
        return self.chosen_format() == "folder"

    def chosen_path(self) -> str:
        """Destinazione (file o sottocartella) con l'estensione del formato scelto."""
        folder = self._output_dir()
        text = self._path_edit.text().strip() or self._default_filename()
        if self.chosen_format() == "folder":
            return str(folder / text)
        ext = ".zip" if self.chosen_zip() else ".pdf"
        if not text.lower().endswith(ext):
            for other in (".pdf", ".zip"):
                if text.lower().endswith(other):
                    text = text[: -len(other)]
                    break
            text += ext
        return str(folder / text)

    def range_label(self) -> str:
        """Suffisso per il nome file: "156", "156-159" oppure "1,3,7-9"."""
        return page_spec.format_pages_label(self.chosen_pages())


def _fmt_duration(seconds: float) -> str:
    """Formatta secondi come MM:SS (o H:MM:SS se serve)."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _fmt_bytes(size: int) -> str:
    """Formatta una dimensione in B/KB/MB/GB (base 1024)."""
    value = float(max(0, int(size)))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class ExportProgressDialog(QDialog):
    """Progress window shown while the missing range pages are translated and
    the PDF is written.

    It keeps the user informed with live feedback (engine/language, range,
    a working/done activity line, page i/N, a busy-then-determinate progress
    bar, running counts, elapsed + estimated remaining time and a per-page
    log). It is **not** application-modal: the user can minimize the app (or
    this window) while the job runs and is alerted by a system notification +
    sound when it finishes. ``MainWindow._batch_busy`` guards the few actions
    that must not run concurrently. When the job finishes it stays open in a
    "completed" state showing the saved file path, so the export never closes
    abruptly. Cancel terminates the in-flight ``pdf2zh_next`` process group.
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
        pdf_path=None,
        parent=None,
        range_label: str | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(T("export.progress.title"))
        self.setMinimumWidth(760)
        # Finestra normale (non solo dialog modale): tasto "riduci a icona" e
        # possibilità di ridurre anche la finestra principale mentre lavora.
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setStyleSheet(theme.settings_qss())
        self._total = max(int(total), 1)
        self._start: float | None = None
        self._done = 0
        self._failed = 0
        self._phase = "idle"
        self._finished = False
        self._current_page: int | None = None
        self._dots = 0
        self._dest: Path | None = None
        self._last_folder: Path | None = None
        self._page_from = int(page_from)
        self._page_to = int(page_to)
        self._range_label_text = range_label
        self._pos: int | None = None
        self._pdf_path = Path(pdf_path) if pdf_path else None
        self._doc = None
        self._labels: list[str] = []
        if self._pdf_path is not None and _has_pymupdf:
            with contextlib.suppress(Exception):
                self._labels = clone_engine.CloneEngine.page_labels(self._pdf_path)

        root = QVBoxLayout(self)
        root.setSpacing(8)

        body = QHBoxLayout()
        body.setSpacing(14)
        left = QVBoxLayout()
        left.setSpacing(6)

        self._ctx_lbl = QLabel(
            T("export.progress.engine_lang", engine=engine_label, lang=lang_label)
        )
        left.addWidget(self._ctx_lbl)

        self._range_lbl = QLabel(
            T(
                "export.progress.pages",
                **{
                    "label": self._range_label_text,
                    "missing": missing,
                    "cached": cached,
                },
            )
            if self._range_label_text is not None
            else T(
                "export.progress.range",
                **{
                    "from": page_from,
                    "to": page_to,
                    "missing": missing,
                    "cached": cached,
                },
            )
        )
        self._range_lbl.setStyleSheet(
            "color: %s; font-size: 12px;" % theme.color("text4")
        )
        left.addWidget(self._range_lbl)

        # Riga di attività: sempre valorizzata, così la finestra non appare
        # mai "vuota" durante l'attesa.
        self._activity_lbl = QLabel("")
        self._activity_lbl.setStyleSheet(
            "color: %s; font-size: 14px; font-weight: bold;" % theme.color("text")
        )
        self._activity_lbl.setWordWrap(True)
        left.addWidget(self._activity_lbl)

        self._page_lbl = QLabel("")
        self._page_lbl.setStyleSheet("color: %s;" % theme.color("text2"))
        left.addWidget(self._page_lbl)

        self._bar = QProgressBar()
        self._bar.setRange(0, self._total)
        self._bar.setValue(0)
        left.addWidget(self._bar)

        self._stats_lbl = QLabel("")
        self._stats_lbl.setStyleSheet(
            "color: %s; font-size: 12px;" % theme.color("text5")
        )
        left.addWidget(self._stats_lbl)

        self._eta_lbl = QLabel("")
        self._eta_lbl.setStyleSheet(
            "color: %s; font-size: 12px;" % theme.color("text5")
        )
        left.addWidget(self._eta_lbl)
        left.addStretch(1)

        body.addLayout(left, 1)

        # Anteprima della pagina correntemente in lavorazione (liquido).
        self._preview = LiquidOverlay(self)
        self._preview.setFixedSize(220, 290)
        body.addWidget(self._preview, 0, Qt.AlignmentFlag.AlignTop)
        root.addLayout(body)

        self._path_lbl = QLabel("")
        self._path_lbl.setStyleSheet(
            "color: %s; font-size: 12px;" % theme.color("ok")
        )
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
        self._btn_download = QPushButton(T("export.progress.save_download"))
        self._btn_download.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_download.setToolTip(T("export.progress.save_download.tip"))
        self._btn_download.clicked.connect(self._save_to_download)
        self._btn_download.setVisible(False)
        btns.addWidget(self._btn_download)
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

    def begin(self, page_1based: int, log: bool = True):
        """Avvia la fase di traduzione mostrando subito l'attività.

        ``log=False`` evita di aggiungere la riga "in lavorazione" al registro:
        nel batch la traduzione di più pagine parte insieme, quindi una riga di
        avvio per la sola prima pagina sarebbe fuorviante. Il registro resta
        pulito con i soli esiti (✓/✗) per pagina.
        """
        self._start = time.perf_counter()
        self._phase = "translating"
        self.set_translating(page_1based, position=1, log=log)
        self._timer.start()

    def _activity_text(self) -> str:
        if not self._current_page:
            return T("export.progress.activity_exporting")
        if self._pos:
            return T(
                "export.progress.activity_page_pos",
                page=self._current_page,
                pos=self._pos,
                total=self._total,
            )
        return T("export.progress.activity_page", page=self._current_page)

    def set_translating(
        self, page_1based: int, position: int | None = None, log: bool = True
    ):
        self._current_page = page_1based
        self._pos = position
        self._phase = "translating"
        self._bar.setRange(0, 0)  # busy/indeterminata
        # Riga "in lavorazione" opzionale: nel flusso batch la si omette, così
        # il registro resta pulito con i soli esiti ✓/✗.
        if log:
            self._log.appendPlainText(
                T("export.progress.activity_page_log", page=page_1based)
            )
        self._page_lbl.setText(
            T(
                "export.progress.label",
                page=page_1based,
                pos=position or (self._done + 1),
                total=self._total,
            )
        )
        self._activity_lbl.setText(self._activity_text() + "." * self._dots)
        self._update_stats()
        self._update_preview(page_1based)

    def _doc_handle(self):
        if self._doc is not None:
            return self._doc
        if not _has_pymupdf or self._pdf_path is None:
            return None
        try:
            self._doc = pymupdf.open(str(self._pdf_path))
        except Exception:
            self._doc = None
        return self._doc

    def _update_preview(self, page_1based: int):
        """Mostra nell'overlay la pagina sorgente correntemente in lavorazione."""
        page0 = max(0, int(page_1based) - 1)
        pix = None
        doc = self._doc_handle()
        if doc is not None and len(doc) > 0:
            index = min(page0, len(doc) - 1)
            try:
                ratio = float(self.devicePixelRatioF())
            except Exception:
                ratio = 1.0
            try:
                pdf_page = doc[index]
                width = int(self._preview.width() * ratio)
                zoom = max(0.05, width / max(pdf_page.rect.width, 1.0))
                pm = pdf_page.get_pixmap(
                    matrix=pymupdf.Matrix(zoom, zoom), alpha=False
                )
                img = QImage(
                    pm.samples, pm.width, pm.height, pm.stride,
                    QImage.Format.Format_RGB888,
                )
                pix = QPixmap.fromImage(img)
            except Exception:
                pix = None
        label = self._labels[page0] if 0 <= page0 < len(self._labels) else ""
        if label and label != str(page_1based):
            caption = T(
                "export.wizard.preview.caption", n=page_1based, label=label
            )
        else:
            caption = T(
                "export.wizard.preview.caption_plain", n=page_1based
            )
        self._preview.start(pix, caption)

    def closeEvent(self, event):  # noqa: N802 — override Qt
        if self._doc is not None:
            with contextlib.suppress(Exception):
                self._doc.close()
            self._doc = None
        super().closeEvent(event)

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
        self._preview.stop()
        self._bar.setRange(0, 0)  # busy mentre si scrive il PDF
        self._page_lbl.setText("")
        self._activity_lbl.setText(T("export.progress.activity_exporting"))
        self._update_stats()

    def set_completed(self, dest: str, count: int, failed: int):
        self._finished = True
        self._timer.stop()
        self._phase = "done"
        self._preview.stop()
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
        # Formato "cartella": la destinazione è essa stessa la cartella da aprire.
        self._last_folder = self._dest if self._dest.is_dir() else self._dest.parent
        if self._range_label_text is not None:
            self._range_lbl.setText(
                T(
                    "export.progress.pages_done",
                    label=self._range_label_text,
                    done=count,
                    failed=failed,
                )
            )
        else:
            self._range_lbl.setText(
                T(
                    "export.progress.range_done",
                    **{
                        "from": self._page_from,
                        "to": self._page_to,
                        "done": count,
                        "failed": failed,
                    },
                )
            )
        self._path_lbl.setText(
            T("export.progress.saved_path", path=str(dest))
        )
        self._path_lbl.setVisible(True)
        self._log.appendPlainText(T("export.progress.completed_title"))
        self._btn_cancel.setVisible(False)
        self._btn_open.setVisible(True)
        self._btn_open.setToolTip(str(self._last_folder))
        self._btn_download.setVisible(True)
        self._btn_close.setVisible(True)
        self._btn_close.setFocus()

    def _save_to_download(self):
        """Copia il risultato nella cartella Download (file o cartella).

        È una **copia**: l'originale resta nella destinazione scelta. Per il
        formato "pagine singole in una cartella" copia l'intera sottocartella.
        """
        if self._dest is None or not self._dest.exists():
            return
        downloads = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation
        )
        target_dir = Path(downloads) if downloads else Path.home() / "Downloads"
        with contextlib.suppress(OSError):
            target_dir.mkdir(parents=True, exist_ok=True)

        def _free_target(base: Path) -> Path:
            if not base.exists():
                return base
            stem, suffix = base.stem, base.suffix
            index = 1
            while True:
                candidate = base.parent / f"{stem}_{index}{suffix}"
                if not candidate.exists():
                    return candidate
                index += 1

        target = _free_target(target_dir / self._dest.name)
        try:
            if self._dest.is_dir():
                shutil.copytree(self._dest, target)
            else:
                shutil.copy2(self._dest, target)
        except OSError:
            self._path_lbl.setText(T("export.progress.download_error"))
            return
        self._last_folder = target.parent
        self._btn_open.setToolTip(str(target.parent))
        self._path_lbl.setText(
            T("export.progress.downloaded", path=str(target))
        )
        self._path_lbl.setVisible(True)

    def set_error(self, message: str):
        """Termina in errore mantenendo la finestra aperta (feedback)."""
        self._finished = True
        self._timer.stop()
        self._phase = "error"
        self._preview.stop()
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
            self._activity_lbl.setText(self._activity_text() + "." * self._dots)
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
        """Apre l'ULTIMA destinazione usata (Download dopo un salvataggio lì)."""
        folder = self._last_folder
        if folder is None and self._dest is not None:
            folder = self._dest.parent
        if folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


# ═══════════════════════════════════════════════════════════════════════════════
#  main window
# ═══════════════════════════════════════════════════════════════════════════════


class MainWindow(QMainWindow):
    # Motore di rendering (PyMuPDF) e di estrazione (PyMuPDF4LLM) fissi;
    # l'engine adattativo dei fix è sempre attivo (nessun dropdown).

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Noesis PDF Cloner")
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
        # ``max_concurrent=1``: una sola traduzione alla volta (robustezza su
        # Windows e niente processi concorrenti; la targhetta "in lavorazione"
        # continua a mostrare le pagine in coda).
        self._clone_engine = clone_engine.CloneEngine(
            cache_root=_app_data_base() / "clones",
            pdf2zh_bin=get_setting("pdf2zh_bin", "") or None,
            max_concurrent=1,
        )
        # Proxy provider locale (avvio automatico opzionale + prova provider).
        self._proxy_manager = proxy_manager.ProxyManager(
            log_dir=_app_data_base() / "clones" / "_tmp"
        )
        self._clone_thread: CloneTranslateThread | None = None
        self._retired_clone_threads: list[CloneTranslateThread] = []
        self._orig_pixmap: QPixmap | None = None
        self._llm_key_prompted = False
        # Chiave LLM verificata con successo in questa sessione ("" = da verificare).
        self._llm_key_verified = ""
        self._key_verify_thread: KeyVerifyThread | None = None
        self._pending_translate_page: int | None = None
        self._clone_generation: int = 0
        self._clone_run_start: dict[str, float] = {}

        # Job lungo in corso (export/batch): blocca le azioni che non devono
        # sovrapporsi ora che la finestra non è più modale (punto 1).
        self._batch_busy: bool = False
        # Standby inibito durante i job (punto 4) e watchdog per il risveglio.
        self._sleep_inhibitor = power.SleepInhibitor()
        self._resume_watch = power.ResumeWatch()
        self._resume_timer = QTimer(self)
        self._resume_timer.setInterval(int(power.CHECK_INTERVAL_S * 1000))
        self._resume_timer.timeout.connect(self._on_power_poll)
        # Icona nel system tray (avvisi + riduzione a icona). Creata dopo la
        # toolbar perché serve l'icona dell'app.
        self._tray: QSystemTrayIcon | None = None

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
        self.translated_panel.export_current_requested.connect(
            self._on_export_current
        )
        self.translated_panel.translate_cancel_requested.connect(
            self._on_cancel_translation
        )
        self.translated_panel.install_engine_requested.connect(
            self._on_install_engine_banner
        )
        self.translated_panel.enter_key_requested.connect(
            self._on_enter_key_banner
        )
        self.translated_panel.use_free_engine_requested.connect(
            self._on_use_free_engine_banner
        )
        self.translated_panel.page_save_download_requested.connect(
            self._on_fab_save_download
        )
        self.translated_panel.page_translate_next_requested.connect(
            self._on_fab_translate_next
        )
        self.translated_panel.page_retranslate_requested.connect(
            self._on_fab_retranslate
        )
        self.translated_panel.page_open_external_requested.connect(
            self._on_fab_open_external
        )
        self.translated_panel.page_purge_requested.connect(
            self._on_fab_purge
        )
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

        # Tema attivo (chiaro/scuro/sistema): i colori vengono dal modulo
        # ``theme``. Il pulsante ▶ Traduci ha un accento proprio applicato qui.
        self.apply_theme()

        # Icona nel system tray (notifiche di fine batch / mostra finestra) e
        # watchdog per il risveglio da standby.
        self._setup_tray()
        self._resume_timer.start()
        # In modalità "come il sistema", segui i cambi di tema del SO.
        with contextlib.suppress(Exception):
            QApplication.styleHints().colorSchemeChanged.connect(
                self._on_color_scheme_changed
            )

        # L'apertura del PDF è gestita in main(): argomento da riga di comando
        # oppure harrison2025.pdf nella directory corrente.

        # Striscia "motore non installato": valutata subito all'avvio.
        self._configure_clone_engine()
        self._update_engine_banner()
        self._update_translate_tooltip()

    # ── tema ──────────────────────────────────────────────────────────────

    def apply_theme(self) -> None:
        """Applica il tema attivo alla finestra, al pannello e al TOC.

        Chiamato all'avvio e dopo un cambio tema nelle Impostazioni. Le
        tavolozze arrivano da ``theme``; i widget che hanno stili inline li
        riapplicano tramite i propri metodi ``apply_theme``.
        """
        self.setStyleSheet(theme.main_qss())
        if hasattr(self, "btn_translate"):
            self.btn_translate.setStyleSheet(
                "QPushButton { background: %s; color: %s; border: none;"
                " border-radius: 4px; padding: 4px 12px; font-size: 13px;"
                " font-weight: bold; }"
                "QPushButton:hover { background: %s; }"
                "QPushButton:disabled { background: %s; color: %s; }"
                % (
                    theme.color("accent"),
                    theme.color("accent_text"),
                    theme.color("accent_hover"),
                    theme.color("disabled_bg"),
                    theme.color("disabled_text"),
                )
            )
        if hasattr(self, "translated_panel"):
            self.translated_panel.apply_theme()
        if hasattr(self, "pdf_view"):
            self.pdf_view.apply_theme()
        if hasattr(self, "toc_panel"):
            self.toc_panel.apply_theme()

    # ── avvisi, tray, standby ──────────────────────────────────────────────

    def _on_color_scheme_changed(self, scheme) -> None:
        """Segue il cambio di tema del SO quando la modalità è "sistema"."""
        if theme.mode() != "system":
            return
        apply_theme_mode("system")
        self.apply_theme()

    def _setup_tray(self) -> None:
        """Crea l'icona nel system tray (se disponibile) per avvisi e ripristino."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = _app_icon()
        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip(T("tray.tip"))
        menu = QMenu(self)
        menu.addAction(T("tray.show"), self._restore_from_tray)
        menu.addSeparator()
        menu.addAction(T("tray.quit"), QApplication.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray

    def _on_tray_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._restore_from_tray()

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _notify_batch(self, message: str, sound: bool | None = None) -> None:
        """Avviso di fine batch: notifica di sistema + suono + badge taskbar.

        Silenzioso se l'utente ha disattivato l'avviso. Non solleva mai: un
        sistema senza tray/audio non deve far fallire il job.
        """
        if not bool(get_setting("notify_on_finish", True)):
            return
        if sound is None:
            sound = bool(get_setting("notify_sound", True))
        with contextlib.suppress(Exception):
            QApplication.alert(self)
        if self._tray is not None and self._tray.isVisible():
            with contextlib.suppress(Exception):
                self._tray.showMessage(
                    T("notify.batch.title"), message, _app_icon(), 8000
                )
        notifications.chime(sound)

    def _start_long_job(self) -> None:
        """Segna l'inizio di un job lungo: blocca le azioni in conflitto."""
        self._batch_busy = True
        if bool(get_setting("prevent_sleep", True)):
            self._sleep_inhibitor.acquire()
        self._resume_watch.reset()

    def _end_long_job(self) -> None:
        self._batch_busy = False
        self._sleep_inhibitor.release()

    def _on_power_poll(self) -> None:
        gap = self._resume_watch.poll()
        if gap > 0:
            self._handle_resume(gap)

    def _handle_resume(self, gap: float) -> None:
        """Risveglio da standby: segnala e lascia che il motore ritenti.

        Durante la sospensione la pagina in volo può fallire (rete assente) o
        andare in timeout: il motore la riproverà, e l'utente viene avvisato.
        """
        log.info("risveglio da standby (%.1f s), batch=%s", gap, self._batch_busy)
        if self._batch_busy:
            self.status_bar.showMessage(T("power.resumed"), 8000)

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

        # Impostazioni (lingua UI, lingue traduzione, preferenze). Il cambio
        # lingua UI avviene SOLO qui.
        _spacer = QWidget()
        _spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        bar.addWidget(_spacer)

        # ▶ Traduci: la traduzione parte SOLO da qui (nessuna auto-traduzione).
        # Nella toolbar principale resta raggiungibile anche con la fascia
        # motori del pannello destro compattata (chevron ▸).
        self.btn_translate = QPushButton(f"▶ {T('clone.translate')}")
        self.btn_translate.setToolTip(T("clone.translate.tip"))
        self.btn_translate.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_translate.setStyleSheet(
            "QPushButton { background: %s; color: %s; border: none;"
            " border-radius: 4px; padding: 4px 12px; font-size: 13px;"
            " font-weight: bold; }"
            "QPushButton:hover { background: %s; }"
            "QPushButton:disabled { background: %s; color: %s; }"
            % (
                theme.color("accent"),
                theme.color("accent_text"),
                theme.color("accent_hover"),
                theme.color("disabled_bg"),
                theme.color("disabled_text"),
            )
        )
        self.btn_translate.clicked.connect(self._on_translate_requested)
        bar.addWidget(self.btn_translate)

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

        # Il FAB delle azioni appartiene alla pagina tradotta appena vista:
        # cambiando pagina sparisce.
        self.translated_panel.hide_page_actions()

        # Render left
        self._display_page(page_num)

        # Clone a destra: si mostra solo la cache del motore selezionato.
        # La traduzione è on demand (pulsante ▶ Traduci nel pannello destro).
        if self._pdf_path:
            self._show_clone_for_page(page_num)

        # Update toolbar
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(page_num + 1)
        self.page_spin.blockSignals(False)

        # Sync TOC highlight
        self.toc_panel.select_page(page_num)

        # Targhetta "in lavorazione" (eventuale pagina in traduzione non mostrata)
        self._update_working_badge()
        self._update_translate_tooltip()

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

    def _update_translate_tooltip(self):
        """Tooltip di ▶ Traduci: motore attivo e pagina corrente.

        Utile quando la fascia motori del pannello destro è compattata: dice
        su quale motore e pagina agirà il pulsante.
        """
        tip = T("clone.translate.tip")
        if self._page_count:
            tip += "\n" + T(
                "clone.translate.tooltip",
                page=self._current_page + 1,
                engine=self._engine_display(get_translation_engine()),
            )
        self.btn_translate.setToolTip(tip)

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
        """Open the online help site in the system browser.

        Apre la guida nella lingua dell'interfaccia (fallback italiano).
        """
        QDesktopServices.openUrl(QUrl(help_url()))

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
        self._apply_api_key(values.get("openrouter_api_key"))
        theme_changed = values.get("theme", get_setting("theme", "dark")) != get_setting("theme", "dark")
        for key in ("zoom", "font_size", "render_md", "show_header",
                    "resume_last_page", "remember_tab", "save_edits",
                    "pdf2zh_bin", "theme", "notify_on_finish", "notify_sound",
                    "prevent_sleep", "llm_pool_workers",
                    "fast_engine", "fast_flags",
                    "llm_reasoning_effort", "llm_json_mode", "fast_worker",
                    "llm_model", "llm_base_url",
                    "llm_proxy_autostart", "llm_proxy_port", "llm_system_prompt"):
            if key in values:
                set_setting(key, values[key])
        set_source_lang(src)   # setters validati (auto solo in sorgente)
        set_target_lang(dst)
        set_translation_engine(engine)
        save_config()

        # Cambio tema: applica subito (risolvendo "come il sistema").
        if theme_changed:
            apply_theme_mode(str(values.get("theme", "dark")))
            self.apply_theme()

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

        # Pannello clone: allinea lingua/engine e aggiorna la vista (on demand:
        # cambiare motore/lingua NON avvia più una traduzione).
        self._configure_clone_engine()
        self._update_engine_banner()
        self.translated_panel.set_target_language(dst)
        self.translated_panel.set_engine(engine)
        if langs_changed or engine_changed:
            self._clone_generation += 1
            self._retire_clone_thread()
            if self._pdf_path:
                self._show_clone_for_page(self._current_page)

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
        self.btn_translate.setText(f"▶ {T('clone.translate')}")
        self._update_translate_tooltip()
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
            self._show_clone_for_page(self._current_page)

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
            self._orig_pixmap = pix
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
        self._clone_engine.llm_pool_workers = max(
            1, int(get_setting("llm_pool_workers", 4) or 4)
        )
        # Feature sperimentale "motore veloce" (default OFF, reversibile).
        self._clone_engine.fast_engine = bool(get_setting("fast_engine", False))
        self._clone_engine.fast_flags = bool(get_setting("fast_flags", False))
        self._clone_engine.llm_reasoning_effort = str(
            get_setting("llm_reasoning_effort", "") or ""
        )
        self._clone_engine.llm_json_mode = bool(get_setting("llm_json_mode", False))
        # Modello / base URL: setting esplicito, altrimenti env o default.
        model = str(get_setting("llm_model", "") or "").strip()
        self._clone_engine.llm_model = model or os.environ.get(
            "PDF_LLM_MODEL", clone_engine.DEFAULT_MODEL
        )
        base = str(get_setting("llm_base_url", "") or "").strip()
        self._clone_engine.llm_base_url = base or os.environ.get(
            "PDF_LLM_BASE_URL", clone_engine.DEFAULT_BASE_URL
        )
        # Proxy provider locale: se l'avvio automatico è attivo, punta la base
        # URL al proxy e avvialo in background.
        if self._clone_engine.fast_engine and bool(
            get_setting("llm_proxy_autostart", False)
        ):
            port = int(get_setting("llm_proxy_port", 8790) or 8790)
            self._clone_engine.llm_base_url = f"http://127.0.0.1:{port}/v1"
            self._ensure_proxy_async(port)
        self._clone_engine.fast_worker = bool(get_setting("fast_worker", False))
        self._clone_engine.numeric_lists = bool(get_setting("numeric_lists", False))
        self._clone_engine.llm_system_prompt = str(
            get_setting("llm_system_prompt", "") or ""
        ).strip()
        if self._clone_engine.fast_worker and self._clone_engine.fast_engine:
            # Pre-avvia il worker persistente (in background): il costo di avvio
            # non ricade sulla prima pagina.
            self._clone_engine.warmup()

    def _ensure_proxy_async(self, port: int) -> None:
        """Avvia il proxy provider in background (best effort, non bloccante)."""
        def _go() -> None:
            try:
                self._proxy_manager.ensure_started(int(port))
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=_go, name="proxy-start", daemon=True).start()

    def ensure_proxy(self, port: int | None = None) -> bool:
        """Assicura il proxy provider (usato dal pulsante "Prova provider")."""
        port = int(port or get_setting("llm_proxy_port", 8790) or 8790)
        try:
            return self._proxy_manager.ensure_started(port)
        except Exception:  # noqa: BLE001
            return False

    def _update_engine_banner(self):
        """Aggiorna le strisce informative (motore e chiave mancanti)."""
        self.translated_panel.set_engine_missing(
            not self._clone_engine.available()
        )
        self.translated_panel.set_engine_key_missing(self._llm_key_needed())

    def _llm_key_needed(self) -> bool:
        """True se il motore LLM è selezionato e non c'è una chiave disponibile."""
        if get_translation_engine() != "llm":
            return False
        return not bool((os.environ.get(keystore.ENV_VAR) or "").strip())

    def _on_install_engine_banner(self):
        """Pulsante *Installa* della striscia: apre il dialog di installazione."""
        dlg = EngineInstallDialog(self)

        def _installed(path: str):
            self._configure_clone_engine()
            self._update_engine_banner()
            self.status_bar.showMessage(T("engine.install.done", path=path), 8000)

        dlg.installed.connect(_installed)
        dlg.exec()

    def _on_enter_key_banner(self):
        """Pulsante *Inserisci chiave* della striscia: apre il dialog dedicato."""
        if self._prompt_api_key():
            self._update_engine_banner()
            if self._pdf_path:
                self._show_clone_for_page(self._current_page)

    def _on_use_free_engine_banner(self):
        """Pulsante *Usa Google/Bing*: passa a un motore gratuito (senza chiave)."""
        self.translated_panel.set_engine("google", emit=True)

    def _apply_api_key(self, key):
        """Salva/rimuove la chiave OpenRouter (archivio per-utente + env).

        La chiave salvata ha la precedenza; rimuovendola si torna alla variabile
        di sistema (se era presente all'avvio).
        """
        if key is None:
            return
        global _API_KEY_SOURCE
        key = str(key).strip()
        store = keystore.KeyStore(_keystore_path())
        if key:
            store.set(key)
            os.environ[keystore.ENV_VAR] = key
            _API_KEY_SOURCE = "file"
        else:
            store.clear()
            if _API_KEY_ENV_AT_START:
                os.environ[keystore.ENV_VAR] = _API_KEY_ENV_AT_START
                _API_KEY_SOURCE = "env"
            else:
                os.environ.pop(keystore.ENV_VAR, None)
                _API_KEY_SOURCE = "none"
        # La chiave è cambiata: va rivalidata prima del prossimo uso.
        self._llm_key_verified = ""

    def _prompt_api_key(self) -> bool:
        """Chiede la chiave OpenRouter; True se dopo il dialogo è disponibile."""
        store = keystore.KeyStore(_keystore_path())
        dlg = ApiKeyDialog(self, initial=store.get())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return bool(os.environ.get("OPENROUTER_API_KEY"))
        key = dlg.value()
        if not key:
            return False
        self._apply_api_key(key)
        return True

    def _ensure_llm_key(self, force: bool = False) -> bool:
        """True se il motore LLM ha una chiave; altrimenti la chiede all'utente.

        Il prompt automatico avviene una sola volta per sessione (``force=True``
        quando l'utente sceglie esplicitamente LLM).
        """
        if os.environ.get("OPENROUTER_API_KEY"):
            return True
        if self._llm_key_prompted and not force:
            return False
        self._llm_key_prompted = True
        return self._prompt_api_key()

    def _no_key_message(self) -> str:
        """Messaggio per chiave mancante, distinguendo la variabile non caricata."""
        return T("clone.key_stale") if _API_KEY_ENV_STALE else T("clone.no_key")

    def _start_key_verify(self, page: int | None):
        """Avvia (non bloccante) la verifica della chiave OpenRouter.

        ``page`` è la pagina da tradurre quando la verifica serve da pre-volo,
        oppure ``None`` per una verifica informativa (es. selezione del motore).
        """
        key = (os.environ.get(keystore.ENV_VAR) or "").strip()
        self._pending_translate_page = page
        if not key:
            return
        if key == self._llm_key_verified:
            if page is not None:
                self._pending_translate_page = None
                self._request_translation(page)
            return
        thread = self._key_verify_thread
        if thread is not None and thread.isRunning():
            return  # verifica in corso: al termine userà la pagina in attesa
        self.translated_panel.set_status(T("clone.status_key_check"))
        self.status_bar.showMessage(T("clone.key_check"))
        thread = KeyVerifyThread(key, self)
        thread.verified.connect(self._on_key_verified)
        thread.finished.connect(
            lambda t=thread: self._on_key_verify_finished(t)
        )
        self._key_verify_thread = thread
        thread.start()

    def _on_key_verify_finished(self, thread):
        """Rilascia il riferimento al thread di verifica terminato."""
        if self._key_verify_thread is thread:
            self._key_verify_thread = None
        thread.deleteLater()

    def _on_key_verified(self, ok: bool, reason: str, key: str):
        """Esito della verifica: prosegue col pre-volo o mostra il consiglio."""
        page = self._pending_translate_page
        self._pending_translate_page = None
        if ok:
            self._llm_key_verified = key
            if page is not None:
                # Rientra nel flusso completo (purge di altri motori, cache…)
                # solo se l'utente è ancora sulla pagina richiesta.
                if page == self._current_page:
                    self._on_translate_requested()
            else:
                self.status_bar.showMessage(T("apikey.verify_ok"), 6000)
            return
        code = {
            "invalid": "invalid_key",
            "missing": "missing_key",
            "unreachable": "network",
        }.get(reason, "unknown")
        text = _friendly_reason(code)
        if page is None:
            # Verifica informativa: non sporcare il pannello.
            self.status_bar.showMessage(text, 8000)
            return
        if page != self._current_page:
            return
        self.translated_panel.hide_spinner()
        self.translated_panel.hide_liquid()
        self.translated_panel.set_status(T("clone.status_error"))
        self.translated_panel.show_message(text)
        self.status_bar.showMessage(text)

    def _engine_display(self, engine: str) -> str:
        return T(f"engine.option.{engine}")

    def _show_clone_for_page(self, page_num: int):
        """Mostra il clone in cache del motore selezionato, o il segnaposto.

        Nessuna traduzione automatica: si avvia solo col pulsante ▶ Traduci.
        """
        if not self._pdf_path:
            return
        self._configure_clone_engine()
        engine = get_translation_engine()
        if self._clone_engine.is_cached(page_num, engine):
            self._display_translated_page(page_num)
            if self._clone_engine.status(page_num, engine) == "empty":
                self.translated_panel.set_status(T("clone.status_empty"))
            else:
                self.translated_panel.set_status(T("clone.status_done"))
        else:
            self.translated_panel.show_pending(
                self._orig_pixmap, T("clone.pending_page")
            )
        self._update_working_badge()

    def _on_translate_requested(self):
        """Pulsante ▶ Traduci: avvia la traduzione della pagina corrente."""
        if self._batch_busy:
            self.status_bar.showMessage(T("export.busy"), 5000)
            return
        if not self._pdf_path or self._mupdf_doc is None or self._page_count == 0:
            return
        self._configure_clone_engine()
        page = self._current_page
        engine = get_translation_engine()

        if self._clone_engine.is_cached(page, engine):
            self._show_clone_for_page(page)
            self.translated_panel.set_status(
                T("clone.status_cached", page=page + 1)
            )
            self.status_bar.showMessage(
                T(
                    "clone.already_cached",
                    page=page + 1,
                    engine=self._engine_display(engine),
                ),
                6000,
            )
            return

        if engine == "llm":
            if not self._ensure_llm_key(force=True):
                self._request_translation(page)  # mostra clone.no_key
                return
            key = (os.environ.get(keystore.ENV_VAR) or "").strip()
            if key and key != self._llm_key_verified:
                # Verifica pre-volo: non far fallire un lavoro lungo per chiave.
                self._start_key_verify(page)
                return

        if not self._clone_engine.available():
            self._request_translation(page)  # mostra clone.no_engine
            return

        # Una traduzione per pagina: la cache della pagina con un altro motore
        # lascia il posto a quello scelto, dopo conferma dell'utente.
        others = [
            e
            for e in self._clone_engine.cached_engines_for_page(page)
            if e != engine
        ]
        if others:
            if not self._confirm_purge(others, engine, page):
                return
            # Ferma le traduzioni in background **di questa pagina** con quei
            # motori: altrimenti riscriverebbero la cache appena eliminata.
            for thread in list(self._retired_clone_threads):
                if (
                    thread.isRunning()
                    and thread.page() == page
                    and thread.engine_name() in others
                ):
                    thread.cancel()
            if (
                self._clone_thread is not None
                and self._clone_thread.isRunning()
                and self._clone_thread.page() == page
                and self._clone_thread.engine_name() in others
            ):
                self._clone_thread.cancel()
            for old in others:
                self._clone_engine.purge_page_cache(old, page)

        self._request_translation(page)

    def _confirm_purge(self, old_engines: list[str], new_engine: str, page: int) -> bool:
        """Chiede conferma prima di eliminare la pagina in cache di altri motori."""
        files = 0
        size = 0
        for eng in old_engines:
            nfiles, nbytes = self._clone_engine.page_cache_stats(eng, page)
            files += nfiles
            size += nbytes
        old_names = ", ".join(self._engine_display(e) for e in old_engines)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(T("clone.purge.title"))
        box.setText(
            T(
                "clone.purge.body",
                old=old_names,
                new=self._engine_display(new_engine),
                page=page + 1,
                size=_fmt_bytes(size),
            )
        )
        confirm = box.addButton(
            T("clone.purge.confirm"), QMessageBox.ButtonRole.AcceptRole
        )
        box.addButton(T("clone.purge.cancel"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is confirm

    def _request_translation(self, page_num: int):
        """Show the cached clone of ``page_num`` or start a background job."""
        if not self._pdf_path:
            return
        self._configure_clone_engine()
        engine = get_translation_engine()

        if engine == "llm" and not self._ensure_llm_key():
            self.translated_panel.hide_liquid()
            message = self._no_key_message()
            self.translated_panel.show_message(message)
            self.translated_panel.set_status(T("clone.status_error"))
            self.status_bar.showMessage(message, 6000)
            return

        if self._clone_engine.is_cached(page_num, engine):
            self._display_translated_page(page_num)
            if self._clone_engine.status(page_num, engine) == "empty":
                self.translated_panel.set_status(T("clone.status_empty"))
                self.status_bar.showMessage(
                    T("clone.empty_page", page=page_num + 1), 6000
                )
            else:
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

        current = self._clone_thread
        if (
            current is not None
            and current.isRunning()
            and current.page() == page_num
        ):
            # Stessa pagina già in traduzione (es. richiesta duplicata al
            # cambio pagina): non avviare un secondo worker, lascia lo spinner.
            return

        self._clone_generation += 1
        self._clone_run_start[engine] = time.time()
        self._retire_clone_thread()
        self.translated_panel.show_translating(
            self._orig_pixmap,
            T("clone.spinner", engine=self._engine_display(engine)),
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
        thread.cancelled.connect(self._on_clone_cancelled)
        self._clone_thread = thread
        thread.start()
        self._update_working_badge()

    def _on_cancel_translation(self):
        """Annulla la traduzione della pagina corrente (solo il thread attivo)."""
        thread = self._clone_thread
        if thread is not None and thread.isRunning():
            thread.cancel()

    def _on_clone_cancelled(self, generation: int, page_num: int, engine: str):
        if generation != self._clone_generation:
            return
        self.translated_panel.hide_liquid()
        self.translated_panel.set_status(T("clone.status_cancelled"))
        self._update_working_badge()
        if page_num != self._current_page:
            return
        self.translated_panel.show_message(T("clone.cancelled"))
        self.status_bar.showMessage(T("clone.cancelled"), 5000)

    def _update_working_badge(self):
        """Targhetta con la pagina in traduzione se non è quella mostrata."""
        pages = {
            t.page()
            for t in self._retired_clone_threads
            if t.isRunning()
        }
        pages.discard(self._current_page)
        if not pages:
            self.translated_panel.set_working("")
        elif len(pages) == 1:
            self.translated_panel.set_working(
                T("clone.working.badge", n=next(iter(pages)) + 1)
            )
        else:
            self.translated_panel.set_working(
                T("clone.working.many", n=len(pages))
            )

    def _retire_clone_thread(self):
        """Detach the current clone thread; keep it alive until it finishes."""
        old = self._clone_thread
        self._clone_thread = None
        if old is None:
            return
        for sig in (old.done, old.error, old.cancelled):
            try:
                sig.disconnect()
            except TypeError:
                pass
        if old.isRunning():
            self._retired_clone_threads.append(old)
            old.finished.connect(self._forget_retired_clone_thread)
            self._update_working_badge()

    def _forget_retired_clone_thread(self):
        t = self.sender()
        if t in self._retired_clone_threads:
            self._retired_clone_threads.remove(t)
        self._update_working_badge()

    def _on_clone_done(self, generation: int, page_num: int, engine: str, path: str):
        if generation != self._clone_generation:
            return
        if page_num != self._current_page or engine != get_translation_engine():
            return
        self.translated_panel.finish_translating(True)
        self._display_translated_page(page_num)
        if self._clone_engine.status(page_num, engine) == "empty":
            self.translated_panel.set_status(T("clone.status_empty"))
            self.status_bar.showMessage(
                T("clone.empty_page", page=page_num + 1), 6000
            )
        else:
            self.translated_panel.set_status(T("clone.status_done"))
            self.status_bar.showMessage(T("clone.done", page=page_num + 1))
            # Azioni pagina: il FAB compare a traduzione fresca completata.
            self.translated_panel.set_page_actions_next_enabled(
                page_num < self._page_count - 1
            )
            self.translated_panel.show_page_actions()
            self._notify_page_done(page_num)
        self._update_working_badge()

    def _notify_page_done(self, page_num: int) -> None:
        """Avvisa a fine pagina SOLO se l'utente non sta guardando la finestra.

        Così chi ha ridotto a icona (o è su un'altra app) sente il campanello e
        vede la notifica; chi sta guardando non viene disturbato.
        """
        if self.isActiveWindow() and not self.isMinimized():
            return
        self._notify_batch(T("notify.page.done", page=page_num + 1))

    def _on_clone_error(self, generation: int, page_num: int, engine: str, message: str):
        if generation != self._clone_generation:
            return
        self.translated_panel.hide_spinner()
        self.translated_panel.hide_liquid()
        self.translated_panel.set_status(T("clone.status_error"))
        self._update_working_badge()
        if page_num != self._current_page:
            return
        text = _friendly_reason(message)
        self.translated_panel.show_message(text)
        self.status_bar.showMessage(text)

    # ── azioni pagina (pulsante flottante) ────────────────────────────────

    def _confirm_page_action(self, title: str, text: str,
                             confirm_label: str) -> bool:
        """Conferma generica per le azioni distruttive del FAB."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(text)
        confirm = box.addButton(confirm_label, QMessageBox.ButtonRole.AcceptRole)
        box.addButton(T("clone.purge.cancel"), QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is confirm

    def _on_fab_save_download(self):
        """⬇ Salva in Download: esporta la pagina corrente nella cartella Download."""
        if not self._pdf_path:
            return
        page = self._current_page
        engine = get_translation_engine()
        source = get_source_lang()
        target = get_target_lang()
        if not self._clone_engine.is_cached(page, engine, source, target):
            self.status_bar.showMessage(T("export.not_ready"), 5000)
            return
        downloads = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation
        )
        target_dir = Path(downloads) if downloads else Path.home() / "Downloads"
        with contextlib.suppress(OSError):
            target_dir.mkdir(parents=True, exist_ok=True)
        base = f"{self._pdf_path.stem}_pag{page + 1}_{engine}_{target}"
        dest = target_dir / f"{base}.pdf"
        index = 1
        while dest.exists():
            dest = target_dir / f"{base}_{index}.pdf"
            index += 1
        try:
            self._clone_engine.export_pdf(
                [page], engine, str(dest), source, target
            )
        except Exception:  # noqa: BLE001 — riportato nella status bar
            log.exception("salvataggio pagina in Download fallito: %s", dest)
            self.status_bar.showMessage(T("clone.fab.save_error"), 5000)
            return
        self.status_bar.showMessage(
            T("clone.fab.saved_download", path=str(dest)), 6000
        )

    def _on_fab_translate_next(self):
        """▶ Traduci la successiva: passa alla pagina dopo e la traduce."""
        if not self._pdf_path or self._page_count == 0:
            return
        if self._current_page >= self._page_count - 1:
            self.status_bar.showMessage(T("clone.fab.last_page"), 4000)
            return
        self._set_page(self._current_page + 1)
        self._on_translate_requested()

    def _on_fab_retranslate(self):
        """🔁 Ritraduci: elimina la cache della pagina (motore attivo) e ritraduce."""
        if not self._pdf_path:
            return
        page = self._current_page
        engine = get_translation_engine()
        if not self._confirm_page_action(
            T("clone.purge.title"),
            T(
                "clone.fab.retranslate_confirm",
                page=page + 1,
                engine=self._engine_display(engine),
            ),
            T("clone.purge.confirm"),
        ):
            return
        self._clone_engine.purge_page_cache(engine, page)
        self._on_translate_requested()

    def _on_fab_open_external(self):
        """👁 Apri con il visualizzatore di sistema il clone della pagina."""
        if not self._pdf_path:
            return
        page = self._current_page
        engine = get_translation_engine()
        path = self._clone_engine.translated_path(page, engine)
        if not path.is_file():
            self.status_bar.showMessage(T("export.not_ready"), 5000)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _on_fab_purge(self):
        """🗑 Cancella cache della pagina (motore attivo), con conferma."""
        if not self._pdf_path:
            return
        page = self._current_page
        engine = get_translation_engine()
        if not self._confirm_page_action(
            T("clone.purge.title"),
            T(
                "clone.fab.purge_confirm",
                page=page + 1,
                engine=self._engine_display(engine),
            ),
            T("clone.purge.confirm"),
        ):
            return
        _files, size = self._clone_engine.purge_page_cache(engine, page)
        self.status_bar.showMessage(
            T("clone.fab.purged", page=page + 1, size=_fmt_bytes(size)), 6000
        )
        self._show_clone_for_page(page)

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
        if self._batch_busy:
            self.status_bar.showMessage(T("export.busy"), 5000)
            return
        self._configure_clone_engine()
        source = get_source_lang()

        dlg = ExportWizardDialog(
            self._clone_engine,
            get_translation_engine(),
            get_target_lang(),
            source,
            self._current_page,
            self._page_count,
            self._pdf_path,
            self,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        engine = dlg.chosen_engine()
        target = dlg.chosen_target()
        source = dlg.chosen_source()
        pages = dlg.chosen_pages()
        want_translate = dlg.translate_missing()
        fmt = dlg.chosen_format()
        dest = dlg.chosen_path()
        if not pages:
            self.status_bar.showMessage(T("export.none_ready"), 5000)
            return
        if engine == "llm" and want_translate and not self._ensure_llm_key():
            self.status_bar.showMessage(self._no_key_message(), 6000)
            return
        cached = [
            p
            for p in pages
            if self._clone_engine.is_cached(p, engine, source, target)
        ]
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

        # Destinazione scelta nello step "Output" del wizard, quindi già nota
        # PRIMA della traduzione: l'attesa termina con il file già scritto.
        # Engine dedicato con motore/lingua scelti: non disturba l'engine
        # condiviso col pannello destro (che può avere un thread in corso).
        translate = bool(want_translate and missing)
        export_engine = self._clone_engine
        if translate:
            export_engine = clone_engine.CloneEngine(
                cache_root=self._clone_engine.cache_root,
                pdf2zh_bin=get_setting("pdf2zh_bin", "") or None,
                max_concurrent=1,
            )
            export_engine.set_document(self._pdf_path)
            export_engine.lang_in = source
            export_engine.lang_out = target
            export_engine.llm_model = self._clone_engine.llm_model
            export_engine.llm_base_url = self._clone_engine.llm_base_url
            export_engine.llm_pool_workers = self._clone_engine.llm_pool_workers
            export_engine.fast_engine = self._clone_engine.fast_engine
            export_engine.fast_flags = self._clone_engine.fast_flags
            export_engine.llm_reasoning_effort = (
                self._clone_engine.llm_reasoning_effort
            )
            export_engine.llm_json_mode = self._clone_engine.llm_json_mode
            export_engine.fast_worker = self._clone_engine.fast_worker
            export_engine.numeric_lists = self._clone_engine.numeric_lists
            export_engine.llm_system_prompt = self._clone_engine.llm_system_prompt

        ok, count, failed = self._run_export_with_progress(
            export_engine,
            pages,
            missing,
            translate,
            engine,
            source,
            target,
            len(cached),
            dest,
            fmt,
        )
        if export_engine is not self._clone_engine:
            # L'engine dedicato all'export non serve più: chiude l'eventuale
            # worker persistente per non lasciare processi orfani.
            export_engine.close()
        if not ok:
            return
        self.status_bar.showMessage(
            T("export.done", count=count, failed=failed), 6000
        )

    def _on_export_current(self):
        """Scorciatoia rapida: esporta subito la pagina corrente in PDF.

        Salta il wizard e non traduce: se la pagina non è ancora in cache
        avvisa e non fa nulla.
        """
        if not self._pdf_path or self._mupdf_doc is None or self._page_count == 0:
            self.status_bar.showMessage(T("export.need_doc"), 4000)
            return
        if self._batch_busy:
            self.status_bar.showMessage(T("export.busy"), 5000)
            return
        self._configure_clone_engine()
        engine = get_translation_engine()
        source = get_source_lang()
        target = get_target_lang()
        page = self._current_page
        if not self._clone_engine.is_cached(page, engine, source, target):
            self.status_bar.showMessage(T("export.not_ready"), 5000)
            return
        default = f"{self._pdf_path.stem}_pag{page + 1}_{engine}_{target}.pdf"
        dest, _ = QFileDialog.getSaveFileName(
            self, T("export.dialog"), default, T("export.filter")
        )
        if not dest:
            return
        if not dest.lower().endswith(".pdf"):
            dest += ".pdf"
        try:
            count = self._clone_engine.export_pdf(
                [page], engine, dest, source, target
            )
        except Exception:  # noqa: BLE001 — riportato nella status bar
            log.exception("export pagina corrente fallito: %s", dest)
            self.status_bar.showMessage(T("export.error"), 5000)
            return
        self.status_bar.showMessage(T("export.done", count=count, failed=0), 6000)

    def _run_export_with_progress(
        self,
        engine,
        pages: list[int],
        missing: list[int],
        translate: bool,
        engine_name: str,
        source: str,
        target: str,
        cached_count: int,
        dest: str,
        fmt: str = "merged",
    ) -> tuple[bool, int, int]:
        """Translate (optionally), merge and save, with a progress window.

        La finestra **non** è application-modal: l'utente può ridurre a icona
        l'app mentre il job gira (punto 1) e viene avvisato a fine lavoro con
        notifica + suono (punto 3). Le pagine mancanti sono tradotte **una alla
        volta**, in sequenza. Lo standby è inibito durante il job (punto 4).

        Returns ``(ok, pages_written, pages_failed)``.
        """
        total = len(missing)
        prog = ExportProgressDialog(
            engine_label=T(f"engine.option.{engine_name}"),
            lang_label=flag_endonym(target),
            page_from=(min(pages) + 1) if pages else 1,
            page_to=(max(pages) + 1) if pages else 1,
            missing=total,
            cached=cached_count,
            total=max(total, 1),
            pdf_path=self._pdf_path,
            parent=self,
            range_label=page_spec.format_pages_label(pages),
        )
        # Non modale: la finestra principale resta riducibile/utilizzabile.
        prog.setWindowModality(Qt.WindowModality.NonModal)
        prog.show()

        self._start_long_job()
        try:
            return self._run_export_body(
                prog, engine, pages, missing, translate, engine_name,
                source, target, dest, fmt,
            )
        finally:
            self._end_long_job()

    def _run_export_body(
        self,
        prog,
        engine,
        pages: list[int],
        missing: list[int],
        translate: bool,
        engine_name: str,
        source: str,
        target: str,
        dest: str,
        fmt: str,
    ) -> tuple[bool, int, int]:
        """Corpo dell'export, separato per garantire il rilascio dello standby."""
        state = {"cancelled": False, "failed": {}}
        if translate and missing:
            thread = CloneExportThread(engine, missing, engine_name)

            def _on_page_error(page: int, reason: str):
                state["failed"][page] = _friendly_reason(reason)

            def _on_page_started(page: int):
                prog.set_translating(page + 1, log=False)

            def _on_progress(done: int, tot: int, page: int):
                if page in state["failed"]:
                    prog.log_fail(page, state["failed"][page])
                else:
                    prog.log_ok(page)
                prog.set_stats(done, len(state["failed"]), tot)

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
            thread.page_started.connect(_on_page_started)
            thread.progress.connect(_on_progress)
            prog.cancelled.connect(_on_cancel)

            prog.begin(missing[0] + 1, log=False)
            loop = QEventLoop()
            thread.batch_finished.connect(lambda *_: loop.quit())
            thread.start()
            loop.exec()
            thread.wait(2000)

            if state["cancelled"] or thread.is_cancelled():
                prog.set_error(T("export.cancelled"))
                prog.exec()
                self.status_bar.showMessage(T("export.cancelled"), 5000)
                self._notify_batch(T("notify.batch.cancelled"))
                return (False, 0, 0)
        else:
            prog.set_phase_exporting()

        ready = [
            p
            for p in pages
            if engine.is_cached(p, engine_name, source, target)
        ]
        if not ready:
            prog.set_error(T("export.none_ready"))
            prog.exec()
            self.status_bar.showMessage(T("export.none_ready"), 5000)
            self._notify_batch(T("notify.batch.error"))
            return (False, 0, 0)
        try:
            stem = self._pdf_path.stem if self._pdf_path else None
            if fmt == "folder":
                count = engine.export_folder(
                    ready, engine_name, dest, source, target, stem=stem
                )
            elif fmt == "zip":
                count = engine.export_zip(
                    ready, engine_name, dest, source, target, stem=stem
                )
            else:
                count = engine.export_pdf(
                    ready, engine_name, dest, source, target
                )
        except Exception:  # noqa: BLE001 — riportato nella finestra
            log.exception("export PDF fallito: %s", dest)
            prog.set_error(T("export.error"))
            prog.exec()
            self.status_bar.showMessage(T("export.error"), 5000)
            self._notify_batch(T("notify.batch.error"))
            return (False, 0, 0)
        failed = sum(
            1
            for p in missing
            if not engine.is_cached(p, engine_name, source, target)
        )
        prog.set_completed(str(dest), count, failed)
        # Avviso PRIMA di exec(): la finestra resta aperta ma l'utente può
        # essere altrove (ridotto a icona).
        if failed:
            self._notify_batch(
                T("notify.batch.partial", count=count, failed=failed)
            )
        else:
            self._notify_batch(T("notify.batch.done", count=count))
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
        """Selezione motore dal pannello destro: aggiorna la vista, non traduce.

        La traduzione parte solo dal pulsante ▶ Traduci. La traduzione
        eventualmente in corso resta attiva in background (va in cache) ma i
        suoi aggiornamenti UI vengono invalidati (generation guard).
        """
        if engine not in CLONE_ENGINES or engine == get_translation_engine():
            return
        set_translation_engine(engine)
        save_config()
        if engine == "llm" and self._ensure_llm_key(force=True):
            # Verifica informativa (non blocca): segnala subito una chiave invalida.
            self._start_key_verify(None)
        self._update_engine_banner()
        self._update_translate_tooltip()
        self._clone_generation += 1
        self._retire_clone_thread()
        self._configure_clone_engine()
        self.translated_panel.set_target_language(get_target_lang())
        if self._pdf_path:
            self._show_clone_for_page(self._current_page)

    # ── file open ─────────────────────────────────────────────────────────

    def _on_open(self):
        if self._batch_busy:
            self.status_bar.showMessage(T("export.busy"), 5000)
            return
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
        # Termina l'eventuale worker persistente del motore (Fase 2).
        with contextlib.suppress(Exception):
            self._clone_engine.close()
        # Termina l'eventuale proxy provider avviato dall'app.
        with contextlib.suppress(Exception):
            self._proxy_manager.stop()
        super().closeEvent(event)


# ═══════════════════════════════════════════════════════════════════════════════
#  entry point
# ═══════════════════════════════════════════════════════════════════════════════


def _bundled_asset(*parts: str) -> Path:
    """Percorso di un asset: dal bundle PyInstaller o dalla cartella del repo."""
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).resolve().parent
    return root.joinpath("assets", *parts)


def _app_icon() -> QIcon:
    """Icona dell'app dalle immagini di ``assets/`` (derivate dal logo).

    Windows preferisce l'``.ico`` multi-risoluzione, gli altri l'immagine PNG.
    """
    names = (
        ("noesispdf.ico", "noesispdf-256.png", "noesispdf.png")
        if sys.platform == "win32"
        else ("noesispdf-256.png", "noesispdf.png", "noesispdf.ico")
    )
    for name in names:
        path = _bundled_asset(name)
        if path.exists():
            icon = QIcon(str(path))
            if not icon.isNull():
                return icon
    return QIcon()


def _setup_logging() -> None:
    """Log su file in app-data, utile per diagnosticare su Windows (no console).

    Ruota a 2 MB × 3 file. Non solleva mai: se la cartella non è scrivibile
    l'app parte comunque senza log.
    """
    try:
        log_dir = _app_data_base() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_dir / "noesis-pdf-cloner.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
    except OSError:
        return
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.getLogger("clone_engine").setLevel(logging.DEBUG)


def main():
    # Nei build congelati (PyInstaller) punta l'OCR di PyMuPDF al Tesseract
    # incluso nel bundle (binario + librerie + tessdata) invece di richiederlo
    # come installazione di sistema.
    _setup_bundled_tesseract()

    app = QApplication(sys.argv)
    app.setApplicationName("noesis-pdf-cloner")
    # L'engine (Qt-free) usa la stessa cartella dati per-utente della GUI, così
    # il fallback del motore e l'auto-rilevamento restano coerenti.
    clone_engine.set_app_data_dir(_app_data_base())
    _setup_logging()
    logging.getLogger("main").info(
        "avvio Noesis PDF Cloner (python=%s, frozen=%s, platform=%s)",
        sys.version.split()[0], getattr(sys, "frozen", False), sys.platform,
    )
    _icon = _app_icon()
    if not _icon.isNull():
        app.setWindowIcon(_icon)

    # Chiave OpenRouter: la chiave salvata (Impostazioni) ha la precedenza, la
    # variabile di sistema OPENROUTER_API_KEY è il fallback. Registriamo anche
    # se nel sistema la variabile esiste ma non è stata ereditata (Windows).
    global _API_KEY_SOURCE, _API_KEY_ENV_AT_START, _API_KEY_ENV_STALE
    _API_KEY_ENV_AT_START = (os.environ.get(keystore.ENV_VAR) or "").strip()
    _API_KEY_SOURCE = keystore.load_into_env(_keystore_path())
    _reg_value, _reg_scope = keystore.registry_env_key()
    _API_KEY_ENV_STALE = bool(_reg_value) and not _API_KEY_ENV_AT_START

    # Config v2: al primo avvio vengono scritti i default (lingua UI = lingua
    # dell'OS o italiano); le scelte persistono tra gli aggiornamenti (la
    # cartella dati è ancorata al nome app, non alla versione).
    config_path = _config_file_path()
    cfg = init_config(config_path, defaults={**DEFAULTS, "lang": _detect_os_lang()})
    set_language(cfg["lang"])
    # Tema scelto (chiaro/scuro/sistema) PRIMA di costruire la finestra, così
    # nasce già con i colori giusti.
    apply_theme_mode(str(cfg.get("theme", "dark")))

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
