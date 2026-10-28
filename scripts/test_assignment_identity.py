"""Assignments survive unrelated changes and reject changes to their actual inputs."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from assignment_identity import build_assignment_identity
from translation_checkpoint import checkpoint


class AssignmentIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        (self.work / "source.pdf").write_bytes(b"immutable")
        self.manifest = self.work / "manifest.json"
        self.units = [{"id": f"u{i}", "translatable": True, "source_text": "An effect applies.",
                       "protected_text": "An effect [[Fx]] applies.", "protected_tokens": {},
                       "inline_fragments": {"[[Fx]]": {"kind": "math", "tex": "x"}},
                       "style_tokens": {}, "flow_role": "paragraph", "list_id": None,
                       "list_level": 0, "source_marker": ""} for i in range(2)]
        self.write_records("units.jsonl", self.units)
        self.write_records("template.jsonl", [{**u, "translated_text": ""} for u in self.units])
        self.payload = {"source_sha256": hashlib.sha256(b"immutable").hexdigest(),
                        "source_pdf": "source.pdf", "assignment_protocol_version": 1,
                        "paths": {"translation_units": "units.jsonl",
                                  "translations_template": "template.jsonl",
                                  "math_review": "math-review.json", "glossary": "glossary.json"}}
        self.write_json("manifest.json", self.payload)
        self.write_json("math-review.json", {"entries": {"[[Fx]]": {"tex": "x", "review_status": "manually_reviewed"}}})
        self.write_json("glossary.json", {"effect": "效应"})
        self.packet = {"assigned_ids": ["u0"],
                       "assignment_identity": build_assignment_identity(self.manifest, ["u0"])}
        self.write_json("packet.json", self.packet)
        self.write_records("batch.jsonl", [{"id": "u0", "translated_text": "效应[[Fx]]作用。"}])

    def write_json(self, name, value):
        (self.work / name).write_text(json.dumps(value), encoding="utf-8")

    def write_records(self, name, values):
        (self.work / name).write_text("".join(json.dumps(v) + "\n" for v in values), encoding="utf-8")

    def save(self, packet=True):
        return checkpoint(self.manifest, self.work / "translations.jsonl", self.work / "batch.jsonl",
                          packet_path=self.work / "packet.json" if packet else None)

    def test_unrelated_units_glossary_and_manifest_changes_are_safe(self):
        self.units[1]["source_text"] = "Other prose changed"
        self.write_records("units.jsonl", self.units)
        self.write_records("template.jsonl", [{**u, "translated_text": ""} for u in self.units])
        self.payload["unrelated"] = "diagnostics"
        self.write_json("manifest.json", self.payload)
        self.write_json("glossary.json", {"effect": "效应", "unrelated concept": "其他概念"})
        self.assertEqual(self.save()["completed"], 1)
        state = json.loads((self.work / "run-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["batch_progress"]["completed"], 1)

    def test_changed_source_math_term_or_flow_reject_without_checkpoint_write(self):
        cases = ("math", "term", "flow")
        for case in cases:
            with self.subTest(case=case):
                if case == "math":
                    self.write_json("math-review.json", {"entries": {"[[Fx]]": {"tex": "y"}}})
                elif case == "term":
                    self.write_json("math-review.json", {"entries": {"[[Fx]]": {"tex": "x", "review_status": "manually_reviewed"}}})
                    self.write_json("glossary.json", {"effect": "效果"})
                else:
                    self.write_json("glossary.json", {"effect": "效应"})
                    self.units[0]["flow_role"] = "list_item"
                    self.write_records("units.jsonl", self.units)
                    self.write_records("template.jsonl", [{**u, "translated_text": ""} for u in self.units])
                with self.assertRaisesRegex(ValueError, "Stale assignment"):
                    self.save()
                self.assertFalse((self.work / "translations.jsonl").exists())

    def test_required_packet_and_exact_assigned_ids(self):
        with self.assertRaisesRegex(ValueError, "requires --packet"):
            self.save(packet=False)
        self.write_records("batch.jsonl", [{"id": "u1", "translated_text": "效应[[Fx]]作用。"}])
        with self.assertRaisesRegex(ValueError, "Batch IDs/order"):
            self.save()


if __name__ == "__main__":
    unittest.main()
