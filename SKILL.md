---
name: translate-paper
description: Translate academic-paper PDFs or requested textbook chapters into Chinese, preserving original visuals and native mathematics and delivering validated PDF, Markdown, and summary artifacts. Supports resumable batches and in-scope Zotero replacement. Use when explicitly requested.
---

# Translate paper

Deliver a faithful Chinese body translation, PDF, Markdown, and summary. Use Codex
translation agents and a separate PDF producer and independent visual reviewer.
The summary is always a UTF-8 Markdown file named `中文论文总结.md`. In Zotero,
store it as a `.md` attachment titled `中文论文总结` (`text/markdown`), never as
an HTML child note. The four required children are two PDFs and two Markdown files.
Select each subagent's model and reasoning effort by role using
[reasoning-policy.md](references/reasoning-policy.md). The coordinator retains
the current conversation's model and effort. Never use translation services or local
translation models. Scripts perform deterministic document processing only.

## Start or resume

1. Resolve project instructions, source, requested range, output locations, and
   local-only versus Zotero scope. For a resumed run, read its existing checkpoint
   and current gate reports first; continue from the first invalid or incomplete
   stage. Do not rediscover accepted work or create a second state ledger.
2. Read [agent-workflow.md](references/agent-workflow.md) and
   [reasoning-policy.md](references/reasoning-policy.md) once for dispatch. Read
   [translation-contract.md](references/translation-contract.md) for fidelity,
   boundary and token rules. Pass each worker only its relevant
   reference and compact assignment, not the whole conversation or full inventory.
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

Never modify the source PDF, Zotero database, or Zotero storage. Do not use
computer-use as a fallback. For an explicitly fresh retranslation, copy the
original source and metadata to a fresh work directory; do not reuse old Chinese
prose. Identify prior artifacts from the item and known output paths, not a broad
filesystem crawl. Normally replace old Zotero artifacts only after the new bundle
passes acceptance; honor an explicit request to delete first.

## Execution gates

**Prepare and review.** Run `prepare_paper.py --input <source.pdf> --work-dir
<work-dir> --ocr auto` once. Inspect diagnostics and relevant source images;
expand to every source page when extraction quality or structure requires it.
Correct extraction through source-hash-bound `boundary-review.json`,
`source-review.json`, `math-review.json`, and `visual-layout.json`, then replay
preparation when source classification changes. Freeze reviewed unit IDs and
tokens before assigning translation. Require source-review pass, a reviewed
boundary, unique visual/caption inventory, and resolved math. Formula-only body
units need reclassification, never filler Chinese.

**Translate.** Preserve original front matter, visuals/captions, mathematics, and
tail; translate the reviewed main body (or requested range). Use the compact
task packets and atomic batch commands in
[translation-contract.md](references/translation-contract.md#batch-checkpoints).
Keep full protected/style tokens in exact order. One coordinator owns canonical
`translations.jsonl` and the within-paper glossary; workers return isolated
`id`/`translated_text` batches. Validate and merge each batch immediately. Preserve
source anomalies faithfully; do not silently repair the author's mathematics.

**Produce.** After the complete translation checkpoint passes, follow
[production.md](references/production.md) for export, rendering, strict/quick
validation, summary, and packaging. Freeze inputs while production runs. Resolve
all structural failures before handing a stable PDF to visual review.

**Inspect.** Follow [visual-review.md](references/visual-review.md). An independent
reviewer must open the contact sheet and **every final page image** at readable
resolution and compare complex content to the source. Collect a complete defect
list for the frozen candidate before repair. A changed PDF requires fresh full
review; byte-identical copies do not. Automated success never substitutes for
image inspection. Require the hash-bound visual report and checker to pass.

**Deliver.** Package only the exact accepted PDF and passing reports. Complete any
requested project links, then in-scope Zotero replacement and four-child
verification. Batch mode continues until its ledger audit is complete. Do not
commit, publish, or modify unrelated resources merely because translation ran.

## Keep work bounded and recoverable

- Follow the stage invalidation table in [production.md](references/production.md#repair-scope).
  Fix all known related defects together; rerun only affected stages. Change a
  shared helper only for a demonstrated general defect with a regression test.
- Read selected JSON fields and IDs, not whole records with span runs, HTML,
  coordinates, and repeated source text. Keep detailed logs on disk; return
  counts, paths, hashes, and actionable failures. Do not claim byte reductions
  are measured token or cost savings.
- Reuse agents whose role, independence, configured model, and effort still fit. Start
  dependent roles only when their inputs are ready. Use bounded waits and concise
  progress updates; do not repeatedly poll unchanged logs or ask agents to restate
  a completed handoff.
- Persist source/input hashes, role ownership, batch progress, accepted paths,
  unresolved findings, and the exact next action in the existing checkpoint.
  Model resolution and passed checks remain reusable while their inputs remain
  valid. Preserve prior failed review reports before overwriting previews.
- Continue through repair, review, packaging, and in-scope ingestion in the same
  task. Length, context pressure, and a saved checkpoint are not stopping points.
  Stop only for missing user decisions, source/access/capabilities, authorization,
  or unavailable external state; record the blocker and precise resume point.
