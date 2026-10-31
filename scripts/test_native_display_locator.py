#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2", "pillow>=10.4.0,<13"]
# ///
"""A native display's actual TeX page must survive a break after its prose."""
import shutil
import tempfile
import unittest
from pathlib import Path

import pymupdf

import native_latex
from typography import ZH_ACADEMIC_V1


@unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX is unavailable")
class NativeDisplayLocatorTests(unittest.TestCase):
    def test_compiled_display_follows_its_atomic_marker_across_page_break(self):
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            source.new_page()
            work = Path(directory)
            prose = {"id": "prose", "kind": "body", "page": 1,
                     "bbox": [0, 0, 100, 20], "translatable": True}
            equation = {"id": "equation", "kind": "equation", "page": 1,
                        "bbox": [0, 20, 100, 80], "render_mode": "source_clip"}
            nodes = [{"id": "prose", "element": prose,
                      "text": "Preceding prose stays on the first page.",
                      "flow_role": "paragraph-start"},
                     {"id": "equation", "element": equation,
                      "text": "La = alpha L, Ma = beta M, Sa = gamma S"}]
            reviews = {"equation": {"review_status": "manually_reviewed",
                       "tex": r"\begin{aligned}L_a&=\alpha L,\\M_a&=\beta M,\\S_a&=\gamma S.\end{aligned}"}}
            tex, stubs, _ = native_latex.build_body_tex(
                nodes, reviews, ZH_ACADEMIC_V1, source, work)
            tex = tex.replace("% TP-NODE equation",
                              r"\vspace*{.96\textheight}" + "\n% TP-NODE equation")
            path = work / "display-break.tex"
            path.write_text(tex, encoding="utf-8")
            pdf, metrics = native_latex.compile_body_tex(work, path)
            self.assertEqual(metrics["missing_glyphs"], 0)
            with pymupdf.open(pdf) as document:
                actual_pages = [index + 1 for index, page in enumerate(document)
                                if any("math" in span["font"].lower()
                                       for block in page.get_text("dict")["blocks"]
                                       for line in block.get("lines", [])
                                       for span in line.get("spans", []))]
                self.assertEqual(actual_pages, [2])
                self.assertIn("Preceding prose", document[0].get_text())
                self.assertIn("TPMATHequation", document[1].get_text())
                self.assertNotIn("TPMATHequation", document[0].get_text())
                self.assertTrue(all(not page.get_images() and not page.get_xobjects()
                                    and not page.get_links() for page in document))
                fonts = [font for font in document[1].get_fonts() if "Math" in font[3]]
                self.assertTrue(fonts)
                self.assertTrue(all(document.extract_font(font[0])[3] for font in fonts))
                count = len(document)
            placements = native_latex.locate_placements(
                pdf, stubs, nodes, 1, count, ZH_ACADEMIC_V1)
            self.assertEqual([item["output_page"] for item in placements], [1, 2])


if __name__ == "__main__":
    unittest.main()
