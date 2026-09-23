# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Check recorded visual-review coverage and PDF identity, not visual quality."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_visual_review(report: dict, source: Path, translated: Path,
                           page_count: int) -> list[str]:
    errors = []
    if report.get("schema_version") != 1 or report.get("status") != "pass":
        errors.append("Visual review must have schema_version 1 and status pass.")
    for key, path in (("source_sha256", source), ("translated_sha256", translated)):
        if not path.is_file() or report.get(key) != sha256(path):
            errors.append(f"Visual review {key} does not match the current PDF.")
    reviewer = report.get("reviewer")
    if not isinstance(reviewer, dict):
        errors.append("Visual reviewer identity is missing.")
    elif (reviewer.get("role") != "visual-review"
          or not str(reviewer.get("agent_id") or "").strip()
          or reviewer.get("mode") not in {"subagent", "coordinator-fallback"}):
        errors.append("Visual reviewer role, agent_id, or mode is invalid.")
    elif reviewer["mode"] == "coordinator-fallback" and not str(
        reviewer.get("fallback_reason") or ""
    ).strip():
        errors.append("Coordinator fallback requires an explanation.")
    if report.get("contact_sheet_reviewed") is not True:
        errors.append("Contact sheet has not been visually reviewed.")
    if report.get("open_issues") != []:
        errors.append("Visual review has unresolved or unrecorded issues.")
    pages = report.get("pages")
    if type(report.get("page_count")) is not int or report["page_count"] != page_count:
        errors.append("Visual review page_count differs from the validated PDF.")
    if not isinstance(pages, list) or any(not isinstance(p, dict) for p in pages):
        return errors + ["Visual review pages must be a list of page records."]
    numbers = [p.get("page") for p in pages]
    if (type(page_count) is not int or page_count < 1
        or any(type(n) is not int for n in numbers)
        or sorted(numbers) != list(range(1, page_count + 1))):
        errors.append("Every output page must be reviewed exactly once.")
    for page in pages:
        checks = page.get("checks")
        if (page.get("status") != "pass" or page.get("findings") != []
            or not isinstance(page.get("image"), str) or not page["image"].strip()
            or not isinstance(checks, list) or not checks
            or any(not isinstance(c, str) or not c.strip() for c in checks)):
            errors.append(f"Page {page.get('page')}: incomplete evidence or unresolved findings.")
    return errors


def check_review_bundle(report: dict, source: Path, translated: Path,
                        strict: dict, quick: dict) -> list[str]:
    """Bind page coverage to the current PDF using the existing quick validator."""
    metrics = quick.get("metrics") or {}
    if (strict.get("status") != "pass"
        or (strict.get("metrics") or {}).get("strict_invariants") is not True
        or quick.get("status") != "pass"):
        return ["Passing strict-invariants and quick reports are required."]
    if (not translated.is_file() or metrics.get("pdf_sha256") != sha256(translated)
        or not source.is_file() or metrics.get("source_sha256") != sha256(source)):
        return ["Quick validation is stale for the current source/output PDF; rerun it."]
    if (strict.get("metrics") or {}).get("translated_pages") != metrics.get("pages"):
        return ["Strict and current quick validation page counts disagree."]
    return validate_visual_review(report, source, translated, metrics.get("pages"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--translated", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--strict-report", required=True, type=Path)
    parser.add_argument("--quick-report", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = json.loads(args.review.read_text(encoding="utf-8"))
        strict = json.loads(args.strict_report.read_text(encoding="utf-8"))
        quick = json.loads(args.quick_report.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("Visual review must be a JSON object.")
        errors = check_review_bundle(report, args.source, args.translated, strict, quick)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        errors = [str(error)]
    print(json.dumps({"status": "fail" if errors else "pass", "errors": errors},
                     ensure_ascii=False, indent=2))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
