# Snowmass 2021 逐篇权利调研记录

本表覆盖目录中的 **541** 条记录（目录当前为 541 条）。

## 调研口径

- arXiv 论文：读取对应 arXiv abstract 页的当前许可证链接；许可证是 arXiv 记录中的一手证据。
- `ARXIV-NONEXCLUSIVE-DISTRIB-1.0` 只说明作者授予 arXiv 分发权，不作为公开翻译或改编许可。
- 外部 CDS/HAL 记录先读取官方 PDF 或仓储元数据；没有明确开放改编许可的记录仍保留为人工核验，不自动放行。
- `translation_status` 诚实记录为 `not-started`；尚未生成译文的记录不标为 `machine-draft`。
- 这不是法律意见；发布完整译文前仍应检查论文正文中的版权声明、期刊政策和作者/权利人许可。

## 文件

- `snowmass2021_rights_manifest.csv`：适合筛选、统计和后续人工更新。
- `snowmass2021_rights_manifest.json`：保留数组字段和完整证据字段。
- `rights_summary.json`：许可证和处置决定计数。
- `cache/`：本地生成的 arXiv abstract 页解析结果，便于审计和重跑；其中可能包含上游页面快照，因此由 Git 忽略，不随公开仓库发布。

## 处置规则

- `eligible-*`：许可证页面允许改编，但发布时仍需保留署名、许可证链接、版本和 ShareAlike/非商业条件。
- `hold-*`：不公开完整译文；先保留原文链接、目录、摘要或翻译状态，待人工核验或取得许可。
