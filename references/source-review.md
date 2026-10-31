# Source review

## Contents

- Native text and image-only regions
- Gate and lifecycle
- Supported operations
- Safety and translation reconciliation

Use `source-review.json` for reviewed extraction corrections that must survive
reruns. Never create a paper-specific repair script when these operations can
express the correction.
Use [rendering-contract.md](rendering-contract.md) for the native-math and visual
preservation rules when deciding classifications and reviewed geometry.

Prefer one preparation pass followed by targeted inspection of reported pages.
Represent page- or paper-specific corrections with the operations below and rerun
preparation; do not repeatedly patch and execute temporary extractor copies. If a
failure is genuinely general and cannot be represented here, minimize it against
the skill script, add a regression test, fix the shared script once, and rerun only
the affected pipeline stage.

Triage unresolved math, flagged units, boundaries, and visual/caption associations
first. Inspect the exact source pages/crops and return proposed corrections by ID.
Do not retranscribe correct `auto_validated` TeX merely to relabel it
`manually_reviewed`. Automated status is not semantic evidence: when source
comparison reveals a systematic notation/extraction error, expand inspection to
all affected entries/pages. Keep correct reviewed entries unchanged and merge a
complete set of related source corrections before replaying preparation once.

For text inspection, select `id`, `page`, `kind`, `source_text`, `protected_text`,
and the relevant math entry; load `span_runs` or geometry only for the specific
reconstruction being diagnosed. Do not print the entire manifest or all hundreds
of math records. After replay, inspect the report delta and changed units instead
of rereading the complete accepted source inventory.

## Native text and image-only regions

Prepare with `--ocr off` by default and inspect native extraction diagnostics.
Use extracted PDF text for readable text layers; do not send the whole document
through visual OCR merely because a few pages are images. For hybrid PDFs,
render only the image-only or corrupt regions needed to recover missing text,
then use the model's own image-reading ability to transcribe the source language,
symbols and code faithfully. A traditional OCR engine or external OCR service
requires an explicit user request; image reading must not silently enable one.

Bind the transcription to the immutable source hash, page and region. Apply it
with supported `source-review.json` operations, including reviewed reading order,
and retain the inspected page/crop location. Rebuild extraction with the same
OCR policy, then review affected units/math before freezing packets. Do not put
Chinese translations into source corrections, guess unreadable symbols, or
replace correct native text with model guesses. Figures preserved as source
clips still need full bounds: include legends, labels and frame edges without
neighboring prose, and verify them in the final page images.

## Gate and lifecycle

Source structure is frozen before translation: `flow_role` is `paragraph`,
`list_item`, or `list_continuation`; list units carry `list_id`, zero-based
`list_level` and the original `source_marker` for new items. A paragraph after a
list has role `paragraph`; a wrapped explanation within an item is a
`list_continuation` with the same list ID/level. Review ambiguous multi-item PDF
blocks into separate units instead of asking the Chinese renderer to infer them.
`replace_unit` and `insert_unit` accept these fields. They are part of the
completed-translation contract.
`flow_reviewed: true` marks an explicit reviewed paragraph-flow decision. Without
`continuation_of`, it declares a new paragraph boundary. Automatically
annotated ordinary body blocks use false so existing source-only cross-page
continuation handling remains available; an extraction block is not by itself a
new paragraph. List roles always follow their source metadata.

For an explicitly reviewed continuation, keep `flow_role: "paragraph"` and set
`continuation_of: "<nearest preceding prose unit ID>"` on `replace_unit` or
`insert_unit`. This automatically sets `flow_reviewed: true`. The anchor may
itself be a continuation, forming a strictly backward chain; it must belong to
the same source section. Adjacent prose and cross-page continuation are allowed,
including when the preceding sentence ends with punctuation. Only independent
native displays and footnotes explicitly anchored within that same paragraph may
intervene. A heading, list, figure/caption, sidebar, or other unrelated prose
blocks the link. Place floating figures with captions and sidebars after their
complete logical paragraph rather than threading them through its displays.
An absent/null `continuation_of` on a reviewed paragraph means a new paragraph.
Never infer a post-display continuation from capitalization or punctuation.
The layout records `continuation_of`, the final `continuation_paragraph_id`, and
the ordered active equation source IDs in `continuation_across_display_ids`;
display ownership stays in its own reviewed math groups. PDF, Markdown and
strict validation consume this same source relationship.
With no intervening display, adjacent continuation text joins the anchor's
paragraph without a new line or paragraph gap; the placement's
`reviewed_continuations` map records each joined source relationship.

Read typed diagnostics in the source report: numbered displays embedded in prose,
uncovered mathematical spans, orphan scripts, ambiguous source alignment and
unresolved flow. Compare their source pages and resolve them with replayable
operations. A small Greek character can be a real variable, script or footnote;
never suppress it solely by character or font size. Do not promote an entire
mixed prose/equation block into a display.

`prepare_paper.py` creates this hash-bound skeleton in the work directory:

```json
{
  "schema_version": 1,
  "source_sha256": "<sha256>",
  "reviewed": false,
  "reason": "",
  "operations": []
}
```

Inspect the source preview, edit the operations, set `reviewed` to `true`, write
one overall reason, and rerun preparation. A reviewed file with a different
source hash is rejected. The operations are replayed against fresh extraction,
so reruns are idempotent.

