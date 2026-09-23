"""The packaging gate must reject stale or incomplete visual review evidence."""
import copy
import tempfile
import unittest
from pathlib import Path

from check_visual_review import check_review_bundle, sha256, validate_visual_review


class VisualReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source, self.output = root / "source.pdf", root / "output.pdf"
        self.source.write_bytes(b"source")
        self.output.write_bytes(b"render one")
        self.report = {
            "schema_version": 1, "status": "pass", "page_count": 2,
            "source_sha256": sha256(self.source), "translated_sha256": sha256(self.output),
            "reviewer": {"role": "visual-review", "agent_id": "reviewer-1", "mode": "subagent"},
            "contact_sheet_reviewed": True, "open_issues": [],
            "pages": [{"page": n, "status": "pass", "image": f"page-{n}.png",
                       "checks": ["Read body and page margins; no clipping."], "findings": []}
                      for n in (1, 2)],
        }

    def check(self, report=None):
        return validate_visual_review(self.report if report is None else report,
                                      self.source, self.output, 2)

    def test_complete_review_passes_but_rerender_invalidates_it(self):
        self.assertEqual(self.check(), [])
        self.output.write_bytes(b"render two")
        self.assertTrue(self.check())

    def test_missing_duplicate_or_out_of_range_page_fails(self):
        for numbers in ([1], [1, 1], [1, 3], [True, 2]):
            report = copy.deepcopy(self.report)
            report["pages"] = [dict(report["pages"][0], page=n) for n in numbers]
            self.assertTrue(self.check(report))

    def test_automated_pass_alone_is_not_visual_evidence(self):
        self.assertTrue(self.check({"status": "pass", "metrics": {"strict_invariants": True}}))
        for key, value in (("open_issues", ["clipping"]), ("contact_sheet_reviewed", False)):
            self.assertTrue(self.check({**self.report, key: value}))
        for key, value in (("image", ""), ("checks", []), ("findings", ["tiny font"]),
                           ("status", "fail")):
            report = copy.deepcopy(self.report)
            report["pages"][0][key] = value
            self.assertTrue(self.check(report))

    def test_unavailable_subagent_fallback_is_explicit(self):
        self.report["reviewer"]["mode"] = "coordinator-fallback"
        self.assertTrue(self.check())
        self.report["reviewer"]["fallback_reason"] = "Subagent tools unavailable in this session."
        self.assertEqual(self.check(), [])

    def test_new_pdf_hash_cannot_hide_stale_page_counts(self):
        strict = {"status": "pass", "metrics": {"strict_invariants": True, "translated_pages": 2}}
        quick = {"status": "pass", "metrics": {"pages": 2,
                 "pdf_sha256": sha256(self.output), "source_sha256": sha256(self.source)}}
        self.assertEqual(check_review_bundle(self.report, self.source, self.output, strict, quick), [])
        self.output.write_bytes(b"rerendered with three pages")
        self.report["translated_sha256"] = sha256(self.output)
        self.assertTrue(check_review_bundle(self.report, self.source, self.output, strict, quick))
        quick["metrics"].update(pdf_sha256=sha256(self.output), pages=3)
        self.assertTrue(check_review_bundle(self.report, self.source, self.output, strict, quick))
        strict["metrics"]["translated_pages"] = 3
        self.assertTrue(check_review_bundle(self.report, self.source, self.output, strict, quick))


if __name__ == "__main__":
    unittest.main()
