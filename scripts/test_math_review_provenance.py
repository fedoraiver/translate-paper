"""Generated mathematics refreshes safely while reviewed mathematics stays bound."""

from __future__ import annotations

import copy
import unittest

import prepare_paper
from display_plan import build_display_plan


def inline(tex="z_i"):
    return {
        "id": "body",
        "order": 1,
        "page": 1,
        "translatable": True,
        "kind": "body",
        "source_text": "Use zi here.",
        "inline_fragments": {
            "[[Fbody_0001]]": {
                "kind": "math",
                "text": "zi",
                "tex": tex,
                "source_bbox": [50, 80, 65, 94],
                "review_status": "auto_validated",
                "runs": [
                    {"text": "z", "font": "CMMI10", "size": 10},
                    {"text": "i", "font": "CMMI7", "size": 7},
                ],
            }
        },
    }


def equation(identifier, order, text, **values):
    return {
        "id": identifier,
        "order": order,
        "page": 1,
        "translatable": False,
        "kind": "equation",
        "render_mode": "source_clip",
        "source_text": text,
        "bbox": [60, 90 + order * 20, 350, 105 + order * 20],
        **values,
    }


class MathReviewProvenanceTests(unittest.TestCase):
    def test_automatic_tex_refreshes_when_previous_generated_hash_matches(self):
        previous = prepare_paper.build_math_review([inline()], "source")
        current = prepare_paper.build_math_review(
            [inline(r"z_{i}")], "source", previous
        )
        entry = current["entries"]["[[Fbody_0001]]"]
        self.assertEqual(entry["tex"], r"z_{i}")
        self.assertEqual(entry["review_status"], "auto_validated")
        self.assertEqual(len(entry["source_fingerprint"]), 64)
        self.assertNotEqual(
            entry["generated_provenance"]["tex_sha256"],
            previous["entries"]["[[Fbody_0001]]"]["generated_provenance"]["tex_sha256"],
        )

    def test_edited_or_legacy_automatic_tex_is_not_overwritten_or_blessed(self):
        for legacy in (False, True):
            previous = prepare_paper.build_math_review([inline()], "source")
            entry = previous["entries"]["[[Fbody_0001]]"]
            entry["tex"] = r"\widehat{z}_i"
            if legacy:
                del entry["generated_provenance"]
            current = prepare_paper.build_math_review(
                [inline(r"z_{i}")], "source", previous
            )
            with self.subTest(legacy=legacy):
                self.assertEqual(
                    current["entries"]["[[Fbody_0001]]"]["tex"], r"\widehat{z}_i"
                )
                self.assertEqual(
                    current["entries"]["[[Fbody_0001]]"]["review_status"], "unresolved"
                )

    def test_manual_tex_survives_generator_changes_but_not_source_changes(self):
        previous = prepare_paper.build_math_review([inline()], "source")
        reviewed = previous["entries"]["[[Fbody_0001]]"]
        reviewed.update({"review_status": "manually_reviewed", "tex": r"\widehat{z}_i"})
        current = prepare_paper.build_math_review(
            [inline(r"z_{i}")], "source", previous
        )
        self.assertEqual(current["entries"]["[[Fbody_0001]]"]["tex"], r"\widehat{z}_i")
        self.assertEqual(
            current["entries"]["[[Fbody_0001]]"]["review_status"], "manually_reviewed"
        )
        changed = inline()
        changed["inline_fragments"]["[[Fbody_0001]]"]["runs"][1]["font"] = (
            "ChangedSourceFont"
        )
        current = prepare_paper.build_math_review([changed], "source", previous)
        self.assertEqual(
            current["entries"]["[[Fbody_0001]]"]["review_status"], "unresolved"
        )
        self.assertEqual(
            current["entries"]["[[Fbody_0001]]"]["previous_review_tex"],
            r"\widehat{z}_i",
        )
        current = prepare_paper.build_math_review([inline()], "different-pdf", previous)
        self.assertEqual(
            current["entries"]["[[Fbody_0001]]"]["review_status"], "unresolved"
        )

    def test_adjacent_displays_are_separate_unless_review_explicitly_owns_group(self):
        items = [equation("eq-a", 1, "a=b"), equation("eq-b", 2, "c=d")]
        initial = prepare_paper.build_math_review(items, "source")
        self.assertEqual(list(initial["entries"]), ["eq-a", "eq-b"])
        reviewed = {
            "source_sha256": "source",
            "entries": {
                "eq-a": {
                    "kind": "display",
                    "source_ids": ["eq-a", "eq-b"],
                    "source_page": 1,
                    "source_text": "a=b c=d",
                    "tex": r"a=b\\c=d",
                    "review_status": "manually_reviewed",
                }
            },
        }
        current = prepare_paper.build_math_review(items, "source", reviewed)
        self.assertEqual(list(current["entries"]), ["eq-a"])
        self.assertEqual(
            current["entries"]["eq-a"]["review_status"], "manually_reviewed"
        )
        self.assertEqual(
            build_display_plan(items, current["entries"])[0]["source_ids"],
            ["eq-a", "eq-b"],
        )

    def test_explicit_grouped_label_before_equation_keeps_active_owner(self):
        items = [
            equation("label", 1, "(7)", render_suppressed=True),
            equation("rule", 2, "a=b"),
        ]
        previous = {
            "source_sha256": "source",
            "entries": {
                "rule": {
                    "kind": "display",
                    "source_ids": ["label", "rule"],
                    "source_page": 1,
                    "source_text": "(7) a=b",
                    "tex": r"a=b\tag{7}",
                    "review_status": "manually_reviewed",
                }
            },
        }
        current = prepare_paper.build_math_review(items, "source", previous)
        self.assertEqual(list(current["entries"]), ["rule"])
        self.assertEqual(
            build_display_plan(items, current["entries"])[0]["review_id"], "rule"
        )

    def test_display_review_invalidates_when_owned_source_geometry_changes(self):
        items = [equation("rule", 1, "a=b")]
        previous = prepare_paper.build_math_review(items, "source")
        previous["entries"]["rule"].update(
            {"tex": "a=b", "review_status": "manually_reviewed"}
        )
        changed = copy.deepcopy(items)
        changed[0]["bbox"][0] += 1
        current = prepare_paper.build_math_review(changed, "source", previous)
        self.assertEqual(current["entries"]["rule"]["review_status"], "unresolved")

    def test_reviewed_boundary_includes_display_after_last_prose(self):
        items = [
            inline(),
            equation("last-display", 2, "a=b"),
            {
                "id": "references",
                "order": 3,
                "page": 1,
                "kind": "heading",
                "source_text": "References",
                "translatable": False,
            },
        ]
        review = prepare_paper.build_math_review(
            items,
            "source",
            boundary={"introduction_id": "body", "post_body_stop_id": "references"},
        )
        self.assertIn("last-display", review["entries"])

    def test_repaired_display_membership_regenerates_unresolved_ownership(self):
        original = [equation("rule", 1, "a=b"), equation("old-label", 2, "(7)")]
        previous = {
            "source_sha256": "source",
            "entries": {
                "rule": {
                    "kind": "display",
                    "source_ids": ["rule", "old-label"],
                    "source_page": 1,
                    "source_text": "a=b (7)",
                    "tex": r"a=b\tag{7}",
                    "review_status": "manually_reviewed",
                }
            },
        }
        current = prepare_paper.build_math_review(original[:1], "source", previous)
        self.assertEqual(current["entries"]["rule"]["source_ids"], ["rule"])
        self.assertEqual(current["entries"]["rule"]["review_status"], "unresolved")
        self.assertEqual(current["invalidated_reviews"][0]["review_id"], "rule")
        self.assertEqual(
            current["invalidated_reviews"][0]["previous_review_tex"], r"a=b\tag{7}"
        )


if __name__ == "__main__":
    unittest.main()
