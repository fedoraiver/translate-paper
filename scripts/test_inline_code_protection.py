"""Source monospace literals must not enter the native mathematics path."""

from collections import Counter
import unittest

import export_translation_markdown as exporter
import native_latex
import prepare_paper
import render_translation


def span(text, font="Consolas", style="text", **kwargs):
    return {"text": text, "font": font, "style": style, "size": 10,
            "flags": 8, "line": 0, "origin": [0, 10],
            "bbox": [0, 0, 100, 12], **kwargs}


class InlineCodeTests(unittest.TestCase):
    def test_inline_record_fields_can_wrap_without_changing_code(self):
        value = "{cost:Float,partno:Nat}"
        element = self.element("Use " + value + ".", [span(value)])
        latex = native_latex.rich_text_to_latex(
            {"id": "body", "text": element["protected_text"], "element": element},
            {}, Counter())
        self.assertIn(r",\allowbreak{}partno", latex)
        self.assertIn(r"\{cost:Float", latex)
        self.assertIn(r"partno:Nat\}", latex)

    def test_prose_brackets_and_parentheticals_remain_translatable(self):
        value = ("Theorem [Sequencing is a derived form] "
                 "(as in OCaml—cf. Rémy and Vouillon, 1998; Vouillon, 2000).")
        text, protected = prepare_paper.protect_text(value)
        self.assertIn("[Sequencing is a derived form]", text)
        self.assertIn("(as in OCaml—cf. Rémy and Vouillon,", text)
        self.assertEqual(list(protected.values()), ["1998", "2000"])
        _, citations = prepare_paper.protect_text("[AB12] [1, 3] (Smith and Jones, 1998)")
        self.assertEqual(list(citations.values()),
                         ["[AB12]", "[1, 3]", "(Smith and Jones, 1998)"])

    def element(self, text, spans):
        element = {"id": "body", "page": 1, "source_text": text,
                   "span_runs": spans}
        prepare_paper.protect_element(element)
        return element

    def test_code_literals_are_exact_atoms_without_prose_parentheses(self):
        for value in ("v0.14", "v0.15", "6.", "+.", "/.", "<-", ":=", ".(i)", "[1, 2]"):
            with self.subTest(value=value):
                element = self.element(f"Use ({value}) here.", [span(value)])
                fragments = list(element["inline_fragments"].values())
                self.assertEqual([(f["kind"], f["text"]) for f in fragments], [("code", value)])
                self.assertIn("Use ([[Fbody_0001]]) here.", element["protected_text"])
                self.assertEqual(fragments[0]["runs"][0]["font"], "Consolas")

    def test_small_cap_split_code_stays_complete(self):
        element = self.element("Press Ctrl-D to quit.", [span("C"), span("trl-D", size=8)])
        self.assertEqual([(f["kind"], f["text"]) for f in element["inline_fragments"].values()],
                         [("code", "Ctrl-D")])

    def test_math_font_overrides_monospace_flag(self):
        element = self.element("Value x+y.", [span("x+y", "CMMI10", "math")])
        self.assertEqual([f["kind"] for f in element["inline_fragments"].values()], ["math"])

    def test_font_name_and_pdf_flag_are_independent_code_evidence(self):
        for run in (span("<-", flags=0), span("<-", font="EmbeddedFont")):
            element = self.element("Use <- here.", [run])
            self.assertEqual([f["kind"] for f in element["inline_fragments"].values()], ["code"])

    def test_native_and_markdown_keep_code_and_italic_emphasis(self):
        element = self.element("Use Base with care.", [
            span("Use", "Times-Roman", flags=0),
            span("Base", "Consolas-Italic", "italic", flags=10),
            span("with care.", "Times-Roman", flags=0),
        ])
        text = element["protected_text"]
        statuses = Counter()
        latex = native_latex.rich_text_to_latex({"id": "body", "text": text, "element": element}, {}, statuses)
        self.assertIn(r"\texttt{Base}", latex)
        self.assertIn(r"\itshape", latex)
        self.assertEqual(statuses, Counter())
        markdown = exporter.rich_text_to_markdown(element, text)
        self.assertIn("`Base`", markdown)
        self.assertIn("*", markdown)
        html = render_translation.rich_text_to_html({"id": "body", "text": text, "element": element})
        self.assertIn("<em><code>Base</code></em>", html)

    def test_code_backticks_use_safe_markdown_delimiter(self):
        element = self.element("Use `Left here.", [span("`Left")])
        markdown = exporter.rich_text_to_markdown(element, element["protected_text"])
        self.assertIn("`` `Left ``", markdown)


if __name__ == "__main__":
    unittest.main()
