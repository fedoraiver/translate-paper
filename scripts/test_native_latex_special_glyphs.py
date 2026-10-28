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
