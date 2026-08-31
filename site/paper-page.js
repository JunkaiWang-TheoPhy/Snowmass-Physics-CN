const root = document.querySelector("#paper-page");
const AUTH = { "not-reviewed": "尚未核验", "license-cleared": "许可证已核验", "needs-permission": "需要额外授权", contacted: "已联系", "response-pending": "等待回复", "permission-granted": "已获书面授权", "permission-denied": "未获授权", unclear: "回复范围不明确", withdrawn: "已撤回" };
const TRANSLATION = { "not-started": "尚未开始", "machine-draft": "机器初译", "human-review": "人工审校", published: "已公开", superseded: "已被新版替代", withdrawn: "已撤回" };
const CONDITIONS = { attribution: "保留原作者、题名、来源和许可证署名", "indicate-changes": "明确标注翻译和后续修改", "share-alike": "译文采用兼容的相同方式共享许可证", "non-commercial": "仅限非商业使用", "written-rightsholder-permission-required": "发布完整译文前取得权利人的范围明确书面许可", "no-public-adaptation-under-current-source-license": "当前来源许可证禁止公开演绎作品，需要另行取得权利人许可", "HAL-distribution-authorization-is-not-adaptation-permission": "HAL 分发授权不等于第三方翻译或改编许可", "verify-current-rightsholder-and-license-before-publication": "发布前核验当前权利人并取得翻译与公开传播许可" };

