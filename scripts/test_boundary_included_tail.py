# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2", "pillow>=10.4.0,<13"]
# ///
"""Regression: an explicitly included final paragraph must be translated once."""
import unittest
import pymupdf

from render_translation import partition_translation_body, is_logical_continuation
from check_translation import validate_boundary_layout


class IncludedTailTests(unittest.TestCase):
    def test_terminal_footnote_does_not_join_the_next_paragraph(self):
        previous = {
            "translatable": True, "section": "Safety", "page": 1,
            "source_text": "This completes the statement.2",
            "inline_fragments": {"[[Fnote]]": {
                "kind": "footnote-marker", "text": "2", "footnote_id": "note2"
            }},
        }
        node = {"flow_role": "paragraph-start", "render_mode": "text",
                "elements": [previous]}
        following = {"translatable": True, "section": "Safety", "page": 2,
                     "source_text": "Progress: A term can take a step."}
        self.assertFalse(is_logical_continuation(node, following, "paragraph-start"))
        previous["source_text"] = "a term that can2"
        self.assertTrue(is_logical_continuation(node, following, "paragraph-start"))

    def test_qed_ends_a_paragraph(self):
        previous = {"translatable": True, "section": "Evaluation", "page": 1,
                    "source_text": "This completes the proof. □"}
        node = {"flow_role": "paragraph-start", "render_mode": "text",
                "elements": [previous]}
        following = {"translatable": True, "section": "Evaluation", "page": 2,
                     "source_text": "We next define a new relation."}
        self.assertFalse(is_logical_continuation(node, following, "paragraph-start"))

    def test_included_last_unit_is_body_without_original_tail(self):
        elements = [
            {"id": "intro", "order": 1, "page": 1,
             "bbox": [50, 50, 450, 90], "translatable": True,
             "kind": "body", "render_mode": "text"},
            {"id": "quote", "order": 2, "page": 1,
             "bbox": [50, 150, 450, 190], "translatable": True,
             "kind": "body", "render_mode": "text"},
        ]
        manifest = {
            "boundary": {"introduction_id": "intro", "post_body_stop_id": "quote"},
            "pages": [{"page": 1, "width": 500, "height": 700, "columns": 1}],
            "page_count": 1,
        }
        body, front, tail = partition_translation_body(elements, manifest)
        self.assertEqual([item["id"] for item in body], ["intro", "quote"])
        self.assertEqual(front, [])
        self.assertEqual(tail, [])
        with pymupdf.open() as source, pymupdf.open() as output:
            source.new_page(width=500, height=700)
            output.new_page(width=500, height=700)
            errors, metrics = validate_boundary_layout(
                source, output,
                {"boundary_layout": {
                    "profile": "original-front-translated-body-original-tail",
                    "translation_page_start": 1, "translation_page_end": 1,
                    "segments": [],
                }}, elements, manifest,
            )
        self.assertEqual(errors, [])
        self.assertEqual(metrics["original_units_checked"], 0)


if __name__ == "__main__":
    unittest.main()
