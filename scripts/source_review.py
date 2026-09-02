#!/usr/bin/env python3
"""Apply source-hash-bound extraction and visual review decisions.

This module never translates text. It deterministically replays reviewed
corrections against freshly extracted paper elements.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SUPPORTED_OPERATIONS = {
    "suppress_units",
    "replace_unit",
    "insert_unit",
    "group_visual",
}
TEXT_KINDS = {
    "abstract-heading",
    "body",
    "caption",
    "footnote",
    "front-matter",
    "heading",
    "keywords",
}
CLIP_KINDS = {"equation", "figure", "figure-text", "margin-note", "table"}
MATH_SIGNAL_RE = re.compile(r"[=<>≤≥≈≠∑∏∫√∞±×÷∂∇∈∉→←↔⊥∗◦^_{}\\]")
PROSE_WORD_RE = re.compile(r"\b[A-Za-z]{3,}\b")


class SourceReviewError(ValueError):
    """A source review cannot be applied safely."""


def make_source_review(source_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "reviewed": False,
        "reason": "",
        "operations": [],
    }


def load_source_review(path: Path, source_sha256: str) -> dict[str, Any]:
    if not path.exists():
        return make_source_review(source_sha256)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SourceReviewError(f"Cannot read source review: {error}") from error
    if not isinstance(payload, dict):
        raise SourceReviewError("Source review must be a JSON object.")
    if int(payload.get("schema_version") or 0) != SCHEMA_VERSION:
        raise SourceReviewError(
            f"Unsupported source review schema: {payload.get('schema_version')!r}"
        )
    if payload.get("source_sha256") != source_sha256:
        if payload.get("reviewed") is True:
            raise SourceReviewError(
                "Reviewed source-review.json belongs to a different source PDF."
            )
        return make_source_review(source_sha256)
    operations = payload.get("operations")
    if not isinstance(operations, list):
        raise SourceReviewError("source-review.json operations must be a list.")
    if operations and payload.get("reviewed") is not True:
        raise SourceReviewError(
            "Source-review operations require reviewed=true and a reason."
        )
    if payload.get("reviewed") is True and not str(payload.get("reason") or "").strip():
        raise SourceReviewError("Reviewed source review requires a non-empty reason.")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise SourceReviewError(
                f"{path.name}:{line_number}: invalid JSON: {error}"
            ) from error
        if not isinstance(record, dict) or not record.get("id"):
            raise SourceReviewError(
                f"{path.name}:{line_number}: expected an object with an id."
            )
        records.append(record)
    return records


def _nonempty_translation_ids(path: Path | None) -> set[str]:
    if path is None:
        return set()
    return {
        str(record["id"])
        for record in _read_jsonl(path)
        if str(record.get("translated_text") or "").strip()
    }


def _clear_protection(element: dict[str, Any]) -> None:
    element["protected_text"] = str(element.get("source_text") or "")
    element["protected_tokens"] = {}
    element["inline_fragments"] = {}
    element["style_tokens"] = {}
    element["block_style"] = "normal"


def _validate_reason(operation: dict[str, Any], index: int) -> str:
    reason = str(operation.get("reason") or "").strip()
    if not reason:
        raise SourceReviewError(f"Operation {index} requires a non-empty reason.")
    return reason


def _validate_bbox(
    bbox: Any,
    page: int,
    page_sizes: dict[int, tuple[float, float]],
    *,
    label: str,
) -> list[float]:
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or any(not isinstance(value, (int, float)) for value in bbox)
    ):
        raise SourceReviewError(f"{label}: bbox must contain four numbers.")
    values = [float(value) for value in bbox]
    x0, y0, x1, y1 = values
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
        raise SourceReviewError(f"{label}: bbox is empty or has negative coordinates.")
    if page not in page_sizes:
        raise SourceReviewError(f"{label}: unknown page {page}.")
    width, height = page_sizes[page]
    if x1 > width + 0.5 or y1 > height + 0.5:
        raise SourceReviewError(f"{label}: bbox exceeds source page {page}.")
    return values


def _default_render_mode(kind: str) -> str:
    return "source_clip" if kind in CLIP_KINDS else "text"


def _refresh_translation_fields(
    element: dict[str, Any],
    protect_element: Callable[[dict[str, Any]], None],
) -> None:
    if element.get("translatable"):
        if element.get("render_mode") != "text":
            raise SourceReviewError(
                f"{element['id']}: translatable units must use render_mode=text."
            )
        protect_element(element)
    else:
        _clear_protection(element)


def _renumber(elements: list[dict[str, Any]]) -> None:
    page_orders: dict[int, int] = {}
    for order, element in enumerate(elements, start=1):
        page = int(element["page"])
        page_orders[page] = page_orders.get(page, 0) + 1
        element["order"] = order
        element["page_order"] = page_orders[page]


def apply_source_review(
    elements: list[dict[str, Any]],
    review: dict[str, Any],
    source_sha256: str,
    page_records: list[dict[str, Any]],
    protect_element: Callable[[dict[str, Any]], None],
    *,
    translations_path: Path | None = None,
) -> dict[str, Any]:
    """Replay reviews without changing the source of completed translations.

    On resume, compare against the last persisted reviewed inventory, not the
    fresh extraction to which the same corrections still need to be applied.
    No checkpoint is changed unless the entire replay preserves that contract.
    """
    completed_ids = _nonempty_translation_ids(translations_path)
    previous = _previous_reviewed_units(
        translations_path, source_sha256, completed_ids
    )
    working = copy.deepcopy(elements)
    report = _apply_source_review_operations(
        working,
        review,
        source_sha256,
        page_records,
        protect_element,
        # A source-bound baseline moves the guard to the final replay result.
        # Without one, keep the conservative operation-level checks below.
        translations_path=translations_path if previous is None else None,
    )
    if previous is not None:
        final = {str(element["id"]): element for element in working}
        for element_id in sorted(completed_ids):
            before, after = previous.get(element_id), final.get(element_id)
            if (
                before is None
                or after is None
                or not before.get("translatable")
                or before.get("render_suppressed")
                or _completed_unit_contract(before)
                != _completed_unit_contract(after)
            ):
                raise SourceReviewError(
                    f"Refusing to change completed translation unit {element_id} "
                    "after replay against the persisted reviewed source."
                )
    elements[:] = working
    return report


def _previous_reviewed_units(
    translations_path: Path | None,
    source_sha256: str,
    completed_ids: set[str],
) -> dict[str, dict[str, Any]] | None:
    if translations_path is None or not completed_ids:
        return None
    manifest_path = translations_path.parent / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SourceReviewError(
            f"Cannot read previous source manifest: {error}"
        ) from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("source_sha256") != source_sha256
    ):
        raise SourceReviewError(
            "Completed translations belong to a different source PDF."
        )
    units_name = str(
        (manifest.get("paths") or {}).get("translation_units") or ""
    )
    units_path = manifest_path.parent / units_name
    if not units_name or not units_path.is_file():
        raise SourceReviewError(
            "Previous reviewed source inventory is missing for completed translations."
        )
    records = _read_jsonl(units_path)
    previous = {str(record["id"]): record for record in records}
    if len(previous) != len(records):
        raise SourceReviewError(
            "Previous reviewed source inventory contains duplicate IDs."
        )
    return previous


def _completed_unit_contract(element: dict[str, Any]) -> dict[str, Any]:
    """Content/formatting contract; order and review explanations may change."""
    fields = (
        "source_text",
        "protected_text",
        "protected_tokens",
        "style_tokens",
        "span_runs",
        "kind",
        "render_mode",
        "page",
        "bbox",
        "section",
    )
    result = {field: element.get(field) for field in fields}
    result["translatable"] = bool(element.get("translatable"))
    result["render_suppressed"] = bool(element.get("render_suppressed"))
    result["block_style"] = element.get("block_style") or "normal"
    fragments = copy.deepcopy(element.get("inline_fragments") or {})
    for fragment in fragments.values():
        # Anchor linking runs after review replay. It is checked again by
        # reconciliation against the completed translation's final metadata.
        fragment.pop("footnote_id", None)
    result["inline_fragments"] = fragments
    return result


def _apply_source_review_operations(
    elements: list[dict[str, Any]],
    review: dict[str, Any],
    source_sha256: str,
    page_records: list[dict[str, Any]],
    protect_element: Callable[[dict[str, Any]], None],
    *,
    translations_path: Path | None = None,
) -> dict[str, Any]:
    """Apply validated operations to an isolated candidate inventory."""

    if review.get("source_sha256") != source_sha256:
        raise SourceReviewError("Source review hash does not match this PDF.")
    operations = list(review.get("operations") or [])
    if not operations:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_sha256": source_sha256,
            "reviewed": review.get("reviewed") is True,
            "status": "pass",
            "applied_operations": 0,
            "affected_ids": [],
            "errors": [],
            "warnings": [],
        }
    if review.get("reviewed") is not True:
        raise SourceReviewError("Source review must set reviewed=true.")

    page_sizes = {
        int(record["page"]): (float(record["width"]), float(record["height"]))
        for record in page_records
    }
    translated_ids = _nonempty_translation_ids(translations_path)
    affected: set[str] = set()
    inserted_ids: set[str] = set()
    replaced_ids: set[str] = set()
    suppressed_ids: set[str] = set()
    visual_member_ids: set[str] = set()
    visual_ids: set[str] = set()

    def index_elements() -> dict[str, dict[str, Any]]:
        return {str(element["id"]): element for element in elements}

    for index, operation in enumerate(operations, start=1):
        if not isinstance(operation, dict):
            raise SourceReviewError(f"Operation {index} must be a JSON object.")
        op = str(operation.get("op") or "")
        if op not in SUPPORTED_OPERATIONS:
            raise SourceReviewError(f"Operation {index} has unsupported op={op!r}.")
        reason = _validate_reason(operation, index)
        by_id = index_elements()

        if op == "suppress_units":
            ids = [str(value) for value in operation.get("ids") or []]
            if not ids or len(ids) != len(set(ids)):
                raise SourceReviewError(
                    f"Operation {index}: suppress_units requires unique ids."
                )
            unknown = set(ids) - set(by_id)
            if unknown:
                raise SourceReviewError(
                    f"Operation {index}: unknown IDs: {sorted(unknown)}"
                )
            overlap = set(ids) & suppressed_ids
            if overlap:
                raise SourceReviewError(
                    f"Operation {index}: units suppressed more than once: "
                    f"{sorted(overlap)}"
                )
            protected = set(ids) & translated_ids
            if protected:
                raise SourceReviewError(
                    "Refusing to suppress units with completed translations: "
                    f"{sorted(protected)}"
                )
            grouped_into = str(operation.get("grouped_into") or "review-suppressed")
            for element_id in ids:
                element = by_id[element_id]
                element["render_suppressed"] = True
                element["grouped_into"] = grouped_into
                element["extraction_artifact"] = reason
                element["translatable"] = False
                _clear_protection(element)
            suppressed_ids.update(ids)
            affected.update(ids)
            continue

        if op == "replace_unit":
            element_id = str(operation.get("id") or "")
            if element_id not in by_id:
                raise SourceReviewError(
                    f"Operation {index}: unknown replacement ID {element_id!r}."
                )
            if element_id in replaced_ids:
                raise SourceReviewError(
                    f"Operation {index}: {element_id} is replaced more than once."
                )
            element = by_id[element_id]
            updated = copy.deepcopy(element)
            allowed = {
                "source_text",
                "kind",
                "render_mode",
                "translatable",
                "page",
                "bbox",
                "span_runs",
                "section",
            }
            changed = False
            for field in allowed:
                if field in operation:
                    if updated.get(field) != operation[field]:
                        changed = True
                    updated[field] = copy.deepcopy(operation[field])
            kind = str(updated.get("kind") or "")
            if kind not in TEXT_KINDS | CLIP_KINDS:
                raise SourceReviewError(
                    f"Operation {index}: unsupported replacement kind {kind!r}."
                )
            if "kind" in operation and "render_mode" not in operation:
                updated["render_mode"] = _default_render_mode(kind)
            page = int(updated.get("page") or 0)
            updated["bbox"] = _validate_bbox(
                updated.get("bbox"),
                page,
                page_sizes,
                label=f"Operation {index}",
            )
            if "source_text" in operation and "span_runs" not in operation:
                updated["span_runs"] = []
            if changed and element_id in translated_ids:
                raise SourceReviewError(
                    f"Refusing to replace completed translation unit {element_id}."
                )
            updated["source_review_reason"] = reason
            _refresh_translation_fields(updated, protect_element)
            element.clear()
            element.update(updated)
            replaced_ids.add(element_id)
            affected.add(element_id)
            continue

        if op == "insert_unit":
            element_id = str(operation.get("id") or "")
            after_id = str(operation.get("after_id") or "")
            if not re.fullmatch(r"review-p\d{4}-u\d{4}", element_id):
                raise SourceReviewError(
                    f"Operation {index}: inserted IDs must match "
                    "review-pdddd-udddd."
                )
            if element_id in by_id or element_id in inserted_ids:
                raise SourceReviewError(
                    f"Operation {index}: duplicate inserted ID {element_id!r}."
                )
            if after_id not in by_id:
                raise SourceReviewError(
                    f"Operation {index}: unknown after_id {after_id!r}."
                )
            anchor = by_id[after_id]
            page = int(operation.get("page") or anchor.get("page") or 0)
            kind = str(operation.get("kind") or "body")
            if kind not in TEXT_KINDS | CLIP_KINDS:
                raise SourceReviewError(
                    f"Operation {index}: unsupported inserted kind {kind!r}."
                )
            source_text = str(operation.get("source_text") or "")
            if not source_text.strip() and kind not in {"figure", "table"}:
                raise SourceReviewError(
                    f"Operation {index}: inserted text cannot be empty."
                )
            bbox = _validate_bbox(
                operation.get("bbox"),
                page,
                page_sizes,
                label=f"Operation {index}",
            )
            inserted = {
                "id": element_id,
                "page": page,
                "bbox": bbox,
                "kind": kind,
                "render_mode": str(
                    operation.get("render_mode") or _default_render_mode(kind)
                ),
                "source_text": source_text,
                "font_size": float(
                    operation.get("font_size") or anchor.get("font_size") or 10.0
                ),
                "font_names": list(operation.get("font_names") or []),
                "span_runs": copy.deepcopy(operation.get("span_runs") or []),
                "confidence": float(operation.get("confidence") or 1.0),
                "section": str(operation.get("section") or anchor.get("section") or ""),
                "translatable": bool(operation.get("translatable", kind == "body")),
                "source_review_reason": reason,
                "source_ref": f"source-review:{element_id}",
            }
            _refresh_translation_fields(inserted, protect_element)
            anchor_index = elements.index(anchor)
            elements.insert(anchor_index + 1, inserted)
            inserted_ids.add(element_id)
            affected.add(element_id)
            continue

        visual_id = str(operation.get("visual_id") or "")
        anchor_id = str(operation.get("anchor_id") or "")
        caption_id = str(operation.get("caption_id") or "")
        member_ids = [str(value) for value in operation.get("member_ids") or []]
        if not visual_id or visual_id in visual_ids:
            raise SourceReviewError(
                f"Operation {index}: visual_id must be non-empty and unique."
            )
        if (
            not member_ids
            or len(member_ids) != len(set(member_ids))
            or anchor_id not in member_ids
        ):
            raise SourceReviewError(
                f"Operation {index}: member_ids must be unique and include anchor_id."
            )
        unknown = (set(member_ids) | {caption_id}) - set(by_id)
        if unknown:
            raise SourceReviewError(
                f"Operation {index}: unknown visual IDs: {sorted(unknown)}"
            )
        overlap = set(member_ids) & visual_member_ids
        if overlap:
            raise SourceReviewError(
                f"Operation {index}: visual members reused: {sorted(overlap)}"
            )
        anchor = by_id[anchor_id]
        members = [by_id[element_id] for element_id in member_ids]
        pages = {int(element["page"]) for element in members}
        if len(pages) != 1:
            raise SourceReviewError(
                f"Operation {index}: grouped visual members must share one page."
            )
        page = pages.pop()
        if "source_bbox" in operation:
            source_bbox = _validate_bbox(
                operation["source_bbox"],
                page,
                page_sizes,
                label=f"Operation {index}",
            )
        else:
            x0 = min(float(element["bbox"][0]) for element in members)
            y0 = min(float(element["bbox"][1]) for element in members)
            x1 = max(float(element["bbox"][2]) for element in members)
            y1 = max(float(element["bbox"][3]) for element in members)
            source_bbox = _validate_bbox(
                [x0, y0, x1, y1],
                page,
                page_sizes,
                label=f"Operation {index}",
            )
        protected = {
            element_id
            for element_id in member_ids
            if element_id != anchor_id and element_id in translated_ids
        }
        if anchor_id in translated_ids and anchor.get("translatable"):
            protected.add(anchor_id)
        if protected:
            raise SourceReviewError(
                "Refusing to group visual units with completed translations: "
                f"{sorted(protected)}"
            )
        kind = str(operation.get("kind") or "figure")
        if kind not in {"figure", "table"}:
            raise SourceReviewError(
                f"Operation {index}: grouped visual kind must be figure or table."
            )
        anchor.update(
            {
                "bbox": source_bbox,
                "kind": kind,
                "render_mode": "source_clip",
                "render_suppressed": False,
                "translatable": False,
                "review_visual_id": visual_id,
                "review_visual_source_ids": member_ids,
                "visual_id": visual_id,
                "source_review_reason": reason,
            }
        )
        _clear_protection(anchor)
        for member in members:
            member["visual_id"] = visual_id
            if member is anchor:
                continue
            member["render_suppressed"] = True
            member["grouped_into"] = visual_id
            member["extraction_artifact"] = reason
            member["translatable"] = False
            _clear_protection(member)
        caption = by_id[caption_id]
        if int(caption["page"]) != page:
            raise SourceReviewError(
                f"Operation {index}: caption and visual must share one page."
            )
        caption.update(
            {
                "kind": "caption",
                "render_mode": "text",
                "render_suppressed": False,
                "translatable": False,
                "visual_id": visual_id,
                "source_review_reason": reason,
            }
        )
        if "caption_text" in operation:
            if caption_id in translated_ids:
                raise SourceReviewError(
                    f"Refusing to replace completed caption {caption_id}."
                )
            caption["source_text"] = str(operation["caption_text"])
            caption["span_runs"] = copy.deepcopy(
                operation.get("caption_span_runs") or []
            )
        _clear_protection(caption)
        visual_ids.add(visual_id)
        visual_member_ids.update(member_ids)
        affected.update(member_ids)
        affected.add(caption_id)

    _renumber(elements)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "reviewed": True,
        "status": "pass",
        "applied_operations": len(operations),
        "affected_ids": sorted(affected),
        "errors": [],
        "warnings": [],
    }


def _formula_only_body(element: dict[str, Any]) -> bool:
    if (
        element.get("kind") != "body"
        or element.get("translatable") is not True
        or element.get("render_suppressed")
    ):
        return False
    text = re.sub(r"\s+", " ", str(element.get("source_text") or "")).strip()
    if len(text) < 8:
        return False
    prose_words = PROSE_WORD_RE.findall(text)
    runs = list(element.get("span_runs") or [])
    visible = sum(
        len(re.sub(r"\s+", "", str(run.get("text") or ""))) for run in runs
    )
    math_chars = sum(
        len(re.sub(r"\s+", "", str(run.get("text") or "")))
        for run in runs
        if run.get("style") == "math"
    )
    math_ratio = math_chars / max(1, visible)
    compact = re.sub(r"\s+", "", text)
    operator_count = len(MATH_SIGNAL_RE.findall(compact))
    return (
        math_ratio >= 0.6 and len(prose_words) < 2
    ) or (
        len(prose_words) == 0
        and operator_count >= 2
        and len(compact) >= 12
    )


def detect_source_issues(
    elements: list[dict[str, Any]],
    visual_layout: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Detect high-risk extraction shapes that require human source review."""

    errors = [
        (
            f"{element['id']}: formula-only content is classified as translatable "
            "body text; reclassify it as native display math or merge it with "
            "the surrounding prose through source-review.json."
        )
        for element in elements
        if _formula_only_body(element)
    ]
    warnings: list[str] = []
    by_page: dict[int, list[dict[str, Any]]] = {}
    for element in elements:
        by_page.setdefault(int(element["page"]), []).append(element)

    for visual in visual_layout.get("visuals") or []:
        page = int(visual.get("page") or 0)
        bbox = visual.get("source_bbox") or []
        if not isinstance(bbox, list) or len(bbox) != 4:
            continue
        x0, y0, x1, y1 = [float(value) for value in bbox]
        source_ids = {str(value) for value in visual.get("source_ids") or []}
        caption_id = str(visual.get("caption_id") or "")
        for element in by_page.get(page, []):
            if (
                element.get("render_suppressed")
                or str(element["id"]) in source_ids
                or str(element["id"]) == caption_id
                or element.get("kind") not in {"heading", "equation"}
            ):
                continue
            ex0, ey0, ex1, ey1 = [float(value) for value in element["bbox"]]
            center_x = (ex0 + ex1) / 2
            center_y = (ey0 + ey1) / 2
            if x0 <= center_x <= x1 and y0 <= center_y <= y1:
                errors.append(
                    f"{element['id']}: {element.get('kind')} lies inside "
                    f"{visual.get('visual_id')} and may be an axis or plot label."
                )

    for page_elements in by_page.values():
        ordered = sorted(page_elements, key=lambda item: int(item.get("page_order") or 0))
        for previous, current in pairwise(ordered):
            if (
                previous.get("kind") == "caption"
                and current.get("kind") == "body"
                and not current.get("render_suppressed")
                and len(str(current.get("source_text") or "").strip()) <= 180
                and re.match(
                    r"^[a-z(]",
                    str(current.get("source_text") or "").strip(),
                )
            ):
                warnings.append(
                    f"{current['id']}: short lowercase prose follows caption "
                    f"{previous['id']}; verify whether it is a caption continuation."
                )
    return errors, warnings


