#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2", "pillow>=10.4.0,<13"]
# ///
"""Native clip and anchored-footnote layout regression coverage."""

from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

import pymupdf

import native_latex
import render_translation
import check_translation
from typography import ZH_ACADEMIC_V1


def clip(unit_id, order, bbox):
    return {"id": unit_id, "order": order, "page": 1, "bbox": bbox,
            "kind": "figure-text", "render_mode": "source_clip",
            "source_text": "Algorithm", "font_size": 9.0}


class NativeClipLayoutTests(unittest.TestCase):
    def composite_caption(self, caption_bbox=None):
        figure = {**clip("figure", 1, [50, 100, 250, 200]),
                  "kind": "figure", "visual_id": "figure-1"}
        caption = {**clip("caption", 2, caption_bbox or [50, 210, 250, 240]),
                   "kind": "caption", "visual_id": "figure-1",
                   "source_text": "Figure 1: Caption retained in source vector."}
        nodes = render_translation.build_flow_nodes([figure, caption], {})
        nodes[0]["visual_layout"] = {
            "visual_id": "figure-1", "caption_id": "caption", "page": 1,
            "source_bbox": figure["bbox"], "source_ids": ["figure"],
            "target_width_pt": 230, "target_height_pt": 115,
            "target_scale": 1.15, "aspect_ratio": 2,
            "source_internal_font_pt": 9, "target_internal_font_pt": 10.35}
        return nodes[0], figure, caption

    def test_calibrated_composite_clip_includes_source_caption_and_keeps_scale(self):
        for caption_bbox in ([50, 210, 250, 240], [40, 210, 270, 240],
                             [50, 60, 250, 90]):
            with self.subTest(caption_bbox=caption_bbox):
                node, figure, caption = self.composite_caption(caption_bbox)
                with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
                    page = source.new_page(width=612, height=792)
                    page.insert_text((60, 140), "FIGURE CONTENT")
                    page.insert_text((caption_bbox[0] + 5, caption_bbox[1] + 12),
                                     "CAPTION VISIBLE", fontsize=9)
                    crop, width, height, scale = native_latex.source_clip_target_geometry(
                        source, node, ZH_ACADEMIC_V1)
                    expected = pymupdf.Rect(figure["bbox"]) | pymupdf.Rect(caption_bbox)
                    self.assertEqual(list(crop), list(expected))
                    self.assertAlmostEqual(scale, 1.15)
                    self.assertAlmostEqual(width / height, crop.width / crop.height)
                    asset = native_latex._create_source_clip_asset(source, node, Path(directory))
                    with pymupdf.open(asset) as clipped:
                        self.assertEqual(list(clipped[0].rect), [0, 0, crop.width, crop.height])
                        # Inspect visible text coordinates: PDF forms retain out-of-clip text.
                        visible = clipped[0].get_text(clip=clipped[0].rect)
                        self.assertIn("FIGURE CONTENT", visible)
                        self.assertIn("CAPTION VISIBLE", visible)
                    _, placements, _ = native_latex.build_body_tex(
                        [node], {}, ZH_ACADEMIC_V1, source, Path(directory))
                placement = {**placements[0], "output_page": 1,
                             "bbox": [100, 100, 100 + width, 100 + height]}
                report = native_latex.visual_placement_report(placement)
                self.assertAlmostEqual(report["target_width_pt"], 230)
                self.assertAlmostEqual(report["target_height_pt"], 115)
                self.assertEqual(report["aspect_ratio"], 2)

    def test_visual_validator_rejects_caption_id_outside_actual_crop(self):
        node, figure, caption = self.composite_caption()
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as output:
            root = Path(directory)
            output.new_page(width=595.276, height=841.89)
            (root / "visual-layout.json").write_text(json.dumps({
                "source_sha256": "hash", "visuals": [node["visual_layout"]]}), encoding="utf-8")
            (root / "units.jsonl").write_text(
                "\n".join(json.dumps(e) for e in [figure, caption]), encoding="utf-8")
            manifest = {"source_sha256": "hash", "paths": {
                "visual_layout": "visual-layout.json", "translation_units": "units.jsonl"}}
            placement = {"id": "figure", "ids": ["figure", "caption"],
                         "render_mode": "source_clip", "source_page": 1,
                         "source_bbox": figure["bbox"], "output_page": 1}
            visual = {**node["visual_layout"], "actual_scale": 1.15,
                      "output_page": 1, "bbox": [100, 100, 330, 215]}
            layout = {"placements": [placement], "visual_layout": {"placements": [visual]}}
            errors, _, _ = check_translation.validate_visual_layout(
                manifest, root / "manifest.json", layout, output)
            self.assertTrue(any("caption source region" in error for error in errors), errors)
            placement["source_bbox"] = [50, 100, 250, 240]
            errors, _, _ = check_translation.validate_visual_layout(
                manifest, root / "manifest.json", layout, output)
            self.assertEqual(errors, [])
            placement["source_page"] = 2
            errors, _, _ = check_translation.validate_visual_layout(
                manifest, root / "manifest.json", layout, output)
            self.assertTrue(any("caption source region" in error for error in errors), errors)

    def test_composite_width_clamp_uses_canonical_caption_envelope(self):
        node, figure, caption = self.composite_caption([40, 210, 560, 240])
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            root = Path(directory)
            source.new_page(width=612, height=792)
            _, stubs, _ = native_latex.build_body_tex(
                [node], {}, ZH_ACADEMIC_V1, source, root)
            stub = stubs[0]
            self.assertAlmostEqual(stub["target_width_pt"], 489.276)
            self.assertLess(stub["actual_scale"], 1.15)
            placement = {**stub, "output_page": 1,
                         "bbox": [53, 100, 542.276, 100 + stub["target_height_pt"]]}
            visual = native_latex.visual_placement_report(placement)
            (root / "visual-layout.json").write_text(json.dumps({
                "source_sha256": "hash", "visuals": [node["visual_layout"]]}), encoding="utf-8")
            (root / "units.jsonl").write_text(
                "\n".join(json.dumps(e) for e in [figure, caption]), encoding="utf-8")
            manifest = {"source_sha256": "hash", "paths": {
                "visual_layout": "visual-layout.json", "translation_units": "units.jsonl"}}
            layout = {"placements": [placement], "visual_layout": {"placements": [visual]}}
            with pymupdf.open() as output:
                output.new_page(width=595.276, height=841.89)
                errors, _, _ = check_translation.validate_visual_layout(
                    manifest, root / "manifest.json", layout, output)
                self.assertEqual(errors, [])
                visual["actual_scale"] *= 0.8
                errors, _, _ = check_translation.validate_visual_layout(
                    manifest, root / "manifest.json", layout, output)
                self.assertTrue(any("font calibration" in e for e in errors), errors)

    def test_text_caption_merged_into_tall_clip_calibrates_combined_envelope(self):
        node, figure, caption = self.composite_caption([50, 600, 250, 640])
        caption["render_mode"] = "text"
        node["elements"][1]["render_mode"] = "text"
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            root = Path(directory)
            source.new_page(width=612, height=792)
            _, stubs, _ = native_latex.build_body_tex([node], {}, ZH_ACADEMIC_V1, source, root)
            stub = stubs[0]
            placement = {**stub, "output_page": 1,
                         "bbox": [100, 100, 100 + stub["target_width_pt"],
                                  100 + stub["target_height_pt"]]}
            visual = native_latex.visual_placement_report(placement)
            (root / "visual-layout.json").write_text(json.dumps({
                "source_sha256": "hash", "visuals": [node["visual_layout"]]}), encoding="utf-8")
            (root / "units.jsonl").write_text(
                "\n".join(json.dumps(e) for e in [figure, caption]), encoding="utf-8")
            manifest = {"source_sha256": "hash", "paths": {
                "visual_layout": "visual-layout.json", "translation_units": "units.jsonl"}}
            layout = {"placements": [placement], "visual_layout": {"placements": [visual]}}
            with pymupdf.open() as output:
                output.new_page(width=595.276, height=841.89)
                errors, _, _ = check_translation.validate_visual_layout(
                    manifest, root / "manifest.json", layout, output)
                self.assertEqual(errors, [])
                placement["source_bbox"] = figure["bbox"]
                errors, _, _ = check_translation.validate_visual_layout(
                    manifest, root / "manifest.json", layout, output)
                self.assertTrue(any("caption source region" in e for e in errors), errors)

    def test_blank_source_padding_is_not_copied_to_output(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            body = pymupdf.open()
            body.new_page().insert_text((50,50), "Translated body")
            body.save(directory / "body.pdf")
            body.close()
            source = pymupdf.open()
            source.new_page()
            tail = [{"role":"tail-original", "mode":"full-page",
                     "source_page":1, "source_bbox":[0,0,595,842], "target_top":0}]
            native_latex.compose_final_pdf(directory / "result.pdf",
                directory / "body.pdf", source, [], tail)
            with pymupdf.open(directory / "result.pdf") as result:
                self.assertEqual(len(result), 1)
            self.assertEqual(tail, [])
            source.close()

    def test_anchored_note_does_not_split_cross_page_sentence(self):
        first = {"id":"a", "order":1, "page":1, "bbox":[50,700,500,730],
                 "kind":"body", "render_mode":"text", "translatable":True,
                 "section":"Introduction", "source_text":"A sentence that continues", "font_size":10}
        note = {**first, "id":"note", "order":2, "kind":"footnote",
                "anchor_id":"a", "source_text":"1. Footnote."}
        second = {**first, "id":"b", "order":3, "page":2,
                  "bbox":[50,50,500,80], "source_text":"on the next page."}
        nodes = render_translation.build_flow_nodes([first,note,second],
            {"a":"这一句延续到", "note":"1. 脚注。", "b":"下一页。"})
        self.assertEqual(nodes[0]["ids"], ["a","b"])
        self.assertEqual(nodes[1]["ids"], ["note"])

    def test_prose_in_next_to_math_is_not_an_automatic_subscript(self):
        self.assertEqual(
            native_latex.prose_to_latex("in an am xi x1", allow_auto_math=True),
            r"in an am \(x_{i}\) \(x_{1}\)",
        )

    def test_caption_between_figures_stays_with_its_reviewed_figure(self):
        first = clip("first", 1, [50, 50, 480, 530])
        first["visual_id"] = "Figure 1"
        caption = {"id": "caption-first", "order": 2, "page": 1,
                   "bbox": [80, 540, 400, 555], "kind": "caption",
                   "render_mode": "text", "source_text": "Figure 1. First",
                   "font_size": 10.0, "visual_id": "Figure 1"}
        second = clip("second", 3, [50, 570, 480, 720])
        second["visual_id"] = "Figure 2"
        nodes = render_translation.build_flow_nodes(
            [first, caption, second], {"caption-first": caption["source_text"]}
        )
        nodes[0]["visual_layout"] = {"visual_id": "Figure 1", "caption_id": "caption-first"}
        nodes[2]["visual_layout"] = {"visual_id": "Figure 2", "caption_id": "caption-second"}
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            source.new_page(width=612, height=792)
            tex, _, _ = native_latex.build_body_tex(
                nodes, {}, ZH_ACADEMIC_V1, source, Path(directory)
            )
        first_chunk = tex.split("% TP-NODE first\n", 1)[1].split("% TP-NODE", 1)[0]
        caption_chunk = tex.split("% TP-NODE caption-first\n", 1)[1].split("% TP-NODE", 1)[0]
        self.assertIn(r"\Needspace", first_chunk)
        self.assertNotIn(r"\Needspace", caption_chunk)

    def test_disjoint_algorithms_never_form_a_page_sized_crop(self):
        elements = [clip("bottom-left", 1, [54, 576, 297, 718]),
                    clip("top-right", 2, [315, 68, 558, 268])]
        legacy = render_translation.build_flow_nodes(elements, {})
        self.assertEqual(len(legacy), 1)  # reproduce legacy rectangle union
        nodes = native_latex.prepare_native_clip_nodes(legacy, elements, ZH_ACADEMIC_V1)
        self.assertEqual([node["id"] for node in nodes], ["bottom-left", "top-right"])
        self.assertEqual([node["element"]["bbox"] for node in nodes],
                         [element["bbox"] for element in elements])
        with pymupdf.open() as source:
            page = source.new_page(width=612, height=792)
            for node, element in zip(nodes, elements):
                self.assertEqual(list(native_latex._source_clip_rect(page, node)), element["bbox"])

    def test_algorithm_text_uses_source_body_font_ratio(self):
        element = clip("algorithm", 1, [54, 70, 297, 170])
        body = {"kind": "body", "render_mode": "text", "font_size": 10.0}
        nodes = native_latex.prepare_native_clip_nodes(
            render_translation.build_flow_nodes([element], {}), [body, element], ZH_ACADEMIC_V1
        )
        with pymupdf.open() as source:
            source.new_page(width=612, height=792)
            _, width, height, scale = native_latex.source_clip_target_geometry(source, nodes[0], ZH_ACADEMIC_V1)
        self.assertAlmostEqual(scale, 1.15)
        self.assertAlmostEqual(width, 243 * 1.15)
        self.assertAlmostEqual(height, 100 * 1.15)

    def test_width_limited_visual_reports_actual_internal_font(self):
        element = clip("wide-table", 1, [40, 100, 558, 150])
        node = render_translation.build_flow_nodes([element], {})[0]
        node["visual_layout"] = {"target_width_pt": 700.0,
                                 "source_internal_font_pt": 7.0,
                                 "target_internal_font_pt": 8.05}
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            source.new_page(width=612, height=792)
            _, placements, _ = native_latex.build_body_tex(
                [node], {}, ZH_ACADEMIC_V1, source, Path(directory)
            )
        actual = placements[0]
        self.assertAlmostEqual(actual["target_internal_font_pt"],
                               7.0 * actual["actual_scale"], places=3)
        self.assertLess(actual["target_internal_font_pt"], 8.05)

    def test_protected_footnote_label_is_not_printed_twice(self):
        token = "[[Fnote_1]]"
        element = {"footnote_number": "2", "anchor_id": "body",
                   "inline_fragments": {token: {"kind": "math", "text": "2."}},
                   "style_tokens": {}}
        node = {"id": "note", "element": element, "text": token + " Note content."}
        self.assertEqual(native_latex.footnote_body_without_number(node), "Note content.")
        self.assertEqual(node["text"], token + " Note content.")

    def test_footnote_keeps_a_different_leading_number(self):
        element = {"footnote_number": "2", "anchor_id": "body",
                   "inline_fragments": {"[[Fvalue]]": {"kind": "math", "text": "3."}},
                   "style_tokens": {}}
        node = {"id": "note", "element": element, "text": "[[Fvalue]] Values."}
        self.assertEqual(native_latex.footnote_body_without_number(node), node["text"])


if __name__ == "__main__":
    unittest.main()
