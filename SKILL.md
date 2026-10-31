---
name: translate-paper
description: Translate academic-paper PDFs or requested textbook chapters into Chinese, preserving original visuals and native mathematics and delivering validated PDF, Markdown, summary, and editable LaTeX source artifacts. Supports resumable batches and in-scope Zotero delivery. Use when explicitly requested.
---

# Translate paper

Deliver faithful Chinese body translation as PDF and Markdown, plus UTF-8
`中文论文总结.md`, plus a rebuildable `LaTeX源码包.zip`. Zotero requires five
stored attachments: two PDFs, two Markdown files, and the LaTeX source ZIP; the
summary title is `中文论文总结` and MIME type `text/markdown`, never an HTML note.
Use Codex translators, a separate PDF producer and independent visual reviewer,
with role settings from [reasoning-policy.md](references/reasoning-policy.md).
Keep the coordinator's current model/effort. Translation services and local
translation models are forbidden; scripts do deterministic processing only.

## Start or resume

1. Resolve project instructions, source, range, output locations, and Zotero scope.
   Resume from the active manifest in `run-state.json` (or its configured existing
   equivalent), then inspect current reports. `translations.jsonl` remains the
   translation authority; the state file is only a recovery pointer. See
   [agent-workflow.md](references/agent-workflow.md#recovery-pointer).
2. Read [agent-workflow.md](references/agent-workflow.md) and
   [reasoning-policy.md](references/reasoning-policy.md) once for dispatch. Read
   [translation-contract.md](references/translation-contract.md) for fidelity,
   boundary and token rules. Give workers relevant references and compact packets.
3. Load the available PDF skill. Use `uv run --script scripts/<script>.py` and
   set `PYTHONUTF8=1` on Windows; use `uv run --with pymupdf python` for ad hoc PDF
   inspection. Paths below are relative to this skill directory.
4. Read conditional references only for the active mode:

| Condition | Reference |
| --- | --- |
| Requested chapter or excerpt | [chapter-workflow.md](references/chapter-workflow.md) |
| Paper list, library audit, or resumed batch | [batch-workflow.md](references/batch-workflow.md) |
| Zotero explicitly or contextually in scope | [zotero-ingestion.md](references/zotero-ingestion.md), plus the available Zotero skill |
| Extraction or source classification needs correction | [source-review.md](references/source-review.md) |
| Source/math reconstruction or PDF typography | [rendering-contract.md](references/rendering-contract.md) |

Never modify the source PDF or directly edit Zotero's database or storage. Use
supported APIs for library changes. Do not use
computer-use as a fallback. For an explicitly fresh retranslation, copy the
original source and metadata to a fresh work directory; do not reuse old Chinese
prose. Identify prior artifacts from the item and known output paths, not a broad
filesystem crawl. Normally replace old Zotero artifacts only after the new bundle
passes acceptance; honor an explicit request to delete first.

## Execution gates

**Prepare and review.** Run `prepare_paper.py --input <source.pdf> --work-dir
<work-dir> --ocr off`. Extract native text first. For image-only pages or regions,
use the model's own image reading and save replayable source corrections; do not
OCR readable native text or enable a traditional OCR engine unless requested.
Review boundaries, source structure, mathematics, visuals,
and source diagnostics. Lists, continuation paragraphs, and display ownership
must be established before freezing units. Resolve suspicious extraction through
the hash-bound review files; formula-only body units require reclassification.
Read [source-review.md](references/source-review.md) for review and isolated repair.

**Preflight.** Seed a small paper-specific glossary from the source, then run
`paper_preflight.py --manifest <manifest> --report <work-dir>/paper-preflight.json`.
It checks readiness, compiles the complete native math inventory, and generates
a source diagnostic sample using the production engine. Inspect that sample and
resolve findings before dispatch. Missing or stale readiness blocks task packets
and export; a compile pass does not establish semantic correctness.

**Translate.** Preserve original front matter, visuals/captions, mathematics, and
tail; translate the reviewed main body (or requested range). Use the compact
task packets and atomic batch commands in
[translation-contract.md](references/translation-contract.md#batch-checkpoints).
Keep full protected/style tokens in exact order. One coordinator owns canonical
`translations.jsonl` and the within-paper glossary; workers return isolated
`id`/`translated_text` batches. Merge with the exact `--packet` and resolve proposed
glossary terms before dependent assignments. Preserve source anomalies faithfully.

**Produce.** After the complete translation checkpoint passes, follow
[production.md](references/production.md) for export, rendering, strict/quick
validation, summary, and packaging. Freeze inputs while production runs. Resolve
structural failures and complete the producer's repair-locus checks before
handing a stable PDF to visual review.

**Inspect.** Follow [visual-review.md](references/visual-review.md). An independent
reviewer must open the contact sheet and **every final page image** at readable
resolution and compare complex content to the source. Collect a complete defect
list for the frozen candidate before repair. A changed PDF requires fresh full
review; byte-identical copies do not. Automated success never substitutes for
image inspection. Require the hash-bound visual report and checker to pass.

**Deliver.** Package only the exact accepted PDF and passing reports. Complete any
requested project links, then in-scope Zotero delivery and five-attachment
byte verification. Select the route in [zotero-ingestion.md](references/zotero-ingestion.md):
retain Connector for new-parent imports; use an authorized existing-parent route
for chapter attachments. On a request to continue uploading, inspect live files
and the delivery ledger first; report completion when nothing is missing.
Batch mode continues until its ledger audit is complete. Do not
commit, publish, or modify unrelated resources merely because translation ran.
Honor an explicitly requested existing parent; preserve it and its other children.
The adapter's new-parent/replacement flow does not authorize replacing that parent.

## Keep work bounded and recoverable

- Follow the stage invalidation table in [production.md](references/production.md#repair-scope).
  Fix all known related defects together; rerun only affected stages. Change a
  shared helper only for a demonstrated general defect with a regression test.
- Use `paper_repair.py` for source changes after translation: preview the impact
  first, then explicitly apply it to a new revision. Never clear the whole
  checkpoint to bypass completed-unit guards.
- Read selected JSON fields/IDs; keep detailed logs on disk. Return counts,
  paths, hashes and actionable failures, never invented token/cost measurements.
- Start fresh agents for each new chapter across all roles, including source/math
  review and repairs; follow [chapter-workflow.md](references/chapter-workflow.md#multi-chapter-runs).
  Within one paper/chapter, reuse only when role, independence, model and effort fit.
  Honor the user's concurrency limit and fill available slots with ready work.
  Start dependent roles only when their inputs are ready. Use bounded waits and concise
  progress updates; do not repeatedly poll unchanged logs or ask agents to restate
  a completed handoff.
- Update the recovery pointer at accepted batches and stage transitions. Reuse
  checks only for unchanged inputs; retain failed reviews and their previews.
- Continue through repair, review, packaging, and in-scope ingestion in the same
  task. Length, context pressure, and a saved checkpoint are not stopping points.
  Stop only for missing user decisions, source/access/capabilities, authorization,
  or unavailable external state; record the blocker and precise resume point.
