"""Reviewed prose/display/prose survives replay, both exports and strict checks."""

from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import unittest
import hashlib
import json

import pymupdf

import assignment_identity
import check_translation
import export_translation_markdown
import native_latex
import paper_repair
import prepare_paper
import render_translation
import source_review
import source_structure
import translation_checkpoint
import translation_context
from display_plan import build_display_plan
from typography import ZH_ACADEMIC_V1


def prose(unit_id, order, text, **fields):
    return {"id": unit_id, "order": order, "page_order": order, "page": 1,
            "kind": "body", "render_mode": "text", "translatable": True,
            "source_text": text, "bbox": [50, 50 + order * 20, 350, 65 + order * 20],
            "font_size": 10, "section": "Images", "span_runs": [],
            "flow_role": "paragraph", "flow_reviewed": True, **fields}


class PostDisplayFlowTests(unittest.TestCase):
    def fixture(self):
        units = [prose("lead", 1, "We define the image as a function"),
                 prose("display1", 2, "I: R -> V,", kind="equation", render_mode="source_clip", translatable=False),
                 prose("display2", 3, "R subset R2,", kind="equation", render_mode="source_clip", translatable=False),
                 prose("tail", 4, "where the domain is rectangular."),
                 prose("display3", 5, "V = R,", kind="equation", render_mode="source_clip", translatable=False),
                 prose("tail2", 6, "The codomain gives the possible values.", page=2),
                 prose("new", 7, "A new source paragraph.", page=2)]
        for unit in units:
            prepare_paper.protect_element(unit)
        review = {"source_sha256": "source", "reviewed": True, "operations": [
            {"op": "replace_unit", "id": "tail", "flow_role": "paragraph", "continuation_of": "lead",
             "reason": "Source image shows continuation after two independent displays."},
            {"op": "replace_unit", "id": "tail2", "continuation_of": "tail",
             "reason": "Source image shows a continued paragraph on the next page."}]}
        source_review.apply_source_review(units, review, "source",
            [{"page": i, "width": 600, "height": 800} for i in (1, 2)], prepare_paper.protect_element)
        reviews = {unit["id"]: {"kind": "display", "source_ids": [unit["id"]],
                   "tex": tex, "review_status": "manually_reviewed"} for unit, tex in
                   zip([units[1], units[2], units[4]], [r"I:R\to V,", r"R\subset\mathbb{R}^2,", "V=R,"])}
        translations = {"lead": "We define the image as a function", "tail": "where the domain is rectangular.",
                        "tail2": "The codomain gives the possible values.", "new": "A new source paragraph."}
        return units, reviews, translations

    def test_review_replay_to_native_tex_markdown_and_strict_validation(self):
        units, reviews, translations = self.fixture()
        plan = build_display_plan(units, reviews)
        nodes = render_translation.build_flow_nodes(units, translations, plan)
        self.assertEqual([node["ids"] for node in nodes], [[unit["id"]] for unit in units])
        self.assertEqual(nodes[3]["flow_role"], "paragraph-continuation")
        self.assertEqual(nodes[3]["continuation_across_display_ids"], ["display1", "display2"])
        self.assertEqual(nodes[5]["continuation_paragraph_id"], "lead")
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            source.new_page()
            source.new_page()
            tex, placements, _ = native_latex.build_body_tex(nodes, reviews, ZH_ACADEMIC_V1, source, Path(directory))
        self.assertIn(r"\par\noindent where the domain", tex)
        self.assertIn(r"\par\noindent The codomain", tex)
        self.assertIn(r"\par A new source paragraph.", tex)
        for value in reviews.values():
            self.assertEqual(tex.count(value["tex"]), 1)
        markdown = export_translation_markdown.export_markdown({}, units,
            [{"id": key, "translated_text": value} for key, value in translations.items()], {"entries": reviews})
        self.assertIn("function\n$$\n" + reviews["display1"]["tex"] + "\n$$\n$$\n", markdown)
        self.assertIn("\n$$\nwhere the domain", markdown)
        self.assertIn("possible values.\n\nA new source paragraph.", markdown)
        self.assertEqual(markdown.count("$$"), 6)
        layout = {"placements": placements}
        self.assertEqual(check_translation.validate_reviewed_flow({}, units, layout), [])
        for field, value in (("flow_role", "paragraph-start"), ("text_indent", 23),
                             ("continuation_of", "display2"), ("continuation_across_display_ids", ["display2"]),
                             ("continuation_paragraph_id", "tail")):
            bad = deepcopy(layout)
            bad["placements"][3][field] = value
            self.assertTrue(check_translation.validate_reviewed_flow({}, units, bad), field)

    def test_without_declaration_display_does_not_change_reviewed_new_paragraph(self):
        units, reviews, translations = self.fixture()
        units[3].pop("continuation_of")
        nodes = render_translation.build_flow_nodes(units, translations, build_display_plan(units, reviews))
        self.assertEqual(nodes[3]["flow_role"], "paragraph-start")
        self.assertEqual(nodes[5]["continuation_paragraph_id"], "tail")

    def test_insert_review_can_declare_continuation_after_display(self):
        units, _, _ = self.fixture()
        units = units[:3]
        source_review.apply_source_review(units, {"source_sha256": "source", "reviewed": True,
            "operations": [{"op": "insert_unit", "id": "review-p0001-u0001", "after_id": "display2",
                "page": 1, "bbox": [50, 130, 350, 145], "kind": "body", "source_text": "where the image continues.",
                "continuation_of": "lead", "reason": "Visible continuation after the display group."}]},
            "source", [{"page": 1, "width": 600, "height": 800}], prepare_paper.protect_element)
        self.assertIs(units[-1]["flow_reviewed"], True)
        self.assertEqual(source_structure.explicit_continuations(units)[units[-1]["id"]]["continuation_of"], "lead")

    def adjacent_fixture(self):
        units = [prose("lead", 1, "If we want", flow_reviewed=False),
                 prose("tail", 2, "to composite the foreground.", page=2, continuation_of="lead"),
                 prose("new", 3, "A new source paragraph.", page=2)]
        for unit in units:
            prepare_paper.protect_element(unit)
        translations = {unit["id"]: unit["source_text"] for unit in units}
        return units, render_translation.build_flow_nodes(units, translations), translations

    def test_adjacent_explicit_continuation_joins_text_and_keeps_review_audit(self):
        units, nodes, translations = self.adjacent_fixture()
        self.assertEqual(nodes[0]["ids"], ["lead", "tail"])
        self.assertEqual(nodes[0]["text"], "If we want to composite the foreground.")
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            source.new_page()
            source.new_page()
            tex, placements, _ = native_latex.build_body_tex(nodes, {}, ZH_ACADEMIC_V1, source, Path(directory))
        self.assertIn(r"\par If we want to composite the foreground.\par", tex)
        self.assertEqual(check_translation.validate_reviewed_flow({}, units, {"placements": placements}), [])
        placements[0]["reviewed_continuations"]["tail"]["continuation_of"] = "new"
        self.assertTrue(check_translation.validate_reviewed_flow({}, units, {"placements": placements}))
        markdown = export_translation_markdown.export_markdown({}, units,
            [{"id": key, "translated_text": value} for key, value in translations.items()])
        self.assertIn("If we want\nto composite the foreground.\n\nA new source paragraph.", markdown)

    def test_bad_source_links_fail_atomically_and_in_both_exporters(self):
        for change in ("missing", "forward", "self", "section", "list", "heading", "figure", "body", "footnote"):
            units, reviews, translations = self.fixture()
            units[3].pop("continuation_of")
            if change in {"missing", "forward", "self"}:
                anchor = {"missing": "unknown", "forward": "new", "self": "tail"}[change]
            else:
                anchor = "lead"
                if change == "section":
                    units[3]["section"] = "Other"
                elif change == "list":
                    units[0].update(flow_role="list_item", list_id="items", list_level=0, source_marker="1.")
                else:
                    units.insert(3, prose("blocker", 3.5, "Unrelated content", kind=change,
                                         render_mode="source_clip" if change == "figure" else "text"))
            original = deepcopy(units)
            review = {"source_sha256": "source", "reviewed": True, "operations": [{"op": "replace_unit", "id": "tail",
                "continuation_of": anchor, "reason": "Invalid proposed anchor."}]}
            with self.subTest(change=change):
                with self.assertRaises(source_review.SourceReviewError):
                    source_review.apply_source_review(units, review, "source",
                        [{"page": i, "width": 600, "height": 800} for i in (1, 2)], prepare_paper.protect_element)
                self.assertEqual(units, original)
                next(unit for unit in units if unit["id"] == "tail")["continuation_of"] = anchor
                with self.assertRaises(ValueError):
                    render_translation.build_flow_nodes(units, translations, build_display_plan(units, reviews))
                with self.assertRaises(ValueError):
                    export_translation_markdown.export_markdown({}, units,
                        [{"id": unit["id"], "translated_text": "Source text."} for unit in units if unit.get("translatable")],
                        {"entries": reviews})

    def test_only_same_paragraph_anchored_footnotes_may_intervene(self):
        units, _, _ = self.fixture()
        note = prose("note", 3, "A note.", kind="footnote", anchor_id="lead")
        units.insert(3, note)
        for index, unit in enumerate(units):
            unit["order"] = index + 1
        self.assertEqual(source_structure.explicit_continuations(units)["tail"]["continuation_of"], "lead")
        # A later continuation may cross a note attached to the initial paragraph owner.
        note2 = {**note, "id": "note2", "order": 6.5}
        units.append(note2)
        source_structure.explicit_continuations(units)
        note["anchor_id"] = "new"
        with self.assertRaisesRegex(ValueError, "unrelated source unit note"):
            source_structure.explicit_continuations(units)

    def test_new_flow_field_invalidates_assignment_and_completed_contract(self):
        units, _, _ = self.fixture()
        unit = units[3]
        changed = {**unit, "continuation_of": None}
        self.assertNotEqual(assignment_identity.unit_identity(unit), assignment_identity.unit_identity(changed))
        self.assertIn("continuation_of", translation_checkpoint.SOURCE_FIELDS)
        self.assertEqual(translation_context.compact_unit(unit, {})["continuation_of"], "lead")
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "translations.jsonl"
            translation_checkpoint.atomic_write(checkpoint, [{**unit, "translated_text": "已有译文。"}])
            with self.assertRaises(source_review.SourceReviewError):
                source_review.reconcile_translations(checkpoint, [changed])
            legacy = {key: value for key, value in unit.items() if key != "continuation_of"}
            translation_checkpoint.atomic_write(checkpoint, [{**legacy, "translated_text": "已有译文。"}])
            with self.assertRaises(source_review.SourceReviewError):
                source_review.reconcile_translations(checkpoint, [unit])

    def test_isolated_repair_reopens_only_changed_continuation_contract(self):
        units, _, translations = self.fixture()
        units[3].pop("continuation_of")
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / "source.pdf"
            source.write_bytes(b"source fixture")
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            templates = [{**unit, "translated_text": ""} for unit in units if unit.get("translatable")]
            records = [{**unit, "translated_text": "已完成译文。"} for unit in templates]
            translation_checkpoint.atomic_write(work / "translation-units.jsonl", units)
            translation_checkpoint.atomic_write(work / "translations.template.jsonl", templates)
            translation_checkpoint.atomic_write(work / "translations.jsonl", records)
            manifest = {"source_pdf": str(source), "source_sha256": source_hash,
                "paths": {"translation_units": "translation-units.jsonl", "translations_template": "translations.template.jsonl"}}
            (work / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            proposal = {"source_sha256": source_hash, "reviewed": True, "reason": "Fix explicit source continuation.",
                "operations": [{"op": "replace_unit", "id": "tail", "continuation_of": "lead", "reason": "Reviewed source."}]}
            (work / "proposal.json").write_text(json.dumps(proposal), encoding="utf-8")
            before = (work / "translations.jsonl").read_bytes()

            def replay(candidate):
                changed = deepcopy(units)
                source_review.apply_source_review(changed, proposal, source_hash,
                    [{"page": i, "width": 600, "height": 800} for i in (1, 2)], prepare_paper.protect_element)
                translation_checkpoint.atomic_write(candidate / "translation-units.jsonl", changed)
                translation_checkpoint.atomic_write(candidate / "translations.template.jsonl",
                    [{**unit, "translated_text": ""} for unit in changed if unit.get("translatable")])
                for name in ("source-review-report.json", "visual-layout.json"):
                    (candidate / name).write_text('{"status":"pass"}', encoding="utf-8")

            report = paper_repair.repair(work / "manifest.json", work / "proposal.json", builder=replay)
            self.assertEqual(report["reopened_completed_ids"], ["tail"])
            self.assertEqual(report["preserved_completed_ids"], ["lead", "tail2", "new"])
            self.assertEqual((work / "translations.jsonl").read_bytes(), before)

    @unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX is unavailable")
    def test_compiled_pdf_continuations_are_flush_and_new_paragraph_is_indented(self):
        units, reviews, translations = self.fixture()
        nodes = render_translation.build_flow_nodes(units, translations, build_display_plan(units, reviews))
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            work = Path(directory)
            source.new_page()
            source.new_page()
            tex, placements, _ = native_latex.build_body_tex(nodes, reviews, ZH_ACADEMIC_V1, source, work)
            path = work / "flow.tex"
            path.write_text(tex, encoding="utf-8")
            pdf, metrics = native_latex.compile_body_tex(work, path)
            self.assertEqual(metrics["missing_glyphs"], 0)
            with pymupdf.open(pdf) as document:
                for text in ("where the domain", "The codomain"):
                    self.assertAlmostEqual(document[0].search_for(text)[0].x0, ZH_ACADEMIC_V1.margin_left, delta=0.6)
                self.assertAlmostEqual(document[0].search_for("A new source paragraph.")[0].x0,
                    ZH_ACADEMIC_V1.margin_left + 23, delta=0.6)
                self.assertFalse(document[0].get_images())

    @unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX is unavailable")
    def test_compiled_adjacent_source_page_split_stays_on_same_line(self):
        _, nodes, _ = self.adjacent_fixture()
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            work = Path(directory)
            source.new_page()
            source.new_page()
            tex, _, _ = native_latex.build_body_tex(nodes, {}, ZH_ACADEMIC_V1, source, work)
            path = work / "adjacent.tex"
            path.write_text(tex, encoding="utf-8")
            pdf, metrics = native_latex.compile_body_tex(work, path)
            self.assertEqual(metrics["missing_glyphs"], 0)
            with pymupdf.open(pdf) as document:
                before = document[0].search_for("want")[0]
                after = document[0].search_for("to composite")[0]
                self.assertAlmostEqual(before.y0, after.y0, delta=0.2)
                self.assertGreater(after.x0, before.x0)


if __name__ == "__main__":
    unittest.main()
