# Zotero ingestion through Pyzotero

## Responsibilities and readiness

All project API operations use the pinned `pyzotero==1.16.0` Python SDK through
`zotero_client.py`. Reads, parent creation, attachment metadata and file uploads,
updates, guarded deletion, and verification use Zotero Desktop 10+ on the local
API. No cloud sync or Web API credential is required for normal delivery.
The independent Zotero plugin is unchanged and is used only to enable/probe or
restart Desktop when needed. Older plugin statements that the Local API is
read-only do not describe Zotero 10.

Do not use computer-use, edit `zotero.sqlite`, or write to Zotero storage. Only
`/connector/getSelectedCollection` remains as a small read-only bridge for the
selected Desktop destination and edit permissions, which the SDK does not expose.
No Connector save session is created by new deliveries.
The SDK client mounts a small read-only file-transport extension for local
attachment redirects: httpx2 can inherit 127.0.0.1 when resolving file:/// URLs,
which Pyzotero's stock transport rejects. It reuses Pyzotero's file reader,
normalizes loopback hosts, and keeps non-local paths rejected. No upstream
package is patched.

```powershell
uv run --script <skill-dir>\scripts\zotero_ingest.py status
uv run --script <skill-dir>\scripts\zotero_ingest.py targets
```

If Desktop is unavailable or its local API is disabled, use the independent
Zotero skill's `status --json` / `enable --restart` helper. Do not enable it by
editing the database. Read-only status reports whether a credential is stored;
it does not claim that a stored key was validated by a write.

## Local authorization

```powershell
uv run --script <skill-dir>\scripts\zotero_ingest.py authorize --timeout-seconds 3600
```

The SDK requests permission with application name `translate-paper`. The user
operates Zotero's native dialog. Keep one request active and let it receive the
response; the process and HTTP request must remain alive. Denial, expiration,
invalid keys, and rate limits are actionable failures, not a reason to loop.

Only an actual `remember: true` response is stored, under
`Codex/translate-paper/ZoteroLocalAPI/<server-id>` in Windows Credential Manager.
A one-time grant is not saved and cannot support a later multi-write command;
choose Always Allow for durable batch delivery. Reads never authorize. Missing
or expired grants leave the delivery receipt intact for a later `resume`.
Never print keys, put them in command arguments/workspace receipts, or recover
keys from Zotero profile preferences. Web keys and local keys are separate.

## Destination and identity

`targets` returns canonical IDs: `user:0`, `user:0/collections/<key>`,
`group:<id>`, and `group:<id>/collections/<key>`. Honor the user's explicit
library/collection; otherwise use the currently selected writable destination,
or the only writable destination when there is exactly one. Ask only if the
intended destination cannot be resolved. Pass its exact ID/name to delivery.

Legacy `C123` / `L1` are Desktop internal IDs, not API keys. The bridge's complete
library/collection hierarchy is matched against SDK data. A legacy mapping must
be unique; same-name paths, ID/name mismatches and unprovable file permissions
block writing. Canonical collection keys disambiguate collections with identical
names when library permissions can be established. Re-resolve the target at
actual execution, not from a pre-translation UI snapshot. Completion checks the
parent's actual library and collection keys, never just the selected UI target.

Keep original-language bibliographic metadata in `zotero-item.json`: `itemType`,
`title`, `creators`, and DOI, arXiv ID or title plus year. Keep venue, date, pages,
URL and other original fields. `_sourceParentKey` may be set only when the source
was obtained from that exact parent; it requires the exact normalized title and
no DOI/arXiv/year conflict. Workflow fields are stripped before SDK creation;
`arXivID` is preserved as an arXiv line in Extra.

```powershell
uv run --script <skill-dir>\scripts\zotero_ingest.py search --metadata <metadata.json>
# Explicit comparison of synced personal-library state, only if needed:
uv run --script <skill-dir>\scripts\zotero_ingest.py search --metadata <metadata.json> --include-web
```

Search and children are fully paginated, with duplicate/repeated-page checks.
Matching uses exact normalized DOI, exact normalized arXiv, title plus year, or
the guarded recorded source key. Conflicting identifiers never fall back to
similar titles; multiple exact parents block replacement.

Web comparison and `zotero_credentials.py verify` also use the SDK. The Web
credential namespace remains `Codex/translate-paper/ZoteroAPI`, with legacy
`Codex/translate-paper-to-zotero/ZoteroAPI` fallback. `ZOTERO_USER_ID` and
`ZOTERO_API_KEY` must be supplied together if used for explicit Web comparison.
The global `--credential-target` option changes only this Web namespace.

