# Zotero ingestion

## Contents

- Responsibilities, metadata, and readiness
- Search and guarded replacement
- Connector ingestion
- Verification and recovery

Use `$zotero:Zotero` plus the skill-local adapter. Do not use computer-use,
edit `zotero.sqlite`, or manipulate Zotero storage.

## Responsibilities

- Zotero plugin: enable/probe local API, search, inspect children and target.
- Local API: read and verify immediate Desktop state.
- Web API: search synced state and delete one guarded parent plus children.
- Connector: create the parent, upload stored PDFs and Markdown files, and place the
  session in the selected collection.
- `zotero_credentials.py`: read the Web API key from Windows Credential Manager.
- `zotero_ingest.py`: enforce identity, sync, sequencing, and verification.

PDF and Markdown file bytes go to the local Connector. Bibliographic identity
and deletion requests go to the Web API.

## Metadata

```json
{
  "itemType": "journalArticle",
  "title": "The Knowledge Complexity of Interactive Proof Systems",
  "creators": [
    {
      "creatorType": "author",
      "firstName": "Shafi",
      "lastName": "Goldwasser"
    }
  ],
  "publicationTitle": "SIAM Journal on Computing",
  "date": "1989",
  "volume": "18",
  "issue": "1",
  "pages": "186-208",
  "DOI": "10.1137/0218012",
  "url": "https://doi.org/10.1137/0218012",
  "language": "en",
  "_sourceParentKey": "6ACE9QQE"
}
```

Retain the original metadata. Require item type, title, at least one creator,
and DOI, arXiv ID, or title plus year.

Set `_sourceParentKey` only when the source PDF or metadata was obtained from
that known Zotero parent. The adapter accepts the key only when the candidate
has the exact normalized title, is a bibliographic parent, and has no DOI,
arXiv, or year conflict. It does not permit arbitrary-key deletion. Workflow
fields are stripped before Connector creation.

## Readiness

Use `python` on this Windows installation:

```powershell
python <zotero-plugin-root>\skills\zotero\scripts\zotero.py status --json
python <skill-dir>\scripts\zotero_credentials.py verify
python <skill-dir>\scripts\zotero_ingest.py status
python <skill-dir>\scripts\zotero_ingest.py targets
```

If needed:

```powershell
python <zotero-plugin-root>\skills\zotero\scripts\zotero.py enable --restart
```

Resolve targets on every run. Honor an explicit user-supplied library or
collection; otherwise use the writable currently selected target. If none is
selected, use the only writable target when exactly one exists. Ask only when
no writable target exists or multiple plausible targets remain. Resolve one
exact Connector target ID with `filesEditable: true`. Stop on duplicate names
or an ID/name mismatch.

The new credential target is `Codex/translate-paper/ZoteroAPI`. The helper
automatically reads the legacy
`Codex/translate-paper-to-zotero/ZoteroAPI` target when the new target is
absent, so the existing configured key remains usable. Never print the key.

## Search and guarded replacement

Resolve identity and preserve the original PDF and metadata before translation.
For replacement, normally defer the guarded deletion below until the new local
bundle has passed all acceptance gates and is ready for ingestion. Revalidate the
exact match and record its current children then; this avoids leaving the library
without a usable item during a long translation or repair. An explicit request
to delete first overrides this ordering. Never re-delete an already removed item
when resuming; inspect the saved result and current local/Web state.

```powershell
python <skill-dir>\scripts\zotero_ingest.py search `
  --metadata <output-dir>\zotero-item.json
```

Matching is strict:

1. exact normalized DOI;
2. exact normalized arXiv ID;
3. exact title plus year;
4. a recorded `_sourceParentKey` with exact title and no identity conflict.

Never fall back from a conflicting DOI or arXiv ID to title similarity. Stop
when multiple parents match.

Preview and record the parent and all children:

```powershell
python <skill-dir>\scripts\zotero_ingest.py delete `
  --metadata <output-dir>\zotero-item.json `
  --item-key <PARENT_KEY>
```

Invoking `$translate-paper` is standing authorization to replace one unique
guarded match. Do not pause for another confirmation:

```powershell
python <skill-dir>\scripts\zotero_ingest.py delete `
  --metadata <output-dir>\zotero-item.json `
  --item-key <PARENT_KEY> `
  --yes-delete
```

The adapter revalidates identity, fetches children, deletes at most 50 keys
with an unmodified-library-version guard, and waits for Desktop sync. If the
Web delete succeeds but local removal does not, stop and ask the user to sync.

## Connector ingestion

```powershell
python <skill-dir>\scripts\zotero_ingest.py ingest `
  --metadata <output-dir>\zotero-item.json `
  --source-pdf <source.pdf> `
  --translated-pdf <translated.pdf> `
  --translated-markdown <translated.md> `
  --summary <summary.md> `
  --target-id <TARGET_ID> `
  --target-name "<COLLECTION_NAME>"
```

After the preview succeeds, rerun `targets` immediately before the actual
ingestion. Confirm that the exact target ID/name pair is still present and
`filesEditable: true`, then rerun ingestion with the same pair and
`--yes-ingest`. Do not reuse a target result captured before translation or a
long batch pause. The adapter must refuse while a matched parent remains,
create one session and parent, place it in the selected target, upload stored
`原文 PDF`, `中文翻译 PDF`, `中文翻译 Markdown`, and `中文论文总结`, then verify
the local result. The summary must be the actual UTF-8 `中文论文总结.md` file,
uploaded with `text/markdown`; never create an HTML note or a note containing
Markdown source.
The Connector upload identifier must have a URL path ending in `.md` (the
adapter builds this without fetching that URL). A fragment alone is insufficient:
Zotero derives the stored extension from the path when its MIME service does not
recognize Markdown. Zotero may rename the basename, but the `.md` suffix must survive.

## Verification and recovery

```powershell
python <skill-dir>\scripts\zotero_ingest.py verify `
  --metadata <output-dir>\zotero-item.json
```

Require exactly one parent, one stored attachment with each required title
(`原文 PDF`, `中文翻译 PDF`, `中文翻译 Markdown`, and `中文论文总结`). The summary
must have a `.md` filename and `text/markdown` content type (`text/plain` is
accepted only when Zotero normalizes the MIME type). A legacy summary note does
not satisfy this check. After
a partial Connector failure, search and inspect children before retrying.
Never blindly rerun ingestion, overwrite generated children, or perform SQL
cleanup.

For a requested format-only migration of legacy summary notes, preserve the
parent and all other children. Back up the original note, convert its complete
content to UTF-8 Markdown, and verify the replacement attachment's bytes and
parent before moving only the old note to trash through an authorized API.
Keep a key/path/hash record for recovery; do not rerun translation or replace
the whole paper merely to change its summary format.

For a paper-list request, run the read-only audit in
[batch-workflow.md](batch-workflow.md) before translating. Treat an item as
already translated only when this exact four-child verification passes; a
parent with partial or differently titled children is not complete.

This verification covers Zotero identity, collection placement, stored
attachments, including the Markdown summary. The configured reading path is Google Chrome,
so do not launch or validate Zotero's built-in PDF reader during ingestion.
