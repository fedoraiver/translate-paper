# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Validate the Chinese paper summary's length and required structure."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.+?)\s*$", re.MULTILINE)
REQUIRED_SECTIONS = {
    "文献信息": ("文献信息", "书目信息", "论文信息"),
    "一句话结论": ("一句话结论", "一句话总结"),
    "研究问题": ("研究问题", "研究目标"),
    "方法与数据": ("方法与数据", "研究方法", "方法和数据"),
    "核心贡献": ("核心贡献", "主要贡献"),
    "主要结果": ("主要结果", "研究结果", "实验结果"),
    "局限": ("局限", "局限性", "研究局限"),
    "复现材料": ("复现材料", "可复现性", "复现信息"),
    "关键词": ("关键词", "关键术语"),
}


def normalized_heading(value: str) -> str:
    value = re.sub(r"[*_`：:]+", "", value)
    return re.sub(r"\s+", "", value).casefold()


def validate_summary(
    markdown: str, minimum: int = 800, maximum: int = 1500
) -> dict[str, object]:
    headings = [normalized_heading(value) for value in HEADING_RE.findall(markdown)]
    missing = [
        canonical
        for canonical, aliases in REQUIRED_SECTIONS.items()
        if not any(
            normalized_heading(alias) in heading
            for alias in aliases
            for heading in headings
        )
    ]
    body = re.sub(r"^\s*#{1,6}\s+", "", markdown, flags=re.MULTILINE)
    body = re.sub(r"[*_`>\[\]()\-]", "", body)
    character_count = len(re.sub(r"\s+", "", body))
    han_count = len(HAN_RE.findall(body))
    errors: list[str] = []
    if character_count < minimum or character_count > maximum:
        errors.append(
            f"Summary length is {character_count}; expected {minimum}-{maximum}."
        )
    if missing:
        errors.append("Missing sections: " + ", ".join(missing))
    if han_count < max(100, character_count // 3):
        errors.append("Summary does not contain enough Chinese text.")
    return {
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "metrics": {
            "characters": character_count,
            "han_characters": han_count,
            "headings": headings,
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a structured 800-1500 character Chinese summary."
    )
    parser.add_argument("--summary", required=True, type=Path)
    parser.add_argument("--min-characters", type=int, default=800)
    parser.add_argument("--max-characters", type=int, default=1500)
    parser.add_argument("--report", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.min_characters < 1 or args.max_characters < args.min_characters:
        raise SystemExit("Invalid summary character range.")
    summary_path = args.summary.expanduser().resolve()
    report = validate_summary(
        summary_path.read_text(encoding="utf-8"),
        args.min_characters,
        args.max_characters,
    )
    if args.report:
        report_path = args.report.expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
