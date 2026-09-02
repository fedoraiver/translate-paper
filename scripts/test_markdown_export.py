#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2"]
# ///
"""Regressions for reviewed mathematics in the body Markdown artifact."""

from __future__ import annotations

import unittest

import export_translation_markdown as exporter


class MarkdownMathTests(unittest.TestCase):
    def unit(self, fragments: dict | None = None) -> dict:
        return {
            "id": "body",
            "order": 1,
            "kind": "body",
            "translatable": True,
            "inline_fragments": fragments or {},
        }

    def test_manual_review_overrides_unresolved_extraction(self) -> None:
        token = "[[Fbody_0001]]"
        unit = self.unit({token: {
            "kind": "math",
            "tex": "[[UNRESOLVED:CMSY-0068]]",
            "review_status": "unresolved",
        }})
        output = exporter.export_markdown(
            {"source_title": "Test paper"},
            [unit],
            [{"id": "body", "translated_text": f"Value {token}."}],
            {"entries": {token: {
                "kind": "inline",
                "tex": r"\langle x\rangle",
                "review_status": "manually_reviewed",
            }}},
        )
        self.assertIn(r"Value $\langle x\rangle$.", output)
        self.assertNotIn("UNRESOLVED", output)

    def test_unresolved_inline_math_cannot_be_exported(self) -> None:
        token = "[[Fbody_0001]]"
        for tex, status in (
            ("x", "unresolved"),
            ("[[UNRESOLVED:CMSY-0068]]", "manually_reviewed"),
            (r"\frac{x", "manually_reviewed"),
        ):
            with self.subTest(tex=tex, status=status):
                unit = self.unit({token: {
                    "kind": "math", "tex": tex, "review_status": status,
                }})
                with self.assertRaises(ValueError):
                    exporter.rich_text_to_markdown(unit, token)

    def test_display_math_without_review_cannot_be_silently_omitted(self) -> None:
        unit = {**self.unit(), "kind": "equation", "translatable": False}
        with self.assertRaisesRegex(ValueError, "requires manual review"):
            exporter.export_markdown({}, [unit], [])

    def test_adjacent_math_fragments_form_one_inline_math_run(self) -> None:
        first, second = "[[Fbody_0001]]", "[[Fbody_0002]]"
        unit = self.unit({
            first: {"kind": "math", "tex": "(1.3", "review_status": "auto_validated"},
            second: {"kind": "math", "tex": r"\mathrm{KB})", "review_status": "auto_validated"},
        })
        output = exporter.rich_text_to_markdown(unit, first + second)
        self.assertEqual(output, r"$(1.3 \mathrm{KB})$")
        self.assertNotIn("$$", output)

    def test_adjacent_same_style_spans_are_unambiguous(self) -> None:
        unit = {**self.unit(), "style_tokens": {"B0001": "bold", "B0002": "bold"}}
        output = exporter.rich_text_to_markdown(
            unit,
            "[[B0001_OPEN]]Name[[B0001_CLOSE]]"
            "[[B0002_OPEN]]: protocol[[B0002_CLOSE]]",
        )
        self.assertEqual(output, "**Name: protocol**")

    def test_reviewed_percent_uses_shared_native_normalization(self) -> None:
        token = "[[Fbody_0001]]"
        unit = self.unit({token: {
            "kind": "math", "tex": "x", "review_status": "auto_validated",
        }})
        output = exporter.rich_text_to_markdown(unit, token, {token: {
            "tex": "10%", "review_status": "manually_reviewed",
        }})
        self.assertEqual(output, r"$10\%$")

    def test_unresolved_marker_in_prose_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unresolved rich token"):
            exporter.rich_text_to_markdown(self.unit(), "[[UNRESOLVED:CMSY-0068]]")

    def test_crossed_styles_are_rejected_before_coalescing(self) -> None:
        unit = {**self.unit(), "style_tokens": {"B0001": "bold", "B0002": "bold"}}
        with self.assertRaisesRegex(ValueError, "crossed style token"):
            exporter.rich_text_to_markdown(
                unit,
                "[[B0001_OPEN]][[B0002_OPEN]]value[[B0001_CLOSE]][[B0002_CLOSE]]",
            )


if __name__ == "__main__":
    unittest.main()
