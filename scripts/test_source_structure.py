"""Source structure regressions independent of translated wording or live papers."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import prepare_paper
import source_review
import source_structure


def span(text, *, line=0, bbox=None, **values):
    return {
        "text": text,
        "line": line,
        "bbox": bbox or [50, 90, 350, 102],
        "origin": [50, 100],
        "size": 10,
        "style": "text",
        "flags": 0,
        "font": "Times-Roman",
        **values,
    }


def unit(text, runs=None, **values):
    return {
        "id": "u1",
        "order": 1,
        "page_order": 1,
        "page": 1,
        "kind": "body",
        "render_mode": "text",
        "translatable": True,
        "source_text": text,
        "span_runs": runs or [],
        "font_size": 10,
        "bbox": [50, 90, 350, 102],
        "section": "1 Introduction",
        **values,
    }


class SourceFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(
            Path(__file__).with_suffix(".fixtures.json").read_text(encoding="utf-8")
        )

    def test_source_geometry_keeps_list_and_nested_continuations(self):
        records = []
        for case in self.fixtures["flow"]:
            box = case["bbox"]
            if case.get("marker"):
                content = case["source_text"][len(case["marker"]) :]
                runs = [
                    span(
                        case["marker"],
                        bbox=[box[0], box[1], case["content_x"] - 3, box[3]],
                    ),
                    span(content, bbox=[case["content_x"], box[1], box[2], box[3]]),
                ]
            else:
                runs = [span(case["source_text"], bbox=box)]
            records.append(
                unit(
                    case["source_text"],
                    runs,
                    id=case["id"],
                    order=case["order"],
                    bbox=box,
                )
            )
        source_structure.annotate_source_flow(records)
        for record, case in zip(records, self.fixtures["flow"]):
            self.assertEqual(record["flow_role"], case["expected_role"], record["id"])
            self.assertEqual(record["list_level"], case["expected_level"], record["id"])
            self.assertIsNone(source_structure.validate_flow(record))
            self.assertIs(record["flow_reviewed"], False)
        self.assertEqual({record["list_id"] for record in records[1:5]}, {"list-first"})
        self.assertEqual(source_review.structured_source_issues(records), [])

    def test_mixed_item_units_require_review_before_translation(self):
        for case in self.fixtures["mixed"]:
            with self.subTest(source=case["source_text"]):
                record = unit(
                    case["source_text"],
                    [span(text, line=i) for i, text in enumerate(case["lines"])],
                )
                source_structure.annotate_source_flow([record])
                self.assertIn("flow_review_required", record)
                self.assertIn(
                    "flow_ambiguous",
                    {
                        issue["code"]
                        for issue in source_review.structured_source_issues([record])
                    },
                )

    def test_list_markers_are_protected_using_source_metadata(self):
        for marker in ("•", "3.", "(b)"):
            record = unit(marker + " Exact source item.")
            source_structure.annotate_source_flow([record])
            prepare_paper.protect_element(record)
            fragments = list(record["inline_fragments"].values())
            self.assertEqual(
                [(part["kind"], part["text"]) for part in fragments],
                [("list-marker", marker)],
            )
            self.assertIn(fragments[0]["token"], record["protected_text"])

    def test_parenthetical_values_are_not_additional_list_items(self):
        text = (
            "1. A dead cell (i.e., binary value at a given location is 0) with exactly three "
            "live neighbors becomes a live cell (i.e., its value set to 1)."
        )
        record = unit(text, [span(text)])
        self.assertEqual(
            [marker["marker"] for marker in source_structure.source_markers(record)],
            ["1."],
        )
        source_structure.annotate_source_flow([record])
        self.assertEqual(record["flow_role"], "list_item")
        self.assertNotIn("flow_review_required", record)
        self.assertEqual(source_review.structured_source_issues([record]), [])

    def test_parenthetical_values_do_not_hide_genuine_inline_list_items(self):
        for lines in (
            ["1. First item (value is 0) with detail. 2. Second item."],
            ["1. First item (value is", "0) with detail. 2. Second item."],
            ["1. First item [value is 0) with detail]. 2. Second item."],
        ):
            with self.subTest(lines=lines):
                record = unit(
                    "\n".join(lines),
                    [span(text, line=i) for i, text in enumerate(lines)],
                )
                self.assertEqual(
                    [marker["marker"] for marker in source_structure.source_markers(record)],
                    ["1.", "2."],
                )
                source_structure.annotate_source_flow([record])
                self.assertIn("flow_review_required", record)

    def test_numeric_expression_and_section_heading_are_not_lists(self):
        records = [
            unit("1. + x", [span("1. + x", style="math")]),
            unit("2. Related Work", kind="heading", order=2),
        ]
        source_structure.annotate_source_flow(records)
        self.assertEqual(records[0]["flow_role"], "paragraph")
        self.assertEqual(records[1]["flow_role"], "paragraph")

    def test_source_list_misclassified_heading_requires_explicit_kind_review(self):
        records = [
            unit("Source paragraph.", font_size=10),
            unit("1. First source item.", kind="heading", order=2, font_size=10),
        ]
        issues = source_review.structured_source_issues(records)
        self.assertEqual(
            [issue["code"] for issue in issues], ["heading_list_ambiguous"]
        )
        records[1]["source_review_reason"] = (
            "Verified that the source uses a heading here."
        )
        self.assertEqual(source_review.structured_source_issues(records), [])
        records[1].pop("source_review_reason")
        records[1]["font_size"] = 14
        self.assertEqual(source_review.structured_source_issues(records), [])

    def test_review_can_split_mixed_unit_and_assign_source_flow(self):
        records = [unit("1. First item.\n2. Second item.")]
        source_structure.annotate_source_flow(records)
        review = {
            "schema_version": 1,
            "source_sha256": "source",
            "reviewed": True,
            "reason": "Read source.",
            "operations": [
                {
                    "op": "replace_unit",
                    "id": "u1",
                    "source_text": "1. First item.",
                    "flow_role": "list_item",
                    "list_id": "review-list",
                    "list_level": 0,
                    "source_marker": "1.",
                    "reason": "Split the first visible item.",
                },
                {
                    "op": "insert_unit",
                    "id": "review-p0001-u0001",
                    "after_id": "u1",
                    "source_text": "2. Second item.",
                    "bbox": [50, 106, 350, 118],
                    "flow_role": "list_item",
                    "list_id": "review-list",
                    "list_level": 0,
                    "source_marker": "2.",
                    "reason": "Split the second visible item.",
                },
            ],
        }
        source_review.apply_source_review(
            records,
            review,
            "source",
            [{"page": 1, "width": 600, "height": 800}],
            prepare_paper.protect_element,
        )
        self.assertEqual(source_review.structured_source_issues(records), [])
        self.assertEqual([record["list_id"] for record in records], ["review-list"] * 2)
        self.assertTrue(all(record["flow_reviewed"] for record in records))

    def test_invalid_review_flow_is_rejected_atomically(self):
        original = [unit("Source paragraph.")]
        for fields in (
            {"flow_role": "list_item"},
            {"flow_role": "paragraph", "list_id": "list"},
            {"list_level": -1},
            {"list_level": True},
            {"flow_role": "guessed"},
            {"flow_reviewed": "true"},
        ):
            records = copy.deepcopy(original)
            review = {
                "reviewed": True,
                "operations": [
                    {
                        "op": "replace_unit",
                        "id": "u1",
                        "reason": "Source flow correction.",
                        **fields,
                    }
                ],
            }
            with (
                self.subTest(fields=fields),
                self.assertRaises(source_review.SourceReviewError),
            ):
                source_review.apply_source_review(
                    records,
                    review,
                    "source",
                    [{"page": 1, "width": 600, "height": 800}],
                    prepare_paper.protect_element,
                )
            self.assertEqual(records, original)

    def test_completed_flow_cannot_change_on_review_or_reconciliation(self):
        before = unit("• Source list item.")
        source_structure.annotate_source_flow([before])
        prepare_paper.protect_element(before)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "translations.jsonl"
            checkpoint.write_text(
                json.dumps({**before, "translated_text": "已有译文"}) + "\n",
                encoding="utf-8",
            )
            (root / "translation-units.jsonl").write_text(
                json.dumps(before) + "\n", encoding="utf-8"
            )
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "source_sha256": "source",
                        "paths": {"translation_units": "translation-units.jsonl"},
                    }
                ),
                encoding="utf-8",
            )
            for field, value in (
                ("flow_role", "list_continuation"),
                ("list_id", "different-list"),
                ("list_level", 1),
                ("source_marker", ""),
                ("flow_reviewed", True),
            ):
                changed = copy.deepcopy(before)
                changed[field] = value
                review = {
                    "reviewed": True,
                    "operations": [
                        {
                            "op": "replace_unit",
                            "id": "u1",
                            field: value,
                            "reason": "Proposed flow change.",
                        }
                    ],
                }
                with self.subTest(field=field):
                    with self.assertRaises(source_review.SourceReviewError):
                        source_review.apply_source_review(
                            [copy.deepcopy(before)],
                            review,
                            "source",
                            [{"page": 1, "width": 600, "height": 800}],
                            prepare_paper.protect_element,
                            translations_path=checkpoint,
                        )
                    with self.assertRaises(source_review.SourceReviewError):
                        source_review.reconcile_translations(checkpoint, [changed])


class SourceMathDiagnosticsTests(unittest.TestCase):
    def codes(self, text, spans):
        record = unit(text, spans)
        prepare_paper.protect_element(record)
        return {
            issue["code"] for issue in source_review.structured_source_issues([record])
        }

    def test_unmatched_and_ambiguous_math_locations_are_reviewed(self):
        self.assertIn(
            "unmatched_source_span",
            self.codes("Use z here.", [span("missing", style="math")]),
        )
        self.assertIn(
            "ambiguous_source_span", self.codes("z plus z", [span("z", style="math")])
        )
        located, issues = source_structure.locate_source_spans(
            "z plus z", [span("z"), span("plus"), span("z")]
        )
        self.assertEqual(len(located), 3)
        self.assertEqual(issues, [])

    def test_dehyphenated_lines_keep_every_span_and_exact_clean_text_offsets(self):
        cases = [
            ("manipulate images.", [span("ma-", line=0, style="italic"),
                                    span("nipulate images.", line=1)]),
            ("Input/output sends information between the parts.", [
                span("Input/out-", line=0), span("put sen-", line=1),
                span("ds information be-", line=2), span("tween the parts.", line=3)]),
            ("manipulate images.", [span("ma-\nnipulate images.")]),
            ("input/out-put remains hyphenated.", [span("input/out-", line=0),
                                                   span("put remains hyphenated.", line=1)]),
        ]
        for text, runs in cases:
            with self.subTest(text=text):
                before = copy.deepcopy(runs)
                located, issues = source_structure.locate_source_spans(text, runs)
                self.assertEqual(issues, [])
                self.assertEqual(len(located), len(runs))
                self.assertEqual(runs, before)
                self.assertEqual([run["_source_run_index"] for run in located], list(range(len(runs))))
                for run in located:
                    self.assertEqual(run["text"], text[run["start"]:run["end"]])
                self.assertEqual(self.codes(text, runs), set())

    def test_same_line_hyphen_is_not_silently_removed(self):
        located, issues = source_structure.locate_source_spans(
            "manipulate images.", [span("ma-", line=0), span("nipulate images.", line=0)])
        self.assertEqual(len(located), 1)
        self.assertIn("unmatched_source_span", {issue["code"] for issue in issues})

    def test_uncovered_math_is_generic_and_respects_reviewed_prose(self):
        for symbol in ("z", "ν", "𝑟"):
            with self.subTest(symbol=symbol):
                record = unit(
                    "Use " + symbol,
                    [span("Use"), span(symbol, style="math", font="CMMI10")],
                )
                self.assertIn(
                    "uncovered_math_span",
                    {
                        issue["code"]
                        for issue in source_review.structured_source_issues([record])
                    },
                )
        self.assertNotIn(
            "uncovered_math_span",
            self.codes("de-", [span("de-", style="math", semantic_kind="prose")]),
        )

    def test_numbered_display_in_mixed_body_requires_source_review(self):
        self.assertIn(
            "numbered_display_in_body",
            self.codes(
                "The argument continues.\nu = v + w (37)",
                [
                    span("The argument continues.", line=0),
                    span("u = v + w (37)", line=1, style="math"),
                ],
            ),
        )
        self.assertNotIn(
            "numbered_display_in_body",
            self.codes(
                "The argument follows from equation (37)",
                [span("The argument follows from equation (37)")],
            ),
        )

    def test_orphan_script_requires_base_and_protected_subscript_does_not(self):
        self.assertIn(
            "orphan_script",
            self.codes(
                "The value i", [span("The value"), span("i", size=7, style="math")]
            ),
        )
        codes = self.codes(
            "The value zi",
            [
                span("The value"),
                span("z", style="math", font="CMMI10"),
                span("i", size=7, style="math", font="CMMI7", origin=[60, 102]),
            ],
        )
        self.assertNotIn("orphan_script", codes)
        self.assertNotIn("uncovered_math_span", codes)


if __name__ == "__main__":
    unittest.main()
