"""Isolated repair preserves completed records and leaves baseline intact on failure."""
import copy
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from paper_repair import repair
import paper_repair
from paper_state import atomic_json
from translation_checkpoint import atomic_write, read_records


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name) / "paper"
        self.work.mkdir()
        (self.work / "source.pdf").write_bytes(b"immutable source")
        self.hash = hashlib.sha256(b"immutable source").hexdigest()
        self.units = [{"id": f"u{i}", "kind": "body", "page": 1,
                       "translatable": True, "source_text": "Source prose.",
                       "protected_text": "Source prose.", "protected_tokens": {},
                       "inline_fragments": {}, "style_tokens": {}} for i in range(2)]
        self.records = [{**u, "translated_text": "已完成的译文。"} for u in self.units]
        atomic_write(self.work / "translation-units.jsonl", self.units)
        atomic_write(self.work / "translations.jsonl", self.records)
        atomic_write(self.work / "translations.template.jsonl", [{**u, "translated_text": ""} for u in self.units])
        self.manifest = self.work / "manifest.json"
        atomic_json(self.manifest, {"source_pdf": "source.pdf", "source_sha256": self.hash,
                                   "paths": {"translation_units": "translation-units.jsonl",
                                             "translations_template": "translations.template.jsonl",
                                             "source_review": "source-review.json"}})
        atomic_json(self.work / "source-review.json", {"source_sha256": self.hash})
        self.proposal = Path(self.temp.name) / "proposal.json"
        atomic_json(self.proposal, {"source_sha256": self.hash, "reviewed": True,
                                   "reason": "Source-verified split", "operations": []})
        self.original = {p.name: p.read_bytes() for p in self.work.iterdir() if p.is_file()}

    def builder(self, work):
        new_units = copy.deepcopy(self.units)
        new_units[0]["source_text"] = "Changed source prose."
        new_units[0]["protected_text"] = "Changed source prose."
        new_units.append({**self.units[0], "id": "review-p0001-u0001"})
        atomic_write(work / "translation-units.jsonl", new_units)
        atomic_write(work / "translations.template.jsonl", [{**u, "translated_text": ""} for u in new_units])
        atomic_json(work / "source-review-report.json", {"status": "pass"})
        atomic_json(work / "visual-layout.json", {"status": "pass"})

    def assert_baseline_unchanged(self):
        for name, data in self.original.items():
            self.assertEqual((self.work / name).read_bytes(), data, name)

    def test_default_is_impact_only_without_publishing_or_state(self):
        report = repair(self.manifest, self.proposal, builder=self.builder)
        self.assertFalse(report["executed"])
        self.assertEqual(report["reopened_completed_ids"], ["u0"])
        self.assertEqual(report["preserved_completed_ids"], ["u1"])
        self.assert_baseline_unchanged()
        self.assertFalse((self.work / "run-state.json").exists())

    def test_apply_only_reopens_changed_anchors_and_updates_one_pointer(self):
        destination = Path(self.temp.name) / "revision-1"
        report = repair(self.manifest, self.proposal, output_dir=destination,
                        apply=True, builder=self.builder)
        new = {r["id"]: r for r in read_records(destination / "translations.jsonl")}
        self.assertEqual(new["u1"], self.records[1])
        self.assertEqual(new["u0"]["translated_text"], "")
        self.assertEqual(new["review-p0001-u0001"]["translated_text"], "")
        self.assert_baseline_unchanged()
        self.assertEqual((destination / "baseline/translations.jsonl").read_bytes(), self.original["translations.jsonl"])
        state = json.loads((self.work / "run-state.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(state["active_manifest"]), destination / "manifest.json")
        self.assertTrue(report["executed"])

    def test_failed_preparation_or_existing_destination_never_publishes(self):
        destination = Path(self.temp.name) / "failed"
        def fail(work):
            (work / "diagnostic.txt").write_text("candidate only")
            raise ValueError("invalid reviewed source")
        with self.assertRaisesRegex(ValueError, "invalid reviewed source"):
            repair(self.manifest, self.proposal, output_dir=destination, apply=True, builder=fail)
        self.assertFalse(destination.exists())
        self.assert_baseline_unchanged()
        destination.mkdir()
        with self.assertRaisesRegex(ValueError, "non-existing"):
            repair(self.manifest, self.proposal, output_dir=destination, apply=True, builder=self.builder)

    def test_explicit_default_flow_fields_do_not_reopen_legacy_paragraph(self):
        def migrate(work):
            self.builder(work)
            units = read_records(work / "translation-units.jsonl")
            for unit in units:
                unit.update(flow_role="paragraph", list_id=None, list_level=0,
                            source_marker="", flow_reviewed=False)
            atomic_write(work / "translation-units.jsonl", units)
            atomic_write(work / "translations.template.jsonl", [{**u, "translated_text": ""} for u in units])
        destination = Path(self.temp.name) / "migrated"
        result = repair(self.manifest, self.proposal, output_dir=destination, apply=True, builder=migrate)
        self.assertEqual(result["reopened_completed_ids"], ["u0"])
        self.assertEqual(read_records(destination / "translations.jsonl")[1], self.records[1])

    def test_removed_completed_record_is_explicitly_reported_and_backed_up(self):
        def suppress(work):
            self.builder(work)
            atomic_write(work / "translation-units.jsonl", [self.units[1]])
            atomic_write(work / "translations.template.jsonl", [{**self.units[1], "translated_text": ""}])
        report = repair(self.manifest, self.proposal, builder=suppress)
        self.assertEqual(report["removed_ids"], ["u0"])
        self.assertEqual(report["reopened_completed_ids"], ["u0"])
        self.assert_baseline_unchanged()

    def test_report_cannot_replace_state_source_report_or_revision_inputs(self):
        source_report = self.work / "source-review-report.json"
        atomic_json(source_report, {"status": "pass"})
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))
        payload["paths"]["source_review_report"] = source_report.name
        atomic_json(self.manifest, payload)
        state = self.work / "run-state.json"
        atomic_json(state, {"source_sha256": self.hash, "stage": "translate"})
        for target in (source_report, state):
            before = target.read_bytes()
            argv = ["paper_repair.py", "--manifest", str(self.manifest),
                    "--source-review", str(self.proposal), "--report", str(target)]
            with self.subTest(target=target), patch("sys.argv", argv), \
                    patch("paper_repair.repair") as execute, patch("builtins.print"):
                self.assertEqual(paper_repair.main(), 1)
                execute.assert_not_called()
            self.assertEqual(target.read_bytes(), before)
        destination = Path(self.temp.name) / "new-revision"
        argv += ["--apply", "--output-dir", str(destination)]
        argv[argv.index("--report") + 1] = str(destination / "manifest.json")
        with patch("sys.argv", argv), patch("paper_repair.repair") as execute, patch("builtins.print"):
            self.assertEqual(paper_repair.main(), 1)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
