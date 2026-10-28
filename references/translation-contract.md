# Translation contract

## Contents

- Translation engine and content boundary
- Fidelity and persistent files
- Compact task packets and batch checkpoints
- Layout-specific rules (linked reference)

## Translation engine

Codex translation agents using the model and effort specified in
[reasoning-policy.md](reasoning-policy.md) perform translation decisions and write the Chinese prose. The coordinator integrates their batches;
an independent agent performs visual review. Follow
[agent-workflow.md](agent-workflow.md) for roles, handoffs, and single-writer rules.
Do not use translation APIs, web or browser translation,
Google Translate, Microsoft Translator, DeepL, local translation models,
or machine-translation packages. Delegation to these Codex subagents is required
when available; it does not authorize external translation services.

OCR, PDF parsing, layout analysis, font shaping, export, and validation are
allowed only as mechanical document-processing steps. OCR output is source
text, not a translation.

## Content boundary

The following is the default for a complete paper. For an explicitly requested
chapter or excerpt, use [chapter-workflow.md](chapter-workflow.md) to define the
range; do not invent Introduction/Conclusion sections or discard exercises.

Translate:

- the Introduction heading;
- section and subsection headings from Introduction through Conclusion;
- narrative body paragraphs in that range;
- when no Conclusion exists, the verified main body through its last section.

Keep verbatim:

- title, subtitle, authors, affiliations, addresses, dates;
- abstract, table of contents, classification codes, keywords;
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
- `source-review.json`: hash-bound, replayable extraction/visual corrections;
- `source-review-report.json`: applied operations and blocking extraction
  diagnostics;
- `translation-units.jsonl`: ordered source elements and protected text;
- `translations.template.jsonl`: empty translation checkpoints;
- `math-review.json`: hash-bound native TeX and review status for ambiguous
  inline fragments and every logical display equation;
- `visual-layout.json`: source-hash-bound figure/table inventory, complete
  source visual bounds, caption association, source and target typography,
  target dimensions, aspect ratio, scale basis, and automatic fallback state;
- `glossary.json`: persistent terminology map;
- source previews and layout-aware extracted text.

Initialize `translations.jsonl` from the template only if absent. Fill only
`translated_text` and save after every section. Export `中文翻译正文.md` from the completed JSONL so the
translation remains auditable independently of PDF layout.

Preserve `flow_role`, `list_id`, `list_level`, and `source_marker` from reviewed
source. A list item and its continuation are separate source units; do not invent
Chinese punctuation or extra breaks to compensate for ambiguous structure.
Resolve that ambiguity in source review before translation.

## Paper glossary

Before the first packet, a translation agent reads the introduction and key
definitions and proposes a small source-specific term map. The coordinator owns
`glossary.json`: a flat nonempty English-to-Chinese mapping, or an explicit
`{"terms": {}, "reviewed": true, "reason": "..."}` when no special terms apply.
Do not populate it with unrelated universal vocabulary.

Workers return proposed terms separately. Merge accepted terms before dispatching
dependent text. When an accepted term changes, inspect only units using that
term and submit deliberate token-preserving revisions. Do not globally replace
provider verbs merely because a formal noun uses a preferred translation.

Section saves are crash- and compaction-recovery checkpoints, not planned pause
points. Validate the just-written records and continue with the next untranslated
unit in the same turn. A partial final response is permitted only under the true
blocking conditions in `SKILL.md`; remaining length or context pressure alone is
not a blocker.

Use [source-review.md](source-review.md) when a visual detector swallows prose,
splits a multi-panel figure, leaves plot labels as headings/equations, or
misclassifies a caption. Do not replace this persistent review with a
paper-specific repair script.

### Batch checkpoints

Generate a compact assignment rather than dumping the full unit/translation JSONL:

First require a current passing `paper-preflight.json`; packet creation refuses
missing, failed, or stale readiness. See production.md for the command.

```powershell
uv run --script <skill-dir>\scripts\translation_context.py `
  --manifest <work-dir>\manifest.json `
  --translations <work-dir>\translations.jsonl `
  --output <work-dir>\agent-work\translation\batch-01.context.json `
  --max-words 3000
```

The read-only helper verifies source/checkpoint identity and selects a contiguous
batch from the first untranslated unit. It preserves exact protected text,
token meanings, reviewed TeX, styles, nearby source context, and interleaved
invariant equations/captions; it omits redundant span runs, HTML, and geometry.
It records input hashes and source/preview/glossary paths. Check the packet's
actual `assigned_ids`; `context_only` and `invariants_context_only` are reading
context, never translation assignments. Inspect source images when compact text
cannot settle a symbol or relationship. Compaction is not source review.

Use `--start-id` and inclusive `--end-id` for disjoint section assignments. The
word cap can stop before the requested end; never assume it assigned that entire
range. A single oversized unit is kept intact and flagged for bounded handling.
`--include-completed` is only for deliberate review of an existing translation,
not fresh retranslation. Existing packet files are not overwritten: use a new
revision name after accepted input changes. The coordinator compares recorded
input hashes before accepting work if its source/math/glossary changed meanwhile.

Prefer about 2,000–3,000 source words and at most about 4,000 per batch, with
section-aware ranges when practical. Agent concurrency does not enlarge the
per-agent context budget. Return only the batch path, assigned count, and real
ambiguities; do not paste completed translations into the conversation.

Write a batch as UTF-8 JSONL with exactly `id` and `translated_text` per record,
using full tokens copied from `protected_text`. No TSV shorthand, token aliases,
or translation produced by mechanical scripts/services. Save one batch with:

```powershell
uv run --script <skill-dir>\scripts\translation_checkpoint.py `
  --manifest <work-dir>\manifest.json `
  --translations <work-dir>\translations.jsonl `
  --batch <work-dir>\batch-01.jsonl `
  --packet <work-dir>\agent-work\translation\batch-01.context.json
```

The helper checks source hash, unique IDs, current source metadata, Chinese prose,
and the exact protected/style-token sequence before any write. It retains all
other records, atomically replaces the checkpoint, and reports completed count,
remaining count, and `next_id`. Replaying identical text is harmless; changing
completed text requires `--replace-completed` for a deliberate revision. Use one
writer per checkpoint. Keep revision batches narrowly scoped.

Newly prepared runs require `--packet` for batch writes. Its assignment identity
binds assigned source/flow/style/protection metadata, associated reviewed math,
and source-relevant glossary terms. Unrelated batches, manifest annotations and
unrelated terms do not invalidate it. Changed assigned inputs require a new
packet and source comparison; never edit the stored identity to pass a stale
batch. Each batch contains exactly its assigned IDs in order. Legacy checkpoints
remain readable.

Omit `--batch` for a read-only resume check. Add `--require-complete` before
export. A partial checkpoint can pass structural checks with `complete: false`;
continue from `next_id`. This helper does not certify translation quality or
replace source, math, PDF, or summary validation. A stale source record must go
through source-review reconciliation, not have its metadata silently overwritten.

## Layout-specific rules

For source classification, mathematical reconstruction, typography, and PDF
composition, read [rendering-contract.md](rendering-contract.md). Translation
workers need those details only when an assigned ambiguity touches layout or
source reconstruction; do not load renderer mechanics for ordinary prose.
