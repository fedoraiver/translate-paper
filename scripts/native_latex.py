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
import statistics
import subprocess
import unicodedata
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
URL_RE = re.compile(r"https?://[^\s<>\[\]\u3000-\u303f\u3400-\u9fff]+", re.IGNORECASE)
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
    def chancery_variant(match: re.Match[str]) -> str:
        character = match.group(1)
        name = unicodedata.name(character, "")
        base = unicodedata.normalize("NFKC", character)
        if name.startswith(("MATHEMATICAL SCRIPT CAPITAL ", "SCRIPT CAPITAL ")) and re.fullmatch(r"[A-Z]", base):
            return rf"\mathcal{{{base}}}"
        return match.group()

    # FE00 requests the chancery variant of a script capital. Latin Modern
    # exposes that alphabet through mathcal, not a standalone selector glyph.
    value = re.sub(r"([^\x00-\x7f])\ufe00", chancery_variant, value)
    # Percentages arrive from the PDF as literal percent signs. In TeX an
    # unescaped percent comments out the closing math delimiter and the rest
    # of the paragraph. Preserve existing escapes (and preceding line breaks).
    value = re.sub(
        r"(?<!\\)((?:\\\\)*)%",
        lambda match: match.group(1) + r"\%",
        value,
    )
    # Normalize common glyph substitutions emitted by embedded paper fonts.
    # Sonic's source uses U+03D5 for phi and a Cyrillic small-de glyph where
    # the mathematical generator is the Latin letter g.
    value = value.replace("ϕ", r"\phi")
    value = value.replace("д", "g")
    value = re.sub(
        r"([A-Za-zΑ-ω])\u20d7",
        lambda match: rf"\vec{{{match.group(1)}}}",
        value,
    )
    value = value.replace("≫", r"\gg ")
    value = value.replace("↔", r"\leftrightarrow ")
    value = value.replace("𝔫", r"\mathfrak{n}")
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


def validate_native_tex_compatibility(tex: str, label: str) -> None:
    if re.search(r"\\tag\s*\{", tex):
        raise MathReviewError(
            f"{label}: \\tag{{...}} is unsupported inside TPDisplayMath; "
            r"write the equation number as \qquad(n)."
        )
    unsupported = {
        "\u20d7": r"use \vec{...}",
        "≫": r"use \gg",
        "↔": r"use \leftrightarrow",
        "𝔫": r"use \mathfrak{n}",
    }
    found = [
        f"U+{ord(character):04X} {character!r}: {replacement}"
        for character, replacement in unsupported.items()
        if character in tex
    ]
    if found:
        raise MathReviewError(
            f"{label}: unsupported native-TeX Unicode remains: "
            + "; ".join(found)
        )
    # unicode-math consumes styled mathematical alphanumerics directly. Keep
    # their distinct alphabets; actual font coverage is checked at compilation.
    combining_math = sorted(
        {
            character
            for character in tex
            if 0x20D0 <= ord(character) <= 0x20FF
        }
    )
    variation_selectors = sorted(
        {
            character
            for character in tex
            if 0xFE00 <= ord(character) <= 0xFE0F
            or 0xE0100 <= ord(character) <= 0xE01EF
        }
    )
    if combining_math or variation_selectors:
        details = ", ".join(
            f"U+{ord(character):04X} {character!r}"
            for character in [*combining_math, *variation_selectors]
        )
        raise MathReviewError(
            f"{label}: unsupported mathematical Unicode {details}; "
            "replace it with explicit reviewed TeX."
        )


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
    validate_native_tex_compatibility(
        tex,
        token or "<unnamed math fragment>",
    )
    if not tex or not _tex_balanced(tex):
        raise MathReviewError(f"{token}: invalid or unbalanced TeX: {tex!r}")
    return tex, status


