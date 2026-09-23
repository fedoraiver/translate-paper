#!/usr/bin/env python3
"""Track, package, resume, and audit translate-paper batches.

The script performs deterministic state and artifact operations only. It never
translates text and never creates, replaces, or deletes Zotero items.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import zotero_ingest
from check_visual_review import check_review_bundle

SCHEMA_VERSION = 1
STAGES = (
    "queued",
    "prepared",
    "translated",
    "strict_validated",
    "packaged",
    "zotero_verified",
)
TERMINAL_STAGES = {"zotero_verified", "skipped_existing"}
ALL_STAGES = {*STAGES, "skipped_existing"}
STAGE_INDEX = {stage: index for index, stage in enumerate(STAGES)}
FINAL_NAMES = {
    "source_pdf": "原文 PDF.pdf",
    "translated_pdf": "中文翻译.pdf",
    "translated_markdown": "中文翻译正文.md",
    "summary": "中文论文总结.md",
    "metadata": "zotero-item.json",
    "layout_report": "中文翻译.layout.json",
    "strict_validation": "translation-validation.json",
    "quick_validation": "quick-validation.json",
    "summary_validation": "summary-validation.json",
    "visual_review": "visual-review.json",
    "source_review_report": "source-review-report.json",
}
REQUIRED_ARTIFACTS = tuple(
    key for key in FINAL_NAMES if key != "source_review_report"
)


class BatchWorkflowError(RuntimeError):
    """The batch ledger or one of its artifacts is unsafe or incomplete."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BatchWorkflowError(f"Cannot read {path}: {error}") from error
    if not isinstance(payload, dict):
        raise BatchWorkflowError(f"{path} must contain a JSON object.")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _resolve(base: Path, value: Any) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _relative_or_absolute(base: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path.resolve())


def load_ledger(path: Path) -> tuple[dict[str, Any], Path]:
    ledger_path = path.expanduser().resolve()
    payload = _read_json(ledger_path)
    if int(payload.get("schema_version") or 0) != SCHEMA_VERSION:
        raise BatchWorkflowError(
            f"Unsupported batch-run schema: {payload.get('schema_version')!r}"
        )
    if not str(payload.get("batch_id") or "").strip():
        raise BatchWorkflowError("batch-run.json requires batch_id.")
    source_list = payload.get("source_list")
    if not isinstance(source_list, dict):
        raise BatchWorkflowError("batch-run.json requires source_list.")
    if not str(source_list.get("section") or "").strip():
        raise BatchWorkflowError("source_list.section is required.")
    if not str(source_list.get("path") or "").strip():
        raise BatchWorkflowError("source_list.path is required.")
    target = payload.get("zotero_target")
    if payload.get("zotero_enabled", True) is not False and (
        not isinstance(target, dict)
        or not str(target.get("id") or "").strip()
        or not str(target.get("name") or "").strip()
    ):
        raise BatchWorkflowError(
            "A Zotero-enabled batch requires zotero_target.id and "
            "zotero_target.name."
        )
    papers = payload.get("papers")
    if not isinstance(papers, list) or not papers:
        raise BatchWorkflowError("batch-run.json requires at least one paper.")
    orders: set[int] = set()
    slugs: set[str] = set()
    for index, paper in enumerate(papers, start=1):
        if not isinstance(paper, dict):
            raise BatchWorkflowError(f"Paper {index} must be an object.")
        order = int(paper.get("order") or 0)
        slug = str(paper.get("slug") or "")
        stage = str(paper.get("stage") or "queued")
        if order <= 0 or order in orders:
            raise BatchWorkflowError(f"Paper {index} has an invalid/duplicate order.")
        if not slug or slug in slugs:
            raise BatchWorkflowError(f"Paper {index} has an invalid/duplicate slug.")
        if not str(paper.get("title") or "").strip():
            raise BatchWorkflowError(f"{slug}: title is required.")
        if stage not in ALL_STAGES:
            raise BatchWorkflowError(f"{slug}: unsupported stage {stage!r}.")
        if not paper.get("source_pdf") or not paper.get("work_dir"):
            raise BatchWorkflowError(f"{slug}: source_pdf and work_dir are required.")
        orders.add(order)
        slugs.add(slug)
        paper["stage"] = stage
    payload["papers"] = sorted(papers, key=lambda item: int(item["order"]))
    return payload, ledger_path


