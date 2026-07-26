---
name: translate-paper
description: Translate academic-paper PDFs into Chinese with the active Codex model, preserve figures, tables, equations, citations, appendices, references, and original bibliographic material, create validated Chinese PDF, Markdown, and summary artifacts, and safely import them into Zotero by default unless the user opts out. Use for text or scanned papers when the user asks for Chinese paper translation, layout-preserving output, paper summarization, Zotero ingestion, or exact-parent replacement.
---

# Translate paper

Translate every Chinese sentence with the active Codex model. Never use a
translation API, website, browser translator, local translation model,
machine-translation library, or subagent. OCR recognizes source text only.

## Required preparation

1. Load and follow `$pdf`.
2. Read [references/translation-contract.md](references/translation-contract.md).
3. Treat Zotero as in scope by default. Skip Zotero only when the user
   explicitly requests no Zotero import or a local-only result; omission is not
   an opt-out. When Zotero is in scope, read
   [references/zotero-ingestion.md](references/zotero-ingestion.md), load
   `$zotero:Zotero`, and use the plugin plus the skill-local adapter.
   Assume Zotero opens PDFs in Google Chrome; do not inspect or report
   built-in-reader overlays. Only if the user explicitly brings the built-in
   reader back into scope, read
   [references/zotero-reader.md](references/zotero-reader.md).
4. Obtain the source PDF. On every ingest run, resolve Zotero targets. Honor an
   explicit user-supplied library or collection; otherwise use the writable
   currently selected target. If none is selected, use the only writable
   target when exactly one exists. Ask for the destination only when no
   writable target exists or multiple plausible targets remain.
5. Never modify the source PDF, `zotero.sqlite`, or Zotero storage. Do not fall
   back to computer-use.

## Workflow

### 1. Prepare and review the paper

```powershell
uv run --script <skill-dir>\scripts\prepare_paper.py `
  --input <source.pdf> `
  --work-dir <cwd>\tmp\pdfs\<paper-slug> `
  --ocr auto
