"""Temi dell'interfaccia (chiaro / scuro / sistema).

Modulo **puro Python** (nessuna dipendenza da PyQt): definisce due tavolozze di
token semantici e costruisce i fogli di stile QSS usati dalla GUI. Il modulo
``main`` sceglie il tema attivo (``set_mode``), lo persiste in ``config.json``
(chiave ``theme``) e applica i QSS restituiti da ``main_qss()``,
``settings_qss()`` e ``wizard_qss()``.

Regola: nessun colore deve restare cablato nel resto del codice. Ogni widget
usa i token tramite ``color("nome")`` oppure un QSS costruito qui. In questo
modo "chiaro" e "scuro" restano coerenti e si aggiunge un tema nuovo toccando
un solo file.

La modalità ``system`` segue il tema del sistema operativo: la risoluzione
effettiva avviene in ``main`` (``QStyleHints.colorScheme``), che chiama
``set_mode("dark"|"light")``. Qui ``system`` è trattato come ``dark``
(fallback) se non risolto.
"""

from __future__ import annotations

from string import Template

# ── tavolozze ────────────────────────────────────────────────────────────────
#
# I nomi dei token sono semantici (non il colore): ``bg`` è lo sfondo della
# finestra, ``text4`` un testo attenuato, ``accent`` il blu dei comandi, ecc.

DARK: dict[str, str] = {
    # superfici
    "bg": "#2b2b2b",
    "bg_alt": "#333333",
    "bg_bar": "#3a3a3a",
    "bg_input": "#444444",
    "bg_hover": "#555555",
    "bg_pressed": "#666666",
    "bg_menu": "#2b2b2b",
    "bg_scroll": "#2f2f2f",
    "bg_tab": "#333333",
    "bg_toast": "#1f1f1f",
    # bordi
    "border": "#444444",
    "border2": "#555555",
    "border3": "#666666",
    # testi
    "text": "#eeeeee",
    "text2": "#dddddd",
    "text3": "#cccccc",
    "text4": "#aaaaaa",
    "text5": "#999999",
    "text_muted": "#93a0b4",
    "text_inv": "#ffffff",
    # accent / stati
    "accent": "#3a6bc5",
    "accent_hover": "#4a7bd5",
    "accent_text": "#ffffff",
    "sel_bg": "#3a6bc5",
    "sel_text": "#ffffff",
    "ok": "#7fd67f",
    "warn": "#ffcc66",
    "err": "#ff6b6b",
    "disabled_bg": "#555555",
    "disabled_text": "#999999",
    # pannello clone
    "bar_bg": "#3a3a3a",
    "bar_border": "#555555",
    "radio_border": "#888888",
    "radio_bg": "#444444",
    "spinner_bg": "rgba(20, 20, 20, 230)",
    "spinner_text": "#ffffff",
    "spinner_border": "#666666",
    "collapse_bg": "rgba(58, 58, 58, 220)",
    "collapse_text": "#dddddd",
    "collapse_border": "#666666",
    "fab_bg": "#1e3452",
    "fab_text": "#ffffff",
    "fab_border": "#5b9bff",
    "fab_hover_bg": "#27436b",
    "fab_hover_border": "#7cb2ff",
    "fab_glow": "#4800f0",
    # riflesso "scintilla" sul FAB: bianco traslucido sul tema scuro
    "fab_spark": "#d9ffffff",
    "badge": "#4a90d9",
    # menu a comparsa del FAB: fondo a gradiente + bordo a rilievo (3D)
    "menu_bg": "#2b2b2b",
    "menu_bg_top": "#3d3d3d",
    "menu_bg_bottom": "#232323",
    "menu_text": "#e8e8e8",
    "menu_border": "#555555",
    "menu_border_hi": "#6a6a6a",
    "menu_border_lo": "#141414",
    "menu_disabled": "#777777",
    # strisce informative
    "banner_engine_bg": "#4a3f1c",
    "banner_engine_border": "#6d5c28",
    "banner_engine_text": "#ffd873",
    "banner_engine_btn": "#b5892a",
    "banner_engine_btn_hover": "#d0a03a",
    "banner_engine_btn_text": "#1a1a1a",
    "banner_key_bg": "#1c2a4a",
    "banner_key_border": "#28406d",
    "banner_key_text": "#9dc0ff",
    "banner_key_btn": "#2a5db5",
    "banner_key_btn_hover": "#3a78d0",
    "banner_key_btn2": "#3a3a3a",
    "banner_key_btn2_text": "#eaeaea",
    "banner_key_btn2_hover": "#4a4a4a",
    # toc / alberi
    "tree_bg": "#2b2b2b",
    "tree_text": "#dddddd",
    "tree_sel_bg": "#3a6bc5",
    "tree_sel_text": "#ffffff",
    # wizard (palette bluastra del servizio)
    "wiz_bg": "#171b23",
    "wiz_header": "#171b23",
    "wiz_line": "#2a3242",
    "wiz_panel": "#1e2430",
    "wiz_title": "#e7ecf3",
    "wiz_muted": "#93a0b4",
    "wiz_accent": "#4f8cff",
    "wiz_accent_hover": "#6ba0ff",
    "wiz_ok": "#35d0a5",
    "wiz_ok_text": "#06231b",
    "wiz_sel_bg": "#1d3350",
    "wiz_hover_border": "#3a465e",
    "wiz_ind_border": "#5a6b86",
    "wiz_primary_text": "#08152e",
    "wiz_thumb_bg": "#f7f4ee",
    "wiz_err": "#ff6b6b",
    # anteprima "liquida"
    "liquid_bg": "#ee0b0e13",
    "liquid_caption_bg": "#e6141414",
    "liquid_text": "#ffffff",
    "liquid_bar": "#e64f8cff",
}

