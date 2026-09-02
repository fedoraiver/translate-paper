"""Regression coverage for prose next to source-font mathematical atoms."""

from __future__ import annotations

import unittest

import prepare_paper


def span(text: str, style: str = "text", font: str = "Times-Roman", **kw):
    return {
        "text": text,
        "font": font,
        "size": 10.0,
        "flags": 0,
        "style": style,
        "origin": [0.0, 10.0],
        "bbox": [0.0, 0.0, 50.0, 12.0],
        "line": 0,
        **kw,
    }


class MathProseBoundariesTests(unittest.TestCase):
    def protect(self, text, spans):
        protected, _, fragments, _ = prepare_paper.build_rich_protection(text, spans)
        return protected, [f for f in fragments.values() if f["kind"] == "math"]

    def test_short_prose_does_not_become_math(self):
        for text in ("(i.e.,", "(e.g.,", "(by", "(as", "(a", "(a 10% "):
            with self.subTest(text=text):
                protected, fragments = self.protect(text, [span(text)])
                self.assertEqual(fragments, [])
                # Numerical invariant protection is independent of math.
                self.assertIn(text.strip().split()[0], protected)

    def test_membership_expression_keeps_explanatory_prose_translatable(self):
        text = "x ∈X (i.e., x is a member)."
        protected, fragments = self.protect(text, [
            span("x", "math", "CMMI10"),
            span("∈X", "math", "CMSY10"),
            span("(i.e.,"),
            span("x", "math", "CMMI10"),
            span("is a member)."),
        ])
        self.assertEqual([f["text"] for f in fragments], ["x ∈X"])
        self.assertIn("(i.e., x is a member).", protected)

    def test_by_phrase_does_not_capture_prose_parenthesis(self):
        text = "Reconstructed (by ECPi) witness."
        protected, fragments = self.protect(text, [
            span("Reconstructed"), span("(by"),
            span("ECP", "math", "CMMI10"),
            span("i", "math", "CMMI7", size=7.0, origin=[20.0, 12.0]),
            span(")"), span("witness."),
        ])
        self.assertEqual([f["text"] for f in fragments], ["ECPi"])
        self.assertEqual(fragments[0]["tex"], "ECP_{i}")
        self.assertIn("(by ", protected)
        self.assertIn(") witness.", protected)

    def test_prose_parenthesis_before_nonmembership_stays_outside_math(self):
        text = "Valid (i.e., x /∈X)."
        protected, fragments = self.protect(text, [
            span("Valid"), span("(i.e.,"),
            span("x", "math", "CMMI10"),
            span("/∈X", "math", "CMSY10"), span(")."),
        ])
        self.assertEqual([f["text"] for f in fragments], ["x /∈X"])
        self.assertIn("(i.e., ", protected)
        self.assertTrue(protected.endswith(")."))

    def test_real_parentheses_and_big_o_remain_complete(self):
        for text, body, wanted in (
            ("Value (x+y).", "x+y", "(x+y)"),
            ("Runtime O(√p).", "√p", "O(√p)"),
        ):
            with self.subTest(text=text):
                _, fragments = self.protect(text, [span(body, "math", "CMMI10")])
                self.assertEqual([f["text"] for f in fragments], [wanted])

    def test_centered_dot_inside_expression_is_not_a_list_bullet(self):
        for text in ("where c = a·b holds", "where p = r · z holds"):
            symbols = ("c", "=", "a", "·", "b") if "c =" in text else (
                "p", "=", "r", "·", "z"
            )
            spans = [span(symbol, "math", "CMSY10" if symbol == "·" else "CMMI10")
                     for symbol in symbols]
            _, fragments = self.protect(text, spans)
            self.assertEqual(len(fragments), 1)
            self.assertEqual(fragments[0]["text"], text.removeprefix("where ").removesuffix(" holds"))
            self.assertIn("·", fragments[0]["text"])

    def test_leading_list_bullet_is_still_excluded(self):
        _, fragments = self.protect("· Compute x+y.", [
            span("·", "math", "CMSY10"), span("Compute"),
            span("x+y", "math", "CMMI10"),
        ])
        self.assertEqual([f["text"] for f in fragments], ["x+y"])

    def test_upright_greek_base_keeps_calligraphic_subscript(self):
        text = "The accumulator ΛX is public."
        _, fragments = self.protect(text, [
            span("The accumulator"), span("Λ", "text", "CMR10"),
            span("X", "math", "CMSY7", size=7.0, origin=[20.0, 12.0]),
            span("is public."),
        ])
        self.assertEqual([f["text"] for f in fragments], ["ΛX"])
        self.assertEqual(fragments[0]["tex"], r"\Lambda _{\mathcal{X}}")

    def test_calligraphic_singleton_does_not_lose_font_semantics(self):
        _, fragments = self.protect("The set X is public.", [
            span("The set"), span("X", "math", "CMSY10"), span("is public."),
        ])
        self.assertEqual([f["tex"] for f in fragments], [r"\mathcal{X}"])

    def test_blackboard_singleton_does_not_lose_font_semantics(self):
        _, fragments = self.protect("Over F we compute.", [
            span("Over"), span("F", "math", "MSBM10"), span("we compute."),
        ])
        self.assertEqual([f["tex"] for f in fragments], [r"\mathbb{F}"])


if __name__ == "__main__":
    unittest.main()
