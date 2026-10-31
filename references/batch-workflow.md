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
  "zotero_target": {"id": "user:0/collections/ABCD1234", "name": "未读论文"},
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
staging paths for `translated_pdf`, `latex_source_archive`,
`metadata`, `layout_report`, `strict_validation`, `quick_validation`,
`visual_review`, or `source_review_report`.

## Commands

Before translating, audit the exact local Zotero parents. Skip this command for
local-only batches:

```powershell
uv run --script <skill-dir>\scripts\batch_workflow.py audit-zotero `
  --ledger <project>\batch-run.json
```

The audit resolves the stable target, reads its actual library through Pyzotero,
and checks the parent's real collection membership. Legacy C/L IDs are accepted
only after a unique full-hierarchy conversion; prefer stable IDs in new ledgers.

Papers with exactly one complete parent and the three required children (original
paper PDF, Chinese translation PDF and LaTeX source ZIP) become
`skipped_existing`. Missing items remain queued. Multiple exact parents or an
incomplete matched parent are blocking results.
Extra local original PDF copies, translation Markdown and summaries are not
generated or packaged. The paper's immutable source input is also required in
Zotero and must match the ZIP's source identity. Books keep their full PDF and
do not need chapter-original attachments. An explicitly requested cleanup uses
the separate guarded flow and preserves paper originals.

Resume from the first non-terminal paper:

```powershell
uv run --script <skill-dir>\scripts\batch_workflow.py next `
  --ledger <project>\batch-run.json
```

The script derives stages from source hashes and artifacts:

`queued -> prepared -> translated -> strict_validated -> packaged -> zotero_verified`

A changed source hash resets only that paper. The terminal states are
`zotero_verified` and `skipped_existing`, or `packaged` for local-only batches.

After strict, quick, and independent visual review pass, package one
paper. The default visual report is `<work-dir>/visual-review.json`; packaging
and final-bundle audit require complete page coverage and matching source/output
PDF hashes. A missing or stale visual report is not a reason to retranslate a
completed paper: review the existing valid PDF and resume packaging. Do not
fabricate a report for an older bundle that lacks review evidence.

```powershell
uv run --script <skill-dir>\scripts\batch_workflow.py package `
  --ledger <project>\batch-run.json `
  --slug <paper-slug>
```

Packaging writes only the standard artifact names and verifies the copied bundle
against the original source input without making a source PDF copy.
The required `latex_source_archive` defaults to `<work-dir>/LaTeX源码包.zip`;
override it when using a staging directory. Generate the rebuildable source ZIP
using [production.md](production.md#rebuildable-latex-source). Package and final
audit validate every ZIP member hash and match its source/accepted-PDF identity.

After `zotero_ingest.py verify` succeeds, record the exact parent:

```powershell
uv run --script <skill-dir>\scripts\batch_workflow.py record-zotero `
  --ledger <project>\batch-run.json `
  --slug <paper-slug> `
  --parent-key <PARENT_KEY>
```

This command repeats live local verification and records the key only when the
paper parent has the three required delivery children, including the stored source ZIP
whose manifest matches the accepted translation PDF. The complete local bundle
also validates the ZIP against the original source PDF. Pass that original input
to paper ingestion with `--source-pdf`; no extra local source copy is needed.

Finish with a full audit:

```powershell
uv run --script <skill-dir>\scripts\batch_workflow.py audit `
  --ledger <project>\batch-run.json `
  --zotero
```

For local-only batches, omit `--zotero`, skip `record-zotero`, and continue
directly from `package` to `next`. Require a passing `batch-audit.json`.
Never restart a completed paper merely
because the conversation was compacted or resumed.
