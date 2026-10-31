#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pymupdf>=1.26.0,<2",
#   "pillow>=10.4.0,<13",
# ]
# ///
"""Validate and render-review a translated paper PDF.

The checks cover source integrity, translation-unit completeness, protected
tokens, Chinese body text, invariant source text, PDF extraction, page
rendering, and visible placeholder or missing-glyph markers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
from collections import Counter
from itertools import pairwise
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps
from display_plan import select_body_units
from typography import ZH_ACADEMIC_V1
from translation_checkpoint import is_preserved_formal_heading
from quick_validate import (
    reviewed_proof_square_count, source_contains_glyph, suspicious_glyphs,
)


HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")
PLACEHOLDER_RE = re.compile(r"\[\[P\d{4}\]\]")
CONTROL_TOKEN_RE = re.compile(
    r"\[\[(?:[BIE][A-Za-z0-9_]*_(?:OPEN|CLOSE)|"
    r"F[A-Za-z0-9_]+|BR|PAR)\]\]"
)
CITATION_LIKE_RE = re.compile(
    r"[\(（][^()（）\n]{0,160}\b(?:18|19|20)\d{2}[a-z]?"
    r"[^()（）\n]{0,80}[\)）]",
    re.IGNORECASE,
)
TYPOGRAPHY_PROFILE = ZH_ACADEMIC_V1.name


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a translated paper and render every page for review."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--translated", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--translations", required=True, type=Path)
    parser.add_argument("--preview-dir", required=True, type=Path)
    parser.add_argument("--dpi", type=int, default=120)
    parser.add_argument(
        "--strict-invariants",
        action="store_true",
        help="Treat missing invariant extracted text as an error",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"{path}:{line_number}: {error}") from error
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected an object")
            records.append(value)
    return records


def normalize_text(value: str) -> str:
    value = value.replace("\u00ad", "")
    value = re.sub(r"(?<=[A-Za-z])-\s+(?=[a-z])", "", value)
    return re.sub(r"\s+", "", value).casefold()


def normalize_font_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def expected_native_math_count(layout: dict[str, Any]) -> int:
    strategies = (
        (layout.get("rich_text") or {}).get("math_render_strategies") or {}
    )
    return sum(
        int(strategies.get(name) or 0)
        for name in ("native-font", "native-display-font")
    )


def validate_display_ownership(
    manifest: dict[str, Any], units: list[dict[str, Any]],
    layout: dict[str, Any], reviews: dict[str, dict[str, Any]],
) -> list[str]:
    """Require every active display review to produce exactly one placement."""
    from display_plan import build_display_plan, select_body_units, validate_display_consumption

    try:
        plan = build_display_plan(select_body_units(units, manifest), reviews)
        placements = list(layout.get("placements") or [])
        validate_display_consumption(plan, placements)
        actual = [item for item in placements if item.get("render_mode") == "native-math"]
        report = dict((layout.get("rich_text") or {}).get("math_review") or {})
        expected_reviews = [str(item["math_review_id"]) for item in actual]
        expected_sources = [str(unit_id) for item in actual for unit_id in item["ids"]]
        if report.get("display_review_ids") != expected_reviews:
            return ["Display review consumption summary differs from native-math placements."]
        if report.get("display_source_ids") != expected_sources:
            return ["Display source ownership summary differs from native-math placements."]
        if int(((layout.get("rich_text") or {}).get("math_render_strategies") or {}).get("native-display-font") or 0) != len(plan):
            return ["Native display count differs from exact source ownership."]
    except (ValueError, TypeError, KeyError) as error:
        return [str(error)]
    return []


def validate_reviewed_flow(
    manifest: dict[str, Any], units: list[dict[str, Any]], layout: dict[str, Any],
) -> list[str]:
    """Require explicit source continuations and rendered paragraph starts to agree."""
    from source_structure import explicit_continuations

    try:
        body = select_body_units(units, manifest)
        links = explicit_continuations(body)
    except (ValueError, TypeError, KeyError) as error:
        return [str(error)]
    placements = list(layout.get("placements") or [])
    errors: list[str] = []
    for unit in body:
        if (unit.get("render_suppressed") or not unit.get("translatable")
                or unit.get("kind") != "body" or unit.get("flow_role") != "paragraph"
                or not unit.get("flow_reviewed")):
            continue
        unit_id = str(unit["id"])
        found = [item for item in placements if unit_id in (item.get("ids") or [item.get("id")])]
        if len(found) != 1:
            errors.append(f"{unit_id}: reviewed paragraph requires exactly one flow placement.")
            continue
        placement = found[0]
        expected = links.get(unit_id)
        if expected and placement.get("id") != unit_id:
            actual = (placement.get("reviewed_continuations") or {}).get(unit_id)
            source_ids = list(placement.get("ids") or [])
            if (expected["continuation_across_display_ids"] or actual != expected
                    or expected["continuation_of"] not in source_ids
                    or source_ids.index(expected["continuation_of"]) >= source_ids.index(unit_id)
                    or placement.get("flow_role") not in {"paragraph-start", "paragraph-continuation"}
                    or placement.get("source_flow_role") != "paragraph"):
                errors.append(f"{unit_id}: merged explicit continuation differs from reviewed source flow.")
        elif expected:
            if placement.get("source_flow_role") != "paragraph" or placement.get("flow_reviewed") is not True:
                errors.append(f"{unit_id}: rendered source flow metadata differs from reviewed paragraph.")
            if (placement.get("id") != unit_id or placement.get("flow_role") != "paragraph-continuation"
                    or placement.get("text_indent") != 0):
                errors.append(f"{unit_id}: reviewed continuation is not a separate flush paragraph continuation.")
            for key, value in expected.items():
                if placement.get(key) != value:
                    errors.append(f"{unit_id}: rendered {key} differs from reviewed source flow.")
        elif (placement.get("id") != unit_id or placement.get("flow_role") != "paragraph-start"
              or placement.get("source_flow_role") != "paragraph" or placement.get("flow_reviewed") is not True
              or placement.get("continuation_of") or placement.get("text_indent") != ZH_ACADEMIC_V1.body_text_indent):
            errors.append(f"{unit_id}: reviewed paragraph start was merged or lost its source indent.")
    return errors


def is_expected_latin_font(value: str) -> bool:
    normalized = normalize_font_name(value)
    return (
        "lmroman" in normalized
        or "latinmodernroman" in normalized
        or "latinmodernmath" in normalized
        or normalized.startswith(
            ("cmr", "cmmi", "cmex", "cmsy", "msam", "msbm")
        )
    )


def has_measurable_prose_start(value: str) -> bool:
    remaining = value.lstrip()
    while True:
        match = re.match(
            r"\[\[(?:PAR|[BIE][A-Za-z0-9_]*_OPEN)\]\]",
            remaining,
        )
        if not match:
            break
        remaining = remaining[match.end() :].lstrip()
    if not remaining or remaining.startswith("[["):
        return False
    return bool(HAN_RE.match(remaining) or re.match(r"[A-Za-z]{2,}", remaining))


def invariant_present(value: str, normalized_output: str) -> bool:
    normalized = normalize_text(value)
    if normalized in normalized_output:
        return True
    chunk_size = min(64, max(32, len(normalized) // 4))
    if len(normalized) < chunk_size:
        return False
    starts = sorted(
        {
            0,
            max(0, (len(normalized) - chunk_size) // 2),
            max(0, len(normalized) - chunk_size),
        }
    )
    chunks = [normalized[start : start + chunk_size] for start in starts]
    required = 1 if len(chunks) == 1 else 2
    return sum(chunk in normalized_output for chunk in chunks) >= required


def index_placements_by_id(
    placements: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for item in placements:
        unit_ids = list(item.get("ids") or [])
        if item.get("id") and item.get("id") not in unit_ids:
            unit_ids.append(item["id"])
        for unit_id in unit_ids:
            if unit_id:
                indexed[str(unit_id)] = item
    return indexed


def full_original_source_pages(layout: dict[str, Any] | None) -> set[int]:
    return {
        int(item.get("source_page") or 0)
        for item in (
            ((layout or {}).get("boundary_layout") or {}).get("segments") or []
        )
        if item.get("mode") == "full-page"
        and item.get("role") in {"front-original", "tail-original"}
    }


def original_boundary_output_text(
    document: pymupdf.Document, layout: dict[str, Any] | None
) -> str:
    """Extract original clips separately so columns cannot interleave prose.

    Only text actually present in the output contributes to the corpus; source
    text and declared coverage are never used as evidence of text preservation.
    Boundary geometry and source/output content are validated independently.
    """
    texts: list[str] = []
    for segment in (
        ((layout or {}).get("boundary_layout") or {}).get("segments") or []
    ):
        if segment.get("role") not in {"front-original", "tail-original"}:
            continue
        page_index = int(segment.get("output_page") or 0) - 1
        if not 0 <= page_index < len(document):
            continue
        rectangle = pymupdf.Rect(segment.get("target_bbox") or [])
        if rectangle.is_empty:
            continue
        # Native content-stream order can preserve legacy Type3 prose whose
        # geometry-based sorting interleaves glyphs. Both remain actual output
        # evidence; neither source strings nor coverage declarations enter it.
        for sort in (False, True):
            texts.append(
                document[page_index].get_text("text", clip=rectangle, sort=sort)
            )
    return "\n".join(texts)


def original_code_output_text(
    document: pymupdf.Document, layout: dict[str, Any] | None,
) -> str:
    texts: list[str] = []
    for placement in (layout or {}).get("placements") or []:
        if (placement.get("render_mode") != "source_clip"
                or (placement.get("kind") != "code" and not placement.get("caption_id"))):
            continue
        page_index = int(placement.get("output_page") or 0) - 1
        rectangle = pymupdf.Rect(placement.get("bbox") or [])
        if 0 <= page_index < len(document) and not rectangle.is_empty:
            texts.append(document[page_index].get_text("text", clip=rectangle, sort=False))
    return "\n".join(texts)


def missing_invariant_ids(
    units: list[dict[str, Any]],
    normalized_output: str,
    layout: dict[str, Any] | None,
    *,
    validated_image_code_ids: set[str] | None = None,
    validated_image_caption_ids: set[str] | None = None,
    validated_native_display_ids: set[str] | None = None,
) -> list[str]:
    exact_original_pages = full_original_source_pages(layout)
    missing: list[str] = []
    for unit in units:
        text = str(unit.get("source_text") or "")
        if (
            unit.get("translatable")
            or unit.get("render_suppressed")
            or int(unit.get("page") or 0) in exact_original_pages
            or (unit.get("render_mode") != "text" and unit.get("kind") != "code")
            or unit.get("kind") in {"header", "footer"}
            or str(unit["id"]) in (validated_native_display_ids or set())
            or (unit.get("kind") == "caption"
                and str(unit["id"]) in (validated_image_caption_ids or set()))
            or (unit.get("kind") == "code"
                and str(unit["id"]) in (validated_image_code_ids or set()))
            or (len(normalize_text(text)) < 60 and unit.get("kind") != "code")
        ):
            continue
        present = (normalize_text(text) in normalized_output if unit.get("kind") == "code"
                   else invariant_present(text, normalized_output))
        if not present:
            missing.append(str(unit["id"]))
    return missing


def _source_prose_span_boxes(
    unit: dict[str, Any], blocks: list[dict[str, Any]],
) -> list[pymupdf.Rect] | None:
    """Use precise spans only for a complete, geometrically identified paragraph."""
    if unit.get("kind") not in {"body", "heading", "footnote"}:
        return None
    text = normalize_text(str(unit.get("source_text") or ""))
    if not text:
        return None
    bounds = pymupdf.Rect(unit.get("bbox") or [])
    matches = []
    for block in blocks:
        if block.get("type") != 0:
            continue
        block_box = pymupdf.Rect(block["bbox"])
        if max(abs(a - b) for a, b in zip(bounds, block_box)) > 0.002:
            continue
        block_text = "\n".join(
            "".join(str(span.get("text") or "") for span in line.get("spans", []))
            for line in block.get("lines", [])
        )
        if normalize_text(block_text) == text:
            matches.append(block)
    if len(matches) != 1:
        return None
    boxes = [pymupdf.Rect(span["bbox"])
             for line in matches[0].get("lines", [])
             for span in line.get("spans", [])
             if str(span.get("text") or "").strip()]
    return boxes or None


def validate_source_clip_envelopes(
    layout: dict[str, Any] | None,
    units: list[dict[str, Any]],
    source_document: pymupdf.Document | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Reject clips that include separately rendered prose or other objects.

    Suppressed fragments belong to their reviewed replacement and must not be
    counted as independently rendered objects. A union of two source clips,
    however, must not swallow active units between them or in another column.
    A wrapped paragraph's rectangle also contains empty space next to its text.
    When its complete source block is identified by text and geometry, check
    the actual spans; otherwise retain the conservative rectangle check.
    """
    errors: list[str] = []
    checked = 0
    page_blocks: dict[int, list[dict[str, Any]]] = {}
    for placement in list((layout or {}).get("placements") or []):
        if placement.get("render_mode") != "source_clip":
            continue
        represented = {str(value) for value in placement.get("ids") or []}
        represented.add(str(placement.get("id") or ""))
        source_page = int(placement.get("source_page") or 0)
        rectangle = pymupdf.Rect(placement.get("source_bbox") or [])
        if rectangle.is_empty:
            errors.append(f"{placement.get('id')}: source clip envelope is empty.")
            continue
        unexpected = []
        for unit in units:
            if (
                str(unit["id"]) in represented
                or unit.get("render_suppressed")
                or unit.get("kind") in {"header", "footer"}
                or int(unit.get("page") or 0) != source_page
            ):
                continue
            intersection = rectangle & pymupdf.Rect(unit.get("bbox") or [])
            if intersection.get_area() > 0.5:
                if source_document is not None and 0 < source_page <= len(source_document):
                    if source_page not in page_blocks:
                        page_blocks[source_page] = source_document[source_page - 1].get_text(
                            "dict", sort=True)["blocks"]
                    spans = _source_prose_span_boxes(unit, page_blocks[source_page])
                    if spans is not None and not any(
                        (rectangle & span).get_area() > 0.5 for span in spans
                    ):
                        continue
                unexpected.append(str(unit["id"]))
        if unexpected:
            errors.append(
                f"{placement.get('id')}: source clip envelope includes "
                "unrequested active source units: " + ", ".join(unexpected)
            )
        checked += 1
    return errors, {"checked": checked, "contaminated_clips": len(errors)}


