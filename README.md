# Snowmass 中文翻译计划

Snowmass 2021 contributed/white papers 的非官方中文翻译、权利核验与开放协作目录。

本项目先核验每篇论文是否允许翻译和公开传播，再推进机器初译、术语统一与人工审校。没有明确开放改编许可、或没有取得范围明确书面授权的论文，可以记录进度，但不会在公开仓库或网站发布完整译文。

> 本项目不代表 Snowmass、SLAC、arXiv、HAL、作者、合作组或出版机构。公开状态是项目内部工作流结论，不构成法律意见或面向第三方的版权清权证明。

## 当前范围

| 指标 | 当前值 | 口径 |
|---|---:|---|
| 去重白皮书 | 541 | 538 条 arXiv 记录，3 条 CDS/HAL 外部记录 |
| 许可证允许改编 | 273 | 仍须逐篇遵守署名、ShareAlike、非商业等条件 |
| 需要额外授权 | 268 | 包括 arXiv non-exclusive、HAL Authorization 和 ND 类许可证 |
| PDF 总页数 | 17,794 | 541 份 PDF 的物理页数 |
| Unicode token 代理总量 | 8,311,295 | 包含参考文献、图注和抽取噪声，不等于模型实际计费 token |

Snowmass 官方提交索引曾报告 548 份 submissions，其中有跨 Frontier 重复。仓库使用最终文集整理出的 541 条去重身份作为当前工作基线，并保留 927 个官方交叉归类位置。

## 网站

静态站点位于 [`site/`](site/)，支持：

- 按题名、作者、arXiv 编号、Frontier 和专题检索；
- 按来源许可证、授权状态、翻译状态和发布资格筛选；
- 按引用数、影响力代理、篇幅和年份排序；
- 查看逐篇来源许可、公开条件、翻译进度、审校者和体量统计。

本地预览：

```bash
python3 -m http.server 4173 --directory site
```

打开 `http://127.0.0.1:4173/`。仓库包含 [`netlify.toml`](netlify.toml)，连接 GitHub 后可由 Netlify 直接发布 `site/`；公开 URL 会在首次部署成功后写回本页。

## 权利处置原则

| 来源许可 | 是否可直接推进公开译文 | 强制条件 |
|---|---|---|
| CC0-1.0 | 可以 | 保留可核验的来源和版本记录 |
| CC-BY-4.0 | 可以 | 署名、原文链接、许可证链接、标明翻译和修改 |
| CC-BY-SA-4.0 | 可以 | CC BY 条件 + 译文使用兼容的 ShareAlike 许可证 |
| CC-BY-NC-SA-4.0 | 可以 | 署名、非商业、ShareAlike、标明翻译和修改 |
| CC-BY-ND / CC-BY-NC-ND | 不可以 | 翻译是演绎作品；公开前另取权利人书面授权 |
| arXiv non-exclusive distribution | 不可以 | 该许可授予 arXiv 分发权，不是第三方改编许可 |
| HAL Authorization | 不可以 | 该授权允许 HAL 分发，不是第三方改编许可 |
| Unknown / 授权范围不清 | 不可以 | 人工核验当前权利人和书面许可范围 |

完整规范见 [`docs/RIGHTS_PROTOCOL.md`](docs/RIGHTS_PROTOCOL.md)。公共字段和状态机见 [`docs/PUBLIC_DATA_MODEL.md`](docs/PUBLIC_DATA_MODEL.md)。

## 翻译流水线

公开项目默认采用以下可审计流程：

```text
原文与版本锁定
  → 权利核验
  → 忠实机器初译
  → 锁定术语表统一
  → 去除公式化机器腔
  → 中文学术表达自然化
  → 人工技术审校
  → 许可证兼容检查
  → 公开发布
```

后四个阶段不得添加事实，或改变数字、单位、公式、引用、链接、人名和锁定术语。每个阶段分别保留可追溯产物，便于审校、回滚和续跑。

## 数据刷新和验证

公共网站不直接读取包含内部研究字段的源清单。它由脚本生成一份脱敏 manifest：

```bash
python3 scripts/build_public_manifest.py
python3 -m unittest scripts.test_public_manifest -v
```

生成结果：

- [`site/data/papers.json`](site/data/papers.json)：541 条逐篇公开记录；
- [`site/data/stats.json`](site/data/stats.json)：许可证、授权、Frontier、年份、篇幅和引用汇总。

构建测试会拒绝重复论文 ID、缺失权利字段、无依据的 `publication_allowed=true`、邮箱地址和常见凭据模式。

## 目录结构

```text
site/                         静态公开网站与脱敏 manifest
translations/                 允许公开的分阶段译文目录规范
output/snowmass2021/          原始名录、统计分析与逐篇权利研究
scripts/                      数据收集、文本准备、翻译与公开构建脚本
docs/RIGHTS_PROTOCOL.md       规范性的权利、授权与发布协议
docs/PUBLIC_DATA_MODEL.md     公开字段、枚举值和状态机
private/                      私有授权后台的无数据 schema 与安全边界
.github/                      PR 模板和持续验证
```

原始 PDF 只保留官方链接，本地缓存位于 Git 忽略目录，不进入公开仓库。

## 参与贡献

欢迎通过 Pull Request：

- 修正论文元数据或许可证证据；
- 认领机器初译或人工审校；
- 改进术语表、中文表达和公式排版；
- 改进网站、统计脚本和权利验证测试。

提交前必须阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md)。PR 不得包含作者邮箱、私人通信、授权附件、API 密钥、下载的原始 PDF，或未满足发布闸门的完整译文。

## 许可证

仓库中的项目代码和项目自有文档采用 [GNU Affero General Public License v3.0](LICENSE)。这份根许可证不覆盖 Snowmass 原论文，也不会把所有译文自动变成 AGPL-3.0。

每篇原论文继续受其来源许可证或版权状态约束；每篇译文的许可证由来源许可兼容性或权利人书面授权决定，并记录在逐篇 metadata 中。不得用根目录 `LICENSE` 覆盖逐篇权利记录。

## 一手来源

- [Snowmass 2021 contributed-paper index](https://atlaswww.hep.anl.gov/snowmass21/doku.php?id=submissions:start)
- [Snowmass 2021 Proceedings](https://www.slac.stanford.edu/econf/C210711/)
- [arXiv license information](https://info.arxiv.org/help/license/index.html)
- [arXiv translations policy](https://info.arxiv.org/help/translations.html)
- [HAL Authorization v1](https://about.hal.science/en/hal-authorisation-v1/)
- [Creative Commons license chooser and conditions](https://creativecommons.org/share-your-work/cclicenses/)
