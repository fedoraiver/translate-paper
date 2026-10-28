"""Source readiness is content-bound, reusable and isolated from translations."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import paper_preflight


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def write_ready_report(manifest: Path) -> Path:
    """Stand in for successful compiler evidence in tests of read-only gates."""
    artifact = manifest.parent / "preflight-evidence.pdf"
    artifact.write_bytes(b"isolated compiler evidence fixture")
    evidence = [{"path": artifact.name,
                 "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}]
    report = {"schema_version": 1, "status": "pass",
              "source_fingerprint": paper_preflight.source_fingerprint(manifest),
              "gates": {name: {"status": "pass"} for name in paper_preflight.REQUIRED_GATES}}
    for name in ("math_compile", "diagnostic_render"):
        report["gates"][name].update(missing_glyphs=0, artifacts=evidence)
    path = paper_preflight.preflight_report_path(manifest)
    write_json(path, report)
    return path


def add_readiness_inputs(manifest: Path) -> None:
    """Complete minimal fixture manifests without invoking a renderer."""
    value = json.loads(manifest.read_text(encoding="utf-8"))
    work = manifest.parent
    source_hash = value["source_sha256"]
    value.setdefault("source_pdf", "../source.pdf")
    paths = value.setdefault("paths", {})
    paths.setdefault("translation_units", "translation-units.jsonl")
    paths.setdefault("translations_template", paths["translation_units"])
    defaults = {
        "boundary_review": {"reviewed": True},
        "source_review": {"reviewed": True, "operations": []},
        "source_review_report": {"status": "pass", "reviewed": True},
        "math_review": {"entries": {}},
        "visual_layout": {"status": "pass", "visuals": []},
        "glossary": {"terms": {}, "reviewed": True, "reason": "No recurring domain terms."},
    }
    for name, payload in defaults.items():
        if name not in paths:
            paths[name] = name.replace("_", "-") + ".json"
            write_json(work / paths[name], {"schema_version": 1, "source_sha256": source_hash,
                                           **payload})
    write_json(manifest, value)
    write_ready_report(manifest)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.source = self.work / "source.pdf"
        self.source.write_bytes(b"source fixture")
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.units = [{"id": "body", "page": 1, "order": 0, "page_order": 0,
                       "bbox": [40, 50, 550, 80], "kind": "body", "render_mode": "text",
                       "translatable": True, "source_text": "A sentence.",
                       "protected_text": "A sentence.", "protected_tokens": {},
                       "inline_fragments": {}, "style_tokens": {}},
                      {"id": "tail", "page": 1, "order": 1, "page_order": 1,
                       "bbox": [40, 100, 550, 120], "kind": "heading", "render_mode": "text",
                       "translatable": False, "source_text": "References"}]
        self.write_units()
        self.manifest = self.work / "manifest.json"
        write_json(self.manifest, {
            "source_pdf": "source.pdf", "source_sha256": self.source_hash,
            "boundary": {"needs_boundary_review": False, "manual_review": True,
                         "introduction_id": "body", "post_body_stop_id": "tail"},
            "source_review": {"status": "pass", "reviewed": True},
            "visual_inventory": {"status": "pass"}, "page_count": 1,
            "pages": [{"page": 1, "width": 595, "height": 842, "columns": 1}],
            "paths": {"translation_units": "units.jsonl", "translations_template": "template.jsonl"},
        })
        add_readiness_inputs(self.manifest)

    def write_units(self):
        (self.work / "units.jsonl").write_text("".join(json.dumps(u) + "\n" for u in self.units),
                                                encoding="utf-8")
        (self.work / "template.jsonl").write_text(json.dumps({**self.units[0], "translated_text": ""}) + "\n",
                                                   encoding="utf-8")

    def test_readiness_ignores_translation_progress_and_timestamps(self):
        before = paper_preflight.assert_source_ready(self.manifest)
        (self.work / "translations.jsonl").write_text('{"translated_text":"译文"}', encoding="utf-8")
        for path in self.work.iterdir():
            os.utime(path, (1000000000, 1000000000))
        self.assertEqual(before, paper_preflight.assert_source_ready(self.manifest))

    def test_units_reviews_source_and_generator_changes_invalidate_readiness(self):
        for name in ("units.jsonl", "math-review.json", "source-review.json",
                     "boundary-review.json", "visual-layout.json", "manifest.json"):
            with self.subTest(name=name):
                path = self.work / name
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                with self.assertRaisesRegex(ValueError, "stale"):
                    paper_preflight.assert_source_ready(self.manifest)
                path.write_bytes(original)
        original_hash = paper_preflight._sha256
        with patch.object(paper_preflight, "_sha256", side_effect=lambda path:
                          "changed-renderer" if path.name == "render_translation.py" else original_hash(path)):
            with self.assertRaisesRegex(ValueError, "stale"):
                paper_preflight.assert_source_ready(self.manifest)
        self.source.write_bytes(b"changed source")
        with self.assertRaisesRegex(ValueError, "hash differs"):
            paper_preflight.assert_source_ready(self.manifest)

    def test_missing_failed_incomplete_or_changed_evidence_is_refused(self):
        report_path = paper_preflight.preflight_report_path(self.manifest)
        report_path.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            paper_preflight.assert_source_ready(self.manifest)
        for mutation in (lambda r: r.update(status="fail"),
                         lambda r: r["gates"].pop("math_resolution"),
                         lambda r: r["gates"]["math_compile"].update(missing_glyphs=1)):
            write_ready_report(self.manifest)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            mutation(report)
            write_json(report_path, report)
            with self.assertRaises(ValueError):
                paper_preflight.assert_source_ready(self.manifest)
        write_ready_report(self.manifest)
        (self.work / "preflight-evidence.pdf").write_bytes(b"different output")
        with self.assertRaisesRegex(ValueError, "artifact"):
            paper_preflight.assert_source_ready(self.manifest)

    def test_preflight_does_not_touch_canonical_translations(self):
        translations = self.work / "translations.jsonl"
        translations.write_bytes(b"existing canonical translated text")
        before = {path: path.read_bytes() for path in self.work.iterdir()
                  if path.name != "paper-preflight.json"}
        def compile_stub(manifest_path, manifest, elements, report_path, fingerprint, gates, **kwargs):
            gates.update({name: {"status": "pass"} for name in
                          ("math_resolution", "math_compile", "diagnostic_render")})
        with patch.object(paper_preflight, "_compile_diagnostics", side_effect=compile_stub):
            report = paper_preflight.run_preflight(self.manifest)
        self.assertEqual(report["status"], "pass", report)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_failed_review_stops_before_compilation(self):
        path = self.work / "boundary-review.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["reviewed"] = False
        write_json(path, payload)
        with patch.object(paper_preflight, "_compile_diagnostics") as compile_diagnostics:
            report = paper_preflight.run_preflight(self.manifest)
        self.assertEqual(report["status"], "fail")
        self.assertIn("manual review", report["errors"][0])
        compile_diagnostics.assert_not_called()

    def test_glossary_requires_terms_or_explicit_empty_review(self):
        for payload, expected in (({}, "fail"), ({"terms": {}}, "fail"),
                                  ({"source": "源文"}, "pass"),
                                  ({"terms": {}, "reviewed": True, "reason": "No domain terms."}, "pass")):
            with self.subTest(payload=payload):
                write_json(self.work / "glossary.json", payload)
                with patch.object(paper_preflight, "_compile_diagnostics"):
                    report = paper_preflight.run_preflight(self.manifest)
                self.assertEqual(report["status"], expected, report)

    def test_source_risks_are_reported_with_unit_identity(self):
        self.units[0]["source_text"] = "x = 1 (27)"
        self.units[0]["protected_text"] = "x = 1 (27)"
        self.write_units()
        report = paper_preflight.run_preflight(self.manifest)
        self.assertEqual(report["status"], "fail")
        issues = report["gates"]["reviewed_source"]["source_issues"]
        self.assertTrue(any(i["code"] == "numbered_display_in_body" and i["unit_id"] == "body"
                            for i in issues), issues)

    def test_all_math_compiles_before_sampling_and_missing_glyphs_fail(self):
        self.units[1]["order"] = 3
        self.units[1]["bbox"] = [40, 220, 550, 240]
        for index in (1, 2):
            self.units.insert(index, {"id": f"eq{index}", "page": 1, "order": index,
                "page_order": index, "kind": "equation", "translatable": False,
                "source_text": f"x={index}", "render_mode": "source_clip",
                "bbox": [40, 100 + index * 20, 550, 115 + index * 20]})
        self.write_units()
        write_json(self.work / "math-review.json", {"schema_version": 1,
            "source_sha256": self.source_hash, "entries": {
                f"eq{i}": {"kind": "display", "source_ids": [f"eq{i}"],
                           "tex": f"x={i}", "review_status": "manually_reviewed"}
                for i in (1, 2)}})
        def reject_glyphs(build_dir, tex_path):
            tex = tex_path.read_text(encoding="utf-8")
            self.assertEqual(tex_path.name, "all-math.tex")
            self.assertIn(r"\setmathfont{Latin Modern Math}", tex)
            self.assertIn(r"\TPDisplayMath{x=1}", tex)
            self.assertIn(r"\TPDisplayMath{x=2}", tex)
            raise RuntimeError("LuaLaTeX reported missing glyphs: fixture glyph")
        with patch("native_latex.compile_body_tex", side_effect=reject_glyphs) as compile_tex:
            report = paper_preflight.run_preflight(self.manifest)
        self.assertEqual(report["status"], "fail", report)
        self.assertIn("math_resolution", report["gates"], report)
        self.assertEqual(report["gates"]["math_resolution"]["display_count"], 2)
        self.assertIn("missing glyphs", report["errors"][0])
        compile_tex.assert_called_once()

    def test_diagnostic_sample_covers_risky_shapes_with_neighbors(self):
        nodes = [{"id": f"u{i}", "ids": [f"u{i}"], "element": {"kind": "body"}}
                 for i in range(20)]
        nodes[3]["flow_role"] = "list-item"
        nodes[6]["joined_continuations"] = 1
        nodes[10]["element"]["kind"] = "equation"
        nodes[15].update(render_mode="source_clip", element={"kind": "figure"})
        selected, categories = paper_preflight.select_diagnostic_nodes(nodes)
        self.assertEqual(set(categories), {"body", "list", "list_continuation", "continuation", "display", "visual"})
        selected_ids = {n["id"] for n in selected}
        self.assertTrue({"u2", "u3", "u4", "u5", "u6", "u7", "u9", "u10", "u11",
                         "u14", "u15", "u16"} <= selected_ids)
        self.assertLessEqual(len(selected), 15)

    def test_diagnostic_includes_nested_script_multiline_and_post_list_shapes(self):
        nodes = [
            {"id": "list", "flow_role": "list-item", "list_level": 1, "element": {"kind": "body"}},
            {"id": "continuation", "flow_role": "list-continuation", "element": {"kind": "body"}},
            {"id": "after", "flow_role": "paragraph-start", "element": {"kind": "body"},
             "inline_fragments": {"[[Fx]]": {"kind": "math", "tex": "x_i"}}},
            {"id": "equation", "element": {"kind": "equation"}},
        ]
        selected, categories = paper_preflight.select_diagnostic_nodes(
            nodes, {"equation": {"tex": r"\begin{aligned}x&=y\\&=z\end{aligned}"}})
        self.assertTrue({"nested_list", "list_continuation", "inline_scripts",
                         "multiline_display", "post_list_paragraph"} <= set(categories))
        self.assertEqual([node["id"] for node in selected], [node["id"] for node in nodes])

    def test_cli_updates_existing_recovery_pointer_preserving_other_reports(self):
        state_path = self.work / "run-state.json"
        write_json(state_path, {"schema_version": 1, "source_sha256": self.source_hash,
                   "stage": "prepare", "accepted_reports": {"other": "kept"}, "assignments": {}})
        report = json.loads((self.work / "paper-preflight.json").read_text(encoding="utf-8"))
        with patch.object(paper_preflight, "run_preflight", return_value=report), \
                patch("sys.argv", ["paper_preflight.py", "--manifest", str(self.manifest)]), \
                patch("builtins.print"):
            self.assertEqual(paper_preflight.main(), 0)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["stage"], "source_ready")
        self.assertEqual(state["accepted_reports"]["other"], "kept")
        self.assertEqual(state["accepted_reports"]["preflight"]["source_fingerprint"],
                         report["source_fingerprint"]["sha256"])

    def test_canonical_inputs_cannot_be_used_as_report_output(self):
        for output in (self.manifest, self.source, self.work / "units.jsonl",
                       self.work / "translations.jsonl"):
            with self.subTest(output=output), self.assertRaisesRegex(ValueError, "separate"):
                paper_preflight.run_preflight(self.manifest, output)

    def test_read_only_helpers_import_without_pdf_dependencies(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); "
                "import paper_preflight, translation_context, translation_checkpoint; "
                "assert 'pymupdf' not in sys.modules; assert 'native_latex' not in sys.modules")
        result = subprocess.run([sys.executable, "-S", "-c", code, str(Path(__file__).parent)],
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("lualatex"), "LuaLaTeX unavailable")
    def test_production_compilation_isolated_and_readiness_reusable(self):
        import pymupdf

        source = pymupdf.open()
        source.new_page(width=595, height=842).insert_text((40, 60), "A sentence.")
        source.save(self.source)
        source.close()
        self.units[0].update(source_text="Let x be one.", protected_text="Let [[Fx]] be one.",
                             inline_fragments={"[[Fx]]": {"kind": "math", "text": "x", "tex": "x",
                                                         "review_status": "auto_validated"}})
        self.units[1].update(order=2, page_order=2, bbox=[40, 180, 550, 200])
        self.units.insert(1, {"id": "eq1", "page": 1, "order": 1, "page_order": 1,
                             "bbox": [40, 100, 550, 120], "kind": "equation", "translatable": False,
                             "render_mode": "source_clip", "source_text": "x=1"})
        self.write_units()
        write_json(self.work / "math-review.json", {"schema_version": 1,
            "source_sha256": self.source_hash,
            "entries": {"[[Fx]]": {"kind": "inline", "tex": "x", "review_status": "auto_validated"},
                        "eq1": {"kind": "display", "source_ids": ["eq1"], "tex": "x=1",
                                "review_status": "manually_reviewed"}}})
        new_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["source_sha256"] = new_hash
        write_json(self.manifest, manifest)
        for name in ("boundary-review.json", "source-review.json", "source-review-report.json",
                     "math-review.json", "visual-layout.json"):
            path = self.work / name
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["source_sha256"] = new_hash
            write_json(path, payload)
        translations = self.work / "translations.jsonl"
        translations.write_bytes(b"unchanged completed translations")
        report = paper_preflight.run_preflight(self.manifest)
        self.assertEqual(report["status"], "pass", report)
        self.assertEqual(report["gates"]["math_resolution"]["inline_count"], 1)
        self.assertEqual(report["gates"]["math_resolution"]["display_count"], 1)
        self.assertEqual(paper_preflight.assert_source_ready(self.manifest), report)
        self.assertEqual(translations.read_bytes(), b"unchanged completed translations")
        self.assertFalse((self.work / "native-latex").exists())
        write_json(self.work / "glossary.json", {"source": "源文"})
        with self.assertRaisesRegex(ValueError, "stale"):
            paper_preflight.assert_source_ready(self.manifest)
        with patch("native_latex.compile_body_tex", side_effect=AssertionError("unnecessary recompile")):
            refreshed = paper_preflight.run_preflight(self.manifest)
        self.assertEqual(refreshed["status"], "pass", refreshed)
        self.assertNotEqual(refreshed["source_fingerprint"], report["source_fingerprint"])
        self.assertEqual(refreshed["gates"]["math_compile"], report["gates"]["math_compile"])
        self.assertEqual(refreshed["gates"]["diagnostic_render"], report["gates"]["diagnostic_render"])


if __name__ == "__main__":
    unittest.main()
