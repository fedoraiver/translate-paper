# Reasoning effort by role

All subagents use the same latest verified general-purpose flagship model:
**GPT-6 Astra (`gpt-6-astra`)**, verified on 2026-09-23. Set reasoning effort
separately for each role. Do not change global Codex settings or claim to change
the already-running coordinator's model/effort. The following are task-specific starting choices for this
technical-paper workflow, not an officially benchmarked optimum.

## Role defaults

| Role | Default effort | When to change |
| --- | --- | --- |
| Translation | `high` | Protect technical meaning, qualifications, proof logic, and terminology. Use `medium` for clearly straightforward prose with an accepted glossary. Reserve `xhigh` for a bounded passage whose substantive ambiguity remains after source/context review. |
| PDF production | `low` | Running established export/render/validation scripts is mostly deterministic. Use `medium` to diagnose a new layout failure; use `high` for a reproduced general parser/renderer defect or interacting math/footnote layout errors. |
| Independent visual review | `medium` | Inspect every page for ordinary layout defects. Start at `high` for dense derivations, inference-rule diagrams, complex multi-column content, or difficult source/output correspondence; also escalate when reviewers disagree on a concrete finding. |
| Source and math review, if delegated | `high` | Resolve reading order, boundary classification, symbol identity, and native TeX from source evidence. Consider `xhigh` only for a localized unresolved issue after obtaining readable source crops. Running extraction/OCR alone remains `low` production work. |
| Summary, if delegated | `medium` | Faithfully compress the requested source. Use `high` when distinguishing subtle assumptions, contributions, and limitations across sections. |
| Metadata inspection, if delegated | `low` | Extract bibliographic fields and perform exact identity checks. Use `medium` for conflicting versions/years/identifiers; unresolved identity must be reported, not guessed. Zotero writes remain coordinator-only. |

Do not create extra agents just to populate this table. Use the existing role
ownership rules and available concurrency. A higher setting never relaxes
translation fidelity, exact-parent matching, or full visual page coverage.

These defaults favor accuracy for this repository's formula-heavy papers and
textbook chapters. Official Codex guidance starts Astra at `low`; the higher
levels above are a workflow-specific choice for semantic review and fidelity,
not a claim that Astra requires high effort for all work.

At the start of a new translation run, verify the latest appropriate flagship
against official OpenAI model documentation and the runtime's available model
IDs, then use that one explicit ID for every subagent. Reuse that resolution
throughout the run. The user's latest-model preference authorizes selecting a
verified successor in future runs, not guessing its name or using an unverified
`latest` alias. If the verified latest model is unavailable, report the capability
blocker instead of silently falling back to an older, cheaper, or inherited
model. Recheck supported efforts and representative quality when changing models.

## Dispatch and escalation

- Use the available runtime's actual effort parameter. For this session's
  `collaboration.spawn_agent`, pass `model: "gpt-6-astra"` (or the verified
  successor) and the role's `reasoning_effort`. Explicit overrides require
  `fork_turns: "none"` or a bounded
  positive turn count; `"all"`/omitted full-history forks do not accept overrides.
  With `"none"`, supply self-contained instructions: role, scope, source/manifest,
  relevant skill references, unit IDs/pages, glossary, exclusive output paths,
  and acceptance gates. Do not trade away required context for a lower effort.
- Example dispatch fields for routine production are `model: "gpt-6-astra"`,
  `reasoning_effort: "low"`, `fork_turns: "none"`, plus the complete task message.
  These are collaboration-tool arguments, not keys for `agents/openai.yaml`.
  Other Codex runtimes may use `model_reasoning_effort` in a custom agent config;
  follow their exposed schema instead of copying unsupported parameters.
- Verify the level is supported by the selected model and runtime. If an effort
  override cannot be applied but the required model can, disclose the actual
  inherited/default effort; never silently switch models or claim the intended
  level took effect. No default uses `none`,
  `max`, or `ultra`; these are not necessary for the routine work assigned here.
- Escalate only the difficult unit/page range or reproduced failure, one level
  at a time. First obtain missing context, improve source/crop legibility, and
  correct invalid inputs. More reasoning cannot recover absent evidence, fix
  an unavailable tool, or replace native PDF/font validation.
- A follow-up message asking an existing agent to "think harder" does not change
  its configured effort. This collaboration tool's follow-up API has no effort
  parameter: finish/checkpoint and stop the previous writer before assigning a
  replacement agent with the new level and explicit handoff. Keep visual review
  independent even when replacing the reviewer; transfer prior findings and
  page coverage without concurrent writes. Prefer a supported runtime setting
  update when another environment actually exposes one.
- Record role, agent ID, requested effort, confirmed effort if exposed, and any
  escalation reason in the existing assignment/checkpoint notes. Do not add a
  second translation-state ledger. Return to the default for subsequent routine
  assignments. Reuse agents when role/effort still fit.

## Evidence and limits

Official sources consulted 2026-09-23:

- [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra):
  supports `low`, `medium`, `high`, `xhigh`, and `max`; all default levels above
  are supported. Runtime tool availability still governs dispatch.
- [Reasoning models](https://developers.openai.com/api/docs/guides/reasoning):
  effort trades reasoning work against latency/token usage; supported levels
  vary by model, and `xhigh` should have a demonstrated benefit.
- [Codex subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents):
  agents can use separate reasoning settings; high effort fits complex logic
  and assumption checks, while low suits straightforward tasks. The runtime
  dispatch constraint above comes from this session's actual tool schema.
- [Images and vision](https://developers.openai.com/api/docs/guides/images-vision):
  small text benefits from enlargement and supported image-detail settings;
  visual interpretation still has limitations. Improve page/crop inputs before
  increasing effort for an unreadable formula or footnote.

No comparative translation benchmark was run to select these levels. When
tuning them, compare adjacent levels on the same source units/pages, glossary,
tools, and acceptance gates. Measure meaning/notation errors, real visual
defects missed, false alarms, repair rounds, elapsed time, and token usage when
available. Keep a lower setting only when acceptance quality is preserved.
Passing deterministic pipeline tests alone does not prove one effort is best.
