const PAGE_SIZE = 24;
const LANG_KEY = "snowmass-language";
const THEME_KEY = "snowmass-theme";

const UI = {
  zh: {
    skipCatalog: "跳到论文目录", brandHome: "Snowmass 中文翻译计划首页", brandName: "Snowmass 中文翻译计划",
    primaryNav: "主要导航", navCatalog: "论文目录", navProgress: "项目进展", navContributors: "同行者", navGuide: "参与指南",
    navWorkflow: "翻译流程", navRights: "授权档案 ↗", navContribute: "在 GitHub 参与",
    heroTitle: "翻越语言的雪线，<br>抵达开放知识。", contactLabel: "项目联系：",
    heroLede: "一个由机器翻译起步、由研究共同体持续审校的中文开放档案。每篇论文都独立记录来源许可证、授权进度、模型与人工修订；没有明确改编许可的全文不会公开。",
    heroExplore: "探索论文图谱", heroRights: "了解授权原则", gateAria: "发布闸门说明", gateLabel: "PUBLICATION GATE · 权利先行",
    gateText: "只有来源许可证允许改编，或权利人给出范围明确的书面授权，完整译文才会公开。",
    signalCleared: "许可证已核验", signalPending: "等待授权", signalBlocked: "暂不公开", metricsAria: "项目统计",
    metricPapers: "去重论文", metricPapersNote: "当前工作目录", metricCleared: "许可证可改编", metricClearedNote: "仍须遵守署名等条件",
    metricPending: "需要额外授权", metricPendingNote: "未获许可前不公开全文", metricPages: "论文总页数", metricPagesNote: "PDF 物理页数",
    frontierBrowse: "按 Snowmass 官方学科浏览", workflowAria: "工作流", workflowSource: "来源核验", workflowMachine: "机器初译",
    workflowTerms: "术语统一", workflowReview: "人工审校", workflowRights: "权利闸门", workflowPublish: "公开发布",
    catalogTitle: "论文、翻译与授权状态", loadingCatalog: "正在加载目录…", filterSearch: "检索", searchPlaceholder: "题名、作者、arXiv、专题…",
    filterFrontier: "Frontier", allFrontiers: "全部 Frontier", filterLicense: "来源许可证", allLicenses: "全部许可证",
    filterAuthorization: "授权状态", allAuthorization: "全部授权状态", filterTranslation: "翻译状态", allTranslation: "全部翻译状态",
    filterPublication: "发布资格", allPublication: "全部", filterSort: "排序", sortTitle: "题名字母顺序", sortCitations: "引用数从高到低",
    sortImpact: "影响力代理从高到低", sortPages: "篇幅从长到短", sortYear: "年份从新到旧", resetFilters: "清除筛选",
    authLicenseCleared: "许可证已核验", authNeedsPermission: "需要额外授权", authContacted: "已联系", authResponsePending: "等待回复",
    authPermissionGranted: "已获书面授权", authPermissionDenied: "未获授权", translationNotStarted: "尚未开始", translationMachineDraft: "机器初译",
    translationHumanReview: "人工审校", translationPublished: "已公开", publicationAllowed: "具备发布基础", publicationBlocked: "暂不可公开全文",
    footerDisclaimer: "非官方社区项目，不代表 Snowmass、SLAC、arXiv、HAL、作者或出版机构。", footerOfficial: "官方提交索引",
    footerArxiv: "arXiv 翻译政策", footerLicense: "项目代码与自有文档采用 AGPL-3.0；第三方论文和每篇译文分别遵循其记录的许可证与书面授权。",
    noscript: "本目录需要 JavaScript 才能加载论文筛选与详情。", themeDark: "深色", themeLight: "浅色", languageAria: "切换为英文",
    themeAriaDark: "切换为深色模式", themeAriaLight: "切换为浅色模式", yearUnknown: "年份未知", authorUnknown: "作者信息待核验",
    unclassified: "未分类", pages: "页", citations: "引用", source: "查看原文 ↗", details: "权利与进度详情 →",
    results: (count, page, total) => `${count} 篇符合条件 · 第 ${page}/${total} 页`, noResults: "没有找到符合条件的论文",
    noResultsHint: "可以缩短关键词，或清除一项许可证、Frontier、授权或翻译筛选。", previous: "上一页", next: "下一页",
    back: "← 返回筛选结果", currentStatus: "当前状态", clearedExplanation: "来源许可允许制作改编作品；公开译文仍须完整落实署名、变更说明、相同方式共享或非商业条件。",
    blockedExplanation: "当前记录没有足够的公开改编授权。机器草稿可以内部保存，但不能作为完整译文公开。",
    sourceRights: "来源与权利", sourceLicense: "来源许可证", permitsAdaptation: "允许改编", rightsDecision: "权利处置", publicationBasis: "发布依据",
    sourceVersion: "来源版本", lastChecked: "最后核验", yesConditions: "是（遵守许可证条件）", noPermission: "否（须另行授权）", unconfirmed: "未确认",
    awaitingReview: "等待人工核验", none: "无", versionUnknown: "仓储记录未提供", unknown: "未知", translationReview: "翻译与审校",
    translationStatus: "翻译状态", translationLicense: "译文许可证", machineModel: "机器模型", titleTranslationStatus: "中文译题状态", titleTranslationModel: "中文译题模型", humanReviewers: "人工审校者", publicTranslation: "公开译文",
    notSpecified: "尚未指定", noMachineDraft: "尚未生成机器草稿", noReviewers: "尚无公开署名审校者", viewTranslation: "查看译文", notPublished: "尚未公开",
    publicConditions: "公开条件", noExtraConditions: "当前来源许可证没有额外列出的发布条件。", topicsPending: "专题信息待补充。",
    metricsImpact: "论文体量与影响力快照", year: "年份", pdfPages: "PDF 页数", tokenProxy: "文本 token 代理", inspireCitations: "INSPIRE 引用",
    citationsNoSelf: "去自引引用", citationsAnnual: "年化引用", impactProxy: "影响力代理", frontierTopics: "Frontier 与专题", action: "行动",
    allowedAction: "这篇论文可进入机器初译与社区审校流程。提交译文前，请核对译文许可证是否兼容，并保留完整署名。",
    blockedAction: "如果希望翻译这篇论文，请先由授权工作流联系通讯作者或权利人，并保存范围明确的书面回复。", github: "前往 GitHub",
    dataLoadFailed: "目录加载失败", cannotLoad: "无法读取公开目录", cannotLoadHint: "请刷新页面；如果问题持续存在，请到 GitHub 提交 issue。",
    metaDescription: "Snowmass 2021 白皮书中文翻译、来源许可证与授权进度的开放目录。", ogDescription: "Snowmass 2021 白皮书的非官方中文翻译、来源许可与授权进度目录。",
  },
  en: {
    skipCatalog: "Skip to paper catalog", brandHome: "Snowmass Open Translation Atlas home", brandName: "Snowmass Open Translation Atlas",
    primaryNav: "Primary navigation", navCatalog: "Paper catalog", navProgress: "Progress", navContributors: "Contributors", navGuide: "Guide",
    navWorkflow: "Workflow", navRights: "Rights protocol ↗", navContribute: "Contribute on GitHub",
    heroTitle: "Cross the language snowline.<br>Reach open knowledge.", contactLabel: "Project contact: ",
    heroLede: "An open Chinese-language archive that starts with machine translation and improves through sustained community review. Every paper records its source license, permission status, model, and human revisions; full translations are never published without clear adaptation rights.",
    heroExplore: "Explore the paper atlas", heroRights: "Read the rights protocol", gateAria: "Publication gate", gateLabel: "PUBLICATION GATE · RIGHTS FIRST",
    gateText: "A full translation is published only when the source license permits adaptation or the rightsholder grants clear written permission.",
    signalCleared: "License cleared", signalPending: "Permission pending", signalBlocked: "Not public", metricsAria: "Project statistics",
    metricPapers: "Unique papers", metricPapersNote: "Current working catalog", metricCleared: "Adaptation permitted", metricClearedNote: "Attribution and other terms still apply",
    metricPending: "Need permission", metricPendingNote: "No full text before permission", metricPages: "Total paper pages", metricPagesNote: "Physical PDF pages",
    frontierBrowse: "Browse the official Snowmass Frontiers", workflowAria: "Translation workflow", workflowSource: "Source review", workflowMachine: "Machine draft",
    workflowTerms: "Terminology", workflowReview: "Human review", workflowRights: "Rights gate", workflowPublish: "Publication",
    catalogTitle: "Papers, translations, and rights status", loadingCatalog: "Loading catalog…", filterSearch: "Search", searchPlaceholder: "Title, author, arXiv, topic…",
    filterFrontier: "Frontier", allFrontiers: "All Frontiers", filterLicense: "Source license", allLicenses: "All licenses",
    filterAuthorization: "Permission status", allAuthorization: "All permission states", filterTranslation: "Translation status", allTranslation: "All translation states",
    filterPublication: "Publication basis", allPublication: "All", filterSort: "Sort", sortTitle: "Title A–Z", sortCitations: "Most cited",
    sortImpact: "Highest impact proxy", sortPages: "Longest first", sortYear: "Newest first", resetFilters: "Clear filters",
    authLicenseCleared: "License cleared", authNeedsPermission: "Additional permission needed", authContacted: "Contacted", authResponsePending: "Awaiting response",
    authPermissionGranted: "Written permission granted", authPermissionDenied: "Permission denied", translationNotStarted: "Not started", translationMachineDraft: "Machine draft",
    translationHumanReview: "Human review", translationPublished: "Published", publicationAllowed: "Publication basis present", publicationBlocked: "Full text not public",
    footerDisclaimer: "An unofficial community project. It does not represent Snowmass, SLAC, arXiv, HAL, authors, or publishers.", footerOfficial: "Official submission index",
    footerArxiv: "arXiv translation policy", footerLicense: "Project code and original documentation use AGPL-3.0. Third-party papers and each translation follow their own recorded licenses and written permissions.",
    noscript: "JavaScript is required to filter and inspect the paper catalog.", themeDark: "Dark", themeLight: "Light", languageAria: "Switch to Chinese",
    themeAriaDark: "Switch to dark mode", themeAriaLight: "Switch to light mode", yearUnknown: "Year unknown", authorUnknown: "Author information pending review",
    unclassified: "Unclassified", pages: "pages", citations: "citations", source: "View source ↗", details: "Rights and progress →",
    results: (count, page, total) => `${count} results · page ${page}/${total}`, noResults: "No papers match these filters",
    noResultsHint: "Try a shorter query or clear a license, Frontier, permission, or translation filter.", previous: "Previous page", next: "Next page",
    back: "← Back to results", currentStatus: "Current status", clearedExplanation: "The source license permits adaptations. Any public translation must still satisfy attribution, change notices, ShareAlike, or non-commercial conditions.",
    blockedExplanation: "This record does not currently show sufficient public adaptation rights. A machine draft may be stored privately but cannot be published as a full translation.",
    sourceRights: "Source and rights", sourceLicense: "Source license", permitsAdaptation: "Adaptation permitted", rightsDecision: "Rights decision", publicationBasis: "Publication basis",
    sourceVersion: "Source version", lastChecked: "Last checked", yesConditions: "Yes (subject to license terms)", noPermission: "No (separate permission required)", unconfirmed: "Unconfirmed",
    awaitingReview: "Awaiting manual review", none: "None", versionUnknown: "Not supplied by repository", unknown: "Unknown", translationReview: "Translation and review",
    translationStatus: "Translation status", translationLicense: "Translation license", machineModel: "Machine model", titleTranslationStatus: "Chinese title status", titleTranslationModel: "Chinese title model", humanReviewers: "Human reviewers", publicTranslation: "Public translation",
    notSpecified: "Not specified", noMachineDraft: "No machine draft yet", noReviewers: "No publicly credited reviewers", viewTranslation: "View translation", notPublished: "Not published",
    publicConditions: "Publication conditions", noExtraConditions: "No additional publication conditions are listed for this source license.", topicsPending: "Topic information pending.",
    metricsImpact: "Length and impact snapshot", year: "Year", pdfPages: "PDF pages", tokenProxy: "Text token proxy", inspireCitations: "INSPIRE citations",
    citationsNoSelf: "Citations excluding self-citations", citationsAnnual: "Citations per year", impactProxy: "Impact proxy", frontierTopics: "Frontiers and topics", action: "Action",
    allowedAction: "This paper may enter machine translation and community review. Before contributing, verify license compatibility and preserve complete attribution.",
    blockedAction: "To translate this paper, the permission workflow must first contact the corresponding author or rightsholder and retain a clear written response.", github: "Open GitHub",
    dataLoadFailed: "Catalog failed to load", cannotLoad: "Unable to read the public catalog", cannotLoadHint: "Refresh the page. If the problem continues, open an issue on GitHub.",
    metaDescription: "An open catalog of Snowmass 2021 paper translations, source licenses, and permission progress.", ogDescription: "An unofficial bilingual translation and rights-status catalog for Snowmass 2021 white papers.",
  },
};

