# 译文目录

这里只有满足公开发布闸门的译文。`publication_allowed=false` 的机器草稿必须留在私有或本地受控存储，不能通过 Git history、PR、issue、artifact 或预览部署进入公开仓库。

目录规范：

```text
translations/<record-slug>/
├── metadata.json
├── source-notes.md
├── machine-draft.md
├── terminology-reviewed.md
├── human-reviewed.md
└── final.md
```

- `source-notes.md`：固定原文版本、章节映射、缺页或抽取异常，不复制整篇原文。
- `machine-draft.md`：忠实机器初译。
- `terminology-reviewed.md`：锁定术语统一后的版本。
- `human-reviewed.md`：人工核对意义、数字、单位、公式、引用和表达后的版本。
- `final.md`：通过发布闸门和 PR 审核的公开译文。
- `metadata.json`：来源、许可证、授权依据、模型、阶段、审校者和变更声明。

完整要求见 [`../CONTRIBUTING.md`](../CONTRIBUTING.md) 和 [`../docs/RIGHTS_PROTOCOL.md`](../docs/RIGHTS_PROTOCOL.md)。

## 发布级硬约束

- `snowmass-global-glossary.json` 是可版本化的中英术语真源。`first_use=true` 的术语在每篇摘要或正文首次出现时必须写成“中文（English，缩写）”；后续才可只用锁定中文或缩写。
- `snowmass-hard-constraints.json` 保存逐篇精确对照。运行页眉等由 TeX 一次定义、却在 PDF 每页重复出现的对象只确定一次译法，所有重复实例必须复用，不能逐页调用模型。
- 图像、图表和矢量图内部的文字整体保持原文，包括坐标轴、图例、标注、实验/任务名称、符号和单位；只翻译图外的图注。BabelDOC IR 中 `xobj_id != 0` 的段落必须标记为 `verbatim_figure_text`，在进入模型前跳过，并在回填时再次核验原文逐字一致，禁止按关键词局部翻译。
- 参考文献条目保持原文和原排版，不进入模型翻译或 BabelDOC 二次排版；仅运行页眉和 `References` 标题按精确对照替换。
- 发布前自检必须确认：所有精确对照已应用、首次术语对照存在、所有图内单元与原文一致、页眉文字/字号/水平位置一致、参考文献正文与原 PDF 一致、编号从 `[1]` 到 `[N]` 连续。任一检查失败时不得装订发布版。
