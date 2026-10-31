# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Plan source repairs, or publish an isolated revision with affected prose reopened."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from functools import partial
from pathlib import Path

from assignment_identity import digest
from paper_state import atomic_json, read_state, state_path, update_state
from source_review import _completed_unit_contract
from translation_checkpoint import SOURCE_FIELDS, atomic_write, load_state, read_records, source_value


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def input_paths(manifest_path: Path) -> dict[str, Path]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    work = manifest_path.parent
    result = {"manifest": manifest_path, "translations": work / "translations.jsonl"}
    for key in ("translation_units", "translations_template", "boundary_review",
                "source_review", "math_review", "visual_layout", "glossary",
                "source_review_report", "paper_preflight"):
        value = manifest.get("paths", {}).get(key)
        if value:
            result[key] = work / value
    for key, value in manifest.get("paths", {}).items():
        if isinstance(value, str) and (work / value).is_file():
            result.setdefault(key, work / value)
    result["source_pdf"] = work / manifest["source_pdf"]
    return {key: path.resolve() for key, path in result.items() if path.is_file()}


def prepare_candidate(work: Path, *, ocr: str = "auto") -> None:
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8-sig"))
    command = ["uv", "run", "--script", str(Path(__file__).with_name("prepare_paper.py")),
               "--input", manifest["source_pdf"], "--work-dir", str(work),
               "--source-review", str(work / "source-review.json"),
               "--boundary-review", str(work / "boundary-review.json"), "--ocr", ocr]
    with (work / "repair-prepare.log").open("w", encoding="utf-8") as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONUTF8": "1"}, check=False)
    if result.returncode:
        raise ValueError(f"Repair preparation failed; see {work / 'repair-prepare.log'}")


