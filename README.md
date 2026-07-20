# translate-paper

`translate-paper` 是一个 Codex skill，用于将学术论文 PDF 翻译为中文，同时保留图表、公式、引文、附录和参考文献，并生成经过验证的中文 PDF、Markdown 与论文总结。默认工作流还支持将最终产物安全导入 Zotero。

## 功能

- 处理文本型和扫描型论文 PDF。
- 仅使用当前 Codex 模型进行翻译；脚本只负责提取、OCR、保护标记、排版和验证。
- 保留原论文的非正文材料，并翻译从 Introduction 到 Conclusion 的正文。
- 输出原始 PDF、中文翻译 PDF、中文正文 Markdown、中文总结和 Zotero 元数据。
- 验证翻译完整性、版面结构、字体、公式、图表和 Zotero 附件结构。

完整行为与安全约束见 [`SKILL.md`](SKILL.md)。

## 安装

需要安装 [Git](https://git-scm.com/)、[uv](https://docs.astral.sh/uv/) 和支持 skills 的 Codex。各 Python 脚本使用 PEP 723 声明自己的依赖，`uv run --script` 会按需解析依赖。

在 Windows PowerShell 中克隆仓库，并把 Codex skill 目录连接到仓库：

```powershell
git clone git@github.com:fedoraiver/translate-paper.git C:\path\to\translate-paper
New-Item -ItemType Junction `
  -Path "$HOME\.codex\skills\translate-paper" `
  -Target "C:\path\to\translate-paper"
```

创建 Junction 前，`$HOME\.codex\skills\translate-paper` 必须不存在。Zotero 导入依赖 Zotero Desktop、对应的 Codex Zotero 插件，以及 Windows Credential Manager 中配置的 Zotero Web API 凭据。

## 使用

在 Codex 中调用该 skill，例如：

```text
使用 $translate-paper 将这篇论文翻译成中文。
```

skill 会按照 `SKILL.md` 中定义的准备、翻译、导出、验证、总结和 Zotero 导入流程执行。最终文件保存在：

```text
output/pdf/<paper-slug>/
```

## 脚本

脚本可以通过 PEP 723 入口直接运行，例如：

```powershell
uv run --script scripts/prepare_paper.py --help
uv run --script scripts/render_translation.py --help
uv run --script scripts/check_translation.py --help
```

这些脚本不会自行做出翻译决策。

## 测试

```powershell
uv run --script scripts/test_paper_pipeline.py -v
uv run --script scripts/test_zotero_ingest.py -v
```

## License

[MIT](LICENSE)
