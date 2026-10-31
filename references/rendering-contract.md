# Source and rendering contract

Read during source/math review and PDF production. Translation fidelity and
content boundaries are defined in [translation-contract.md](translation-contract.md).

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

Standalone source code and pseudocode use `kind: "code"`, `source_clip`, and
`translatable: false`, preserving exact source text and reviewed bounds. Keep
them as independent vector clips without captions; do not classify their
operators or variables as native mathematics. Markdown retains the source as
a fenced code block, including its line breaks and indentation.

Calibrate visual size from the type inside the object:

`target internal font = source internal font / source body font × 11.5pt`.

Apply the resulting scale to both dimensions, center the vector object, and
shrink it only when it exceeds the content box. Never expand every visual to
`\linewidth`. If internal font size cannot be measured, preserve the source
visual/content-width ratio in the target content area, record
`automatic_fallback: true`, and emit a warning. This fallback does not permit a
missing or duplicate object.

After rendering, require an independent visual-review agent to inspect the full
contact sheet and every output page image at readable resolution, comparing
complex content to source pages. Follow [visual-review.md](visual-review.md) for
the PDF-hash-bound report and repair/review loop. Strict validation alone does
not satisfy this gate; any PDF change invalidates the prior visual acceptance.

Retain source span typography in translatable prose. Render source bold and
italic emphasis on the translated equivalent. Detect numeric bracket citations
before generic protected values and render body citations as superscripts;
exclude bibliography entries and mathematical intervals. Reconstruct inline
mathematics from recorded font, size, baseline, and spatially adjacent runs,
including runs split across overlapping PDF text blocks. Merge display-equation
spans into logical formulas. Render inline and standalone mathematics as embedded
LuaLaTeX text with Latin Modern Math, retaining real super/subscripts. SVG,
raster, HTML image, source equation clip, and PDF image/Form fallback are
forbidden for those equations. If reconstruction is not lossless, leave the review
unresolved and abort export.

A numbered figure may itself contain a syntax or inference-rule diagram. Keep
that complete figure as the reviewed vector visual, including labels and caption;
do not extract its internal formulas a second time. This figure-preservation rule
does not permit converting standalone equations to figures to bypass math review.

Treat a formula-only translatable body unit as an extraction error. Reclassify
it as native display math or merge it with its actual surrounding prose; never
add filler Chinese merely to pass the Chinese-text check.

Classify mathematical expressions before interpreting PDF bold flags. A
symbol-heavy or math-font run must enter the math fragment/review path and must
not create prose bold markers. Ordinary body text may be bold only when its
source span is genuinely bold; genuine bold and italic emphasis must remain.
Expand fragment boundaries to include adjacent function names and balanced
delimiters, so `O(\sqrt{p})` is one atom. Literal Unicode `√` is forbidden in
native TeX; use a complete `\sqrt{...}` command.

Do not use `\tag{...}` inside `TPDisplayMath`; write an equation number as
`\qquad(n)`. Normalize reviewed mathematical text to TeX before compilation:
use `\vec{...}` for combining vector arrows, `\gg` for `≫`,
`\mathfrak{n}` for the Fraktur glyph, and `\leftrightarrow` for `↔`.
The renderer may normalize these known forms but must report any unsupported
Unicode with the affected unit ID before LuaLaTeX.

Treat a long sentence beginning with “Table N gives/shows/compares...” as
narrative body, not a caption. A caption must be a compact label attached to its
visual.

Compose the document as `original front -> translated body -> original tail`.
The original front includes the title, authors, abstract, and table of
contents. The original tail includes acknowledgements, references, and
appendices.
Copy full untranslated source pages as vector PDF pages. If the Introduction or
post-Conclusion boundary occurs mid-page, crop the untranslated portion from
the source PDF without rasterization and place it on a separate output page.
Always begin the translated body on a new page. Do not reconstruct front matter,
acknowledgements, appendices, or references from extracted text.

## Default Chinese typography

Use reviewed source flow metadata for list items, continuations and ordinary
paragraphs. Do not infer list termination from Chinese punctuation. Standalone
display equations retain their exact reviewed source ownership even when the
extractor placed suppressed labels between adjacent equations. All exporters
and validators must agree on that ownership; report each consumed review ID.

Use the general `zh-academic-v1` profile for translated PDFs:

- A4 pages with 53pt left/right, 48.5pt top, and 55.5pt bottom margins;
- SimSun 11.5pt narrative text, justified with 16.9pt absolute line pitch
  and natural 0pt letter spacing;
- 23pt first-line indent and 4pt paragraph spacing;
- SimHei 13.8pt level-one headings and SimHei 11.5pt lower-level headings;
- ideographic spacing after section numbers;
- hanging indentation for lists;
- no new paragraph or repeated indent for a source-page continuation.
- no repeated indent for an explicitly reviewed `paragraph` with
  `continuation_of`, including across independent display groups; the anchor is
  the nearest preceding prose, and each display retains its reviewed ownership.
- no new paragraph for a same-page extraction split when the preceding block
  ends mid-sentence; preserve an explicit line break before a new bold
  definition label and represent that label as a semantic paragraph start with
  the normal 23pt indent, not a bare HTML line break;
- use Latin Modern Roman regular, bold, and italic for non-mathematical Latin
  text while retaining SimSun and SimHei for Chinese;
- translated text drawn directly into the final page box so its PDF text-layer
  coordinates match its visible location in the PDF.

Record an explicit relationship between each footnote number, source anchor,
and note body. Insert the translated note at the anchor and let TeX repaginate
the body so the complete note remains at the bottom of the same translated
page, below a visible horizontal rule and in smaller SimSun text. Do not render
the note again as body text. `end-of-body` is permitted only when the note
itself is taller than the usable page, and that condition must be recorded as
`oversized_footnote: true`. Scale figures and tables proportionally to fit the
content box; never stretch or crop them. Keep every visual with its caption on
one page. Use a width-constrained TeX box for long equations so mathematical
glyphs remain embedded text.

Treat extraction blocks as layout units, not automatically as paragraphs.
Explicit reviewed continuation links take precedence over punctuation-based
extraction-split handling. Reject links crossing sections or unrelated content;
only native displays and explicitly linked same-paragraph footnotes may intervene.
Merge only consecutive translated body units in the same section when the
previous source page ends mid-sentence and no heading, list, figure, table, or
other source clip intervenes. Record merged IDs and typography metadata in the
layout report so validation can distinguish real paragraphs from continuations.
Use tall temporary pages only for measurement. Never copy those pages into the
final PDF as Form XObjects; reject 4096pt measurement forms and unexpected link
annotations during validation.

`中文翻译.layout.json` must report actual visual dimensions, applied scale,
source and target internal font size, aspect ratio, visual page, caption page,
footnote anchor page, and style-consistency metrics. Validation compares this
report with `visual-layout.json`; inventory mismatches, out-of-bounds geometry,
aspect-ratio changes, caption-page changes, footnote-page changes, or
unexpected bold body text fail the run.

## Reader scope

Validate the PDF's own text layer, geometry, and intrinsic links. The configured
Zotero workflow opens PDFs in Google Chrome, so Zotero built-in-reader overlays
are outside the default translation and acceptance scope. Do not mention or
optimize them unless the user explicitly asks to use the built-in reader again.
In that case, follow [zotero-reader.md](zotero-reader.md).
