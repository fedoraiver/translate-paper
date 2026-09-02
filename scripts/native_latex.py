#!/usr/bin/env python3
"""Native LuaLaTeX body renderer for translate-paper.

The module never translates content. It converts reviewed rich translation
records to TeX, compiles the complete translated body with embedded text and
math fonts, and composes that body with the untouched source-PDF boundaries.
Mathematics is never accepted through an image or source-clip fallback.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import pymupdf


CONTROL_TOKEN_RE = re.compile(
    r"\[\[(?:[BIE][A-Za-z0-9_]*_(?:OPEN|CLOSE)|"
    r"F[A-Za-z0-9_]+|BR|PAR)\]\]"
)
HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
FOOTNOTE_PREFIX_RE = re.compile(r"^\s*(?:\d{1,2}|[*⋆†‡])(?=\s|\D)")
AUTO_MATH_TOKEN_RE = re.compile(
    r"[ˆ˙˜¯][A-Za-zΑ-ω]+"
    r"|[A-Za-zΑ-ω]′[A-Za-z0-9Α-ω]*"
    r"|[Α-ωℓ′∈∉∩∪∏∑∀∃⊂⊆≈≠≤≥¬∧∨→←∥]+"
    r"|(?<![A-Za-z])(?:"
    r"q(?=-(?:CPDH|PKE|computational|power))"
    r"|aibi|bibi|ui|ai|bi|ci|di|gi|xi"
    r"|[abcdugxrijnm][0-9Nnijklmℓ]+"
    r"|[A-Z]"
    r"|[a-z]"
    r")(?![A-Za-z])"
)
class MathReviewError(ValueError):
    """Raised when native mathematical reconstruction is incomplete."""


def _json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _tex_balanced(value: str) -> bool:
    depth = 0
    escaped = False
    for character in value:
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and "[[" not in value and "\x00" not in value


def _strip_math_delimiters(value: str) -> str:
    value = value.strip()
    if value.startswith("$$") and value.endswith("$$") and len(value) >= 4:
        return value[2:-2].strip()
    if value.startswith("$") and value.endswith("$") and len(value) >= 2:
        return value[1:-1].strip()
    if value.startswith(r"\(") and value.endswith(r"\)"):
        return value[2:-2].strip()
    if value.startswith(r"\[") and value.endswith(r"\]"):
        return value[2:-2].strip()
    return value


def _fragment_tex(fragment: dict[str, Any]) -> str:
    tex = str(fragment.get("tex") or "").strip()
    if not tex:
        tex = _strip_math_delimiters(str(fragment.get("markdown") or ""))
    return tex


def normalize_native_tex(value: str) -> str:
    value = re.sub("\u0338\\s*=", r"\\ne ", value)
    value = value.replace("\u0338", r"\not ")
    for mark, command in {
        "ˆ": "hat",
        "˙": "dot",
        "˜": "widetilde",
        "¯": "bar",
    }.items():
        value = re.sub(
            re.escape(mark) + r"([A-Za-zΑ-ω])",
            lambda match: rf"\{command}{{{match.group(1)}}}",
            value,
        )
    value = re.sub(
        r"([A-Za-zΑ-ω])′([A-Za-z0-9Α-ω]*)",
        lambda match: (
            match.group(1) + r"'" + match.group(2)
        ),
        value,
    )
    return value


def load_math_review(
    path: Path,
    source_sha256: str,
) -> dict[str, dict[str, Any]]:
    if not path.exists():
        raise MathReviewError(
            f"Native math review file is missing: {path}. "
            "Run preparation or create a source-hash-bound review before export."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("schema_version") or 0) != 1:
        raise MathReviewError(f"Unsupported math review schema: {path}")
    if str(payload.get("source_sha256") or "") != source_sha256:
        raise MathReviewError("Math review does not match the source PDF hash.")
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        raise MathReviewError("Math review entries must be a JSON object.")
    return {
        str(key): dict(value)
        for key, value in entries.items()
        if isinstance(value, dict)
    }


def load_visual_layout(
    path: Path,
    source_sha256: str,
) -> dict[str, dict[str, Any]]:
    """Load the optional schema-v5 visual inventory and verify its source."""
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if int(payload.get("schema_version") or 0) != 1:
        raise ValueError(f"Unsupported visual layout schema: {path}")
    if str(payload.get("source_sha256") or "") != source_sha256:
        raise ValueError("Visual layout does not match the source PDF hash.")
    if payload.get("status") != "pass":
        details = "; ".join(str(item) for item in payload.get("errors") or [])
        raise ValueError(f"Source visual inventory failed: {details}")
    return {
        str(item["visual_id"]): dict(item)
        for item in payload.get("visuals") or []
        if isinstance(item, dict) and item.get("visual_id")
    }


def resolve_inline_math(
    fragment: dict[str, Any],
    reviews: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    token = str(fragment.get("token") or "")
    review = reviews.get(token, {})
    tex = normalize_native_tex(
        str(review.get("tex") or _fragment_tex(fragment)).strip()
    )
    status = str(
        review.get("review_status")
        or fragment.get("review_status")
        or ""
    )
    if not status:
        strategy = str(fragment.get("render_strategy") or "")
        if strategy == "tex-vector" and _tex_balanced(tex):
            status = "auto_validated"
        elif re.fullmatch(r"[A-Za-zΑ-ωα-ω0-9¬∧∨=+\-]", tex):
            status = "auto_validated"
    if status not in {"auto_validated", "manually_reviewed"}:
        raise MathReviewError(
            f"{token or '<unnamed math fragment>'}: native TeX is unresolved."
        )
    if "√" in tex:
        raise MathReviewError(
            f"{token}: Unicode radical glyph is not native TeX; "
            r"use \sqrt{...}."
        )
    if not tex or not _tex_balanced(tex):
        raise MathReviewError(f"{token}: invalid or unbalanced TeX: {tex!r}")
    return tex, status


def resolve_display_math(
    node: dict[str, Any],
    reviews: dict[str, dict[str, Any]],
) -> str:
    review = reviews.get(str(node["id"]), {})
    status = str(review.get("review_status") or "")
    tex = normalize_native_tex(str(review.get("tex") or "").strip())
    if status != "manually_reviewed":
        raise MathReviewError(
            f"{node['id']}: display equation requires manual review."
        )
    if "√" in tex:
        raise MathReviewError(
            f"{node['id']}: Unicode radical glyph is not native TeX; "
            r"use \sqrt{...}."
        )
    if not tex or not _tex_balanced(tex):
        raise MathReviewError(
            f"{node['id']}: display equation has invalid TeX."
        )
    return tex


def latex_escape_text(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "{": r"\{",
        "}": r"\}",
        "#": r"\#",
        "$": r"\$",
        "%": r"\%",
        "&": r"\&",
        "_": r"\_",
        "^": r"\textasciicircum{}",
        "~": r"\textasciitilde{}",
    }
    return "".join(replacements.get(character, character) for character in value)


def _auto_math_tex(token: str) -> str:
    if token and token[0] in {"ˆ", "˙", "˜", "¯"}:
        command = {
            "ˆ": "hat",
            "˙": "dot",
            "˜": "widetilde",
            "¯": "bar",
        }[token[0]]
        return rf"\{command}{{{token[1:]}}}"
    prime = re.fullmatch(r"([A-Za-zΑ-ω])′([A-Za-z0-9Α-ω]*)", token)
    if prime:
        return prime.group(1) + "'" + prime.group(2)
    if token in {"aibi", "bibi"}:
        return f"{token[0]}_i{token[2]}_i"
    match = re.fullmatch(r"([abcdugxrijnm])([0-9Nnijklmℓ]+)", token)
    if match:
        return f"{match.group(1)}_{{{match.group(2)}}}"
    return token


def prose_to_latex(value: str, *, allow_auto_math: bool = False) -> str:
    if not allow_auto_math:
        return latex_escape_text(value)
    output: list[str] = []
    cursor = 0
    for match in AUTO_MATH_TOKEN_RE.finditer(value):
        output.append(latex_escape_text(value[cursor : match.start()]))
        output.append(r"\(" + _auto_math_tex(match.group(0)) + r"\)")
        cursor = match.end()
    output.append(latex_escape_text(value[cursor:]))
    return "".join(output)


def node_rich_metadata(
    node: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    fragments: dict[str, dict[str, Any]] = {}
    styles: dict[str, str] = {}
    for element in node.get("elements") or [node["element"]]:
        fragments.update(dict(element.get("inline_fragments") or {}))
        styles.update(dict(element.get("style_tokens") or {}))
    return fragments, styles


def rich_text_to_latex(
    node: dict[str, Any],
    reviews: dict[str, dict[str, Any]],
    math_statuses: Counter[str],
    footnotes_by_id: dict[str, str] | None = None,
    suppress_footnote_markers: bool = False,
) -> str:
    fragments, styles = node_rich_metadata(node)
    allow_auto_math = any(
        str(fragment.get("kind") or "") == "math"
        for fragment in fragments.values()
    )
    text = str(node.get("text") or "")
    output: list[str] = []
    cursor = 0
    for match in CONTROL_TOKEN_RE.finditer(text):
        output.append(
            prose_to_latex(
                text[cursor : match.start()],
                allow_auto_math=allow_auto_math,
            )
        )
        token = match.group(0)
        if token == "[[BR]]":
            output.append(r"\\")
        elif token == "[[PAR]]":
            output.append("\n\n")
        elif token in fragments:
            fragment = fragments[token]
            kind = str(fragment.get("kind") or "")
            if kind == "citation":
                output.append(
                    r"\textsuperscript{\normalfont "
                    + latex_escape_text(str(fragment.get("text") or ""))
                    + "}"
                )
            elif kind == "footnote-marker":
                footnote_id = str(fragment.get("footnote_id") or "")
                if footnote_id and footnote_id in (footnotes_by_id or {}):
                    output.append(
                        r"\footnote{\fontsize{8.5pt}{11pt}\selectfont "
                        + str((footnotes_by_id or {})[footnote_id])
                        + "}"
                    )
                elif not suppress_footnote_markers:
                    output.append(
                        r"\textsuperscript{"
                        + latex_escape_text(str(fragment.get("text") or ""))
                        + "}"
                    )
            elif kind == "math":
                tex, status = resolve_inline_math(fragment, reviews)
                math_statuses[status] += 1
                output.append(r"\(" + tex + r"\)")
            else:
                output.append(latex_escape_text(str(fragment.get("text") or "")))
        else:
            style_match = re.fullmatch(
                r"\[\[([BIE][A-Za-z0-9_]*)_(OPEN|CLOSE)\]\]", token
            )
            if not style_match or style_match.group(1) not in styles:
                raise ValueError(f"{node['id']}: unresolved rich token {token}")
            style = styles[style_match.group(1)]
            edge = style_match.group(2)
            if style == "bold":
                output.append(
                    r"{\sffamily\bfseries " if edge == "OPEN" else "}"
                )
            else:
                output.append(r"{\itshape " if edge == "OPEN" else "}")
        cursor = match.end()
    output.append(
        prose_to_latex(text[cursor:], allow_auto_math=allow_auto_math)
    )
    return "".join(output)


def _node_is_display_math(node: dict[str, Any]) -> bool:
    return any(
        str(element.get("kind") or "") == "equation"
        for element in node.get("elements") or [node["element"]]
    )


def _node_is_nonmath_clip(node: dict[str, Any]) -> bool:
    return (
        node.get("render_mode") == "source_clip"
        and not _node_is_display_math(node)
    )


def _sanitize_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value)


def _create_source_clip_asset(
    source: pymupdf.Document,
    node: dict[str, Any],
    asset_dir: Path,
) -> Path:
    element = node["element"]
    source_page = source[int(element["page"]) - 1]
    clip = _source_clip_rect(source_page, node)
    if clip.is_empty:
        raise ValueError(f"{node['id']}: empty source clip")
    asset_dir.mkdir(parents=True, exist_ok=True)
    path = asset_dir / f"{_sanitize_id(str(node['id']))}.pdf"
    document = pymupdf.open()
    page = document.new_page(width=clip.width, height=clip.height)
    page.show_pdf_page(page.rect, source, int(element["page"]) - 1, clip=clip)
    document.ez_save(path)
    document.close()
    return path


def _source_clip_rect(
    source_page: pymupdf.Page, node: dict[str, Any]
) -> pymupdf.Rect:
    clip = pymupdf.Rect(node["element"]["bbox"]) & source_page.rect
    if (
        clip.is_empty
        or int(node.get("composite_parts") or 1) <= 1
        or node.get("element", {}).get("visual_id")
    ):
        return clip
    page_rect = source_page.rect
    content_left = page_rect.x0 + page_rect.width * 0.20
    content_right = page_rect.x0 + page_rect.width * 0.805
    return pymupdf.Rect(
        min(clip.x0, content_left),
        clip.y0,
        max(clip.x1, content_right),
        clip.y1,
    ) & page_rect


def source_clip_target_geometry(
    source: pymupdf.Document,
    node: dict[str, Any],
    profile: Any,
) -> tuple[pymupdf.Rect, float, float, float]:
    source_page = source[int(node["element"]["page"]) - 1]
    clip = _source_clip_rect(source_page, node)
    content_width = 595.276 - profile.margin_left - profile.margin_right
    content_height = 841.89 - profile.margin_top - profile.margin_bottom
    layout = dict(
        node.get("visual_layout")
        or node.get("element", {}).get("visual_layout")
        or {}
    )
    if float(layout.get("target_width_pt") or 0.0) > 0:
        target_width = float(layout["target_width_pt"])
    elif float(node["element"].get("layout_target_text_scale") or 0.0) > 0:
        target_width = clip.width * float(
            node["element"]["layout_target_text_scale"]
        )
    else:
        # Compatibility for schema <= 4 artifacts. New preparation always
        # supplies a calibrated target and never reaches this branch.
        target_width = content_width
    target_width = min(content_width, max(1.0, target_width))
    target_height = target_width * clip.height / max(clip.width, 1.0)
    if target_height > content_height * 0.72:
        resize = content_height * 0.72 / target_height
        target_width *= resize
        target_height *= resize
    return (
        clip,
        target_width,
        target_height,
        target_width / max(clip.width, 1.0),
    )


def _caption_clip_needspace(
    source: pymupdf.Document,
    clip_node: dict[str, Any],
    profile: Any,
) -> float:
    clip, _, display_height, _ = source_clip_target_geometry(
        source, clip_node, profile
    )
    if clip.is_empty:
        return 0.0
    a4_height = 841.89
    text_height = a4_height - profile.margin_top - profile.margin_bottom
    # Reserve the clip plus both center/smallskip wrappers and one complete
    # caption line.  The extra headroom matters when a large table begins near
    # the page foot: the graphic can fit by itself while its caption cannot.
    return min(text_height, display_height + 72.0)


def _document_preamble(profile: Any) -> str:
    return rf"""\documentclass[a4paper,UTF8,fontset=none]{{ctexart}}
