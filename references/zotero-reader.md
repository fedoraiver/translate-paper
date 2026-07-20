# Zotero reader compatibility

Read this file only when the user explicitly asks to diagnose Zotero's built-in
PDF reader. The default configured workflow opens Zotero PDFs in Google Chrome,
so skip this reference during normal translation and ingestion.

## Distinguish the two link layers

1. Inspect `pdf_native_links` in the validation report or call
   `page.get_links()` with PyMuPDF.
2. If the count is nonzero, the PDF contains intrinsic link annotations. Fix
   unintended annotations in the PDF and validate again.
3. If the count is zero but Zotero still highlights citations or shows link
   previews, classify them as Zotero Smart Reference overlays. Zotero creates
   these at reader runtime from the searchable text layer; they are not PDF
   link objects and cannot be validated from a rendered page or static PDF
   structure.
4. Compare the same stored attachment in an external reader when the source is
   still uncertain.

Never claim that direct text embedding, zero native links, successful page
rendering, or Zotero re-ingestion proves that Smart Reference overlays are
correct. Those checks prove only the PDF's own geometry and storage integrity.

## Preservation rules

- Preserve citation text and reference markers exactly as required by the
  translation contract.
- Keep the translated text searchable, selectable, and accessible.
- Never rasterize the translated body, split author names, insert invisible
  characters, or otherwise corrupt citations merely to defeat Zotero's
  detector.
- Do not delete and rebuild a Zotero parent again solely because a
  viewer-generated overlay is misplaced; re-ingestion does not change the
  reader heuristic.
- Do not patch Zotero application archives or inject CSS. Updates overwrite
  such changes, and hiding an overlay does not reliably disable its click
  target.

## Current Zotero behavior and workarounds

Zotero 9.0.6 has no user-facing or Config Editor preference that disables
Smart Reference overlays independently. Recheck the installed version,
settings, and current Zotero documentation before applying this statement to a
later version.

Offer these workarounds without changing the user's settings unless requested:

- Set **Settings -> General -> Open PDFs using** to Google Chrome, the system
  default, or another external reader. This bypasses Zotero's runtime overlays
  but also bypasses its integrated annotation UI.
- Keep Zotero's reader as the default and open an individual attachment
  externally through **Show File**.
- Hold **Alt** while selecting text beneath a detected link and press **Esc**
  to dismiss a popup.

Only for an explicitly requested built-in-reader diagnosis, report
`pdf_native_links`, `searchable_text_layer`, and `citation_text_candidates`.
Do not add this diagnostic to normal translation handoffs.
