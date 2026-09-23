#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import batch_workflow


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BatchWorkflowTests(unittest.TestCase):
    def make_batch(self, root: Path) -> Path:
        (root / "paper-list.pdf").write_bytes(b"%PDF paper list")
        source = root / "source.pdf"
        source.write_bytes(b"%PDF source")
        work = root / "work"
        work.mkdir()
        ledger = {
            "schema_version": 1,
            "batch_id": "classic-protocols",
            "source_list": {
                "path": "paper-list.pdf",
                "section": "Classic protocols",
            },
            "zotero_target": {"id": "C1", "name": "Unread papers"},
            "output_root": "output/pdf",
            "papers": [
                {
                    "order": 1,
                    "slug": "01-paper",
                    "title": "A Paper",
                    "year": "2020",
                    "identity": {"doi": "10.1000/example"},
                    "source_pdf": "source.pdf",
                    "work_dir": "work",
                    "final_dir": "translations/papers/group/01-paper",
                    "stage": "queued",
                }
            ],
        }
        path = root / "batch-run.json"
        write_json(path, ledger)
        return path

    def make_validated_artifacts(self, root: Path) -> None:
        source = root / "source.pdf"
        work = root / "work"
        source_hash = sha256(source)
        write_json(
            work / "manifest.json",
            {
                "source_sha256": source_hash,
                "boundary": {"needs_boundary_review": False},
                "source_review": {
                    "status": "pass",
                    "applied_operations": 0,
                },
                "visual_inventory": {"status": "pass"},
            },
        )
        (work / "translation-units.jsonl").write_text(
            '{"id":"u1","translatable":true}\n',
            encoding="utf-8",
        )
        (work / "translations.jsonl").write_text(
            '{"id":"u1","translated_text":"译文"}\n',
            encoding="utf-8",
        )
        (work / "中文翻译.pdf").write_bytes(b"%PDF translated")
        (work / "中文翻译正文.md").write_text("# 译文\n", encoding="utf-8")
        (work / "中文论文总结.md").write_text("# 总结\n", encoding="utf-8")
        write_json(
            work / "zotero-item.json",
            {"title": "A Paper", "date": "2020", "DOI": "10.1000/example"},
        )
        write_json(work / "中文翻译.layout.json", {"layout": "ok"})
        write_json(
            work / "translated-preview" / "validation-report.json",
            {
                "status": "pass",
                "metrics": {
                    "strict_invariants": True,
                    "invariant_text_misses": 0,
                    "translated_pages": 1,
                },
            },
        )
        write_json(
            work / "quick-validation.json",
            {"status": "pass", "metrics": {"source_sha256": source_hash,
             "pdf_sha256": sha256(work / "中文翻译.pdf"), "pages": 1}},
        )
        write_json(
            work / "summary-validation.json",
            {"status": "pass", "metrics": {"characters": 1250}},
        )
        write_json(
            work / "source-review-report.json",
            {"status": "pass", "applied_operations": 0},
        )
        write_json(work / "visual-review.json", {
            "schema_version": 1, "status": "pass", "page_count": 1, "source_sha256": source_hash,
            "translated_sha256": sha256(work / "中文翻译.pdf"),
            "reviewer": {"role": "visual-review", "agent_id": "test-reviewer", "mode": "subagent"},
            "contact_sheet_reviewed": True, "open_issues": [],
            "pages": [{"page": 1, "status": "pass", "image": "page-0001.png",
                       "checks": ["Margins and text legible."], "findings": []}],
        })

    def test_visual_review_is_required_and_bound_to_packaged_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = self.make_batch(root)
            self.make_validated_artifacts(root)
            review = root / "work" / "visual-review.json"
            valid = review.read_bytes()
            review.unlink()
            args = argparse.Namespace(ledger=ledger_path, slug="01-paper")
            with self.assertRaises(batch_workflow.BatchWorkflowError):
                batch_workflow.cmd_package(args)
            review.write_bytes(valid)
            with mock.patch("builtins.print"):
                batch_workflow.cmd_package(args)
            ledger, path = batch_workflow.load_ledger(ledger_path)
            paths = batch_workflow._paper_paths(ledger, path, ledger["papers"][0])
            final_pdf = paths["final_dir"] / "中文翻译.pdf"
            accepted_bytes = final_pdf.read_bytes()
            paths["translated_pdf"].write_bytes(b"unreviewed staging revision")
            with self.assertRaises(batch_workflow.BatchWorkflowError):
                batch_workflow.cmd_package(args)
            self.assertEqual(final_pdf.read_bytes(), accepted_bytes)
            (paths["final_dir"] / "中文翻译.pdf").write_bytes(b"changed after review")
            self.assertFalse(batch_workflow._final_bundle_complete(paths))

    def test_next_resumes_first_nonterminal_paper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = self.make_batch(root)
            args = argparse.Namespace(ledger=ledger_path)
            with mock.patch("builtins.print") as output:
                result = batch_workflow.cmd_next(args)
            saved, _ = batch_workflow.load_ledger(ledger_path)
        self.assertEqual(result, 0)
        self.assertEqual(saved["papers"][0]["stage"], "queued")
        payload = json.loads(output.call_args.args[0])
        self.assertEqual(payload["paper"]["slug"], "01-paper")

    def test_package_uses_configurable_final_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = self.make_batch(root)
            self.make_validated_artifacts(root)
            args = argparse.Namespace(ledger=ledger_path, slug="01-paper")
            with mock.patch("builtins.print"):
                result = batch_workflow.cmd_package(args)
            ledger, _ = batch_workflow.load_ledger(ledger_path)
            final_dir = root / "translations" / "papers" / "group" / "01-paper"
            final_names = sorted(path.name for path in final_dir.iterdir())
        self.assertEqual(result, 0)
        self.assertEqual(ledger["papers"][0]["stage"], "packaged")
        self.assertEqual(
            final_names,
            sorted(
                name
                for key, name in batch_workflow.FINAL_NAMES.items()
                if key != "source_review_report"
            ),
        )

    def test_strict_validation_flag_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = self.make_batch(root)
            self.make_validated_artifacts(root)
            report = root / "work" / "translated-preview" / "validation-report.json"
            payload = json.loads(report.read_text(encoding="utf-8"))
            payload["metrics"]["strict_invariants"] = False
            write_json(report, payload)
            ledger, path = batch_workflow.load_ledger(ledger_path)
            batch_workflow.refresh_ledger(ledger, path)
        self.assertEqual(ledger["papers"][0]["stage"], "translated")

    def test_source_hash_change_invalidates_only_that_paper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = self.make_batch(root)
            self.make_validated_artifacts(root)
            with mock.patch("builtins.print"):
                batch_workflow.cmd_package(
                    argparse.Namespace(ledger=ledger_path, slug="01-paper")
                )
            (root / "source.pdf").write_bytes(b"%PDF changed")
            ledger, path = batch_workflow.load_ledger(ledger_path)
            batch_workflow.refresh_ledger(ledger, path)
        self.assertEqual(ledger["papers"][0]["stage"], "queued")
        self.assertIn("hash changed", ledger["papers"][0]["state_error"])

    def test_audit_zotero_marks_complete_existing_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = self.make_batch(root)
            result = {
                "ok": True,
                "translated": True,
                "parent_key": "PARENT1",
                "verification": {
                    "ok": True,
                    "children": [
                        {"title": "原文 PDF"},
                        {"title": "中文翻译 PDF"},
                        {"title": "中文翻译 Markdown"},
                        {"title": "中文论文总结"},
                    ],
                },
            }
            with (
                mock.patch.object(
                    batch_workflow,
                    "audit_paper_zotero",
                    return_value=result,
                ),
                mock.patch("builtins.print"),
            ):
                code = batch_workflow.cmd_audit_zotero(
                    argparse.Namespace(ledger=ledger_path)
                )
            ledger, _ = batch_workflow.load_ledger(ledger_path)
        self.assertEqual(code, 0)
        self.assertEqual(ledger["papers"][0]["stage"], "skipped_existing")
        self.assertEqual(ledger["papers"][0]["zotero_parent_key"], "PARENT1")

    def test_record_zotero_requires_live_four_child_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = self.make_batch(root)
            self.make_validated_artifacts(root)
            with mock.patch("builtins.print"):
                batch_workflow.cmd_package(
                    argparse.Namespace(ledger=ledger_path, slug="01-paper")
                )
            result = {
                "ok": True,
                "translated": True,
                "parent_key": "PARENT1",
                "verification": {"ok": True, "children": [{}, {}, {}, {}]},
            }
            with (
                mock.patch.object(
                    batch_workflow,
                    "audit_paper_zotero",
                    return_value=result,
                ),
                mock.patch("builtins.print"),
            ):
                code = batch_workflow.cmd_record_zotero(
                    argparse.Namespace(
                        ledger=ledger_path,
                        slug="01-paper",
                        parent_key="PARENT1",
                    )
                )
            ledger, _ = batch_workflow.load_ledger(ledger_path)
        self.assertEqual(code, 0)
        self.assertEqual(ledger["papers"][0]["stage"], "zotero_verified")

    def test_local_only_batch_treats_packaged_as_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = self.make_batch(root)
            payload = json.loads(ledger_path.read_text(encoding="utf-8"))
            payload["zotero_enabled"] = False
            payload.pop("zotero_target")
            write_json(ledger_path, payload)
            self.make_validated_artifacts(root)
            with mock.patch("builtins.print"):
                batch_workflow.cmd_package(
                    argparse.Namespace(ledger=ledger_path, slug="01-paper")
                )
            with mock.patch("builtins.print") as output:
                batch_workflow.cmd_next(argparse.Namespace(ledger=ledger_path))
            result = json.loads(output.call_args.args[0])
        self.assertTrue(result["complete"])

    def test_invalid_stage_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger_path = self.make_batch(root)
            payload = json.loads(ledger_path.read_text(encoding="utf-8"))
            payload["papers"][0]["stage"] = "published"
            write_json(ledger_path, payload)
            with self.assertRaises(batch_workflow.BatchWorkflowError):
                batch_workflow.load_ledger(ledger_path)


if __name__ == "__main__":
    unittest.main()
