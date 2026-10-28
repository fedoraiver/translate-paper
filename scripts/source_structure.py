"""Source-only flow evidence and span alignment shared by preparation/review."""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any

FLOW_FIELDS = ("flow_role", "list_id", "list_level", "source_marker", "flow_reviewed")
FLOW_ROLES = {"paragraph", "list_item", "list_continuation"}
MARKER_RE = re.compile(
    r"^\s*(?P<marker>[•·▪●◦‣⁃–—\-]|\d+[.)]|\([0-9A-Za-z]+\)|[A-Za-z][.)])"
    r"(?=\s|$)\s*(?P<content>.*)$"
)


def locate_source_spans(
    source_text: str, span_runs: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Align ordered source runs and expose lossy or nonunique alignments.

    Earliest and latest monotone alignments agree when the source evidence
    locates a run unambiguously. Whitespace normalization retains exact offsets.
    """
    parts = list(re.finditer(r"\s+|\S", source_text))
    searchable = "".join(" " if p.group().isspace() else p.group() for p in parts)
    located: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    cursor = 0
    for index, run in enumerate(span_runs):
        needle = re.sub(r"\s+", " ", str(run.get("text") or "")).strip()
        if not needle:
            continue
        start = searchable.find(needle, cursor)
        if start < 0:
            issues.append(
                {
                    "code": "unmatched_source_span",
                    "span_index": index,
                    "message": "Source span cannot be located in the extracted text.",
                }
            )
            continue
        end = start + len(needle)
        item = dict(run)
        item.update(
            {
                "start": parts[start].start(),
                "end": parts[end - 1].end(),
                "text": source_text[parts[start].start() : parts[end - 1].end()],
                "_source_run_index": index,
                "_normalized_start": start,
                "_needle": needle,
            }
        )
        located.append(item)
        cursor = end
    cursor = len(searchable)
    for run in reversed(located):
        latest = searchable.rfind(run["_needle"], 0, cursor)
        if latest != run["_normalized_start"]:
            issues.append(
                {
                    "code": "ambiguous_source_span",
                    "span_index": run["_source_run_index"],
                    "source_start": run["start"],
                    "source_end": run["end"],
                    "message": "Source span has more than one ordered text location.",
                }
            )
        if latest >= 0:
            cursor = latest
    for run in located:
        del run["_needle"], run["_normalized_start"]
    return located, sorted(issues, key=lambda issue: issue["span_index"])


def source_lines(element: dict[str, Any]) -> list[dict[str, Any]]:
    """Recover visible source lines without consulting translated prose."""
    groups: OrderedDict[int, list[dict[str, Any]]] = OrderedDict()
    for run in element.get("span_runs") or []:
        groups.setdefault(int(run.get("line") or 0), []).append(run)
    if not groups:
        return [
            {"text": line, "runs": [], "bbox": None}
            for line in str(element.get("source_text") or "").splitlines()
        ]
    result: list[dict[str, Any]] = []
    for runs in groups.values():
        pieces: list[str] = []
        for run in runs:
            value = str(run.get("text") or "")
            marker_only = MARKER_RE.match(pieces[0]) if len(pieces) == 1 else None
            if marker_only and not marker_only["content"].strip():
                if not pieces[-1].endswith(" ") and not value.startswith(" "):
                    pieces.append(" ")
            pieces.append(value)
        text = "".join(pieces)
        boxes = [run["bbox"] for run in runs if len(run.get("bbox") or []) == 4]
        box = (
            [
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            ]
            if boxes
            else None
        )
        result.append({"text": text, "runs": runs, "bbox": box})
    return result


def source_markers(element: dict[str, Any]) -> list[dict[str, Any]]:
    lines = source_lines(element)
    markers: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        match = MARKER_RE.match(line["text"])
        if not match:
            continue
        content = match["content"].strip()
        if not content and index + 1 < len(lines):
            content = lines[index + 1]["text"].strip()
        # Bare equation labels and leading numeric calculations are not lists.
        if not re.match(r"[A-Za-z\u3400-\u9fff]", content):
            continue
        first = next(
            (run for run in line["runs"] if str(run.get("text") or "").strip()), {}
        )
        if first.get("semantic_kind") == "math" or first.get("style") == "math":
            continue
        content_x = None
        if line["bbox"]:
            following = next(
                (run for run in line["runs"][1:] if str(run.get("text") or "").strip()),
                None,
            )
            if following and len(following.get("bbox") or []) == 4:
                content_x = float(following["bbox"][0])
            elif not match["content"].strip() and index + 1 < len(lines):
                next_box = lines[index + 1]["bbox"]
                content_x = float(next_box[0]) if next_box else None
            elif match["content"].strip():
                content_x = float(line["bbox"][0]) + (
                    len(match["marker"]) * 0.5 + 0.3
                ) * float(first.get("size") or 10)
        markers.append(
            {
                "marker": match["marker"],
                "line": index,
                "marker_x": float(line["bbox"][0]) if line["bbox"] else None,
                "content_x": content_x,
            }
        )
        for embedded in re.finditer(
            r"\s(?P<marker>[•▪●◦‣⁃]|\d+[.)])\s+(?=[A-Za-z\u3400-\u9fff])",
            match["content"],
        ):
            markers.append(
                {
                    "marker": embedded["marker"],
                    "line": index,
                    "marker_x": None,
                    "content_x": None,
                }
            )
    return markers


def annotate_source_flow(elements: list[dict[str, Any]]) -> None:
    """Assign conservative list roles using source markers and hanging indents.

    A block containing multiple list starts is deliberately left review-required;
    translating that block as one paragraph would lose its source structure.
    """
    active: dict[str, Any] | None = None
    for element in sorted(elements, key=lambda item: int(item.get("order") or 0)):
        element.update(flow_metadata({}))
        if element.get("kind") != "body" or element.get("render_suppressed"):
            active = None
            continue
        element.pop("flow_review_required", None)
        markers = source_markers(element)
        if len(markers) > 1 or (markers and markers[0]["line"] > 0):
            element["flow_review_required"] = (
                "A source unit contains multiple list items or paragraph/list boundaries; "
                "split it with source review and assign explicit flow metadata."
            )
            active = None
            continue
        box = element.get("bbox") or [0, 0, 0, 0]
        page = int(element.get("page") or 0)
        size = float(element.get("font_size") or 10.0)
        if markers:
            marker = markers[0]
            marker_x = marker["marker_x"]
            x = float(marker_x if marker_x is not None else box[0])
            near_list = (
                active is not None
                and active["page"] == page
                and 0 <= float(box[1]) - active["bottom"] <= size * 3
            )
            indents = list(active["indents"]) if near_list else []
            level = next(
                (
                    index
                    for index, indent in enumerate(indents)
                    if abs(x - indent) <= size * 0.6
                ),
                None,
            )
            if level is None and indents and x >= indents[-1] + size * 0.9:
                level = len(indents)
                indents.append(x)
            if level is None:
                level, indents = 0, [x]
                list_id = f"list-{element['id']}"
            else:
                list_id = active["list_id"]
                indents = indents[: level + 1]
            element.update(
                {
                    "flow_role": "list_item",
                    "list_id": list_id,
                    "source_marker": marker["marker"],
                    "list_level": level,
                }
            )
            active = {
                "page": page,
                "bottom": float(box[3]),
                "marker_x": x,
                "content_x": marker["content_x"],
                "list_id": list_id,
                "indents": indents,
                "list_level": level,
            }
        elif (
            active is not None
            and active["page"] == page
            and active["content_x"] is not None
            and active["content_x"] > active["marker_x"] + size * 0.3
            and abs(float(box[0]) - active["content_x"]) <= size * 0.45
            and 0 <= float(box[1]) - active["bottom"] <= size * 1.6
        ):
            element.update(
                {
                    "flow_role": "list_continuation",
                    "list_id": active["list_id"],
                    "list_level": active["list_level"],
                }
            )
            active["bottom"] = float(box[3])
        elif (
            active is not None
            and active["page"] != page
            and active["content_x"] is not None
            and abs(float(box[0]) - active["content_x"]) <= size * 0.45
        ):
            element["flow_review_required"] = (
                "Possible list continuation across a source page boundary."
            )
            active = None
        else:
            active = None


def validate_flow(element: dict[str, Any]) -> str | None:
    if not isinstance(element.get("flow_reviewed", False), bool):
        return "flow_reviewed must be a boolean."
    role = element.get("flow_role", "paragraph")
    if role not in FLOW_ROLES:
        return f"Unsupported flow_role {role!r}."
    level = element.get("list_level", 0)
    if isinstance(level, bool) or not isinstance(level, int) or level < 0:
        return "list_level must be a nonnegative integer."
    list_id = element.get("list_id")
    marker = element.get("source_marker", "")
    if not isinstance(marker, str):
        return "source_marker must be source text."
    if role == "paragraph":
        if list_id is not None or level != 0 or marker:
            return "Paragraph flow must have list_id=null, list_level=0 and an empty source_marker."
    elif not isinstance(list_id, str) or not list_id.strip():
        return "List flow requires a nonempty list_id."
    elif role == "list_item" and not marker.strip():
        return "List items require their source_marker."
    elif role == "list_item" and not str(
        element.get("source_text") or ""
    ).lstrip().startswith(marker):
        return "source_marker must match the beginning of the source list item."
    elif role == "list_continuation" and marker:
        return "List continuations must have an empty source_marker."
    return None


def flow_metadata(element: dict[str, Any]) -> dict[str, Any]:
    return {
        "flow_role": element.get("flow_role", "paragraph"),
        "list_id": element.get("list_id"),
        "list_level": element.get("list_level", 0),
        "source_marker": element.get("source_marker", ""),
        "flow_reviewed": element.get("flow_reviewed", False),
    }