LIGHT: dict[str, str] = {
    # superfici
    "bg": "#f2f3f5",
    "bg_alt": "#e9ebef",
    "bg_bar": "#e2e5ea",
    "bg_input": "#ffffff",
    "bg_hover": "#d7dbe2",
    "bg_pressed": "#c8cdd6",
    "bg_menu": "#ffffff",
    "bg_scroll": "#e6e8ec",
    "bg_tab": "#e2e5ea",
    "bg_toast": "#ffffff",
    # bordi
    "border": "#c9ced6",
    "border2": "#b6bcc6",
    "border3": "#9aa1ac",
    # testi
    "text": "#1c2430",
    "text2": "#2a3442",
    "text3": "#39434f",
    "text4": "#4d5866",
    "text5": "#6a7480",
    "text_muted": "#5a6a86",
    "text_inv": "#ffffff",
    # accent / stati
    "accent": "#2f6fe0",
    "accent_hover": "#4a86f0",
    "accent_text": "#ffffff",
    "sel_bg": "#2f6fe0",
    "sel_text": "#ffffff",
    "ok": "#1f8a4c",
    "warn": "#b06a00",
    "err": "#c0392b",
    "disabled_bg": "#d7dbe2",
    "disabled_text": "#9aa1ac",
    # pannello clone
    "bar_bg": "#e2e5ea",
    "bar_border": "#c9ced6",
    "radio_border": "#8a919c",
    "radio_bg": "#ffffff",
    "spinner_bg": "rgba(255, 255, 255, 235)",
    "spinner_text": "#1c2430",
    "spinner_border": "#b6bcc6",
    "collapse_bg": "rgba(226, 229, 234, 235)",
    "collapse_text": "#2a3442",
    "collapse_border": "#b6bcc6",
    "fab_bg": "#ffffff",
    "fab_text": "#12305f",
    "fab_border": "#2f6fe0",
    "fab_hover_bg": "#eaf1ff",
    "fab_hover_border": "#1e6bff",
    "fab_glow": "#1e6bff",
    # riflesso "scintilla" sul FAB: blu traslucido (il bianco sarebbe
    # invisibile sul pulsante chiaro appoggiato a una pagina bianca)
    "fab_spark": "#8c2f6fe0",
    "badge": "#2f6fe0",
    # menu a comparsa del FAB: fondo a gradiente + bordo a rilievo (3D)
    "menu_bg": "#ffffff",
    "menu_bg_top": "#ffffff",
    "menu_bg_bottom": "#e9edf3",
    "menu_text": "#1c2430",
    "menu_border": "#9aa3b0",
    "menu_border_hi": "#c3cad4",
    "menu_border_lo": "#7f8895",
    "menu_disabled": "#9aa1ac",
    # strisce informative
    "banner_engine_bg": "#fdf3d6",
    "banner_engine_border": "#e6c766",
    "banner_engine_text": "#7a5200",
    "banner_engine_btn": "#c9961f",
    "banner_engine_btn_hover": "#ddad35",
    "banner_engine_btn_text": "#1a1a1a",
    "banner_key_bg": "#e3ecff",
    "banner_key_border": "#a8c1f0",
    "banner_key_text": "#1c3f8f",
    "banner_key_btn": "#2f6fe0",
    "banner_key_btn_hover": "#4a86f0",
    "banner_key_btn2": "#e2e5ea",
    "banner_key_btn2_text": "#1c2430",
    "banner_key_btn2_hover": "#d7dbe2",
    # toc / alberi
    "tree_bg": "#ffffff",
    "tree_text": "#2a3442",
    "tree_sel_bg": "#2f6fe0",
    "tree_sel_text": "#ffffff",
    # wizard
    "wiz_bg": "#f4f6fa",
    "wiz_header": "#ffffff",
    "wiz_line": "#d7deea",
    "wiz_panel": "#ffffff",
    "wiz_title": "#1a2230",
    "wiz_muted": "#5a6a86",
    "wiz_accent": "#2f6fe0",
    "wiz_accent_hover": "#4a86f0",
    "wiz_ok": "#1f9d6b",
    "wiz_ok_text": "#ffffff",
    "wiz_sel_bg": "#dbe8ff",
    "wiz_hover_border": "#a9b7cc",
    "wiz_ind_border": "#8a919c",
    "wiz_primary_text": "#ffffff",
    "wiz_thumb_bg": "#fbfaf7",
    "wiz_err": "#c0392b",
    # anteprima "liquida"
    "liquid_bg": "#e6f2f3f5",
    "liquid_caption_bg": "#e6ffffff",
    "liquid_text": "#1c2430",
    "liquid_bar": "#e62f6fe0",
}

