"""UI internationalization — lightweight dict-based i18n (no Qt dependency).

The UI chrome (buttons, tooltips, status bar, dialogs) is looked up through
``T(key, **fmt)`` against ``_STRINGS``. The chosen language is a module-level
global switched at runtime (``set_language``); the choice is persisted to a
small JSON file by the application (this module only receives paths, so it
stays pure and testable without a QApplication).

The language is loaded before the UI is built, and each widget exposes a
``retranslate()`` method that re-applies the strings, enabling on-the-fly
switching without restarting.

PyInstaller note: translations live inside this module (no external resource
files), so ``--onefile`` bundles them automatically via the import.
"""

import json
import os
from pathlib import Path

__all__ = [
    "LANGUAGES",
    "TRANSLATION_LANGUAGES",
    "TRANSLATION_ENGINES",
    "DEFAULTS",
    "T",
    "get_language",
    "set_language",
    "get_source_lang",
    "set_source_lang",
    "get_target_lang",
    "set_target_lang",
    "get_translation_engine",
    "set_translation_engine",
    "flag_endonym",
    "get_config",
    "get_setting",
    "set_setting",
    "init_config",
    "load_config",
    "save_config",
    "load_language",
    "save_language",
    "ensure_config",
]

# Language codes → display names (also used for the toolbar selector).
LANGUAGES: dict[str, str] = {
    "it": "🇮🇹 Italiano",
    "en": "🇬🇧 English",
    "fr": "🇫🇷 Français",
    "de": "🇩🇪 Deutsch",
    "es": "🇪🇸 Español",
}

# Translation languages (source/target). ``auto`` is allowed for the source
# only. Values are (flag, endonym): the endonym is the language's own name,
# invariant with respect to the UI language, e.g. "🇫🇷 Français".
TRANSLATION_LANGUAGES: dict[str, tuple[str, str]] = {
    "auto": ("🌐", "Auto"),  # rilevamento automatico (solo sorgente)
    "en": ("🇬🇧", "English"),
    "it": ("🇮🇹", "Italiano"),
    "fr": ("🇫🇷", "Français"),
    "de": ("🇩🇪", "Deutsch"),
    "es": ("🇪🇸", "Español"),
    "pt": ("🇵🇹", "Português"),
    "nl": ("🇳🇱", "Nederlands"),
    "pl": ("🇵🇱", "Polski"),
    "ru": ("🇷🇺", "Русский"),
    "zh": ("🇨🇳", "中文"),
    "ja": ("🇯🇵", "日本語"),
    "ko": ("🇰🇷", "한국어"),
    "ar": ("🇸🇦", "العربية"),
    "tr": ("🇹🇷", "Türkçe"),
}

# Translation engines selectable in the settings dialog (label via T()).
# I tre motori di pdfcloner: catena gratuita Google, Bing, LLM via OpenRouter.
TRANSLATION_ENGINES: tuple[str, ...] = ("google", "bing", "openai")

# Default configuration (config.json schema v2). ``last_tab``/``last_pages``
# are runtime state persisted alongside the user settings.
DEFAULTS: dict = {
    "lang": "it",           # lingua UI (LANGUAGES)
    "src_lang": "auto",     # origine traduzione (TRANSLATION_LANGUAGES)
    "dst_lang": "it",       # destinazione traduzione (TRANSLATION_LANGUAGES, no auto)
    "engine": "google",     # motore di traduzione (TRANSLATION_ENGINES)
    "pdf2zh_bin": "",        # percorso pdf2zh_next (vuoto = auto-rilevamento)
    "clone_bar_collapsed": False,  # barra motori del pannello destro collassata
    "zoom": 3.0,             # risoluzione base del render (0.5–4.0);
                           # lo zoom visibile è runtime (1.0 = adatta)
    "render_md": True,       # rendering Markdown on/off
    "show_header": True,     # riga "── Backend … Fix: …" on/off
    "remember_tab": True,    # riapri il pannello sull'ultima tab usata
    "resume_last_page": True,  # riprendi dall'ultima pagina del documento
    "save_edits": True,      # salva le modifiche ai testi (per documento)
    "font_size": 12,         # dimensione font testo estratto (10–16 pt)
    "last_tab": "original",  # ultima tab attiva (original|translated|images)
    "last_pages": {},        # nome.pdf → ultima pagina (max 20, LRU)
}

_current: str = "it"
_CONFIG: dict = dict(DEFAULTS)
_CONFIG_PATH: str | None = None


def get_language() -> str:
    """Return the currently active language code."""
    return _current


def set_language(code: str) -> None:
    """Switch the active language at runtime (no-op for unknown codes)."""
    global _current
    if code in LANGUAGES:
        _current = code
        _CONFIG["lang"] = code


def get_source_lang() -> str:
    """Return the document (source) language for translation."""
    return _CONFIG.get("src_lang", "auto")


def set_source_lang(code: str) -> None:
    """Set the document (source) language (validated against the list)."""
    if code in TRANSLATION_LANGUAGES:
        _CONFIG["src_lang"] = code


def get_target_lang() -> str:
    """Return the translation (target) language."""
    return _CONFIG.get("dst_lang", "it")


def set_target_lang(code: str) -> None:
    """Set the translation (target) language (auto not allowed)."""
    if code in TRANSLATION_LANGUAGES and code != "auto":
        _CONFIG["dst_lang"] = code


def get_translation_engine() -> str:
    """Return the active translation engine id (google|microsoft)."""
    return _CONFIG.get("engine", "google")


def set_translation_engine(code: str) -> None:
    """Set the translation engine (validated against the known list)."""
    if code in TRANSLATION_ENGINES:
        _CONFIG["engine"] = code


def flag_endonym(code: str) -> str:
    """Flag + endonym for a translation language, e.g. "🇫🇷 Français"."""
    flag, name = TRANSLATION_LANGUAGES.get(code, ("", code))
    return f"{flag} {name}".strip()


# ═══════════════════════════════════════════════════════════════════════════════
#  strings — every key must exist in every language
# ═══════════════════════════════════════════════════════════════════════════════

