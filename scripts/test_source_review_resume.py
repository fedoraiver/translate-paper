#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2", "pillow>=10.4.0,<13"]
# ///
"""Regression tests for resuming reviewed papers with completed translations."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import prepare_paper
import source_review


class SourceReviewResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.translations = self.root / "translations.jsonl"
        self.pages = [{"page": 1, "width": 600, "height": 800}]
        self.raw = [
            {
                "id": "body",
                "order": 1,
                "page": 1,
                "page_order": 1,
                "bbox": [54, 100, 297, 150],
                "kind": "body",
                "render_mode": "text",
                "translatable": True,
                "source_text": "Raw extracton has 12 tokens [3].",
                "span_runs": [],
                "section": "1 Introduction",
            }
        ]
        prepare_paper.protect_element(self.raw[0])
        self.review = {
            "schema_version": 1,
            "source_sha256": "source",
            "reviewed": True,
            "reason": "Verified the PDF source.",
            "operations": [
                {
                    "op": "replace_unit",
                    "id": "body",
                    "source_text": "Reviewed extraction has 12 tokens [3].",
                    "reason": "Restore the exact visible source prose.",
                }
            ],
        }
        self.previous = copy.deepcopy(self.raw)
        source_review.apply_source_review(
            self.previous,
            self.review,
            "source",
            self.pages,
            prepare_paper.protect_element,
        )
        self.write_jsonl(self.root / "translation-units.jsonl", self.previous)
        self.write_manifest("source")
        unit = self.previous[0]
        record = {
            key: unit[key]
            for key in (
                "id",
                "section",
                "source_text",
                "protected_text",
                "protected_tokens",
                "inline_fragments",
                "style_tokens",
            )
        }
        record["translated_text"] = (
            "Completed checkpoint text [[P0001]] [[Fbody_0001]]"
        )
        self.write_jsonl(self.translations, [record])

    @staticmethod
    def write_jsonl(path: Path, records: list[dict]) -> None:
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    def write_manifest(self, source_hash: str) -> None:
        (self.root / "manifest.json").write_text(
            json.dumps(
                {
                    "source_sha256": source_hash,
                    "paths": {"translation_units": "translation-units.jsonl"},
                }
            ),
            encoding="utf-8",
        )

    def replay(
        self, elements: list[dict], review: dict | None = None, protect=None
    ) -> dict:
        return source_review.apply_source_review(
            elements,
            review or self.review,
            "source",
            self.pages,
            protect or prepare_paper.protect_element,
            translations_path=self.translations,
        )

    def test_replay_of_same_review_preserves_completed_record(self) -> None:
        fresh = copy.deepcopy(self.raw)
        before = source_review._read_jsonl(self.translations)
        report = self.replay(fresh)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(fresh, self.previous)
        source_review.reconcile_translations(self.translations, fresh)
        self.assertEqual(source_review._read_jsonl(self.translations), before)

    def test_real_source_change_rejects_and_leaves_inputs_untouched(
        self,
    ) -> None:
        review = copy.deepcopy(self.review)
        review["operations"][0]["source_text"] = (
            "A different reviewed source sentence."
        )
        fresh = copy.deepcopy(self.raw)
        checkpoint = self.translations.read_bytes()
        with self.assertRaisesRegex(
            source_review.SourceReviewError, "completed translation unit body"
        ):
            self.replay(fresh, review)
        self.assertEqual(fresh, self.raw)
        self.assertEqual(self.translations.read_bytes(), checkpoint)

    def test_protected_token_and_style_changes_reject(self) -> None:
        changes = {
            "protected_text": "Changed token structure",
            "protected_tokens": {"[[P0001]]": "99"},
            "style_tokens": {"Bbody_0001": "bold"},
            "block_style": "italic",
            "inline_fragments": {
                "[[Fbody_0001]]": {"kind": "citation", "text": "[99]"}
            },
        }
        for field, value in changes.items():
            with self.subTest(field=field):

                def changed_protection(element, key=field, replacement=value):
                    prepare_paper.protect_element(element)
                    element[key] = replacement

                with self.assertRaises(source_review.SourceReviewError):
                    self.replay(
                        copy.deepcopy(self.raw), protect=changed_protection
                    )

    def test_suppression_of_completed_content_still_rejects(self) -> None:
        review = copy.deepcopy(self.review)
        review["operations"].append(
            {
                "op": "suppress_units",
                "ids": ["body"],
                "reason": "Proposed suppression.",
            }
        )
        with self.assertRaisesRegex(
            source_review.SourceReviewError, "completed translation unit body"
        ):
            self.replay(copy.deepcopy(self.raw), review)

    def test_grouping_completed_content_still_rejects(self) -> None:
        fresh = copy.deepcopy(self.raw)
        fresh.append(
            {
                **copy.deepcopy(fresh[0]),
                "id": "caption",
                "order": 2,
                "bbox": [54, 160, 297, 175],
                "translatable": False,
            }
        )
        review = copy.deepcopy(self.review)
        review["operations"].append(
            {
                "op": "group_visual",
                "visual_id": "Figure 1",
                "anchor_id": "body",
                "member_ids": ["body"],
                "caption_id": "caption",
                "reason": "Proposed grouping.",
            }
        )
        with self.assertRaisesRegex(
            source_review.SourceReviewError, "completed translation unit body"
        ):
            self.replay(fresh, review)

    def test_check_uses_final_state_after_all_operations(self) -> None:
        # Reclassification may temporarily suppress a caption before a later
        # reviewed operation restores it; only an identical final unit is safe.
        fresh = copy.deepcopy(self.raw)
        fresh.append(
            {
                **copy.deepcopy(fresh[0]),
                "id": "figure",
                "order": 2,
                "bbox": [54, 30, 297, 80],
                "translatable": False,
            }
        )
        review = copy.deepcopy(self.review)
        review["operations"].insert(
            0,
            {
                "op": "group_visual",
                "visual_id": "Figure 1",
                "anchor_id": "figure",
                "member_ids": ["figure"],
                "caption_id": "body",
                "reason": "Bind the original visual to its caption.",
            },
        )
        review["operations"][1].update({"kind": "body", "translatable": True})
        self.replay(fresh, review)
        self.assertEqual(
            fresh[0]["protected_text"], self.previous[0]["protected_text"]
        )
        self.assertTrue(fresh[0]["translatable"])

    def test_missing_or_wrong_source_baseline_cannot_authorize_replay(
        self,
    ) -> None:
        self.write_manifest("other-source")
        with self.assertRaisesRegex(
            source_review.SourceReviewError, "different source PDF"
        ):
            self.replay(copy.deepcopy(self.raw))
        self.write_manifest("source")
        self.write_jsonl(self.root / "translation-units.jsonl", [])
        with self.assertRaises(source_review.SourceReviewError):
            self.replay(copy.deepcopy(self.raw))

    def test_without_manifest_conservative_replacement_guard_remains(
        self,
    ) -> None:
        self.root.joinpath("manifest.json").unlink()
        with self.assertRaisesRegex(
            source_review.SourceReviewError, "Refusing to replace completed"
        ):
            self.replay(copy.deepcopy(self.raw))

    def test_changed_unreviewed_completed_unit_is_also_rejected(self) -> None:
        fresh = copy.deepcopy(self.previous)
        fresh[0]["source_text"] = (
            "Changed by the extractor without a review operation."
        )
        review = {**self.review, "operations": []}
        with self.assertRaises(source_review.SourceReviewError):
            self.replay(fresh, review)

    def test_reconciliation_refuses_completed_source_metadata_changes(
        self,
    ) -> None:
        changed = copy.deepcopy(self.previous)
        changed[0]["protected_tokens"]["[[P0001]]"] = "99"
        checkpoint = self.translations.read_bytes()
        with self.assertRaisesRegex(
            source_review.SourceReviewError, "protected_tokens"
        ):
            source_review.reconcile_translations(self.translations, changed)
        self.assertEqual(self.translations.read_bytes(), checkpoint)


if __name__ == "__main__":
    unittest.main()