## New-parent delivery and guarded replacement

For papers, `ingest` creates one parent and three stored `imported_file`
attachments: `原文 PDF`, `中文翻译 PDF`, and `LaTeX 源码包`. For chapters under
a book that already has its full PDF, preserve that original full-book attachment
and deliver only the chapter translation PDF and source ZIP. Do not upload a
redundant chapter-original excerpt, translation Markdown or summary, or create notes.
Existing exact parents are refused; use `attach` for preserving an existing one.

```powershell
uv run --script <skill-dir>\scripts\zotero_ingest.py ingest `
  --metadata <output-dir>\zotero-item.json `
  --source-pdf <original-paper.pdf> `
  --translated-pdf <translated.pdf> `
  --latex-source-archive <output-dir>\LaTeX源码包.zip `
  --target-id user:0/collections/ABCD1234 --target-name "未读论文" `
  --receipt <work-dir>\zotero-upload.json
```

This previews only. Add `--yes-ingest` to execute an authorized delivery. A
receipt is always written before creation: absent `--receipt`, its default is
`zotero-upload.json` beside the metadata. An existing receipt is refused; use
`resume`. SDK creation uses preallocated legal keys and version zero; confirmed
keys survive missing responses and process interruptions. SDK attachment upload
uses explicit dictionaries rather than the attachment-template convenience
methods unavailable locally. Files are uploaded serially; Pyzotero 1.16.0 reads
each whole file into memory. No custom streaming protocol or silent alternative
write route is retained.

`--translated-pdf` and `--latex-source-archive` are required. Papers also require
`--source-pdf`: it checks archive source identity and uploads the original paper.
For book metadata, `--source-pdf` is an optional local
identity check only; it never uploads the excerpt. Legacy `--translated-markdown` and `--summary`
arguments are accepted but neither read nor uploaded. Local packaging requires
the PDF, source ZIP and supporting metadata/reports in [production.md](production.md).

For an explicitly authorized replacement, preserve original source and metadata,
prepare and accept the new bundle first, then revalidate and preview deletion:

```powershell
uv run --script <skill-dir>\scripts\zotero_ingest.py delete `
  --metadata <metadata.json> --item-key <PARENT_KEY>
```

Invoking `$translate-paper` remains standing authorization to replace one unique
guarded match; do not ask again for that operation. Add `--yes-delete` only for
that authorized deletion. It permanently deletes the one parent and its recorded
children through the SDK, with a local library-version precondition and a limit
of 50 objects. No chunking or cloud-sync wait is introduced. `--wait-local-seconds`
is accepted for CLI compatibility but no longer controls a sync wait. For group
search/delete/verification use `--library-type group --library-id <group-id>`.
An explicit existing-parent chapter request never authorizes its replacement.

## Existing parents and recovery

Use implemented `attach` for existing parents and chapters; see
[existing-parent delivery](zotero-existing-parent.md). It shares the two delivery file
flags with `ingest`, requires `--parent-key` plus that parent's metadata and exact
destination, and uses `--title-prefix` to separate chapter attachment titles.

```powershell
uv run --script <skill-dir>\scripts\zotero_ingest.py resume `
  --receipt <work-dir>\zotero-upload.json
# Execute after reviewing the saved/live state:
uv run --script <skill-dir>\scripts\zotero_ingest.py resume `
  --receipt <work-dir>\zotero-upload.json --yes-resume
```

Version 2 receipts bind server ID, library/collection, parent and child keys,
local input paths/hashes, owned titles, original unrelated children and confirmed
stages. Resume validates the accepted files and live parent before any writes.
Matching stored bytes are adopted/skipped; confirmed metadata without a file is
uploaded to the same key. A duplicate title, wrong parent/key, changed input,
conflicting stored bytes, disappeared confirmed item, or changed server blocks
writing and preserves evidence. A timeout never proves a POST failed. A completed
delivery is reverified and reported complete without requiring write credentials.

When resuming an old five-file paper receipt, keep the original PDF, Chinese PDF
and source ZIP eligible for upload. Move only Markdown and summary records/hashes
into `local_only_files` / `local_only_hashes`. For a chapter, exclude the original
excerpt as well. Excluded paths need not exist. Preview leaves the receipt
unchanged; an executed resume saves this narrowed scope and retained evidence.
These are historical receipt fields, not instructions to save or regenerate
document copies.
Already stored local-only attachments are left in place and are not required by
verification. This policy change does not authorize deleting existing attachments.

Legacy version 1 Connector receipts are recovered into a separate
`<receipt-stem>.pyzotero.json`; the original remains unchanged. A saved parent key,
or an explicitly supplied `resume --parent-key`, must match the original identity
and destination. Missing/unprovable parent ownership blocks conversion, rather
than creating another parent. See [historical Connector recovery](zotero-connector.md).

## Verification and batch completion

```powershell
uv run --script <skill-dir>\scripts\zotero_ingest.py verify `
  --metadata <metadata.json> --expected-dir <output-dir> `
  --target-id user:0/collections/ABCD1234