_STRINGS: dict[str, dict[str, str]] = {
    # ── main toolbar ────────────────────────────────────────────────────────
    "toolbar.nav": {
        "it": "Navigazione", "en": "Navigation", "fr": "Navigation",
        "de": "Navigation", "es": "Navegación",
    },
    "toolbar.open": {
        "it": "📂 Apri PDF", "en": "📂 Open PDF", "fr": "📂 Ouvrir un PDF",
        "de": "📂 PDF öffnen", "es": "📂 Abrir PDF",
    },
    "toolbar.toc": {
        "it": "📑 Indice", "en": "📑 Index", "fr": "📑 Sommaire",
        "de": "📑 Inhaltsverzeichnis", "es": "📑 Índice",
    },
    "toolbar.toc.tip": {
        "it": "Mostra/nascondi l'indice (TOC) del PDF",
        "en": "Show/hide the PDF table of contents (TOC)",
        "fr": "Afficher/masquer la table des matières (TOC) du PDF",
        "de": "Inhaltsverzeichnis (TOC) des PDF ein-/ausblenden",
        "es": "Mostrar/ocultar el índice (TOC) del PDF",
    },
    "toolbar.prev": {
        "it": "◀ Prec.", "en": "◀ Prev.", "fr": "◀ Préc.",
        "de": "◀ Zurück", "es": "◀ Ant.",
    },
    "toolbar.next": {
        "it": "Succ. ▶", "en": "Next ▶", "fr": "Suiv. ▶",
        "de": "Weiter ▶", "es": "Sig. ▶",
    },
    "toolbar.of": {
        "it": "di", "en": "of", "fr": "de", "de": "von", "es": "de",
    },
    "toolbar.zoom_out.tip": {
        "it": "Riduci zoom (Ctrl+-)", "en": "Zoom out (Ctrl+-)",
        "fr": "Zoom arrière (Ctrl+-)", "de": "Verkleinern (Strg+-)",
        "es": "Alejar (Ctrl+-)",
    },
    "toolbar.zoom_in.tip": {
        "it": "Aumenta zoom (Ctrl++)", "en": "Zoom in (Ctrl++)",
        "fr": "Zoom avant (Ctrl++)", "de": "Vergrößern (Strg++)",
        "es": "Acercar (Ctrl++)",
    },
    "toolbar.zoom.scale": {
        "it": "Scala: {x}x", "en": "Scale: {x}x", "fr": "Échelle : {x}x",
        "de": "Skalierung: {x}x", "es": "Escala: {x}x",
    },
    "toolbar.md.on": {
        "it": "📝 MD ✓", "en": "📝 MD ✓", "fr": "📝 MD ✓",
        "de": "📝 MD ✓", "es": "📝 MD ✓",
    },
    "toolbar.md.plain": {
        "it": "📝 Plain", "en": "📝 Plain", "fr": "📝 Texte brut",
        "de": "📝 Klartext", "es": "📝 Texto plano",
    },
    "toolbar.md.tip": {
        "it": "Attiva/disattiva rendering Markdown → HTML\n(Ctrl+M per toggle)",
        "en": "Toggle Markdown rendering → HTML\n(Ctrl+M to toggle)",
        "fr": "Activer/désactiver le rendu Markdown → HTML\n(Ctrl+M pour basculer)",
        "de": "Markdown-Rendering → HTML ein-/ausschalten\n(Strg+M zum Umschalten)",
        "es": "Activar/desactivar el renderizado Markdown → HTML\n(Ctrl+M para alternar)",
    },
    "toolbar.help": {
        "it": "❓ Guida", "en": "❓ Help", "fr": "❓ Aide",
        "de": "❓ Hilfe", "es": "❓ Ayuda",
    },
    "toolbar.help.tip": {
        "it": "Apre la guida online nel browser",
        "en": "Opens the online help in the browser",
        "fr": "Ouvre l'aide en ligne dans le navigateur",
        "de": "Öffnet die Online-Hilfe im Browser",
        "es": "Abre la ayuda en línea en el navegador",
    },
    # ── page toolbar ────────────────────────────────────────────────────────
    "page_toolbar.title": {
        "it": "Pagina", "en": "Page", "fr": "Page", "de": "Seite", "es": "Página",
    },
    "page_toolbar.select": {
        "it": "🖱️ Seleziona zona", "en": "🖱️ Select region",
        "fr": "🖱️ Sélectionner une zone", "de": "🖱️ Bereich auswählen",
        "es": "🖱️ Seleccionar zona",
    },
    "page_toolbar.select.tip": {
        "it": "Trascina col mouse una zona della pagina\nper estrarne l'immagine nella tab 🖼️ Immagini",
        "en": "Drag a page region with the mouse\nto extract its image into the 🖼️ Images tab",
        "fr": "Glissez une zone de la page avec la souris\npour extraire son image dans l'onglet 🖼️ Images",
        "de": "Ziehen Sie mit der Maus einen Bereich der Seite,\num sein Bild in den Tab 🖼️ Bilder zu extrahieren",
        "es": "Arrastra una zona de la página con el ratón\npara extraer su imagen en la pestaña 🖼️ Imágenes",
    },
    "page_toolbar.exclude": {
        "it": "🚫 Escludi zona", "en": "🚫 Exclude region",
        "fr": "🚫 Exclure une zone", "de": "🚫 Bereich ausschließen",
        "es": "🚫 Excluir zona",
    },
    "page_toolbar.exclude.tip": {
        "it": "Trascina col mouse una zona (header, footer, immagine,\ndidascalia…) per escluderla: il motore adattativo riordina\nil testo rimanente. È aggiuntivo al sistema automatico.\n\nSe la zona contiene un'immagine, viene estratta anche nella\ntab 🖼️ Immagini (escludi + estrai in un solo gesto).",
        "en": "Drag a region (header, footer, image, caption…) with the\nmouse to exclude it: the adaptive engine reorders the\nremaining text. It adds to the automatic system.\n\nIf the region contains an image, it is also extracted into\nthe 🖼️ Images tab (exclude + extract in one gesture).",
        "fr": "Glissez une zone (en-tête, pied de page, image,\nlégende…) pour l'exclure : le moteur adaptatif réordonne\nle texte restant. C'est un ajout au système automatique.\n\nSi la zone contient une image, elle est aussi extraite dans\nl'onglet 🖼️ Images (exclure + extraire en un seul geste).",
        "de": "Ziehen Sie einen Bereich (Kopfzeile, Fußzeile, Bild,\nBildunterschrift…) zum Ausschließen: Die adaptive Engine\nordnet den verbleibenden Text neu. Ergänzend zum\nautomatischen System.\n\nEnthält der Bereich ein Bild, wird es auch in den Tab\n🖼️ Bilder extrahiert (Ausschließen + Extrahieren in einem\nSchritt).",
        "es": "Arrastra una zona (encabezado, pie de página, imagen,\nleyenda…) para excluirla: el motor adaptativo reordena\nel texto restante. Es adicional al sistema automático.\n\nSi la zona contiene una imagen, también se extrae en la\npestaña 🖼️ Imágenes (excluir + extraer en un solo gesto).",
    },
    "page_toolbar.include": {
        "it": "🟩 Includi zona", "en": "🟩 Include region",
        "fr": "🟩 Inclure une zone", "de": "🟩 Bereich einschließen",
        "es": "🟩 Incluir zona",
    },
    "page_toolbar.include.tip": {
        "it": "Trascina col mouse i box verdi nell'ordine di lettura che vuoi:\nil testo verrà ricostruito seguendo la numerazione (1, 2, 3…).\nUn box verde = una colonna/regione di lettura.",
        "en": "Drag the green boxes with the mouse in the reading order\nyou want: the text is rebuilt following the numbering (1, 2, 3…).\nOne green box = one reading column/region.",
        "fr": "Glissez les boîtes vertes avec la souris dans l'ordre de\nlecture souhaité : le texte est reconstruit selon la\nnumérotation (1, 2, 3…). Une boîte verte = une\ncolonne/région de lecture.",
        "de": "Ziehen Sie die grünen Boxen mit der Maus in der\ngewünschten Lesereihenfolge: Der Text wird gemäß der\nNummerierung (1, 2, 3…) neu aufgebaut. Eine grüne Box =\neine Lesespalte/-region.",
        "es": "Arrastra los recuadros verdes con el ratón en el orden de\nlectura que quieras: el texto se reconstruye siguiendo la\nnumeración (1, 2, 3…). Un recuadro verde = una\ncolumna/región de lectura.",
    },
    "page_toolbar.reset": {
        "it": "🧹 Reset zone", "en": "🧹 Reset zones",
        "fr": "🧹 Réinitialiser les zones", "de": "🧹 Zonen zurücksetzen",
        "es": "🧹 Restablecer zonas",
    },
    "page_toolbar.reset.tip": {
        "it": "Rimuove tutte le zone (rosse e verdi) dalla pagina corrente",
        "en": "Removes all zones (red and green) from the current page",
        "fr": "Supprime toutes les zones (rouges et vertes) de la page courante",
        "de": "Entfernt alle Zonen (rot und grün) von der aktuellen Seite",
        "es": "Elimina todas las zonas (rojas y verdes) de la página actual",
    },
    # ── right panel tabs ────────────────────────────────────────────────────
    "tab.original": {
        "it": "📄 Originale", "en": "📄 Original", "fr": "📄 Original",
        "de": "📄 Original", "es": "📄 Original",
    },
    "tab.images": {
        "it": "🖼️ Immagini", "en": "🖼️ Images", "fr": "🖼️ Images",
        "de": "🖼️ Bilder", "es": "🖼️ Imágenes",
    },
    # ── text editor mini-toolbar ────────────────────────────────────────────
    "editor.decrease": {
        "it": "Riduci testo", "en": "Decrease text size",
        "fr": "Réduire le texte", "de": "Text verkleinern", "es": "Reducir texto",
    },
    "editor.increase": {
        "it": "Aumenta testo", "en": "Increase text size",
        "fr": "Agrandir le texte", "de": "Text vergrößern", "es": "Aumentar texto",
    },
    "editor.reset": {
        "it": "Ripristina dimensione", "en": "Reset size",
        "fr": "Réinitialiser la taille", "de": "Größe zurücksetzen",
        "es": "Restablecer tamaño",
    },
    "editor.export": {
        "it": "Esporta testo", "en": "Export text",
        "fr": "Exporter le texte", "de": "Text exportieren",
        "es": "Exportar texto",
    },
    "editor.export_dialog": {
        "it": "Salva testo come...", "en": "Save text as...",
        "fr": "Enregistrer le texte sous...", "de": "Text speichern unter...",
        "es": "Guardar texto como...",
    },
    "editor.export_filter": {
        "it": "Markdown (*.md);;Testo (*.txt)",
        "en": "Markdown (*.md);;Text (*.txt)",
        "fr": "Markdown (*.md);;Texte (*.txt)",
        "de": "Markdown (*.md);;Text (*.txt)",
        "es": "Markdown (*.md);;Texto (*.txt)",
    },
    "editor.export_error": {
        "it": "❌ Errore di esportazione", "en": "❌ Export error",
        "fr": "❌ Erreur d'exportation", "de": "❌ Exportfehler",
        "es": "❌ Error de exportación",
    },
    "editor.unsaved": {
        "it": "Modifiche non salvate", "en": "Unsaved edits",
        "fr": "Modifications non enregistrées",
        "de": "Nicht gespeicherte Änderungen", "es": "Cambios sin guardar",
    },
    # ── spinner / gallery status ────────────────────────────────────────────
    "status.translating": {
        "it": "⏳ Traducendo...", "en": "⏳ Translating...",
        "fr": "⏳ Traduction...", "de": "⏳ Übersetzen...",
        "es": "⏳ Traduciendo...",
    },
    "status.translating_engine": {
        "it": "⏳ Traducendo con {engine}…", "en": "⏳ Translating with {engine}…",
        "fr": "⏳ Traduction avec {engine}…", "de": "⏳ Übersetzen mit {engine}…",
        "es": "⏳ Traduciendo con {engine}…",
    },
    "status.translation_error": {
        "it": "⚠️ Traduzione fallita ({engine}): {reason} — riprova o scegli l'altro motore nelle Impostazioni",
        "en": "⚠️ Translation failed ({engine}): {reason} — retry or choose the other engine in Settings",
        "fr": "⚠️ Échec de la traduction ({engine}) : {reason} — réessayez ou choisissez l'autre moteur dans Paramètres",
        "de": "⚠️ Übersetzung fehlgeschlagen ({engine}): {reason} — erneut versuchen oder die andere Engine in den Einstellungen wählen",
        "es": "⚠️ Error de traducción ({engine}): {reason} — reinténtalo o elige el otro motor en Configuración",
    },
    "toast.engine_changed": {
        "it": "⚙️ Engine cambiato: {engine}",
        "en": "⚙️ Engine changed: {engine}",
        "fr": "⚙️ Moteur changé : {engine}",
        "de": "⚙️ Engine geändert: {engine}",
        "es": "⚙️ Motor cambiado: {engine}",
    },
    "status.extracting": {
        "it": "⏳ Estrazione in corso...", "en": "⏳ Extracting...",
        "fr": "⏳ Extraction en cours...", "de": "⏳ Extraktion läuft...",
        "es": "⏳ Extrayendo...",
    },
    "status.copied": {
        "it": "✅ Copiata", "en": "✅ Copied", "fr": "✅ Copiée",
        "de": "✅ Kopiert", "es": "✅ Copiada",
    },
    "status.saved": {
        "it": "✅ Salvata", "en": "✅ Saved", "fr": "✅ Enregistrée",
        "de": "✅ Gespeichert", "es": "✅ Guardada",
    },
    "status.exported": {
        "it": "✅ Esportato", "en": "✅ Exported", "fr": "✅ Exporté",
        "de": "✅ Exportiert", "es": "✅ Exportado",
    },
    # ── images gallery ──────────────────────────────────────────────────────
    "gallery.empty": {
        "it": "Nessuna zona catturata.\n\nUsa 🖱️ Seleziona zona per ritagliare una figura dalla pagina.",
        "en": "No captured region.\n\nUse 🖱️ Select region to crop a figure from the page.",
        "fr": "Aucune zone capturée.\n\nUtilisez 🖱️ Sélectionner une zone pour découper une figure de la page.",
        "de": "Kein Bereich erfasst.\n\nVerwenden Sie 🖱️ Bereich auswählen, um eine Abbildung aus der Seite auszuschneiden.",
        "es": "No hay ninguna zona capturada.\n\nUsa 🖱️ Seleccionar zona para recortar una figura de la página.",
    },
    "gallery.zoom_tip": {
        "it": "Clicca per ingrandire", "en": "Click to enlarge",
        "fr": "Cliquez pour agrandir", "de": "Zum Vergrößern klicken",
        "es": "Haz clic para ampliar",
    },
    "gallery.save": {
        "it": "💾 Salva", "en": "💾 Save", "fr": "💾 Enregistrer",
        "de": "💾 Speichern", "es": "💾 Guardar",
    },
    "gallery.copy": {
        "it": "📋 Copia", "en": "📋 Copy", "fr": "📋 Copier",
        "de": "📋 Kopieren", "es": "📋 Copiar",
    },
    "gallery.remove": {
        "it": "🗑️ Rimuovi", "en": "🗑️ Remove", "fr": "🗑️ Supprimer",
        "de": "🗑️ Entfernen", "es": "🗑️ Eliminar",
    },
    "gallery.save_dialog": {
        "it": "Salva immagine", "en": "Save image",
        "fr": "Enregistrer l'image", "de": "Bild speichern",
        "es": "Guardar imagen",
    },
    "gallery.save_filter": {
        "it": "PNG (*.png);;JPEG (*.jpg);;Tutti i file (*)",
        "en": "PNG (*.png);;JPEG (*.jpg);;All files (*)",
        "fr": "PNG (*.png);;JPEG (*.jpg);;Tous les fichiers (*)",
        "de": "PNG (*.png);;JPEG (*.jpg);;Alle Dateien (*)",
        "es": "PNG (*.png);;JPEG (*.jpg);;Todos los archivos (*)",
    },
    # ── dock / status bar ───────────────────────────────────────────────────
    "dock.toc": {
        "it": "Indice", "en": "Table of contents", "fr": "Sommaire",
        "de": "Inhaltsverzeichnis", "es": "Índice",
    },
    "status.ready": {
        "it": "Pronto — apri un file PDF con 📂 Apri PDF  |  Backend testo: PyMuPDF4LLM ⚡",
        "en": "Ready — open a PDF with 📂 Open PDF  |  Text backend: PyMuPDF4LLM ⚡",
        "fr": "Prêt — ouvrez un PDF avec 📂 Ouvrir un PDF  |  Backend texte : PyMuPDF4LLM ⚡",
        "de": "Bereit — öffnen Sie ein PDF mit 📂 PDF öffnen  |  Text-Backend: PyMuPDF4LLM ⚡",
        "es": "Listo — abre un PDF con 📂 Abrir PDF  |  Backend de texto: PyMuPDF4LLM ⚡",
    },
    "status.no_image": {
        "it": "Nessuna immagine estraibile dalla zona selezionata",
        "en": "No extractable image in the selected region",
        "fr": "Aucune image extractible dans la zone sélectionnée",
        "de": "Kein extrahierbares Bild im ausgewählten Bereich",
        "es": "No hay ninguna imagen extraíble en la zona seleccionada",
    },
    "status.image_extracted": {
        "it": "Immagine estratta dalla zona: {name}",
        "en": "Image extracted from region: {name}",
        "fr": "Image extraite de la zone : {name}",
        "de": "Bild aus Bereich extrahiert: {name}",
        "es": "Imagen extraída de la zona: {name}",
    },
    "status.zone_excluded": {
        "it": "Zona esclusa ({count} sulla pagina) — trascina altre zone o premi 🚫 Escludi zona per terminare",
        "en": "Region excluded ({count} on page) — drag more regions or press 🚫 Exclude region to finish",
        "fr": "Zone exclue ({count} sur la page) — faites glisser d'autres zones ou appuyez sur 🚫 Exclure une zone pour terminer",
        "de": "Bereich ausgeschlossen ({count} auf der Seite) — ziehen Sie weitere Bereiche oder drücken Sie 🚫 Bereich ausschließen zum Beenden",
        "es": "Zona excluida ({count} en la página) — arrastra más zonas o pulsa 🚫 Excluir zona para terminar",
    },
    "status.zone_excluded_image": {
        "it": "Zona esclusa e immagine estratta ({name}) — trascina altre zone o premi 🚫 Escludi zona per terminare",
        "en": "Region excluded and image extracted ({name}) — drag more regions or press 🚫 Exclude region to finish",
        "fr": "Zone exclue et image extraite ({name}) — faites glisser d'autres zones ou appuyez sur 🚫 Exclure une zone pour terminer",
        "de": "Bereich ausgeschlossen und Bild extrahiert ({name}) — ziehen Sie weitere Bereiche oder drücken Sie 🚫 Bereich ausschließen zum Beenden",
        "es": "Zona excluida e imagen extraída ({name}) — arrastra más zonas o pulsa 🚫 Excluir zona para terminar",
    },
    "status.zone_included": {
        "it": "Zona inclusa (n. {count}) — trascina il prossimo box nell'ordine di lettura o premi 🟩 Includi zona per terminare",
        "en": "Region included (no. {count}) — drag the next box in reading order or press 🟩 Include region to finish",
        "fr": "Zone incluse (n° {count}) — faites glisser la boîte suivante dans l'ordre de lecture ou appuyez sur 🟩 Inclure une zone pour terminer",
        "de": "Bereich eingeschlossen (Nr. {count}) — ziehen Sie die nächste Box in Lesereihenfolge oder drücken Sie 🟩 Bereich einschließen zum Beenden",
        "es": "Zona incluida (n.º {count}) — arrastra el siguiente recuadro en orden de lectura o pulsa 🟩 Incluir zona para terminar",
    },
    "status.zones_reset": {
        "it": "Zone rimosse per questa pagina",
        "en": "Zones removed for this page",
        "fr": "Zones supprimées pour cette page",
        "de": "Zonen für diese Seite entfernt",
        "es": "Zonas eliminadas para esta página",
    },
    "status.page": {
        "it": "Pagina {page} di {total}  —  {name}  |  Testo: PyMuPDF4LLM ⚡ ({ms} ms)",
        "en": "Page {page} of {total}  —  {name}  |  Text: PyMuPDF4LLM ⚡ ({ms} ms)",
        "fr": "Page {page} sur {total}  —  {name}  |  Texte : PyMuPDF4LLM ⚡ ({ms} ms)",
        "de": "Seite {page} von {total}  —  {name}  |  Text: PyMuPDF4LLM ⚡ ({ms} ms)",
        "es": "Página {page} de {total}  —  {name}  |  Texto: PyMuPDF4LLM ⚡ ({ms} ms)",
    },
    "status.empty_pdf": {
        "it": "PDF senza pagine", "en": "PDF with no pages",
        "fr": "PDF sans pages", "de": "PDF ohne Seiten", "es": "PDF sin páginas",
    },
    # ── extraction header / engine labels ───────────────────────────────────
    "header.line": {
        "it": "── Backend: PyMuPDF4LLM ⚡  │  {ms} ms  │  {chars} caratteri  │  Fix: {label}  │  OCR: {ocr}  │  Trad: {engine} ──\n\n",
        "en": "── Backend: PyMuPDF4LLM ⚡  │  {ms} ms  │  {chars} characters  │  Fix: {label}  │  OCR: {ocr}  │  Transl: {engine} ──\n\n",
        "fr": "── Backend : PyMuPDF4LLM ⚡  │  {ms} ms  │  {chars} caractères  │  Correctifs : {label}  │  OCR : {ocr}  │  Trad. : {engine} ──\n\n",
        "de": "── Backend: PyMuPDF4LLM ⚡  │  {ms} ms  │  {chars} Zeichen  │  Fix: {label}  │  OCR: {ocr}  │  Übers.: {engine} ──\n\n",
        "es": "── Backend: PyMuPDF4LLM ⚡  │  {ms} ms  │  {chars} caracteres  │  Fix: {label}  │  OCR: {ocr}  │  Trad.: {engine} ──\n\n",
    },
    "engine.label.auto": {
        "it": "Engine adattativo", "en": "Adaptive engine",
        "fr": "Moteur adaptatif", "de": "Adaptive Engine", "es": "Motor adaptativo",
    },
    "engine.label.manual": {
        "it": "Zone manuali", "en": "Manual zones",
        "fr": "Zones manuelles", "de": "Manuelle Zonen", "es": "Zonas manuales",
    },
    "engine.option.google": {
        "it": "Google Translate", "en": "Google Translate",
        "fr": "Google Translate", "de": "Google Translate", "es": "Google Translate",
    },
    "engine.option.microsoft": {
        "it": "Microsoft Edge (gratuito)", "en": "Microsoft Edge (Free)",
        "fr": "Microsoft Edge (gratuit)", "de": "Microsoft Edge (kostenlos)",
        "es": "Microsoft Edge (gratis)",
    },
    "engine.short.google": {
        "it": "Google", "en": "Google",
        "fr": "Google", "de": "Google", "es": "Google",
    },
    "engine.short.microsoft": {
        "it": "Microsoft", "en": "Microsoft",
        "fr": "Microsoft", "de": "Microsoft", "es": "Microsoft",
    },
    "engine.option.bing": {
        "it": "Bing (gratuito)", "en": "Bing (free)",
        "fr": "Bing (gratuit)", "de": "Bing (kostenlos)", "es": "Bing (gratis)",
    },
    "engine.short.bing": {
        "it": "Bing", "en": "Bing", "fr": "Bing", "de": "Bing", "es": "Bing",
    },
    "engine.option.openai": {
        "it": "LLM (OpenRouter)", "en": "LLM (OpenRouter)",
        "fr": "LLM (OpenRouter)", "de": "LLM (OpenRouter)",
        "es": "LLM (OpenRouter)",
    },
    "engine.short.openai": {
        "it": "LLM", "en": "LLM", "fr": "LLM", "de": "LLM", "es": "LLM",
    },
    # ── clone translation (pannello destro) ─────────────────────────────────
    "clone.spinner": {
        "it": "Traduzione in corso…\n{engine}",
        "en": "Translating…\n{engine}",
        "fr": "Traduction en cours…\n{engine}",
        "de": "Übersetzung läuft…\n{engine}",
        "es": "Traduciendo…\n{engine}",
    },
    "clone.translating": {
        "it": "Traduzione pagina {page}… ({engine})",
        "en": "Translating page {page}… ({engine})",
        "fr": "Traduction de la page {page}… ({engine})",
        "de": "Seite {page} wird übersetzt… ({engine})",
        "es": "Traduciendo la página {page}… ({engine})",
    },
    "clone.page_pending": {
        "it": "In attesa della traduzione…", "en": "Waiting for translation…",
        "fr": "En attente de la traduction…", "de": "Warte auf Übersetzung…",
        "es": "Esperando la traducción…",
    },
    "clone.status_running": {
        "it": "Traduzione…", "en": "Translating…", "fr": "Traduction…",
        "de": "Übersetzung…", "es": "Traduciendo…",
    },
    "clone.status_done": {
        "it": "Pronto", "en": "Ready", "fr": "Prêt", "de": "Fertig",
        "es": "Listo",
    },
    "clone.status_cached": {
        "it": "Pagina {page} in cache", "en": "Page {page} cached",
        "fr": "Page {page} en cache", "de": "Seite {page} im Cache",
        "es": "Página {page} en caché",
    },
    "clone.status_error": {
        "it": "Errore", "en": "Error", "fr": "Erreur", "de": "Fehler",
        "es": "Error",
    },
    "clone.done": {
        "it": "Clone pagina {page} pronto", "en": "Clone of page {page} ready",
        "fr": "Clone de la page {page} prêt", "de": "Klon von Seite {page} fertig",
        "es": "Clon de la página {page} listo",
    },
    "clone.failed": {
        "it": "Traduzione non riuscita: {reason}",
        "en": "Translation failed: {reason}",
        "fr": "Échec de la traduction : {reason}",
        "de": "Übersetzung fehlgeschlagen: {reason}",
        "es": "Traducción fallida: {reason}",
    },
    "clone.no_engine": {
        "it": "Motore pdf2zh_next non trovato.\nInstalla il motore di clonazione (vedi README / script di setup) oppure imposta il percorso in ⚙️ Impostazioni.",
        "en": "pdf2zh_next engine not found.\nInstall the clone engine (see README / setup script) or set its path in ⚙️ Settings.",
        "fr": "Moteur pdf2zh_next introuvable.\nInstallez le moteur de clonage (voir README / script d'installation) ou définissez son chemin dans ⚙️ Paramètres.",
        "de": "pdf2zh_next-Engine nicht gefunden.\nInstalliere die Klon-Engine (siehe README / Setup-Skript) oder lege den Pfad in ⚙️ Einstellungen fest.",
        "es": "Motor pdf2zh_next no encontrado.\nInstala el motor de clonación (ver README / script de instalación) o define su ruta en ⚙️ Ajustes.",
    },
    "clone.no_engine_short": {
        "it": "motore assente", "en": "engine missing", "fr": "moteur absent",
        "de": "Engine fehlt", "es": "motor ausente",
    },
    "clone.render_error": {
        "it": "Impossibile mostrare la pagina tradotta",
        "en": "Unable to display the translated page",
        "fr": "Impossible d'afficher la page traduite",
        "de": "Übersetzte Seite kann nicht angezeigt werden",
        "es": "No se puede mostrar la página traducida",
    },
    "clone.bar.collapse": {
        "it": "Comprimi la barra dei motori",
        "en": "Collapse the engines bar",
        "fr": "Réduire la barre des moteurs",
        "de": "Engine-Leiste einklappen",
        "es": "Contraer la barra de motores",
    },
    "clone.bar.expand": {
        "it": "Espandi la barra dei motori",
        "en": "Expand the engines bar",
        "fr": "Développer la barre des moteurs",
        "de": "Engine-Leiste ausklappen",
        "es": "Expandir la barra de motores",
    },
    # ── dialogs ─────────────────────────────────────────────────────────────
    "dlg.open": {
        "it": "Apri PDF", "en": "Open PDF", "fr": "Ouvrir un PDF",
        "de": "PDF öffnen", "es": "Abrir PDF",
    },
    "dlg.open_filter": {
        "it": "PDF Files (*.pdf);;All Files (*)",
        "en": "PDF Files (*.pdf);;All Files (*)",
        "fr": "Fichiers PDF (*.pdf);;Tous les fichiers (*)",
        "de": "PDF-Dateien (*.pdf);;Alle Dateien (*)",
        "es": "Archivos PDF (*.pdf);;Todos los archivos (*)",
    },
    "dlg.error": {
        "it": "Errore", "en": "Error", "fr": "Erreur", "de": "Fehler", "es": "Error",
    },
    "dlg.file_not_found": {
        "it": "File non trovato:\n{path}", "en": "File not found:\n{path}",
        "fr": "Fichier introuvable :\n{path}", "de": "Datei nicht gefunden:\n{path}",
        "es": "Archivo no encontrado:\n{path}",
    },
    "dlg.pdf_error": {
        "it": "Errore PDF", "en": "PDF Error", "fr": "Erreur PDF",
        "de": "PDF-Fehler", "es": "Error de PDF",
    },
    "dlg.cannot_open": {
        "it": "Impossibile aprire il PDF:\n{e}",
        "en": "Unable to open the PDF:\n{e}",
        "fr": "Impossible d'ouvrir le PDF :\n{e}",
        "de": "PDF kann nicht geöffnet werden:\n{e}",
        "es": "No se puede abrir el PDF:\n{e}",
    },
    # ── page view messages ──────────────────────────────────────────────────
    "view.start_hint": {
        "it": "Apri un PDF per iniziare", "en": "Open a PDF to start",
        "fr": "Ouvrez un PDF pour commencer", "de": "Öffnen Sie ein PDF, um zu beginnen",
        "es": "Abre un PDF para empezar",
    },
    "view.page_unavailable": {
        "it": "(pagina non disponibile)", "en": "(page unavailable)",
        "fr": "(page indisponible)", "de": "(Seite nicht verfügbar)",
        "es": "(página no disponible)",
    },
    "view.no_pymupdf": {
        "it": "(pymupdf non installato)", "en": "(pymupdf not installed)",
        "fr": "(pymupdf non installé)", "de": "(pymupdf nicht installiert)",
        "es": "(pymupdf no instalado)",
    },
    "view.empty_pdf": {
        "it": "(PDF vuoto)", "en": "(empty PDF)", "fr": "(PDF vide)",
        "de": "(leeres PDF)", "es": "(PDF vacío)",
    },
    # ── extraction fallbacks (shown in the text panel) ──────────────────────
    "extract.no_pymupdf4llm": {
        "it": "(pymupdf4llm non installato — esegui: pip install pymupdf4llm)",
        "en": "(pymupdf4llm not installed — run: pip install pymupdf4llm)",
        "fr": "(pymupdf4llm non installé — exécutez : pip install pymupdf4llm)",
        "de": "(pymupdf4llm nicht installiert — ausführen: pip install pymupdf4llm)",
        "es": "(pymupdf4llm no instalado — ejecuta: pip install pymupdf4llm)",
    },
    "extract.empty_page": {
        "it": "(nessun testo estraibile su questa pagina)",
        "en": "(no extractable text on this page)",
        "fr": "(aucun texte extractible sur cette page)",
        "de": "(kein extrahierbarer Text auf dieser Seite)",
        "es": "(no hay texto extraíble en esta página)",
    },
    "extract.error": {
        "it": "(errore pymupdf4llm: {e})", "en": "(pymupdf4llm error: {e})",
        "fr": "(erreur pymupdf4llm : {e})", "de": "(pymupdf4llm-Fehler: {e})",
        "es": "(error de pymupdf4llm: {e})",
    },
    # ── table of contents ───────────────────────────────────────────────────
    "toc.no_title": {
        "it": "(senza titolo)", "en": "(untitled)", "fr": "(sans titre)",
        "de": "(ohne Titel)", "es": "(sin título)",
    },
    "toc.page_fmt": {
        "it": "{title}  ·  p. {page}", "en": "{title}  ·  p. {page}",
        "fr": "{title}  ·  p. {page}", "de": "{title}  ·  S. {page}",
        "es": "{title}  ·  p. {page}",
    },
    # ── settings dialog ────────────────────────────────────────────────────
    "settings.button": {
        "it": "⚙️ Impostazioni", "en": "⚙️ Settings",
        "fr": "⚙️ Paramètres", "de": "⚙️ Einstellungen",
        "es": "⚙️ Configuración",
    },
    "settings.button.tip": {
        "it": "Lingua interfaccia, lingue di traduzione e preferenze",
        "en": "Interface language, translation languages and preferences",
        "fr": "Langue de l'interface, langues de traduction et préférences",
        "de": "Oberflächensprache, Übersetzungssprachen und Einstellungen",
        "es": "Idioma de la interfaz, idiomas de traducción y preferencias",
    },
    "settings.title": {
        "it": "Impostazioni", "en": "Settings", "fr": "Paramètres",
        "de": "Einstellungen", "es": "Configuración",
    },
    "settings.group.lang": {
        "it": "Lingua", "en": "Language", "fr": "Langue",
        "de": "Sprache", "es": "Idioma",
    },
    "settings.lang.ui": {
        "it": "Lingua interfaccia", "en": "Interface language",
        "fr": "Langue de l'interface", "de": "Oberflächensprache",
        "es": "Idioma de la interfaz",
    },
    "settings.lang.source": {
        "it": "Lingua del documento (origine)",
        "en": "Document language (source)",
        "fr": "Langue du document (source)",
        "de": "Dokumentensprache (Quelle)",
        "es": "Idioma del documento (origen)",
    },
    "settings.lang.target": {
        "it": "Lingua della traduzione (destinazione)",
        "en": "Translation language (target)",
        "fr": "Langue de traduction (cible)",
        "de": "Übersetzungssprache (Ziel)",
        "es": "Idioma de traducción (destino)",
    },
    "settings.group.translation": {
        "it": "Traduzione", "en": "Translation", "fr": "Traduction",
        "de": "Übersetzung", "es": "Traducción",
    },
    "settings.translation.engine": {
        "it": "Motore di traduzione", "en": "Translation engine",
        "fr": "Moteur de traduction", "de": "Übersetzungs-Engine",
        "es": "Motor de traducción",
    },
    "settings.group.clone": {
        "it": "Motore di clonazione (pdf2zh v2)", "en": "Clone engine (pdf2zh v2)",
        "fr": "Moteur de clonage (pdf2zh v2)",
        "de": "Klon-Engine (pdf2zh v2)", "es": "Motor de clonación (pdf2zh v2)",
    },
    "settings.clone.pdf2zh": {
        "it": "Eseguibile pdf2zh_next", "en": "pdf2zh_next executable",
        "fr": "Exécutable pdf2zh_next", "de": "pdf2zh_next-Programm",
        "es": "Ejecutable pdf2zh_next",
    },
    "settings.clone.pdf2zh.ph": {
        "it": "auto (cerca .venv2)", "en": "auto (search .venv2)",
        "fr": "auto (cherche .venv2)", "de": "auto (.venv2 suchen)",
        "es": "auto (busca .venv2)",
    },
    "settings.clone.browse": {
        "it": "Sfoglia…", "en": "Browse…", "fr": "Parcourir…",
        "de": "Durchsuchen…", "es": "Examinar…",
    },
    "settings.clone.hint": {
        "it": "Il motore pdf2zh_next gira in un venv separato (Python 3.12). "
              "Se vuoto viene cercato automaticamente in .venv2. La chiave "
              "OPENROUTER_API_KEY serve solo per il motore LLM.",
        "en": "The pdf2zh_next engine runs in a separate venv (Python 3.12). "
              "If empty it is auto-detected in .venv2. OPENROUTER_API_KEY is "
              "only needed for the LLM engine.",
        "fr": "Le moteur pdf2zh_next s'exécute dans un venv séparé (Python 3.12). "
              "S'il est vide, il est détecté automatiquement dans .venv2. "
              "OPENROUTER_API_KEY n'est nécessaire que pour le moteur LLM.",
        "de": "Die pdf2zh_next-Engine läuft in einem separaten venv (Python 3.12). "
              "Wenn leer, wird sie automatisch in .venv2 gesucht. "
              "OPENROUTER_API_KEY ist nur für die LLM-Engine nötig.",
        "es": "El motor pdf2zh_next se ejecuta en un venv aparte (Python 3.12). "
              "Si está vacío, se busca automáticamente en .venv2. "
              "OPENROUTER_API_KEY solo es necesaria para el motor LLM.",
    },
    "settings.group.text": {
        "it": "Testo", "en": "Text", "fr": "Texte",
        "de": "Text", "es": "Texto",
    },
    "settings.text.font": {
        "it": "Dimensione font", "en": "Font size",
        "fr": "Taille de police", "de": "Schriftgröße",
        "es": "Tamaño de fuente",
    },
    "settings.text.md": {
        "it": "Rendering Markdown", "en": "Markdown rendering",
        "fr": "Rendu Markdown", "de": "Markdown-Rendering",
        "es": "Renderizado Markdown",
    },
    "settings.text.header": {
        "it": "Mostra l'header di estrazione",
        "en": "Show extraction header",
        "fr": "Afficher l'en-tête d'extraction",
        "de": "Extraktions-Header anzeigen",
        "es": "Mostrar la cabecera de extracción",
    },
    "settings.edits.save": {
        "it": "Salva le modifiche ai testi",
        "en": "Save text edits",
        "fr": "Enregistrer les modifications de texte",
        "de": "Textänderungen speichern",
        "es": "Guardar cambios de texto",
    },
    "settings.edits.clear": {
        "it": "🗑️ Cancella modifiche salvate",
        "en": "🗑️ Clear saved edits",
        "fr": "🗑️ Effacer les modifications enregistrées",
        "de": "🗑️ Gespeicherte Änderungen löschen",
        "es": "🗑️ Borrar cambios guardados",
    },
    "settings.edits.clear_confirm": {
        "it": "Cancellare le modifiche salvate di questo documento?",
        "en": "Delete the saved edits of this document?",
        "fr": "Supprimer les modifications enregistrées de ce document ?",
        "de": "Gespeicherte Änderungen dieses Dokuments löschen?",
        "es": "¿Borrar los cambios guardados de este documento?",
    },
    "settings.edits.clear_done": {
        "it": "✅ Modifiche cancellate", "en": "✅ Edits cleared",
        "fr": "✅ Modifications effacées", "de": "✅ Änderungen gelöscht",
        "es": "✅ Cambios borrados",
    },
    "settings.group.view": {
        "it": "Visualizzazione", "en": "View", "fr": "Affichage",
        "de": "Anzeige", "es": "Vista",
    },
    "settings.view.zoom": {
        "it": "Zoom di avvio", "en": "Initial zoom", "fr": "Zoom initial",
        "de": "Start-Zoom", "es": "Zoom inicial",
    },
    "settings.group.behavior": {
        "it": "Comportamento", "en": "Behavior", "fr": "Comportement",
        "de": "Verhalten", "es": "Comportamiento",
    },
    "settings.behavior.resume": {
        "it": "Riprendi dall'ultima pagina del documento",
        "en": "Resume at the document's last page",
        "fr": "Reprendre à la dernière page du document",
        "de": "An der letzten Seite des Dokuments fortfahren",
        "es": "Reanudar en la última página del documento",
    },
    "settings.behavior.tab": {
        "it": "Ricorda l'ultima tab del pannello destro",
        "en": "Remember the last right-panel tab",
        "fr": "Mémoriser le dernier onglet du panneau droit",
        "de": "Letzten Tab des rechten Bereichs merken",
        "es": "Recordar la última pestaña del panel derecho",
    },
    "settings.ok": {
        "it": "OK", "en": "OK", "fr": "OK", "de": "OK", "es": "OK",
    },
    "settings.cancel": {
        "it": "Annulla", "en": "Cancel", "fr": "Annuler",
        "de": "Abbrechen", "es": "Cancelar",
    },
}