def _paper_paths(
    ledger: dict[str, Any],
    ledger_path: Path,
    paper: dict[str, Any],
) -> dict[str, Path]:
    base = ledger_path.parent
    source = _resolve(base, paper["source_pdf"])
    work_dir = _resolve(base, paper["work_dir"])
    output_root = _resolve(base, ledger.get("output_root") or "output/pdf")
    final_dir = _resolve(
        base,
        paper.get("final_dir") or output_root / str(paper["slug"]),
    )
    declared = dict(paper.get("artifacts") or {})
    defaults = {
        "source_pdf": source,
        "translated_pdf": work_dir / "中文翻译.pdf",
        "translated_markdown": work_dir / "中文翻译正文.md",
        "summary": work_dir / "中文论文总结.md",
        "metadata": work_dir / "zotero-item.json",
        "layout_report": work_dir / "中文翻译.layout.json",
        "strict_validation": work_dir / "translated-preview" / "validation-report.json",
        "quick_validation": work_dir / "quick-validation.json",
        "summary_validation": work_dir / "summary-validation.json",
        "visual_review": work_dir / "visual-review.json",
        "source_review_report": work_dir / "source-review-report.json",
    }
    artifacts = {
        key: _resolve(base, declared[key]) if key in declared else value
        for key, value in defaults.items()
    }
    return {
        "source_pdf": source,
        "work_dir": work_dir,
        "final_dir": final_dir,
        **artifacts,
    }


def _json_pass(path: Path) -> tuple[bool, dict[str, Any] | None]:
    if not path.is_file():
        return False, None
    payload = _read_json(path)
    return payload.get("status") == "pass", payload


