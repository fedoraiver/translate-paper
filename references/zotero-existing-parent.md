# Delivery under an existing Zotero parent

Use when the user requests preserving an existing book/paper and appending files.
The standard `zotero_ingest.py ingest` creates a new parent and is not an append
command. Use an available authorized API adapter; do not claim a nonexistent
append command or disable the new-parent identity guard. The coordinator owns
all library writes. No computer-use, direct SQLite edits, or storage writes.
If no suitable adapter is available, prepare a narrowly scoped resumable client
against the supported API and check its requests/recovery with offline mocks
before live writes. A missing convenience CLI alone is not a capability blocker;
unsupported routes, unavailable authorization or unreadable live state can be.

## Establish the receipt before writing

Record the exact parent key, bibliographic identity, full-source attachment key
and file hash, and the current child-key set. Bind each chapter's five intended
titles to its accepted local files, hashes and active revision. Keep chapter
delivery separate from the whole parent's child count: other chapters, source
files, notes and unrelated children must remain intact.

Read the delivery ledger and all live children with pagination. For each owned
title, require one matching parent/type and compare actual stored bytes:

- Matching file already present: adopt/verify its key and skip the upload.
- No matching title: create only that missing attachment.
- One created attachment with a confirmed missing file: resume that key's file
  upload using supported preconditions; do not create another metadata item.
- Duplicate titles, conflicting keys or different bytes: stop that write and
  preserve the evidence; do not overwrite or delete to make a retry pass.

On an uncertain create response, query live state before retrying. Use supported
write tokens/preconditions and persist each confirmed stage/key. A timeout does
not authorize blindly repeating a POST. If the whole requested set already
matches, report it complete even when the user says "continue uploading".

## Desktop Local API authorization

For Zotero 10+, get the `Zotero-Server-ID` from `GET /api/` and partition cached
versions and credentials by that server ID. Web API keys are not Local API keys.
Older plugin route maps may describe Local API as read-only; establish the
running version's actual supported routes from primary documentation or local
implementation before declaring a write-capability blocker. A version-specific
write route does not supply a missing append adapter or waive its authorization.
Use the established client's exact credential target when resuming; do not
discard an existing valid authorization and invent another client identity.

1. Reuse this client's remembered key from the OS credential store. If absent,
   start one live `POST /api/local/authorize` with the real application name and
   server ID; the user handles the native permission dialog. No automated clicks.
2. Allow a realistic user-response timeout (for example, a configurable 3600
   seconds) while returning control for progress updates. A terminated client or
   expired HTTP request cannot receive a later approval. Explain the expiry and
   identify a fresh request instead of repeatedly telling the user the old one
   is waiting. Keep one request active, not a queue of repeated prompts.
3. Respect the returned `remember` choice. `false` permits a single authenticated
   write and needs a new grant afterwards; `true` allows reuse. Calling authorize
   again still prompts, even with the same app name. Never assume the user's
   message "I will allow" means the server has returned a usable key.
4. On actual `remember: true`, save the key in Windows Credential Manager (or the
   platform's secure store), with the correct server/client identity, before
   proceeding. Keep one client across the batch and reuse the stored key after
   a process interruption. Do not save a one-time key as a durable credential.
   Use a distinct Local API credential namespace; never replace the Web API key
   in `zotero_credentials.py`'s default or legacy target with a Local API key.
   Never print keys or put them in workspace files, command arguments or logs;
   do not read Zotero profile preferences to recover a lost key.
5. A rejected key or changed server identity requires diagnosis, not an infinite
   authorization loop. Honor `Retry-After` on authorization limits. Preserve the
   partial receipt on failure; do not restart the chapter's metadata creation.

The normal full-file flow is attachment metadata creation, file authorization,
raw byte upload, and upload registration. Preserve `If-None-Match`/`If-Match`
preconditions and verify the final stored file. Do not interpret success of one
stage as delivery of the complete file or chapter.

## Final verification and recovery records

After upload, re-query the exact parent and every intended attachment, verify
all five accepted-file hashes per chapter and the LaTeX archive identity, and
confirm the full source and pre-existing child set remain preserved. Normal
attachment reading may update `lastRead` and its `version`: allow only those
actual forward reading changes, record the differences, and continue to reject
changes to titles, parents, file identities or other metadata. Do not rewrite
the original baseline to hide a difference.

Older accepted bundle schemas may use different field names. Validate their
documented identity, member hashes and reports through a narrow schema adapter;
do not rewrite an accepted bundle or rerun translation merely to satisfy a new
reader. Unknown or unverifiable formats remain unresolved, not a pass.

Update chapter receipts, the existing book recovery record, pending counts and
the user-facing index only from verified results. Keep local-accepted and
Zotero-verified stages distinct. When all included chapters pass, clear stale
"awaiting authorization/upload" next actions and report the final counts. Do
not leave a verified completion described as pending or upload again because
an old queue file retains its historical name.

## Primary references

- [Local API, authorization and server IDs](https://www.zotero.org/support/dev/web_api/v3/local_api)
- [File uploads and preconditions](https://www.zotero.org/support/dev/web_api/v3/file_upload)
- [Web API item updates](https://www.zotero.org/support/dev/web_api/v3/write_requests)
