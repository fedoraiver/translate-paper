# Translation contract

## Translation engine

The active Codex model must perform every translation decision and write every
Chinese sentence. Do not use translation APIs, web or browser translation,
Google Translate, Microsoft Translator, DeepL, local translation models,
machine-translation packages, or delegated agents.

OCR, PDF parsing, layout analysis, font shaping, export, and validation are
allowed only as mechanical document-processing steps. OCR output is source
text, not a translation.

## Content boundary

Translate:

- the Introduction heading;
- section and subsection headings from Introduction through Conclusion;
- narrative body paragraphs in that range;
- when no Conclusion exists, the verified main body through its last section.

Keep verbatim:

- title, subtitle, authors, affiliations, addresses, dates;
- abstract, classification codes, keywords;
- figures, tables, algorithms, listings, and their captions;
- display equations and mathematical notation;
- citation markers and bibliography keys;
- acknowledgements, funding, conflicts, author contributions;
- appendices, supplements, endnotes, and references.

Never guess a boundary. A reviewed `boundary-review.json` must contain the
source hash, exact start and stop-before IDs, a reason, and explicit permission
when the paper has no Conclusion. Rerunning preparation applies it
deterministically.

## Fidelity

- Preserve every number, sign, unit, identifier, URL, DOI, arXiv ID, citation,
  variable, code fragment, and protected token.
- Preserve each `[[Pdddd]]` and typed `[[F...]]` fragment exactly once.
  Preserve paired bold/italic markers exactly once, in order, around the
  translated equivalent. Missing, duplicate, reversed, or unresolved markers
  are errors.
- Treat canonical assumption, protocol, and problem names as contextual formal
  names and normally keep them in English. Translate generic concepts such as
  completeness, soundness, witness, and commitment into Chinese. Record
  decisions for within-paper consistency, but do not use a global mechanical
  terminology table.
- Preserve proper nouns unless a conventional Chinese name is unambiguous.
- Use the glossary consistently and retain standard acronyms on first use.
- Do not add explanations, criticism, citations, or outside facts.
- Do not omit repeated text, body footnotes, qualifications, or negative
  results.

## Persistent files

`prepare_paper.py` creates:

- `manifest.json`: identity, geometry, extraction methods, boundaries, paths;
- `boundary-review.json`: hash-bound manual review gate and candidates;
- `translation-units.jsonl`: ordered source elements and protected text;
- `translations.template.jsonl`: empty translation checkpoints;
- `math-review.json`: hash-bound native TeX and review status for ambiguous
  inline fragments and every logical display equation;
- `visual-layout.json`: source-hash-bound figure/table inventory, complete
  source visual bounds, caption association, source and target typography,
  target dimensions, aspect ratio, scale basis, and automatic fallback state;
- `glossary.json`: persistent terminology map;
- source previews and layout-aware extracted text.

Create `translations.jsonl` from the template. Fill only `translated_text` and
save after every section. Export `中文翻译正文.md` from the completed JSONL so the
translation remains auditable independently of PDF layout.

## Visual preservation

Review every page before translation when it is scanned, OCR confidence is low,
boundaries are uncertain, columns mix with full-width content, or equations and
captions are misclassified.

Use `source_clip` for figures, complex tables, captions, and damaged
non-mathematical OCR regions that must remain visually identical. Treat a
multi-fragment vector figure, its drawing primitives, and its internal labels
as one visual envelope, with its caption associated but not absorbed into an
adjacent formula. Every numbered source figure/table must have one unique
inventory entry and exactly one rendered placement. A missing, duplicate,
cropped, distorted, or split visual is a blocking error.

Calibrate visual size from the type inside the object:

`target internal font = source internal font / source body font × 11.5pt`.

Apply the resulting scale to both dimensions, center the vector object, and
shrink it only when it exceeds the content box. Never expand every visual to
`\linewidth`. If internal font size cannot be measured, preserve the source
visual/content-width ratio in the target content area, record
`automatic_fallback: true`, and emit a warning. This fallback does not permit a
missing or duplicate object.

