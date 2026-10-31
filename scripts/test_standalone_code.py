#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2", "pillow>=10.4.0,<13"]
# ///
"""Standalone pseudocode remains invariant throughout the ordinary pipeline."""

from __future__ import annotations

import copy
import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pymupdf

import check_translation
import export_translation_markdown
import native_latex
import paper_preflight
import prepare_paper
import render_translation
import source_review
from display_plan import build_display_plan
from typography import ZH_ACADEMIC_V1


FIXTURE = json.loads(Path(__file__).with_suffix(".fixtures.json").read_text(encoding="utf-8"))


class StandaloneCodeTests(unittest.TestCase):
    def setUp(self):
        self.pages = [{"page": 1, "width": 612, "height": 792}]
        self.units = [{
            "id": "intro", "order": 1, "page_order": 1, "page": 1,
            "bbox": [72, 60, 500, 90], "kind": "body", "render_mode": "text",
            "translatable": True, "source_text": "An explanation before the examples.",
            "font_size": 10, "section": "Introduction", "span_runs": [],
        }]
        prepare_paper.protect_element(self.units[0])
        for index, entry in enumerate(FIXTURE["listings"], 2):
            self.units.append({
                **self.units[0], "id": f"example-{index}", "order": index,
                "page_order": index, "bbox": entry["bbox"], "source_text": entry["text"],
                "kind": "equation", "render_mode": "source_clip", "translatable": False,
            })
        self.review = {
            **source_review.make_source_review("fixture-source"), "reviewed": True,
            "reason": "Independent fixture pseudocode verified against source.",
            "operations": [{"op": "replace_unit", "id": unit["id"],
                            "kind": "code", "translatable": False,
                            "reason": "Preserve standalone pseudocode as original code."}
                           for unit in self.units[1:]],
        }

    def apply_review(self):
        return source_review.apply_source_review(
            self.units, self.review, "fixture-source", self.pages, prepare_paper.protect_element)

    def nodes(self):
        self.apply_review()
        return render_translation.build_flow_nodes(self.units, {"intro": "示例前的说明。"})

    def test_review_and_prepare_skip_math_and_prose_diagnostics_without_caption(self):
        self.apply_review()
        self.assertTrue(all(unit["render_mode"] == "source_clip" for unit in self.units[1:]))
        self.assertTrue(all(not unit["inline_fragments"] for unit in self.units[1:]))
        math = prepare_paper.build_math_review(self.units, "fixture-source")
        self.assertEqual(math["entries"], {})
        self.assertEqual(build_display_plan(self.units, math["entries"]), [])
        self.assertEqual(paper_preflight._math_inventory(self.units, {}, []), ([], []))
        with pymupdf.open() as source:
            source.new_page(width=612, height=792)
            visuals = prepare_paper.build_visual_layout(source, self.units, self.pages, "fixture-source")
        self.assertEqual(visuals["visuals"], [])
        self.assertEqual(visuals["errors"], [])
        self.assertEqual(source_review.detect_source_issues(self.units, visuals)[0], [])

    def test_prepare_command_replays_code_review_and_omits_it_from_translation_template(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "independent-fixture.pdf"
            work = root / "prepared"
            with pymupdf.open() as source:
                page = source.new_page(width=612, height=792)
                page.insert_text((72, 50), "1 Introduction", fontname="hebo", fontsize=14)
                page.insert_text((72, 90), "An explanation before the code.", fontsize=10)
                page.insert_text((72, 135), FIXTURE["listings"][0]["text"], fontname="cour", fontsize=10)
                page.insert_text((72, 320), "2 Conclusion", fontname="hebo", fontsize=14)
                page.insert_text((72, 350), "The final explanation.", fontsize=10)
                source.save(pdf_path)
            args = ["prepare_paper.py", "--input", str(pdf_path), "--work-dir", str(work),
                    "--ocr", "off", "--render-dpi", "36"]
            with mock.patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(prepare_paper.main(), 0)
            units = [json.loads(line) for line in (work / "translation-units.jsonl").read_text(encoding="utf-8").splitlines()]
            code_units = [unit for unit in units if pymupdf.Rect(unit["bbox"]).intersects(
                pymupdf.Rect(FIXTURE["listings"][0]["bbox"]))]
            review = source_review.make_source_review(prepare_paper.sha256_file(pdf_path))
            review.update(reviewed=True, reason="Verified independent fixture code.", operations=[{
                "op": "replace_unit", "id": code_units[0]["id"], "kind": "code",
                "render_mode": "source_clip", "translatable": False,
                "source_text": FIXTURE["listings"][0]["text"], "bbox": FIXTURE["listings"][0]["bbox"],
                "reason": "Keep the original pseudocode.",
            }])
            if len(code_units) > 1:
                review["operations"].append({"op": "suppress_units", "ids": [unit["id"] for unit in code_units[1:]],
                                             "reason": "Already preserved by the code source clip."})
            (work / "source-review.json").write_text(json.dumps(review), encoding="utf-8")
            with mock.patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(prepare_paper.main(), 0)
            units = [json.loads(line) for line in (work / "translation-units.jsonl").read_text(encoding="utf-8").splitlines()]
            code = next(unit for unit in units if unit["kind"] == "code")
            template = (work / "translations.template.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(code["id"], template)
            self.assertEqual(code["source_text"], FIXTURE["listings"][0]["text"])
            math = json.loads((work / "math-review.json").read_text(encoding="utf-8"))
            self.assertNotIn(code["id"], math["entries"])

    def test_insert_code_and_reject_translatable_text_or_empty_code(self):
        self.review["operations"] = [{
            "op": "insert_unit", "id": "review-p0001-u0001", "after_id": "intro",
            "kind": "code", "source_text": "return x + 1", "bbox": [72, 95, 200, 110],
            "reason": "Restore a captionless code block.",
        }]
        self.apply_review()
        inserted = self.units[1]
        self.assertEqual((inserted["kind"], inserted["render_mode"], inserted["translatable"]),
                         ("code", "source_clip", False))
        for fields in ({"translatable": True}, {"render_mode": "text"}, {"source_text": ""}):
            with self.subTest(fields=fields), self.assertRaises(source_review.SourceReviewError):
                source_review._refresh_translation_fields({**inserted, **fields}, prepare_paper.protect_element)

    def test_markdown_keeps_exact_lines_without_listing_sidecar(self):
        self.apply_review()
        markdown = export_translation_markdown.export_markdown(
            {"source_title": "Fixture"}, self.units,
            [{"id": "intro", "translated_text": "示例前的说明。"}])
        for entry in FIXTURE["listings"]:
            self.assertIn("```\n" + entry["text"] + "\n```", markdown)
        self.assertNotIn("$$", markdown)

    def test_adjacent_code_is_not_merged_with_code_or_other_clips(self):
        nodes = self.nodes()
        self.assertEqual([node["ids"] for node in nodes], [["intro"], ["example-2"], ["example-3"]])
        self.units.append({**self.units[-1], "id": "figure", "order": 4, "kind": "figure-text"})
        nodes = render_translation.build_flow_nodes(self.units, {"intro": "说明。"})
        self.assertEqual(nodes[-1]["ids"], ["figure"])

    def test_invariant_checks_require_actual_short_code_text_and_one_owned_placement(self):
        self.apply_review()
        unit = self.units[1]
        layout = {"placements": [{
            "id": unit["id"], "ids": [unit["id"]], "render_mode": "source_clip",
            "source_page": 1, "source_bbox": unit["bbox"],
            "output_page": 1, "bbox": unit["bbox"],
        }]}
        normalized = check_translation.normalize_text(unit["source_text"])
        self.assertEqual(check_translation.missing_invariant_ids([unit], normalized, layout), [])
        self.assertEqual(check_translation.missing_invariant_ids([unit], "", layout), [unit["id"]])
        with pymupdf.open() as output:
            page = output.new_page(width=612, height=792)
            page.insert_text((72, 135), unit["source_text"], fontsize=10)
            self.assertEqual(check_translation.validate_code_preservation(output, [unit], layout), [])
            placement = layout["placements"][0]
            placement["source_bbox"] = [value + 0.00001 for value in unit["bbox"]]
            self.assertEqual(check_translation.validate_code_preservation(output, [unit], layout), [])
            placement["source_bbox"] = [72, 120, 290, 170]
            self.assertTrue(check_translation.validate_code_preservation(output, [unit], layout))
            placement["source_bbox"] = unit["bbox"]
            changed = copy.deepcopy(unit)
            changed["source_text"] = unit["source_text"].replace("something", "Something")
            self.assertTrue(check_translation.validate_code_preservation(output, [changed], layout))
            self.assertTrue(check_translation.validate_code_preservation(output, [unit], {}))
            layout["placements"] *= 2
            self.assertTrue(check_translation.validate_code_preservation(output, [unit], layout))

    @unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX unavailable")
    def test_native_pdf_keeps_source_vectors_and_text_without_math_review(self):
        nodes = self.nodes()
        # Render the ASCII fixture; smart quotes are checked losslessly in Markdown above.
        nodes = nodes[:2]
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            root = Path(directory)
            page = source.new_page(width=612, height=792)
            page.insert_text((72, 135), self.units[1]["source_text"], fontname="cour", fontsize=10)
            page.draw_line((72, 177), (290, 177))
            nodes = native_latex.prepare_native_clip_nodes(nodes, self.units, ZH_ACADEMIC_V1)
            tex, stubs, statuses = native_latex.build_body_tex(nodes, {}, ZH_ACADEMIC_V1, source, root)
            self.assertEqual(dict(statuses), {})
            self.assertEqual(stubs[1]["render_mode"], "source_clip")
            self.assertNotIn(r"\TPDisplayMath{", tex.split(r"\begin{document}", 1)[1])
            path = root / "body.tex"
            path.write_text(tex, encoding="utf-8")
            pdf, _ = native_latex.compile_body_tex(root, path)
            placements = native_latex.locate_placements(pdf, stubs, nodes, 1, 1, ZH_ACADEMIC_V1)
            with pymupdf.open(pdf) as output:
                layout = {"placements": placements}
                self.assertEqual(check_translation.validate_code_preservation(output, [self.units[1]], layout), [])
                self.assertEqual(check_translation.validate_source_clip_envelopes(layout, self.units[:2])[0], [])
                self.assertTrue(output[0].get_drawings())
                self.assertEqual(output[0].get_images(), [])
                self.assertIn("Courier", " ".join(str(font) for font in output[0].get_fonts()))
                layout.update(typography_profile="zh-academic-v1", flow={"text_embedding": "native-lualatex"},
                              boundary_layout={"translation_page_start": 1, "translation_page_end": 1})
                _, _, metrics = check_translation.validate_typography(output, layout, set())
                self.assertNotIn("Courier", " ".join(metrics["latin_fonts"]))


if __name__ == "__main__":
    unittest.main()
