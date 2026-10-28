#!/usr/bin/env python3
"""Source-bound proof-square evidence must not whitelist missing-glyph boxes."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import pymupdf
import quick_validate


class ReviewedProofSquareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work / "source.pdf"
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((50, 50), "Legacy proof ending extracts as ut.")
            document.save(self.source)
        self.review = self.work / "math-review.json"

    def output(self, squares=1):
        path = self.work / f"output-{squares}.pdf"
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((50, 50), "Native proof ending.")
            # PyMuPDF's bundled CJK font provides an actual searchable square.
            page.insert_text((50, 80), "□" * squares, fontname="china-s")
            document.save(path)
        return path

    def write_review(self, status="manually_reviewed", tex=r"{\square}", stale=False):
        self.review.write_text(json.dumps({
            "schema_version": 1,
            "source_sha256": "0" * 64 if stale else quick_validate.sha256(self.source),
            "entries": {"[[Fproof]]": {"tex": tex, "review_status": status}},
        }), encoding="utf-8")

    def validate(self, output, review=True):
        return quick_validate.validate_pdf(
            output, self.source, math_review=self.review if review else None)

    def test_accepted_standalone_proof_square_is_allowed(self):
        for status in ("manually_reviewed", "auto_validated"):
            for tex in (r"{\square}", r"\Box", r"{{\mdlgwhtsquare}}"):
                with self.subTest(status=status, tex=tex):
                    self.write_review(status=status, tex=tex)
                    report = self.validate(self.output())
                    self.assertEqual(report["status"], "pass", report)
                    self.assertEqual(report["metrics"]["reviewed_proof_squares"], 1)

    def test_absent_stale_and_unresolved_review_do_not_explain_square(self):
        output = self.output()
        self.assertEqual(self.validate(output, review=False)["status"], "fail")
        for fields in ({"stale": True}, {"status": "unresolved"},
                       {"tex": r"x+\square"}):
            with self.subTest(fields=fields):
                self.write_review(**fields)
                self.assertEqual(self.validate(output)["status"], "fail")

    def test_one_reviewed_square_does_not_hide_an_extra_square(self):
        self.write_review()
        report = self.validate(self.output(squares=2))
        self.assertEqual(report["status"], "fail")
        self.assertTrue(any("suspicious glyph" in e for e in report["errors"]))

    def test_square_allowance_is_document_wide_not_per_page(self):
        self.write_review()
        output = self.work / "two-pages.pdf"
        with pymupdf.open() as document:
            for _ in range(2):
                document.new_page().insert_text((50, 50), "□", fontname="china-s")
            document.save(output)
        self.assertEqual(self.validate(output)["status"], "fail")

    def test_source_and_review_do_not_double_count_or_allow_other_markers(self):
        self.assertEqual(quick_validate.suspicious_glyphs("□□", "□", 1), ["□"])
        self.assertEqual(quick_validate.suspicious_glyphs("□", "⊓\n⊔", 1), [])
        self.assertEqual(quick_validate.suspicious_glyphs("�■", "ut", 1), ["�", "■"])

    def test_review_requires_the_actual_source(self):
        self.write_review()
        report = quick_validate.validate_pdf(self.output(), math_review=self.review)
        self.assertEqual(report["status"], "fail")


if __name__ == "__main__":
    unittest.main()
