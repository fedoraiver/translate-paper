#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pymupdf>=1.26.0,<2",
#   "pillow>=10.4.0,<13",
# ]
# ///

from __future__ import annotations

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
        self.assertEqual(markdown, "$c = c^{α}$")

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
        self.assertRegex(
            table_chunk,
            r"\\Needspace\{\d+\.\d{2}pt\}\s+\\par\\smallskip",
        )
        needspace = native_latex._caption_clip_needspace(
            source, table, render_translation.PROFILE
        )
        self.assertGreater(needspace, 340.0)
        source.close()

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

    def test_mixed_boundary_pages_are_split_around_translated_body(self) -> None:
        elements = [
            {
                **self.unit("front", 1, "front-matter", "Authors"),
                "page": 1,
            },
            {
                **self.unit("intro", 2, "heading", "1 Introduction"),
                "page": 2,
                "bbox": [120, 588, 200, 600],
                "translatable": True,
            },
            {
                **self.unit("body", 3, "body", "Body."),
                "page": 3,
                "translatable": True,
            },
            {
                **self.unit("ack", 4, "heading", "Acknowledgement"),
                "page": 20,
                "bbox": [120, 363, 210, 375],
            },
            {
                **self.unit("refs", 5, "body", "References"),
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
        self.assertEqual([item["id"] for item in body], ["intro", "body"])
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
