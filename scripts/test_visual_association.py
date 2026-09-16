import unittest

import prepare_paper
import pymupdf


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


if __name__ == "__main__":
    unittest.main()
