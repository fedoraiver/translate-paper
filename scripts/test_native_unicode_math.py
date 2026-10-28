#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2"]
# ///
"""Unicode mathematical alphabets retain style through native rendering."""

import shutil
import tempfile
import unittest
from pathlib import Path

import pymupdf

import native_latex
from typography import ZH_ACADEMIC_V1


class NativeUnicodeMathTests(unittest.TestCase):
    def test_supported_alphabets_are_not_flattened_or_rejected(self):
        tex = "𝐀+𝐴+𝑥+𝒜+𝔇+𝔽+𝖠+𝗯+𝘊+𝙙+𝚎+𝟙+𝜎"
        self.assertEqual(native_latex.normalize_native_tex(tex), tex)
        native_latex.validate_native_tex_compatibility(tex, "alphabet-regression")
        resolved, status = native_latex.resolve_inline_math({
            "token": "[[Falphabet]]", "kind": "math", "tex": tex,
            "review_status": "auto_validated",
        }, {})
        self.assertEqual((resolved, status), (tex, "auto_validated"))

    def test_chancery_variants_become_explicit_calligraphic_tex(self):
        source = "𝒜\ufe00+𝒞\ufe00+𝒮\ufe00+𝒱\ufe00+ℳ\ufe00"
        expected = r"\mathcal{A}+\mathcal{C}+\mathcal{S}+\mathcal{V}+\mathcal{M}"
        normalized = native_latex.normalize_native_tex(source)
        self.assertEqual(normalized, expected)
        self.assertEqual(native_latex.normalize_native_tex(normalized), expected)
        native_latex.validate_native_tex_compatibility(normalized, "chancery-regression")

    def test_unhandled_variations_and_combining_marks_remain_blocking(self):
        for tex in ("𝒜\ufe01", "x\ufe00", "x\u20e1"):
            with self.subTest(tex=tex), self.assertRaises(native_latex.MathReviewError):
                native_latex.validate_native_tex_compatibility(
                    native_latex.normalize_native_tex(tex), "unhandled-regression")

    @unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX is unavailable")
    def test_styled_letters_and_chancery_compile_as_native_searchable_math(self):
        source = "𝐀+𝐴+𝔽+𝜎+𝒜\ufe00+ℳ\ufe00"
        normalized = native_latex.normalize_native_tex(source)
        native_latex.validate_native_tex_compatibility(normalized, "compile-regression")
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            path = work / "unicode-math.tex"
            path.write_text(
                native_latex._document_preamble(ZH_ACADEMIC_V1)
                + r"\TPDisplayMath{" + normalized + "}\n" + r"\end{document}",
                encoding="utf-8",
            )
            pdf, metrics = native_latex.compile_body_tex(work, path)
            self.assertEqual(metrics["missing_glyphs"], 0)
            with pymupdf.open(pdf) as document:
                page = document[0]
                text = page.get_text()
                for letter in ("𝐀", "𝐴", "𝔽", "𝒜"):
                    self.assertIn(letter, text)
                self.assertFalse(page.get_images())
                self.assertFalse(page.get_xobjects())
                math_fonts = [font for font in page.get_fonts() if "Math" in font[3]]
                self.assertTrue(math_fonts)
                self.assertTrue(all(document.extract_font(font[0])[3] for font in math_fonts))


if __name__ == "__main__":
    unittest.main()
