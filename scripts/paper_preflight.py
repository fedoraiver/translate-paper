# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2"]
# ///
"""Check reviewed source readiness and render isolated production diagnostics.

Importing readiness helpers requires only the standard library. PDF and renderer
dependencies are loaded only by the compilation stage; canonical translations
and production output directories are never written by this command.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
REPORT_NAME = "paper-preflight.json"
INPUT_NAMES = (
    "translation_units", "translations_template", "boundary_review",
    "source_review", "source_review_report", "math_review", "visual_layout", "glossary",
)
MODULE_NAMES = (
    "paper_preflight.py", "prepare_paper.py", "source_review.py", "display_plan.py",
    "source_structure.py", "native_latex.py", "render_translation.py", "typography.py",
)
REQUIRED_GATES = (
    "reviewed_source", "math_resolution", "math_compile", "diagnostic_render",
)


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _resolve(work: Path, value: str | Path) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else work / path).resolve()


def _records(path: Path) -> list[dict[str, Any]]:
    records = []
    seen = set()
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise ValueError(f"{path}:{number}: expected an object with a string id")
        if not value["id"] or value["id"] in seen:
            raise ValueError(f"{path}:{number}: empty or duplicate id")
        seen.add(value["id"])
        records.append(value)
    return records


def source_fingerprint(manifest_path: Path) -> dict[str, Any]:
    """Hash source, extraction/protection, review decisions and implementation.

    Content hashes deliberately exclude canonical translations and file times.
    A newly completed translation cannot invalidate reviewed source readiness.
    """
    manifest_path = Path(manifest_path).resolve()
    manifest = _json(manifest_path)
    work = manifest_path.parent
    source = _resolve(work, manifest["source_pdf"])
    source_hash = _sha256(source)
    if source_hash != manifest.get("source_sha256"):
        raise ValueError("Source PDF hash differs from manifest; prepare/review again")
    inputs = {"manifest": _sha256(manifest_path), "source_pdf": source_hash}
    for name in INPUT_NAMES:
        value = (manifest.get("paths") or {}).get(name)
        if not value:
            raise ValueError(f"Preflight requires manifest.paths.{name}; prepare/review again")
        inputs[name] = _sha256(_resolve(work, value))
    modules = {name: _sha256(Path(__file__).parent / name) for name in MODULE_NAMES}
    contract = {"schema_version": SCHEMA_VERSION, "inputs": inputs, "modules": modules}
    digest = hashlib.sha256(json.dumps(
        contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {"sha256": digest, **contract}


def preflight_report_path(manifest_path: Path) -> Path:
    manifest_path = Path(manifest_path).resolve()
    manifest = _json(manifest_path)
    return _resolve(manifest_path.parent,
                    (manifest.get("paths") or {}).get("paper_preflight") or REPORT_NAME)


def assert_source_ready(manifest_path: Path, report_path: Path | None = None) -> dict[str, Any]:
    """Return the current passing report or reject missing/stale/failed readiness."""
    manifest_path = Path(manifest_path).resolve()
    path = Path(report_path).resolve() if report_path else preflight_report_path(manifest_path)
    try:
        report = _json(path)
    except (OSError, ValueError) as error:
        raise ValueError(f"Source preflight report is missing or unreadable: {path}; "
                         "run paper_preflight.py before dispatch or production") from error
    if report.get("schema_version") != SCHEMA_VERSION or report.get("status") != "pass":
        raise ValueError(f"Source preflight did not pass: {path}; resolve its failures")
    try:
        current = source_fingerprint(manifest_path)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"Source preflight inputs are invalid: {error}") from error
    if report.get("source_fingerprint") != current:
        raise ValueError("Source preflight is stale; source, reviews, protection, or "
                         "render implementation changed. Run paper_preflight.py again")
    gates = report.get("gates") or {}
    if not isinstance(gates, dict) or any(
            not isinstance(gates.get(name), dict) or gates[name].get("status") != "pass"
            for name in REQUIRED_GATES):
        raise ValueError("Source preflight report is incomplete; rerun paper_preflight.py")
    for name in ("math_compile", "diagnostic_render"):
        gate = gates[name]
        if gate.get("missing_glyphs") != 0:
            raise ValueError(f"Source preflight {name} lacks a passing missing-glyph check")
        for artifact in gate.get("artifacts") or []:
            try:
                artifact_path = _resolve(path.parent, artifact["path"])
                valid = _sha256(artifact_path) == artifact["sha256"]
            except (OSError, KeyError, TypeError):
                valid = False
            if not valid:
                raise ValueError(f"Source preflight {name} artifact is missing or changed; rerun")
        if not gate.get("artifacts"):
            raise ValueError(f"Source preflight {name} artifacts are missing; rerun")
    return report


def _reviewed_source(manifest_path: Path, manifest: dict[str, Any],
                     elements: list[dict[str, Any]]) -> dict[str, Any]:
    import source_review

    work = manifest_path.parent
    paths = manifest["paths"]
    reviews = {name: _json(_resolve(work, paths[name])) for name in
               ("boundary_review", "source_review", "source_review_report", "visual_layout")}
    errors = []
    source_hash = manifest["source_sha256"]
    for name, review in reviews.items():
        if review.get("source_sha256") != source_hash:
            errors.append(f"{name} does not match source hash")
    boundary = manifest.get("boundary") or {}
    if (boundary.get("needs_boundary_review") is not False
            or boundary.get("manual_review") is not True
            or reviews["boundary_review"].get("reviewed") is not True):
        errors.append("Translation boundary requires an applied manual review")
    if (manifest.get("partial_run")
            or (manifest.get("source_review") or {}).get("status") != "pass"
            or (manifest.get("source_review") or {}).get("reviewed") is not True
            or reviews["source_review"].get("reviewed") is not True
            or reviews["source_review_report"].get("status") != "pass"
            or reviews["source_review_report"].get("reviewed") is not True
            or reviews["source_review_report"].get("errors")):
        errors.append("Source extraction requires a complete passing reviewed preparation")
    for label, recorded, payload in (
            ("Boundary", manifest.get("boundary_review_sha256"), reviews["boundary_review"]),
            ("Source", reviews["source_review_report"].get("source_review_sha256"), reviews["source_review"])):
        if recorded is not None or manifest.get("assignment_protocol_version"):
            if recorded != _cache_key(payload):
                errors.append(f"{label} review decisions changed after preparation; replay preparation")
    if ((manifest.get("visual_inventory") or {}).get("status") != "pass"
            or reviews["visual_layout"].get("status") != "pass"):
        errors.append("Source visual inventory must pass")
    glossary = _json(_resolve(work, paths["glossary"]))
    terms = glossary.get("terms") if "terms" in glossary else glossary
    if not isinstance(terms, dict) or any(
            not isinstance(key, str) or not key.strip()
            or not isinstance(value, str) or not value.strip() for key, value in terms.items()):
        errors.append("Glossary must contain a flat map of source terms to Chinese terms")
    elif not terms and not (glossary.get("reviewed") is True
                            and str(glossary.get("reason") or "").strip()):
        errors.append("Empty glossary requires reviewed=true and a reason")
    issue_errors, warnings = source_review.detect_source_issues(elements, reviews["visual_layout"])
    errors.extend(issue_errors)
    issues = source_review.structured_source_issues(elements, reviews["visual_layout"])
    return {"status": "fail" if errors else "pass", "errors": errors,
            "warnings": warnings, "source_issues": issues}


def _math_inventory(elements: list[dict[str, Any]], reviews: dict[str, Any],
                    display_groups: list[dict[str, Any]]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    from native_latex import resolve_display_math, resolve_inline_math

    inline = []
    for element in elements:
        if element.get("render_suppressed") or not element.get("translatable"):
            continue
        for token, fragment in (element.get("inline_fragments") or {}).items():
            if fragment.get("kind") != "math":
                continue
            review = reviews.get(token, {})
            if ("source_text" in review
                    and review["source_text"] != str(fragment.get("text") or "")):
                raise ValueError(f"{token}: reviewed mathematics belongs to different source text")
            if ("source_page" in review
                    and review["source_page"] != element.get("page")):
                raise ValueError(f"{token}: reviewed mathematics belongs to a different source page")
            tex, _ = resolve_inline_math({**fragment, "token": token}, reviews)
            inline.append((token, tex))
    displays = [(group["review_id"], resolve_display_math({"id": group["review_id"]}, reviews))
                for group in display_groups]
    return inline, displays


def _artifacts(paths: list[Path], report_path: Path) -> list[dict[str, str]]:
    result = []
    for path in paths:
        try:
            value = str(path.resolve().relative_to(report_path.parent))
        except ValueError:
            value = str(path.resolve())
        result.append({"path": value, "sha256": _sha256(path)})
    return result


def _cache_key(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _cached_compile(previous: dict[str, Any], key: str, report_path: Path) -> dict[str, Any] | None:
    if (not isinstance(previous, dict)
            or previous.get("status") != "pass" or previous.get("cache_key") != key
            or previous.get("missing_glyphs") != 0 or not previous.get("artifacts")):
        return None
    try:
        if all(_sha256(_resolve(report_path.parent, item["path"])) == item["sha256"]
               for item in previous["artifacts"]):
            return copy.deepcopy(previous)
    except (OSError, KeyError, TypeError):
        pass
    return None


def select_diagnostic_nodes(nodes: list[dict[str, Any]],
                            reviews: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Pick bounded source examples with adjacent context for each risky shape."""
    categories: dict[str, list[str]] = {}
    positions = set()
    predicates = {
        "body": lambda n: n.get("element", {}).get("kind") == "body",
        "list": lambda n: n.get("flow_role") == "list-item",
        "nested_list": lambda n: n.get("flow_role") in {"list-item", "list-continuation"}
                                  and int(n.get("list_level") or 0) > 0,
        "list_continuation": lambda n: n.get("flow_role") == "list-continuation"
                               or bool(n.get("joined_continuations")),
        "continuation": lambda n: bool(n.get("joined_continuations") or n.get("continuation_of")
                                       or n.get("flow_role") == "paragraph-continuation"),
        "display": lambda n: (n.get("element", {}).get("kind") == "equation"
                               or n.get("render_mode") == "native-math"),
        "visual": lambda n: n.get("render_mode") == "source_clip"
                    and n.get("element", {}).get("kind") != "equation",
        "inline_scripts": lambda n: any(
            fragment.get("kind") == "math" and any(
                sign in str((reviews or {}).get(token, fragment).get("tex") or "") for sign in ("_", "^"))
            for token, fragment in (n.get("inline_fragments") or
                                    n.get("element", {}).get("inline_fragments") or {}).items()),
        "multiline_display": lambda n: n.get("element", {}).get("kind") == "equation" and
            any(sign in str((reviews or {}).get(n["id"], {}).get("tex") or "")
                for sign in (r"\\", r"\begin{aligned}", r"\begin{cases}")),
    }
    for category, predicate in predicates.items():
        for index, node in enumerate(nodes):
            if predicate(node):
                categories[category] = list(node.get("ids") or [node["id"]])
                positions.update(range(max(0, index - 1), min(len(nodes), index + 2)))
                break
    for index, node in enumerate(nodes[1:], 1):
        if (nodes[index - 1].get("flow_role") in {"list-item", "list-continuation"}
                and node.get("flow_role") in {"paragraph-start", "paragraph-continuation"}):
            categories["post_list_paragraph"] = list(node.get("ids") or [node["id"]])
            positions.update(range(index - 1, min(len(nodes), index + 2)))
            break
    if not positions and nodes:
        positions.add(0)
    return [copy.deepcopy(nodes[index]) for index in sorted(positions)], categories