After rendering, perform two visual reviews: first inspect the full contact
sheet for global pagination, then inspect at high resolution every page with a
figure, table, footnote, equation, mixed columns, or multiple source clips.
Compare the source and output inventories during the second pass.

Retain source span typography in translatable prose. Render source bold and
italic emphasis on the translated equivalent. Detect numeric bracket citations
before generic protected values and render body citations as superscripts;
exclude bibliography entries and mathematical intervals. Reconstruct inline
mathematics from recorded font, size, baseline, and spatially adjacent runs,
including runs split across overlapping PDF text blocks. Merge display-equation
spans into logical formulas. Render all mathematics as embedded LuaLaTeX text
with Latin Modern Math, retaining real super/subscripts. SVG, raster, HTML
image, source equation clip, and PDF image/Form fallback are forbidden for
mathematical content. If reconstruction is not lossless, leave the review
unresolved and abort export.

Classify mathematical expressions before interpreting PDF bold flags. A
symbol-heavy or math-font run must enter the math fragment/review path and must
not create prose bold markers. Ordinary body text may be bold only when its
source span is genuinely bold; genuine bold and italic emphasis must remain.
Expand fragment boundaries to include adjacent function names and balanced
delimiters, so `O(\sqrt{p})` is one atom. Literal Unicode `√` is forbidden in
native TeX; use a complete `\sqrt{...}` command.

Treat a long sentence beginning with “Table N gives/shows/compares...” as
narrative body, not a caption. A caption must be a compact label attached to its
visual.

Compose the document as `original front -> translated body -> original tail`.
Copy full untranslated source pages as vector PDF pages. If the Introduction or
post-Conclusion boundary occurs mid-page, crop the untranslated portion from
the source PDF without rasterization and place it on a separate output page.
Always begin the translated body on a new page. Do not reconstruct front matter,
acknowledgements, appendices, or references from extracted text.

## Default Chinese typography

Use the general `zh-academic-v1` profile for translated PDFs:

- A4 pages with 53pt left/right, 48.5pt top, and 55.5pt bottom margins;
- SimSun 11.5pt narrative text, justified with 16.9pt absolute line pitch
  and natural 0pt letter spacing;
- 23pt first-line indent and 4pt paragraph spacing;
- SimHei 13.8pt level-one headings and SimHei 11.5pt lower-level headings;
- ideographic spacing after section numbers;
- hanging indentation for lists;
- no new paragraph or repeated indent for a source-page continuation.
- no new paragraph for a same-page extraction split when the preceding block
  ends mid-sentence; preserve an explicit line break before a new bold
  definition label and represent that label as a semantic paragraph start with
  the normal 23pt indent, not a bare HTML line break;
- use Latin Modern Roman regular, bold, and italic for non-mathematical Latin
  text while retaining SimSun and SimHei for Chinese;
- translated text drawn directly into the final page box so its PDF text-layer
  coordinates match its visible location in the PDF.

Place body footnotes at the bottom of the translated page that contains their
anchor, below a visible horizontal rule and in smaller SimSun text. If reserving
that area would collide with body text, move the complete footnote to a ruled
end-of-body footnote area. Scale figures and tables proportionally to fit the
content box; never stretch or crop them. Use a width-constrained TeX box for
long equations so mathematical glyphs remain embedded text.

Treat extraction blocks as layout units, not automatically as paragraphs.
Merge only consecutive translated body units in the same section when the
previous source page ends mid-sentence and no heading, list, figure, table, or
other source clip intervenes. Record merged IDs and typography metadata in the
layout report so validation can distinguish real paragraphs from continuations.
Use tall temporary pages only for measurement. Never copy those pages into the
final PDF as Form XObjects; reject 4096pt measurement forms and unexpected link
annotations during validation.

## Reader scope

Validate the PDF's own text layer, geometry, and intrinsic links. The configured
Zotero workflow opens PDFs in Google Chrome, so Zotero built-in-reader overlays
are outside the default translation and acceptance scope. Do not mention or
optimize them unless the user explicitly asks to use the built-in reader again.
In that case, follow [zotero-reader.md](zotero-reader.md).
