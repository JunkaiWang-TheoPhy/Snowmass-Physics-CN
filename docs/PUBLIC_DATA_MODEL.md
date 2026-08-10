# Snowmass 公开数据模型

公开网站读取两个生成文件：

- `site/data/papers.json`：逐篇公开记录数组；
- `site/data/stats.json`：从逐篇记录聚合的统计对象。

两者由 `scripts/build_public_manifest.py` 生成，不接受绕过源清单的手工授权结论。

## 逐篇字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `paper_id` | string | 面向用户的论文身份，例如 `arXiv:2203.07864` |
| `record_id` | string | 规范化唯一键，例如 `arxiv:2203.07864` |
| `title` | string | 官方目录题名 |
| `title_zh` | string | 项目生成的中文译题；不替代官方英文题名 |
| `title_zh_status` | enum | 中文译题状态；首批值为 `machine-draft` |
| `title_zh_model` | string | 生成中文译题的模型标识 |
| `authors_as_listed` | string | 官方目录作者列，不用于推断通讯作者 |
| `frontiers` | string[] | Snowmass Frontier 代码；跨 Frontier 记录保留多个值 |
| `topics` | string[] | 官方 Frontier 页面的专题归类 |
| `source_url` | URL | arXiv、CDS 或 HAL 原始记录页 |
| `source_version` | string/null | 已核验的来源版本；外部记录可能为空 |
| `source_license` | string | 规范化许可证标识 |
| `source_license_url` | URL | 许可证全文或官方说明 |
| `permits_adaptation` | boolean/null | `null` 表示当前证据不足，不表示允许 |
| `license_decision` | string | 权利研究层的处置结论 |
| `translation_status` | enum | 翻译状态机当前值 |
| `translation_license` | string/null | 译文许可证；未确定时为空 |
| `machine_model` | string/null | 生成机器草稿的供应商/模型标识 |
| `human_reviewers` | string[] | 同意公开署名的人工审校者 |
| `authorization_status` | enum | 公开授权状态 |
| `publication_allowed` | boolean | 构建脚本根据来源许可或书面授权派生的发布闸门 |
| `publication_basis` | enum | `source-license`、`permission-granted` 或 `manual-hold` |
| `publication_conditions` | string[] | 必须落实的署名、SA、NC 或另行授权条件 |
| `publication_translation_url` | URL/null | 已公开译文 URL；未发布时为空 |
| `public_updated_at` | datetime/null | 当前公开权利状态的证据更新时间 |
| `publication_year` | integer/null | INSPIRE/HAL/CDS 元数据年份 |
| `citation_count` | number/null | INSPIRE 引用快照 |
| `citation_count_without_self_citations` | number/null | INSPIRE 去自引引用快照 |
| `citations_per_year` | number/null | 以检索日期计算的引用速度代理 |
| `impact_proxy_score_0_100` | number/null | 引用百分位与年化引用百分位的内部排序代理 |
| `page_count` | number/null | PDF 物理页数 |
| `unicode_token_count` | number/null | PDF 文本的 Unicode 字母数字 token 代理 |
| `frontier_labels` | string[] | Frontier 英文全名 |
| `primary_arxiv_category` | string/null | 主要 arXiv 分类 |

## 枚举值

### `translation_status`

- `not-started`
- `machine-draft`
- `human-review`
- `published`
- `superseded`
- `withdrawn`

### `authorization_status`

- `not-reviewed`
- `license-cleared`
- `needs-permission`
- `contacted`
- `response-pending`
- `permission-granted`
- `permission-denied`
- `unclear`
- `withdrawn`

### `publication_basis`

- `source-license`：来源许可证明确允许改编；
- `permission-granted`：私有证据系统存在范围明确、已复核的书面授权；
- `manual-hold`：证据不足、许可证禁止改编或授权范围未核验。

## 派生规则

当前构建器仅在以下来源许可中自动设置 `publication_allowed=true`：

- `CC0-1.0`
- `CC-BY-4.0`
- `CC-BY-SA-4.0`
- `CC-BY-NC-SA-4.0`

其他许可证一律保持 `false`，直到未来的私有授权同步器提供经过人工复核的 `permission-granted` 证据。公共 JSON 本身不是授权证据，直接修改该字段必须被审查和 CI 拒绝。

## 聚合统计

`stats.json` 包含：

- `catalog_count`
- `source_snapshot_date`
- `license_counts`
- `authorization_counts`
- `translation_counts`
- `publication_counts`
- `frontier_counts`
- `year_counts`
- `page_summary`
- `citation_summary`
- `unicode_token_summary`

Frontier 计数包含跨列论文，因此各项之和大于 541。引用、影响力和 token 是检索或抽取快照，不是官方评价指标。

## 公开/私有边界

公共记录不得加入邮箱、电话、通信内容、授权附件、私有对象 URL、数据库主键映射、内部评分或凭据。私有表结构见 `private/schema.sql`，实际数据不得进入 Git。