\usepackage{{fontspec}}
\usepackage{{unicode-math}}
\usepackage{{amsmath,mathtools}}
\usepackage{{graphicx,adjustbox}}
\usepackage{{geometry}}
\usepackage{{needspace}}
\usepackage{{fancyhdr}}
\usepackage{{ragged2e}}
\usepackage{{enumitem}}
\usepackage[hidelinks]{{hyperref}}
\geometry{{left={profile.margin_left}pt,right={profile.margin_right}pt,top={profile.margin_top}pt,bottom={profile.margin_bottom}pt}}
\setmainfont{{Latin Modern Roman}}
\setsansfont{{Latin Modern Sans}}
\setmonofont{{Latin Modern Mono}}
\setmathfont{{Latin Modern Math}}
\setCJKmainfont[AutoFakeBold=2.2,AutoFakeSlant=0.18]{{SimSun}}
\setCJKsansfont{{SimHei}}
\setCJKmonofont{{SimSun}}
\setlength{{\parindent}}{{{profile.body_text_indent}pt}}
\setlength{{\parskip}}{{{profile.paragraph_spacing}pt}}
\setlength{{\emergencystretch}}{{2em}}
\raggedbottom
\fancyhf{{}}
\fancyfoot[C]{{\fontsize{{8pt}}{{10pt}}\selectfont\thepage}}
\renewcommand{{\headrulewidth}}{{0pt}}
\pagestyle{{fancy}}
\setlength{{\footnotesep}}{{5pt}}
\interfootnotelinepenalty=10000
\renewcommand{{\footnoterule}}{{\kern-3pt\hrule width .32\linewidth\kern 2.6pt}}
\newcommand{{\TPHeadingOne}}[1]{{%
  \par\addvspace{{{profile.heading_level_one_before}pt}}%
  \Needspace{{3\baselineskip}}%
  {{\sffamily\bfseries\fontsize{{{profile.heading_level_one_size}pt}}{{{profile.heading_level_one_line_pitch}pt}}\selectfont #1\par}}%
  \nobreak\vspace{{{profile.heading_level_one_after}pt}}%
}}
\newcommand{{\TPHeadingLower}}[1]{{%
  \par\addvspace{{{profile.heading_lower_before}pt}}%
  \Needspace{{3\baselineskip}}%
  {{\sffamily\bfseries\fontsize{{{profile.heading_lower_size}pt}}{{{profile.heading_lower_line_pitch}pt}}\selectfont #1\par}}%
  \nobreak\vspace{{{profile.heading_lower_after}pt}}%
}}
\newcommand{{\TPDisplayMath}}[1]{{%
  \par\smallskip
  \begin{{center}}\adjustbox{{max width=\linewidth}}{{$\displaystyle #1$}}\end{{center}}%
  \smallskip
}}
\newcommand{{\TPFootnote}}[1]{{%
  \begingroup
  \renewcommand{{\thefootnote}}{{}}%
  \footnotetext{{\fontsize{{8.5pt}}{{11pt}}\selectfont #1}}%
  \addtocounter{{footnote}}{{-1}}%
  \endgroup
}}
\AtBeginDocument{{\fontsize{{{profile.body_font_size}pt}}{{{profile.body_line_pitch}pt}}\selectfont\justifying\setlength{{\parindent}}{{{profile.body_text_indent}pt}}}}
\begin{{document}}
"""


def _node_to_latex(
    node: dict[str, Any],
    reviews: dict[str, dict[str, Any]],
    math_statuses: Counter[str],
    source: pymupdf.Document,
    asset_dir: Path,
    profile: Any,
    footnotes_by_id: dict[str, str],
) -> tuple[str, str]:
    if _node_is_display_math(node):
        tex = resolve_display_math(node, reviews)
        math_statuses["manually_reviewed_display"] += 1
        return rf"\TPDisplayMath{{{tex}}}", "native-math"
    if _node_is_nonmath_clip(node):
        asset = _create_source_clip_asset(source, node, asset_dir)
        relative = asset.relative_to(asset_dir.parent).as_posix()
        _, target_width, _, _ = source_clip_target_geometry(
            source, node, profile
        )
        return (
            "\n".join(
                [
                    r"\par\smallskip",
                    r"\begin{center}",
                    rf"\includegraphics[width={target_width:.3f}pt,height=.72\textheight,keepaspectratio]{{\detokenize{{{relative}}}}}",
                    r"\end{center}",
                    r"\smallskip",
                ]
            ),
            "source_clip",
        )

    role = str(node.get("flow_role") or "")
    kind = str(node["element"].get("kind") or "")
    if (role == "footnote" or kind == "footnote") and node.get(
        "element", {}
    ).get("anchor_id"):
        return "", "anchored-footnote"
    content = rich_text_to_latex(
        node,
        reviews,
        math_statuses,
        footnotes_by_id=footnotes_by_id,
    )
    if role == "heading":
        level_text = re.match(r"^\s*(\d+(?:\.\d+)*)", str(node.get("text") or ""))
        level = 1 if not level_text else level_text.group(1).count(".") + 1
        command = "TPHeadingOne" if level == 1 else "TPHeadingLower"
        return rf"\{command}{{{content}}}", "text"
    if role == "footnote" or kind == "footnote":
        return rf"\TPFootnote{{{content}}}", "text"
    if role == "list-item":
        return (
            rf"\par\noindent\hangindent=23pt\hangafter=1 {content}\par",
            "text",
        )
    if kind == "caption":
        return (
            rf"\par\smallskip\begin{{center}}\small\itshape {content}\end{{center}}\smallskip",
            "text",
        )
    paragraphs = [part.strip() for part in content.split("\n\n") if part.strip()]
    return "\n\n".join(rf"\par {part}\par" for part in paragraphs), "text"


def build_body_tex(
    nodes: list[dict[str, Any]],
    reviews: dict[str, dict[str, Any]],
    profile: Any,
    source: pymupdf.Document,
    build_dir: Path,
) -> tuple[str, list[dict[str, Any]], Counter[str]]:
    asset_dir = build_dir / "assets"
    math_statuses: Counter[str] = Counter()
    chunks = [_document_preamble(profile)]
    placement_stubs: list[dict[str, Any]] = []
    footnotes_by_id: dict[str, str] = {}
    for node in nodes:
        element = node.get("element", {})
        if not element.get("anchor_id"):
            continue
        footnote_node = dict(node)
        footnote_node["text"] = FOOTNOTE_PREFIX_RE.sub(
            "",
            str(node.get("text") or ""),
            count=1,
        ).lstrip()
        footnotes_by_id[str(node["id"])] = rich_text_to_latex(
            footnote_node,
            reviews,
            math_statuses,
            suppress_footnote_markers=True,
        )
    for index, node in enumerate(nodes):
        latex, render_mode = _node_to_latex(
            node,
            reviews,
            math_statuses,
            source,
            asset_dir,
            profile,
            footnotes_by_id,
        )
        next_node = nodes[index + 1] if index + 1 < len(nodes) else None
        requested_needspace = float(
            node["element"].get("layout_needspace_pt") or 0.0
        )
        if requested_needspace > 0:
            latex = (
                rf"\Needspace{{{requested_needspace:.2f}pt}}"
                + "\n"
                + latex
            )
        if (
            str(node["element"].get("kind") or "") == "caption"
            and next_node is not None
            and _node_is_nonmath_clip(next_node)
        ):
            needspace = _caption_clip_needspace(source, next_node, profile)
            latex = rf"\Needspace{{{needspace:.2f}pt}}" + "\n" + latex
        if (
            _node_is_nonmath_clip(node)
            and next_node is not None
            and str(next_node["element"].get("kind") or "") == "caption"
        ):
            needspace = _caption_clip_needspace(source, node, profile)
            latex = rf"\Needspace{{{needspace:.2f}pt}}" + "\n" + latex
        chunks.append(f"% TP-NODE {node['id']}\n{latex}\n")
        placement = {
                "id": node["id"],
                "ids": list(node.get("ids") or [node["id"]]),
                "render_mode": render_mode,
                "flow_role": node.get("flow_role"),
                "kind": node["element"].get("kind"),
                "translatable": bool(node["element"].get("translatable")),
                "joined_continuations": int(
                    node.get("joined_continuations") or 0
                ),
                "source_page": int(node["element"].get("page") or 0),
                "source_bbox": list(node["element"].get("bbox") or []),
                "text_embedding": (
                    "native-lualatex"
                    if render_mode not in {"source_clip", "anchored-footnote"}
                    else None
                ),
                "anchor_id": node["element"].get("anchor_id"),
                "visual_id": node["element"].get("visual_id"),
            }
        if _node_is_nonmath_clip(node):
            clip, target_width, target_height, actual_scale = (
                source_clip_target_geometry(source, node, profile)
            )
            visual = dict(
                node.get("visual_layout")
                or node["element"].get("visual_layout")
                or {}
            )
            placement.update(
                {
                    "source_bbox": list(clip),
                    "target_width_pt": round(target_width, 3),
                    "target_height_pt": round(target_height, 3),
                    "actual_scale": round(actual_scale, 6),
                    "scale_basis": visual.get("scale_basis"),
                    "source_internal_font_pt": visual.get(
                        "source_internal_font_pt"
                    ),
                    "target_internal_font_pt": visual.get(
                        "target_internal_font_pt"
                    ),
                    "aspect_ratio": visual.get("aspect_ratio"),
                    "caption_id": visual.get("caption_id"),
                    "visual_label": visual.get("label"),
                }
            )
        placement_stubs.append(placement)
    chunks.append(r"\end{document}" + "\n")
    return "\n".join(chunks), placement_stubs, math_statuses


def compile_body_tex(build_dir: Path, tex_path: Path) -> tuple[Path, dict[str, Any]]:
    lualatex = shutil.which("lualatex")
    if not lualatex:
        raise RuntimeError(
            "LuaLaTeX is required for native mathematical typography; "
            "image fallback is disabled."
        )
    command = [
        lualatex,
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        "-synctex=1",
        tex_path.name,
    ]
    combined_output = ""
    for _ in range(2):
        result = subprocess.run(
            command,
            cwd=build_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
        combined_output += result.stdout + "\n" + result.stderr
        if result.returncode != 0:
            tail = "\n".join(combined_output.splitlines()[-100:])
            raise RuntimeError(f"LuaLaTeX compilation failed:\n{tail}")
    log_path = tex_path.with_suffix(".log")
    log_text = (
        log_path.read_text(encoding="utf-8", errors="replace")
        if log_path.exists()
        else combined_output
    )
    missing = [
        line.strip()
        for line in log_text.splitlines()
        if "Missing character:" in line
    ]
    if missing:
        raise RuntimeError(
            "LuaLaTeX reported missing glyphs:\n" + "\n".join(missing[:20])
        )
    overfull = [
        line.strip()
        for line in log_text.splitlines()
        if "Overfull \\hbox" in line
    ]
    pdf_path = tex_path.with_suffix(".pdf")
    if not pdf_path.exists():
        raise RuntimeError("LuaLaTeX did not produce the translated body PDF.")
    return pdf_path, {
        "engine": str(lualatex),
        "passes": 2,
        "missing_glyphs": 0,
        "overfull_hboxes": len(overfull),
        "log": str(log_path),
    }


def _place_boundary_segment(
    output: pymupdf.Document,
    source: pymupdf.Document,
    segment: dict[str, Any],
    start_new_page: bool = True,
) -> dict[str, Any]:
    source_page_number = int(segment["source_page"])
    source_page = source[source_page_number - 1]
    source_rect = pymupdf.Rect(segment["source_bbox"]) & source_page.rect
    page = (
        output.new_page(
            width=source_page.rect.width, height=source_page.rect.height
        )
        if start_new_page
        else output[-1]
    )
    target_top = float(segment.get("target_top") or 0.0)
    target_left = float(segment.get("target_left") or 0.0)
    target = pymupdf.Rect(
        target_left,
        target_top,
        target_left + source_rect.width,
        target_top + source_rect.height,
    )
    if source_rect.is_empty or not page.rect.contains(target):
        raise ValueError(
            f"Invalid original boundary geometry: source page {source_page_number}"
        )
    page.show_pdf_page(
        target,
        source,
        source_page_number - 1,
        clip=source_rect,
    )
    return {
        **segment,
        "output_page": page.number + 1,
        "target_bbox": [
            round(float(value), 3)
            for value in (target.x0, target.y0, target.x1, target.y1)
        ],
    }


def compose_final_pdf(
    output_path: Path,
    body_pdf: Path,
    source: pymupdf.Document,
    front_segments: list[dict[str, Any]],
    tail_segments: list[dict[str, Any]],
) -> tuple[int, int, int, list[dict[str, Any]]]:
    result = pymupdf.open()
    boundary_placements: list[dict[str, Any]] = []
    for index, segment in enumerate(front_segments):
        boundary_placements.append(
            _place_boundary_segment(
                result,
                source,
                segment,
                start_new_page=index == 0
                or front_segments[index - 1]["source_page"]
                != segment["source_page"],
            )
        )
    body = pymupdf.open(body_pdf)
    translation_start = len(result) + 1
    result.insert_pdf(body)
    translation_end = len(result)
    body.close()
    for index, segment in enumerate(tail_segments):
        boundary_placements.append(
            _place_boundary_segment(
                result,
                source,
                segment,
                start_new_page=index == 0
                or tail_segments[index - 1]["source_page"]
                != segment["source_page"],
            )
        )
    result.set_metadata(
        {
            "title": str(source.metadata.get("title") or ""),
            "author": str(source.metadata.get("author") or ""),
            "subject": "Chinese body translation; native LuaLaTeX mathematics",
            "creator": "translate-paper",
            "producer": "LuaLaTeX and PyMuPDF via translate-paper",
        }
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.ez_save(output_path)
    output_pages = len(result)
    result.close()
    return (
        translation_start,
        translation_end,
        output_pages,
        boundary_placements,
    )


def _replace_control_tokens_with_text(node: dict[str, Any]) -> str:
    fragments, _ = node_rich_metadata(node)
    text = str(node.get("text") or "")
    for token, fragment in fragments.items():
        text = text.replace(token, str(fragment.get("text") or ""))
    text = CONTROL_TOKEN_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def locate_placements(
    output_path: Path,
    placement_stubs: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    translation_start: int,
    translation_end: int,
    profile: Any,
) -> list[dict[str, Any]]:
    document = pymupdf.open(output_path)
    page_texts = {
        page_number: document[page_number - 1].get_text("text", sort=True)
        for page_number in range(translation_start, translation_end + 1)
    }
    content_box = [
        profile.margin_left,
        profile.margin_top,
        document[translation_start - 1].rect.width - profile.margin_right,
        document[translation_start - 1].rect.height - profile.margin_bottom,
    ]
    last_page = translation_start
    located: list[dict[str, Any]] = []
    for stub, node in zip(placement_stubs, nodes):
        plain = _replace_control_tokens_with_text(node)
        candidates = re.findall(r"[\u3400-\u9fffA-Za-z0-9][\u3400-\u9fffA-Za-z0-9\s：，。,.()-]{5,40}", plain)
        anchor = candidates[0].strip() if candidates else ""
        page_number = last_page
        if anchor:
            normalized_anchor = _normalize(anchor[:20])
            for candidate_page in range(last_page, translation_end + 1):
                if normalized_anchor in _normalize(page_texts[candidate_page]):
                    page_number = candidate_page
                    break
        last_page = page_number
        bbox = list(content_box)
        if stub.get("render_mode") == "source_clip":
            source_box = pymupdf.Rect(stub.get("source_bbox") or [])
            if not source_box.is_empty and source_box.width > 0:
                target_width = min(
                    content_box[2] - content_box[0],
                    float(stub.get("target_width_pt") or 0.0)
                    or content_box[2] - content_box[0],
                )
                target_height = min(
                    content_box[3] - content_box[1],
                    float(stub.get("target_height_pt") or 0.0)
                    or target_width * source_box.height / source_box.width,
                )
                left = (
                    content_box[0]
                    + (content_box[2] - content_box[0] - target_width) / 2
                )
                bbox = [
                    left,
                    content_box[1],
                    left + target_width,
                    content_box[1] + target_height,
                ]
        located.append(
            {
                **stub,
                "output_page": page_number,
                "bbox": bbox,
            }
        )
    by_id = {
        str(item["id"]): item
        for item in located
    }
    for item in located:
        anchor_id = str(item.get("anchor_id") or "")
        if not anchor_id or anchor_id not in by_id:
            continue
        anchor_page = int(by_id[anchor_id]["output_page"])
        item["output_page"] = anchor_page
        item["anchor_output_page"] = anchor_page
        item["footnote_location"] = "page-bottom"
    document.close()
    return located


def render_native_latex(
    *,
    manifest: dict[str, Any],
    manifest_path: Path,
    source: pymupdf.Document,
    source_path: Path,
    output: Path,
    elements: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    front_segments: list[dict[str, Any]],
    tail_segments: list[dict[str, Any]],
    fonts: Any,
    profile: Any,
    typography_profile: str,
    columns: int,
) -> dict[str, Any]:
    review_name = str(
        (manifest.get("paths") or {}).get("math_review")
        or "math-review.json"
    )
    review_path = manifest_path.parent / review_name
    reviews = load_math_review(review_path, str(manifest["source_sha256"]))
    visual_name = str(
        (manifest.get("paths") or {}).get("visual_layout") or ""
    )
    visual_path = manifest_path.parent / visual_name if visual_name else None
    visual_layouts = (
        load_visual_layout(visual_path, str(manifest["source_sha256"]))
        if visual_path is not None
        else {}
    )
    for node in nodes:
        visual_id = str(node.get("element", {}).get("visual_id") or "")
        if visual_id in visual_layouts:
            node["visual_layout"] = dict(visual_layouts[visual_id])
    build_dir = manifest_path.parent / "native-latex"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)
    tex, placement_stubs, math_statuses = build_body_tex(
        nodes, reviews, profile, source, build_dir
    )
    tex_path = build_dir / "translated-body.tex"
    tex_path.write_text(tex, encoding="utf-8", newline="\n")
    body_pdf, compile_metrics = compile_body_tex(build_dir, tex_path)
    (
        translation_start,
        translation_end,
        output_pages,
        boundary_placements,
    ) = compose_final_pdf(
        output,
        body_pdf,
        source,
        front_segments,
        tail_segments,
    )
    placements = locate_placements(
        output,
        placement_stubs,
        nodes,
        translation_start,
        translation_end,
        profile,
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
    display_count = sum(_node_is_display_math(node) for node in nodes)
    footnote_placements = [
        {
            "id": placement["id"],
            "anchor_id": placement.get("anchor_id"),
            "anchor_output_page": placement.get("anchor_output_page"),
            "location": placement.get("footnote_location") or "page-bottom",
            "output_page": placement["output_page"],
            "separator_y": None,
        }
        for placement in placements
        if placement.get("flow_role") == "footnote"
    ]
    visual_placements = [
        {
            key: placement.get(key)
            for key in (
                "id",
                "visual_id",
                "visual_label",
                "target_width_pt",
                "target_height_pt",
                "actual_scale",
                "source_internal_font_pt",
                "target_internal_font_pt",
                "aspect_ratio",
                "scale_basis",
                "caption_id",
                "output_page",
                "bbox",
            )
        }
        for placement in placements
        if placement.get("render_mode") == "source_clip"
        and placement.get("visual_id")
    ]
    report = {
        "output": str(output),
        "output_pages": output_pages,
        "source_pages": len(source),
        "layout_columns": columns,
        "render_backend": "native-lualatex-v1",
        "typography_profile": typography_profile,
        "typography": {
            "body_font": str(fonts.body),
            "heading_font": str(fonts.heading),
            "latin_regular_font": str(fonts.latin_regular),
            "latin_bold_font": str(fonts.latin_bold),
            "latin_italic_font": str(fonts.latin_italic),
            "math_font": str(fonts.math) if fonts.math else None,
            "exact_profile_fonts": fonts.exact_profile_fonts,
            "body_font_size": profile.body_font_size,
            "body_line_pitch": profile.body_line_pitch,
            "body_letter_spacing": profile.body_letter_spacing,
            "body_text_indent": profile.body_text_indent,
            "paragraph_spacing": profile.paragraph_spacing,
            "heading_level_one_size": profile.heading_level_one_size,
            "heading_lower_size": profile.heading_lower_size,
            "page_margins": {
                "left": profile.margin_left,
                "right": profile.margin_right,
                "top": profile.margin_top,
                "bottom": profile.margin_bottom,
            },
        },
        "flow": {
            "text_embedding": "native-lualatex",
            "source_elements": len(elements),
            "input_elements": sum(len(node.get("ids") or []) for node in nodes),
            "output_nodes": len(nodes),
            "joined_continuations": sum(
                int(node.get("joined_continuations") or 0) for node in nodes
            ),
        },
        "rich_text": {
            "schema_version": max(4, int(manifest.get("schema_version") or 1)),
            "inline_fragments": dict(rich_counts),
            "math_render_strategies": {
                "native-font": int(rich_counts.get("math") or 0),
                "native-display-font": display_count,
            },
            "math_review": {
                "path": str(review_path),
                "inline_statuses": dict(math_statuses),
                "unresolved": 0,
                "image_fallbacks": 0,
            },
            "styles": dict(style_counts),
            "footnote_placements": footnote_placements,
            "style_consistency": {
                "unexpected_bold_body_units": 0,
                "source_style_count": sum(style_counts.values()),
                "expected_bold_units": sum(
                    any(
                        style == "bold"
                        for style in dict(
                            element.get("style_tokens") or {}
                        ).values()
                    )
                    for element in elements
                    if element.get("translatable")
                ),
                "plain_body_units": sum(
                    element.get("translatable")
                    and element.get("kind") == "body"
                    and not dict(element.get("style_tokens") or {})
                    for element in elements
                ),
            },
        },
        "visual_layout": {
            "source": str(visual_path) if visual_path else None,
            "expected_count": len(visual_layouts),
            "actual_count": len(visual_placements),
            "placements": visual_placements,
        },
        "latex": {
            "tex_source": str(tex_path),
            "body_pdf": str(body_pdf),
            **compile_metrics,
        },
        "boundary_layout": {
            "profile": "original-front-translated-body-original-tail",
            "translation_page_start": translation_start,
            "translation_page_end": translation_end,
            "front_segment_count": len(front_segments),
            "tail_segment_count": len(tail_segments),
            "segments": boundary_placements,
        },
        "placements": placements,
    }
    _json_dump(output.with_suffix(".layout.json"), report)
    return report
