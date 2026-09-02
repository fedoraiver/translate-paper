#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pymupdf>=1.26.0,<2",
#   "pillow>=10.4.0,<13",
# ]
# ///

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

import pymupdf

import check_translation
import check_summary
import export_translation_markdown as export_markdown
import native_latex
import prepare_paper
import quick_validate
import render_translation
import source_review


class PaperPipelineTests(unittest.TestCase):
    def test_quick_validate_accepts_searchable_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paper.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "searchable paper")
            document.save(path)
            document.close()
            report = quick_validate.validate_pdf(path)
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["metrics"]["searchable_pages"], 1)

    def test_protected_numbers_do_not_consume_following_word_initials(self) -> None:
        source = (
            "2 Blockchain uses Section 3 shows 1 MB, 2.3 Key ideas, "
            "[12], and 10 ms."
        )
        protected, tokens = prepare_paper.protect_text(source)
        self.assertIn("Blockchain", protected)
        self.assertIn("shows", protected)
        self.assertIn("Key", protected)
        self.assertIn("MB", "".join(tokens.values()))
        self.assertNotIn("B Blockchain", protected)
        restored = protected
        for token, value in tokens.items():
            restored = restored.replace(token, value)
        self.assertEqual(restored, source)

    def test_rich_protection_preserves_bold_math_and_citation(self) -> None:
        source = "Completeness: For a1 see [24]."
        spans = [
            {
                "text": "Completeness:",
                "font": "CMBX10",
                "size": 10.0,
                "flags": 20,
                "style": "bold",
                "origin": [0.0, 10.0],
                "bbox": [0.0, 0.0, 70.0, 12.0],
                "line": 0,
                "span": 0,
            },
            {
                "text": "a",
                "font": "CMMI10",
                "size": 10.0,
                "flags": 6,
                "style": "math",
                "origin": [90.0, 10.0],
                "bbox": [90.0, 0.0, 96.0, 12.0],
                "line": 0,
                "span": 1,
            },
            {
                "text": "1",
                "font": "CMR7",
                "size": 7.0,
                "flags": 4,
                "style": "text",
                "origin": [96.0, 12.0],
                "bbox": [96.0, 4.0, 100.0, 12.0],
                "line": 0,
                "span": 2,
            },
        ]
        protected, numbers, fragments, styles = (
            prepare_paper.build_rich_protection(source, spans)
        )
        self.assertEqual(numbers, {})
        self.assertIn("[[B0001_OPEN]]Completeness:[[B0001_CLOSE]]", protected)
        self.assertEqual(styles, {"B0001": "bold"})
        self.assertEqual(
            [item["kind"] for item in fragments.values()],
            ["math", "citation"],
        )
        math = next(
            item for item in fragments.values() if item["kind"] == "math"
        )
        self.assertIn("<sub>1</sub>", math["html"])
        self.assertEqual(math["markdown"], "$a_{1}$")

    def test_math_markup_preserves_upright_operators_between_spans(self) -> None:
        source = "c = cα"
        runs = [
            {
                "text": "c",
                "size": 10.0,
                "style": "math",
                "origin": [0.0, 10.0],
                "start": 0,
                "end": 1,
            },
            {
                "text": "c",
                "size": 10.0,
                "style": "math",
                "origin": [20.0, 10.0],
                "start": 4,
                "end": 5,
            },
            {
                "text": "α",
                "size": 7.0,
                "style": "math",
                "origin": [26.0, 6.0],
                "start": 5,
                "end": 6,
            },
        ]
        html, markdown = prepare_paper.math_markup(
            runs, source, 0, len(source)
        )
        self.assertIn(" = ", html)
        self.assertIn("<sup>α</sup>", html)
        self.assertEqual(markdown, r"$c = c^{\alpha}$")

    def test_normalize_math_tex_converts_unicode_greek_and_micro_sign(self) -> None:
        self.assertEqual(
            prepare_paper.normalize_math_tex("µ(λ)+α+ρ+ℓ"),
            r"\mu (\lambda )+\alpha +\rho +\ell",
        )

    def test_math_markup_decodes_mathabx_and_blackboard_fonts(self) -> None:
        runs = [
            {
                "text": "P",
                "font": "TeX-matha10",
                "size": 10.0,
                "style": "math",
                "origin": [0.0, 10.0],
            },
            {
                "text": "Z",
                "font": "MSBM10",
                "size": 10.0,
                "style": "math",
                "origin": [10.0, 10.0],
            },
            {
                "text": "p",
                "font": "CMMI8",
                "size": 7.0,
                "style": "math",
                "origin": [20.0, 12.0],
            },
        ]
        _, markdown = prepare_paper.math_markup(runs)
        self.assertEqual(markdown, r"$\in \mathbb{Z}_{p}$")
        self.assertEqual(
            prepare_paper.math_render_strategy(runs, markdown),
            "tex-vector",
        )

    def test_math_markup_keeps_unmapped_large_glyph_for_review(self) -> None:
        runs = [
            {
                "text": "A",
                "font": "TeX-mathx10",
                "size": 10.0,
                "style": "math",
                "origin": [0.0, 10.0],
            }
        ]
        _, markdown = prepare_paper.math_markup(runs)
        self.assertIn("[[UNRESOLVED:MATHX-0041]]", markdown)
        self.assertEqual(
            prepare_paper.math_render_strategy(runs, markdown),
            "source-vector",
        )

    def test_symbol_font_bullet_is_not_protected_as_math(self) -> None:
        source = "• Block version: validation rules."
        spans = [
            {
                "text": "•",
                "font": "Symbol",
                "size": 10.0,
                "flags": 0,
                "style": "math",
                "origin": [10.0, 20.0],
                "bbox": [10.0, 10.0, 15.0, 20.0],
                "line": 0,
                "span": 0,
            },
            {
                "text": " Block version: validation rules.",
                "font": "Times-Roman",
                "size": 10.0,
                "flags": 0,
                "style": "text",
                "origin": [15.0, 20.0],
                "bbox": [15.0, 10.0, 180.0, 20.0],
                "line": 0,
                "span": 1,
            },
        ]
        protected, _, fragments, _ = prepare_paper.build_rich_protection(
            source, spans
        )
        self.assertEqual(protected, source)
        self.assertEqual(fragments, {})

    def test_table_narrative_is_body_not_caption(self) -> None:
        kind = prepare_paper.classify_text_kind(
            "Table 1 gives a comparison to other proofs.",
            pymupdf.Rect(100, 200, 480, 240),
            pymupdf.Rect(0, 0, 595, 842),
            10.0,
            10.0,
            4,
        )
        self.assertEqual(kind, "body")

    def test_top_of_page_prose_is_not_assumed_to_be_a_running_header(
        self,
    ) -> None:
        kind = prepare_paper.classify_text_kind(
            "This paragraph continues from the preceding page and spans the body.",
            pymupdf.Rect(72, 74, 540, 100),
            pymupdf.Rect(0, 0, 612, 792),
            10.9,
            10.9,
            6,
        )
        self.assertEqual(kind, "body")

    def test_math_line_starting_with_number_is_not_a_heading(self) -> None:
        kind = prepare_paper.classify_text_kind(
            "1 Xn · · · X(m−1)n",
            pymupdf.Rect(180, 300, 390, 320),
            pymupdf.Rect(0, 0, 595, 842),
            11.0,
            10.0,
            12,
        )
        self.assertEqual(kind, "equation")

    def test_short_numbered_formula_without_heading_word_is_equation(self) -> None:
        kind = prepare_paper.classify_text_kind(
            "1 ga2",
            pymupdf.Rect(180, 300, 230, 320),
            pymupdf.Rect(0, 0, 595, 842),
            11.0,
            10.0,
            18,
        )
        self.assertEqual(kind, "equation")

    def test_long_numbered_formula_prefix_is_not_a_heading(self) -> None:
        kind = prepare_paper.classify_text_kind(
            "1 µ+1 group elements and 2µN operations",
            pymupdf.Rect(120, 300, 470, 330),
            pymupdf.Rect(0, 0, 595, 842),
            11.0,
            10.0,
            31,
        )
        self.assertEqual(kind, "equation")

    def test_lowercase_paragraph_tail_with_inline_math_is_body(self) -> None:
        kind = prepare_paper.classify_text_kind(
            "accepts, b = 1.",
            pymupdf.Rect(130, 700, 240, 720),
            pymupdf.Rect(0, 0, 595, 842),
            10.0,
            10.0,
            7,
        )
        self.assertEqual(kind, "body")

    def test_matrix_fragments_do_not_trigger_two_column_layout(self) -> None:
        elements = [
            {
                "bbox": [130, 100, 280, 500],
                "kind": "body",
                "render_mode": "text",
                "source_text": "t0,0 t0,1 t1,0 t1,1 tm-1,0 tm-1,1",
            },
            {
                "bbox": [330, 100, 470, 500],
                "kind": "body",
                "render_mode": "text",
                "source_text": "t'0 t'1 t''0 t''1 u1 u2 u3 u4",
            },
            {
                "bbox": [130, 520, 470, 560],
                "kind": "body",
                "render_mode": "text",
                "source_text": "This paragraph spans the normal single-column body width.",
            },
        ]
        self.assertEqual(prepare_paper.classify_columns(elements, 595.0), 1)

    def test_two_prose_columns_are_still_detected(self) -> None:
        elements = [
            {
                "bbox": [40, 100, 280, 180],
                "kind": "body",
                "render_mode": "text",
                "source_text": "Left column paragraph with several ordinary prose words.",
            },
            {
                "bbox": [40, 200, 280, 280],
                "kind": "body",
                "render_mode": "text",
                "source_text": "Another left paragraph with enough ordinary prose words.",
            },
            {
                "bbox": [320, 100, 560, 180],
                "kind": "body",
                "render_mode": "text",
                "source_text": "Right column paragraph with several ordinary prose words.",
            },
            {
                "bbox": [320, 200, 560, 280],
                "kind": "body",
                "render_mode": "text",
                "source_text": "Another right paragraph with enough ordinary prose words.",
            },
        ]
        self.assertEqual(prepare_paper.classify_columns(elements, 600.0), 2)

    def test_algorithm_instruction_with_inline_math_is_body(self) -> None:
        kind = prepare_paper.classify_text_kind(
            "For 1 ≤ i ≤ µ + 1: If i = µ + 1",
            pymupdf.Rect(130, 300, 430, 340),
            pymupdf.Rect(0, 0, 595, 842),
            10.0,
            10.0,
            10,
        )
        self.assertEqual(kind, "body")

    def test_overlapping_inline_math_blocks_merge_into_body(self) -> None:
        def record(
            text: str,
            bbox: list[float],
            font: str,
            baseline: float,
        ) -> tuple[
            dict[str, object],
            str,
            list[float],
            list[str],
            list[dict[str, object]],
        ]:
            span = {
                "text": text,
                "font": font,
                "size": 10.0,
                "flags": 4,
                "origin": [bbox[0], baseline],
                "bbox": bbox,
            }
            block = {
                "bbox": bbox,
                "lines": [{"bbox": bbox, "spans": [span]}],
            }
            source, sizes, fonts, spans = prepare_paper.block_text(block)
            return block, source, sizes, fonts, spans

        parsed = [
            record("Cost O(", [100, 100, 300, 130], "LMRoman10-Regular", 128),
            record("√", [250, 110, 260, 125], "LMMathSymbols10-Regular", 119.5),
            record("N).", [260, 115, 400, 145], "LMRoman10-Regular", 128),
        ]
        merged = prepare_paper.merge_fragmented_text_blocks(
            parsed,
            pymupdf.Rect(0, 0, 595, 842),
            10.0,
            3,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0][1], "Cost O(√N).")

    def test_inline_math_merge_preserves_adjacent_visual_lines(self) -> None:
        def make_span(
            text: str,
            bbox: list[float],
            font: str,
            baseline: float,
        ) -> dict[str, object]:
            return {
                "text": text,
                "font": font,
                "size": 10.0,
                "flags": 4,
                "origin": [bbox[0], baseline],
                "bbox": bbox,
            }

        body_lines = [
            {
                "bbox": [100, 100, 400, 122],
                "spans": [
                    make_span(
                        "First sentence.",
                        [100, 100, 400, 122],
                        "LMRoman10-Regular",
                        120,
                    )
                ],
            },
            {
                "bbox": [100, 122, 400, 145],
                "spans": [
                    make_span(
                        "Cost O(",
                        [100, 122, 300, 145],
                        "LMRoman10-Regular",
                        140,
                    ),
                    make_span(
                        "N).",
                        [260, 122, 400, 145],
                        "LMRoman10-Regular",
                        140,
                    ),
                ],
            },
            {
                "bbox": [100, 142, 400, 165],
                "spans": [
                    make_span(
                        "Next sentence.",
                        [100, 142, 400, 165],
                        "LMRoman10-Regular",
                        160,
                    )
                ],
            },
        ]
        body_block = {"bbox": [100, 100, 400, 165], "lines": body_lines}
        body_source, body_sizes, body_fonts, body_spans = prepare_paper.block_text(
            body_block
        )
        radical_bbox = [250, 125, 260, 143]
        radical_block = {
            "bbox": radical_bbox,
            "lines": [
                {
                    "bbox": radical_bbox,
                    "spans": [
                        make_span(
                            "√",
                            radical_bbox,
                            "LMMathSymbols10-Regular",
                            131.5,
                        )
                    ],
                }
            ],
        }
        radical_source, radical_sizes, radical_fonts, radical_spans = (
            prepare_paper.block_text(radical_block)
        )
        parsed = [
            (body_block, body_source, body_sizes, body_fonts, body_spans),
            (
                radical_block,
                radical_source,
                radical_sizes,
                radical_fonts,
                radical_spans,
            ),
        ]
        merged = prepare_paper.merge_fragmented_text_blocks(
            parsed,
            pymupdf.Rect(0, 0, 595, 842),
            10.0,
            3,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(
            merged[0][1],
            "First sentence.\nCost O(√N).\nNext sentence.",
        )

    def test_reviewed_boundary_supports_paper_without_conclusion(self) -> None:
        units = [
            self.unit("u1", 1, "front-matter", "Title"),
            self.unit("u2", 2, "front-matter", "1. Introduction. Opening."),
            self.unit("u3", 3, "body", "Main text 12 ms."),
            self.unit("u4", 4, "heading", "Acknowledgments."),
        ]
        boundary = prepare_paper.apply_reviewed_boundary(
            units,
            {
                "source_sha256": "hash",
                "reviewed": True,
                "start_id": "u2",
                "stop_before_id": "u4",
                "allow_missing_conclusion": True,
                "reason": "The last main section precedes Acknowledgments.",
                "include_ids": [],
                "exclude_ids": [],
            },
            "hash",
        )
        self.assertTrue(boundary["manual_review"])
        self.assertFalse(boundary["needs_boundary_review"])
        self.assertFalse(units[0]["translatable"])
        self.assertTrue(units[1]["translatable"])
        self.assertTrue(units[2]["translatable"])
        self.assertFalse(units[3]["translatable"])

    def test_repeated_top_text_becomes_header(self) -> None:
        units = [
            {
                **self.unit(f"u{page}", page, "body", "RUNNING JOURNAL TITLE"),
                "bbox": [10, 20, 200, 40],
            }
            for page in range(2, 5)
        ]
        changed = prepare_paper.mark_repeated_running_text(
            units,
            [{"page": page, "height": 700} for page in range(2, 5)],
        )
        self.assertEqual(len(changed), 3)
        self.assertTrue(all(unit["kind"] == "header" for unit in units))

    def test_markdown_export_restores_tokens(self) -> None:
        units = [
            {
                **self.unit("u1", 1, "heading", "1. Introduction"),
                "translatable": True,
                "protected_tokens": {"[[P0001]]": "1"},
            },
            {
                **self.unit("u2", 2, "body", "Result [3]."),
                "translatable": True,
                "protected_tokens": {},
                "inline_fragments": {
                    "[[Fu2_0001]]": {
                        "token": "[[Fu2_0001]]",
                        "kind": "citation",
                        "text": "[3]",
                    }
                },
            },
        ]
        output = export_markdown.export_markdown(
            {"source_title": "Original Title"},
            units,
            [
                {"id": "u1", "translated_text": "[[P0001]]. 引言"},
                {"id": "u2", "translated_text": "结果见 [[Fu2_0001]]。"},
            ],
        )
        self.assertIn("# Original Title", output)
        self.assertIn("## 1. 引言", output)
        self.assertIn("结果见<sup>[3]</sup>。", output)

    def test_summary_validator_requires_length_and_sections(self) -> None:
        headings = "\n\n".join(
            f"## {aliases[0]}\n内容"
            for aliases in check_summary.REQUIRED_SECTIONS.values()
        )
        valid = check_summary.validate_summary(headings + "\n" + "中" * 850)
        self.assertEqual(valid["status"], "pass")
        invalid = check_summary.validate_summary("## 研究问题\n很短")
        self.assertEqual(invalid["status"], "fail")
        self.assertTrue(invalid["errors"])

    def test_academic_heading_hierarchy_and_separator(self) -> None:
        main = self.unit("h1", 1, "heading", "2 Blockchain architecture")
        sub = self.unit("h2", 2, "heading", "2.3 Key characteristics")
        main_node = {
            "element": main,
            "flow_role": "heading",
        }
        sub_node = {
            "element": sub,
            "flow_role": "heading",
        }
        self.assertEqual(render_translation.heading_level(main), 1)
        self.assertEqual(render_translation.heading_level(sub), 2)
        self.assertEqual(
            render_translation.style_for(main_node)["font_size"], 13.8
        )
        self.assertEqual(
            render_translation.style_for(sub_node)["font_size"], 11.5
        )
        self.assertEqual(
            render_translation.normalize_heading_text("2.3 Key characteristics"),
            "2.3\u3000Key characteristics",
        )

    def test_cross_page_continuation_becomes_one_logical_paragraph(self) -> None:
        first = {
            **self.unit("p1", 1, "body", "Blockchain was first proposed by"),
            "page": 1,
            "section": "1 Introduction",
            "translatable": True,
        }
        second = {
            **self.unit("p2", 2, "body", "Nakamoto in 2008."),
            "page": 2,
            "section": "1 Introduction",
            "translatable": True,
        }
        nodes = render_translation.build_flow_nodes(
            [first, second],
            {"p1": "区块链最早由", "p2": "中本聪于2008年提出。"},
        )
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["ids"], ["p1", "p2"])
        self.assertEqual(nodes[0]["joined_continuations"], 1)
        self.assertEqual(nodes[0]["text"], "区块链最早由中本聪于2008年提出。")

    def test_same_page_split_definition_becomes_one_logical_paragraph(self) -> None:
        first = {
            **self.unit("p1", 1, "body", "Completeness: The prover knows a"),
            "page": 1,
            "bbox": [100, 100, 480, 112],
            "font_size": 10.0,
            "section": "1 Introduction",
            "translatable": True,
        }
        second = {
            **self.unit("p2", 2, "body", "witness.\nSoundness: No false proof."),
            "page": 1,
            "bbox": [100, 113, 480, 137],
            "font_size": 10.0,
            "section": "1 Introduction",
            "translatable": True,
        }
        nodes = render_translation.build_flow_nodes(
            [first, second],
            {
                "p1": "完备性：证明者知道一个",
                "p2": "见证。[[PAR]]可靠性：不能证明假命题。",
            },
        )
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["ids"], ["p1", "p2"])
        self.assertIn("[[PAR]]可靠性", nodes[0]["text"])

    def test_body_paragraph_and_list_use_distinct_indentation(self) -> None:
        paragraph = {
            **self.unit("p1", 1, "body", "A paragraph."),
            "translatable": True,
        }
        bullet = {
            **self.unit("p2", 2, "body", "• An item."),
            "translatable": True,
        }
        paragraph_node = render_translation.build_flow_nodes(
            [paragraph], {"p1": "这是一个段落。"}
        )[0]
        list_node = render_translation.build_flow_nodes(
            [bullet], {"p2": "• 这是一个列表项。"}
        )[0]
        paragraph_style = render_translation.style_for(paragraph_node)
        list_style = render_translation.style_for(list_node)
        self.assertEqual(paragraph_node["flow_role"], "paragraph-start")
        self.assertEqual(paragraph_style["font_family"], "PaperBody")
        self.assertEqual(paragraph_style["font_size"], 11.5)
        self.assertEqual(paragraph_style["line_height"], 16.9)
        self.assertEqual(paragraph_style["letter_spacing"], 0.0)
        self.assertEqual(paragraph_style["text_indent"], 23.0)
        self.assertEqual(paragraph_style["margin_after"], 4.0)
        self.assertEqual(list_node["flow_role"], "list-item")
        self.assertEqual(list_style["text_indent"], -11.5)
        self.assertEqual(list_style["padding_left"], 23.0)

    def test_caption_continuation_merges_without_swallowing_table_text(
        self,
    ) -> None:
        caption = {
            **self.unit(
                "caption",
                1,
                "caption",
                "Figure 1 A diagram (see online",
            ),
            "page": 1,
            "bbox": [100, 100, 480, 112],
            "font_size": 9.0,
            "translatable": False,
        }
        continuation = {
            **self.unit(
                "continuation",
                2,
                "figure-text",
                "version for colours)",
            ),
            "page": 1,
            "bbox": [100, 114, 220, 126],
            "font_size": 9.0,
            "render_mode": "source_clip",
            "translatable": False,
        }
        table_text = {
            **self.unit(
                "table",
                3,
                "figure-text",
                "Property Public blockchain",
            ),
            "page": 1,
            "bbox": [100, 130, 480, 240],
            "font_size": 9.0,
            "render_mode": "source_clip",
            "translatable": False,
        }
        nodes = render_translation.build_flow_nodes(
            [caption, continuation, table_text],
            {
                "caption": caption["source_text"],
                "continuation": continuation["source_text"],
                "table": table_text["source_text"],
            },
        )
        self.assertEqual(nodes[0]["ids"], ["caption", "continuation"])
        self.assertEqual(
            nodes[0]["text"],
            "Figure 1 A diagram (see online version for colours)",
        )
        self.assertEqual(nodes[1]["render_mode"], "source_clip")

    def test_windows_academic_font_pair_is_selected(self) -> None:
        fonts = render_translation.find_typography_fonts()
        self.assertEqual(fonts.body.name.casefold(), "simsun.ttc")
        self.assertEqual(fonts.heading.name.casefold(), "simhei.ttf")
        self.assertEqual(fonts.latin_regular.name.casefold(), "lmroman10-regular.otf")
        self.assertEqual(fonts.latin_bold.name.casefold(), "lmroman10-bold.otf")
        self.assertEqual(fonts.latin_italic.name.casefold(), "lmroman10-italic.otf")
        self.assertEqual(fonts.math.name.casefold(), "latinmodern-math.otf")
        self.assertTrue(fonts.exact_profile_fonts)

    def test_latin_prose_uses_dedicated_font_span(self) -> None:
        markup = render_translation.plain_text_to_html(
            "采用 Pedersen commitment 和 NIZK。"
        )
        self.assertIn(
            "<span class='latin'>Pedersen commitment</span>",
            markup,
        )
        self.assertIn("<span class='latin'>NIZK</span>", markup)

    def test_semantic_paragraph_restarts_first_line_indent(self) -> None:
        fonts = render_translation.find_typography_fonts()
        node = {
            "id": "definitions",
            "text": "完备性：成立。[[PAR]]可靠性：成立。",
            "element": {
                "kind": "body",
                "inline_fragments": {},
                "style_tokens": {},
            },
            "flow_role": "paragraph-start",
        }
        _, markup, css = render_translation.fragment_payload(node, fonts)
        self.assertIn(
            "</div><div class='fragment semantic-paragraph'>",
            markup,
        )
        self.assertIn(".semantic-paragraph", css)
        self.assertIn("text-indent: 23.0pt", css)

    def test_math_strategy_rejects_ambiguous_nested_scripts(self) -> None:
        fragment = {
            "kind": "math",
            "markdown": "$g^{x}^{q}$",
            "runs": [],
        }
        self.assertFalse(
            render_translation.tex_is_safe_and_lossless(
                fragment, render_translation.fragment_tex(fragment)
            )
        )

    def test_math_strategy_accepts_structured_latin_modern_expression(self) -> None:
        fragment = {
            "kind": "math",
            "tex": r"g^r\prod_{i=1}^{q}(g^{x^i})^{a_i}",
            "runs": [],
            "render_strategy": "tex-vector",
        }
        self.assertTrue(
            render_translation.tex_is_safe_and_lossless(
                fragment, render_translation.fragment_tex(fragment)
            )
        )

    def test_single_math_variable_uses_direct_math_font(self) -> None:
        fragment = {
            "kind": "math",
            "tex": "q",
            "html": '<i class="math-var">q</i>',
        }
        self.assertTrue(
            render_translation.font_math_is_safe_and_lossless(fragment, "q")
        )
        self.assertFalse(
            render_translation.font_math_is_safe_and_lossless(
                {**fragment, "tex": "q_i"}, "q_i"
            )
        )
        self.assertTrue(
            render_translation.font_math_is_safe_and_lossless(
                {**fragment, "tex": "¬", "html": "¬"}, "¬"
            )
        )

    def test_indent_probe_skips_leading_math_fragments(self) -> None:
        self.assertFalse(
            check_translation.has_measurable_prose_start(
                "[[Fp0001_0001]]-CPDH assumption"
            )
        )
        self.assertTrue(
            check_translation.has_measurable_prose_start(
                "[[Bp0001_0001_OPEN]]完备性："
            )
        )
        self.assertTrue(
            check_translation.has_measurable_prose_start(
                "Goldwasser、Micali 提出"
            )
        )

    def test_rich_fragment_renders_controlled_html(self) -> None:
        fonts = render_translation.find_typography_fonts()
        node = {
            "id": "p1",
            "text": (
                "[[Bp1_0001_OPEN]]完备性：[[Bp1_0001_CLOSE]]"
                "结果[[Fp1_0001]]，变量[[Fp1_0002]]。"
            ),
            "element": {
                "kind": "body",
                "inline_fragments": {
                    "[[Fp1_0001]]": {
                        "kind": "citation",
                        "text": "[24]",
                    },
                    "[[Fp1_0002]]": {
                        "kind": "math",
                        "text": "a1",
                        "html": "<i class=\"math-var\">a</i><sub>1</sub>",
                    },
                },
                "style_tokens": {"Bp1_0001": "bold"},
            },
            "flow_role": "paragraph-start",
        }
        _, markup, css = render_translation.fragment_payload(node, fonts)
        self.assertIn("<strong>完备性：</strong>", markup)
        self.assertIn("<sup class='citation'>[24]</sup>", markup)
        self.assertIn("<sub>1</sub>", markup)
        self.assertIn("font-family: PaperMath", css)
        self.assertIn(
            ".fragment .math-var {\n          font-family: PaperLatin;",
            css,
        )

    def test_footnote_uses_small_unindented_style(self) -> None:
        node = {
            "element": {"kind": "footnote"},
            "flow_role": "footnote",
        }
        style = render_translation.style_for(node)
        self.assertEqual(style["font_size"], 8.5)
        self.assertEqual(style["text_indent"], 0.0)

    def test_footnote_falls_back_to_end_when_anchor_page_is_full(self) -> None:
        source = pymupdf.open()
        fonts = render_translation.find_typography_fonts()
        renderer = render_translation.FlowRenderer(
            source,
            595.28,
            841.89,
            1,
            18.0,
            fonts,
            render_translation.PROFILE,
        )
        renderer.y = renderer.bottom - 3.0
        node = {
            "id": "fn1",
            "ids": ["fn1"],
            "text": "1 这是一条需要回退的完整脚注。",
            "element": {"kind": "footnote", "translatable": True},
            "flow_role": "footnote",
            "joined_continuations": 0,
        }
        renderer.place_footnote(node)
        self.assertEqual([item["id"] for item in renderer.deferred_footnotes], ["fn1"])
        renderer.flush_deferred_footnotes()
        self.assertEqual(
            renderer.placements[-1]["footnote_location"], "end-of-body"
        )
        renderer.document.close()
        source.close()

    def test_source_clip_scaling_preserves_ratio_and_page_bounds(self) -> None:
        source = pymupdf.open()
        source.new_page(width=595.28, height=841.89)
        fonts = render_translation.find_typography_fonts()
        renderer = render_translation.FlowRenderer(
            source,
            595.28,
            841.89,
            1,
            18.0,
            fonts,
            render_translation.PROFILE,
        )
        node = {
            "id": "formula",
            "element": {
                "kind": "equation",
                "page": 1,
                "bbox": [100.0, 40.0, 200.0, 820.0],
            },
            "composite_parts": 1,
        }
        clip, width, height, _ = renderer.source_clip_geometry(node)
        self.assertAlmostEqual(width / height, clip.width / clip.height, places=6)
        self.assertLessEqual(width, renderer.content_width)
        self.assertLessEqual(
            height, renderer.bottom - renderer.margin_top
        )
        renderer.document.close()
        source.close()

    def test_source_clip_stays_with_following_caption(self) -> None:
        source = pymupdf.open()
        source.new_page(width=595.28, height=841.89)
        fonts = render_translation.find_typography_fonts()
        renderer = render_translation.FlowRenderer(
            source,
            595.28,
            841.89,
            1,
            18.0,
            fonts,
            render_translation.PROFILE,
        )
        element = {
            "kind": "table",
            "page": 1,
            "bbox": [40.0, 40.0, 555.0, 340.0],
            "translatable": False,
        }
        node = {
            "id": "table",
            "ids": ["table"],
            "element": element,
            "elements": [element],
            "composite_parts": 1,
        }
        clip_height = renderer.measure_source_clip_height(node) - 4.0
        renderer.y = renderer.bottom - clip_height - 1.0
        renderer.place_source_clip(node, keep_with_height=20.0)
        self.assertEqual(renderer.page.number, 1)
        self.assertAlmostEqual(
            renderer.placements[-1]["bbox"][1],
            renderer.margin_top,
            places=3,
        )
        renderer.document.close()
        source.close()

    def test_native_composite_clip_includes_disconnected_content_edge(
        self,
    ) -> None:
        source = pymupdf.open()
        page = source.new_page(width=595.28, height=841.89)
        node = {
            "id": "figure",
            "element": {
                "page": 1,
                "bbox": [120.0, 400.0, 440.0, 500.0],
            },
            "composite_parts": 2,
        }
        clip = native_latex._source_clip_rect(page, node)
        self.assertLessEqual(clip.x0, 120.0)
        self.assertGreaterEqual(clip.x1, 595.28 * 0.805)
        source.close()

    def test_native_visual_layout_bbox_overrides_composite_union(self) -> None:
        source = pymupdf.open()
        page = source.new_page(width=595.28, height=841.89)
        node = {
            "id": "figure",
            "element": {
                "page": 1,
                "bbox": [100.0, 400.0, 500.0, 600.0],
                "visual_id": "figure-1",
            },
            "composite_parts": 3,
            "visual_layout": {
                "source_bbox": [130.0, 420.0, 470.0, 580.0],
                "target_width_pt": 340.0,
            },
        }
        clip = native_latex._source_clip_rect(page, node)
        self.assertEqual(list(clip), [130.0, 420.0, 470.0, 580.0])
        source.close()

    def test_joined_placement_indexes_every_source_unit(self) -> None:
        placement = {
            "id": "p1",
            "ids": ["p1", "p2"],
            "output_page": 4,
        }
        indexed = check_translation.index_placements_by_id([placement])
        self.assertIs(indexed["p1"], placement)
        self.assertIs(indexed["p2"], placement)

    def test_full_original_source_pages_only_include_full_boundaries(
        self,
    ) -> None:
        pages = check_translation.full_original_source_pages(
            {
                "boundary_layout": {
                    "segments": [
                        {
                            "source_page": 1,
                            "mode": "full-page",
                            "role": "front-original",
                        },
                        {
                            "source_page": 8,
                            "mode": "full-page",
                            "role": "tail-original",
                        },
                        {
                            "source_page": 7,
                            "mode": "partial-page",
                            "role": "tail-original",
                        },
                    ]
                }
            }
        )
        self.assertEqual(pages, {1, 8})

    def test_native_source_clip_reserves_space_for_following_caption(
        self,
    ) -> None:
        source = pymupdf.open()
        source.new_page(width=439.36, height=666.14)
        table_element = {
            "kind": "table",
            "page": 1,
            "bbox": [7.0, 65.0, 432.0, 303.0],
            "translatable": False,
        }
        table = {
            "id": "table",
            "ids": ["table"],
            "element": table_element,
            "elements": [table_element],
            "render_mode": "source_clip",
            "flow_role": "source-clip",
            "composite_parts": 1,
        }
        caption_element = {
            "kind": "caption",
            "page": 1,
            "bbox": [70.0, 323.0, 369.0, 334.0],
            "translatable": False,
            "inline_fragments": {},
            "style_tokens": {},
        }
        caption = {
            "id": "caption",
            "ids": ["caption"],
            "element": caption_element,
            "elements": [caption_element],
            "render_mode": "text",
            "flow_role": "invariant-text",
            "text": "Table 1: Caption",
            "joined_continuations": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            tex, _, _ = native_latex.build_body_tex(
                [table, caption],
                {},
                render_translation.PROFILE,
                source,
                Path(directory),
            )
        table_chunk = tex.split("% TP-NODE table\n", 1)[1].split(
            "% TP-NODE caption\n", 1
        )[0]
        self.assertIn("TPVIStable", table_chunk)
        self.assertRegex(
            table_chunk,
            r"\\Needspace\{\d+\.\d{2}pt\}\s+\\par\\smallskip",
        )
        needspace = native_latex._caption_clip_needspace(
            source, table, render_translation.PROFILE
        )
        self.assertGreater(needspace, 340.0)
        source.close()

    def test_native_source_clip_location_uses_exact_visual_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_path = Path(directory) / "output.pdf"
            document = pymupdf.open()
            first = document.new_page(width=595.28, height=841.89)
            first.insert_text((72, 72), "repeated table text")
            second = document.new_page(width=595.28, height=841.89)
            second.insert_text((72, 72), "repeated table text TPVIStable")
            document.save(output_path)
            document.close()
            stub = {
                "id": "table",
                "ids": ["table"],
                "render_mode": "source_clip",
                "source_bbox": [100.0, 100.0, 300.0, 200.0],
                "target_width_pt": 200.0,
                "target_height_pt": 100.0,
            }
            node = {
                "id": "table",
                "text": "repeated table text",
                "element": {"inline_fragments": {}, "style_tokens": {}},
            }
            placements = native_latex.locate_placements(
                output_path,
                [stub],
                [node],
                1,
                2,
                render_translation.PROFILE,
            )
        self.assertEqual(placements[0]["output_page"], 2)

    def test_native_inline_math_never_uses_an_image_asset(self) -> None:
        token = "[[Fp1_0001]]"
        node = {
            "id": "p1",
            "text": f"变量{token}。",
            "element": {
                "kind": "body",
                "inline_fragments": {
                    token: {
                        "token": token,
                        "kind": "math",
                        "tex": "a_i",
                        "asset_path": "forbidden.svg",
                        "review_status": "manually_reviewed",
                    }
                },
                "style_tokens": {},
            },
        }
        statuses = Counter()
        rendered = native_latex.rich_text_to_latex(node, {}, statuses)
        self.assertIn(r"\(a_i\)", rendered)
        self.assertNotIn("forbidden.svg", rendered)
        self.assertEqual(statuses["manually_reviewed"], 1)

    def test_native_math_rejects_unicode_radical_glyph(self) -> None:
        token = "[[Fp1_0001]]"
        fragment = {
            "token": token,
            "kind": "math",
            "tex": "O(√p)",
            "review_status": "manually_reviewed",
        }
        with self.assertRaisesRegex(
            native_latex.MathReviewError,
            r"use \\sqrt\{\.\.\.\}",
        ):
            native_latex.resolve_inline_math(fragment, {})

    def test_native_prose_does_not_guess_english_words_are_math(self) -> None:
        node = {
            "id": "caption",
            "text": "Digital signature used in blockchain.",
            "element": {
                "kind": "caption",
                "inline_fragments": {},
                "style_tokens": {},
            },
        }
        rendered = native_latex.rich_text_to_latex(node, {}, Counter())
        self.assertEqual(rendered, "Digital signature used in blockchain.")
        self.assertNotIn(r"\(in\)", rendered)

    def test_math_font_is_optional_when_paper_has_no_math(self) -> None:
        self.assertEqual(
            check_translation.expected_native_math_count(
                {
                    "rich_text": {
                        "math_render_strategies": {
                            "native-font": 0,
                            "native-display-font": 0,
                        }
                    }
                }
            ),
            0,
        )
        self.assertEqual(
            check_translation.expected_native_math_count(
                {
                    "rich_text": {
                        "math_render_strategies": {
                            "native-font": 3,
                            "native-display-font": 2,
                        }
                    }
                }
            ),
            5,
        )

    def test_native_display_math_requires_manual_review(self) -> None:
        node = {
            "id": "eq1",
            "elements": [{"kind": "equation"}],
            "element": {"kind": "equation"},
        }
        with self.assertRaises(native_latex.MathReviewError):
            native_latex.resolve_display_math(
                node,
                {
                    "eq1": {
                        "tex": r"\sum_i a_i",
                        "review_status": "unresolved",
                    }
                },
            )

    def test_math_review_groups_display_equation_and_flags_ambiguous_inline(
        self,
    ) -> None:
        inline = {
            **self.unit("p1", 1, "body", "x"),
            "inline_fragments": {
                "[[Fp1_0001]]": {
                    "kind": "math",
                    "token": "[[Fp1_0001]]",
                    "text": "x",
                    "tex": "x",
                    "review_status": "unresolved",
                    "source_bbox": [0, 0, 10, 10],
                }
            },
        }
        equation = {
            **self.unit("eq1", 2, "equation", "a = b"),
            "render_mode": "source_clip",
            "page": 1,
            "bbox": [10, 20, 100, 40],
        }
        review = prepare_paper.build_math_review(
            [inline, equation],
            "hash",
        )
        self.assertEqual(review["source_sha256"], "hash")
        self.assertEqual(
            review["entries"]["[[Fp1_0001]]"]["review_status"],
            "unresolved",
        )
        self.assertEqual(review["entries"]["eq1"]["kind"], "display")

    def test_math_review_splits_adjacent_display_groups_by_visual_id(
        self,
    ) -> None:
        first = {
            **self.unit("eq1", 1, "equation", "a = b"),
            "render_mode": "source_clip",
            "page": 1,
            "bbox": [10, 20, 100, 40],
            "visual_id": "math-p1-a",
        }
        second = {
            **self.unit("eq2", 2, "equation", "c = d"),
            "render_mode": "source_clip",
            "page": 1,
            "bbox": [10, 50, 100, 70],
            "visual_id": "math-p1-b",
        }
        review = prepare_paper.build_math_review([first, second], "hash")
        self.assertEqual(list(review["entries"]), ["eq1", "eq2"])
        self.assertEqual(review["entries"]["eq1"]["source_ids"], ["eq1"])
        self.assertEqual(review["entries"]["eq2"]["source_ids"], ["eq2"])

    def test_math_review_keeps_auto_validated_inline_for_audit(self) -> None:
        inline = {
            **self.unit("p1", 1, "body", "x"),
            "inline_fragments": {
                "[[Fp1_0001]]": {
                    "kind": "math",
                    "token": "[[Fp1_0001]]",
                    "text": "x_i",
                    "tex": "x_i",
                    "review_status": "auto_validated",
                    "source_bbox": [0, 0, 10, 10],
                }
            },
        }
        review = prepare_paper.build_math_review([inline], "hash")
        self.assertEqual(
            review["entries"]["[[Fp1_0001]]"]["review_status"],
            "auto_validated",
        )

    def test_math_review_ignores_original_tail_and_non_math_visuals(
        self,
    ) -> None:
        translated = {
            **self.unit("body", 10, "body", "text"),
            "translatable": True,
        }
        visual = {
            **self.unit("figure", 11, "figure", ""),
            "render_mode": "source_clip",
            "translatable": False,
        }
        equation = {
            **self.unit("equation", 12, "equation", "a = b"),
            "render_mode": "source_clip",
            "translatable": False,
        }
        translated_after = {
            **self.unit("body-after", 13, "body", "text"),
            "translatable": True,
        }
        tail = {
            **self.unit("tail-equation", 20, "equation", "c = d"),
            "render_mode": "source_clip",
            "translatable": False,
        }
        tail["inline_fragments"] = {
            "[[Ftail_0001]]": {
                "kind": "math",
                "text": "x",
                "tex": "x",
                "review_status": "unresolved",
            }
        }
        review = prepare_paper.build_math_review(
            [translated, visual, equation, translated_after, tail],
            "hash",
        )
        self.assertIn("equation", review["entries"])
        self.assertNotIn("figure", review["entries"])
        self.assertNotIn("tail-equation", review["entries"])
        self.assertNotIn("[[Ftail_0001]]", review["entries"])

    def test_math_review_does_not_reuse_tex_for_changed_source_text(self) -> None:
        inline = {
            **self.unit("p1", 1, "body", "y"),
            "inline_fragments": {
                "[[Fp1_0001]]": {
                    "kind": "math",
                    "token": "[[Fp1_0001]]",
                    "text": "y",
                    "tex": "y",
                    "review_status": "unresolved",
                    "source_bbox": [0, 0, 10, 10],
                }
            },
        }
        review = prepare_paper.build_math_review(
            [inline],
            "hash",
            {
                "source_sha256": "hash",
                "entries": {
                    "[[Fp1_0001]]": {
                        "kind": "inline",
                        "source_page": 1,
                        "source_text": "x",
                        "tex": "x_i",
                        "review_status": "manually_reviewed",
                    }
                },
            },
        )
        entry = review["entries"]["[[Fp1_0001]]"]
        self.assertEqual(entry["source_text"], "y")
        self.assertEqual(entry["tex"], "y")
        self.assertEqual(entry["review_status"], "unresolved")

    def test_symbol_only_display_fragments_are_promoted_and_grouped(self) -> None:
        equation = {
            **self.unit("eq", 1, "equation", "a = b"),
            "render_mode": "source_clip",
            "page": 1,
            "order": 1,
            "bbox": [150, 200, 350, 220],
            "span_runs": [],
        }
        symbol = {
            **self.unit("symbol", 1, "body", "h"),
            "render_mode": "text",
            "page": 1,
            "order": 2,
            "bbox": [350, 200, 360, 220],
            "span_runs": [{"text": "h", "style": "math"}],
        }
        operator = {
            **self.unit("operator", 1, "body", "Pr"),
            "render_mode": "text",
            "page": 1,
            "order": 3,
            "bbox": [130, 200, 150, 220],
            "span_runs": [{"text": "Pr", "style": "text"}],
        }
        elements = [equation, symbol, operator]
        prepare_paper.promote_display_math_fragments(elements, 10.0)
        self.assertTrue(all(item["kind"] == "equation" for item in elements))
        review = prepare_paper.build_math_review(elements, "hash")
        self.assertEqual(list(review["entries"]), ["eq"])
        self.assertEqual(
            review["entries"]["eq"]["source_ids"],
            ["eq", "symbol", "operator"],
        )

    def test_math_fragment_misclassified_as_footnote_is_promoted(self) -> None:
        element = {
            **self.unit("subscript", 1, "footnote", "0,0 t′"),
            "render_mode": "text",
            "page": 1,
            "order": 1,
            "bbox": [200, 600, 245, 614],
            "span_runs": [
                {"text": "0,0", "style": "text"},
                {"text": "t′", "style": "math"},
            ],
        }
        prepare_paper.promote_display_math_fragments(
            [
                {
                    **self.unit("eq", 1, "equation", "a = b"),
                    "render_mode": "source_clip",
                    "bbox": [180, 600, 210, 614],
                    "span_runs": [],
                },
                element,
            ],
            10.0,
        )
        self.assertEqual(element["kind"], "equation")
        self.assertEqual(element["render_mode"], "source_clip")

    def test_untranslated_front_matter_footnote_needs_no_anchor(self) -> None:
        footnote = {
            **self.unit("front-note", 1, "footnote", "† Publication note"),
            "translatable": False,
            "inline_fragments": {},
        }
        report = prepare_paper.link_footnote_anchors([footnote])
        self.assertEqual(report, {"footnotes": 0, "linked": 0, "warnings": []})

    def test_markdown_exports_reviewed_display_math(self) -> None:
        units = [
            {
                **self.unit("intro", 1, "heading", "1 Introduction"),
                "translatable": True,
            },
            {
                **self.unit("eq1", 2, "equation", "a = b"),
                "render_mode": "source_clip",
                "translatable": False,
            },
        ]
        output = export_markdown.export_markdown(
            {
                "source_title": "Paper",
                "boundary": {
                    "introduction_id": "intro",
                    "post_body_stop_id": None,
                },
            },
            units,
            [{"id": "intro", "translated_text": "1 引言"}],
            {
                "entries": {
                    "eq1": {
                        "kind": "display",
                        "source_ids": ["eq1"],
                        "tex": r"\sum_i a_i=b",
                        "review_status": "manually_reviewed",
                    }
                }
            },
        )
        self.assertIn("$$\n\\sum_i a_i=b\n$$", output)

    def test_legacy_plain_text_unit_renders_without_rich_fields(self) -> None:
        fonts = render_translation.find_typography_fonts()
        node = {
            "id": "legacy",
            "ids": ["legacy"],
            "text": "旧版纯文本翻译单元。",
            "element": {"kind": "body"},
            "flow_role": "paragraph-start",
            "joined_continuations": 0,
        }
        _, markup, _ = render_translation.fragment_payload(node, fonts)
        self.assertIn("旧版纯文本翻译单元。", markup)

    def test_only_generic_academic_profile_is_supported(self) -> None:
        self.assertEqual(
            set(render_translation.SUPPORTED_PROFILES), {"zh-academic-v1"}
        )
        argv = [
            "render_translation.py",
            "--manifest",
            "manifest.json",
            "--translations",
            "translations.jsonl",
            "--output",
            "translated.pdf",
            "--typography-profile",
            "legacy-profile",
        ]
        with mock.patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit):
                render_translation.parse_args()

    def test_academic_profile_uses_a4_equivalent_spacing(self) -> None:
        profile = render_translation.PROFILE
        self.assertEqual(profile.body_font_size, 11.5)
        self.assertEqual(profile.body_line_pitch, 16.9)
        self.assertEqual(profile.body_letter_spacing, 0.0)
        self.assertEqual(profile.body_text_indent, 23.0)
        self.assertEqual(profile.margin_left, 53.0)
        self.assertEqual(profile.margin_right, 53.0)
        self.assertEqual(profile.margin_top, 48.5)
        self.assertEqual(profile.margin_bottom, 55.5)

    def test_direct_text_embedding_has_no_oversized_measurement_form(self) -> None:
        fonts = render_translation.find_typography_fonts()
        node = {
            "id": "p1",
            "text": "区块链引用测试（Lee, 2015）。",
            "element": {"kind": "body"},
            "flow_role": "paragraph-start",
        }
        measurement, height, _ = render_translation.fragment_document(
            node, 489.28, fonts
        )
        measurement.close()
        document = pymupdf.open()
        page = document.new_page(width=595.28, height=841.89)
        target = pymupdf.Rect(53.0, 48.5, 542.28, 48.5 + height)
        render_translation.draw_text_fragment(page, node, target, fonts)
        self.assertIn("Lee, 2015", page.get_text())
        self.assertEqual(page.get_links(), [])
        self.assertTrue(
            all(pymupdf.Rect(item[3]).height < 100 for item in page.get_xobjects())
        )
        reader_metrics = check_translation.analyze_reader_compatibility(
            document, 1, 1
        )
        self.assertEqual(reader_metrics["pdf_native_links"], 0)
        self.assertEqual(reader_metrics["oversized_text_forms"], 0)
        self.assertTrue(reader_metrics["searchable_text_layer"])
        self.assertEqual(reader_metrics["citation_text_candidates"], 1)
        self.assertEqual(
            reader_metrics["zotero_smart_reference_status"],
            "runtime-generated-not-static-pdf",
        )
        document.close()

    def test_reader_diagnostic_distinguishes_intrinsic_pdf_links(self) -> None:
        document = pymupdf.open()
        page = document.new_page(width=300, height=300)
        page.insert_text((30, 40), "See (Lee, 2015).")
        page.insert_link(
            {
                "kind": pymupdf.LINK_URI,
                "from": pymupdf.Rect(30, 25, 120, 45),
                "uri": "https://example.com",
            }
        )
        document.reload_page(page)
        reader_metrics = check_translation.analyze_reader_compatibility(
            document, 1, 1
        )
        self.assertEqual(reader_metrics["pdf_native_links"], 1)
        self.assertEqual(reader_metrics["citation_text_candidates"], 1)
        document.close()

    def test_consecutive_source_clips_form_one_visual_envelope(self) -> None:
        first = {
            **self.unit("f1", 1, "figure-text", ""),
            "page": 4,
            "bbox": [100, 400, 200, 450],
            "render_mode": "source_clip",
        }
        second = {
            **self.unit("f2", 2, "table", ""),
            "page": 4,
            "bbox": [180, 420, 300, 500],
            "render_mode": "source_clip",
        }
        nodes = render_translation.build_flow_nodes(
            [first, second], {"f1": "", "f2": ""}
        )
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["ids"], ["f1", "f2"])
        self.assertEqual(nodes[0]["composite_parts"], 2)
        self.assertEqual(
            nodes[0]["element"]["bbox"], [100.0, 400.0, 300.0, 500.0]
        )

    def test_figure_clip_and_display_equation_do_not_merge(self) -> None:
        figure = {
            **self.unit("figure", 1, "figure", ""),
            "page": 4,
            "bbox": [80, 80, 360, 250],
            "render_mode": "source_clip",
        }
        equation = {
            **self.unit("equation", 2, "equation", ""),
            "page": 4,
            "bbox": [140, 270, 300, 290],
            "render_mode": "source_clip",
        }
        nodes = render_translation.build_flow_nodes(
            [figure, equation], {"figure": "", "equation": ""}
        )
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0]["ids"], ["figure"])
        self.assertEqual(nodes[1]["ids"], ["equation"])

    def test_composite_clip_expands_to_overlapping_vector_artwork(self) -> None:
        class SourcePage:
            rect = pymupdf.Rect(0, 0, 612, 792)

            @staticmethod
            def get_drawings() -> list[dict[str, pymupdf.Rect]]:
                return [
                    {"rect": pymupdf.Rect(114, 386, 485, 502)},
                    {"rect": pymupdf.Rect(0, 0, 612, 792)},
                ]

        expanded = render_translation.expand_composite_source_clip(
            SourcePage(),
            pymupdf.Rect(120, 419, 443, 497),
            13,
        )
        self.assertEqual(tuple(expanded), (112.0, 417.0, 487.0, 499.0))

    def mixed_column_boundary_fixture(
        self,
    ) -> tuple[pymupdf.Document, list[dict[str, object]], dict[str, object]]:
        source = pymupdf.open()
        for _ in range(2):
            source.new_page(width=612, height=792)
        records = [
            (
                "title",
                1,
                "front-matter",
                [160, 96, 452, 125],
                "Original title across both columns",
                (190, 115),
            ),
            (
                "abstract",
                1,
                "body",
                [54, 228, 297, 617],
                "Original abstract ends in left column",
                (54, 600),
            ),
            (
                "intro",
                1,
                "heading",
                [54, 630, 150, 643],
                "Introduction",
                (54, 640),
            ),
            (
                "body-left",
                1,
                "body",
                [54, 653, 297, 720],
                "Excluded left introduction body",
                (54, 680),
            ),
            (
                "body-right",
                1,
                "body",
                [315, 228, 558, 720],
                "Excluded right introduction body",
                (315, 250),
            ),
            (
                "conclusion",
                2,
                "body",
                [54, 72, 297, 160],
                "Excluded conclusion before acknowledgments",
                (54, 100),
            ),
            (
                "ack",
                2,
                "heading",
                [54, 173, 297, 186],
                "Acknowledgments",
                (54, 184),
            ),
            (
                "tail-left",
                2,
                "body",
                [54, 197, 297, 720],
                "Original left acknowledgments and references",
                (54, 230),
            ),
            (
                "tail-right",
                2,
                "body",
                [315, 72, 558, 720],
                "Original right references start at page top",
                (315, 85),
            ),
        ]
        elements = []
        for order, (unit_id, page, kind, bbox, text, origin) in enumerate(
            records, 1
        ):
            source[page - 1].insert_text(origin, text, fontsize=9)
            elements.append(
                {
                    **self.unit(unit_id, order, kind, text),
                    "page": page,
                    "bbox": bbox,
                    "translatable": unit_id
                    in {"intro", "body-left", "body-right", "conclusion"},
                }
            )
        manifest = {
            "page_count": 2,
            "pages": [
                {"page": page, "width": 612, "height": 792, "columns": 2}
                for page in (1, 2)
            ],
            "boundary": {
                "introduction_id": "intro",
                "post_body_stop_id": "ack",
            },
        }
        return source, elements, manifest

    def test_native_mixed_columns_preserve_front_and_tail_without_body(
        self,
    ) -> None:
        source, elements, manifest = self.mixed_column_boundary_fixture()
        _, front, tail = render_translation.partition_translation_body(
            elements, manifest
        )
        with tempfile.TemporaryDirectory() as directory:
            body_path = Path(directory) / "body.pdf"
            output_path = Path(directory) / "output.pdf"
            body = pymupdf.open()
            body.new_page().insert_text(
                (72, 72), "Translated body placeholder"
            )
            body.save(body_path)
            body.close()
            start, end, count, placements = native_latex.compose_final_pdf(
                output_path, body_path, source, front, tail
            )
            self.assertEqual((start, end, count), (2, 2, 3))
            output = pymupdf.open(
                stream=output_path.read_bytes(), filetype="pdf"
            )
            front_text = output[0].get_text()
            tail_text = output[2].get_text()
            self.assertIn("Original title across both columns", front_text)
            self.assertIn("Original abstract ends in left column", front_text)
            self.assertNotIn("Excluded", front_text)
            self.assertNotIn("Introduction", front_text)
            self.assertIn("Acknowledgments", tail_text)
            self.assertIn(
                "Original right references start at page top", tail_text
            )
            self.assertNotIn("Excluded", tail_text)
            layout = {
                "boundary_layout": {
                    "profile": "original-front-translated-body-original-tail",
                    "translation_page_start": start,
                    "translation_page_end": end,
                    "segments": placements,
                },
                "placements": [],
            }
            errors, metrics = check_translation.validate_boundary_layout(
                source, output, layout, elements, manifest
            )
            self.assertEqual(errors, [])
            self.assertEqual(metrics["segments_checked"], len(placements))
            broken_layout = copy.deepcopy(layout)
            broken_layout["boundary_layout"]["segments"][0]["source_bbox"] = [
                0,
                0,
                612,
                792,
            ]
            broken_errors, _ = check_translation.validate_boundary_layout(
                source, output, broken_layout, elements, manifest
            )
            self.assertTrue(
                any(
                    "overlaps translated body" in error
                    for error in broken_errors
                )
            )
            missing_layout = copy.deepcopy(layout)
            missing_layout["boundary_layout"]["segments"] = [
                item
                for item in placements
                if not (
                    item["role"] == "tail-original"
                    and item["source_bbox"][0] > 0
                )
            ]
            missing_errors, _ = check_translation.validate_boundary_layout(
                source, output, missing_layout, elements, manifest
            )
            self.assertTrue(
                any(
                    "tail-right is not completely preserved" in error
                    for error in missing_errors
                )
            )
            # Each output boundary page remains vector content at source geometry.
            self.assertFalse(output[0].get_images())
            self.assertFalse(output[2].get_images())
            output.close()
        source.close()

    def test_flow_renderer_keeps_boundary_column_clips_on_one_page(
        self,
    ) -> None:
        source, elements, manifest = self.mixed_column_boundary_fixture()
        _, front, tail = render_translation.partition_translation_body(
            elements, manifest
        )
        renderer = render_translation.FlowRenderer(
            source, 612, 792, 1, 18, mock.Mock(), render_translation.PROFILE
        )
        for index, segment in enumerate(front):
            renderer.place_boundary_segment(segment, start_new_page=False)
        for index, segment in enumerate(tail):
            renderer.place_boundary_segment(segment, start_new_page=index == 0)
        self.assertEqual(len(renderer.document), 2)
        self.assertNotIn("Excluded", renderer.document[0].get_text())
        self.assertNotIn("Excluded", renderer.document[1].get_text())
        self.assertIn(
            "Original title across both columns",
            renderer.document[0].get_text(),
        )
        self.assertIn(
            "Original right references start at page top",
            renderer.document[1].get_text(),
        )
        renderer.document.close()
        source.close()

    def test_boundary_partition_rejects_overlapping_original_and_body(
        self,
    ) -> None:
        source, elements, manifest = self.mixed_column_boundary_fixture()
        elements[1]["bbox"][3] = 640
        with self.assertRaisesRegex(
            ValueError, "Original front and body overlap"
        ):
            render_translation.partition_translation_body(elements, manifest)
        source.close()

    def test_mixed_boundary_pages_are_split_around_translated_body(
        self,
    ) -> None:
        elements = [
            {
                **self.unit("front", 1, "front-matter", "Authors"),
                "page": 1,
            },
            {
                **self.unit("abstract", 2, "front-matter", "Abstract"),
                "page": 2,
                "bbox": [120, 80, 450, 560],
            },
            {
                **self.unit("intro", 3, "heading", "1 Introduction"),
                "page": 2,
                "bbox": [120, 588, 200, 600],
                "translatable": True,
            },
            {
                **self.unit("body", 4, "body", "Body."),
                "page": 3,
                "translatable": True,
            },
            {
                **self.unit("conclusion", 5, "body", "Conclusion."),
                "page": 20,
                "bbox": [120, 80, 450, 340],
                "translatable": True,
            },
            {
                **self.unit("ack", 6, "heading", "Acknowledgement"),
                "page": 20,
                "bbox": [120, 363, 210, 375],
            },
            {
                **self.unit("refs", 7, "body", "References"),
                "page": 21,
            },
        ]
        manifest = {
            "page_count": 24,
            "pages": [
                {"page": page, "width": 595.28, "height": 841.89}
                for page in range(1, 25)
            ],
            "boundary": {
                "introduction_id": "intro",
                "post_body_stop_id": "ack",
            },
        }
        body, front, tail = render_translation.partition_translation_body(
            elements, manifest
        )
        self.assertEqual(
            [item["id"] for item in body], ["intro", "body", "conclusion"]
        )
        self.assertEqual(
            [(item["source_page"], item["mode"]) for item in front],
            [(1, "full-page"), (2, "partial-page")],
        )
        self.assertEqual(front[-1]["source_bbox"], [0.0, 0.0, 595.28, 580.0])
        self.assertEqual(len(tail), 5)
        self.assertEqual(tail[0]["source_page"], 20)
        self.assertEqual(tail[0]["mode"], "partial-page")
        self.assertEqual(tail[0]["source_bbox"][1], 351.0)
        self.assertEqual(tail[0]["target_top"], 42.0)
        self.assertEqual(tail[-1]["source_page"], 24)

    def test_boundary_at_page_top_does_not_create_empty_front_crop(self) -> None:
        elements = [
            {
                **self.unit("front", 1, "front-matter", "Abstract"),
                "page": 1,
            },
            {
                **self.unit("intro", 2, "heading", "1 Introduction"),
                "page": 2,
                "bbox": [60, 90, 180, 105],
                "translatable": True,
            },
        ]
        manifest = {
            "page_count": 2,
            "pages": [
                {"page": page, "width": 516.48, "height": 728.64}
                for page in range(1, 3)
            ],
            "boundary": {
                "introduction_id": "intro",
                "post_body_stop_id": None,
            },
        }
        _, front, tail = render_translation.partition_translation_body(
            elements, manifest
        )
        self.assertEqual(len(front), 1)
        self.assertEqual(front[0]["source_page"], 1)
        self.assertEqual(tail, [])

    def test_visual_layout_keeps_narrow_table_at_typographic_scale(self) -> None:
        document = pymupdf.open()
        page = document.new_page(width=595.276, height=841.89)
        page.insert_text((75, 90), "Body prose", fontsize=10)
        page.insert_text((220, 250), "x  y  z", fontsize=8)
        elements = [
            {
                **self.unit("body", 1, "body", "Body prose"),
                "page": 1,
                "bbox": [72, 78, 520, 95],
                "font_size": 10.0,
            },
            {
                **self.unit("table", 2, "table", ""),
                "page": 1,
                "bbox": [200, 220, 320, 280],
                "render_mode": "source_clip",
                "font_size": 8.0,
            },
            {
                **self.unit("caption", 3, "caption", "Table 6.2: Values"),
                "page": 1,
                "bbox": [210, 290, 350, 305],
                "font_size": 8.0,
            },
        ]
        layout = prepare_paper.build_visual_layout(
            document,
            elements,
            [{"page": 1, "width": 595.276, "height": 841.89}],
            "hash",
        )
        visual = layout["visuals"][0]
        self.assertEqual(layout["status"], "pass")
        self.assertEqual(visual["scale_basis"], "internal-font-ratio")
        self.assertAlmostEqual(visual["target_internal_font_pt"], 9.2, places=1)
        self.assertAlmostEqual(visual["target_width_pt"], 138.0, places=1)
        self.assertLess(visual["target_width_pt"], 489.0 * 0.5)
        document.close()

    def test_visual_layout_falls_back_to_source_width_ratio_with_warning(
        self,
    ) -> None:
        document = pymupdf.open()
        document.new_page(width=600, height=800)
        elements = [
            {
                **self.unit("body", 1, "body", "Body"),
                "page": 1,
                "bbox": [50, 80, 550, 100],
                "font_size": 10.0,
            },
            {
                **self.unit("figure", 2, "figure", ""),
                "page": 1,
                "bbox": [150, 200, 350, 300],
                "render_mode": "source_clip",
            },
            {
                **self.unit("caption", 3, "caption", "Figure 6.1: Curve"),
                "page": 1,
                "bbox": [150, 310, 350, 325],
            },
        ]
        layout = prepare_paper.build_visual_layout(
            document,
            elements,
            [{"page": 1, "width": 600, "height": 800}],
            "hash",
        )
        self.assertEqual(
            layout["visuals"][0]["scale_basis"], "source-width-ratio"
        )
        self.assertTrue(layout["visuals"][0]["automatic_fallback"])
        self.assertEqual(len(layout["warnings"]), 1)
        document.close()

    def test_visual_inventory_rejects_missing_and_duplicate_visuals(self) -> None:
        document = pymupdf.open()
        document.new_page(width=600, height=800)
        elements = [
            {
                **self.unit(f"caption-{index}", index, "caption", "Table 6.3"),
                "page": 1,
                "bbox": [100, 100 + index * 20, 250, 115 + index * 20],
            }
            for index in (1, 2)
        ]
        layout = prepare_paper.build_visual_layout(
            document,
            elements,
            [{"page": 1, "width": 600, "height": 800}],
            "hash",
        )
        self.assertEqual(layout["status"], "fail")
        self.assertTrue(any("occurs 2 times" in item for item in layout["errors"]))
        self.assertTrue(any("no source visual" in item for item in layout["errors"]))
        document.close()

    def test_visual_on_full_original_tail_page_needs_no_separate_placement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            visual_path = root / "visual-layout.json"
            manifest = {
                "schema_version": 5,
                "source_sha256": "hash",
                "paths": {"visual_layout": visual_path.name},
            }
            visual_path.write_text(
                (
                    '{"source_sha256":"hash","errors":[],"warnings":[],'
                    '"visuals":[{"visual_id":"figure-1","page":35}]}'
                ),
                encoding="utf-8",
            )
            output = pymupdf.open()
            output.new_page(width=595.28, height=841.89)
            errors, warnings, metrics = check_translation.validate_visual_layout(
                manifest,
                manifest_path,
                {
                    "visual_layout": {"placements": []},
                    "placements": [],
                    "boundary_layout": {
                        "segments": [
                            {
                                "source_page": 35,
                                "mode": "full-page",
                                "role": "tail-original",
                            }
                        ]
                    },
                },
                output,
            )
            output.close()
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual(metrics["status"], "pass")
        self.assertEqual(metrics["checked"], 1)
        self.assertEqual(metrics["original_boundary_visuals"], 1)

    def test_visual_in_original_column_requires_complete_crop_coverage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "visual-layout.json"
            inventory.write_text(
                json.dumps(
                    {
                        "source_sha256": "hash",
                        "errors": [],
                        "warnings": [],
                        "visuals": [
                            {
                                "visual_id": "figure-tail",
                                "page": 15,
                                "source_bbox": [50, 300, 250, 600],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            manifest = {
                "schema_version": 5,
                "source_sha256": "hash",
                "paths": {"visual_layout": inventory.name},
            }
            segment = {
                "source_page": 15,
                "mode": "partial-page",
                "role": "tail-original",
                "source_bbox": [0, 161, 306, 792],
            }
            layout = {
                "visual_layout": {"placements": []},
                "placements": [],
                "boundary_layout": {"segments": [segment]},
            }
            output = pymupdf.open()
            output.new_page(width=612, height=792)
            errors, _, metrics = check_translation.validate_visual_layout(
                manifest, root / "manifest.json", layout, output
            )
            self.assertEqual(errors, [])
            self.assertEqual(metrics["original_boundary_visuals"], 1)
            segment["source_bbox"][1] = 400
            errors, _, _ = check_translation.validate_visual_layout(
                manifest, root / "manifest.json", layout, output
            )
            self.assertTrue(
                any(
                    "expected one rendered visual" in error for error in errors
                )
            )
            output.close()

    def test_math_fragment_includes_big_o_parentheses_and_is_not_bold(self) -> None:
        source = "Runtime is O(√p)."
        spans = [
            {
                "text": "Runtime is ",
                "font": "Times-Roman",
                "size": 10.0,
                "flags": 0,
                "style": "text",
                "origin": [0, 10],
                "bbox": [0, 0, 55, 12],
                "line": 0,
                "span": 0,
            },
            {
                "text": "√p",
                "font": "CMBX10",
                "size": 10.0,
                "flags": 16,
                "style": "bold",
                "origin": [70, 10],
                "bbox": [70, 0, 82, 12],
                "line": 0,
                "span": 1,
            },
        ]
        _, _, fragments, styles = prepare_paper.build_rich_protection(
            source, spans
        )
        self.assertEqual(
            [item["text"] for item in fragments.values()], ["O(√p)"]
        )
        self.assertEqual(styles, {})

    def test_footnote_anchor_is_linked_and_rendered_at_marker(self) -> None:
        anchor = {
            **self.unit("body", 1, "body", "Claim 1"),
            "page": 1,
            "translatable": True,
            "inline_fragments": {
                "[[Fbody_0001]]": {
                    "kind": "footnote-marker",
                    "text": "1",
                    "token": "[[Fbody_0001]]",
                }
            },
        }
        footnote = {
            **self.unit("fn", 2, "footnote", "1 Note"),
            "page": 1,
            "translatable": True,
            "inline_fragments": {},
        }
        result = prepare_paper.link_footnote_anchors([anchor, footnote])
        self.assertEqual(result["linked"], 1)
        self.assertEqual(footnote["anchor_id"], "body")
        node = {
            "id": "body",
            "text": "论断[[Fbody_0001]]。",
            "element": anchor,
        }
        rendered = native_latex.rich_text_to_latex(
            node,
            {},
            Counter(),
            footnotes_by_id={"fn": "页底注"},
        )
        self.assertIn(
            r"\footnote[1]{\fontsize{8.5pt}{11pt}\selectfont 页底注}",
            rendered,
        )
        self.assertNotIn(r"\textsuperscript", rendered)

    def test_source_review_groups_visual_fragments_idempotently(self) -> None:
        elements = [
            {
                **self.unit("visual-a", 1, "figure", ""),
                "page": 1,
                "bbox": [10, 10, 80, 80],
                "render_mode": "source_clip",
                "translatable": False,
            },
            {
                **self.unit("visual-b", 2, "figure-text", "axis"),
                "page": 1,
                "bbox": [75, 20, 130, 70],
                "render_mode": "source_clip",
                "translatable": False,
            },
            {
                **self.unit("caption", 3, "body", "Figure 2. Results"),
                "page": 1,
                "bbox": [10, 85, 130, 100],
                "translatable": False,
            },
        ]
        review = {
            "schema_version": 1,
            "source_sha256": "source",
            "reviewed": True,
            "reason": "The page preview shows one multi-panel figure.",
            "operations": [
                {
                    "op": "group_visual",
                    "visual_id": "Figure 2",
                    "anchor_id": "visual-a",
                    "member_ids": ["visual-a", "visual-b"],
                    "caption_id": "caption",
                    "reason": "Join the two vector fragments and bind the caption.",
                }
            ],
        }
        page_records = [{"page": 1, "width": 200, "height": 200}]
        first = copy.deepcopy(elements)
        second = copy.deepcopy(elements)
        first_report = source_review.apply_source_review(
            first,
            review,
            "source",
            page_records,
            prepare_paper.protect_element,
        )
        second_report = source_review.apply_source_review(
            second,
            review,
            "source",
            page_records,
            prepare_paper.protect_element,
        )
        self.assertEqual(first, second)
        self.assertEqual(first_report, second_report)
        self.assertEqual(first[0]["bbox"], [10.0, 10.0, 130.0, 80.0])
        self.assertTrue(first[1]["render_suppressed"])
        self.assertEqual(first[2]["kind"], "caption")

    def test_source_review_refuses_to_discard_completed_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            translations = Path(directory) / "translations.jsonl"
            translations.write_text(
                '{"id":"body","translated_text":"已经翻译"}\n',
                encoding="utf-8",
            )
            elements = [
                {
                    **self.unit("body", 1, "body", "Source prose"),
                    "page": 1,
                    "translatable": True,
                }
            ]
            review = {
                "schema_version": 1,
                "source_sha256": "source",
                "reviewed": True,
                "reason": "The source preview classifies this as a plot label.",
                "operations": [
                    {
                        "op": "suppress_units",
                        "ids": ["body"],
                        "reason": "This text belongs inside the plot.",
                    }
                ],
            }
            with self.assertRaises(source_review.SourceReviewError):
                source_review.apply_source_review(
                    elements,
                    review,
                    "source",
                    [{"page": 1, "width": 200, "height": 200}],
                    prepare_paper.protect_element,
                    translations_path=translations,
                )

    def test_source_review_inserts_recovered_prose_and_reconciles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            translations = Path(directory) / "translations.jsonl"
            translations.write_text(
                '{"id":"body","translated_text":"已有译文"}\n',
                encoding="utf-8",
            )
            elements = [
                {
                    **self.unit("body", 1, "body", "Existing prose."),
                    "page": 1,
                    "section": "1 Introduction",
                    "translatable": True,
                }
            ]
            prepare_paper.protect_element(elements[0])
            review = {
                "schema_version": 1,
                "source_sha256": "source",
                "reviewed": True,
                "reason": "The source preview contains a swallowed paragraph.",
                "operations": [
                    {
                        "op": "insert_unit",
                        "id": "review-p0001-u0001",
                        "after_id": "body",
                        "page": 1,
                        "bbox": [10, 30, 180, 60],
                        "kind": "body",
                        "source_text": "Recovered source prose.",
                        "translatable": True,
                        "reason": "Recover exact prose visible below the figure.",
                    }
                ],
            }
            source_review.apply_source_review(
                elements,
                review,
                "source",
                [{"page": 1, "width": 200, "height": 200}],
                prepare_paper.protect_element,
                translations_path=translations,
            )
            source_review.reconcile_translations(translations, elements)
            records = [
                json.loads(line)
                for line in translations.read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual([record["id"] for record in records], [
            "body",
            "review-p0001-u0001",
        ])
        self.assertEqual(records[0]["translated_text"], "已有译文")
        self.assertEqual(records[1]["translated_text"], "")

    def test_source_review_detects_formula_only_body_and_plot_axis(self) -> None:
        elements = [
            {
                **self.unit("formula", 1, "body", "x_i = y_i + z_i"),
                "page": 1,
                "translatable": True,
                "span_runs": [
                    {"text": "x_i = y_i + z_i", "style": "math"}
                ],
            },
            {
                **self.unit("axis", 2, "heading", "10 20 30"),
                "page": 1,
                "bbox": [40, 40, 80, 55],
                "translatable": True,
            },
        ]
        visual_layout = {
            "visuals": [
                {
                    "visual_id": "Figure 3",
                    "page": 1,
                    "source_bbox": [10, 10, 100, 100],
                    "source_ids": ["plot"],
                    "caption_id": "caption",
                }
            ]
        }
        errors, _ = source_review.detect_source_issues(elements, visual_layout)
        self.assertTrue(any("formula-only" in error for error in errors))
        self.assertTrue(any("axis or plot label" in error for error in errors))

    def test_suppressed_plot_equation_is_not_added_to_math_review(self) -> None:
        body = {
            **self.unit("body", 1, "body", "Translated body."),
            "translatable": True,
            "inline_fragments": {},
        }
        axis = {
            **self.unit("axis", 2, "equation", "10 20 30"),
            "render_mode": "source_clip",
            "render_suppressed": True,
            "translatable": False,
            "visual_id": "Figure 1",
        }
        tail = {
            **self.unit("tail", 3, "body", "More translated body."),
            "translatable": True,
            "inline_fragments": {},
        }
        review = prepare_paper.build_math_review(
            [body, axis, tail],
            "source",
        )
        self.assertNotIn("axis", review["entries"])

    def test_reviewed_source_hash_mismatch_is_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source-review.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source_sha256": "old",
                        "reviewed": True,
                        "reason": "Reviewed against the old source.",
                        "operations": [],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(source_review.SourceReviewError):
                source_review.load_source_review(path, "new")

    def test_native_math_normalizes_known_missing_glyph_sequences(self) -> None:
        self.assertEqual(
            native_latex.normalize_native_tex("x⃗ ≫ 𝔫 ↔ y"),
            r"\vec{x} \gg  \mathfrak{n} \leftrightarrow  y",
        )
        self.assertIn(r"\vec{x}", native_latex.latex_escape_text("x⃗"))
        self.assertIn(r"\gg", native_latex.latex_escape_text("≫"))
        self.assertIn(r"\mathfrak{n}", native_latex.latex_escape_text("𝔫"))
        self.assertIn(
            r"\leftrightarrow",
            native_latex.latex_escape_text("↔"),
        )

    def test_native_display_math_rejects_tag_with_actionable_numbering(self) -> None:
        node = {
            "id": "equation-4",
            "element": {"kind": "equation"},
        }
        reviews = {
            "equation-4": {
                "review_status": "manually_reviewed",
                "tex": r"x=y\tag{4}",
            }
        }
        with self.assertRaisesRegex(
            native_latex.MathReviewError,
            r"\\qquad\(n\)",
        ):
            native_latex.resolve_display_math(node, reviews)

    def test_native_prose_reports_unsupported_unicode_with_unit_id(self) -> None:
        node = {
            "id": "body-unsupported",
            "text": "使用数学字符 𝕏。",
            "element": {
                "inline_fragments": {},
                "style_tokens": {},
            },
        }
        with self.assertRaisesRegex(ValueError, "body-unsupported"):
            native_latex.rich_text_to_latex(node, {}, Counter())

    @staticmethod
    def unit(
        unit_id: str, order: int, kind: str, source_text: str
    ) -> dict[str, object]:
        return {
            "id": unit_id,
            "order": order,
            "page": order,
            "bbox": [0, 0, 100, 20],
            "kind": kind,
            "render_mode": "text",
            "source_text": source_text,
        }


if __name__ == "__main__":
    unittest.main()