def repair(manifest_path: Path, proposal: Path, *, output_dir: Path | None = None,
           apply: bool = False, builder=prepare_candidate) -> dict:
    manifest_path, proposal = manifest_path.resolve(), proposal.resolve()
    original = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    review = json.loads(proposal.read_text(encoding="utf-8-sig"))
    proposal_hash = file_hash(proposal)
    if review.get("source_sha256") != original["source_sha256"]:
        raise ValueError("Repair proposal belongs to a different source PDF")
    if review.get("reviewed") is not True or not review.get("reason"):
        raise ValueError("Repair source proposal must be reviewed with a reason")
    if apply and (output_dir is None or output_dir.resolve().exists()):
        raise ValueError("--apply requires a new, non-existing --output-dir")
    paths = input_paths(manifest_path)
    if "translations" not in paths:
        raise ValueError("Repair requires an existing canonical translations.jsonl")
    load_state(manifest_path, paths["translations"])
    before_hashes = {key: file_hash(path) for key, path in paths.items()}
    old_units = {r["id"]: r for r in read_records(paths["translation_units"])}
    old_records = {r["id"]: r for r in read_records(paths["translations"])}
    root_state = state_path(manifest_path).resolve()
    read_state(manifest_path)  # Check an existing recovery pointer before doing work.
    state_hash = file_hash(root_state) if root_state.exists() else None
    parent = output_dir.resolve().parent if apply else None
    if parent:
        parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="paper-repair-", dir=parent) as temporary:
        temporary_path = Path(temporary)
        work = temporary_path / "revision"
        baseline = work / "baseline"
        baseline.mkdir(parents=True)
        names = {"manifest": "manifest.json", "translation_units": "translation-units.jsonl",
                 "translations_template": "translations.template.jsonl",
                 "boundary_review": "boundary-review.json", "source_review": "source-review.json",
                 "math_review": "math-review.json", "visual_layout": "visual-layout.json",
                 "glossary": "glossary.json", "translations": "translations.jsonl"}
        for key, name in names.items():
            if key in paths:
                shutil.copyfile(paths[key], baseline / name)
                if key != "translations":
                    shutil.copyfile(paths[key], work / name)
        # The probe contains no completed prose. The original and baseline remain immutable.
        probe_manifest = dict(original)
        probe_manifest["source_pdf"] = str(paths["source_pdf"])
        probe_manifest["paths"] = {**original.get("paths", {}),
                                  **{k: v for k, v in names.items() if k in paths},
                                  "work_dir": str(work), "run_state": str(root_state)}
        probe_manifest["paths"].pop("paper_preflight", None)
        atomic_json(work / "manifest.json", probe_manifest)
        atomic_json(work / "source-review.json", review)
        builder(work)
        candidate_manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8-sig"))
        if candidate_manifest.get("source_sha256") != original["source_sha256"]:
            raise ValueError("Repair preparation changed source identity")
        source_report = json.loads((work / "source-review-report.json").read_text(encoding="utf-8-sig"))
        visual_report = json.loads((work / "visual-layout.json").read_text(encoding="utf-8-sig"))
        if source_report.get("status") != "pass" or visual_report.get("status") != "pass":
            raise ValueError("Repair source or visual inventory is not passing")
        new_units = {r["id"]: r for r in read_records(work / candidate_manifest["paths"]["translation_units"])}
        templates = read_records(work / candidate_manifest["paths"]["translations_template"])
        changed = sorted(unit_id for unit_id, old in old_units.items()
                         if unit_id not in new_units or
                         _completed_unit_contract(old) != _completed_unit_contract(new_units[unit_id]))
        reopened = sorted(unit_id for unit_id in changed
                          if old_records.get(unit_id, {}).get("translated_text", "").strip())
        preserved, records = [], []
        for template in templates:
            unit_id = template["id"]
            old = old_records.get(unit_id)
            if old is not None and unit_id not in changed:
                if any(source_value(old, key) != source_value(template, key) for key in SOURCE_FIELDS):
                    raise ValueError(f"Unaccounted checkpoint metadata change: {unit_id}")
                records.append(old)
                if old.get("translated_text", "").strip():
                    preserved.append(unit_id)
            else:
                records.append({**template, "translated_text": ""})
        report = {"schema_version": 1, "status": "pass", "executed": apply,
                  "source_sha256": original["source_sha256"],
                  "input_hashes": before_hashes, "proposal_sha256": proposal_hash,
                  "changed_ids": changed, "reopened_completed_ids": reopened,
                  "new_ids": sorted(set(new_units) - set(old_units)),
                  "removed_ids": sorted(set(old_units) - set(new_units)),
                  "preserved_completed_ids": preserved,
                  "preservation": "identical parsed record fields; serialization may differ",
                  "remaining_ids": [r["id"] for r in records if not r.get("translated_text", "").strip()]}
        report["plan_sha256"] = digest(report)
        if any(file_hash(path) != before_hashes[key] for key, path in paths.items()):
            raise ValueError("Baseline changed during repair; no revision published")
        if file_hash(proposal) != proposal_hash:
            raise ValueError("Source proposal changed during repair; no revision published")
        if (file_hash(root_state) if root_state.exists() else None) != state_hash:
            raise ValueError("Recovery pointer changed during repair; no revision published")
        if not apply:
            return report
        destination = output_dir.resolve()
        # Repoint only manifest paths referring to the temporary revision, never source content.
        candidate_manifest["paths"]["run_state"] = str(root_state)
        candidate_manifest["paths"]["work_dir"] = str(destination)
        for key, value in candidate_manifest["paths"].items():
            if isinstance(value, str) and value.startswith(str(work) + os.sep):
                candidate_manifest["paths"][key] = str(destination / Path(value).relative_to(work))
        atomic_json(work / "manifest.json", candidate_manifest)
        atomic_write(work / "translations.jsonl", records)
        atomic_json(work / "repair-plan.json", report)
        load_state(work / "manifest.json", work / "translations.jsonl")
        os.rename(work, destination)
        report["revision_dir"] = str(destination)
        try:
            update_state(destination / "manifest.json", stage="repair",
                         next_action="Review changed math and translate only remaining repair anchors",
                         repair_revision={"directory": str(destination), "plan_sha256": report["plan_sha256"]},
                         input_hashes=before_hashes, open_findings=report["remaining_ids"])
        except OSError as error:
            report["state_warning"] = f"Revision published; recovery pointer update failed: {error}"
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--source-review", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--ocr", choices=("auto", "off", "force"), default="auto")
    args = parser.parse_args()
    try:
        if args.report:
            forbidden = set(input_paths(args.manifest).values()) | {
                args.source_review.resolve(), state_path(args.manifest).resolve()}
            if args.report.resolve() in forbidden:
                raise ValueError("Report must not overwrite repair inputs")
            if args.output_dir and (args.report.resolve() == args.output_dir.resolve()
                                    or args.output_dir.resolve() in args.report.resolve().parents):
                raise ValueError("Use the revision's generated repair-plan.json; report must be outside the revision")
        result = repair(args.manifest, args.source_review, output_dir=args.output_dir, apply=args.apply,
                        builder=partial(prepare_candidate, ocr=args.ocr))
        if args.report:
            atomic_json(args.report, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "fail", "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
