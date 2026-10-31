#!/usr/bin/env python3
"""Safely replace and ingest translated papers in Zotero.

Reads/deletes use the Zotero Web API and a key stored by
``zotero_credentials.py``. Creation, stored PDF/Markdown/LaTeX ZIP uploads,
and collection placement use Zotero Desktop's Connector server.
The script never edits zotero.sqlite or Zotero's storage directory.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import http.client
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable

from zotero_credentials import DEFAULT_TARGET, read_credential, verify_credential
from export_latex_project import validate_archive


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


WEB_BASE = os.environ.get("ZOTERO_WEB_API_BASE", "https://api.zotero.org")
LOCAL_BASE = os.environ.get("ZOTERO_LOCAL_BASE_URL", "http://127.0.0.1:23119")
USER_AGENT = "translate-paper/2.0"
CONNECTOR_HEADERS = {"X-Zotero-Connector-API-Version": "3"}
API_HEADERS = {"Zotero-API-Version": "3"}
SUMMARY_ATTACHMENT_TITLE = "中文论文总结"
SOURCE_ATTACHMENT_TITLE = "原文 PDF"
TRANSLATION_ATTACHMENT_TITLE = "中文翻译 PDF"
TRANSLATION_MARKDOWN_TITLE = "中文翻译 Markdown"
LATEX_SOURCE_TITLE = "LaTeX 源码包"
FINAL_FILENAMES = {
    SOURCE_ATTACHMENT_TITLE: "原文 PDF.pdf",
    TRANSLATION_ATTACHMENT_TITLE: "中文翻译.pdf",
    TRANSLATION_MARKDOWN_TITLE: "中文翻译正文.md",
    SUMMARY_ATTACHMENT_TITLE: "中文论文总结.md",
    LATEX_SOURCE_TITLE: "LaTeX源码包.zip",
}


class ZoteroError(RuntimeError):
    """A user-actionable Zotero workflow error."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def dump_json(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream)


