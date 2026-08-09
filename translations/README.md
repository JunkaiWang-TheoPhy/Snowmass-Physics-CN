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