const FRONTIER_LABELS = {
  AF: ["加速器前沿", "Accelerator Frontier"], CEF: ["社群参与前沿", "Community Engagement Frontier"], CompF: ["计算前沿", "Computational Frontier"],
  CF: ["宇宙前沿", "Cosmic Frontier"], EF: ["能量前沿", "Energy Frontier"], IF: ["仪器学前沿", "Instrumentation Frontier"],
  NF: ["中微子前沿", "Neutrino Frontier"], RPF: ["稀有过程与精密测量前沿", "Rare Processes and Precision Measurements Frontier"],
  TF: ["理论前沿", "Theory Frontier"], UF: ["地下设施与基础设施前沿", "Underground Facilities and Infrastructure Frontier"],
};

const STATUS = {
  authorization: {
    "not-reviewed": ["尚未核验", "Not reviewed"], "license-cleared": ["许可证已核验", "License cleared"], "needs-permission": ["需要额外授权", "Additional permission needed"],
    contacted: ["已联系", "Contacted"], "response-pending": ["等待回复", "Awaiting response"], "permission-granted": ["已获书面授权", "Written permission granted"],
    "permission-denied": ["未获授权", "Permission denied"], unclear: ["回复范围不明确", "Response scope unclear"], withdrawn: ["已撤回", "Withdrawn"],
  },
  translation: {
    "not-started": ["尚未开始", "Not started"], "machine-draft": ["机器初译", "Machine draft"], "human-review": ["人工审校", "Human review"],
    published: ["已公开", "Published"], superseded: ["已被新版替代", "Superseded"], withdrawn: ["已撤回", "Withdrawn"],
  },
};

