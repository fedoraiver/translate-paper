#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2", "pillow>=10.4.0,<13"]
# ///
"""Image-only code is verified from actual source/output pixels, never invented text."""
from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path

import pymupdf

import check_translation as check
import native_latex
import render_translation
from typography import ZH_ACADEMIC_V1


class ImageCodePreservationTests(unittest.TestCase):
    def setUp(self):
        self.box = pymupdf.Rect(30, 40, 230, 90)
        self.text = "if t < 0:\n    return false"
        self.unit = {"id": "image-code", "order": 1, "page_order": 1, "page": 1,
                     "bbox": list(self.box), "kind": "code", "render_mode": "source_clip",
                     "source_text": self.text, "translatable": False, "section": "Example"}

    def source(self, text=None, *, native=False):
        text = self.text if text is None else text
        document = pymupdf.open()
        page = document.new_page(width=320, height=240)
        if native:
            page.insert_text((35, 56), text, fontname="cour", fontsize=10)
        else:
            with pymupdf.open() as drawing:
                vector = drawing.new_page(width=320, height=240)
                vector.insert_text((35, 56), text, fontname="cour", fontsize=10)
                pixmap = vector.get_pixmap(matrix=pymupdf.Matrix(3, 3), alpha=False)
                page.insert_image(page.rect, pixmap=pixmap)
        return document

    def output(self, source, target=None):
        target = target or pymupdf.Rect(85.237, 99.579, 315.237, 157.079)
        document = pymupdf.open()
        page = document.new_page(width=420, height=360)
        page.show_pdf_page(target, source, 0, clip=self.box)
        layout = {"placements": [{"id": self.unit["id"], "ids": [self.unit["id"]],
                                 "render_mode": "source_clip", "source_page": 1,
                                 "source_bbox": list(self.box), "output_page": 1,
                                 "bbox": list(target)}]}
        return document, layout

    def validate(self, source, output, layout, units=None):
        evidence = []
        errors = check.validate_code_preservation(
            output, units or [self.unit], layout, source_document=source, evidence=evidence)
        return errors, evidence

    def test_image_code_faithful_scale_translation_has_visual_evidence_and_no_fake_text(self):
        with self.source() as source:
            output, layout = self.output(source)
            with output:
                self.assertEqual(source[0].get_text(), "")
                self.assertEqual(output[0].get_text(), "")
                errors, evidence = self.validate(source, output, layout)
                self.assertEqual(errors, [])
                self.assertEqual(evidence[0]["source_modality"], "image")
                self.assertEqual(evidence[0]["status"], "pass")
                self.assertTrue(evidence[0]["source_pixel_sha256"])
                verified = {entry["id"] for entry in evidence
                            if entry["status"] == "pass" and entry["source_modality"] == "image"}
                self.assertEqual(check.missing_invariant_ids([self.unit], "", layout), ["image-code"])
                self.assertEqual(check.missing_invariant_ids(
                    [self.unit], "", layout, validated_image_code_ids=verified), [])

    def test_changed_single_operator_or_missing_line_cannot_pass_pixel_check(self):
        with self.source() as source:
            for changed in [self.text.replace("<", ">"), self.text.replace("false", "falsc"),
                            self.text.replace("false", "fa1se"), "if t < 0:"]:
                with self.subTest(changed=changed), self.source(changed) as changed_source:
                    output, layout = self.output(changed_source)
                    with output:
                        errors, evidence = self.validate(source, output, layout)
                        self.assertTrue(errors)
                        self.assertEqual(evidence[0]["status"], "fail")

    def test_same_bitmap_wrong_crop_or_visible_operator_overlay_fails(self):
        with self.source() as source:
            for clip in [self.box + (0, .05, 0, .05), self.box + (0, 2, 0, 2),
                         pymupdf.Rect(30, 40, 230, 65)]:
                with self.subTest(clip=clip):
                    output, layout = self.output(source)
                    with output:
                        page = output[0]
                        page.clean_contents()
                        output.update_stream(page.get_contents()[0], b"")
                        page.show_pdf_page(pymupdf.Rect(layout['placements'][0]['bbox']),
                                           source, 0, clip=clip, keep_proportion=False)
                        self.assertTrue(self.validate(source, output, layout)[0])
            output, layout = self.output(source)
            with output:
                target = pymupdf.Rect(layout['placements'][0]['bbox'])
                output[0].draw_rect(target + (0, 0, -target.width / 2, -target.height / 2),
                                    fill=(1, 1, 1), color=None)
                self.assertTrue(self.validate(source, output, layout)[0])
            output, layout = self.output(source)
            with output:
                target = pymupdf.Rect(layout['placements'][0]['bbox'])
                output[0].insert_text((target.x0 + 20, target.y0 + 18), ">", fontsize=12)
                self.assertTrue(self.validate(source, output, layout)[0])

    def test_other_disjoint_clip_of_same_full_page_bitmap_does_not_contaminate_identity(self):
        with self.source() as source:
            output, layout = self.output(source)
            with output:
                other_source = pymupdf.Rect(30, 140, 230, 190)
                target = pymupdf.Rect(85.237, 200.579, 315.237, 258.079)
                output[0].show_pdf_page(target, source, 0, clip=other_source)
                layout['placements'].append({'id':'other-figure','ids':['other-figure'],
                                             'render_mode':'source_clip','source_page':1,
                                             'source_bbox':list(other_source),'output_page':1,'bbox':list(target)})
                self.assertEqual(self.validate(source, output, layout)[0], [])

    def test_unknown_outlined_code_without_bitmap_identity_keeps_exact_pixel_requirement(self):
        with pymupdf.open() as source:
            page = source.new_page(width=320, height=240)
            page.draw_rect(pymupdf.Rect(40, 50, 44, 75), color=None, fill=(0, 0, 0))
            page.draw_rect(pymupdf.Rect(40, 71, 55, 75), color=None, fill=(0, 0, 0))
            output, layout = self.output(source)
            with output:
                errors, evidence = self.validate(source, output, layout)
                self.assertTrue(errors)
                self.assertEqual(evidence[0]['source_modality'], 'nontext-vector')
                self.assertFalse(evidence[0].get('noise_eligible', False))

    def test_only_exact_small_bottom_tracking_marker_is_allowed(self):
        for text, size, location, expected in [
                ('TPVISimage-code', .1, 'bottom', True),
                ('TPVISdifferent-code', .1, 'bottom', False),
                ('TPVISimage-code', 1, 'bottom', False),
                ('TPVISimage-code', .1, 'middle', False)]:
            with self.subTest(text=text, size=size, location=location), self.source() as source:
                output, layout = self.output(source)
                with output:
                    box = pymupdf.Rect(layout['placements'][0]['bbox'])
                    y = box.y1 - .01 if location == 'bottom' else box.y0 + box.height / 2
                    output[0].insert_text((box.x0, y), text, fontsize=size)
                    errors, evidence = self.validate(source, output, layout)
                    self.assertEqual(not errors, expected)
                    if expected:
                        self.assertEqual(evidence[0]['layout_markers'][0]['text'], text)

    def test_wrong_ownership_source_crop_target_aspect_and_neighbor_body_fail(self):
        with self.source() as source:
            output, layout = self.output(source)
            with output:
                for field, value in [("ids", ["image-code", "other"]),
                                     ("source_bbox", [29, 39, 231, 91]),
                                     ("bbox", [85.237, 99.579, 315.237, 160])]:
                    with self.subTest(field=field):
                        changed = copy.deepcopy(layout)
                        changed["placements"][0][field] = value
                        self.assertTrue(self.validate(source, output, changed)[0])
                neighbor = {"id": "body", "page": 1, "kind": "body", "translatable": True,
                            "bbox": [40, 45, 100, 60], "source_text": "Adjacent explanatory prose."}
                self.assertTrue(self.validate(source, output, layout, [self.unit, neighbor])[0])
                full = {**self.unit, "bbox": list(source[0].rect)}
                changed = copy.deepcopy(layout)
                changed["placements"][0]["source_bbox"] = full["bbox"]
                self.assertTrue(self.validate(source, output, changed, [full])[0])

    def test_native_source_code_still_requires_verbatim_text_and_owned_crop(self):
        with self.source(native=True) as source:
            output, layout = self.output(source)
            with output:
                errors, evidence = self.validate(source, output, layout)
                self.assertEqual(errors, [])
                self.assertEqual(evidence[0]["source_modality"], "text")
                changed = {**self.unit, "source_text": self.text.replace("<", ">")}
                self.assertTrue(self.validate(source, output, layout, [changed])[0])
                altered = copy.deepcopy(layout)
                altered["placements"][0]["source_bbox"][0] -= 1
                self.assertTrue(self.validate(source, output, altered)[0])

    @unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX unavailable")
    def test_real_native_tex_asset_scale_preserves_image_code(self):
        with tempfile.TemporaryDirectory() as directory, self.source() as source:
            root = Path(directory)
            nodes = render_translation.build_flow_nodes([self.unit], {})
            nodes = native_latex.prepare_native_clip_nodes(nodes, [self.unit], ZH_ACADEMIC_V1)
            tex, stubs, _ = native_latex.build_body_tex(nodes, {}, ZH_ACADEMIC_V1, source, root)
            path = root / "body.tex"
            path.write_text(tex, encoding="utf-8")
            pdf, _ = native_latex.compile_body_tex(root, path)
            placements = native_latex.locate_placements(pdf, stubs, nodes, 1, 1, ZH_ACADEMIC_V1)
            with pymupdf.open(pdf) as output:
                errors, evidence = self.validate(source, output, {"placements": placements})
                self.assertEqual(errors, [], evidence)
                self.assertEqual(evidence[0]["status"], "pass")


if __name__ == "__main__":
    unittest.main()
