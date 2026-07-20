#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "pymupdf>=1.26.0,<2",
# ]
# ///
"""Render Codex-authored translation records into a high-fidelity PDF.

The script restores protected source tokens, groups source-page continuations
into logical paragraphs, reflows text as vector PDF fragments, and embeds
preserved source regions with Page.show_pdf_page(). It performs no translation.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf
from typography import SUPPORTED_PROFILES, ZH_ACADEMIC_V1, TypographyProfile


HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
LIST_RE = re.compile(
    r"^\s*(?:[•●▪◦‣*-]|\(?\d{1,3}[.)、]|[（(]?[一二三四五六七八九十]+[)）、])\s*"
)
HEADING_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)[.)]?\s+")
PARAGRAPH_END_RE = re.compile(r"""[。！？.!?]["'”’）)\]]*\s*$""")
CONTROL_TOKEN_RE = re.compile(
    r"\[\[(?:[BIE][A-Za-z0-9_]*_(?:OPEN|CLOSE)|"
    r"F[A-Za-z0-9_]+|BR|PAR)\]\]"
)
LATIN_PROSE_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9]*(?:[-'’][A-Za-z0-9]+)*"
    r"(?:[ \u00a0]+[A-Za-z][A-Za-z0-9]*(?:[-'’][A-Za-z0-9]+)*)*"
)
AUTO_MATH_TOKEN_RE = re.compile(
    r"(?<![A-Za-z])(?:"
    r"q(?=-(?:CPDH|PKE|computational|power))"
    r"|aibi|bibi|ui|ai|bi|ci|di|gi|xi"
    r"|[abcdugxrijnm][0-9Nnijklmℓ]+"
    r"|[A-Z]"
    r"|[αβγδεζηθικλμνξοπρστυφχψωπ]"
    r"|[a-z]"
    r")(?![A-Za-z])"
)
TYPOGRAPHY_PROFILE = ZH_ACADEMIC_V1.name
PROFILE = ZH_ACADEMIC_V1


@dataclass(frozen=True)
class TypographyFonts:
    body: Path
    heading: Path
    latin_regular: Path
    latin_bold: Path
    latin_italic: Path
    math: Path | None
    exact_profile_fonts: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Render a paper from manifest and Codex-authored translations. "
            "This script performs no translation."
        )
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--translations", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--typography-profile",
        choices=sorted(SUPPORTED_PROFILES),
        default=TYPOGRAPHY_PROFILE,
    )
    parser.add_argument("--gutter", type=float, default=18.0)
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
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            records.append(value)
    return records


def load_translation_map(path: Path) -> dict[str, dict[str, Any]]:
    records = read_jsonl(path)
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = str(record.get("id", ""))
        if not record_id:
            raise ValueError(f"Translation record without id in {path}")
        if record_id in result:
            raise ValueError(f"Duplicate translation id: {record_id}")
        result[record_id] = record
    return result


def restore_tokens(element: dict[str, Any], translated_text: str) -> str:
    restored = translated_text
    for token, value in element.get("protected_tokens", {}).items():
        count = restored.count(token)
        if count != 1:
            raise ValueError(
                f"{element['id']}: protected token {token} occurs {count} times"
            )
        restored = restored.replace(token, value)
    if re.search(r"\[\[P\d{4}\]\]", restored):
        raise ValueError(f"{element['id']}: unresolved protected token")
    for token in element.get("inline_fragments", {}):
        count = restored.count(token)
        if count != 1:
            raise ValueError(
                f"{element['id']}: inline fragment {token} occurs {count} times"
            )
    for key in element.get("style_tokens", {}):
        open_token = f"[[{key}_OPEN]]"
        close_token = f"[[{key}_CLOSE]]"
        if restored.count(open_token) != 1 or restored.count(close_token) != 1:
            raise ValueError(
                f"{element['id']}: style token {key} must open and close once"
            )
        if restored.find(open_token) > restored.find(close_token):
            raise ValueError(f"{element['id']}: style token {key} is reversed")
    return restored


def find_first_font(candidates: list[Path]) -> Path | None:
    return next((path for path in candidates if path.exists()), None)


def kpsewhich_font(name: str) -> Path | None:
    executable = shutil.which("kpsewhich")
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except OSError:
        return None
    path = Path(result.stdout.strip()) if result.returncode == 0 else None
    return path if path and path.exists() else None


def find_typography_fonts() -> TypographyFonts:
    exact_body = Path("C:/Windows/Fonts/simsun.ttc")
    exact_heading = Path("C:/Windows/Fonts/simhei.ttf")
    latin_regular = kpsewhich_font("lmroman10-regular.otf")
    latin_bold = kpsewhich_font("lmroman10-bold.otf")
    latin_italic = kpsewhich_font("lmroman10-italic.otf")
    latin_math = kpsewhich_font("latinmodern-math.otf")
    if (
        exact_body.exists()
        and exact_heading.exists()
        and latin_regular
        and latin_bold
        and latin_italic
        and latin_math
    ):
        return TypographyFonts(
            exact_body,
            exact_heading,
            latin_regular,
            latin_bold,
            latin_italic,
            latin_math,
            True,
        )

    body = find_first_font(
        [
            Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/noto/NotoSerifCJK-Regular.ttc"),
            Path("/System/Library/Fonts/Supplemental/Songti.ttc"),
        ]
    )
    heading = find_first_font(
        [
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
            Path("/System/Library/Fonts/PingFang.ttc"),
        ]
    )
    if body is None or heading is None:
        raise RuntimeError(
            "The Chinese academic typography profile requires a CJK serif body font "
            "and CJK sans/black heading font. Install SimSun and SimHei on "
            "Windows, or Noto Serif CJK and Noto Sans CJK elsewhere."
        )
    latin_regular = latin_regular or find_first_font(
        [
            Path("/usr/share/fonts/opentype/lmodern/lmroman10-regular.otf"),
            Path("/usr/share/texmf/fonts/opentype/public/lm/lmroman10-regular.otf"),
            Path("/Library/Fonts/Latin Modern Roman.otf"),
            Path("C:/Windows/Fonts/times.ttf"),
        ]
    )
    latin_bold = latin_bold or find_first_font(
        [
            Path("/usr/share/fonts/opentype/lmodern/lmroman10-bold.otf"),
            Path("/usr/share/texmf/fonts/opentype/public/lm/lmroman10-bold.otf"),
            Path("/Library/Fonts/Latin Modern Roman Bold.otf"),
            Path("C:/Windows/Fonts/timesbd.ttf"),
        ]
    )
    latin_italic = latin_italic or find_first_font(
        [
            Path("/usr/share/fonts/opentype/lmodern/lmroman10-italic.otf"),
            Path("/usr/share/texmf/fonts/opentype/public/lm/lmroman10-italic.otf"),
            Path("/Library/Fonts/Latin Modern Roman Italic.otf"),
            Path("C:/Windows/Fonts/timesi.ttf"),
        ]
    )
    if not latin_regular or not latin_bold or not latin_italic:
        raise RuntimeError(
            "Latin Modern Roman or a compatible serif fallback is required."
        )
    math_font = find_first_font(
        [
            Path("/usr/share/fonts/opentype/lmodern-math/latinmodern-math.otf"),
            Path("/usr/share/texmf/fonts/opentype/public/lm-math/latinmodern-math.otf"),
            Path("/System/Library/Fonts/Supplemental/Cambria.ttf"),
            Path("C:/Windows/Fonts/cambria.ttc"),
        ]
    )
    return TypographyFonts(
        body,
        heading,
        latin_regular,
        latin_bold,
        latin_italic,
        latin_math or math_font,
        False,
    )