const CONDITIONS = {
  attribution: ["保留原作者、题名、来源和许可证署名", "Credit the authors, title, source, and license"],
  "indicate-changes": ["明确标注翻译和后续修改", "Identify the translation and later changes"],
  "share-alike": ["译文采用兼容的相同方式共享许可证", "Use a compatible ShareAlike license"],
  "non-commercial": ["仅限非商业使用", "Non-commercial use only"],
  "written-rightsholder-permission-required": ["发布完整译文前取得权利人的范围明确书面许可", "Obtain clear written rightsholder permission before publishing a full translation"],
  "no-public-adaptation-under-current-source-license": ["当前来源许可证禁止公开演绎作品，需要另行取得权利人许可", "The current source license does not permit public adaptations; separate permission is required"],
  "HAL-distribution-authorization-is-not-adaptation-permission": ["HAL 的分发授权不等于第三方翻译或改编许可", "HAL distribution authorization is not third-party adaptation permission"],
  "verify-current-rightsholder-and-license-before-publication": ["发布前核验当前权利人并取得翻译与公开传播许可", "Verify the current rightsholder and obtain translation and publication permission"],
};

const elements = {
  catalog: document.querySelector("#catalog"), detail: document.querySelector("#detail-panel"), grid: document.querySelector("#paper-grid"),
  pagination: document.querySelector("#pagination"), resultCount: document.querySelector("#result-count"), form: document.querySelector("#filters"),
  search: document.querySelector("#search"), frontier: document.querySelector("#frontier"), license: document.querySelector("#license"),
  authorization: document.querySelector("#authorization"), translation: document.querySelector("#translation"), publication: document.querySelector("#publication"),
  sort: document.querySelector("#sort"), reset: document.querySelector("#reset-filters"), languageToggle: document.querySelector("#language-toggle"),
  themeToggle: document.querySelector("#theme-toggle"), themeLabel: document.querySelector("#theme-label"),
};

