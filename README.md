# translate-paper

`translate-paper` 是一个 Codex skill，用于将单篇或一组学术论文 PDF
翻译为中文，同时保留图表、公式、引文、附录和参考文献，并生成经过验证
的中文 PDF、Markdown 与论文总结。默认工作流还支持审计 Zotero 中缺少
翻译的论文、断点续跑和安全导入最终产物。

仅在用户明确要求使用此技能时调用。教材章节采用专门的边界审阅流程；
项目约定的本地输出、文件目录和课程链接规则优先于默认归档方式。

## 功能

- 处理文本型和扫描型论文 PDF。
- 从论文列表或指定章节建立可恢复的批处理清单，只处理 Zotero 中缺少
  完整中文附件的论文。
- 长任务会持续落盘检查点，但会在同一轮自动处理后续批次、验证和交付，
  不把检查点当作等待用户输入“继续”的暂停点。
- 翻译、排版和独立视觉检查由不同子 agent 分工，统一使用最新已核实的旗舰模型（当前 GPT-6 Astra），按角色设置推理强度；主 agent 负责合并与交付。
- 中文 PDF 生成后逐页实际看图，修复问题并复查；视觉报告绑定最终 PDF 哈希，未通过不得交付。
- 保留原论文的非正文材料，并翻译从 Introduction 到 Conclusion 的正文。
- 通过哈希绑定的 `source-review.json` 重放人工核对过的视觉清单和提取修复。
- 使用统一检查点工具校验并原子合并译文批次，报告下一未译单元，避免覆盖已有译文。
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
使用 $translate-paper 翻译这个列表中 Zotero 里还没有中文附件的论文，并支持继续未完成批次。
```

skill 会按照 `SKILL.md` 中定义的准备、翻译、导出、验证、总结和 Zotero
导入流程执行。默认最终文件保存在：

```text
output/pdf/<paper-slug>/
```

项目也可以在 `batch-run.json` 中为每篇论文指定
`translations/papers/<group>/<paper-slug>/` 等归档目录。

## 脚本

脚本可以通过 PEP 723 入口直接运行，例如：

```powershell
uv run --script scripts/prepare_paper.py --help
uv run --script scripts/render_translation.py --help
uv run --script scripts/check_translation.py --help
python scripts/batch_workflow.py --help
python scripts/translation_checkpoint.py --help
python scripts/check_visual_review.py --help
```

这些脚本不会自行做出翻译决策。

## 测试

```powershell
uv run --script scripts/test_paper_pipeline.py -v
uv run --script scripts/test_zotero_ingest.py -v
python scripts/test_batch_workflow.py -v
python scripts/test_translation_checkpoint.py -v
python scripts/test_visual_review.py -v
```

## License

[MIT](LICENSE)
