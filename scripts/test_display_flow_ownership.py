"""Display ownership and reviewed source list structure reach both exports."""

from copy import deepcopy
from argparse import Namespace
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

import pymupdf

import check_translation
import display_plan
import export_translation_markdown
import native_latex
import render_translation
import source_structure
from typography import ZH_ACADEMIC_V1


class DisplayOwnershipTests(unittest.TestCase):
    def rules(self):
        units = []
        for index in range(1, 4):
            units.append({"id": f"rule{index}", "order": len(units) + 1, "page": 1,
                          "bbox": [100, 50 * index, 350, 50 * index + 20],
                          "kind": "equation", "render_mode": "source_clip",
                          "translatable": False, "source_text": f"x_{index} = y_{index}"})
            units.append({"id": f"label{index}", "order": len(units) + 1, "page": 1,
                          "bbox": [360, 50 * index, 380, 50 * index + 20],
                          "kind": "body", "render_mode": "text", "render_suppressed": True,
                          "translatable": False, "source_text": f"({index})"})
        return units

    def reviews(self):
        return {f"rule{index}": {"kind": "display", "source_ids": [f"rule{index}"],
                "tex": rf"x_{index}=y_{index}\qquad({index})", "review_status": "manually_reviewed"}
                for index in range(1, 4)}

    def export(self, units, reviews):
        plan = display_plan.build_display_plan(units, reviews)
        nodes = render_translation.build_flow_nodes(units, {}, display_plan=plan)
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            source.new_page()
            tex, placements, _ = native_latex.build_body_tex(nodes, reviews, ZH_ACADEMIC_V1, source, Path(directory))
        markdown = export_translation_markdown.export_markdown({}, units, [], {"entries": reviews})
        display_plan.validate_display_consumption(plan, placements)
        return plan, nodes, tex, placements, markdown

    def test_three_adjacent_rules_separated_by_suppressed_labels_stay_independent(self):
        units, reviews = self.rules(), self.reviews()
        self.assertEqual([[unit["id"] for unit in group] for group in display_plan.default_display_groups(units)],
                         [["rule1"], ["rule2"], ["rule3"]])
        # The legacy reader must not coalesce adjacent clips either.
        legacy_nodes = render_translation.build_flow_nodes(units, {})
        self.assertEqual([node["ids"] for node in legacy_nodes], [["rule1"], ["rule2"], ["rule3"]])
        _, nodes, tex, placements, markdown = self.export(units, reviews)
        self.assertEqual([node["ids"] for node in nodes], [["rule1"], ["rule2"], ["rule3"]])
        self.assertEqual(tex.count(r"\TPDisplayMath{"), 3)
        self.assertEqual(markdown.count("$$\n"), 6)
        self.assertEqual([item["math_review_id"] for item in placements], list(reviews))
        for entry in reviews.values():
            self.assertEqual(markdown.count(entry["tex"]), 1)
            self.assertEqual(tex.count(entry["tex"]), 1)

    def test_explicit_group_owns_every_rule_and_label_once(self):
        units = self.rules()
        ids = [unit["id"] for unit in units]
        reviews = {"rule1": {"kind": "display", "source_ids": ids,
                   "tex": r"\begin{gathered}x_1=y_1\quad(1)\\x_2=y_2\quad(2)\\x_3=y_3\quad(3)\end{gathered}",
                   "review_status": "manually_reviewed"}}
        plan, nodes, tex, placements, markdown = self.export(units, reviews)
        self.assertEqual(len(plan), 1)
        self.assertEqual(nodes[0]["ids"], ids)
        self.assertEqual(placements[0]["ids"], ids)
        self.assertEqual(markdown.count("$$\n"), 2)
        self.assertEqual(tex.count(reviews["rule1"]["tex"]), 1)
        self.assertEqual([[unit["id"] for unit in group] for group in display_plan.default_display_groups(units, reviews)], [ids])

    def test_invalid_membership_and_stale_source_metadata_fail(self):
        for change in ("missing-review", "missing-members", "duplicate", "overlap", "missing-id",
                       "wrong-order", "wrong-page", "wrong-source", "wrong-bbox", "crossing"):
            with self.subTest(change=change):
                units, reviews = self.rules(), self.reviews()
                if change == "missing-review": del reviews["rule2"]
                elif change == "missing-members": del reviews["rule1"]["source_ids"]
                elif change == "duplicate": reviews["rule1"]["source_ids"] *= 2
                elif change == "overlap": reviews["rule1"]["source_ids"] += ["rule2"]
                elif change == "missing-id": reviews["rule1"]["source_ids"] += ["unknown"]
                elif change == "wrong-order": reviews["rule1"]["source_ids"] = ["label1", "rule1"]
                elif change == "wrong-page": reviews["rule1"]["source_page"] = 2
                elif change == "wrong-source": reviews["rule1"]["source_text"] = "changed"
                elif change == "wrong-bbox": reviews["rule1"]["source_bbox"] = [0, 0, 1, 1]
                elif change == "crossing": reviews["rule1"]["source_ids"] = ["rule1", "rule3"]
                with self.assertRaises(ValueError):
                    display_plan.build_display_plan(units, reviews)
                with self.assertRaises(ValueError):
                    export_translation_markdown.export_markdown({}, units, [], {"entries": reviews})

    def test_layout_validation_rejects_missing_duplicate_and_mismatched_consumption(self):
        units, reviews = self.rules(), self.reviews()
        _, _, _, placements, _ = self.export(units, reviews)
        report = {"display_review_ids": [item["math_review_id"] for item in placements],
                  "display_source_ids": [unit_id for item in placements for unit_id in item["ids"]]}
        layout = {"placements": placements, "rich_text": {"math_review": report,
                  "math_render_strategies": {"native-display-font": 3}}}
        self.assertEqual(check_translation.validate_display_ownership({}, units, layout, reviews), [])
        for change in ("missing", "duplicate", "mismatch", "summary"):
            broken = deepcopy(layout)
            if change == "missing": broken["placements"].pop()
            elif change == "duplicate": broken["placements"].append(deepcopy(placements[0]))
            elif change == "mismatch": broken["placements"][0]["ids"] = ["rule2"]
            else: broken["rich_text"]["math_review"]["display_review_ids"] = []
            self.assertTrue(check_translation.validate_display_ownership({}, units, broken, reviews), change)

    def test_preparation_invalidates_groups_changed_by_legitimate_source_repairs(self):
        for change in ("removed-owner", "suppressed-owner", "removed-label", "reclassified-owner", "moved-source"):
            with self.subTest(change=change):
                units = self.rules()
                reviews = {"rule1": {"kind": "display", "source_ids": ["rule1", "label1", "rule2"],
                           "tex": "old-reviewed-rule", "review_status": "manually_reviewed"}}
                if change == "removed-owner": units = [unit for unit in units if unit["id"] != "rule1"]
                elif change == "suppressed-owner": units[0]["render_suppressed"] = True
                elif change == "removed-label": units = [unit for unit in units if unit["id"] != "label1"]
                elif change == "reclassified-owner": units[0]["kind"] = "body"
                else: units[2]["page"] = 2
                invalidations = []
                groups = display_plan.default_display_groups(units, reviews, invalidations=invalidations)
                self.assertEqual(len(invalidations), 1)
                self.assertEqual(invalidations[0]["review_id"], "rule1")
                expected = [[unit["id"]] for unit in display_plan.active_display_units(units)]
                self.assertEqual([[unit["id"] for unit in group] for group in groups], expected)
        for invalid_reviews in (
            {"rule1": {"kind": "display", "source_ids": ["rule1", "rule1"]}},
            {"rule1": {"kind": "display", "source_ids": ["rule1", "rule2"]},
             "rule2": {"kind": "display", "source_ids": ["rule2"]}},
        ):
            with self.assertRaises(ValueError):
                display_plan.default_display_groups(self.rules(), invalid_reviews)

    @unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX is unavailable")
    def test_independent_and_explicit_grouped_rules_compile_all_labels_once(self):
        for grouped in (False, True):
            with self.subTest(grouped=grouped), tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
                units, reviews = self.rules(), self.reviews()
                if grouped:
                    reviews = {"rule1": {"kind": "display", "source_ids": [item["id"] for item in units],
                               "tex": r"\begin{gathered}x_1=y_1\quad(1)\\x_2=y_2\quad(2)\\x_3=y_3\quad(3)\end{gathered}",
                               "review_status": "manually_reviewed"}}
                plan = display_plan.build_display_plan(units, reviews)
                nodes = render_translation.build_flow_nodes(units, {}, display_plan=plan)
                source.new_page()
                work = Path(directory)
                tex, placements, _ = native_latex.build_body_tex(nodes, reviews, ZH_ACADEMIC_V1, source, work)
                display_plan.validate_display_consumption(plan, placements)
                path = work / "rules.tex"
                path.write_text(tex, encoding="utf-8")
                pdf, metrics = native_latex.compile_body_tex(work, path)
                self.assertEqual(metrics["missing_glyphs"], 0)
                with pymupdf.open(pdf) as document:
                    page = document[0]
                    labels = [page.search_for(f"({index})") for index in range(1, 4)]
                    self.assertEqual([len(found) for found in labels], [1, 1, 1])
                    self.assertLess(labels[0][0].y0, labels[1][0].y0)
                    self.assertLess(labels[1][0].y0, labels[2][0].y0)
                    self.assertFalse(page.get_images())