function preferredLanguage() {
  const requested = new URLSearchParams(location.search).get("lang");
  if (["zh", "en"].includes(requested)) return requested;
  const saved = localStorage.getItem(LANG_KEY);
  if (["zh", "en"].includes(saved)) return saved;
  return navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en";
}

const state = { papers: [], stats: null, page: 1, lang: preferredLanguage(), theme: document.documentElement.dataset.theme || "light", currentPaper: null };
const t = (key) => UI[state.lang][key];
const localized = (map, key) => map[key]?.[state.lang === "zh" ? 0 : 1] || key;

function escapeHTML(value) { return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;"); }
function safeURL(value) { try { const url = new URL(value, location.href); return ["http:", "https:"].includes(url.protocol) ? url.href : "#"; } catch { return "#"; } }
function numberFormat(value, digits = 0) { return Number.isFinite(value) ? new Intl.NumberFormat(state.lang === "zh" ? "zh-CN" : "en-US", { maximumFractionDigits: digits }).format(value) : "—"; }
function formatFrontiers(frontiers, separator) { return frontiers.map((code) => escapeHTML(localized(FRONTIER_LABELS, code))).join(separator); }
function statusClass(status) { if (["license-cleared", "permission-granted", "published"].includes(status)) return "badge-cleared"; if (["needs-permission", "contacted", "response-pending", "unclear", "machine-draft", "human-review"].includes(status)) return "badge-pending"; if (["permission-denied", "withdrawn"].includes(status)) return "badge-blocked"; return ""; }

function applyStaticTranslations() {
  document.documentElement.lang = state.lang === "zh" ? "zh-CN" : "en";
  document.title = state.lang === "zh" ? "Snowmass 中文翻译计划" : "Snowmass Open Translation Atlas";
  document.querySelector('meta[name="description"]').content = t("metaDescription");
  document.querySelector('meta[property="og:title"]').content = document.title;
  document.querySelector('meta[property="og:description"]').content = t("ogDescription");
  document.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); });
  document.querySelectorAll("[data-i18n-html]").forEach((node) => { node.innerHTML = t(node.dataset.i18nHtml); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => { node.placeholder = t(node.dataset.i18nPlaceholder); });
  document.querySelectorAll("[data-i18n-aria]").forEach((node) => { node.setAttribute("aria-label", t(node.dataset.i18nAria)); });
  document.querySelectorAll(".frontier-nav-grid a").forEach((link) => {
    const code = link.querySelector("b")?.textContent;
    const label = link.querySelector("span");
    if (code && label) label.textContent = localized(FRONTIER_LABELS, code);
  });
  elements.languageToggle.textContent = state.lang === "zh" ? "EN" : "中文";
  elements.languageToggle.setAttribute("aria-label", t("languageAria"));
  elements.languageToggle.setAttribute("aria-pressed", String(state.lang === "en"));
  elements.themeLabel.textContent = state.theme === "light" ? t("themeDark") : t("themeLight");
  elements.themeToggle.setAttribute("aria-label", state.theme === "light" ? t("themeAriaDark") : t("themeAriaLight"));
  elements.themeToggle.setAttribute("aria-pressed", String(state.theme === "dark"));
}

