# 参与 Snowmass 中文翻译计划

感谢贡献。这个项目欢迎代码、数据、权利证据、翻译和人工审校，但所有公开内容都必须先通过逐篇发布闸门。

## 可以提交什么

- 论文题名、作者列、Frontier、专题和原文链接修正；
- 来源许可证、版本或公开权利证据修正；
- 对具备发布资格论文的机器初译、术语统一和人工审校；
- 术语表、公式排版、引用链接和中文表达改进；
- 网站、数据构建器、测试、统计和可访问性改进。

## 提交译文前

1. 在 `site/data/papers.json` 找到论文并确认 `record_id`。
2. 检查 `publication_allowed`。如果是 `false`，不要把完整译文提交到分支、PR、issue、CI artifact 或预览部署。
3. 打开 `source_license_url`，确认许可证对应所翻译的具体版本。
4. 阅读 [`docs/RIGHTS_PROTOCOL.md`](docs/RIGHTS_PROTOCOL.md)，选择兼容的译文许可证。
5. 如果发布依据是书面授权，只在 metadata 中引用私有授权记录 ID，不得提交邮件、邮箱或附件。

## 翻译目录

公开译文采用以下布局：

```text
translations/<record-slug>/
├── metadata.json
├── source-notes.md
├── machine-draft.md
├── terminology-reviewed.md
├── human-reviewed.md
└── final.md
```

`record-slug` 使用安全文件名，例如 `arxiv-2203.07864`。各阶段必须分开保存；不得用最终稿覆盖机器初译并丢失审计链。

最低 `metadata.json`：

```json
{
  "paper_id": "arXiv:2203.07864",
  "record_id": "arxiv:2203.07864",
  "source_url": "https://arxiv.org/abs/2203.07864",
  "source_version": "v1",
  "source_license": "CC-BY-4.0",
  "source_license_url": "https://creativecommons.org/licenses/by/4.0/",
  "publication_basis": "source-license",
  "translation_status": "human-review",
  "translation_license": "CC-BY-SA-4.0",
  "machine_model": "provider/model-name",
  "machine_generated_at": "2026-08-09",
  "human_reviewers": [],
  "change_notice": "Unofficial Simplified Chinese translation; terminology and prose edited by project contributors."
}
```

## 默认英译中流程

1. 忠实翻译原文，不省略段落、脚注、公式标号和引用。
2. 按锁定术语表统一术语。
3. 去除机器翻译的公式化句式和生硬连接。
4. 在不改变事实的前提下自然化、学术化中文。
5. 人工逐项核对数字、单位、公式、引用、链接、人名和术语。

第 3、4 步不得新增事实、扩大结论、弱化限定词，或改变数字、单位、公式、引用、链接、人名和锁定术语。

## Pull Request 检查清单

提交者必须在 PR 中确认：

- [ ] PR 指向唯一 `paper_id`、`record_id` 和固定来源版本；
- [ ] 来源许可证或私有授权记录可核验；
- [ ] `publication_allowed=true` 有 `source-license` 或 `permission-granted` 依据；
- [ ] 译文许可证与 BY、SA、NC 条件兼容；
- [ ] 保留原作者、原题名、来源 URL、许可证 URL 和修改说明；
- [ ] 机器模型、日期、阶段和人工审校者记录完整；
- [ ] 数字、单位、公式、引用、链接、人名和术语已经核对；
- [ ] 没有作者邮箱、私人通信、附件、内部备注、凭据或原始 PDF；
- [ ] 已运行 `python3 scripts/build_public_manifest.py`；
- [ ] 已运行 `python3 -m unittest scripts.test_public_manifest -v`。

## 权利证据修正

公开许可证修正必须给出官方仓储页面、许可证页面、论文 PDF 中的许可声明或出版方页面。不要仅引用搜索结果摘要、二手博客或模型生成解释。

涉及私人授权通信的修正只提交脱敏结论和私有证据 ID。需要向维护者提供敏感证据时，使用 GitHub Private Vulnerability Reporting 或维护者明确指定的私有渠道，不要放进公开 issue。

## 禁止内容

- 未满足发布闸门的完整译文或译文片段集合；
- 从官方站点下载并镜像的原始 PDF；
- 作者、权利人或审校者的非公开联系方式；
- 邮件正文、邮件头、签名档、授权附件和内部备注；
- API 密钥、访问令牌、数据库连接串或本地 `.env`；
- 伪造、推断或夸大授权范围的 metadata；
- 暗示作者、Snowmass 或出版方为本项目背书的文字。

## 代码和文档

- 尽量使用标准库和现有工具，避免为静态目录引入运行时依赖；
- 新增公开字段时同时更新构建器、测试和 `docs/PUBLIC_DATA_MODEL.md`；
- 修改权利规则时同时更新 `docs/RIGHTS_PROTOCOL.md` 和相关测试；
- 提交信息遵守仓库的 Lore commit 协议，记录约束、否决方案和验证证据。
