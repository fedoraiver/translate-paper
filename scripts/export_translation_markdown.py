# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2"]
# ///
"""Export the reviewed Chinese body translation as readable Markdown.

This script performs no translation. It restores protected source tokens from
Codex-authored JSONL and writes only records marked translatable.
"""

from __future__ import annotations

import argparse
import json
import re
from itertools import pairwise
from pathlib import Path
from typing import Any

from native_latex import resolve_display_math, resolve_inline_math

PLACEHOLDER_RE = re.compile(r"\[\[P\d{4}\]\]")
CONTROL_TOKEN_RE = re.compile(
    r"\[\[(?:[BIE][A-Za-z0-9_]*_(?:OPEN|CLOSE)|"
    r"F[A-Za-z0-9_]+|BR|PAR)\]\]"
)
STYLE_TOKEN_RE = re.compile(r"\[\[([BIE][A-Za-z0-9_]*)_(OPEN|CLOSE)\]\]")


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


def restore_tokens(unit: dict[str, Any], translated_text: str) -> str:
    restored = translated_text
    for token, source_text in unit.get("protected_tokens", {}).items():
        if restored.count(token) != 1:
            raise ValueError(
                f"{unit['id']}: protected token {token} must occur exactly once"
            )
        restored = restored.replace(token, str(source_text))
    if PLACEHOLDER_RE.search(restored):
        raise ValueError(f"{unit['id']}: unresolved protected token")
    for token in unit.get("inline_fragments", {}):
        if restored.count(token) != 1:
            raise ValueError(
                f"{unit['id']}: inline fragment {token} must occur exactly once"
            )
    for key in unit.get("style_tokens", {}):
        for edge in ("OPEN", "CLOSE"):
            token = f"[[{key}_{edge}]]"
            if restored.count(token) != 1:
                raise ValueError(
                    f"{unit['id']}: style token {token} must occur exactly once"
                )
    return restored.strip()


def rich_text_to_markdown(
    unit: dict[str, Any],
    text: str,
    reviews: dict[str, dict[str, Any]] | None = None,
) -> str:
    fragments = dict(unit.get("inline_fragments") or {})
    styles = dict(unit.get("style_tokens") or {})
    output: list[str] = []
    pending_math: list[str] = []
    style_stack: list[str] = []
    matches = list(CONTROL_TOKEN_RE.finditer(text))
    coalesced_style_edges: set[int] = set()
    for index, (left, right) in enumerate(pairwise(matches)):
        left_style = STYLE_TOKEN_RE.fullmatch(left.group(0))
        right_style = STYLE_TOKEN_RE.fullmatch(right.group(0))
        if (
            left.end() == right.start()
            and left_style
            and right_style
            and left_style.group(2) == "CLOSE"
            and right_style.group(2) == "OPEN"
            and left_style.group(1) in styles
            and styles.get(left_style.group(1))
            == styles.get(right_style.group(1))
        ):
            coalesced_style_edges.update((index, index + 1))

    def flush_math() -> None:
        if pending_math:
            # A space separates TeX control words without introducing a second
            # adjacent dollar delimiter, which Markdown can read as display math.
            output.append("$" + " ".join(pending_math) + "$")
            pending_math.clear()

    cursor = 0
    for index, match in enumerate(matches):
        literal = text[cursor : match.start()]
        if literal:
            flush_math()
            output.append(literal)
        token = match.group(0)
        fragment = fragments.get(token, {})
        if fragment.get("kind") == "math":
            tex, _ = resolve_inline_math(
                {**fragment, "token": token}, reviews or {}
            )
            pending_math.append(tex)
            cursor = match.end()
            continue
        flush_math()
        if token == "[[BR]]":
            output.append("  \n")
        elif token == "[[PAR]]":
            output.append("\n\n")
        elif token in fragments:
            fragment = fragments[token]
            kind = str(fragment.get("kind") or "")
            value = str(fragment.get("text") or "")
            if kind == "citation":
                if output:
                    output[-1] = re.sub(r"\s+$", "", output[-1])
                output.append(f"<sup>{value}</sup>")
            elif kind == "footnote-marker":
                output.append(f"<sup>{value}</sup>")
            else:
                output.append(value)
        else:
            style_match = STYLE_TOKEN_RE.fullmatch(token)
            if not style_match or style_match.group(1) not in styles:
                raise ValueError(f"{unit['id']}: unknown rich token {token}")
            key, edge = style_match.groups()
            if edge == "OPEN":
                style_stack.append(key)
            elif not style_stack or style_stack.pop() != key:
                raise ValueError(f"{unit['id']}: reversed or crossed style token {token}")
            if index not in coalesced_style_edges:
                marker = "**" if styles[key] == "bold" else "*"
                output.append(marker)
        cursor = match.end()
    flush_math()
    output.append(text[cursor:])
    if style_stack:
        raise ValueError(f"{unit['id']}: unclosed style token")
    rendered = "".join(output)
    if CONTROL_TOKEN_RE.search(rendered) or "[[UNRESOLVED:" in rendered:
        raise ValueError(f"{unit['id']}: unresolved rich token")
    if unit.get("block_style") == "italic":
        rendered = f"<em>{rendered}</em>"
    return rendered.strip()


