---
name: translate-paper
description: Translate academic-paper PDFs or requested textbook chapters into Chinese with coordinated Codex agents using the latest verified flagship model, preserving figures and native mathematics and producing validated PDF, Markdown, and summary artifacts. Supports resumable paper batches and Zotero ingestion when in scope. Use when explicitly requested.
---

# Translate paper

Translate with Codex agents all using the latest verified flagship model
(currently GPT-6 Astra, `gpt-6-astra`). Assign translation,
layout, and independent visual review to separate subagents under a coordinating
main agent. Never use a translation API, browser translator, local translation
model, or machine-translation package. Use scripts only for deterministic
extraction, OCR, review replay, rendering, validation, packaging, and Zotero
operations.

## Agent roles

Read [references/agent-workflow.md](references/agent-workflow.md) before dispatch.
The coordinator owns canonical checkpoints, shared source/layout reviews, integration, and
Zotero writes; the translation agent writes isolated batches; the layout agent
owns rendering; an independent visual agent inspects the final PDF page images.
Run independent work in parallel and wait for required inputs between stages.
Do not let multiple agents write the same artifact or let the producer approve
its own PDF. Explicitly select the same verified model for every subagent,
currently `gpt-6-astra`; do not substitute cheaper models for routine roles.
Apply [role-specific reasoning effort](references/reasoning-policy.md):
translation and source/math review `high`, routine PDF production and metadata
inspection `low`, visual review and summary `medium`; use `high` visual review
for dense mathematics or complex source/output correspondence. Pass actual
spawn settings and follow the reference's escalation rules. If subagent tools
are unavailable, follow the disclosed fallback
in that reference while retaining every validation and visual-review gate.

## Completion and continuity

Complete the user's requested outcome in the current turn by default. Translation
batches, persisted JSONL, ledgers, and progress summaries are recovery checkpoints,
not turn boundaries. After saving and validating each checkpoint, immediately
continue with the next batch, unresolved formula, render, visual review, package,
requested downstream edit, and Zotero step that remains in scope. Do not ask the
user to type "continue" and do not send a final partial-completion response while
safe, authorized work remains runnable.

For long work, give concise progress updates without yielding the task. Before
context pressure or compaction, finish the current atomic edit, persist it, and
record the exact next unit or paper in the existing checkpoint; after compaction or
resume, inspect that checkpoint and continue automatically without redoing passed
work. Elapsed time, paper length, formula count, the number of remaining batches,
or the existence of a resumable checkpoint are not blockers.

Stop before completion only when further progress requires a material user choice,
missing source or credentials, unavailable external state, authorization outside
the request, or human evidence that cannot be obtained with the available tools.
Report the exact blocker and the saved resume point. Ordinary extraction defects,
validation failures with actionable diagnostics, and recoverable tool errors must
be investigated and retried in the same turn.

## Efficient execution

- Use the skill scripts for deterministic work and Codex agents only for
  translation and review decisions. Batch independent read-only inspections and
  validations when this does not create competing writes.
- Run preparation once, inspect its reports and only the pages they identify, then
  rerun the narrow affected stage after a reviewed correction. Do not repeatedly
  OCR, extract, or render the whole paper without new evidence.
- Express paper-specific extraction corrections in `boundary-review.json`,
  `source-review.json`, `math-review.json`, or `visual-layout.json`. Do not fork or
  patch a helper under a project's temporary directory. Change a skill script only
  for a demonstrated general defect, add a regression test, and preserve the
  reviewed paper checkpoints.
- Keep `translations.jsonl` as the canonical translation state. Temporary section
  files may support an atomic edit, but merge and validate them immediately; do not
  spend a later turn rediscovering or reconciling completed batches.
- Wait for long-running extraction, OCR, compilation, and rendering commands and
  continue when they finish. A normally running command is progress, not a reason
  to return control to the user.

## Prepare

1. Read applicable project instructions first. Resolve the source, requested
   content range, final PDF paths, supporting-artifact directory, downstream
   links, and local-only/Zotero scope before creating files. A project-local
   workflow applies to this project, not every future use of the skill.
2. Load the available PDF skill. Use `uv run --script` for the bundled Python
   scripts; for ad hoc PDF inspection use `uv run --with pymupdf python` rather
   than guessing a bundled interpreter path. Use UTF-8 explicitly for JSON/text;
   on Windows set `PYTHONUTF8=1` for Python tools using locale-default reads.
3. Read [references/translation-contract.md](references/translation-contract.md).
   For a chapter or excerpt, also read
   [references/chapter-workflow.md](references/chapter-workflow.md); a section
   of one document is not a multi-paper batch.
4. Choose single-paper mode unless the request names a paper list, asks which
   Zotero papers lack translations, or resumes multiple papers. For those
   cases, read [references/batch-workflow.md](references/batch-workflow.md).
5. Default to Zotero only when the request and applicable project instructions
   allow it. Local-only scope skips Zotero readiness, target selection, credentials,
   ingestion, and verification; it is not a blocker. When in scope, read
   [references/zotero-ingestion.md](references/zotero-ingestion.md), load
   `$zotero:Zotero`, and use the plugin plus skill-local adapter.
6. Never modify the source PDF, `zotero.sqlite`, or Zotero storage. Do not use
   computer-use as a fallback.

## Single-paper workflow

### 1. Extract and review