def resolve_display_math(
    node: dict[str, Any],
    reviews: dict[str, dict[str, Any]],
) -> str:
    review = reviews.get(str(node.get("math_review_id") or node["id"]), {})
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
    validate_native_tex_compatibility(tex, str(node["id"]))
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
        "α": r"\(\alpha\)",
        "β": r"\(\beta\)",
        "γ": r"\(\gamma\)",
        "δ": r"\(\delta\)",
        "ε": r"\(\epsilon\)",
        "ζ": r"\(\zeta\)",
        "η": r"\(\eta\)",
        "θ": r"\(\theta\)",
        "ι": r"\(\iota\)",
        "κ": r"\(\kappa\)",
        "λ": r"\(\lambda\)",
        "μ": r"\(\mu\)",
        "ν": r"\(\nu\)",
        "ξ": r"\(\xi\)",
        "ο": r"\(o\)",
        "π": r"\(\pi\)",
        "ρ": r"\(\rho\)",
        "σ": r"\(\sigma\)",
        "τ": r"\(\tau\)",
        "υ": r"\(\upsilon\)",
        "φ": r"\(\phi\)",
        "ϕ": r"\(\phi\)",
        "χ": r"\(\chi\)",
        "ψ": r"\(\psi\)",
        "ω": r"\(\omega\)",
        "Γ": r"\(\Gamma\)",
        "Δ": r"\(\Delta\)",
        "Θ": r"\(\Theta\)",
        "Λ": r"\(\Lambda\)",
        "Ξ": r"\(\Xi\)",
        "Π": r"\(\Pi\)",
        "Σ": r"\(\Sigma\)",
        "Φ": r"\(\Phi\)",
        "Ψ": r"\(\Psi\)",
        "Ω": r"\(\Omega\)",
        "д": "g",
        "ℓ": r"\(\ell\)",
        "∈": r"\(\in\)",
        "∉": r"\(\notin\)",
        "∅": r"\(\varnothing\)",
        "∪": r"\(\cup\)",
        "∏": r"\(\prod\)",
        "∑": r"\(\sum\)",
        "∥": r"\(\parallel\)",
        "≈": r"\(\approx\)",
        "≠": r"\(\neq\)",
        "≤": r"\(\leq\)",
        "≥": r"\(\geq\)",
        "⊂": r"\(\subset\)",
        "⊆": r"\(\subseteq\)",
        "⊥": r"\(\bot\)",
        "→": r"\(\to\)",
        "←": r"\(\leftarrow\)",
        "↔": r"\(\leftrightarrow\)",
        "↦": r"\(\mapsto\)",
        "↪": r"\(\hookrightarrow\)",
        "−": r"\(-\)",
        "×": r"\(\times\)",
        "′": r"\(\prime\)",
        "□": r"\(\square\)",
        "⊙": r"\(\odot\)",
        "⌈": r"\(\lceil\)",
        "⌉": r"\(\rceil\)",
        "⁰": r"\textsuperscript{0}",
        "¹": r"\textsuperscript{1}",
        "²": r"\textsuperscript{2}",
        "³": r"\textsuperscript{3}",
        "⁴": r"\textsuperscript{4}",
        "⁵": r"\textsuperscript{5}",
        "⁶": r"\textsuperscript{6}",
        "⁷": r"\textsuperscript{7}",
        "⁸": r"\textsuperscript{8}",
        "⁹": r"\textsuperscript{9}",
        "⁺": r"\textsuperscript{+}",
        "⁻": r"\textsuperscript{-}",
        "ⁿ": r"\textsuperscript{n}",
        "ⁱ": r"\textsuperscript{i}",
        "ᵀ": r"\textsuperscript{T}",
        "ᴰ": r"\textsuperscript{D}",
        "ᵃ": r"\textsuperscript{a}",
        "ᵇ": r"\textsuperscript{b}",
        "ᶜ": r"\textsuperscript{c}",
        "ᵈ": r"\textsuperscript{d}",
        "ᵉ": r"\textsuperscript{e}",
        "ᶠ": r"\textsuperscript{f}",
        "ᵍ": r"\textsuperscript{g}",
        "ʰ": r"\textsuperscript{h}",
        "ʲ": r"\textsuperscript{j}",
        "ᵏ": r"\textsuperscript{k}",
        "ˡ": r"\textsuperscript{l}",
        "ᵐ": r"\textsuperscript{m}",
        "ᵒ": r"\textsuperscript{o}",
        "ᵖ": r"\textsuperscript{p}",
        "ʳ": r"\textsuperscript{r}",
        "ˢ": r"\textsuperscript{s}",
        "ᵘ": r"\textsuperscript{u}",
        "ᵛ": r"\textsuperscript{v}",
        "ˣ": r"\textsuperscript{x}",
        "ʸ": r"\textsuperscript{y}",
        "ᶻ": r"\textsuperscript{z}",
        "ʷ": r"\textsuperscript{w}",
        "ᵗ": r"\textsuperscript{t}",
        "ᵢ": r"\textsubscript{i}",
        "₀": r"\textsubscript{0}",
        "₁": r"\textsubscript{1}",
        "₂": r"\textsubscript{2}",
        "₃": r"\textsubscript{3}",
        "₄": r"\textsubscript{4}",
        "₅": r"\textsubscript{5}",
        "₆": r"\textsubscript{6}",
        "₇": r"\textsubscript{7}",
        "₈": r"\textsubscript{8}",
        "₉": r"\textsubscript{9}",
        "ₓ": r"\textsubscript{x}",
        "ₙ": r"\textsubscript{n}",
        "ⱼ": r"\textsubscript{j}",
        "ₐ": r"\textsubscript{a}",
        "ₑ": r"\textsubscript{e}",
        "ₕ": r"\textsubscript{h}",
        "ₖ": r"\textsubscript{k}",
        "ₗ": r"\textsubscript{l}",
        "ₘ": r"\textsubscript{m}",
        "ₒ": r"\textsubscript{o}",
        "ₚ": r"\textsubscript{p}",
        "ₛ": r"\textsubscript{s}",
        "ₜ": r"\textsubscript{t}",
        "⟨": r"\(\langle\)",
        "⟩": r"\(\rangle\)",
        "⋯": r"\(\cdots\)",
        "≡": r"\(\equiv\)",
        "∘": r"\(\circ\)",
        "★": r"\(\star\)",
        "≫": r"\(\gg\)",
        "𝔫": r"\(\mathfrak{n}\)",
    }
    output: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if index + 1 < len(value) and value[index + 1] == "\u20d7":
            output.append(r"\(\vec{" + character + r"}\)")
            index += 2
            continue
        if character == "‣" or "①" <= character <= "⑳" or "\u25a0" <= character <= "\u25ff":
            output.append(r"\TPTextSymbol{" + character + "}")
            index += 1
            continue
        output.append(replacements.get(character, character))
        index += 1
    return "".join(output)


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
    output: list[str] = []
    cursor = 0
    for match in URL_RE.finditer(value):
        output.append(_plain_prose_to_latex(
            value[cursor:match.start()], allow_auto_math=allow_auto_math))
        # Keep the current text font and exact characters, including TeX
        # specials. Invisible breakpoints also prevent automatic URL hyphens
        # and work safely inside heading/style macro arguments without links.
        output.append(r"\allowbreak{}".join(latex_escape_text(char) for char in match.group(0)))
        cursor = match.end()
    output.append(_plain_prose_to_latex(value[cursor:], allow_auto_math=allow_auto_math))
    return "".join(output)


