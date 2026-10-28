"""Compact assignments preserve semantics, boundaries, and canonical state."""
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from translation_context import build_packet, main
from test_paper_preflight import add_readiness_inputs, write_ready_report


class TranslationContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        (self.work / "source.pdf").write_bytes(b"source fixture")
        self.source_hash = hashlib.sha256(b"source fixture").hexdigest()
        self.units = [{
            "id": f"u{i}", "page": 1, "order": i * 2,
            "kind": "body", "translatable": True,
            "flow_role": "list_item", "list_id": "list1", "list_level": 0, "source_marker": "•",
            "source_text": "Let x be one.",
            "protected_text": "Let [[Fmath]] be [[P0001]].",
            "protected_tokens": {"[[P0001]]": "one"},
            "inline_fragments": {"[[Fmath]]": {"kind": "math", "text": "x",
                "tex": "wrong", "runs": [{"bbox": [0, 0, 20, 20]}],
                "html": "expensive duplicate", "review_status": "unresolved"}},
            "style_tokens": {}, "span_runs": [{"text": "verbose"}],
        } for i in range(3)]
        self.dest = self.work / "translations.jsonl"
        self.write("units.jsonl", [self.units[0], {
            "id": "eq1", "page": 1, "order": 1, "kind": "equation",
            "translatable": False, "source_text": "x=1",
        }, *self.units[1:]])
        self.write("template.jsonl", [dict(u, translated_text="") for u in self.units])
        (self.work / "math-review.json").write_text(json.dumps({
            "source_sha256": self.source_hash,
            "entries": {"[[Fmath]]": {"tex": "x", "review_status": "manually_reviewed"},
                        "eq1": {"tex": "x=1", "review_status": "manually_reviewed"}},
        }), encoding="utf-8")
        self.manifest = self.work / "manifest.json"
        self.manifest.write_text(json.dumps({
            "source_pdf": "source.pdf", "source_sha256": self.source_hash,
            "paths": {"translation_units": "units.jsonl",
                      "translations_template": "template.jsonl",
                      "math_review": "math-review.json"},
        }), encoding="utf-8")
        add_readiness_inputs(self.manifest)

    def write(self, name, records):
        (self.work / name).write_text("".join(
            json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def test_reviewed_math_and_interleaved_equations_survive_compaction(self):
        before = {p: p.read_bytes() for p in self.work.iterdir()}
        packet = build_packet(self.manifest, self.dest, end_id="u1")
        self.assertEqual(packet["assigned_ids"], ["u0", "u1"])
        self.assertEqual(packet["units"][0]["protected_text"], self.units[0]["protected_text"])
        self.assertEqual(packet["units"][0]["inline_fragments"]["[[Fmath]]"]["tex"], "x")
        self.assertEqual(packet["invariants_context_only"][0]["tex"], "x=1")
        self.assertEqual(packet["context_only"][0]["id"], "u2")
        self.assertNotIn("runs", packet["units"][0]["inline_fragments"]["[[Fmath]]"])
        self.assertNotIn("span_runs", packet["units"][0])
        self.assertEqual(packet["units"][0]["source_marker"], "•")
        self.assertEqual(packet["units"][0]["flow_role"], "list_item")
        self.assertEqual(before, {p: p.read_bytes() for p in self.work.iterdir()})

    def test_budget_is_contiguous_and_single_large_unit_is_not_cut(self):
        packet = build_packet(self.manifest, self.dest, max_words=5)
        self.assertEqual(packet["assigned_ids"], ["u0"])
        self.assertEqual(packet["next_id"], "u1")
        packet = build_packet(self.manifest, self.dest, start_id="u1", max_words=1)
        self.assertTrue(packet["oversized_single_unit"])
        self.assertEqual(packet["assigned_ids"], ["u1"])
        self.assertEqual([r["id"] for r in packet["context_only"]], ["u0", "u2"])

    def test_resume_skips_completed_prefix_without_reusing_old_prose(self):
        self.write("translations.jsonl", [dict(u, translated_text=(
            "设 [[Fmath]] 为 [[P0001]]。" if i == 0 else ""))
            for i, u in enumerate(self.units)])
        packet = build_packet(self.manifest, self.dest)
        self.assertEqual(packet["assigned_ids"], ["u1", "u2"])
        self.assertNotIn("translated_text", packet["context_only"][0])
        with self.assertRaisesRegex(ValueError, "completed"):
            build_packet(self.manifest, self.dest, start_id="u0")
        self.assertEqual(build_packet(self.manifest, self.dest, start_id="u0",
                         include_completed=True)["assigned_ids"], ["u0", "u1", "u2"])

    def test_source_or_math_identity_conflicts_fail(self):
        review = self.work / "math-review.json"
        d = json.loads(review.read_text()); d["source_sha256"] = "stale"
        review.write_text(json.dumps(d), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "preflight is stale"):
            build_packet(self.manifest, self.dest)
        (self.work / "source.pdf").write_bytes(b"changed source")
        with self.assertRaisesRegex(ValueError, "hash differs"):
            build_packet(self.manifest, self.dest)

    def test_unknown_and_reversed_ranges_fail(self):
        for arguments in ({"start_id": "missing"}, {"end_id": "missing"},
                          {"start_id": "u2", "end_id": "u0"}, {"max_words": 0}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                build_packet(self.manifest, self.dest, **arguments)

    def test_assignment_requires_passing_current_preflight(self):
        report = self.work / "paper-preflight.json"
        report.unlink()
        with self.assertRaisesRegex(ValueError, "preflight report is missing"):
            build_packet(self.manifest, self.dest)
        write_ready_report(self.manifest)
        packet = build_packet(self.manifest, self.dest)
        self.assertIn("assignment_identity", packet)
        self.assertEqual(len(packet["source_preflight_sha256"]), 64)
        payload = json.loads(report.read_text(encoding="utf-8"))
        payload["status"] = "fail"
        report.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "did not pass"):
            build_packet(self.manifest, self.dest)

    def test_cli_refuses_to_overwrite_an_existing_assignment(self):
        packet = self.work / "assigned.json"
        packet.write_bytes(b"existing frozen assignment")
        argv = ["translation_context.py", "--manifest", str(self.manifest),
                "--translations", str(self.dest), "--output", str(packet)]
        with patch("sys.argv", argv), patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(main(), 1)
        self.assertEqual(packet.read_bytes(), b"existing frozen assignment")
        self.assertFalse(self.dest.exists())


if __name__ == "__main__":
    unittest.main()
