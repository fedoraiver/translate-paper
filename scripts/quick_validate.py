#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pymupdf>=1.26.0,<2",
# ]
# ///
"""Run fast structural checks on a generated translated-paper PDF."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import pymupdf


UNRESOLVED_TEXT = ("[[",)
SUSPICIOUS_GLYPHS = ("\ufffd", "\u25a1", "\u25a0")


def source_contains_glyph(marker: str, source_text: str) -> bool:
    """Recognize a TeX proof square extracted as its two component glyphs."""
    return marker in source_text or (
        marker == "\u25a1" and re.search(r"⊓\s*⊔", source_text) is not None
    )


def reviewed_proof_square_count(
    math_review: Path | None, source: Path | None,
) -> int:
    """Count accepted standalone square formulas bound to the actual PDF.

    Malformed, absent, stale, or unresolved evidence grants no allowance. Do not
    infer squares from source OCR (legacy Type3 squares may extract as ``ut``),
    prose declarations, or formulas that merely contain a square command.
    """
    if math_review is None or source is None or not source.is_file():
        return 0
    try:
        payload = json.loads(math_review.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("source_sha256") != sha256(source)
            or not isinstance(payload.get("entries"), dict)
        ):
            return 0
    except (OSError, ValueError):
        return 0
    count = 0
    for entry in payload["entries"].values():
        if not isinstance(entry, dict) or entry.get("review_status") not in {
            "auto_validated", "manually_reviewed",
        }:
            continue
        tex = re.sub(r"\s+", "", str(entry.get("tex") or ""))
        while tex.startswith("{") and tex.endswith("}"):
            tex = tex[1:-1]
        if tex in {r"\square", r"\Box", r"\mdlgwhtsquare"}:
            count += 1
    return count


def suspicious_glyphs(
    output_text: str, source_text: str, reviewed_proof_squares: int = 0,
) -> list[str]:
    """Report unexplained markers, limiting proof-square evidence by count."""
    source_squares = source_text.count("□") + len(re.findall(r"⊓\s*⊔", source_text))
    # A reviewed formula and source extraction can describe the same square.
    # Adding them would incorrectly grant a second allowance for one proof end.
    square_limit = max(source_squares, reviewed_proof_squares)
    return [
        marker for marker in SUSPICIOUS_GLYPHS
        if (
            output_text.count(marker) > square_limit if marker == "□"
            else marker in output_text and not source_contains_glyph(marker, source_text)
        )
    ]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_pdf(
    translated: Path,
    source: Path | None = None,
    expected_source_sha256: str = "",
    math_review: Path | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    metrics: dict[str, Any] = {}
    source_glyph_text = ""
    if not translated.is_file():
        return {
            "status": "fail",
            "errors": [f"Translated PDF not found: {translated}"],
            "metrics": metrics,
        }
    if source is not None:
        if not source.is_file():
            errors.append(f"Source PDF not found: {source}")
        else:
            source_hash = sha256(source)
            metrics["source_sha256"] = source_hash
            if (
                expected_source_sha256
                and source_hash.casefold()
                != expected_source_sha256.casefold()
            ):
                errors.append("Source PDF hash changed.")
            try:
                source_document = pymupdf.open(source)
                try:
                    source_glyph_text = "\n".join(
                        page.get_text("text", sort=True)
                        for page in source_document
                    )
                finally:
                    source_document.close()
            except Exception as error:
                errors.append(f"Source PDF could not be inspected: {error}")

    try:
        document = pymupdf.open(translated)
    except Exception as error:
        return {
            "status": "fail",
            "errors": [f"Translated PDF could not be opened: {error}"],
            "metrics": metrics,
        }

    try:
        metrics["pages"] = len(document)
        metrics["bytes"] = translated.stat().st_size
        metrics["pdf_sha256"] = sha256(translated)
        if not document.is_pdf:
            errors.append("Output is not a PDF document.")
        if document.needs_pass:
            errors.append("Output PDF unexpectedly requires a password.")
        if not len(document):
            errors.append("Output PDF has no pages.")

        blank_pages: list[int] = []
        searchable_pages = 0
        output_texts: list[str] = []
        for index, page in enumerate(document):
            rect = page.rect
            coordinates = (rect.x0, rect.y0, rect.x1, rect.y1)
            if (
                not all(math.isfinite(value) for value in coordinates)
                or rect.width <= 0
                or rect.height <= 0
            ):
                errors.append(f"Page {index + 1} has an invalid media box.")
                continue
            text = page.get_text("text")
            output_texts.append(text)
            if text.strip():
                searchable_pages += 1
            if any(marker in text for marker in UNRESOLVED_TEXT):
                errors.append(
                    f"Page {index + 1} contains an unresolved or suspicious glyph."
                )
            if (
                not text.strip()
                and not page.get_images(full=True)
                and not page.get_drawings()
            ):
                blank_pages.append(index + 1)
            try:
                page.get_pixmap(
                    matrix=pymupdf.Matrix(0.25, 0.25), alpha=False
                )
            except Exception as error:
                errors.append(f"Page {index + 1} failed to render: {error}")

        reviewed_squares = reviewed_proof_square_count(math_review, source)
        metrics["reviewed_proof_squares"] = reviewed_squares
        for marker in suspicious_glyphs(
            "\n".join(output_texts), source_glyph_text, reviewed_squares
        ):
            errors.append(f"Output contains suspicious glyph {marker!r}.")
        metrics["searchable_pages"] = searchable_pages
        metrics["blank_pages"] = blank_pages
        if blank_pages:
            errors.append(
                "Output contains blank pages: "
                + ", ".join(str(page) for page in blank_pages)
            )
        if searchable_pages == 0:
            errors.append("Output PDF has no searchable text layer.")
    finally:
        document.close()

    return {
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "metrics": metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--translated", required=True, type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--expected-source-sha256", default="")
    parser.add_argument("--math-review", type=Path,
                        help="Optional source-hash-bound native math review")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    report = validate_pdf(
        args.translated,
        args.source,
        args.expected_source_sha256,
        args.math_review,
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