def _plain_prose_to_latex(value: str, *, allow_auto_math: bool = False) -> str:
    if not allow_auto_math:
        return latex_escape_text(value)
    output: list[str] = []
    cursor = 0
    for match in AUTO_MATH_TOKEN_RE.finditer(value):
        output.append(latex_escape_text(value[cursor : match.start()]))
        token = match.group(0)
        if token in {"in", "an", "am"}:
            output.append(latex_escape_text(token))
        else:
            output.append(r"\(" + _auto_math_tex(token) + r"\)")
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
    style_runs = list(node.get("text_style_runs") or [])
    if len(style_runs) > 1:
        # Flow may join source-page continuations with different block styles.
        # Scope each run without creating paragraphs or touching math fragments.
        text = str(node.get("text") or "")
        parts: list[str] = []
        for index, run in enumerate(style_runs):
            end = (int(style_runs[index + 1]["start"])
                   if index + 1 < len(style_runs) else len(text))
            child = {**node, "text": text[int(run["start"]):end],
                     "text_style_runs": [], "_native_block_style": run["block_style"]}
            parts.append(rich_text_to_latex(
                child, reviews, math_statuses, footnotes_by_id,
                suppress_footnote_markers,
            ))
        return "".join(parts)
    block_style = str(node.get("_native_block_style") or (
        style_runs[0].get("block_style") if style_runs
        else node.get("element", {}).get("block_style")
    ) or "normal")
    fragments, styles = node_rich_metadata(node)
    allow_auto_math = any(
        str(fragment.get("kind") or "") == "math"
        for fragment in fragments.values()
    )
    text = str(node.get("text") or "")
    if node.get("flow_role") == "list-item" and not node.get("source_flow_role"):
        def list_boundary(match: re.Match[str]) -> str:
            marker = match.group(1)
            fragment = fragments.get(marker, {})
            label = str(fragment.get("text") or marker)
            return "[[PAR]]" + marker if re.match(
                r"^(?:[•●▪◦‣*-]|\d{1,3}[.)、])", label
            ) else match.group(0)
        text = re.sub(r"\n\s*(\[\[F[A-Za-z0-9_]+\]\]|[•●▪◦‣*\-]|\d{1,3}[.)、])",
                      list_boundary, text)
    unmatched_vector = any(
        index == 0 or not re.fullmatch(r"[A-Za-zΑ-ω]", text[index - 1])
        for index, character in enumerate(text)
        if character == "\u20d7"
    )
    if unmatched_vector:
        raise ValueError(
            f"{node['id']}: combining vector arrow U+20D7 has no base "
            r"character; use a reviewed \vec{...} math fragment."
        )
    unsupported_prose = sorted(
        {
            character
            for character in text
            if (
                0x1D400 <= ord(character) <= 0x1D7FF
                and character != "𝔫"
            )
            or (
                0x20D0 <= ord(character) <= 0x20FF
                and character != "\u20d7"
            )
        }
    )
    if unsupported_prose:
        details = ", ".join(
            f"U+{ord(character):04X} {character!r}"
            for character in unsupported_prose
        )
        raise ValueError(
            f"{node['id']}: unsupported mathematical Unicode {details}; "
            "move it into a reviewed native-math fragment."
        )
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
                    marker = latex_escape_text(
                        str(fragment.get("text") or "")
                    )
                    explicit_number = f"[{marker}]" if marker.isdigit() else ""
                    output.append(
                        r"\footnote"
                        + explicit_number
                        + r"{\fontsize{8.5pt}{11pt}\selectfont "
                        + str((footnotes_by_id or {})[footnote_id])
                        + "}"
                    )
                elif not suppress_footnote_markers:
                    output.append(
                        r"\textsuperscript{"
                        + latex_escape_text(str(fragment.get("text") or ""))
                        + "}"
                    )
            elif kind == "code":
                raw_code = str(fragment.get("text") or "")
                parts = re.split(r"(?<=[,;.(])|(?<=[a-z])(?=[A-Z])", raw_code)
                code = r"\allowbreak{}".join(latex_escape_text(part) for part in parts)
                output.append(
                    r"\texttt{" + code + "}"
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
                    r"{\sffamily\bfseries\upshape " if edge == "OPEN" else "}"
                )
            else:
                output.append(r"{\itshape " if edge == "OPEN" else "}")
        cursor = match.end()
    output.append(
        prose_to_latex(text[cursor:], allow_auto_math=allow_auto_math)
    )
    rendered = "".join(output)
    return r"{\itshape " + rendered + "}" if block_style == "italic" else rendered


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


