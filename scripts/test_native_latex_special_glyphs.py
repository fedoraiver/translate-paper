#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2"]
# ///
"""Literal percent and enclosed-number rendering regressions."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from collections import Counter

import pymupdf

import native_latex
import render_translation
from typography import ZH_ACADEMIC_V1


class NativeSpecialGlyphTests(unittest.TestCase):
    def test_sentence_within_bullet_stays_with_item_before_next_bullet(self):
        element = {"id": "list", "order": 1, "page": 1, "bbox": [0, 0, 400, 100],
                   "kind": "body", "render_mode": "text", "translatable": True,
                   "source_text": "• Config.\nIn particular details;\n• Disabled."}
        node = render_translation.build_flow_nodes(
            [element], {"list": "• 配置。\n特别地，配置更新；\n• 禁用。"})[0]
        content, _ = native_latex._node_to_latex(node, {}, Counter(), None, Path("."), ZH_ACADEMIC_V1, {})
        self.assertEqual(content.count(r"\hangindent"), 2, content)
        self.assertNotIn(r"\par 特别地", content)

    def test_prose_after_finished_bullet_is_normal_paragraph(self):
        element = {"id": "list", "order": 1, "page": 1, "bbox": [0, 0, 400, 100],
                   "kind": "body", "render_mode": "text", "translatable": True,
                   "source_text": "• Item.\nNext paragraph."}
        node = render_translation.build_flow_nodes(
            [element], {"list": "• 一项。\n随后继续论证。"})[0]
        content, _ = native_latex._node_to_latex(node, {}, Counter(), None, Path("."), ZH_ACADEMIC_V1, {})
        self.assertIn("\\par 随后继续论证。\\par", content)
        self.assertEqual(content.count(r"\hangindent"), 1)

    def test_item_after_extracted_equation_number_starts_new_paragraph(self):
        fragments = {key: {"kind": "math", "text": label, "tex": label,
                           "review_status": "auto_validated"}
                     for key, label in [("[[Fone]]", "1."), ("[[Feq]]", "x=y"),
                                        ("[[Fnum]]", "(39)"), ("[[Ftwo]]", "2.")]}
        element = {"id": "list", "order": 1, "page": 1, "bbox": [0, 0, 400, 100],
                   "kind": "body", "render_mode": "text", "translatable": True,
                   "source_text": "1. First equation (39) 2. Second",
                   "inline_fragments": fragments}
        text = "[[Fone]] First\n[[Feq]] [[Fnum]] [[Ftwo]] Second"
        node = render_translation.build_flow_nodes([element], {"list": text})[0]
        content, _ = native_latex._node_to_latex(node, {}, Counter(), None, Path("."), ZH_ACADEMIC_V1, {})
        self.assertEqual(content.count(r"\par\noindent"), 2, content)
        self.assertIn(r"\((39)\)\par", content)

    def test_list_boundaries_survive_full_flow_normalization(self):
        for text, fragments, count in [
            ("• One\n• Two", {}, 2),
            ("[[Fone]] One\n[[Ftwo]] Two", {
                "[[Fone]]": {"kind": "math", "text": "1.", "tex": "1.",
                             "review_status": "auto_validated"},
                "[[Ftwo]]": {"kind": "math", "text": "2.", "tex": "2.",
                             "review_status": "auto_validated"}}, 2),
        ]:
            element = {"id": "list", "order": 1, "page": 1,
                       "bbox": [50, 50, 400, 100], "kind": "body",
                       "render_mode": "text", "translatable": True,
                       "source_text": "1. One\n2. Two", "inline_fragments": fragments}
            nodes = render_translation.build_flow_nodes([element], {"list": text})
            content, _ = native_latex._node_to_latex(
                nodes[0], {}, Counter(), None, Path("."), ZH_ACADEMIC_V1, {})
            self.assertEqual(content.count(r"\par\noindent"), count, content)

    def test_list_items_start_separate_paragraphs_including_tokenized_numbers(self):
        element = {"inline_fragments": {"[[Ftwo]]": {
            "kind": "math", "text": "2.", "tex": "2.",
            "review_status": "auto_validated"}}}
        node = {"id": "list", "flow_role": "list-item", "element": element,
                "text": "• Parent\n• Next\n‣ Nested\n[[Ftwo]]Numbered"}
        content, _ = native_latex._node_to_latex(
            node, {}, Counter(), None, Path("."), ZH_ACADEMIC_V1, {})
        self.assertEqual(content.count(r"\par\noindent"), 4)
        self.assertIn(r"\hspace*{23pt}\hangindent=46pt", content)
        self.assertIn(r"\(2.\)Numbered", content)

    @unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX is unavailable")
    def test_nested_triangular_bullet_is_preserved_without_missing_glyphs(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            node = {"id": "list", "flow_role": "list-item", "element": {},
                    "text": "• Parent\n‣ Child"}
            content, _ = native_latex._node_to_latex(
                node, {}, Counter(), None, work, ZH_ACADEMIC_V1, {})
            self.assertIn(r"\hangindent=23pt", content)
            self.assertIn(r"\TPTextSymbol{‣}", content)
            path = work / "nested-list.tex"
            path.write_text(native_latex._document_preamble(ZH_ACADEMIC_V1)
                            + content + r"\end{document}", encoding="utf-8")
            pdf, metrics = native_latex.compile_body_tex(work, path)
            self.assertEqual(metrics["missing_glyphs"], 0)
            with pymupdf.open(pdf) as document:
                text = "".join(page.get_text() for page in document)
                self.assertIn("‣", text)
                self.assertIn("Child", text)

    def test_url_path_is_not_reinterpreted_as_automatic_mathematics(self):
        url = "https://example.test/a1/ui/b2?x=1&y=2#part"
        self.assertEqual(native_latex.prose_to_latex(url, allow_auto_math=True),
                         native_latex.prose_to_latex(url))
        self.assertEqual(native_latex.prose_to_latex("ordinary a1", allow_auto_math=True),
                         r"ordinary \(a_{1}\)")

    @unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX is unavailable")
    def test_long_protected_urls_wrap_as_searchable_text_without_links_or_hyphens(self):
        urls = [
            "https://example.test/" + "longunbrokensegment" * 6 + "/paper.pdf",
            "https://example.test/a1/file_name%20part?q=value&other=two#anchor",
        ]
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            tex = native_latex._document_preamble(ZH_ACADEMIC_V1)
            tex += r"\pagestyle{empty}" + "\n"
            for index, url in enumerate(urls):
                unit = {"id": str(index), "protected_tokens": {"[[P0001]]": url}}
                text = render_translation.restore_tokens(unit, "[[P0001]]")
                node = {"id": str(index), "element": unit, "text": text}
                content = native_latex.rich_text_to_latex(node, {}, Counter())
                tex += r"\noindent\begin{minipage}{170pt}" + content + r"\end{minipage}\par" + "\n"
            path = work / "urls.tex"
            path.write_text(tex + r"\end{document}", encoding="utf-8")
            pdf, metrics = native_latex.compile_body_tex(work, path)
            self.assertEqual(metrics["overfull_hboxes"], 0)
            with pymupdf.open(pdf) as document:
                compact = "".join("".join(page.get_text() for page in document).split())
                self.assertEqual(compact, "".join(urls))
                self.assertFalse(any(page.get_links() for page in document))
                self.assertFalse(any(page.get_images() for page in document))
                for page in document:
                    spans = [span for block in page.get_text("dict")["blocks"]
                             if block["type"] == 0 for line in block["lines"] for span in line["spans"]]
                    self.assertTrue(all("LMRoman" in span["font"] for span in spans))

    @unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX is unavailable")
    def test_exercise_arrow_and_open_proof_square_render_as_native_text(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            content = native_latex.latex_escape_text("Exercise ★ ↪ Proof □")
            path = work / "exercise-symbols.tex"
            path.write_text(
                native_latex._document_preamble(ZH_ACADEMIC_V1)
                + content + "\n" + r"\end{document}", encoding="utf-8"
            )
            pdf, metrics = native_latex.compile_body_tex(work, path)
            self.assertEqual(metrics["missing_glyphs"], 0)
            with pymupdf.open(pdf) as document:
                text = "".join(page.get_text() for page in document)
                self.assertIn("↪", text)
                self.assertIn("□", text)
                self.assertFalse(any(page.get_images() for page in document))

    def test_native_percentage_is_escaped_once(self):
        for raw, wanted in (("10%", r"10\%"), (r"10\%", r"10\%"),
                            (r"10\\%", r"10\\\%")):
            with self.subTest(raw=raw):
                normalized = native_latex.normalize_native_tex(raw)
                self.assertEqual(normalized, wanted)
                self.assertEqual(native_latex.normalize_native_tex(normalized), wanted)

    def test_percentage_fragment_keeps_a_literal_percent(self):
        tex, status = native_latex.resolve_inline_math({
            "token": "[[Fpercentage]]", "kind": "math", "tex": "10%",
            "review_status": "auto_validated",
        }, {})
        self.assertEqual(tex, r"10\%")
        self.assertEqual(status, "auto_validated")

    def test_enclosed_numbers_use_symbol_font_without_changing_text(self):
        self.assertEqual(
            native_latex.latex_escape_text("①②③④⑳▲"),
            "".join(r"\TPTextSymbol{" + c + "}" for c in "①②③④⑳▲"),
        )

    @unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX is unavailable")
    def test_compile_percentage_and_italic_enclosed_numbers(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            percent = native_latex.normalize_native_tex("10%")
            numbers = native_latex.latex_escape_text("①②③④⑳▲")
            tex = native_latex._document_preamble(ZH_ACADEMIC_V1)
            tex += "Ratio \\(" + percent + "\\) remains literal.\\par\n"
            tex += r"{\itshape " + numbers + r"}\par" + "\n"
            tex += r"{\bfseries " + numbers + r"}\par" + "\n"
            tex += r"\end{document}" + "\n"
            path = work / "special-glyphs.tex"
            path.write_text(tex, encoding="utf-8")
            pdf, metrics = native_latex.compile_body_tex(work, path)
            self.assertEqual(metrics["missing_glyphs"], 0)
            with pymupdf.open(pdf) as document:
                text = "".join(page.get_text() for page in document)
            compact = "".join(text.split())
            self.assertIn("10%", compact)
            self.assertEqual(compact.count("①②③④⑳▲"), 2)


if __name__ == "__main__":
    unittest.main()
