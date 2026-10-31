#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2"]
# ///
"""Export the accepted native-LaTeX production source as a portable ZIP project."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_relative(name: str) -> str:
    path = PurePosixPath(name)
    if (not name or "\\" in name or path.is_absolute() or ":" in name
            or any(part in {"..", "."} for part in name.split("/"))):
        raise ValueError(f"Project path must be relative and contained: {name!r}")
    return name


def validate_archive(path: Path, *, source_pdf: Path | None = None,
                     translated_pdf: Path | None = None) -> dict:
    """Check archive integrity, bounded paths, inventory, and delivery identity."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ValueError("Duplicate ZIP members")
            for name in names:
                safe_relative(name)
            manifest = json.loads(archive.read("source-manifest.json"))
            if manifest.get("schema_version") != 1:
                raise ValueError("Unsupported LaTeX source manifest")
            files = manifest["files"]
            if set(names) != set(files) | {"source-manifest.json"}:
                raise ValueError("ZIP members differ from source manifest")
            if not {"body.tex", "build.py", "README.md", "provenance/translations.jsonl"} <= set(files):
                raise ValueError("Incomplete editable LaTeX project")
            for name, digest in files.items():
                if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                    raise ValueError(f"Source archive hash mismatch: {name}")
            identity = manifest["identity"]
            if identity.get("canonical_translations_sha256") != files["provenance/translations.jsonl"]:
                raise ValueError("Canonical translation snapshot identity mismatch")
            for key, pdf in (("source_sha256", source_pdf),
                             ("accepted_pdf_sha256", translated_pdf)):
                if pdf is not None and identity.get(key) != sha256(pdf):
                    raise ValueError(f"LaTeX project identity mismatch: {key}")
            report = manifest.get("rebuild_validation", {})
            if report.get("status") != "pass" or report.get("pages") != identity.get("accepted_pages"):
                raise ValueError("Missing passing portable rebuild validation")
            return manifest
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid LaTeX source archive: {error}") from error


# This exact standalone entrypoint is shipped inside each project. It deliberately
# has no dependency on the skill directory or the original work directory.
BUILD_SCRIPT = '''#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf>=1.26.0,<2"]
# ///
"""Compile editable body.tex twice, then assemble original front/body/tail."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import pymupdf

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default="lualatex")
    parser.add_argument("--output", type=Path, default=Path("rebuilt.pdf"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    engine = shutil.which(args.engine)
    if not engine:
        raise SystemExit("Install LuaLaTeX and required fonts; see README.md")
    build = root / "build"
    build.mkdir(exist_ok=True)
    for turn in range(2):
        result = subprocess.run([engine, "-interaction=nonstopmode", "-halt-on-error",
            "-file-line-error", "-no-shell-escape", "-output-directory=build", "body.tex"],
            cwd=root, capture_output=True, timeout=300)
        (build / f"pass-{turn+1}.txt").write_bytes(result.stdout + result.stderr)
        if result.returncode:
            raise SystemExit(f"LuaLaTeX failed; inspect build/pass-{turn+1}.txt\\n" +
                             result.stdout.decode("utf-8", errors="replace")[-10000:])
    if "Missing character:" in (build / "body.log").read_text(encoding="utf-8", errors="replace"):
        raise SystemExit("Missing glyphs; inspect build/body.log and required fonts")
    result = pymupdf.open()
    for part in (root / "original/front.pdf", build / "body.pdf", root / "original/tail.pdf"):
        if part.is_file():
            with pymupdf.open(part) as doc:
                result.insert_pdf(doc)
    output = args.output if args.output.is_absolute() else root / args.output
    result.ez_save(output)
    print(json.dumps({"output": str(output), "pages": len(result)}))
    result.close()

if __name__ == "__main__":
    main()
'''