def T(key: str, **fmt) -> str:
    """Return the string for ``key`` in the active language.

    ``fmt`` is applied with ``str.format`` for parameterized messages (e.g.
    ``T("status.page", page=3, total=12, name="x.pdf", ms="210")``). Falls
    back to Italian, then to the key itself, on any miss; formatting errors
    degrade to the raw translated text instead of raising.
    """
    table = _STRINGS.get(key)
    if not table:
        return key
    text = table.get(_current) or table.get("it") or key
    if not fmt:
        return text
    try:
        return text.format(**fmt)
    except (KeyError, IndexError, ValueError):
        return text


# ═══════════════════════════════════════════════════════════════════════════════
#  persistence — atomic write, tolerant read, forward-compatible schema
# ═══════════════════════════════════════════════════════════════════════════════


def _read_config(path) -> dict | None:
    """Parse the config file; ``None`` when missing or unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _to_bool(value, default: bool) -> bool:
    """Coerce a config value to bool (accepts bools, 0/1, 'true'/'false')."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    return default


def _validate_config(raw: dict, defaults: dict) -> dict:
    """Merge ``raw`` over ``defaults`` with per-field validation.

    Unknown fields are ignored (forward compatibility); wrong types and
    out-of-range values degrade to the defaults/clamps instead of crashing.
    """
    out = dict(defaults)
    if raw.get("lang") in LANGUAGES:
        out["lang"] = raw["lang"]
    if raw.get("src_lang") in TRANSLATION_LANGUAGES:
        out["src_lang"] = raw["src_lang"]
    dst = raw.get("dst_lang")
    if dst in TRANSLATION_LANGUAGES and dst != "auto":
        out["dst_lang"] = dst
    try:
        out["zoom"] = min(4.0, max(0.5, float(raw.get("zoom", out["zoom"]))))
    except (TypeError, ValueError):
        pass
    try:
        out["font_size"] = min(16, max(10, int(raw.get("font_size", out["font_size"]))))
    except (TypeError, ValueError):
        pass
    for key in ("render_md", "show_header", "remember_tab", "resume_last_page", "save_edits", "clone_bar_collapsed"):
        out[key] = _to_bool(raw.get(key, out[key]), out[key])
    if raw.get("last_tab") in ("original", "translated", "images"):
        out["last_tab"] = raw["last_tab"]
    pages = raw.get("last_pages")
    if isinstance(pages, dict):
        cleaned: dict[str, int] = {}
        for name, page in pages.items():
            try:
                cleaned[str(name)] = int(page)
            except (TypeError, ValueError):
                pass
        out["last_pages"] = dict(list(cleaned.items())[-20:])  # cap LRU 20
    pdf2zh = raw.get("pdf2zh_bin")
    if isinstance(pdf2zh, str):
        out["pdf2zh_bin"] = pdf2zh
    return out