function setTheme(theme, persist = true) {
  state.theme = theme;
  document.documentElement.dataset.theme = theme;
  if (persist) localStorage.setItem(THEME_KEY, theme);
  applyStaticTranslations();
}

function setLanguage(lang, persist = true) {
  state.lang = lang;
  if (persist) localStorage.setItem(LANG_KEY, lang);
  applyStaticTranslations();
  rebuildDynamicSelects();
  if (state.stats) renderMetrics();
  if (state.currentPaper) renderDetail(state.currentPaper);
  else if (state.papers.length) renderCatalog();
  else writeURLState();
}

function rebuildDynamicSelects() {
  if (!state.papers.length) return;
  const frontierValue = elements.frontier.value;
  const licenseValue = elements.license.value;
  elements.frontier.querySelectorAll("option[data-generated]").forEach((node) => node.remove());
  elements.license.querySelectorAll("option[data-generated]").forEach((node) => node.remove());
  [...new Set(state.papers.flatMap((paper) => paper.frontiers))].sort().forEach((value) => {
    const option = document.createElement("option"); option.dataset.generated = "true"; option.value = value; option.textContent = `${value} · ${localized(FRONTIER_LABELS, value)}`; elements.frontier.append(option);
  });
  [...new Set(state.papers.map((paper) => paper.source_license).filter(Boolean))].sort().forEach((value) => {
    const option = document.createElement("option"); option.dataset.generated = "true"; option.value = value; option.textContent = value; elements.license.append(option);
  });
  elements.frontier.value = frontierValue; elements.license.value = licenseValue;
}

function readURLState() {
  const params = new URLSearchParams(location.search);
  elements.search.value = params.get("q") || ""; elements.frontier.value = params.get("frontier") || ""; elements.license.value = params.get("license") || "";
  elements.authorization.value = params.get("authorization") || ""; elements.translation.value = params.get("translation") || "";
  elements.publication.value = params.get("publication") || ""; elements.sort.value = params.get("sort") || "title";
  state.page = Math.max(1, Number.parseInt(params.get("page") || "1", 10) || 1);
}

function writeURLState({ push = false, paper = state.currentPaper } = {}) {
  const params = new URLSearchParams();
  params.set("lang", state.lang);
  const values = { q: elements.search.value.trim(), frontier: elements.frontier.value, license: elements.license.value, authorization: elements.authorization.value,
    translation: elements.translation.value, publication: elements.publication.value, sort: elements.sort.value === "title" ? "" : elements.sort.value, page: state.page > 1 ? String(state.page) : "" };
  Object.entries(values).forEach(([key, value]) => { if (value) params.set(key, value); });
  if (paper) params.set("paper", paper);
  history[push ? "pushState" : "replaceState"]({}, "", `${location.pathname}?${params}${location.hash}`);
}

