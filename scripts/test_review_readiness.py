import json
import tempfile
import unittest
from pathlib import Path

from review_readiness import check_readiness, defects, sha256


class ReviewReadinessTests(unittest.TestCase):
    def test_actual_candidate_and_all_defects_need_recorded_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            pdf, prior, image, regression = [work / name for name in
                                           ("candidate.pdf", "prior.json", "page.png", "regression.log")]
            pdf.write_bytes(b"current candidate")
            image.write_bytes(b"image fixture")
            regression.write_text("full path regression passes")
            review = {"status": "fail", "page_count": 1, "pages": [{"page": 1, "status": "fail", "findings": [{"category": "list",
                       "source_unit": "u4", "symptom": "list collapsed"}]}]}
            prior.write_text(json.dumps(review), encoding="utf-8")
            issue = defects(review)[0]
            moved = {"pages": [{"page": 6, "findings": [{"category": "list",
                     "source_unit": "u4", "symptom": "list collapsed"}]}]}
            self.assertEqual(issue["defect_id"], defects(moved)[0]["defect_id"])
            repairs = {"translated_sha256": sha256(pdf), "prior_review_sha256": [sha256(prior)],
                       "risk_images": [str(image)], "repairs": []}
            self.assertEqual(check_readiness(pdf, [prior], repairs)["status"], "fail")
            repairs["repairs"] = [{"defect_id": issue["defect_id"], "status": "resolved",
                                   "inspected_images": [str(image)], "systemic": True,
                                   "scope": "all list instances", "repeated": True}]
            self.assertEqual(check_readiness(pdf, [prior], repairs)["status"], "fail")
            repairs["repairs"][0].update(related_occurrence_images=[str(image)],
                                         regression_evidence=[str(regression)])
            self.assertEqual(check_readiness(pdf, [prior], repairs)["status"], "pass")
            incomplete = {**review, "status": "in_progress", "page_count": 4}
            prior.write_text(json.dumps(incomplete), encoding="utf-8")
            repairs["prior_review_sha256"] = [sha256(prior)]
            result = check_readiness(pdf, [prior], repairs)
            self.assertEqual(result["status"], "fail")
            self.assertTrue(any("Finish prior" in error for error in result["errors"]))
            incomplete["status"] = "fail"
            prior.write_text(json.dumps(incomplete), encoding="utf-8")
            repairs["prior_review_sha256"] = [sha256(prior)]
            self.assertEqual(check_readiness(pdf, [prior], repairs)["status"], "fail")
            prior.write_text(json.dumps(review), encoding="utf-8")
            repairs["prior_review_sha256"] = [sha256(prior)]
            pdf.write_bytes(b"different candidate")
            self.assertEqual(check_readiness(pdf, [prior], repairs)["status"], "fail")


if __name__ == "__main__":
    unittest.main()
