"""Measure production LuaLaTeX baselines across real body font switches."""

from collections import Counter
from pathlib import Path
import shutil
import tempfile
import unittest

import pymupdf

import native_latex
from typography import ZH_ACADEMIC_V1


PDF_POINTS_PER_TEX_POINT = 72 / 72.27


def compile_pitch_fixture(work):
    fragments = {"[[Fmath]]": {"kind": "math", "tex": r"x_i^2+\alpha",
                              "review_status": "manually_reviewed"}}
    # Use the same rich-text font commands and document preamble as production.
    node = {"id": "styles", "element": {
        "id": "styles", "kind": "body", "inline_fragments": fragments,
        "style_tokens": {"Bbold": "bold", "Iitalic": "italic"}}}
    cases = ["普通中文 ordinary Latin text.",
             "中文 [[Bbold_OPEN]]bold Latin 粗体[[Bbold_CLOSE]] 正文。",
             "中文 [[Iitalic_OPEN]]italic Latin 斜体[[Iitalic_CLOSE]] 正文。",
             "原生公式 [[Fmath]] and ordinary Latin 正文。",
             "回到普通中文 ordinary Latin text."]
    lines = []
    for group in range(3):
        body = []
        for index, case in enumerate(cases):
            label = f"Pitch{group}{index}"
            node["text"] = label + " " + case
            body.append(native_latex.rich_text_to_latex(node, {}, Counter()))
        lines.append(r"\\".join(body) + r"\par")
    tex = (native_latex._document_preamble(ZH_ACADEMIC_V1)
           + lines[0] + r"\TPHeadingLower{字体切换后的正文}"
           + lines[1] + r"\TPDisplayMath{f(x)=\int_0^1 t^2\,dt}"
           + r"\TPFootnote{NoteLineA 这是同页脚注 with Latin and \(x_i\)."
           + r"\\NoteLineB 脚注的第二行。}"
           + lines[2] + r"\end{document}")
    path = work / "line-pitch.tex"
    path.write_text(tex, encoding="utf-8")
    pdf, metrics = native_latex.compile_body_tex(work, path)
    rows = {}
    with pymupdf.open(pdf) as document:
        for page in document:
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        for word in span["text"].split():
                            if word.startswith(("Pitch", "NoteLine")):
                                rows[word] = {"baseline": span["origin"][1],
                                              "size": span["size"],
                                              "bbox": list(span["bbox"]),
                                              "page": page.number}
    return rows, metrics


@unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX is unavailable")
class NativeLinePitchTests(unittest.TestCase):
    def test_absolute_body_pitch_survives_inline_heading_math_and_note_fonts(self):
        with tempfile.TemporaryDirectory() as directory:
            rows, metrics = compile_pitch_fixture(Path(directory))
        self.assertEqual(len(rows), 17, rows)
        self.assertEqual(metrics["missing_glyphs"], 0)
        expected_pitch = ZH_ACADEMIC_V1.body_line_pitch * PDF_POINTS_PER_TEX_POINT
        expected_size = ZH_ACADEMIC_V1.body_font_size * PDF_POINTS_PER_TEX_POINT
        for group in range(3):
            for index in range(5):
                row = rows[f"Pitch{group}{index}"]
                self.assertAlmostEqual(row["size"], expected_size, delta=.02)
                if index:
                    previous = rows[f"Pitch{group}{index-1}"]
                    self.assertEqual(row["page"], previous["page"])
                    self.assertAlmostEqual(row["baseline"] - previous["baseline"],
                                           expected_pitch, delta=.03,
                                           msg=f"Rendered leading after font switch: {rows}")
                self.assertLessEqual(row["bbox"][3],
                                     841.89 - ZH_ACADEMIC_V1.margin_bottom
                                     * PDF_POINTS_PER_TEX_POINT + .1)
        note_a, note_b = rows["NoteLineA"], rows["NoteLineB"]
        self.assertEqual(note_a["page"], rows["Pitch20"]["page"])
        self.assertEqual(note_a["page"], note_b["page"])
        self.assertAlmostEqual(note_a["size"], 8.5 * PDF_POINTS_PER_TEX_POINT,
                               delta=.02)
        self.assertAlmostEqual(note_b["baseline"] - note_a["baseline"],
                               11 * PDF_POINTS_PER_TEX_POINT, delta=.03)


if __name__ == "__main__":
    unittest.main()