function renderMetrics() {
  document.querySelector("#stat-catalog").textContent = numberFormat(state.stats.catalog_count);
  document.querySelector("#stat-cleared").textContent = numberFormat(state.stats.authorization_counts["license-cleared"] || 0);
  document.querySelector("#stat-permission").textContent = numberFormat(state.stats.authorization_counts["needs-permission"] || 0);
  document.querySelector("#stat-pages").textContent = numberFormat(state.stats.page_summary.total);
}

function paperSearchText(paper) { return [paper.paper_id, paper.record_id, paper.title, paper.title_zh, paper.authors_as_listed, ...(paper.frontiers || []), ...(paper.frontier_labels || []), ...(paper.topics || []), paper.primary_arxiv_category].join(" ").toLocaleLowerCase("zh-CN"); }
function filteredPapers() {
  const query = elements.search.value.trim().toLocaleLowerCase("zh-CN");
  const papers = state.papers.filter((paper) => (!query || paperSearchText(paper).includes(query)) && (!elements.frontier.value || paper.frontiers.includes(elements.frontier.value)) &&
    (!elements.license.value || paper.source_license === elements.license.value) && (!elements.authorization.value || paper.authorization_status === elements.authorization.value) &&
    (!elements.translation.value || paper.translation_status === elements.translation.value) && (elements.publication.value !== "allowed" || paper.publication_allowed) &&
    (elements.publication.value !== "blocked" || !paper.publication_allowed));
  const title = (a, b) => a.title.localeCompare(b.title, "en", { sensitivity: "base" });
  const descending = (field) => (a, b) => (Number.isFinite(b[field]) ? b[field] : -Infinity) - (Number.isFinite(a[field]) ? a[field] : -Infinity) || title(a, b);
  return papers.sort({ title, citations: descending("citation_count"), impact: descending("impact_proxy_score_0_100"), pages: descending("page_count"), year: descending("publication_year") }[elements.sort.value] || title);
}

function bilingualTitle(paper, context = "card") {
  const chinese = state.lang === "zh" ? `<p class="${context === "detail" ? "detail-title-zh" : "paper-title-zh"}">${escapeHTML(paper.title_zh)}</p>` : "";
  return `${context === "detail" ? `<h1 id="detail-title">${escapeHTML(paper.title)}</h1>` : `<h3>${escapeHTML(paper.title)}</h3>`}${chinese}`;
}

function renderPaperCard(paper) {
  const auth = localized(STATUS.authorization, paper.authorization_status); const translation = localized(STATUS.translation, paper.translation_status);
  const publication = paper.publication_allowed ? t("publicationAllowed") : t("publicationBlocked"); const publicationClass = paper.publication_allowed ? "badge-cleared" : "badge-blocked";
  return `<article class="paper-card"><div class="paper-card-top"><span class="paper-id">${escapeHTML(paper.paper_id)}</span><span class="paper-year">${escapeHTML(paper.publication_year || t("yearUnknown"))}</span></div>
    ${bilingualTitle(paper)}<p class="paper-authors">${escapeHTML(paper.authors_as_listed || t("authorUnknown"))}</p><div class="badges" aria-label="${escapeHTML(t("currentStatus"))}">
    <span class="badge ${statusClass(paper.authorization_status)}">${escapeHTML(auth)}</span><span class="badge ${statusClass(paper.translation_status)}">${escapeHTML(translation)}</span><span class="badge ${publicationClass}">${escapeHTML(publication)}</span></div>
    <div class="paper-meta"><span>${formatFrontiers(paper.frontiers, " · ") || t("unclassified")}</span><span>${numberFormat(paper.page_count)} ${t("pages")}</span><span>${numberFormat(paper.citation_count)} ${t("citations")}</span></div>
    <div class="paper-card-footer"><a href="${safeURL(paper.source_url)}" target="_blank" rel="noreferrer">${t("source")}</a><button class="paper-detail-button" type="button" data-paper="${escapeHTML(paper.record_id)}">${t("details")}</button></div></article>`;
}

function renderPagination(totalPages) {
  if (totalPages <= 1) { elements.pagination.innerHTML = ""; return; }
  const current = state.page; const candidates = new Set([1, totalPages, current - 2, current - 1, current, current + 1, current + 2]);
  const pages = [...candidates].filter((page) => page >= 1 && page <= totalPages).sort((a, b) => a - b); let previous = 0;
  const buttons = [`<button type="button" data-page="${current - 1}" ${current === 1 ? "disabled" : ""} aria-label="${t("previous")}">←</button>`];
  pages.forEach((page) => { if (previous && page - previous > 1) buttons.push('<span aria-hidden="true">…</span>'); buttons.push(`<button type="button" data-page="${page}" ${page === current ? 'aria-current="page"' : ""}>${page}</button>`); previous = page; });
  buttons.push(`<button type="button" data-page="${current + 1}" ${current === totalPages ? "disabled" : ""} aria-label="${t("next")}">→</button>`); elements.pagination.innerHTML = buttons.join("");
}

