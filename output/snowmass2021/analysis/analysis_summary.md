# Snowmass 2021 白皮书统计与排序

统计对象：541 篇去重后的 contributed (white) papers。引用元数据抓取日期：2026-08-09。

## 口径

- 学科：以 Snowmass Frontier/专题组为主，同时保留 INSPIRE 的 primary arXiv category 和学科族；跨 Frontier 论文在各相关 Frontier 中计数，`fractional_paper_count` 用于不重复的份额统计。
- 年份：优先使用 INSPIRE `earliest_date`；未匹配的外部记录只按 2022 年推断，并在 `year_source` 标注。
- 引用：使用 INSPIRE `citation_count` 与 `citation_count_without_self_citations`；覆盖 537/541 篇。INSPIRE 只统计其数据库覆盖记录中的引用，不能等同于 Google Scholar、Web of Science、Scopus 或 ADS。
- 影响力代理分数：`0.7 × 引用数百分位 + 0.3 × 年化引用数百分位`，只在有 INSPIRE 引用数的记录中计算；它是排序代理，不是期刊影响因子或官方评价。

## 最高引用论文（前 10）

| # | 题名 | arXiv | 引用数 | 年化引用 |
|---:|---|---|---:|---:|
| 1 | Cosmology Intertwined: A Review of the Particle Physics, Astrophysics, and Cosmology Associated with the Cosmological Tensions and Anomalies | 2203.06142 | 1594 | 361.17 |
| 2 | Quantum Simulation for High Energy Physics | 2204.03381 | 544 | 125.36 |
| 3 | The Forward Physics Facility at the High-Luminosity LHC | 2203.05090 | 422 | 95.50 |
| 4 | Axion Dark Matter | 2203.14923 | 346 | 79.23 |
| 5 | The Present and Future Status of Heavy Neutral Leptons | 2203.08039 | 343 | 77.91 |
| 6 | Inflation: Theory and Observations | 2203.08128 | 320 | 72.69 |
| 7 | Generalized Symmetries in Quantum Field Theory and Beyond | 2205.09545 | 312 | 73.85 |
| 8 | The International Linear Collider | 2203.07622 | 293 | 66.51 |
| 9 | The Forward Physics Facility: Sites, Experiments, and Physics Potential | 2109.10905 | 287 | 58.83 |
| 10 | Celestial Holography | 2111.11392 | 284 | 60.27 |

## 最高影响力代理分数论文（前 10）

| # | 题名 | arXiv | 代理分数 | 引用数 |
|---:|---|---|---:|---:|
| 1 | Cosmology Intertwined: A Review of the Particle Physics, Astrophysics, and Cosmology Associated with the Cosmological Tensions and Anomalies | 2203.06142 | 99.91 | 1594 |
| 2 | Quantum Simulation for High Energy Physics | 2204.03381 | 99.72 | 544 |
| 3 | The Forward Physics Facility at the High-Luminosity LHC | 2203.05090 | 99.53 | 422 |
| 4 | Axion Dark Matter | 2203.14923 | 99.35 | 346 |
| 5 | The Present and Future Status of Heavy Neutral Leptons | 2203.08039 | 99.16 | 343 |
| 6 | Inflation: Theory and Observations | 2203.08128 | 98.92 | 320 |
| 7 | Generalized Symmetries in Quantum Field Theory and Beyond | 2205.09545 | 98.85 | 312 |
| 8 | The International Linear Collider | 2203.07622 | 98.60 | 293 |
| 9 | The Forward Physics Facility: Sites, Experiments, and Physics Potential | 2109.10905 | 98.31 | 287 |
| 10 | Celestial Holography | 2111.11392 | 98.29 | 284 |

## 作者论文数排行（前 20；含合作组名称）

| # | 作者/合作组 | 论文数 | Frontier 数 | 合作论文引用总和 |
|---:|---|---:|---:|---:|
| 1 | Tsai, Yu-Dai | 22 | 10 | 2956 |
| 2 | Shiltsev, Vladimir D. | 20 | 8 | 792 |
| 3 | Kelly, Kevin J. | 17 | 8 | 2088 |
| 4 | Han, Tao | 16 | 9 | 1405 |
| 5 | Xie, Keping | 15 | 9 | 2231 |
| 6 | Liu, Zhen | 14 | 9 | 1949 |
| 7 | Nachman, Benjamin | 14 | 9 | 1644 |
| 8 | Nagaitsev, Sergei | 14 | 9 | 888 |
| 9 | Cyr-Racine, Francis-Yan | 13 | 4 | 2613 |
| 10 | Takhistov, Volodymyr | 13 | 6 | 1758 |
| 11 | Chachamis, Grigorios | 13 | 9 | 1665 |
| 12 | Diwan, Milind Vaman | 13 | 9 | 1122 |
| 13 | Barzi, Emanuela | 13 | 10 | 593 |
| 14 | Dutta, Bhaskar | 12 | 8 | 1422 |
| 15 | Batell, Brian | 12 | 7 | 1370 |
| 16 | Kahn, Yonatan | 12 | 7 | 1312 |
| 17 | Meade, Patrick Roddy | 12 | 8 | 1217 |
| 18 | Denisov, Dmitri S | 12 | 8 | 1199 |
| 19 | Barrow, Joshua L. | 12 | 9 | 645 |
| 20 | Belomestnykh, Sergey | 12 | 9 | 599 |

## 篇幅与字数（PDF 文本抽取）

- 541 篇 PDF 共 **17,794 页**；平均 32.9 页，中位数 21 页，P25/P75 为 12/39 页。
- 去空白字符 **43,116,615**；Unicode token **8,311,295**；Latin word token **7,319,902**，平均约 13,530 个/篇。
- 页数分布：<10 页 86 篇；10–19 页 169 篇；20–39 页 155 篇；40–79 页 90 篇；80–119 页 29 篇；≥120 页 12 篇。
- 口径与逐篇结果见 `length_summary.md`、`length_summary.json` 和 `length_records.csv`。词数包含参考文献、图表标题、页眉页脚及抽取噪声，不是出版商权威正文 word count。

## 主要文件

- `enriched_papers.csv/json`：补齐作者、年份、学科分类和引用字段的论文级数据。
- `discipline_statistics.csv`、`topic_statistics.csv`、`discipline_family_statistics.csv`：学科/专题统计。
- `year_statistics.csv`：年份趋势。
- `author_ranking.csv`：作者/合作组排行。
- `paper_ranking_citations.csv`、`paper_ranking_citation_velocity.csv`、`paper_ranking_impact_proxy.csv`：论文排序。
- `length_records.csv/json`、`length_summary.md/json`：PDF 页数、字符、token 和词数统计。
- `analysis_metadata.json`：数据源、抓取日期、覆盖率和局限性。
