# Source review

## Contents

- Gate and lifecycle
- Supported operations
- Safety and translation reconciliation

Use `source-review.json` for reviewed extraction corrections that must survive
reruns. Never create a paper-specific repair script when these operations can
express the correction.

Prefer one preparation pass followed by targeted inspection of reported pages.
Represent page- or paper-specific corrections with the operations below and rerun
preparation; do not repeatedly patch and execute temporary extractor copies. If a
failure is genuinely general and cannot be represented here, minimize it against
the skill script, add a regression test, fix the shared script once, and rerun only
the affected pipeline stage.

## Gate and lifecycle

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

## Safety

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