function renderCatalog({ preserveScroll = true } = {}) {
  state.currentPaper = null; const papers = filteredPapers(); const totalPages = Math.max(1, Math.ceil(papers.length / PAGE_SIZE)); state.page = Math.min(state.page, totalPages);
  const visible = papers.slice((state.page - 1) * PAGE_SIZE, state.page * PAGE_SIZE); elements.detail.hidden = true; elements.catalog.hidden = false;
  elements.resultCount.textContent = t("results")(numberFormat(papers.length), state.page, totalPages);
  elements.grid.innerHTML = visible.length ? visible.map(renderPaperCard).join("") : `<div class="empty-state"><strong>${t("noResults")}</strong><p>${t("noResultsHint")}</p></div>`;
  renderPagination(totalPages); writeURLState(); if (!preserveScroll) elements.catalog.scrollIntoView({ behavior: "smooth", block: "start" });
}

function definitionRow(term, value) { return `<dt>${escapeHTML(term)}</dt><dd>${value}</dd>`; }
function renderDetail(recordId, { push = false } = {}) {
  const paper = state.papers.find((entry) => entry.record_id === recordId); if (!paper) { state.currentPaper = null; renderCatalog(); return; }
  state.currentPaper = paper.record_id; const auth = localized(STATUS.authorization, paper.authorization_status); const translation = localized(STATUS.translation, paper.translation_status);
  const conditions = paper.publication_conditions.length ? paper.publication_conditions.map((item) => `<li>${escapeHTML(localized(CONDITIONS, item))}</li>`).join("") : `<li>${t("noExtraConditions")}</li>`;
  const topics = paper.topics.length ? paper.topics.map((topic) => `<li>${escapeHTML(topic)}</li>`).join("") : `<li>${t("topicsPending")}</li>`;
  const reviewers = paper.human_reviewers.length ? paper.human_reviewers.map(escapeHTML).join(state.lang === "zh" ? "、" : ", ") : t("noReviewers");
  const adaptation = paper.permits_adaptation === true ? t("yesConditions") : paper.permits_adaptation === false ? t("noPermission") : t("unconfirmed");
  elements.detail.innerHTML = `<button type="button" class="detail-back" id="detail-back">${t("back")}</button><div class="detail-header"><div><p class="eyebrow">${escapeHTML(paper.paper_id)} · ${formatFrontiers(paper.frontiers, " / ")}</p>
    ${bilingualTitle(paper, "detail")}<p class="detail-authors">${escapeHTML(paper.authors_as_listed || t("authorUnknown"))}</p></div><aside class="detail-status" aria-label="${t("currentStatus")}"><p class="principle-index">${t("currentStatus")}</p><div class="badges">
    <span class="badge ${statusClass(paper.authorization_status)}">${escapeHTML(auth)}</span><span class="badge ${statusClass(paper.translation_status)}">${escapeHTML(translation)}</span><span class="badge ${paper.publication_allowed ? "badge-cleared" : "badge-blocked"}">${paper.publication_allowed ? t("publicationAllowed") : t("publicationBlocked")}</span></div><p>${paper.publication_allowed ? t("clearedExplanation") : t("blockedExplanation")}</p></aside></div>
    <div class="detail-grid"><section class="detail-block"><h2>${t("sourceRights")}</h2><dl class="definition-list">
    ${definitionRow(t("sourceLicense"), `<a href="${safeURL(paper.source_license_url)}" target="_blank" rel="noreferrer">${escapeHTML(paper.source_license || t("unknown"))} ↗</a>`)}${definitionRow(t("permitsAdaptation"), adaptation)}
    ${definitionRow(t("rightsDecision"), escapeHTML(paper.license_decision || t("awaitingReview")))}${definitionRow(t("publicationBasis"), escapeHTML(paper.publication_basis || t("none")))}
    ${definitionRow(t("sourceVersion"), escapeHTML(paper.source_version || t("versionUnknown")))}${definitionRow(t("lastChecked"), escapeHTML(paper.public_updated_at ? paper.public_updated_at.slice(0, 10) : t("unknown")))}</dl></section>
    <section class="detail-block"><h2>${t("translationReview")}</h2><dl class="definition-list">${definitionRow(t("translationStatus"), escapeHTML(translation))}${definitionRow(t("translationLicense"), escapeHTML(paper.translation_license || t("notSpecified")))}
    ${definitionRow(t("machineModel"), escapeHTML(paper.machine_model || t("noMachineDraft")))}${definitionRow(t("titleTranslationStatus"), escapeHTML(localized(STATUS.translation, paper.title_zh_status)))}${definitionRow(t("titleTranslationModel"), escapeHTML(paper.title_zh_model))}${definitionRow(t("humanReviewers"), reviewers)}${definitionRow(t("publicTranslation"), paper.publication_translation_url ? `<a href="${safeURL(paper.publication_translation_url)}">${t("viewTranslation")}</a>` : t("notPublished"))}</dl></section>
    <section class="detail-block"><h2>${t("publicConditions")}</h2><ul class="condition-list">${conditions}</ul></section><section class="detail-block"><h2>${t("metricsImpact")}</h2><dl class="definition-list">
    ${definitionRow(t("year"), escapeHTML(paper.publication_year || t("unknown")))}${definitionRow(t("pdfPages"), numberFormat(paper.page_count))}${definitionRow(t("tokenProxy"), numberFormat(paper.unicode_token_count))}
    ${definitionRow(t("inspireCitations"), numberFormat(paper.citation_count))}${definitionRow(t("citationsNoSelf"), numberFormat(paper.citation_count_without_self_citations))}${definitionRow(t("citationsAnnual"), numberFormat(paper.citations_per_year, 1))}
    ${definitionRow(t("impactProxy"), paper.impact_proxy_score_0_100 == null ? "—" : `${numberFormat(paper.impact_proxy_score_0_100, 1)} / 100`)}</dl></section>
    <section class="detail-block"><h2>${t("frontierTopics")}</h2><ul class="topic-list">${topics}</ul></section><section class="detail-block"><h2>${t("action")}</h2><p>${paper.publication_allowed ? t("allowedAction") : t("blockedAction")}</p>
    <div class="detail-actions"><a class="button button-primary" href="${safeURL(paper.source_url)}" target="_blank" rel="noreferrer">${t("source")}</a><a class="button button-secondary" href="https://github.com/JunkaiWang-TheoPhy/Snowmass-Physics-CN" target="_blank" rel="noreferrer">${t("github")}</a></div></section></div>`;
  elements.catalog.hidden = true; elements.detail.hidden = false; writeURLState({ push, paper: paper.record_id }); if (push) scrollTo({ top: 0, behavior: "smooth" });
}

