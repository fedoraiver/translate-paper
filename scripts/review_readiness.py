# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Check recorded producer inspection before another full independent review.

This validates identities and recorded evidence, not whether an image was viewed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from assignment_identity import digest
from paper_state import atomic_json


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def defects(review: dict) -> list[dict]:
    result = {}
    for page in review.get("pages", []):
        for finding in page.get("findings", []):
            item = {"symptom": finding} if isinstance(finding, str) else dict(finding)
            item.setdefault("page", page["page"])
            item.setdefault("category", "layout")
            item.setdefault("source_unit", None)
            stable = {key: item.get(key) for key in ("category", "source_unit", "symptom")}
            if not item.get("source_unit"):
                stable["initial_page"] = item["page"]
            item.setdefault("defect_id", "defect-" + digest(stable)[:16])
            result[item["defect_id"]] = item
    for issue in review.get("open_issues", []):
        item = {"symptom": issue} if isinstance(issue, str) else dict(issue)
        if not item.get("defect_id") and any(
                existing.get("symptom") == item.get("symptom") for existing in result.values()):
            continue
        item.setdefault("category", "layout")
        item.setdefault("source_unit", None)
        item.setdefault("defect_id", "defect-" + digest(item)[:16])
        result.setdefault(item["defect_id"], item)
    return list(result.values())


def check_readiness(translated: Path, prior_reviews: list[Path], repairs: dict) -> dict:
    errors = []
    expected_hashes = [sha256(path) for path in prior_reviews]
    if repairs.get("translated_sha256") != sha256(translated):
        errors.append("Producer inspection belongs to a different candidate PDF")
    if repairs.get("prior_review_sha256", []) != expected_hashes:
        errors.append("Prior defect reports changed or are missing from producer inspection")
    issues, occurrences = {}, Counter()
    for path in prior_reviews:
        prior = json.loads(path.read_text(encoding="utf-8-sig"))
        pages = prior.get("pages", [])
        count = prior.get("page_count")
        complete_count = (isinstance(count, int) and not isinstance(count, bool) and count > 0
                          and sorted(page.get("page", 0) for page in pages) == list(range(1, count + 1)))
        if (prior.get("status", "fail") not in {"pass", "fail"} or not complete_count
                or any(page.get("status") not in {"pass", "fail"} for page in pages)):
            errors.append("Finish prior page/source review before consolidating repairs")
        for issue in defects(prior):
            issues[issue["defect_id"]] = issue
            occurrences[issue["defect_id"]] += 1
    categories = Counter(issue.get("category", "layout") for issue in issues.values())
    records = repairs.get("repairs", [])
    supplied = [r.get("defect_id") for r in records]
    if len(set(supplied)) != len(supplied) or set(supplied) != set(issues):
        errors.append("Repair inspections must cover each prior defect exactly once")
    evidence = []
    for record in records:
        label = record.get("defect_id", "unknown")
        if record.get("status") != "resolved":
            errors.append(f"{label}: repair is not resolved")
        images = record.get("inspected_images", [])
        if not images:
            errors.append(f"{label}: missing actual repair-locus inspection")
        evidence.extend(images)
        systemic = (record.get("systemic") or
                    categories[issues.get(label, {}).get("category", "layout")] > 1)
        if systemic:
            related = record.get("related_occurrence_images", [])
            if not related or not record.get("scope"):
                errors.append(f"{label}: systemic class needs occurrence images and scope")
            evidence.extend(related)
        if record.get("repeated") or occurrences[label] > 1:
            regression = record.get("regression_evidence", [])
            if not regression:
                errors.append(f"{label}: repeated defect needs full-path regression evidence")
            evidence.extend(regression)
    risk_images = repairs.get("risk_images", [])
    if not risk_images:
        errors.append("Producer must inspect current risk pages before review")
    evidence.extend(risk_images)
    for value in evidence:
        path = Path(value)
        if not path.is_absolute() or not path.is_file():
            errors.append(f"Inspection evidence is missing or not absolute: {value}")
    return {"schema_version": 1, "status": "pass" if not errors else "fail",
            "translated_sha256": sha256(translated), "prior_review_sha256": expected_hashes,
            "defect_count": len(issues), "errors": errors,
            "limitation": "Checks recorded identities/coverage, not truth of actual image inspection"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--translated", required=True, type=Path)
    parser.add_argument("--prior-review", action="append", default=[], type=Path)
    parser.add_argument("--repairs", type=Path, help="Producer-authored JSON inspection evidence")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        if not args.repairs:
            print(json.dumps({"defects": [item for path in args.prior_review for item in
                              defects(json.loads(path.read_text(encoding="utf-8-sig")))]},
                             ensure_ascii=False, indent=2))
            return 0
        payload = json.loads(args.repairs.read_text(encoding="utf-8-sig"))
        result = check_readiness(args.translated, args.prior_review, payload)
        if args.report:
            protected = {p.resolve() for p in [args.translated, args.repairs, *args.prior_review]}
            if args.report.resolve() in protected:
                raise ValueError("Report must not overwrite input evidence")
            atomic_json(args.report, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "pass" else 1
    except (ValueError, TypeError, KeyError, OSError) as error:
        print(json.dumps({"status": "fail", "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