PALETTES: dict[str, dict[str, str]] = {"dark": DARK, "light": LIGHT}

_VALID_MODES = ("dark", "light", "system")
_mode: str = "dark"
_resolved: str = "dark"


def normalize_mode(mode: str) -> str:
    """Normalizza una modalità di tema (default ``dark``)."""
    code = (mode or "").strip().lower()
    return code if code in _VALID_MODES else "dark"


def set_mode(mode: str) -> None:
    """Imposta la modalità richiesta (``dark``/``light``/``system``).

    ``system`` viene conservato come richiesta; la risoluzione effettiva va fatta
    dal chiamante con ``set_resolved`` (PyQt non è disponibile qui).
    """
    global _mode, _resolved
    _mode = normalize_mode(mode)
    if _mode != "system":
        _resolved = _mode


def set_resolved(mode: str) -> None:
    """Imposta la palette effettiva quando la modalità richiesta è ``system``."""
    global _resolved
    _resolved = normalize_mode(mode)


def mode() -> str:
    """Modalità richiesta: ``dark``, ``light`` o ``system``."""
    return _mode


def resolved() -> str:
    """Modalità effettiva usata per i colori (``dark`` o ``light``)."""
    return _resolved if _resolved in ("dark", "light") else "dark"


def is_dark() -> bool:
    return resolved() == "dark"