class SourceListFlowTests(unittest.TestCase):
    def units(self):
        def unit(unit_id, order, text, box, spans):
            return {"id": unit_id, "order": order, "page": 1, "bbox": box,
                    "kind": "body", "render_mode": "text", "translatable": True,
                    "source_text": text, "font_size": 10,
                    "span_runs": [{"text": value, "bbox": bbox, "line": 0} for value, bbox in spans]}
        units = [
            unit("first", 1, "1. First item ends.", [50, 100, 300, 110],
                 [("1.", [50, 100, 60, 110]), ("First item ends.", [65, 100, 300, 110])]),
            unit("continued", 2, "Continuation stays in the item.", [65, 115, 300, 125],
                 [("Continuation stays in the item.", [65, 115, 300, 125])]),
            unit("second", 3, "2. Second item.", [50, 130, 300, 140],
                 [("2.", [50, 130, 60, 140]), ("Second item.", [65, 130, 300, 140])]),
            unit("after", 4, "After the list.", [50, 150, 300, 160],
                 [("After the list.", [50, 150, 300, 160])]),
        ]
        source_structure.annotate_source_flow(units)
        return units

    def translations(self):
        return {"first": "1. First list item.[[BR]]", "continued": "Continuation stays in item.",
                "second": "2. Second list item.", "after": "After list paragraph."}

    def test_source_structure_drives_flow_without_punctuation_inference(self):
        units = self.units()
        self.assertEqual([unit["flow_role"] for unit in units],
                         ["list_item", "list_continuation", "list_item", "paragraph"])
        nodes = render_translation.build_flow_nodes(units, self.translations(), display_plan=[])
        self.assertEqual([node["ids"] for node in nodes], [["first", "continued"], ["second"], ["after"]])
        self.assertEqual([node["flow_role"] for node in nodes], ["list-item", "list-item", "paragraph-start"])
        self.assertEqual(nodes[0]["source_marker"], "1.")
        self.assertNotIn("[[PAR]]", nodes[0]["text"])
        # A structured paragraph stays a paragraph even if its translation begins with a bullet.
        self.assertEqual(render_translation.flow_role(units[-1], "• translation"), "paragraph-start")

    def test_markdown_continuation_stays_inside_source_list(self):
        units = self.units()
        translations = [{"id": unit_id, "translated_text": value.replace("[[BR]]", "")}
                        for unit_id, value in self.translations().items()]
        markdown = export_translation_markdown.export_markdown({}, units, translations)
        self.assertIn("1. First list item. Continuation stays in item.\n2. Second list item.\n\nAfter list paragraph.", markdown)

    def test_inferred_paragraph_keeps_source_continuity_but_reviewed_boundary_does_not(self):
        first, second = self.units()[0], self.units()[-1]
        first.update({"source_text": "An unfinished", "flow_role": "paragraph", "list_id": None,
                      "source_marker": "", "list_level": 0})
        second.update({"page": 2, "source_text": "source sentence ends."})
        translations = {first["id"]: "First source fragment", second["id"]: "continues here."}
        nodes = render_translation.build_flow_nodes([first, second], translations)
        self.assertEqual(len(nodes), 1)
        second["flow_reviewed"] = True
        nodes = render_translation.build_flow_nodes([first, second], translations)
        self.assertEqual(len(nodes), 2)

    @unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX is unavailable")
    def test_source_to_tex_to_pdf_list_continuation_and_post_list_positions(self):
        units = self.units()
        nodes = render_translation.build_flow_nodes(units, self.translations(), display_plan=[])
        with tempfile.TemporaryDirectory() as directory, pymupdf.open() as source:
            work = Path(directory)
            source.new_page()
            tex, placements, _ = native_latex.build_body_tex(nodes, {}, ZH_ACADEMIC_V1, source, work)
            self.assertIn(r"\leftskip=23pt\parindent=-11.5pt", tex)
            self.assertNotIn(r"\hangindent", tex)
            self.assertEqual(placements[0]["source_flow_role"], "list_item")
            path = work / "lists.tex"
            path.write_text(tex, encoding="utf-8")
            pdf, metrics = native_latex.compile_body_tex(work, path)
            self.assertEqual(metrics["missing_glyphs"], 0)
            with pymupdf.open(pdf) as document:
                page = document[0]
                marker = page.search_for("1.")[0]
                continuation = page.search_for("Continuation")[0]
                after = page.search_for("After list paragraph.")[0]
                second = page.search_for("2.")[0]
                self.assertAlmostEqual(marker.x0, ZH_ACADEMIC_V1.margin_left + 11.5, delta=0.6)
                self.assertAlmostEqual(second.x0, marker.x0, delta=0.6)
                self.assertAlmostEqual(continuation.x0, ZH_ACADEMIC_V1.margin_left + 23, delta=0.6)
                self.assertAlmostEqual(after.x0, ZH_ACADEMIC_V1.margin_left + 23, delta=0.6)
                self.assertLess(continuation.y0, second.y0)
                self.assertLess(second.y0, after.y0)