def _compile_diagnostics(manifest_path: Path, manifest: dict[str, Any],
                         elements: list[dict[str, Any]], report_path: Path,
                         fingerprint: dict[str, Any], gates: dict[str, Any],
                         previous_gates: dict[str, Any] | None = None) -> None:
    import pymupdf
    import native_latex
    import render_translation
    from display_plan import build_display_plan
    from typography import ZH_ACADEMIC_V1

    work = manifest_path.parent
    previous_gates = previous_gates or {}
    reviews = native_latex.load_math_review(
        _resolve(work, manifest["paths"]["math_review"]), manifest["source_sha256"])
    body, _, _ = render_translation.partition_translation_body(elements, manifest)
    groups = build_display_plan(body, reviews)
    inline, displays = _math_inventory(body, reviews, groups)
    gates["math_resolution"] = {"status": "pass", "inline_count": len(inline),
                                "display_count": len(displays),
                                "display_groups": [{"review_id": g["review_id"],
                                                    "source_ids": g["source_ids"]} for g in groups]}
    build_dir = report_path.parent / (report_path.stem + "-artifacts") / fingerprint["sha256"][:16]
    build_dir.mkdir(parents=True, exist_ok=True)
    math_dir = build_dir / "math"
    math_dir.mkdir(exist_ok=True)
    chunks = [native_latex._document_preamble(ZH_ACADEMIC_V1), r"\noindent 数学排版检查。\par"]
    for label, tex in inline:
        chunks.append(f"% {label}\n" + rf"\noindent\({tex}\)\par")
    for label, tex in displays:
        chunks.append(f"% {label}\n" + rf"\TPDisplayMath{{{tex}}}")
    chunks.append(r"\end{document}")
    math_tex = math_dir / "all-math.tex"
    math_content = "\n".join(chunks)
    math_key = _cache_key({"tex": math_content, "modules": {name: fingerprint["modules"][name]
                           for name in ("native_latex.py", "typography.py", "paper_preflight.py")}})
    cached = _cached_compile(previous_gates.get("math_compile") or {}, math_key, report_path)
    if cached:
        gates["math_compile"] = cached
    else:
        math_tex.write_text(math_content, encoding="utf-8", newline="\n")
        math_pdf, math_metrics = native_latex.compile_body_tex(math_dir, math_tex)
        gates["math_compile"] = {"status": "pass", **math_metrics, "cache_key": math_key,
            "artifacts": _artifacts([math_tex, math_pdf, math_tex.with_suffix(".log")], report_path)}

    rendered = {element["id"]: render_translation.restore_tokens(
        element, str(element.get("protected_text") or element.get("source_text") or ""))
        if element.get("translatable") else str(element.get("source_text") or "")
        for element in body}
    nodes = render_translation.build_flow_nodes(copy.deepcopy(body), rendered, display_plan=groups)
    visual_layouts = native_latex.load_visual_layout(
        _resolve(work, manifest["paths"]["visual_layout"]), manifest["source_sha256"])
    for node in nodes:
        visual_id = str(node.get("element", {}).get("visual_id") or "")
        if visual_id in visual_layouts:
            node["visual_layout"] = dict(visual_layouts[visual_id])
    nodes = native_latex.prepare_native_clip_nodes(nodes, body, ZH_ACADEMIC_V1)
    sample, categories = select_diagnostic_nodes(nodes, reviews)
    if not sample:
        raise ValueError("Source diagnostic has no body nodes")
    diagnostic_dir = build_dir / "source-sample"
    diagnostic_dir.mkdir(exist_ok=True)
    with pymupdf.open(_resolve(work, manifest["source_pdf"])) as source:
        tex, placements, _ = native_latex.build_body_tex(
            sample, reviews, ZH_ACADEMIC_V1, source, diagnostic_dir)
    sample_tex = diagnostic_dir / "source-sample.tex"
    stable_tex = tex.replace(str(build_dir), "<BUILD>").replace(build_dir.as_posix(), "<BUILD>")
    sample_key = _cache_key({"tex": stable_tex, "source_sha256": manifest["source_sha256"],
        "sample": sample, "visual_layouts": visual_layouts,
        "modules": {name: fingerprint["modules"][name] for name in
                    ("native_latex.py", "typography.py", "render_translation.py", "paper_preflight.py")}})
    cached = _cached_compile(previous_gates.get("diagnostic_render") or {}, sample_key, report_path)
    if cached:
        gates["diagnostic_render"] = cached
    else:
        sample_tex.write_text(tex, encoding="utf-8", newline="\n")
        sample_pdf, sample_metrics = native_latex.compile_body_tex(diagnostic_dir, sample_tex)
        gates["diagnostic_render"] = {"status": "pass", **sample_metrics, "cache_key": sample_key,
            "sample_source_ids": [unit_id for n in sample for unit_id in (n.get("ids") or [n["id"]])],
            "categories": categories, "placements": placements,
            "artifacts": _artifacts([sample_tex, sample_pdf, sample_tex.with_suffix(".log")], report_path)}