README = """# 可编辑 LaTeX 源码包

body.tex 是已接受译本的实际原生正文源码，可以直接修改；图片、保留的
代码/视觉片段位于 assets/。original/front.pdf 与 tail.pdf（存在时）仅含
已接受译本中保留的原文前后页面。完整译本由 build.py 拼装，单编 body.tex
只生成中文正文。包内不含整本书、缓存或系统字体。

依赖：Python >=3.11、PyMuPDF >=1.26,<2、LuaLaTeX（例如 TeX Live），以及
body.tex 所声明的字体，通常包括 SimSun、SimHei、Segoe UI Symbol、Latin
Modern Roman/Sans/Mono 和 Latin Modern Math。请在有合法许可的系统安装
这些字体；包内不分发系统字体。更换字体/TeX版本可能改变断行与页数。
TeX 包依赖以 body.tex 的 usepackage 为准（ctex、unicode-math、tikz 等）。

解压至任意目录后运行：

    uv run --script build.py

或先安装 PyMuPDF，再运行 `python build.py`。可用 `--engine <lualatex路径>`
指定引擎，`--output <路径>` 指定完整PDF；默认输出 rebuilt.pdf。编译日志在
build/。首次导出的独立搬移构建已经逐页核对 accepted PDF 的文本与96dpi
像素，验证结果及所有输入哈希见 source-manifest.json；PDF二进制不要求相同。

provenance/translations.jsonl 是导出时的 canonical translations 快照。
修改 body.tex 仅修改此独立项目，不自动回写翻译检查点或宣称原验收仍有效。
重新进入正式翻译流程时，canonical translations.jsonl 仍是翻译权威。
"""


