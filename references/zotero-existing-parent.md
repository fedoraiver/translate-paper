# Delivery under an existing Zotero parent

Use `zotero_ingest.py attach` through the pinned Pyzotero local API. It preserves
the exact existing parent and uses no Connector save session. The coordinator
owns library writes; no computer-use, SQLite edits or direct storage writes.

## Inputs and authorization

Read [ingestion and authorization](zotero-ingestion.md) first. Use the exact
parent's bibliographic metadata, not chapter metadata masquerading as the book.
The local bundle checks chapter source/translated-PDF identity through that
chapter's accepted source archive. Zotero checks the stored archive against the
stored Chinese PDF. Each chapter gets its own receipt.

```powershell
uv run --script <skill-dir>\scripts\zotero_ingest.py attach `
  --parent-key <BOOK_KEY> --metadata <book-metadata.json> `
  --translated-pdf <chapter-translated.pdf> `
  --latex-source-archive <chapter-source.zip> `
  --target-id user:0/collections/ABCD1234 --target-name "书籍" `
  --title-prefix "第 1 章" --receipt <chapter-work>\zotero-upload.json
```

Preview first, then add `--yes-attach` for the already authorized operation.
The two titles become `第 1 章 / 中文翻译 PDF` and `第 1 章 / LaTeX 源码包`.
Book metadata also selects this two-file chapter delivery when the prefix is
empty. A paper with no chapter prefix requires its original PDF as a third
attachment through `--source-pdf`. Prefixes are
stored in receipts and must stay identical during later verification/recovery.
Do not generate or package an original chapter PDF copy, Chinese Markdown or
summary. A chapter excerpt needed for processing is an input only.
Preserve the book's full original PDF. Never remove a paper's original PDF while
cleaning redundant chapter attachments.

## Preserve and reconcile

Before writing, save the parent, all paginated children and each unrelated
stored file's actual hash (or its confirmed missing-file state). Existing titles
owned by this delivery are reconciled separately:

- Exact parent/type/title and matching bytes: adopt the key and skip upload.
- No matching title: create only that missing attachment, with its reserved key.
- Existing attachment metadata with a confirmed absent file: upload to that key.
- Duplicate titles, conflicting keys, different bytes or missing confirmed items:
  stop and preserve the receipt; never overwrite/delete to make a retry pass.

Preallocated keys are checkpointed before creates. Unknown response outcomes are
resolved from live state before retrying. Other chapters, source files, notes and
unrelated children remain intact. Recheck their metadata and stored bytes after
delivery. Only forward version and `lastRead` changes caused by ordinary reads
are tolerated; title, parent, file or other metadata changes are rejected.

Use `resume --receipt <receipt> --yes-resume` after interruption. If every owned
file already matches, report complete and issue no writes. A new `attach` with a
new receipt also adopts matching files; it does not duplicate the chapter.

## Verify and report

```powershell
uv run --script <skill-dir>\scripts\zotero_ingest.py verify `
  --metadata <book-metadata.json> --parent-key <BOOK_KEY> `
  --title-prefix "第 1 章" --expected-dir <chapter-final-dir> `
  --target-id user:0/collections/ABCD1234
```

Require the chapter's two uniquely titled stored files and source ZIP identity;
the book may have other children. Confirm actual library/collection and
preservation of the baseline. Keep local-accepted and Zotero-verified stages
distinct; clear stale pending-upload status only from verified outcomes.

Accepted legacy bundle schema differences still require a narrow documented
reader, not rewriting accepted artifacts or rerunning translation. Unknown or
unverifiable formats are not a pass.
