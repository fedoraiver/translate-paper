from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from export_latex_project import collect_dependencies, safe_relative, validate_archive


def make_archive(output: Path, source: Path, translated: Path, *, extra: dict | None = None) -> None:
    files = {"body.tex": b"editable source", "build.py": b"build entrypoint",
             "README.md": b"dependencies", "provenance/translations.jsonl": b"canonical snapshot"}
    files.update(extra or {})
    payload = {"schema_version": 1, "identity": {
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "accepted_pdf_sha256": hashlib.sha256(translated.read_bytes()).hexdigest(), "accepted_pages": 1,
        "canonical_translations_sha256": hashlib.sha256(files["provenance/translations.jsonl"]).hexdigest()},
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        "rebuild_validation": {"status": "pass", "pages": 1}}
    with zipfile.ZipFile(output, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr("source-manifest.json", json.dumps(payload))


class LaTeXProjectTests(unittest.TestCase):
    def test_mapping_math_with_relative_assets_is_not_a_drive_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native, project = root / "native", root / "relocated/project"
            (native / "assets").mkdir(parents=True)
            (native / "assets/figure.pdf").write_bytes(b"figure")
            # These are actual mathematical forms from chapter 2 production TeX.
            text = (r"\TPDisplayMath{f:\mathbb{R}\mapsto\mathbb{Z},}" + "\n"
                    + r"\(sqr:\mathbb{R}\mapsto\mathbb{R}\)" + "\n"
                    + r"\input{section}" + "\n"
                    + r"\includegraphics{\detokenize{assets/figure.pdf}}")
            (native / "body.tex").write_text(text, encoding="utf-8")
            (native / "section.tex").write_text(r"\(f:\mathbf{A}\mapsto\mathbf{B}\)", encoding="utf-8")
            collect_dependencies(native / "body.tex", project)
            self.assertEqual((project / "body.tex").read_text(encoding="utf-8"), text)
            self.assertEqual((project / "section.tex").read_bytes(), (native / "section.tex").read_bytes())
            self.assertEqual((project / "assets/figure.pdf").read_bytes(), b"figure")

    def test_unsafe_resource_arguments_remain_rejected(self):
        commands = ("includegraphics", "input", "include", "VerbatimInput", "lstinputlisting")
        names = ("/etc/secret", "/Users/local/secret", "C:/secret", "C:\\secret",
                 "C:secret", "//server/share/secret", "\\\\server\\share\\secret",
                 "../secret", "assets/../secret", "./secret")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for command in commands:
                for name in names:
                    for detokenize in (False, True):
                        argument = "\\detokenize{" + name + "}" if detokenize else name
                        (root / "body.tex").write_text("\\" + command + "{" + argument + "}", encoding="utf-8")
                        with self.subTest(command=command, name=name, detokenize=detokenize), self.assertRaises(ValueError):
                            collect_dependencies(root / "body.tex", root / "project")

    def test_resolved_external_resource_remains_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native = root / "native"
            native.mkdir()
            original_resolve = Path.resolve
            for command, suffix in (("input", ".tex"), ("includegraphics", ".pdf")):
                outside = root / ("outside" + suffix)
                outside.write_text("external", encoding="utf-8")
                link = native / ("link" + suffix)
                (native / "body.tex").write_text("\\" + command + "{" + link.name + "}", encoding="utf-8")

                def resolve(path, *args, **kwargs):
                    # Model a symlink's resolved target without Windows link privileges.
                    return outside if path == link else original_resolve(path, *args, **kwargs)

                with self.subTest(command=command), patch.object(Path, "resolve", resolve):
                    with self.assertRaisesRegex(ValueError, "Missing/external"):
                        collect_dependencies(native / "body.tex", root / "project")

    def test_copies_referenced_assets_only_and_retains_editable_tex(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            native, project = root / "native", root / "elsewhere/project"
            (native / "assets").mkdir(parents=True)
            (native / "body.tex").write_text(r"Chinese math $x^2$ \includegraphics{\detokenize{assets/figure.pdf}}", encoding="utf-8")
            (native / "assets/figure.pdf").write_bytes(b"figure")
            (native / "assets/unreferenced.pdf").write_bytes(b"do not package")
            collect_dependencies(native / "body.tex", project)
            self.assertEqual((project / "body.tex").read_bytes(), (native / "body.tex").read_bytes())
            self.assertEqual((project / "assets/figure.pdf").read_bytes(), b"figure")
            self.assertFalse((project / "assets/unreferenced.pdf").exists())

    def test_missing_or_external_dependency_blocks_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("missing.pdf", "../outside.pdf", "C:/secret.pdf"):
                (root / "body.tex").write_text("\\includegraphics{" + name + "}", encoding="utf-8")
                with self.subTest(name=name), self.assertRaises(ValueError):
                    collect_dependencies(root / "body.tex", root / "project")

    def test_archive_identity_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, translated, archive = root / "s.pdf", root / "t.pdf", root / "p.zip"
            source.write_bytes(b"source")
            translated.write_bytes(b"accepted")
            make_archive(archive, source, translated)
            validate_archive(archive, source_pdf=source, translated_pdf=translated)
            translated.write_bytes(b"different accepted PDF")
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                validate_archive(archive, source_pdf=source, translated_pdf=translated)
            with zipfile.ZipFile(archive) as original:
                files = {n: original.read(n) for n in original.namelist()}
            files["body.tex"] = b"untracked change"
            with zipfile.ZipFile(archive, "w") as changed:
                for name, data in files.items():
                    changed.writestr(name, data)
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                validate_archive(archive)

    def test_unsafe_paths_fail_even_with_valid_hashes(self):
        for name in ("/etc/file", "../secret", "assets/../secret", "C:/file", "assets\\file"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                safe_relative(name)


if __name__ == "__main__":
    unittest.main()