def palette() -> dict[str, str]:
    """Tavolozza effettiva (dict token → colore)."""
    return dict(PALETTES.get(resolved(), DARK))


def color(token: str, fallback: str = "#888888") -> str:
    """Colore del token nella palette attiva (fallback se il token manca)."""
    return PALETTES.get(resolved(), DARK).get(token, fallback)


# ── QSS: main window ─────────────────────────────────────────────────────────

_MAIN_QSS = Template(
    """
    QMainWindow { background: $bg; }
    QWidget { selection-background-color: $sel_bg; selection-color: $sel_text; }
    QToolBar {
        background: $bg_alt; padding: 4px; spacing: 6px;
        border-bottom: 1px solid $border;
    }
    QToolBar QPushButton {
        background: $bg_input; color: $text; border: 1px solid $border2;
        border-radius: 4px; padding: 6px 14px; font-size: 13px;
    }
    QToolBar QPushButton:hover { background: $bg_hover; }
    QToolBar QPushButton:pressed { background: $bg_pressed; }
    QToolBar QPushButton:checked { background: $accent; color: $accent_text; }
    QToolBar QSpinBox {
        background: $bg_input; color: $text; border: 1px solid $border2;
        border-radius: 4px; padding: 4px 8px; font-size: 13px;
        min-width: 60px;
    }
    /* Page-number box: no up/down buttons (they made the widget look
       cluttered); navigation is via ◀ ▶ or by typing a page number. */
    QToolBar QSpinBox::up-button, QToolBar QSpinBox::down-button {
        width: 0px; border: none; background: transparent;
    }
    QToolBar QLabel { color: $text3; font-size: 13px; }
    QStatusBar { background: $bg_alt; color: $text4; }

    /* Punti che altrimenti ereditano il tema di sistema: colori espliciti
       così l'app resta coerente su qualunque impostazione del sistema. */
    QScrollArea { background: $bg; border: none; }
    QSplitter::handle { background: $bg_alt; }
    QDockWidget { color: $text2; }
    QDockWidget::title {
        background: $bg_alt; color: $text2; padding: 5px 8px;
        text-align: left;
    }
    QDockWidget::close-button, QDockWidget::float-button {
        background: $bg_input; border: none; border-radius: 2px;
    }
    QDockWidget::close-button:hover,
    QDockWidget::float-button:hover { background: $bg_hover; }
    QScrollBar:vertical {
        background: $bg_scroll; width: 12px; margin: 0;
    }
    QScrollBar::handle:vertical {
        background: $bg_hover; min-height: 24px;
        border-radius: 6px; margin: 2px;
    }
    QScrollBar::handle:vertical:hover { background: $bg_pressed; }
    QScrollBar:horizontal {
        background: $bg_scroll; height: 12px; margin: 0;
    }
    QScrollBar::handle:horizontal {
        background: $bg_hover; min-width: 24px;
        border-radius: 6px; margin: 2px;
    }
    QScrollBar::handle:horizontal:hover { background: $bg_pressed; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
        height: 0; width: 0;
    }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
        background: none;
    }
    QToolTip {
        background: $bg_menu; color: $text; border: 1px solid $border2;
        padding: 4px 6px;
    }
    """
)


def main_qss() -> str:
    """Foglio di stile della finestra principale."""
    return _MAIN_QSS.substitute(palette())


# ── QSS: dialoghi (Impostazioni, chiave, progresso) ──────────────────────────