def run_preflight(manifest_path: Path, report_path: Path | None = None) -> dict[str, Any]:
    manifest_path = Path(manifest_path).resolve()
    path = Path(report_path).resolve() if report_path else preflight_report_path(manifest_path)
    manifest = _json(manifest_path)
    protected_paths = {manifest_path, _resolve(manifest_path.parent, manifest["source_pdf"]),
                       manifest_path.parent / "translations.jsonl"}
    protected_paths.update(_resolve(manifest_path.parent, value)
                           for name, value in (manifest.get("paths") or {}).items()
                           if name != "paper_preflight" and isinstance(value, str))
    if path in protected_paths:
        raise ValueError("Preflight report must be separate from canonical inputs and translations")
    report: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "status": "fail",
        "source_sha256": manifest.get("source_sha256"), "gates": {}, "errors": []}
    try:
        try:
            previous_gates = _json(path).get("gates") if path.is_file() else {}
        except (OSError, ValueError):
            previous_gates = {}
        if not isinstance(previous_gates, dict):
            previous_gates = {}
        fingerprint = source_fingerprint(manifest_path)
        report["source_fingerprint"] = fingerprint
        elements = _records(_resolve(manifest_path.parent, manifest["paths"]["translation_units"]))
        report["gates"]["reviewed_source"] = _reviewed_source(manifest_path, manifest, elements)
        source_gate = report["gates"]["reviewed_source"]
        if source_gate["status"] != "pass":
            raise ValueError("; ".join(source_gate["errors"]))
        _compile_diagnostics(manifest_path, manifest, elements, path, fingerprint, report["gates"],
                             previous_gates=previous_gates)
        if source_fingerprint(manifest_path) != fingerprint:
            raise ValueError("Source preflight inputs changed while checks were running; rerun")
        report["status"] = "pass"
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, ImportError) as error:
        report["errors"].append(str(error))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = run_preflight(args.manifest, args.report)
        if report["status"] == "pass":
            from paper_state import read_state, state_path, update_state

            manifest_path = args.manifest.resolve()
            manifest = _json(manifest_path)
            if manifest.get("assignment_protocol_version") or state_path(manifest_path, manifest).exists():
                reports = dict(read_state(manifest_path).get("accepted_reports") or {})
                path = args.report.resolve() if args.report else preflight_report_path(manifest_path)
                reports["preflight"] = {"path": str(path), "sha256": _sha256(path),
                                        "source_fingerprint": report["source_fingerprint"]["sha256"]}
                update_state(manifest_path, stage="source_ready", accepted_reports=reports,
                             next_action="Create a translation task packet for the first unfinished batch")
    except (OSError, ValueError, KeyError, TypeError) as error:
        report = {"status": "fail", "errors": [str(error)]}
    print(json.dumps({"status": report["status"], "errors": report.get("errors", []),
                      "source_fingerprint": (report.get("source_fingerprint") or {}).get("sha256")},
                     ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
