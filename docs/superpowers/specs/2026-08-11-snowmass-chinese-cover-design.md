# Snowmass 中文译本说明页设计

## 目标

为每份可公开发布的 Snowmass 中文译本添加一张独立的第一页，同时原始论文及 BabelDOC 生成页面保持不变。说明页用于标明中文译本性质、来源、贡献者、网站、联系信息和许可证条件。

## 页面内容

- 项目名：`Snowmass White Paper Chinese Translation Collaboration`
- PDF 横幅使用由规范源图 `site/assets/snowmass-mountain.svg` 离线生成并已提交的栅格派生图 `site/assets/snowmass-mountain.png`，同时保留 SVG 作为源 artwork。
- 中文标题和英文原标题。
- 原作者。
- arXiv、DOI（存在时）和原文链接；链接在 PDF 中可点击。
- 中文翻译贡献者：`WangTheoPhys*`
- `Website (Temporary): snowmass-physics-cn.netlify.app`
- 指向 `https://snowmass-physics-cn.netlify.app/` 的二维码和可点击链接。
- 翻译版本及日期。
- 声明：`本译文由中文翻译协作项目制作，不代表原作者审定或认可；如有歧义，以英文原文为准。`
- 原文许可证、许可证链接及适用的署名/注明修改要求。
- 脚注：`*Contact: WangTheoPhys@outlook.com`

## 架构

采用后置装订：BabelDOC 先照常生成 PDF，独立脚本再生成一页说明页，并把它插入发布版 PDF 的最前面。脚本从 `site/data/papers.json` 读取权利和论文元数据；缺失 DOI 时显示 `DOI: 未提供`，不得猜测。只有 `publication_allowed: true` 的记录可以生成发布版。

原始 BabelDOC PDF 不覆盖。输出为新的、稳定命名的发布文件，并记录输入、说明页和最终文件的 SHA-256，以支持断点续做和审计。

## 版式

A4 竖版。上部为现有雪山横幅；中部依次呈现项目名、中文标题、英文标题和作者；下部为来源与许可信息。二维码位于网站信息右侧，贡献者、免责声明和联系脚注位于页面底部。整体沿用网站的灰蓝色、低饱和学术风格，不使用机构徽标。

## 验证

- 单元测试验证权利门、元数据、页面数量、首页文本、链接、二维码和哈希记录。
- 对实际 A/B 论文生成样张，渲染第一页为 PNG，检查中文字体、换行、链接区、二维码清晰度以及是否存在裁切或重叠。
- 最终 PDF 的页数必须等于输入 PDF 页数加一，原输入页的内容哈希语义上不被改写。
