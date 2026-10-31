#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2", "pillow>=10.4.0,<13"]
# ///
"""Invariant exemptions require actual image envelopes or owned native glyphs."""
import copy
import unittest
from pathlib import Path

import pymupdf

import check_translation as check


class CaptionInvariantTests(unittest.TestCase):
    box = pymupdf.Rect(30, 40, 230, 145)
    caption = {"id": "caption", "order": 2, "page": 1, "kind": "caption",
               "render_mode": "text", "translatable": False,
               "bbox": [30, 110, 230, 145], "visual_id": "Figure 1",
               "source_text": "Figure 1. The complete original caption remains visible with its original figure and all internal labels."}
    figure = {"id": "figure", "order": 1, "page": 1, "kind": "figure",
              "render_mode": "source_clip", "translatable": False,
              "bbox": [30, 40, 230, 100], "visual_id": "Figure 1"}

    def source(self, changed=False, native=False, blank_caption=False):
        document = pymupdf.open()
        page = document.new_page(width=320, height=240)
        with pymupdf.open() as drawing:
            original = drawing.new_page(width=320, height=240)
            original.draw_rect(pymupdf.Rect(self.figure["bbox"]), color=(0, 0, 0))
            original.insert_text((40, 70), "FIGURE CONTENT", fontsize=10)
            if not blank_caption:
                original.insert_text((32, 121), "Figure 1. " + ("CHANGED" if changed else "ORIGINAL") + " CAPTION", fontsize=8)
                original.insert_text((32, 135), "The second caption line remains.", fontsize=8)
            if native:
                page.show_pdf_page(page.rect, drawing, 0)
            else:
                page.insert_image(page.rect, pixmap=original.get_pixmap(matrix=pymupdf.Matrix(3, 3)))
        return document

    def output(self, source, actual_clip=None):
        target = pymupdf.Rect(80, 90, 320, 216)
        document = pymupdf.open()
        page = document.new_page(width=420, height=360)
        page.show_pdf_page(target, source, 0, clip=actual_clip or self.box, keep_proportion=False)
        layout = {"placements": [{"id": "figure", "ids": ["figure", "caption"],
                                  "kind": "figure", "render_mode": "source_clip",
                                  "visual_id": "Figure 1", "caption_id": "caption",
                                  "source_page": 1, "source_bbox": list(self.box),
                                  "output_page": 1, "bbox": list(target)}]}
        return document, layout

    def validate(self, source, output, layout, units=None):
        evidence = []
        errors = check.validate_image_caption_preservation(
            source, output, units or [self.figure, self.caption], layout, evidence)
        return errors, evidence

    def test_actual_combined_image_caption_exempts_only_its_verified_id(self):
        with self.source() as source:
            output, layout = self.output(source)
            with output:
                errors, evidence = self.validate(source, output, layout)
                self.assertEqual(errors, [], evidence)
                self.assertEqual(evidence[0]["status"], "pass")
                self.assertEqual(evidence[0]["source_ids"], ["figure", "caption"])
                ordinary = {**self.caption, "id": "ordinary", "kind": "body"}
                units = [self.caption, ordinary]
                self.assertEqual(check.missing_invariant_ids(units, "", layout), ["caption", "ordinary"])
                self.assertEqual(check.missing_invariant_ids(
                    units, "", layout, validated_image_caption_ids={"caption", "ordinary"}), ["ordinary"])

    def test_altered_caption_or_truncated_output_is_not_exempted(self):
        with self.source() as source, self.source(changed=True) as changed:
            output, layout = self.output(changed)
            with output:
                self.assertTrue(self.validate(source, output, layout)[0])
            for clip in [self.box + (0, .05, 0, .05), pymupdf.Rect(30, 40, 230, 128)]:
                with self.subTest(clip=clip):
                    output, layout = self.output(source, clip)
                    with output:
                        self.assertTrue(self.validate(source, output, layout)[0])

    def test_wrong_page_caption_ownership_and_source_envelope_fail(self):
        with self.source() as source:
            output, layout = self.output(source)
            with output:
                output.new_page(width=420, height=360)
                for field, value in [("output_page", 2), ("source_page", 2),
                                     ("source_bbox", [30, 40, 230, 128]),
                                     ("caption_id", "other"),
                                     ("ids", ["figure", "caption", "caption"])]:
                    with self.subTest(field=field):
                        broken = copy.deepcopy(layout)
                        broken["placements"][0][field] = value
                        self.assertTrue(self.validate(source, output, broken)[0])
                duplicated = copy.deepcopy(layout)
                duplicated["placements"] *= 2
                self.assertTrue(self.validate(source, output, duplicated)[0])
                neighbor = {"id": "neighbor", "kind": "body", "page": 1,
                            "translatable": True, "bbox": [50, 103, 160, 109]}
                self.assertTrue(self.validate(source, output, layout, [self.figure, self.caption, neighbor])[0])

    def test_visible_text_or_vector_overlay_fails_image_identity(self):
        for vector in (False, True):
            with self.subTest(vector=vector), self.source() as source:
                output, layout = self.output(source)
                with output:
                    if vector:
                        output[0].draw_rect(pymupdf.Rect(80, 170, 320, 216), fill=(1, 1, 1), color=None)
                    else:
                        output[0].insert_text((100, 190), "WRONG CAPTION", fontsize=10)
                    self.assertTrue(self.validate(source, output, layout)[0])

    def test_native_source_caption_keeps_text_invariant(self):
        with self.source(native=True) as source:
            output, layout = self.output(source)
            with output:
                errors, evidence = self.validate(source, output, layout)
                self.assertEqual(errors, [])
                self.assertEqual(evidence[0]["status"], "text-invariant-required")
                self.assertEqual(check.missing_invariant_ids([self.caption], "", layout), ["caption"])

    def test_declared_caption_on_blank_source_region_is_rejected(self):
        with self.source(blank_caption=True) as source:
            output, layout = self.output(source)
            with output:
                errors, evidence = self.validate(source, output, layout)
                self.assertTrue(any("no visible content" in error for error in errors))
                self.assertEqual(evidence[0]["status"], "fail")


