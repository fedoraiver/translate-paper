# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Export a compact, source-bound translation assignment without modifying state."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from translation_checkpoint import load_state, read_records, validate_text


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compact_unit(unit: dict, reviews: dict) -> dict:
    fragments = {}
    for token, fragment in (unit.get("inline_fragments") or {}).items():
        compact = {key: fragment[key] for key in
                   ("kind", "text", "tex", "review_status", "footnote_id")
                   if key in fragment}
        review = reviews.get(token, {})
        for key in ("tex", "review_status"):
            if key in review:
                compact[key] = review[key]
        fragments[token] = compact
    return {
        "id": unit["id"], "page": unit.get("page"), "kind": unit.get("kind"),
        "order": unit.get("order"),
        "block_style": unit.get("block_style"),
        "section": unit.get("section", ""),
        "protected_text": unit["protected_text"],
        "protected_tokens": unit.get("protected_tokens", {}),
        "inline_fragments": fragments,
        "style_tokens": unit.get("style_tokens", {}),
    }


def build_packet(manifest_path: Path, translations: Path, start_id: str | None = None,
                 end_id: str | None = None, max_words: int = 3000,
                 include_completed: bool = False) -> dict:
    if not 1 <= max_words <= 4000:
        raise ValueError("max_words must be between 1 and 4000")
    manifest_path = manifest_path.resolve()
    units, records = load_state(manifest_path, translations)
    for unit, record in zip(units, records):
        validate_text(unit, record.get("translated_text", ""))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    work = manifest_path.parent
    paths = manifest["paths"]
    hashes = {"manifest": sha256(manifest_path),
              "translation_units": sha256(work / paths["translation_units"])}
    reviews = {}
    if paths.get("math_review"):
        review_path = work / paths["math_review"]
        payload = json.loads(review_path.read_text(encoding="utf-8-sig"))
        if payload.get("source_sha256") != manifest["source_sha256"]:
            raise ValueError("Math review does not match source hash")
        reviews = payload.get("entries", {})
        if not isinstance(reviews, dict):
            raise ValueError("Math review entries must be a token-keyed object")
        hashes["math_review"] = sha256(review_path)
    glossary_path = work / "glossary.json"
    if glossary_path.exists():
        hashes["glossary"] = sha256(glossary_path)

    positions = {u["id"]: i for i, u in enumerate(units)}
    for label, value in (("start_id", start_id), ("end_id", end_id)):
        if value is not None and value not in positions:
            raise ValueError(f"Unknown translatable {label}: {value}")
    completed = {r["id"] for r in records if r.get("translated_text", "").strip()}
    start = positions[start_id] if start_id else next(
        (i for i, u in enumerate(units) if include_completed or u["id"] not in completed),
        len(units),
    )
    stop = positions[end_id] + 1 if end_id else len(units)
    if end_id and start >= stop:
        raise ValueError("end_id precedes the selected start")
    if start_id in completed and not include_completed:
        raise ValueError("Start unit is completed; use include_completed for deliberate review")

    selected = []
    words = 0
    for unit in units[start:stop]:
        # Keep one contiguous assignment; do not silently jump over completed work.
        if unit["id"] in completed and not include_completed:
            break
        count = len(re.findall(r"\S+", unit.get("source_text", "")))
        if selected and words + count > max_words:
            break
        selected.append(unit)
        words += count
    end = start + len(selected)
    neighbors = ([units[start - 1]] if selected and start else [])
    if selected and end < len(units):
        neighbors.append(units[end])
    invariants = []
    if selected:
        all_units = read_records(work / paths["translation_units"])
        all_positions = {u["id"]: i for i, u in enumerate(all_units)}
        left = all_positions[units[max(0, start - 1)]["id"]]
        right = all_positions[units[min(len(units) - 1, end)]["id"]]
        for unit in all_units[left:right + 1]:
            if (unit.get("translatable") or unit.get("render_suppressed")
                    or unit.get("kind") in {"header", "footer"}):
                continue
            invariant = {key: unit.get(key) for key in
                         ("id", "page", "kind", "order", "source_text")}
            review = reviews.get(unit["id"], {})
            if review.get("tex"):
                invariant["tex"] = review["tex"]
                invariant["review_status"] = review.get("review_status")
            invariants.append(invariant)
    source = Path(manifest["source_pdf"])
    source = source if source.is_absolute() else work / source
    return {
        "schema_version": 1, "source_sha256": manifest["source_sha256"],
        "source_pdf": str(source.resolve()), "manifest": str(manifest_path),
        "input_hashes": hashes,
        "glossary": str(glossary_path.resolve()) if glossary_path.exists() else None,
        "source_preview": str((work / paths["source_preview"]).resolve())
        if paths.get("source_preview") else None,
        "assigned_ids": [u["id"] for u in selected],
        "source_words": words,
        "oversized_single_unit": len(selected) == 1 and words > max_words,
        "next_id": units[end]["id"] if selected and end < stop else None,
        "units": [compact_unit(u, reviews) for u in selected],
        "context_only": [compact_unit(u, reviews) for u in neighbors],
        "invariants_context_only": invariants,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--translations", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--start-id")
    parser.add_argument("--end-id")
    parser.add_argument("--max-words", type=int, default=3000)
    parser.add_argument("--include-completed", action="store_true")
    args = parser.parse_args()
    try:
        packet = build_packet(args.manifest, args.translations, args.start_id,
                              args.end_id, args.max_words, args.include_completed)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Never replace canonical files or a packet already assigned to a worker.
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(packet, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(json.dumps({"status": "pass", "output": str(args.output.resolve()),
                          "units": len(packet["units"]),
                          "source_words": packet["source_words"],
                          "next_id": packet["next_id"],
                          "oversized_single_unit": packet["oversized_single_unit"]},
                         ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"status": "fail", "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
