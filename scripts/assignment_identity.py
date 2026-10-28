"""Content-scoped identity for model assignments; no translation or state writes."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from source_structure import flow_metadata

UNIT_FIELDS = (
    "id", "page", "kind", "section", "source_text", "protected_text",
    "protected_tokens", "inline_fragments", "style_tokens", "block_style",
    "flow_role", "list_id", "list_level", "source_marker", "flow_reviewed",
)


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def glossary_terms(payload: dict) -> dict:
    terms = payload.get("terms", payload)
    if not isinstance(terms, dict):
        raise ValueError("Glossary terms must be an object")
    return {key: value for key, value in terms.items()
            if isinstance(key, str) and isinstance(value, str)}


def relevant_terms(terms: dict, text: str) -> dict:
    return {term: value for term, value in sorted(terms.items())
            if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text,
                         flags=re.IGNORECASE)}


def unit_identity(unit: dict) -> str:
    payload = {**unit, **flow_metadata(unit)}
    return digest({key: payload.get(key) for key in UNIT_FIELDS})


def build_assignment_identity(manifest_path: Path, assigned_ids: list[str],
                              context_ids: list[str] | None = None) -> dict:
    manifest = read_json(manifest_path)
    work = manifest_path.parent
    units = {unit["id"]: unit for unit in
             (json.loads(line) for line in
              (work / manifest["paths"]["translation_units"]).read_text(
                  encoding="utf-8-sig").splitlines() if line.strip())}
    if len(set(assigned_ids)) != len(assigned_ids):
        raise ValueError("Duplicate assignment IDs")
    reviews = {}
    review_name = manifest["paths"].get("math_review")
    if review_name:
        payload = read_json(work / review_name)
        if payload.get("source_sha256", manifest["source_sha256"]) != manifest["source_sha256"]:
            raise ValueError("Mathematical review belongs to a different source PDF")
        reviews = payload.get("entries", {})
    selected = []
    for unit_id in assigned_ids:
        unit = units.get(unit_id)
        if not unit or not unit.get("translatable") or unit.get("render_suppressed"):
            raise ValueError(f"Assignment unit unavailable: {unit_id}")
        selected.append(unit)
    context_ids = list(dict.fromkeys(context_ids or []))
    context = []
    for unit_id in context_ids:
        unit = units.get(unit_id)
        if not unit or unit.get("render_suppressed"):
            raise ValueError(f"Assignment context unavailable: {unit_id}")
        context.append(unit)
    unit_hashes = {u["id"]: unit_identity(u) for u in selected}
    context_hashes = {u["id"]: unit_identity(u) for u in context}
    tokens = {token for u in [*selected, *context] for token, fragment in
              (u.get("inline_fragments") or {}).items()
              if fragment.get("kind") == "math"}
    math_hashes = {token: digest(reviews.get(token)) for token in sorted(tokens)}
    for review_id, entry in reviews.items():
        if entry.get("kind") == "display" and (
                review_id in context_ids or set(entry.get("source_ids") or []) & set(context_ids)):
            math_hashes[review_id] = digest(entry)
    glossary_path = work / manifest["paths"].get("glossary", "glossary.json")
    terms = glossary_terms(read_json(glossary_path)) if glossary_path.exists() else {}
    body = "\n".join(u.get("source_text", "") for u in [*selected, *context])
    result = {"schema_version": 1, "source_sha256": manifest["source_sha256"],
              "assigned_ids": assigned_ids, "unit_hashes": unit_hashes,
              "context_ids": context_ids, "context_hashes": context_hashes,
              "math_hashes": math_hashes, "glossary_terms": relevant_terms(terms, body)}
    return {**result, "sha256": digest(result)}


def validate_assignment_identity(packet: dict, manifest_path: Path,
                                 batch_ids: list[str]) -> None:
    expected = packet.get("assignment_identity")
    if not isinstance(expected, dict) or expected.get("schema_version") != 1:
        raise ValueError("Packet has no supported assignment identity; regenerate it")
    ids = expected.get("assigned_ids")
    if not isinstance(ids, list) or batch_ids != ids or packet.get("assigned_ids") != ids:
        raise ValueError("Batch IDs/order differ from assigned packet")
    unsigned = {key: value for key, value in expected.items() if key != "sha256"}
    if expected.get("sha256") != digest(unsigned):
        raise ValueError("Assignment identity checksum differs")
    actual = build_assignment_identity(manifest_path, ids, expected.get("context_ids", []))
    for field in ("source_sha256", "unit_hashes", "context_hashes", "math_hashes", "glossary_terms"):
        if actual[field] != expected.get(field):
            raise ValueError(f"Stale assignment {field}; regenerate/review affected packet")
