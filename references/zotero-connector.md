# Historical Connector receipt recovery

This reference is only for version 1 `connector-new-parent` receipts. Normal
imports, appends and file uploads now use Pyzotero against Zotero 10+ Local API;
follow [ingestion](zotero-ingestion.md). Do not revive raw Connector write helpers.
The only active Connector call reads selected destinations and permissions.

## Why a session cannot be resumed by ID alone

The old flow called `saveItems`, `updateSession`, then `saveAttachment`, referring
to the new parent by an in-memory client alias. `saveItems` created a new parent;
a library/collection save target or an existing Zotero key did not turn it into
an append operation. The mapping between session alias and parent lived in
Zotero memory and could disappear on restart. A saved session ID cannot recreate
that mapping or bind an arbitrary pre-existing book.

## Supported migration

```powershell
uv run --script <skill-dir>\scripts\zotero_ingest.py resume `
  --receipt <legacy-upload.json> --parent-key <ORIGINAL_PARENT_KEY>
```

The parent argument is optional only if the original receipt already records its
key. It must be the original parent with matching bibliographic identity and
actual destination. Inspect live children and stored bytes before executing.
Add `--yes-resume` to save a separate `<receipt-stem>.pyzotero.json` and resume
through the SDK. Keep the historical receipt unchanged and use the converted
receipt for subsequent recovery. A conflicting parent argument or an already
existing converted receipt blocks conversion and reports the correct next step.

The conversion retains accepted local paths, expected hashes and confirmed keys.
Papers retain the original PDF, Chinese PDF and source ZIP as delivery files.
For chapters, the full-book original is preserved and only the Chinese PDF and
ZIP are delivered. Excluded Markdown/summary and chapter-excerpt records remain
historical receipt evidence without reading or uploading those paths. It adopts uniquely
matching delivery attachments, uploads missing files to known metadata keys, and
creates only missing owned attachments. A timeout/201 from an old Connector
upload is not proof of completion. Verify the required paper/chapter delivery
files and ZIP identities. Existing excluded attachments remain intact.
Historical receipt fields do not require generating or retaining those documents
locally. Remove old Zotero attachments only through an explicitly requested
cleanup, as documented in the ingestion reference.

If the original parent cannot be proved, stop and request its explicit key. Do
not recreate the parent, alter the database/storage, or use temporary parents as
a workaround. The previous imported_url files remain valid stored attachments;
new SDK attachments use imported_file, and verification accepts both modes.

## Historical implementation references

- [Connector HTTP server](https://www.zotero.org/support/dev/client_coding/connector_http_server)
- [Zotero 10.0.6 Connector](https://github.com/zotero/zotero/blob/10.0.6/chrome/content/zotero/xpcom/server/server_connector.js)
- [Session mapping](https://github.com/zotero/zotero/blob/10.0.6/chrome/content/zotero/xpcom/server/saveSession.js)
- [Local API and authorization](https://www.zotero.org/support/dev/web_api/v3/local_api)
