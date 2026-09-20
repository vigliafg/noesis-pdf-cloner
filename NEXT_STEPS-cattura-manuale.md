# Nota — routine di cattura manuale delle immagini (🖱️ Seleziona zona)

Redatto il 2026-08-18. Documento di lavoro per il **miglioramento** della
cattura manuale delle immagini. L'autocattura (figure embedded) è stata
**rimossa**: resta solo la selezione a mouse di una zona.

---

## 1. Stato attuale — dove sta il codice

Tutto in `main.py` (versione lite, un solo engine PyMuPDF).

| Componente | Dove | Cosa fa |
|---|---|---|
| `PdfPageView` (QGraphicsView) | `main.py` | Rubber-band a mouse. Emette `region_selected(x0,y0,x1,y1)` in **coordinate scena** (pixel full-res del pixmap) al `mouseReleaseEvent`. Soglia minima 4px. |
| `MainWindow._on_region_selected` | `main.py` | Spegne la modalità, converte scena→punti PDF dividendo per `self._render_scale`, chiama `_extract_image_region`. |
| `MainWindow._extract_image_region` | `main.py` | Salva il risultato in `page_XXXX_region_0.{ext}` (sovrascrive), ritorna un `file://` URI. |
| `_region_image(doc, page, clip, zoom)` | `main.py` (helper puro) | 1) immagine embedded **interamente dentro** la selezione → `_image_as_png`; 2) altrimenti render della zona `get_pixmap(clip, matrix)`. |
| `_image_as_png(doc, xref)` | `main.py` (helper puro) | `Pixmap(doc, xref)` → converte CMYK→RGB → `tobytes("png")`. Fallback ai byte raw di `extract_image`. |
| `TranslatablePanel` gallery | `main.py` | `show_images` / `_rebuild_images_panel` / `_make_image_card` (anteprima + 💾 Salva / 📋 Copia). |

Coordinate: il pixmap è renderizzato a `Matrix(self._render_scale, ...)` e la
scena è `QRectF(pixmap.rect())`, quindi **scena ÷ `_render_scale` = punti PDF**.

## 2. Bug già risolti — NON regredire

1. **JPEG2000 (`.jpx`) non visualizzabile** — i byte raw `.jpx` non sono
   decodificabili da Qt (`QPixmap` → null). Risolto normalizzando a PNG in
   `_image_as_png`.
2. **JPEG2000 in CMYK** — `Pixmap(...).tobytes("png")` fallisce con
   `ValueError("unsupported colorspace for 'png'")` (il PNG non supporta
   CMYK). Risolto convertendo CMYK→RGB (`pix.n == 4 and not pix.alpha`).
   Caso reale: `andrew2020.pdf` pag. 20 (xref CMYK jpx).
3. Test di regressione in `tests/test_images.py`: sintetici (JPEG RGB, CMYK)
   + reali (`porth2014.pdf` pag. 44, `andrew2020.pdf` pag. 20). Gated con
   `@unittest.skipUnless(os.path.exists(...))`.

Comando test: `.venv/bin/python -m unittest discover -s tests -v`

## 3. Limiti noti (da migliorare)

1. **File unico per pagina**: il file è sempre `page_XXXX_region_0.*` e
   `_current_images = [uri]` sostituisce la cattura precedente. Una sola
   zona per pagina, niente gallery di più catture.
2. **Embedded non ritagliato sull'intersezione**: se la selezione copre
   un'immagine embedded *intera*, torna l'immagine embedded originale
   (buono, massima risoluzione). Ma se la selezione la interseca solo in
   parte, si va al render (perde la risoluzione nativa). Mancanza: crop
   dell'embedded sull'intersezione esatta.
3. **Mappatura coordinate fragile**: si divide per `self._render_scale`. È
   corretto finché il pixmap mostrato è renderizzato esattamente a quella
   scala, ma è un accoppiamento implicito (se un giorno si aggiunge un fit
   o una cache diversa, si rompe senza errori visibili).
4. **Risoluzione del render**: il fallback usa `max(self._render_scale, 4.0)`.
   Su figure piccole può essere poco nitida.
5. **Toggle "one-shot"**: dopo ogni cattura la modalità selezione si spegne;
   per un'altra zona va ripremuto il bottone. Nessun Esc per annullare, nessun
   feedback del rettangolo oltre al rubber-band.
6. **Colore CMYK→RGB**: perde l'eventuale profilo ICC (sfumature leggermente
   diverse). Accettabile ora; da valutare se serve fedeltà colore.

## 4. Proposte di miglioramento (ordine suggerito)

### P1 — Crop dell'embedded sull'intersezione esatta
Quando la selezione interseca un'immagine embedded ma non la copre del tutto,
ritagliare l'immagine nativa sull'intersezione invece di fare il render.
Approccio senza dipendenze nuove: `Pixmap(doc, xref)` → `pix.set_origin`/
crop del sub-rect (`pymupdf.Pixmap` supporta il crop via `IRect` e
`pix.copy()`) oppure `get_pixmap` sull'intersezione. (Il repo padre suggeriva
PIL per il crop; PyMuPDF da solo è sufficiente e già disponibile.)

### P1 — Più catture per pagina
File con indice/timestamp (`page_XXXX_region_1.png`, `_2`, …) e accumulo nella
gallery (`_current_images.append` + dedup per URI). Serve ripensare il reset su
cambio pagina (oggi `_extract_text` azzera `_current_images`).

### P2 — Mappatura coordinate robusta
Eliminare l'accoppiamento con `_render_scale`: derivare la scala dalla
`transform()` della view o dal `_pix_item` (es. `self._pix_item.mapToScene` /
confronto `pixmap.width` vs `page.rect.width`). Oppure far emettere al
`PdfPageView` direttamente coordinate in **punti PDF** (il view conosce
pixmap e scala), così `MainWindow` non deve dividerle.

### P2 — Render a risoluzione più alta
Fallback a zoom più alto (es. 6x) o proporzionale alla dimensione della zona
rispetto alla pagina, per catture nitide.

### P3 — UX della selezione
- `Esc` per annullare il rubber-band.
- Anteprima del rettangolo con dimensioni (punti PDF / pixel).
- Opzione "resta in modalità selezione" dopo la cattura.
- Valutare il doppio nesting `QScrollArea` → `QGraphicsView` (oggi la view ha
  anche le sue scrollbar interne): semplificare o rimuovere l'outer scroll area.

## 5. Come verificare dopo le modifiche

1. `.venv/bin/python -m unittest discover -s tests -v` (deve restare verde).
2. Prova manuale GUI su almeno: `harrison2025.pdf`, `porth2014.pdf` pag. 44,
   `andrew2020.pdf` pag. 20 (jpx CMYK). Verifica che la cattura mostri
   l'immagine, che 💾 Salva scriva un **PNG** valido e che non compaiano
   warning `Pixmap is a null pixmap` nel terminale.
3. Verifica i casi: figura raster, figura vettoriale, figura composita
   (più XObject), zona che interseca solo in parte un'immagine embedded.