def _translations_complete(work_dir: Path) -> bool:
    units_path = work_dir / "translation-units.jsonl"
    translations_path = work_dir / "translations.jsonl"
    if not units_path.is_file() or not translations_path.is_file():
        return False

    def read_jsonl(path: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                if not isinstance(record, dict):
                    return []
                records.append(record)
        return records

    units = read_jsonl(units_path)
    translations = read_jsonl(translations_path)
    required = {
        str(unit.get("id"))
        for unit in units
        if unit.get("translatable") is True
    }
    supplied = {
        str(record.get("id"))
        for record in translations
        if str(record.get("translated_text") or "").strip()
    }
    return bool(required) and required == supplied


def _manifest_prepared(path: Path, source_sha256: str) -> bool:
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file():
        return False
    manifest = _read_json(manifest_path)
    return (
        manifest.get("source_sha256") == source_sha256
        and not bool((manifest.get("boundary") or {}).get("needs_boundary_review"))
        and (manifest.get("source_review") or {}).get("status") == "pass"
        and (manifest.get("visual_inventory") or {}).get("status") == "pass"
    )


def _strict_validation_passes(paths: dict[str, Path]) -> bool:
    strict_ok, strict = _json_pass(paths["strict_validation"])
    quick_ok, quick = _json_pass(paths["quick_validation"])
    summary_ok, _ = _json_pass(paths["summary_validation"])
    if not (strict_ok and quick_ok and summary_ok):
        return False
    strict_metrics = dict((strict or {}).get("metrics") or {})
    quick_metrics = dict((quick or {}).get("metrics") or {})
    return (
        strict_metrics.get("strict_invariants") is True
        and int(strict_metrics.get("invariant_text_misses") or 0) == 0
        and quick_metrics.get("source_sha256") == sha256_file(paths["source_pdf"])
        and all(paths[key].is_file() for key in REQUIRED_ARTIFACTS)
        and not check_review_bundle(
            _read_json(paths["visual_review"]), paths["source_pdf"],
            paths["translated_pdf"], strict, quick,
        )
    )


def _requires_source_review_report(work_dir: Path) -> bool:
    manifest_path = work_dir / "manifest.json"
    if not manifest_path.is_file():
        return False
    manifest = _read_json(manifest_path)
    return int(
        (manifest.get("source_review") or {}).get("applied_operations") or 0
    ) > 0


def _final_bundle_complete(paths: dict[str, Path]) -> bool:
    final_dir = paths["final_dir"]
    required = [final_dir / FINAL_NAMES[key] for key in REQUIRED_ARTIFACTS]
    if _requires_source_review_report(paths["work_dir"]):
        required.append(final_dir / FINAL_NAMES["source_review_report"])
    if not all(path.is_file() for path in required):
        return False
    if sha256_file(final_dir / FINAL_NAMES["source_pdf"]) != sha256_file(
        paths["source_pdf"]
    ):
        return False
    for name in (
        "translation-validation.json",
        "quick-validation.json",
        "summary-validation.json",
    ):
        ok, _ = _json_pass(final_dir / name)
        if not ok:
            return False
    strict = _read_json(final_dir / "translation-validation.json")
    return (
        (strict.get("metrics") or {}).get("strict_invariants") is True
        and not check_review_bundle(
            _read_json(final_dir / FINAL_NAMES["visual_review"]),
            final_dir / FINAL_NAMES["source_pdf"],
            final_dir / FINAL_NAMES["translated_pdf"],
            strict, _read_json(final_dir / "quick-validation.json"),
        )
    )


def refresh_paper(
    ledger: dict[str, Any],
    ledger_path: Path,
    paper: dict[str, Any],
) -> dict[str, Any]:
    paths = _paper_paths(ledger, ledger_path, paper)
    source = paths["source_pdf"]
    if not source.is_file():
        paper["stage"] = "queued"
        paper["state_error"] = f"Source PDF not found: {source}"
        return paper

    source_hash = sha256_file(source)
    recorded_hash = str(paper.get("source_sha256") or "")
    source_changed = False
    if recorded_hash and recorded_hash != source_hash:
        source_changed = True
        paper["stage"] = "queued"
        paper.pop("zotero_parent_key", None)
        paper.pop("zotero_verification", None)
        paper["state_error"] = "Source PDF hash changed; downstream state reset."
    paper["source_sha256"] = source_hash

    if paper.get("stage") == "skipped_existing":
        paper.pop("state_error", None)
        return paper

    derived = "queued"
    if _manifest_prepared(paths["work_dir"], source_hash):
        derived = "prepared"
    if derived == "prepared" and _translations_complete(paths["work_dir"]):
        derived = "translated"
    if derived == "translated" and _strict_validation_passes(paths):
        derived = "strict_validated"
    if _final_bundle_complete(paths):
        derived = "packaged"
    if (
        derived == "packaged"
        and paper.get("zotero_parent_key")
        and paper.get("zotero_verification", {}).get("ok") is True
    ):
        derived = "zotero_verified"
    paper["stage"] = derived
    if not source_changed:
        paper.pop("state_error", None)
    return paper


def refresh_ledger(
    ledger: dict[str, Any],
    ledger_path: Path,
) -> dict[str, Any]:
    source_list = ledger["source_list"]
    source_list_path = _resolve(ledger_path.parent, source_list.get("path"))
    if source_list_path.is_file():
        current_hash = sha256_file(source_list_path)
        recorded_hash = str(source_list.get("sha256") or "")
        if recorded_hash and recorded_hash != current_hash:
            raise BatchWorkflowError(
                "The source paper-list document changed; review the batch list."
            )
        source_list["sha256"] = current_hash
    elif source_list.get("path"):
        raise BatchWorkflowError(
            f"Source paper-list document not found: {source_list_path}"
        )
    for paper in ledger["papers"]:
        refresh_paper(ledger, ledger_path, paper)
    return ledger


def _identity_metadata(paper: dict[str, Any], paths: dict[str, Path]) -> dict[str, Any]:
    metadata_path = paths["metadata"]
    if metadata_path.is_file():
        return _read_json(metadata_path)
    identity = dict(paper.get("identity") or {})
    metadata: dict[str, Any] = {
        "title": paper["title"],
        "date": str(paper.get("year") or ""),
    }
    if identity.get("doi"):
        metadata["DOI"] = identity["doi"]
    if identity.get("arxiv"):
        metadata["arXivID"] = identity["arxiv"]
    if not metadata["date"] and not (metadata.get("DOI") or metadata.get("arXivID")):
        raise BatchWorkflowError(
            f"{paper['slug']}: Zotero audit requires DOI, arXiv, or title plus year."
        )
    return metadata


def audit_paper_zotero(
    ledger: dict[str, Any],
    ledger_path: Path,
    paper: dict[str, Any],
) -> dict[str, Any]:
    paths = _paper_paths(ledger, ledger_path, paper)
    metadata = _identity_metadata(paper, paths)
    matches = zotero_ingest.local_matches(metadata)
    if len(matches) > 1:
        return {
            "ok": False,
            "error": f"expected at most one exact parent, found {len(matches)}",
            "matches": zotero_ingest.public_matches(matches),
        }
    if not matches:
        return {"ok": True, "translated": False, "matches": []}
    parent_key = str(matches[0]["key"])
    verification = zotero_ingest.verify_generated_children(
        zotero_ingest.fetch_local_children(parent_key)
    )
    return {
        "ok": verification["ok"],
        "translated": verification["ok"],
        "parent_key": parent_key,
        "parent": zotero_ingest.public_matches(matches)[0],
        "verification": verification,
    }


def _find_paper(ledger: dict[str, Any], slug: str) -> dict[str, Any]:
    matches = [paper for paper in ledger["papers"] if paper["slug"] == slug]
    if len(matches) != 1:
        raise BatchWorkflowError(f"Unknown or ambiguous paper slug: {slug}")
    return matches[0]


def _terminal_stages(ledger: dict[str, Any]) -> set[str]:
    if ledger.get("zotero_enabled", True) is False:
        return {*TERMINAL_STAGES, "packaged"}
    return set(TERMINAL_STAGES)


def cmd_audit_zotero(args: argparse.Namespace) -> int:
    ledger, path = load_ledger(args.ledger)
    if ledger.get("zotero_enabled", True) is False:
        raise BatchWorkflowError("This batch explicitly disables Zotero.")
    refresh_ledger(ledger, path)
    results: dict[str, Any] = {}
    failed = False
    for paper in ledger["papers"]:
        result = audit_paper_zotero(ledger, path, paper)
        results[paper["slug"]] = result
        if not result["ok"]:
            failed = True
            continue
        if result["translated"]:
            if paper["stage"] not in {"packaged", "zotero_verified"}:
                paper["stage"] = "skipped_existing"
            paper["zotero_parent_key"] = result["parent_key"]
            paper["zotero_verification"] = result["verification"]
        elif paper["stage"] == "skipped_existing":
            paper["stage"] = "queued"
            paper.pop("zotero_parent_key", None)
            paper.pop("zotero_verification", None)
    _write_json(path, ledger)
    output = {"ok": not failed, "papers": results}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


def cmd_next(args: argparse.Namespace) -> int:
    ledger, path = load_ledger(args.ledger)
    refresh_ledger(ledger, path)
    _write_json(path, ledger)
    paper = next(
        (
            item
            for item in ledger["papers"]
            if item.get("stage") not in _terminal_stages(ledger)
        ),
        None,
    )
    output = {
        "complete": paper is None,
        "paper": paper,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def _atomic_copy(source: Path, destination: Path) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    shutil.copy2(source, temporary)
    temporary.replace(destination)


def cmd_package(args: argparse.Namespace) -> int:
    ledger, path = load_ledger(args.ledger)
    refresh_ledger(ledger, path)
    paper = _find_paper(ledger, args.slug)
    if paper["stage"] not in {"strict_validated", "packaged"}:
        raise BatchWorkflowError(
            f"{paper['slug']}: package requires strict_validated, "
            f"found {paper['stage']}."
        )
    paths = _paper_paths(ledger, path, paper)
    final_dir = paths["final_dir"]
    if not _strict_validation_passes(paths):
        raise BatchWorkflowError(
            f"{paper['slug']}: current staging artifacts require passing structural "
            "and visual reviews before copying."
        )
    source_destination = final_dir / FINAL_NAMES["source_pdf"]
    if (
        source_destination.exists()
        and sha256_file(source_destination) != sha256_file(paths["source_pdf"])
    ):
        raise BatchWorkflowError(
            f"Refusing to replace a different source PDF in {final_dir}."
        )
    final_dir.mkdir(parents=True, exist_ok=True)
    keys = list(REQUIRED_ARTIFACTS)
    if _requires_source_review_report(paths["work_dir"]):
        keys.append("source_review_report")
    for key in keys:
        source = paths[key]
        if not source.is_file():
            raise BatchWorkflowError(f"Missing package artifact {key}: {source}")
        _atomic_copy(source, final_dir / FINAL_NAMES[key])
    if not _final_bundle_complete(paths):
        raise BatchWorkflowError(
            f"{paper['slug']}: packaged bundle failed post-copy verification."
        )
    paper["final_dir"] = _relative_or_absolute(path.parent, final_dir)
    paper["stage"] = "packaged"
    _write_json(path, ledger)
    print(
        json.dumps(
            {
                "ok": True,
                "slug": paper["slug"],
                "stage": paper["stage"],
                "final_dir": str(final_dir),
                "files": [FINAL_NAMES[key] for key in keys],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_record_zotero(args: argparse.Namespace) -> int:
    ledger, path = load_ledger(args.ledger)
    if ledger.get("zotero_enabled", True) is False:
        raise BatchWorkflowError("This batch explicitly disables Zotero.")
    refresh_ledger(ledger, path)
    paper = _find_paper(ledger, args.slug)
    if paper["stage"] not in {"packaged", "zotero_verified"}:
        raise BatchWorkflowError(
            f"{paper['slug']}: Zotero recording requires packaged artifacts."
        )
    result = audit_paper_zotero(ledger, path, paper)
    if not result.get("ok") or not result.get("translated"):
        raise BatchWorkflowError(
            f"{paper['slug']}: live Zotero verification did not pass."
        )
    if result.get("parent_key") != args.parent_key:
        raise BatchWorkflowError(
            f"{paper['slug']}: verified parent {result.get('parent_key')} does "
            f"not match requested {args.parent_key}."
        )
    paper["zotero_parent_key"] = args.parent_key
    paper["zotero_verification"] = result["verification"]
    paper["stage"] = "zotero_verified"
    _write_json(path, ledger)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    ledger, path = load_ledger(args.ledger)
    if args.zotero and ledger.get("zotero_enabled", True) is False:
        raise BatchWorkflowError("Cannot audit Zotero for a local-only batch.")
    refresh_ledger(ledger, path)
    results: list[dict[str, Any]] = []
    ok = True
    for paper in ledger["papers"]:
        paths = _paper_paths(ledger, path, paper)
        item: dict[str, Any] = {
            "slug": paper["slug"],
            "stage": paper["stage"],
            "source_sha256": paper.get("source_sha256"),
            "final_bundle": (
                None
                if paper["stage"] == "skipped_existing"
                else _final_bundle_complete(paths)
            ),
        }
        if paper["stage"] not in _terminal_stages(ledger):
            item["error"] = "paper is not terminal"
            ok = False
        elif paper["stage"] == "zotero_verified" and not item["final_bundle"]:
            item["error"] = "verified paper has an incomplete final bundle"
            ok = False
        if args.zotero:
            zotero = audit_paper_zotero(ledger, path, paper)
            item["zotero"] = zotero
            if not zotero.get("ok") or not zotero.get("translated"):
                ok = False
        results.append(item)
    _write_json(path, ledger)
    report = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": ledger["batch_id"],
        "status": "pass" if ok else "fail",
        "papers": results,
    }
    report_path = (
        args.report.expanduser().resolve()
        if args.report
        else path.parent / "batch-audit.json"
    )
    _write_json(report_path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resume, package, and audit a translate-paper batch."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    audit_zotero = commands.add_parser(
        "audit-zotero",
        help="Mark papers that already have a complete translated Zotero parent.",
    )
    audit_zotero.add_argument("--ledger", required=True, type=Path)
    audit_zotero.set_defaults(func=cmd_audit_zotero)

    next_paper = commands.add_parser(
        "next",
        help="Return the first non-terminal paper and refresh derived stages.",
    )
    next_paper.add_argument("--ledger", required=True, type=Path)
    next_paper.set_defaults(func=cmd_next)

    package = commands.add_parser(
        "package",
        help="Copy one strictly validated artifact bundle to its final directory.",
    )
    package.add_argument("--ledger", required=True, type=Path)
    package.add_argument("--slug", required=True)
    package.set_defaults(func=cmd_package)

    record = commands.add_parser(
        "record-zotero",
        help="Record a parent key only after live four-child verification.",
    )
    record.add_argument("--ledger", required=True, type=Path)
    record.add_argument("--slug", required=True)
    record.add_argument("--parent-key", required=True)
    record.set_defaults(func=cmd_record_zotero)

    audit = commands.add_parser(
        "audit",
        help="Audit all terminal states, bundles, and optionally Zotero.",
    )
    audit.add_argument("--ledger", required=True, type=Path)
    audit.add_argument("--zotero", action="store_true")
    audit.add_argument("--report", type=Path)
    audit.set_defaults(func=cmd_audit)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except (BatchWorkflowError, zotero_ingest.ZoteroError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
