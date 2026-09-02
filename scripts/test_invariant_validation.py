#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2", "pillow>=10.4.0,<13"]
# ///
"""Regressions for original text and reviewed source-clip validation."""

from __future__ import annotations

import unittest

import check_translation as checker
import pymupdf


class InvariantValidationTests(unittest.TestCase):
    def unit(self, unit_id: str, bbox: list[float], **fields) -> dict:
        return {
            "id": unit_id,
            "page": 1,
            "bbox": bbox,
            "kind": "body",
            "render_mode": "text",
            "translatable": False,
            "source_text": (
                "This original paragraph must remain complete and must be "
                "found in the output rather than inferred from declared coverage."
            ),
            **fields,
        }

    def test_suppressed_fragments_are_not_independent_english_invariants(self) -> None:
        suppressed = self.unit("merged", [10, 10, 20, 20], render_suppressed=True)
        active = self.unit("active", [10, 30, 20, 40])
        self.assertEqual(checker.missing_invariant_ids([suppressed, active], "", {}), ["active"])

    def test_original_columns_are_checked_without_interleaving(self) -> None:
        left_lines = [
            "Acknowledgments begin.",
            "Funding identifiers follow.",
            "Project names remain.",
            "Every grant is retained.",
            "Contributors are credited.",
            "Affiliations are unchanged.",
            "Research support follows.",
            "Attribution ends here.",
        ]
        right_lines = [f"Reference number {index} is separate." for index in range(8)]
        with pymupdf.open() as document:
            page = document.new_page(width=800, height=400)
            page.insert_text((20, 100), "\n".join(left_lines), fontsize=9)
            page.insert_text((420, 100), "\n".join(right_lines), fontsize=9)
            layout = {"boundary_layout": {"segments": [
                {"role": "tail-original", "output_page": 1, "target_bbox": [0, 0, 400, 400]},
                {"role": "tail-original", "output_page": 1, "target_bbox": [400, 0, 800, 400]},
            ]}}
            source_text = "\n".join(left_lines)
            global_text = page.get_text("text", sort=True)
            self.assertFalse(checker.invariant_present(source_text, checker.normalize_text(global_text)))
            corpus = checker.original_boundary_output_text(document, layout)
            self.assertTrue(checker.invariant_present(source_text, checker.normalize_text(corpus)))

    def test_declared_original_clip_does_not_hide_missing_output_text(self) -> None:
        unit = self.unit("missing-original", [20, 20, 200, 100])
        with pymupdf.open() as document:
            document.new_page(width=400, height=400)
            layout = {"boundary_layout": {"segments": [{
                "role": "tail-original", "mode": "partial-page",
                "source_page": 1, "output_page": 1,
                "target_bbox": [0, 0, 400, 400],
            }]}}
            corpus = checker.original_boundary_output_text(document, layout)
            self.assertEqual(checker.missing_invariant_ids([unit], checker.normalize_text(corpus), layout), ["missing-original"])

    def test_union_of_distant_algorithms_rejects_swallowed_prose(self) -> None:
        units = [
            self.unit("left-bottom", [10, 250, 190, 350], render_mode="source_clip"),
            self.unit("right-top", [210, 10, 390, 120], render_mode="source_clip"),
            self.unit("narrative", [10, 130, 190, 220], translatable=True),
        ]
        layout = {"placements": [{
            "id": "left-bottom", "ids": ["left-bottom", "right-top"],
            "render_mode": "source_clip", "source_page": 1,
            "source_bbox": [10, 10, 390, 350],
        }]}
        errors, metrics = checker.validate_source_clip_envelopes(layout, units)
        self.assertEqual(len(errors), 1)
        self.assertIn("narrative", errors[0])
        self.assertEqual(metrics["contaminated_clips"], 1)

    def test_separate_algorithms_allow_their_suppressed_pseudocode(self) -> None:
        units = [
            self.unit("algorithm", [10, 10, 190, 120], render_mode="source_clip"),
            self.unit("pseudocode", [20, 25, 180, 110], render_suppressed=True),
            self.unit("narrative", [10, 140, 190, 220], translatable=True),
        ]
        layout = {"placements": [{
            "id": "algorithm", "ids": ["algorithm"],
            "render_mode": "source_clip", "source_page": 1,
            "source_bbox": [10, 10, 190, 120],
        }]}
        errors, metrics = checker.validate_source_clip_envelopes(layout, units)
        self.assertEqual(errors, [])
        self.assertEqual(metrics["checked"], 1)


if __name__ == "__main__":
    unittest.main()