def collect_dependencies(tex_path: Path, project: Path) -> None:
    """Copy only referenced native assets, rejecting machine-local paths."""
    root = tex_path.parent.resolve()
    pending = [(tex_path, project / "body.tex")]
    seen: set[Path] = set()
    pattern = re.compile(r"\\(includegraphics|input|include|VerbatimInput|lstinputlisting)\*?(?:\[[^\]]*\])?\{(?:\\detokenize\{([^{}]+)\}|([^{}]+))\}")
    while pending:
        source, target = pending.pop()
        if source in seen:
            continue
        seen.add(source)
        text = source.read_text(encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        for command, detokenized, literal in pattern.findall(text):
            name = detokenized or literal
            # Validate resource arguments, not mathematical text such as f:\mathbb.
            safe_relative(name)
            candidate = (source.parent / name).resolve()
            if not candidate.is_file():
                suffixes = (".tex",) if command in {"input", "include"} else (".pdf", ".png", ".jpg", ".jpeg")
                candidate = next((candidate.with_suffix(s) for s in suffixes if candidate.with_suffix(s).is_file()), candidate)
            if not candidate.is_relative_to(root) or not candidate.is_file():
                raise ValueError(f"Missing/external project dependency: {name}")
            destination = project / candidate.relative_to(root)
            if command in {"input", "include"}:
                pending.append((candidate, destination))
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(candidate, destination)


def compare_pdfs(expected: Path, actual: Path) -> dict:
    import pymupdf
    pages = []
    with pymupdf.open(expected) as left, pymupdf.open(actual) as right:
        if len(left) != len(right):
            raise ValueError(f"Portable rebuild page count differs: {len(left)} != {len(right)}")
        for index, (a, b) in enumerate(zip(left, right), 1):
            text = a.get_text("text", sort=True)
            if text != b.get_text("text", sort=True):
                raise ValueError(f"Portable rebuild text differs on page {index}")
            pa, pb = a.get_pixmap(dpi=96, alpha=False), b.get_pixmap(dpi=96, alpha=False)
            if (pa.width, pa.height, pa.samples) != (pb.width, pb.height, pb.samples):
                raise ValueError(f"Portable rebuild pixels differ on page {index}")
            pages.append({"page": index, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                          "pixels_sha256_96dpi": hashlib.sha256(pa.samples).hexdigest()})
    return {"status": "pass", "pages": len(pages), "comparison": "exact text and 96dpi RGB pixels", "per_page": pages}


def export_project(manifest_path: Path, translated_pdf: Path, layout_path: Path,
                   translations: Path, output: Path, engine: str = "lualatex") -> dict:
    import pymupdf
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    source_pdf = Path(manifest["source_pdf"])
    if not source_pdf.is_absolute():
        source_pdf = manifest_path.parent / source_pdf
    if sha256(source_pdf) != manifest["source_sha256"]:
        raise ValueError("Source no longer matches preparation manifest")
    if layout.get("render_backend") != "native-lualatex-v1":
        raise ValueError("Export requires the actual native-LaTeX production source")
    native = layout["latex"]
    tex_path, body_pdf = Path(native["tex_source"]), Path(native["body_pdf"])
    tex_path = tex_path if tex_path.is_absolute() else layout_path.parent / tex_path
    body_pdf = body_pdf if body_pdf.is_absolute() else layout_path.parent / body_pdf
    boundary = layout["boundary_layout"]
    start, end = int(boundary["translation_page_start"]), int(boundary["translation_page_end"])
    with tempfile.TemporaryDirectory(prefix="translate-paper-latex-") as directory:
        project = Path(directory) / "relocated-project"
        project.mkdir()
        collect_dependencies(tex_path, project)
        (project / "build.py").write_text(BUILD_SCRIPT, encoding="utf-8", newline="\n")
        (project / "README.md").write_text(README, encoding="utf-8", newline="\n")
        (project / "provenance").mkdir()
        shutil.copyfile(translations, project / "provenance/translations.jsonl")
        with pymupdf.open(translated_pdf) as accepted, pymupdf.open(body_pdf) as body:
            if not 1 <= start <= end <= len(accepted) or len(body) != end - start + 1:
                raise ValueError("Native body/boundary page identity mismatch")
            for name, first, last in (("front", 0, start - 2), ("tail", end, len(accepted) - 1)):
                if last >= first:
                    (project / "original").mkdir(exist_ok=True)
                    with pymupdf.open() as part:
                        part.insert_pdf(accepted, from_page=first, to_page=last)
                        part.ez_save(project / "original" / f"{name}.pdf")
            accepted_pages = len(accepted)
        result = subprocess.run([sys.executable, str(project / "build.py"), "--engine", engine],
                                cwd=project, capture_output=True, timeout=660)
        if result.returncode:
            raise ValueError("Independent portable build failed: " + (result.stdout + result.stderr).decode("utf-8", errors="replace"))
        validation = compare_pdfs(translated_pdf, project / "rebuilt.pdf")
        payload = {
            "schema_version": 1,
            "identity": {"source_sha256": sha256(source_pdf), "accepted_pdf_sha256": sha256(translated_pdf),
                         "accepted_pages": accepted_pages, "canonical_translations_sha256": sha256(translations)},
            "production_inputs": {"native_tex_sha256": sha256(tex_path), "native_body_pdf_sha256": sha256(body_pdf),
                                  "layout_sha256": sha256(layout_path), "preparation_manifest_sha256": sha256(manifest_path)},
            "authority": "canonical translations.jsonl; standalone TeX edits do not write back",
            "rebuild_validation": validation,
            "files": {p.relative_to(project).as_posix(): sha256(p) for p in project.rglob("*")
                      if p.is_file() and "build" not in p.relative_to(project).parts and p.name != "rebuilt.pdf"},
        }
        (project / "source-manifest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.name + ".tmp")
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(set(payload["files"]) | {"source-manifest.json"}):
                archive.write(project / name, name)
        validate_archive(temporary, source_pdf=source_pdf, translated_pdf=translated_pdf)
        temporary.replace(output)
    return {"output": str(output), "sha256": sha256(output), "rebuild_validation": validation}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--translated", type=Path, required=True)
    parser.add_argument("--layout", type=Path, required=True)
    parser.add_argument("--translations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--engine", default="lualatex")
    args = parser.parse_args()
    print(json.dumps(export_project(args.manifest.resolve(), args.translated.resolve(), args.layout.resolve(),
                                   args.translations.resolve(), args.output.resolve(), args.engine), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