def fragment_math_metrics(fragment: dict[str, Any]) -> tuple[float, float]:
    runs = list(fragment.get("runs") or [])
    sized = [
        run for run in runs if float(run.get("size") or 0.0) > 0
    ]
    if not sized:
        return (
            float(fragment.get("height_em") or 1.18),
            float(fragment.get("vertical_align_em") or -0.20),
        )
    dominant = max(float(run["size"]) for run in sized)
    boxes = [
        pymupdf.Rect(run.get("bbox") or [0, 0, 0, 0])
        for run in sized
    ]
    union = pymupdf.Rect(boxes[0])
    for box in boxes[1:]:
        union |= box
    baselines = [
        float((run.get("origin") or [0.0, union.y1])[1])
        for run in sized
        if float(run["size"]) >= dominant * 0.88
    ]
    baseline = (
        sorted(baselines)[len(baselines) // 2]
        if baselines
        else union.y1
    )
    height_em = max(0.72, min(2.4, union.height / dominant))
    vertical_em = -max(0.0, min(0.55, (union.y1 - baseline) / dominant))
    return height_em, vertical_em


def fragment_tex(fragment: dict[str, Any]) -> str:
    explicit = str(fragment.get("tex") or "").strip()
    if explicit:
        return explicit
    markdown = str(fragment.get("markdown") or "").strip()
    if len(markdown) >= 2 and markdown.startswith("$") and markdown.endswith("$"):
        return markdown[1:-1].strip()
    return ""


def tex_is_safe_and_lossless(fragment: dict[str, Any], tex: str) -> bool:
    if not tex or len(tex) > 700:
        return False
    if any(
        forbidden in tex
        for forbidden in (
            r"\begin",
            r"\end",
            r"\input",
            r"\include",
            r"\write",
            r"\openout",
            r"\read",
            r"\catcode",
        )
    ):
        return False
    depth = 0
    for char in tex:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                return False
    if depth != 0 or "$" in tex:
        return False
    if not re.search(r"[A-Za-z0-9Α-ω∑∏¬∧∨∈≤≥=<>]", tex):
        return False
    explicit_reconstruction = bool(
        fragment.get("reconstructed")
        and fragment.get("render_strategy") == "tex-vector"
    )
    if not explicit_reconstruction and re.search(r"\^\{[^{}]*\}\s*\^\{", tex):
        return False
    if fragment.get("render_strategy") == "source-vector":
        return False
    runs = list(fragment.get("runs") or [])
    if not explicit_reconstruction and any(
        re.search(r"(?:CMEX|CMSY|MSAM|MSBM)", str(run.get("font") or ""), re.I)
        for run in runs
    ):
        return False
    return True


def render_tex_math_svg(tex: str, asset_dir: Path, digest: str) -> Path | None:
    lualatex = shutil.which("lualatex")
    dvisvgm = shutil.which("dvisvgm")
    pdftocairo = shutil.which("pdftocairo")
    if not lualatex or not (pdftocairo or dvisvgm):
        return None
    base = f"{digest}-tex"
    svg_path = asset_dir / f"{base}.svg"
    failure_path = asset_dir / f"{base}.failed"
    if svg_path.exists():
        return svg_path
    if failure_path.exists() and not pdftocairo:
        return None
    failure_path.unlink(missing_ok=True)
    tex_path = asset_dir / f"{base}.tex"
    pdf_path = asset_dir / f"{base}.pdf"
    tex_path.write_text(
        "\n".join(
            (
                r"\documentclass[preview,border=0.15pt]{standalone}",
                r"\usepackage{unicode-math}",
                r"\setmathfont{Latin Modern Math}",
                r"\begin{document}",
                rf"\(\displaystyle {tex}\)",
                r"\end{document}",
                "",
            )
        ),
        encoding="utf-8",
    )
    compile_result = subprocess.run(
        [
            lualatex,
            "--interaction=nonstopmode",
            "--halt-on-error",
            f"--output-directory={asset_dir}",
            str(tex_path),
        ],
        cwd=asset_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )
    if compile_result.returncode != 0 or not pdf_path.exists():
        failure_path.write_text(
            compile_result.stdout[-2000:],
            encoding="utf-8",
        )
        for suffix in (".aux", ".log", ".tex", ".pdf"):
            candidate = asset_dir / f"{base}{suffix}"
            if candidate.exists():
                candidate.unlink()
        return None
    if pdftocairo:
        svg_command = [
            pdftocairo,
            "-svg",
            str(pdf_path),
            str(svg_path),
        ]
    else:
        svg_command = [
            dvisvgm,
            "--pdf",
            "--no-fonts",
            "--bbox=min",
            "-o",
            str(svg_path),
            str(pdf_path),
        ]
    svg_result = subprocess.run(
        svg_command,
        cwd=asset_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        check=False,
    )
    if svg_result.returncode != 0 or not svg_path.exists():
        failure_path.write_text(
            svg_result.stdout[-1000:] + "\n" + svg_result.stderr[-1000:],
            encoding="utf-8",
        )
        for suffix in (".aux", ".log", ".tex", ".pdf"):
            candidate = asset_dir / f"{base}{suffix}"
            if candidate.exists():
                candidate.unlink()
        return None
    for suffix in (".aux", ".log", ".tex", ".pdf"):
        candidate = asset_dir / f"{base}{suffix}"
        if candidate.exists():
            candidate.unlink()
    return svg_path


def render_source_math_svg(
    source: pymupdf.Document,
    element: dict[str, Any],
    fragment: dict[str, Any],
    asset_dir: Path,
    digest: str,
) -> Path | None:
    runs = list(fragment.get("runs") or [])
    boxes = [
        pymupdf.Rect(run.get("bbox") or [0, 0, 0, 0])
        for run in runs
        if pymupdf.Rect(run.get("bbox") or [0, 0, 0, 0]).get_area() > 0
    ]
    explicit_box = fragment.get("source_bbox")
    if explicit_box:
        boxes.append(pymupdf.Rect(explicit_box))
    if not boxes:
        return None
    clip = pymupdf.Rect(boxes[0])
    for box in boxes[1:]:
        clip |= box
    clip.x0 -= 0.35
    clip.y0 -= 0.35
    clip.x1 += 0.35
    clip.y1 += 0.35
    source_page = int(fragment.get("source_page") or element.get("page") or 0)
    if not (1 <= source_page <= len(source)):
        return None
    svg_path = asset_dir / f"{digest}-source.svg"
    if svg_path.exists():
        return svg_path
    temporary = pymupdf.open()
    try:
        page = temporary.new_page(width=clip.width, height=clip.height)
        page.show_pdf_page(
            page.rect,
            source,
            source_page - 1,
            clip=clip,
            keep_proportion=True,
        )
        svg_path.write_text(
            page.get_svg_image(text_as_path=True),
            encoding="utf-8",
        )
    finally:
        temporary.close()
    return svg_path


def font_math_is_safe_and_lossless(
    fragment: dict[str, Any], tex: str
) -> bool:
    return bool(
        fragment.get("html")
        and re.fullmatch(r"[A-Za-zΑ-Ωα-ω⋆¬∧∨∈≤≥≠≈=<>]", tex)
    )


def prepare_math_assets(
    source: pymupdf.Document,
    elements: list[dict[str, Any]],
    asset_dir: Path,
) -> Counter[str]:
    asset_dir.mkdir(parents=True, exist_ok=True)
    strategies: Counter[str] = Counter()
    for element in elements:
        for fragment in dict(element.get("inline_fragments") or {}).values():
            if fragment.get("kind") != "math":
                continue
            tex = fragment_tex(fragment)
            requested = str(fragment.get("render_strategy") or "auto")
            fragment.pop("asset_path", None)
            if requested != "source-vector" and font_math_is_safe_and_lossless(
                fragment, tex
            ):
                fragment["resolved_render_strategy"] = "font-vector"
                strategies["font-vector"] += 1
                continue
            digest = hashlib.sha256(
                (
                    str(element.get("page"))
                    + "\0"
                    + str(fragment.get("text") or "")
                    + "\0"
                    + tex
                    + "\0"
                    + requested
                ).encode("utf-8")
            ).hexdigest()[:24]
            asset: Path | None = None
            strategy = "source-vector"
            if requested != "source-vector" and tex_is_safe_and_lossless(
                fragment, tex
            ):
                asset = render_tex_math_svg(tex, asset_dir, digest)
                if asset:
                    strategy = "tex-vector"
            if asset is None:
                asset = render_source_math_svg(
                    source, element, fragment, asset_dir, digest
                )
            if asset is None:
                strategy = "legacy-html"
            else:
                fragment["asset_path"] = str(asset)
            height_em, vertical_em = fragment_math_metrics(fragment)
            fragment["height_em"] = height_em
            fragment["vertical_align_em"] = vertical_em
            fragment["resolved_render_strategy"] = strategy
            strategies[strategy] += 1
    return strategies


def heading_level(element: dict[str, Any]) -> int:
    match = HEADING_NUMBER_RE.match(str(element.get("source_text") or ""))
    return match.group(1).count(".") + 1 if match else 1


def normalize_heading_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    match = re.match(r"^(\d+(?:\.\d+)*)[.)]?[ \u00a0\u3000]+(.+)$", text)
    if match:
        return f"{match.group(1)}\u3000{match.group(2).strip()}"
    return text


def normalize_body_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(
        r"\s*\n+\s*(?=\[\[[BI][A-Za-z0-9_]*_OPEN\]\])",
        "[[PAR]]",
        text,
    )
    text = re.sub(r"(?<=[A-Za-z])-\s*\n+\s*(?=[a-z])", "", text)
    text = re.sub(
        r"(?<=[\u3400-\u9fff])\s*\n+\s*"
        r"(?=[\u3400-\u9fff\d，。；：！？、）])",
        "",
        text,
    )
    text = re.sub(
        r"(?<=\d)\s*\n+\s*(?=[\u3400-\u9fff])",
        "",
        text,
    )
    text = re.sub(r"\s*\n+\s*", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def flow_role(element: dict[str, Any], text: str) -> str:
    if element.get("kind") == "heading":
        return "heading"
    if element.get("translatable") and element.get("kind") == "body":
        source = str(element.get("source_text") or "")
        if LIST_RE.match(text) or LIST_RE.match(source):
            return "list-item"
        return "paragraph-start"
    if element.get("translatable") and element.get("kind") == "footnote":
        return "footnote"
    return "invariant-text"


def is_logical_continuation(
    previous_node: dict[str, Any], element: dict[str, Any], role: str
) -> bool:
    if role != "paragraph-start":
        return False
    if previous_node.get("flow_role") != "paragraph-start":
        return False
    if previous_node.get("render_mode") != "text":
        return False
    previous_element = previous_node["elements"][-1]
    if not previous_element.get("translatable"):
        return False
    if str(previous_element.get("section") or "") != str(
        element.get("section") or ""
    ):
        return False
    previous_page = int(previous_element.get("page") or 0)
    current_page = int(element.get("page") or 0)
    previous_source = str(previous_element.get("source_text") or "").strip()
    if not previous_source or PARAGRAPH_END_RE.search(previous_source):
        return False
    if current_page == previous_page + 1:
        return True
    if current_page != previous_page:
        return False
    previous_rect = pymupdf.Rect(previous_element.get("bbox") or [0, 0, 0, 0])
    current_rect = pymupdf.Rect(element.get("bbox") or [0, 0, 0, 0])
    gap = current_rect.y0 - previous_rect.y1
    font_size = max(
        float(previous_element.get("font_size") or 10.0),
        float(element.get("font_size") or 10.0),
    )
    same_left_edge = abs(current_rect.x0 - previous_rect.x0) <= font_size * 2.0
    return -font_size * 0.5 <= gap <= font_size * 2.2 and same_left_edge


def join_continuation(left: str, right: str) -> str:
    left = left.rstrip()
    right = right.lstrip()
    if not left:
        return right
    if not right:
        return left
    if left.endswith("-") and re.match(r"^[a-z]", right):
        return left[:-1] + right
    if HAN_RE.search(left[-1]) and (
        HAN_RE.search(right[0]) or right[0].isdigit() or right[0] in "，。；：！？、）"
    ):
        return left + right
    return left + " " + right


def build_flow_nodes(
    elements: list[dict[str, Any]], rendered_text: dict[str, str]
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for element in sorted(elements, key=lambda item: int(item["order"])):
        if element.get("kind") in {"header", "footer"}:
            continue
        if element.get("render_mode") == "source_clip":
            if (
                nodes
                and nodes[-1].get("render_mode") == "source_clip"
                and int(nodes[-1]["element"]["page"]) == int(element["page"])
            ):
                previous = nodes[-1]
                union = pymupdf.Rect(previous["element"]["bbox"])
                union |= pymupdf.Rect(element["bbox"])
                previous["element"]["bbox"] = [
                    round(float(value), 3)
                    for value in (union.x0, union.y0, union.x1, union.y1)
                ]
                previous["element"]["kind"] = "composite-source-clip"
                previous["ids"].append(element["id"])
                previous["elements"].append(element)
                previous["composite_parts"] = len(previous["ids"])
                continue
            composite_element = dict(element)
            nodes.append(
                {
                    "id": element["id"],
                    "ids": [element["id"]],
                    "elements": [element],
                    "element": composite_element,
                    "render_mode": "source_clip",
                    "flow_role": "source-clip",
                    "composite_parts": 1,
                }
            )
            continue

        raw_text = str(rendered_text[element["id"]])
        text = (
            normalize_heading_text(raw_text)
            if element.get("kind") == "heading"
            else normalize_body_text(raw_text)
        )
        role = flow_role(element, text)
        if nodes and is_logical_continuation(nodes[-1], element, role):
            previous = nodes[-1]
            previous["text"] = join_continuation(previous["text"], text)
            previous["ids"].append(element["id"])
            previous["elements"].append(element)
            previous["joined_continuations"] = len(previous["ids"]) - 1
            continue

        nodes.append(
            {
                "id": element["id"],
                "ids": [element["id"]],
                "elements": [element],
                "element": element,
                "render_mode": "text",
                "flow_role": role,
                "text": text,
                "joined_continuations": 0,
            }
        )
    return nodes


def partition_translation_body(
    elements: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split source-original front/tail material from the translated body."""
    ordered = sorted(elements, key=lambda item: int(item["order"]))
    positions = {str(element["id"]): index for index, element in enumerate(ordered)}
    boundary = manifest.get("boundary") or {}
    start_id = str(boundary.get("introduction_id") or "")
    stop_id = str(boundary.get("post_body_stop_id") or "")
    if start_id not in positions:
        raise ValueError("Translation start ID is missing from source elements.")
    start_index = positions[start_id]
    stop_index = positions.get(stop_id, len(ordered)) if stop_id else len(ordered)
    if stop_index <= start_index:
        raise ValueError("Translation stop boundary precedes the start boundary.")

    body_elements = ordered[start_index:stop_index]
    start_element = ordered[start_index]
    stop_element = ordered[stop_index] if stop_index < len(ordered) else None
    page_records = {
        int(page["page"]): page for page in list(manifest.get("pages") or [])
    }
    source_page_count = int(manifest.get("page_count") or len(page_records))

    def page_geometry(page_number: int) -> tuple[float, float]:
        page = page_records.get(page_number)
        if not page:
            raise ValueError(f"Missing geometry for source page {page_number}.")
        return float(page["width"]), float(page["height"])

    front_segments: list[dict[str, Any]] = []
    start_page = int(start_element["page"])
    for page_number in range(1, start_page):
        width, height = page_geometry(page_number)
        front_segments.append(
            {
                "role": "front-original",
                "mode": "full-page",
                "source_page": page_number,
                "source_bbox": [0.0, 0.0, width, height],
                "target_top": 0.0,
            }
        )
    start_width, start_height = page_geometry(start_page)
    start_y = float(start_element["bbox"][1])
    if start_y > start_height * 0.22:
        front_segments.append(
            {
                "role": "front-original",
                "mode": "partial-page",
                "source_page": start_page,
                "source_bbox": [
                    0.0,
                    0.0,
                    start_width,
                    min(start_height, max(1.0, start_y - 8.0)),
                ],
                "target_top": 0.0,
            }
        )

    tail_segments: list[dict[str, Any]] = []
    if stop_element is not None:
        stop_page = int(stop_element["page"])
        stop_width, stop_height = page_geometry(stop_page)
        stop_y = float(stop_element["bbox"][1])
        if stop_y <= stop_height * 0.22:
            first_tail_bbox = [0.0, 0.0, stop_width, stop_height]
            first_tail_mode = "full-page"
            target_top = 0.0
        else:
            first_tail_bbox = [
                0.0,
                max(0.0, stop_y - 12.0),
                stop_width,
                stop_height,
            ]
            first_tail_mode = "partial-page"
            target_top = 42.0
        tail_segments.append(
            {
                "role": "tail-original",
                "mode": first_tail_mode,
                "source_page": stop_page,
                "source_bbox": first_tail_bbox,
                "target_top": target_top,
            }
        )
        for page_number in range(stop_page + 1, source_page_count + 1):
            width, height = page_geometry(page_number)
            tail_segments.append(
                {
                    "role": "tail-original",
                    "mode": "full-page",
                    "source_page": page_number,
                    "source_bbox": [0.0, 0.0, width, height],
                    "target_top": 0.0,
                }
            )
    return body_elements, front_segments, tail_segments


def expand_composite_source_clip(
    source_page: pymupdf.Page,
    clip: pymupdf.Rect,
    composite_parts: int,
) -> pymupdf.Rect:
    """Include vector artwork that extends beyond extracted text/table boxes."""
    if composite_parts <= 1:
        return clip

    page_rect = source_page.rect
    expanded_x0 = clip.x0
    expanded_x1 = clip.x1
    for drawing in source_page.get_drawings():
        drawing_rect = pymupdf.Rect(drawing["rect"])
        vertical_overlap = min(clip.y1, drawing_rect.y1) - max(
            clip.y0, drawing_rect.y0
        )
        horizontal_overlap = min(clip.x1, drawing_rect.x1) - max(
            clip.x0, drawing_rect.x0
        )
        if vertical_overlap <= 0 or horizontal_overlap <= 0:
            continue
        if drawing_rect.width >= page_rect.width * 0.95:
            continue
        expanded_x0 = min(expanded_x0, drawing_rect.x0)
        expanded_x1 = max(expanded_x1, drawing_rect.x1)

    padding = 2.0
    return pymupdf.Rect(
        max(page_rect.x0, expanded_x0 - padding),
        max(page_rect.y0, clip.y0 - padding),
        min(page_rect.x1, expanded_x1 + padding),
        min(page_rect.y1, clip.y1 + padding),
    )


def style_for(node: dict[str, Any]) -> dict[str, Any]:
    element = node["element"]
    kind = element["kind"]
    role = node["flow_role"]
    level = heading_level(element) if kind == "heading" else None
    if kind == "title":
        font_size = min(
            19.0, max(15.0, float(element.get("font_size") or 15.0))
        )
        return {
            "font_family": "serif",
            "font_size": font_size,
            "line_height": font_size * 1.25,
            "letter_spacing": 0.0,
            "alignment": "center",
            "text_indent": 0.0,
            "padding_left": 0.0,
            "margin_before": 0.0,
            "margin_after": 5.0,
            "heading_level": None,
            "keep_with_next": False,
        }
    if kind == "front-matter":
        font_size = min(
            11.0, max(9.0, float(element.get("font_size") or 9.0))
        )
        return {
            "font_family": "serif",
            "font_size": font_size,
            "line_height": font_size * 1.3,
            "letter_spacing": 0.0,
            "alignment": "center",
            "text_indent": 0.0,
            "padding_left": 0.0,
            "margin_before": 0.0,
            "margin_after": 2.0,
            "heading_level": None,
            "keep_with_next": False,
        }
    if kind == "heading":
        main = level == 1
        return {
            "font_family": "PaperHeading",
            "font_size": (
                PROFILE.heading_level_one_size
                if main
                else PROFILE.heading_lower_size
            ),
            "line_height": (
                PROFILE.heading_level_one_line_pitch
                if main
                else PROFILE.heading_lower_line_pitch
            ),
            "letter_spacing": 0.0,
            "alignment": "left",
            "text_indent": 0.0,
            "padding_left": 0.0,
            "margin_before": (
                PROFILE.heading_level_one_before
                if main
                else PROFILE.heading_lower_before
            ),
            "margin_after": (
                PROFILE.heading_level_one_after
                if main
                else PROFILE.heading_lower_after
            ),
            "heading_level": level,
            "keep_with_next": True,
        }
    if role == "paragraph-start":
        return {
            "font_family": "PaperBody",
            "font_size": PROFILE.body_font_size,
            "line_height": PROFILE.body_line_pitch,
            "letter_spacing": PROFILE.body_letter_spacing,
            "alignment": "justify",
            "text_indent": PROFILE.body_text_indent,
            "padding_left": 0.0,
            "margin_before": 0.0,
            "margin_after": PROFILE.paragraph_spacing,
            "heading_level": None,
            "keep_with_next": False,
        }
    if role == "list-item":
        return {
            "font_family": "PaperBody",
            "font_size": PROFILE.body_font_size,
            "line_height": PROFILE.body_line_pitch,
            "letter_spacing": PROFILE.body_letter_spacing,
            "alignment": "justify",
            "text_indent": PROFILE.list_text_indent,
            "padding_left": PROFILE.list_padding_left,
            "margin_before": 0.0,
            "margin_after": PROFILE.list_spacing,
            "heading_level": None,
            "keep_with_next": False,
        }
    if role == "footnote":
        return {
            "font_family": "PaperBody",
            "font_size": 8.5,
            "line_height": 11.5,
            "letter_spacing": 0.0,
            "alignment": "justify",
            "text_indent": 0.0,
            "padding_left": 0.0,
            "margin_before": 0.0,
            "margin_after": 0.0,
            "heading_level": None,
            "keep_with_next": False,
        }
    if kind == "caption":
        font_size = 8.2
    elif kind in {"abstract-heading", "keywords"}:
        font_size = min(
            11.0, max(9.2, float(element.get("font_size") or 9.2))
        )
    else:
        font_size = min(10.2, max(9.0, float(element.get("font_size") or 10.0)))
    return {
        "font_family": "serif",
        "font_size": font_size,
        "line_height": font_size * 1.3,
        "letter_spacing": 0.0,
        "alignment": "left" if kind == "caption" else "justify",
        "text_indent": 0.0,
        "padding_left": 0.0,
        "margin_before": 0.0,
        "margin_after": 2.0,
        "heading_level": None,
        "keep_with_next": kind in {"abstract-heading", "keywords"},
    }


def make_font_archive(
    fonts: TypographyFonts, node: dict[str, Any] | None = None
) -> pymupdf.Archive:
    archive = pymupdf.Archive(str(fonts.body.parent))
    font_parents = {
        fonts.heading.parent,
        fonts.latin_regular.parent,
        fonts.latin_bold.parent,
        fonts.latin_italic.parent,
    }
    if fonts.math:
        font_parents.add(fonts.math.parent)
    for parent in sorted(font_parents, key=str):
        if parent != fonts.body.parent:
            archive.add(str(parent))
    if node is not None:
        fragments, _ = node_rich_metadata(node)
        asset_parents = {
            Path(str(fragment["asset_path"])).parent
            for fragment in fragments.values()
            if fragment.get("asset_path")
        }
        for parent in sorted(asset_parents, key=str):
            archive.add(str(parent))
    return archive


def node_rich_metadata(
    node: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    fragments: dict[str, dict[str, Any]] = {}
    styles: dict[str, str] = {}
    for element in node.get("elements") or [node["element"]]:
        fragments.update(dict(element.get("inline_fragments") or {}))
        styles.update(dict(element.get("style_tokens") or {}))
    return fragments, styles


def auto_math_markup(token: str) -> str:
    if token in {"aibi", "bibi"}:
        return (
            f"<i class='math-var'>{token[0]}</i><sub>i</sub>"
            f"<i class='math-var'>{token[2]}</i><sub>i</sub>"
        )
    match = re.fullmatch(r"([abcdugxrijnm])([0-9Nnijklmℓ]+)", token)
    if match:
        return (
            f"<i class='math-var'>{match.group(1)}</i>"
            f"<sub>{html.escape(match.group(2))}</sub>"
        )
    return f"<i class='math-var'>{html.escape(token)}</i>"


def prose_text_to_html(value: str) -> str:
    output: list[str] = []
    cursor = 0
    for match in LATIN_PROSE_RE.finditer(value):
        output.append(html.escape(value[cursor : match.start()]))
        output.append(
            "<span class='latin'>"
            + html.escape(match.group(0))
            + "</span>"
        )
        cursor = match.end()
    output.append(html.escape(value[cursor:]))
    return "".join(output)


def plain_text_to_html(value: str) -> str:
    output: list[str] = []
    cursor = 0
    for match in AUTO_MATH_TOKEN_RE.finditer(value):
        output.append(prose_text_to_html(value[cursor : match.start()]))
        output.append(
            "<span class='math auto-math'>"
            + auto_math_markup(match.group(0))
            + "</span>"
        )
        cursor = match.end()
    output.append(prose_text_to_html(value[cursor:]))
    return "".join(output)


def rich_text_to_html(node: dict[str, Any]) -> str:
    fragments, styles = node_rich_metadata(node)
    output: list[str] = []
    cursor = 0
    for match in CONTROL_TOKEN_RE.finditer(str(node["text"])):
        output.append(
            plain_text_to_html(str(node["text"])[cursor : match.start()])
        )
        token = match.group(0)
        if token == "[[BR]]":
            output.append("<br>")
        elif token == "[[PAR]]":
            output.append(
                "</div><div class='fragment semantic-paragraph'>"
            )
        elif token in fragments:
            fragment = fragments[token]
            kind = str(fragment.get("kind") or "")
            if kind == "citation":
                if output:
                    output[-1] = re.sub(r"\s+$", "", output[-1])
                output.append(
                    "<sup class='citation'>"
                    + html.escape(str(fragment.get("text") or ""))
                    + "</sup>"
                )
            elif kind == "footnote-marker":
                output.append(
                    "<sup class='footnote-marker'>"
                    + html.escape(str(fragment.get("text") or ""))
                    + "</sup>"
                )
            elif kind == "math":
                if fragment.get("asset_path"):
                    asset_name = Path(str(fragment["asset_path"])).name
                    height_em = float(fragment.get("height_em") or 1.05)
                    vertical_em = float(fragment.get("vertical_align_em") or -0.18)
                    output.append(
                        "<span class='math math-vector'>"
                        f"<img src='{html.escape(asset_name)}' "
                        f"style='height:{height_em:.4f}em;"
                        "width:auto;"
                        f"vertical-align:{vertical_em:.4f}em' />"
                        "</span>"
                    )
                else:
                    math_html = str(fragment.get("html") or "")
                    output.append(f"<span class='math'>{math_html}</span>")
            else:
                output.append(html.escape(str(fragment.get("text") or "")))
        else:
            style_match = re.fullmatch(
                r"\[\[([BIE][A-Za-z0-9_]*)_(OPEN|CLOSE)\]\]", token
            )
            if not style_match or style_match.group(1) not in styles:
                output.append(html.escape(token))
            else:
                kind = styles[style_match.group(1)]
                edge = style_match.group(2)
                tag = "strong" if kind == "bold" else "em"
                output.append(f"<{tag}>" if edge == "OPEN" else f"</{tag}>")
        cursor = match.end()
    output.append(plain_text_to_html(str(node["text"])[cursor:]))
    return "".join(output)


def fragment_payload(
    node: dict[str, Any], fonts: TypographyFonts
) -> tuple[dict[str, Any], str, str]:
    style = style_for(node)
    rich_html = rich_text_to_html(node)
    markup = f"<div class='fragment'>{rich_html}</div>"
    math_face = fonts.math.name if fonts.math else fonts.body.name
    block_italic = any(
        str(element.get("block_style") or "") == "italic"
        for element in node.get("elements") or [node["element"]]
    )
    css = f"""
        @font-face {{
          font-family: PaperBody;
          src: url('{fonts.body.name}');
        }}
        @font-face {{
          font-family: PaperHeading;
          src: url('{fonts.heading.name}');
        }}
        @font-face {{
          font-family: PaperLatin;
          src: url('{fonts.latin_regular.name}');
          font-weight: 400;
          font-style: normal;
        }}
        @font-face {{
          font-family: PaperLatin;
          src: url('{fonts.latin_bold.name}');
          font-weight: 700;
          font-style: normal;
        }}
        @font-face {{
          font-family: PaperLatin;
          src: url('{fonts.latin_italic.name}');
          font-weight: 400;
          font-style: italic;
        }}
        @font-face {{
          font-family: PaperMath;
          src: url('{math_face}');
        }}
        * {{ box-sizing: border-box; }}
        body {{ margin: 0; padding: 0; }}
        .fragment {{
          font-family: {style['font_family']};
          font-size: {style['font_size']}pt;
          line-height: {style['line_height']}pt;
          letter-spacing: {style['letter_spacing']}pt;
          text-align: {style['alignment']};
          font-weight: 400;
          font-style: {'italic' if block_italic else 'normal'};
          text-indent: {style['text_indent']}pt;
          padding: 0 0 0 {style['padding_left']}pt;
          color: #111;
          overflow-wrap: break-word;
          margin: 0;
        }}
        .fragment strong {{
          font-family: PaperHeading;
          font-weight: 700;
        }}
        .fragment em {{ font-style: italic; }}
        .fragment .latin {{ font-family: PaperLatin; }}
        .fragment strong .latin {{
          font-family: PaperLatin;
          font-weight: 700;
        }}
        .fragment em .latin {{
          font-family: PaperLatin;
          font-style: italic;
        }}
        .semantic-paragraph {{
          text-indent: {style['text_indent']}pt;
        }}
        .fragment .math {{
          font-family: PaperMath;
          font-style: normal;
          white-space: nowrap;
        }}
        .fragment .math-var {{
          font-family: PaperLatin;
          font-style: italic;
        }}
        .fragment .math sub, .fragment .math sup {{
          font-size: 72%;
          line-height: 0;
        }}
        .fragment .math-vector img {{
          display: inline;
          max-width: 100%;
        }}
        .fragment .citation, .fragment .footnote-marker {{
          font-family: PaperLatin;
          font-size: 72%;
          line-height: 0;
          vertical-align: super;
        }}
    """
    return style, markup, css


def fragment_document(
    node: dict[str, Any],
    width: float,
    fonts: TypographyFonts,
) -> tuple[pymupdf.Document, float, dict[str, Any]]:
    style, markup, css = fragment_payload(node, fonts)
    archive = make_font_archive(fonts, node)
    max_height = 4096.0
    document = pymupdf.open()
    page = document.new_page(width=width, height=max_height)
    spare_height, scale = page.insert_htmlbox(
        pymupdf.Rect(0, 0, width, max_height),
        markup,
        css=css,
        archive=archive,
        scale_low=1,
    )
    if spare_height < 0 or scale < 0.999:
        document.close()
        raise ValueError(f"{node['id']}: text fragment did not fit")
    used_height = max(6.0, max_height - spare_height + 1.0)
    return document, used_height, style


def draw_text_fragment(
    page: pymupdf.Page,
    node: dict[str, Any],
    target: pymupdf.Rect,
    fonts: TypographyFonts,
) -> None:
    _, markup, css = fragment_payload(node, fonts)
    spare_height, scale = page.insert_htmlbox(
        target,
        markup,
        css=css,
        archive=make_font_archive(fonts, node),
        scale_low=1,
    )
    if spare_height < -0.01 or scale < 0.999:
        raise ValueError(f"{node['id']}: direct text fragment did not fit")


def restore_verbatim_full_boundary_pages(
    output: Path,
    source_path: Path,
    segments: list[dict[str, Any]],
) -> None:
    """Replace full-page source copies after save to retain original text objects."""
    replacements = {
        int(segment["output_page"]) - 1: int(segment["source_page"]) - 1
        for segment in segments
        if segment.get("mode") == "full-page"
    }
    if not replacements:
        return
    rendered = pymupdf.open(output)
    source = pymupdf.open(source_path)
    rebuilt = pymupdf.open()
    try:
        for output_index in range(len(rendered)):
            source_index = replacements.get(output_index)
            if source_index is None:
                rebuilt.insert_pdf(
                    rendered, from_page=output_index, to_page=output_index
                )
            else:
                rebuilt.insert_pdf(
                    source, from_page=source_index, to_page=source_index
                )
        rebuilt.set_metadata(rendered.metadata)
        temporary = output.with_name(f"{output.stem}.boundary-fixed.pdf")
        rebuilt.ez_save(temporary)
    finally:
        rebuilt.close()
        source.close()
        rendered.close()
    temporary.replace(output)


def restore_verbatim_source_clips(
    output: Path,
    source_path: Path,
    placements: list[dict[str, Any]],
) -> None:
    """Re-overlay imported vector clips after text-font subsetting."""
    clips = [
        placement
        for placement in placements
        if placement.get("render_mode") == "source_clip"
    ]
    if not clips:
        return
    rendered = pymupdf.open(output)
    source = pymupdf.open(source_path)
    temporary = output.with_name(f"{output.stem}.clips-fixed.pdf")
    try:
        for placement in clips:
            page = rendered[int(placement["output_page"]) - 1]
            target = pymupdf.Rect(placement["bbox"])
            source_clip = pymupdf.Rect(placement["source_bbox"])
            page.draw_rect(
                target,
                color=None,
                fill=(1.0, 1.0, 1.0),
                width=0.0,
                overlay=True,
            )
            page.show_pdf_page(
                target,
                source,
                int(placement["source_page"]) - 1,
                clip=source_clip,
                keep_proportion=True,
                overlay=True,
            )
        rendered.ez_save(temporary)
    finally:
        source.close()
        rendered.close()
    temporary.replace(output)


class FlowRenderer:
    def __init__(
        self,
        source: pymupdf.Document,
        width: float,
        height: float,
        columns: int,
        gutter: float,
        fonts: TypographyFonts,
        profile: TypographyProfile,
    ) -> None:
        self.source = source
        self.width = width
        self.height = height
        self.columns = columns
        self.gutter = gutter
        self.fonts = fonts
        self.profile = profile
        self.margin_left = profile.margin_left
        self.margin_right = profile.margin_right
        self.margin_top = profile.margin_top
        self.margin_bottom = profile.margin_bottom
        self.content_width = width - self.margin_left - self.margin_right
        self.column_width = (
            self.content_width
            if columns == 1
            else (self.content_width - gutter) / 2
        )
        self.bottom = height - self.margin_bottom
        self.document = pymupdf.open()
        self.page: pymupdf.Page | None = None
        self.column = 0
        self.y = self.margin_top
        self.column_tops = [self.margin_top for _ in range(columns)]
        self.placements: list[dict[str, Any]] = []
        self.boundary_placements: list[dict[str, Any]] = []
        self.boundary_pages: set[int] = set()
        self.deferred_footnotes: list[dict[str, Any]] = []
        self.page_has_footnote = False
        self.new_page()

    def new_page(
        self, width: float | None = None, height: float | None = None
    ) -> None:
        self.page = self.document.new_page(
            width=width if width is not None else self.width,
            height=height if height is not None else self.height,
        )
        self.column = 0
        self.y = self.margin_top
        self.column_tops = [self.margin_top for _ in range(self.columns)]
        self.bottom = self.page.rect.height - self.margin_bottom
        self.page_has_footnote = False

    def place_boundary_segment(
        self, segment: dict[str, Any], start_new_page: bool
    ) -> None:
        source_page_number = int(segment["source_page"])
        source_page = self.source[source_page_number - 1]
        clip = pymupdf.Rect(segment["source_bbox"])
        target_top = float(segment.get("target_top") or 0.0)
        if start_new_page:
            self.new_page(source_page.rect.width, source_page.rect.height)
        assert self.page is not None
        target = pymupdf.Rect(
            0.0,
            target_top,
            clip.width,
            target_top + clip.height,
        )
        if target.x1 > self.page.rect.width + 0.1:
            raise ValueError(
                f"Boundary segment exceeds output page width: {source_page_number}"
            )
        if target.y1 > self.page.rect.height + 0.1:
            raise ValueError(
                f"Boundary segment exceeds output page height: {source_page_number}"
            )
        self.page.show_pdf_page(
            target,
            self.source,
            source_page_number - 1,
            clip=clip,
            keep_proportion=True,
        )
        output_page = self.page.number + 1
        self.boundary_pages.add(output_page)
        self.boundary_placements.append(
            {
                **segment,
                "output_page": output_page,
                "target_bbox": [
                    target.x0,
                    target.y0,
                    target.x1,
                    target.y1,
                ],
            }
        )

    def column_x(self) -> float:
        return self.margin_left + self.column * (self.column_width + self.gutter)

    def advance(self) -> None:
        if self.columns == 2 and self.column == 0:
            self.column = 1
            self.y = self.column_tops[1]
        else:
            self.new_page()

    def ensure_space(
        self, height: float, keep_with_next: bool = False
    ) -> None:
        reserve = 30.0 if keep_with_next else 0.0
        if self.y + height + reserve > self.bottom:
            self.advance()

    def measure_text_height(self, node: dict[str, Any]) -> float:
        element = node["element"]
        full_width = (
            self.columns == 2 and element["kind"] in {"title", "front-matter"}
        )
        width = self.content_width if full_width else self.column_width
        fragment, height, style = fragment_document(node, width, self.fonts)
        fragment.close()
        return height + float(style["margin_after"])

    def source_clip_geometry(
        self, node: dict[str, Any]
    ) -> tuple[pymupdf.Rect, float, float, bool]:
        element = node["element"]
        clip = pymupdf.Rect(element["bbox"])
        source_page = self.source[element["page"] - 1]
        clip = expand_composite_source_clip(
            source_page,
            clip,
            int(node.get("composite_parts") or 1),
        )
        if clip.is_empty or clip.width <= 0 or clip.height <= 0:
            return clip, 0.0, 0.0, False
        page_record_width = source_page.rect.width
        source_columns = 2 if clip.width < page_record_width * 0.72 else 1
        source_slot_width = (
            page_record_width * 0.46
            if source_columns == 2
            else page_record_width * 0.86
        )
        full_width = clip.width >= page_record_width * 0.72
        target_width = (
            self.content_width
            if full_width
            else min(
                self.column_width,
                max(24.0, clip.width * self.column_width / source_slot_width),
            )
        )
        target_height = target_width * clip.height / clip.width
        max_height = self.bottom - self.margin_top
        if target_height > max_height:
            scale = max_height / target_height
            target_width *= scale
            target_height *= scale
        return clip, target_width, target_height, full_width

    def measure_source_clip_height(self, node: dict[str, Any]) -> float:
        _, _, target_height, _ = self.source_clip_geometry(node)
        return target_height + 4.0

    def place_text(
        self, node: dict[str, Any], keep_with_height: float = 0.0
    ) -> None:
        element = node["element"]
        full_width = (
            self.columns == 2 and element["kind"] in {"title", "front-matter"}
        )
        fragment_width = self.content_width if full_width else self.column_width
        fragment, fragment_height, style = fragment_document(
            node, fragment_width, self.fonts
        )
        fragment.close()
        if full_width:
            if self.column != 0:
                self.new_page()
            if self.y + fragment_height > self.bottom:
                self.new_page()
        gap_before = (
            0.0
            if self.y <= self.margin_top + 0.1
            else float(style["margin_before"])
        )
        self.ensure_space(
            fragment_height
            + gap_before
            + float(style["margin_after"])
            + keep_with_height
            + (2.0 if keep_with_height else 0.0)
        )
        if self.y <= self.margin_top + 0.1:
            gap_before = 0.0
        self.y += gap_before
        assert self.page is not None
        target_x = self.margin_left if full_width else self.column_x()
        target = pymupdf.Rect(
            target_x,
            self.y,
            target_x + fragment_width,
            self.y + fragment_height,
        )
        draw_text_fragment(self.page, node, target, self.fonts)
        font_file = (
            self.fonts.body
            if style["font_family"] == "PaperBody"
            else self.fonts.heading
            if style["font_family"] == "PaperHeading"
            else None
        )
        self.placements.append(
            {
                "id": node["id"],
                "ids": node["ids"],
                "output_page": self.page.number + 1,
                "bbox": [target.x0, target.y0, target.x1, target.y1],
                "render_mode": "text",
                "text_embedding": "direct-htmlbox",
                "kind": element["kind"],
                "flow_role": node["flow_role"],
                "translatable": bool(element.get("translatable")),
                "joined_continuations": node["joined_continuations"],
                "font_family": style["font_family"],
                "font_file": str(font_file) if font_file else None,
                "font_size": style["font_size"],
                "line_height": style["line_height"],
                "letter_spacing": style["letter_spacing"],
                "text_indent": style["text_indent"],
                "padding_left": style["padding_left"],
                "heading_level": style["heading_level"],
                "gap_before": gap_before,
                "gap_after": style["margin_after"],
            }
        )
        self.y = target.y1 + float(style["margin_after"])
        if full_width:
            self.column_tops = [max(top, self.y) for top in self.column_tops]

    def place_footnote(self, node: dict[str, Any]) -> None:
        """Place one anchored footnote below a rule or defer it to the end."""
        if self.page_has_footnote:
            self.deferred_footnotes.append(node)
            return
        fragment, fragment_height, style = fragment_document(
            node, self.content_width, self.fonts
        )
        fragment.close()
        standard_bottom = self.page.rect.height - self.margin_bottom
        target_top = standard_bottom - fragment_height
        rule_y = target_top - 7.0
        if self.y + 8.0 > rule_y:
            self.deferred_footnotes.append(node)
            return
        assert self.page is not None
        self.page.draw_line(
            pymupdf.Point(self.margin_left, rule_y),
            pymupdf.Point(
                self.margin_left + min(self.content_width * 0.32, 150.0),
                rule_y,
            ),
            color=(0.2, 0.2, 0.2),
            width=0.6,
        )
        target = pymupdf.Rect(
            self.margin_left,
            target_top,
            self.margin_left + self.content_width,
            standard_bottom,
        )
        draw_text_fragment(self.page, node, target, self.fonts)
        self.placements.append(
            {
                "id": node["id"],
                "ids": node["ids"],
                "output_page": self.page.number + 1,
                "bbox": [target.x0, target.y0, target.x1, target.y1],
                "render_mode": "text",
                "text_embedding": "direct-htmlbox",
                "kind": "footnote",
                "flow_role": "footnote",
                "translatable": True,
                "joined_continuations": node["joined_continuations"],
                "font_family": style["font_family"],
                "font_file": str(self.fonts.body),
                "font_size": style["font_size"],
                "line_height": style["line_height"],
                "letter_spacing": style["letter_spacing"],
                "text_indent": style["text_indent"],
                "padding_left": style["padding_left"],
                "heading_level": None,
                "gap_before": 0.0,
                "gap_after": 0.0,
                "footnote_location": "page-bottom",
                "separator_y": rule_y,
            }
        )
        self.bottom = rule_y - 5.0
        self.page_has_footnote = True

    def flush_deferred_footnotes(self) -> None:
        if not self.deferred_footnotes:
            return
        notes = list(self.deferred_footnotes)
        self.deferred_footnotes.clear()
        self.new_page()
        assert self.page is not None
        self.page.draw_line(
            pymupdf.Point(self.margin_left, self.margin_top + 7.0),
            pymupdf.Point(
                self.margin_left + min(self.content_width * 0.32, 150.0),
                self.margin_top + 7.0,
            ),
            color=(0.2, 0.2, 0.2),
            width=0.6,
        )
        self.y = self.margin_top + 14.0
        for node in notes:
            self.place_text(node)
            self.placements[-1]["footnote_location"] = "end-of-body"

    def place_source_clip(self, node: dict[str, Any]) -> None:
        element = node["element"]
        clip, target_width, target_height, full_width = (
            self.source_clip_geometry(node)
        )
        if clip.is_empty or clip.width <= 0 or clip.height <= 0:
            return
        if full_width:
            if self.columns == 2 and (
                self.column != 0 or self.y > self.margin_top + 1
            ):
                self.new_page()
        self.ensure_space(target_height)
        assert self.page is not None
        target = pymupdf.Rect(
            self.column_x(),
            self.y,
            self.column_x() + target_width,
            self.y + target_height,
        )
        self.page.show_pdf_page(
            target,
            self.source,
            element["page"] - 1,
            clip=clip,
            keep_proportion=True,
        )
        self.placements.append(
            {
                "id": node["id"],
                "ids": node["ids"],
                "output_page": self.page.number + 1,
                "bbox": [target.x0, target.y0, target.x1, target.y1],
                "source_bbox": [clip.x0, clip.y0, clip.x1, clip.y1],
                "source_page": int(element["page"]),
                "render_mode": "source_clip",
                "kind": element["kind"],
                "flow_role": "source-clip",
                "translatable": any(
                    bool(item.get("translatable"))
                    for item in node["elements"]
                ),
                "composite_parts": int(node.get("composite_parts") or 1),
            }
        )
        self.y = target.y1 + 4.0
        if full_width:
            self.column_tops = [max(top, self.y) for top in self.column_tops]

    def finish(self) -> pymupdf.Document:
        translated_page_number = 0
        for output_page, page in enumerate(self.document, start=1):
            if output_page in self.boundary_pages:
                continue
            translated_page_number += 1
            text = str(translated_page_number)
            point = pymupdf.Point(
                (
                    page.rect.width
                    - pymupdf.get_text_length(text, fontsize=8)
                )
                / 2,
                page.rect.height - self.margin_bottom / 2,
            )
            page.insert_text(
                point, text, fontsize=8, color=(0.25, 0.25, 0.25)
            )
        return self.document


def main() -> int:
    args = parse_args()
    if args.typography_profile != TYPOGRAPHY_PROFILE:
        raise SystemExit(
            f"Unsupported typography profile: {args.typography_profile!r}"
        )
    manifest_path = args.manifest.expanduser().resolve()
    translations_path = args.translations.expanduser().resolve()
    output = args.output.expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if manifest.get("partial_run"):
        raise SystemExit("Refusing to render a partial diagnostic preparation.")
    if manifest.get("boundary", {}).get("needs_boundary_review"):
        raise SystemExit(
            "Translation boundaries require review. Correct the JSONL and "
            "manifest before rendering."
        )

    source_path = Path(manifest["source_pdf"])
    if sha256_file(source_path) != manifest["source_sha256"]:
        raise SystemExit("Source PDF no longer matches the preparation manifest.")
    units_path = manifest_path.parent / manifest["paths"]["translation_units"]
    elements = read_jsonl(units_path)
    translations = load_translation_map(translations_path)
    required_ids = {
        element["id"] for element in elements if element.get("translatable")
    }
    supplied_ids = set(translations)
    missing = sorted(required_ids - supplied_ids)
    extra = sorted(supplied_ids - required_ids)
    if missing or extra:
        raise SystemExit(
            f"Translation ID mismatch. Missing: {missing[:10]}; extra: {extra[:10]}"
        )

    rendered_text: dict[str, str] = {}
    for element in elements:
        if not element.get("translatable"):
            rendered_text[element["id"]] = element.get("source_text", "")
            continue
        translated = str(
            translations[element["id"]].get("translated_text", "")
        ).strip()
        if not translated:
            raise SystemExit(f"{element['id']}: translated_text is empty")
        if (
            element["kind"] == "body"
            and len(element.get("source_text", "")) >= 40
            and not HAN_RE.search(translated)
        ):
            raise SystemExit(f"{element['id']}: no Chinese text detected")
        rendered_text[element["id"]] = restore_tokens(element, translated)

    pages = manifest["pages"]
    first_page = pages[0]
    column_votes = Counter(page["columns"] for page in pages)
    columns = 2 if column_votes[2] > column_votes[1] else 1
    source = pymupdf.open(source_path)
    source_hash_before = sha256_file(source_path)
    fonts = find_typography_fonts()
    math_asset_strategies = prepare_math_assets(
        source,
        elements,
        manifest_path.parent / "math-assets",
    )
    try:
        body_elements, front_segments, tail_segments = partition_translation_body(
            elements, manifest
        )
    except ValueError as error:
        source.close()
        raise SystemExit(str(error)) from error
    nodes = build_flow_nodes(body_elements, rendered_text)
    renderer = FlowRenderer(
        source=source,
        width=float(first_page["width"]),
        height=float(first_page["height"]),
        columns=columns,
        gutter=args.gutter,
        fonts=fonts,
        profile=PROFILE,
    )
    try:
        for index, segment in enumerate(front_segments):
            renderer.place_boundary_segment(segment, start_new_page=index > 0)
        if front_segments:
            renderer.new_page()
        translation_page_start = renderer.page.number + 1
        for index, node in enumerate(nodes):
            if node["render_mode"] == "source_clip":
                renderer.place_source_clip(node)
            elif node["flow_role"] == "footnote":
                renderer.place_footnote(node)
            else:
                keep_with_height = 0.0
                if index + 1 < len(nodes):
                    next_node = nodes[index + 1]
                    if node["flow_role"] == "heading":
                        keep_with_height = (
                            renderer.measure_text_height(next_node)
                            if next_node["render_mode"] == "text"
                            else renderer.measure_source_clip_height(next_node)
                        )
                    elif (
                        node["element"]["kind"] == "caption"
                        and next_node["render_mode"] == "source_clip"
                    ):
                        keep_with_height = renderer.measure_source_clip_height(
                            next_node
                        )
                renderer.place_text(node, keep_with_height)
        renderer.flush_deferred_footnotes()
        translation_page_end = renderer.page.number + 1
        for segment in tail_segments:
            renderer.place_boundary_segment(segment, start_new_page=True)
        result = renderer.finish()
        result.set_metadata(
            {
                **{
                    key: value or ""
                    for key, value in manifest.get("source_metadata", {}).items()
                    if key
                    in {
                        "title",
                        "author",
                        "subject",
                        "keywords",
                        "creator",
                        "producer",
                        "creationDate",
                        "modDate",
                    }
                },
                "subject": "Chinese body translation; non-body material preserved",
                "producer": "PyMuPDF via translate-paper",
            }
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        # Subset text fonts for a practical file size, then restore every
        # imported vector clip from the untouched source PDF. Subsetting a
        # composite document directly may rewrite a clip's symbolic fonts.
        result.subset_fonts()
        result.ez_save(output)
        restore_verbatim_source_clips(
            output,
            source_path,
            renderer.placements,
        )
        restore_verbatim_full_boundary_pages(
            output,
            source_path,
            renderer.boundary_placements,
        )
        rich_counts = Counter(
            str(fragment.get("kind") or "unknown")
            for element in elements
            for fragment in dict(element.get("inline_fragments") or {}).values()
            if element.get("translatable")
        )
        style_counts = Counter(
            str(kind)
            for element in elements
            for kind in dict(element.get("style_tokens") or {}).values()
            if element.get("translatable")
        )
        report = {
            "output": str(output),
            "output_pages": len(result),
            "source_pages": len(source),
            "layout_columns": columns,
            "typography_profile": TYPOGRAPHY_PROFILE,
            "typography": {
                "body_font": str(fonts.body),
                "heading_font": str(fonts.heading),
                "latin_regular_font": str(fonts.latin_regular),
                "latin_bold_font": str(fonts.latin_bold),
                "latin_italic_font": str(fonts.latin_italic),
                "math_font": str(fonts.math) if fonts.math else None,
                "exact_profile_fonts": fonts.exact_profile_fonts,
                "body_font_size": PROFILE.body_font_size,
                "body_line_pitch": PROFILE.body_line_pitch,
                "body_letter_spacing": PROFILE.body_letter_spacing,
                "body_text_indent": PROFILE.body_text_indent,
                "paragraph_spacing": PROFILE.paragraph_spacing,
                "heading_level_one_size": PROFILE.heading_level_one_size,
                "heading_lower_size": PROFILE.heading_lower_size,
                "page_margins": {
                    "left": PROFILE.margin_left,
                    "right": PROFILE.margin_right,
                    "top": PROFILE.margin_top,
                    "bottom": PROFILE.margin_bottom,
                },
            },
            "flow": {
                "text_embedding": "direct-htmlbox",
                "source_elements": len(elements),
                "input_elements": len(body_elements),
                "output_nodes": len(nodes),
                "joined_continuations": sum(
                    int(node.get("joined_continuations") or 0)
                    for node in nodes
                ),
            },
            "rich_text": {
                "schema_version": int(manifest.get("schema_version") or 1),
                "inline_fragments": dict(rich_counts),
                "math_render_strategies": dict(math_asset_strategies),
                "styles": dict(style_counts),
                "footnote_placements": [
                    {
                        "id": placement["id"],
                        "location": placement.get("footnote_location"),
                        "output_page": placement["output_page"],
                        "separator_y": placement.get("separator_y"),
                    }
                    for placement in renderer.placements
                    if placement.get("flow_role") == "footnote"
                ],
            },
            "boundary_layout": {
                "profile": "original-front-translated-body-original-tail",
                "translation_page_start": translation_page_start,
                "translation_page_end": translation_page_end,
                "front_segment_count": len(front_segments),
                "tail_segment_count": len(tail_segments),
                "segments": renderer.boundary_placements,
            },
            "placements": renderer.placements,
        }
        output.with_suffix(".layout.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    finally:
        source.close()

    if source_hash_before != sha256_file(source_path):
        output.unlink(missing_ok=True)
        output.with_suffix(".layout.json").unlink(missing_ok=True)
        raise SystemExit("Source PDF changed while rendering; output removed.")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
