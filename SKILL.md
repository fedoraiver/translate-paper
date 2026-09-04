---
name: translate-paper
description: Translate one or more academic-paper PDFs into Chinese with the active Codex model, complete the requested workflow continuously end to end, preserve figures, tables, native mathematics, citations, appendices, references, and original bibliographic material, validate PDF/Markdown/summary artifacts, and safely organize them in Zotero by default. Use for single papers, paper lists or sections, Zotero missing-translation audits, resumable translation batches, layout-preserving output, summaries, ingestion, or exact-parent replacement.
---

# Translate paper

Translate every Chinese sentence with the active Codex model. Never use a
translation API, browser translator, local translation model, machine-
translation package, or delegated agent. Use scripts only for deterministic
extraction, OCR, review replay, rendering, validation, packaging, and Zotero
operations.

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

- Use the skill scripts for deterministic work and the active model only for
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

1. Load and follow `$pdf`.
2. Read [references/translation-contract.md](references/translation-contract.md).
3. Choose single-paper mode unless the request names a list/section, asks which
   Zotero papers lack translations, or resumes multiple papers. For those
   cases, read [references/batch-workflow.md](references/batch-workflow.md).
4. Treat Zotero as in scope unless the user explicitly requests local-only
   output. When in scope, read
   [references/zotero-ingestion.md](references/zotero-ingestion.md), load
   `$zotero:Zotero`, and use the plugin plus skill-local adapter.
5. Never modify the source PDF, `zotero.sqlite`, or Zotero storage. Do not use
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
- Translate the reviewed Introduction-through-Conclusion main body.
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

Copy `translations.template.jsonl` to `translations.jsonl`. Fill only
`translated_text` for translatable records.

- Translate from `protected_text`.
- Preserve every `[[Pdddd]]`, `[[F...]]`, bold marker, and italic marker
  exactly once and in order.
- Keep IDs and one JSON object per line.
- Translate in section batches of at most about 4,000 source words, persist and
  validate every completed batch, then begin the next batch in the same turn.
- Keep formal assumption/protocol/problem names in English when conventional;
  translate generic concepts into Chinese.
- Use `glossary.json` only for within-paper consistency.

### 3. Export, render, and validate

Use `zh-academic-v1` and native LuaLaTeX mathematics:

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

Require a passing strict report. Inspect the contact sheet, then inspect at
readable resolution every page containing a figure, table, footnote, equation,
mixed columns, or multiple clips. Reject clipping, duplication, caption splits,
missing glyphs, mathematical image fallbacks, unexpected bold text, lost
invariant text, or boundary overlap.

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

Write `中文论文总结.md` from the paper only, using the nine required headings
enforced by `check_summary.py`. Target 1,200–1,400 Chinese characters while
retaining the accepted 800–1,500 range.

```powershell
uv run --script <skill-dir>\scripts\check_summary.py `
  --summary <staging-dir>\中文论文总结.md `
  --report <work-dir>\summary-validation.json
```

Write original-language `zotero-item.json` with item type, title, creators,
date, publication fields, DOI/arXiv/URL, and language. Require DOI, arXiv ID, or
exact title plus year. Set `_sourceParentKey` only when the run began from that
known parent.

Use `output/pdf/<slug>` as the default final directory. Honor a project-
supplied directory such as `translations/papers/<group>/<slug>`. Include:

- byte-identical `原文 PDF.pdf`;
- `中文翻译.pdf`, `中文翻译正文.md`, `中文论文总结.md`, `zotero-item.json`;
- `中文翻译.layout.json`, `translation-validation.json`;
- `quick-validation.json` and `summary-validation.json`;
- `source-review-report.json` when review operations were applied.

### 5. Ingest and verify Zotero

Follow the exact sequence in
[references/zotero-ingestion.md](references/zotero-ingestion.md): readiness,
targets, search, guarded replacement, ingest preview, targets again immediately
before `--yes-ingest`, actual ingest, then verify.

Require one exact parent with exactly four children: stored `原文 PDF`, stored
`中文翻译 PDF`, stored `中文翻译 Markdown`, and one `中文论文总结` note. Report the
local final paths, target, parent key, and verified child titles.

## Batch workflow

Create the source-hash-bound `batch-run.json` described in
[references/batch-workflow.md](references/batch-workflow.md). Audit Zotero
before translating, skip only parents whose four-child verification passes,
and call `next` after every completion or resume. Continue processing returned
papers in the same turn until `next` reports `complete` or a true blocker from
the continuity contract is reached.

Process the returned paper through the single-paper gates, stage its artifacts
under its work directory (or declare overrides), call `package`, ingest and
verify Zotero, then call `record-zotero`. Finish only when `audit --zotero`
writes a passing `batch-audit.json`.

Never restart a terminal paper after compaction or resume. A changed source hash
invalidates only that paper.
