# Snowmass 中文翻译权利、授权与发布协议

版本：1.0

生效日期：2026-08-09

适用范围：本仓库、公开网站、机器翻译产物、人工校订稿、授权 Agent 和相关 Pull Request

本文件使用“必须”“不得”“应当”表达规范性要求。它是项目的发布控制协议，不是法律意见；出现许可证冲突、权利主体争议或授权范围争议时，必须暂停公开并交由具备相应权限和专业能力的人复核。

## 1. 基本原则

1. 原论文的版权和许可证事实，与本项目对译文的工作流处置，必须分开记录。
2. 翻译是演绎作品。除公共领域、明确允许改编的开放许可证或范围明确的权利人书面许可外，不得公开完整译文。
3. 机器完成翻译不等于获得发布权；发送授权请求、收到自动回复或对方保持沉默，也不等于获得授权。
4. 每篇论文独立清权。不得以根目录许可证、同一作者其他论文的许可证、同一合作组惯例或 arXiv 收录事实替代逐篇证据。
5. 公开仓库只保存公众可核验的信息。联系人邮箱、私人通信、内部备注和未脱敏证据只保存在私有系统。
6. 发现状态不确定、证据矛盾或许可证兼容性不明时，发布闸门默认关闭。

## 2. 权威依据

- [arXiv 许可证说明](https://info.arxiv.org/help/license/index.html)：作者或其他权利人通常保留版权；不同版本可以采用不同许可证；arXiv non-exclusive license 是向 arXiv 授予的分发许可。
- [arXiv 翻译政策](https://info.arxiv.org/help/translations.html)：翻译属于演绎作品；仍受版权保护的作品在发布译文前需要当前版权持有人的许可。
- [arXiv non-exclusive distribution license](https://arxiv.org/licenses/nonexclusive-distrib/1.0/)：授权 arXiv 非独占分发，不向任意第三方授予改编权。
- [HAL Authorization v1](https://about.hal.science/en/hal-authorisation-v1/)：允许 HAL 在线传播；超出法定例外的复制或改编仍需联系作者或权利人。
- [Creative Commons licenses](https://creativecommons.org/share-your-work/cclicenses/)：BY、SA、NC、ND 条件的官方说明。

若仓储网页、论文 PDF、出版社页面和作者书面回复不一致，必须保存各证据及时间戳，暂停自动发布，并人工判断哪个权利主体、作品版本和许可文本有效。

## 3. 每篇论文的最低记录

每篇论文至少必须记录：

```yaml
paper_id: "arXiv:xxxx.xxxxx"
source_url: "https://arxiv.org/abs/xxxx.xxxxx"
source_version: "v1"
source_license: "CC-BY-4.0"
source_license_url: "https://creativecommons.org/licenses/by/4.0/"
permits_adaptation: true
license_decision: "eligible-after-attribution-check"
authorization_status: "license-cleared"
publication_allowed: true
publication_basis: "source-license"
translation_status: "machine-draft"
translation_license: "CC-BY-SA-4.0"
machine_model: "provider/model-name"
human_reviewers: []
public_updated_at: "2026-08-09T00:00:00Z"
```

`publication_allowed` 是发布闸门的派生字段，不是可以独立编辑的主观标签。任何把它改为 `true` 的变更都必须同时满足本协议第 5 节，并通过自动测试和人工复核。

## 4. 来源许可证处置矩阵

| `source_license` | `permits_adaptation` | 默认处置 | 公开译文条件 |
|---|---:|---|---|
| `CC0-1.0` | `true` | `license-cleared` | 保留来源、版本和项目变更记录 |
| `CC-BY-4.0` | `true` | `license-cleared` | 署名、原文链接、许可链接、标明翻译和修改 |
| `CC-BY-SA-4.0` | `true` | `license-cleared` | CC BY 条件；译文采用 CC BY-SA 4.0 或经核验兼容许可 |
| `CC-BY-NC-SA-4.0` | `true` | `license-cleared` | 署名、非商业、ShareAlike；译文不得误标为允许商业使用 |
| `CC-BY-ND-*` | `false` | `needs-permission` | 另取权利人的范围明确书面许可 |
| `CC-BY-NC-ND-*` | `false` | `needs-permission` | 另取权利人的范围明确书面许可，并遵守授权中的非商业条件 |
| `ARXIV-NONEXCLUSIVE-DISTRIB-1.0` | `null` | `needs-permission` | 核验当前权利人并取得翻译、公开传播和社区修改许可 |
| `HAL-AUTHORIZATION-V1` | `null` | `needs-permission` | 核验当前权利人并取得翻译、公开传播和社区修改许可 |
| `unknown` | `null` | `needs-permission` | 找到可核验证据或取得权利人书面许可 |

不得仅凭论文出现在 arXiv、HAL、CDS、INSPIRE、Snowmass proceedings 或作者个人网站，就推断第三方可以公开翻译。

## 5. 发布闸门

完整译文只有在以下两条路径之一成立时才可公开。

### 路径 A：来源许可证允许改编

必须同时满足：

1. 证据指向所翻译的具体版本；
2. 许可证明确允许制作演绎作品；
3. 译文许可证与 BY、SA、NC 等条件兼容；
4. 译文显著标注“非官方中文翻译”；
5. 保留作者、原题名、原文链接、来源版本、来源许可证链接和修改说明；
6. 机器模型、翻译阶段和公开审校者可追溯；
7. 自动检查和人工复核均通过。

### 路径 B：取得书面授权

必须同时满足：

1. 回复者是当前权利人、明确获授权的代表，或可核验的合作组/出版方联系人；
2. 书面回复能对应具体论文和版本；
3. 明确允许翻译为简体中文；
4. 明确允许在 GitHub 和公开静态网站传播完整译文；
5. 明确是否允许社区通过 Pull Request 修改译文；
6. 明确译文适用的许可证、署名、非商业、期限、地域和撤回条件；
7. 私有系统保存原始回复、接收时间、证据哈希和适用范围；
8. 公开 manifest 只发布脱敏范围摘要和状态，不公开私人通信。

“可以”“没问题”“欢迎翻译”等未明确渠道、修改权或许可证范围的回复必须标记为 `unclear`，不得自动打开发布闸门。

## 6. 翻译状态机

允许的公开状态：

```text
not-started
  → machine-draft
  → human-review
  → published
  → superseded / withdrawn
```

- `not-started`：没有受项目控制的机器草稿。
- `machine-draft`：已经生成机器初译；不表示允许公开。
- `human-review`：至少一名人工审校者正在核对意义、术语、公式、引用和中文表达。
- `published`：译文通过发布闸门，并存在公开版本 URL。
- `superseded`：已由新版译文或新版原文对应译文替代。
- `withdrawn`：因权利、质量、作者请求或其他原因停止公开。

没有发布资格的机器草稿必须保存在私有或本地受控存储中，不能通过 Git history、PR artifact、预览部署或公开 CI 日志泄露。

## 7. 授权状态机

允许的公开状态：

```text
not-reviewed
  → license-cleared

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

本项目当前公共 schema 把 `license-scope-verified` 归并展示为 `permission-granted`；私有系统仍应保留两步审核，避免“收到许可”在范围核验前直接触发发布。

## 8. 授权 Agent 的强制边界

授权 Agent 可以：

- 从论文、作者主页、合作组主页和出版方页面整理候选公开联系人；
- 针对具体论文生成个性化授权请求草稿；
- 检查变量替换、重复联系人、既有授权、退信和冷却期；
- 生成待人工审批的批次和后续跟进清单；
- 对回复提供非决定性的分类建议。

授权 Agent 不得：

- 未经人工确认自动发送首封或跟进邮件；
- 从数据经纪商或无关个人资料搜集联系方式；
- 把多个无关作者放在同一封可见收件人列表；
- 把沉默、自动回复、转发或模糊礼貌回复视为许可；
- 自动作出版权归属、许可证兼容或授权范围的最终法律判断；
- 把联系人、邮件正文、附件、内部评分或私人备注写入公开仓库。

每封请求必须清楚说明：项目非官方性质、论文题名和 ID、简体中文翻译、GitHub/网站公开渠道、社区 PR 修改方式、拟议译文许可证、署名方式、不回复不视为同意，以及对方可以附条件同意、拒绝、转交权利人或要求停止联系。

批量发送必须设置每批上限、每域名限速、去重、冷却期、退信记录和抑制名单。作者要求停止联系后，必须阻止后续自动批次再次加入该联系人。

## 9. 授权证据与隐私

私有证据至少记录：

- 论文 ID、论文版本和权利主体；
- 请求范围和拟议许可证；
- 发出时间、接收时间和操作者；
- 原始回复的只读对象引用、SHA-256 哈希和 MIME 类型；
- 授权结论、附加条件、有效期和复核者；
- 公开状态变更事件。

公开仓库和静态构建不得包含：

- 通讯作者或权利人的邮箱、电话、私人地址；
- 邮件正文、邮件头、附件、签名档和私人回复；
- Supabase、邮件服务、GitHub、Netlify 或模型供应商凭据；
- 内部备注、联系人质量评分和未脱敏对象 URL。

公开状态只允许包含 `authorization_status`、`publication_allowed`、`publication_basis`、条件摘要和更新时间。

## 10. Pull Request 发布检查

包含译文或权利状态修改的 PR 必须：

1. 指向对应 `paper_id` 和固定原文版本；
2. 提供公开可核验的来源许可证链接，或引用私有授权记录 ID；
3. 说明译文许可证为何兼容；
4. 保留作者、原题名、原文链接和修改说明；
5. 记录机器模型、生成日期、流水线阶段和人工审校者；
6. 不包含私人联系方式、通信、原始 PDF 或秘密；
7. 通过 manifest、敏感数据和发布闸门自动测试；
8. 获得至少一名具备合并权限的人工审核者批准。

公共 JSON 的手工修改不能代替来源权利清单或私有授权记录。生成字段必须通过构建脚本更新。

## 11. 更正、撤回和下架

作者、权利人或其代表提出更正、署名修改、范围限制或下架请求时：

1. 立即把相关译文标记为 `withdrawn` 或关闭公开访问；
2. 保留最小审计事件，不继续公开争议内容；
3. 私下核验请求人与权利主体的关系；
4. 根据许可证、授权文本和适用要求作出人工处置；
5. 在公开时间线上记录脱敏结果，例如“应权利人请求撤回”或“署名已更正”；
6. 清除缓存和预览部署中的受影响全文。

如果 Git history 已包含不应公开的数据，应先撤下站点和仓库访问，再制定历史清理和凭据轮换方案。不得只删除最新文件后宣称风险已消除。

## 12. 版本和复核

- 来源论文版本变化时必须重新核验许可证；arXiv 不同版本可能有不同许可证。
- 权利规则、公开 schema 或授权邮件范围变化时，应更新本协议版本并在 PR 中说明迁移影响。
- 引用数、影响力代理和篇幅统计只用于浏览与排序，不参与权利判断。
- 每次公开发布前，CI 必须重新生成脱敏 manifest，并验证记录数、发布依据和敏感数据扫描。