_SETTINGS_QSS = Template(
    """
    QDialog { background: $bg; }
    QGroupBox { color: $text; border: 1px solid $border2; border-radius: 6px;
                margin-top: 10px; padding-top: 8px; font-size: 13px; }
    QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
    QLabel { color: $text3; font-size: 13px; }
    QComboBox, QSpinBox, QDoubleSpinBox {
        background: $bg_input; color: $text; border: 1px solid $border2;
        border-radius: 4px; padding: 4px 8px; font-size: 13px;
        min-width: 220px;
    }
    QComboBox QAbstractItemView { background: $bg_input; color: $text;
        selection-background-color: $sel_bg; selection-color: $sel_text; }
    QLineEdit { background: $bg_input; color: $text; border: 1px solid $border2;
        border-radius: 4px; padding: 4px 8px; font-size: 13px; }
    QPlainTextEdit { background: $bg_input; color: $text;
        border: 1px solid $border2; }
    QCheckBox { color: $text2; font-size: 13px; spacing: 8px; }
    QRadioButton { color: $text2; font-size: 13px; spacing: 8px; }
    QRadioButton::indicator {
        width: 13px; height: 13px; border: 1px solid $radio_border;
        border-radius: 7px; background: $radio_bg;
    }
    QRadioButton::indicator:checked {
        background: $accent; border-color: $accent;
    }
    QPushButton {
        background: $bg_input; color: $text; border: 1px solid $border2;
        border-radius: 4px; padding: 6px 18px; font-size: 13px;
    }
    QPushButton:hover { background: $bg_hover; }
    QPushButton:pressed { background: $bg_pressed; }
    QProgressBar {
        background: $bg_input; color: $text; border: 1px solid $border2;
        border-radius: 4px; text-align: center;
    }
    QProgressBar::chunk { background: $accent; border-radius: 3px; }
    QScrollArea#settingsScroll { background: transparent; border: none; }
    QScrollArea#settingsScroll > QWidget > QWidget { background: transparent; }
    QWidget#settingsContent { background: transparent; }
    """
)


def settings_qss() -> str:
    """Foglio di stile dei dialoghi (Impostazioni, chiave, progresso export)."""
    return _SETTINGS_QSS.substitute(palette())


# ── QSS: wizard di esportazione ──────────────────────────────────────────────

