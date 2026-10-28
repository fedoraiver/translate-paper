"""Explicit reviewed listing inventory preserves invariant examples in Markdown."""

import unittest

import export_translation_markdown as exporter


class MarkdownListingTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {"source_sha256": "test-source", "source_title": "Tutorial"}
        self.units = [
            {"id": "first", "order": 1, "kind": "body", "translatable": True},
            {"id": "example", "order": 2, "kind": "margin-note", "render_mode": "source_clip",
             "translatable": False, "source_text": '# print_endline "hello";;\nhello\n- : unit = ()'},
            {"id": "last", "order": 3, "kind": "body", "translatable": True},
        ]
        self.translations = [{"id": "first", "translated_text": "前文。"},
                             {"id": "last", "translated_text": "后文。"}]
        self.inventory = {"source_sha256": "test-source", "listings": [
            {"id": "example", "semantic_kind": "code-listing"}]}

    def export(self, inventory=None):
        return exporter.export_markdown(self.manifest, self.units, self.translations,
                                        listing_layout=inventory or self.inventory)

    def test_examples_stay_between_translated_paragraphs_with_exact_newlines(self):
        output = self.export()
        self.assertIn("```\n" + self.units[1]["source_text"] + "\n```", output)
        self.assertLess(output.index("前文。"), output.index("print_endline"))
        self.assertLess(output.index("print_endline"), output.index("后文。"))

    def test_no_inventory_does_not_infer_code_from_margin_note(self):
        output = exporter.export_markdown(self.manifest, self.units, self.translations)
        self.assertNotIn("print_endline", output)

    def test_explicit_unboxed_code_expression_is_also_preserved(self):
        self.inventory["listings"][0]["semantic_kind"] = "code-expression"
        self.assertIn(self.units[1]["source_text"], self.export())

    def test_changed_source_and_invalid_listing_ids_fail(self):
        for inventory in (
            {**self.inventory, "source_sha256": "different"},
            {**self.inventory, "listings": [{"id": "missing", "semantic_kind": "code-listing"}]},
            {**self.inventory, "listings": self.inventory["listings"] * 2},
            {**self.inventory, "listings": [{"id": "first", "semantic_kind": "code-listing"}]},
        ):
            with self.subTest(inventory=inventory), self.assertRaises(ValueError):
                self.export(inventory)

    def test_listing_content_cannot_close_its_own_fence(self):
        self.units[1]["source_text"] = 'literal ``` marker\nnext line'
        self.assertIn('````\nliteral ``` marker\nnext line\n````', self.export())

    def test_listings_outside_selected_body_fail_instead_of_disappearing(self):
        self.manifest["boundary"] = {"introduction_id": "last"}
        with self.assertRaises(ValueError):
            self.export()


if __name__ == "__main__":
    unittest.main()