```

Inspect `manifest.json`, `visual-layout.json`, `translation-units.jsonl`,
`layout.txt`, and all source previews. Verify reading order, the Introduction
start, the last main-section end, and the first excluded heading. Require one
unique inventory entry for every numbered figure and table. Missing, duplicate,
cropped, or malformed visual objects are blocking errors.

For each visual, verify that `visual-layout.json` records the complete vector
envelope (drawing, internal labels, and caption association), source hash,
source body/internal font sizes, target dimensions, aspect ratio, and scale
basis. Size by typography:

`target internal font = source internal font / source body font × 11.5pt`.

When internal font size cannot be measured, automatically continue using the
source visual/content-width ratio and retain the generated warning. This is the
only sizing fallback; a missing or duplicate visual never falls back.

Inspect `math-review.json`. Every inline mathematical fragment must contain
valid TeX and a review status. Reconstruct each display equation as one logical
TeX block, set `review_status` to `manually_reviewed`, and compare it with the
source preview. Do not translate or export while any mathematical entry is
`unresolved`.

If `manifest.json` says `needs_boundary_review: true`, edit
`boundary-review.json`, not the manifest:

- set `start_id` and `stop_before_id` from `candidates`;
- set `reviewed` to `true` and write a specific `reason`;
- set `allow_missing_conclusion` only after confirming that the paper genuinely
  has no Conclusion heading;
- use `include_ids` and `exclude_ids` only for OCR classification mistakes.

Rerun the same preparation command. It applies the reviewed file only when its
source hash matches. Do not translate until the resulting manifest clears the
review gate. For scanned mathematical papers, visually compare OCR text with
every source page and change equations, diagrams, tables, and damaged regions
to `source_clip`.

### 2. Translate with Codex

Copy `translations.template.jsonl` to `translations.jsonl`. Fill only
`translated_text`:

- translate records whose `translatable` value is `true`;
- translate from `protected_text`, preserving every `[[Pdddd]]`, `[[F...]]`,
  and paired `[[B..._OPEN/CLOSE]]` / `[[I..._OPEN/CLOSE]]` marker exactly
  once and in order;
- place the translated equivalent inside source bold/italic marker pairs; keep
  a source line break before a bold definition label such as Completeness,
  Soundness, or Zero-knowledge;
- retain IDs and one JSON object per line;
- work in section-sized batches of at most about 4,000 source words;
- decide terminology from context. Keep canonical assumption, protocol, and
  problem names in English while translating generic concepts into Chinese.
  Use `glossary.json` only as a per-paper consistency record, never as a
  mechanical global replacement table;
- retain standard English acronyms on first use;
- persist each completed batch before continuing.

Scripts may extract, protect, render, or validate text. They must never make a
translation decision.

### 3. Export, render, and validate

Use the default `zh-academic-v1` general Chinese academic typography profile:

- A4 narrative body: SimSun 11.5pt, justified, 16.9pt absolute line pitch,
  natural 0pt letter spacing, 23pt first-line indent, and 4pt paragraph spacing;
- level-one headings: SimHei 13.8pt with strong spacing;
- level-two and deeper headings: SimHei 11.5pt with compact spacing;
- translated-page margins: 53pt left/right, 48.5pt top, and 55.5pt bottom;
- numbered headings: one ideographic space between the number and title;
- list items: hanging indent, never the narrative first-line indent;
- source-page continuations: merge into one logical paragraph before layout.
- source bold/italic runs: preserve them in translated prose;
- non-mathematical Latin text: use Latin Modern Roman regular, bold, and italic
  faces while retaining SimSun/SimHei for Chinese;
- definition items: render a semantic paragraph start with the normal 23pt
  first-line indent, never a bare HTML line break;
- all inline and display mathematics: render as embedded LuaLaTeX text with
  Latin Modern Math. Never use raster images, SVG, HTML images, source-PDF
  equation clips, or PDF image/Form fallbacks for mathematical content;
- native-TeX review gate: abort export when TeX is missing, unbalanced,
  unreviewed, fails LuaLaTeX compilation, or reports a missing glyph. Never
  weaken this gate by falling back to a visual copy of the formula;
- mathematical fragments: protect the complete atomic expression, including
  adjacent function names and delimiters such as `O(\sqrt{p})`. Reject a
  literal Unicode `√`; it must be represented by complete `\sqrt{...}` TeX.
- style consistency: math-font or symbol-font runs enter math review before
  prose styling. Never emit bold body text without a source bold span; preserve
  genuine source bold and italic spans exactly;
- numeric body citations: render as superscripts; never superscript bibliography
  entries or mathematical intervals;
- body footnotes: place below a separator at the anchored translated-page
  bottom, or move the complete note to an end-of-body footnote area when it
  cannot fit;
- figures and tables: scale proportionally within the content box without
  clipping or distortion. Only non-mathematical visual objects may use source
  vector clips;
- draw translated text directly into its final page box; never re-embed the
  4096pt measurement page because the final PDF must expose one unambiguous
  text coordinate system.

Compose the final PDF in this fixed order:

- preserve all material before the Introduction as vector source-PDF pages;
- start the translated Introduction-to-Conclusion body on a fresh page;
- preserve all material after the Conclusion as vector source-PDF pages;
- when a boundary shares a source page, split that page at the reviewed
  boundary and place the untranslated vector crop on its own output page;
- never reflow authors, abstract, keywords, biographies, acknowledgements,
  appendices, or references as substitute text.

```powershell
uv run --script <skill-dir>\scripts\export_translation_markdown.py `
  --manifest <work-dir>\manifest.json `
  --translations <work-dir>\translations.jsonl `
  --output <output-dir>\中文翻译正文.md

uv run --script <skill-dir>\scripts\render_translation.py `
  --manifest <work-dir>\manifest.json `
  --translations <work-dir>\translations.jsonl `
  --output <output-dir>\中文翻译.pdf