_WIZARD_QSS = Template(
    """
    QDialog { background: $wiz_bg; }
    QScrollArea#wizScroll, QWidget#wizViewport, QWidget#wizPane {
        background: transparent; border: none;
    }
    QWidget#wizHeader { background: $wiz_header; border-bottom: 1px solid $wiz_line; }
    QLabel#wizTitle { color: $wiz_title; font-size: 14px; font-weight: 600; }
    QPushButton#wizX { background: transparent; border: 0; color: $wiz_muted;
                       font-size: 17px; padding: 0 6px; }
    QPushButton#wizX:hover { color: $wiz_title; }
    QWidget#wizSteps { background: $wiz_panel; border-bottom: 1px solid $wiz_line; }
    QLabel#wizStepNum {
        color: $wiz_muted; border: 1px solid $wiz_line; border-radius: 10px;
        min-width: 20px; max-width: 20px; min-height: 20px; max-height: 20px;
        font-size: 11px;
    }
    QLabel#wizStepName { color: $wiz_muted; font-size: 12px; }
    QWidget#wizStep[state="on"] QLabel#wizStepNum {
        border-color: $wiz_accent; color: $wiz_accent;
    }
    QWidget#wizStep[state="on"] QLabel#wizStepName { color: $wiz_title; }
    QWidget#wizStep[state="done"] QLabel#wizStepNum {
        background: $wiz_ok; border-color: $wiz_ok; color: $wiz_ok_text;
    }
    QWidget#wizStep[state="done"] QLabel#wizStepName { color: $wiz_title; }
    QFrame#wizSep { background: $wiz_line; max-height: 1px; }
    QLabel#wizH3 { color: $wiz_title; font-size: 14px; font-weight: 600; }
    QLabel#wizHint { color: $wiz_muted; font-size: 12.5px; }
    QLabel#wizThumb { background: $wiz_thumb_bg; border: 1px solid $wiz_line; border-radius: 4px; }
    QLabel#wizThumbBig { background: $wiz_thumb_bg; border: 1px solid $wiz_line; border-radius: 6px; }
    QWidget#wizCard {
        background: $wiz_panel; border: 1px solid $wiz_line; border-radius: 11px;
    }
    QPushButton#wizCardOpt {
        background: $wiz_panel; border: 1px solid $wiz_line; color: $wiz_title;
        border-radius: 11px; padding: 12px; text-align: left; font-size: 13px;
    }
    QPushButton#wizCardOpt:hover { border-color: $wiz_hover_border; }
    QPushButton#wizCardOpt:checked { border-color: $wiz_accent; background: $wiz_sel_bg; }
    QLabel#wizEst { background: $wiz_panel; border: 1px solid $wiz_line; border-radius: 11px;
                    padding: 12px 14px; color: $wiz_muted; font-size: 12.5px; }
    QLabel#wizSum { background: $wiz_panel; border: 1px solid $wiz_line; border-radius: 11px;
                    padding: 14px; color: $wiz_title; font-size: 13px; }
    QLineEdit, QComboBox, QSpinBox {
        background: $wiz_panel; border: 1px solid $wiz_line; color: $wiz_title;
        border-radius: 9px; padding: 8px 10px; font-size: 13px;
    }
    QSpinBox#wizSpin { padding: 6px 4px; font-size: 13px; }
    QFrame#wizModeSep { background: $wiz_line; border: none; max-width: 1px; }
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: $wiz_accent; }
    QComboBox QAbstractItemView { background: $wiz_panel; color: $wiz_title;
        selection-background-color: $wiz_accent; selection-color: $wiz_primary_text; }
    QCheckBox, QRadioButton { color: $wiz_title; font-size: 13px; spacing: 8px; }
    QCheckBox::indicator {
        width: 14px; height: 14px; border: 1px solid $wiz_ind_border;
        border-radius: 4px; background: $wiz_panel;
    }
    QCheckBox::indicator:checked { background: $wiz_accent; border-color: $wiz_accent; }
    QRadioButton::indicator {
        width: 13px; height: 13px; border: 1px solid $wiz_ind_border;
        border-radius: 7px; background: $wiz_panel;
    }
    QRadioButton::indicator:checked { background: $wiz_accent; border-color: $wiz_accent; }
    QPushButton#wizSeg {
        background: $wiz_bg; border: 1px solid $wiz_line; color: $wiz_muted;
        border-radius: 9px; padding: 8px 14px; font-size: 12.5px;
    }
    QPushButton#wizSeg:hover { color: $wiz_title; }
    QPushButton#wizSeg:checked { background: $wiz_panel; color: $wiz_title; border-color: $wiz_accent; }
    QWidget#wizFooter { background: $wiz_panel; border-top: 1px solid $wiz_line; }
    QLabel#wizError { color: $wiz_err; font-size: 12px; }
    QPushButton#wizGhost { background: transparent; border: 0; color: $wiz_muted;
        padding: 9px 12px; font-size: 13px; }
    QPushButton#wizGhost:hover { color: $wiz_title; }
    QPushButton#wizBtn { background: $wiz_bg; border: 1px solid $wiz_line; color: $wiz_title;
        border-radius: 9px; padding: 9px 16px; font-size: 13px; }
    QPushButton#wizBtn:hover { border-color: $wiz_accent; }
    QPushButton#wizPrimary { background: $wiz_accent; border: 1px solid $wiz_accent;
        color: $wiz_primary_text; border-radius: 9px; padding: 9px 16px; font-size: 13px;
        font-weight: 700; }
    QPushButton#wizPrimary:hover { background: $wiz_accent_hover; }
    """
)


def wizard_qss() -> str:
    """Foglio di stile del wizard di esportazione."""
    return _WIZARD_QSS.substitute(palette())
