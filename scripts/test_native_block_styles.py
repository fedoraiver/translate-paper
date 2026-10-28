#!/usr/bin/env python3
"""Whole-paragraph emphasis survives native rendering and flow merging."""

from collections import Counter
from pathlib import Path
import shutil
import tempfile
import unittest

import pymupdf

import native_latex
import render_translation
from typography import ZH_ACADEMIC_V1


class NativeBlockStyleTests(unittest.TestCase):
    def element(self, unit_id, order, source, style="normal", page=1):
        return {"id": unit_id, "order": order, "page": page,
                "bbox": [50, 50, 450, 100], "kind": "body",
                "render_mode": "text", "translatable": True,
                "section": "Results", "source_text": source,
                "block_style": style, "inline_fragments": {}, "style_tokens": {}}

    def theorem_nodes(self):
        theorem = self.element("statement", 1, "Label. Assume the result.", "italic")
        theorem["style_tokens"] = {"Blabel": "bold"}
        theorem["inline_fragments"] = {"[[Fvalue]]": {
            "token": "[[Fvalue]]", "kind": "math", "tex": "x_i",
            "review_status": "manually_reviewed"}}
        normal = self.element("normal", 2, "Following paragraph.")
        return render_translation.build_flow_nodes([theorem, normal], {
            "statement": "[[Blabel_OPEN]]Label[[Blabel_CLOSE]] Assume [[Fvalue]] holds.",
            "normal": "Following paragraph.",
        })

    def test_block_italic_preserves_upright_bold_label_and_native_math(self):
        nodes = self.theorem_nodes()
        statuses = Counter()
        rendered = native_latex.rich_text_to_latex(nodes[0], {}, statuses)
        self.assertIn(r"\itshape", rendered)
        self.assertIn(r"\sffamily\bfseries\upshape Label", rendered)
        self.assertIn(r"\(x_i\)", rendered)
        self.assertEqual(statuses, Counter(manually_reviewed=1))
        self.assertEqual(native_latex.rich_text_to_latex(nodes[1], {}, Counter()),
                         "Following paragraph.")

    def test_merged_continuation_keeps_style_transitions_without_new_paragraph(self):
        elements = [self.element("first", 1, "An unfinished", "italic"),
                    self.element("next", 2, "continuation", page=2),
                    self.element("last", 3, "ends here.", "italic", page=3)]
        nodes = render_translation.build_flow_nodes(elements, {
            "first": "Italic prefix", "next": "upright middle", "last": "italic ending.",
        })
        self.assertEqual(len(nodes), 1)
        rendered = native_latex.rich_text_to_latex(nodes[0], {}, Counter())
        self.assertIn(r"{\itshape Italic prefix }upright middle {\itshape italic ending.}", rendered)
        self.assertNotIn(r"\par", rendered)

    def test_merged_hyphenated_text_retains_exact_join(self):
        elements = [self.element("first", 1, "A multi-", "italic"),
                    self.element("next", 2, "party result.", page=2)]
        node = render_translation.build_flow_nodes(elements, {
            "first": "multi-", "next": "party result.",
        })[0]
        self.assertEqual(node["text"], "multiparty result.")
        self.assertEqual(native_latex.rich_text_to_latex(node, {}, Counter()),
                         r"{\itshape multi}party result.")

    def column_pair(self, ending="including many"):
        first = self.element("left", 1, ending, "italic")
        first["bbox"] = [54, 665, 297, 720]
        second = self.element("right", 2, "other systems.")
        second["bbox"] = [315, 217, 558, 261]
        return first, second

    def test_cross_column_continuation_keeps_style_without_repeated_indent(self):
        first, second = self.column_pair()
        # A top-of-column continuation may occupy only one short line.
        for bbox in ([315, 217, 558, 261], [315, 71, 396, 81]):
            with self.subTest(bbox=bbox):
                second["bbox"] = bbox
                nodes = render_translation.build_flow_nodes([first, second], {
                    "left": "以及许多", "right": "其他证明系统。"})
                self.assertEqual(len(nodes), 1)
                self.assertEqual(nodes[0]["ids"], ["left", "right"])
                self.assertEqual(nodes[0]["joined_continuations"], 1)
                rendered = native_latex.rich_text_to_latex(nodes[0], {}, Counter())
                self.assertEqual(rendered, r"{\itshape 以及许多}其他证明系统。")
                self.assertNotIn(r"\par", rendered)

    def test_column_jump_does_not_merge_completed_paragraphs_or_proof_endings(self):
        for ending in ("Complete sentence.", "Finished.2", "Proof □", "Proof ∎", "Proof ⊓⊔"):
            with self.subTest(ending=ending):
                first, second = self.column_pair(ending)
                first["inline_fragments"] = {"[[Fnote]]": {
                    "kind": "footnote-marker", "footnote_id": "note", "text": "2"}}
                nodes = render_translation.build_flow_nodes([first, second], {
                    "left": "前段。", "right": "后段。"})
                self.assertEqual(len(nodes), 2)

    def test_column_jump_preserves_section_list_heading_and_clip_boundaries(self):
        for change in ({"kind": "heading"}, {"section": "Other"},
                       {"source_text": "• List item"}, {"translatable": False},
                       {"render_mode": "source_clip"}):
            with self.subTest(change=change):
                first, second = self.column_pair()
                second.update(change)
                nodes = render_translation.build_flow_nodes([first, second], {
                    "left": "前半", "right": "后半。"})
                self.assertEqual(len(nodes), 2)
        first, second = self.column_pair()
        figure = {**first, "id": "figure", "order": 2,
                  "kind": "figure", "render_mode": "source_clip"}
        second["order"] = 3
        nodes = render_translation.build_flow_nodes([first, figure, second], {
            "left": "前半", "right": "后半。"})
        self.assertEqual(len(nodes), 3)

    def test_unrelated_side_by_side_or_distant_boxes_do_not_join(self):
        for bbox in ([315, 665, 558, 720], [315, 725, 558, 760],
                     [600, 71, 840, 100], [280, 71, 523, 100]):
            with self.subTest(bbox=bbox):
                first, second = self.column_pair()
                second["bbox"] = bbox
                nodes = render_translation.build_flow_nodes([first, second], {
                    "left": "前半", "right": "后半。"})
                self.assertEqual(len(nodes), 2)

    def test_new_bold_definition_label_starts_paragraph_after_colon(self):
        first, second = self.column_pair("The properties are:")
        second["source_text"] = "Sequencing. The order matters."
        second["protected_text"] = "[[Blabel_OPEN]]Sequencing.[[Blabel_CLOSE]] The order matters."
        second["style_tokens"] = {"Blabel": "bold"}
        for bbox in ([54, 725, 297, 760], [315, 71, 558, 110]):
            with self.subTest(bbox=bbox):
                second["bbox"] = bbox
                nodes = render_translation.build_flow_nodes([first, second], {
                    "left": "其性质如下：",
                    "right": "[[Blabel_OPEN]]顺序。[[Blabel_CLOSE]]顺序很重要。"})
                self.assertEqual(len(nodes), 2)
                self.assertEqual(nodes[1]["flow_role"], "paragraph-start")

    def test_standalone_bold_term_starts_normal_paragraph(self):
        first = self.element("intro", 1, "The drawbacks are:")
        first["bbox"] = [34.5, 578.25, 544.751, 609.75]
        first["font_size"] = 12.0
        for page in (1, 2):
            with self.subTest(page=page):
                term = self.element("term", 2, "Complexity", page=page)
                term["bbox"] = [34.5, 630.798, 96.984, 642.822]
                term["font_size"] = 12.0
                term["protected_text"] = " [[Bterm_OPEN]]Complexity[[Bterm_CLOSE]] \n"
                term["style_tokens"] = {"Bterm": "bold"}
                nodes = render_translation.build_flow_nodes([first, term], {
                    "intro": "其缺点如下：",
                    "term": "[[Bterm_OPEN]]复杂性[[Bterm_CLOSE]]"})
                self.assertEqual(len(nodes), 2)
                self.assertEqual(nodes[1]["flow_role"], "paragraph-start")
                self.assertEqual(nodes[1]["ids"], ["term"])

    def test_leading_bold_emphasis_without_label_punctuation_still_continues(self):
        first, second = self.column_pair()
        second["protected_text"] = "[[Bterm_OPEN]]other[[Bterm_CLOSE]] systems."
        second["style_tokens"] = {"Bterm": "bold"}
        nodes = render_translation.build_flow_nodes([first, second], {
            "left": "许多", "right": "[[Bterm_OPEN]]其他[[Bterm_CLOSE]]系统。"})
        self.assertEqual(len(nodes), 1)

    def test_unfinished_list_item_continues_across_column_with_hanging_indent(self):
        first, second = self.column_pair("• Execution: perform the operation,")
        nodes = render_translation.build_flow_nodes([first, second], {
            "left": "• 执行操作，", "right": "并记录结果。"})
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["flow_role"], "list-item")
        self.assertEqual(nodes[0]["ids"], ["left", "right"])
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            source.new_page()
            tex, _, _ = native_latex.build_body_tex(
                nodes, {}, ZH_ACADEMIC_V1, source, Path(directory))
        self.assertEqual(tex.count(r"\hangindent=23pt\hangafter=1"), 1)

    def test_column_continuation_skips_anchored_footnote(self):
        first, second = self.column_pair()
        note = {**first, "id": "note", "order": 2,
                "kind": "footnote", "anchor_id": "left"}
        second["order"] = 3
        nodes = render_translation.build_flow_nodes([first, note, second], {
            "left": "前半", "note": "脚注。", "right": "后半。"})
        self.assertEqual(nodes[0]["ids"], ["left", "right"])
        self.assertEqual(nodes[1]["id"], "note")

    def interrupted_page_elements(self):
        first = self.element("before", 1, "We repeatedly")
        note = {**self.element("note", 2, "A note."),
                "kind": "footnote", "anchor_id": "before", "footnote_number": "1"}
        figure = {**self.element("figure", 3, "Figure contents", page=2),
                  "kind": "figure", "render_mode": "source_clip", "translatable": False,
                  "visual_id": "figure-1", "bbox": [50, 50, 450, 120]}
        caption = {**figure, "id": "caption", "order": 4, "kind": "caption",
                   "bbox": [50, 125, 450, 145]}
        continuation = self.element("after", 5, "use these analyses for x.", "italic", page=2)
        continuation["bbox"] = [50, 160, 450, 190]
        continuation["inline_fragments"] = {"[[Fx]]": {
            "kind": "math", "tex": "x_i", "review_status": "manually_reviewed"}}
        continuation["style_tokens"] = {"Bterm": "bold"}
        following = self.element("new", 6, "New paragraph.", page=2)
        return [first, note, figure, caption, continuation, following]

    def interrupted_page_nodes(self, elements=None):
        return render_translation.build_flow_nodes(elements or self.interrupted_page_elements(), {
            "before": "We repeatedly", "note": "1. A note.",
            "after": "use [[Bterm_OPEN]]these[[Bterm_CLOSE]] analyses for [[Fx]].",
            "new": "New paragraph.",
        })

    def test_page_continuation_after_visual_stays_in_order_without_indent(self):
        nodes = self.interrupted_page_nodes()
        self.assertEqual([n["id"] for n in nodes], ["before", "note", "figure", "after", "new"])
        continuation = nodes[3]
        self.assertEqual(continuation["flow_role"], "paragraph-continuation")
        self.assertEqual(continuation["continuation_of"], "before")
        self.assertEqual(continuation["continuation_across_visual_ids"], ["figure", "caption"])
        self.assertEqual(continuation["ids"], ["after"])
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            source.new_page()
            source.new_page().insert_text((60, 80), "Figure contents")
            tex, placements, _ = native_latex.build_body_tex(
                nodes, {}, ZH_ACADEMIC_V1, source, Path(directory))
        chunk = tex.split("% TP-NODE after\n", 1)[1].split("% TP-NODE", 1)[0]
        self.assertTrue(chunk.startswith(r"\par\noindent "), chunk)
        self.assertIn(r"\itshape", chunk)
        self.assertIn(r"\sffamily\bfseries\upshape these", chunk)
        self.assertIn(r"\(x_i\)", chunk)
        self.assertEqual(placements[3]["continuation_of"], "before")
        self.assertEqual(placements[3]["text_indent"], 0)
        following = tex.split("% TP-NODE new\n", 1)[1].split("% TP-NODE", 1)[0]
        self.assertNotIn(r"\noindent", following)

    def test_visual_interruption_does_not_cross_real_paragraph_boundaries(self):
        for change in ("sentence-end", "proof-end", "list-before", "list-after", "heading",
                       "section", "indented-source", "bold-label", "unanchored-note", "equation"):
            with self.subTest(change=change):
                elements = self.interrupted_page_elements()
                if change == "sentence-end": elements[0]["source_text"] += "."
                elif change == "proof-end": elements[0]["source_text"] += " ⊓⊔"
                elif change == "list-before": elements[0]["source_text"] = "• " + elements[0]["source_text"]
                elif change == "list-after": elements[4]["source_text"] = "• " + elements[4]["source_text"]
                elif change == "heading": elements[1]["kind"] = "heading"
                elif change == "section": elements[4]["section"] = "Another section"
                elif change == "indented-source": elements[4]["span_runs"] = [{"text": "use", "bbox": [65, 160, 80, 170]}]
                elif change == "bold-label": elements[4]["protected_text"] = "[[Blabel_OPEN]]Definition.[[Blabel_CLOSE]] Text."
                elif change == "unanchored-note": del elements[1]["anchor_id"]
                elif change == "equation": elements[2]["kind"] = "equation"
                nodes = self.interrupted_page_nodes(elements)
                after = next(n for n in nodes if "after" in n["ids"])
                self.assertNotEqual(after["flow_role"], "paragraph-continuation")
                self.assertNotIn("continuation_of", after)

    @unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX is unavailable")
    def test_pdf_interrupted_continuation_is_flush_left_and_next_paragraph_indented(self):
        nodes = self.interrupted_page_nodes()
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            work = Path(directory)
            source.new_page()
            source.new_page().insert_text((60, 80), "Figure contents")
            tex, _, _ = native_latex.build_body_tex(nodes, {}, ZH_ACADEMIC_V1, source, work)
            path = work / "continuation.tex"
            path.write_text(tex, encoding="utf-8")
            pdf, metrics = native_latex.compile_body_tex(work, path)
            self.assertEqual(metrics["missing_glyphs"], 0)
            with pymupdf.open(pdf) as document:
                locations = {word: next(rect for page in document for rect in page.search_for(word))
                             for word in ("use", "New paragraph.")}
                self.assertAlmostEqual(locations["use"].x0, ZH_ACADEMIC_V1.margin_left, delta=0.6)
                self.assertAlmostEqual(locations["New paragraph."].x0 - locations["use"].x0, 23, delta=0.6)

    def test_same_style_and_chinese_continuations_keep_their_original_join(self):
        for second_style, expected in (
            ("italic", r"{\itshape 前半后半。}"),
            ("normal", r"{\itshape 前半}后半。"),
        ):
            with self.subTest(second_style=second_style):
                elements = [self.element("first", 1, "An unfinished", "italic"),
                            self.element("next", 2, "ending.", second_style, page=2)]
                node = render_translation.build_flow_nodes(elements, {
                    "first": "前半", "next": "后半。",
                })[0]
                self.assertEqual(node["text"], "前半后半。")
                self.assertEqual(native_latex.rich_text_to_latex(node, {}, Counter()), expected)

    @unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX is unavailable")
    def test_pdf_body_is_italic_label_and_following_paragraph_are_upright(self):
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            work = Path(directory)
            source.new_page()
            tex, _, _ = native_latex.build_body_tex(
                self.theorem_nodes(), {}, ZH_ACADEMIC_V1, source, work)
            path = work / "styles.tex"
            path.write_text(tex, encoding="utf-8")
            pdf, metrics = native_latex.compile_body_tex(work, path)
            self.assertEqual(metrics["missing_glyphs"], 0)
            with pymupdf.open(pdf) as document:
                spans = [span for page in document for block in page.get_text("dict")["blocks"]
                         for line in block.get("lines", []) for span in line["spans"]]
                for word, italic in (("Assume", True), ("Label", False), ("Following", False)):
                    fonts = [span["font"].lower() for span in spans if word in span["text"]]
                    self.assertTrue(fonts, word)
                    self.assertTrue(all(("italic" in f or "slanted" in f) == italic for f in fonts),
                                    (word, fonts))
                self.assertTrue(any("latinmodernmath" in s["font"].lower() for s in spans))


if __name__ == "__main__":
    unittest.main()
