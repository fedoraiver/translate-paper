#!/usr/bin/env python3

from __future__ import annotations

import http.server
import json
import tempfile
from pathlib import Path
import threading
import unittest
import urllib.parse
from unittest import mock
from test_export_latex_project import make_archive

import zotero_ingest as ingest


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

    def test_connector_item_overrides_id_and_strips_control_fields(self) -> None:
        metadata = {
            **self.metadata,
            "arXivID": "1234.5678",
            "_private": True,
            "_sourceParentKey": "6ACE9QQE",
        }
        item = ingest.connector_item(metadata, "paper-id")
        self.assertEqual(item["id"], "paper-id")
        self.assertNotIn("arXivID", item)
        self.assertNotIn("_private", item)
        self.assertNotIn("_sourceParentKey", item)

    def test_pdf_signature_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / "good.pdf"
            bad = Path(directory) / "bad.pdf"
            good.write_bytes(b"%PDF-1.7\n")
            bad.write_bytes(b"plain text")
            ingest.validate_pdf(good, "good")
            with self.assertRaises(ingest.ZoteroError):
                ingest.validate_pdf(bad, "bad")

    def test_markdown_upload_url_retains_extension_for_connector(self) -> None:
        for source in ("https://eprint.iacr.org/1996/004",
                       "https://example.test/paper.pdf?download=1#page=3"):
            url = ingest.markdown_attachment_url(source, "中文论文总结.md")
            parsed = urllib.parse.urlsplit(url)
            self.assertTrue(parsed.path.endswith(".md"))
            self.assertTrue(urllib.parse.unquote(parsed.path).endswith("中文论文总结.md"))
            self.assertEqual(parsed.query, "")

    def test_generated_child_verification(self) -> None:
        children = [
            {
                "key": "SOURCE01",
                "data": {
                    "itemType": "attachment",
                    "title": ingest.SOURCE_ATTACHMENT_TITLE,
                    "linkMode": "imported_file",
                    "contentType": "application/pdf",
                },
            },
            {
                "key": "TRANS001",
                "data": {
                    "itemType": "attachment",
                    "title": ingest.TRANSLATION_ATTACHMENT_TITLE,
                    "linkMode": "imported_url",
                    "contentType": "application/pdf",
                },
            },
            {
                "key": "MARKDOWN",
                "data": {
                    "itemType": "attachment",
                    "title": ingest.TRANSLATION_MARKDOWN_TITLE,
                    "linkMode": "imported_file",
                    "contentType": "text/markdown",
                },
            },
            {
                "key": "SUMMARY1",
                "data": {
                    "itemType": "attachment",
                    "title": ingest.SUMMARY_ATTACHMENT_TITLE,
                    "linkMode": "imported_file",
                    "contentType": "text/markdown",
                    "filename": "中文论文总结.md",
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
        legacy = children[:3] + children[4:] + [{"data": {
            "itemType": "note", "note": "<h1>中文论文总结</h1><p>内容</p>"
        }}]
        self.assertFalse(ingest.verify_generated_children(legacy)["ok"])
        for field, value in (("filename", "summary.html"),
                             ("linkMode", "linked_file"),
                             ("contentType", "text/html")):
            with self.subTest(field=field):
                invalid = children[:3] + children[4:] + [{"data": {**children[3]["data"], field: value}}]
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
            (root / "translation.md").write_text("translated body", encoding="utf-8")
            (root / "summary.md").write_text("summary", encoding="utf-8")
            make_archive(archive, source, translated)
            specs = [(ingest.SOURCE_ATTACHMENT_TITLE, "application/pdf", "source.pdf"),
                     (ingest.TRANSLATION_ATTACHMENT_TITLE, "application/pdf", "translated.pdf"),
                     (ingest.TRANSLATION_MARKDOWN_TITLE, "text/markdown", "translation.md"),
                     (ingest.SUMMARY_ATTACHMENT_TITLE, "text/markdown", "summary.md"),
                     (ingest.LATEX_SOURCE_TITLE, "application/zip", "source.zip")]
            children = [{"data": {"itemType": "attachment", "title": title, "contentType": mime,
                                  "filename": filename, "linkMode": "imported_file"}}
                        for title, mime, filename in specs]
            def child_path(child):
                return root / child["data"]["filename"]
            with mock.patch.object(ingest, "stored_child_path", side_effect=child_path):
                self.assertTrue(ingest.verify_generated_children(children, verify_archive_identity=True)["ok"])
                translated.write_bytes(b"changed")
                result = ingest.verify_generated_children(children, verify_archive_identity=True)
            self.assertFalse(result["ok"])
            self.assertIn("identity mismatch", result["errors"][0])

    def test_all_children_are_read_beyond_one_hundred(self) -> None:
        children = [{"key": f"KEY{i:05d}", "data": {"itemType": "attachment"}}
                    for i in range(206)]
        for route in ("local", "web"):
            with self.subTest(route=route):
                def page(path, **kwargs):
                    start = int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["start"][0])
                    return ingest.HttpResponse(200, {}, json.dumps(children[start:start + 100]).encode())
                name = "local_request" if route == "local" else "web_request"
                with mock.patch.object(ingest, name, side_effect=page) as request:
                    result = (ingest.fetch_local_children("PARENT01") if route == "local"
                              else ingest.fetch_web_children("PARENT01", user_id="1", api_key="test-only"))
                self.assertEqual(result, children)
                self.assertEqual(request.call_count, 3)

    def test_exact_multiple_of_page_size_terminates_on_empty_page(self) -> None:
        children = [{"key": f"KEY{i:05d}"} for i in range(100)]
        pages = [ingest.HttpResponse(200, {}, json.dumps(children).encode()),
                 ingest.HttpResponse(200, {}, b"[]")]
        with mock.patch.object(ingest, "local_request", side_effect=pages) as request:
            self.assertEqual(ingest.fetch_local_children("PARENT01"), children)
        self.assertEqual(request.call_count, 2)

    def test_repeated_child_page_stops_instead_of_looping(self) -> None:
        children = [{"key": f"KEY{i:05d}"} for i in range(100)]
        response = ingest.HttpResponse(200, {}, json.dumps(children).encode())
        with mock.patch.object(ingest, "local_request", return_value=response) as request:
            with self.assertRaisesRegex(ingest.ZoteroError, "duplicate.*repeated"):
                ingest.fetch_local_children("PARENT01")
        self.assertEqual(request.call_count, 2)

    def test_invalid_child_pages_are_rejected(self) -> None:
        for payload in ({"error": "not a list"}, [None], [{}], [{"key": "A"}, {"key": "A"}]):
            with self.subTest(payload=payload):
                response = ingest.HttpResponse(200, {}, json.dumps(payload).encode())
                with mock.patch.object(ingest, "local_request", return_value=response):
                    with self.assertRaises(ingest.ZoteroError):
                        ingest.fetch_local_children("PARENT01")

    def _stored_bundle(self, root):
        paths = {title: root / name for title, name in ingest.FINAL_FILENAMES.items()}
        paths[ingest.SOURCE_ATTACHMENT_TITLE].write_bytes(b"%PDF-source")
        paths[ingest.TRANSLATION_ATTACHMENT_TITLE].write_bytes(b"%PDF-accepted")
        paths[ingest.TRANSLATION_MARKDOWN_TITLE].write_bytes(b"body accepted\n")
        paths[ingest.SUMMARY_ATTACHMENT_TITLE].write_bytes(b"summary good\n")
        make_archive(paths[ingest.LATEX_SOURCE_TITLE], paths[ingest.SOURCE_ATTACHMENT_TITLE],
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

    def test_five_files_are_byte_verified_despite_connector_renaming(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, children, stored, expected = self._stored_bundle(Path(directory))
            with mock.patch.object(ingest, "stored_child_path", side_effect=lambda child: stored[child["key"]]):
                result = ingest.verify_generated_children(children, verify_archive_identity=True,
                    expected_hashes=expected, expected_parent_key="PARENT01")
            self.assertTrue(result["ok"], result["errors"])
            self.assertEqual(len(result["verified_files"]), 5)
            self.assertEqual({r["title"]: {k: r[k] for k in ("sha256", "md5", "size")}
                              for r in result["verified_files"]}, expected)

    def test_same_length_markdown_corruption_cannot_pass_zip_identity_check(self) -> None:
        for title, changed in ((ingest.TRANSLATION_MARKDOWN_TITLE, b"body tampered\n"),
                               (ingest.SUMMARY_ATTACHMENT_TITLE, b"summary evil\n")):
            with self.subTest(title=title), tempfile.TemporaryDirectory() as directory:
                paths, children, stored, expected = self._stored_bundle(Path(directory))
                self.assertEqual(paths[title].stat().st_size, len(changed))
                paths[title].write_bytes(changed)
                with mock.patch.object(ingest, "stored_child_path", side_effect=lambda child: stored[child["key"]]):
                    result = ingest.verify_generated_children(children, verify_archive_identity=True,
                        expected_hashes=expected, expected_parent_key="PARENT01")
                self.assertFalse(result["ok"])
                self.assertIn(f"stored file bytes differ from accepted input: {title}", result["errors"])

    def test_missing_stored_file_is_not_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, children, stored, expected = self._stored_bundle(Path(directory))
            paths[ingest.SUMMARY_ATTACHMENT_TITLE].unlink()
            with mock.patch.object(ingest, "stored_child_path", side_effect=lambda child: stored[child["key"]]):
                result = ingest.verify_generated_children(children, expected_hashes=expected)
            self.assertFalse(result["ok"])
            self.assertEqual(len(result["verified_files"]), 4)

    def test_wrong_parent_is_rejected_before_reading_file_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, children, _, expected = self._stored_bundle(Path(directory))
            children[-1]["data"]["parentItem"] = "OTHER001"
            with mock.patch.object(ingest, "stored_child_path") as locate:
                result = ingest.verify_generated_children(children, expected_hashes=expected,
                    expected_parent_key="PARENT01")
            self.assertFalse(result["ok"])
            locate.assert_not_called()

    def test_ingest_keeps_existing_parent_guard_without_connector_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, _, _, _ = self._stored_bundle(root)
            metadata = root / "zotero-item.json"
            metadata.write_text(json.dumps(self.metadata), encoding="utf-8")
            args = ingest.build_parser().parse_args([
                "ingest", "--metadata", str(metadata), "--target-id", "L1", "--yes-ingest",
                "--source-pdf", str(paths[ingest.SOURCE_ATTACHMENT_TITLE]),
                "--translated-pdf", str(paths[ingest.TRANSLATION_ATTACHMENT_TITLE]),
                "--translated-markdown", str(paths[ingest.TRANSLATION_MARKDOWN_TITLE]),
                "--summary", str(paths[ingest.SUMMARY_ATTACHMENT_TITLE]),
                "--latex-source-archive", str(paths[ingest.LATEX_SOURCE_TITLE]),
            ])
            with (mock.patch.object(ingest, "resolve_target", return_value={"id": "L1"}),
                  mock.patch.object(ingest, "read_api_credentials", return_value=("1", "test-only")),
                  mock.patch.object(ingest, "local_matches", return_value=[{"key": "PARENT01"}]),
                  mock.patch.object(ingest, "web_matches", return_value=[]),
                  mock.patch.object(ingest, "connector_request") as connector,
                  mock.patch.object(ingest, "connector_upload_file") as upload):
                with self.assertRaisesRegex(ingest.ZoteroError, "exact parent already exists"):
                    ingest.cmd_ingest(args)
            connector.assert_not_called()
            upload.assert_not_called()

    def test_verify_expected_directory_detects_corrupt_stored_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, children, _, _ = self._stored_bundle(root)
            metadata = root / "zotero-item.json"
            metadata.write_text(json.dumps(self.metadata), encoding="utf-8")
            stored = {}
            (root / "stored").mkdir()
            for child in children:
                path = root / "stored" / child["data"]["filename"]
                path.write_bytes(paths[child["data"]["title"]].read_bytes())
                stored[child["key"]] = path
            args = ingest.build_parser().parse_args([
                "verify", "--metadata", str(metadata), "--expected-dir", str(root),
            ])
            with (mock.patch.object(ingest, "local_matches", return_value=[{"key": "PARENT01", "data": self.metadata}]),
                  mock.patch.object(ingest, "fetch_local_children", return_value=children),
                  mock.patch.object(ingest, "stored_child_path", side_effect=lambda child: stored[child["key"]]),
                  mock.patch.object(ingest, "dump_json") as output):
                self.assertEqual(ingest.cmd_verify(args), 0)
                body = next(child for child in children if child["data"]["title"] == ingest.TRANSLATION_MARKDOWN_TITLE)
                stored[body["key"]].write_bytes(b"body tampered\n")
                self.assertEqual(ingest.cmd_verify(args), 1)
            self.assertIn("stored file bytes differ from accepted input: " + ingest.TRANSLATION_MARKDOWN_TITLE,
                          output.call_args.args[0]["errors"])

    def test_ingest_does_not_report_success_for_corrupt_uploaded_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, children, _, _ = self._stored_bundle(root)
            metadata = root / "zotero-item.json"
            metadata.write_text(json.dumps(self.metadata), encoding="utf-8")
            (root / "stored").mkdir()
            stored = {}
            for child in children:
                path = root / "stored" / child["data"]["filename"]
                path.write_bytes(paths[child["data"]["title"]].read_bytes())
                stored[child["key"]] = path
            def upload(path, **kwargs):
                if kwargs["title"] == ingest.SUMMARY_ATTACHMENT_TITLE:
                    child = next(c for c in children if c["data"]["title"] == ingest.SUMMARY_ATTACHMENT_TITLE)
                    stored[child["key"]].write_bytes(b"summary evil\n")
            args = ingest.build_parser().parse_args([
                "ingest", "--metadata", str(metadata), "--target-id", "L1", "--yes-ingest",
                "--source-pdf", str(paths[ingest.SOURCE_ATTACHMENT_TITLE]),
                "--translated-pdf", str(paths[ingest.TRANSLATION_ATTACHMENT_TITLE]),
                "--translated-markdown", str(paths[ingest.TRANSLATION_MARKDOWN_TITLE]),
                "--summary", str(paths[ingest.SUMMARY_ATTACHMENT_TITLE]),
                "--latex-source-archive", str(paths[ingest.LATEX_SOURCE_TITLE]),
            ])
            with (mock.patch.object(ingest, "resolve_target", return_value={"id": "L1", "name": "My Library"}),
                  mock.patch.object(ingest, "read_api_credentials", return_value=("1", "test-only")),
                  mock.patch.object(ingest, "local_matches", return_value=[]),
                  mock.patch.object(ingest, "web_matches", return_value=[]),
                  mock.patch.object(ingest, "connector_request"),
                  mock.patch.object(ingest, "connector_upload_file", side_effect=upload),
                  mock.patch.object(ingest, "wait_for_unique_local_match", return_value=[{"key": "PARENT01", "data": self.metadata}]),
                  mock.patch.object(ingest, "fetch_local_children", return_value=children),
                  mock.patch.object(ingest, "stored_child_path", side_effect=lambda child: stored[child["key"]]),
                  mock.patch.object(ingest, "selected_target", return_value={"libraryID": 1, "id": None,
                      "targets": [{"id": "L1", "name": "My Library"}]}),
                  mock.patch.object(ingest, "dump_json") as output):
                with self.assertRaisesRegex(ingest.ZoteroError, "stored file bytes differ from accepted input"):
                    ingest.cmd_ingest(args)
            output.assert_not_called()

    def _receipt_ingest_args(self, root, paths, receipt):
        metadata = root / "zotero-item.json"
        metadata.write_text(json.dumps(self.metadata), encoding="utf-8")
        return ingest.build_parser().parse_args([
            "ingest", "--metadata", str(metadata), "--target-id", "L1", "--yes-ingest",
            "--receipt", str(receipt),
            "--source-pdf", str(paths[ingest.SOURCE_ATTACHMENT_TITLE]),
            "--translated-pdf", str(paths[ingest.TRANSLATION_ATTACHMENT_TITLE]),
            "--translated-markdown", str(paths[ingest.TRANSLATION_MARKDOWN_TITLE]),
            "--summary", str(paths[ingest.SUMMARY_ATTACHMENT_TITLE]),
            "--latex-source-archive", str(paths[ingest.LATEX_SOURCE_TITLE]),
        ])

    def _ingest_mocks(self, children, stored):
        return {
            "resolve_target": mock.Mock(return_value={"id": "L1", "name": "My Library"}),
            "read_api_credentials": mock.Mock(return_value=("1", "test-only")),
            "local_matches": mock.Mock(return_value=[]),
            "web_matches": mock.Mock(return_value=[]),
            "connector_request": mock.Mock(),
            "connector_upload_file": mock.Mock(),
            "wait_for_unique_local_match": mock.Mock(return_value=[{"key": "PARENT01", "data": self.metadata}]),
            "fetch_local_children": mock.Mock(return_value=children),
            "stored_child_path": mock.Mock(side_effect=lambda child: stored[child["key"]]),
            "selected_target": mock.Mock(return_value={"libraryID": 1, "id": None,
                "targets": [{"id": "L1", "name": "My Library"}]}),
            "dump_json": mock.Mock(),
        }

    def test_receipt_exists_before_creation_and_preserves_uncertain_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, children, stored, _ = self._stored_bundle(root)
            receipt = root / "upload.json"
            args = self._receipt_ingest_args(root, paths, receipt)
            mocks = self._ingest_mocks(children, stored)
            observed = {}
            def request(path, **kwargs):
                if path.endswith("saveItems"):
                    saved = json.loads(receipt.read_text(encoding="utf-8"))
                    observed.update(session_id=saved["session_id"], alias=saved["connector_item_id"])
                    self.assertEqual(saved["session_id"], kwargs["json_body"]["sessionID"])
                    self.assertEqual(saved["connector_item_id"], kwargs["json_body"]["items"][0]["id"])
            def upload(path, **kwargs):
                if kwargs["title"] == ingest.TRANSLATION_ATTACHMENT_TITLE:
                    raise ingest.ZoteroError("Simulated lost response")
            mocks["connector_request"].side_effect = request
            mocks["connector_upload_file"].side_effect = upload
            with mock.patch.multiple(ingest, **mocks):
                with self.assertRaisesRegex(ingest.ZoteroError, "client item alias"):
                    ingest.cmd_ingest(args)
            saved = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "interrupted")
            self.assertEqual(saved["session_id"], observed["session_id"])
            self.assertEqual(saved["connector_item_id"], observed["alias"])
            self.assertEqual(saved["parent_key"], "PARENT01")
            self.assertEqual([r["status"] for r in saved["files"]],
                             ["uploaded", "uploading", "pending", "pending", "pending"])
            self.assertNotIn("test-only", receipt.read_text(encoding="utf-8"))
            mocks["dump_json"].assert_not_called()

    def test_existing_receipt_is_not_overwritten_or_reimported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, children, stored, _ = self._stored_bundle(root)
            receipt = root / "upload.json"
            original = b'{"status":"interrupted","session_id":"old-session"}\n'
            receipt.write_bytes(original)
            args = self._receipt_ingest_args(root, paths, receipt)
            mocks = self._ingest_mocks(children, stored)
            with mock.patch.multiple(ingest, **mocks):
                with self.assertRaisesRegex(ingest.ZoteroError, "receipt already exists"):
                    ingest.cmd_ingest(args)
            self.assertEqual(receipt.read_bytes(), original)
            mocks["connector_request"].assert_not_called()
            mocks["connector_upload_file"].assert_not_called()

    def test_completed_receipt_contains_five_verified_keys_and_original_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, children, stored, expected = self._stored_bundle(root)
            receipt = root / "upload.json"
            args = self._receipt_ingest_args(root, paths, receipt)
            mocks = self._ingest_mocks(children, stored)
            with mock.patch.multiple(ingest, **mocks):
                self.assertEqual(ingest.cmd_ingest(args), 0)
            saved = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "files-verified")
            self.assertEqual(saved["expected_hashes"], expected)
            self.assertEqual({r["key"] for r in saved["files"]}, {c["key"] for c in children})
            self.assertTrue(all(r["status"] == "verified" for r in saved["files"]))
            result = mocks["dump_json"].call_args.args[0]
            self.assertEqual(result["connector_item_id"], saved["connector_item_id"])
            self.assertEqual(result["session_id"], saved["session_id"])

    def test_streams_pdf_to_connector_with_required_headers(self) -> None:
        observed: dict[str, object] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                observed.update(
                    {
                        "path": self.path,
                        "content_type": self.headers["Content-Type"],
                        "metadata": self.headers["X-Metadata"],
                        "body": self.rfile.read(length),
                    }
                )
                self.send_response(201)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original_base = ingest.LOCAL_BASE
        try:
            ingest.LOCAL_BASE = f"http://127.0.0.1:{server.server_port}"
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "paper.pdf"
                path.write_bytes(b"%PDF-test")
                response = ingest.connector_upload_pdf(
                    path,
                    session_id="session",
                    parent_item_id="parent",
                    title=ingest.SOURCE_ATTACHMENT_TITLE,
                    url="https://example.test/paper.pdf",
                )
            self.assertEqual(response.status, 201)
            self.assertEqual(observed["path"], "/connector/saveAttachment")
            self.assertEqual(observed["content_type"], "application/pdf")
            self.assertEqual(observed["body"], b"%PDF-test")
            metadata = json.loads(str(observed["metadata"]))
            self.assertEqual(metadata["parentItemID"], "parent")
        finally:
            ingest.LOCAL_BASE = original_base
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_streams_markdown_to_connector_with_required_headers(self) -> None:
        observed: dict[str, object] = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                observed.update(
                    {
                        "content_type": self.headers["Content-Type"],
                        "body": self.rfile.read(length),
                    }
                )
                self.send_response(201)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original_base = ingest.LOCAL_BASE
        try:
            ingest.LOCAL_BASE = f"http://127.0.0.1:{server.server_port}"
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "translation.md"
                path.write_text("# 中文翻译", encoding="utf-8")
                response = ingest.connector_upload_file(
                    path,
                    session_id="session",
                    parent_item_id="parent",
                    title=ingest.TRANSLATION_MARKDOWN_TITLE,
                    url="https://example.test/translation.md",
                    content_type="text/markdown",
                )
            self.assertEqual(response.status, 201)
            self.assertEqual(observed["content_type"], "text/markdown")
            self.assertEqual(observed["body"], "# 中文翻译".encode("utf-8"))
        finally:
            ingest.LOCAL_BASE = original_base
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