def load_config(path, defaults: dict | None = None) -> dict:
    """Load and validate the config file, merging missing fields with defaults."""
    defaults = defaults or DEFAULTS
    data = _read_config(path)
    if data is None:
        return dict(defaults)
    return _validate_config(data, defaults)


def save_config(path=None, config: dict | None = None) -> None:
    """Write the config atomically (temp file + os.replace).

    Uses the module-level path when ``path`` is None (set by init_config).
    A crash mid-write leaves the previous file intact.
    """
    if path is not None:
        target = Path(path)
    elif _CONFIG_PATH:
        target = Path(_CONFIG_PATH)
    else:
        return
    payload = config if config is not None else _CONFIG
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, target)


def init_config(path, defaults: dict | None = None) -> dict:
    """Load config into module state and set the global save path.

    On first run (missing/corrupted file) the defaults are written
    immediately, so after the first launch the file always exists.
    """
    global _CONFIG, _CONFIG_PATH, _current
    _CONFIG_PATH = str(Path(path))
    cfg = load_config(path, defaults)
    if _read_config(path) is None:
        save_config(path, cfg)
    _CONFIG = cfg
    _current = cfg.get("lang", "it")
    return dict(_CONFIG)


def get_config() -> dict:
    """Snapshot of the current in-memory config."""
    return dict(_CONFIG)