def prepare_native_clip_nodes(
    nodes: list[dict[str, Any]],
    elements: list[dict[str, Any]],
    profile: Any,
) -> list[dict[str, Any]]:
    """Keep independently reviewed clips separate and scale their source text."""
    body_sizes = [
        float(element.get("font_size") or 0.0)
        for element in elements
        if element.get("kind") == "body"
        and element.get("render_mode") == "text"
        and float(element.get("font_size") or 0.0) > 0
    ]
    source_body_size = statistics.median(body_sizes) if body_sizes else 10.0
    text_scale = profile.body_font_size / source_body_size
    result: list[dict[str, Any]] = []
    for original in nodes:
        parts = list(original.get("elements") or [original["element"]])
        # Legacy flow batching unions adjacent source rectangles. Separate
        # invariant objects without a shared reviewed visual ID must never
        # become one rectangular crop (which can include unrelated prose).
        if (
            _node_is_nonmath_clip(original)
            and not original["element"].get("visual_id")
            and len(parts) > 1
        ):
            children = [
                {
                    **original,
                    "id": part["id"],
                    "ids": [part["id"]],
                    "element": dict(part),
                    "elements": [part],
                    "composite_parts": 1,
                }
                for part in parts
            ]
        else:
            children = [{**original, "element": dict(original["element"])}]
        for node in children:
            if (
                _node_is_nonmath_clip(node)
                and not node.get("visual_layout")
                and not node["element"].get("visual_layout")
                and not node["element"].get("layout_target_text_scale")
            ):
                node["element"]["layout_target_text_scale"] = text_scale
            result.append(node)
    return preserve_reviewed_caption_nodes(result)


