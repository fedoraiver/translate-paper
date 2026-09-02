#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pymupdf>=1.26.0,<2",
#   "rapidocr>=3.6.0,<4",
#   "onnxruntime>=1.18.0,<2",
# ]
# ///
"""Prepare a paper PDF for translation by the active Codex model.

This script extracts and protects source text, detects the strict
Introduction-through-Conclusion boundary, records source regions that should be
preserved, and renders source previews. It performs no translation.
"""

from __future__ import annotations

import argparse
import bisect
import glob
import hashlib
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import pymupdf
from typography import ZH_ACADEMIC_V1


INTRO_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*[.)]?\s+)?(?:introduction|引言)\b",
    re.IGNORECASE,
)
CONCLUSION_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*[.)]?\s+)?"
    r"(?:conclusions?|concluding remarks?|discussion and conclusions?|结论)\b",
    re.IGNORECASE,
)
STOP_RE = re.compile(
    r"^\s*(?:[A-Z]|\d+(?:\.\d+)*)?[.)]?\s*"
    r"(?:acknowledg(?:e)?ments?|funding|conflicts? of interest|"
    r"author contributions?|references?|bibliography|appendix|appendices|"
    r"supplement(?:ary material)?|致谢|参考文献|附录)\b",
    re.IGNORECASE,
)
CAPTION_RE = re.compile(
    r"^\s*(?:figure|fig\.?|table|algorithm|listing|图|表|算法)"
    r"\s*[A-Z]?\d+(?:\.\d+)*",
    re.IGNORECASE,
)
CAPTION_NARRATIVE_RE = re.compile(
    r"^\s*(?:table|figure|fig\.?)\s*[A-Z]?\d+\s+"
    r"(?:gives?|shows?|compares?|summari[sz]es?|reports?|presents?|"
    r"provides?|lists?|illustrates?|describes?)\b",
    re.IGNORECASE,
)
ABSTRACT_RE = re.compile(r"^\s*(?:abstract|摘要)\b", re.IGNORECASE)
KEYWORDS_RE = re.compile(
    r"^\s*(?:keywords?|index terms?|jel classification|关键词)\b",
    re.IGNORECASE,
)
HEADING_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*[.)]?\s+|[A-Z][.)]\s+)[^\n]{2,120}$"
)
MATH_CHARS = set(
    "=<>≤≥≈≠∑∏∫√∞λμµσβαγδθφψω±×÷∂∇"
    "+−-*/^·∈∉←→↔⊥∗◦"
)
CITATION_RE = re.compile(
    r"\[(?:\d{1,4}(?:\s*[-–,]\s*\d{1,4})*)\]"
)
RICH_TOKEN_RE = re.compile(
    r"\[\[(?:[BIE][A-Za-z0-9_]*_(?:OPEN|CLOSE)|"
    r"F[A-Za-z0-9_]+|BR|PAR)\]\]"
)
MATH_FONT_RE = re.compile(
    r"(?:^|[-+])(CMMI|CMSY|CMEX|MSAM|MSBM)|MATH|SYMBOL",
    re.IGNORECASE,
)
LIST_MARKER_RE = re.compile(r"^[•·▪●◦‣⁃–—\-]$")
BOLD_FONT_RE = re.compile(r"(?:CMBX|BOLD|SEMIBOLD|DEMIBOLD)", re.IGNORECASE)
ITALIC_FONT_RE = re.compile(r"(?:CMTI|ITALIC|OBLIQUE|SLANTED)", re.IGNORECASE)
PROTECTED_RE = re.compile(
    r"https?://[^\s)\]}>,;]+"
    r"|(?:doi\s*:\s*)?10\.\d{4,9}/[^\s)\]}>,;]+"
    r"|(?:arXiv\s*:?\s*)?\d{4}\.\d{4,5}(?:v\d+)?"
    r"|\[[^\[\]\n]{1,180}\]"
    r"|\((?=[^()\n]{0,180}(?:19|20)\d{2})[^()\n]{1,180}\)"
    r"|\$[^$\n]+\$"
    r"|(?<![\w.])[-+]?\d+(?:[.,]\d+)*(?:\s*(?:%|ms|s|min|h|Hz|"
    r"kHz|MHz|GHz|B|KB|MB|GB|TB|m|cm|mm|km|kg|g|mg|USD|USDC|"
    r"bps|bp|dB|°C|K))?(?![A-Za-z])"
)
VISUAL_LABEL_RE = re.compile(
    r"(?:(?:\b(?P<latin_kind>figure|fig\.?|table))|"
    r"(?P<cjk_kind>图|表))\s*"
    r"(?P<number>[A-Z]?\d+(?:\.\d+)*)",
    re.IGNORECASE,
)
FOOTNOTE_PREFIX_RE = re.compile(r"^\s*(?P<number>\d{1,2}|[*⋆†‡])(?=\s|\D)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract ordered translation units and visual-preservation regions "
            "from a paper PDF. This script never translates text."
        )
    )
    parser.add_argument("--input", required=True, type=Path, help="Source PDF")
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="Intermediate directory (default: ./tmp/pdfs/<paper-slug>)",
    )
    parser.add_argument(
        "--ocr",
        choices=("auto", "force", "off"),
        default="auto",
        help="Use RapidOCR for scan-like pages",
    )
    parser.add_argument(
        "--render-dpi",
        type=int,
        default=120,
        help="DPI for source-page previews",
    )
    parser.add_argument(
        "--ocr-dpi",
        type=int,
        default=220,
        help="DPI for OCR input pages",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=0,
        help="Process only the first N pages for diagnostics; 0 means all pages",
    )
    parser.add_argument(
        "--boundary-review",
        type=Path,
        help=(
            "Reviewed boundary JSON. Defaults to <work-dir>/boundary-review.json; "
            "a reviewed file is applied automatically on reruns."
        ),
    )
    return parser.parse_args()


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug[:80] or "paper"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_dump(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def rect_list(rect: pymupdf.Rect) -> list[float]:
    return [round(float(value), 3) for value in (rect.x0, rect.y0, rect.x1, rect.y1)]


def intersection_ratio(a: pymupdf.Rect, b: pymupdf.Rect) -> float:
    intersection = a & b
    if intersection.is_empty or a.get_area() <= 0:
        return 0.0
    return intersection.get_area() / a.get_area()


def page_drawing_rects(page: pymupdf.Page) -> list[pymupdf.Rect]:
    rects: list[pymupdf.Rect] = []
    for drawing in page.get_drawings():
        rect = pymupdf.Rect(drawing.get("rect") or [])
        if not rect.is_empty and rect.width > 0 and rect.height >= 0:
            rects.append(rect & page.rect)
    return [rect for rect in rects if not rect.is_empty]


def mark_page_footnotes(
    elements: list[dict[str, Any]],
    drawing_rects: list[pymupdf.Rect],
    page_rect: pymupdf.Rect,
    body_size: float,
) -> None:
    separators = [
        rect
        for rect in drawing_rects
        if rect.y0 >= page_rect.height * 0.52
        and rect.height <= 2.0
        and 18.0 <= rect.width <= page_rect.width * 0.65
    ]
    separator_y = min((rect.y0 for rect in separators), default=None)
    for element in elements:
        if element.get("kind") not in {"body", "footnote"}:
            continue
        rect = pymupdf.Rect(element["bbox"])
        text = str(element.get("source_text") or "")
        below_rule = separator_y is not None and rect.y0 >= separator_y - 1.0
        marker_like = bool(FOOTNOTE_PREFIX_RE.match(text))
        at_page_foot = rect.y0 >= page_rect.height * 0.70
        small_enough = float(element.get("font_size") or body_size) <= body_size * 1.05
        if small_enough and (below_rule or (at_page_foot and marker_like)):
            element["kind"] = "footnote"
            element["render_mode"] = "text"
            if separator_y is not None:
                element["source_footnote_separator_y"] = round(
                    float(separator_y), 3
                )


def add_vector_figure_candidates(
    elements: list[dict[str, Any]],
    drawing_rects: list[pymupdf.Rect],
    page_rect: pymupdf.Rect,
    page_number: int,
    body_size: float,
) -> None:
    """Recover vector-only figures that expose no raster image block."""
    captions = [
        element
        for element in elements
        if element.get("kind") == "caption"
        and re.match(
            r"^\s*(?:(?:figure|fig\.?)\b|图)",
            str(element.get("source_text") or ""),
            re.IGNORECASE,
        )
    ]
    existing = [
        pymupdf.Rect(element["bbox"])
        for element in elements
        if element.get("kind") == "figure"
    ]
    components = [
        rect
        for rect in drawing_rects
        if rect.width >= 4.0
        and rect.height >= 4.0
        and rect.get_area() >= 20.0
    ]
    for caption_index, caption in enumerate(captions, start=1):
        caption_rect = pymupdf.Rect(caption["bbox"])
        if any(
            candidate.y0 <= caption_rect.y0
            and caption_rect.y0 - candidate.y1 <= page_rect.height * 0.12
            for candidate in existing
        ):
            continue
        window_top = max(page_rect.y0, caption_rect.y0 - page_rect.height * 0.48)
        candidates = [
            rect
            for rect in components
            if rect.y0 >= window_top
            and rect.y1 <= caption_rect.y0 + 2.0
            and rect.x1 >= page_rect.x0 + page_rect.width * 0.04
            and rect.x0 <= page_rect.x1 - page_rect.width * 0.04
        ]
        if not candidates:
            continue
        visual = pymupdf.Rect(candidates[0])
        for rect in candidates[1:]:
            visual |= rect
        label_window = pymupdf.Rect(
            max(page_rect.x0, visual.x0 - body_size * 2.0),
            max(window_top, visual.y0 - body_size * 2.0),
            min(page_rect.x1, visual.x1 + body_size * 2.0),
            min(caption_rect.y0, visual.y1 + body_size * 2.0),
        )
        internal_labels = [
            element
            for element in elements
            if element is not caption
            and element.get("kind") in {"body", "figure-text"}
            and float(element.get("font_size") or body_size)
            <= body_size * 1.05
            and pymupdf.Point(
                (
                    pymupdf.Rect(element["bbox"]).x0
                    + pymupdf.Rect(element["bbox"]).x1
                )
                / 2,
                (
                    pymupdf.Rect(element["bbox"]).y0
                    + pymupdf.Rect(element["bbox"]).y1
                )
                / 2,
            )
            in label_window
        ]
        for label in internal_labels:
            visual |= pymupdf.Rect(label["bbox"])
            label["kind"] = "figure-text"
            label["render_mode"] = "source_clip"
        if visual.get_area() < page_rect.get_area() * 0.003:
            continue
        elements.append(
            {
                "page": page_number,
                "bbox": rect_list(visual),
                "kind": "figure",
                "render_mode": "source_clip",
                "source_text": "",
                "font_size": body_size,
                "font_names": [],
                "confidence": 1.0,
                "source_ref": (
                    f"vector-figure-{page_number}-{caption_index}"
                ),
            }
        )
        existing.append(visual)


def normalized_heading(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def clean_lines(lines: list[str]) -> str:
    text = "\n".join(line.strip() for line in lines if line.strip())
    text = re.sub(r"(?<=[A-Za-z])-\n(?=[a-z])", "", text)
    text = re.sub(r"(?<![.!?:;])\n(?=\S)", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def span_style(span: dict[str, Any]) -> str:
    font = str(span.get("font") or "")
    flags = int(span.get("flags") or 0)
    if MATH_FONT_RE.search(font):
        return "math"
    if flags & 16 or BOLD_FONT_RE.search(font):
        return "bold"
    if flags & 2 or ITALIC_FONT_RE.search(font):
        return "italic"
    return "text"


def block_text(
    block: dict[str, Any],
) -> tuple[str, list[float], list[str], list[dict[str, Any]]]:
    lines: list[str] = []
    sizes: list[float] = []
    fonts: list[str] = []
    span_runs: list[dict[str, Any]] = []
    for line_index, line in enumerate(block.get("lines", [])):
        line_text = "".join(span.get("text", "") for span in line.get("spans", []))
        lines.append(line_text)
        for span_index, span in enumerate(line.get("spans", [])):
            size = span.get("size")
            if isinstance(size, (int, float)):
                sizes.append(float(size))
            font = span.get("font")
            if font:
                fonts.append(str(font))
            origin = list(span.get("origin") or [0.0, 0.0])
            bbox = list(span.get("bbox") or [0.0, 0.0, 0.0, 0.0])
            span_runs.append(
                {
                    "text": str(span.get("text") or ""),
                    "font": str(font or ""),
                    "size": round(float(size or 0.0), 3),
                    "flags": int(span.get("flags") or 0),
                    "style": span_style(span),
                    "origin": [round(float(value), 3) for value in origin[:2]],
                    "bbox": [round(float(value), 3) for value in bbox[:4]],
                    "line": line_index,
                    "span": span_index,
                }
            )
    return clean_lines(lines), sizes, fonts, span_runs


def _line_baseline(line: dict[str, Any], *, equation_fragment: bool) -> float:
    spans = [
        span
        for span in line.get("spans", [])
        if str(span.get("text") or "").strip() and span.get("origin")
    ]
    if not spans:
        bbox = line.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        return float(bbox[3])
    if equation_fragment:
        baseline = max(float(span["origin"][1]) for span in spans)
        text = "".join(str(span.get("text") or "") for span in spans).strip()
        if text == "√":
            baseline += max(float(span.get("size") or 0.0) for span in spans) * 0.85
        return baseline
    maximum_size = max(float(span.get("size") or 0.0) for span in spans)
    dominant = [
        float(span["origin"][1])
        for span in spans
        if float(span.get("size") or 0.0) >= maximum_size * 0.8
    ]
    return statistics.median(dominant)


def _merge_raw_lines(
    blocks: list[dict[str, Any]],
    block_kinds: list[str],
) -> list[dict[str, Any]]:
    """Reassemble inline-math fragments without collapsing adjacent visual lines."""
    lines: list[dict[str, Any]] = []
    for block_index, block in enumerate(blocks):
        block_kind = block_kinds[block_index]
        for line in block.get("lines", []):
            if not line.get("spans"):
                continue
            record = dict(line)
            record["_block_kind"] = block_kind
            record["_baseline"] = _line_baseline(
                record,
                equation_fragment=block_kind in {"equation", "footnote"},
            )
            lines.append(record)
    if not lines:
        return []

    parents = list(range(len(lines)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    body_lines = [
        index for index, line in enumerate(lines) if line["_block_kind"] == "body"
    ]
    for position, left in enumerate(body_lines):
        left_line = lines[left]
        left_sizes = [
            float(span.get("size") or 0.0)
            for span in left_line.get("spans", [])
            if float(span.get("size") or 0.0) > 0
        ]
        tolerance = max(1.0, (max(left_sizes) if left_sizes else 10.0) * 0.18)
        for right in body_lines[position + 1 :]:
            if abs(
                float(left_line["_baseline"]) - float(lines[right]["_baseline"])
            ) <= tolerance:
                union(left, right)
    for index, line in enumerate(lines):
        if line["_block_kind"] not in {"equation", "footnote"}:
            continue
        rect = pymupdf.Rect(line.get("bbox") or [])
        font_sizes = [
            float(span.get("size") or 0.0)
            for span in line.get("spans", [])
            if float(span.get("size") or 0.0) > 0
        ]
        tolerance = max(2.0, (max(font_sizes) if font_sizes else 10.0) * 0.55)
        candidates: list[tuple[float, int]] = []
        for body_index in body_lines:
            body_line = lines[body_index]
            body_rect = pymupdf.Rect(body_line.get("bbox") or [])
            horizontal_overlap = min(rect.x1, body_rect.x1) - max(rect.x0, body_rect.x0)
            if horizontal_overlap <= 0:
                continue
            distance = abs(float(line["_baseline"]) - float(body_line["_baseline"]))
            if distance <= tolerance:
                candidates.append((distance, body_index))
        if candidates:
            _, body_index = min(candidates)
            union(index, body_index)

    grouped: dict[int, list[dict[str, Any]]] = {}
    for index, line in enumerate(lines):
        grouped.setdefault(find(index), []).append(line)

    merged: list[dict[str, Any]] = []
    for group in grouped.values():
        group.sort(key=lambda line: float((line.get("bbox") or [0.0])[0]))
        union_rect = pymupdf.Rect(group[0].get("bbox") or [])
        spans: list[dict[str, Any]] = []
        for line in group:
            union_rect |= pymupdf.Rect(line.get("bbox") or [])
            spans.extend(line.get("spans") or [])
        spans.sort(key=lambda span: float((span.get("bbox") or [0.0])[0]))
        merged.append(
            {
                "bbox": rect_list(union_rect),
                "wmode": group[0].get("wmode", 0),
                "dir": group[0].get("dir", (1.0, 0.0)),
                "spans": spans,
                "_baseline": statistics.median(
                    float(line["_baseline"]) for line in group
                ),
            }
        )
    return sorted(
        merged,
        key=lambda line: (
            float(line.get("_baseline") or 0.0),
            float((line.get("bbox") or [0.0])[0]),
        ),
    )


def merge_fragmented_text_blocks(
    parsed: list[
        tuple[
            dict[str, Any],
            str,
            list[float],
            list[str],
            list[dict[str, Any]],
        ]
    ],
    page_rect: pymupdf.Rect,
    body_size: float,
    page_number: int,
) -> list[
    tuple[
        dict[str, Any],
        str,
        list[float],
        list[str],
        list[dict[str, Any]],
    ]
]:
    """Merge body text whose inline mathematics was emitted as overlapping blocks."""
    if len(parsed) < 2:
        return parsed

    kinds = [
        classify_text_kind(
            text,
            pymupdf.Rect(block.get("bbox") or []),
            page_rect,
            statistics.median(sizes) if sizes else body_size,
            body_size,
            page_number,
        )
        for block, text, sizes, _, _ in parsed
    ]
    parents = list(range(len(parsed)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    eligible = {"body", "equation", "footnote"}
    for left in range(len(parsed)):
        if kinds[left] not in eligible:
            continue
        left_rect = pymupdf.Rect(parsed[left][0].get("bbox") or [])
        for right in range(left + 1, len(parsed)):
            if kinds[right] not in eligible:
                continue
            right_rect = pymupdf.Rect(parsed[right][0].get("bbox") or [])
            overlap = left_rect & right_rect
            if not overlap.is_empty and overlap.width > 0 and overlap.height > 0:
                union(left, right)

    components: dict[int, list[int]] = {}
    for index in range(len(parsed)):
        components.setdefault(find(index), []).append(index)

    replacements: dict[int, tuple[
        dict[str, Any],
        str,
        list[float],
        list[str],
        list[dict[str, Any]],
    ]] = {}
    consumed: set[int] = set()
    for indices in components.values():
        component_kinds = {kinds[index] for index in indices}
        if (
            len(indices) < 2
            or "body" not in component_kinds
            or not component_kinds.intersection({"equation", "footnote"})
        ):
            continue
        blocks = [parsed[index][0] for index in indices]
        merged_rect = pymupdf.Rect(blocks[0].get("bbox") or [])
        for block in blocks[1:]:
            merged_rect |= pymupdf.Rect(block.get("bbox") or [])
        merged_block = {
            **blocks[0],
            "bbox": rect_list(merged_rect),
            "lines": _merge_raw_lines(
                blocks,
                [kinds[index] for index in indices],
            ),
        }
        text, sizes, fonts, span_runs = block_text(merged_block)
        first = min(indices)
        replacements[first] = (
            merged_block,
            text,
            sizes,
            fonts,
            span_runs,
        )
        consumed.update(index for index in indices if index != first)

    return [
        replacements.get(index, record)
        for index, record in enumerate(parsed)
        if index not in consumed
    ]


def locate_span_runs(
    source_text: str, span_runs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Map source spans onto the cleaned block text without guessing failures."""
    located: list[dict[str, Any]] = []
    cursor = 0
    for run in span_runs:
        needle = re.sub(r"\s+", " ", str(run.get("text") or "")).strip()
        if not needle:
            continue
        start = source_text.find(needle, cursor)
        if start < 0:
            continue
        item = dict(run)
        item["start"] = start
        item["end"] = start + len(needle)
        item["text"] = needle
        located.append(item)
        cursor = start + len(needle)
    return located


MATHA_TEX = {
    "`": "+",
    "´": "-",
    "ˆ": r"\times ",
    "¨": r"\cdot ",
    "˝": r"\circ ",
    "˘": r"\pm ",
    "˚": r"\ast ",
    "“": "=",
    "‰": r"\neq ",
    "?": r"\sqrt{}",
    "@": r"\forall ",
    "D": r"\exists ",
    "P": r"\in ",
    "R": r"\notin ",
    "^": r"\wedge ",
    "p": "(",
    "q": ")",
    "r": "[",
    "s": "]",
    "t": r"\{",
    "u": r"\}",
    "x": r"\langle ",
    "y": r"\rangle ",
    "z": r"\backslash ",
    "{": "/",
    "|": r"\mid ",
    "}": r"\Vert ",
    "Ă": r"\subset ",
    "ă": "<",
    "ą": ">",
    "ď": r"\leq ",
    "ě": r"\geq ",
    "Ð": r"\leftarrow ",
    "Ñ": r"\rightarrow ",
    "Ø": r"\leftrightarrow ",
    "Ý": r"\relbar ",
    "ñ": r"\Rightarrow ",
    "ù": r"\Relbar ",
    "‹": r"\ast ",
}

MATHX_TEX = {
    "ř": r"\sum ",
    "ÿ": r"\sum ",
    "ś": r"\prod ",
    "ź": r"\prod ",
}


def math_run_tex(run: dict[str, Any]) -> str:
    """Decode the source font's glyph slots into portable TeX."""
    value = str(run.get("text") or "")
    font = str(run.get("font") or "")
    if re.match(r"TeX-matha\d*$", font):
        return "".join(MATHA_TEX.get(character, character) for character in value)
    if re.match(r"TeX-mathb\d*$", font):
        return (
            value.replace("r", r"\lceil ")
            .replace("s", r"\rceil ")
        )
    if re.match(r"TeX-mathx\d*$", font):
        parts: list[str] = []
        for character in value:
            if character.isspace():
                parts.append(character)
            elif character in MATHX_TEX:
                parts.append(MATHX_TEX[character])
            else:
                parts.append(
                    f"[[UNRESOLVED:MATHX-{ord(character):04X}]]"
                )
        return "".join(parts)
    if re.match(r"MSBM\d*$", font):
        return "".join(
            rf"\mathbb{{{character}}}"
            if character.isalpha()
            else character
            for character in value
        )
    if re.match(r"CMSY\d*$", font):
        parts = []
        for character in value:
            if character in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                parts.append(rf"\mathcal{{{character}}}")
            elif character == "{":
                parts.append(r"\{")
            elif character == "}":
                parts.append(r"\}")
            elif character == "•":
                parts.append(r"\bullet ")
            elif character.isspace():
                parts.append(character)
            else:
                parts.append(
                    f"[[UNRESOLVED:CMSY-{ord(character):04X}]]"
                )
        return "".join(parts)
    if re.match(r"CMMIB\d*$", font):
        return rf"\boldsymbol{{{value}}}"
    if re.match(r"CMBX\d*$", font) and re.fullmatch(
        r"[A-Za-z0-9]+", value
    ):
        return rf"\mathbf{{{value}}}"
    if re.match(r"CMR\d*$", font):
        value = value.replace("$", r"\$")
        value = value.replace("ˆ", r"\widehatmark{}")
        if re.fullmatch(r"[A-Za-z]{2,}", value):
            return rf"\mathrm{{{value}}}"
    return value


def normalize_math_tex(tex: str) -> str:
    """Join split glyphs and scripts after font-aware decoding."""
    greek_tex = {
        "Α": "A",
        "Β": "B",
        "Γ": r"\Gamma ",
        "Δ": r"\Delta ",
        "Ε": "E",
        "Ζ": "Z",
        "Η": "H",
        "Θ": r"\Theta ",
        "Ι": "I",
        "Κ": "K",
        "Λ": r"\Lambda ",
        "Μ": "M",
        "Ν": "N",
        "Ξ": r"\Xi ",
        "Ο": "O",
        "Π": r"\Pi ",
        "Ρ": "P",
        "Σ": r"\Sigma ",
        "Τ": "T",
        "Υ": r"\Upsilon ",
        "Φ": r"\Phi ",
        "Χ": "X",
        "Ψ": r"\Psi ",
        "Ω": r"\Omega ",
        "α": r"\alpha ",
        "β": r"\beta ",
        "γ": r"\gamma ",
        "δ": r"\delta ",
        "ε": r"\epsilon ",
        "ϵ": r"\varepsilon ",
        "ζ": r"\zeta ",
        "η": r"\eta ",
        "θ": r"\theta ",
        "ϑ": r"\vartheta ",
        "ι": r"\iota ",
        "κ": r"\kappa ",
        "λ": r"\lambda ",
        "μ": r"\mu ",
        "µ": r"\mu ",
        "ν": r"\nu ",
        "ξ": r"\xi ",
        "ο": r"\omicron ",
        "π": r"\pi ",
        "ϖ": r"\varpi ",
        "ρ": r"\rho ",
        "ϱ": r"\varrho ",
        "σ": r"\sigma ",
        "ς": r"\varsigma ",
        "τ": r"\tau ",
        "υ": r"\upsilon ",
        "φ": r"\phi ",
        "ϕ": r"\varphi ",
        "χ": r"\chi ",
        "ψ": r"\psi ",
        "ω": r"\omega ",
        "ℓ": r"\ell ",
    }
    tex = "".join(greek_tex.get(character, character) for character in tex)
    tex = tex.replace(r"\leftarrow \relbar ", r"\longleftarrow ")
    tex = tex.replace(r"\Relbar \Rightarrow ", r"\Longrightarrow ")
    tex = tex.replace(r"\Leftarrow \Relbar ", r"\Longleftarrow ")
    for _ in range(8):
        updated = re.sub(
            r"\^\{([^{}]*)\}\s*\^\{([^{}]*)\}",
            r"^{\1\2}",
            tex,
        )
        updated = re.sub(
            r"_\{([^{}]*)\}\s*_\{([^{}]*)\}",
            r"_{\1\2}",
            updated,
        )
        if updated == tex:
            break
        tex = updated
    tex = re.sub(
        r"\^\{\\\$\}\s*\\longleftarrow\s*",
        r"\\xleftarrow{\\$} ",
        tex,
    )
    tex = re.sub(
        r"\\longleftarrow\s*\^\{\\\$\}",
        r"\\xleftarrow{\\$}",
        tex,
    )
    tex = re.sub(
        r"\\widehatmark\{\}\s*([A-Za-zΑ-ω])",
        r"\\hat{\1}",
        tex,
    )
    tex = re.sub(
        r"([A-Za-zΑ-ω])\s*\\widehatmark\{\}",
        r"\\hat{\1}",
        tex,
    )
    tex = re.sub(r"(\\[A-Za-z]+) (?=[}\]])", r"\1", tex)
    return re.sub(r"[ \t]+", " ", tex).strip()


def math_markup(
    runs: list[dict[str, Any]],
    source_text: str | None = None,
    start: int | None = None,
    end: int | None = None,
) -> tuple[str, str]:
    """Create safe HTML and Markdown for a simple source math run."""
    if not runs:
        return "", ""
    dominant_size = max(float(run.get("size") or 0.0) for run in runs)
    baselines = [
        float((run.get("origin") or [0.0, 0.0])[1])
        for run in runs
        if float(run.get("size") or 0.0) >= dominant_size * 0.9
    ]
    baseline = statistics.median(baselines) if baselines else 0.0
    html_parts: list[str] = []
    markdown_parts: list[str] = []
    cursor = start
    for run in runs:
        run_start = int(run.get("start", cursor or 0))
        run_end = int(run.get("end", run_start))
        if source_text is not None and cursor is not None and run_start > cursor:
            gap = source_text[cursor:run_start]
            escaped_gap = (
                gap.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            html_parts.append(escaped_gap)
            markdown_parts.append(gap)
        value = str(run.get("text") or "")
        tex_value = math_run_tex(run)
        escaped = (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        size = float(run.get("size") or 0.0)
        origin_y = float((run.get("origin") or [0.0, baseline])[1])
        script = size > 0 and dominant_size > 0 and size <= dominant_size * 0.82
        if script and origin_y > baseline + 0.4:
            html_parts.append(f"<sub>{escaped}</sub>")
            markdown_parts.append(f"_{{{tex_value}}}")
        elif script and origin_y < baseline - 0.4:
            html_parts.append(f"<sup>{escaped}</sup>")
            markdown_parts.append(f"^{{{tex_value}}}")
        elif run.get("style") == "math" and re.search(r"[A-Za-zΑ-ω]", value):
            html_parts.append(f"<i class=\"math-var\">{escaped}</i>")
            markdown_parts.append(tex_value)
        else:
            html_parts.append(escaped)
            markdown_parts.append(tex_value)
        if cursor is not None:
            cursor = max(cursor, run_end)
    if (
        source_text is not None
        and cursor is not None
        and end is not None
        and end > cursor
    ):
        gap = source_text[cursor:end]
        escaped_gap = (
            gap.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        html_parts.append(escaped_gap)
        markdown_parts.append(gap)
    return (
        "".join(html_parts),
        "$" + normalize_math_tex("".join(markdown_parts)) + "$",
    )


def math_fragment_bbox(runs: list[dict[str, Any]]) -> list[float] | None:
    boxes = [
        pymupdf.Rect(run.get("bbox") or [0, 0, 0, 0])
        for run in runs
        if pymupdf.Rect(run.get("bbox") or [0, 0, 0, 0]).get_area() > 0
    ]
    if not boxes:
        return None
    union = pymupdf.Rect(boxes[0])
    for box in boxes[1:]:
        union |= box
    return rect_list(union)


def math_render_strategy(runs: list[dict[str, Any]], markdown: str) -> str:
    if "[[UNRESOLVED:" in markdown:
        return "source-vector"
    tex = markdown[1:-1] if markdown.startswith("$") and markdown.endswith("$") else ""
    if re.search(r"\^\{[^{}]*\}\s*\^\{", tex):
        return "source-vector"
    dominant = max((float(run.get("size") or 0.0) for run in runs), default=0.0)
    if dominant and any(
        0 < float(run.get("size") or 0.0) < dominant * 0.64 for run in runs
    ):
        return "source-vector"
    return "tex-vector"


def looks_like_math_run(run: dict[str, Any]) -> bool:
    """Recognize symbol-heavy expressions even when PDF font flags say bold."""
    value = str(run.get("text") or "").strip()
    if not value or len(value) > 120:
        return False
    # PDF text spans often end at a font change immediately after "(i.e.,"
    # or "(by". Punctuation does not turn these short prose words into math.
    if re.search(r"\b(?:i\s*\.\s*e|e\s*\.\s*g)\s*\.", value, re.IGNORECASE):
        return False
    if re.search(r"\b(?:an|as|at|by|in|is|of|on|or|to)\b", value):
        return False
    if re.search(r"(?:^|[\s(])a(?:\s+(?=\d)|\s*$)", value):
        return False
    # Delimiters are recovered around real expressions below. Treating a
    # closing prose parenthesis as a math seed swallows it into the preceding
    # expression, as in "(by ECP_i)".
    if re.fullmatch(r"[\s(){}\[\],.;:]+", value):
        return False
    # TeX's upright Greek capitals (for example a Lambda accumulator base)
    # live in CMR, unlike italic variables. They still belong to adjacent
    # mathematical subscripts and must participate in the same fragment.
    if re.fullmatch(r"CMR\d+", str(run.get("font") or "")) and re.fullmatch(
        r"[Α-Ωα-ω]+", value
    ):
        return True
    if not re.search(r"[=<>≤≥≈≠+\-*/^√∑∏∫(){}\[\]0-9]", value):
        return False
    prose_words = [
        word
        for word in re.findall(r"[A-Za-z]{3,}", value)
        if word.casefold() not in {"mod", "log", "exp", "sin", "cos", "tan"}
    ]
    return not prose_words


def expand_math_fragment_bounds(
    source_text: str, start: int, end: int
) -> tuple[int, int]:
    """Keep function-style delimiters such as ``O(√p)`` in one fragment."""
    prefix = source_text[:start]
    function_open = re.search(r"[A-Za-z][A-Za-z0-9]*\(\s*$", prefix)
    if function_open:
        start = function_open.start()
    else:
        parenthesis_open = re.search(r"\(\s*$", prefix)
        if parenthesis_open:
            start = parenthesis_open.start()
    fragment = source_text[start:end]
    open_parentheses = fragment.count("(") - fragment.count(")")
    while open_parentheses > 0 and end < len(source_text):
        character = source_text[end]
        if character.isspace():
            end += 1
            continue
        if character == ")":
            end += 1
            open_parentheses -= 1
            continue
        break
    return start, end


def build_rich_protection(
    source_text: str,
    span_runs: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, str], dict[str, dict[str, Any]], dict[str, str]]:
    """Protect exact values plus source typography using controlled tokens."""
    located = locate_span_runs(source_text, span_runs or [])
    body_size = (
        statistics.median(
            float(run.get("size") or 0.0)
            for run in located
            if float(run.get("size") or 0.0) > 0
        )
        if located
        else 0.0
    )

    atomic: list[dict[str, Any]] = []
    for match in CITATION_RE.finditer(source_text):
        atomic.append(
            {
                "start": match.start(),
                "end": match.end(),
                "kind": "citation",
                "text": match.group(0),
                "runs": [],
            }
        )

    math_runs = [
        run
        for run in located
        if not (
            LIST_MARKER_RE.fullmatch(str(run.get("text") or "").strip())
            and not source_text[: int(run["start"])].rsplit("\n", 1)[-1].strip()
        )
        and (
            run.get("style") == "math"
            or looks_like_math_run(run)
            or (
                body_size > 0
                and float(run.get("size") or 0.0) <= body_size * 0.82
                and re.fullmatch(r"[\wΑ-ω]+", str(run.get("text") or ""))
            )
        )
    ]
    groups: list[list[dict[str, Any]]] = []
    for run in math_runs:
        if not groups:
            groups.append([run])
            continue
        previous = groups[-1][-1]
        gap = source_text[int(previous["end"]) : int(run["start"])]
        same_line = int(previous.get("line", -1)) == int(run.get("line", -2))
        monotonic = int(run["start"]) >= int(previous["end"])
        if (
            monotonic
            and same_line
            and len(gap) <= 4
            and re.fullmatch(r"[\s,.;:(){}\[\]=+\-*/^]*", gap)
        ):
            groups[-1].append(run)
        else:
            groups.append([run])
    for group in groups:
        start = int(group[0]["start"])
        end = int(group[-1]["end"])
        start, end = expand_math_fragment_bounds(source_text, start, end)
        if end <= start:
            continue
        fragment_text = source_text[start:end]
        compact_fragment = fragment_text.strip()
        decorated_singleton = any(
            re.search(r"\\(?:mathcal|mathbb|mathbf|boldsymbol)\{", math_run_tex(run))
            for run in group
        )
        if (
            re.fullmatch(r"[A-Za-zΑ-ω][,.;:]?", compact_fragment)
            and not decorated_singleton
        ):
            following = source_text[end : end + 1]
            if not (
                compact_fragment == "q"
                and following in {"-", "("}
            ):
                continue
        if any(start < item["end"] and end > item["start"] for item in atomic):
            continue
        is_footnote_marker = (
            bool(re.fullmatch(r"(?:\d+|[⋆*†‡])", compact_fragment))
            and all(
                float(run.get("size") or 0.0) <= body_size * 0.82
                for run in group
            )
            and any(int(run.get("flags") or 0) & 1 for run in group)
        )
        math_html, markdown = math_markup(
            group, source_text, start, end
        )
        source_bbox = math_fragment_bbox(group)
        atomic.append(
            {
                "start": start,
                "end": end,
                "kind": (
                    "footnote-marker" if is_footnote_marker else "math"
                ),
                "text": fragment_text,
                "html": math_html,
                "markdown": markdown,
                "tex": (
                    markdown[1:-1]
                    if markdown.startswith("$") and markdown.endswith("$")
                    else ""
                ),
                "render_strategy": math_render_strategy(group, markdown),
                "display": False,
                "review_status": (
                    "auto_validated"
                    if math_render_strategy(group, markdown) == "tex-vector"
                    else "unresolved"
                ),
                "source_bbox": source_bbox,
                "runs": group,
            }
        )
    atomic.sort(key=lambda item: (int(item["start"]), -int(item["end"])))
    non_overlapping: list[dict[str, Any]] = []
    for item in atomic:
        if non_overlapping and int(item["start"]) < int(non_overlapping[-1]["end"]):
            continue
        non_overlapping.append(item)
    atomic = non_overlapping

    style_intervals: list[dict[str, Any]] = []
    for run in located:
        style = str(run.get("style") or "")
        if style not in {"bold", "italic"}:
            continue
        if any(
            int(run["start"]) < int(item["end"])
            and int(run["end"]) > int(item["start"])
            and item["kind"] == "math"
            for item in atomic
        ):
            continue
        style_intervals.append(
            {
                "start": int(run["start"]),
                "end": int(run["end"]),
                "kind": style,
            }
        )
    merged_styles: list[dict[str, Any]] = []
    for interval in style_intervals:
        if (
            merged_styles
            and merged_styles[-1]["kind"] == interval["kind"]
            and interval["start"] >= merged_styles[-1]["end"]
            and source_text[merged_styles[-1]["end"] : interval["start"]].isspace()
        ):
            merged_styles[-1]["end"] = interval["end"]
        else:
            merged_styles.append(dict(interval))

    protected_tokens: dict[str, str] = {}
    inline_fragments: dict[str, dict[str, Any]] = {}
    style_tokens: dict[str, str] = {}
    start_events: dict[int, list[str]] = {}
    end_events: dict[int, list[str]] = {}
    for index, interval in enumerate(merged_styles, start=1):
        prefix = "B" if interval["kind"] == "bold" else "I"
        key = f"{prefix}{index:04d}"
        open_token = f"[[{key}_OPEN]]"
        close_token = f"[[{key}_CLOSE]]"
        style_tokens[key] = interval["kind"]
        start_events.setdefault(int(interval["start"]), []).append(open_token)
        end_events.setdefault(int(interval["end"]), []).append(close_token)

    atomic_by_start: dict[int, dict[str, Any]] = {}
    for index, item in enumerate(atomic, start=1):
        token = f"[[F{index:04d}]]"
        fragment = {
            key: value
            for key, value in item.items()
            if key not in {"start", "end"}
        }
        fragment["token"] = token
        inline_fragments[token] = fragment
        item["token"] = token
        atomic_by_start[int(item["start"])] = item

    def protect_plain(value: str) -> str:
        if not re.search(
            r"\d|\[|\(|\$|https?://|doi|arXiv",
            value,
            re.IGNORECASE,
        ):
            return value
        parts: list[str] = []
        cursor = 0
        for match in PROTECTED_RE.finditer(value):
            parts.append(value[cursor : match.start()])
            token = f"[[P{len(protected_tokens) + 1:04d}]]"
            protected_tokens[token] = match.group(0)
            parts.append(token)
            cursor = match.end()
        parts.append(value[cursor:])
        return "".join(parts)

    output: list[str] = []
    cursor = 0
    event_positions = sorted(
        set(start_events) | set(end_events) | set(atomic_by_start)
    )
    while cursor < len(source_text):
        if cursor in end_events:
            output.extend(reversed(end_events[cursor]))
        if cursor in start_events:
            output.extend(start_events[cursor])
        atom = atomic_by_start.get(cursor)
        if atom is not None:
            output.append(str(atom["token"]))
            cursor = int(atom["end"])
            continue
        event_index = bisect.bisect_right(event_positions, cursor)
        next_position = (
            event_positions[event_index]
            if event_index < len(event_positions)
            else len(source_text)
        )
        output.append(protect_plain(source_text[cursor:next_position]))
        cursor = next_position
    if cursor in end_events:
        output.extend(reversed(end_events[cursor]))
    return "".join(output), protected_tokens, inline_fragments, style_tokens


def image_coverage(page: pymupdf.Page) -> float:
    page_area = page.rect.get_area()
    if page_area <= 0:
        return 0.0
    rectangles: list[pymupdf.Rect] = []
    for image in page.get_images(full=True):
        try:
            rectangles.extend(page.get_image_rects(image[0]))
        except Exception:
            continue
    if not rectangles:
        return 0.0
    return max(rect.get_area() for rect in rectangles) / page_area


def page_is_scan(page: pymupdf.Page, mode: str) -> bool:
    if mode == "force":
        return True
    if mode == "off":
        return False
    text = re.sub(r"\s+", "", page.get_text("text"))
    return len(text) < 80 and image_coverage(page) >= 0.45


def classify_columns(elements: list[dict[str, Any]], page_width: float) -> int:
    text_boxes = [
        pymupdf.Rect(element["bbox"])
        for element in elements
        if element["render_mode"] == "text"
        and element.get("kind") in {"body", "footnote"}
        and len(element.get("source_text", "")) >= 20
        and len(
            re.findall(
                r"\b[A-Za-z][A-Za-z-]{2,}\b",
                str(element.get("source_text") or ""),
            )
        )
        >= 3
    ]
    left = [rect for rect in text_boxes if rect.x1 <= page_width * 0.61]
    right = [rect for rect in text_boxes if rect.x0 >= page_width * 0.39]
    aligned_pairs = sum(
        1
        for left_rect in left
        if any(
            max(left_rect.y0, right_rect.y0)
            < min(left_rect.y1, right_rect.y1)
            for right_rect in right
        )
    )
    return 2 if len(left) >= 2 and len(right) >= 2 and aligned_pairs >= 2 else 1


def reading_order(
    elements: list[dict[str, Any]], page_width: float, columns: int
) -> list[dict[str, Any]]:
    if columns == 1:
        return sorted(elements, key=lambda item: (item["bbox"][1], item["bbox"][0]))

    full_width = [
        item
        for item in elements
        if item.get("kind") in {"title", "front-matter"}
        or (item["bbox"][2] - item["bbox"][0]) >= page_width * 0.60
    ]
    narrow = [item for item in elements if item not in full_width]
    full_width.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))

    ordered: list[dict[str, Any]] = []
    band_top = -math.inf
    for full in full_width:
        band_bottom = full["bbox"][1]
        band = [
            item
            for item in narrow
            if band_top <= item["bbox"][1] < band_bottom
        ]
        ordered.extend(
            sorted(
                band,
                key=lambda item: (
                    0
                    if (item["bbox"][0] + item["bbox"][2]) / 2 < page_width / 2
                    else 1,
                    item["bbox"][1],
                    item["bbox"][0],
                ),
            )
        )
        ordered.append(full)
        band_top = full["bbox"][3]

    tail = [item for item in narrow if item["bbox"][1] >= band_top]
    ordered.extend(
        sorted(
            tail,
            key=lambda item: (
                0
                if (item["bbox"][0] + item["bbox"][2]) / 2 < page_width / 2
                else 1,
                item["bbox"][1],
                item["bbox"][0],
            ),
        )
    )

    seen: set[int] = set()
    deduplicated: list[dict[str, Any]] = []
    for item in ordered:
        marker = id(item)
        if marker not in seen:
            seen.add(marker)
            deduplicated.append(item)
    for item in elements:
        if id(item) not in seen:
            deduplicated.append(item)
    return deduplicated


def classify_text_kind(
    text: str,
    rect: pymupdf.Rect,
    page_rect: pymupdf.Rect,
    font_size: float,
    body_size: float,
    page_number: int,
) -> str:
    compact = normalized_heading(text)
    if rect.y0 >= page_rect.height * 0.92:
        return "footer"
    if (
        rect.y0 >= page_rect.height * 0.85
        and re.fullmatch(r"(?:\d{1,4}|[ivxlcdm]{1,8})", compact, re.IGNORECASE)
    ):
        return "footer"
    if rect.height >= rect.width * 3 and (
        rect.x1 <= page_rect.width * 0.12 or rect.x0 >= page_rect.width * 0.88
    ):
        return "margin-note"
    if CAPTION_RE.match(compact) and not CAPTION_NARRATIVE_RE.match(compact):
        return "caption"
    if ABSTRACT_RE.match(compact):
        return "abstract-heading"
    if KEYWORDS_RE.match(compact):
        return "keywords"
    if page_number == 1 and font_size >= body_size * 1.55 and len(compact) <= 300:
        return "title"
    center = (rect.x0 + rect.x1) / 2
    if (
        page_number == 1
        and rect.y0 <= page_rect.height * 0.45
        and abs(center - page_rect.width / 2) <= page_rect.width * 0.16
        and len(compact) <= 300
    ):
        return "front-matter"
    math_ratio = (
        sum(1 for char in compact if char in MATH_CHARS) / max(1, len(compact))
    )
    has_heading_word = bool(
        re.search(r"\b[A-Za-z][A-Za-z-]{2,}\b", compact)
    )
    numbered_tail = re.match(
        r"^\s*\d+(?:\.\d+)*[.)]?\s+(?P<tail>.+)$",
        compact,
    )
    starts_with_heading_word = bool(
        numbered_tail
        and re.match(
            r"[A-Za-z][A-Za-z-]{2,}\b",
            numbered_tail.group("tail"),
        )
    )
    starts_with_lowercase_prose = bool(
        re.match(r"^[^A-Za-z]*[a-z][a-z-]{2,}\b", compact)
    )
    starts_with_prose_keyword = bool(
        re.match(
            r"^(?:[–—-]\s*)?(?:For|If|Else|Return|Run|Set|While|Rewind|Obtain)\b",
            compact,
        )
    )
    if (
        math_ratio >= 0.06
        and len(compact.split()) <= 40
        and not starts_with_lowercase_prose
        and not starts_with_prose_keyword
    ) or (
        HEADING_RE.match(compact)
        and not has_heading_word
        and len(compact.split()) <= 16
    ) or (
        HEADING_RE.match(compact)
        and math_ratio > 0
        and numbered_tail is not None
        and not starts_with_heading_word
    ):
        return "equation"
    if (
        INTRO_RE.match(compact)
        or CONCLUSION_RE.match(compact)
        or STOP_RE.match(compact)
        or (
            HEADING_RE.match(compact)
            and has_heading_word
            and math_ratio < 0.06
        )
        or (
            font_size >= body_size * 1.18
            and len(compact) <= 180
            and math_ratio < 0.06
        )
    ):
        return "heading"
    if (
        font_size <= body_size * 0.92
        and rect.y0 >= page_rect.height * 0.55
        and re.match(r"^\s*(?:[⋆*†‡]|\d{1,2}\b)", compact)
    ):
        return "footnote"
    return "body"


def detect_tables(page: pymupdf.Page) -> list[pymupdf.Rect]:
    try:
        finder = page.find_tables()
        return [pymupdf.Rect(table.bbox) for table in finder.tables]
    except Exception:
        return []


def promote_display_math_fragments(
    elements: list[dict[str, Any]],
    body_size: float,
) -> None:
    """Keep symbol-only pieces of a display equation in one native-math group."""
    equation_rects = [
        pymupdf.Rect(element.get("bbox") or [])
        for element in elements
        if element.get("kind") == "equation"
    ]
    if not equation_rects:
        return
    pending = [
        element
        for element in elements
        if element.get("kind") in {"body", "footnote"}
        and element.get("render_mode") == "text"
    ]
    changed = True
    while changed:
        changed = False
        for element in list(pending):
            runs = list(element.get("span_runs") or [])
            visible_runs = [
                run for run in runs if str(run.get("text") or "").strip()
            ]
            visible_chars = sum(
                len(re.sub(r"\s+", "", str(run.get("text") or "")))
                for run in visible_runs
            )
            math_chars = sum(
                len(re.sub(r"\s+", "", str(run.get("text") or "")))
                for run in visible_runs
                if run.get("style") == "math"
            )
            math_ratio = math_chars / max(1, visible_chars)
            compact = normalized_heading(str(element.get("source_text") or ""))
            rect = pymupdf.Rect(element.get("bbox") or [])
            short_operator = compact in {"Pr", "E", "Var", "Cov"}
            has_prose_word = bool(re.search(r"\b[A-Za-z]{3,}\b", compact))
            starts_with_lowercase_prose = bool(
                re.match(r"^[^A-Za-z]*[a-z][a-z-]{2,}\b", compact)
            )
            starts_with_prose_keyword = bool(
                re.match(
                    r"^(?:[–—-]\s*)?(?:For|If|Else|Return|Run|Set|While|Rewind|Obtain)\b",
                    compact,
                )
            )
            near_equation = any(
                min(rect.y1, equation_rect.y1)
                - max(rect.y0, equation_rect.y0)
                > 0
                or min(
                    abs(rect.y0 - equation_rect.y1),
                    abs(equation_rect.y0 - rect.y1),
                )
                <= body_size * 1.5
                for equation_rect in equation_rects
            )
            if (
                near_equation
                and (
                    rect.height <= body_size * 4.0
                    or not has_prose_word
                )
                and len(re.sub(r"\s+", "", compact)) <= 240
                and (
                    math_ratio >= 0.35
                    or short_operator
                    or not has_prose_word
                )
                and (not starts_with_lowercase_prose or short_operator)
                and (not starts_with_prose_keyword or short_operator)
            ):
                element["kind"] = "equation"
                element["render_mode"] = "source_clip"
                equation_rects.append(rect)
                pending.remove(element)
                changed = True


def extract_text_page(
    page: pymupdf.Page, page_number: int
) -> tuple[list[dict[str, Any]], int]:
    raw = page.get_text("dict", sort=True)
    parsed: list[
        tuple[
            dict[str, Any],
            str,
            list[float],
            list[str],
            list[dict[str, Any]],
        ]
    ] = []
    all_sizes: list[float] = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        text, sizes, fonts, span_runs = block_text(block)
        if not text:
            continue
        parsed.append((block, text, sizes, fonts, span_runs))
        all_sizes.extend(sizes)
    body_size = statistics.median(all_sizes) if all_sizes else 10.0
    parsed = merge_fragmented_text_blocks(
        parsed,
        page.rect,
        body_size,
        page_number,
    )

    table_rects = detect_tables(page)
    image_rects = [
        pymupdf.Rect(block["bbox"])
        for block in raw.get("blocks", [])
        if block.get("type") == 1
        and pymupdf.Rect(block["bbox"]).get_area() >= page.rect.get_area() * 0.002
    ]
    drawing_rects = page_drawing_rects(page)

    elements: list[dict[str, Any]] = []
    for index, table_rect in enumerate(table_rects, start=1):
        elements.append(
            {
                "page": page_number,
                "bbox": rect_list(table_rect),
                "kind": "table",
                "render_mode": "source_clip",
                "source_text": "",
                "font_size": body_size,
                "font_names": [],
                "confidence": 1.0,
                "source_ref": f"table-{page_number}-{index}",
            }
        )
    for index, image_rect in enumerate(image_rects, start=1):
        if any(intersection_ratio(image_rect, table) >= 0.8 for table in table_rects):
            continue
        elements.append(
            {
                "page": page_number,
                "bbox": rect_list(image_rect),
                "kind": "figure",
                "render_mode": "source_clip",
                "source_text": "",
                "font_size": body_size,
                "font_names": [],
                "confidence": 1.0,
                "source_ref": f"figure-{page_number}-{index}",
            }
        )

    for block, text, sizes, fonts, span_runs in parsed:
        rect = pymupdf.Rect(block["bbox"])
        if any(intersection_ratio(rect, table) >= 0.35 for table in table_rects):
            continue
        font_size = statistics.median(sizes) if sizes else body_size
        kind = classify_text_kind(
            text, rect, page.rect, font_size, body_size, page_number
        )
        if any(
            intersection_ratio(rect, image_rect) >= 0.25
            for image_rect in image_rects
        ):
            kind = "figure-text"
        render_mode = (
            "source_clip"
            if kind in {"equation", "margin-note", "figure-text"}
            else "text"
        )
        elements.append(
            {
                "page": page_number,
                "bbox": rect_list(rect),
                "kind": kind,
                "render_mode": render_mode,
                "source_text": text,
                "font_size": round(font_size, 3),
                "font_names": sorted(set(fonts)),
                "span_runs": span_runs,
                "confidence": 1.0,
            }
        )

    promote_display_math_fragments(elements, body_size)
    mark_page_footnotes(elements, drawing_rects, page.rect, body_size)
    add_vector_figure_candidates(
        elements,
        drawing_rects,
        page.rect,
        page_number,
        body_size,
    )

    # A composite vector figure may be represented by several small image
    # blocks, leaving a central label outside every individual rectangle.
    # When the page has a caption and multiple image fragments, preserve text
    # inside their combined visual envelope as part of the figure.
    caption_count = sum(
        1 for element in elements if element["kind"] == "caption"
    )
    if len(image_rects) >= 2 and caption_count == 1:
        composite_rect = pymupdf.Rect(image_rects[0])
        for image_rect in image_rects[1:]:
            composite_rect |= image_rect
        for element in elements:
            if element["kind"] != "body":
                continue
            rect = pymupdf.Rect(element["bbox"])
            center = pymupdf.Point(
                (rect.x0 + rect.x1) / 2,
                (rect.y0 + rect.y1) / 2,
            )
            if center in composite_rect:
                element["kind"] = "figure-text"
                element["render_mode"] = "source_clip"
        elements = [
            element
            for element in elements
            if not (
                element["kind"] in {"figure", "figure-text"}
                and (
                    intersection_ratio(
                        pymupdf.Rect(element["bbox"]), composite_rect
                    )
                    >= 0.8
                    or pymupdf.Point(
                        (
                            pymupdf.Rect(element["bbox"]).x0
                            + pymupdf.Rect(element["bbox"]).x1
                        )
                        / 2,
                        (
                            pymupdf.Rect(element["bbox"]).y0
                            + pymupdf.Rect(element["bbox"]).y1
                        )
                        / 2,
                    )
                    in composite_rect
                )
            )
        ]
        elements.append(
            {
                "page": page_number,
                "bbox": rect_list(composite_rect),
                "kind": "figure",
                "render_mode": "source_clip",
                "source_text": "",
                "font_size": body_size,
                "font_names": [],
                "confidence": 1.0,
                "source_ref": f"composite-figure-{page_number}",
            }
        )

    columns = classify_columns(elements, page.rect.width)
    ordered = reading_order(elements, page.rect.width, columns)

    # Vector figures frequently expose their internal labels as independent
    # text blocks instead of one image block. After a figure caption, preserve
    # small text blocks as source clips until normal-size prose or a heading
    # resumes. This also catches a caption split across adjacent PDF blocks.
    after_caption = False
    for element in ordered:
        if element["kind"] == "caption":
            after_caption = True
            continue
        if not after_caption:
            continue
        if element["kind"] == "heading":
            after_caption = False
            continue
        if element["kind"] in {"figure", "table", "equation", "margin-note"}:
            continue
        if (
            element["kind"] == "body"
            and element["font_size"] <= body_size * 0.95
        ):
            element["kind"] = "figure-text"
            element["render_mode"] = "source_clip"
            continue
        after_caption = False

    return ordered, columns


def normalize_rapidocr_result(raw: Any) -> list[tuple[list[list[float]], str, float]]:
    if hasattr(raw, "boxes") and hasattr(raw, "txts"):
        raw_boxes = getattr(raw, "boxes", None)
        raw_texts = getattr(raw, "txts", None)
        raw_scores = getattr(raw, "scores", None)
        boxes = list(raw_boxes) if raw_boxes is not None else []
        texts = list(raw_texts) if raw_texts is not None else []
        scores = (
            list(raw_scores) if raw_scores is not None else [1.0] * len(texts)
        )
        return [
            (box, str(text), float(score))
            for box, text, score in zip(boxes, texts, scores)
        ]

    payload = raw[0] if isinstance(raw, tuple) and raw else raw
    if payload is None:
        return []
    normalized: list[tuple[list[list[float]], str, float]] = []
    for item in payload:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            normalized.append((item[0], str(item[1]), float(item[2])))
    return normalized


def merge_ocr_lines(
    lines: list[dict[str, Any]], page_width: float
) -> list[dict[str, Any]]:
    if not lines:
        return []
    columns = classify_columns(lines, page_width)
    lanes: list[list[dict[str, Any]]] = [[], []] if columns == 2 else [[]]
    for line in lines:
        center = (line["bbox"][0] + line["bbox"][2]) / 2
        lane = 0 if columns == 1 or center < page_width / 2 else 1
        lanes[lane].append(line)

    merged: list[dict[str, Any]] = []
    for lane in lanes:
        lane.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
        current: dict[str, Any] | None = None
        for line in lane:
            if current is None:
                current = dict(line)
                continue
            current_height = max(1.0, current["bbox"][3] - current["bbox"][1])
            gap = line["bbox"][1] - current["bbox"][3]
            indent = abs(line["bbox"][0] - current["bbox"][0])
            if gap <= current_height * 1.25 and indent <= page_width * 0.06:
                current["source_text"] = (
                    current["source_text"].rstrip("-") + " " + line["source_text"]
                )
                current["bbox"] = [
                    min(current["bbox"][0], line["bbox"][0]),
                    min(current["bbox"][1], line["bbox"][1]),
                    max(current["bbox"][2], line["bbox"][2]),
                    max(current["bbox"][3], line["bbox"][3]),
                ]
                current["confidence"] = min(
                    current["confidence"], line["confidence"]
                )
            else:
                merged.append(current)
                current = dict(line)
        if current is not None:
            merged.append(current)
    return reading_order(merged, page_width, columns)


def extract_ocr_page(
    page: pymupdf.Page,
    page_number: int,
    work_dir: Path,
    dpi: int,
    engine: Any,
) -> tuple[list[dict[str, Any]], int, float]:
    scale = dpi / 72.0
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    image_path = work_dir / "ocr-input" / f"page-{page_number:04d}.png"
    image_path.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(image_path)
    raw = engine(str(image_path))
    results = normalize_rapidocr_result(raw)
    lines: list[dict[str, Any]] = []
    for box, text, score in results:
        if not text.strip():
            continue
        xs = [float(point[0]) / scale for point in box]
        ys = [float(point[1]) / scale for point in box]
        rect = pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))
        approx_size = max(7.0, rect.height * 0.78)
        kind = classify_text_kind(
            text, rect, page.rect, approx_size, 10.0, page_number
        )
        lines.append(
            {
                "page": page_number,
                "bbox": rect_list(rect),
                "kind": kind,
                "render_mode": "source_clip" if kind == "equation" else "text",
                "source_text": text.strip(),
                "font_size": round(approx_size, 3),
                "font_names": [],
                "confidence": round(float(score), 4),
            }
        )
    merged = merge_ocr_lines(lines, page.rect.width)
    columns = classify_columns(merged, page.rect.width)
    confidence = (
        statistics.mean(item["confidence"] for item in lines) if lines else 0.0
    )
    return merged, columns, confidence


def protect_text(text: str) -> tuple[str, dict[str, str]]:
    protected: dict[str, str] = {}
    parts: list[str] = []
    cursor = 0
    for match in PROTECTED_RE.finditer(text):
        parts.append(text[cursor : match.start()])
        token = f"[[P{len(protected) + 1:04d}]]"
        protected[token] = match.group(0)
        parts.append(token)
        cursor = match.end()
    parts.append(text[cursor:])
    return "".join(parts), protected


def protect_element(element: dict[str, Any]) -> None:
    span_runs = list(element.get("span_runs") or [])
    (
        protected_text,
        protected_tokens,
        inline_fragments,
        style_tokens,
    ) = build_rich_protection(
        str(element.get("source_text") or ""),
        span_runs,
    )
    prose_runs = [
        run
        for run in span_runs
        if run.get("style") in {"text", "bold", "italic"}
        and re.search(r"[A-Za-z]", str(run.get("text") or ""))
    ]
    prose_chars = sum(len(str(run.get("text") or "")) for run in prose_runs)
    italic_chars = sum(
        len(str(run.get("text") or ""))
        for run in prose_runs
        if run.get("style") == "italic"
    )
    whole_italic = prose_chars > 0 and italic_chars / prose_chars >= 0.55
    if whole_italic:
        for key, kind in list(style_tokens.items()):
            if kind != "italic":
                continue
            protected_text = protected_text.replace(f"[[{key}_OPEN]]", "")
            protected_text = protected_text.replace(f"[[{key}_CLOSE]]", "")
            del style_tokens[key]
    namespace = re.sub(r"[^A-Za-z0-9]", "", str(element.get("id") or "unit"))
    renamed_fragments: dict[str, dict[str, Any]] = {}
    for sequence, (token, fragment) in enumerate(
        inline_fragments.items(), start=1
    ):
        new_token = f"[[F{namespace}_{sequence:04d}]]"
        protected_text = protected_text.replace(token, new_token)
        updated = dict(fragment)
        updated["token"] = new_token
        if updated.get("kind") == "math":
            updated["source_page"] = int(element.get("page") or 0)
        renamed_fragments[new_token] = updated
    renamed_styles: dict[str, str] = {}
    for sequence, (key, kind) in enumerate(style_tokens.items(), start=1):
        prefix = "B" if kind == "bold" else "I"
        new_key = f"{prefix}{namespace}_{sequence:04d}"
        protected_text = protected_text.replace(
            f"[[{key}_OPEN]]", f"[[{new_key}_OPEN]]"
        )
        protected_text = protected_text.replace(
            f"[[{key}_CLOSE]]", f"[[{new_key}_CLOSE]]"
        )
        renamed_styles[new_key] = kind
    element["protected_text"] = protected_text
    element["protected_tokens"] = protected_tokens
    element["inline_fragments"] = renamed_fragments
    element["style_tokens"] = renamed_styles
    element["block_style"] = "italic" if whole_italic else "normal"


def apply_translation_boundary(
    elements: list[dict[str, Any]]
) -> dict[str, Any]:
    active = False
    intro_id: str | None = None
    conclusion_id: str | None = None
    stop_id: str | None = None
    current_section = ""
    seen_conclusion = False
    conclusion_level: int | None = None

    def heading_level(value: str) -> int | None:
        match = re.match(r"^\s*(\d+(?:\.\d+)*)", value)
        if not match:
            return None
        return match.group(1).count(".") + 1

    for element in elements:
        text = normalized_heading(element.get("source_text", ""))
        is_heading = element["kind"] in {
            "heading",
            "abstract-heading",
            "keywords",
        }
        if is_heading and INTRO_RE.match(text) and intro_id is None:
            active = True
            intro_id = element["id"]
            current_section = text
        elif active and is_heading and CONCLUSION_RE.match(text):
            conclusion_id = element["id"]
            seen_conclusion = True
            conclusion_level = heading_level(text) or 1
            current_section = text
        elif (
            active
            and seen_conclusion
            and is_heading
            and heading_level(text) is not None
            and heading_level(text) <= (conclusion_level or 1)
        ):
            stop_id = element["id"]
            active = False
            current_section = text
        elif active and is_heading and STOP_RE.match(text):
            stop_id = element["id"]
            active = False
            current_section = text
        elif is_heading and text:
            current_section = text

        translatable = (
            active
            and element["render_mode"] == "text"
            and element["kind"] in {"heading", "body", "footnote"}
        )
        element["translatable"] = translatable
        element["section"] = current_section
        if translatable:
            protect_element(element)
        else:
            element["protected_text"] = element.get("source_text", "")
            element["protected_tokens"] = {}
            element["inline_fragments"] = {}
            element["style_tokens"] = {}

    return {
        "introduction_id": intro_id,
        "conclusion_id": conclusion_id,
        "post_body_stop_id": stop_id,
        "introduction_found": intro_id is not None,
        "conclusion_found": conclusion_id is not None,
        "post_body_stop_found": stop_id is not None,
        "needs_boundary_review": not (intro_id and conclusion_id),
        "seen_conclusion_before_stop": bool(seen_conclusion and stop_id),
        "manual_review": False,
    }


def mark_repeated_running_text(
    elements: list[dict[str, Any]], page_records: list[dict[str, Any]]
) -> list[str]:
    """Reclassify repeated top-of-page OCR text as running headers."""
    page_heights = {
        int(record["page"]): float(record["height"]) for record in page_records
    }
    occurrences: dict[str, list[dict[str, Any]]] = {}
    for element in elements:
        text = normalized_heading(str(element.get("source_text") or ""))
        page = int(element["page"])
        height = page_heights.get(page, 0.0)
        if (
            page <= 1
            or not height
            or float(element["bbox"][3]) > height * 0.20
            or not (5 <= len(text) <= 120)
        ):
            continue
        key = re.sub(r"\W+", " ", text).strip().casefold()
        if key:
            occurrences.setdefault(key, []).append(element)

    changed: list[str] = []
    for matches in occurrences.values():
        if len({int(item["page"]) for item in matches}) < 3:
            continue
        for element in matches:
            if element["kind"] in {"body", "heading"}:
                element["kind"] = "header"
                changed.append(str(element["id"]))
    return changed


def boundary_review_candidates(
    elements: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for element in elements:
        text = normalized_heading(str(element.get("source_text") or ""))
        signal = None
        if INTRO_RE.match(text):
            signal = "start"
        elif STOP_RE.match(text):
            signal = "stop-before"
        elif CONCLUSION_RE.match(text):
            signal = "conclusion"
        elif element.get("kind") == "heading" and len(text) <= 220:
            signal = "heading"
        if signal:
            candidates.append(
                {
                    "id": element["id"],
                    "page": element["page"],
                    "kind": element["kind"],
                    "signal": signal,
                    "text": text[:220],
                }
            )
    return candidates


def make_boundary_review(
    elements: list[dict[str, Any]],
    boundary: dict[str, Any],
    source_sha256: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_sha256": source_sha256,
        "reviewed": False,
        "start_id": boundary.get("introduction_id"),
        "stop_before_id": boundary.get("post_body_stop_id"),
        "allow_missing_conclusion": False,
        "reason": "",
        "include_ids": [],
        "exclude_ids": [],
        "candidates": boundary_review_candidates(elements),
    }


def apply_reviewed_boundary(
    elements: list[dict[str, Any]],
    review: dict[str, Any],
    source_sha256: str,
) -> dict[str, Any]:
    if review.get("source_sha256") != source_sha256:
        raise ValueError("Boundary review source hash does not match this PDF.")
    if review.get("reviewed") is not True:
        raise ValueError("Boundary review must set reviewed to true.")

    by_id = {str(element["id"]): element for element in elements}
    start_id = str(review.get("start_id") or "")
    stop_id = str(review.get("stop_before_id") or "")
    if start_id not in by_id or stop_id not in by_id:
        raise ValueError("Boundary review start_id and stop_before_id must exist.")
    start_order = int(by_id[start_id]["order"])
    stop_order = int(by_id[stop_id]["order"])
    if start_order >= stop_order:
        raise ValueError("Boundary review start must precede stop-before.")
    if not review.get("reason"):
        raise ValueError("Boundary review requires a non-empty reason.")

    include_ids = {str(value) for value in review.get("include_ids", [])}
    exclude_ids = {str(value) for value in review.get("exclude_ids", [])}
    unknown = (include_ids | exclude_ids) - set(by_id)
    if unknown:
        raise ValueError(f"Boundary review contains unknown IDs: {sorted(unknown)}")
    if include_ids & exclude_ids:
        raise ValueError("Boundary review include_ids and exclude_ids overlap.")

    current_section = ""
    conclusion_id: str | None = None
    for element in elements:
        element_id = str(element["id"])
        text = normalized_heading(str(element.get("source_text") or ""))
        if (
            start_order <= int(element["order"]) < stop_order
            and (element.get("kind") == "heading" or element_id == start_id)
            and text
        ):
            current_section = text
        if (
            start_order <= int(element["order"]) < stop_order
            and CONCLUSION_RE.match(text)
            and conclusion_id is None
        ):
            conclusion_id = element_id

        in_range = start_order <= int(element["order"]) < stop_order
        translatable = (
            in_range
            and element.get("render_mode") == "text"
            and element.get("kind")
            in {"heading", "body", "front-matter", "footnote"}
        )
        if element_id in include_ids:
            translatable = True
        if element_id in exclude_ids:
            translatable = False
        element["translatable"] = translatable
        element["section"] = current_section
        if translatable:
            protect_element(element)
        else:
            element["protected_text"] = element.get("source_text", "")
            element["protected_tokens"] = {}
            element["inline_fragments"] = {}
            element["style_tokens"] = {}

    allow_missing = review.get("allow_missing_conclusion") is True
    if conclusion_id is None and not allow_missing:
        raise ValueError(
            "No Conclusion was found; set allow_missing_conclusion to true "
            "after verifying the paper's last main section."
        )
    return {
        "introduction_id": start_id,
        "conclusion_id": conclusion_id,
        "post_body_stop_id": stop_id,
        "introduction_found": True,
        "conclusion_found": conclusion_id is not None,
        "post_body_stop_found": True,
        "needs_boundary_review": False,
        "seen_conclusion_before_stop": conclusion_id is not None,
        "manual_review": True,
        "review_reason": str(review["reason"]),
    }


def executable_candidates(name: str) -> list[Path]:
    candidates: list[Path] = []
    located = shutil.which(name)
    if located:
        candidates.append(Path(located))
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["where.exe", name],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            candidates.extend(
                Path(line.strip())
                for line in result.stdout.splitlines()
                if line.strip()
            )
        except Exception:
            pass
        candidates.extend(
            Path(value)
            for value in glob.glob(
                f"C:/texlive/*/bin/windows/{name}.exe", recursive=False
            )
        )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen and candidate.exists():
            seen.add(key)
            unique.append(candidate)
    return unique


def find_working_executable(name: str) -> Path | None:
    for candidate in executable_candidates(name):
        try:
            result = subprocess.run(
                [str(candidate), "-v"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                return candidate
        except Exception:
            continue
    return None


def create_layout_text(source: Path, output: Path, document: pymupdf.Document) -> str:
    executable = find_working_executable("pdftotext")
    if executable is not None:
        result = subprocess.run(
            [
                str(executable),
                "-layout",
                "-enc",
                "UTF-8",
                str(source),
                str(output),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode == 0 and output.exists():
            return str(executable)
    fallback = "\n\f\n".join(page.get_text("text", sort=True) for page in document)
    output.write_text(fallback, encoding="utf-8")
    return "pymupdf-fallback"


def render_source_previews(
    document: pymupdf.Document, preview_dir: Path, dpi: int, page_count: int
) -> None:
    preview_dir.mkdir(parents=True, exist_ok=True)
    scale = dpi / 72.0
    for page_index in range(page_count):
        pixmap = document[page_index].get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), alpha=False
        )
        pixmap.save(preview_dir / f"page-{page_index + 1:04d}.png")


def build_math_review(
    elements: list[dict[str, Any]],
    source_sha256: str,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a hash-bound native-math review gate without losing decisions."""
    previous_entries = (
        dict(existing.get("entries") or {})
        if isinstance(existing, dict)
        and existing.get("source_sha256") == source_sha256
        else {}
    )
    entries: dict[str, dict[str, Any]] = {}
    translatable_orders = [
        int(element["order"])
        for element in elements
        if element.get("translatable") is True
    ]
    translation_start = (
        min(translatable_orders) if translatable_orders else None
    )
    translation_stop = (
        max(translatable_orders) if translatable_orders else None
    )

    for element in elements:
        if (
            element.get("translatable") is False
            or element.get("render_suppressed")
        ):
            continue
        for token, fragment in dict(
            element.get("inline_fragments") or {}
        ).items():
            if fragment.get("kind") != "math":
                continue
            fragment["display"] = False
            fragment["source_page"] = int(element.get("page") or 0)
            status = str(fragment.get("review_status") or "unresolved")
            previous = dict(previous_entries.get(token) or {})
            if (
                previous
                and (
                    int(previous.get("source_page") or 0)
                    != int(element.get("page") or 0)
                    or str(previous.get("source_text") or "")
                    != str(fragment.get("text") or "")
                )
            ):
                previous = {}
            entries[token] = {
                "kind": "inline",
                "display": False,
                "source_page": int(element.get("page") or 0),
                "source_bbox": fragment.get("source_bbox"),
                "source_text": str(fragment.get("text") or ""),
                "tex": str(previous.get("tex") or fragment.get("tex") or ""),
                "review_status": str(
                    previous.get("review_status") or status
                ),
            }

    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for element in sorted(elements, key=lambda item: int(item["order"])):
        order = int(element["order"])
        is_in_translation = (
            translation_start is None
            or translation_stop is None
            or translation_start <= order <= translation_stop
        )
        is_clip = (
            is_in_translation
            and not element.get("render_suppressed")
            and element.get("render_mode") == "source_clip"
            and element.get("kind") == "equation"
        )
        same_page = (
            current
            and int(current[-1]["page"]) == int(element.get("page") or 0)
        )
        same_visual_group = (
            current
            and str(current[-1].get("visual_id") or "")
            == str(element.get("visual_id") or "")
        )
        if is_clip and (not current or (same_page and same_visual_group)):
            current.append(element)
            continue
        if current:
            groups.append(current)
            current = []
        if is_clip:
            current = [element]
    if current:
        groups.append(current)

    for group in groups:
        if not any(item.get("kind") == "equation" for item in group):
            continue
        entry_id = str(group[0]["id"])
        source_box = pymupdf.Rect(group[0]["bbox"])
        for item in group[1:]:
            source_box |= pymupdf.Rect(item["bbox"])
        previous = dict(previous_entries.get(entry_id) or {})
        source_ids = [str(item["id"]) for item in group]
        source_text = " ".join(
            str(item.get("source_text") or "").strip()
            for item in group
            if str(item.get("source_text") or "").strip()
        )
        if previous and (
            list(previous.get("source_ids") or []) != source_ids
            or int(previous.get("source_page") or 0) != int(group[0]["page"])
            or str(previous.get("source_text") or "") != source_text
        ):
            previous = {}
        entries[entry_id] = {
            "kind": "display",
            "display": True,
            "source_page": int(group[0]["page"]),
            "source_bbox": rect_list(source_box),
            "source_ids": source_ids,
            "source_text": source_text,
            "tex": str(previous.get("tex") or ""),
            "review_status": str(
                previous.get("review_status") or "unresolved"
            ),
        }

    return {
        "schema_version": 1,
        "source_sha256": source_sha256,
        "math_profile": "native-lualatex-v1",
        "entries": entries,
    }


def link_footnote_anchors(
    elements: list[dict[str, Any]],
) -> dict[str, Any]:
    """Link each page-bottom footnote to the nearest matching superscript."""
    anchors: dict[tuple[int, str], list[tuple[int, dict[str, Any], str]]] = {}
    for element in elements:
        if not element.get("translatable"):
            continue
        for token, fragment in dict(
            element.get("inline_fragments") or {}
        ).items():
            if fragment.get("kind") != "footnote-marker":
                continue
            number = str(fragment.get("text") or "").strip()
            anchors.setdefault((int(element["page"]), number), []).append(
                (int(element["order"]), element, token)
            )

    linked = 0
    warnings: list[str] = []
    used: set[tuple[str, str]] = set()
    for footnote in elements:
        if footnote.get("kind") != "footnote":
            continue
        if not footnote.get("translatable"):
            continue
        text = str(footnote.get("source_text") or "")
        prefix = FOOTNOTE_PREFIX_RE.match(text)
        number = prefix.group("number") if prefix else ""
        footnote["footnote_number"] = number or None
        candidates = [
            candidate
            for candidate in anchors.get((int(footnote["page"]), number), [])
            if candidate[0] < int(footnote["order"])
            and (str(candidate[1]["id"]), candidate[2]) not in used
        ]
        if not candidates:
            warnings.append(
                f"{footnote['id']}: no same-page footnote anchor was found."
            )
            continue
        _, anchor, token = max(candidates, key=lambda item: item[0])
        footnote["anchor_id"] = str(anchor["id"])
        footnote["anchor_token"] = token
        fragment = dict(anchor["inline_fragments"][token])
        fragment["footnote_id"] = str(footnote["id"])
        anchor["inline_fragments"][token] = fragment
        used.add((str(anchor["id"]), token))
        linked += 1
    return {
        "footnotes": sum(
            element.get("kind") == "footnote" and element.get("translatable")
            for element in elements
        ),
        "linked": linked,
        "warnings": warnings,
    }


def visual_label(text: str) -> tuple[str, str] | None:
    match = VISUAL_LABEL_RE.search(text)
    if not match:
        return None
    raw_kind = str(
        match.group("latin_kind") or match.group("cjk_kind") or ""
    ).casefold()
    kind = "table" if raw_kind in {"table", "表"} else "figure"
    number = str(match.group("number")).upper()
    return kind, f"{kind}-{number}"


def source_body_font_size(elements: list[dict[str, Any]]) -> float:
    sizes = [
        float(element.get("font_size") or 0.0)
        for element in elements
        if element.get("kind") == "body"
        and float(element.get("font_size") or 0.0) > 0
        and element.get("render_mode") == "text"
    ]
    return statistics.median(sizes) if sizes else 10.0


def visual_internal_font_size(
    document: pymupdf.Document,
    page_number: int,
    visual_rect: pymupdf.Rect,
) -> float | None:
    """Return the character-weighted dominant font size inside a visual."""
    weighted: dict[float, int] = {}
    raw = document[page_number - 1].get_text(
        "dict", clip=visual_rect, sort=True
    )
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = re.sub(r"\s+", "", str(span.get("text") or ""))
                size = round(float(span.get("size") or 0.0), 2)
                if text and size > 0:
                    weighted[size] = weighted.get(size, 0) + len(text)
    if not weighted:
        return None
    return max(weighted.items(), key=lambda item: (item[1], item[0]))[0]


def _source_content_width(
    page_record: dict[str, Any],
    elements: list[dict[str, Any]],
) -> float:
    page_number = int(page_record["page"])
    widths = [
        pymupdf.Rect(element["bbox"]).width
        for element in elements
        if int(element["page"]) == page_number
        and element.get("render_mode") == "text"
        and element.get("kind") in {"body", "heading", "caption"}
        and pymupdf.Rect(element["bbox"]).width > 0
    ]
    return max(widths) if widths else float(page_record["width"]) * 0.86


def build_visual_layout(
    document: pymupdf.Document,
    elements: list[dict[str, Any]],
    page_records: list[dict[str, Any]],
    source_sha256: str,
) -> dict[str, Any]:
    """Build a hash-bound figure/table inventory and calibrated target sizes."""
    page_map = {int(record["page"]): record for record in page_records}
    body_size = source_body_font_size(elements)
    output_width = (
        595.276 - ZH_ACADEMIC_V1.margin_left - ZH_ACADEMIC_V1.margin_right
    )
    output_height = (
        841.89 - ZH_ACADEMIC_V1.margin_top - ZH_ACADEMIC_V1.margin_bottom
    )
    captions = [
        element
        for element in elements
        if element.get("kind") == "caption"
        and not element.get("render_suppressed")
        and visual_label(str(element.get("source_text") or ""))
    ]
    candidates = [
        element
        for element in elements
        if element.get("kind") in {"figure", "table"}
        and element.get("render_mode") == "source_clip"
    ]
    warnings: list[str] = []
    errors: list[str] = []
    label_counts = Counter(
        visual_label(str(caption.get("source_text") or ""))[1]
        for caption in captions
    )
    for label, count in sorted(label_counts.items()):
        if count != 1:
            errors.append(f"{label}: source caption occurs {count} times.")

    used: set[str] = set()
    visuals: list[dict[str, Any]] = []
    for caption in captions:
        caption_kind, label = visual_label(
            str(caption.get("source_text") or "")
        ) or ("figure", "")
        page_number = int(caption["page"])
        caption_rect = pymupdf.Rect(caption["bbox"])
        same_page = [
            candidate
            for candidate in candidates
            if str(candidate["id"]) not in used
            and int(candidate["page"]) == page_number
            and (
                candidate.get("kind") == caption_kind
                or caption_kind == "figure"
            )
        ]
        if not same_page:
            errors.append(f"{label}: caption has no source visual object.")
            continue
        visual = min(
            same_page,
            key=lambda item: min(
                abs(pymupdf.Rect(item["bbox"]).y1 - caption_rect.y0),
                abs(pymupdf.Rect(item["bbox"]).y0 - caption_rect.y1),
            ),
        )
        used.add(str(visual["id"]))
        visual_rect = pymupdf.Rect(visual["bbox"])
        visual_parts = [
            element
            for element in elements
            if int(element["page"]) == page_number
            and element.get("render_mode") == "source_clip"
            and element.get("kind") != "equation"
            and (
                element is visual
                or intersection_ratio(
                    pymupdf.Rect(element["bbox"]), visual_rect
                )
                >= 0.5
            )
        ]
        for part in visual_parts:
            visual_rect |= pymupdf.Rect(part["bbox"])
            if part in candidates:
                used.add(str(part["id"]))
        visual["bbox"] = rect_list(visual_rect)
        internal_size = visual_internal_font_size(
            document, page_number, visual_rect
        )
        source_width = _source_content_width(page_map[page_number], elements)
        if internal_size is not None:
            scale = ZH_ACADEMIC_V1.body_font_size / body_size
            target_internal = internal_size * scale
            basis = "internal-font-ratio"
            fallback = False
        else:
            scale = output_width / max(source_width, 1.0)
            target_internal = None
            basis = "source-width-ratio"
            fallback = True
            warnings.append(
                f"{label}: internal font size was unavailable; "
                "used source visual/content width ratio."
            )
        target_width = min(output_width, visual_rect.width * scale)
        target_height = target_width * visual_rect.height / max(
            visual_rect.width, 1.0
        )
        if target_height > output_height * 0.72:
            height_scale = output_height * 0.72 / target_height
            target_width *= height_scale
            target_height *= height_scale
            scale *= height_scale
            if target_internal is not None:
                target_internal *= height_scale
        visual_id = label
        entry = {
            "visual_id": visual_id,
            "label": label,
            "kind": caption_kind,
            "page": page_number,
            "source_ids": [str(part["id"]) for part in visual_parts],
            "caption_id": str(caption["id"]),
            "source_bbox": rect_list(visual_rect),
            "source_body_font_pt": round(body_size, 3),
            "source_internal_font_pt": (
                round(internal_size, 3) if internal_size is not None else None
            ),
            "target_internal_font_pt": (
                round(target_internal, 3)
                if target_internal is not None
                else None
            ),
            "target_width_pt": round(target_width, 3),
            "target_height_pt": round(target_height, 3),
            "target_scale": round(scale, 6),
            "aspect_ratio": round(
                visual_rect.width / max(visual_rect.height, 1.0), 6
            ),
            "scale_basis": basis,
            "automatic_fallback": fallback,
        }
        visuals.append(entry)
        for element in [*visual_parts, caption]:
            element["visual_id"] = visual_id
        visual["visual_layout"] = entry

    for candidate in candidates:
        if str(candidate["id"]) in used:
            continue
        candidate["visual_id"] = f"unlabelled-{candidate['id']}"
        errors.append(
            f"{candidate['id']}: source visual has no unique numbered caption."
        )
    return {
        "schema_version": 1,
        "source_sha256": source_sha256,
        "typography_profile": ZH_ACADEMIC_V1.name,
        "source_body_font_pt": round(body_size, 3),
        "output_body_font_pt": ZH_ACADEMIC_V1.body_font_size,
        "status": "pass" if not errors else "fail",
        "warnings": warnings,
        "errors": errors,
        "visuals": visuals,
    }


def main() -> int:
    args = parse_args()
    source = args.input.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise SystemExit(f"Input is not a PDF file: {source}")

    document = pymupdf.open(source)
    if document.needs_pass:
        raise SystemExit("Encrypted PDF requires a password; source was not changed.")
    page_count = len(document)
    process_count = (
        min(page_count, args.page_limit) if args.page_limit > 0 else page_count
    )
    title = document.metadata.get("title") or source.stem
    slug = slugify(title)
    work_dir = (
        args.work_dir.expanduser().resolve()
        if args.work_dir
        else (Path.cwd() / "tmp" / "pdfs" / slug).resolve()
    )
    work_dir.mkdir(parents=True, exist_ok=True)

    source_hash_before = sha256_file(source)
    render_source_previews(
        document, work_dir / "source-preview", args.render_dpi, process_count
    )
    layout_tool = create_layout_text(source, work_dir / "layout.txt", document)

    ocr_engine: Any | None = None
    all_elements: list[dict[str, Any]] = []
    page_records: list[dict[str, Any]] = []
    global_order = 0

    for page_index in range(process_count):
        page = document[page_index]
        page_number = page_index + 1
        scan_like = page_is_scan(page, args.ocr)
        if scan_like and args.ocr != "off":
            if ocr_engine is None:
                from rapidocr import RapidOCR

                ocr_engine = RapidOCR()
            elements, columns, confidence = extract_ocr_page(
                page, page_number, work_dir, args.ocr_dpi, ocr_engine
            )
            extraction_method = "rapidocr"
        else:
            elements, columns = extract_text_page(page, page_number)
            confidence = 1.0
            extraction_method = "pymupdf"

        for page_order, element in enumerate(elements, start=1):
            global_order += 1
            element["id"] = f"p{page_number:04d}-u{page_order:04d}"
            element["order"] = global_order
            element["page_order"] = page_order
            all_elements.append(element)

        page_records.append(
            {
                "page": page_number,
                "width": round(page.rect.width, 3),
                "height": round(page.rect.height, 3),
                "columns": columns,
                "scan_like": scan_like,
                "extraction_method": extraction_method,
                "ocr_confidence": round(confidence, 4),
                "element_count": len(elements),
                "needs_visual_review": scan_like and confidence < 0.85,
            }
        )

    repeated_headers = mark_repeated_running_text(all_elements, page_records)
    boundary = apply_translation_boundary(all_elements)
    review_path = (
        args.boundary_review.expanduser().resolve()
        if args.boundary_review
        else work_dir / "boundary-review.json"
    )
    if review_path.exists():
        try:
            review = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SystemExit(f"Cannot read boundary review: {error}") from error
        if review.get("source_sha256") != source_hash_before:
            if review.get("reviewed") is True:
                raise SystemExit(
                    "Reviewed boundary file belongs to a different source PDF."
                )
            review = make_boundary_review(
                all_elements, boundary, source_hash_before
            )
            json_dump(review_path, review)
        elif review.get("reviewed") is True:
            try:
                boundary = apply_reviewed_boundary(
                    all_elements, review, source_hash_before
                )
            except ValueError as error:
                raise SystemExit(f"Invalid boundary review: {error}") from error
    else:
        review_path.parent.mkdir(parents=True, exist_ok=True)
        json_dump(
            review_path,
            make_boundary_review(all_elements, boundary, source_hash_before),
        )
    math_review_path = work_dir / "math-review.json"
    existing_math_review: dict[str, Any] | None = None
    if math_review_path.exists():
        try:
            loaded_math_review = json.loads(
                math_review_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as error:
            raise SystemExit(f"Cannot read math review: {error}") from error
        if isinstance(loaded_math_review, dict):
            existing_math_review = loaded_math_review
    math_review = build_math_review(
        all_elements,
        source_hash_before,
        existing_math_review,
    )
    json_dump(math_review_path, math_review)
    footnote_links = link_footnote_anchors(all_elements)
    visual_layout = build_visual_layout(
        document,
        all_elements,
        page_records,
        source_hash_before,
    )
    visual_layout_path = work_dir / "visual-layout.json"
    json_dump(visual_layout_path, visual_layout)
    units_path = work_dir / "translation-units.jsonl"
    template_path = work_dir / "translations.template.jsonl"
    write_jsonl(units_path, all_elements)
    write_jsonl(
        template_path,
        (
            {
                "id": element["id"],
                "section": element["section"],
                "source_text": element["source_text"],
                "protected_text": element["protected_text"],
                "protected_tokens": element["protected_tokens"],
                "inline_fragments": element.get("inline_fragments", {}),
                "style_tokens": element.get("style_tokens", {}),
                "translated_text": "",
            }
            for element in all_elements
            if element["translatable"]
        ),
    )
    glossary_path = work_dir / "glossary.json"
    if not glossary_path.exists():
        json_dump(glossary_path, {})

    source_hash_after = sha256_file(source)
    if source_hash_before != source_hash_after:
        raise RuntimeError("Source PDF hash changed during preparation.")

    manifest = {
        "schema_version": 5,
        "created_by": "prepare_paper.py",
        "translation_engine": "active-codex-model-only",
        "source_pdf": str(source),
        "source_sha256": source_hash_before,
        "source_title": title,
        "source_metadata": document.metadata,
        "paper_slug": slug,
        "page_count": page_count,
        "processed_page_count": process_count,
        "partial_run": process_count != page_count,
        "pages": page_records,
        "boundary": boundary,
        "translatable_unit_count": sum(
            1 for element in all_elements if element["translatable"]
        ),
        "paths": {
            "work_dir": str(work_dir),
            "translation_units": units_path.name,
            "translations_template": template_path.name,
            "glossary": glossary_path.name,
            "layout_text": "layout.txt",
            "source_preview": "source-preview",
            "boundary_review": str(review_path),
            "math_review": math_review_path.name,
            "visual_layout": visual_layout_path.name,
        },
        "tools": {
            "layout_extractor": layout_tool,
            "page_renderer": f"pymupdf {pymupdf.VersionBind}",
            "ocr": "RapidOCR with ONNX Runtime"
            if any(page["extraction_method"] == "rapidocr" for page in page_records)
            else "not used",
            "repeated_running_text_ids": repeated_headers,
        },
        "warnings": [
            message
            for condition, message in (
                (
                    boundary["needs_boundary_review"],
                    "Translation boundaries require manual review.",
                ),
                (
                    process_count != page_count,
                    "This is a partial diagnostic run and must not be used for final output.",
                ),
                (
                    any(page["needs_visual_review"] for page in page_records),
                    "One or more OCR pages have confidence below 0.85.",
                ),
                (
                    any(
                        entry.get("review_status") == "unresolved"
                        for entry in math_review["entries"].values()
                    ),
                    "One or more mathematical fragments require native-TeX review.",
                ),
                (
                    bool(footnote_links["warnings"]),
                    "One or more footnotes lack a same-page source anchor.",
                ),
                (
                    bool(visual_layout["warnings"]),
                    "One or more visuals used source-width fallback sizing.",
                ),
                (
                    visual_layout["status"] != "pass",
                    "The source visual inventory has blocking errors.",
                ),
            )
            if condition
        ],
        "rich_math_profile": "native-lualatex-v1",
        "visual_inventory": {
            "status": visual_layout["status"],
            "count": len(visual_layout["visuals"]),
            "warnings": len(visual_layout["warnings"]),
            "errors": len(visual_layout["errors"]),
        },
        "footnotes": footnote_links,
    }
    json_dump(work_dir / "manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