def get_setting(key: str, default=None):
    """Return a config value (falls back to ``default``)."""
    return _CONFIG.get(key, default)


def set_setting(key: str, value) -> None:
    """Update a config value in memory (validated for scalar ranges)."""
    if key == "lang":
        # La lingua è speciale: sincronizza anche la lingua attiva di T().
        set_language(value)
        return
    if key == "zoom":
        try:
            _CONFIG[key] = min(4.0, max(0.5, float(value)))
            return
        except (TypeError, ValueError):
            pass
    elif key == "font_size":
        try:
            _CONFIG[key] = min(16, max(10, int(value)))
            return
        except (TypeError, ValueError):
            pass
    _CONFIG[key] = value


# ── compat wrappers (pre-config-v2 API) ──────────────────────────────────────


def load_language(path, default: str = "it") -> str:
    """Read the stored UI language code; ``default`` on any problem."""
    cfg = load_config(path, {**DEFAULTS, "lang": default})
    return cfg.get("lang", default)


def save_language(path, code: str) -> None:
    """Write the UI language, merging with the existing config file.

    Merges (never clobbers) so other fields (src/dst/zoom…) survive.
    """
    existing = _read_config(path) or {}
    existing["lang"] = code
    save_config(path, existing)


def ensure_config(path, default: str = "it") -> str:
    """Return the stored UI language, writing the defaults on first run."""
    path = Path(path)
    cfg = load_config(path, {**DEFAULTS, "lang": default})
    if _read_config(path) is None:
        save_config(path, cfg)
    return cfg.get("lang", default)
