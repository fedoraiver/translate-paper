#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2", "pillow>=10.4.0,<13"]
# ///
"""Native clip and anchored-footnote layout regression coverage."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pymupdf

import native_latex
import render_translation
from typography import ZH_ACADEMIC_V1


def clip(unit_id, order, bbox):
    return {"id": unit_id, "order": order, "page": 1, "bbox": bbox,
            "kind": "figure-text", "render_mode": "source_clip",
            "source_text": "Algorithm", "font_size": 9.0}


class NativeClipLayoutTests(unittest.TestCase):
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