```

Verify exact titles/types, stored modes, actual destination, and file bytes read
through the SDK into temporary verification copies. The Chinese PDF uses
`application/pdf`; the source uses `.zip` and `application/zip`. Validate the
stored ZIP against the stored Chinese PDF and compare SHA-256/MD5/size identities
to accepted inputs. For papers, require the stored original PDF and validate it
against the ZIP's source identity too. `--source-pdf` can verify the original's
accepted bytes without making an extra copy in the output directory.
A missing source ZIP is incomplete; adding it does not invalidate a prior
accepted PDF review. `--expected-dir` uses the standard packaged filenames.

Use `verify --parent-key <key> --title-prefix "第 1 章"` for a chapter's owned
two-file subset; unrelated children are preserved. Batch audits resolve the
actual target library and require actual collection membership before recording
`skipped_existing` or `zotero_verified`. Keep local acceptance and Zotero delivery
stages separate. See [batch workflow](batch-workflow.md).

Do not generate a summary or translation Markdown, or archive a standalone
original PDF copy. Existing Zotero summaries, original PDFs and translation
Markdown are preserved unless the user separately requests a specific cleanup.

## Explicit historical attachment cleanup

Use this flow only when the user requests removal of earlier skill-generated
attachments. It is separate from normal delivery and never replaces a parent.
Resolve the exact parents from the library and recorded deliveries, then call
`prepare_cleanup_manifest(client, parent_keys)` and save its result with
`_write_upload_receipt`. The manifest records metadata and byte identities, not
document backups. Save document copies only if the user asks for them.

```powershell
uv run --script <skill-dir>\scripts\zotero_ingest.py cleanup --manifest <cleanup.json>
uv run --script <skill-dir>\scripts\zotero_ingest.py cleanup --manifest <cleanup.json> --yes-cleanup
```

The preview validates the entire manifest without authorizing or deleting.
`--yes-cleanup` permanently deletes only recorded translation Markdown, summaries,
and redundant chapter-original excerpts under books with full PDFs. Paper
originals are never cleanup candidates. Preserve paper originals, full-book originals,
translation PDFs, source ZIPs, parent metadata, collections, unrelated notes and
annotations. A candidate with child notes/annotations or changed bytes blocks
cleanup. Deletion uses the pinned SDK, library-version preconditions and batches
of at most 50 attachments. Confirm absence after every batch and checkpoint
before and after writes. Reconcile an uncertain response against live keys before
retrying; an already completed manifest verifies preservation without new writes.

Search for missing existing source ZIPs only in the user-specified directories.
Verify ZIP membership hashes and its accepted-PDF identity before any upload.
When the user requests existing ZIPs only, report missing sources without
repackaging or retranslating. Cleanup completion and delivery completeness
are separate outcomes; a missing ZIP does not prevent separately authorized
attachment cleanup.

### Restore a missing paper original

Use `attach-source` to repair an existing paper without requiring or regenerating
a missing ZIP. It cannot target books. Resolve the original from known paths or
the publisher/author; prefer byte-identical recovery using the recorded SHA-256.
It preserves the exact parent and all other children, checkpoints a reserved key,
and verifies stored bytes. Repeat the same command/receipt after interruption.

```powershell
uv run --script <skill-dir>\scripts\zotero_ingest.py attach-source `
  --metadata <paper-metadata.json> --parent-key <PAPER_KEY> `
  --source-pdf <original-paper.pdf> --expected-sha256 <recorded-source-hash> `
  --target-id user:0 --receipt <source-upload.json>
```

Add `--yes-attach-source` for the authorized write. If an older two-file paper
delivery receipt needs its original added, use `resume --source-pdf <original>`;
the archive source identity is checked before it becomes upload-eligible.

The configured PDF reader is Google Chrome. Do not launch or diagnose Zotero's
built-in reader during ingestion; follow `zotero-reader.md` only on an explicit
reader diagnosis request.
