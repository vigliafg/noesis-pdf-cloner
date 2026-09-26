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
TRANSLATION_ENGINES: tuple[str, ...] = ("google", "bing", "llm")

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
    "theme": "dark",         # tema UI: dark | light | system
    "notify_on_finish": True,  # avviso (notifica + suono) a fine batch
    "notify_sound": True,      # suono d'avviso a fine batch
    "prevent_sleep": True,     # impedisci lo standby durante la traduzione
    "llm_pool_workers": 4,     # richieste LLM in parallelo dentro una pagina
    # Feature sperimentale "motore veloce": patch runtime del motore (default
    # OFF, reversibile). Vedi .opencode/plan/velocita-traduzione.md.
    "fast_engine": False,      # motore veloce (wrapper + patch runtime)
    "fast_flags": False,       # preset "traduzione rapida" (salta controlli)
    "fast_worker": False,      # worker persistente (Fase 2, richiede fast_engine)
    # Opzioni LLM avanzate (usate solo a fast_engine attivo).
    "llm_reasoning_effort": "",  # "" | minimal | low | medium | high
    "llm_json_mode": False,      # --openai-enable-json-mode
    # Modello e base URL LLM (vuoto = default/env PDF_LLM_MODEL / PDF_LLM_BASE_URL).
    "llm_model": "",
    "llm_base_url": "",
    # Proxy provider locale (pin Groq): avvio automatico dall'app + porta.
    "llm_proxy_autostart": False,
    "llm_proxy_port": 8790,
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
    "toolbar.export": {
        "it": "💾 Esporta", "en": "💾 Export", "fr": "💾 Exporter",
        "de": "💾 Exportieren", "es": "💾 Exportar",
    },
    "toolbar.export.tip": {
        "it": "Esporta in PDF la pagina tradotta (Ctrl+E)",
        "en": "Export the translated page as PDF (Ctrl+E)",
        "fr": "Exporter la page traduite en PDF (Ctrl+E)",
        "de": "Die übersetzte Seite als PDF exportieren (Strg+E)",
        "es": "Exportar a PDF la página traducida (Ctrl+E)",
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
        "it": "Pronto — apri un file PDF con 📂 Apri PDF",
        "en": "Ready — open a PDF with 📂 Open PDF",
        "fr": "Prêt — ouvrez un PDF avec 📂 Ouvrir un PDF",
        "de": "Bereit — öffnen Sie ein PDF mit 📂 PDF öffnen",
        "es": "Listo — abre un PDF con 📂 Abrir PDF",
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
        "it": "Pagina {page} di {total}  —  {name}",
        "en": "Page {page} of {total}  —  {name}",
        "fr": "Page {page} sur {total}  —  {name}",
        "de": "Seite {page} von {total}  —  {name}",
        "es": "Página {page} de {total}  —  {name}",
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
    "engine.option.llm": {
        "it": "LLM (OpenRouter)", "en": "LLM (OpenRouter)",
        "fr": "LLM (OpenRouter)", "de": "LLM (OpenRouter)",
        "es": "LLM (OpenRouter)",
    },
    "engine.short.llm": {
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
    "clone.translate": {
        "it": "Traduci", "en": "Translate", "fr": "Traduire",
        "de": "Übersetzen", "es": "Traducir",
    },
    "clone.translate.tip": {
        "it": "Traduce la pagina corrente con il motore selezionato",
        "en": "Translate the current page with the selected engine",
        "fr": "Traduire la page courante avec le moteur sélectionné",
        "de": "Die aktuelle Seite mit der gewählten Engine übersetzen",
        "es": "Traduce la página actual con el motor seleccionado",
    },
    "clone.translate.tooltip": {
        "it": "Pagina {page} · motore: {engine}",
        "en": "Page {page} · engine: {engine}",
        "fr": "Page {page} · moteur : {engine}",
        "de": "Seite {page} · Engine: {engine}",
        "es": "Página {page} · motor: {engine}",
    },
    "clone.engine_missing": {
        "it": "Motore di traduzione non installato: installalo per tradurre le pagine.",
        "en": "Translation engine not installed: install it to translate pages.",
        "fr": "Moteur de traduction non installé : installez-le pour traduire les pages.",
        "de": "Übersetzungs-Engine nicht installiert: installieren, um Seiten zu übersetzen.",
        "es": "Motor de traducción no instalado: instálalo para traducir las páginas.",
    },
    "clone.engine_missing.install": {
        "it": "Installa motore", "en": "Install engine",
        "fr": "Installer le moteur", "de": "Engine installieren",
        "es": "Instalar motor",
    },
    "clone.key_missing": {
        "it": "Manca la chiave OpenRouter per il motore LLM. Puoi inserirla "
              "oppure usare un motore gratuito (Google/Bing), che non la richiede.",
        "en": "The OpenRouter key for the LLM engine is missing. Enter it or use "
              "a free engine (Google/Bing), which does not require it.",
        "fr": "La clé OpenRouter pour le moteur LLM est absente. Saisissez-la ou "
              "utilisez un moteur gratuit (Google/Bing), qui ne la demande pas.",
        "de": "Der OpenRouter-Schlüssel für die LLM-Engine fehlt. Trage ihn ein "
              "oder nutze eine kostenlose Engine (Google/Bing), die ihn nicht "
              "benötigt.",
        "es": "Falta la clave de OpenRouter para el motor LLM. Introdúcela o usa "
              "un motor gratuito (Google/Bing), que no la necesita.",
    },
    "clone.key_missing.enter": {
        "it": "Inserisci chiave", "en": "Enter key",
        "fr": "Saisir la clé", "de": "Schlüssel eingeben",
        "es": "Introducir clave",
    },
    "clone.key_missing.use_free": {
        "it": "Usa Google/Bing", "en": "Use Google/Bing",
        "fr": "Utiliser Google/Bing", "de": "Google/Bing nutzen",
        "es": "Usar Google/Bing",
    },
    "clone.pending_page": {
        "it": "Premi ▶ Traduci nella barra in alto per tradurre questa pagina",
        "en": "Press ▶ Translate in the top bar to translate this page",
        "fr": "Appuyez sur ▶ Traduire dans la barre du haut pour traduire cette page",
        "de": "▶ Übersetzen in der oberen Leiste drücken, um diese Seite zu übersetzen",
        "es": "Pulsa ▶ Traducir en la barra superior para traducir esta página",
    },
    "clone.status_todo": {
        "it": "Da tradurre", "en": "Not translated", "fr": "À traduire",
        "de": "Zu übersetzen", "es": "Sin traducir",
    },
    "clone.purge.title": {
        "it": "Eliminare la traduzione esistente?",
        "en": "Delete the existing translation?",
        "fr": "Supprimer la traduction existante ?",
        "de": "Die vorhandene Übersetzung löschen?",
        "es": "¿Eliminar la traducción existente?",
    },
    "clone.purge.body": {
        "it": "Per tradurre la pagina {page} con {new} verrà eliminata dalla "
              "cache la traduzione della stessa pagina fatta con {old} "
              "({size}).\n\nContinuare?",
        "en": "To translate page {page} with {new}, the cached translation of "
              "this page made with {old} ({size}) will be deleted.\n\nContinue?",
        "fr": "Pour traduire la page {page} avec {new}, la traduction en cache "
              "de cette page faite avec {old} ({size}) sera supprimée.\n\n"
              "Continuer ?",
        "de": "Um Seite {page} mit {new} zu übersetzen, wird die "
              "zwischengespeicherte Übersetzung dieser Seite mit {old} "
              "({size}) gelöscht.\n\nFortfahren?",
        "es": "Para traducir la página {page} con {new}, se eliminará de la "
              "caché la traducción de esta página hecha con {old} "
              "({size}).\n\n¿Continuar?",
    },
    "clone.purge.confirm": {
        "it": "Elimina e traduci", "en": "Delete and translate",
        "fr": "Supprimer et traduire", "de": "Löschen und übersetzen",
        "es": "Eliminar y traducir",
    },
    "clone.purge.cancel": {
        "it": "Annulla", "en": "Cancel", "fr": "Annuler",
        "de": "Abbrechen", "es": "Cancelar",
    },
    "clone.status_running": {
        "it": "Traduzione…", "en": "Translating…", "fr": "Traduction…",
        "de": "Übersetzung…", "es": "Traduciendo…",
    },
    "clone.status_done": {
        "it": "Pronto", "en": "Ready", "fr": "Prêt", "de": "Fertig",
        "es": "Listo",
    },
    "clone.status_empty": {
        "it": "Nessun testo da tradurre", "en": "Nothing to translate",
        "fr": "Aucun texte à traduire", "de": "Kein Text zu übersetzen",
        "es": "Nada que traducir",
    },
    "clone.empty_page": {
        "it": "Pagina {page}: nessun testo da tradurre, mostro l'originale.",
        "en": "Page {page}: nothing to translate, showing the original.",
        "fr": "Page {page} : aucun texte à traduire, affichage de l'original.",
        "de": "Seite {page}: kein Text zu übersetzen, zeige das Original.",
        "es": "Página {page}: nada que traducir, muestro el original.",
    },
    "clone.status_cached": {
        "it": "Pagina {page} in cache", "en": "Page {page} cached",
        "fr": "Page {page} en cache", "de": "Seite {page} im Cache",
        "es": "Página {page} en caché",
    },
    "clone.already_cached": {
        "it": "La pagina {page} tradotta da {engine} è già in cache.",
        "en": "Page {page} translated by {engine} is already cached.",
        "fr": "La page {page} traduite par {engine} est déjà en cache.",
        "de": "Seite {page}, übersetzt mit {engine}, ist bereits im Cache.",
        "es": "La página {page} traducida por {engine} ya está en caché.",
    },
    "clone.status_error": {
        "it": "Errore", "en": "Error", "fr": "Erreur", "de": "Fehler",
        "es": "Error",
    },
    "clone.status_cancelled": {
        "it": "Annullata", "en": "Cancelled", "fr": "Annulée",
        "de": "Abgebrochen", "es": "Cancelada",
    },
    "clone.cancelled": {
        "it": "Traduzione annullata.", "en": "Translation cancelled.",
        "fr": "Traduction annulée.", "de": "Übersetzung abgebrochen.",
        "es": "Traducción cancelada.",
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
    "clone.no_key": {
        "it": "Serve una chiave OpenRouter per il motore LLM: inseriscila in "
              "⚙️ Impostazioni → Motore (o in un dialogo dedicato). In "
              "alternativa usa il motore Google/Bing: non serve la chiave.",
        "en": "An OpenRouter key is required for the LLM engine: enter it in "
              "⚙️ Settings → Engine (or in the dedicated dialog). Alternatively "
              "use the Google/Bing engine: no key needed.",
        "fr": "Une clé OpenRouter est requise pour le moteur LLM : saisissez-la "
              "dans ⚙️ Paramètres → Moteur (ou dans la boîte dédiée). Sinon, "
              "utilisez le moteur Google/Bing : aucune clé requise.",
        "de": "Für die LLM-Engine ist ein OpenRouter-Schlüssel nötig: trage ihn "
              "in ⚙️ Einstellungen → Engine ein (oder im Dialog). Alternativ die "
              "Google/Bing-Engine nutzen: kein Schlüssel nötig.",
        "es": "Se necesita una clave OpenRouter para el motor LLM: introdúcela "
              "en ⚙️ Ajustes → Motor (o en el diálogo dedicado). Si no, usa el "
              "motor Google/Bing: no hace falta clave.",
    },
    "clone.invalid_key": {
        "it": "Chiave OpenRouter non valida o rifiutata (401). Controllala in "
              "⚙️ Impostazioni → Motore. Nota: la chiave salvata lì ha la "
              "precedenza sulla variabile di sistema OPENROUTER_API_KEY.",
        "en": "Invalid or rejected OpenRouter key (401). Check it in "
              "⚙️ Settings → Engine. Note: the key saved there overrides the "
              "OPENROUTER_API_KEY system variable.",
        "fr": "Clé OpenRouter non valide ou refusée (401). Vérifiez-la dans "
              "⚙️ Paramètres → Moteur. Remarque : la clé enregistrée là-bas est "
              "prioritaire sur la variable système OPENROUTER_API_KEY.",
        "de": "Ungültiger oder abgelehnter OpenRouter-Schlüssel (401). Prüfe ihn "
              "in ⚙️ Einstellungen → Engine. Hinweis: Der dort gespeicherte "
              "Schlüssel hat Vorrang vor der Systemvariable OPENROUTER_API_KEY.",
        "es": "Clave OpenRouter no válida o rechazada (401). Compruébala en "
              "⚙️ Ajustes → Motor. Nota: la clave guardada allí tiene prioridad "
              "sobre la variable de sistema OPENROUTER_API_KEY.",
    },
    "clone.err.forbidden": {
        "it": "OpenRouter ha rifiutato l'accesso (403). Controlla la chiave e i "
              "permessi in ⚙️ Impostazioni → Motore.",
        "en": "OpenRouter refused access (403). Check the key and permissions in "
              "⚙️ Settings → Engine.",
        "fr": "OpenRouter a refusé l'accès (403). Vérifiez la clé et les "
              "autorisations dans ⚙️ Paramètres → Moteur.",
        "de": "OpenRouter hat den Zugriff verweigert (403). Prüfe Schlüssel und "
              "Berechtigungen in ⚙️ Einstellungen → Engine.",
        "es": "OpenRouter rechazó el acceso (403). Comprueba la clave y los "
              "permisos en ⚙️ Ajustes → Motor.",
    },
    "clone.err.no_credits": {
        "it": "Credito OpenRouter esaurito (402). Aggiungi credito su OpenRouter "
              "oppure usa il motore Google/Bing.",
        "en": "OpenRouter credit exhausted (402). Add credit on OpenRouter or use "
              "the Google/Bing engine.",
        "fr": "Crédit OpenRouter épuisé (402). Ajoutez du crédit sur OpenRouter "
              "ou utilisez le moteur Google/Bing.",
        "de": "OpenRouter-Guthaben aufgebraucht (402). Lade Guthaben bei "
              "OpenRouter auf oder nutze die Google/Bing-Engine.",
        "es": "Crédito de OpenRouter agotado (402). Añade crédito en OpenRouter "
              "o usa el motor Google/Bing.",
    },
    "clone.err.rate_limited": {
        "it": "Troppe richieste a OpenRouter (429). Attendi qualche istante e "
              "riprova, oppure usa il motore Google/Bing.",
        "en": "Too many requests to OpenRouter (429). Wait a moment and retry, "
              "or use the Google/Bing engine.",
        "fr": "Trop de requêtes vers OpenRouter (429). Attendez un instant et "
              "réessayez, ou utilisez le moteur Google/Bing.",
        "de": "Zu viele Anfragen an OpenRouter (429). Warte kurz und versuche es "
              "erneut oder nutze die Google/Bing-Engine.",
        "es": "Demasiadas solicitudes a OpenRouter (429). Espera un momento y "
              "reintenta, o usa el motor Google/Bing.",
    },
    "clone.err.model_not_found": {
        "it": "Modello LLM non disponibile su OpenRouter (404). Verifica il "
              "modello o usa il motore Google/Bing.",
        "en": "LLM model not available on OpenRouter (404). Check the model or "
              "use the Google/Bing engine.",
        "fr": "Modèle LLM indisponible sur OpenRouter (404). Vérifiez le modèle "
              "ou utilisez le moteur Google/Bing.",
        "de": "LLM-Modell auf OpenRouter nicht verfügbar (404). Prüfe das Modell "
              "oder nutze die Google/Bing-Engine.",
        "es": "Modelo LLM no disponible en OpenRouter (404). Comprueba el modelo "
              "o usa el motor Google/Bing.",
    },
    "clone.err.network": {
        "it": "Impossibile contattare OpenRouter (rete, proxy o timeout). "
              "Controlla la connessione e riprova; puoi intanto usare il motore "
              "Google/Bing.",
        "en": "Could not reach OpenRouter (network, proxy or timeout). Check your "
              "connection and retry; you can use the Google/Bing engine meanwhile.",
        "fr": "Impossible de joindre OpenRouter (réseau, proxy ou délai). "
              "Vérifiez la connexion et réessayez ; vous pouvez utiliser le moteur "
              "Google/Bing en attendant.",
        "de": "OpenRouter nicht erreichbar (Netzwerk, Proxy oder Timeout). Prüfe "
              "die Verbindung und versuche es erneut; nutze in der Zwischenzeit "
              "die Google/Bing-Engine.",
        "es": "No se pudo contactar con OpenRouter (red, proxy o tiempo de "
              "espera). Comprueba la conexión y reintenta; mientras tanto puedes "
              "usar el motor Google/Bing.",
    },
    "clone.key_check": {
        "it": "Verifica della chiave OpenRouter…",
        "en": "Checking the OpenRouter key…",
        "fr": "Vérification de la clé OpenRouter…",
        "de": "OpenRouter-Schlüssel wird geprüft…",
        "es": "Comprobando la clave de OpenRouter…",
    },
    "clone.status_key_check": {
        "it": "verifica chiave…", "en": "checking key…",
        "fr": "vérification de la clé…", "de": "Schlüssel wird geprüft…",
        "es": "comprobando clave…",
    },
    "clone.key_stale": {
        "it": "La variabile di sistema OPENROUTER_API_KEY è impostata ma questa "
              "app non l'ha ancora caricata (su Windows serve riavviare l'app "
              "dopo averla creata). Riapri l'app oppure inserisci la chiave in "
              "⚙️ Impostazioni → Motore.",
        "en": "The OPENROUTER_API_KEY system variable is set but this app has not "
              "loaded it yet (on Windows, restart the app after creating it). "
              "Reopen the app or enter the key in ⚙️ Settings → Engine.",
        "fr": "La variable système OPENROUTER_API_KEY est définie mais cette app "
              "ne l'a pas encore chargée (sous Windows, redémarrez l'app après "
              "l'avoir créée). Rouvrez l'app ou saisissez la clé dans "
              "⚙️ Paramètres → Moteur.",
        "de": "Die Systemvariable OPENROUTER_API_KEY ist gesetzt, aber diese App "
              "hat sie noch nicht geladen (unter Windows die App nach dem Anlegen "
              "neu starten). Starte die App neu oder trage den Schlüssel in "
              "⚙️ Einstellungen → Engine ein.",
        "es": "La variable de sistema OPENROUTER_API_KEY está definida pero esta "
              "app aún no la ha cargado (en Windows, reinicia la app después de "
              "crearla). Reabre la app o introduce la clave en ⚙️ Ajustes → Motor.",
    },
    "clone.no_engine": {
        "it": "Motore pdf2zh_next non trovato.\nInstallalo con «Installa motore» in ⚙️ Impostazioni.",
        "en": "pdf2zh_next engine not found.\nInstall it with “Install engine” in ⚙️ Settings.",
        "fr": "Moteur pdf2zh_next introuvable.\nInstallez-le avec « Installer le moteur » dans ⚙️ Paramètres.",
        "de": "pdf2zh_next-Engine nicht gefunden.\nInstalliere sie mit „Engine installieren“ in ⚙️ Einstellungen.",
        "es": "Motor pdf2zh_next no encontrado.\nInstálalo con «Instalar motor» en ⚙️ Ajustes.",
    },
    "clone.no_engine_short": {
        "it": "motore assente", "en": "engine missing", "fr": "moteur absent",
        "de": "Engine fehlt", "es": "motor ausente",
    },
    "settings.clone.install": {
        "it": "Installa motore", "en": "Install engine",
        "fr": "Installer le moteur", "de": "Engine installieren",
        "es": "Instalar motor",
    },
    "settings.clone.install.tip": {
        "it": "Scarica Python 3.12 e pdf2zh_next in .venv2 accanto all'app (richiede uv).",
        "en": "Downloads Python 3.12 and pdf2zh_next into .venv2 next to the app (requires uv).",
        "fr": "Télécharge Python 3.12 et pdf2zh_next dans .venv2 à côté de l'app (nécessite uv).",
        "de": "Lädt Python 3.12 und pdf2zh_next in .venv2 neben der App (erfordert uv).",
        "es": "Descarga Python 3.12 y pdf2zh_next en .venv2 junto a la app (requiere uv).",
    },
    "engine.install.title": {
        "it": "Installa motore di clonazione", "en": "Install clone engine",
        "fr": "Installer le moteur de clonage", "de": "Klon-Engine installieren",
        "es": "Instalar motor de clonación",
    },
    "engine.install.intro": {
        "it": "Verrà creata la cartella .venv2 accanto all'app e installato pdf2zh_next. Richiede «uv» e una connessione; il download può richiedere qualche minuto. Se la cartella dell'app non è scrivibile, viene usata la cartella dati per-utente.",
        "en": "A .venv2 folder will be created next to the app and pdf2zh_next installed. Requires “uv” and a connection; the download may take a few minutes. If the app folder is not writable, the per-user data folder is used.",
        "fr": "Un dossier .venv2 sera créé à côté de l'app et pdf2zh_next installé. Nécessite « uv » et une connexion ; le téléchargement peut prendre quelques minutes. Si le dossier de l'app n'est pas accessible en écriture, le dossier de données par utilisateur est utilisé.",
        "de": "Neben der App wird der Ordner .venv2 erstellt und pdf2zh_next installiert. Erfordert „uv“ und eine Verbindung; der Download kann einige Minuten dauern. Ist der App-Ordner nicht beschreibbar, wird der Benutzer-Datenordner verwendet.",
        "es": "Se creará la carpeta .venv2 junto a la app y se instalará pdf2zh_next. Requiere «uv» y conexión; la descarga puede tardar unos minutos. Si la carpeta de la app no es escribible, se usa la carpeta de datos del usuario.",
    },
    "engine.install.running": {
        "it": "Installazione in corso…", "en": "Installing…",
        "fr": "Installation en cours…", "de": "Installation läuft…",
        "es": "Instalación en curso…",
    },
    "engine.install.cancelling": {
        "it": "Annullamento…", "en": "Cancelling…",
        "fr": "Annulation…", "de": "Wird abgebrochen…",
        "es": "Cancelando…",
    },
    "engine.install.cancel": {
        "it": "Annulla", "en": "Cancel", "fr": "Annuler",
        "de": "Abbrechen", "es": "Cancelar",
    },
    "engine.install.close": {
        "it": "Chiudi", "en": "Close", "fr": "Fermer",
        "de": "Schließen", "es": "Cerrar",
    },
    "engine.install.done": {
        "it": "✅ Motore installato:\\n{path}", "en": "✅ Engine installed:\\n{path}",
        "fr": "✅ Moteur installé :\\n{path}", "de": "✅ Engine installiert:\\n{path}",
        "es": "✅ Motor instalado:\\n{path}",
    },
    "engine.install.cancelled": {
        "it": "Installazione annullata.", "en": "Installation cancelled.",
        "fr": "Installation annulée.", "de": "Installation abgebrochen.",
        "es": "Instalación cancelada.",
    },
    "engine.install.no_uv": {
        "it": "«uv» non trovato. Installalo da https://docs.astral.sh/uv/ e riprova.",
        "en": "“uv” not found. Install it from https://docs.astral.sh/uv/ and try again.",
        "fr": "« uv » introuvable. Installez-le depuis https://docs.astral.sh/uv/ et réessayez.",
        "de": "„uv“ nicht gefunden. Installiere es von https://docs.astral.sh/uv/ und versuche es erneut.",
        "es": "«uv» no encontrado. Instálalo desde https://docs.astral.sh/uv/ e inténtalo de nuevo.",
    },
    "engine.install.failed": {
        "it": "❌ Installazione non riuscita. Controlla la connessione e riprova.",
        "en": "❌ Installation failed. Check your connection and try again.",
        "fr": "❌ Échec de l'installation. Vérifiez la connexion et réessayez.",
        "de": "❌ Installation fehlgeschlagen. Verbindung prüfen und erneut versuchen.",
        "es": "❌ La instalación falló. Comprueba la conexión e inténtalo de nuevo.",
    },
    "engine.install.failed_detail": {
        "it": "❌ Installazione non riuscita: {e}", "en": "❌ Installation failed: {e}",
        "fr": "❌ Échec de l'installation : {e}",
        "de": "❌ Installation fehlgeschlagen: {e}",
        "es": "❌ La instalación falló: {e}",
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
    # ── export della pagina tradotta ────────────────────────────────────────
    "clone.export.tip": {
        "it": "Esporta la pagina tradotta in PDF",
        "en": "Export the translated page to PDF",
        "fr": "Exporter la page traduite en PDF",
        "de": "Die übersetzte Seite als PDF exportieren",
        "es": "Exportar la página traducida a PDF",
    },
    "clone.fab.title": {
        "it": "✓ Azioni pagina", "en": "✓ Page actions",
        "fr": "✓ Actions de la page", "de": "✓ Seitenaktionen",
        "es": "✓ Acciones de la página",
    },
    "clone.fab.tip": {
        "it": "Azioni per la pagina tradotta",
        "en": "Actions for the translated page",
        "fr": "Actions pour la page traduite",
        "de": "Aktionen für die übersetzte Seite",
        "es": "Acciones para la página traducida",
    },
    "clone.fab.save_download": {
        "it": "⬇ Salva in Download", "en": "⬇ Save to Downloads",
        "fr": "⬇ Enregistrer dans Téléchargements",
        "de": "⬇ In Downloads speichern",
        "es": "⬇ Guardar en Descargas",
    },
    "clone.fab.export": {
        "it": "💾 Esporta…", "en": "💾 Export…", "fr": "💾 Exporter…",
        "de": "💾 Exportieren…", "es": "💾 Exportar…",
    },
    "clone.fab.next": {
        "it": "▶ Traduci la successiva", "en": "▶ Translate next",
        "fr": "▶ Traduire la suivante", "de": "▶ Nächste übersetzen",
        "es": "▶ Traducir la siguiente",
    },
    "clone.fab.retranslate": {
        "it": "🔁 Ritraduci", "en": "🔁 Retranslate",
        "fr": "🔁 Retraduire", "de": "🔁 Neu übersetzen",
        "es": "🔁 Volver a traducir",
    },
    "clone.fab.open_external": {
        "it": "👁 Apri con il visualizzatore di sistema",
        "en": "👁 Open with system viewer",
        "fr": "👁 Ouvrir avec la visionneuse système",
        "de": "👁 Mit Systembetrachter öffnen",
        "es": "👁 Abrir con el visor del sistema",
    },
    "clone.fab.purge": {
        "it": "🗑 Cancella cache della pagina",
        "en": "🗑 Clear page cache",
        "fr": "🗑 Vider le cache de la page",
        "de": "🗑 Seiten-Cache löschen",
        "es": "🗑 Borrar caché de la página",
    },
    "clone.fab.saved_download": {
        "it": "Pagina salvata in Download: {path}",
        "en": "Page saved to Downloads: {path}",
        "fr": "Page enregistrée dans Téléchargements : {path}",
        "de": "Seite in Downloads gespeichert: {path}",
        "es": "Página guardada en Descargas: {path}",
    },
    "clone.fab.save_error": {
        "it": "Impossibile salvare la pagina in Download.",
        "en": "Could not save the page to Downloads.",
        "fr": "Impossible d'enregistrer la page dans Téléchargements.",
        "de": "Seite konnte nicht in Downloads gespeichert werden.",
        "es": "No se pudo guardar la página en Descargas.",
    },
    "clone.fab.last_page": {
        "it": "Sei già all'ultima pagina.",
        "en": "You are already on the last page.",
        "fr": "Vous êtes déjà à la dernière page.",
        "de": "Sie sind bereits auf der letzten Seite.",
        "es": "Ya estás en la última página.",
    },
    "clone.fab.retranslate_confirm": {
        "it": "Ritradurre la pagina {page}? Il clone attuale ({engine}) verrà "
              "eliminato e rigenerato.",
        "en": "Retranslate page {page}? The current clone ({engine}) will be "
              "deleted and regenerated.",
        "fr": "Retraduire la page {page} ? Le clone actuel ({engine}) sera "
              "supprimé et régénéré.",
        "de": "Seite {page} neu übersetzen? Der aktuelle Klon ({engine}) wird "
              "gelöscht und neu erzeugt.",
        "es": "¿Volver a traducir la página {page}? El clon actual ({engine}) "
              "se eliminará y regenerará.",
    },
    "clone.fab.purge_confirm": {
        "it": "Eliminare dalla cache la pagina {page} ({engine})? Dovrai "
              "ritradurla per rivederla.",
        "en": "Remove page {page} ({engine}) from the cache? You will need to "
              "retranslate it to see it again.",
        "fr": "Supprimer la page {page} ({engine}) du cache ? Vous devrez la "
              "retraduire pour la revoir.",
        "de": "Seite {page} ({engine}) aus dem Cache entfernen? Sie müssen sie "
              "neu übersetzen, um sie wiederzusehen.",
        "es": "¿Eliminar la página {page} ({engine}) de la caché? Tendrás que "
              "volver a traducirla para verla.",
    },
    "clone.fab.purged": {
        "it": "Cache della pagina {page} eliminata ({size}).",
        "en": "Page {page} cache cleared ({size}).",
        "fr": "Cache de la page {page} vidé ({size}).",
        "de": "Cache der Seite {page} gelöscht ({size}).",
        "es": "Caché de la página {page} borrada ({size}).",
    },
    "export.title": {
        "it": "Esporta pagina tradotta", "en": "Export translated page",
        "fr": "Exporter la page traduite",
        "de": "Übersetzte Seite exportieren",
        "es": "Exportar página traducida",
    },
    "export.mode.current": {
        "it": "Pagina corrente", "en": "Current page", "fr": "Page actuelle",
        "de": "Aktuelle Seite", "es": "Página actual",
    },
    "export.mode.range": {
        "it": "Intervallo di pagine", "en": "Page range",
        "fr": "Plage de pages", "de": "Seitenbereich",
        "es": "Intervalo de páginas",
    },
    "export.range.from": {
        "it": "Da pagina", "en": "From page", "fr": "De la page",
        "de": "Von Seite", "es": "Desde la página",
    },
    "export.range.to": {
        "it": "A pagina", "en": "To page", "fr": "À la page",
        "de": "Bis Seite", "es": "Hasta la página",
    },
    "export.range.from.short": {
        "it": "Da", "en": "From", "fr": "De", "de": "Von", "es": "De",
    },
    "export.range.to.short": {
        "it": "A", "en": "To", "fr": "À", "de": "Bis", "es": "A",
    },
    "export.mode.free": {
        "it": "Pagine (1,3,7-9)", "en": "Pages (1,3,7-9)",
        "fr": "Pages (1,3,7-9)", "de": "Seiten (1,3,7-9)",
        "es": "Páginas (1,3,7-9)",
    },
    "export.free.placeholder": {
        "it": "es. 1,3,7-9", "en": "e.g. 1,3,7-9", "fr": "ex. 1,3,7-9",
        "de": "z. B. 1,3,7-9", "es": "p. ej. 1,3,7-9",
    },
    "export.free.hint": {
        "it": "Pagine singole o intervalli separati da virgole: 1,3,7-9. "
              "Usa «all» per tutte le pagine.",
        "en": "Single pages or ranges separated by commas: 1,3,7-9. "
              "Use “all” for every page.",
        "fr": "Pages isolées ou plages séparées par des virgules : 1,3,7-9. "
              "Utilisez « all » pour toutes les pages.",
        "de": "Einzelseiten oder Bereiche, durch Kommas getrennt: 1,3,7-9. "
              "„all“ für alle Seiten.",
        "es": "Páginas sueltas o rangos separados por comas: 1,3,7-9. "
              "Usa «all» para todas las páginas.",
    },
    "export.free.err.empty": {
        "it": "Indica almeno una pagina (es. 1,3,7-9).",
        "en": "Enter at least one page (e.g. 1,3,7-9).",
        "fr": "Indiquez au moins une page (ex. 1,3,7-9).",
        "de": "Geben Sie mindestens eine Seite an (z. B. 1,3,7-9).",
        "es": "Indica al menos una página (p. ej. 1,3,7-9).",
    },
    "export.free.err.token": {
        "it": "Voce non valida: «{token}».",
        "en": "Invalid item: “{token}”.",
        "fr": "Entrée non valide : « {token} ».",
        "de": "Ungültiger Eintrag: „{token}“.",
        "es": "Entrada no válida: «{token}».",
    },
    "export.free.err.range": {
        "it": "Intervallo non valido: «{token}».",
        "en": "Invalid range: “{token}”.",
        "fr": "Plage non valide : « {token} ».",
        "de": "Ungültiger Bereich: „{token}“.",
        "es": "Rango no válido: «{token}».",
    },
    "export.free.err.bounds": {
        "it": "Pagina fuori intervallo (1–{total}).",
        "en": "Page out of range (1–{total}).",
        "fr": "Page hors plage (1–{total}).",
        "de": "Seite außerhalb des Bereichs (1–{total}).",
        "es": "Página fuera de rango (1–{total}).",
    },
    "export.free.err.no_pages": {
        "it": "Il documento non contiene pagine.",
        "en": "The document has no pages.",
        "fr": "Le document ne contient aucune page.",
        "de": "Das Dokument enthält keine Seiten.",
        "es": "El documento no contiene páginas.",
    },
    "export.free.err.none": {
        "it": "Nessuna pagina selezionata.",
        "en": "No pages selected.",
        "fr": "Aucune page sélectionnée.",
        "de": "Keine Seiten ausgewählt.",
        "es": "Ninguna página seleccionada.",
    },
    "export.free.err.too_many": {
        "it": "Troppe pagine richieste.",
        "en": "Too many pages requested.",
        "fr": "Trop de pages demandées.",
        "de": "Zu viele Seiten angefordert.",
        "es": "Demasiadas páginas solicitadas.",
    },
    "export.ready": {
        "it": "Già tradotte in cache: {cached} di {total} · Da tradurre: {missing}",
        "en": "Already translated in cache: {cached} of {total} · To translate: {missing}",
        "fr": "Déjà traduites en cache : {cached} sur {total} · À traduire : {missing}",
        "de": "Bereits im Cache übersetzt: {cached} von {total} · Zu übersetzen: {missing}",
        "es": "Ya traducidas en caché: {cached} de {total} · Por traducir: {missing}",
    },
    "export.translate_missing": {
        "it": "Traduci prima le pagine mancanti (attesa)",
        "en": "Translate missing pages first (will wait)",
        "fr": "Traduire d'abord les pages manquantes (attente)",
        "de": "Fehlende Seiten zuerst übersetzen (Wartezeit)",
        "es": "Traducir primero las páginas faltantes (espera)",
    },
    "export.dialog": {
        "it": "Salva PDF tradotto", "en": "Save translated PDF",
        "fr": "Enregistrer le PDF traduit",
        "de": "Übersetztes PDF speichern",
        "es": "Guardar PDF traducido",
    },
    "export.filter": {
        "it": "PDF (*.pdf)", "en": "PDF (*.pdf)", "fr": "PDF (*.pdf)",
        "de": "PDF (*.pdf)", "es": "PDF (*.pdf)",
    },
    "export.not_ready": {
        "it": "La pagina corrente non è ancora tradotta.",
        "en": "The current page is not translated yet.",
        "fr": "La page actuelle n'est pas encore traduite.",
        "de": "Die aktuelle Seite ist noch nicht übersetzt.",
        "es": "La página actual aún no está traducida.",
    },
    "export.none_ready": {
        "it": "Nessuna pagina dell'intervallo è tradotta.",
        "en": "No page in the range is translated.",
        "fr": "Aucune page de la plage n'est traduite.",
        "de": "Keine Seite im Bereich ist übersetzt.",
        "es": "Ninguna página del intervalo está traducida.",
    },
    "export.done": {
        "it": "✅ Esportate {count} pagine (non riuscite: {failed})",
        "en": "✅ Exported {count} pages (failed: {failed})",
        "fr": "✅ {count} pages exportées (échecs : {failed})",
        "de": "✅ {count} Seiten exportiert (fehlgeschlagen: {failed})",
        "es": "✅ {count} páginas exportadas (fallidas: {failed})",
    },
    "export.error": {
        "it": "❌ Errore di esportazione", "en": "❌ Export error",
        "fr": "❌ Erreur d'exportation", "de": "❌ Exportfehler",
        "es": "❌ Error de exportación",
    },
    "export.need_doc": {
        "it": "Apri prima un PDF", "en": "Open a PDF first",
        "fr": "Ouvrez d'abord un PDF", "de": "Öffnen Sie zuerst ein PDF",
        "es": "Abre primero un PDF",
    },
    "export.busy": {
        "it": "Un'esportazione è già in corso: attendi che finisca.",
        "en": "An export is already running: wait for it to finish.",
        "fr": "Une exportation est déjà en cours : attendez la fin.",
        "de": "Ein Export läuft bereits: Warten Sie, bis er fertig ist.",
        "es": "Ya hay una exportación en curso: espera a que termine.",
    },
    "export.progress.title": {
        "it": "Traduzione delle pagine mancanti",
        "en": "Translating missing pages",
        "fr": "Traduction des pages manquantes",
        "de": "Fehlende Seiten werden übersetzt",
        "es": "Traduciendo las páginas faltantes",
    },
    "export.progress.label": {
        "it": "Pagina {page} — {pos} di {total}",
        "en": "Page {page} — {pos} of {total}",
        "fr": "Page {page} — {pos} sur {total}",
        "de": "Seite {page} — {pos} von {total}",
        "es": "Página {page} — {pos} de {total}",
    },
    "export.progress.cancelling": {
        "it": "Interruzione…", "en": "Cancelling…",
        "fr": "Annulation…", "de": "Abbruch…",
        "es": "Cancelando…",
    },
    "export.cancelled": {
        "it": "Esportazione annullata.", "en": "Export cancelled.",
        "fr": "Exportation annulée.", "de": "Export abgebrochen.",
        "es": "Exportación cancelada.",
    },
    "export.failed_pages": {
        "it": "Pagine non riuscite: {count}", "en": "Failed pages: {count}",
        "fr": "Pages échouées : {count}",
        "de": "Fehlgeschlagene Seiten: {count}",
        "es": "Páginas fallidas: {count}",
    },
    "export.group.translation": {
        "it": "Traduzione", "en": "Translation", "fr": "Traduction",
        "de": "Übersetzung", "es": "Traducción",
    },
    "export.group.format": {
        "it": "Formato di uscita", "en": "Output format",
        "fr": "Format de sortie", "de": "Ausgabeformat",
        "es": "Formato de salida",
    },
    "export.format.merged": {
        "it": "Un unico PDF", "en": "A single PDF",
        "fr": "Un seul PDF", "de": "Eine einzelne PDF",
        "es": "Un único PDF",
    },
    "export.format.zip": {
        "it": "Pagine singole (ZIP)", "en": "Single pages (ZIP)",
        "fr": "Pages séparées (ZIP)", "de": "Einzelne Seiten (ZIP)",
        "es": "Páginas sueltas (ZIP)",
    },
    "export.filter_zip": {
        "it": "Archivio ZIP (*.zip)", "en": "ZIP archive (*.zip)",
        "fr": "Archive ZIP (*.zip)", "de": "ZIP-Archiv (*.zip)",
        "es": "Archivo ZIP (*.zip)",
    },
    "export.dialog_zip": {
        "it": "Salva pagine tradotte (ZIP)",
        "en": "Save translated pages (ZIP)",
        "fr": "Enregistrer les pages traduites (ZIP)",
        "de": "Übersetzte Seiten speichern (ZIP)",
        "es": "Guardar páginas traducidas (ZIP)",
    },
    "export.menu.wizard": {
        "it": "Esporta… (procedura guidata)",
        "en": "Export… (guided)",
        "fr": "Exporter… (assisté)",
        "de": "Exportieren… (Assistent)",
        "es": "Exportar… (guiado)",
    },
    "export.menu.current": {
        "it": "Esporta pagina corrente (rapido)",
        "en": "Export current page (quick)",
        "fr": "Exporter la page actuelle (rapide)",
        "de": "Aktuelle Seite exportieren (schnell)",
        "es": "Exportar página actual (rápido)",
    },
    "export.wizard.title": {
        "it": "Esporta pagine tradotte",
        "en": "Export translated pages",
        "fr": "Exporter les pages traduites",
        "de": "Übersetzte Seiten exportieren",
        "es": "Exportar páginas traducidas",
    },
    "export.wizard.step.file": {
        "it": "File", "en": "File", "fr": "Fichier", "de": "Datei", "es": "Archivo",
    },
    "export.wizard.step.pages": {
        "it": "Pagine", "en": "Pages", "fr": "Pages", "de": "Seiten", "es": "Páginas",
    },
    "export.wizard.step.langs": {
        "it": "Lingue", "en": "Languages", "fr": "Langues", "de": "Sprachen",
        "es": "Idiomas",
    },
    "export.wizard.step.engine": {
        "it": "Motore", "en": "Engine", "fr": "Moteur", "de": "Engine", "es": "Motor",
    },
    "export.wizard.step.output": {
        "it": "Output", "en": "Output", "fr": "Sortie", "de": "Ausgabe", "es": "Salida",
    },
    "export.wizard.next": {
        "it": "Avanti →", "en": "Next →", "fr": "Suivant →", "de": "Weiter →",
        "es": "Siguiente →",
    },
    "export.wizard.back": {
        "it": "← Indietro", "en": "← Back", "fr": "← Retour", "de": "← Zurück",
        "es": "← Atrás",
    },
    "export.wizard.start": {
        "it": "Avvia l'esportazione", "en": "Start export",
        "fr": "Lancer l'exportation", "de": "Export starten",
        "es": "Iniciar exportación",
    },
    "export.wizard.file.title": {
        "it": "Documento", "en": "Document", "fr": "Document", "de": "Dokument",
        "es": "Documento",
    },
    "export.wizard.file.hint": {
        "it": "Il PDF aperto di cui stai esportando le pagine tradotte.",
        "en": "The open PDF whose translated pages you are exporting.",
        "fr": "Le PDF ouvert dont vous exportez les pages traduites.",
        "de": "Das geöffnete PDF, dessen übersetzte Seiten exportiert werden.",
        "es": "El PDF abierto cuyas páginas traducidas vas a exportar.",
    },
    "export.wizard.file.info": {
        "it": "{name}\n{pages} pagine · {size} MB",
        "en": "{name}\n{pages} pages · {size} MB",
        "fr": "{name}\n{pages} pages · {size} Mo",
        "de": "{name}\n{pages} Seiten · {size} MB",
        "es": "{name}\n{pages} páginas · {size} MB",
    },
    "export.wizard.file.none": {
        "it": "Nessun PDF aperto.", "en": "No PDF open.", "fr": "Aucun PDF ouvert.",
        "de": "Kein PDF geöffnet.", "es": "Ningún PDF abierto.",
    },
    "export.wizard.pages.title": {
        "it": "Quali pagine?", "en": "Which pages?", "fr": "Quelles pages ?",
        "de": "Welche Seiten?", "es": "¿Qué páginas?",
    },
    "export.wizard.pages.hint": {
        "it": "Esporta la pagina corrente oppure un intervallo.",
        "en": "Export the current page or a range.",
        "fr": "Exportez la page actuelle ou une plage.",
        "de": "Aktuelle Seite oder einen Bereich exportieren.",
        "es": "Exporta la página actual o un intervalo.",
    },
    "export.wizard.langs.title": {
        "it": "Lingue", "en": "Languages", "fr": "Langues", "de": "Sprachen",
        "es": "Idiomas",
    },
    "export.wizard.langs.hint": {
        "it": "Origine «Auto» riconosce la lingua da sola.",
        "en": "Source «Auto» detects the language on its own.",
        "fr": "La source « Auto » détecte la langue.",
        "de": "Quelle «Auto» erkennt die Sprache.",
        "es": "El origen «Auto» detecta el idioma.",
    },
    "export.wizard.engine.title": {
        "it": "Motore", "en": "Engine", "fr": "Moteur", "de": "Engine", "es": "Motor",
    },
    "export.wizard.engine.hint": {
        "it": "Google/Bing sono gratuiti; LLM (OpenRouter) ha un costo per pagina.",
        "en": "Google/Bing are free; LLM (OpenRouter) costs per page.",
        "fr": "Google/Bing sont gratuits ; LLM (OpenRouter) coûte par page.",
        "de": "Google/Bing sind kostenlos; LLM (OpenRouter) kostet pro Seite.",
        "es": "Google/Bing son gratuitos; LLM (OpenRouter) cuesta por página.",
    },
    "export.wizard.est": {
        "it": "Da tradurre: {missing} di {total} · già in cache: {cached} · tempo stimato: ~{time}",
        "en": "To translate: {missing} of {total} · cached: {cached} · estimated time: ~{time}",
        "fr": "À traduire : {missing} sur {total} · en cache : {cached} · temps estimé : ~{time}",
        "de": "Zu übersetzen: {missing} von {total} · im Cache: {cached} · geschätzt: ~{time}",
        "es": "Por traducir: {missing} de {total} · en caché: {cached} · tiempo estimado: ~{time}",
    },
    "export.wizard.output.title": {
        "it": "Output", "en": "Output", "fr": "Sortie", "de": "Ausgabe", "es": "Salida",
    },
    "export.wizard.output.hint": {
        "it": "Ultimo controllo: nome, formato e destinazione.",
        "en": "Final check: name, format and destination.",
        "fr": "Dernière vérification : nom, format et destination.",
        "de": "Letzte Prüfung: Name, Format und Ziel.",
        "es": "Última comprobación: nombre, formato y destino.",
    },
    "export.wizard.output.path": {
        "it": "Percorso di salvataggio", "en": "Save path",
        "fr": "Chemin d'enregistrement", "de": "Speicherpfad", "es": "Ruta de guardado",
    },
    "export.wizard.output.browse": {
        "it": "Sfoglia…", "en": "Browse…", "fr": "Parcourir…",
        "de": "Durchsuchen…", "es": "Examinar…",
    },
    "export.wizard.output.folder": {
        "it": "Cartella di destinazione", "en": "Destination folder",
        "fr": "Dossier de destination", "de": "Zielordner",
        "es": "Carpeta de destino",
    },
    "export.wizard.output.choose_folder": {
        "it": "Scegli cartella…", "en": "Choose folder…",
        "fr": "Choisir un dossier…", "de": "Ordner wählen…",
        "es": "Elegir carpeta…",
    },
    "export.wizard.output.filename": {
        "it": "Nome file", "en": "File name", "fr": "Nom du fichier",
        "de": "Dateiname", "es": "Nombre de archivo",
    },
    "export.wizard.output.folder_name": {
        "it": "Nome cartella", "en": "Folder name", "fr": "Nom du dossier",
        "de": "Ordnername", "es": "Nombre de carpeta",
    },
    "export.format.folder": {
        "it": "Pagine singole in una cartella",
        "en": "Single pages in a folder",
        "fr": "Pages séparées dans un dossier",
        "de": "Einzelseiten in einem Ordner",
        "es": "Páginas sueltas en una carpeta",
    },
    "export.wizard.sum.file": {
        "it": "File", "en": "File", "fr": "Fichier", "de": "Datei", "es": "Archivo",
    },
    "export.wizard.sum.langs": {
        "it": "Lingue", "en": "Languages", "fr": "Langues", "de": "Sprachen",
        "es": "Idiomas",
    },
    "export.wizard.sum.engine": {
        "it": "Motore", "en": "Engine", "fr": "Moteur", "de": "Engine", "es": "Motor",
    },
    "export.wizard.sum.output": {
        "it": "Uscita", "en": "Output", "fr": "Sortie", "de": "Ausgabe", "es": "Salida",
    },
    "export.wizard.pages.numbering": {
        "it": "I numeri sono le pagine fisiche del file (1 = prima pagina). "
              "Alcuni PDF hanno una numerazione stampata diversa (romana, con "
              "prefisso, ecc.): la mostriamo accanto all'anteprima.",
        "en": "The numbers are the file's physical pages (1 = first page). Some "
              "PDFs use a different printed numbering (roman, prefixed, etc.): "
              "we show it next to the preview.",
        "fr": "Les numéros sont les pages physiques du fichier (1 = première "
              "page). Certains PDF utilisent une numérotation imprimée "
              "différente (romaine, avec préfixe, etc.) : nous l'affichons à "
              "côté de l'aperçu.",
        "de": "Die Nummern sind die physischen Seiten der Datei (1 = erste "
              "Seite). Manche PDFs haben eine andere gedruckte Nummerierung "
              "(römisch, mit Präfix usw.): wir zeigen sie neben der Vorschau.",
        "es": "Los números son las páginas físicas del archivo (1 = primera "
              "página). Algunos PDF usan una numeración impresa distinta "
              "(romana, con prefijo, etc.): la mostramos junto a la vista previa.",
    },
    "export.wizard.preview.caption": {
        "it": "pagina fisica {n} · stampata «{label}»",
        "en": "physical page {n} · printed «{label}»",
        "fr": "page physique {n} · imprimée «{label}»",
        "de": "physische Seite {n} · gedruckt «{label}»",
        "es": "página física {n} · impresa «{label}»",
    },
    "export.wizard.preview.caption_plain": {
        "it": "pagina {n}", "en": "page {n}", "fr": "page {n}",
        "de": "Seite {n}", "es": "página {n}",
    },
    "export.wizard.preview.spin_label": {
        "it": "«{label}» stampata", "en": "printed «{label}»",
        "fr": "«{label}» imprimée", "de": "gedruckt «{label}»",
        "es": "impresa «{label}»",
    },
    "export.wizard.preview.label_diff": {
        "it": "La numerazione stampata è diversa da quella fisica per la "
              "selezione scelta.",
        "en": "The printed numbering differs from the physical one for the "
              "selected range.",
        "fr": "La numérotation imprimée diffère de la numérotation physique "
              "pour la plage sélectionnée.",
        "de": "Die gedruckte Nummerierung weicht für den gewählten Bereich von "
              "der physischen ab.",
        "es": "La numeración impresa difiere de la física para el intervalo "
              "seleccionado.",
    },
    "export.wizard.preview.count": {
        "it": "{n} pagine selezionate · {label}",
        "en": "{n} pages selected · {label}",
        "fr": "{n} pages sélectionnées · {label}",
        "de": "{n} Seiten ausgewählt · {label}",
        "es": "{n} páginas seleccionadas · {label}",
    },
    "export.wizard.preview.none": {
        "it": "Anteprima non disponibile (PyMuPDF assente).",
        "en": "Preview unavailable (PyMuPDF missing).",
        "fr": "Aperçu indisponible (PyMuPDF absent).",
        "de": "Vorschau nicht verfügbar (PyMuPDF fehlt).",
        "es": "Vista previa no disponible (falta PyMuPDF).",
    },
    "clone.working.badge": {
        "it": "in lavorazione: pagina {n}", "en": "working: page {n}",
        "fr": "en cours : page {n}", "de": "in Arbeit: Seite {n}",
        "es": "en curso: página {n}",
    },
    "clone.working.many": {
        "it": "{n} pagine in lavorazione", "en": "{n} pages in progress",
        "fr": "{n} pages en cours", "de": "{n} Seiten in Arbeit",
        "es": "{n} páginas en curso",
    },
    "export.progress.engine_lang": {
        "it": "Motore: {engine} · Lingua di uscita: {lang}",
        "en": "Engine: {engine} · Output language: {lang}",
        "fr": "Moteur : {engine} · Langue de sortie : {lang}",
        "de": "Engine: {engine} · Ausgabesprache: {lang}",
        "es": "Motor: {engine} · Idioma de salida: {lang}",
    },
    "export.progress.range": {
        "it": "Intervallo: {from}–{to} · Da tradurre: {missing} · In cache: {cached}",
        "en": "Range: {from}–{to} · To translate: {missing} · Cached: {cached}",
        "fr": "Plage : {from}–{to} · À traduire : {missing} · En cache : {cached}",
        "de": "Bereich: {from}–{to} · Zu übersetzen: {missing} · Im Cache: {cached}",
        "es": "Intervalo: {from}–{to} · Por traducir: {missing} · En caché: {cached}",
    },
    "export.progress.range_done": {
        "it": "Intervallo: {from}–{to} · Tradotte: {done} · Non riuscite: {failed}",
        "en": "Range: {from}–{to} · Translated: {done} · Failed: {failed}",
        "fr": "Plage : {from}–{to} · Traduites : {done} · Échecs : {failed}",
        "de": "Bereich: {from}–{to} · Übersetzt: {done} · Fehlgeschlagen: {failed}",
        "es": "Intervalo: {from}–{to} · Traducidas: {done} · Fallidas: {failed}",
    },
    "export.progress.pages": {
        "it": "Pagine {label} · Da tradurre: {missing} · In cache: {cached}",
        "en": "Pages {label} · To translate: {missing} · Cached: {cached}",
        "fr": "Pages {label} · À traduire : {missing} · En cache : {cached}",
        "de": "Seiten {label} · Zu übersetzen: {missing} · Im Cache: {cached}",
        "es": "Páginas {label} · Por traducir: {missing} · En caché: {cached}",
    },
    "export.progress.pages_done": {
        "it": "Pagine {label} · Tradotte: {done} · Non riuscite: {failed}",
        "en": "Pages {label} · Translated: {done} · Failed: {failed}",
        "fr": "Pages {label} · Traduites : {done} · Échecs : {failed}",
        "de": "Seiten {label} · Übersetzt: {done} · Fehlgeschlagen: {failed}",
        "es": "Páginas {label} · Traducidas: {done} · Fallidas: {failed}",
    },
    "export.progress.stats": {
        "it": "Tradotte: {done} · Non riuscite: {failed} · Trascorso: {elapsed}",
        "en": "Translated: {done} · Failed: {failed} · Elapsed: {elapsed}",
        "fr": "Traduites : {done} · Échecs : {failed} · Écoulé : {elapsed}",
        "de": "Übersetzt: {done} · Fehlgeschlagen: {failed} · Verstrichen: {elapsed}",
        "es": "Traducidas: {done} · Fallidas: {failed} · Transcurrido: {elapsed}",
    },
    "export.progress.eta": {
        "it": "Tempo stimato rimanente: ~{eta}",
        "en": "Estimated time remaining: ~{eta}",
        "fr": "Temps restant estimé : ~{eta}",
        "de": "Geschätzte Restzeit: ~{eta}",
        "es": "Tiempo restante estimado: ~{eta}",
    },
    "export.progress.page_ok": {
        "it": "✓ pag {page}", "en": "✓ page {page}", "fr": "✓ p. {page}",
        "de": "✓ S. {page}", "es": "✓ pág. {page}",
    },
    "export.progress.page_fail": {
        "it": "✗ pag {page} — {reason}", "en": "✗ page {page} — {reason}",
        "fr": "✗ p. {page} — {reason}", "de": "✗ S. {page} — {reason}",
        "es": "✗ pág. {page} — {reason}",
    },
    "export.progress.activity_page": {
        "it": "⏳ Pagina {page} in lavorazione",
        "en": "⏳ Page {page} in progress",
        "fr": "⏳ Page {page} en cours",
        "de": "⏳ Seite {page} in Arbeit",
        "es": "⏳ Página {page} en proceso",
    },
    "export.progress.activity_page_log": {
        "it": "▶ Pagina {page} in lavorazione",
        "en": "▶ Page {page} in progress",
        "fr": "▶ Page {page} en cours",
        "de": "▶ Seite {page} in Arbeit",
        "es": "▶ Página {page} en proceso",
    },
    "export.progress.activity_page_pos": {
        "it": "⏳ Pagina {page} in lavorazione ({pos} di {total})",
        "en": "⏳ Page {page} in progress ({pos} of {total})",
        "fr": "⏳ Page {page} en cours ({pos} sur {total})",
        "de": "⏳ Seite {page} in Arbeit ({pos} von {total})",
        "es": "⏳ Página {page} en proceso ({pos} de {total})",
    },
    "export.progress.activity_done_page": {
        "it": "✓ Pagina {page} completata", "en": "✓ Page {page} done",
        "fr": "✓ Page {page} terminée", "de": "✓ Seite {page} fertig",
        "es": "✓ Página {page} completada",
    },
    "export.progress.activity_exporting": {
        "it": "💾 Preparazione e salvataggio del PDF…",
        "en": "💾 Preparing and saving the PDF…",
        "fr": "💾 Préparation et enregistrement du PDF…",
        "de": "💾 PDF wird vorbereitet und gespeichert…",
        "es": "💾 Preparando y guardando el PDF…",
    },
    "export.progress.completed_title": {
        "it": "✅ Esportazione completata", "en": "✅ Export completed",
        "fr": "✅ Exportation terminée", "de": "✅ Export abgeschlossen",
        "es": "✅ Exportación completada",
    },
    "export.progress.completed_summary": {
        "it": "{count} pagine salvate · non riuscite: {failed} · tempo totale: {elapsed}",
        "en": "{count} pages saved · failed: {failed} · total time: {elapsed}",
        "fr": "{count} pages enregistrées · échecs : {failed} · durée totale : {elapsed}",
        "de": "{count} Seiten gespeichert · fehlgeschlagen: {failed} · Gesamtzeit: {elapsed}",
        "es": "{count} páginas guardadas · fallidas: {failed} · tiempo total: {elapsed}",
    },
    "export.progress.saved_path": {
        "it": "File: {path}", "en": "File: {path}", "fr": "Fichier : {path}",
        "de": "Datei: {path}", "es": "Archivo: {path}",
    },
    "export.progress.save_download": {
        "it": "⬇ Copia in Download", "en": "⬇ Copy to Downloads",
        "fr": "⬇ Copier dans Téléchargements",
        "de": "⬇ In Downloads kopieren",
        "es": "⬇ Copiar en Descargas",
    },
    "export.progress.save_download.tip": {
        "it": "Copia il file esportato nella cartella Download; l'originale resta "
              "dove l'hai salvato.",
        "en": "Copies the exported file to the Downloads folder; the original "
              "stays where you saved it.",
        "fr": "Copie le fichier exporté dans le dossier Téléchargements ; "
              "l'original reste à l'endroit choisi.",
        "de": "Kopiert die exportierte Datei in den Downloads-Ordner; das "
              "Original bleibt am gewählten Ort.",
        "es": "Copia el archivo exportado a la carpeta Descargas; el original "
              "permanece donde lo guardaste.",
    },
    "export.progress.downloaded": {
        "it": "✅ Copiato in {path}", "en": "✅ Copied to {path}",
        "fr": "✅ Copié dans {path}", "de": "✅ Kopiert nach {path}",
        "es": "✅ Copiado en {path}",
    },
    "export.progress.download_error": {
        "it": "❌ Impossibile salvare nella cartella Download.",
        "en": "❌ Could not save to the Downloads folder.",
        "fr": "❌ Impossible d'enregistrer dans Téléchargements.",
        "de": "❌ Speichern im Downloads-Ordner fehlgeschlagen.",
        "es": "❌ No se pudo guardar en la carpeta Descargas.",
    },
    "export.progress.open_folder": {
        "it": "📂 Apri cartella", "en": "📂 Open folder",
        "fr": "📂 Ouvrir le dossier", "de": "📂 Ordner öffnen",
        "es": "📂 Abrir carpeta",
    },
    "export.progress.close": {
        "it": "Chiudi", "en": "Close", "fr": "Fermer", "de": "Schließen",
        "es": "Cerrar",
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
    "settings.clone.apikey": {
        "it": "Chiave OpenRouter (LLM)", "en": "OpenRouter key (LLM)",
        "fr": "Clé OpenRouter (LLM)", "de": "OpenRouter-Schlüssel (LLM)",
        "es": "Clave OpenRouter (LLM)",
    },
    "apikey.title": {
        "it": "Chiave OpenRouter", "en": "OpenRouter key",
        "fr": "Clé OpenRouter", "de": "OpenRouter-Schlüssel",
        "es": "Clave OpenRouter",
    },
    "apikey.intro": {
        "it": "Serve solo per il motore LLM (OpenRouter). La chiave viene "
              "salvata in un file per-utente protetto nella cartella dati "
              "dell'app e non finisce mai in config.json o nei log.",
        "en": "Needed only for the LLM engine (OpenRouter). The key is stored "
              "in a protected per-user file in the app data folder and never "
              "goes into config.json or the logs.",
        "fr": "Nécessaire uniquement pour le moteur LLM (OpenRouter). La clé "
              "est enregistrée dans un fichier par utilisateur protégé dans le "
              "dossier de données de l'app et n'apparaît jamais dans "
              "config.json ni les logs.",
        "de": "Nur für die LLM-Engine (OpenRouter) nötig. Der Schlüssel wird in "
              "einer geschützten Benutzerdatei im App-Datenordner gespeichert "
              "und landet nie in config.json oder den Logs.",
        "es": "Solo se necesita para el motor LLM (OpenRouter). La clave se "
              "guarda en un archivo por usuario protegido en la carpeta de "
              "datos de la app y nunca acaba en config.json ni en los logs.",
    },
    "apikey.placeholder": {
        "it": "sk-or-…", "en": "sk-or-…", "fr": "sk-or-…",
        "de": "sk-or-…", "es": "sk-or-…",
    },
    "apikey.show": {
        "it": "Mostra", "en": "Show", "fr": "Afficher", "de": "Anzeigen",
        "es": "Mostrar",
    },
    "apikey.verify": {
        "it": "Verifica", "en": "Verify", "fr": "Vérifier", "de": "Prüfen",
        "es": "Verificar",
    },
    "apikey.clear": {
        "it": "Rimuovi", "en": "Remove", "fr": "Supprimer", "de": "Entfernen",
        "es": "Quitar",
    },
    "apikey.removed": {
        "it": "Chiave rimossa.", "en": "Key removed.",
        "fr": "Clé supprimée.", "de": "Schlüssel entfernt.",
        "es": "Clave eliminada.",
    },
    "apikey.verify_ok": {
        "it": "✅ Chiave valida.", "en": "✅ Valid key.", "fr": "✅ Clé valide.",
        "de": "✅ Gültiger Schlüssel.", "es": "✅ Clave válida.",
    },
    "apikey.verify_invalid": {
        "it": "❌ Chiave non valida (401).", "en": "❌ Invalid key (401).",
        "fr": "❌ Clé non valide (401).", "de": "❌ Ungültiger Schlüssel (401).",
        "es": "❌ Clave no válida (401).",
    },
    "apikey.verify_missing": {
        "it": "Inserisci una chiave.", "en": "Enter a key.",
        "fr": "Saisissez une clé.", "de": "Gib einen Schlüssel ein.",
        "es": "Introduce una clave.",
    },
    "apikey.verify_unreachable": {
        "it": "Impossibile contattare OpenRouter (rete?).",
        "en": "Could not reach OpenRouter (network?).",
        "fr": "Impossible de joindre OpenRouter (réseau ?).",
        "de": "OpenRouter nicht erreichbar (Netzwerk?).",
        "es": "No se pudo contactar con OpenRouter (¿red?).",
    },
    "apikey.link": {
        "it": "Crea o gestisci una chiave su OpenRouter",
        "en": "Create or manage a key on OpenRouter",
        "fr": "Créer ou gérer une clé sur OpenRouter",
        "de": "Schlüssel auf OpenRouter erstellen oder verwalten",
        "es": "Crear o gestionar una clave en OpenRouter",
    },
    "apikey.source.file": {
        "it": "Chiave attiva: quella salvata qui.",
        "en": "Active key: the one saved here.",
        "fr": "Clé active : celle enregistrée ici.",
        "de": "Aktiver Schlüssel: der hier gespeicherte.",
        "es": "Clave activa: la guardada aquí.",
    },
    "apikey.source.env_shadowed": {
        "it": "La variabile di sistema OPENROUTER_API_KEY è impostata ma viene "
              "ignorata: vince la chiave salvata qui (svuotala per usarla).",
        "en": "The OPENROUTER_API_KEY system variable is set but ignored: the key "
              "saved here wins (clear it to use the variable).",
        "fr": "La variable système OPENROUTER_API_KEY est définie mais ignorée : "
              "la clé enregistrée ici est prioritaire (effacez-la pour l'utiliser).",
        "de": "Die Systemvariable OPENROUTER_API_KEY ist gesetzt, wird aber "
              "ignoriert: Der hier gespeicherte Schlüssel hat Vorrang (leeren, um "
              "die Variable zu nutzen).",
        "es": "La variable de sistema OPENROUTER_API_KEY está definida pero se "
              "ignora: prevalece la clave guardada aquí (vacíala para usarla).",
    },
    "apikey.source.env": {
        "it": "Chiave attiva: variabile di sistema OPENROUTER_API_KEY. Se salvi "
              "una chiave qui, avrà la precedenza.",
        "en": "Active key: OPENROUTER_API_KEY system variable. If you save a key "
              "here, it will take precedence.",
        "fr": "Clé active : variable système OPENROUTER_API_KEY. Si vous "
              "enregistrez une clé ici, elle sera prioritaire.",
        "de": "Aktiver Schlüssel: Systemvariable OPENROUTER_API_KEY. Wenn du hier "
              "einen Schlüssel speicherst, hat er Vorrang.",
        "es": "Clave activa: variable de sistema OPENROUTER_API_KEY. Si guardas "
              "una clave aquí, tendrá prioridad.",
    },
    "apikey.source.none": {
        "it": "Nessuna chiave: il motore LLM non può tradurre finché non ne "
              "inserisci una.",
        "en": "No key: the LLM engine cannot translate until you enter one.",
        "fr": "Aucune clé : le moteur LLM ne peut pas traduire tant que vous n'en "
              "saisissez pas.",
        "de": "Kein Schlüssel: Die LLM-Engine kann nicht übersetzen, bis du einen "
              "eingibst.",
        "es": "Sin clave: el motor LLM no puede traducir hasta que introduzcas una.",
    },
    "apikey.source.env_stale": {
        "it": "Variabile di sistema OPENROUTER_API_KEY trovata ma non ancora "
              "caricata da questa app: riavvia l'app (o il PC) perché venga letta, "
              "oppure inserisci qui la chiave.",
        "en": "OPENROUTER_API_KEY system variable found but not loaded by this app "
              "yet: restart the app (or the PC) so it is read, or enter the key here.",
        "fr": "Variable système OPENROUTER_API_KEY trouvée mais pas encore chargée "
              "par cette app : redémarrez l'app (ou le PC) pour qu'elle soit lue, "
              "ou saisissez la clé ici.",
        "de": "Systemvariable OPENROUTER_API_KEY gefunden, aber von dieser App "
              "noch nicht geladen: Starte die App (oder den PC) neu, damit sie "
              "gelesen wird, oder trage den Schlüssel hier ein.",
        "es": "Variable de sistema OPENROUTER_API_KEY encontrada pero aún no "
              "cargada por esta app: reinicia la app (o el PC) para leerla, o "
              "introduce la clave aquí.",
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
    # ── aspetto (tema) ──────────────────────────────────────────────────────
    "settings.group.appearance": {
        "it": "Aspetto", "en": "Appearance", "fr": "Apparence",
        "de": "Erscheinungsbild", "es": "Apariencia",
    },
    "settings.appearance.theme": {
        "it": "Tema", "en": "Theme", "fr": "Thème",
        "de": "Design", "es": "Tema",
    },
    "settings.theme.dark": {
        "it": "Scuro", "en": "Dark", "fr": "Sombre",
        "de": "Dunkel", "es": "Oscuro",
    },
    "settings.theme.light": {
        "it": "Chiaro", "en": "Light", "fr": "Clair",
        "de": "Hell", "es": "Claro",
    },
    "settings.theme.system": {
        "it": "Come il sistema", "en": "Follow system", "fr": "Comme le système",
        "de": "Wie das System", "es": "Como el sistema",
    },
    # ── notifiche di fine batch ─────────────────────────────────────────────
    "settings.group.notifications": {
        "it": "Avvisi", "en": "Notifications", "fr": "Notifications",
        "de": "Benachrichtigungen", "es": "Avisos",
    },
    "settings.notify.on_finish": {
        "it": "Avvisa quando un batch è terminato",
        "en": "Notify when a batch finishes",
        "fr": "Notifier à la fin d'un lot",
        "de": "Benachrichtigen, wenn ein Stapel fertig ist",
        "es": "Avisar cuando termina un lote",
    },
    "settings.notify.sound": {
        "it": "Suono d'avviso a fine batch",
        "en": "Alert sound at batch end",
        "fr": "Son d'alerte à la fin d'un lot",
        "de": "Signalton am Ende eines Stapels",
        "es": "Sonido de aviso al terminar un lote",
    },
    "settings.notify.prevent_sleep": {
        "it": "Impedisci lo standby durante la traduzione",
        "en": "Prevent sleep while translating",
        "fr": "Empêcher la veille pendant la traduction",
        "de": "Standby während der Übersetzung verhindern",
        "es": "Evitar la suspensión durante la traducción",
    },
    "settings.notify.test": {
        "it": "🔔 Prova suono",
        "en": "🔔 Test sound",
        "fr": "🔔 Tester le son",
        "de": "🔔 Ton testen",
        "es": "🔔 Probar sonido",
    },
    "settings.notify.player": {
        "it": "Riproduttore audio rilevato: {player}",
        "en": "Detected audio player: {player}",
        "fr": "Lecteur audio détecté : {player}",
        "de": "Erkannter Audioplayer: {player}",
        "es": "Reproductor de audio detectado: {player}",
    },
    # ── prestazioni ─────────────────────────────────────────────────────────
    "settings.group.performance": {
        "it": "Prestazioni", "en": "Performance", "fr": "Performances",
        "de": "Leistung", "es": "Rendimiento",
    },
    "settings.performance.llm_workers": {
        "it": "Richieste LLM in parallelo (per pagina)",
        "en": "Parallel LLM requests (per page)",
        "fr": "Requêtes LLM en parallèle (par page)",
        "de": "Parallele LLM-Anfragen (pro Seite)",
        "es": "Solicitudes LLM en paralelo (por página)",
    },
    "settings.performance.llm_workers.tip": {
        "it": "Quante parti di testo tradurre insieme nel motore LLM. Valori alti sono più veloci ma possono toccare i limiti del servizio.",
        "en": "How many text chunks to translate at once in the LLM engine. Higher is faster but may hit service rate limits.",
        "fr": "Combien de portions de texte traduire en même temps avec le moteur LLM. Plus élevé = plus rapide, mais peut atteindre les limites du service.",
        "de": "Wie viele Textabschnitte die LLM-Engine gleichzeitig übersetzt. Höher ist schneller, kann aber die Dienstlimits erreichen.",
        "es": "Cuántos fragmentos de texto traducir a la vez con el motor LLM. Más alto es más rápido, pero puede alcanzar los límites del servicio.",
    },
    "settings.performance.fast_engine": {
        "it": "Motore veloce (sperimentale)",
        "en": "Fast engine (experimental)",
        "fr": "Moteur rapide (expérimental)",
        "de": "Schnelle Engine (experimentell)",
        "es": "Motor rápido (experimental)",
    },
    "settings.performance.fast_engine.tip": {
        "it": "Avvia il motore tramite un wrapper che applica patch di velocità (cache dei font, niente monitor memoria). Non cambia il risultato. Disattivalo per tornare al comportamento standard.",
        "en": "Runs the engine through a wrapper that applies speed patches (font cache, no memory monitor). The result is unchanged. Turn it off to return to the standard behaviour.",
        "fr": "Lance le moteur via un wrapper qui applique des correctifs de vitesse (cache des polices, sans moniteur mémoire). Le résultat est inchangé. Désactivez-le pour revenir au comportement standard.",
        "de": "Startet die Engine über einen Wrapper mit Geschwindigkeits-Patches (Font-Cache, kein Speicher-Monitor). Das Ergebnis bleibt gleich. Deaktivieren für das Standardverhalten.",
        "es": "Ejecuta el motor mediante un wrapper que aplica mejoras de velocidad (caché de fuentes, sin monitor de memoria). El resultado no cambia. Desactívalo para volver al comportamiento estándar.",
    },
    "settings.performance.fast_flags": {
        "it": "Traduzione rapida: salta controlli avanzati (sperimentale)",
        "en": "Fast translation: skip advanced checks (experimental)",
        "fr": "Traduction rapide : ignorer les contrôles avancés (expérimental)",
        "de": "Schnelle Übersetzung: erweiterte Prüfungen überspringen (experimentell)",
        "es": "Traducción rápida: omitir comprobaciones avanzadas (experimental)",
    },
    "settings.performance.fast_flags.tip": {
        "it": "Aggiunge flag che saltano controlli geometrici/formule quando la pagina ha testo. Più veloce, ma la resa di formule e layout può risultare meno precisa. Su pagine scansionate non ha effetto.",
        "en": "Adds flags that skip geometry/formula checks when the page has text. Faster, but formula and layout fidelity may be lower. Has no effect on scanned pages.",
        "fr": "Ajoute des options qui ignorent les contrôles de géométrie/formules lorsque la page contient du texte. Plus rapide, mais la fidélité des formules et de la mise en page peut baisser. Sans effet sur les pages scannées.",
        "de": "Fügt Optionen hinzu, die Geometrie-/Formelprüfungen bei Textseiten überspringen. Schneller, aber Formel- und Layouttreue kann geringer sein. Ohne Wirkung bei gescannten Seiten.",
        "es": "Añade opciones que omiten comprobaciones de geometría/fórmulas cuando la página tiene texto. Más rápido, pero la fidelidad de fórmulas y diseño puede ser menor. Sin efecto en páginas escaneadas.",
    },
    "settings.performance.fast_worker": {
        "it": "Worker persistente (sperimentale)",
        "en": "Persistent worker (experimental)",
        "fr": "Worker persistant (expérimental)",
        "de": "Persistenter Worker (experimentell)",
        "es": "Worker persistente (experimental)",
    },
    "settings.performance.fast_worker.tip": {
        "it": "Tiene un processo del motore caldo tra una pagina e l'altra: elimina gli import e i warmup ripetuti (più veloce dalla seconda pagina). Richiede il motore veloce. Se non disponibile, si torna automaticamente al metodo normale.",
        "en": "Keeps an engine process warm between pages: removes repeated imports and warmups (faster from the second page onward). Requires the fast engine. Falls back automatically if unavailable.",
        "fr": "Garde un processus moteur actif entre les pages : supprime les imports et warmups répétés (plus rapide dès la deuxième page). Nécessite le moteur rapide. Retour automatique si indisponible.",
        "de": "Hält einen Engine-Prozess zwischen Seiten warm: entfernt wiederholte Importe und Warmups (schneller ab der zweiten Seite). Erfordert die schnelle Engine. Fällt automatisch zurück.",
        "es": "Mantiene un proceso del motor caliente entre páginas: elimina importaciones y calentamientos repetidos (más rápido desde la segunda página). Requiere el motor rápido. Vuelve solo si no está disponible.",
    },
    "settings.performance.reasoning_effort": {
        "it": "LLM: reasoning effort",
        "en": "LLM: reasoning effort",
        "fr": "LLM : effort de raisonnement",
        "de": "LLM: Reasoning-Aufwand",
        "es": "LLM: esfuerzo de razonamiento",
    },
    "settings.performance.reasoning_effort.tip": {
        "it": "Per i modelli ragionativi (es. gpt-oss): valori bassi come 'minimal' riducono i token di ragionamento e la latenza. Richiede il motore veloce attivo.",
        "en": "For reasoning models (e.g. gpt-oss): low values like 'minimal' cut reasoning tokens and latency. Requires the fast engine.",
        "fr": "Pour les modèles de raisonnement (ex. gpt-oss) : des valeurs basses comme « minimal » réduisent les tokens de raisonnement et la latence. Nécessite le moteur rapide.",
        "de": "Für Reasoning-Modelle (z. B. gpt-oss): niedrige Werte wie 'minimal' reduzieren Reasoning-Tokens und Latenz. Erfordert die schnelle Engine.",
        "es": "Para modelos de razonamiento (p. ej. gpt-oss): valores bajos como 'minimal' reducen los tokens de razonamiento y la latencia. Requiere el motor rápido.",
    },
    "settings.performance.json_mode": {
        "it": "LLM: JSON mode",
        "en": "LLM: JSON mode",
        "fr": "LLM : mode JSON",
        "de": "LLM: JSON-Modus",
        "es": "LLM: modo JSON",
    },
    "settings.performance.json_mode.tip": {
        "it": "Chiede al provider risposte JSON strutturate (se supportato). Può ridurre errori di parsing. Richiede il motore veloce attivo.",
        "en": "Asks the provider for structured JSON responses (if supported). May reduce parsing errors. Requires the fast engine.",
        "fr": "Demande au fournisseur des réponses JSON structurées (si pris en charge). Peut réduire les erreurs d'analyse. Nécessite le moteur rapide.",
        "de": "Fordert strukturierte JSON-Antworten vom Anbieter an (falls unterstützt). Kann Parsing-Fehler reduzieren. Erfordert die schnelle Engine.",
        "es": "Solicita al proveedor respuestas JSON estructuradas (si se admite). Puede reducir errores de análisis. Requiere el motor rápido.",
    },
    "settings.llm.model": {
        "it": "LLM: modello",
        "en": "LLM: model",
        "fr": "LLM : modèle",
        "de": "LLM: Modell",
        "es": "LLM: modelo",
    },
    "settings.llm.model.tip": {
        "it": "Modello del motore LLM (es. inception/mercury-2.5, openai/gpt-oss-120b). Vuoto = default (o variabile PDF_LLM_MODEL).",
        "en": "LLM engine model (e.g. inception/mercury-2.5, openai/gpt-oss-120b). Empty = default (or PDF_LLM_MODEL env).",
        "fr": "Modèle du moteur LLM (ex. inception/mercury-2.5, openai/gpt-oss-120b). Vide = défaut (ou variable PDF_LLM_MODEL).",
        "de": "Modell der LLM-Engine (z. B. inception/mercury-2.5, openai/gpt-oss-120b). Leer = Standard (oder PDF_LLM_MODEL).",
        "es": "Modelo del motor LLM (p. ej. inception/mercury-2.5, openai/gpt-oss-120b). Vacío = predeterminado (o PDF_LLM_MODEL).",
    },
    "settings.llm.base_url": {
        "it": "LLM: base URL",
        "en": "LLM: base URL",
        "fr": "LLM : URL de base",
        "de": "LLM: Basis-URL",
        "es": "LLM: URL base",
    },
    "settings.llm.base_url.tip": {
        "it": "Endpoint OpenAI-compatibile. Default OpenRouter; es. http://127.0.0.1:8790/v1 per il proxy provider (pin Groq). Vuoto = default (o variabile PDF_LLM_BASE_URL).",
        "en": "OpenAI-compatible endpoint. Default OpenRouter; e.g. http://127.0.0.1:8790/v1 for the provider proxy (Groq pin). Empty = default (or PDF_LLM_BASE_URL env).",
        "fr": "Endpoint compatible OpenAI. Par défaut OpenRouter ; ex. http://127.0.0.1:8790/v1 pour le proxy de fournisseur (pin Groq). Vide = défaut (ou PDF_LLM_BASE_URL).",
        "de": "OpenAI-kompatibler Endpunkt. Standard OpenRouter; z. B. http://127.0.0.1:8790/v1 für den Provider-Proxy (Groq-Pin). Leer = Standard (oder PDF_LLM_BASE_URL).",
        "es": "Endpoint compatible con OpenAI. Por defecto OpenRouter; p. ej. http://127.0.0.1:8790/v1 para el proxy de proveedor (pin Groq). Vacío = predeterminado (o PDF_LLM_BASE_URL).",
    },
    "settings.proxy.autostart": {
        "it": "Avvia automaticamente il proxy provider",
        "en": "Start the provider proxy automatically",
        "fr": "Démarrer automatiquement le proxy de fournisseur",
        "de": "Provider-Proxy automatisch starten",
        "es": "Iniciar automáticamente el proxy de proveedor",
    },
    "settings.proxy.autostart.tip": {
        "it": "Avvia in background il proxy locale (pin del provider, es. Groq) e usa automaticamente il suo indirizzo come base URL LLM. Richiede il motore veloce.",
        "en": "Starts the local proxy in the background (provider pin, e.g. Groq) and uses its address as the LLM base URL. Requires the fast engine.",
        "fr": "Démarre le proxy local en arrière-plan (pin du fournisseur, ex. Groq) et utilise son adresse comme URL de base LLM. Nécessite le moteur rapide.",
        "de": "Startet den lokalen Proxy im Hintergrund (Provider-Pin, z. B. Groq) und nutzt dessen Adresse als LLM-Basis-URL. Erfordert die schnelle Engine.",
        "es": "Inicia el proxy local en segundo plano (pin del proveedor, p. ej. Groq) y usa su dirección como URL base del LLM. Requiere el motor rápido.",
    },
    "settings.proxy.port": {
        "it": "Porta del proxy",
        "en": "Proxy port",
        "fr": "Port du proxy",
        "de": "Proxy-Port",
        "es": "Puerto del proxy",
    },
    "settings.proxy.port.tip": {
        "it": "Porta locale del proxy provider (default 8790).",
        "en": "Local port of the provider proxy (default 8790).",
        "fr": "Port local du proxy de fournisseur (défaut 8790).",
        "de": "Lokaler Port des Provider-Proxys (Standard 8790).",
        "es": "Puerto local del proxy de proveedor (predeterminado 8790).",
    },
    "settings.proxy.test": {
        "it": "Prova provider",
        "en": "Test provider",
        "fr": "Tester le fournisseur",
        "de": "Provider testen",
        "es": "Probar proveedor",
    },
    "settings.proxy.test.tip": {
        "it": "Invia una piccola richiesta e mostra quale provider risponde (es. Groq). Utile per verificare il pin.",
        "en": "Sends a tiny request and shows which provider answers (e.g. Groq). Useful to verify the pin.",
        "fr": "Envoie une petite requête et indique quel fournisseur répond (ex. Groq). Utile pour vérifier le pin.",
        "de": "Sendet eine kleine Anfrage und zeigt, welcher Provider antwortet (z. B. Groq). Nützlich zur Pin-Prüfung.",
        "es": "Envía una petición pequeña y muestra qué proveedor responde (p. ej. Groq). Útil para verificar el pin.",
    },
    "settings.proxy.test.running": {
        "it": "Verifica in corso…",
        "en": "Testing…",
        "fr": "Vérification…",
        "de": "Prüfung läuft…",
        "es": "Comprobando…",
    },
    "settings.proxy.test.ok": {
        "it": "Provider: {provider} — modello: {model}",
        "en": "Provider: {provider} — model: {model}",
        "fr": "Fournisseur : {provider} — modèle : {model}",
        "de": "Provider: {provider} — Modell: {model}",
        "es": "Proveedor: {provider} — modelo: {model}",
    },
    "settings.proxy.test.error": {
        "it": "Errore: {error}",
        "en": "Error: {error}",
        "fr": "Erreur : {error}",
        "de": "Fehler: {error}",
        "es": "Error: {error}",
    },
    # ── notifiche (contenuti) ───────────────────────────────────────────────
    "notify.batch.title": {
        "it": "Traduzione completata", "en": "Translation complete",
        "fr": "Traduction terminée", "de": "Übersetzung abgeschlossen",
        "es": "Traducción completada",
    },
    "notify.batch.done": {
        "it": "Batch terminato: {count} pagine tradotte.",
        "en": "Batch finished: {count} pages translated.",
        "fr": "Lot terminé : {count} pages traduites.",
        "de": "Stapel fertig: {count} Seiten übersetzt.",
        "es": "Lote terminado: {count} páginas traducidas.",
    },
    "notify.page.done": {
        "it": "Pagina {page} tradotta.",
        "en": "Page {page} translated.",
        "fr": "Page {page} traduite.",
        "de": "Seite {page} übersetzt.",
        "es": "Página {page} traducida.",
    },
    "notify.batch.partial": {
        "it": "Batch terminato: {count} tradotte, {failed} non riuscite.",
        "en": "Batch finished: {count} translated, {failed} failed.",
        "fr": "Lot terminé : {count} traduites, {failed} échouées.",
        "de": "Stapel fertig: {count} übersetzt, {failed} fehlgeschlagen.",
        "es": "Lote terminado: {count} traducidas, {failed} fallidas.",
    },
    "notify.batch.cancelled": {
        "it": "Batch annullato dall'utente.",
        "en": "Batch cancelled by the user.",
        "fr": "Lot annulé par l'utilisateur.",
        "de": "Stapel vom Benutzer abgebrochen.",
        "es": "Lote cancelado por el usuario.",
    },
    "notify.batch.error": {
        "it": "Batch terminato con errore.",
        "en": "Batch finished with an error.",
        "fr": "Lot terminé avec une erreur.",
        "de": "Stapel mit Fehler beendet.",
        "es": "Lote terminado con error.",
    },
    "power.resumed": {
        "it": "Il computer si è risvegliato: la traduzione riprende dalla pagina interrotta.",
        "en": "The computer woke up: translation resumes from the interrupted page.",
        "fr": "L'ordinateur s'est réveillé : la traduction reprend à la page interrompue.",
        "de": "Der Computer ist aufgewacht: Die Übersetzung wird ab der unterbrochenen Seite fortgesetzt.",
        "es": "El equipo se ha reanudado: la traducción continúa desde la página interrumpida.",
    },
    "tray.tip": {
        "it": "Noesis PDF Cloner", "en": "Noesis PDF Cloner",
        "fr": "Noesis PDF Cloner", "de": "Noesis PDF Cloner",
        "es": "Noesis PDF Cloner",
    },
    "tray.show": {
        "it": "Mostra finestra", "en": "Show window", "fr": "Afficher la fenêtre",
        "de": "Fenster anzeigen", "es": "Mostrar ventana",
    },
    "tray.quit": {
        "it": "Esci", "en": "Quit", "fr": "Quitter",
        "de": "Beenden", "es": "Salir",
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

    # Merge generico forward-compatible: per ogni chiave nota nei default,
    # conserva il valore salvato (con coercizione di tipo). Prima molte chiavi
    # (engine, theme, notify_*, llm_pool_workers, stringhe LLM…) venivano
    # ignorate e tornavano al default a ogni riavvio.
    for key, default in defaults.items():
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(default, bool):
            out[key] = _to_bool(value, default)
        elif isinstance(default, int):
            try:
                out[key] = int(value)
            except (TypeError, ValueError):
                pass
        elif isinstance(default, float):
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                pass
        elif isinstance(default, str):
            if isinstance(value, str):
                out[key] = value
        elif isinstance(default, dict):
            if isinstance(value, dict):
                out[key] = value

    out["lang"] = raw["lang"] if raw.get("lang") in LANGUAGES else defaults["lang"]
    out["src_lang"] = (
        raw["src_lang"]
        if raw.get("src_lang") in TRANSLATION_LANGUAGES
        else defaults["src_lang"]
    )
    dst = raw.get("dst_lang")
    out["dst_lang"] = (
        dst if (dst in TRANSLATION_LANGUAGES and dst != "auto") else defaults["dst_lang"]
    )
    out["engine"] = (
        raw["engine"]
        if raw.get("engine") in TRANSLATION_ENGINES
        else defaults["engine"]
    )
    theme = str(raw.get("theme", "")).strip().lower()
    out["theme"] = theme if theme in ("dark", "light", "system") else defaults["theme"]
    effort = str(raw.get("llm_reasoning_effort", "")).strip().lower()
    out["llm_reasoning_effort"] = (
        effort if effort in ("", "minimal", "low", "medium", "high")
        else defaults["llm_reasoning_effort"]
    )
    try:
        out["zoom"] = min(4.0, max(0.5, float(raw.get("zoom", out["zoom"]))))
    except (TypeError, ValueError):
        pass
    try:
        out["font_size"] = min(16, max(10, int(raw.get("font_size", out["font_size"]))))
    except (TypeError, ValueError):
        pass
    try:
        out["llm_pool_workers"] = min(
            16, max(1, int(raw.get("llm_pool_workers", out["llm_pool_workers"])))
        )
    except (TypeError, ValueError):
        pass
    try:
        out["llm_proxy_port"] = min(
            65535, max(1024, int(raw.get("llm_proxy_port", out["llm_proxy_port"])))
        )
    except (TypeError, ValueError):
        pass
    for key in ("render_md", "show_header", "remember_tab", "resume_last_page", "save_edits", "clone_bar_collapsed", "fast_engine", "fast_flags", "fast_worker", "llm_json_mode", "notify_on_finish", "notify_sound", "prevent_sleep", "llm_proxy_autostart"):
        out[key] = _to_bool(raw.get(key, out[key]), out[key])
    out["last_tab"] = (
        raw["last_tab"]
        if raw.get("last_tab") in ("original", "translated", "images")
        else defaults["last_tab"]
    )
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
    elif key == "llm_pool_workers":
        try:
            _CONFIG[key] = min(16, max(1, int(value)))
            return
        except (TypeError, ValueError):
            pass
    elif key == "theme":
        value = str(value).strip().lower()
        _CONFIG[key] = value if value in ("dark", "light", "system") else "dark"
        return
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