function escapeHTML(value) { return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;"); }
function safeURL(value) { try { const url = new URL(value, location.href); return ["http:", "https:"].includes(url.protocol) ? url.href : "#"; } catch { return "#"; } }
function paperSlug(paper) {
  const record = String(paper.record_id || "");
  if (record.startsWith("arxiv:")) return record.slice(6);
  const source = String(paper.source_url || "");
  const cds = source.match(/cds\.cern\.ch\/record\/(\d+)/); if (cds) return `cds-${cds[1]}`;
  const hal = source.match(/hal-(\d+)/); if (hal) return `hal-${hal[1]}`;
  return encodeURIComponent(record);
}
function requestedSlug() { const match = window.location.pathname.match(/\/paper\/(.+?)\/?$/); return decodeURIComponent(match?.[1] || "").replace(/^arxiv:/i, ""); }
function badgeClass(status) { if (["license-cleared", "permission-granted", "published"].includes(status)) return "badge-cleared"; if (["needs-permission", "contacted", "response-pending", "unclear", "machine-draft", "human-review"].includes(status)) return "badge-pending"; if (["permission-denied", "withdrawn"].includes(status)) return "badge-blocked"; return ""; }
function row(term, value) { return `<dt>${escapeHTML(term)}</dt><dd>${value}</dd>`; }

function renderPaper(paper, position, total) {
  const authorization = AUTH[paper.authorization_status] || paper.authorization_status || "尚未核验";
  const translation = TRANSLATION[paper.translation_status] || paper.translation_status || "状态未知";
  const translationURL = paper.publication_allowed === true && paper.publication_translation_url ? safeURL(paper.publication_translation_url) : null;
  const conditions = (paper.publication_conditions || []).map((item) => `<li>${escapeHTML(CONDITIONS[item] || item)}</li>`).join("") || "<li>当前记录未列出额外条件。</li>";
  const topics = (paper.topics || []).map((item) => `<li>${escapeHTML(item)}</li>`).join("") || "<li>专题信息待补充。</li>";
  const permalink = `${location.origin}/paper/${paperSlug(paper)}/`;
  document.title = `${paper.title_zh || paper.title} | Snowmass 中文翻译计划`;
  root.innerHTML = `<article class="paper-permalink">
    <header class="paper-permalink-hero"><p class="eyebrow">TRANSLATION PAGE · 本论文译文页</p><p class="paper-index">第 ${position} 篇 / 共 ${total} 篇 · PAPER ${String(position).padStart(3, "0")} / ${total}</p><h1>${escapeHTML(paper.title_zh || paper.title)}</h1>${paper.title_zh ? `<p class="paper-original-title">${escapeHTML(paper.title)}</p>` : ""}<p class="detail-authors">${escapeHTML(paper.authors_as_listed || "作者信息待核验")}</p><div class="badges" aria-label="当前状态"><span class="badge ${badgeClass(paper.authorization_status)}">${escapeHTML(authorization)}</span><span class="badge ${badgeClass(paper.translation_status)}">${escapeHTML(translation)}</span><span class="badge ${paper.publication_allowed ? "badge-cleared" : "badge-blocked"}">${paper.publication_allowed ? "具备公开改编基础" : "暂不可公开全文"}</span></div></header>
    <div class="paper-permalink-actions">${translationURL ? `<a class="button button-primary" href="${translationURL}">阅读中文译文 PDF</a>` : `<span class="translation-unavailable">中文全文尚未公开</span>`}<a class="button button-secondary" href="${safeURL(paper.source_url)}" target="_blank" rel="noreferrer">查看英文原文 ↗</a></div>
    <div class="detail-grid paper-permalink-grid"><section class="detail-block"><h2>论文记录</h2><dl class="definition-list">${row("文献编号", escapeHTML(paper.paper_id || paper.record_id))}${row("永久链接", `<a href="${permalink}">${escapeHTML(permalink)}</a>`)}${row("年份", escapeHTML(paper.publication_year || "未提供"))}${row("研究前沿", escapeHTML((paper.frontiers || []).join(" · ") || "未分类"))}${row("页数", escapeHTML(paper.page_count ?? "未提供"))}${row("引用数", escapeHTML(paper.citation_count ?? "未提供"))}</dl></section>
    <section class="detail-block"><h2>来源与权利</h2><dl class="definition-list">${row("来源许可证", `<a href="${safeURL(paper.source_license_url)}" target="_blank" rel="noreferrer">${escapeHTML(paper.source_license || "未知")} ↗</a>`)}${row("允许改编", paper.permits_adaptation === true ? "是（须遵守条件）" : paper.permits_adaptation === false ? "否（须另行授权）" : "未确认")}${row("授权状态", escapeHTML(authorization))}${row("来源版本", escapeHTML(paper.source_version || "未提供"))}${row("最后核验", escapeHTML(paper.public_updated_at ? paper.public_updated_at.slice(0, 10) : "未知"))}</dl></section>
    <section class="detail-block"><h2>发布条件</h2><ul class="condition-list">${conditions}</ul></section><section class="detail-block"><h2>专题</h2><ul class="topic-list">${topics}</ul></section></div>
    <aside class="rights-note"><h2>关于译文状态</h2><p>${paper.publication_allowed ? "本记录具备公开改编基础，但只有完成翻译、审校和许可证条件后，中文全文才会在此页面公开。" : "本记录当前没有足够的公开改编授权；未获许可前，本页只展示目录、来源与进度信息。"}</p></aside></article>`;
}
function renderMissing(slug) { document.title = "论文未收录 | Snowmass 中文翻译计划"; root.innerHTML = `<section class="paper-page-error"><p class="eyebrow">RECORD NOT FOUND</p><h1>未找到这篇论文</h1><p>目录中没有与 <code>${escapeHTML(slug)}</code> 匹配的记录。请检查永久链接，或返回论文目录。</p><a class="button button-primary" href="../?lang=zh#catalog">返回论文目录</a></section>`; }
async function loadPaper() { const slug = requestedSlug(); try { const response = await fetch(document.body.dataset.papersUrl, { cache: "no-cache" }); if (!response.ok) throw new Error(`papers.json: ${response.status}`); const papers = await response.json(); const paper = papers.find((entry) => paperSlug(entry).toLowerCase() === slug.toLowerCase()); if (!paper) return renderMissing(slug); renderPaper(paper, papers.indexOf(paper) + 1, papers.length); } catch (error) { console.error("Unable to load paper record", error); root.innerHTML = `<section class="paper-page-error"><p class="eyebrow">LOAD ERROR</p><h1>暂时无法读取论文记录</h1><p>请稍后刷新页面。</p><button class="button button-primary" type="button" id="paper-retry">重新加载</button></section>`; document.querySelector("#paper-retry").addEventListener("click", loadPaper); } }
loadPaper();