class ProductionEntryTests(unittest.TestCase):
    def test_pdf_and_markdown_entrypoints_require_current_source_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            manifest = work / "manifest.json"
            manifest.write_text("{}", encoding="utf-8")
            args = Namespace(manifest=manifest, translations=work / "missing.jsonl",
                             output=work / "output.pdf", typography_profile=render_translation.TYPOGRAPHY_PROFILE,
                             gutter=18.0, title=None, listing_layout=None)
            for module in (render_translation, export_translation_markdown):
                with self.subTest(module=module.__name__), mock.patch.object(module, "parse_args", return_value=args), \
                     mock.patch("paper_preflight.assert_source_ready", side_effect=ValueError("Source preflight is stale")):
                    with self.assertRaisesRegex(SystemExit, "preflight is stale"):
                        module.main()
                    self.assertFalse(args.output.exists())

    def test_successful_production_updates_recovery_without_losing_assignments_or_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            source = work / "source.pdf"
            with pymupdf.open() as document:
                document.new_page()
                document.save(source)
            source_hash = render_translation.sha256_file(source)
            manifest_path = work / "manifest.json"
            manifest = {"source_pdf": str(source), "source_sha256": source_hash,
                        "assignment_protocol_version": 1,
                        "boundary": {"introduction_id": "body", "needs_boundary_review": False},
                        "paths": {"translation_units": "units.jsonl", "math_review": "math-review.json",
                                  "run_state": "run-state.json"},
                        "pages": [{"page": 1, "width": 595, "height": 842, "columns": 1}], "page_count": 1}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            unit = {"id": "body", "order": 1, "page": 1, "bbox": [50, 50, 250, 70],
                    "kind": "body", "render_mode": "text", "translatable": True,
                    "source_text": "Short source paragraph.", "flow_role": "paragraph", "flow_reviewed": True,
                    "list_id": None, "list_level": 0, "source_marker": ""}
            (work / "units.jsonl").write_text(json.dumps(unit) + "\n", encoding="utf-8")
            translations = work / "translations.jsonl"
            translations.write_text(json.dumps({"id": "body", "translated_text": "译文段落。"}) + "\n", encoding="utf-8")
            (work / "math-review.json").write_text(json.dumps({"schema_version": 1, "source_sha256": source_hash,
                                                              "entries": {}}), encoding="utf-8")
            initial = {"source_sha256": source_hash, "assignments": {"producer": "worker-1"},
                       "accepted_reports": {"dispatch": "accepted"}, "input_hashes": {"earlier": "keep"}}
            state_path = work / "run-state.json"
            state_path.write_text(json.dumps(initial), encoding="utf-8")
            output = work / "translated.pdf"
            args = Namespace(manifest=manifest_path, translations=translations, output=output,
                             typography_profile=render_translation.TYPOGRAPHY_PROFILE, gutter=18.0)

            def render(**kwargs):
                with pymupdf.open() as document:
                    document.new_page()
                    document.save(kwargs["output"])
                kwargs["output"].with_suffix(".layout.json").write_text("{}", encoding="utf-8")

            with mock.patch.object(render_translation, "parse_args", return_value=args), \
                 mock.patch.object(render_translation, "find_typography_fonts", return_value=None), \
                 mock.patch("paper_preflight.assert_source_ready", return_value={"source_fingerprint": {"inputs": {"source_pdf": source_hash}}}), \
                 mock.patch.object(native_latex, "render_native_latex", side_effect=render), \
                 mock.patch("builtins.print"):
                self.assertEqual(render_translation.main(), 0)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["stage"], "produced")
            self.assertEqual(state["assignments"], initial["assignments"])
            self.assertEqual(state["accepted_reports"], initial["accepted_reports"])
            self.assertEqual(state["input_hashes"]["earlier"], "keep")
            self.assertEqual(state["input_hashes"]["translations"], render_translation.sha256_file(translations))
            self.assertEqual(state["input_hashes"]["produced_pdf"], render_translation.sha256_file(output))


if __name__ == "__main__":
    unittest.main()
