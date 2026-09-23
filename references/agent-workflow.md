# Agent workflow

Use separate task-scoped subagents for translation, PDF production, and visual
review. Explicitly select the same latest verified flagship model for all roles,
currently `gpt-6-astra`, and vary only reasoning effort using
[reasoning-policy.md](reasoning-policy.md).
Use collaboration/subagent tools within the current task, not new
user-facing tasks or chats. Translation APIs and other translation engines remain
forbidden.

## Ownership and handoffs

The coordinator assigns bounded work, supplies the relevant skill references,
and remains responsible for completion. Each assignment names the source PDF
and SHA-256, manifest, exact unit IDs or page range, required inputs, exclusive
output paths, and acceptance criteria. Return artifact paths, validation
results, unresolved findings, and the exact resume point rather than only a
message saying the work is done.

Pass the selected effort in the actual spawn/configuration field, not just in
the task prose. Record requested and confirmed effort (or `unverified` if the
tool does not expose it) with the assignment. Read the runtime-specific dispatch
rules in [reasoning-policy.md](reasoning-policy.md#dispatch-and-escalation).

| Role | Work and exclusive outputs |
| --- | --- |
| Coordinator | Own `translations.jsonl`, `glossary.json`, `boundary-review.json`, `source-review.json`, `math-review.json`, `visual-layout.json`, the batch ledger, final packaging, project links, and all Zotero mutations. Validate and merge proposed changes serially; the independent reviewer alone authors `visual-review.json`. |
| Translation agent | Translate assigned units from `protected_text`; write independent `agent-work/translation/<batch-id>.jsonl` batches and proposed glossary changes. Never overwrite the canonical checkpoint or another agent's batch. |
| PDF production agent | Run deterministic export, rendering, and structural validation against a frozen input revision. Exclusively own the assigned staging PDF, generated layout/validation files, and preview directory while production runs. Return proposed source/math/layout corrections for the coordinator to apply. |
| Visual review agent | Independently inspect the completed Chinese PDF's actual rendered page images. Own the visual review report and any review-only crops in its assigned directory; do not modify the PDF, translations, or shared review files. |

Keep the visual reviewer distinct from the agents that translated and produced
the PDF. Reuse the translation agent for successive batches and the reviewer
for successive review rounds. A source-review or summary assignment may use an
additional agent when it has useful independent work and a free slot; do not
create idle agents merely to fill a role.

The coordinator merges each completed translation batch with
`translation_checkpoint.py`, checks the resulting report, and resolves glossary
conflicts before assigning dependent text. Keep batch size and fidelity rules
from [translation-contract.md](translation-contract.md). Agent availability does
not permit concurrent writes to a canonical checkpoint or shared review file.

## Dependencies and safe parallel work

1. Resolve the source and content boundary, then freeze the reviewed source
   units and protected tokens before assigning translation. Source-review
   inspection can run alongside independent metadata inspection; their proposed
   corrections still merge through the coordinator.
2. Assign one translation agent by default. For a large document, independent
   sections may use separate agents only with disjoint unit IDs, separate batch
   files, and the same accepted glossary. Merge in document order. Summary
   drafting from the reviewed source may run alongside translation.
3. Start the final export only after the canonical checkpoint passes
   `--require-complete` and source/math gates pass. The coordinator must not edit
   its inputs while PDF production is running. Different papers may run in
   parallel only in disjoint work and staging directories; preserve ledger
   ordering and let the coordinator alone update shared batch state.
4. Hand the completed, stable PDF and its SHA-256 to the visual reviewer after
   structural checks pass. While review runs, independent summary validation or
   metadata work may continue. No renderer may overwrite the PDF or its page
   previews during review.
5. Package, update downstream links, and ingest only after both structural and
   visual gates pass for the exact PDF being delivered. Copying that PDF must
   preserve its bytes; recompute its hash at the final path before accepting the
   report. A different PDF requires a new review.

## Independent visual acceptance

Follow [visual-review.md](visual-review.md) for the visual review procedure and
report format. The reviewer must actually open every page image at readable
resolution, including plain text pages and preserved front/tail pages. A contact
sheet is an overview, not evidence that individual pages have been checked.
Inspect detailed crops where full-page viewing is insufficient and compare
source visuals, equations, and boundary material with their output placements.

The report must identify the reviewed PDF path, SHA-256 and page count, reviewer,
and per-page evidence: image paths actually opened, observed result, concrete
findings or a concise pass observation, and unresolved defects. Do not infer a
visual pass from text extraction, script success, filenames, or an agent's bare
claim. The coordinator checks complete page coverage and the artifact hash
before accepting the report.

Send defects to the responsible agent. The coordinator applies accepted shared
input corrections, then gives the production agent exclusive access to rerender
and rerun affected checks. Return the new PDF to the same independent reviewer.
For every changed PDF, review all final pages again and record closure evidence
for each earlier defect; pagination changes can move defects to other pages.
Never carry forward a visual pass solely because the source hash is unchanged.

## Continuity and unavailable tools

Wait for assigned work and continue through merge, render, review, repair,
packaging, and in-scope Zotero verification without asking the user to resume.
Before compaction, persist role ownership, input revisions, accepted artifact
paths, unresolved findings, and next work in the existing paper checkpoint;
resume from those artifacts rather than repeating completed translation.

If subagent tools are unavailable or fail after a bounded recovery attempt, the
coordinator may perform the roles sequentially. Disclose the loss of independent
review in the visual report and final response; never describe self-review as
independent. Limited concurrency is not unavailability: queue dependent work or
release finished agents. If actual page-image viewing is unavailable, save a
blocked visual review and report the missing capability and resume point. Do
not silently skip visual inspection or deliver an unreviewed PDF as accepted.
