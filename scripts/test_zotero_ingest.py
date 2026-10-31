#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyzotero==1.16.0"]
# ///
from __future__ import annotations

from contextlib import contextmanager
import copy
from email.parser import BytesParser
from email import policy
import hashlib
import http.server
import threading
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

import httpx2
from test_export_latex_project import make_archive
import zotero_client as sdk
import zotero_credentials as credentials
import zotero_ingest as ingest


@contextmanager
def stored_files(children, paths):
    yield {c["data"]["title"]: paths[c.get("key", c["data"]["title"])] for c in children}


class ZoteroIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = {
            "itemType": "journalArticle",
            "title": "The Knowledge Complexity of Interactive Proof Systems",
            "creators": [
                {
                    "creatorType": "author",
                    "firstName": "Shafi",
                    "lastName": "Goldwasser",
                }
            ],
            "date": "1989",
            "DOI": "https://doi.org/10.1137/0218012",
        }


    def test_normalizes_doi(self) -> None:
        self.assertEqual(
            ingest.normalize_doi(" DOI: 10.1137/0218012. "),
            "10.1137/0218012",
        )


    def test_doi_url_is_not_mistaken_for_arxiv_id(self) -> None:
        self.assertIsNone(
            ingest.extract_arxiv(
                {"url": "https://doi.org/10.1109/SP.2018.00020"}
            )
        )


    def test_extracts_arxiv_only_from_explicit_arxiv_fields(self) -> None:
        self.assertEqual(
            ingest.extract_arxiv({"arXivID": "arXiv: 1709.00020v2"}),
            "1709.00020",
        )
        self.assertEqual(
            ingest.extract_arxiv(
                {"extra": "DOI: 10.1000/2018.00020\narXiv: 1709.00020v1"}
            ),
            "1709.00020",
        )


    def test_doi_match_does_not_fall_back_to_title(self) -> None:
        candidate = {
            "data": {
                "itemType": "journalArticle",
                "title": self.metadata["title"],
                "date": "1989",
                "DOI": "10.0000/wrong",
            }
        }
        self.assertIsNone(ingest.match_reason(self.metadata, candidate))


    def test_title_and_year_fallback(self) -> None:
        metadata = dict(self.metadata)
        metadata.pop("DOI")
        candidate = {
            "data": {
                "itemType": "journalArticle",
                "title": "  THE Knowledge Complexity of Interactive Proof Systems ",
                "date": "Spring 1989",
            }
        }
        self.assertEqual(ingest.match_reason(metadata, candidate), "title+year")


    def test_source_parent_key_matches_incomplete_original_parent(self) -> None:
        metadata = {**self.metadata, "_sourceParentKey": "6ACE9QQE"}
        candidate = {
            "key": "6ACE9QQE",
            "data": {
                "itemType": "journalArticle",
                "title": self.metadata["title"],
                "date": "",
                "DOI": "",
            },
        }
        self.assertEqual(
            ingest.match_reason(metadata, candidate), "source-parent-key"
        )


    def test_source_parent_key_rejects_conflicting_identity(self) -> None:
        metadata = {**self.metadata, "_sourceParentKey": "6ACE9QQE"}
        wrong_title = {
            "key": "6ACE9QQE",
            "data": {
                "itemType": "journalArticle",
                "title": "A Different Paper",
            },
        }
        wrong_doi = {
            "key": "6ACE9QQE",
            "data": {
                "itemType": "journalArticle",
                "title": self.metadata["title"],
                "DOI": "10.0000/wrong",
            },
        }
        self.assertIsNone(ingest.match_reason(metadata, wrong_title))
        self.assertIsNone(ingest.match_reason(metadata, wrong_doi))


    def test_metadata_requires_stable_identity(self) -> None:
        metadata = dict(self.metadata)
        metadata.pop("DOI")
        metadata.pop("date")
        with self.assertRaises(ingest.ZoteroError):
            ingest.validate_metadata(metadata)


    def test_pdf_signature_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / "good.pdf"
            bad = Path(directory) / "bad.pdf"
            good.write_bytes(b"%PDF-1.7\n")
            bad.write_bytes(b"plain text")
            ingest.validate_pdf(good, "good")
            with self.assertRaises(ingest.ZoteroError):
                ingest.validate_pdf(bad, "bad")


    def test_generated_child_verification(self) -> None:
        children = [
            {
                "key": "TRANS001",
                "data": {
                    "itemType": "attachment",
                    "title": ingest.TRANSLATION_ATTACHMENT_TITLE,
                    "linkMode": "imported_url",
                    "contentType": "application/pdf",
                },
            },
        ]
        children.append({"key": "LATEX001", "data": {
            "itemType": "attachment", "title": ingest.LATEX_SOURCE_TITLE,
            "linkMode": "imported_file", "contentType": "application/zip", "filename": "source.zip"}})
        self.assertTrue(ingest.verify_generated_children(children)["ok"])
        extra = children + [
            {
                "key": "EXTRA001",
                "data": {"itemType": "note", "note": "<p>unexpected</p>"},
            }
        ]
        self.assertFalse(ingest.verify_generated_children(extra)["ok"])
        legacy = children + [{"data": {"itemType": "attachment", "title": title}}
                             for title in ingest.LOCAL_ONLY_TITLES]
        self.assertTrue(ingest.verify_generated_children(legacy)["ok"])
        for field, value in (("linkMode", "linked_file"), ("contentType", "text/html")):
            invalid = [{"data": {**children[0]["data"], field: value}}, children[1]]
            self.assertFalse(ingest.verify_generated_children(invalid)["ok"])
        self.assertFalse(ingest.verify_generated_children(children[:-1])["ok"])
        for field, value in (("filename", "source.tex"), ("contentType", "text/plain"), ("linkMode", "linked_file")):
            invalid = children[:-1] + [{"data": {**children[-1]["data"], field: value}}]
            self.assertFalse(ingest.verify_generated_children(invalid)["ok"])


    def test_live_verification_rejects_archive_for_different_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, translated, archive = root / "source.pdf", root / "translated.pdf", root / "source.zip"
            source.write_bytes(b"source")
            translated.write_bytes(b"accepted")
            make_archive(archive, source, translated)
            specs = [(ingest.TRANSLATION_ATTACHMENT_TITLE, "application/pdf", "translated.pdf"),
                     (ingest.LATEX_SOURCE_TITLE, "application/zip", "source.zip")]
            children = [{"data": {"itemType": "attachment", "title": title, "contentType": mime,
                                  "filename": filename, "linkMode": "imported_file"}}
                        for title, mime, filename in specs]
            def child_path(child):
                return root / child["data"]["filename"]
            with mock.patch.object(ingest, "verification_files", side_effect=lambda children, **kw: stored_files(children, {c.get("key", c["data"]["title"]): child_path(c) for c in children})):
                self.assertTrue(ingest.verify_generated_children(children, verify_archive_identity=True)["ok"])
                translated.write_bytes(b"changed")
                result = ingest.verify_generated_children(children, verify_archive_identity=True)
            self.assertFalse(result["ok"])
            self.assertIn("identity mismatch", result["errors"][0])

    def test_paper_verification_requires_original_matching_archive_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, children, stored, _ = self._stored_bundle(root)
            original = {"key": "SOURCE01", "data": {"itemType": "attachment", "parentItem": "PARENT01",
                "title": ingest.SOURCE_ATTACHMENT_TITLE, "contentType": "application/pdf",
                "filename": "source.pdf", "linkMode": "imported_file"}}
            stored["SOURCE01"] = root / "source.pdf"
            with mock.patch.object(ingest, "verification_files", side_effect=lambda rows, **kw: stored_files(rows, stored)):
                self.assertFalse(ingest.verify_generated_children(children, include_source=True)["ok"])
                children.append(original)
                self.assertTrue(ingest.verify_generated_children(children, include_source=True,
                                                                verify_archive_identity=True)["ok"])
                (root / "source.pdf").write_bytes(b"%PDF-wrong-original")
                self.assertFalse(ingest.verify_generated_children(children, include_source=True,
                                                                 verify_archive_identity=True)["ok"])


    def _stored_bundle(self, root):
        paths = {title: root / name for title, name in ingest.FINAL_FILENAMES.items()}
        source = root / "source.pdf"
        source.write_bytes(b"%PDF-source")
        paths[ingest.TRANSLATION_ATTACHMENT_TITLE].write_bytes(b"%PDF-accepted")
        make_archive(paths[ingest.LATEX_SOURCE_TITLE], source,
                     paths[ingest.TRANSLATION_ATTACHMENT_TITLE])
        children, stored = [], {}
        for index, (title, path) in enumerate(paths.items()):
            key = f"FILE{index:04d}"
            mime = {".pdf": "application/pdf", ".md": "text/markdown", ".zip": "application/zip"}[path.suffix]
            children.append({"key": key, "data": {"itemType": "attachment", "parentItem": "PARENT01",
                "title": title, "linkMode": "imported_url", "contentType": mime,
                "filename": f"renamed{index}{path.suffix}"}})
            stored[key] = path
        expected = {title: ingest.file_identity(path) for title, path in paths.items()}
        return paths, children, stored, expected


    def test_two_files_are_byte_verified_despite_connector_renaming(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, children, stored, expected = self._stored_bundle(Path(directory))
            with mock.patch.object(ingest, "verification_files", side_effect=lambda children, **kw: stored_files(children, stored)):
                result = ingest.verify_generated_children(children, verify_archive_identity=True,
                    expected_hashes=expected, expected_parent_key="PARENT01")
            self.assertTrue(result["ok"], result["errors"])
            self.assertEqual(len(result["verified_files"]), 2)
            self.assertEqual({r["title"]: {k: r[k] for k in ("sha256", "md5", "size")}
                              for r in result["verified_files"]}, expected)


    def test_stored_delivery_file_corruption_is_rejected(self) -> None:
        for title in (ingest.TRANSLATION_ATTACHMENT_TITLE, ingest.LATEX_SOURCE_TITLE):
            with self.subTest(title=title), tempfile.TemporaryDirectory() as directory:
                paths, children, stored, expected = self._stored_bundle(Path(directory))
                changed = bytearray(paths[title].read_bytes())
                changed[-1] ^= 1
                paths[title].write_bytes(changed)
                with mock.patch.object(ingest, "verification_files", side_effect=lambda children, **kw: stored_files(children, stored)):
                    result = ingest.verify_generated_children(children, verify_archive_identity=True,
                        expected_hashes=expected, expected_parent_key="PARENT01")
                self.assertFalse(result["ok"])
                self.assertIn(f"stored file bytes differ from accepted input: {title}", result["errors"])


    def test_missing_stored_file_is_not_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, children, stored, expected = self._stored_bundle(Path(directory))
            paths[ingest.LATEX_SOURCE_TITLE].unlink()
            with mock.patch.object(ingest, "verification_files", side_effect=lambda children, **kw: stored_files(children, stored)):
                result = ingest.verify_generated_children(children, expected_hashes=expected)
            self.assertFalse(result["ok"])
            self.assertEqual(len(result["verified_files"]), 1)


    def test_wrong_parent_is_rejected_before_reading_file_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, children, _, expected = self._stored_bundle(Path(directory))
            children[-1]["data"]["parentItem"] = "OTHER001"
            with mock.patch.object(ingest, "verification_files") as locate:
                result = ingest.verify_generated_children(children, expected_hashes=expected,
                    expected_parent_key="PARENT01")
            self.assertFalse(result["ok"])
            locate.assert_not_called()


class LocalLibrary:
    """HTTP fixture exercising the real SDK, without Zotero or credential writes."""
    def __init__(self):
        self.items = {}
        self.files = {}
        self.uploads = {}
        self.version = 1
        self.server_id = "TESTSERVER"
        self.requests = []
        self.creates = []
        self.deleted = []
        self.fail_upload = False
        self.lose_create = False
        self.lose_registration = False
        self.authorize_status = 200
        self.remember = True
        self.rate_limit = False
        self.repeat_page = False
        self.collections = [{"key": "COLLECT1", "data": {"name": "Papers", "parentCollection": False},
                             "library": {"type": "user", "id": 1, "name": "My Library"}}]
        self.groups = []

    def response(self, request, value=None, status=200, *, content=None, headers=None):
        fields = {"Content-Type": "application/json", "Zotero-Server-ID": self.server_id,
                  "Last-Modified-Version": str(self.version), **(headers or {})}
        return httpx2.Response(status, json=value if content is None else None,
                               content=content, headers=fields, request=request)

    def row(self, data, kind="user", lib_id="0"):
        return {"key": data["key"], "version": self.version, "data": {**data, "version": self.version},
                "library": {"type": kind, "id": 1 if kind == "user" else int(lib_id)},
                "meta": {}, "links": {}}

    def handle(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.headers.get("Zotero-Server-ID", self.server_id) != self.server_id:
            return self.response(request, status=412, content=b"Zotero-Server-ID does not match this server")
        if path == "/api/itemTypes":
            return self.response(request, [{"itemType": "journalArticle", "localized": "Article"}])
        if path == "/api/local/authorize":
            return self.response(request, {"key": "test-local-secret", "remember": self.remember}, self.authorize_status)
        if path == "/api/users/0/groups":
            return self.response(request, self.groups)
        if request.method not in {"GET", "HEAD"} and not path.startswith("/api/local/uploads"):
            if request.headers.get("Zotero-API-Key") != "test-local-secret":
                return self.response(request, status=401, content=b"Invalid or expired API key")
        if request.method == "POST" and path.startswith("/api/local/uploads"):
            if self.fail_upload:
                self.fail_upload = False
                raise httpx2.ReadTimeout("lost upload response", request=request)
            message = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.read())
            body = next(part.get_payload(decode=True) for part in message.iter_parts()
                        if part.get_param("name", header="content-disposition") == "file")
            self.uploads[path]["body"] = body
            return self.response(request, status=201)
        segments = path.strip("/").split("/")
        kind, lib_id = ("group" if segments[1] == "groups" else "user"), segments[2]
        resource = segments[3]
        if resource == "collections":
            return self.response(request, self.collections)
        if resource != "items":
            return self.response(request, status=404, content=b"not found")
        key = segments[4] if len(segments) > 4 else None
        if len(segments) > 5 and segments[5] == "file":
            if request.method == "GET":
                if key not in self.files:
                    return self.response(request, status=404, content=b"missing file")
                return self.response(request, content=self.files[key],
                    headers={"Content-Type": self.items[key]["data"]["contentType"]})
            fields = {k: values[0] for k, values in urllib.parse.parse_qs(request.read().decode(), keep_blank_values=True).items()}
            if "upload" in fields:
                upload = self.uploads["/api/local/uploads/" + fields["upload"]]
                body = upload["body"]
                if hashlib.md5(body).hexdigest() != upload["md5"]:
                    return self.response(request, status=400, content=b"wrong md5")
                self.files[key] = body
                self.items[key]["data"].update(md5=upload["md5"], filename=upload["filename"])
                self.version += 1
                self.items[key]["version"] = self.items[key]["data"]["version"] = self.version
                if self.lose_registration:
                    self.lose_registration = False
                    raise httpx2.ReadTimeout("lost registration response", request=request)
                return self.response(request, status=204)
            if key in self.files:
                return self.response(request, {"exists": 1})
            if request.headers.get("If-None-Match") != "*":
                return self.response(request, status=412, content=b"missing file precondition")
            upload_key = "UP" + key
            self.uploads["/api/local/uploads/" + upload_key] = fields
            return self.response(request, {"url": sdk.LOCAL_BASE + "/api/local/uploads/" + upload_key,
                                          "uploadKey": upload_key, "params": {"key": fields["md5"]}})
        if request.method == "POST":
            success, successful, failed = {}, {}, {}
            for index, data in enumerate(json.loads(request.read())):
                self.creates.append(copy.deepcopy(data))
                if data["key"] in self.items:
                    failed[str(index)] = {"code": 412, "message": "key already exists"}
                    continue
                self.version += 1
                row = self.row(data, kind, lib_id)
                self.items[data["key"]] = row
                success[str(index)], successful[str(index)] = data["key"], row
            if self.lose_create:
                self.lose_create = False
                raise httpx2.ReadTimeout("lost creation response", request=request)
            return self.response(request, {"success": success, "successful": successful, "failed": failed})
        if request.method == "DELETE":
            if request.headers.get("If-Unmodified-Since-Version") != str(self.version):
                return self.response(request, status=412, content=b"library changed")
            keys = request.url.params["itemKey"].split(",")
            self.deleted.extend(keys)
            for removed in keys:
                self.items.pop(removed, None)
                self.files.pop(removed, None)
            self.version += 1
            return self.response(request, status=204)
        if key not in {None, "top"} and len(segments) == 5:
            if key not in self.items:
                return self.response(request, status=404, content=b"not found")
            return self.response(request, self.items[key])
        rows = [row for row in self.items.values() if row["library"]["type"] == kind
                and (kind == "user" or row["library"]["id"] == int(lib_id))]
        if request.url.params.get("format") == "versions":
            return self.response(request, {row["key"]: row["version"] for row in rows})
        if len(segments) > 5 and segments[5] == "children":
            rows = [row for row in rows if row["data"].get("parentItem") == key]
        elif key == "top":
            rows = [row for row in rows if not row["data"].get("parentItem")]
        start, limit = int(request.url.params.get("start", 0)), int(request.url.params.get("limit", 100))
        headers = {"Total-Results": str(len(rows))}
        if start + limit < len(rows):
            next_url = str(request.url.copy_set_param("start", start + limit))
            headers["Link"] = f'<{next_url}>; rel="next"'
        if self.repeat_page and start:
            start = 0
        return self.response(request, rows[start:start+limit], headers=headers)

    def post(self, url, **kwargs):
        kwargs.pop("timeout", None)
        kwargs.pop("trust_env", None)
        request = httpx2.Request("POST", url, **kwargs)
        return self.handle(request)


class SDKDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = LocalLibrary()
        self.real_client = sdk.Client
        transport = httpx2.MockTransport(lambda request: self.library.handle(request))
        def factory(*args, **kwargs):
            return self.real_client(*args, transport=transport, **kwargs)
        for target in ("zotero_ingest.Client", "zotero_client.Client"):
            patcher = mock.patch(target, side_effect=factory)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch("zotero_credentials.read_local_credential", return_value="test-local-secret")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("httpx2.post", side_effect=self.library.post)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.desktop = {"libraryID": 1, "id": 11, "targets": [
            {"id": "L1", "name": "My Library", "filesEditable": True, "level": 0},
            {"id": "C11", "name": "Papers", "filesEditable": True, "level": 1}]}
        patcher = mock.patch("zotero_client.selected_target", side_effect=lambda: copy.deepcopy(self.desktop))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.metadata = {"itemType": "journalArticle", "title": "A Paper", "date": "2020",
                         "DOI": "10.1000/example", "creators": [{"creatorType": "author", "lastName": "Author"}]}
        self.metadata_path = self.root / "metadata.json"
        self.metadata_path.write_text(json.dumps(self.metadata), encoding="utf-8")
        self.paths = {title: self.root / name for title, name in ingest.FINAL_FILENAMES.items()}
        self.paths.update({ingest.SOURCE_ATTACHMENT_TITLE: self.root / "source.pdf",
                           ingest.TRANSLATION_MARKDOWN_TITLE: self.root / "translation.md",
                           ingest.SUMMARY_ATTACHMENT_TITLE: self.root / "summary.md"})
        self.paths[ingest.SOURCE_ATTACHMENT_TITLE].write_bytes(b"%PDF-source")
        self.paths[ingest.TRANSLATION_ATTACHMENT_TITLE].write_bytes(b"%PDF-translated")
        self.paths[ingest.TRANSLATION_MARKDOWN_TITLE].write_text("中文正文", encoding="utf-8")
        self.paths[ingest.SUMMARY_ATTACHMENT_TITLE].write_text("中文总结", encoding="utf-8")
        make_archive(self.paths[ingest.LATEX_SOURCE_TITLE], self.paths[ingest.SOURCE_ATTACHMENT_TITLE],
                     self.paths[ingest.TRANSLATION_ATTACHMENT_TITLE])
        self.receipt = self.root / "upload.json"

    def args(self, command="ingest", *, execute=True, prefix="", parent=None):
        values = [command, "--metadata", str(self.metadata_path), "--target-id", "C11",
                  "--title-prefix", prefix, "--receipt", str(self.receipt),
                  "--source-pdf", str(self.paths[ingest.SOURCE_ATTACHMENT_TITLE])]
        if execute:
            values.append("--yes-" + command)
        if parent:
            values.extend(["--parent-key", parent])
        for flag, role in (("translated-pdf", ingest.TRANSLATION_ATTACHMENT_TITLE),
                           ("latex-source-archive", ingest.LATEX_SOURCE_TITLE)):
            values.extend(["--" + flag, str(self.paths[role])])
        return ingest.build_parser().parse_args(values)

    def deliver(self, **kwargs):
        args = self.args(**kwargs)
        with mock.patch.object(ingest, "dump_json") as output:
            args.func(args)
        return output.call_args.args[0]

    def resume(self, **kwargs):
        args = ingest.build_parser().parse_args(["resume", "--receipt", str(self.receipt), "--yes-resume"])
        for key, value in kwargs.items():
            setattr(args, key, value)
        with mock.patch.object(ingest, "dump_json") as output:
            ingest.cmd_resume(args)
        return output.call_args.args[0]

    def parent(self, key="PARENT01"):
        self.library.items[key] = self.library.row({**self.metadata, "key": key, "collections": ["COLLECT1"]})
        return key

    def cleanup_manifest(self):
        parent = self.parent()
        for key, title, mime, content in (
            ("PDF00001", ingest.TRANSLATION_ATTACHMENT_TITLE, "application/pdf", b"%PDF-translated"),
            ("ZIP00001", ingest.LATEX_SOURCE_TITLE, "application/zip", self.paths[ingest.LATEX_SOURCE_TITLE].read_bytes()),
            ("BOOK0001", "PDF", "application/pdf", b"%PDF-full-book"),
            ("OLD00001", "原文 PDF", "application/pdf", b"%PDF-source"),
            ("OLD00002", "中文翻译 Markdown", "text/markdown", b"old translation"),
            ("OLD00003", "中文论文总结", "text/markdown", b"old summary"),
        ):
            data = {"key": key, "parentItem": parent, "itemType": "attachment", "title": title,
                    "contentType": mime, "linkMode": "imported_file", "filename": key}
            self.library.items[key] = self.library.row(data)
            self.library.files[key] = content
        self.library.items["NOTE0001"] = self.library.row(
            {"key": "NOTE0001", "parentItem": parent, "itemType": "note", "note": "Keep this note"})
        self.library.items["ANNOT001"] = self.library.row(
            {"key": "ANNOT001", "parentItem": "PDF00001", "itemType": "annotation", "annotationText": "Keep"})
        with ingest.Client() as client:
            manifest = ingest.prepare_cleanup_manifest(client, [parent])
        self.cleanup_path = self.root / "cleanup.json"
        ingest._write_upload_receipt(self.cleanup_path, manifest)
        return manifest

    def cleanup(self, execute=True):
        args = ingest.build_parser().parse_args(["cleanup", "--manifest", str(self.cleanup_path)]
                                                + (["--yes-cleanup"] if execute else []))
        with mock.patch.object(ingest, "dump_json") as output:
            ingest.cmd_cleanup(args)
        return output.call_args.args[0]

    def attach_source(self, execute=True):
        args = ingest.build_parser().parse_args(["attach-source", "--metadata", str(self.metadata_path),
            "--parent-key", "PARENT01", "--target-id", "C11", "--source-pdf",
            str(self.paths[ingest.SOURCE_ATTACHMENT_TITLE]), "--expected-sha256",
            ingest.file_identity(self.paths[ingest.SOURCE_ATTACHMENT_TITLE])["sha256"],
            "--receipt", str(self.root / "source-upload.json")]
            + (["--yes-attach-source"] if execute else []))
        with mock.patch.object(ingest, "dump_json") as output:
            args.func(args)
        return output.call_args.args[0]

    def test_original_restoration_preserves_existing_files_and_is_idempotent(self):
        self.cleanup_manifest()
        self.library.items.pop("OLD00001")
        self.library.files.pop("OLD00001")
        baseline = copy.deepcopy(self.library.items)
        baseline_files = copy.deepcopy(self.library.files)
        self.attach_source(execute=False)
        self.assertFalse((self.root / "source-upload.json").exists())
        result = self.attach_source()
        self.assertEqual(self.library.files[result["attachment_key"]], b"%PDF-source")
        self.assertEqual({k:self.library.items[k] for k in baseline}, baseline)
        self.assertEqual({k:self.library.files[k] for k in baseline_files}, baseline_files)
        before = len(self.library.creates)
        self.attach_source()
        self.assertEqual(len(self.library.creates), before)
        self.assertEqual(self.library.deleted, [])

    def test_original_restoration_resumes_uncertain_metadata_and_upload(self):
        for fail in ("lose_create", "fail_upload", "lose_registration"):
            with self.subTest(fail=fail):
                self.library.items.clear()
                self.library.files.clear()
                self.library.creates.clear()
                (self.root / "source-upload.json").unlink(missing_ok=True)
                self.parent()
                setattr(self.library, fail, True)
                with self.assertRaises(sdk.ZoteroError):
                    self.attach_source()
                self.attach_source()
                self.assertEqual(len(self.library.creates), 1)
                self.assertEqual(len(self.library.files), 1)

    def test_paper_requires_source_and_book_does_not_upload_excerpt(self):
        args = self.args()
        args.source_pdf = None
        with self.assertRaisesRegex(sdk.ZoteroError, "requires --source-pdf"):
            args.func(args)
        args.title_prefix = "paper-v2"
        with self.assertRaisesRegex(sdk.ZoteroError, "requires --source-pdf"):
            args.func(args)
        self.metadata["itemType"] = "book"
        self.metadata_path.write_text(json.dumps(self.metadata), encoding="utf-8")
        parent = self.parent()
        self.deliver(command="attach", parent=parent)
        self.assertEqual({x["title"] for x in self.library.creates}, set(ingest.FINAL_FILENAMES))
        with self.assertRaisesRegex(sdk.ZoteroError, "paper originals only"):
            self.attach_source()

    def test_old_two_file_paper_receipt_can_resume_with_recorded_original(self):
        self.library.lose_create = True
        with self.assertRaises(sdk.ZoteroError):
            self.deliver()
        saved = json.loads(self.receipt.read_text(encoding="utf-8"))
        saved["files"] = [r for r in saved["files"] if r["role"] != ingest.SOURCE_ATTACHMENT_TITLE]
        saved["expected_hashes"].pop(ingest.SOURCE_ATTACHMENT_TITLE)
        self.receipt.write_text(json.dumps(saved), encoding="utf-8")
        self.resume(source_pdf=str(self.paths[ingest.SOURCE_ATTACHMENT_TITLE]))
        self.assertEqual(len(self.library.files), 3)

    def test_cleanup_preview_delete_and_repeat_preserve_unrelated_items(self):
        self.cleanup_manifest()
        before = copy.deepcopy(self.library.items)
        files = copy.deepcopy(self.library.files)
        self.assertFalse(self.cleanup(execute=False)["executed"])
        self.assertEqual(self.library.items, before)
        result = self.cleanup()
        self.assertEqual(result["attachments_deleted"], 2)
        self.assertEqual(set(self.library.deleted), {"OLD00002", "OLD00003"})
        for key in set(before) - set(self.library.deleted):
            self.assertEqual(self.library.items[key], before[key])
            if key in files:
                self.assertEqual(self.library.files[key], files[key])
        count = len(self.library.deleted)
        self.assertFalse(self.cleanup()["executed"])
        self.assertEqual(len(self.library.deleted), count)
        self.assertEqual(list(self.root.glob("cleanup*")), [self.cleanup_path])

    def test_cleanup_rejects_changed_file_or_new_annotation_before_deletion(self):
        self.cleanup_manifest()
        for key in ("OLD00001", "PDF00001"):
            original = self.library.files[key]
            self.library.files[key] = b"changed file"
            with self.assertRaises(ingest.ZoteroError):
                self.cleanup()
            self.library.files[key] = original
        self.library.items["ANNOT002"] = self.library.row(
            {"key": "ANNOT002", "parentItem": "OLD00002", "itemType": "annotation"})
        with self.assertRaises(ingest.ZoteroError):
            self.cleanup()
        self.assertEqual(self.library.deleted, [])

    def test_cleanup_manifest_cannot_remove_parent_or_full_book(self):
        manifest = self.cleanup_manifest()
        for key in ("PARENT01", "BOOK0001"):
            with self.subTest(key=key):
                changed = copy.deepcopy(manifest)
                changed["parents"][0]["remove"][0]["item"] = self.library.items[key]
                ingest._write_upload_receipt(self.cleanup_path, changed)
                with self.assertRaises(ingest.ZoteroError):
                    self.cleanup()
                self.assertEqual(self.library.deleted, [])

    def test_cleanup_resumes_lost_delete_reply_without_repeating_deletion(self):
        self.cleanup_manifest()
        original = self.library.handle
        lost = False
        def handler(request):
            nonlocal lost
            response = original(request)
            if request.method == "DELETE" and not lost:
                lost = True
                raise httpx2.ReadTimeout("lost deletion reply", request=request)
            return response
        self.library.handle = handler
        with self.assertRaises(ingest.ZoteroError):
            self.cleanup()
        self.assertEqual(len(self.library.deleted), 2)
        self.assertEqual(self.cleanup()["status"], "complete")
        self.assertEqual(len(self.library.deleted), 2)

    def test_cleanup_library_version_precondition_prevents_concurrent_deletion(self):
        self.cleanup_manifest()
        original = self.library.handle
        def handler(request):
            if request.method == "DELETE":
                self.library.version += 1
            return original(request)
        self.library.handle = handler
        with self.assertRaises(ingest.ZoteroError):
            self.cleanup()
        self.assertEqual(self.library.deleted, [])

    def test_cleanup_pages_large_books_and_deletes_at_most_fifty_per_request(self):
        parent = self.parent()
        self.library.items[parent]["data"]["itemType"] = "book"
        self.library.items["FULLBOOK"] = self.library.row({"key": "FULLBOOK", "itemType": "attachment",
            "parentItem": parent, "title": "PDF", "filename": "book.pdf", "linkMode": "imported_file",
            "contentType": "application/pdf"})
        self.library.files["FULLBOOK"] = b"%PDF-full-book"
        for chapter in range(1, 37):
            for number, role in enumerate(("原文节选 PDF", "中文翻译 Markdown", "中文论文总结", "中文翻译 PDF")):
                key = f"C{chapter:03d}{number:04d}"
                self.library.items[key] = self.library.row({"key": key, "itemType": "attachment",
                    "parentItem": parent, "title": f"第{chapter}章 {role}", "filename": key,
                    "contentType": "application/pdf" if role.endswith("PDF") else "text/markdown",
                    "linkMode": "imported_file"})
                self.library.files[key] = key.encode()
        with ingest.Client() as client:
            manifest = ingest.prepare_cleanup_manifest(client, [parent])
        self.cleanup_path = self.root / "cleanup.json"
        ingest._write_upload_receipt(self.cleanup_path, manifest)
        self.assertEqual(self.cleanup()["attachments_deleted"], 108)
        sizes = [len(request.url.params["itemKey"].split(",")) for request in self.library.requests
                 if request.method == "DELETE"]
        self.assertEqual(sizes, [50, 50, 8])
        self.assertEqual(len(self.library.items), 38)

    def test_paper_ingest_uploads_original_translation_and_latex(self):
        result = self.deliver()
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertEqual(receipt["schema_version"], 2)
        self.assertEqual(receipt["status"], "files-verified")
        self.assertTrue(result["verification"]["target_ok"])
        self.assertEqual(len(result["verification"]["verified_files"]), 3)
        self.assertEqual(len(self.library.creates), 4)
        self.assertEqual(len(self.library.files), 3)
        self.assertEqual({r["role"] for r in receipt["files"]},
                         {"原文 PDF", "中文翻译 PDF", "LaTeX 源码包"})
        self.assertNotIn("test-local-secret", self.receipt.read_text(encoding="utf-8"))
        self.assertTrue(all(d.get("linkMode") == "imported_file" for d in self.library.creates[1:]))
        self.assertTrue(all("/connector/" not in str(r.url) for r in self.library.requests))
        for record in receipt["files"]:
            self.assertEqual(self.library.files[record["key"]], Path(record["path"]).read_bytes())
            self.assertEqual(self.library.items[record["key"]]["data"]["filename"], Path(record["path"]).name)

    def test_preview_does_not_authorize_write_or_create_receipt(self):
        self.deliver(execute=False)
        self.assertFalse(self.receipt.exists())
        self.assertFalse(any(r.method != "GET" for r in self.library.requests))

    def test_delivery_does_not_require_local_only_files(self):
        for role in ingest.LOCAL_ONLY_TITLES:
            self.paths[role].unlink()
        result = self.deliver()
        self.assertTrue(result["verification"]["ok"])
        self.assertEqual(len(self.library.files), 3)

    def test_legacy_cli_paths_are_never_uploaded(self):
        args = self.args()
        args.source_pdf = str(self.paths[ingest.SOURCE_ATTACHMENT_TITLE])
        args.translated_markdown = str(self.root / "missing-translation.md")
        args.summary = str(self.root / "missing-summary.md")
        with mock.patch.object(ingest, "dump_json"):
            args.func(args)
        self.assertEqual({d["title"] for d in self.library.creates[1:]},
                         {"原文 PDF", "中文翻译 PDF", "LaTeX 源码包"})

    def test_optional_source_path_still_checks_local_archive_identity(self):
        args = self.args()
        args.source_pdf = str(self.paths[ingest.SOURCE_ATTACHMENT_TITLE])
        self.paths[ingest.SOURCE_ATTACHMENT_TITLE].write_bytes(b"%PDF-wrong-source")
        with self.assertRaisesRegex(ValueError, "identity mismatch: source_sha256"):
            args.func(args)
        self.assertFalse(self.library.creates)

    def test_existing_parent_guard_prevents_creation(self):
        self.parent()
        with self.assertRaisesRegex(sdk.ZoteroError, "exact parent already exists"):
            self.deliver()
        self.assertEqual(self.library.creates, [])

    def test_receipt_is_present_before_first_write_and_existing_receipt_is_refused(self):
        original = self.library.handle
        def handler(request):
            if request.method == "POST":
                saved = json.loads(self.receipt.read_text(encoding="utf-8"))
                self.assertEqual(len(saved["files"]), 3)
                self.assertTrue(all(r["key"] for r in saved["files"]))
            return original(request)
        self.library.handle = handler
        self.deliver()
        before = self.receipt.read_bytes()
        with self.assertRaisesRegex(sdk.ZoteroError, "receipt already exists"):
            self.deliver()
        self.assertEqual(self.receipt.read_bytes(), before)

    def test_uncertain_parent_creation_resumes_same_keys(self):
        self.library.lose_create = True
        with self.assertRaises(sdk.ZoteroError):
            self.deliver()
        saved = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertIn(saved["parent_key"], self.library.items)
        self.resume()
        self.assertEqual(len(self.library.creates), 4)
        self.assertEqual(len(self.library.items), 4)

    def test_partial_upload_and_lost_registration_resume_without_duplicates(self):
        for fail in ("fail_upload", "lose_registration"):
            with self.subTest(fail=fail):
                self.library.items.clear()
                self.library.files.clear()
                self.library.creates.clear()
                self.receipt.unlink(missing_ok=True)
                setattr(self.library, fail, True)
                with self.assertRaises(sdk.ZoteroError):
                    self.deliver()
                self.resume()
                self.assertEqual(len(self.library.creates), 4)
                self.assertEqual(len(self.library.files), 3)
                before = len(self.library.requests)
                self.resume()
                self.assertTrue(all(r.method == "GET" for r in self.library.requests[before:]))

    def test_five_file_paper_receipt_keeps_original_and_drops_markdown_summary(self):
        self.library.lose_create = True
        with self.assertRaises(sdk.ZoteroError):
            self.deliver()
        saved = json.loads(self.receipt.read_text(encoding="utf-8"))
        for role in ingest.LOCAL_ONLY_TITLES:
            path = self.paths[role]
            saved["files"].append({"role": role, "title": role, "path": str(path),
                "mime": "application/pdf" if path.suffix == ".pdf" else "text/markdown",
                "key": ingest.new_key(), "status": "pending", "confirmed": False})
            saved["expected_hashes"][role] = ingest.file_identity(path)
            path.unlink()
        self.receipt.write_text(json.dumps(saved), encoding="utf-8")
        before = self.receipt.read_bytes()
        self.resume(yes_resume=False)
        self.assertEqual(self.receipt.read_bytes(), before)
        self.assertEqual(len(self.library.creates), 1)
        self.resume()
        current = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertEqual(len(current["files"]), 3)
        self.assertEqual({r["role"] for r in current["local_only_files"]}, ingest.LOCAL_ONLY_TITLES)
        self.assertEqual(set(current["local_only_hashes"]), ingest.LOCAL_ONLY_TITLES)
        self.assertEqual(len(self.library.files), 3)
        self.assertEqual({d["title"] for d in self.library.creates[1:]},
                         {"原文 PDF", "中文翻译 PDF", "LaTeX 源码包"})

    def test_attach_preserves_book_and_other_children_and_reuses_owned_files(self):
        self.metadata["itemType"] = "book"
        self.metadata_path.write_text(json.dumps(self.metadata), encoding="utf-8")
        parent = self.parent()
        self.library.items["NOTE0001"] = self.library.row({"key": "NOTE0001", "itemType": "note", "parentItem": parent, "note": "keep me"})
        self.library.items["BOOKPDF1"] = self.library.row({"key": "BOOKPDF1", "itemType": "attachment", "parentItem": parent,
            "title": "Full book", "contentType": "application/pdf", "filename": "book.pdf", "linkMode": "imported_file"})
        self.library.files["BOOKPDF1"] = b"%PDF-full-book"
        for index, title in enumerate(sorted(ingest.LOCAL_ONLY_TITLES)):
            key = f"OLD{index:05d}"
            self.library.items[key] = self.library.row({"key": key, "itemType": "attachment", "parentItem": parent,
                "title": f"第 1 章 / {title}", "contentType": "text/plain", "filename": "old.txt", "linkMode": "imported_file"})
            self.library.files[key] = f"keep {title}".encode()
        baseline = copy.deepcopy(self.library.items)
        baseline_files = dict(self.library.files)
        self.deliver(command="attach", prefix="第 1 章", parent=parent)
        self.assertEqual({k:self.library.items[k] for k in baseline}, baseline)
        self.assertEqual({k:self.library.files[k] for k in baseline_files}, baseline_files)
        self.assertEqual({d["title"] for d in self.library.creates},
                         {"第 1 章 / 中文翻译 PDF", "第 1 章 / LaTeX 源码包"})
        self.receipt.unlink()
        created = len(self.library.creates)
        self.deliver(command="attach", prefix="第 1 章", parent=parent)
        self.assertEqual(len(self.library.creates), created)
        self.assertTrue(all(d.get("parentItem") == parent for d in self.library.creates))

    def test_conflicting_bytes_and_duplicate_titles_never_overwrite(self):
        self.deliver()
        saved = json.loads(self.receipt.read_text(encoding="utf-8"))
        child = saved["files"][0]["key"]
        writes = len(self.library.creates)
        self.library.files[child] = b"tampered"
        with self.assertRaisesRegex(sdk.ZoteroError, "different bytes"):
            self.resume()
        self.assertEqual(len(self.library.creates), writes)
        self.library.files[child] = Path(saved["files"][0]["path"]).read_bytes()
        duplicate = copy.deepcopy(self.library.items[child])
        duplicate["key"] = duplicate["data"]["key"] = "DUPLICAT"
        self.library.items["DUPLICAT"] = duplicate
        with self.assertRaisesRegex(sdk.ZoteroError, "Duplicate titles"):
            self.resume()

    def test_changed_accepted_files_and_server_are_rejected(self):
        self.deliver()
        accepted = self.paths[ingest.TRANSLATION_ATTACHMENT_TITLE].read_bytes()
        self.paths[ingest.TRANSLATION_ATTACHMENT_TITLE].write_bytes(b"%PDF-different")
        with self.assertRaisesRegex(sdk.ZoteroError, "local files changed"):
            self.resume()
        self.paths[ingest.TRANSLATION_ATTACHMENT_TITLE].write_bytes(accepted)
        self.library.server_id = "DIFFERENT"
        with self.assertRaises(sdk.ZoteroError):
            self.resume()

    def test_pagination_and_repeated_page_protection_use_sdk(self):
        for index in range(206):
            key = f"KEY{index:05d}"
            self.library.items[key] = self.library.row({"key": key, "itemType": "note", "parentItem": "PARENT01"})
        with self.real_client(transport=httpx2.MockTransport(self.library.handle)) as client:
            self.assertEqual(len(ingest.fetch_local_children("PARENT01", client=client)), 206)
        self.library.repeat_page = True
        with self.real_client(transport=httpx2.MockTransport(self.library.handle)) as client:
            with self.assertRaisesRegex(sdk.ZoteroError, "duplicate|repeated"):
                client.all("children", "PARENT01")

    def test_target_conversion_nested_paths_and_ambiguity(self):
        self.assertEqual(sdk.resolve_target("C11")["id"], "user:0/collections/COLLECT1")
        self.library.collections.append({"key": "NESTED01", "data": {"name": "Papers", "parentCollection": "COLLECT1"}})
        self.desktop["targets"].append({"id": "C12", "name": "Papers", "level": 2, "filesEditable": True})
        self.assertEqual(sdk.resolve_target("C12")["collection_key"], "NESTED01")
        self.library.collections.append({"key": "DUPLICAT", "data": {"name": "Papers", "parentCollection": False}})
        with self.assertRaisesRegex(sdk.ZoteroError, "ambiguous"):
            sdk.resolve_target("C11")
        with self.assertRaisesRegex(sdk.ZoteroError, "mismatch"):
            sdk.resolve_target("user:0", "wrong name")

    def test_group_delivery_uses_group_endpoint_and_membership(self):
        self.library.groups = [{"id": 42, "data": {"name": "Team"}}]
        self.desktop["targets"].extend([{"id": "L2", "name": "Team", "level": 0, "filesEditable": True},
                                       {"id": "C21", "name": "Papers", "level": 1, "filesEditable": True}])
        args = self.args()
        args.target_id = "group:42/collections/COLLECT1"
        with mock.patch.object(ingest, "dump_json"):
            ingest.cmd_ingest(args)
        self.assertTrue(all(row["library"] == {"type": "group", "id": 42} for row in self.library.items.values()))
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        self.assertEqual(receipt["target"]["library_id"], "42")

    def test_delete_is_local_guarded_and_uses_version_precondition(self):
        self.deliver()
        saved = json.loads(self.receipt.read_text(encoding="utf-8"))
        args = ingest.build_parser().parse_args(["delete", "--metadata", str(self.metadata_path),
            "--item-key", saved["parent_key"], "--yes-delete"])
        with mock.patch.object(ingest, "dump_json"):
            ingest.cmd_delete(args)
        request = next(r for r in self.library.requests if r.method == "DELETE")
        self.assertIn("If-Unmodified-Since-Version", request.headers)
        self.assertEqual(len(self.library.deleted), 4)
        self.assertEqual(self.library.items, {})

    def test_legacy_receipt_conversion_keeps_original(self):
        result = self.deliver()
        saved = json.loads(self.receipt.read_text(encoding="utf-8"))
        legacy = {"schema_version": 1, "route": "connector-new-parent", "identity": ingest.identity(self.metadata),
                  "target": {"id": "C11", "name": "Papers"}, "parent_key": result["parent"]["key"],
                  "expected_hashes": saved["expected_hashes"],
                  "files": [{"title": r["role"], "path": r["path"]} for r in saved["files"]]}
        for role in ingest.LOCAL_ONLY_TITLES:
            path = self.paths[role]
            legacy["expected_hashes"][role] = ingest.file_identity(path)
            legacy["files"].append({"title": role, "path": str(path)})
            path.unlink()
        self.receipt.write_text(json.dumps(legacy), encoding="utf-8")
        before = self.receipt.read_bytes()
        self.resume()
        self.assertEqual(self.receipt.read_bytes(), before)
        converted = self.receipt.with_name("upload.pyzotero.json")
        self.assertEqual(json.loads(converted.read_text(encoding="utf-8"))["status"], "files-verified")

    def test_authorization_remember_choices_errors_and_no_status_prompt(self):
        args = ingest.build_parser().parse_args(["authorize"])
        with mock.patch.object(credentials, "write_credential") as store, mock.patch.object(ingest, "dump_json"):
            for remember in (True, False):
                self.library.remember = remember
                store.reset_mock()
                ingest.cmd_authorize(args)
                self.assertEqual(store.call_count, int(remember))
                if remember:
                    self.assertEqual(store.call_args.args[0], credentials.LOCAL_TARGET + "/TESTSERVER")
            for status in (403, 429):
                self.library.authorize_status = status
                with self.assertRaises(sdk.ZoteroError):
                    ingest.cmd_authorize(args)
            self.library.authorize_status = 200
            before = len(self.library.requests)
            ingest.cmd_status(ingest.build_parser().parse_args(["status"]))
            self.assertTrue(all(r.method == "GET" for r in self.library.requests[before:]))

    def test_sdk_errors_redact_web_and_local_secrets(self):
        with self.real_client(api_key="web-secret", transport=httpx2.MockTransport(self.library.handle)) as client:
            client.zot.local_api_key = "local-secret"
            with mock.patch.object(client.zot, "item", side_effect=RuntimeError("/keys/web-secret local-secret")):
                with self.assertRaises(sdk.ZoteroError) as caught:
                    client.item("ANYKEY01")
            self.assertNotIn("web-secret", str(caught.exception))
            self.assertNotIn("local-secret", str(caught.exception))

    def test_local_only_workflow_imports_without_sdk(self):
        script = "import sys; sys.modules['pyzotero']=None; import batch_workflow; batch_workflow.build_parser().parse_args(['next','--ledger','unused.json'])"
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parent,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_attachment_metadata_response_loss_resumes_reserved_key(self):
        original = self.library.handle
        def handler(request):
            if request.method == "POST" and request.url.path.endswith("/items") and len(self.library.creates) == 1:
                self.library.lose_create = True
            return original(request)
        self.library.handle = handler
        with self.assertRaises(sdk.ZoteroError):
            self.deliver()
        self.assertEqual(len(self.library.items), 2)
        self.library.handle = original
        self.resume()
        self.assertEqual(len(self.library.creates), 4)

    def test_creation_rejection_does_not_report_delivery_success(self):
        original = self.library.handle
        def handler(request):
            if request.method == "POST" and request.url.path.endswith("/items"):
                return self.library.response(request, {"success": {}, "failed": {"0": {"code": 400, "message": "bad field"}}})
            return original(request)
        self.library.handle = handler
        with self.assertRaisesRegex(sdk.ZoteroError, "confirm.*creation"):
            self.deliver()
        self.assertEqual(json.loads(self.receipt.read_text(encoding="utf-8"))["status"], "interrupted")
        self.assertEqual(self.library.items, {})

    def test_missing_or_expired_local_key_leaves_receipt_without_authorization_loop(self):
        for key in (None, "expired-local-key"):
            self.receipt.unlink(missing_ok=True)
            before = len(self.library.requests)
            with mock.patch.object(credentials, "read_local_credential", return_value=key):
                with self.assertRaises(sdk.ZoteroError):
                    self.deliver()
            self.assertEqual(self.library.items, {})
            saved = json.loads(self.receipt.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "interrupted")
            self.assertNotIn("expired-local-key", self.receipt.read_text(encoding="utf-8"))
            self.assertTrue(all(r.url.path != "/api/local/authorize" for r in self.library.requests[before:]))

    def test_unrelated_file_tampering_blocks_completion(self):
        parent = self.parent()
        self.library.items["BOOKPDF1"] = self.library.row({"key": "BOOKPDF1", "itemType": "attachment", "parentItem": parent,
            "title": "Full book", "contentType": "application/pdf", "filename": "book.pdf", "linkMode": "imported_file"})
        self.library.files["BOOKPDF1"] = b"original book"
        original = self.library.handle
        def handler(request):
            response = original(request)
            if request.method == "POST" and "/api/local/uploads/" in request.url.path:
                self.library.files["BOOKPDF1"] = b"changed book"
            return response
        self.library.handle = handler
        with self.assertRaisesRegex(sdk.ZoteroError, "pre-existing stored file changed"):
            self.deliver(command="attach", parent=parent, prefix="chapter 1")
        self.assertEqual(json.loads(self.receipt.read_text(encoding="utf-8"))["status"], "interrupted")

    def test_authorization_timeout_uses_configured_wait_and_single_request(self):
        original = self.library.handle
        def handler(request):
            if request.url.path == "/api/local/authorize":
                self.assertEqual(request.extensions["timeout"]["read"], 3600)
                self.library.requests.append(request)
                raise httpx2.ReadTimeout("approval request expired", request=request)
            return original(request)
        self.library.handle = handler
        with mock.patch.object(credentials, "write_credential") as store:
            with self.assertRaisesRegex(sdk.ZoteroError, "expired"):
                ingest.cmd_authorize(ingest.build_parser().parse_args(["authorize"]))
            store.assert_not_called()
        self.assertEqual(sum(r.url.path == "/api/local/authorize" for r in self.library.requests), 1)

    def test_concurrent_authorization_is_refused(self):
        with sdk.authorization_lock("TESTSERVER"):
            with self.assertRaisesRegex(sdk.ZoteroError, "already active"):
                with sdk.authorization_lock("TESTSERVER"):
                    self.fail("second authorization lock acquired")

    def test_rate_limited_sdk_read_retries_and_returns_real_results(self):
        key = self.parent()
        original = self.library.handle
        limited = []
        def handler(request):
            if not limited and request.url.path.endswith("/items/top"):
                limited.append(True)
                return self.library.response(request, status=429, content=b"slow down", headers={"Retry-After": "0"})
            return original(request)
        with self.real_client(transport=httpx2.MockTransport(handler)) as client:
            rows = client.all("top")
        self.assertTrue(limited)
        self.assertEqual([r["key"] for r in rows], [key])

    def test_delete_limit_and_concurrent_library_change_are_guarded(self):
        parent = self.parent()
        args = ingest.build_parser().parse_args(["delete", "--metadata", str(self.metadata_path), "--item-key", parent, "--yes-delete"])
        for index in range(50):
            key = f"NOTE{index:04d}"
            self.library.items[key] = self.library.row({"key": key, "itemType": "note", "parentItem": parent})
        with self.assertRaisesRegex(sdk.ZoteroError, "more than 50"):
            ingest.cmd_delete(args)
        self.library.items = {parent: self.library.items[parent]}
        original = self.library.handle
        def handler(request):
            if request.method == "DELETE":
                self.library.version += 1
            return original(request)
        self.library.handle = handler
        with self.assertRaises(sdk.ZoteroError):
            ingest.cmd_delete(args)
        self.assertEqual(self.library.deleted, [])
        self.assertIn(parent, self.library.items)

    def test_search_pagination_does_not_miss_exact_match_after_first_page(self):
        for index in range(120):
            key = f"PAPER{index:03d}"
            data = {**self.metadata, "key": key, "DOI": f"10.9999/{index}"}
            self.library.items[key] = self.library.row(data)
        self.parent()
        with self.real_client(transport=httpx2.MockTransport(self.library.handle)) as client:
            self.assertEqual([r["key"] for r in ingest.local_matches(self.metadata, client=client)], ["PARENT01"])

    def test_canonical_target_disambiguates_duplicate_names_but_readonly_permissions_block_writes(self):
        self.library.collections.append({"key": "DUPLICAT", "data": {"name": "Papers", "parentCollection": False}})
        self.assertEqual(sdk.resolve_target("user:0/collections/DUPLICAT")["collection_key"], "DUPLICAT")
        self.desktop["targets"][0]["filesEditable"] = False
        with self.assertRaisesRegex(sdk.ZoteroError, "permission"):
            sdk.resolve_target("user:0/collections/DUPLICAT")

    def test_parent_metadata_strips_control_fields_and_keeps_arxiv(self):
        data = ingest.parent_metadata({**self.metadata, "_sourceParentKey": "PARENT01", "arXivID": "1234.5678"})
        self.assertNotIn("_sourceParentKey", data)
        self.assertNotIn("arXivID", data)
        self.assertEqual(data["extra"], "arXiv: 1234.5678")

    def test_web_credential_verification_uses_sdk_and_legacy_namespace(self):
        def handler(request):
            self.assertIn("/keys/web-secret", request.url.path)
            self.assertEqual(request.headers["Authorization"], "Bearer web-secret")
            return httpx2.Response(200, json={"userID": 7, "access": {"user": {"library": True}}}, request=request)
        with mock.patch.object(credentials, "_read_exact_credential", side_effect=[FileNotFoundError(), ("7", "web-secret")]) as read:
            self.assertEqual(credentials.read_credential(), ("7", "web-secret"))
            self.assertEqual(read.call_args.args, (credentials.LEGACY_TARGET,))
        with mock.patch.object(credentials, "read_credential", return_value=("7", "web-secret")), mock.patch.object(
            sdk, "Client", side_effect=lambda *a, **kw: self.real_client(*a, transport=httpx2.MockTransport(handler), **kw)):
            result = credentials.verify_credential()
        self.assertEqual(result["user_id"], "7")
        self.assertNotIn("web-secret", json.dumps(result))

    def test_batch_audit_checks_actual_collection_membership(self):
        import batch_workflow
        from test_batch_workflow import BatchWorkflowTests
        self.deliver()
        ledger_path = BatchWorkflowTests().make_batch(self.root)
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["zotero_target"] = {"id": "user:0/collections/COLLECT1", "name": "Papers"}
        result = batch_workflow.audit_paper_zotero(ledger, ledger_path, ledger["papers"][0])
        self.assertTrue(result["translated"])
        self.library.items[result["parent_key"]]["data"]["collections"] = []
        result = batch_workflow.audit_paper_zotero(ledger, ledger_path, ledger["papers"][0])
        self.assertFalse(result["translated"])
        self.assertIn("requested library/collection", result["verification"]["errors"][-1])

    def test_local_file_redirect_downloads_unicode_path_with_real_sdk(self):
        path = self.root / "中文 文件.md"
        path.write_bytes("exact 中文 bytes".encode())
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", path.as_uri())
                self.send_header("Zotero-Server-ID", "TESTSERVER")
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(sdk, "LOCAL_BASE", f"http://127.0.0.1:{server.server_port}"), self.real_client() as client:
                output = self.root / "download.md"
                client.download("FILE0001", output)
                self.assertEqual(output.read_bytes(), path.read_bytes())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
