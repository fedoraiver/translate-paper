# Zotero ingestion

Use `$zotero:Zotero` plus the skill-local adapter. Do not use computer-use,
edit `zotero.sqlite`, or manipulate Zotero storage.

## Responsibilities

- Zotero plugin: enable/probe local API, search, inspect children and target.
- Local API: read and verify immediate Desktop state.
- Web API: search synced state and delete one guarded parent plus children.
- Connector: create the parent, upload stored PDFs, add the note, and place the
  session in the selected collection.
- `zotero_credentials.py`: read the Web API key from Windows Credential Manager.
- `zotero_ingest.py`: enforce identity, sync, sequencing, and verification.

PDF bytes and summary text go to the local Connector. Bibliographic identity
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

After the preview succeeds, immediately rerun with `--yes-ingest`. The adapter
must refuse while a matched parent remains, create one session and parent,
place it in the selected target, upload stored `原文 PDF`, `中文翻译 PDF`, and
`中文翻译 Markdown`, create the `中文论文总结` child note, and verify the local
result.

## Verification and recovery

```powershell
python <skill-dir>\scripts\zotero_ingest.py verify `
  --metadata <output-dir>\zotero-item.json
```

Require exactly one parent, one stored attachment with each required title
(`原文 PDF`, `中文翻译 PDF`, and `中文翻译 Markdown`), and one summary note. After
a partial Connector failure, search and inspect children before retrying.
Never blindly rerun ingestion, overwrite generated children, or perform SQL
cleanup.

This verification covers Zotero identity, collection placement, stored
attachments, and the child note. The configured reading path is Google Chrome,
so do not launch or validate Zotero's built-in PDF reader during ingestion.