def complete_report(
    report: dict[str, Any],
    errors: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    completed = copy.deepcopy(report)
    completed["errors"] = [*completed.get("errors", []), *errors]
    completed["warnings"] = [*completed.get("warnings", []), *warnings]
    completed["status"] = "pass" if not completed["errors"] else "fail"
    return completed


def reconcile_translations(
    path: Path,
    elements: list[dict[str, Any]],
) -> None:
    """Rebuild translations.jsonl without discarding completed translations."""

    if not path.exists():
        return
    existing_records = _read_jsonl(path)
    existing = {str(record["id"]): record for record in existing_records}
    target = [
        element for element in elements if element.get("translatable") is True
    ]
    target_ids = {str(element["id"]) for element in target}
    completed_orphans = sorted(
        element_id
        for element_id, record in existing.items()
        if element_id not in target_ids
        and str(record.get("translated_text") or "").strip()
    )
    if completed_orphans:
        raise SourceReviewError(
            "Refusing to discard completed translations during reconciliation: "
            f"{completed_orphans}"
        )
    records: list[dict[str, Any]] = []
    for element in target:
        element_id = str(element["id"])
        old = existing.get(element_id, {})
        if str(old.get("translated_text") or "").strip():
            for field in (
                "source_text",
                "protected_text",
                "protected_tokens",
                "inline_fragments",
                "style_tokens",
            ):
                if field in old and old[field] != element.get(field):
                    raise SourceReviewError(
                        f"Refusing to change {field} for completed translation "
                        f"unit {element_id} during reconciliation."
                    )
        records.append(
            {
                "id": element_id,
                "section": element.get("section", ""),
                "source_text": element.get("source_text", ""),
                "protected_text": element.get("protected_text", ""),
                "protected_tokens": element.get("protected_tokens", {}),
                "inline_fragments": element.get("inline_fragments", {}),
                "style_tokens": element.get("style_tokens", {}),
                "translated_text": str(old.get("translated_text") or ""),
            }
        )
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