def _write_upload_receipt(path: Path | None, receipt: dict[str, Any], *, create: bool = False) -> None:
    if path is None:
        return
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
        if create:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(text)
            return
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        temporary.replace(path)
    except FileExistsError:
        raise ZoteroError("Upload receipt already exists; inspect it and live items before retrying.") from None
    except OSError as error:
        raise ZoteroError(f"Cannot save upload receipt: {error}") from error
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    json_body: Any = None,
    timeout: float = 20,
    allowed: tuple[int, ...] = (200,),
) -> HttpResponse:
    request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    req = urllib.request.Request(url, data=body, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = HttpResponse(
                response.status,
                dict(response.headers.items()),
                response.read(),
            )
    except urllib.error.HTTPError as exc:
        result = HttpResponse(exc.code, dict(exc.headers.items()), exc.read())
    except OSError as exc:
        raise ZoteroError(f"Cannot reach Zotero endpoint: {exc}") from exc

    if result.status not in allowed:
        detail = result.text().strip()[:500] or "empty response"
        raise ZoteroError(
            f"{method} {urllib.parse.urlsplit(url).path} failed "
            f"with HTTP {result.status}: {detail}"
        )
    return result


def web_request(
    path: str,
    *,
    user_id: str,
    api_key: str,
    method: str = "GET",
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    allowed: tuple[int, ...] = (200,),
) -> HttpResponse:
    auth_headers = {
        **API_HEADERS,
        "Authorization": f"Bearer {api_key}",
        **(headers or {}),
    }
    return request(
        WEB_BASE.rstrip("/") + path,
        method=method,
        headers=auth_headers,
        json_body=json_body,
        allowed=allowed,
    )


def local_request(path: str, *, allowed: tuple[int, ...] = (200,)) -> HttpResponse:
    return request(
        LOCAL_BASE.rstrip("/") + path,
        headers=API_HEADERS,
        timeout=5,
        allowed=allowed,
    )


def connector_request(
    path: str,
    *,
    json_body: Any = None,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    allowed: tuple[int, ...] = (200,),
) -> HttpResponse:
    return request(
        LOCAL_BASE.rstrip("/") + path,
        method="POST",
        headers={**CONNECTOR_HEADERS, **(headers or {})},
        json_body=json_body,
        body=body,
        timeout=30,
        allowed=allowed,
    )


def connector_upload_file(
    path: Path,
    *,
    session_id: str,
    parent_item_id: str,
    title: str,
    url: str,
    content_type: str,
) -> HttpResponse:
    base = urllib.parse.urlsplit(LOCAL_BASE)
    if base.scheme != "http" or not base.hostname:
        raise ZoteroError("ZOTERO_LOCAL_BASE_URL must be a local http:// endpoint.")
    connection = http.client.HTTPConnection(base.hostname, base.port or 80, timeout=60)
    metadata = json.dumps(
        {
            "sessionID": session_id,
            "parentItemID": parent_item_id,
            "title": title,
            "url": url,
        },
        ensure_ascii=True,
    )
    try:
        connection.putrequest("POST", "/connector/saveAttachment")
        connection.putheader("User-Agent", USER_AGENT)
        connection.putheader(
            "X-Zotero-Connector-API-Version",
            CONNECTOR_HEADERS["X-Zotero-Connector-API-Version"],
        )
        connection.putheader("Content-Type", content_type)
        connection.putheader("Content-Length", str(path.stat().st_size))
        connection.putheader("X-Metadata", metadata)
        connection.endheaders()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                connection.send(chunk)
        raw_response = connection.getresponse()
        response = HttpResponse(
            raw_response.status,
            dict(raw_response.getheaders()),
            raw_response.read(),
        )
    except OSError as exc:
        raise ZoteroError(f"Cannot upload {title} to Zotero: {exc}") from exc
    finally:
        connection.close()
    if response.status != 201:
        raise ZoteroError(
            f"Uploading {title} failed with HTTP {response.status}: "
            f"{response.text().strip()[:500] or 'empty response'}"
        )
    return response


def connector_upload_pdf(
    path: Path, *, session_id: str, parent_item_id: str, title: str, url: str
) -> HttpResponse:
    return connector_upload_file(
        path,
        session_id=session_id,
        parent_item_id=parent_item_id,
        title=title,
        url=url,
        content_type="application/pdf",
    )


def read_api_credentials(target: str) -> tuple[str, str]:
    env_user = os.environ.get("ZOTERO_USER_ID")
    env_key = os.environ.get("ZOTERO_API_KEY")
    if env_user and env_key:
        return env_user, env_key
    if bool(env_user) != bool(env_key):
        raise ZoteroError("Set both ZOTERO_USER_ID and ZOTERO_API_KEY, or neither.")
    try:
        return read_credential(target)
    except FileNotFoundError as exc:
        raise ZoteroError(
            "Zotero API credential is not configured. Run "
            "zotero_credentials.py store first."
        ) from exc


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip()


def normalize_title(value: str) -> str:
    return normalize_space(value).casefold()


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    result = normalize_space(value)
    result = re.sub(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)", "", result, flags=re.I)
    result = result.strip().rstrip(".,;)")
    match = re.search(r"10\.\d{4,9}/\S+", result, flags=re.I)
    return match.group(0).casefold() if match else None


def normalize_arxiv(value: str | None) -> str | None:
    if not value:
        return None
    result = normalize_space(value)
    result = re.sub(
        r"^(?:arxiv:\s*|https?://arxiv\.org/(?:abs|pdf)/)", "", result, flags=re.I
    )
    result = re.sub(r"\.pdf$", "", result, flags=re.I)
    match = re.search(
        r"(?:[a-z-]+(?:\.[a-z-]+)?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?",
        result,
        flags=re.I,
    )
    if not match:
        return None
    return re.sub(r"v\d+$", "", match.group(0), flags=re.I).casefold()


def extract_year(data: dict[str, Any]) -> str | None:
    raw = str(data.get("date") or data.get("year") or "")
    match = re.search(r"\b(18|19|20|21)\d{2}\b", raw)
    return match.group(0) if match else None


def extract_doi(data: dict[str, Any]) -> str | None:
    for field in ("DOI", "doi", "url", "extra"):
        doi = normalize_doi(str(data.get(field) or ""))
        if doi:
            return doi
    return None


def extract_arxiv(data: dict[str, Any]) -> str | None:
    for field in ("arXivID", "archiveLocation"):
        arxiv = normalize_arxiv(str(data.get(field) or ""))
        if arxiv:
            return arxiv
    url = str(data.get("url") or "")
    if re.search(r"https?://arxiv\.org/(?:abs|pdf)/", url, flags=re.I):
        arxiv = normalize_arxiv(url)
        if arxiv:
            return arxiv
    extra = str(data.get("extra") or "")
    for match in re.finditer(
        r"(?:arxiv:\s*|https?://arxiv\.org/(?:abs|pdf)/)"
        r"([a-z-]+(?:\.[a-z-]+)?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?",
        extra,
        flags=re.I,
    ):
        arxiv = normalize_arxiv(match.group(0))
        if arxiv:
            return arxiv
    return None


def item_data(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data")
    return data if isinstance(data, dict) else item


def validate_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise ZoteroError("Metadata must be one JSON object.")
    if not normalize_space(str(metadata.get("title") or "")):
        raise ZoteroError("Metadata requires a non-empty title.")
    if not normalize_space(str(metadata.get("itemType") or "")):
        raise ZoteroError("Metadata requires itemType.")
    creators = metadata.get("creators")
    if not isinstance(creators, list) or not creators:
        raise ZoteroError("Metadata requires at least one creator.")
    for creator in creators:
        if not isinstance(creator, dict) or not creator.get("creatorType"):
            raise ZoteroError("Each creator requires creatorType.")
        if not creator.get("name") and not creator.get("lastName"):
            raise ZoteroError("Each creator requires name or lastName.")

    if not extract_doi(metadata) and not extract_arxiv(metadata):
        if not extract_year(metadata):
            raise ZoteroError(
                "Without DOI or arXiv ID, metadata requires a publication year."
            )
    source_parent_key = str(metadata.get("_sourceParentKey") or "").strip()
    if source_parent_key and not re.fullmatch(
        r"[A-Z0-9]{8}", source_parent_key, re.IGNORECASE
    ):
        raise ZoteroError("_sourceParentKey must be one 8-character Zotero item key.")
    return metadata


def load_metadata(path: str) -> dict[str, Any]:
    metadata_path = Path(path).expanduser().resolve()
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ZoteroError(f"Cannot read metadata JSON: {exc}") from exc
    return validate_metadata(payload)


def identity(metadata: dict[str, Any]) -> dict[str, str | None]:
    return {
        "title": normalize_space(str(metadata.get("title") or "")),
        "year": extract_year(metadata),
        "doi": extract_doi(metadata),
        "arxiv": extract_arxiv(metadata),
        "source_parent_key": source_parent_key(metadata),
    }


def source_parent_key(metadata: dict[str, Any]) -> str | None:
    value = str(metadata.get("_sourceParentKey") or "").strip().upper()
    return value or None


def source_parent_match_reason(
    expected: dict[str, Any], candidate_item: dict[str, Any]
) -> str | None:
    expected_key = source_parent_key(expected)
    candidate = item_data(candidate_item)
    candidate_key = str(
        candidate_item.get("key") or candidate.get("key") or ""
    ).upper()
    if not expected_key or candidate_key != expected_key:
        return None
    if candidate.get("itemType") in {"attachment", "note", "annotation"}:
        return None
    if normalize_title(str(candidate.get("title") or "")) != normalize_title(
        str(expected.get("title") or "")
    ):
        return None

    for extractor in (extract_doi, extract_arxiv, extract_year):
        expected_value = extractor(expected)
        candidate_value = extractor(candidate)
        if expected_value and candidate_value and expected_value != candidate_value:
            return None
    return "source-parent-key"


def match_reason(
    expected: dict[str, Any], candidate_item: dict[str, Any]
) -> str | None:
    candidate = item_data(candidate_item)
    expected_doi = extract_doi(expected)
    if expected_doi:
        if extract_doi(candidate) == expected_doi:
            return "doi"
        return source_parent_match_reason(expected, candidate_item)

    expected_arxiv = extract_arxiv(expected)
    if expected_arxiv:
        if extract_arxiv(candidate) == expected_arxiv:
            return "arxiv"
        return source_parent_match_reason(expected, candidate_item)

    expected_year = extract_year(expected)
    if not expected_year:
        return None
    if (
        normalize_title(str(candidate.get("title") or ""))
        == normalize_title(str(expected.get("title") or ""))
        and extract_year(candidate) == expected_year
    ):
        return "title+year"
    return source_parent_match_reason(expected, candidate_item)


def summarize_item(item: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
    data = item_data(item)
    return {
        "key": item.get("key") or data.get("key"),
        "version": item.get("version") or data.get("version"),
        "itemType": data.get("itemType"),
        "title": data.get("title"),
        "year": extract_year(data),
        "DOI": extract_doi(data),
        "match": reason,
    }


def summarize_child(item: dict[str, Any]) -> dict[str, Any]:
    data = item_data(item)
    return {
        "key": item.get("key") or data.get("key"),
        "itemType": data.get("itemType"),
        "title": data.get("title"),
        "linkMode": data.get("linkMode"),
        "contentType": data.get("contentType"),
    }


def _search_terms(metadata: dict[str, Any]) -> list[str]:
    result = [
        value for value in (extract_doi(metadata), extract_arxiv(metadata)) if value
    ]
    result.append(normalize_space(str(metadata["title"])))
    return list(dict.fromkeys(result))


def _search(
    metadata: dict[str, Any],
    fetch: Any,
) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for term in _search_terms(metadata):
        query = urllib.parse.urlencode(
            {
                "q": term,
                "qmode": "everything",
                "limit": 100,
                "format": "json",
            }
        )
        payload = fetch(query)
        if not isinstance(payload, list):
            raise ZoteroError("Zotero search returned an unexpected response.")
        for item in payload:
            key = item.get("key") or item_data(item).get("key")
            if key:
                found[str(key)] = item
    return list(found.values())


def web_matches(
    metadata: dict[str, Any], *, user_id: str, api_key: str
) -> list[dict[str, Any]]:
    items = _search(
        metadata,
        lambda query: web_request(
            f"/users/{user_id}/items/top?{query}",
            user_id=user_id,
            api_key=api_key,
        ).json(),
    )
    if key := source_parent_key(metadata):
        response = web_request(
            f"/users/{user_id}/items/{key}",
            user_id=user_id,
            api_key=api_key,
            allowed=(200, 404),
        )
        if response.status == 200:
            items.append(response.json())
    deduplicated = {
        str(item.get("key") or item_data(item).get("key")): item for item in items
    }
    return [
        {**summarize_item(item, reason), "_raw": item}
        for item in deduplicated.values()
        if (reason := match_reason(metadata, item))
    ]


def local_matches(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    items = _search(
        metadata,
        lambda query: local_request(f"/api/users/0/items/top?{query}").json(),
    )
    if key := source_parent_key(metadata):
        response = local_request(
            f"/api/users/0/items/{key}", allowed=(200, 404)
        )
        if response.status == 200:
            items.append(response.json())
    deduplicated = {
        str(item.get("key") or item_data(item).get("key")): item for item in items
    }
    return [
        {**summarize_item(item, reason), "_raw": item}
        for item in deduplicated.values()
        if (reason := match_reason(metadata, item))
    ]


def public_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in match.items() if key != "_raw"}
        for match in matches
    ]


def selected_target() -> dict[str, Any]:
    payload = connector_request("/connector/getSelectedCollection", json_body={}).json()
    if not isinstance(payload, dict):
        raise ZoteroError("Connector returned an invalid target list.")
    return payload


def resolve_target(target_id: str, target_name: str | None = None) -> dict[str, Any]:
    payload = selected_target()
    targets = payload.get("targets") or []
    matches = [target for target in targets if target.get("id") == target_id]
    if len(matches) != 1:
        raise ZoteroError(f"Unknown or ambiguous Zotero target ID: {target_id}")
    target = matches[0]
    if target_name is not None and target.get("name") != target_name:
        raise ZoteroError(
            f"Target {target_id} is named {target.get('name')!r}, not {target_name!r}."
        )
    if not target.get("filesEditable"):
        raise ZoteroError(f"Target {target_id} does not allow stored files.")
    return target


def source_url(metadata: dict[str, Any]) -> str:
    raw = str(metadata.get("url") or "").strip()
    if raw.startswith(("http://", "https://")):
        return raw
    doi = extract_doi(metadata)
    if doi:
        return f"https://doi.org/{doi}"
    arxiv = extract_arxiv(metadata)
    if arxiv:
        return f"https://arxiv.org/abs/{arxiv}"
    return "https://www.zotero.org/"


def connector_item(metadata: dict[str, Any], connector_id: str) -> dict[str, Any]:
    control_fields = {"arXivID", "year", "sourceURL"}
    result = {
        key: value
        for key, value in metadata.items()
        if key not in control_fields and not key.startswith("_")
    }
    result["id"] = connector_id
    return result


def markdown_attachment_url(base_url: str, filename: str) -> str:
    # Connector derives the stored extension from the URL path; a fragment is
    # insufficient when Zotero's MIME service has no extension for text/markdown.
    # This URL identifies generated content; saveAttachment uploads the bytes.
    parts = urllib.parse.urlsplit(base_url)
    return urllib.parse.urlunsplit(parts._replace(
        path=parts.path.rstrip("/") + "/" + urllib.parse.quote(filename),
        query="",
        fragment="codex-generated",
    ))


def validate_pdf(path: Path, label: str) -> None:
    if not path.is_file():
        raise ZoteroError(f"{label} does not exist: {path}")
    with path.open("rb") as stream:
        signature = stream.read(5)
    if path.stat().st_size < 5 or signature != b"%PDF-":
        raise ZoteroError(f"{label} is not a valid PDF: {path}")


def _fetch_children_pages(
    fetch_page: Callable[[int], HttpResponse], label: str
) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    while True:
        page = fetch_page(offset).json()
        if not isinstance(page, list) or len(page) > 100:
            raise ZoteroError(f"{label} returned invalid child items.")
        keys = []
        for child in page:
            if not isinstance(child, dict):
                raise ZoteroError(f"{label} returned an invalid child item.")
            key = child.get("key") or item_data(child).get("key")
            if not isinstance(key, str) or not key:
                raise ZoteroError(f"{label} returned a child without a key.")
            keys.append(key)
        if len(set(keys)) != len(keys) or seen.intersection(keys):
            raise ZoteroError(f"{label} returned duplicate child keys or a repeated page.")
        children.extend(page)
        seen.update(keys)
        if len(page) < 100:
            return children
        offset += len(page)


def fetch_web_children(
    parent_key: str, *, user_id: str, api_key: str
) -> list[dict[str, Any]]:
    return _fetch_children_pages(
        lambda start: web_request(
            f"/users/{user_id}/items/{urllib.parse.quote(parent_key)}/children?limit=100&start={start}",
            user_id=user_id, api_key=api_key,
        ), "Web API",
    )


def fetch_local_children(parent_key: str) -> list[dict[str, Any]]:
    return _fetch_children_pages(
        lambda start: local_request(
            f"/api/users/0/items/{urllib.parse.quote(parent_key)}/children?limit=100&start={start}"
        ), "Local API",
    )


def wait_for_local_absence(parent_key: str, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        response = local_request(
            f"/api/users/0/items/{urllib.parse.quote(parent_key)}",
            allowed=(200, 404),
        )
        if response.status == 404:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1)


def wait_for_unique_local_match(
    metadata: dict[str, Any], timeout: int = 10
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while True:
        matches = local_matches(metadata)
        if len(matches) != 0 or time.monotonic() >= deadline:
            return matches
        time.sleep(0.5)


def latest_library_version(*, user_id: str, api_key: str) -> str:
    response = web_request(
        f"/users/{user_id}/items?format=versions&limit=1",
        user_id=user_id,
        api_key=api_key,
    )
    version = response.headers.get("Last-Modified-Version")
    if not version:
        raise ZoteroError("Web API omitted Last-Modified-Version.")
    return version


def stored_child_path(child: dict[str, Any]) -> Path:
    key = str(child.get("key") or item_data(child).get("key") or "")
    if not re.fullmatch(r"[A-Z0-9]{8}", key):
        raise ValueError("Invalid stored attachment key")
    response = local_request(f"/api/users/0/items/{key}/file/view/url")
    url = urllib.parse.urlsplit(response.text.strip())
    if url.scheme != "file" or url.netloc not in {"", "localhost"}:
        raise ValueError("Expected local stored-attachment file URL")
    return Path(urllib.request.url2pathname(url.path))


def file_identity(path: Path) -> dict[str, Any]:
    sha256, md5, size = hashlib.sha256(), hashlib.md5(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            sha256.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return {"sha256": sha256.hexdigest(), "md5": md5.hexdigest(), "size": size}


def verify_generated_children(
    children: list[dict[str, Any]], *, verify_archive_identity: bool = False,
    expected_hashes: dict[str, dict[str, Any]] | None = None,
    expected_parent_key: str | None = None,
) -> dict[str, Any]:
    summarized = [summarize_child(child) for child in children]
    source = [
        child
        for child in children
        if item_data(child).get("itemType") == "attachment"
        and item_data(child).get("title") == SOURCE_ATTACHMENT_TITLE
    ]
    translation = [
        child
        for child in children
        if item_data(child).get("itemType") == "attachment"
        and item_data(child).get("title") == TRANSLATION_ATTACHMENT_TITLE
    ]
    markdown = [
        child
        for child in children
        if item_data(child).get("itemType") == "attachment"
        and item_data(child).get("title") == TRANSLATION_MARKDOWN_TITLE
    ]
    summaries = [
        child
        for child in children
        if item_data(child).get("itemType") == "attachment"
        and item_data(child).get("title") == SUMMARY_ATTACHMENT_TITLE
    ]
    archives = [child for child in children
                if item_data(child).get("itemType") == "attachment"
                and item_data(child).get("title") == LATEX_SOURCE_TITLE]
    errors: list[str] = []
    if expected_hashes is not None and set(expected_hashes) != set(FINAL_FILENAMES):
        errors.append("expected hashes must describe exactly the five required files")
    if expected_parent_key is not None:
        for child in children:
            if item_data(child).get("parentItem") != expected_parent_key:
                errors.append("child belongs to a different parent")
    if len(children) != 5:
        errors.append(f"expected exactly five child items, found {len(children)}")
    if len(archives) != 1:
        errors.append(f"expected one {LATEX_SOURCE_TITLE}, found {len(archives)}")
    if len(source) != 1:
        errors.append(f"expected one {SOURCE_ATTACHMENT_TITLE}, found {len(source)}")
    if len(translation) != 1:
        errors.append(
            f"expected one {TRANSLATION_ATTACHMENT_TITLE}, found {len(translation)}"
        )
    if len(markdown) != 1:
        errors.append(
            f"expected one {TRANSLATION_MARKDOWN_TITLE}, found {len(markdown)}"
        )
    if len(summaries) != 1:
        errors.append(
            f"expected one {SUMMARY_ATTACHMENT_TITLE} Markdown attachment, "
            f"found {len(summaries)}"
        )
    for attachment in source + translation:
        data = item_data(attachment)
        if data.get("linkMode") not in {"imported_file", "imported_url"}:
            errors.append(f"attachment {data.get('title')} is not stored")
        if data.get("contentType") != "application/pdf":
            errors.append(f"attachment {data.get('title')} is not a PDF")
    for attachment in markdown + summaries:
        data = item_data(attachment)
        if data.get("linkMode") not in {"imported_file", "imported_url"}:
            errors.append(f"attachment {data.get('title')} is not stored")
        if data.get("contentType") not in {"text/markdown", "text/plain"}:
            errors.append(
                f"attachment {data.get('title')} is not readable Markdown"
            )
    for attachment in summaries:
        if Path(str(item_data(attachment).get("filename") or "")).suffix.lower() != ".md":
            errors.append("summary attachment filename must end in .md")
    for attachment in archives:
        data = item_data(attachment)
        if data.get("linkMode") not in {"imported_file", "imported_url"}:
            errors.append("LaTeX source archive is not stored")
        if data.get("contentType") != "application/zip" or Path(str(data.get("filename") or "")).suffix.lower() != ".zip":
            errors.append("LaTeX source archive must have .zip filename and application/zip MIME")
    stored_paths: dict[str, Path] = {}
    verified_files = []
    if (verify_archive_identity or expected_hashes is not None) and not errors:
        for child in children:
            data = item_data(child)
            title = data["title"]
            try:
                path = stored_child_path(child)
                actual = file_identity(path)
                stored_paths[title] = path
                if expected_hashes is not None and actual != expected_hashes[title]:
                    errors.append(f"stored file bytes differ from accepted input: {title}")
                else:
                    verified_files.append({"key": child.get("key") or data.get("key"),
                                           "title": title, **actual})
            except (ValueError, OSError, ZoteroError) as error:
                errors.append(f"Stored file verification failed for {title}: {error}")
    if verify_archive_identity and not errors:
        try:
            validate_archive(stored_paths[LATEX_SOURCE_TITLE],
                             source_pdf=stored_paths[SOURCE_ATTACHMENT_TITLE],
                             translated_pdf=stored_paths[TRANSLATION_ATTACHMENT_TITLE])
        except (ValueError, OSError, ZoteroError) as error:
            errors.append(f"LaTeX source archive verification failed: {error}")
    return {"ok": not errors, "errors": errors, "children": summarized,
            "verified_files": verified_files}


def cmd_status(args: argparse.Namespace) -> int:
    status: dict[str, Any] = {}
    try:
        if os.environ.get("ZOTERO_USER_ID") and os.environ.get("ZOTERO_API_KEY"):
            user_id, api_key = read_api_credentials(args.credential_target)
            credential = web_request(
                "/keys/current", user_id=user_id, api_key=api_key
            ).json()
            access = credential.get("access") or {}
            returned_user_id = str(credential.get("userID") or "")
            if returned_user_id != user_id:
                raise ZoteroError("ZOTERO_USER_ID does not match the API key owner.")
        else:
            credential = verify_credential(args.credential_target)
            access = credential.get("access") or {}
            returned_user_id = str(credential.get("user_id") or "")
        user_access = access.get("user") or {}
        required = ("library", "files", "notes", "write")
        missing = [
            permission for permission in required if not user_access.get(permission)
        ]
        status["credential"] = {
            "valid": not missing,
            "user_id": returned_user_id,
            "personal_library": user_access,
            "group_access": access.get("groups"),
            "missing_permissions": missing,
        }
    except Exception as exc:
        status["credential"] = {"valid": False, "error": str(exc)}

    for name, path, connector in (
        ("local_api", "/api/", False),
        ("connector", "/connector/ping", True),
    ):
        try:
            response = (
                connector_request(path, json_body={})
                if connector
                else local_request(path)
            )
            status[name] = {"ok": True, "status": response.status}
        except ZoteroError as exc:
            status[name] = {"ok": False, "error": str(exc)}
    dump_json(status)
    return (
        0
        if all(
            status.get(name, {}).get("ok", status.get(name, {}).get("valid", False))
            for name in ("credential", "local_api", "connector")
        )
        else 1
    )


def cmd_targets(args: argparse.Namespace) -> int:
    payload = selected_target()
    dump_json(
        {
            "selected": {
                "library": payload.get("libraryName"),
                "collection": payload.get("name"),
                "local_collection_id": payload.get("id"),
                "filesEditable": payload.get("filesEditable"),
            },
            "targets": payload.get("targets") or [],
        }
    )
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    metadata = load_metadata(args.metadata)
    user_id, api_key = read_api_credentials(args.credential_target)
    web = web_matches(metadata, user_id=user_id, api_key=api_key)
    local = local_matches(metadata)
    dump_json(
        {
            "identity": identity(metadata),
            "web_matches": public_matches(web),
            "local_matches": public_matches(local),
        }
    )
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    metadata = load_metadata(args.metadata)
    user_id, api_key = read_api_credentials(args.credential_target)
    matches = web_matches(metadata, user_id=user_id, api_key=api_key)
    chosen = [match for match in matches if match.get("key") == args.item_key]
    if len(chosen) != 1:
        raise ZoteroError(
            "Refusing deletion: item key is not one unambiguous exact Web API match."
        )
    children = fetch_web_children(args.item_key, user_id=user_id, api_key=api_key)
    keys = [str(item.get("key") or item_data(item).get("key")) for item in children] + [
        args.item_key
    ]
    if any(not key or key == "None" for key in keys):
        raise ZoteroError("A child item is missing its Zotero key.")
    if len(keys) > 50:
        raise ZoteroError(
            f"Refusing deletion of {len(keys)} items; Zotero batch limit is 50."
        )

    plan = {
        "executed": False,
        "parent": public_matches(chosen)[0],
        "children": [summarize_child(child) for child in children],
        "delete_keys": keys,
    }
    if not args.yes_delete:
        dump_json(plan)
        return 0

    version = latest_library_version(user_id=user_id, api_key=api_key)
    query = urllib.parse.urlencode({"itemKey": ",".join(keys)})
    response = web_request(
        f"/users/{user_id}/items?{query}",
        user_id=user_id,
        api_key=api_key,
        method="DELETE",
        headers={"If-Unmodified-Since-Version": version},
        allowed=(204,),
    )
    sync_error = None
    try:
        local_removed = wait_for_local_absence(args.item_key, args.wait_local_seconds)
    except ZoteroError as exc:
        local_removed = False
        sync_error = str(exc)
    plan.update(
        {
            "executed": True,
            "web_status": response.status,
            "local_removed": local_removed,
        }
    )
    if sync_error:
        plan["local_check_error"] = sync_error
    if not local_removed:
        plan["next_action"] = (
            "Sync Zotero Desktop, then rerun search. Do not ingest while the "
            "old local parent remains."
        )
    dump_json(plan)
    return 0 if local_removed else 3


def cmd_ingest(args: argparse.Namespace) -> int:
    metadata = load_metadata(args.metadata)
    source_pdf = Path(args.source_pdf).expanduser().resolve()
    translated_pdf = Path(args.translated_pdf).expanduser().resolve()
    translated_markdown = (
        Path(args.translated_markdown).expanduser().resolve()
    )
    summary_path = Path(args.summary).expanduser().resolve()
    latex_archive = Path(args.latex_source_archive).expanduser().resolve()
    validate_pdf(source_pdf, "Source PDF")
    validate_pdf(translated_pdf, "Translated PDF")
    try:
        validate_archive(latex_archive, source_pdf=source_pdf, translated_pdf=translated_pdf)
    except ValueError as error:
        raise ZoteroError(str(error)) from error
    if not translated_markdown.is_file():
        raise ZoteroError(
            f"Translated Markdown does not exist: {translated_markdown}"
        )
    markdown_text = translated_markdown.read_text(encoding="utf-8")
    if not normalize_space(markdown_text):
        raise ZoteroError("Translated Markdown is empty.")
    if summary_path.suffix.lower() != ".md":
        raise ZoteroError("Summary must be a Markdown (.md) file.")
    if not summary_path.is_file():
        raise ZoteroError(f"Summary does not exist: {summary_path}")
    summary = summary_path.read_text(encoding="utf-8")
    if not normalize_space(summary):
        raise ZoteroError("Summary is empty.")

    accepted_paths = {
        SOURCE_ATTACHMENT_TITLE: source_pdf,
        TRANSLATION_ATTACHMENT_TITLE: translated_pdf,
        TRANSLATION_MARKDOWN_TITLE: translated_markdown,
        SUMMARY_ATTACHMENT_TITLE: summary_path,
        LATEX_SOURCE_TITLE: latex_archive,
    }
    expected_hashes = {title: file_identity(path) for title, path in accepted_paths.items()}
    target = resolve_target(args.target_id, args.target_name)
    user_id, api_key = read_api_credentials(args.credential_target)
    local = local_matches(metadata)
    web = web_matches(metadata, user_id=user_id, api_key=api_key)
    if local or web:
        raise ZoteroError(
            "Refusing ingestion: an exact parent already exists. "
            f"local={','.join(str(match.get('key')) for match in local) or '-'} "
            f"web={','.join(str(match.get('key')) for match in web) or '-'}"
        )
    if not args.yes_ingest:
        dump_json(
            {
                "executed": False,
                "identity": identity(metadata),
                "target": target,
                "source_pdf": str(source_pdf),
                "translated_pdf": str(translated_pdf),
                "translated_markdown": str(translated_markdown),
                "summary": str(summary_path),
                "latex_source_archive": str(latex_archive),
                "expected_hashes": expected_hashes,
            }
        )
        return 0

    session_id = f"codex-{uuid.uuid4().hex}"
    connector_id = f"paper-{uuid.uuid4().hex}"
    base_url = source_url(metadata)
    receipt_path = Path(args.receipt).expanduser().resolve() if args.receipt else None
    receipt = {"schema_version": 1, "route": "connector-new-parent", "status": "creating-parent",
               "session_id": session_id, "connector_item_id": connector_id,
               "identity": identity(metadata), "target": target, "expected_hashes": expected_hashes,
               "files": [{"title": title, "path": str(path), "status": "pending"}
                         for title, path in accepted_paths.items()]}
    _write_upload_receipt(receipt_path, receipt, create=True)
    try:
        connector_request(
            "/connector/saveItems",
            json_body={
                "sessionID": session_id,
                "items": [connector_item(metadata, connector_id)],
                "uri": base_url,
            },
            allowed=(201,),
        )
        connector_request(
            "/connector/updateSession",
            json_body={
                "sessionID": session_id,
                "target": args.target_id,
                "tags": [],
            },
        )

        matches = wait_for_unique_local_match(metadata)
        if len(matches) != 1:
            raise ZoteroError(f"Cannot resolve the created parent ({len(matches)} matches).")
        receipt["parent_key"] = str(matches[0]["key"])
        _write_upload_receipt(receipt_path, receipt)
        for record in receipt["files"]:
            path, title = Path(record["path"]), record["title"]
            if title == SOURCE_ATTACHMENT_TITLE:
                url, mime = base_url, "application/pdf"
            elif title == TRANSLATION_ATTACHMENT_TITLE:
                url, mime = base_url + "#codex-chinese-translation", "application/pdf"
            else:
                url = markdown_attachment_url(base_url, path.name)
                mime = "application/zip" if title == LATEX_SOURCE_TITLE else "text/markdown"
            receipt["status"], record["status"] = "uploading-files", "uploading"
            _write_upload_receipt(receipt_path, receipt)
            connector_upload_file(path, session_id=session_id, parent_item_id=connector_id,
                                  title=title, url=url, content_type=mime)
            record["status"] = "uploaded"
            _write_upload_receipt(receipt_path, receipt)

        matches = wait_for_unique_local_match(metadata)
        if len(matches) != 1:
            raise ZoteroError(
                f"Ingestion created an ambiguous local result ({len(matches)} matches)."
            )
        parent_key = str(matches[0]["key"])
        if parent_key != receipt["parent_key"]:
            raise ZoteroError("The created parent identity changed during upload.")
        receipt["parent_key"] = parent_key
        receipt["status"] = "verifying-files"
        _write_upload_receipt(receipt_path, receipt)
        children = fetch_local_children(parent_key)
        verification = verify_generated_children(
            children, verify_archive_identity=True, expected_hashes=expected_hashes,
            expected_parent_key=parent_key,
        )
        current_target = selected_target()
        target_exists = any(
            entry.get("id") == args.target_id
            and entry.get("name") == target.get("name")
            for entry in current_target.get("targets") or []
        )
        if args.target_id.startswith("C"):
            target_selected = current_target.get("id") == int(args.target_id[1:])
        else:
            target_selected = current_target.get("id") is None and current_target.get(
                "libraryID"
            ) == int(args.target_id[1:])
        target_ok = target_exists and target_selected
        verification["target_ok"] = target_ok
        verification["target"] = {
            "id": args.target_id,
            "name": target.get("name"),
        }
        if not verification["ok"] or not target_ok:
            raise ZoteroError(
                "Ingestion completed but verification failed: "
                + "; ".join(verification.get("errors") or ["target mismatch"])
            )
        keys = {file["title"]: file["key"] for file in verification["verified_files"]}
        for record in receipt["files"]:
            record.update(key=keys[record["title"]], status="verified")
        receipt["status"] = "files-verified"
        receipt["verification"] = verification
        _write_upload_receipt(receipt_path, receipt)
    except (ZoteroError, OSError, ValueError) as exc:
        receipt["status"] = "interrupted"
        receipt["error"] = "Inspect live records and stored bytes before resuming; do not repeat new-parent import."
        _write_upload_receipt(receipt_path, receipt)
        raise ZoteroError(
            f"{exc} Connector session: {session_id}; client item alias: {connector_id}. Search the parent and "
            "inspect its children before retrying."
        ) from exc
    dump_json(
        {
            "executed": True,
            "session_id": session_id,
            "connector_item_id": connector_id,
            "receipt": str(receipt_path) if receipt_path is not None else None,
            "parent": public_matches(matches)[0],
            "verification": verification,
        }
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    metadata = load_metadata(args.metadata)
    expected_hashes = None
    if args.expected_dir:
        directory = Path(args.expected_dir).expanduser().resolve()
        try:
            expected_hashes = {title: file_identity(directory / name)
                               for title, name in FINAL_FILENAMES.items()}
        except OSError as error:
            raise ZoteroError(f"Cannot read accepted bundle: {error}") from error
    matches = local_matches(metadata)
    if len(matches) != 1:
        dump_json(
            {
                "ok": False,
                "error": f"expected one exact local parent, found {len(matches)}",
                "matches": public_matches(matches),
            }
        )
        return 1
    parent_key = str(matches[0]["key"])
    verification = verify_generated_children(
        fetch_local_children(parent_key), verify_archive_identity=True,
        expected_hashes=expected_hashes, expected_parent_key=parent_key,
    )
    verification["parent"] = public_matches(matches)[0]
    dump_json(verification)
    return 0 if verification["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely replace and ingest translated papers in Zotero."
    )
    parser.add_argument(
        "--credential-target",
        default=DEFAULT_TARGET,
        help="Windows Credential Manager target for the Zotero Web API key.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser(
        "status", help="Check credentials and Zotero endpoints"
    )
    status.set_defaults(func=cmd_status)

    targets = commands.add_parser("targets", help="List Connector save targets")
    targets.set_defaults(func=cmd_targets)

    search = commands.add_parser("search", help="Find exact parent matches")
    search.add_argument("--metadata", required=True)
    search.set_defaults(func=cmd_search)

    delete = commands.add_parser(
        "delete", help="Preview or delete one exact parent and all children"
    )
    delete.add_argument("--metadata", required=True)
    delete.add_argument("--item-key", required=True)
    delete.add_argument("--yes-delete", action="store_true")
    delete.add_argument("--wait-local-seconds", type=int, default=30)
    delete.set_defaults(func=cmd_delete)

    ingest = commands.add_parser(
        "ingest",
        help=(
            "Preview or create a parent, two stored PDFs, one stored "
            "translation Markdown attachment, one stored summary Markdown attachment, and a LaTeX source ZIP"
        ),
    )
    ingest.add_argument("--metadata", required=True)
    ingest.add_argument("--source-pdf", required=True)
    ingest.add_argument("--translated-pdf", required=True)
    ingest.add_argument("--translated-markdown", required=True)
    ingest.add_argument("--summary", required=True)
    ingest.add_argument("--latex-source-archive", required=True)
    ingest.add_argument("--target-id", required=True)
    ingest.add_argument("--target-name")
    ingest.add_argument("--yes-ingest", action="store_true")
    ingest.add_argument("--receipt", help="Persist session, client alias and per-file state for recovery; refuses an existing file.")
    ingest.set_defaults(func=cmd_ingest)

    verify = commands.add_parser(
        "verify", help="Verify the final local Zotero structure"
    )
    verify.add_argument("--metadata", required=True)
    verify.add_argument("--expected-dir", help="Compare all five stored files to this accepted standard bundle.")
    verify.set_defaults(func=cmd_verify)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if getattr(args, "wait_local_seconds", 0) < 0:
            raise ZoteroError("--wait-local-seconds must not be negative.")
        return int(args.func(args))
    except (ZoteroError, json.JSONDecodeError, UnicodeError) as exc:
        dump_json({"ok": False, "error": str(exc)}, stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