def export_markdown(
    manifest: dict[str, Any],
    units: list[dict[str, Any]],
    translations: list[dict[str, Any]],
    math_review: dict[str, Any] | None = None,
) -> str:
    translation_map: dict[str, dict[str, Any]] = {}
    for record in translations:
        record_id = str(record.get("id") or "")
        if not record_id or record_id in translation_map:
            raise ValueError(f"Invalid or duplicate translation id: {record_id!r}")
        translation_map[record_id] = record

    required = {
        str(unit["id"]) for unit in units if unit.get("translatable") is True
    }
    if set(translation_map) != required:
        missing = sorted(required - set(translation_map))
        extra = sorted(set(translation_map) - required)
        raise ValueError(
            f"Translation ID mismatch; missing={missing[:10]}, extra={extra[:10]}"
        )

    title = str(manifest.get("source_title") or "Paper")
    lines = [f"# {title}", "", "> 中文翻译正文（仅含经复核的可翻译正文范围）", ""]
    footnotes: list[str] = []
    ordered_units = sorted(units, key=lambda item: int(item["order"]))
    positions = {
        str(unit["id"]): index for index, unit in enumerate(ordered_units)
    }
    boundary = dict(manifest.get("boundary") or {})
    start = positions.get(str(boundary.get("introduction_id") or ""), 0)
    stop = positions.get(
        str(boundary.get("post_body_stop_id") or ""),
        len(ordered_units),
    )
    review_entries = dict((math_review or {}).get("entries") or {})
    consumed_display_ids: set[str] = set()
    for unit in ordered_units[start:stop]:
        unit_id = str(unit["id"])
        if unit_id in consumed_display_ids or unit.get("render_suppressed"):
            continue
        display = dict(review_entries.get(unit_id) or {})
        if display.get("kind") == "display" or unit.get("kind") == "equation":
            tex = resolve_display_math(unit, review_entries)
            lines.extend(("$$", tex, "$$", ""))
            consumed_display_ids.update(
                str(value) for value in display.get("source_ids") or [unit_id]
            )
            continue
        if not unit.get("translatable"):
            continue
        record = translation_map[unit_id]
        translated = str(record.get("translated_text") or "").strip()
        if not translated:
            raise ValueError(f"{unit['id']}: translated_text is empty")
        restored = rich_text_to_markdown(
            unit, restore_tokens(unit, translated), review_entries
        )
        if unit.get("kind") == "footnote":
            footnotes.append(restored)
            continue
        if unit.get("kind") == "heading":
            lines.extend((f"## {restored}", ""))
        else:
            lines.extend((restored, ""))
    if footnotes:
        lines.extend(("---", "", "## 脚注", ""))
        for footnote in footnotes:
            lines.extend((footnote, ""))
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export Codex-authored paper translations as Markdown."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--translations", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--title", help="Override the source title in Markdown only.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_path = args.manifest.expanduser().resolve()
    translations_path = args.translations.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if args.title:
        manifest["source_title"] = args.title
    if manifest.get("boundary", {}).get("needs_boundary_review"):
        raise SystemExit("Translation boundary review is incomplete.")
    units_path = manifest_path.parent / manifest["paths"]["translation_units"]
    math_review: dict[str, Any] | None = None
    math_review_name = str(
        (manifest.get("paths") or {}).get("math_review") or ""
    )
    if math_review_name:
        math_review_path = manifest_path.parent / math_review_name
        if not math_review_path.exists():
            raise SystemExit(f"Math review file is missing: {math_review_path}")
        loaded = json.loads(math_review_path.read_text(encoding="utf-8"))
        if loaded.get("source_sha256") != manifest.get("source_sha256"):
            raise SystemExit("Math review does not match the source PDF.")
        math_review = loaded
    markdown = export_markdown(
        manifest,
        read_jsonl(units_path),
        read_jsonl(translations_path),
        math_review,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8", newline="\n")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
