# Production and delivery

Read when the reviewed translation is complete. The producer owns staging outputs;
the coordinator owns accepted inputs, packaging, and Zotero writes.
Read [rendering-contract.md](rendering-contract.md) for native math, preserved
visuals/front/tail, and the `zh-academic-v1` layout requirements.

## Export, render, and validate

Before translation dispatch and again after affected source/math/engine changes:

```powershell
uv run --script <skill-dir>\scripts\paper_preflight.py `
  --manifest <work-dir>\manifest.json `
  --report <work-dir>\paper-preflight.json
```

Inspect its source diagnostic sample for source structures present in this paper.
The complete mathematical inventory uses the actual production preamble and font
checks. Both sample and math compile are diagnostics, not Chinese translations
or visual acceptance. A changed input invalidates readiness; unchanged compile
inputs may reuse their verified artifacts. Packet creation, batch readiness and
formal export use the same readiness assertion.

First run `translation_checkpoint.py --manifest <manifest> --translations
<translations> --require-complete`. This is a text checkpoint gate, not a
substitute for source, math, or visual review. Use `zh-academic-v1` and native
LuaLaTeX mathematics:

```powershell
uv run --script <skill-dir>\scripts\export_translation_markdown.py `
  --manifest <work-dir>\manifest.json `
  --translations <work-dir>\translations.jsonl `
  --output <staging-dir>\中文翻译正文.md

uv run --script <skill-dir>\scripts\render_translation.py `
  --manifest <work-dir>\manifest.json `
  --translations <work-dir>\translations.jsonl `
  --output <staging-dir>\中文翻译.pdf

uv run --script <skill-dir>\scripts\check_translation.py `
  --source <source.pdf> `
  --translated <staging-dir>\中文翻译.pdf `
  --manifest <work-dir>\manifest.json `
  --translations <work-dir>\translations.jsonl `
  --preview-dir <work-dir>\translated-preview `
  --strict-invariants
```

Require passing strict and quick reports, then assign mandatory independent visual review
using [visual-review.md](visual-review.md). The reviewer
must actually open the contact sheet and **every page image of the final Chinese
PDF** at readable resolution, with source comparisons for complex content.
Save `visual-review.json` bound to the reviewed PDF hash and containing per-page
observations. Fix defects, rerender, and repeat review before delivery. Automated
checks or a contact sheet alone cannot establish visual acceptance.

Before assigning that review, the producer opens the risk pages and each prior
repair location in the new candidate. Record stable defect IDs, category, source
units, current output/image locations, regression evidence and actual local
inspection. For a systemic defect, check all its instances, including list
continuations and post-list paragraphs. Repeated failure of the same class must
trigger focused diagnosis of the full source-to-PDF path before another full
review. Unit tests of the final formatting function alone are insufficient.

The reviewer may report serious findings promptly, but the producer waits for
the complete consolidated defect list before rerendering. Do not spend a new
full review on a candidate whose previously reported defect still reproduces.

Use `review_readiness.py --translated <candidate.pdf> --prior-review <prior.json>`
to list stable defect IDs. The producer authors a JSON inspection record with
`translated_sha256`, ordered `prior_review_sha256`, current `risk_images`, and
`repairs`: each has `defect_id`, `status: resolved`, and absolute
`inspected_images`. Systemic classes also record `scope` and
`related_occurrence_images`; repeated failures record `regression_evidence` paths.
Run the same command with `--repairs <producer-inspection.json> --report
<producer-readiness.json>` before independent review. Repeat `--prior-review` for
earlier relevant rounds. With no prior defects the risk-page inspection is still
required. The checker validates identities and recorded coverage, not whether
the producer actually opened an image. Do not fabricate inspection records.

Use `\qquad(n)`, not `\tag{n}`, for display-equation numbering. Prefer reviewed
TeX such as `\vec{...}`, `\gg`, `\mathfrak{n}`, and `\leftrightarrow`; resolve
any unit-specific unsupported-Unicode error before rerendering.

Run the fast structural check and save its report:

```powershell
uv run --script <skill-dir>\scripts\quick_validate.py `
  --source <source.pdf> `
  --translated <staging-dir>\中文翻译.pdf `
  --expected-source-sha256 <source-sha256> `
  --report <work-dir>\quick-validation.json
```

## Summarize and package

Packaging requires a passing final visual review and `check_visual_review.py`
check in addition to the strict, quick, and summary checks. Summary drafting
from the reviewed source may run in parallel with translation or rendering.

Write UTF-8 Markdown `中文论文总结.md` from the requested source only, using these nine headings:
文献信息、一句话结论、研究问题、方法与数据、核心贡献、主要结果、局限、复现材料、关键词。
Target 1,200–1,400 counted characters within the accepted 800–1,500 range.
`check_summary.py` counts non-whitespace text including Latin terms, numbers,
and headings, not just Chinese characters. For theoretical or textbook material,
describe definitions, proof methods, and examples; state when experiments or
reproduction materials are not provided rather than inventing them.
Deliver this same file as the Zotero stored attachment titled `中文论文总结`,
with a `.md` filename and `text/markdown` content type. Do not convert it into
an HTML note or place Markdown source inside a Zotero note.

```powershell
uv run --script <skill-dir>\scripts\check_summary.py `
  --summary <staging-dir>\中文论文总结.md `
  --report <work-dir>\summary-validation.json
```

Write original-language `zotero-item.json` with item type, title, creators,
date, publication fields, DOI/arXiv/URL, and language. Require DOI, arXiv ID, or
exact title plus year. Set `_sourceParentKey` only when the run began from that
known parent.

