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

import pymupdf

import native_latex
from typography import ZH_ACADEMIC_V1


class NativeSpecialGlyphTests(unittest.TestCase):
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
