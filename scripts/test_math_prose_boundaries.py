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
    def test_reviewed_math_span_without_font_metrics(self):
        _, _, fragments, _ = prepare_paper.build_rich_protection(
            "x", [{"text": "x", "style": "math", "semantic_kind": "math", "line": 0}],
        )
        self.assertIn(("x", "math"), {
            (item["text"], item["kind"]) for item in fragments.values()
        })

    def test_reviewed_interval_takes_precedence_over_numeric_citation(self):
        text = "Use [0, 1], then see [2, 3]."
        runs = [
            span("Use "),
            span("[0, 1]", semantic_kind="math"),
            span(", then see "),
            span("[2, 3]"),
            span("."),
        ]
        _, _, fragments, _ = prepare_paper.build_rich_protection(text, runs)
        kinds = {(item["text"], item["kind"]) for item in fragments.values()}
        self.assertIn(("[0, 1]", "math"), kinds)
        self.assertIn(("[2, 3]", "citation"), kinds)
        self.assertNotIn(("[0, 1]", "citation"), kinds)

    def test_literal_set_braces_survive_raw_math_span_decoding(self):
        for font in ("NewCMMath-Book", "CMR10", "CMMI10", "UnknownMath-Regular"):
            with self.subTest(font=font):
                self.assertEqual(
                    prepare_paper.math_run_tex(span("{1,…,n}", "math", font)),
                    r"\{1,…,n\}",
                )
        self.assertEqual(
            prepare_paper.math_run_tex(span("{dom(𝜎)}", "math", "NewCMMath-Book")),
            r"\{\mathrm{dom}(𝜎)\}",
        )
        _, markdown = prepare_paper.math_markup([
            span("{1,…,n}", "math", "NewCMMath-Book")])
        self.assertEqual(markdown, r"$\{1,…,n\}$")

    def test_literal_brace_escaping_preserves_tex_commands_and_glyphs(self):
        for value in (r"\mathrm{track}", r"\sqrt{x+y}", r"\{x\}",
                      r"\left\{x\right\}", r"\Gamma", "𝑥+𝜎"):
            with self.subTest(value=value):
                self.assertEqual(
                    prepare_paper.math_run_tex(span(value, "math", "NewCMMath-Book")), value)
        self.assertEqual(
            prepare_paper.math_run_tex(span("A", "math", "CMSY10")), r"\mathcal{A}")
        self.assertEqual(
            prepare_paper.math_run_tex(span("ˆ", "math", "CMR10")), r"\widehatmark{}")

    def test_unicode_math_fonts_keep_basic_latin_names_upright(self):
        for font in ("NewCMMath-Book", "LatinModernMath-Regular", "STIXTwoMath-Regular", "CambriaMath"):
            for name in ("track", "pr", "id", "dom", "installed", "target", "anotherName"):
                with self.subTest(font=font, name=name):
                    self.assertEqual(
                        prepare_paper.math_run_tex(span(name, "math", font, flags=4)),
                        rf"\mathrm{{{name}}}",
                    )
        self.assertEqual(
            prepare_paper.math_run_tex(span("𝑘∈dom(𝜎)", "math", "NewCMMath-Book", flags=4)),
            r"𝑘∈\mathrm{dom}(𝜎)",
        )
        _, markdown = prepare_paper.math_markup([
            span("track", "math", "NewCMMath-Book", flags=4),
            span("Γ", "math", "NewCMMath-Book", flags=5, size=7, origin=[25, 12]),
        ])
        self.assertEqual(markdown, r"$\mathrm{track}_{\Gamma}$")

    def test_upright_name_decoding_does_not_reinterpret_variables_or_tex(self):
        for value, font, flags in (
            ("track", "CMMI10", 4),
            ("xy", "CMMI10", 4),
            ("track", "NewCMMath-Book", 6),
            ("track", "NewCMMath-Italic", 4),
            ("track", "UnknownMath-Regular", 4),
            ("𝑡𝑟𝑎𝑐𝑘", "NewCMMath-Book", 4),
            ("𝒯𝔽𝑥", "NewCMMath-Book", 4),
            ("x+y", "NewCMMath-Book", 4),
            (r"\operatorname{track}", "NewCMMath-Book", 4),
        ):
            with self.subTest(value=value, font=font, flags=flags):
                self.assertEqual(
                    prepare_paper.math_run_tex(span(value, "math", font, flags=flags)), value)

    def test_span_location_normalizes_nbsp_without_reusing_earlier_word(self):
        source = "Earlier effects. See\u00a01. effectiter"
        runs = [span("Earlier effects."), span("See\u00a01. "),
                span("effect", "math", "NewCMMath-Book"),
                span("iter", "math", "NewCMMath-Book", size=7)]
        located = prepare_paper.locate_span_runs(source, runs)
        self.assertEqual(len(located), len(runs))
        self.assertEqual(located[1]["text"], "See\u00a01.")
        self.assertEqual(located[2]["start"], source.rindex("effect"))
        for run in located:
            self.assertEqual(source[run["start"]:run["end"]], run["text"])

    def test_span_location_preserves_offsets_across_collapsed_whitespace(self):
        source = "Intro\t \u00a0term\r\n  x"
        located = prepare_paper.locate_span_runs(
            source, [span("Intro term"), span("x", "math")]
        )
        self.assertEqual(located[0]["start"], 0)
        self.assertEqual(located[0]["end"], source.index("\r"))
        self.assertEqual(located[0]["text"], source[:source.index("\r")])
        self.assertEqual(located[1]["start"], source.index("x"))

    def test_prose_parenthesis_around_several_variable_mentions_is_not_math(self):
        text = "image (x goes to red, y goes to green, z goes to blue)"
        for variable in "xyz":
            with self.subTest(variable=variable):
                start = text.index(variable, text.index("("))
                self.assertEqual(prepare_paper.expand_math_fragment_bounds(text, start, start + 1),
                                 (start, start + 1))
        _, _, fragments, _ = prepare_paper.build_rich_protection(text, [
            span("image (", semantic_kind="prose"),
            span("x", "math", "CMMI10", semantic_kind="math"),
            span("goes to red,", semantic_kind="prose"),
            span("y", "math", "CMMI10", semantic_kind="math"),
            span("goes to green,", semantic_kind="prose"),
            span("z", "math", "CMMI10", semantic_kind="math"),
            span("goes to blue)", semantic_kind="prose"),
        ])
        self.assertEqual([fragment["text"] for fragment in fragments.values()], list("xyz"))

    def test_balanced_math_parentheses_still_expand(self):
        for text, atom, expected in (("Use O(√p) here.", "√p", "O(√p)"),
                                     ("Use (x+y) here.", "x+y", "(x+y)"),
                                     ("Use f(x goes to red) here.", "x", "x")):
            with self.subTest(text=text):
                start = text.index(atom)
                left, right = prepare_paper.expand_math_fragment_bounds(text, start, start + len(atom))
                self.assertEqual(text[left:right], expected)

    def test_explicit_reviewed_math_keeps_single_variables(self):
        _, _, fragments, _ = prepare_paper.build_rich_protection(
            "for n", [span("for", semantic_kind="prose"),
                      span("n", "math", "CMMI10", semantic_kind="math")]
        )
        self.assertEqual([f["tex"] for f in fragments.values()], ["n"])

    def test_lucida_math_alphabets_keep_symbol_font_semantics(self):
        for font, value, expected in (
            ("Lucida New Math Symbol", "A", r"\mathcal{A}"),
            ("Lucida New Math Symbol", "L", r"\mathcal{L}"),
            ("Lucida New Math Arrows", "B", r"\mathbb{B}"),
        ):
            with self.subTest(font=font, value=value):
                self.assertEqual(prepare_paper.math_run_tex(span(value, "math", font)), expected)
                _, _, fragments, _ = prepare_paper.build_rich_protection(value, [span(value, "math", font)])
                self.assertEqual([f["tex"] for f in fragments.values()], [expected])

    def test_explicit_reviewed_prose_does_not_seed_math(self):
        for value in ("(cf.", "de-", "1.", "Rémy (1995),"):
            with self.subTest(value=value):
                _, _, fragments, _ = prepare_paper.build_rich_protection(
                    value, [span(value, semantic_kind="prose")]
                )
                self.assertFalse(any(f["kind"] == "math" for f in fragments.values()))

    def test_reviewed_prose_keeps_emphasis_and_adjacent_math(self):
        _, _, fragments, styles = prepare_paper.build_rich_protection(
            "(cf. x2", [span("(cf.", "italic", semantic_kind="prose"),
                       span("x", "math", "CMMI10"),
                       span("2", "math", "CMR7", size=7, origin=[5, 12])]
        )
        self.assertEqual([f["text"] for f in fragments.values()], ["x2"])
        self.assertEqual([f["tex"] for f in fragments.values()], ["x_{2}"])
        self.assertIn("italic", styles.values())

    def test_leading_numeric_enumerator_on_own_line_is_not_math(self):
        for marker in ("1.", "2.", "3.", "4.", "1)"):
            text = marker + "\nOCaml provides a useful feature."
            protected, _, fragments, _ = prepare_paper.build_rich_protection(text, [
                span(marker, "text", "RooneyWeb-Light", flags=4),
                span("OCaml provides a useful feature.", line=1),
            ])
            self.assertFalse(any(f["kind"] == "math" for f in fragments.values()))
            self.assertIn("OCaml provides a useful feature.", protected)

    def test_leading_numeric_expression_remains_math(self):
        for value in ("1.0 + 2.0", "1. + x"):
            _, _, fragments, _ = prepare_paper.build_rich_protection(value, [span(value)])
            self.assertEqual([f["kind"] for f in fragments.values()], ["math"])

    def test_author_year_citation_superscript_plus_stays_whole(self):
        text = "See [BCG+13,CFH+15]."
        protected, _, fragments, _ = prepare_paper.build_rich_protection(text, [
            span("See [BCG"), span("+", "math", "CMR7", size=7),
            span("13,CFH"), span("+", "math", "CMR7", size=7),
            span("15]."),
        ])
        self.assertEqual([f["text"] for f in fragments.values()], ["[BCG+13,CFH+15]"])
        self.assertNotIn("BCG", protected)

    def test_map_codomain_interval_is_not_a_citation(self):
        text = "Given f : N → [0, 1] we proceed."
        _, _, fragments, _ = prepare_paper.build_rich_protection(text, [
            span("Given"), span("f : N →", "math", "CMMI10"),
            span("[0, 1]"), span("we proceed."),
        ])
        self.assertFalse(any(f["kind"] == "citation" for f in fragments.values()))
        self.assertTrue(any("[0, 1]" in f["text"] for f in fragments.values()))

    def test_numeric_reference_still_is_a_citation(self):
        _, _, fragments, _ = prepare_paper.build_rich_protection(
            "See [1, 2] for details.", [span("See [1, 2] for details.")]
        )
        self.assertTrue(any(f["kind"] == "citation" for f in fragments.values()))

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