Preparation writes `source-review-report.json`. Do not translate until its
`status` is `pass`. It reports formula-only body units, headings/equations
inside visual envelopes, and possible caption continuations.

## Operations

Every operation requires a non-empty `reason`.

Suppress extraction artifacts:

```json
{
  "op": "suppress_units",
  "ids": ["p0011-u0020", "p0011-u0021"],
  "grouped_into": "Figure 2",
  "reason": "Axis labels already preserved inside the vector figure."
}
```

Reclassify or restore one existing unit:

```json
{
  "op": "replace_unit",
  "id": "p0011-u0047",
  "kind": "caption",
  "render_mode": "text",
  "translatable": false,
  "source_text": "Figure 2. Exact source caption.",
  "reason": "The extractor split and misclassified the caption."
}
```

`replace_unit` may also set `page`, `bbox`, `span_runs`, and `section`. When
changing `source_text`, include reviewed `span_runs` if rich inline mathematics
must be retained.

Preserve a standalone code or pseudocode block with `kind: "code"`,
`render_mode: "source_clip"`, and `translatable: false`. Keep its exact source
text, line breaks and indentation in `source_text`, and review the complete
source `bbox`. Suppress duplicated extraction fragments separately. Code is a
captionless invariant object, not a figure or equation; it bypasses prose/math
reconstruction, stays a vector source clip in PDF, and exports as a fenced
Markdown block without a separate listing inventory. Both `replace_unit` and
`insert_unit` accept this representation.

Correct all affected source and protection metadata before translating those
units. If a later visual review reveals a defect in a completed unit, preserve
the old checkpoint, review the exact changed unit, and deliberately retranslate
only affected content; do not clear the whole checkpoint or falsify the source
metadata to get past the reconciliation guard.

Insert exact source prose swallowed by a visual detector:

```json
{
  "op": "insert_unit",
  "id": "review-p0011-u0001",
  "after_id": "p0011-u0047",
  "page": 11,
  "bbox": [72, 410, 520, 468],
  "kind": "body",
  "source_text": "Exact text verified in the source preview.",
  "translatable": true,
  "reason": "Recover prose visible below Figure 2."
}
```

Inserted IDs must match `review-pdddd-udddd`. Use source-page coordinates and
never reconstruct missing prose from memory.

Group disconnected vector fragments and bind one caption:

```json
{
  "op": "group_visual",
  "visual_id": "Figure 2",
  "anchor_id": "p0011-u0010",
  "member_ids": ["p0011-u0010", "p0011-u0011", "p0011-u0012"],
  "caption_id": "p0011-u0047",
  "source_bbox": [64, 92, 535, 392],
  "kind": "figure",
  "reason": "The source page contains one multi-panel vector figure."
}
```

Omit `source_bbox` to use the union of member bounds. Set `caption_text` only
when the exact source caption was verified. Each member may belong to only one
reviewed visual.

`caption_id` is required and must identify an existing caption, even when the
visual is unnumbered. For a genuinely captionless object, use `replace_unit` to
restore its kind and complete bounds, then suppress duplicated fragments; do not
pass an empty caption ID or manufacture a caption. Review chapter-specific
classification traps in [chapter-workflow.md](chapter-workflow.md).

## Safety

### Repairs after translation

Preview the complete reviewed proposal against an isolated preparation:

```powershell
uv run --script <skill-dir>\scripts\paper_repair.py `
  --manifest <active-work-dir>\manifest.json `
  --source-review <combined-reviewed-proposal.json> `
  --report <paper-root>\repair-impact.json
```

The default only reports impact and destroys the temporary probe. Inspect changed,
removed, new, and reopened completed IDs. Then run the same arguments with
`--apply --output-dir <new-revision-dir>` to publish an isolated revision. The
destination must not exist. The helper backs up the exact checkpoint, reopens
only changed source contracts, verifies unaffected parsed records, and updates
the single recovery pointer. Formatting bytes may change; do not label parsed
field equality as byte identity.

Review changed mathematics, rerun preflight, generate new packets and deliberately
translate only reopened/new prose. Retain the prior PDF and reports until the
new candidate passes every downstream gate. A failed preparation never modifies
the original checkpoint; never manually falsify source metadata or empty the
whole checkpoint to defeat its guard.

- Unknown IDs, duplicate operations, invalid page geometry, reused visual
  members, and operation conflicts are blocking errors.
- Preparation refuses to suppress or change a unit with a non-empty completed
  translation. Unchanged review operations may replay when the previous
  manifest and reviewed unit inventory belong to the same source hash. The
  complete replay must preserve each completed unit's source, protection,
  inline fragments, styles, geometry, and translatable status. Order and review
  explanations may change without invalidating completed content. Without a
  source-bound previous inventory, conservative operation-level guards apply.
- Review operations run against an isolated candidate inventory. A failed
  completed-unit check leaves the caller's inventory and translation checkpoint
  unchanged; it never skips an operation merely to make the check pass.
- When operations change the translatable inventory, preparation reconciles
  `translations.jsonl`: unchanged translations are retained, new units are
  empty, and completed orphan translations or changed source/protection/style
  metadata block the run.
- Reclassify formula-only body records as native equations or merge them with
  their real surrounding prose. Never add meaningless Chinese text merely to
  satisfy the renderer.
