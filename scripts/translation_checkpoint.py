# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Validate and atomically save model-authored translation batches; no translation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


TOKEN_RE = re.compile(r"\[\[[^\[\]\r\n]+\]\]")
HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
SOURCE_FIELDS = (
    "source_text", "protected_text", "protected_tokens", "inline_fragments",
    "style_tokens",
)


def read_records(path: Path) -> list[dict]:
    records = []
    seen = set()
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise ValueError(f"{path}:{number}: expected object with string id")
        unit_id = record["id"]
        if not unit_id or unit_id in seen:
            raise ValueError(f"{path}:{number}: empty or duplicate id {unit_id!r}")
        seen.add(unit_id)
        records.append(record)
    return records


def validate_text(unit: dict, text: str) -> None:
    if not isinstance(text, str):
        raise ValueError(f"{unit['id']}: translated_text must be a string")
    if not text.strip():
        return
    expected = TOKEN_RE.findall(unit["protected_text"])
    actual = TOKEN_RE.findall(text)
    if expected != actual:
        raise ValueError(
            f"{unit['id']}: protected/style token sequence differs; "
            f"expected {expected}, got {actual}"
        )
    if not HAN_RE.search(TOKEN_RE.sub("", text)):
        raise ValueError(
            f"{unit['id']}: no Chinese prose; review formula-only source units "
            "instead of adding filler"
        )


def load_state(manifest_path: Path, destination: Path) -> tuple[list[dict], list[dict]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    work = manifest_path.parent
    source = Path(manifest["source_pdf"])
    if not source.is_absolute():
        source = work / source
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != manifest["source_sha256"]:
        raise ValueError("Source PDF hash differs from manifest; prepare/review again")
    units = [u for u in read_records(work / manifest["paths"]["translation_units"])
             if u.get("translatable") is True]
    state_path = destination if destination.exists() else (
        work / manifest["paths"]["translations_template"]
    )
    records = read_records(state_path)
    by_id = {r["id"]: r for r in records}
    if set(by_id) != {u["id"] for u in units}:
        raise ValueError("Checkpoint inventory differs; reconcile through prepare_paper.py")
    for unit in units:
        record = by_id[unit["id"]]
        for field in SOURCE_FIELDS:
            if record.get(field) != unit.get(field):
                raise ValueError(f"{unit['id']}: stale {field}; reconcile source review")
        if not isinstance(record.get("translated_text", ""), str):
            raise ValueError(f"{unit['id']}: translated_text must be a string")
    return units, [by_id[u["id"]] for u in units]


def merge_batch(units: list[dict], records: list[dict], batch: list[dict],
                replace_completed: bool = False) -> list[dict]:
    by_id = {u["id"]: u for u in units}
    candidate = {r["id"]: dict(r) for r in records}
    seen = set()
    for entry in batch:
        unit_id = entry.get("id")
        if unit_id in seen or unit_id not in by_id:
            raise ValueError(f"Unknown or duplicate batch id: {unit_id}")
        seen.add(unit_id)
        if set(entry) != {"id", "translated_text"}:
            raise ValueError(f"{unit_id}: batch fields must be id and translated_text only")
        text = entry["translated_text"]
        validate_text(by_id[unit_id], text)
        if not text.strip():
            raise ValueError(f"{unit_id}: empty batch translation")
        old = candidate[unit_id].get("translated_text", "")
        if old.strip() and old != text and not replace_completed:
            raise ValueError(f"{unit_id}: completed text differs; use --replace-completed "
                             "only for an intentional revision")
        candidate[unit_id]["translated_text"] = text
    if not seen:
        raise ValueError("Batch is empty")
    return [candidate[u["id"]] for u in units]


def atomic_write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, delete=False) as stream:
            name = stream.name
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and Path(name).exists():
            Path(name).unlink()


def checkpoint(manifest: Path, destination: Path, batch_path: Path | None = None,
               replace_completed: bool = False, require_complete: bool = False) -> dict:
    units, records = load_state(manifest, destination)
    if batch_path:
        if batch_path.resolve() == destination.resolve():
            raise ValueError("Batch and canonical checkpoint must be separate files")
        records = merge_batch(units, records, read_records(batch_path), replace_completed)
    for unit, record in zip(units, records):
        validate_text(unit, record.get("translated_text", ""))
    remaining = [r["id"] for r in records if not r.get("translated_text", "").strip()]
    if require_complete and remaining:
        raise ValueError(f"{len(remaining)} untranslated units; next: {remaining[0]}")
    if batch_path:
        atomic_write(destination, records)
    return {
        "status": "pass", "complete": not remaining,
        "total": len(records), "completed": len(records) - len(remaining),
        "remaining": len(remaining), "next_id": remaining[0] if remaining else None,
        "translations": str(destination), "saved": batch_path is not None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--translations", required=True, type=Path)
    parser.add_argument("--batch", type=Path, help="JSONL with id and translated_text only")
    parser.add_argument("--replace-completed", action="store_true")
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()
    try:
        report = checkpoint(args.manifest.resolve(), args.translations.resolve(),
                            args.batch, args.replace_completed, args.require_complete)
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"status": "fail", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
