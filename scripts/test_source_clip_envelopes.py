#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2", "pillow>=10.4.0,<13"]
# ///
"""Source-span evidence for clips next to wrapped prose."""

from __future__ import annotations

import unittest

import pymupdf

import check_translation as checker


class SourceClipEnvelopeTests(unittest.TestCase):
    def wrapped_prose(self, source):
        page = source.new_page(width=500, height=400)
        lines = [
            "The paragraph wraps beside the illustration.",
            "The next line remains beside it.",
            "The final line returns below the illustration and crosses the full page.",
        ]
        for position, text in zip(((220, 90), (220, 103), (40, 116)), lines):
            page.insert_text(position, text, fontsize=10)
        page.insert_text((45, 96), "Figure 1: source caption.", fontsize=8)
        block = next(block for block in page.get_text("dict")["blocks"]
                     if block.get("type") == 0 and block["lines"][0]["spans"][0]["text"] == lines[0])
        unit = {"id": "wrapped", "page": 1, "bbox": list(block["bbox"]),
                "kind": "body", "render_mode": "text", "translatable": True,
                "source_text": " ".join(lines)}
        placement = {"id": "figure", "ids": ["figure", "caption"],
                     "render_mode": "source_clip", "source_page": 1,
                     "source_bbox": [40, 75, 200, 104]}
        return unit, placement

    def test_wrapped_prose_envelope_does_not_mean_visible_text_was_clipped(self):
        with pymupdf.open() as source:
            unit, placement = self.wrapped_prose(source)
            self.assertGreater((pymupdf.Rect(unit["bbox"])
                                & pymupdf.Rect(placement["source_bbox"])).get_area(), 0.5)
            errors, _ = checker.validate_source_clip_envelopes(
                {"placements": [placement]}, [unit], source_document=source)
            self.assertEqual(errors, [])

    def test_real_source_span_intrusion_still_fails(self):
        with pymupdf.open() as source:
            unit, placement = self.wrapped_prose(source)
            placement["source_bbox"][2] = 250
            errors, metrics = checker.validate_source_clip_envelopes(
                {"placements": [placement]}, [unit], source_document=source)
            self.assertEqual(len(errors), 1)
            self.assertIn("wrapped", errors[0])
            self.assertEqual(metrics["contaminated_clips"], 1)

    def test_unavailable_or_incomplete_source_evidence_keeps_conservative_envelope(self):
        with pymupdf.open() as source:
            unit, placement = self.wrapped_prose(source)
            layout = {"placements": [placement]}
            self.assertEqual(len(checker.validate_source_clip_envelopes(layout, [unit])[0]), 1)
            unit["source_text"] += " A reviewed continuation absent from this source block."
            self.assertEqual(len(checker.validate_source_clip_envelopes(
                layout, [unit], source_document=source)[0]), 1)


if __name__ == "__main__":
    unittest.main()
