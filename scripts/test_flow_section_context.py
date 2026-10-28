"""Run with uv run --with pymupdf python test_flow_section_context.py [--baseline]."""
import copy
import json
import unittest
from pathlib import Path

import render_translation as module

HERE = Path(__file__).resolve().parent


def build(elements):
    return module.build_flow_nodes(elements, {e["id"]: e["source_text"] for e in elements})


class FlowSectionRegression(unittest.TestCase):
    fixtures = json.loads((HERE / "test_flow_section_context.fixtures.json").read_text(encoding="utf-8"))

    def test_six_source_proven_page_continuations(self):
        for case in self.fixtures:
            with self.subTest(ids=case["expected_ids"]):
                nodes = build(copy.deepcopy(case["elements"]))
                self.assertIn(case["expected_ids"], [n["ids"] for n in nodes])

    def test_true_visible_heading_still_separates(self):
        elements = copy.deepcopy(self.fixtures[0]["elements"])
        heading = copy.deepcopy(elements[0])
        heading.update(id="visible-next-section", order=elements[1]["order"] + 0.5)
        elements.insert(2, heading)
        self.assertNotIn(self.fixtures[0]["expected_ids"], [n["ids"] for n in build(elements)])

    def test_suppressed_running_heading_does_not_separate(self):
        elements = copy.deepcopy(self.fixtures[0]["elements"])
        heading = copy.deepcopy(elements[0])
        heading.update(id="suppressed-folio", order=elements[1]["order"] + 0.5, render_suppressed=True)
        elements.insert(2, heading)
        self.assertIn(self.fixtures[0]["expected_ids"], [n["ids"] for n in build(elements)])

    def test_completed_paragraph_still_separates(self):
        elements = copy.deepcopy(self.fixtures[0]["elements"])
        elements[1]["source_text"] += "."
        self.assertNotIn(self.fixtures[0]["expected_ids"], [n["ids"] for n in build(elements)])

    def test_input_source_metadata_is_not_mutated(self):
        elements = copy.deepcopy(self.fixtures[0]["elements"])
        expected = copy.deepcopy(elements)
        build(elements)
        self.assertEqual(elements, expected)

    def test_without_visible_heading_preserves_existing_section_guard(self):
        elements = copy.deepcopy(self.fixtures[0]["elements"][1:])
        self.assertNotIn(self.fixtures[0]["expected_ids"], [n["ids"] for n in build(elements)])

if __name__ == "__main__":
    unittest.main(verbosity=2)
