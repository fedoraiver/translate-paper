#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyzotero==1.16.0"]
# ///
"""Deliver validated paper bundles through Pyzotero's local API."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import unicodedata
from typing import Any

from export_latex_project import validate_archive
from zotero_credentials import DEFAULT_TARGET, read_credential, read_local_credential, save_local_credential
from zotero_client import (Client, ZoteroError, authorization_lock, redact, resolve_target,
                           target_catalog)

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

SUMMARY_ATTACHMENT_TITLE = "中文论文总结"
SOURCE_ATTACHMENT_TITLE = "原文 PDF"
TRANSLATION_ATTACHMENT_TITLE = "中文翻译 PDF"
TRANSLATION_MARKDOWN_TITLE = "中文翻译 Markdown"
LATEX_SOURCE_TITLE = "LaTeX 源码包"
FINAL_FILENAMES = {
    TRANSLATION_ATTACHMENT_TITLE: "中文翻译.pdf",
    LATEX_SOURCE_TITLE: "LaTeX源码包.zip",
}
LOCAL_ONLY_TITLES = {TRANSLATION_MARKDOWN_TITLE, SUMMARY_ATTACHMENT_TITLE}


def needs_paper_source(metadata: dict[str, Any]) -> bool:
    return metadata.get("itemType") != "book"


def delivery_filenames(include_source: bool) -> dict[str, str]:
    return ({SOURCE_ATTACHMENT_TITLE: "原文 PDF.pdf", **FINAL_FILENAMES}
            if include_source else dict(FINAL_FILENAMES))


def dump_json(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream)


def _write_upload_receipt(path: Path, receipt: dict[str, Any], *, create: bool = False) -> None:
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


def validate_pdf(path: Path, label: str) -> None:
    if not path.is_file():
        raise ZoteroError(f"{label} does not exist: {path}")
    with path.open("rb") as stream:
        signature = stream.read(5)
    if path.stat().st_size < 5 or signature != b"%PDF-":
        raise ZoteroError(f"{label} is not a valid PDF: {path}")


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
    client: Client | None = None, title_prefix: str = "", owned_only: bool = False,
    include_source: bool = False,
) -> dict[str, Any]:
    required = delivery_filenames(include_source)
    titles = {title: attachment_title(title, title_prefix) for title in required}
    local_only = {attachment_title(title, title_prefix) for title in LOCAL_ONLY_TITLES}
    children = [child for child in children if item_data(child).get("title") not in local_only]
    if title_prefix or owned_only:
        children = [child for child in children if item_data(child).get("title") in titles.values()]
    children = copy.deepcopy(children)
    for child in children:
        original = item_data(child).get("title")
        for standard, actual in titles.items():
            if original == actual:
                item_data(child)["title"] = standard
    summarized = [summarize_child(child) for child in children]
    translation = [
        child
        for child in children
        if item_data(child).get("itemType") == "attachment"
        and item_data(child).get("title") == TRANSLATION_ATTACHMENT_TITLE
    ]
    archives = [child for child in children
                if item_data(child).get("itemType") == "attachment"
                and item_data(child).get("title") == LATEX_SOURCE_TITLE]
    errors: list[str] = []
    allowed_expected = [set(FINAL_FILENAMES), set(required)]
    if include_source:
        allowed_expected.append({SOURCE_ATTACHMENT_TITLE})
    if expected_hashes is not None and set(expected_hashes) not in allowed_expected:
        errors.append("expected hashes must describe the required delivery files")
    if expected_parent_key is not None:
        for child in children:
            if item_data(child).get("parentItem") != expected_parent_key:
                errors.append("child belongs to a different parent")
    if len(children) != len(required):
        errors.append(f"expected exactly {len(required)} delivery child items, found {len(children)}")
    originals = [child for child in children if item_data(child).get("title") == SOURCE_ATTACHMENT_TITLE]
    if include_source and len(originals) != 1:
        errors.append(f"expected one {SOURCE_ATTACHMENT_TITLE}, found {len(originals)}")
    if len(archives) != 1:
        errors.append(f"expected one {LATEX_SOURCE_TITLE}, found {len(archives)}")
    if len(translation) != 1:
        errors.append(
            f"expected one {TRANSLATION_ATTACHMENT_TITLE}, found {len(translation)}"
        )
    for attachment in translation + (originals if include_source else []):
        data = item_data(attachment)
        if data.get("linkMode") not in {"imported_file", "imported_url"}:
            errors.append(f"attachment {data.get('title')} is not stored")
        if data.get("contentType") != "application/pdf":
            errors.append(f"attachment {data.get('title')} is not a PDF")
    for attachment in archives:
        data = item_data(attachment)
        if data.get("linkMode") not in {"imported_file", "imported_url"}:
            errors.append("LaTeX source archive is not stored")
        if data.get("contentType") != "application/zip" or Path(str(data.get("filename") or "")).suffix.lower() != ".zip":
            errors.append("LaTeX source archive must have .zip filename and application/zip MIME")
    verified_files = []
    if (verify_archive_identity or expected_hashes is not None) and not errors:
        with verification_files(children, client=client) as stored_paths:
            for child in children:
                data = item_data(child)
                title = data["title"]
                try:
                    actual = file_identity(stored_paths[title])
                    if expected_hashes is not None and title in expected_hashes and actual != expected_hashes[title]:
                        errors.append(f"stored file bytes differ from accepted input: {title}")
                    else:
                        verified_files.append({"key": child.get("key") or data.get("key"),
                                               "title": title, **actual})
                except (OSError, ValueError) as error:
                    errors.append(f"Stored file verification failed for {title}: {error}")
            if verify_archive_identity and not errors:
                try:
                    validate_archive(stored_paths[LATEX_SOURCE_TITLE],
                                     source_pdf=stored_paths[SOURCE_ATTACHMENT_TITLE] if include_source else None,
                                     translated_pdf=stored_paths[TRANSLATION_ATTACHMENT_TITLE])
                except (OSError, ValueError) as error:
                    errors.append(f"LaTeX source archive verification failed: {error}")
    return {"ok": not errors, "errors": errors, "children": summarized,
            "verified_files": verified_files}


@contextmanager
def use_client(client: Client | None = None, *, library_type: str = "user", library_id: str = "0"):
    if client is not None:
        yield client
    else:
        with Client(library_type, library_id) as owned:
            yield owned


def _matches(metadata: dict[str, Any], client: Client) -> list[dict[str, Any]]:
    found = {}
    for term in _search_terms(metadata):
        for item in client.all("top", q=term, qmode="everything"):
            found[item["key"]] = item
    if key := source_parent_key(metadata):
        item = client.item(key, missing_ok=True)
        if item is not None:
            found[key] = item
    return [{**summarize_item(item, reason), "_raw": item} for item in found.values()
            if item_data(item).get("itemType") not in {"attachment", "note", "annotation"}
            and (reason := match_reason(metadata, item))]


def local_matches(metadata: dict[str, Any], *, client: Client | None = None) -> list[dict[str, Any]]:
    with use_client(client) as current:
        return _matches(metadata, current)


def web_matches(metadata: dict[str, Any], *, user_id: str, api_key: str) -> list[dict[str, Any]]:
    with Client(library_id=user_id, local=False, api_key=api_key) as current:
        return _matches(metadata, current)


def public_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in row.items() if key != "_raw"} for row in matches]


def fetch_local_children(parent_key: str, *, client: Client | None = None) -> list[dict[str, Any]]:
    with use_client(client) as current:
        return current.all("children", parent_key)


@contextmanager
def verification_files(children: list[dict[str, Any]], *, client: Client | None = None):
    with use_client(client) as current, tempfile.TemporaryDirectory(prefix="zotero-verify-") as directory:
        paths = {}
        for index, child in enumerate(children):
            data = item_data(child)
            suffix = Path(data.get("filename", "")).suffix
            path = Path(directory) / f"{index}{suffix}"
            try:
                current.download(child.get("key") or data["key"], path)
            except ZoteroError as exc:
                if exc.status != 404:
                    raise
            paths[data["title"]] = path
        yield paths


def attachment_title(role: str, prefix: str) -> str:
    return f"{prefix} / {role}" if prefix else role


def new_key() -> str:
    return "".join(secrets.choice("23456789ABCDEFGHIJKLMNPQRSTUVWXYZ") for _ in range(8))


def parent_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in metadata.items()
              if not key.startswith("_") and key not in {"arXivID", "id", "key", "version"}}
    if metadata.get("arXivID"):
        result["extra"] = (result.get("extra", "") + "\narXiv: " + metadata["arXivID"]).strip()
    return result


def target_matches(parent: dict[str, Any], target: dict[str, Any]) -> bool:
    library = parent.get("library", {})
    if library.get("type") != target["library_type"]:
        return False
    if target["library_type"] == "group" and str(library.get("id")) != target["library_id"]:
        return False
    collection = target.get("collection_key")
    return not collection or collection in item_data(parent).get("collections", [])


def _metadata_without_reading_changes(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item_data(item).items() if key not in {"version", "lastRead"}}


def _stored_identity(client: Client, item: dict[str, Any]) -> dict[str, Any] | None:
    with verification_files([item], client=client) as paths:
        path = paths[item_data(item)["title"]]
        return file_identity(path) if path.is_file() else None


def _snapshot(client: Client, children: list[dict[str, Any]], owned_titles: set[str]) -> list[dict[str, Any]]:
    baseline = []
    for child in children:
        data = item_data(child)
        if data.get("title") in owned_titles:
            continue
        record = {"item": child}
        if data.get("itemType") == "attachment" and data.get("linkMode") in {"imported_file", "imported_url"}:
            record["file_identity"] = _stored_identity(client, child)
        baseline.append(record)
    return baseline


def _assert_preserved(client: Client, receipt: dict[str, Any], parent: dict[str, Any], children: list[dict[str, Any]]):
    if receipt.get("parent_baseline") and _metadata_without_reading_changes(parent) != _metadata_without_reading_changes(receipt["parent_baseline"]):
        raise ZoteroError("Existing parent metadata changed during delivery.")
    live = {child["key"]: child for child in children}
    for record in receipt.get("baseline_children", []):
        before = record["item"]
        after = live.get(before["key"])
        if (after is None or _metadata_without_reading_changes(before) != _metadata_without_reading_changes(after)
                or int(after.get("version", 0)) < int(before.get("version", 0))):
            raise ZoteroError("A pre-existing unrelated child changed or disappeared.")
        if "file_identity" in record and _stored_identity(client, after) != record["file_identity"]:
            raise ZoteroError("A pre-existing stored file changed during delivery.")


def _accepted_files(args: argparse.Namespace, metadata: dict[str, Any]) -> dict[str, Path]:
    paths = {TRANSLATION_ATTACHMENT_TITLE: Path(args.translated_pdf).resolve(),
             LATEX_SOURCE_TITLE: Path(args.latex_source_archive).resolve()}
    if needs_paper_source(metadata):
        if not args.source_pdf:
            raise ZoteroError("Paper delivery requires --source-pdf for the original PDF attachment.")
        paths[SOURCE_ATTACHMENT_TITLE] = Path(args.source_pdf).resolve()
    _validate_files(paths)
    if args.source_pdf:
        source = Path(args.source_pdf).resolve()
        validate_pdf(source, "Source PDF")
        validate_archive(paths[LATEX_SOURCE_TITLE], source_pdf=source,
                         translated_pdf=paths[TRANSLATION_ATTACHMENT_TITLE])
    return paths


def _validate_files(paths: dict[str, Path]):
    validate_pdf(paths[TRANSLATION_ATTACHMENT_TITLE], "Translated PDF")
    if SOURCE_ATTACHMENT_TITLE in paths:
        validate_pdf(paths[SOURCE_ATTACHMENT_TITLE], "Source PDF")
    validate_archive(paths[LATEX_SOURCE_TITLE],
                     source_pdf=paths.get(SOURCE_ATTACHMENT_TITLE),
                     translated_pdf=paths[TRANSLATION_ATTACHMENT_TITLE])


def _receipt_files(receipt: dict[str, Any]) -> dict[str, Path]:
    records = receipt["files"]
    required = delivery_filenames(needs_paper_source(receipt["metadata"]))
    historical = set(FINAL_FILENAMES) | LOCAL_ONLY_TITLES | {SOURCE_ATTACHMENT_TITLE}
    if len(records) == 5 and {r["role"] for r in records} == historical:
        receipt["local_only_files"] = [r for r in records if r["role"] not in required]
        receipt["local_only_hashes"] = {role: digest for role, digest in receipt["expected_hashes"].items()
                                      if role not in required}
        records = receipt["files"] = [r for r in records if r["role"] in required]
        receipt["expected_hashes"] = {role: receipt["expected_hashes"][role] for role in required}
    if len(records) != len(required) or {r["role"] for r in records} != set(required):
        raise ZoteroError("Receipt must include the paper original, translation PDF and ZIP, or a chapter's PDF and ZIP. Supply resume --source-pdf for an older two-file paper receipt.")
    paths = {r["role"]: Path(r["path"]) for r in records}
    if {role: file_identity(path) for role, path in paths.items()} != receipt["expected_hashes"]:
        raise ZoteroError("Accepted local files changed since the receipt was recorded.")
    _validate_files(paths)
    for record in records:
        if record["title"] != attachment_title(record["role"], receipt["title_prefix"]):
            raise ZoteroError("Receipt attachment title does not match its role.")
    return paths


def _parent(client: Client, key: str, metadata: dict[str, Any]) -> dict[str, Any]:
    item = client.item(key)
    if item_data(item).get("itemType") in {"attachment", "note", "annotation"} or not match_reason(metadata, item):
        raise ZoteroError("The specified parent does not match the exact bibliographic identity.")
    return item


def _check_attachment(child: dict[str, Any], record: dict[str, Any], parent_key: str):
    data = item_data(child)
    if (data.get("parentItem") != parent_key or data.get("itemType") != "attachment"
            or data.get("title") != record["title"]
            or data.get("linkMode") not in {"imported_file", "imported_url"}
            or Path(data.get("filename", "")).suffix.lower() != Path(record["path"]).suffix.lower()
            or data.get("contentType") not in ({"text/markdown", "text/plain"}
                if record["mime"] == "text/markdown" else {record["mime"]})):
        raise ZoteroError("An attachment has a conflicting parent, title, type, or filename.")


def _reconcile(client: Client, receipt: dict[str, Any], receipt_path: Path):
    children = fetch_local_children(receipt["parent_key"], client=client)
    for record in receipt["files"]:
        candidates = [c for c in children if item_data(c).get("title") == record["title"]]
        keyed = client.item(record["key"], missing_ok=True)
        if len(candidates) > 1 or (keyed is not None and candidates and candidates[0]["key"] != keyed["key"]):
            raise ZoteroError("Duplicate titles or conflicting attachment keys; no files were overwritten.")
        if candidates:
            child = candidates[0]
            if record.get("confirmed") and record["key"] != child["key"]:
                raise ZoteroError("A confirmed attachment key changed.")
            record["key"] = child["key"]
        else:
            child = keyed
        if child is not None:
            _check_attachment(child, record, receipt["parent_key"])
            actual = _stored_identity(client, child)
            if actual is not None and actual != receipt["expected_hashes"][record["role"]]:
                raise ZoteroError("An existing attachment contains different bytes; refusing overwrite.")
            record["status"] = "verified" if actual is not None else "metadata-created"
            record["confirmed"] = True
        elif record.get("confirmed"):
            raise ZoteroError("A previously confirmed attachment disappeared.")
        else:
            record["status"] = "pending"
    _write_upload_receipt(receipt_path, receipt)


def _finish(client: Client, receipt: dict[str, Any], receipt_path: Path):
    parent = _parent(client, receipt["parent_key"], receipt["metadata"])
    if not target_matches(parent, receipt["target"]):
        raise ZoteroError("Parent is not in the requested library/collection.")
    children = fetch_local_children(receipt["parent_key"], client=client)
    _assert_preserved(client, receipt, parent, children)
    verification = verify_generated_children(children, verify_archive_identity=True,
        expected_hashes=receipt["expected_hashes"], expected_parent_key=receipt["parent_key"],
        client=client, title_prefix=receipt["title_prefix"], owned_only=receipt["mode"] == "attach",
        include_source=needs_paper_source(receipt["metadata"]))
    verification.update(target_ok=True, target=receipt["target"], server_id=client.server_id)
    if not verification["ok"]:
        raise ZoteroError("Stored-file verification failed: " + "; ".join(verification["errors"]))
    receipt.update(status="files-verified", verification=verification)
    receipt.pop("error", None)
    _write_upload_receipt(receipt_path, receipt)
    return {"executed": True, "source": "local", "receipt": str(receipt_path),
            "parent": summarize_item(parent), "verification": verification}


def _deliver(client: Client, receipt: dict[str, Any], receipt_path: Path):
    try:
        _receipt_files(receipt)
        if client.server_id != receipt["server_id"]:
            raise ZoteroError("Receipt belongs to a different Zotero server.")
        current = client.item(receipt["parent_key"], missing_ok=True)
        if current is None:
            if receipt["mode"] != "ingest" or receipt.get("parent_confirmed"):
                raise ZoteroError("The existing or confirmed parent disappeared.")
            if local_matches(receipt["metadata"], client=client):
                raise ZoteroError("An exact parent appeared under a different key; refusing duplicate creation.")
        else:
            _parent(client, receipt["parent_key"], receipt["metadata"])
            if not target_matches(current, receipt["target"]):
                raise ZoteroError("Parent library/collection changed.")
            _reconcile(client, receipt, receipt_path)
            _assert_preserved(client, receipt, current, fetch_local_children(receipt["parent_key"], client=client))
        if current is not None and all(r["status"] == "verified" for r in receipt["files"]):
            return _finish(client, receipt, receipt_path)
        client.require_write_key()
        if current is None:
            data = {**parent_metadata(receipt["metadata"]), "key": receipt["parent_key"], "version": 0,
                    "collections": [receipt["target"]["collection_key"]] if receipt["target"].get("collection_key") else []}
            client.create(data)
        receipt["parent_confirmed"] = True
        _write_upload_receipt(receipt_path, receipt)
        for record in receipt["files"]:
            if record["status"] == "verified":
                continue
            if record["status"] == "pending":
                record["status"] = "creating-metadata"
                _write_upload_receipt(receipt_path, receipt)
                client.create({"key": record["key"], "version": 0, "itemType": "attachment",
                    "parentItem": receipt["parent_key"], "title": record["title"],
                    "linkMode": "imported_file", "contentType": record["mime"],
                    "filename": Path(record["path"]).name})
                record.update(status="metadata-created", confirmed=True)
                _write_upload_receipt(receipt_path, receipt)
            record["status"] = "uploading"
            _write_upload_receipt(receipt_path, receipt)
            child = client.item(record["key"])
            _check_attachment(child, record, receipt["parent_key"])
            client.upload(child, Path(record["path"]))
            record["status"] = "uploaded"
            _write_upload_receipt(receipt_path, receipt)
        _reconcile(client, receipt, receipt_path)
        return _finish(client, receipt, receipt_path)
    except (ZoteroError, OSError, ValueError) as exc:
        receipt.update(status="interrupted", error=redact(str(exc), client.zot.local_api_key, client.zot.api_key))
        _write_upload_receipt(receipt_path, receipt)
        raise ZoteroError(f"{receipt['error']} Resume from {receipt_path} after resolving the cause.") from None


def cmd_delivery(args: argparse.Namespace) -> int:
    metadata = load_metadata(args.metadata)
    paths = _accepted_files(args, metadata)
    target = resolve_target(args.target_id, args.target_name)
    mode = args.command
    receipt_path = Path(args.receipt).resolve() if args.receipt else Path(args.metadata).resolve().parent / "zotero-upload.json"
    if receipt_path.exists():
        raise ZoteroError("Upload receipt already exists; use resume instead of repeating the import.")
    with Client(target["library_type"], target["library_id"]) as client:
        baseline, parent = [], None
        if mode == "ingest":
            if local_matches(metadata, client=client):
                raise ZoteroError("Refusing ingestion: an exact parent already exists. Use attach for an existing parent.")
            parent_key = new_key()
        else:
            parent_key = args.parent_key.upper()
            parent = _parent(client, parent_key, metadata)
            if not target_matches(parent, target):
                raise ZoteroError("Existing parent is not in the requested destination.")
            children = fetch_local_children(parent_key, client=client)
            owned = {attachment_title(role, args.title_prefix.strip()) for role in paths}
            baseline = _snapshot(client, children, owned)
        receipt = {"schema_version": 2, "route": "pyzotero-local", "mode": mode,
            "status": "prepared", "server_id": client.server_id, "target": target,
            "metadata": metadata, "parent_key": parent_key, "parent_confirmed": mode == "attach",
            "parent_baseline": parent, "baseline_children": baseline,
            "title_prefix": args.title_prefix.strip(),
            "expected_hashes": {role: file_identity(path) for role, path in paths.items()},
            "files": [{"role": role, "title": attachment_title(role, args.title_prefix.strip()),
                       "path": str(path), "mime": {".pdf": "application/pdf", ".md": "text/markdown", ".zip": "application/zip"}[path.suffix.lower()],
                       "key": new_key(), "status": "pending", "confirmed": False} for role, path in paths.items()]}
        if not getattr(args, "yes_" + mode):
            dump_json({"executed": False, "source": "local", "receipt": str(receipt_path), "plan": receipt})
            return 0
        _write_upload_receipt(receipt_path, receipt, create=True)
        dump_json(_deliver(client, receipt, receipt_path))
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    return cmd_delivery(args)


def cmd_attach_source(args: argparse.Namespace) -> int:
    metadata = load_metadata(args.metadata)
    if metadata.get("itemType") == "book":
        raise ZoteroError("attach-source restores paper originals only; preserve the book's full PDF.")
    source = Path(args.source_pdf).resolve()
    validate_pdf(source, "Source PDF")
    digest = file_identity(source)
    if args.expected_sha256 and digest["sha256"] != args.expected_sha256:
        raise ZoteroError("Original PDF does not match the recorded source hash.")
    target = resolve_target(args.target_id, args.target_name)
    path = Path(args.receipt).resolve()
    with Client(target["library_type"], target["library_id"]) as client:
        parent = _parent(client, args.parent_key.upper(), metadata)
        if not target_matches(parent, target):
            raise ZoteroError("Paper parent is not in the requested destination.")
        if path.exists():
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if (receipt.get("mode") != "attach-source" or receipt.get("schema_version") != 1
                    or receipt["server_id"] != client.server_id or receipt["parent_key"] != parent["key"]
                    or receipt["target"] != target or receipt["expected_hashes"] != {SOURCE_ATTACHMENT_TITLE: digest}):
                raise ZoteroError("Original-PDF recovery receipt conflicts with the current inputs.")
        else:
            receipt = {"schema_version": 1, "route": "pyzotero-local", "mode": "attach-source",
                "server_id": client.server_id, "parent_key": parent["key"], "metadata": metadata,
                "target": target, "parent_baseline": parent, "title_prefix": "", "status": "prepared",
                "baseline_children": _snapshot(client, fetch_local_children(parent["key"], client=client),
                                                {SOURCE_ATTACHMENT_TITLE}),
                "expected_hashes": {SOURCE_ATTACHMENT_TITLE: digest},
                "files": [{"role": SOURCE_ATTACHMENT_TITLE, "title": SOURCE_ATTACHMENT_TITLE,
                    "path": str(source), "mime": "application/pdf", "key": new_key(),
                    "confirmed": False, "status": "pending"}]}
        if (len(receipt["files"]) != 1 or receipt["files"][0]["role"] != SOURCE_ATTACHMENT_TITLE
                or receipt["files"][0]["title"] != SOURCE_ATTACHMENT_TITLE
                or receipt["files"][0]["mime"] != "application/pdf"):
            raise ZoteroError("Original-PDF recovery may only own one source attachment.")
        if not args.yes_attach_source:
            dump_json({"executed": False, "parent": summarize_item(parent), "source": str(source),
                       "identity": digest, "receipt": str(path)})
            return 0
        try:
            _write_upload_receipt(path, receipt)
            _reconcile(client, receipt, path)
            _assert_preserved(client, receipt, parent, fetch_local_children(parent["key"], client=client))
            record = receipt["files"][0]
            if record["status"] != "verified":
                client.require_write_key()
                if record["status"] == "pending":
                    record["status"] = "creating-metadata"
                    _write_upload_receipt(path, receipt)
                    client.create({"key": record["key"], "version": 0, "itemType": "attachment",
                        "parentItem": parent["key"], "title": SOURCE_ATTACHMENT_TITLE,
                        "linkMode": "imported_file", "contentType": "application/pdf", "filename": source.name})
                record["status"] = "uploading"
                _write_upload_receipt(path, receipt)
                client.upload(client.item(record["key"]), source)
                _reconcile(client, receipt, path)
            parent = _parent(client, parent["key"], metadata)
            _assert_preserved(client, receipt, parent, fetch_local_children(parent["key"], client=client))
            if receipt["files"][0]["status"] != "verified":
                raise ZoteroError("Stored original PDF has not been byte-verified.")
            receipt.update(status="files-verified")
            receipt.pop("error", None)
            _write_upload_receipt(path, receipt)
            dump_json({"executed": True, "status": "files-verified", "parent_key": parent["key"],
                       "attachment_key": record["key"], "identity": digest, "receipt": str(path)})
            return 0
        except (ZoteroError, OSError, ValueError) as exc:
            receipt.update(status="interrupted", error=redact(str(exc), client.zot.local_api_key, client.zot.api_key))
            _write_upload_receipt(path, receipt)
            raise


def _legacy_receipt(client: Client, original: dict[str, Any], path: Path, parent_key: str | None):
    key = parent_key or original.get("parent_key")
    if not key or (parent_key and original.get("parent_key") not in {None, parent_key}):
        raise ZoteroError("Specify the original exact --parent-key to recover this legacy receipt.")
    ident = original["identity"]
    metadata = {"title": ident["title"], "date": ident.get("year"), "DOI": ident.get("doi"),
                "arXivID": ident.get("arxiv"), "_sourceParentKey": ident.get("source_parent_key")}
    parent = _parent(client, key, metadata)
    if not target_matches(parent, resolve_target(original["target"]["id"], original["target"].get("name"))):
        raise ZoteroError("Legacy parent is not in the original target.")
    converted = path.with_name(path.stem + ".pyzotero.json")
    if converted.exists():
        raise ZoteroError(f"A converted receipt already exists; resume {converted}.")
    receipt = {"schema_version": 2, "route": "pyzotero-local", "mode": "ingest", "status": "prepared",
        "server_id": client.server_id, "metadata": item_data(parent), "parent_key": key,
        "parent_confirmed": True, "parent_baseline": parent, "baseline_children": [], "title_prefix": "",
        "target": resolve_target(original["target"]["id"], original["target"].get("name")),
        "expected_hashes": original["expected_hashes"], "legacy_receipt": str(path),
        "files": [{"role": r["title"], "title": r["title"], "path": r["path"],
                   "mime": {".pdf": "application/pdf", ".md": "text/markdown", ".zip": "application/zip"}[Path(r["path"]).suffix.lower()],
                   "key": r.get("key") or new_key(), "confirmed": bool(r.get("key")), "status": "pending"}
                  for r in original["files"]]}
    _receipt_files(receipt)
    return receipt, converted


def cmd_resume(args: argparse.Namespace) -> int:
    path = Path(args.receipt).resolve()
    receipt = json.loads(path.read_text(encoding="utf-8"))
    legacy = receipt.get("schema_version") == 1 and receipt.get("route") == "connector-new-parent"
    if not legacy and (receipt.get("schema_version") != 2 or receipt.get("route") != "pyzotero-local"):
        raise ZoteroError("Unsupported delivery receipt.")
    target = resolve_target(receipt["target"]["id"], receipt["target"].get("name"))
    with Client(target["library_type"], target["library_id"], server_id=None if legacy else receipt["server_id"]) as client:
        if legacy:
            receipt, path = _legacy_receipt(client, receipt, path, args.parent_key)
        elif args.parent_key and args.parent_key.upper() != receipt["parent_key"]:
            raise ZoteroError("Explicit parent key conflicts with the saved receipt.")
        if args.source_pdf and needs_paper_source(receipt["metadata"]):
            if not any(r["role"] == SOURCE_ATTACHMENT_TITLE for r in receipt["files"]):
                source = Path(args.source_pdf).resolve()
                validate_pdf(source, "Source PDF")
                receipt["files"].append({"role": SOURCE_ATTACHMENT_TITLE, "title": SOURCE_ATTACHMENT_TITLE,
                    "path": str(source), "mime": "application/pdf", "key": new_key(),
                    "confirmed": False, "status": "pending"})
                receipt["expected_hashes"][SOURCE_ATTACHMENT_TITLE] = file_identity(source)
        _receipt_files(receipt)
        if not args.yes_resume:
            dump_json({"executed": False, "receipt": str(path), "parent_key": receipt["parent_key"],
                       "status": receipt["status"], "server_id": client.server_id})
            return 0
        if legacy:
            _write_upload_receipt(path, receipt, create=True)
        dump_json(_deliver(client, receipt, path))
    return 0


def cmd_authorize(args: argparse.Namespace) -> int:
    with Client(timeout=args.timeout_seconds) as client:
        server = client.server_id
        with authorization_lock(server):
            result = client.call("authorize_local", "translate-paper")
            saved = save_local_credential(server, result["key"], remember=result["remember"])
        dump_json({"authorized": True, "remembered": saved, "server_id": server,
                   "next_action": "Run or resume delivery." if saved else
                   "One-time authorization was not saved; choose Always Allow for a resumable multi-write delivery."})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with Client(args.library_type, args.library_id) as client:
        server = client.server_id
        try:
            stored = bool(read_local_credential(server))
        except (OSError, RuntimeError) as exc:
            raise ZoteroError(f"Cannot read local credential store: {exc}") from None
        dump_json({"source": "local", "local_api": {"ok": True, "server_id": server},
                   "local_authorization": {"stored": stored, "validated_by_write": False},
                   "web_credentials_required": False})
    return 0


def cmd_targets(args: argparse.Namespace) -> int:
    dump_json(target_catalog())
    return 0


def _web_credentials(target: str) -> tuple[str, str]:
    user, key = os.environ.get("ZOTERO_USER_ID"), os.environ.get("ZOTERO_API_KEY")
    if bool(user) != bool(key):
        raise ZoteroError("Set both ZOTERO_USER_ID and ZOTERO_API_KEY, or neither.")
    return (user, key) if user and key else read_credential(target)


def cmd_search(args: argparse.Namespace) -> int:
    metadata = load_metadata(args.metadata)
    with Client(args.library_type, args.library_id) as client:
        result = {"source": "local", "identity": identity(metadata), "server_id": client.server_id,
                  "local_matches": public_matches(local_matches(metadata, client=client))}
    if args.include_web:
        if args.library_type != "user":
            raise ZoteroError("Web comparison currently supports the configured personal library only.")
        user, key = _web_credentials(args.credential_target)
        result["web_matches"] = public_matches(web_matches(metadata, user_id=user, api_key=key))
    dump_json(result)
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    metadata = load_metadata(args.metadata)
    with Client(args.library_type, args.library_id) as client:
        version = client.library_version()
        matches = local_matches(metadata, client=client)
        if len(matches) != 1 or matches[0]["key"] != args.item_key.upper():
            raise ZoteroError("Refusing deletion: item key is not one unambiguous exact local match.")
        children = fetch_local_children(args.item_key, client=client)
        objects = children + [matches[0]["_raw"]]
        if len(objects) > 50:
            raise ZoteroError("Refusing deletion of more than 50 items.")
        plan = {"executed": False, "source": "local", "server_id": client.server_id,
                "parent": public_matches(matches)[0], "children": [summarize_child(c) for c in children],
                "delete_keys": [o["key"] for o in objects]}
        if args.yes_delete:
            client.require_write_key()
            client.call("delete_item", objects, last_modified=version)
            if client.item(args.item_key, missing_ok=True) is not None:
                raise ZoteroError("Deletion response succeeded but the local parent still exists.")
            plan.update(executed=True, local_removed=True)
        dump_json(plan)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    metadata = load_metadata(args.metadata)
    target = resolve_target(args.target_id, args.target_name) if args.target_id else None
    kind, lib_id = (target["library_type"], target["library_id"]) if target else (args.library_type, args.library_id)
    with Client(kind, lib_id) as client:
        if args.parent_key:
            parent = _parent(client, args.parent_key, metadata)
        else:
            matches = local_matches(metadata, client=client)
            if len(matches) != 1:
                raise ZoteroError(f"Expected one exact local parent, found {len(matches)}.")
            parent = matches[0]["_raw"]
        expected = ({role: file_identity(Path(args.expected_dir) / filename)
                    for role, filename in FINAL_FILENAMES.items()} if args.expected_dir else None)
        include_source = needs_paper_source(metadata)
        if args.source_pdf and include_source:
            expected = expected or {}
            expected[SOURCE_ATTACHMENT_TITLE] = file_identity(Path(args.source_pdf))
        result = verify_generated_children(fetch_local_children(parent["key"], client=client),
            verify_archive_identity=True, expected_hashes=expected, expected_parent_key=parent["key"],
            client=client, title_prefix=args.title_prefix, owned_only=bool(args.parent_key),
            include_source=include_source)
        if target and not target_matches(parent, target):
            result["ok"] = False
            result["errors"].append("Parent is not in the requested library/collection.")
        result.update(parent=summarize_item(parent), server_id=client.server_id, source="local")
        dump_json(result)
        return 0 if result["ok"] else 1


def _cleanup_role(title: str) -> tuple[str, str] | None:
    match = re.fullmatch(
        r"(?:(第\s*\d+\s*章)(?:\s*/\s*|\s+))?"
        r"(原文 PDF|原文节选 PDF|中文翻译 Markdown|中文论文总结|中文翻译 PDF|LaTeX 源码包)", title,
    )
    return (re.sub(r"\s+", "", match[1] or ""), match[2]) if match else None


def _cleanup_identity(client: Client, item: dict[str, Any]) -> dict[str, Any]:
    data = client.call("file", item["key"])
    return {"sha256": hashlib.sha256(data).hexdigest(),
            "md5": hashlib.md5(data).hexdigest(), "size": len(data)}


def prepare_cleanup_manifest(client: Client, parent_keys: list[str]) -> dict[str, Any]:
    """Snapshot explicitly selected historical deliveries without saving document copies."""
    all_items = client.all("items")
    parents = []
    for key in dict.fromkeys(parent_keys):
        parent = client.item(key)
        if item_data(parent).get("itemType") in {"attachment", "note", "annotation"}:
            raise ZoteroError("Cleanup requires an existing bibliographic parent.")
        children = fetch_local_children(key, client=client)
        roles: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for child in children:
            role = _cleanup_role(item_data(child).get("title", ""))
            if role:
                roles.setdefault(role, []).append(child)
        remove = []
        for (prefix, role), matches in roles.items():
            if role not in LOCAL_ONLY_TITLES | {SOURCE_ATTACHMENT_TITLE, "原文节选 PDF"}:
                continue
            if role.startswith("原文") and not (
                prefix and item_data(parent).get("itemType") == "book"
                and any(item_data(c).get("contentType") == "application/pdf"
                        and _cleanup_role(item_data(c).get("title", "")) is None for c in children)
            ):
                continue
            pdfs = roles.get((prefix, TRANSLATION_ATTACHMENT_TITLE), [])
            if len(matches) != 1 or len(pdfs) != 1 or item_data(pdfs[0]).get("contentType") != "application/pdf":
                raise ZoteroError("Cleanup requires a unique historical role and matching translation PDF.")
            child = matches[0]
            data = item_data(child)
            expected_mimes = {"application/pdf"} if role.startswith("原文") else {"text/markdown", "text/plain"}
            if (data.get("itemType") != "attachment" or data.get("parentItem") != key
                    or data.get("linkMode") not in {"imported_file", "imported_url"}
                    or data.get("contentType") not in expected_mimes):
                raise ZoteroError("Historical cleanup attachment has an unexpected type or parent.")
            if any(item_data(row).get("parentItem") == child["key"] for row in all_items):
                raise ZoteroError("A cleanup attachment has child notes or annotations; preserve it for review.")
            remove.append({"item": child, "file_identity": _cleanup_identity(client, child), "status": "pending"})
        if not remove:
            continue
        removed_keys = {record["item"]["key"] for record in remove}
        kept = [child for child in children if child["key"] not in removed_keys]
        baseline = _snapshot(client, kept, set())
        kept_keys = {child["key"] for child in kept}
        descendants = [row for row in all_items if item_data(row).get("parentItem") in kept_keys]
        parents.append({"parent_key": key, "metadata": parent_metadata(item_data(parent)),
                        "parent_baseline": parent, "baseline_children": baseline,
                        "baseline_annotations": descendants, "remove": remove})
    return {"schema_version": 1, "kind": "translate-paper-attachment-cleanup",
            "server_id": client.server_id, "library_type": client.library_type,
            "library_id": client.library_id, "status": "prepared", "parents": parents}


def _check_cleanup(client: Client, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if client.server_id != manifest["server_id"]:
        raise ZoteroError("Cleanup manifest belongs to a different Zotero server.")
    live_items = client.all("items")
    pending, seen = [], set()
    for unit in manifest["parents"]:
        parent = _parent(client, unit["parent_key"], unit["metadata"])
        children = fetch_local_children(unit["parent_key"], client=client)
        _assert_preserved(client, unit, parent, children)
        _assert_preserved(client, {"baseline_children": [{"item": row} for row in unit["baseline_annotations"]]},
                          parent, live_items)
        kept = {record["item"]["key"] for record in unit["baseline_children"]}
        for record in unit["remove"]:
            before = record["item"]
            data = item_data(before)
            role = _cleanup_role(data.get("title", ""))
            key = before["key"]
            original_excerpt = bool(role and role[0] and role[1] in {SOURCE_ATTACHMENT_TITLE, "原文节选 PDF"}
                                    and unit["metadata"].get("itemType") == "book"
                                    and any(item_data(r["item"]).get("contentType") == "application/pdf"
                                            and _cleanup_role(item_data(r["item"]).get("title", "")) is None
                                            for r in unit["baseline_children"]))
            if (key in seen or key in kept or not role or (role[1] not in LOCAL_ONLY_TITLES and not original_excerpt)
                    or data.get("itemType") != "attachment" or data.get("parentItem") != unit["parent_key"]):
                raise ZoteroError("Cleanup manifest may only remove the listed historical attachments.")
            seen.add(key)
            current = client.item(key, missing_ok=True)
            if current is None:
                if record["status"] not in {"deleting", "deleted"}:
                    raise ZoteroError("An unattempted cleanup attachment disappeared.")
                record["status"] = "deleted"
                continue
            if record["status"] == "deleted":
                raise ZoteroError("A deleted cleanup key reappeared; refusing to delete it again.")
            if (_metadata_without_reading_changes(current) != _metadata_without_reading_changes(before)
                    or _cleanup_identity(client, current) != record["file_identity"]):
                raise ZoteroError("A cleanup attachment changed after preview; no changed file was deleted.")
            if any(item_data(row).get("parentItem") == key for row in live_items):
                raise ZoteroError("A cleanup attachment gained notes or annotations.")
            pending.append(record)
    return pending


def cmd_cleanup(args: argparse.Namespace) -> int:
    path = Path(args.manifest).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("kind") != "translate-paper-attachment-cleanup":
        raise ZoteroError("Unsupported attachment cleanup manifest.")
    with Client(manifest["library_type"], manifest["library_id"]) as client:
        version = client.library_version()
        pending = _check_cleanup(client, manifest)
        total = sum(len(unit["remove"]) for unit in manifest["parents"])
        if not args.yes_cleanup:
            dump_json({"executed": False, "manifest": str(path), "attachments": total,
                       "pending": len(pending), "permanent": True})
            return 0
        try:
            if pending:
                client.require_write_key()
            for start in range(0, len(pending), 50):
                batch = pending[start:start + 50]
                for record in batch:
                    record["status"] = "deleting"
                manifest["status"] = "deleting"
                _write_upload_receipt(path, manifest)
                client.call("delete_item", [record["item"] for record in batch], last_modified=version)
                for record in batch:
                    if client.item(record["item"]["key"], missing_ok=True) is not None:
                        raise ZoteroError("Zotero did not confirm permanent attachment deletion.")
                    record["status"] = "deleted"
                _write_upload_receipt(path, manifest)
                version = client.library_version()
                _check_cleanup(client, manifest)
            if manifest["status"] != "complete":
                manifest["status"] = "complete"
                manifest.pop("error", None)
                _write_upload_receipt(path, manifest)
            dump_json({"executed": bool(pending), "status": "complete", "attachments_deleted": total,
                       "preserved": True, "manifest": str(path)})
            return 0
        except (ZoteroError, OSError, ValueError) as exc:
            manifest.update(status="interrupted", error=redact(str(exc), client.zot.local_api_key, client.zot.api_key))
            _write_upload_receipt(path, manifest)
            raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deliver validated paper bundles through Pyzotero.")
    parser.add_argument("--credential-target", default=DEFAULT_TARGET, help="Web credential target, used only for --include-web.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, function in (("status", cmd_status), ("targets", cmd_targets), ("search", cmd_search),
                           ("delete", cmd_delete), ("ingest", cmd_ingest), ("attach", cmd_delivery),
                           ("resume", cmd_resume), ("verify", cmd_verify), ("authorize", cmd_authorize),
                           ("cleanup", cmd_cleanup), ("attach-source", cmd_attach_source)):
        command = commands.add_parser(name)
        command.set_defaults(func=function)
        if name == "cleanup":
            command.add_argument("--manifest", required=True)
            command.add_argument("--yes-cleanup", action="store_true")
        if name in {"status", "search", "delete", "verify"}:
            command.add_argument("--library-type", choices=["user", "group"], default="user")
            command.add_argument("--library-id", default="0")
        if name in {"search", "delete", "ingest", "attach", "verify", "attach-source"}:
            command.add_argument("--metadata", required=True)
        if name in {"ingest", "attach", "verify", "attach-source"}:
            command.add_argument("--target-id", required=name != "verify")
            command.add_argument("--target-name")
            if name != "attach-source":
                command.add_argument("--title-prefix", default="")
        if name in {"ingest", "attach"}:
            for flag in ("translated-pdf", "latex-source-archive"):
                command.add_argument("--" + flag, required=True)
            command.add_argument("--source-pdf", help="Required and uploaded for papers; local identity check only for chapters.")
            for flag in ("translated-markdown", "summary"):
                command.add_argument("--" + flag, help="Legacy local-only argument; never uploaded or read.")
            command.add_argument("--receipt", help="Default: zotero-upload.json next to metadata; use resume if it exists.")
            command.add_argument("--yes-" + name, action="store_true")
        if name in {"attach", "resume", "verify", "attach-source"}:
            command.add_argument("--parent-key", required=name in {"attach", "attach-source"})
        if name == "attach-source":
            command.add_argument("--source-pdf", required=True)
            command.add_argument("--expected-sha256")
            command.add_argument("--receipt", required=True)
            command.add_argument("--yes-attach-source", action="store_true")
        if name == "resume":
            command.add_argument("--receipt", required=True)
            command.add_argument("--yes-resume", action="store_true")
        if name in {"resume", "verify"}:
            command.add_argument("--source-pdf")
        if name == "authorize":
            command.add_argument("--timeout-seconds", type=float, default=3600)
        if name == "search":
            command.add_argument("--include-web", action="store_true")
        if name == "verify":
            command.add_argument("--expected-dir")
        if name == "delete":
            command.add_argument("--item-key", required=True)
            command.add_argument("--yes-delete", action="store_true")
            command.add_argument("--wait-local-seconds", type=int, default=30, help="Deprecated; local writes are immediately visible.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if getattr(args, "timeout_seconds", 1) <= 0 or getattr(args, "wait_local_seconds", 0) < 0:
            raise ZoteroError("Timeout values must be positive (or zero for deprecated sync waiting).")
        return int(args.func(args))
    except (ZoteroError, OSError, ValueError, KeyError, TypeError) as exc:
        dump_json({"ok": False, "error": str(exc)}, stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