class NativeMathInvariantTests(unittest.TestCase):
    def fixture(self, glyph=True):
        document = pymupdf.open()
        page = document.new_page(width=420, height=360)
        if glyph:
            font = Path(r'C:\texlive\2026\texmf-dist\fonts\opentype\public\lm-math\latinmodern-math.otf')
            if not font.exists():
                document.close()
                self.skipTest("Latin Modern Math unavailable")
            page.insert_text((60, 100), "a = a", fontname="NativeMath", fontfile=str(font))
        unit = {"id": "equation", "order": 1, "page": 1, "bbox": [20, 40, 220, 100],
                "kind": "equation", "render_mode": "text", "translatable": False,
                "source_text": "Displayed intensity = (a′)ᵞ = (a¹ᐟᵞ)ᵞ(Maximum intensity) = a(Maximum intensity)."}
        layout = {"render_backend": "native-lualatex-v1", "placements": [{
            "id": "equation", "ids": ["equation"], "render_mode": "native-math",
            "math_review_id": "equation", "text_embedding": "native-lualatex",
            "output_page": 1, "bbox": [20, 40, 300, 150]}],
            "rich_text": {"math_render_strategies": {"native-display-font": 1},
                          "math_review": {"display_review_ids": ["equation"],
                                          "display_source_ids": ["equation"],
                                          "unresolved": 0, "image_fallbacks": 0}}}
        review = {"source_sha256": "hash", "entries": {"equation": {
            "kind": "display", "source_ids": ["equation"], "tex": "a=a",
            "review_status": "manually_reviewed"}}}
        return document, unit, layout, review

    def validate(self, document, unit, layout, review):
        evidence = []
        errors = check.validate_native_display_invariants(
            document, {"source_sha256": "hash"}, [unit], layout, review, evidence)
        return errors, evidence

    def test_owned_native_math_uses_embedded_glyph_evidence_not_source_unicode_string(self):
        document, unit, layout, review = self.fixture()
        with document:
            errors, evidence = self.validate(document, unit, layout, review)
            self.assertEqual(errors, [], evidence)
            self.assertEqual(evidence[0]["status"], "pass")
            self.assertEqual(check.missing_invariant_ids([unit], "", layout), ["equation"])
            self.assertEqual(check.missing_invariant_ids(
                [unit], "", layout, validated_native_display_ids=set(evidence[0]["source_ids"])), [])

    def test_declared_native_math_without_glyphs_is_not_accepted(self):
        document, unit, layout, review = self.fixture(glyph=False)
        with document:
            errors, evidence = self.validate(document, unit, layout, review)
            self.assertTrue(errors)
            self.assertEqual(evidence[0]["status"], "fail")

    def test_stale_missing_duplicate_or_fallback_review_never_exempts_raw_text(self):
        document, unit, layout, review = self.fixture()
        with document:
            for defect in ("hash", "missing", "duplicate", "unresolved", "fallback", "review-status"):
                broken_layout, broken_review = copy.deepcopy(layout), copy.deepcopy(review)
                if defect == "hash": broken_review["source_sha256"] = "stale"
                elif defect == "missing": broken_layout["placements"] = []
                elif defect == "duplicate": broken_layout["placements"] *= 2
                elif defect == "unresolved": broken_layout["rich_text"]["math_review"]["unresolved"] = 1
                elif defect == "fallback": broken_layout["rich_text"]["math_render_strategies"]["source-vector"] = 1
                else: broken_review["entries"]["equation"]["review_status"] = "unresolved"
                with self.subTest(defect=defect):
                    errors, evidence = self.validate(document, unit, broken_layout, broken_review)
                    self.assertTrue(errors)
                    self.assertTrue(all(item["status"] == "fail" for item in evidence))


if __name__ == "__main__":
    unittest.main()