def preserve_reviewed_caption_nodes(
    nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Export each reviewed visual and its source caption as one vector asset."""
    replacements: dict[int, dict[str, Any]] = {}
    consumed: set[int] = set()
    for index, node in enumerate(nodes):
        if not _node_is_nonmath_clip(node):
            continue
        visual = node.get("visual_layout") or node["element"].get("visual_layout") or {}
        caption_id = str(visual.get("caption_id") or "")
        if visual.get("captionless") or not caption_id:
            continue
        if caption_id in (node.get("ids") or [node["id"]]):
            continue
        matches = [i for i, candidate in enumerate(nodes)
                   if caption_id in (candidate.get("ids") or [candidate["id"]])]
        if len(matches) != 1:
            raise ValueError(f"{node['id']}: reviewed caption {caption_id} missing or duplicated")
        caption_index = matches[0]
        caption = nodes[caption_index]
        parts = list(caption.get("elements") or [caption["element"]])
        if (
            caption_index in consumed
            or abs(caption_index - index) != 1
            or any(part.get("kind") != "caption" for part in parts)
            or any(int(part["page"]) != int(node["element"]["page"]) for part in parts)
        ):
            raise ValueError(f"{node['id']}: reviewed caption requires a contiguous same-page source crop")
        # Keep source classifications intact; only the rendering node is combined.
        combined = {**node, "element": dict(node["element"])}
        combined["elements"] = list(node.get("elements") or [node["element"]]) + parts
        combined["ids"] = list(node.get("ids") or [node["id"]]) + list(
            caption.get("ids") or [caption["id"]])
        combined["composite_parts"] = len(combined["elements"])
        combined["source_caption_ids"] = [part["id"] for part in parts]
        bounds = pymupdf.Rect(node["element"]["bbox"])
        for part in parts:
            bounds |= pymupdf.Rect(part["bbox"])
        combined["element"]["bbox"] = list(bounds)
        replacements[index] = combined
        consumed.add(caption_index)
    return [replacements.get(index, node) for index, node in enumerate(nodes)
            if index not in consumed]


def footnote_body_without_number(node: dict[str, Any]) -> str:
    """Drop only the recorded label; TeX emits that label at the anchor."""
    text = str(node.get("text") or "").lstrip()
    number = str(node.get("element", {}).get("footnote_number") or "")
    fragments, _ = node_rich_metadata(node)
    first = CONTROL_TOKEN_RE.match(text)
    if number and first:
        fragment = fragments.get(first.group(0), {})
        if re.fullmatch(
            re.escape(number) + r"[.)]?",
            str(fragment.get("text") or "").strip(),
        ):
            return text[first.end():].lstrip()
    if number:
        return re.sub(
            r"^" + re.escape(number) + r"(?=\s|[.)]|$)[.)]?\s*",
            "",
            text,
            count=1,
        )
    return FOOTNOTE_PREFIX_RE.sub("", text, count=1).lstrip()


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
    visual_layout = dict(
        node.get("visual_layout")
        or node.get("element", {}).get("visual_layout")
        or {}
    )
    calibrated_bbox = visual_layout.get("source_bbox")
    if calibrated_bbox:
        calibrated = pymupdf.Rect(calibrated_bbox) & source_page.rect
        if not calibrated.is_empty:
            # Calibration describes the visual itself. A merged vector caption
            # is also part of the exported asset and must not be clipped away.
            for part in node.get("elements") or []:
                if (
                    part.get("render_mode") == "source_clip"
                    or (
                        part.get("kind") == "caption"
                        and (
                            str(part.get("id")) == str(visual_layout.get("caption_id"))
                            or part.get("id") in node.get("source_caption_ids", [])
                        )
                    )
                ):
                    calibrated |= pymupdf.Rect(part["bbox"]) & source_page.rect
            return calibrated & source_page.rect
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
        if layout.get("source_bbox"):
            calibrated = pymupdf.Rect(layout["source_bbox"]) & source_page.rect
            if not calibrated.is_empty:
                target_width *= clip.width / calibrated.width
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


def _caption_matches_clip(caption: dict[str, Any], clip: dict[str, Any]) -> bool:
    """Use the reviewed association before applying adjacency-based keep rules."""
    visual = clip.get("visual_layout") or {}
    if visual.get("captionless") is True:
        return False
    caption_id = str(visual.get("caption_id") or "")
    if caption_id:
        return caption_id in (caption.get("ids") or [caption["id"]])
    caption_visual = str(caption["element"].get("visual_id") or "")
    clip_visual = str(visual.get("visual_id") or clip["element"].get("visual_id") or "")
    if caption_visual and clip_visual:
        return caption_visual == clip_visual
    return True


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
    visual = clip_node.get("visual_layout") or clip_node.get("element", {}).get("visual_layout") or {}
    reviewed_needspace = float(visual.get("layout_needspace_pt") or 0.0)
    return min(text_height, max(display_height + 72.0, reviewed_needspace))


def _document_preamble(profile: Any) -> str:
    return rf"""\documentclass[a4paper,UTF8,fontset=none]{{ctexart}}
\usepackage{{fontspec}}
\usepackage{{unicode-math}}
\usepackage{{amsmath,mathtools}}
\usepackage{{graphicx,adjustbox}}
\usepackage{{tikz}}
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
\newfontfamily\TPSymbolFont[AutoFakeBold=2.2,AutoFakeSlant=0.18]{{Segoe UI Symbol}}
\newcommand{{\TPTextSymbol}}[1]{{{{\TPSymbolFont #1}}}}
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
        placement_marker = "TPVIS" + _sanitize_id(str(node["id"]))
        return (
            "\n".join(
                [
                    r"\par\smallskip",
                    r"\begin{center}",
                    (
                        r"\makebox[0pt][l]{"
                        rf"\fontsize{{0.1pt}}{{0.1pt}}\selectfont {placement_marker}"
                        r"}"
                        rf"\includegraphics[width={target_width:.3f}pt,height=.72\textheight,keepaspectratio]{{\detokenize{{{relative}}}}}"
                    ),
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
        if node.get("source_flow_role"):
            level = int(node.get("list_level") or 0)
            # All lines stay within the same list text column, including a
            # wrapped continuation carried from the next source unit.
            left = profile.list_padding_left * (level + 1)
            first = profile.list_text_indent
            return (rf"\par\begingroup\leftskip={left:g}pt\parindent={first:g}pt "
                    + content + r"\par\endgroup"), "text"
        items = [part.strip() for part in content.split("\n\n") if part.strip()]
        rendered_items = []
        for index, part in enumerate(items):
            if index and not re.match(r"^(?:[•●▪◦*\-]|\\TPTextSymbol\{‣\}|\\\(\d{1,3}[.)、])", part):
                rendered_items.append(rf"\par {part}\par")
                continue
            nested = part.startswith(r"\TPTextSymbol{‣}")
            nested_indent = r"\hspace*{23pt}" if nested else ""
            hang = 46 if nested else 23
            rendered_items.append(
                rf"\par\noindent{nested_indent}\hangindent={hang}pt\hangafter=1 {part}\par")
        return "\n".join(rendered_items), "text"
    if role == "list-continuation" and node.get("source_flow_role"):
        left = profile.list_padding_left * (int(node.get("list_level") or 0) + 1)
        return (rf"\par\begingroup\leftskip={left:g}pt\parindent=0pt "
                + content + r"\par\endgroup"), "text"
    if kind == "caption":
        return (
            rf"\par\smallskip\begin{{center}}\small\itshape {content}\end{{center}}\smallskip",
            "text",
        )
    paragraphs = [part.strip() for part in content.split("\n\n") if part.strip()]
    return "\n\n".join(
        rf"\par\noindent {part}\par"
        if index == 0 and role == "paragraph-continuation"
        else rf"\par {part}\par"
        for index, part in enumerate(paragraphs)
    ), "text"


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
        footnote_node["text"] = footnote_body_without_number(node)
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
            and _caption_matches_clip(node, next_node)
        ):
            needspace = _caption_clip_needspace(source, next_node, profile)
            latex = rf"\Needspace{{{needspace:.2f}pt}}" + "\n" + latex
        if (
            _node_is_nonmath_clip(node)
            and next_node is not None
            and str(next_node["element"].get("kind") or "") == "caption"
            and _caption_matches_clip(next_node, node)
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
        if render_mode == "native-math":
            placement["math_review_id"] = str(node.get("math_review_id") or node["id"])
        if node.get("source_flow_role"):
            placement.update({key: node.get(key) for key in
                              ("source_flow_role", "flow_reviewed", "list_id", "list_level", "source_marker")})
            if node.get("flow_role") in {"list-item", "list-continuation"}:
                placement["padding_left"] = profile.list_padding_left * (int(node.get("list_level") or 0) + 1)
                placement["text_indent"] = profile.list_text_indent if node["flow_role"] == "list-item" else 0
            else:
                placement["text_indent"] = profile.body_text_indent
        if node.get("continuation_of"):
            placement.update({
                "continuation_of": node["continuation_of"],
                "continuation_across_visual_ids": node["continuation_across_visual_ids"],
                "text_indent": 0,
            })
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
                    "visual_source_bbox": visual.get("source_bbox"),
                    "target_width_pt": round(target_width, 3),
                    "target_height_pt": round(target_height, 3),
                    "actual_scale": round(actual_scale, 6),
                    "scale_basis": visual.get("scale_basis"),
                    "source_internal_font_pt": visual.get(
                        "source_internal_font_pt"
                    ),
                    "target_internal_font_pt": (
                        round(float(visual["source_internal_font_pt"]) * actual_scale, 3)
                        if visual.get("source_internal_font_pt") is not None
                        else None
                    ),
                    "aspect_ratio": visual.get("aspect_ratio"),
                    "caption_id": visual.get("caption_id"),
                    "visual_label": visual.get("label"),
                }
            )
        placement_stubs.append(placement)
    chunks.append(r"\end{document}" + "\n")
    return "\n".join(chunks), placement_stubs, math_statuses


def visual_placement_report(placement: dict[str, Any]) -> dict[str, Any]:
    """Report the calibrated visual separately from its combined caption crop."""
    report = {key: placement.get(key) for key in (
        "id", "visual_id", "visual_label", "target_width_pt", "target_height_pt",
        "actual_scale", "source_internal_font_pt", "target_internal_font_pt",
        "aspect_ratio", "scale_basis", "caption_id", "output_page", "bbox",
    )}
    if placement.get("visual_source_bbox"):
        visual = pymupdf.Rect(placement["visual_source_bbox"])
        crop = pymupdf.Rect(placement["source_bbox"])
        target = pymupdf.Rect(placement["bbox"])
        scale = float(placement["actual_scale"])
        left = target.x0 + (visual.x0 - crop.x0) * scale
        top = target.y0 + (visual.y0 - crop.y0) * scale
        width, height = visual.width * scale, visual.height * scale
        report.update({
            "source_bbox": list(visual),
            "target_width_pt": round(width, 3),
            "target_height_pt": round(height, 3),
            "bbox": [left, top, left + width, top + height],
        })
    return report


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
    # Omit wholly empty printer-padding pages, while retaining scanned,
    # vector-only, and annotated source pages. Keep reported segment counts
    # consistent with the pages actually composed.
    for segments in (front_segments, tail_segments):
        kept = []
        for segment in segments:
            page = source[int(segment["source_page"]) - 1]
            blank = (
                segment.get("mode") == "full-page"
                and not page.get_text().strip()
                and not page.get_images()
                and not page.get_drawings()
                and not list(page.annots() or [])
                and not page.get_links()
            )
            if not blank:
                kept.append(segment)
        segments[:] = kept
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
        if stub.get("render_mode") == "source_clip":
            anchor = "TPVIS" + _sanitize_id(str(stub["id"]))
        else:
            candidates = re.findall(
                r"[\u3400-\u9fffA-Za-z0-9]"
                r"[\u3400-\u9fffA-Za-z0-9\s：，。,.()-]{5,40}",
                plain,
            )
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
        str(unit_id): item
        for item in located
        for unit_id in item.get("ids") or [item.get("id")]
        if unit_id
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
    display_plan: list[dict[str, Any]] | None = None,
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
    nodes = prepare_native_clip_nodes(nodes, elements, profile)
    build_dir = manifest_path.parent / "native-latex"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)
    tex, placement_stubs, math_statuses = build_body_tex(
        nodes, reviews, profile, source, build_dir
    )
    if display_plan is not None:
        from display_plan import validate_display_consumption
        validate_display_consumption(display_plan, placement_stubs)
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
        visual_placement_report(placement)
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
                "display_review_ids": [str(item["math_review_id"]) for item in placement_stubs
                                       if item.get("render_mode") == "native-math"],
                "display_source_ids": [str(unit_id) for item in placement_stubs
                                       if item.get("render_mode") == "native-math"
                                       for unit_id in item["ids"]],
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
