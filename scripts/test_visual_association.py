import unittest
import copy
import json
import tempfile
from pathlib import Path

import prepare_paper
import pymupdf
import source_review
import render_translation
import native_latex
import check_translation
from typography import ZH_ACADEMIC_V1


class VisualAssociationTests(unittest.TestCase):
    def test_chapter_hyphenated_figure_numbers_are_distinct(self):
        self.assertEqual(prepare_paper.visual_label("Figure 5-1: First"), ("figure", "figure-5-1"))
        self.assertEqual(prepare_paper.visual_label("Figure 5-3: Third"), ("figure", "figure-5-3"))

    @staticmethod
    def element(identifier, kind, bbox, text="", **extra):
        return {
            "id": identifier,
            "page": 1,
            "kind": kind,
            "bbox": bbox,
            "source_text": text,
            "font_size": 10.0,
            **extra,
        }

    def test_reviewed_pairing_wins_over_vertical_distance(self):
        with pymupdf.open() as document:
            document.new_page(width=600, height=800)
            units = [
                self.element("body", "body", [50, 30, 550, 50], "Body"),
                self.element(
                    "left",
                    "figure",
                    [50, 100, 280, 180],
                    render_mode="source_clip",
                    review_visual_id="figure-1",
                ),
                self.element(
                    "right",
                    "figure",
                    [320, 100, 550, 181],
                    render_mode="source_clip",
                    review_visual_id="figure-2",
                ),
                self.element(
                    "cap-left",
                    "caption",
                    [50, 190, 280, 210],
                    "Figure 1. Left",
                    visual_id="figure-1",
                    source_review_reason="Reviewed pairing",
                ),
                self.element(
                    "cap-right",
                    "caption",
                    [320, 190, 550, 210],
                    "Figure 2. Right",
                    visual_id="figure-2",
                    source_review_reason="Reviewed pairing",
                ),
            ]
            result = prepare_paper.build_visual_layout(
                document, units, [{"page": 1, "width": 600, "height": 800}], "hash"
            )
            self.assertEqual(result["status"], "pass")
            pairs = {v["visual_id"]: v["caption_id"] for v in result["visuals"]}
            self.assertEqual(pairs, {"figure-1": "cap-left", "figure-2": "cap-right"})

    def test_width_clamp_updates_scale_and_font_calibration(self):
        with pymupdf.open() as document:
            page = document.new_page(width=600, height=800)
            page.insert_text((80, 140), "table cells", fontsize=7)
            units = [
                self.element("body", "body", [50, 30, 550, 50], "Body"),
                self.element(
                    "table", "table", [40, 100, 560, 160], render_mode="source_clip"
                ),
                self.element(
                    "caption", "caption", [40, 170, 560, 190], "Table 1. Wide table"
                ),
            ]
            result = prepare_paper.build_visual_layout(
                document, units, [{"page": 1, "width": 600, "height": 800}], "hash"
            )
            self.assertEqual(result["status"], "pass")
            visual = result["visuals"][0]
            scale = visual["target_width_pt"] / 520
            self.assertLess(scale, 1)
            self.assertAlmostEqual(visual["target_scale"], scale, places=5)
            self.assertAlmostEqual(
                visual["target_internal_font_pt"], 7 * scale, places=3
            )

    def test_reviewed_captionless_objects_survive_inventory_render_and_validation(self):
        for kind in ("table", "figure"):
            with self.subTest(kind=kind), pymupdf.open() as document, tempfile.TemporaryDirectory() as directory:
                page = document.new_page(width=600, height=800)
                page.insert_text((80, 140), "Object labels", fontsize=7)
                pages = [{"page": 1, "width": 600, "height": 800}]
                units = [self.element("object", kind, [40, 100, 560, 160],
                                      render_mode="source_clip", order=1)]
                review = {"schema_version": 1, "source_sha256": "hash", "reviewed": True,
                          "reason": "Compared with source page.", "operations": [{
                              "op": "replace_unit", "id": "object", "kind": kind,
                              "bbox": [40, 100, 560, 160],
                              "reason": "Complete object has no caption in source."}]}
                source_review.apply_source_review(
                    units, review, "hash", pages, prepare_paper.protect_element)
                inventory = prepare_paper.build_visual_layout(document, units, pages, "hash")
                self.assertEqual(inventory["status"], "pass", inventory["errors"])
                visual = inventory["visuals"][0]
                self.assertEqual(visual["source_ids"], ["object"])
                self.assertEqual(visual["source_bbox"], [40, 100, 560, 160])
                self.assertEqual(visual["label"], "")
                self.assertIsNone(visual["caption_id"])
                self.assertTrue(visual["captionless"])
                self.assertEqual(visual["kind"], kind)
                nodes = render_translation.build_flow_nodes(units, {})
                self.assertEqual(len(nodes), 1)
                nodes[0]["visual_layout"] = visual
                self.assertFalse(native_latex._caption_matches_clip(
                    {"id": "unrelated-caption", "element": {}}, nodes[0]))
                root = Path(directory)
                _, placements, _ = native_latex.build_body_tex(
                    nodes, {}, ZH_ACADEMIC_V1, document, root)
                placement = placements[0]
                placement.update({"output_page": 1, "bbox": [
                    53, 100, 53 + placement["target_width_pt"],
                    100 + placement["target_height_pt"]]})
                rendered = native_latex.visual_placement_report(placement)
                (root / "visual-layout.json").write_text(json.dumps(inventory), encoding="utf-8")
                (root / "units.jsonl").write_text(json.dumps(units[0]) + "\n", encoding="utf-8")
                manifest = {"source_sha256": "hash", "paths": {
                    "visual_layout": "visual-layout.json", "translation_units": "units.jsonl"}}
                layout = {"placements": placements, "visual_layout": {"placements": [rendered]}}
                with pymupdf.open() as output:
                    output.new_page(width=595.276, height=841.89)
                    errors, _, metrics = check_translation.validate_visual_layout(
                        manifest, root / "manifest.json", layout, output)
                    self.assertEqual(errors, [])
                    self.assertEqual(metrics["checked"], 1)
                    boundary_layout = {"boundary_layout": {"segments": [{
                        "role": "front-original", "mode": "clip", "source_page": 1,
                        "source_bbox": [0, 0, 600, 180]}]}}
                    errors, _, metrics = check_translation.validate_visual_layout(
                        manifest, root / "manifest.json", boundary_layout, output)
                    self.assertEqual(errors, [])
                    self.assertEqual(metrics["original_boundary_visuals"], 1)
                    for rendered_items, expected_error in (
                        ([], "expected one rendered visual, found 0"),
                        ([rendered, rendered], "expected one rendered visual, found 2"),
                        ([{**rendered, "target_height_pt": 20}], "aspect ratio changed"),
                    ):
                        invalid = copy.deepcopy(layout)
                        invalid["visual_layout"]["placements"] = rendered_items
                        errors, _, _ = check_translation.validate_visual_layout(
                            manifest, root / "manifest.json", invalid, output)
                        self.assertTrue(any(expected_error in error for error in errors), errors)
                    # A missing caption ID alone must never bypass caption preservation.
                    inventory["visuals"][0].pop("captionless")
                    (root / "visual-layout.json").write_text(json.dumps(inventory), encoding="utf-8")
                    errors, _, _ = check_translation.validate_visual_layout(
                        manifest, root / "manifest.json", layout, output)
                    self.assertTrue(any("caption placement is missing" in error for error in errors), errors)

    def test_unreviewed_captionless_objects_remain_blocking(self):
        with pymupdf.open() as document:
            document.new_page(width=600, height=800)
            units = [self.element("object", "table", [40, 100, 560, 160],
                                  render_mode="source_clip")]
            result = prepare_paper.build_visual_layout(
                document, units, [{"page": 1, "width": 600, "height": 800}], "hash")
            self.assertEqual(result["status"], "fail")
            self.assertIn("no unique numbered caption", result["errors"][0])

    def test_adjacent_reviewed_captionless_objects_remain_distinct_with_fallback(self):
        with pymupdf.open() as document:
            document.new_page(width=600, height=800)
            units = [self.element(str(index), "table", [40, top, 560, top + 60],
                                  render_mode="source_clip", order=index,
                                  source_review_reason="Verified complete captionless table.")
                     for index, top in enumerate((100, 170), start=1)]
            result = prepare_paper.build_visual_layout(
                document, units, [{"page": 1, "width": 600, "height": 800}], "hash")
            self.assertEqual(result["status"], "pass")
            self.assertEqual(len(result["visuals"]), 2)
            self.assertEqual(len(result["warnings"]), 2)
            self.assertEqual(len(render_translation.build_flow_nodes(units, {})), 2)
            for visual in result["visuals"]:
                self.assertTrue(visual["automatic_fallback"])
                self.assertAlmostEqual(visual["target_width_pt"] / visual["target_height_pt"],
                                       visual["aspect_ratio"], places=4)

    def test_reviewed_group_with_missing_caption_still_fails(self):
        with pymupdf.open() as document:
            document.new_page(width=600, height=800)
            units = [self.element("object", "table", [40, 100, 560, 160],
                                  render_mode="source_clip", review_visual_id="table-1",
                                  source_review_reason="Reviewed numbered visual.")]
            result = prepare_paper.build_visual_layout(
                document, units, [{"page": 1, "width": 600, "height": 800}], "hash")
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["visuals"], [])


if __name__ == "__main__":
    unittest.main()
