"""Lazy Pyzotero access shared by delivery, credentials, and batch auditing."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import urllib.parse
import urllib.request


LOCAL_BASE = os.environ.get("ZOTERO_LOCAL_BASE_URL", "http://127.0.0.1:23119").rstrip("/")


class ZoteroError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def redact(message: str, *secrets: str | None) -> str:
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[redacted]")
    return re.sub(r"(/keys/)[^\s/?'\"]+", r"\1[redacted]", message)


def _sdk():
    try:
        from pyzotero import Zotero
        import httpx2
    except ImportError:
        raise ZoteroError("Pyzotero is required for Zotero operations; run this command with uv run --script.") from None
    return Zotero, httpx2


def _file_transport():
    from pyzotero.filetransport import FileTransport

    class LocalFileTransport(FileTransport):
        def handle_request(self, request):
            # httpx2 can inherit the HTTP host when following file:/// redirects.
            if request.url.host in {"127.0.0.1", "::1"}:
                request.url = request.url.copy_with(host="localhost")
            if request.url.path.startswith(("//", "/\\")):
                raise ValueError("Only local file paths are allowed")
            response = super().handle_request(request)
            response.headers["Content-Type"] = "application/octet-stream"
            return response

    return LocalFileTransport()


def selected_target() -> dict[str, Any]:
    """The SDK does not expose Desktop's selected target or edit permissions."""
    parsed = urllib.parse.urlsplit(LOCAL_BASE)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ZoteroError("ZOTERO_LOCAL_BASE_URL must be a loopback http:// address.")
    request = urllib.request.Request(
        LOCAL_BASE + "/connector/getSelectedCollection", data=b"{}",
        headers={"Content-Type": "application/json", "X-Zotero-Connector-API-Version": "3"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except (OSError, ValueError):
        raise ZoteroError("Cannot read Zotero Desktop's selected target and permissions.") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("targets"), list):
        raise ZoteroError("Zotero returned an invalid target list.")
    return payload


@contextmanager
def authorization_lock(server_id: str):
    name = hashlib.sha256(server_id.encode()).hexdigest()[:24]
    with (Path(tempfile.gettempdir()) / f"translate-paper-zotero-{name}.lock").open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ZoteroError("A Zotero authorization request is already active.") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class Client:
    def __init__(self, library_type: str = "user", library_id: str = "0", *,
                 local: bool = True, api_key: str | None = None,
                 server_id: str | None = None, transport=None, timeout: float = 20):
        Zotero, httpx2 = _sdk()
        parsed = urllib.parse.urlsplit(LOCAL_BASE)
        if local and (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise ZoteroError("ZOTERO_LOCAL_BASE_URL must be a loopback http:// address.")
        self.library_type, self.library_id = library_type, str(library_id)
        self.local = local
        self.zot = Zotero(self.library_id, library_type, api_key, local=local, server_id=server_id,
                          client=httpx2.Client(follow_redirects=True, timeout=timeout,
                                               trust_env=not local, transport=transport,
                                               mounts={"file://": _file_transport()} if local else None))
        if local:
            self.zot.endpoint = LOCAL_BASE + "/api"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.zot.client.close()

    @property
    def server_id(self) -> str:
        if not self.zot.server_id:
            self.call("item_types")
        if not self.zot.server_id:
            raise ZoteroError("Zotero 10+ with local API write support is required.")
        return self.zot.server_id

    def call(self, method: str, *args, **kwargs):
        try:
            return getattr(self.zot, method)(*args, **kwargs)
        except Exception as exc:
            names = {cls.__name__ for cls in type(exc).__mro__}
            status = next((code for name, code in {
                "ResourceNotFoundError": 404, "LocalAPIKeyRequiredError": 401,
                "LocalAPIDeniedError": 403, "ServerIDMismatchError": 412,
                "PreConditionFailedError": 412, "TooManyRequestsError": 429,
                "TooManyRetriesError": 429,
            }.items() if name in names), None)
            detail = redact(str(exc), self.zot.api_key, self.zot.local_api_key)
            raise ZoteroError(f"Zotero {method} failed: {detail}", status=status) from None

    def all(self, method: str, *args, **kwargs) -> list[dict[str, Any]]:
        page = self.call(method, *args, limit=100, **kwargs)
        rows, keys, links = [], set(), set()
        while True:
            if not isinstance(page, list):
                raise ZoteroError("Zotero returned invalid paginated items.")
            for row in page:
                key = row.get("key", row.get("id")) if isinstance(row, dict) else None
                if not key or key in keys:
                    raise ZoteroError("Zotero returned missing or duplicate keys / a repeated page.")
                keys.add(key)
                rows.append(row)
            following = (self.zot.links or {}).get("next")
            if not following:
                return rows
            if following in links:
                raise ZoteroError("Zotero returned a repeated pagination link.")
            links.add(following)
            page = self.call("follow")

    def item(self, key: str, *, missing_ok: bool = False):
        try:
            return self.call("item", key)
        except ZoteroError as exc:
            if missing_ok and exc.status == 404:
                return None
            raise

    def require_write_key(self):
        from zotero_credentials import read_local_credential
        try:
            key = read_local_credential(self.server_id)
        except (OSError, RuntimeError) as exc:
            raise ZoteroError(f"Cannot read local credential store: {exc}") from None
        if not key:
            raise ZoteroError("No remembered local authorization. Run zotero_ingest.py authorize and choose Always Allow.")
        self.zot.local_api_key = key

    def create(self, data: dict[str, Any]):
        result = self.call("create_items", [data])
        if result.get("failed") or str(result.get("success", {}).get("0", "")) != data["key"]:
            raise ZoteroError("Zotero did not confirm the requested item creation.")
        return self.item(data["key"])

    def upload(self, item: dict[str, Any], path: Path):
        payload = {**item.get("data", item), "filename": str(path)}
        result = self.call("upload_attachments", [payload])
        if result.get("failure") or not (result.get("success") or result.get("unchanged")):
            raise ZoteroError("Zotero did not confirm the attachment upload.")

    def download(self, key: str, path: Path):
        path.write_bytes(self.call("file", key))

    def library_version(self) -> int:
        self.call("item_versions", limit=1)
        return int(self.zot.request.headers["Last-Modified-Version"])


def target_id(library_type: str, library_id: str, collection: str | None = None) -> str:
    base = f"{library_type}:{library_id}"
    return base + f"/collections/{collection}" if collection else base


def target_catalog() -> dict[str, Any]:
    desktop = selected_target()
    with Client() as client:
        groups = client.all("groups")
        libraries = [("user", "0", None)] + [
            ("group", str(group["id"]), group.get("data", group).get("name")) for group in groups
        ]
    roots = [t for t in desktop["targets"] if t.get("level") == 0]
    group_names = [name for kind, key, name in libraries if kind == "group"]
    personal = [r["name"] for r in roots if r["name"] not in group_names]
    legacy = {}
    stack = []
    for row in desktop["targets"]:
        level = int(row["level"])
        if level > len(stack):
            raise ZoteroError("Invalid Desktop target hierarchy.")
        stack = stack[:level] + [row["name"]]
        legacy.setdefault(tuple(stack), []).append(row)
    targets = []
    for kind, lib_id, name in libraries:
        with Client(kind, lib_id) as client:
            collections = client.all("collections")
        if kind == "user":
            name = collections[0].get("library", {}).get("name") if collections else None
            name = name or (personal[0] if len(personal) == 1 else None)
        by_key = {row["key"]: row["data"] for row in collections}

        def collection_path(key: str, seen: frozenset = frozenset()) -> tuple:
            if key in seen or key not in by_key:
                raise ZoteroError("Invalid API collection hierarchy.")
            row = by_key[key]
            parent = row.get("parentCollection")
            prefix = collection_path(parent, seen | {key}) if parent else (name,)
            return prefix + (row["name"],)

        paths = {key: collection_path(key) for key in by_key}
        path_counts = Counter(paths.values())
        root_matches = legacy.get((name,), [])
        root_writable = len(root_matches) == 1 and root_matches[0].get("filesEditable") is True
        for collection, path in [(None, (name,))] + list(paths.items()):
            matches = legacy.get(path, [])
            # Identical API paths cannot establish a mapping to internal Desktop IDs.
            api_count = path_counts[path] if collection else 1
            matched = matches[0] if len(matches) == 1 and api_count == 1 else {}
            targets.append({"id": target_id(kind, lib_id, collection), "name": path[-1],
                            "library_type": kind, "library_id": lib_id, "collection_key": collection,
                            "path": list(path), "legacy_id": matched.get("id"),
                            "filesEditable": root_writable})
    # A same-named library can otherwise map one Desktop ID to two API libraries.
    legacy_counts = Counter(row["legacy_id"] for row in targets if row["legacy_id"])
    root_counts = Counter(row["path"][0] for row in targets if row["collection_key"] is None)
    for row in targets:
        if row["legacy_id"] and legacy_counts[row["legacy_id"]] != 1:
            row.update(legacy_id=None, filesEditable=False)
        if root_counts[row["path"][0]] != 1:
            row.update(legacy_id=None, filesEditable=False)
    selected = f"C{desktop['id']}" if desktop.get("id") is not None else f"L{desktop.get('libraryID')}"
    current = [row["id"] for row in targets if row["legacy_id"] == selected]
    return {"selected": current[0] if len(current) == 1 else None, "targets": targets}


def resolve_target(value: str, name: str | None = None) -> dict[str, Any]:
    catalog = target_catalog()
    matches = [row for row in catalog["targets"] if value in {row["id"], row["legacy_id"]}]
    if len(matches) != 1:
        raise ZoteroError("Unknown or ambiguous Zotero target; use the stable ID from targets.")
    target = matches[0]
    if name is not None and target["name"] != name:
        raise ZoteroError("Zotero target ID/name mismatch.")
    if not target["filesEditable"]:
        raise ZoteroError("Cannot establish stored-file write permission for this target.")
    return target