def _normalized_code_pixels(
    document: pymupdf.Document,
    page_index: int,
    rectangle: pymupdf.Rect,
    source_size: tuple[float, float],
) -> pymupdf.Pixmap:
    """Render the actual clipped PDF contents at a shared origin and source scale."""
    with pymupdf.open() as normalized:
        page = normalized.new_page(width=source_size[0], height=source_size[1])
        page.show_pdf_page(page.rect, document, page_index, clip=rectangle,
                           keep_proportion=False)
        return page.get_pixmap(matrix=pymupdf.Matrix(4, 4), alpha=False)


def _code_bitmap_transform(
    image: dict[str, Any], source_box: pymupdf.Rect, target_box: pymupdf.Rect,
) -> tuple[float, ...]:
    sx, sy = target_box.width / source_box.width, target_box.height / source_box.height
    mapping = pymupdf.Matrix(sx, 0, 0, sy, target_box.x0 - sx * source_box.x0,
                             target_box.y0 - sy * source_box.y0)
    return tuple(pymupdf.Matrix(image["transform"]) * mapping)


def _source_clip_bitmap_noise_evidence(
    source: pymupdf.Document, output: pymupdf.Document,
    source_page_index: int, output_page_index: int,
    source_box: pymupdf.Rect, target_box: pymupdf.Rect,
    unit_id: str, layout: dict[str, Any] | None,
    *, combined_caption: bool = False,
) -> dict[str, Any]:
    """Allow interpolation noise only for unchanged bitmaps at their exact source transform."""
    source_page, output_page = source[source_page_index], output[output_page_index]
    source_images = [image for image in source_page.get_image_info(hashes=True)
                     if pymupdf.Rect(image["bbox"]).intersects(source_box)]
    actual = [image for image in output_page.get_image_info(hashes=True)
              if pymupdf.Rect(image["bbox"]).intersects(target_box)]
    key = lambda image: (image["digest"].hex(), image["width"], image["height"])
    expected = [(key(image), _code_bitmap_transform(image, source_box, target_box), target_box)
                for image in source_images]

    def transform_delta(image, signature):
        mapping = ~pymupdf.Matrix(signature[1]) * pymupdf.Matrix(image["transform"])
        box = signature[2]
        return max(max(abs(q.x - p.x), abs(q.y - p.y))
                   for p in (box.tl, box.tr, box.bl, box.br)
                   for q in [p * mapping])

    def transform_tolerance(box):
        # Production includegraphics rounds scaling coefficients. Bound that
        # measured error at this crop, not at the far-away full-page image edge.
        if combined_caption:
            # Caption envelopes pass through two PDF Forms; includegraphics
            # rounds their crop dimensions as well as its scale coefficient.
            return min(0.04, 0.002 + max(box.width, box.height) * 0.00015)
        return 0.002 + max(box.width, box.height) * 0.0001

    def matches(image, signature):
        return (key(image) == signature[0]
                and transform_delta(image, signature) <= transform_tolerance(signature[2]))

    # Image bboxes can extend far outside their Form clip. Exclude only instances
    # proven to belong to another disjoint, reviewed source-clip placement.
    other_signatures = []
    for placement in (layout or {}).get("placements") or []:
        if (placement.get("render_mode") != "source_clip"
                or int(placement.get("output_page") or 0) != output_page_index + 1
                or unit_id in (placement.get("ids") or [placement.get("id")])):
            continue
        other_target = pymupdf.Rect(placement.get("bbox") or [])
        other_source = pymupdf.Rect(placement.get("source_bbox") or [])
        page_index = int(placement.get("source_page") or 0) - 1
        if (other_target.is_empty or other_source.is_empty or other_target.intersects(target_box)
                or not 0 <= page_index < len(source)):
            continue
        for image in source[page_index].get_image_info(hashes=True):
            if pymupdf.Rect(image["bbox"]).intersects(other_source):
                other_signatures.append((key(image), _code_bitmap_transform(
                    image, other_source, other_target), other_target))
    actual = [image for image in actual
              if any(matches(image, signature) for signature in expected)
              or not any(matches(image, signature) for signature in other_signatures)]
    unmatched = list(actual)
    deltas = []
    measured_boxes = []
    output_to_source_transforms = []
    for signature, source_image in zip(expected, source_images):
        match = next((image for image in unmatched if matches(image, signature)), None)
        if match is None:
            break
        deltas.append(transform_delta(match, signature))
        mapping = ~pymupdf.Matrix(signature[1]) * pymupdf.Matrix(match["transform"])
        measured_boxes.append(pymupdf.Rect(target_box.tl * mapping, target_box.br * mapping))
        output_to_source_transforms.append(
            ~pymupdf.Matrix(match["transform"]) * pymupdf.Matrix(source_image["transform"]))
        unmatched.remove(match)
    transforms_match = bool(expected) and len(deltas) == len(expected) and not unmatched

    def drawings_in_clip(page, box):
        for drawing in page.get_drawings():
            pad = max(float(drawing.get("width") or 0) / 2, 0.001)
            if (pymupdf.Rect(drawing["rect"]) + (-pad, -pad, pad, pad)).intersects(box):
                return True
        return False

    marker = "TPVIS" + re.sub(r"[^A-Za-z0-9_.-]+", "-", unit_id)
    markers = []
    extra_text = False
    for span in output_page.get_texttrace():
        box = pymupdf.Rect(span["bbox"])
        if not box.intersects(target_box):
            continue
        text = "".join(chr(character[0]) for character in span["chars"])
        if (text == marker and float(span.get("size") or 0) <= 0.11
                and box.y0 >= target_box.y1 - 0.2 and box.y1 <= target_box.y1 + 0.2
                and box.x0 >= target_box.x0 - 0.002 and box.x1 <= target_box.x1 + 0.002):
            markers.append({"text": text, "size_pt": span["size"], "bbox": list(box)})
        else:
            extra_text = True
    no_vector_overlays = (not drawings_in_clip(source_page, source_box)
                          and not drawings_in_clip(output_page, target_box))
    source_digests = sorted(key(image) for image in source_images)
    output_digests = sorted(key(image) for image in actual)
    return {
        "source_bitmap_digests": source_digests, "output_bitmap_digests": output_digests,
        "expected_bitmap_transforms": [list(signature[1]) for signature in expected],
        "output_bitmap_transforms": [list(image["transform"]) for image in actual],
        "bitmap_multisets_identical": bool(source_digests) and source_digests == output_digests,
        "bitmap_transforms_match": transforms_match,
        "maximum_transform_delta_pt": max(deltas) if deltas else None,
        "transform_tolerance_pt": transform_tolerance(target_box),
        "transform_relative_tolerance": 0.00015 if combined_caption else 0.0001,
        "no_vector_overlays": no_vector_overlays,
        "measured_output_bbox": list(measured_boxes[0]) if transforms_match else None,
        "output_to_source_transform": (list(output_to_source_transforms[0])
                                       if transforms_match else None),
        "no_extra_text": not extra_text, "layout_markers": markers,
        "noise_eligible": (transforms_match and source_digests == output_digests
                           and no_vector_overlays and not extra_text),
    }


