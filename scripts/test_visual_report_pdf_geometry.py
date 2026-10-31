#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2"]
# ///
"""Geometry regressions for calibrated visual reports."""
import unittest

from native_latex import visual_placement_report

PDF_PER_TEX_PT = 72 / 72.27


class VisualReportPdfGeometry(unittest.TestCase):
    def placement(self, visual):
        return {
            "id": "combined", "visual_id": "figure-1",
            "source_bbox": [0, 0, 100, 60],
            "visual_source_bbox": visual,
            "target_width_pt": 200.0, "target_height_pt": 120.0,
            "actual_scale": 2.0,
            "bbox": [53, 100, 53 + 200 * PDF_PER_TEX_PT,
                     100 + 120 * PDF_PER_TEX_PT],
        }

    def test_full_width_visual_stays_inside_located_crop(self):
        placement = self.placement([0, 0, 100, 50])
        report = visual_placement_report(placement)
        self.assertAlmostEqual(report["bbox"][2], placement["bbox"][2], places=5)
        self.assertLessEqual(report["bbox"][2], placement["bbox"][2] + 0.001)
        self.assertEqual(report["target_width_pt"], 200.0)

    def test_inset_visual_uses_pdf_scale_for_offsets_and_extent(self):
        report = visual_placement_report(self.placement([10, 5, 90, 45]))
        scale = 2.0 * PDF_PER_TEX_PT
        expected = [53 + 10 * scale, 100 + 5 * scale,
                    53 + 90 * scale, 100 + 45 * scale]
        for actual, target in zip(report["bbox"], expected):
            self.assertAlmostEqual(actual, target, places=5)
        self.assertEqual(report["target_width_pt"], 160.0)
        self.assertEqual(report["target_height_pt"], 80.0)


if __name__ == "__main__":
    unittest.main()
