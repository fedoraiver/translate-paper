# Batch workflow

## Contents

- Ledger schema and paths
- Zotero audit and resume commands
- Packaging, recording, and final audit

Use a batch ledger when the request names a paper list, asks for Zotero items
without translations, or resumes a long multi-paper run.

The ledger makes a run recoverable; it does not schedule one paper or stage per
conversation turn. In one continuous run, repeat `next -> single-paper gates ->
package -> Zotero verify/record -> next` until `next` reports `complete`. Persist
each transition and emit progress updates, but do not yield for a manual
"continue" between papers or stages. Stop only for a blocking condition defined
by the completion and continuity contract in `SKILL.md`.

## Ledger

Create `batch-run.json` beside the project artifacts:

```json
{
  "schema_version": 1,
  "batch_id": "classic-protocols",
  "source_list": {
    "path": "zero-knowledge-proof-resources-summary.pdf",
    "sha256": "<optional until first refresh>",
    "section": "零知识证明协议的经典论文"
  },
  "zotero_target": {"id": "C123", "name": "未读论文"},
  "output_root": "output/pdf",
  "papers": [
    {
      "order": 1,
      "slug": "01-gmr89",
      "title": "The Knowledge Complexity of Interactive Proof Systems",
      "year": "1989",
      "identity": {"doi": "10.1137/0218012"},
      "source_pdf": "sources/gmr89.pdf",
      "work_dir": "tmp/pdfs/01-gmr89",
      "final_dir": "translations/papers/classic-protocols/01-gmr89",
      "stage": "queued"
    }
  ]
}
```

Paths are relative to the ledger unless absolute. `final_dir` is optional and
defaults to `<output_root>/<slug>`. Use `output/pdf` as the default output root;
honor a project-provided archive directory.

Keep Zotero enabled when allowed by the request and project instructions. For a
local-only request or project, set
`"zotero_enabled": false`; only then may `zotero_target` be omitted and the
terminal local stage be `packaged`.

Each paper must have a stable order, slug, title, source PDF, work directory,
and DOI, arXiv ID, or title plus year. Use an `artifacts` object to override
staging paths for `translated_pdf`, `translated_markdown`, `summary`,
`metadata`, `layout_report`, `strict_validation`, `quick_validation`,
`summary_validation`, `visual_review`, or `source_review_report`.

## Commands

Before translating, audit the exact local Zotero parents. Skip this command for
local-only batches:

```powershell
python <skill-dir>\scripts\batch_workflow.py audit-zotero `
  --ledger <project>\batch-run.json
```

Items with exactly one complete parent and four required children become
`skipped_existing`. Missing items remain queued. Multiple exact parents or an
incomplete matched parent are blocking results.

Resume from the first non-terminal paper:

```powershell
python <skill-dir>\scripts\batch_workflow.py next `
  --ledger <project>\batch-run.json
```

The script derives stages from source hashes and artifacts:

`queued -> prepared -> translated -> strict_validated -> packaged -> zotero_verified`

A changed source hash resets only that paper. The terminal states are
`zotero_verified` and `skipped_existing`, or `packaged` for local-only batches.

After strict, quick, summary, and independent visual review pass, package one
paper. The default visual report is `<work-dir>/visual-review.json`; packaging
and final-bundle audit require complete page coverage and matching source/output
PDF hashes. A missing or stale visual report is not a reason to retranslate a
completed paper: review the existing valid PDF and resume packaging. Do not
fabricate a report for an older bundle that lacks review evidence.

```powershell
python <skill-dir>\scripts\batch_workflow.py package `
  --ledger <project>\batch-run.json `
  --slug <paper-slug>
```

Packaging writes only the standard artifact names. It refuses to replace a
different source PDF and verifies the copied bundle.

After `zotero_ingest.py verify` succeeds, record the exact parent:

```powershell
python <skill-dir>\scripts\batch_workflow.py record-zotero `
  --ledger <project>\batch-run.json `
  --slug <paper-slug> `
  --parent-key <PARENT_KEY>
```

This command repeats live local verification and records the key only when the
parent has exactly the four required children.

Finish with a full audit:

```powershell
python <skill-dir>\scripts\batch_workflow.py audit `
  --ledger <project>\batch-run.json `
  --zotero
```

For local-only batches, omit `--zotero`, skip `record-zotero`, and continue
directly from `package` to `next`. Require a passing `batch-audit.json`.
Never restart a completed paper merely
because the conversation was compacted or resumed.