```powershell
uv run --script <skill-dir>\scripts\prepare_paper.py `
  --input <source.pdf> `
  --work-dir <work-dir> `
  --ocr auto
```

Inspect the manifest, boundary review, source previews, translation units,
math review, visual layout, and source-review report.

- Preserve the title, authors, abstract, and table of contents as original
  front matter.
- Translate the reviewed Introduction-through-Conclusion main body, or the
  explicit chapter/excerpt boundary.
- Preserve acknowledgements, references, and appendices as the original tail.
- Clear `needs_boundary_review` through `boundary-review.json`, never by
  editing the manifest.
- Require `source_review.status: pass`, one unique inventory entry for every
  numbered figure/table, and no unresolved mathematical entry.

When extraction swallows prose, splits a multi-panel visual, misclassifies a
caption, or leaves plot labels as headings/equations, read
[references/source-review.md](references/source-review.md). Record corrections
in source-hash-bound `source-review.json` and rerun preparation. Do not create a
paper-specific repair script.

Treat a formula-only translatable body record as an extraction error. Convert
it to reviewed native display math or merge it with its real surrounding prose;
never add filler Chinese to pass validation.

### 2. Translate

Keep existing `translations.jsonl` on resume; never overwrite it with the empty
template. Use [translation-contract.md](references/translation-contract.md#batch-checkpoints)
for the reusable checkpoint command. It initializes from the template only when
needed, merges model-authored JSONL batches atomically, validates exact token
order and source identity, and reports the next untranslated ID.

- Translate from `protected_text`.
- Preserve every `[[Pdddd]]`, `[[F...]]`, bold marker, and italic marker
  exactly once and in order. Copy full tokens; do not invent shortened aliases
  such as `[F1]` or regenerate token IDs from a guessed naming convention.
- Keep IDs and one JSON object per line.
- Translate in section batches of at most about 4,000 source words, persist and
  validate every completed batch, then begin the next batch in the same turn.
- Keep formal assumption/protocol/problem names in English when conventional;
  translate generic concepts into Chinese.
- Use `glossary.json` only for within-paper consistency.

### 3. Export, render, and validate

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

Require a passing strict report, then assign mandatory independent visual review
using [references/visual-review.md](references/visual-review.md). The reviewer
must actually open the contact sheet and **every page image of the final Chinese
PDF** at readable resolution, with source comparisons for complex content.
Save `visual-review.json` bound to the reviewed PDF hash and containing per-page
observations. Fix defects, rerender, and repeat review before delivery. Automated
checks or a contact sheet alone cannot establish visual acceptance.

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

### 4. Summarize and package

Packaging requires a passing final visual review and `check_visual_review.py`
check in addition to the strict, quick, and summary checks. Summary drafting
from the reviewed source may run in parallel with translation or rendering.

Write `中文论文总结.md` from the requested source only, using these nine headings:
文献信息、一句话结论、研究问题、方法与数据、核心贡献、主要结果、局限、复现材料、关键词。
Target 1,200–1,400 counted characters within the accepted 800–1,500 range.
`check_summary.py` counts non-whitespace text including Latin terms, numbers,
and headings, not just Chinese characters. For theoretical or textbook material,
describe definitions, proof methods, and examples; state when experiments or
reproduction materials are not provided rather than inventing them.

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
[chapter-workflow.md](references/chapter-workflow.md). The standard bundle includes:

- byte-identical `原文 PDF.pdf`;
- `中文翻译.pdf`, `中文翻译正文.md`, `中文论文总结.md`, `zotero-item.json`;
- `中文翻译.layout.json`, `translation-validation.json`;
- `quick-validation.json` and `summary-validation.json`;
- `visual-review.json` with final PDF hash and complete page coverage;
- `source-review-report.json` when review operations were applied.

Complete requested/project-required course or index links only after the final
PDF passes validation. Check every matching entry, use paths to the actual final
files, and preserve existing resource and completion-checkbox behavior. These
are conditional project integration steps, not a requirement to create an index.

### 5. Ingest and verify Zotero (when in scope)

Follow the exact sequence in
[references/zotero-ingestion.md](references/zotero-ingestion.md): readiness,
targets, search, guarded replacement, ingest preview, targets again immediately
before `--yes-ingest`, actual ingest, then verify.

Require one exact parent with exactly four children: stored `原文 PDF`, stored
`中文翻译 PDF`, stored `中文翻译 Markdown`, and one `中文论文总结` note. Report the
local final paths, target, parent key, and verified child titles.

## Batch workflow

Create the source-hash-bound `batch-run.json` described in
[references/batch-workflow.md](references/batch-workflow.md). When Zotero is in
scope, audit it before translating and skip only parents whose four-child
verification passes. Call `next` after every completion or resume. Continue processing returned
papers in the same turn until `next` reports `complete` or a true blocker from
the continuity contract is reached.

Process the returned paper through the single-paper gates, stage its artifacts
under its work directory (or declare overrides), call `package`, ingest and
verify Zotero, then call `record-zotero`. Finish only when `audit --zotero`
writes a passing `batch-audit.json`. For local-only batches, set
`zotero_enabled: false`, skip all Zotero steps, continue after `package`, and
finish with `audit` without `--zotero`.

Never restart a terminal paper after compaction or resume. A changed source hash
invalidates only that paper.