function handleFilterChange() { state.page = 1; renderCatalog(); }
async function initialize() {
  applyStaticTranslations();
  try {
    const [papersResponse, statsResponse] = await Promise.all([fetch("data/papers.json"), fetch("data/stats.json")]); if (!papersResponse.ok || !statsResponse.ok) throw new Error("public data request failed");
    [state.papers, state.stats] = await Promise.all([papersResponse.json(), statsResponse.json()]); rebuildDynamicSelects(); readURLState(); renderMetrics();
    const recordId = new URLSearchParams(location.search).get("paper"); if (recordId) renderDetail(recordId); else renderCatalog();
  } catch (error) { console.error(error); elements.resultCount.textContent = t("dataLoadFailed"); elements.grid.innerHTML = `<div class="error-state"><strong>${t("cannotLoad")}</strong><p>${t("cannotLoadHint")}</p></div>`; }
}

elements.languageToggle.addEventListener("click", () => setLanguage(state.lang === "zh" ? "en" : "zh"));
elements.themeToggle.addEventListener("click", () => setTheme(state.theme === "light" ? "dark" : "light"));
elements.form.addEventListener("input", handleFilterChange); elements.form.addEventListener("change", handleFilterChange);
elements.form.addEventListener("reset", () => setTimeout(() => { state.page = 1; renderCatalog(); }, 0));
elements.grid.addEventListener("click", (event) => { const button = event.target.closest("[data-paper]"); if (button) renderDetail(button.dataset.paper, { push: true }); });
elements.pagination.addEventListener("click", (event) => { const button = event.target.closest("[data-page]"); if (!button || button.disabled) return; state.page = Number(button.dataset.page); renderCatalog({ preserveScroll: false }); });
elements.detail.addEventListener("click", (event) => { if (event.target.closest("#detail-back")) { state.currentPaper = null; renderCatalog(); elements.catalog.scrollIntoView({ block: "start" }); } });
addEventListener("popstate", () => { const params = new URLSearchParams(location.search); const lang = params.get("lang"); if (["zh", "en"].includes(lang)) state.lang = lang; applyStaticTranslations(); readURLState(); const recordId = params.get("paper"); if (recordId) renderDetail(recordId); else renderCatalog(); });
initialize();