def validate_code_preservation(
    document: pymupdf.Document,
    units: list[dict[str, Any]],
    layout: dict[str, Any] | None,
    *,
    source_document: pymupdf.Document | None = None,
    evidence: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Check native code text or image-only code pixels against actual source clips."""
    errors: list[str] = []
    for unit in units:
        if unit.get("kind") != "code" or unit.get("render_suppressed"):
            continue
        unit_id = str(unit["id"])
        unit_errors: list[str] = []
        record: dict[str, Any] = {"id": unit_id, "status": "fail",
                                  "source_modality": "unverified"}
        placements = [item for item in (layout or {}).get("placements") or []
                      if unit_id == str(item.get("id") or "")
                      or unit_id in item.get("ids", [])]
        if len(placements) != 1:
            errors.append(f"{unit_id}: code requires exactly one source clip placement.")
            if evidence is not None:
                evidence.append(record)
            continue
        placement = placements[0]
        represented = set(placement.get("ids") or [placement.get("id")])
        source_box = pymupdf.Rect(placement.get("source_bbox") or [])
        expected_box = pymupdf.Rect(unit.get("bbox") or [])
        if (placement.get("render_mode") != "source_clip"
                or represented != {unit_id}
                or int(placement.get("source_page") or 0) != int(unit["page"])
                or source_box.is_empty
                or max(abs(a - b) for a, b in zip(source_box, expected_box)) > 0.002):
            unit_errors.append(f"{unit_id}: code source clip differs from its reviewed bounds/ownership.")
        page_index = int(placement.get("output_page") or 0) - 1
        target_box = pymupdf.Rect(placement.get("bbox") or [])
        record.update(source_page=int(unit["page"]), source_bbox=list(source_box),
                      output_page=page_index + 1, output_bbox=list(target_box))
        if (not 0 <= page_index < len(document) or target_box.is_empty
                or not document[page_index].rect.contains(target_box)):
            unit_errors.append(f"{unit_id}: code output clip is empty or outside its actual page.")
        source_page_index = int(unit["page"]) - 1
        source_clip_text = None
        if source_document is not None:
            if (not 0 <= source_page_index < len(source_document) or source_box.is_empty
                    or not source_document[source_page_index].rect.contains(source_box)):
                unit_errors.append(f"{unit_id}: code source clip is outside its actual source page.")
            else:
                source_page = source_document[source_page_index]
                source_clip_text = source_page.get_text("text", clip=source_box)
                has_bitmap = any(pymupdf.Rect(image["bbox"]).intersects(source_box)
                                 for image in source_page.get_image_info())
                record["source_modality"] = ("text" if source_clip_text.strip() else
                                             "image" if has_bitmap else "nontext-vector")
                if source_box.get_area() >= source_page.rect.get_area() * 0.9:
                    unit_errors.append(f"{unit_id}: code source clip covers an entire source page.")
                envelope_errors, _ = validate_source_clip_envelopes(
                    {"placements": [placement]}, units, source_document=source_document)
                unit_errors.extend(envelope_errors)
        if (not source_box.is_empty and not target_box.is_empty
                and abs((target_box.width / target_box.height)
                        / (source_box.width / source_box.height) - 1) > 0.002):
            unit_errors.append(f"{unit_id}: code source clip aspect ratio changed.")
        source_text = re.sub(r"\s+", "", str(unit.get("source_text") or ""))
        if record["source_modality"] in {"image", "nontext-vector"}:
            if not source_text:
                unit_errors.append(f"{unit_id}: reviewed image code has no source transcription.")
            if not unit_errors:
                size = (source_box.width, source_box.height)
                source_pixels = _normalized_code_pixels(
                    source_document, source_page_index, source_box, size)
                output_pixels = _normalized_code_pixels(document, page_index, target_box, size)
                source_samples, output_samples = source_pixels.samples, output_pixels.samples
                record.update(pixel_profile="source-clip-pixels-v1", normalized_dpi=288,
                              pixel_dimensions=[source_pixels.width, source_pixels.height],
                              source_pixel_sha256=hashlib.sha256(source_samples).hexdigest(),
                              output_pixel_sha256=hashlib.sha256(output_samples).hexdigest(),
                              pixels_identical=source_samples == output_samples)
                if not any(value < 250 for value in source_samples):
                    unit_errors.append(f"{unit_id}: image code source crop contains no visible content.")
                if source_samples != output_samples:
                    noise = _source_clip_bitmap_noise_evidence(
                        source_document, document, source_page_index, page_index,
                        source_box, target_box, unit_id, layout)
                    record.update(noise)
                    if noise["noise_eligible"]:
                        # The production stub is rounded independently of the
                        # graphics affine. Normalize the actual retained bitmap
                        # crop using its measured affine, only after that affine
                        # has passed the tight placement geometry check above.
                        output_pixels = _normalized_code_pixels(
                            document, page_index, pymupdf.Rect(noise["measured_output_bbox"]), size)
                        output_samples = output_pixels.samples
                        record.update(normalized_output_pixel_sha256=hashlib.sha256(output_samples).hexdigest(),
                                      geometry_normalized_pixels_identical=source_samples == output_samples)
                        size_px = (source_pixels.width, source_pixels.height)
                        source_image = Image.frombytes("RGB", size_px, source_samples)
                        output_image = Image.frombytes("RGB", size_px, output_samples)
                        difference = ImageChops.difference(
                            source_image.filter(ImageFilter.GaussianBlur(1)),
                            output_image.filter(ImageFilter.GaussianBlur(1)))
                        differences = difference.tobytes()
                        source_ink = sum(255 - value for value in source_samples)
                        output_ink = sum(255 - value for value in output_samples)
                        mass_delta = abs(source_ink - output_ink) / max(source_ink, 1)
                        edge_error = sum(differences) / max(source_ink, 1)
                        maximum = max(differences)
                        accepted = maximum <= 64 and mass_delta <= 0.01 and edge_error <= 0.25
                        record.update(antialias_radius_px=1, maximum_blurred_channel_difference=maximum,
                                      maximum_blurred_channel_difference_limit=64,
                                      ink_mass_delta_ratio=mass_delta, ink_mass_delta_limit=0.01,
                                      blurred_difference_per_source_ink=edge_error,
                                      blurred_difference_per_source_ink_limit=0.25,
                                      accepted_antialias_noise=accepted)
                    else:
                        accepted = False
                    if not accepted:
                        unit_errors.append(f"{unit_id}: image code pixels differ in its actual output clip.")
        else:
            actual_text = (document[page_index].get_text("text", clip=target_box)
                           if 0 <= page_index < len(document) and not target_box.is_empty else "")
            if (source_clip_text is not None
                    and source_text != re.sub(r"\s+", "", source_clip_text)):
                unit_errors.append(f"{unit_id}: reviewed code text differs from its actual source clip.")
            if not source_text or source_text not in re.sub(r"\s+", "", actual_text):
                unit_errors.append(f"{unit_id}: code text is missing or changed in its output clip.")
        record["status"] = "fail" if unit_errors else "pass"
        if evidence is not None:
            evidence.append(record)
        errors.extend(unit_errors)
    return errors


def _direct_bitmap_clip_identity(
    source: pymupdf.Document, output: pymupdf.Document,
    source_index: int, output_index: int, source_box: pymupdf.Rect,
    bitmap_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Compare actual page renders without adding another rounded PDF Form.

    Only unchanged bitmap instances at tightly matched reviewed transforms may
    select this coordinate system. Real output clipping and overlays remain in
    the page display list and must produce exactly the source RGB samples.
    Texttrace bounds are not visibility evidence: text can be clipped by a Form.
    """
    if not (bitmap_evidence.get("bitmap_multisets_identical")
            and bitmap_evidence.get("bitmap_transforms_match")
            and bitmap_evidence.get("no_vector_overlays")
            and bitmap_evidence.get("output_to_source_transform")):
        return {"direct_affine_pixels_identical": False}
    source_matrix = pymupdf.Matrix(4, 0, 0, 4, -4 * source_box.x0, -4 * source_box.y0)
    output_to_source = pymupdf.Matrix(bitmap_evidence["output_to_source_transform"])
    original = source[source_index].get_pixmap(
        matrix=source_matrix, clip=source_box, alpha=False, colorspace=pymupdf.csRGB)
    rendered = output[output_index].get_pixmap(
        matrix=output_to_source * source_matrix,
        clip=source_box * ~output_to_source, alpha=False, colorspace=pymupdf.csRGB)
    return {
        "direct_affine_source_pixel_sha256": hashlib.sha256(original.samples).hexdigest(),
        "direct_affine_output_pixel_sha256": hashlib.sha256(rendered.samples).hexdigest(),
        "direct_affine_pixel_rects": [list(original.irect), list(rendered.irect)],
        "direct_affine_pixels_identical": (original.irect == rendered.irect
                                            and original.samples == rendered.samples),
    }


def validate_image_caption_preservation(
    source: pymupdf.Document, output: pymupdf.Document,
    units: list[dict[str, Any]], layout: dict[str, Any] | None,
    evidence: list[dict[str, Any]],
) -> list[str]:
    """Verify image captions through their actual owned figure/caption envelope."""
    errors: list[str] = []
    by_id = {str(unit["id"]): unit for unit in units}
    for caption in units:
        if (caption.get("kind") != "caption" or caption.get("translatable")
                or caption.get("render_suppressed")):
            continue
        unit_id = str(caption["id"])
        placements = [item for item in (layout or {}).get("placements") or []
                      if unit_id == str(item.get("id") or "")
                      or unit_id in (item.get("ids") or [])]
        if not any(item.get("render_mode") == "source_clip" for item in placements):
            continue  # Native captions retain the ordinary text invariant.
        record: dict[str, Any] = {"id": unit_id, "status": "fail"}
        evidence.append(record)
        failures: list[str] = []
        if len(placements) != 1:
            errors.append(f"{unit_id}: caption requires exactly one source-clip owner.")
            continue
        placement = placements[0]
        ids = list(placement.get("ids") or [placement.get("id")])
        members = [by_id[value] for value in ids if value in by_id]
        source_box = pymupdf.Rect(placement.get("source_bbox") or [])
        caption_box = pymupdf.Rect(caption.get("bbox") or [])
        target_box = pymupdf.Rect(placement.get("bbox") or [])
        source_index = int(placement.get("source_page") or 0) - 1
        output_index = int(placement.get("output_page") or 0) - 1
        canonical = pymupdf.Rect()
        for member in members:
            canonical |= pymupdf.Rect(member.get("bbox") or [])
        if (len(set(ids)) != len(ids) or len(members) != len(ids)
                or unit_id not in ids or placement.get("render_mode") != "source_clip"
                or placement.get("caption_id", unit_id) != unit_id
                or any(member.get("translatable") or member.get("render_suppressed")
                       or int(member.get("page") or 0) != source_index + 1
                       or (member is not caption and member.get("render_mode") != "source_clip")
                       or (caption.get("visual_id") and member.get("visual_id") != caption.get("visual_id"))
                       for member in members)
                or canonical.is_empty or source_box.is_empty or caption_box.is_empty
                or max(abs(a - b) for a, b in zip(canonical, source_box)) > 0.002
                or not rectangle_covered_by_regions(caption_box, [source_box])):
            failures.append(f"{unit_id}: caption source clip differs from reviewed group bounds/ownership.")
        if (not 0 <= source_index < len(source) or not 0 <= output_index < len(output)
                or target_box.is_empty or source_box.is_empty
                or (0 <= source_index < len(source) and not source[source_index].rect.contains(source_box))
                or (0 <= output_index < len(output) and not output[output_index].rect.contains(target_box))):
            failures.append(f"{unit_id}: caption source/output clip is empty or outside its actual page.")
        if failures:
            errors.extend(failures)
            continue
        source_text = source[source_index].get_text("text", clip=caption_box)
        has_bitmap = any(pymupdf.Rect(image["bbox"]).intersects(caption_box)
                         for image in source[source_index].get_image_info())
        record.update(source_page=source_index + 1, source_bbox=list(source_box),
                      source_caption_bbox=list(caption_box), source_ids=ids,
                      output_page=output_index + 1, output_bbox=list(target_box),
                      source_modality=("text" if source_text.strip() else
                                       "image" if has_bitmap else "nontext-vector"))
        if source_text.strip():
            record["status"] = "text-invariant-required"
            continue
        caption_pixels = _normalized_code_pixels(
            source, source_index, caption_box, (caption_box.width, caption_box.height))
        if not any(value < 250 for value in caption_pixels.samples):
            errors.append(f"{unit_id}: reviewed caption source region contains no visible content.")
            continue
        if (source_box.get_area() >= source[source_index].rect.get_area() * 0.9
                or abs((target_box.width / target_box.height)
                       / (source_box.width / source_box.height) - 1) > 0.002):
            failures.append(f"{unit_id}: image caption crop is page-sized or its aspect ratio changed.")
        envelope_errors, _ = validate_source_clip_envelopes(
            {"placements": [placement]}, units, source_document=source)
        failures.extend(envelope_errors)
        if failures:
            errors.extend(failures)
            continue
        size = (source_box.width, source_box.height)
        original = _normalized_code_pixels(source, source_index, source_box, size)
        rendered = _normalized_code_pixels(output, output_index, target_box, size)
        record.update(normalized_dpi=288, pixel_dimensions=[original.width, original.height],
                      source_pixel_sha256=hashlib.sha256(original.samples).hexdigest(),
                      output_pixel_sha256=hashlib.sha256(rendered.samples).hexdigest(),
                      pixels_identical=original.samples == rendered.samples)
        accepted = original.samples == rendered.samples and any(value < 250 for value in original.samples)
        if original.samples != rendered.samples:
            # This is an envelope identity check, not standalone-code ownership.
            # The full group ID identifies its marker; each caption keeps its own ID.
            noise = _source_clip_bitmap_noise_evidence(
                source, output, source_index, output_index, source_box, target_box,
                str(placement["id"]), layout, combined_caption=len(ids) > 1)
            record.update(noise)
            if noise["noise_eligible"]:
                rendered = _normalized_code_pixels(
                    output, output_index, pymupdf.Rect(noise["measured_output_bbox"]), size)
                accepted = original.samples == rendered.samples
                record.update(normalized_output_pixel_sha256=hashlib.sha256(rendered.samples).hexdigest(),
                              geometry_normalized_pixels_identical=accepted)
            if not accepted:
                direct = _direct_bitmap_clip_identity(
                    source, output, source_index, output_index, source_box, noise)
                record.update(direct)
                accepted = direct["direct_affine_pixels_identical"]
        record["status"] = "pass" if accepted else "fail"
        if not accepted:
            errors.append(f"{unit_id}: image caption differs in its actual output group clip.")
    return errors


def validate_native_display_invariants(
    document: pymupdf.Document, manifest: dict[str, Any], units: list[dict[str, Any]],
    layout: dict[str, Any] | None, review_payload: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> list[str]:
    """Native display source ownership uses native glyph evidence, not raw Unicode equality."""
    if not layout or layout.get("render_backend") != "native-lualatex-v1":
        return []
    reviews = dict(review_payload.get("entries") or {})
    errors = validate_display_ownership(manifest, units, layout, reviews)
    if review_payload.get("source_sha256") != manifest.get("source_sha256"):
        errors.append("Native display invariant review does not match the source PDF.")
    rich = layout.get("rich_text") or {}
    strategies = rich.get("math_render_strategies") or {}
    summary = rich.get("math_review") or {}
    if (int(summary.get("unresolved") or 0) or int(summary.get("image_fallbacks") or 0)
            or any(int(strategies.get(name) or 0) for name in
                   ("tex-vector", "source-vector", "font-vector", "image"))):
        errors.append("Native display invariants require resolved native mathematics without fallbacks.")
    for placement in layout.get("placements") or []:
        if placement.get("render_mode") != "native-math":
            continue
        review_id = str(placement.get("math_review_id") or "")
        entry = reviews.get(review_id) or {}
        record: dict[str, Any] = {"id": review_id, "source_ids": list(placement.get("ids") or []),
                                  "status": "fail", "output_page": placement.get("output_page")}
        evidence.append(record)
        page_index = int(placement.get("output_page") or 0) - 1
        if (placement.get("text_embedding") != "native-lualatex"
                or entry.get("review_status") not in {"manually_reviewed", "auto_validated"}
                or not str(entry.get("tex") or "").strip()
                or not 0 <= page_index < len(document)):
            errors.append(f"{review_id}: native display review/embedding is incomplete.")
            continue
        box = pymupdf.Rect(placement.get("bbox") or [])
        math_spans = [span for block in document[page_index].get_text("dict", clip=box).get("blocks", [])
                      for line in block.get("lines", []) for span in line.get("spans", [])
                      if "math" in str(span.get("font") or "").casefold() and str(span.get("text") or "").strip()]
        embedded = [font[3] for font in document[page_index].get_fonts()
                    if "math" in font[3].casefold() and document.extract_font(font[0])[3]]
        record.update(actual_math_fonts=sorted(set(embedded)), actual_math_glyph_spans=len(math_spans))
        if not math_spans or not embedded:
            errors.append(f"{review_id}: actual display page lacks embedded native mathematical glyphs.")
    if not errors:
        for record in evidence:
            record["status"] = "pass"
    return errors


def make_contact_sheet(images: list[Path], output: Path) -> None:
    if not images:
        return
    columns = 3
    thumb_width = 360
    border = 12
    label_height = 24
    thumbnails: list[Image.Image] = []
    for index, path in enumerate(images, start=1):
        with Image.open(path) as image:
            converted = image.convert("RGB")
            height = max(1, round(converted.height * thumb_width / converted.width))
            resized = converted.resize((thumb_width, height), Image.Resampling.LANCZOS)
            canvas = Image.new(
                "RGB",
                (thumb_width + border * 2, height + border * 2 + label_height),
                "white",
            )
            canvas.paste(resized, (border, border + label_height))
            draw = ImageDraw.Draw(canvas)
            draw.text((border, 4), f"Page {index}", fill="black")
            thumbnails.append(ImageOps.expand(canvas, border=1, fill="#bbbbbb"))
    cell_width = max(image.width for image in thumbnails)
    cell_height = max(image.height for image in thumbnails)
    rows = math.ceil(len(thumbnails) / columns)
    sheet = Image.new("RGB", (cell_width * columns, cell_height * rows), "#dddddd")
    for index, image in enumerate(thumbnails):
        x = (index % columns) * cell_width
        y = (index // columns) * cell_height
        sheet.paste(image, (x, y))
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output)


def han_font_counts(
    document: pymupdf.Document, placement: dict[str, Any]
) -> Counter[str]:
    page_index = int(placement["output_page"]) - 1
    if not (0 <= page_index < len(document)):
        return Counter()
    rect = pymupdf.Rect(placement["bbox"])
    counts: Counter[str] = Counter()
    blocks = document[page_index].get_text("dict", clip=rect).get("blocks", [])
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = str(span.get("text") or "")
                count = len(HAN_RE.findall(text))
                if count:
                    counts[str(span.get("font") or "")] += count
    return counts


def latin_font_counts(
    document: pymupdf.Document, placement: dict[str, Any]
) -> Counter[str]:
    page_index = int(placement["output_page"]) - 1
    if not (0 <= page_index < len(document)):
        return Counter()
    rect = pymupdf.Rect(placement["bbox"])
    counts: Counter[str] = Counter()
    blocks = document[page_index].get_text("dict", clip=rect).get("blocks", [])
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = str(span.get("text") or "")
                count = len(LATIN_RE.findall(text))
                if count:
                    counts[str(span.get("font") or "")] += count
    return counts


def first_line_delta(
    document: pymupdf.Document, placement: dict[str, Any]
) -> float | None:
    page_index = int(placement["output_page"]) - 1
    if not (0 <= page_index < len(document)):
        return None
    rect = pymupdf.Rect(placement["bbox"])
    spans: list[dict[str, Any]] = []
    blocks = document[page_index].get_text("dict").get("blocks", [])
    for block in blocks:
        for line in block.get("lines", []):
            spans.extend(
                span
                for span in line.get("spans", [])
                if rect.y0 - 0.5
                <= (
                    float(span["bbox"][1]) + float(span["bbox"][3])
                )
                / 2.0
                <= rect.y1 + 0.5
                and float(span["bbox"][2]) >= rect.x0
                and float(span["bbox"][0]) <= rect.x1
                if float(span.get("size") or 0) >= 9.5
                and not re.search(
                    r"(?:Math|CM[A-Z]*\d|MSAM|MSBM)",
                    str(span.get("font") or ""),
                    re.IGNORECASE,
                )
                and str(span.get("text") or "").strip()
            )
    if not spans:
        return None
    first_y = min(
        float((span.get("origin") or [0.0, span["bbox"][3]])[1])
        for span in spans
    )
    first_line = [
        span
        for span in spans
        if abs(
            float((span.get("origin") or [0.0, span["bbox"][3]])[1])
            - first_y
        )
        <= 2.0
    ]
    if not first_line:
        return None
    return min(float(span["bbox"][0]) for span in first_line) - rect.x0


def placement_han_metrics(
    document: pymupdf.Document, placement: dict[str, Any]
) -> dict[str, list[float]]:
    page_index = int(placement["output_page"]) - 1
    if not (0 <= page_index < len(document)):
        return {"font_sizes": [], "glyph_width_ratios": [], "line_pitches": []}
    rect = pymupdf.Rect(placement["bbox"])
    raw = document[page_index].get_text("rawdict", clip=rect)
    font_sizes: list[float] = []
    glyph_width_ratios: list[float] = []
    line_tops: list[float] = []
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            has_han = False
            for span in line.get("spans", []):
                size = float(span.get("size") or 0)
                for char in span.get("chars", []):
                    value = str(char.get("c") or "")
                    if not HAN_RE.fullmatch(value):
                        continue
                    has_han = True
                    font_sizes.append(size)
                    if size > 0:
                        bbox = pymupdf.Rect(char["bbox"])
                        glyph_width_ratios.append(bbox.width / size)
            if has_han:
                line_tops.append(float(line["bbox"][1]))
    unique_tops: list[float] = []
    for top in sorted(line_tops):
        if not unique_tops or abs(top - unique_tops[-1]) > 0.5:
            unique_tops.append(top)
    line_pitches = [
        later - earlier for earlier, later in zip(unique_tops, unique_tops[1:])
    ]
    return {
        "font_sizes": font_sizes,
        "glyph_width_ratios": glyph_width_ratios,
        "line_pitches": line_pitches,
    }


def differs(actual: Any, expected: float, tolerance: float = 0.01) -> bool:
    try:
        return abs(float(actual) - expected) > tolerance
    except (TypeError, ValueError):
        return True


def analyze_reader_compatibility(
    document: pymupdf.Document,
    translation_start: int,
    translation_end: int,
) -> dict[str, Any]:
    """Report PDF facts without claiming to validate viewer-generated overlays."""
    metrics: dict[str, Any] = {
        "pdf_native_links": 0,
        "oversized_text_forms": 0,
        "searchable_text_layer": False,
        "citation_text_candidates": 0,
        "zotero_smart_reference_status": "runtime-generated-not-static-pdf",
    }
    if not (
        1 <= translation_start <= translation_end <= len(document)
    ):
        return metrics
    body_text: list[str] = []
    for page_number in range(translation_start, translation_end + 1):
        page = document[page_number - 1]
        metrics["pdf_native_links"] += len(page.get_links())
        page_text = page.get_text()
        body_text.append(page_text)
        metrics["citation_text_candidates"] += len(
            CITATION_LIKE_RE.findall(page_text)
        )
        for xobject in page.get_xobjects():
            name = str(xobject[1] or "").casefold()
            bbox = pymupdf.Rect(xobject[3])
            if name == "fullpage" and abs(bbox.height - 4096.0) <= 1.0:
                metrics["oversized_text_forms"] += 1
    metrics["searchable_text_layer"] = bool("".join(body_text).strip())
    return metrics


def validate_typography(
    document: pymupdf.Document,
    layout: dict[str, Any] | None,
    required_ids: set[str],
    bold_body_ids: set[str] | None = None,
    measurable_indent_ids: set[str] | None = None,
) -> tuple[list[str], list[str], dict[str, Any]]:
    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {
        "typography_profile": None,
        "paragraphs_checked": 0,
        "headings_checked": 0,
        "joined_continuations": 0,
        "han_fonts": {},
        "latin_fonts": {},
        "body_font_size_median": None,
        "body_line_pitch_median": None,
        "han_glyph_width_ratio_median": None,
        "content_left_margin": None,
        "content_right_margin": None,
        "text_embedding": None,
        "pdf_native_links": 0,
        "oversized_text_forms": 0,
        "searchable_text_layer": False,
        "citation_text_candidates": 0,
        "zotero_smart_reference_status": "runtime-generated-not-static-pdf",
    }
    if layout is None:
        warnings.append("Typography layout report is missing.")
        return errors, warnings, metrics
    profile = layout.get("typography_profile")
    metrics["typography_profile"] = profile
    if profile != TYPOGRAPHY_PROFILE:
        errors.append(
            f"Unexpected typography profile: {profile!r}; "
            f"expected {TYPOGRAPHY_PROFILE!r}."
        )
        return errors, warnings, metrics
    flow = layout.get("flow") or {}
    metrics["text_embedding"] = flow.get("text_embedding")
    if flow.get("text_embedding") not in {
        "direct-htmlbox",
        "native-lualatex",
    }:
        errors.append(
            "Translated text is not directly embedded into final PDF pages."
        )

    typography = layout.get("typography") or {}
    body_font = Path(str(typography.get("body_font") or "")).name.casefold()
    heading_font = Path(str(typography.get("heading_font") or "")).name.casefold()
    latin_regular = Path(
        str(typography.get("latin_regular_font") or "")
    ).name.casefold()
    latin_bold = Path(
        str(typography.get("latin_bold_font") or "")
    ).name.casefold()
    latin_italic = Path(
        str(typography.get("latin_italic_font") or "")
    ).name.casefold()
    if body_font != "simsun.ttc":
        errors.append(f"Body font is not SimSun: {body_font or 'missing'}")
    if heading_font != "simhei.ttf":
        errors.append(f"Heading font is not SimHei: {heading_font or 'missing'}")
    for role, filename in (
        ("regular", latin_regular),
        ("bold", latin_bold),
        ("italic", latin_italic),
    ):
        if "lmroman10" not in filename:
            errors.append(
                f"Latin {role} font is not Latin Modern Roman: "
                f"{filename or 'missing'}"
            )
    if differs(typography.get("body_font_size"), ZH_ACADEMIC_V1.body_font_size):
        errors.append("Body font size metadata is not 11.5pt.")
    if differs(
        typography.get("body_line_pitch"), ZH_ACADEMIC_V1.body_line_pitch
    ):
        errors.append("Body line pitch metadata is not 16.9pt.")
    if differs(
        typography.get("body_letter_spacing"),
        ZH_ACADEMIC_V1.body_letter_spacing,
    ):
        errors.append("Body letter spacing metadata is not 0pt.")
    if differs(
        typography.get("body_text_indent"), ZH_ACADEMIC_V1.body_text_indent
    ):
        errors.append("Body text indent metadata is not 23pt.")
    if differs(
        typography.get("paragraph_spacing"), ZH_ACADEMIC_V1.paragraph_spacing
    ):
        errors.append("Paragraph spacing metadata is not 4pt.")
    page_margins = typography.get("page_margins") or {}
    expected_margins = {
        "left": ZH_ACADEMIC_V1.margin_left,
        "right": ZH_ACADEMIC_V1.margin_right,
        "top": ZH_ACADEMIC_V1.margin_top,
        "bottom": ZH_ACADEMIC_V1.margin_bottom,
    }
    for name, expected in expected_margins.items():
        if differs(page_margins.get(name), expected):
            errors.append(
                f"Page {name} margin metadata is not {expected:g}pt."
            )

    bold_body_ids = bold_body_ids or set()
    placements = list(layout.get("placements") or [])
    placed_ids = {
        str(unit_id)
        for placement in placements
        for unit_id in placement.get("ids", [placement.get("id")])
        if unit_id
    }
    missing_ids = sorted(required_ids - placed_ids)
    if missing_ids:
        errors.append(
            "Translatable IDs missing from layout placements: "
            + ", ".join(missing_ids[:20])
        )

    if flow.get("text_embedding") == "native-lualatex":
        boundary = dict(layout.get("boundary_layout") or {})
        translation_start = int(boundary.get("translation_page_start") or 0)
        translation_end = int(boundary.get("translation_page_end") or 0)
        if not (
            1 <= translation_start <= translation_end <= len(document)
        ):
            errors.append("Native LuaLaTeX body page range is invalid.")
            return errors, warnings, metrics
        han_fonts: Counter[str] = Counter()
        latin_fonts: Counter[str] = Counter()
        math_fonts: Counter[str] = Counter()
        body_sizes: list[float] = []
        for page_index in range(translation_start - 1, translation_end):
            page = document[page_index]
            code_boxes = [pymupdf.Rect(item["bbox"]) for item in placements
                          if item.get("kind") == "code" and item.get("render_mode") == "source_clip"
                          and int(item.get("output_page") or 0) == page_index + 1]
            for block in page.get_text("dict", sort=True).get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        span_box = pymupdf.Rect(span["bbox"])
                        center = (span_box.tl + span_box.br) / 2
                        if any(box.contains(center) for box in code_boxes):
                            continue
                        text = str(span.get("text") or "")
                        font = str(span.get("font") or "")
                        if HAN_RE.search(text):
                            han_fonts[font] += len(HAN_RE.findall(text))
                            if "simhei" not in font.casefold():
                                body_sizes.append(float(span.get("size") or 0.0))
                        if LATIN_RE.search(text):
                            latin_fonts[font] += len(LATIN_RE.findall(text))
                        if "math" in font.casefold():
                            math_fonts[font] += max(1, len(text))
        metrics["han_fonts"] = dict(han_fonts)
        metrics["latin_fonts"] = dict(latin_fonts)
        latin_prose_fonts = {name: count for name, count in latin_fonts.items()
                             if "math" not in name.casefold() and "mono" not in name.casefold()}
        metrics["math_fonts"] = dict(math_fonts)
        metrics["paragraphs_checked"] = sum(
            placement.get("flow_role") in {"paragraph-start", "paragraph-continuation", "list-item", "list-continuation"}
            for placement in placements
        )
        metrics["headings_checked"] = sum(
            placement.get("flow_role") == "heading"
            for placement in placements
        )
        metrics["joined_continuations"] = int(
            (layout.get("flow") or {}).get("joined_continuations") or 0
        )
        if body_sizes:
            metrics["body_font_size_median"] = round(
                statistics.median(body_sizes), 3
            )
        if not any("simsun" in name.casefold() for name in han_fonts):
            errors.append("Native LuaLaTeX body does not embed SimSun text.")
        if not any("simhei" in name.casefold() for name in han_fonts):
            errors.append("Native LuaLaTeX headings do not embed SimHei text.")
        if latin_prose_fonts and not any(
            "lmroman" in normalize_font_name(name)
            or "latinmodernroman" in normalize_font_name(name)
            for name in latin_prose_fonts
        ):
            errors.append(
                "Native LuaLaTeX Latin prose does not use Latin Modern Roman."
            )
        if expected_native_math_count(layout) and not math_fonts:
            errors.append(
                "Native LuaLaTeX body exposes no embedded mathematical font."
            )
        if metrics["paragraphs_checked"] == 0:
            errors.append("No translated paragraphs were checked for typography.")
        if metrics["headings_checked"] == 0:
            errors.append("No translated headings were checked for typography.")
        reader_metrics = analyze_reader_compatibility(
            document, translation_start, translation_end
        )
        metrics.update(reader_metrics)
        if reader_metrics["oversized_text_forms"]:
            errors.append(
                "Native LuaLaTeX pages contain oversized measurement forms."
            )
        if reader_metrics["pdf_native_links"]:
            errors.append(
                "Native LuaLaTeX pages contain unexpected PDF link annotations."
            )
        return errors, warnings, metrics

    global_fonts: Counter[str] = Counter()
    global_latin_fonts: Counter[str] = Counter()
    text_placements = [
        placement
        for placement in placements
        if placement.get("render_mode") == "text"
    ]
    sequence_index = {
        id(placement): index for index, placement in enumerate(placements)
    }
    body_font_sizes: list[float] = []
    body_line_pitches: list[float] = []
    body_glyph_width_ratios: list[float] = []
    content_rects: list[pymupdf.Rect] = []
    for placement in text_placements:
        role = placement.get("flow_role")
        fonts = han_font_counts(document, placement)
        latin_fonts = latin_font_counts(document, placement)
        global_fonts.update(fonts)
        global_latin_fonts.update(latin_fonts)
        dominant = fonts.most_common(1)[0][0] if fonts else ""
        actual = placement_han_metrics(document, placement)
        content_rects.append(pymupdf.Rect(placement["bbox"]))
        if placement.get("text_embedding") != "direct-htmlbox":
            errors.append(
                f"{placement.get('id')}: text placement is not direct-htmlbox."
            )
        if role in {"paragraph-start", "list-item"}:
            metrics["paragraphs_checked"] += 1
            body_font_sizes.extend(actual["font_sizes"])
            body_line_pitches.extend(
                pitch
                for pitch in actual["line_pitches"]
                if 12.0 <= pitch <= 22.0
            )
            body_glyph_width_ratios.extend(actual["glyph_width_ratios"])
            placement_ids = {
                str(value)
                for value in placement.get(
                    "ids", [placement.get("id")]
                )
                if value
            }
            bold_dominant = bool(placement_ids & bold_body_ids)
            valid_body_font = (
                "simsun" in dominant.casefold()
                or (
                    bold_dominant
                    and "simhei" in dominant.casefold()
                )
            )
            if not valid_body_font:
                errors.append(
                    f"{placement.get('id')}: Chinese body font is "
                    f"{dominant or 'not detectable'}, expected SimSun "
                    "(or SimHei when source-inherited bold dominates)."
                )
            if actual["font_sizes"]:
                actual_size = statistics.median(actual["font_sizes"])
                if differs(
                    actual_size, ZH_ACADEMIC_V1.body_font_size, tolerance=0.2
                ):
                    errors.append(
                        f"{placement.get('id')}: rendered body font size is "
                        f"{actual_size:.2f}pt, expected 11.5pt."
                    )
            if role == "paragraph-start":
                if differs(
                    placement.get("text_indent"),
                    ZH_ACADEMIC_V1.body_text_indent,
                ):
                    errors.append(
                        f"{placement.get('id')}: paragraph indent metadata "
                        "is not 23pt."
                    )
                primary_id = str(
                    next(
                        iter(
                            placement.get(
                                "ids", [placement.get("id")]
                            )
                        ),
                        placement.get("id") or "",
                    )
                )
                if (
                    measurable_indent_ids is None
                    or primary_id in measurable_indent_ids
                ):
                    delta = first_line_delta(document, placement)
                    if delta is None:
                        warnings.append(
                            f"{placement.get('id')}: first-line indent was not measurable."
                        )
                    elif not 21.0 <= delta <= 25.0:
                        errors.append(
                            f"{placement.get('id')}: rendered first-line indent "
                            f"is {delta:.2f}pt, expected about 23pt."
                        )
            elif differs(
                placement.get("text_indent"), ZH_ACADEMIC_V1.list_text_indent
            ) or differs(
                placement.get("padding_left"), ZH_ACADEMIC_V1.list_padding_left
            ):
                errors.append(
                    f"{placement.get('id')}: list hanging indent metadata "
                    "does not match the profile."
                )
        elif role == "heading" and placement.get("translatable"):
            metrics["headings_checked"] += 1
            if "simhei" not in dominant.casefold():
                errors.append(
                    f"{placement.get('id')}: Chinese heading font is "
                    f"{dominant or 'not detectable'}, expected SimHei."
                )
            level = int(placement.get("heading_level") or 1)
            expected_size = (
                ZH_ACADEMIC_V1.heading_level_one_size
                if level == 1
                else ZH_ACADEMIC_V1.heading_lower_size
            )
            if differs(placement.get("font_size"), expected_size):
                errors.append(
                    f"{placement.get('id')}: heading level {level} size is not "
                    f"{expected_size:g}pt."
                )
            if actual["font_sizes"]:
                actual_size = statistics.median(actual["font_sizes"])
                if differs(actual_size, expected_size, tolerance=0.2):
                    errors.append(
                        f"{placement.get('id')}: rendered heading size is "
                        f"{actual_size:.2f}pt, expected {expected_size:g}pt."
                    )
            index = sequence_index[id(placement)]
            next_placement = (
                placements[index + 1]
                if index + 1 < len(placements)
                else None
            )
            if (
                next_placement is None
                or next_placement.get("output_page")
                != placement.get("output_page")
            ):
                errors.append(
                    f"{placement.get('id')}: heading is orphaned at a page end."
                )
        metrics["joined_continuations"] += int(
            placement.get("joined_continuations") or 0
        )

    metrics["han_fonts"] = dict(global_fonts)
    metrics["latin_fonts"] = dict(global_latin_fonts)
    unexpected_latin = {
        name: count
        for name, count in global_latin_fonts.items()
        if not is_expected_latin_font(name)
    }
    if unexpected_latin:
        errors.append(
            "Translated-body Latin text uses unexpected fonts: "
            + ", ".join(
                f"{name} ({count})"
                for name, count in sorted(unexpected_latin.items())
            )
        )
    if body_font_sizes:
        body_size_median = statistics.median(body_font_sizes)
        metrics["body_font_size_median"] = round(body_size_median, 3)
        if differs(
            body_size_median, ZH_ACADEMIC_V1.body_font_size, tolerance=0.2
        ):
            errors.append(
                f"Rendered body font median is {body_size_median:.2f}pt, "
                "expected 11.5pt."
            )
    if body_line_pitches:
        line_pitch_median = statistics.median(body_line_pitches)
        metrics["body_line_pitch_median"] = round(line_pitch_median, 3)
        if differs(
            line_pitch_median, ZH_ACADEMIC_V1.body_line_pitch, tolerance=0.5
        ):
            errors.append(
                f"Rendered body line pitch is {line_pitch_median:.2f}pt, "
                "expected 16.9pt."
            )
    else:
        warnings.append("Rendered body line pitch was not measurable.")
    if body_glyph_width_ratios:
        glyph_ratio_median = statistics.median(body_glyph_width_ratios)
        metrics["han_glyph_width_ratio_median"] = round(glyph_ratio_median, 3)
        if not 0.98 <= glyph_ratio_median <= 1.02:
            errors.append(
                f"Rendered Han glyph width ratio is {glyph_ratio_median:.3f}; "
                "expected natural 1em spacing."
            )
    if content_rects:
        translated_pages = [
            document[int(placement["output_page"]) - 1]
            for placement in text_placements
        ]
        page_width = float(translated_pages[0].rect.width)
        left_margin = min(rect.x0 for rect in content_rects)
        right_margin = page_width - max(rect.x1 for rect in content_rects)
        metrics["content_left_margin"] = round(left_margin, 3)
        metrics["content_right_margin"] = round(right_margin, 3)
        if differs(left_margin, ZH_ACADEMIC_V1.margin_left, tolerance=2.0):
            errors.append(
                f"Rendered left margin is {left_margin:.2f}pt, expected 53pt."
            )
        if differs(right_margin, ZH_ACADEMIC_V1.margin_right, tolerance=2.0):
            errors.append(
                f"Rendered right margin is {right_margin:.2f}pt, expected 53pt."
            )
    boundary = layout.get("boundary_layout") or {}
    translation_start = int(boundary.get("translation_page_start") or 0)
    translation_end = int(boundary.get("translation_page_end") or 0)
    if 1 <= translation_start <= translation_end <= len(document):
        reader_metrics = analyze_reader_compatibility(
            document, translation_start, translation_end
        )
        metrics.update(reader_metrics)
        if reader_metrics["oversized_text_forms"]:
            errors.append(
                "Translated pages contain "
                f"{reader_metrics['oversized_text_forms']} oversized "
                "measurement-page forms, so the text layer is not expressed "
                "directly in final-page geometry."
            )
        if reader_metrics["pdf_native_links"]:
            errors.append(
                "Translated pages contain "
                f"{reader_metrics['pdf_native_links']} unexpected intrinsic "
                "PDF link annotations."
            )
    if any("microsoft yahei" in name.casefold() for name in global_fonts):
        errors.append("Rendered Chinese text still uses Microsoft YaHei.")
    if metrics["paragraphs_checked"] == 0:
        errors.append("No translated paragraphs were checked for typography.")
    if metrics["headings_checked"] == 0:
        errors.append("No translated headings were checked for typography.")
    return errors, warnings, metrics


def rectangle_covered_by_regions(
    rectangle: pymupdf.Rect, regions: list[pymupdf.Rect]
) -> bool:
    """Check complete coverage without double-counting overlapping clips."""
    clipped = [rectangle & region for region in regions]
    clipped = [region for region in clipped if not region.is_empty]
    edges = sorted(
        {value for region in clipped for value in (region.x0, region.x1)}
    )
    area = 0.0
    for left, right in pairwise(edges):
        intervals = sorted(
            (region.y0, region.y1)
            for region in clipped
            if region.x0 <= left and region.x1 >= right
        )
        end = float("-inf")
        for top, bottom in intervals:
            area += (right - left) * max(0.0, bottom - max(top, end))
            end = max(end, bottom)
    return area >= rectangle.get_area() - 0.5


def validate_boundary_layout(
    source_document: pymupdf.Document,
    output_document: pymupdf.Document,
    layout: dict[str, Any] | None,
    elements: list[dict[str, Any]] | None = None,
    manifest: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    metrics: dict[str, Any] = {
        "profile": None,
        "front_segments": 0,
        "tail_segments": 0,
        "segments_checked": 0,
        "original_units_checked": 0,
    }
    if layout is None:
        errors.append("Boundary layout report is missing.")
        return errors, metrics
    boundary = layout.get("boundary_layout")
    if not isinstance(boundary, dict):
        errors.append("Boundary layout metadata is missing.")
        return errors, metrics
    expected_profile = "original-front-translated-body-original-tail"
    metrics["profile"] = boundary.get("profile")
    if boundary.get("profile") != expected_profile:
        errors.append(
            f"Unexpected boundary profile: {boundary.get('profile')!r}."
        )
        return errors, metrics

    segments = list(boundary.get("segments") or [])
    front = [item for item in segments if item.get("role") == "front-original"]
    tail = [item for item in segments if item.get("role") == "tail-original"]
    metrics["front_segments"] = len(front)
    metrics["tail_segments"] = len(tail)
    translation_start = int(boundary.get("translation_page_start") or 0)
    translation_end = int(boundary.get("translation_page_end") or 0)
    if not (1 <= translation_start <= translation_end <= len(output_document)):
        errors.append("Translated-body page range is invalid.")
        return errors, metrics

    front_pages = list(
        dict.fromkeys(int(item.get("output_page") or 0) for item in front)
    )
    tail_pages = list(
        dict.fromkeys(int(item.get("output_page") or 0) for item in tail)
    )
    if front_pages != list(range(1, translation_start)):
        errors.append(
            "Original front segments are not contiguous before the body."
        )
    if tail_pages != list(
        range(translation_end + 1, len(output_document) + 1)
    ):
        errors.append(
            "Original tail segments are not contiguous after the body."
        )
    placement_pages = {
        int(item.get("output_page") or 0)
        for item in list(layout.get("placements") or [])
    }
    boundary_pages = set(front_pages + tail_pages)
    if placement_pages & boundary_pages:
        errors.append(
            "Translated flow content shares an original boundary page."
        )

    original_units: list[tuple[dict[str, Any], str]] = []
    body_units: list[dict[str, Any]] = []
    if elements is not None and manifest is not None:
        ordered = sorted(elements, key=lambda item: int(item["order"]))
        positions = {
            str(item["id"]): index for index, item in enumerate(ordered)
        }
        source_boundary = manifest.get("boundary") or {}
        start = positions.get(
            str(source_boundary.get("introduction_id") or "")
        )
        stop_id = str(source_boundary.get("post_body_stop_id") or "")
        stop = positions.get(stop_id, len(ordered))
        explicit_include_ids = set(source_boundary.get("include_ids") or [])
        included = [
            index for index, item in enumerate(ordered)
            if (item.get("translatable") or item["id"] in explicit_include_ids)
            and not item.get("render_suppressed")
        ]
        if start is not None and included:
            start = min(start, min(included))
            stop = max(stop, max(included) + 1)
        if start is None or stop <= start:
            errors.append(
                "Source boundary IDs are invalid for overlap validation."
            )
        else:
            body_units = ordered[start:stop]
            original_units = [
                (item, "front-original") for item in ordered[:start]
            ] + [(item, "tail-original") for item in ordered[stop:]]

    source_regions: dict[tuple[int, str], list[pymupdf.Rect]] = {}
    target_regions: dict[int, list[pymupdf.Rect]] = {}

    for segment in segments:
        source_page_index = int(segment.get("source_page") or 0) - 1
        output_page_index = int(segment.get("output_page") or 0) - 1
        if not (0 <= source_page_index < len(source_document)):
            errors.append(
                f"Boundary source page is invalid: {source_page_index + 1}"
            )
            continue
        if not (0 <= output_page_index < len(output_document)):
            errors.append(
                f"Boundary output page is invalid: {output_page_index + 1}"
            )
            continue
        source_rect = pymupdf.Rect(segment.get("source_bbox") or [])
        target_rect = pymupdf.Rect(segment.get("target_bbox") or [])
        if source_rect.is_empty or target_rect.is_empty:
            errors.append(
                f"Boundary segment has an empty rectangle: "
                f"source page {source_page_index + 1}"
            )
            continue
        source_bounds = source_document[source_page_index].rect + (-0.01, -0.01, 0.01, 0.01)
        target_bounds = output_document[output_page_index].rect + (-0.01, -0.01, 0.01, 0.01)
        if not source_bounds.contains(source_rect):
            errors.append(
                f"Boundary source crop exceeds page {source_page_index + 1}."
            )
        if not target_bounds.contains(target_rect):
            errors.append(
                f"Boundary target crop exceeds page {output_page_index + 1}."
            )
        if (
            abs(source_rect.width - target_rect.width) > 0.1
            or abs(source_rect.height - target_rect.height) > 0.1
        ):
            errors.append(
                f"Original boundary scale changed on output page {output_page_index + 1}."
            )
        previous_targets = target_regions.setdefault(output_page_index, [])
        if any(
            (target_rect & previous).get_area() > 0.5
            for previous in previous_targets
        ):
            errors.append(
                f"Original boundary clips overlap on output page {output_page_index + 1}."
            )
        previous_targets.append(target_rect)
        source_regions.setdefault(
            (source_page_index + 1, str(segment.get("role") or "")), []
        ).append(source_rect)
        for unit in body_units:
            if (
                int(unit["page"]) == source_page_index + 1
                and (source_rect & pymupdf.Rect(unit["bbox"])).get_area() > 0.5
            ):
                errors.append(
                    f"Original boundary crop overlaps translated body unit {unit['id']}."
                )
        source_text = normalize_text(
            source_document[source_page_index].get_text(
                "text", clip=source_rect, sort=True
            )
        )
        output_text = normalize_text(
            output_document[output_page_index].get_text(
                "text", clip=target_rect, sort=True
            )
        )
        if len(source_text) >= 32 and not invariant_present(
            source_text, output_text
        ):
            errors.append(
                f"Original boundary content differs on output page "
                f"{output_page_index + 1}."
            )
        if segment.get("mode") == "full-page":
            source_page = source_document[source_page_index]
            output_page = output_document[output_page_index]
            if (
                abs(source_page.rect.width - output_page.rect.width) > 0.1
                or abs(source_page.rect.height - output_page.rect.height) > 0.1
            ):
                errors.append(
                    f"Full original page geometry changed on output page "
                    f"{output_page_index + 1}."
                )
        metrics["segments_checked"] += 1
    for unit, role in original_units:
        if unit.get("render_suppressed"):
            continue
        if not rectangle_covered_by_regions(
            pymupdf.Rect(unit["bbox"]),
            source_regions.get((int(unit["page"]), role), []),
        ):
            errors.append(
                f"Original boundary unit {unit['id']} is not completely preserved."
            )
        metrics["original_units_checked"] += 1
    return errors, metrics


def validate_visual_layout(
    manifest: dict[str, Any],
    manifest_path: Path,
    layout: dict[str, Any] | None,
    output_document: pymupdf.Document,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """Compare the hash-bound source inventory with rendered visual geometry."""
    errors: list[str] = []
    warnings: list[str] = []
    path_name = str((manifest.get("paths") or {}).get("visual_layout") or "")
    if not path_name:
        if int(manifest.get("schema_version") or 0) >= 5:
            errors.append("Schema-v5 manifest has no paths.visual_layout.")
        return errors, warnings, {"status": "not-available"}
    path = manifest_path.parent / path_name
    if not path.exists():
        return [f"Visual layout inventory is missing: {path}"], warnings, {}
    try:
        inventory = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return (
            [f"Visual layout inventory could not be read: {error}"],
            warnings,
            {},
        )
    if inventory.get("source_sha256") != manifest.get("source_sha256"):
        errors.append(
            "Visual layout inventory source hash differs from manifest."
        )
    errors.extend(str(item) for item in inventory.get("errors") or [])
    warnings.extend(str(item) for item in inventory.get("warnings") or [])
    expected = {
        str(item["visual_id"]): dict(item)
        for item in inventory.get("visuals") or []
        if isinstance(item, dict) and item.get("visual_id")
    }
    visual_report = dict((layout or {}).get("visual_layout") or {})
    actual_items = list(visual_report.get("placements") or [])
    actual: dict[str, list[dict[str, Any]]] = {}
    for item in actual_items:
        actual.setdefault(str(item.get("visual_id") or ""), []).append(item)
    full_original_pages = {
        int(item.get("source_page") or 0)
        for item in (
            ((layout or {}).get("boundary_layout") or {}).get("segments") or []
        )
        if item.get("mode") == "full-page"
        and item.get("role") in {"front-original", "tail-original"}
    }
    original_regions: dict[int, list[pymupdf.Rect]] = {}
    for segment in ((layout or {}).get("boundary_layout") or {}).get(
        "segments"
    ) or []:
        if segment.get("role") in {
            "front-original",
            "tail-original",
        } and segment.get("source_bbox"):
            original_regions.setdefault(
                int(segment.get("source_page") or 0), []
            ).append(pymupdf.Rect(segment["source_bbox"]))
    placements = {
        str(unit_id): item
        for item in (layout or {}).get("placements") or []
        for unit_id in item.get("ids") or [item.get("id")]
        if unit_id
    }
    units_name = str((manifest.get("paths") or {}).get("translation_units") or "")
    source_units = {
        str(unit["id"]): unit
        for unit in read_jsonl(manifest_path.parent / units_name)
    } if units_name else {}
    checked = 0
    fallback_count = 0
    original_boundary_visuals = 0
    for visual_id, source_item in expected.items():
        rendered = actual.get(visual_id, [])
        if not rendered and (
            int(source_item.get("page") or 0) in full_original_pages
            or (
                source_item.get("source_bbox")
                and rectangle_covered_by_regions(
                    pymupdf.Rect(source_item["source_bbox"]),
                    original_regions.get(
                        int(source_item.get("page") or 0), []
                    ),
                )
            )
        ):
            checked += 1
            original_boundary_visuals += 1
            continue
        if len(rendered) != 1:
            errors.append(
                f"{visual_id}: expected one rendered visual, found {len(rendered)}."
            )
            continue
        item = rendered[0]
        checked += 1
        fallback_count += int(bool(source_item.get("automatic_fallback")))
        width = float(item.get("target_width_pt") or 0.0)
        height = float(item.get("target_height_pt") or 0.0)
        expected_width = float(source_item.get("target_width_pt") or 0.0)
        page_number = int(item.get("output_page") or 0)
        caption = placements.get(str(source_item.get("caption_id") or ""))
        source_caption = source_units.get(str(source_item.get("caption_id") or ""))
        calibration_factor = 1.0
        if (caption and source_caption and caption.get("id") == item.get("id")
                and caption.get("render_mode") == "source_clip"
                and int(caption.get("source_page") or 0) == int(source_caption.get("page") or 0)
                and rectangle_covered_by_regions(
                    pymupdf.Rect(source_caption.get("bbox") or []),
                    [pymupdf.Rect(caption.get("source_bbox") or [])])
                and source_item.get("source_bbox") and expected_width
                and 1 <= page_number <= len(output_document)):
            visual_rect = pymupdf.Rect(source_item["source_bbox"])
            combined = visual_rect | pymupdf.Rect(source_caption["bbox"])
            if not visual_rect.is_empty and not combined.is_empty:
                page_rect = output_document[page_number - 1].rect
                content_width = page_rect.width - ZH_ACADEMIC_V1.margin_left - ZH_ACADEMIC_V1.margin_right
                content_height = page_rect.height - ZH_ACADEMIC_V1.margin_top - ZH_ACADEMIC_V1.margin_bottom
                base_scale = expected_width / visual_rect.width
                # Only the canonical combined envelope can justify reducing the
                # calibrated visual scale to fit its preserved vector caption.
                calibration_factor = min(
                    1.0, content_width / (combined.width * base_scale),
                    content_height * 0.72 / (combined.height * base_scale),
                )
                expected_width *= calibration_factor
        if expected_width and abs(width - expected_width) > 0.6:
            errors.append(
                f"{visual_id}: rendered width {width:.2f}pt differs from "
                f"target {expected_width:.2f}pt."
            )
        expected_ratio = float(source_item.get("aspect_ratio") or 0.0)
        actual_ratio = width / height if height > 0 else 0.0
        if expected_ratio and abs(actual_ratio - expected_ratio) > 0.003:
            errors.append(f"{visual_id}: rendered aspect ratio changed.")
        bbox = pymupdf.Rect(item.get("bbox") or [])
        if page_number < 1 or page_number > len(output_document):
            errors.append(f"{visual_id}: output page is invalid.")
        elif (
            bbox.x0 < ZH_ACADEMIC_V1.margin_left - 0.6
            or bbox.x1
            > output_document[page_number - 1].rect.width
            - ZH_ACADEMIC_V1.margin_right
            + 0.6
        ):
            errors.append(
                f"{visual_id}: rendered visual exceeds content width."
            )
        reviewed_captionless = (
            source_item.get("captionless") is True
            and bool(source_item.get("source_review_reason"))
            and not source_item.get("caption_id")
        )
        if not caption and not reviewed_captionless:
            errors.append(f"{visual_id}: caption placement is missing.")
        elif caption and int(caption.get("output_page") or 0) != page_number:
            errors.append(
                f"{visual_id}: visual and caption are on different pages."
            )
        if caption and (
            caption.get("render_mode") == "source_clip"
            or (source_caption or {}).get("render_mode") == "source_clip"
        ):
            if not source_caption or (
                caption.get("render_mode") != "source_clip"
                or pymupdf.Rect(source_caption.get("bbox") or []).is_empty
                or int(caption.get("source_page") or 0) != int(source_caption.get("page") or 0)
                or not rectangle_covered_by_regions(
                    pymupdf.Rect(source_caption.get("bbox") or []),
                    [pymupdf.Rect(caption.get("source_bbox") or [])],
                )
            ):
                errors.append(f"{visual_id}: caption source region is not covered by its actual crop.")
        source_internal = source_item.get("source_internal_font_pt")
        target_internal = source_item.get("target_internal_font_pt")
        if source_internal is not None and target_internal is not None:
            target_internal = float(target_internal) * calibration_factor
            actual_internal = float(source_internal) * float(
                item.get("actual_scale") or 0.0
            )
            if abs(actual_internal - float(target_internal)) > 0.2:
                errors.append(
                    f"{visual_id}: internal font calibration differs by "
                    f"{abs(actual_internal - float(target_internal)):.2f}pt."
                )
    extra = sorted(set(actual) - set(expected) - {""})
    if extra:
        errors.append("Unexpected rendered visuals: " + ", ".join(extra))
    return (
        errors,
        warnings,
        {
            "status": "pass" if not errors else "fail",
            "inventory_path": str(path),
            "expected": len(expected),
            "checked": checked,
            "automatic_fallbacks": fallback_count,
            "original_boundary_visuals": original_boundary_visuals,
        },
    )


def main() -> int:
    args = parse_args()
    source = args.source.expanduser().resolve()
    translated = args.translated.expanduser().resolve()
    manifest_path = args.manifest.expanduser().resolve()
    translations_path = args.translations.expanduser().resolve()
    preview_dir = args.preview_dir.expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    units_path = manifest_path.parent / manifest["paths"]["translation_units"]
    units = read_jsonl(units_path)
    translations = read_jsonl(translations_path)

    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {}

    if not source.exists():
        errors.append(f"Source PDF not found: {source}")
    elif sha256_file(source) != manifest.get("source_sha256"):
        errors.append("Source PDF SHA-256 does not match manifest")
    if not translated.exists():
        errors.append(f"Translated PDF not found: {translated}")

    translation_map: dict[str, dict[str, Any]] = {}
    for record in translations:
        record_id = str(record.get("id", ""))
        if not record_id:
            errors.append("Translation record has no id")
        elif record_id in translation_map:
            errors.append(f"Duplicate translation id: {record_id}")
        else:
            translation_map[record_id] = record

    required = {unit["id"] for unit in units if unit.get("translatable")}
    bold_body_ids: set[str] = set()
    measurable_indent_ids = {
        record_id
        for record_id, record in translation_map.items()
        if has_measurable_prose_start(
            str(record.get("translated_text") or "")
        )
    }
    expected_inline = Counter(
        str(fragment.get("kind") or "unknown")
        for unit in units
        if unit.get("translatable")
        for fragment in dict(unit.get("inline_fragments") or {}).values()
    )
    expected_styles = Counter(
        str(kind)
        for unit in units
        if unit.get("translatable")
        for kind in dict(unit.get("style_tokens") or {}).values()
    )
    expected_footnote_ids = {
        str(unit["id"])
        for unit in units
        if unit.get("translatable") and unit.get("kind") == "footnote"
    }
    supplied = set(translation_map)
    for missing in sorted(required - supplied):
        errors.append(f"Missing translation: {missing}")
    for extra in sorted(supplied - required):
        errors.append(f"Unexpected translation id: {extra}")

    for unit in units:
        if not unit.get("translatable") or unit["id"] not in translation_map:
            continue
        translated_text = str(
            translation_map[unit["id"]].get("translated_text", "")
        ).strip()
        if not translated_text:
            errors.append(f"{unit['id']}: translated_text is empty")
            continue
        for token in unit.get("protected_tokens", {}):
            count = translated_text.count(token)
            if count != 1:
                errors.append(
                    f"{unit['id']}: {token} occurs {count} times in translation"
                )
        for token in unit.get("inline_fragments", {}):
            count = translated_text.count(token)
            if count != 1:
                errors.append(
                    f"{unit['id']}: inline fragment {token} occurs "
                    f"{count} times in translation"
                )
        for key in unit.get("style_tokens", {}):
            open_token = f"[[{key}_OPEN]]"
            close_token = f"[[{key}_CLOSE]]"
            if (
                translated_text.count(open_token) != 1
                or translated_text.count(close_token) != 1
            ):
                errors.append(
                    f"{unit['id']}: style token {key} must open and close once"
                )
            elif translated_text.find(open_token) > translated_text.find(
                close_token
            ):
                errors.append(f"{unit['id']}: style token {key} is reversed")
        if (
            unit["kind"] == "body"
            and len(unit.get("source_text", "")) >= 40
            and not HAN_RE.search(translated_text)
        ):
            errors.append(f"{unit['id']}: long body unit has no Chinese text")
        if normalize_text(translated_text) == normalize_text(
            unit.get("protected_text", "")
        ) and not is_preserved_formal_heading(unit, translated_text):
            errors.append(f"{unit['id']}: translation is unchanged from source")
        total_han = len(HAN_RE.findall(translated_text))
        bold_han = 0
        for key, kind in dict(unit.get("style_tokens") or {}).items():
            if kind != "bold":
                continue
            match = re.search(
                re.escape(f"[[{key}_OPEN]]")
                + r"(.*?)"
                + re.escape(f"[[{key}_CLOSE]]"),
                translated_text,
                re.DOTALL,
            )
            if match:
                bold_han += len(HAN_RE.findall(match.group(1)))
        if total_han and bold_han * 2 >= total_han:
            bold_body_ids.add(str(unit["id"]))

    rendered_paths: list[Path] = []
    output_text = ""
    boundary_invariant_text = ""
    code_invariant_text = ""
    output_page_count = 0
    typography_metrics: dict[str, Any] = {}
    boundary_metrics: dict[str, Any] = {}
    visual_metrics: dict[str, Any] = {}
    source_clip_metrics: dict[str, Any] = {}
    code_preservation_evidence: list[dict[str, Any]] = []
    caption_preservation_evidence: list[dict[str, Any]] = []
    native_display_evidence: list[dict[str, Any]] = []
    review_payload: dict[str, Any] = {}
    layout_path = translated.with_suffix(".layout.json")
    layout: dict[str, Any] | None = None
    if layout_path.exists():
        try:
            loaded_layout = json.loads(layout_path.read_text(encoding="utf-8"))
            if isinstance(loaded_layout, dict):
                layout = loaded_layout
            else:
                errors.append("Typography layout report is not a JSON object.")
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"Typography layout report could not be read: {error}")
    if layout is not None and int(manifest.get("schema_version") or 1) >= 2:
        if layout.get("render_backend") == "native-lualatex-v1":
            errors.extend(validate_reviewed_flow(manifest, units, layout))
            try:
                review_name = str((manifest.get("paths") or {}).get("math_review") or "math-review.json")
                review_payload = json.loads((manifest_path.parent / review_name).read_text(encoding="utf-8"))
                if review_payload.get("source_sha256") != manifest.get("source_sha256"):
                    errors.append("Math review does not match the source PDF.")
                else:
                    errors.extend(validate_display_ownership(manifest, units, layout, dict(review_payload.get("entries") or {})))
            except (OSError, ValueError, TypeError) as error:
                errors.append(f"Display ownership review could not be checked: {error}")
        rich = dict(layout.get("rich_text") or {})
        actual_inline = Counter(dict(rich.get("inline_fragments") or {}))
        actual_styles = Counter(dict(rich.get("styles") or {}))
        if actual_inline != expected_inline:
            errors.append(
                "Rich inline-fragment counts differ between units and layout."
            )
        if actual_styles != expected_styles:
            errors.append("Rich style counts differ between units and layout.")
        typography = dict(layout.get("typography") or {})
        if expected_inline.get("math", 0):
            math_font = Path(str(typography.get("math_font") or "")).name.casefold()
            if math_font not in {
                "latinmodern-math.otf",
                "cambria.ttc",
                "dejavuserif.ttf",
            }:
                errors.append(
                    f"Math font is unavailable or unexpected: {math_font or 'missing'}"
                )
            strategies = dict(rich.get("math_render_strategies") or {})
            if layout.get("render_backend") == "native-lualatex-v1":
                if int(strategies.get("native-font") or 0) != int(
                    expected_inline.get("math") or 0
                ):
                    errors.append(
                        "Not every inline mathematical fragment used the "
                        "native math font."
                    )
                forbidden = {
                    name: count
                    for name, count in strategies.items()
                    if name in {
                        "tex-vector",
                        "source-vector",
                        "font-vector",
                        "image",
                    }
                    and int(count or 0) > 0
                }
                review_metrics = dict(rich.get("math_review") or {})
                if forbidden or int(
                    review_metrics.get("image_fallbacks") or 0
                ):
                    errors.append(
                        "Native mathematical export contains an image/vector "
                        f"fallback: {forbidden}"
                    )
                if int(review_metrics.get("unresolved") or 0):
                    errors.append("Native mathematical review is unresolved.")
            elif not any(
                strategies.get(name, 0)
                for name in ("tex-vector", "source-vector")
            ):
                errors.append(
                    "No mathematical fragment used a vector render strategy."
                )
        footnote_placements = list(rich.get("footnote_placements") or [])
        placed_footnotes = {
            str(item.get("id"))
            for item in footnote_placements
            if item.get("location") == "page-bottom"
            or (
                item.get("location") == "end-of-body"
                and next(
                    (
                        bool(unit.get("oversized_footnote"))
                        for unit in units
                        if str(unit.get("id")) == str(item.get("id"))
                    ),
                    False,
                )
            )
        }
        missing_footnotes = sorted(expected_footnote_ids - placed_footnotes)
        if missing_footnotes:
            errors.append(
                "Footnotes lack a bottom/end placement: "
                + ", ".join(missing_footnotes)
            )
        unit_by_id = {str(unit["id"]): unit for unit in units}
        placement_by_id = index_placements_by_id(
            list(layout.get("placements") or [])
        )
        for item in footnote_placements:
            unit = unit_by_id.get(str(item.get("id"))) or {}
            anchor_id = str(unit.get("anchor_id") or "")
            if not anchor_id:
                continue
            anchor = placement_by_id.get(anchor_id)
            if not anchor:
                errors.append(f"{item.get('id')}: footnote anchor is missing.")
            elif int(item.get("output_page") or 0) != int(
                anchor.get("output_page") or 0
            ):
                errors.append(
                    f"{item.get('id')}: footnote is not on its anchor page."
                )
        style_consistency = dict(rich.get("style_consistency") or {})
        if int(style_consistency.get("unexpected_bold_body_units") or 0):
            errors.append("Layout reports unexpected bold body text.")
    if translated.exists():
        try:
            output_document = pymupdf.open(translated)
            boundary_invariant_text = original_boundary_output_text(
                output_document, layout
            )
            output_page_count = len(output_document)
            if output_page_count == 0:
                errors.append("Translated PDF has no pages")
            scale = args.dpi / 72.0
            preview_dir.mkdir(parents=True, exist_ok=True)
            for page_index, page in enumerate(output_document):
                output_text += "\n" + page.get_text("text", sort=True)
                try:
                    pixmap = page.get_pixmap(
                        matrix=pymupdf.Matrix(scale, scale), alpha=False
                    )
                    path = preview_dir / f"page-{page_index + 1:04d}.png"
                    pixmap.save(path)
                    rendered_paths.append(path)
                except Exception as error:
                    errors.append(
                        f"Page {page_index + 1} failed to render: {error}"
                    )
            typography_errors, typography_warnings, typography_metrics = (
                validate_typography(
                    output_document,
                    layout,
                    required,
                    bold_body_ids,
                    measurable_indent_ids,
                )
            )
            errors.extend(typography_errors)
            warnings.extend(typography_warnings)
            source_document = pymupdf.open(source)
            try:
                boundary_errors, boundary_metrics = validate_boundary_layout(
                    source_document, output_document, layout, units, manifest
                )
                errors.extend(boundary_errors)
                visual_errors, visual_warnings, visual_metrics = (
                    validate_visual_layout(
                        manifest,
                        manifest_path,
                        layout,
                        output_document,
                    )
                )
                errors.extend(visual_errors)
                warnings.extend(visual_warnings)
                source_clip_errors, source_clip_metrics = (
                    validate_source_clip_envelopes(layout, units, source_document=source_document)
                )
                errors.extend(source_clip_errors)
                errors.extend(validate_code_preservation(
                    output_document, select_body_units(units, manifest), layout,
                    source_document=source_document, evidence=code_preservation_evidence))
                errors.extend(validate_image_caption_preservation(
                    source_document, output_document, select_body_units(units, manifest),
                    layout, caption_preservation_evidence))
                errors.extend(validate_native_display_invariants(
                    output_document, manifest, units, layout, review_payload,
                    native_display_evidence))
                code_invariant_text = original_code_output_text(output_document, layout)
            finally:
                source_document.close()
            output_document.close()
        except Exception as error:
            errors.append(f"Translated PDF could not be opened: {error}")

    if PLACEHOLDER_RE.search(output_text):
        errors.append("Rendered PDF contains unresolved protected placeholders")
    if CONTROL_TOKEN_RE.search(output_text):
        errors.append("Rendered PDF contains unresolved rich-text tokens")
    source_glyph_text = ""
    if source.exists():
        source_glyph_document = pymupdf.open(source)
        try:
            source_glyph_text = "\n".join(
                page.get_text("text", sort=True)
                for page in source_glyph_document
            )
        finally:
            source_glyph_document.close()
    math_review_name = (manifest.get("paths") or {}).get("math_review")
    reviewed_squares = reviewed_proof_square_count(
        manifest_path.parent / math_review_name if math_review_name else None,
        source,
    )
    metrics["reviewed_proof_squares"] = reviewed_squares
    for marker in suspicious_glyphs(output_text, source_glyph_text, reviewed_squares):
        errors.append(f"Rendered PDF text contains suspicious glyph {marker!r}")

    normalized_output = normalize_text(output_text + "\n" + boundary_invariant_text + "\n" + code_invariant_text)
    validated_image_code_ids = {
        entry["id"] for entry in code_preservation_evidence
        if entry["status"] == "pass" and entry["source_modality"] in {"image", "nontext-vector"}
    }
    validated_image_caption_ids = {
        entry["id"] for entry in caption_preservation_evidence
        if entry["status"] == "pass" and entry.get("source_modality") in {"image", "nontext-vector"}
    }
    validated_native_display_ids = {
        unit_id for entry in native_display_evidence if entry["status"] == "pass"
        for unit_id in entry["source_ids"]
    }
    invariant_missing = missing_invariant_ids(
        units, normalized_output, layout, validated_image_code_ids=validated_image_code_ids,
        validated_image_caption_ids=validated_image_caption_ids,
        validated_native_display_ids=validated_native_display_ids)
    if invariant_missing:
        message = (
            "Invariant source text was not found verbatim in extracted output: "
            + ", ".join(invariant_missing[:20])
        )
        if args.strict_invariants:
            errors.append(message)
        else:
            warnings.append(message)

    contact_sheet = preview_dir / "contact-sheet.png"
    if rendered_paths:
        make_contact_sheet(rendered_paths, contact_sheet)
    metrics.update(
        {
            "source_pages": manifest.get("page_count"),
            "translated_pages": output_page_count,
            "translation_units": len(required),
            "rendered_pages": len(rendered_paths),
            "strict_invariants": bool(args.strict_invariants),
            "invariant_text_misses": len(invariant_missing),
            "contact_sheet": str(contact_sheet) if contact_sheet.exists() else None,
            "typography": typography_metrics,
            "boundary_layout": boundary_metrics,
            "visual_layout": visual_metrics,
            "source_clip_envelopes": source_clip_metrics,
            "code_preservation": code_preservation_evidence,
            "caption_preservation": caption_preservation_evidence,
            "native_display_invariants": native_display_evidence,
            "rich_text": {
                "inline_fragments": dict(expected_inline),
                "styles": dict(expected_styles),
                "footnotes": len(expected_footnote_ids),
            },
        }
    )
    report = {
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "warnings": warnings,
        "metrics": metrics,
    }
    report_path = preview_dir / "validation-report.json"
    preview_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
