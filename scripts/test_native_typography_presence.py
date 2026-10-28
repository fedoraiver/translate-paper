"""Chinese-plus-math papers need no Latin prose glyphs, but wrong Latin fonts fail."""
import shutil
import tempfile
import unittest
from pathlib import Path

import pymupdf

import check_translation
import native_latex
from typography import ZH_ACADEMIC_V1


@unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX is unavailable")
class NativeTypographyPresenceTests(unittest.TestCase):
    def test_absent_latin_prose_is_valid_and_actual_wrong_font_still_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            tex = work / "presence.tex"
            tex.write_text(native_latex._document_preamble(ZH_ACADEMIC_V1)
                           + r"\TPHeadingOne{引言}\par 这是一段中文说明。\TPDisplayMath{x_i=1}\end{document}",
                           encoding="utf-8")
            pdf, _ = native_latex.compile_body_tex(work, tex)
            layout = {"typography_profile": "zh-academic-v1",
                      "flow": {"text_embedding": "native-lualatex"},
                      "boundary_layout": {"translation_page_start": 1, "translation_page_end": 1},
                      "placements": [{"id": "body", "flow_role": "paragraph-start"},
                                     {"id": "heading", "flow_role": "heading"}]}
            with pymupdf.open(pdf) as document:
                errors, _, metrics = check_translation.validate_typography(document, layout, set())
                self.assertEqual(metrics["latin_fonts"], {})
                self.assertFalse(any("Latin prose does not" in error for error in errors), errors)
                document[0].insert_text((70, 200), "Unexpected Latin prose", fontname="helv")
                errors, _, _ = check_translation.validate_typography(document, layout, set())
                self.assertTrue(any("Latin prose does not" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
