# Snowmass 中文翻译与授权工作流设计

日期：2026-08-09
状态：设计已获用户确认，等待书面规格复核
范围：Snowmass 2021 contributed/white papers 的公开翻译目录、授权状态展示和人工确认式授权邮件工作流

## 1. 背景与目标

项目的基础目录已经包含 541 条去重论文记录，以及逐篇来源许可证和翻译状态清单。官方提交索引的原始计数为 548（部分论文跨 Frontier 重复），本设计以当前去重后的 541 条记录作为工作基线。当前清单区分了明确允许改编的许可证、明确禁止公开改编的许可证、arXiv 非独占分发许可和 HAL Authorization。arXiv 官方把翻译视为演绎作品，并要求发布译文前取得当前版权持有人的许可；HAL Authorization 也只授权 HAL 在线分发，不自动授权第三方复制或改编。因此，系统必须把“许可证事实”“授权请求”“翻译进度”和“公开发布资格”分开记录。

权利判断以官方材料为依据：[arXiv 许可证说明](https://info.arxiv.org/help/license/index.html)、[arXiv 翻译政策](https://info.arxiv.org/help/translations.html)和 [HAL Authorization v1](https://about.hal.science/en/hal-authorisation-v1/)。这些链接用于解释工作流边界，不替代具体论文的许可证文本、作者授权或适用法判断。

目标：

1. 让公众按论文查看原文、来源许可证、翻译阶段和公开授权阶段。
2. 让管理员在私有后台管理联系人、授权请求、回复、证据和后续跟进。
3. 让授权 Agent 批量检索和生成个性化邮件，但必须经过人工确认后才发送。
4. 保留可审计的状态变更和授权证据，不把沉默、退信或模糊回复误判为许可。
5. 让 GitHub 成为公开内容和代码的事实源，Netlify 只部署经过构建检查的静态阅读站点。

非目标：

- 不自动代表项目作出法律结论。
- 不自动抓取或公开私人邮箱。
- 不把作者未明确授权的论文全文、原始 PDF 或译文推送到公开站点。
- 第一阶段不实现全自动邮箱阅读、自动判断法律回复或自动追问作者。

## 2. 方案选择

### 方案 A：纯 GitHub 文件驱动

论文、授权状态和邮件模板全部以 YAML/JSON 存放，网站从 GitHub 构建，授权 Agent 在本地运行。

优点是成本最低、审计简单、适合单人维护。缺点是联系人邮箱和邮件记录很难安全保存，也不适合多人协作或队列化发送。

### 方案 B：公开静态站点 + 私有授权数据库（采用）

公开论文目录和脱敏状态存放在 GitHub，Netlify 构建站点；联系人、授权请求、邮件队列和证据索引放入私有数据库。初始实现使用 Supabase/Postgres 兼容接口，具体邮件服务通过抽象适配器接入。

优点是公开数据和私人数据边界清晰，成本可控，后续可以加入管理员后台、队列和审计。缺点是需要维护一个私有后端和身份认证。

### 方案 C：完整自动化授权平台

加入邮箱收件箱接入、自动回复分类、自动跟进和复杂权限编排。

该方案适合成熟团队，但当前会扩大隐私、误判、邮箱合规和维护范围。暂不采用；自动收件和自动分类保留为后续扩展。

## 3. 总体架构

```text
官方论文目录 / rights manifest
              │
              ├── 公共数据构建器 ──> GitHub ──> Netlify 静态站点
              │                     │
              │                     └── public manifest（脱敏）
              │
              └── 私有同步器 ──> Supabase/Postgres
                                    │
                                    ├── 联系人和来源
                                    ├── 授权请求队列
                                    ├── 邮件草稿与发送记录
                                    ├── 回复分类和证据索引
                                    └── 管理员审计日志

授权 Agent：检索 → 生成草稿 → 预检 → 待审核 → 人工批准 → 批量发送 → 记录结果
```

### 公共层

公共层只发布可公开信息：

- 论文 ID、题名、作者列、Frontier 和专题组。
- 原文链接、来源版本和来源许可证。
- `translation_status`、`authorization_status` 和 `publication_allowed`。
- 授权状态最后更新时间。
- 公开的许可依据类型，例如 `source-license`、`permission-granted`、`permission-denied`。
- 译文版本、审校者公开署名和变更记录。

公共层不发布：

- 通讯作者邮箱、个人电话和私人地址。
- 邮件正文、私人回复、内部备注和未脱敏附件。
- 只有内部审查意义的联系人来源评分。

### 私有层

私有层保存最小必要的联系和授权数据。管理员通过 Supabase Auth 或等价的强身份认证进入；数据库和邮件服务凭据不进入 GitHub、Netlify 构建产物或前端代码。

## 4. 数据模型

### 4.1 `papers`

论文表以现有 `snowmass2021_rights_manifest.json` 为种子，字段包括：

```yaml
paper_id: arXiv:2203.08210
record_id: arxiv:2203.08210
title: ...
authors_as_listed: ...
source_url: https://arxiv.org/abs/2203.08210
source_version: v1
source_version_url: https://arxiv.org/abs/2203.08210v1
source_license: CC-BY-4.0
source_license_url: https://creativecommons.org/licenses/by/4.0/
permits_adaptation: true | false | null
translation_status: not-started | machine-draft | human-review | published
authorization_status: not-reviewed | license-cleared | needs-permission | contacted | response-pending | permission-granted | permission-denied | withdrawn
publication_allowed: true | false | null
publication_basis: source-license | permission-granted | public-domain | manual-hold | denied
public_updated_at: 2026-08-09T00:00:00Z
```

`publication_allowed` 是项目工作流的发布闸门，不是对所有司法辖区的法律意见。只有明确开放改编许可或经过人工核验的书面授权，才可以设为 `true`。

### 4.2 `contacts`（私有）

```yaml
contact_id:
paper_id:
name:
role: corresponding-author | collaboration-contact | rights-holder | publisher
institution:
email:
source_url:
source_type: paper | author-page | collaboration-page | publisher-page
verified_at:
verification_status: unverified | verified | stale | bounced
notes:
```

同一联系人可以对应多篇论文，但不应因为姓名相同就自动合并；合并需要人工确认。

### 4.3 `authorization_requests`（私有）

```yaml
request_id:
paper_id:
contact_id:
campaign_id:
request_type: translation-and-publication
requested_scope:
  - translate-to-simplified-chinese
  - publish-on-github
  - publish-on-static-website
  - accept-community-edits
  - apply-project-content-license
requested_license: CC-BY-SA-4.0
status: draft | pending-review | approved-to-send | sent | delivered | bounced | replied | closed
first_sent_at:
last_event_at:
follow_up_due_at:
permission_scope:
permission_decision: unknown | granted | granted-with-conditions | denied | unclear
permission_evidence_id:
```

授权请求必须写明请求范围，不能使用“请授权我们使用这篇文章”这种范围不清的表述。至少要说明：翻译语言、公开渠道、是否接受社区 PR、拟使用的译文许可证、署名方式和撤回/更正方式。

### 4.4 `authorization_events`（私有审计日志）

每次草稿生成、人工批准、发送、退信、回复分类、授权范围修改和公开状态变化都写入不可变事件：

```yaml
event_id:
request_id:
event_type:
actor_type: human | agent | system
actor_id:
occurred_at:
old_status:
new_status:
metadata:
```

邮件正文和附件不直接写入公共仓库；数据库只保存私有存储对象的引用、哈希、MIME 类型和访问权限。

## 5. 公开状态机

### 翻译状态

```text
not-started
  → machine-draft
  → human-review
  → published
  → superseded / withdrawn
```

### 授权状态

```text
not-reviewed
  → license-cleared
  → (无需联系作者)

not-reviewed
  → needs-permission
  → contacted
  → response-pending
  → permission-granted
  → license-scope-verified

response-pending → permission-denied
response-pending → unclear
任何阶段 → withdrawn
```

规则：

- 发送邮件不等于获得授权。
- 作者回复“可以”“没问题”但未说明公开渠道或改编范围时，进入 `unclear`，不能直接发布。
- 只有明确书面许可，或原许可证本身明确允许改编，才可进入 `license-scope-verified`。
- 许可证允许改编但限制非商业或 ShareAlike 时，发布配置必须继承这些条件。
- 任何人都不能通过编辑公共 JSON 直接把 `publication_allowed` 改成 `true`；该字段由私有审核结果生成。

## 6. 授权 Agent 工作流

### 6.1 论文和联系人筛选

管理员按 Frontier、许可证、作者机构、状态和批次筛选论文。Agent 只使用公开论文、作者页面、合作组页面和出版商页面中可核验的联系人来源；不从数据经纪商或无关个人资料中搜集邮箱。

如果一篇论文有多个通讯作者或合作组作者，Agent 应创建多个候选联系人，但默认只生成一封主邮件，避免重复轰炸。

### 6.2 草稿生成

Agent 对每篇论文生成个性化草稿，必须包含：

- 论文题目、ID、原文链接和作者归属。
- 项目简介和非官方翻译声明。
- 请求的具体语言、发布渠道、译文许可证和社区校订方式。
- 原文版权和作者署名如何保留。
- 允许对方选择“同意、附条件同意、拒绝、转交权利人、需要更多信息”。
- 不回复不视为同意。

Agent 不得自动承诺超出项目能力的内容，例如独家翻译、商业出版、代表 Snowmass/作者发言或保证永久不修改。

### 6.3 预检和人工审核

每封草稿进入 `pending-review` 后，管理员看到：

- 论文和联系人来源。
- 当前来源许可证及其限制。
- 请求授权范围。
- 邮件正文和变量替换结果。
- 同一联系人近期已发送的请求数。
- 是否存在已有授权或冲突状态。

管理员可以逐封批准、批量批准同一模板批次、退回修改或取消。只有 `approved-to-send` 的请求可以进入发送队列。

### 6.4 批量发送

批量发送必须具备：

- 每批数量上限和每域名速率限制。
- 去重和冷却期，避免同一联系人重复收到同类请求。
- 退信、自动回复和发送失败记录。
- 发件人身份、项目主页、隐私说明和停止联系方式。
- 不把多个作者放在同一封可见收件人列表中。

第一阶段不自动读取私人邮箱内容；管理员将回复复制为脱敏摘要，或在后台手动分类。这可以避免 Agent 对模糊法律语言作出错误判断。

### 6.5 回复和授权证据

回复分类：

- `granted`：明确允许翻译和指定公开渠道。
- `granted-with-conditions`：有署名、许可证、范围、版本或非商业限制。
- `denied`：明确拒绝。
- `redirected`：转交合作组、出版社或版权持有人。
- `unclear`：礼貌回应但授权范围不明确。
- `no-response`：超出跟进策略仍无回复，不得当成许可。

授权证据必须保存原文、来源、接收时间、适用论文版本和适用范围。公共页面只显示“已获得授权”和范围摘要，不展示私人邮件全文。

## 7. 公开网站设计

### 论文详情页

每页分为四块：

1. **原文信息**：题目、作者、来源版本、arXiv/CDS/HAL 链接。
2. **权利卡片**：来源许可证、是否允许改编、授权状态、发布依据。
3. **翻译卡片**：机器翻译、人工校订、当前版本、贡献者和更新时间。
4. **变更时间线**：只显示公开事件，例如“许可证核验”“机器初译完成”“社区校订发布”“授权状态更新”。

页面必须明确标注“非官方中文翻译”，不能暗示作者、Snowmass、SLAC 或出版方背书。

### 筛选和统计

网站支持按以下条件筛选：

- Frontier、专题组、作者/合作组。
- 来源许可证。
- `license-cleared`、`needs-permission`、`contacted`、`permission-granted` 等公开授权状态。
- `not-started`、`machine-draft`、`human-review`、`published` 等翻译状态。
- 可公开发布、待人工核验、明确禁止改编。

统计图只使用脱敏公共数据，例如各 Frontier 的翻译完成率、授权完成率和待核验数量，不展示个人联系成功率或私信内容。

## 8. 隐私、安全和合规边界

- 联系邮箱只存在私有数据库，禁止进入 Git、公开 JSON、Netlify 构建产物和前端日志。
- API 密钥、邮箱凭据和数据库连接串使用托管密钥，不写入仓库。
- 私有后台采用最小权限；发送权限和修改授权结论权限分离。
- 导出 CSV 时默认去掉邮箱、邮件正文和私人备注。
- 记录数据保留期限和删除/更正流程；作者要求停止联系时，加入抑制名单并阻止后续批量请求。
- 公开页面只展示项目内部状态，不声称构成法律意见或版权清权证明。
- 原始 PDF 默认只保留官方链接；本地缓存目录不进入公开仓库。

## 9. 分阶段交付

### 阶段 1：公开目录和状态模型

- 使用现有 541 条权利清单。
- 生成脱敏公共 manifest。
- 完成论文详情页、筛选、状态徽章和基本时间线。
- 不发送邮件。

验收：公开站点中找不到邮箱和私有备注；每篇论文的公开状态可由 manifest 重建。

### 阶段 2：私有授权后台

- 建立 `contacts`、`authorization_requests` 和 `authorization_events`。
- 加入管理员登录、联系人去重、批次筛选、权限范围表单和授权证据索引。

验收：私有记录不会进入公共构建；状态变更都有操作者和时间戳。

### 阶段 3：Agent 草稿与人工发送

- 批量生成个性化邮件。
- 添加预检、人工批准、速率限制、发送记录和退信记录。
- 第一阶段使用人工回复分类，不自动读取邮箱。

验收：未经人工批准的请求无法发送；同一联系人不会在冷却期内重复收到相同请求；发送失败可重试且不会重复计数。

### 阶段 4：回复辅助和运营统计

- 提供回复摘要录入和条件许可表单。
- 生成授权漏斗、各 Frontier 状态统计和待跟进清单。
- 只有在收件箱隐私、权限和误判测试通过后，才评估自动回复分类。

## 10. 测试与验收标准

### 数据测试

- 541 条论文均有唯一 `paper_id` 和原文链接。
- `publication_allowed=true` 必须关联明确许可证或授权证据。
- `CC BY-ND`、`CC BY-NC-ND`、arXiv non-exclusive 和 HAL Authorization 不得自动进入公开完整译文状态。
- 许可证条件和译文许可证不兼容时，构建失败而不是静默发布。

### 隐私测试

- 公共构建产物不包含邮箱、邮件正文、私人回复、私有对象 URL 或数据库密钥。
- 日志和错误信息不输出完整邮件地址或授权附件内容。
- 非管理员访问私有 API 返回拒绝。

### 工作流测试

- 草稿必须经过人工批准才能发送。
- 发送、退信、拒绝、附条件许可和不回复分别进入正确状态。
- 模糊回复不能自动提升为授权。
- 每次授权状态变化都生成审计事件，并能追溯到论文版本和证据。

## 11. 预期结果

最终系统应把“翻译项目”变成一个可审计的开放协作目录：公众看到清晰、克制的进度；贡献者知道哪些论文可以安全参与；管理员可以批量处理授权请求；作者能够明确知道项目想做什么、如何署名、在哪些渠道发布以及如何提出限制或撤回要求。
