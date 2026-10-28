"""One small recovery pointer; translation JSONL remains authoritative."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, delete=False) as stream:
            name = stream.name
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if name and Path(name).exists():
            Path(name).unlink()


def state_path(manifest_path: Path, manifest: dict | None = None) -> Path:
    manifest = manifest or json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    path = Path(manifest.get("paths", {}).get("run_state", "run-state.json"))
    return path if path.is_absolute() else manifest_path.parent / path


def read_state(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    path = state_path(manifest_path, manifest)
    state = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {
        "schema_version": 1, "source_sha256": manifest["source_sha256"],
        "active_manifest": str(manifest_path.resolve()), "stage": "prepare",
        "assignments": {}, "accepted_reports": {}, "open_findings": [],
        "next_action": "Review prepared source and run preflight",
    }
    if state.get("source_sha256") != manifest["source_sha256"]:
        raise ValueError("Recovery state belongs to a different source PDF")
    return state


def update_state(manifest_path: Path, *, stage: str | None = None,
                 next_action: str | None = None, **fields) -> dict:
    state = read_state(manifest_path)
    state.update(fields)
    state["active_manifest"] = str(manifest_path.resolve())
    if stage is not None:
        state["stage"] = stage
    if next_action is not None:
        state["next_action"] = next_action
    atomic_json(state_path(manifest_path), state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--stage")
    parser.add_argument("--next-action")
    parser.add_argument("--fields", type=Path,
                        help="JSON metadata: assignments, input hashes, findings and reports")
    args = parser.parse_args()
    try:
        fields = json.loads(args.fields.read_text(encoding="utf-8-sig")) if args.fields else {}
        forbidden = set(fields) - {"assignments", "input_hashes", "accepted_reports",
                                   "open_findings", "repair_revision", "batch_progress"}
        if forbidden:
            raise ValueError(f"Unsupported recovery metadata: {sorted(forbidden)}")
        result = (update_state(args.manifest.resolve(), stage=args.stage,
                               next_action=args.next_action, **fields)
                  if args.stage or args.next_action or args.fields
                  else read_state(args.manifest.resolve()))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "fail", "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
