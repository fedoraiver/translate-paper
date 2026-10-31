#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2", "pillow>=10.4.0,<13"]
# ///
"""Exact native visual identities must locate their actual PDF placements."""
import tempfile
import unittest
from pathlib import Path

import pymupdf

import native_latex
from typography import ZH_ACADEMIC_V1


class NativeClipLocatorTests(unittest.TestCase):
    def locate(self, markers, ids, render_mode="source_clip"):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "output.pdf"
            with pymupdf.open() as document:
                for page_markers in markers:
                    page = document.new_page(width=595.276, height=841.89)
                    for row, marker in enumerate(page_markers):
                        page.insert_text((100, 200 + row * 30), marker, fontsize=8)
                document.save(path)
            stubs = [{"id": unit_id, "ids": [unit_id],
                      "render_mode": render_mode, "source_bbox": [0, 0, 100, 40],
                      "target_width_pt": 100, "target_height_pt": 40}
                     for unit_id in ids]
            nodes = [{"element": {}, "text": ""} for _ in ids]
            return native_latex.locate_placements(
                path, stubs, nodes, 1, len(markers), ZH_ACADEMIC_V1)

    def test_long_shared_prefix_ids_find_distinct_actual_pages(self):
        ids = ["review-p0003-u0008", "review-p0003-u0010"]
        placed = self.locate([["TPVIS" + ids[0]], ["TPVIS" + ids[1]]], ids)
        self.assertEqual([item["output_page"] for item in placed], [1, 2])
        self.assertAlmostEqual(placed[1]["bbox"][0], 100)
        self.assertAlmostEqual(placed[1]["bbox"][3], 200)

    def test_missing_full_marker_does_not_fall_back_to_prefix_or_default_box(self):
        with self.assertRaisesRegex(ValueError, "expected exactly one.*marker"):
            self.locate([["TPVISreview-p0003-u0008"]], ["review-p0003-u0010"])

    def test_duplicate_full_marker_is_rejected(self):
        marker = "TPVISreview-p0003-u0010"
        with self.assertRaisesRegex(ValueError, "expected exactly one.*marker"):
            self.locate([[marker], [marker]], ["review-p0003-u0010"])

    def test_native_displays_use_their_own_exact_marker_pages(self):
        ids = ["review-p0003-u0008", "review-p0003-u0010"]
        placed = self.locate([[], ["TPMATH" + ids[0]], ["TPMATH" + ids[1]]],
                             ids, "native-math")
        self.assertEqual([item["output_page"] for item in placed], [2, 3])

    def test_missing_display_marker_does_not_inherit_a_prose_page(self):
        with self.assertRaisesRegex(ValueError, "expected exactly one.*marker"):
            self.locate([[]], ["equation"], "native-math")

    def test_duplicate_display_marker_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "expected exactly one.*marker"):
            self.locate([["TPMATHequation"], ["TPMATHequation"]],
                        ["equation"], "native-math")


if __name__ == "__main__":
    unittest.main()
