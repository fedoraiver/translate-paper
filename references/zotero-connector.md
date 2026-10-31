# Connector imports

Use with `zotero-ingestion.md` for new-parent imports or recovery of their
original active session. This describes the standard Zotero 10.0.6 interface;
consult the running version's implementation when capabilities differ.

## Session and parent identity

The existing helper uses this sequence:

1. Check exact bibliographic identity and the writable target library/collection.
2. `POST /connector/saveItems` with a unique `sessionID` and a client item `id`.
   This creates a new bibliographic parent.
3. `POST /connector/updateSession` with that session and the exact `L...` or
   `C...` target. These targets mean library/collection, not a book item.
4. Send the five files to `/connector/saveAttachment`, using the same session
   and the client item alias as `parentItemID`.
5. Resolve the resulting parent key through read-only Local API queries and
   verify destination, unique titles and every stored file's bytes.

`saveAttachment` resolves its parent only through
`session.getItemByConnectorKey(parentItemID)`. It does not look up an arbitrary
existing Zotero key or numeric ID. Setting `items[].id` to a book's key still
creates a new parent; `_sourceParentKey` is an identity-search hint removed from
the creation payload. Selecting a book in the UI also does not bind a parent:
the Connector's save target is a library or collection.

A still-valid original session can receive more files for its registered item.
Session mappings live in Zotero memory, with no public restore/bind-existing-item
endpoint. Do not promise cross-restart recovery or a fixed lifetime from the
session ID alone. For a pre-existing book outside that session, follow
[existing-parent delivery](zotero-existing-parent.md); do not create a temporary
parent and move/delete records as an unrequested workaround.

## File transport

- Use `X-Zotero-Connector-API-Version: 3`, the actual `Content-Length`, and raw
  file bytes. `X-Metadata` is ASCII-safe JSON (`ensure_ascii=True`) containing
  `sessionID`, `parentItemID`, `title`, and `url`.
- Use `application/pdf`, `text/markdown`, and `application/zip` for the respective
  artifacts. Do not send Markdown as `text/plain`: the generic server can decode
  it into a string before the stream-based attachment handler receives it.
- The URL identifies the uploaded attachment and supplies its extension; this
  endpoint saves the supplied stream rather than downloading that URL. Generated
  Markdown/ZIP URLs need a real `.md`/`.zip` path suffix, not just a fragment.
- The explicit attachment title is separate from its stored filename. Zotero
  can derive/rename the basename from parent metadata. Check the required suffix,
  MIME, title and hashes rather than assuming it preserves the upload basename.
- These files are locally stored `imported_url` attachments, not external links.
  `imported_file` is also a stored mode used by other routes; verify the selected
  route's actual mode instead of requiring the wrong one.

## Completion and recovery

A `201` response is not a complete receipt, and repeated `saveAttachment` calls
can create duplicates. Record the session, client alias, resolved parent key,
expected titles/file hashes and confirmed attachment keys. On a timeout or
connection failure, inspect live records and bytes before another write; a lost
response does not prove the operation failed. Reuse a live session only after
determining which files are missing. If the session is lost, preserve accepted
files and use a supported existing-parent route; do not rerun new-parent import.

For resumable imports pass `ingest --receipt <work-dir>/zotero-upload.json`.
The helper saves the session, client alias and expected hashes before its first
write, then checkpoints file status and confirmed keys. It refuses an existing
receipt instead of replacing partial evidence. A receipt preserves recovery
information; it cannot restore a Connector session after Zotero loses that session.

Use complete paginated child results. Verify all five files against accepted
local artifacts, including both Markdown files, and validate the source ZIP's
source/translated-PDF identity. `zotero_ingest.py ingest` performs this byte check;
`verify --expected-dir <final-dir>` repeats it for the standard packaged names.

## Primary references

- [Connector HTTP server](https://www.zotero.org/support/dev/client_coding/connector_http_server)
- [Zotero 10.0.6 endpoints](https://github.com/zotero/zotero/blob/10.0.6/chrome/content/zotero/xpcom/server/server_connector.js)
- [Session mapping](https://github.com/zotero/zotero/blob/10.0.6/chrome/content/zotero/xpcom/server/saveSession.js)
- [Attachment stream handling](https://github.com/zotero/zotero/blob/10.0.6/chrome/content/zotero/xpcom/attachments.js)
