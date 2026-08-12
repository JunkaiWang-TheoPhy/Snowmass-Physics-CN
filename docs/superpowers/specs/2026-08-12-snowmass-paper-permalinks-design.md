# Snowmass 单篇论文永久链接设计

## 目标

为公开目录中的每条论文记录提供稳定、可直接访问和分享的站内地址：
`https://snowmass-physics-cn.netlify.app/paper/<arXiv-id>/`。示例论文使用
`https://snowmass-physics-cn.netlify.app/paper/2203.07506/`。

## 架构

网站继续以 `site/data/papers.json` 作为唯一公开数据源，不为 541 篇论文复制静态 HTML。
Netlify 将 `/paper/*` 内部重写到现有入口页并保留浏览器地址；`site/app.js` 从路径提取
arXiv ID，映射为 `record_id`，然后直接渲染详情。首页原有 `?paper=arxiv:...` 地址继续兼容，
但站内详情入口统一生成永久路径。

页面资源和公开 JSON 使用根路径，保证入口从多级路径加载时不失效。不存在或格式错误的论文
ID 显示站内“未收录”状态，不静默跳回首页。

## 封面契约

第 06 栏显示 `TRANSLATION PAGE / 本论文译文页` 以及无协议前缀的站内永久地址。
页面底部翻译主页、二维码和交互 PDF 中对应的可点击热区均改为该论文永久地址。
链接由论文 `record_id` 推导，不得在批量封面中硬编码示例编号。

## 验证

- 自动测试验证 Netlify 使用 200 rewrite、嵌套路径资源可加载、路径解析与旧查询参数兼容。
- 自动测试验证封面链接生成及缺失 DOI 文案。
- 本地 HTTP 请求验证 `/paper/2203.07506/` 返回入口页。
- 重渲染并目视检查封面，检查二维码内容和 PDF URI 热区。
