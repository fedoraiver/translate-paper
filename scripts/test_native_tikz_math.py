#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2"]
# ///
"""Reviewed math diagrams stay native paths and searchable mathematical text."""

import shutil
import tempfile
import unicodedata
import unittest
from pathlib import Path

import pymupdf

import native_latex
from typography import ZH_ACADEMIC_V1


DIAGRAM = r"""\vcenter{\hbox{\begingroup
\definecolor{DiagramColor}{rgb}{0.5,0.1,0.1}
\begin{tikzpicture}[x=1pt,y=-1pt,scale=1.05,transform shape]
\path[use as bounding box] (0,0) rectangle (120,60);
\path[draw=DiagramColor,line width=0.5pt,dash pattern=on 3pt off 3pt]
  (15,20) -- (100,20);
\path[draw=DiagramColor] (100,20) .. controls (98,19) and (97,18) .. (96,17);
\node[anchor=base west,inner sep=0pt,outer sep=0pt,
  font=\fontsize{10pt}{12pt}\selectfont] at (0,24) {$\Gamma$};
\node[anchor=base west,inner sep=0pt,outer sep=0pt,
  font=\fontsize{10pt}{12pt}\selectfont] at (100,24) {$\partial\Gamma$};
\node[anchor=base west,inner sep=0pt,outer sep=0pt,
  font=\fontsize{10pt}{12pt}\selectfont] at (45,14) {$f_1$};
\end{tikzpicture}\endgroup}}"""


class NativeTikzMathTests(unittest.TestCase):
    def test_native_preamble_loads_tikz_for_reviewed_math_diagrams(self):
        self.assertIn(r"\usepackage{tikz}", native_latex._document_preamble(ZH_ACADEMIC_V1))

    @unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX is unavailable")
    def test_reviewed_diagram_compiles_without_image_or_form_fallback(self):
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            source.new_page()
            unit = {"id": "diagram", "kind": "equation", "page": 1,
                    "bbox": [0, 0, 120, 60], "render_mode": "source_clip"}
            node = {"id": "diagram", "element": unit, "render_mode": "source_clip"}
            reviews = {"diagram": {"tex": DIAGRAM, "review_status": "manually_reviewed"}}
            work = Path(directory)
            tex, placements, _ = native_latex.build_body_tex(
                [node], reviews, ZH_ACADEMIC_V1, source, work)
            self.assertEqual(placements[0]["render_mode"], "native-math")
            self.assertEqual(placements[0]["text_embedding"], "native-lualatex")
            self.assertNotIn(r"\includegraphics", tex)
            path = work / "diagram.tex"
            path.write_text(tex, encoding="utf-8")
            pdf, metrics = native_latex.compile_body_tex(work, path)
            self.assertEqual(metrics["missing_glyphs"], 0)
            self.assertEqual(metrics["overfull_hboxes"], 0)
            with pymupdf.open(pdf) as document:
                self.assertEqual(len(document), 1)
                page = document[0]
                self.assertFalse(page.get_images())
                self.assertFalse(page.get_xobjects())
                self.assertTrue(page.get_drawings())
                text = unicodedata.normalize("NFKC", page.get_text())
                for label in ("Γ", "∂", "f"):
                    self.assertIn(label, text)
                fonts = [font for font in page.get_fonts() if "Math" in font[3]]
                self.assertTrue(fonts)
                self.assertTrue(all("LatinModernMath" in font[3] for font in fonts))
                self.assertTrue(all(document.extract_font(font[0])[3] for font in fonts))


if __name__ == "__main__":
    unittest.main()