uv run --script <skill-dir>\scripts\check_translation.py `
  --source <source.pdf> `
  --translated <output-dir>\中文翻译.pdf `
  --manifest <work-dir>\manifest.json `
  --translations <work-dir>\translations.jsonl `
  --preview-dir <work-dir>\translated-preview
```

Require a passing report, then conduct two visual passes: inspect the full
contact sheet for global pagination and inspect at readable resolution every
page containing figures, tables, footnotes, equations, mixed one/two-column
layout, or multiple source clips. Compare the source and output visual
inventories in the second pass. Fix clipping, overlap, fragmentation,
distortion, caption splitting, missing glyphs, malformed citations, or
untranslated body before continuing. Confirm that the layout report names
`native-lualatex-v1`, `zh-academic-v1`, SimSun, SimHei, Latin Modern Roman,
and Latin Modern Math; do not accept Microsoft YaHei for translated Chinese
text or SimSun for translated English prose. Require `native-font` for every
inline fragment, `native-display-font` for every display equation, zero
unresolved math, and zero mathematical image fallbacks. Confirm
that `boundary_layout` records contiguous original front segments, translated
body pages, and original tail segments with no shared output page.
Require `native-lualatex` text embedding, zero 4096pt measurement forms, and no
unexpected intrinsic PDF link annotations on translated-body pages.
Run `scripts/quick_validate.py` as the final fast structural smoke test before
publishing or replacing a Zotero attachment.

### 4. Write and validate the Chinese summary

Write `<output-dir>\中文论文总结.md`, using only the paper, with these headings:

- 文献信息
- 一句话结论
- 研究问题
- 方法与数据
- 核心贡献
- 主要结果
- 局限
- 复现材料
- 关键词

Use 800–1,500 Chinese characters. Preserve exact reported numbers and label any
inference as an inference. If the paper provides no dataset, code, or
supplementary material, say so instead of inventing one.

```powershell
uv run --script <skill-dir>\scripts\check_summary.py `
  --summary <output-dir>\中文论文总结.md `
  --report <work-dir>\summary-validation.json
```

### 5. Prepare Zotero metadata

Write `<output-dir>\zotero-item.json` with original-language metadata. Include
`itemType`, title, creators, date, and all available publication, volume, issue,
pages, DOI, URL, and language fields. Never translate Zotero bibliographic
metadata.

Require DOI, arXiv ID, or exact title plus year. If this run began from a known
Zotero parent, record its exact item key as `_sourceParentKey`; never infer that
field from a search result. The adapter strips it before creating the new item.

### 6. Ingest into Zotero by default

Run this step unless the user explicitly opted out of Zotero import. Do not
interpret silence about Zotero as permission to skip it.

Follow [references/zotero-ingestion.md](references/zotero-ingestion.md):

1. Check plugin, local API, Connector, and Web API credential readiness.
2. Resolve one writable destination target.
3. Search local and synced state.
4. Stop on multiple matches or conflicting identity.
5. Immediately delete one unique exact/source-parent match and its children
   with `--yes-delete`. Invoking this skill is standing authorization for that
   guarded replacement; do not request another confirmation.
6. Wait for local sync absence, preview ingestion, then run `--yes-ingest`.
7. Verify one parent with `原文 PDF`, `中文翻译 PDF`,
   `中文翻译 Markdown`, and `中文论文总结`.

Never ingest while an old matched parent remains locally. After a partial
Connector failure, search and inspect children before any retry.
Verify Zotero storage structure only; do not launch or inspect the built-in PDF
reader because the configured reading path is Google Chrome.

## Output contract

- Intermediates: `tmp/pdfs/<paper-slug>/`.
- Finals: `output/pdf/<paper-slug>/`.
- Finals include the byte-identical original PDF, `中文翻译.pdf`,
  `中文翻译正文.md`, `中文论文总结.md`, and `zotero-item.json`.
- Report local paths even after successful Zotero ingestion.
- Report Zotero parent key, target, and verified child titles after ingestion.
