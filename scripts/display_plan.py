"""Exact source ownership for reviewed display mathematics.

Display boundaries are review decisions. Adjacency, suppressed labels and clip
geometry never silently combine independently reviewed rules.
"""

from __future__ import annotations

from collections import Counter
from typing import Any


class DisplayPlanError(ValueError):
    """The display review cannot account for the active source exactly once."""


def select_body_units(elements: list[dict[str, Any]], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Select the same reviewed body range without requiring page geometry."""
    ordered = sorted(elements, key=lambda item: int(item["order"]))
    positions = {str(item["id"]): index for index, item in enumerate(ordered)}
    boundary = dict(manifest.get("boundary") or {})
    start = positions.get(str(boundary.get("introduction_id") or ""), 0)
    stop = positions.get(str(boundary.get("post_body_stop_id") or ""), len(ordered))
    included = {str(value) for value in boundary.get("include_ids") or []}
    indices = [index for index, item in enumerate(ordered)
               if (item.get("translatable") or str(item["id"]) in included)
               and not item.get("render_suppressed")]
    if indices:
        start, stop = min(start, min(indices)), max(stop, max(indices) + 1)
    if ordered and stop <= start:
        raise DisplayPlanError("Translation stop boundary precedes the start boundary.")
    return ordered[start:stop]


def active_display_units(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in sorted(elements, key=lambda item: int(item["order"]))
            if item.get("kind") == "equation" and not item.get("render_suppressed")]


def _members(
    review_id: str, entry: dict[str, Any], by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    source_ids = entry.get("source_ids")
    if not isinstance(source_ids, list) or not source_ids:
        raise DisplayPlanError(
            f"{review_id}: display review requires source_ids; rerun preparation and review mathematics."
        )
    if any(not isinstance(value, str) or not value for value in source_ids):
        raise DisplayPlanError(f"{review_id}: display source_ids must contain nonempty strings.")
    if len(set(source_ids)) != len(source_ids):
        raise DisplayPlanError(f"{review_id}: duplicate display source_ids.")
    missing = [value for value in source_ids if value not in by_id]
    if missing:
        raise DisplayPlanError(f"{review_id}: display source_ids are missing from the active body: {missing}.")
    members = [by_id[value] for value in source_ids]
    if source_ids != [str(item["id"]) for item in sorted(members, key=lambda item: int(item["order"]))]:
        raise DisplayPlanError(f"{review_id}: display source_ids disagree with source reading order.")
    active = [item for item in members if item.get("kind") == "equation" and not item.get("render_suppressed")]
    if not active or review_id != str(active[0]["id"]):
        raise DisplayPlanError(f"{review_id}: display owner must be its first active equation source ID.")
    if any(item.get("kind") != "equation" and not item.get("render_suppressed") for item in members):
        raise DisplayPlanError(f"{review_id}: display ownership includes active non-equation source text.")
    if len({int(item.get("page") or 0) for item in members}) != 1:
        raise DisplayPlanError(f"{review_id}: display source_ids cross source pages.")
    member_ids = set(source_ids)
    first_order, last_order = int(members[0]["order"]), int(members[-1]["order"])
    if any(first_order < int(item["order"]) < last_order
           and str(item["id"]) not in member_ids and not item.get("render_suppressed")
           for item in by_id.values()):
        raise DisplayPlanError(f"{review_id}: display grouping crosses another active source unit.")
    return members


def _source_metadata(members: list[dict[str, Any]]) -> dict[str, Any]:
    boxes = [list(item.get("bbox") or []) for item in members]
    bbox = ([min(box[0] for box in boxes), min(box[1] for box in boxes),
             max(box[2] for box in boxes), max(box[3] for box in boxes)]
            if all(len(box) == 4 for box in boxes) else None)
    return {
        "source_page": int(members[0].get("page") or 0),
        "source_text": " ".join(str(item.get("source_text") or "").strip()
                                for item in members if str(item.get("source_text") or "").strip()),
        "source_bbox": bbox,
    }


def build_display_plan(
    elements: list[dict[str, Any]], reviews: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate and order every display, including explicitly owned labels."""
    by_id = {str(item["id"]): item for item in elements}
    if len(by_id) != len(elements):
        raise DisplayPlanError("Duplicate source unit IDs in display plan.")
    expected = {str(item["id"]) for item in active_display_units(elements)}
    owned: dict[str, str] = {}
    plan: list[dict[str, Any]] = []
    for review_id, entry in reviews.items():
        if not isinstance(entry, dict):
            raise DisplayPlanError(f"{review_id}: math review entry must be an object.")
        if entry.get("kind") != "display" and not entry.get("display"):
            continue
        members = _members(str(review_id), entry, by_id)
        for item in members:
            unit_id = str(item["id"])
            if unit_id in owned:
                raise DisplayPlanError(f"{unit_id}: overlapping display ownership by {owned[unit_id]} and {review_id}.")
            owned[unit_id] = str(review_id)
        metadata = _source_metadata(members)
        for key in ("source_page", "source_text"):
            if key in entry and entry[key] != metadata[key]:
                raise DisplayPlanError(f"{review_id}: display {key} mismatch with owned source units.")
        if entry.get("source_bbox") is not None and metadata["source_bbox"] is not None:
            bbox = entry["source_bbox"]
            if not isinstance(bbox, list) or len(bbox) != 4 or any(
                abs(float(left) - float(right)) > 0.002
                for left, right in zip(bbox, metadata["source_bbox"])
            ):
                raise DisplayPlanError(f"{review_id}: display source_bbox mismatch with owned source units.")
        plan.append({"review_id": str(review_id), "source_ids": [str(item["id"]) for item in members],
                     "elements": members})
    missing = sorted(expected - set(owned))
    if missing:
        raise DisplayPlanError("Display equation requires manual review with exact source_ids; missing ownership: "
                               + ", ".join(missing))
    return sorted(plan, key=lambda group: int(by_id[group["review_id"]]["order"]))


def default_display_groups(
    elements: list[dict[str, Any]], previous_entries: dict[str, dict[str, Any]] | None = None,
    *, invalidations: list[dict[str, Any]] | None = None,
) -> list[list[dict[str, Any]]]:
    """Preserve explicit prior membership; new displays begin as singletons."""
    by_id = {str(item["id"]): item for item in elements}
    groups: list[list[dict[str, Any]]] = []
    owned: set[str] = set()
    for review_id, entry in (previous_entries or {}).items():
        if entry.get("kind") != "display" and not entry.get("display"):
            continue
        source_ids = entry.get("source_ids")
        if source_ids and (not isinstance(source_ids, list)
                           or any(not isinstance(value, str) or not value for value in source_ids)):
            raise DisplayPlanError(f"{review_id}: display source_ids must be a list of nonempty strings.")
        if source_ids and len(set(source_ids)) != len(source_ids):
            raise DisplayPlanError(f"{review_id}: duplicate prior display source_ids.")
        try:
            members = _members(str(review_id), entry, by_id)
        except DisplayPlanError as error:
            # A source repair can remove, suppress, split or move an old owner.
            # Its old TeX must not prevent deriving the replacement review gate.
            if invalidations is not None:
                invalidations.append({"review_id": str(review_id), "source_ids": source_ids or [],
                                      "reason": str(error)})
            continue
        ids = {str(item["id"]) for item in members}
        if owned & ids:
            raise DisplayPlanError(f"{review_id}: overlapping prior display ownership.")
        owned.update(ids)
        groups.append(members)
    groups.extend([item] for item in active_display_units(elements) if str(item["id"]) not in owned)
    return sorted(groups, key=lambda group: min(int(item["order"]) for item in group))


def validate_display_consumption(plan: list[dict[str, Any]], placements: list[dict[str, Any]]) -> None:
    """Check emitted native displays against every review and source owner."""
    expected = {group["review_id"]: list(group["source_ids"]) for group in plan}
    consumed = [placement for placement in placements if placement.get("render_mode") == "native-math"]
    counts = Counter(str(item.get("math_review_id") or "") for item in consumed)
    if counts != Counter({review_id: 1 for review_id in expected}):
        raise DisplayPlanError(f"Display review consumption mismatch: expected {sorted(expected)}, consumed {dict(counts)}.")
    for placement in consumed:
        review_id = str(placement["math_review_id"])
        if list(placement.get("ids") or []) != expected[review_id]:
            raise DisplayPlanError(f"{review_id}: display placement source_ids mismatch.")