Use `output/pdf/<slug>` as the default final directory. Honor project rules that
separate final PDFs from supporting artifacts; do not create a second bundle in
the default directory. Chapter source provenance is defined in
[chapter-workflow.md](chapter-workflow.md). The standard bundle includes:

- byte-identical `原文 PDF.pdf`;
- `中文翻译.pdf`, `中文翻译正文.md`, `中文论文总结.md`, `zotero-item.json`;
- `LaTeX源码包.zip`, the editable source of the exact accepted PDF;
- `中文翻译.layout.json`, `translation-validation.json`;
- `quick-validation.json` and `summary-validation.json`;
- `visual-review.json` with final PDF hash and complete page coverage;
- `source-review-report.json` when review operations were applied.

Verify the source copy's SHA-256 and the delivered PDF's exact match to the visual
report. If a final bundle already exists, identify its owned files from its
checkpoint or manifest before replacement; do not recursively delete a guessed
directory. Keep unrelated files. A fresh translation uses separate staging until
all gates pass.

### Rebuildable LaTeX source

After acceptance, export from the actual `native-latex` production inputs, without
rerendering or replacing the accepted PDF:

```powershell
uv run --script <skill-dir>\scripts\export_latex_project.py `
  --manifest <work-dir>\manifest.json `
  --translations <work-dir>\translations.jsonl `
  --translated <staging-dir>\中文翻译.pdf `
  --layout <staging-dir>\中文翻译.layout.json `
  --output <staging-dir>\LaTeX源码包.zip
```

The exporter packages editable `body.tex`, every referenced image/code asset,
only the accepted original front/tail PDF pages, and standalone `build.py`.
`body.tex` alone compiles the Chinese body; `build.py` runs LuaLaTeX twice and
assembles the complete PDF with PyMuPDF. It copies the canonical translation
snapshot for provenance; editing this independent TeX project never writes back
to canonical `translations.jsonl`, which remains the translation authority.

The exporter builds in a separate temporary directory and requires identical
page count, extracted text and 96dpi rendered pixels to the accepted PDF.
`source-manifest.json` records source/accepted-PDF/canonical-input hashes, each
package member's hash, and per-page rebuild evidence. Packaging and ingestion
reject source/output identity mismatches, unsafe relative paths, and changed
members. The source ZIP is required for complete local bundles as well as Zotero.

Include build instructions and font dependencies, normally SimSun, SimHei,
Segoe UI Symbol and Latin Modern fonts. Do not distribute unlicensed system
fonts, credentials, caches, or the whole source book. Fonts and the LuaLaTeX
environment must be installed separately; changing them can change pagination.
A lone `.tex` file without its dependencies is not a complete deliverable.

Complete requested/project-required course or index links only after the final
PDF passes validation. Check every matching entry, use paths to the actual final
files, and preserve existing resource and completion-checkbox behavior. These
are conditional project integration steps, not a requirement to create an index.

## Repair scope

Do not restart the whole workflow after a local defect. A report is reusable only
while its relevant inputs and validator are unchanged; a failure is never cached
as acceptance.

| Change | Required rerun |
| --- | --- |
| Source PDF, boundary, extraction classification or protected tokens | Replay preparation; reconcile existing translations; revise only affected prose; run all downstream gates. |
| Chinese prose or reviewed native TeX | Checkpoint; export Markdown; render; strict and quick checks; full independent visual review. No new OCR/extraction when source metadata is unchanged. |
| Visual size/spacing or paragraph-flow renderer | Render; strict and quick checks; full visual review. Re-export Markdown only if semantic text/grouping used by that exporter changes. |
| Validator-only correction with byte-identical PDF | Regression-test the correction; rerun affected checks. Do not recompile or restart a completed visual review solely for that change. |
| Summary only | Summary check and any dependent Markdown delivery copy/attachment; no paper render or PDF review. |
| Byte-identical packaging or Zotero upload | Verify hashes/identity and destination; retain the accepted PDF review. |

For code corrections, reproduce the general failure and run focused regression
tests. Run the relevant broader suite once after related edits are settled; do
not repeatedly rerun an unchanged passing suite. No tests are needed merely to
restyle instruction prose. Treat nonzero process exit codes as failures; do not
let a successful later command obscure an earlier failure in a chained shell.

Formula ownership is explicit: the reviewed `source_ids` determine logical
displays for PDF, Markdown and validation. Suppressed extraction fragments do
not authorize merging independently reviewed rules. Layout output records every
consumed review and source ID; missing, duplicate or overlapping ownership blocks
export even if compilation succeeds.

Before freezing a candidate, inspect the compiler's actionable errors/warnings
and prioritize known risk pages: first/last body pages, long inline formula lists,
multiline captions, and proof endings followed by new paragraphs. This production
triage does not replace the independent all-page review.

Use reviewed native `\mdlgwhtsquare` for a hollow proof-end square with this
unicode-math stack; `\square` is not guaranteed to exist. Preserve the semantics
when the source extracts a square as `⊓⊔`, and preserve the paragraph after it.
For an overflowing inline list, an appropriate reviewed `\allowbreak` after a
comma can preserve notation while permitting wrapping. For a multiline visual
caption, a source-hash-bound `visual-layout.json` entry may set
`layout_needspace_pt` to reserve the measured group height. Inspect the result;
do not copy another paper's height or reduce font size to conceal clipping.

Keep full logs on disk. The producer's single completion handoff contains PDF
path/hash/page count, strict/quick report paths and status, preview directory,
and unresolved findings. Freeze both PDF and previews until the reviewer finishes;
return to production only after the coordinator has integrated the repair set.
