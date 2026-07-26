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
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image, ImageDraw, ImageOps
from typography import ZH_ACADEMIC_V1


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
            for block in page.get_text("dict", sort=True).get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
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
        metrics["math_fonts"] = dict(math_fonts)
        metrics["paragraphs_checked"] = sum(
            placement.get("flow_role") in {"paragraph-start", "list-item"}
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
        if not any(
            "lmroman" in normalize_font_name(name)
            or "latinmodernroman" in normalize_font_name(name)
            for name in latin_fonts
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


def validate_boundary_layout(
    source_document: pymupdf.Document,
    output_document: pymupdf.Document,
    layout: dict[str, Any] | None,
) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    metrics: dict[str, Any] = {
        "profile": None,
        "front_segments": 0,
        "tail_segments": 0,
        "segments_checked": 0,
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

    front_pages = [int(item.get("output_page") or 0) for item in front]
    tail_pages = [int(item.get("output_page") or 0) for item in tail]
    if front_pages != list(range(1, translation_start)):
        errors.append("Original front segments are not contiguous before the body.")
    if tail_pages != list(range(translation_end + 1, len(output_document) + 1)):
        errors.append("Original tail segments are not contiguous after the body.")
    placement_pages = {
        int(item.get("output_page") or 0)
        for item in list(layout.get("placements") or [])
    }
    boundary_pages = set(front_pages + tail_pages)
    if placement_pages & boundary_pages:
        errors.append("Translated flow content shares an original boundary page.")

    for segment in segments:
        source_page_index = int(segment.get("source_page") or 0) - 1
        output_page_index = int(segment.get("output_page") or 0) - 1
        if not (0 <= source_page_index < len(source_document)):
            errors.append(f"Boundary source page is invalid: {source_page_index + 1}")
            continue
        if not (0 <= output_page_index < len(output_document)):
            errors.append(f"Boundary output page is invalid: {output_page_index + 1}")
            continue
        source_rect = pymupdf.Rect(segment.get("source_bbox") or [])
        target_rect = pymupdf.Rect(segment.get("target_bbox") or [])
        if source_rect.is_empty or target_rect.is_empty:
            errors.append(
                f"Boundary segment has an empty rectangle: "
                f"source page {source_page_index + 1}"
            )
            continue
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
        return [f"Visual layout inventory could not be read: {error}"], warnings, {}
    if inventory.get("source_sha256") != manifest.get("source_sha256"):
        errors.append("Visual layout inventory source hash differs from manifest.")
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
    placements = {
        str(unit_id): item
        for item in (layout or {}).get("placements") or []
        for unit_id in item.get("ids") or [item.get("id")]
        if unit_id
    }
    checked = 0
    fallback_count = 0
    for visual_id, source_item in expected.items():
        rendered = actual.get(visual_id, [])
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
        page_number = int(item.get("output_page") or 0)
        if page_number < 1 or page_number > len(output_document):
            errors.append(f"{visual_id}: output page is invalid.")
        elif (
            bbox.x0 < ZH_ACADEMIC_V1.margin_left - 0.6
            or bbox.x1
            > output_document[page_number - 1].rect.width
            - ZH_ACADEMIC_V1.margin_right
            + 0.6
        ):
            errors.append(f"{visual_id}: rendered visual exceeds content width.")
        caption = placements.get(str(source_item.get("caption_id") or ""))
        if not caption:
            errors.append(f"{visual_id}: caption placement is missing.")
        elif int(caption.get("output_page") or 0) != page_number:
            errors.append(f"{visual_id}: visual and caption are on different pages.")
        source_internal = source_item.get("source_internal_font_pt")
        target_internal = source_item.get("target_internal_font_pt")
        if source_internal is not None and target_internal is not None:
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
    return errors, warnings, {
        "status": "pass" if not errors else "fail",
        "inventory_path": str(path),
        "expected": len(expected),
        "checked": checked,
        "automatic_fallbacks": fallback_count,
    }


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
        ):
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
    output_page_count = 0
    typography_metrics: dict[str, Any] = {}
    boundary_metrics: dict[str, Any] = {}
    visual_metrics: dict[str, Any] = {}
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
            if item.get("location") in {"page-bottom", "end-of-body"}
        }
        missing_footnotes = sorted(expected_footnote_ids - placed_footnotes)
        if missing_footnotes:
            errors.append(
                "Footnotes lack a bottom/end placement: "
                + ", ".join(missing_footnotes)
            )
    if translated.exists():
        try:
            output_document = pymupdf.open(translated)
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
                    source_document, output_document, layout
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
            finally:
                source_document.close()
            output_document.close()
        except Exception as error:
            errors.append(f"Translated PDF could not be opened: {error}")

    if PLACEHOLDER_RE.search(output_text):
        errors.append("Rendered PDF contains unresolved protected placeholders")
    if CONTROL_TOKEN_RE.search(output_text):
        errors.append("Rendered PDF contains unresolved rich-text tokens")
    for marker in ("\ufffd", "\u25a1", "\u25a0"):
        if marker in output_text:
            errors.append(f"Rendered PDF text contains suspicious glyph {marker!r}")

    normalized_output = normalize_text(output_text)
    invariant_missing: list[str] = []
    for unit in units:
        text = unit.get("source_text", "")
        if (
            unit.get("translatable")
            or unit.get("render_mode") != "text"
            or unit.get("kind") in {"header", "footer"}
            or len(normalize_text(text)) < 60
        ):
            continue
        if not invariant_present(text, normalized_output):
            invariant_missing.append(unit["id"])
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
            "invariant_text_misses": len(invariant_missing),
            "contact_sheet": str(contact_sheet) if contact_sheet.exists() else None,
            "typography": typography_metrics,
            "boundary_layout": boundary_metrics,
            "visual_layout": visual_metrics,
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
