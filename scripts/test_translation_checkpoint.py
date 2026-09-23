"""Checkpoint regressions: atomic failure, source identity, and lossless resume."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from translation_checkpoint import atomic_write, checkpoint


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work / "source.pdf"
        self.source.write_bytes(b"immutable source fixture")
        self.manifest = self.work / "manifest.json"
        self.dest = self.work / "translations.jsonl"
        self.batch = self.work / "batch.jsonl"
        self.units = [{
            "id": f"u{i}", "translatable": True, "section": "Introduction",
            "source_text": "Let x be 1.",
            "protected_text": "[[B0001_OPEN]]Let[[B0001_CLOSE]] [[Fmath]] be [[P0001]].",
            "protected_tokens": {"[[P0001]]": "1"},
            "inline_fragments": {"[[Fmath]]": {"kind": "math", "tex": "x"}},
            "style_tokens": {"B0001": "bold"},
        } for i in range(2)]
        self.write_records("units.jsonl", self.units)
        self.write_records("template.jsonl", [
            {k: v for k, v in dict(u, translated_text="").items() if k != "translatable"}
            for u in self.units
        ])
        self.manifest.write_text(json.dumps({
            "source_pdf": "source.pdf",
            "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "paths": {"translation_units": "units.jsonl",
                      "translations_template": "template.jsonl"},
        }), encoding="utf-8")
        self.text = "[[B0001_OPEN]]设[[B0001_CLOSE]][[Fmath]]为[[P0001]]。"

    def write_records(self, name, records):
        (self.work / name).write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
            encoding="utf-8")

    def save(self, entries, **kwargs):
        self.write_records("batch.jsonl", entries)
        return checkpoint(self.manifest, self.dest, self.batch, **kwargs)

    def entry(self, unit_id="u0", text=None):
        return {"id": unit_id, "translated_text": self.text if text is None else text}

    def test_resume_and_idempotent_replay_preserve_metadata(self):
        report = self.save([self.entry()])
        self.assertEqual((report["completed"], report["next_id"]), (1, "u1"))
        before = self.dest.read_bytes()
        self.save([self.entry()])
        self.assertEqual(self.dest.read_bytes(), before)
        self.save([self.entry("u1")], require_complete=True)
        records = [json.loads(s) for s in self.dest.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(records[0]["inline_fragments"], self.units[0]["inline_fragments"])
        self.assertTrue(checkpoint(self.manifest, self.dest, require_complete=True)["complete"])

    def test_bad_batch_never_partially_writes(self):
        self.save([self.entry()])
        before = self.dest.read_bytes()
        for entries in (
            [self.entry("u1"), self.entry("unknown")],
            [self.entry("u1"), self.entry("u1")],
            [self.entry("u1", self.text.replace("[[P0001]]", ""))],
            [self.entry("u1", self.text + "[[P0001]]")],
            [self.entry("u1", self.text.replace("[[Fmath]]", "[[Fwrong]]"))],
            [self.entry("u1", self.text.replace("[[Fmath]]为[[P0001]]", "[[P0001]]为[[Fmath]]"))],
            [self.entry("u1", "")],
            [self.entry("u1", self.units[0]["protected_text"])],
            [{**self.entry("u1"), "source_text": "changed"}],
        ):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                self.save(entries)
            self.assertEqual(self.dest.read_bytes(), before)

    def test_revision_requires_explicit_option(self):
        self.save([self.entry()])
        with self.assertRaisesRegex(ValueError, "completed text differs"):
            self.save([self.entry(text=self.text + "成立。")])
        self.save([self.entry(text=self.text + "成立。")], replace_completed=True)

    def test_changed_pdf_or_source_metadata_blocks_resume(self):
        self.save([self.entry()])
        self.source.write_bytes(b"different source")
        with self.assertRaisesRegex(ValueError, "hash differs"):
            checkpoint(self.manifest, self.dest)
        self.source.write_bytes(b"immutable source fixture")
        self.units[0]["source_text"] = "Changed prose"
        self.write_records("units.jsonl", self.units)
        with self.assertRaisesRegex(ValueError, "stale source_text"):
            checkpoint(self.manifest, self.dest)

    def test_intentional_revision_can_repair_invalid_existing_tokens(self):
        self.save([self.entry()])
        records = [json.loads(s) for s in self.dest.read_text(encoding="utf-8").splitlines()]
        records[0]["translated_text"] = "损坏的旧译文。"
        self.write_records("translations.jsonl", records)
        with self.assertRaisesRegex(ValueError, "token sequence"):
            checkpoint(self.manifest, self.dest)
        self.save([self.entry()], replace_completed=True)
        self.assertEqual(checkpoint(self.manifest, self.dest)["completed"], 1)

    def test_duplicate_checkpoint_ids_and_orphans_are_rejected(self):
        self.save([self.entry()])
        records = [json.loads(s) for s in self.dest.read_text(encoding="utf-8").splitlines()]
        self.write_records("translations.jsonl", records + [records[0]])
        with self.assertRaisesRegex(ValueError, "duplicate id"):
            checkpoint(self.manifest, self.dest)
        records[1]["id"] = "orphan"
        self.write_records("translations.jsonl", records)
        with self.assertRaisesRegex(ValueError, "inventory differs"):
            checkpoint(self.manifest, self.dest)

    def test_read_only_check_does_not_initialize_or_claim_complete(self):
        report = checkpoint(self.manifest, self.dest)
        self.assertFalse(report["complete"])
        self.assertFalse(self.dest.exists())
        with self.assertRaisesRegex(ValueError, "untranslated"):
            self.save([self.entry()], require_complete=True)
        self.assertFalse(self.dest.exists())

    def test_failed_replace_retains_original_and_cleans_temporary_file(self):
        self.save([self.entry()])
        before = self.dest.read_bytes()
        files = set(self.work.iterdir())
        with patch("translation_checkpoint.os.replace", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                atomic_write(self.dest, [])
        self.assertEqual(self.dest.read_bytes(), before)
        self.assertEqual(set(self.work.iterdir()), files)


if __name__ == "__main__":
    unittest.main()
