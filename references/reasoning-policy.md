# Model and reasoning effort by role

Select the model and reasoning effort explicitly for each subagent using the
policy below. These are workflow-specific starting choices, not benchmarked
optima. The coordinator retains the current conversation's model and effort;
do not change global Codex settings or claim to change the running coordinator.

## Role defaults

| Role | Default | When to change |
| --- | --- | --- |
| Translation | `gpt-6.1-terra` / `high` | Use Terra / `medium` for clear, straightforward prose with an accepted glossary. Assign core passages involving complex definitions, quantifier scope, proofs, or safety guarantees to `gpt-6.1-sol` / `high`; use Sol / `xhigh` for localized ambiguity that remains after source and context review. |
| PDF production | `gpt-6.1-terra` / `low` | Use Terra / `medium` for new layouts or local typesetting adjustments. Assign complex generator changes or cross-file diagnosis to Sol / `high`. |
| Independent visual review | `gpt-6.1-terra` / `medium` | Use Terra / `high` for dense formulas, multiple columns, complex tables, or small superscripts and subscripts. Refer questions about formula meaning or translation correctness to source and math review, supplying the source location and specific concern. |
| Source and math review, if delegated | `gpt-6.1-sol` / `high` | Use Sol / `xhigh` for localized unresolved questions about quantifiers, conditions, symbol dependencies, or proof continuity after obtaining readable source evidence. Provide the source location and specific concern. Extraction/OCR alone is production work. |
| Summary, if delegated | `gpt-6.1-terra` / `medium` | Use Sol / `high` when judging contribution boundaries, comparing technical assumptions, or explaining subtle limitations. |
| Metadata inspection, if delegated | `gpt-6.1-terra` / `low` | Use Terra / `medium` for conflicts in authors, years, versions, DOI, or venue; check original sources and report identity as unresolved when evidence is insufficient. Zotero writes remain coordinator-only. |

Do not create extra agents merely to populate this table. A higher setting never
relaxes translation fidelity, exact-parent matching, or full visual page coverage.

At the start of a translation run, verify that the models and role-specific
efforts needed are available in the runtime. Use their explicit IDs; do not
follow a `latest` alias or silently substitute another model. If a required model
or effort is unavailable, report the capability blocker and the actual setting if
known; do not claim the requested setting took effect. An explicit user request
to change models takes precedence; check compatibility and representative quality.

## Dispatch and escalation

- Use the runtime's actual model and effort parameters. For this session's
  `collaboration.spawn_agent`, pass the selected `model` and `reasoning_effort`.
  Explicit overrides require `fork_turns: "none"`; full-history forks do not
  accept overrides. Supply a self-contained assignment with role, scope, source
  and manifest, relevant skill references, units or pages, glossary, exclusive
  output paths, and acceptance gates.
- For routine PDF production, use `model: "gpt-6.1-terra"`,
  `reasoning_effort: "low"`, and `fork_turns: "none"`. Other runtimes may use
  different configuration fields; follow the exposed schema.
- Record role, agent ID, requested and confirmed model and effort where exposed
  (otherwise `unverified`), and the reason for a configuration change in the
  existing assignment or checkpoint. Do not add a second translation-state ledger.
- Limit escalation or model switching to the difficult unit, page range, or
  reproduced failure. First obtain missing context and readable source crops;
  extra reasoning cannot recover absent evidence or replace validation.
- A follow-up message cannot change an existing agent's configured model or
  effort in this collaboration tool. Finish and checkpoint the previous writer
  before assigning a replacement with the new configuration and explicit handoff.
  Keep visual review independent; transfer findings and coverage without
  concurrent writes. Use a supported runtime update if another environment
  exposes one. Return to defaults for subsequent routine assignments and reuse
  agents only when role, independence, model, and effort still fit.

## Evidence and limits

These choices are not supported by a comparative translation benchmark. When
tuning them, compare configurations on the same source units, glossary, tools,
and acceptance gates; measure meaning and notation errors, visual defects,
repair rounds, elapsed time, and token usage when available. Passing automated
pipeline tests alone does not establish translation or visual-review quality.
