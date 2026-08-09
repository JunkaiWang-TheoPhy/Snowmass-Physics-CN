# Snowmass 2021 白皮书名录

本目录按 Snowmass 官方的 **contributed (white) papers** 口径整理，不把 Frontier Summary、Topical Group 或 cross-Frontier reports 混入白皮书名录。

## 结果

- 去重后的论文身份：**541**（538 篇有 arXiv 编号，3 篇只有 CERN/HAL 外部记录）。
- 官方十个 Frontier 页面列出的交叉归类位置：**927**。
- 官方归档页面的大小写敏感 PDF 路径：**546**；其中有 6 个记录存在路径/大小写变体，详见 `path_variants.json`。
- Snowmass 维基提交索引曾声明总数 **548**（页面最后修改于 2022-10-18）；该数字与最终 SLAC proceedings HTML 的当前归档快照不完全一致，因此这里同时保留两种官方口径，不强行把差异算作新增论文。

## 文件

- `snowmass2021_whitepapers.md`：可直接阅读的 541 条完整表格。
- `snowmass2021_whitepapers.csv`：适合 Excel、表格筛选和排序。
- `snowmass2021_whitepaper_placements.csv`：官方页面的 927 个交叉归类位置（保留重复项，含 Frontier/专题组归属）。
- `snowmass2021_whitepapers.json`：完整字段，包括 Frontier、专题组、交叉归类次数、官方 PDF 路径变体及标题/作者变体。
- `metadata.json`：来源、统计口径和校验数字。
- `path_variants.json`：官方归档中大小写或路径不一致的条目。
- `rights/`：逐篇许可证与翻译发布处置记录；包含 CSV/JSON 清单、统计摘要和可审计的 arXiv 页面缓存。

## 版权与翻译发布状态

`rights/snowmass2021_rights_manifest.csv` 覆盖全部 541 条记录。当前调研以 arXiv 每篇当前版本的许可证页面为一手证据；3 条外部 CDS/HAL 记录使用官方 PDF 或仓储元数据补充，并另存本地 PDF 前两页的版权/许可提示。仅显示“允许改编”的许可证并不等于可以忽略署名、版本、ShareAlike 或非商业条件；`hold-*` 记录在人工核验或取得授权前不应发布完整中文译文。

尚未生成译文的条目统一记为 `translation_status: not-started`，不会把权利调研误报成机器翻译完成。

本工作区根目录的 `LICENSE` 不应被解释为 Snowmass 原论文或其中文翻译的统一授权；公开项目发布时应继续按 `rights/` 中的逐篇记录拆分代码、编辑内容和第三方原作的许可。

## 权威来源

- [Snowmass 2021 contributed-paper index](https://atlaswww.hep.anl.gov/snowmass21/doku.php?id=submissions:start)
- [Snowmass 2021 Proceedings](https://www.slac.stanford.edu/econf/C210711/)
