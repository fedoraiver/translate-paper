#!/usr/bin/env python3

from __future__ import annotations

import http.server
import json
import tempfile
from pathlib import Path
import threading
import unittest

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

    def test_summary_html_has_one_fixed_title_and_escapes_text(self) -> None:
        rendered = ingest.summary_markdown_to_html(
            "# 中文论文总结\n\n## 研究问题\n\nA < B\n\n- 一\n- 二"
        )
        self.assertEqual(rendered.count("<h1>中文论文总结</h1>"), 1)
        self.assertIn("<h3>研究问题</h3>", rendered)
        self.assertIn("A &lt; B", rendered)
        self.assertIn("<ul><li>一</li><li>二</li></ul>", rendered)

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
                    "itemType": "note",
                    "note": "<h1>中文论文总结</h1><p>内容</p>",
                },
            },
        ]
        self.assertTrue(ingest.verify_generated_children(children)["ok"])
        extra = children + [
            {
                "key": "EXTRA001",
                "data": {"itemType": "note", "note": "<p>unexpected</p>"},
            }
        ]
        self.assertFalse(ingest.verify_generated_children(extra)["ok"])

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
