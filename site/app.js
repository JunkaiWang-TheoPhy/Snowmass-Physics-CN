const PAGE_SIZE = 24;

const FRONTIER_LABELS = {
  AF: "加速器科学与技术",
  CEF: "社群参与",
  CompF: "计算前沿",
  CF: "宇宙前沿",
  EF: "能量前沿",
  IF: "仪器学前沿",
  NF: "中微子前沿",
  RPF: "稀有过程与精密测量",
  TF: "理论前沿",
  UF: "地下设施与基础设施",
};

const AUTHORIZATION_LABELS = {
  "not-reviewed": "尚未核验",
  "license-cleared": "许可证已核验",
  "needs-permission": "需要额外授权",
  contacted: "已联系",
  "response-pending": "等待回复",
  "permission-granted": "已获书面授权",
  "permission-denied": "未获授权",
  unclear: "回复范围不明确",
  withdrawn: "已撤回",
};

const TRANSLATION_LABELS = {
  "not-started": "尚未开始",
  "machine-draft": "机器初译",
  "human-review": "人工审校",
  published: "已公开",
  superseded: "已被新版替代",
  withdrawn: "已撤回",
};

const CONDITION_LABELS = {
  attribution: "保留原作者、题名、来源和许可证署名",
  "indicate-changes": "明确标注翻译和后续修改",
  "share-alike": "译文采用兼容的相同方式共享许可证",
  "non-commercial": "仅限非商业使用",
  "written-rightsholder-permission-required": "发布完整译文前取得权利人的范围明确书面许可",
  "no-public-adaptation-under-current-source-license": "当前来源许可证禁止公开演绎作品，需要另行取得权利人许可",
  "HAL-distribution-authorization-is-not-adaptation-permission": "HAL 的分发授权不等于第三方翻译或改编许可",
  "verify-current-rightsholder-and-license-before-publication": "发布前核验当前权利人并取得翻译与公开传播许可",
};

const numberFormatter = new Intl.NumberFormat("zh-CN");
const decimalFormatter = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });

const elements = {
  catalog: document.querySelector("#catalog"),
  detail: document.querySelector("#detail-panel"),
  grid: document.querySelector("#paper-grid"),
  pagination: document.querySelector("#pagination"),
  resultCount: document.querySelector("#result-count"),
  form: document.querySelector("#filters"),
  search: document.querySelector("#search"),
  frontier: document.querySelector("#frontier"),
  license: document.querySelector("#license"),
  authorization: document.querySelector("#authorization"),
  translation: document.querySelector("#translation"),
  publication: document.querySelector("#publication"),
  sort: document.querySelector("#sort"),
  reset: document.querySelector("#reset-filters"),
};

const state = {
  papers: [],
  stats: null,
  page: 1,
};

const SITE_ORIGIN = "https://snowmass-physics-cn.netlify.app";

function paperPath(recordId) {
  const value = String(recordId || "");
  if (value.toLowerCase().startsWith("arxiv:")) {
    return `/paper/${encodeURIComponent(value.slice(6))}/`;
  }
  const cds = value.match(/cds\.cern\.ch\/record\/(\d+)/);
  if (cds) return `/paper/cds-${cds[1]}/`;
  const hal = value.match(/hal-(\d+)/);
  if (hal) return `/paper/hal-${hal[1]}/`;
  return `/paper/${encodeURIComponent(value)}/`;
}

function recordIdFromLocation(location) {
  const match = location.pathname.match(/^\/paper\/([^/]+)\/?$/);
  if (match) {
    const slug = decodeURIComponent(match[1]);
    if (slug.startsWith("cds-")) return `external:https://cds.cern.ch/record/${slug.slice(4)}`;
    if (slug.startsWith("hal-")) return `external:https://hal.archives-ouvertes.fr/hal-${slug.slice(4)}`;
    return `arxiv:${slug}`;
  }
  return new URLSearchParams(location.search).get("paper");
}

function setCanonical(path = "/") {
  const canonical = document.querySelector('link[rel="canonical"]');
  if (canonical) canonical.href = `${SITE_ORIGIN}${path}`;
}

function escapeHTML(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safeURL(value) {
  try {
    const url = new URL(value, window.location.href);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "#";
  } catch {
    return "#";
  }
}

function formatNumber(value, empty = "—") {
  return Number.isFinite(value) ? numberFormatter.format(value) : empty;
}

function formatDecimal(value, empty = "—") {
  return Number.isFinite(value) ? decimalFormatter.format(value) : empty;
}

function formatBytes(value, empty = "—") {
  if (!Number.isFinite(value)) return empty;
  return `${decimalFormatter.format(value / 1_000_000)} MB`;
}

function statusClass(status) {
  if (["license-cleared", "permission-granted", "published"].includes(status)) return "badge-cleared";
  if (["needs-permission", "contacted", "response-pending", "unclear", "machine-draft", "human-review"].includes(status)) return "badge-pending";
  if (["permission-denied", "withdrawn"].includes(status)) return "badge-blocked";
  return "";
}

function fillSelect(select, values, labelForValue) {
  const selected = select.value;
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = labelForValue(value);
    select.append(option);
  }
  select.value = selected;
}

function readURLState() {
  const params = new URLSearchParams(window.location.search);
  elements.search.value = params.get("q") || "";
  elements.frontier.value = params.get("frontier") || "";
  elements.license.value = params.get("license") || "";
  elements.authorization.value = params.get("authorization") || "";
  elements.translation.value = params.get("translation") || "";
  elements.publication.value = params.get("publication") || "";
  elements.sort.value = params.get("sort") || "title";
  state.page = Math.max(1, Number.parseInt(params.get("page") || "1", 10) || 1);
}

function writeURLState({ push = false } = {}) {
  const params = new URLSearchParams();
  const values = {
    q: elements.search.value.trim(),
    frontier: elements.frontier.value,
    license: elements.license.value,
    authorization: elements.authorization.value,
    translation: elements.translation.value,
    publication: elements.publication.value,
    sort: elements.sort.value === "title" ? "" : elements.sort.value,
    page: state.page > 1 ? String(state.page) : "",
  };

  for (const [key, value] of Object.entries(values)) {
    if (value) params.set(key, value);
  }
  const target = `/${params.size ? `?${params}` : ""}`;
  window.history[push ? "pushState" : "replaceState"]({}, "", target);
  setCanonical("/");
}

function renderMetrics() {
  const stats = state.stats;
  document.querySelector("#stat-catalog").textContent = formatNumber(stats.catalog_count);
  document.querySelector("#stat-cleared").textContent = formatNumber(stats.authorization_counts["license-cleared"] || 0);
  document.querySelector("#stat-permission").textContent = formatNumber(stats.authorization_counts["needs-permission"] || 0);
  document.querySelector("#stat-pages").textContent = formatNumber(stats.page_summary.total);
}

function paperSearchText(paper) {
  return [
    paper.paper_id,
    paper.record_id,
    paper.title,
    paper.authors_as_listed,
    ...(paper.frontiers || []),
    ...(paper.frontier_labels || []),
    ...(paper.topics || []),
    paper.primary_arxiv_category,
  ].join(" ").toLocaleLowerCase("zh-CN");
}

function filteredPapers() {
  const query = elements.search.value.trim().toLocaleLowerCase("zh-CN");
  const frontier = elements.frontier.value;
  const sourceLicense = elements.license.value;
  const authorization = elements.authorization.value;
  const translation = elements.translation.value;
  const publication = elements.publication.value;

  const papers = state.papers.filter((paper) => {
    if (query && !paperSearchText(paper).includes(query)) return false;
    if (frontier && !paper.frontiers.includes(frontier)) return false;
    if (sourceLicense && paper.source_license !== sourceLicense) return false;
    if (authorization && paper.authorization_status !== authorization) return false;
    if (translation && paper.translation_status !== translation) return false;
    if (publication === "allowed" && !paper.publication_allowed) return false;
    if (publication === "blocked" && paper.publication_allowed) return false;
    return true;
  });

  const compareTitle = (a, b) => a.title.localeCompare(b.title, "en", { sensitivity: "base" });
  const descending = (field) => (a, b) => {
    const first = Number.isFinite(a[field]) ? a[field] : -Infinity;
    const second = Number.isFinite(b[field]) ? b[field] : -Infinity;
    return second - first || compareTitle(a, b);
  };
  const sorters = {
    title: compareTitle,
    citations: descending("citation_count"),
    impact: descending("impact_proxy_score_0_100"),
    pages: descending("page_count"),
    year: descending("publication_year"),
  };
  return papers.sort(sorters[elements.sort.value] || compareTitle);
}

function renderPaperCard(paper) {
  const frontiers = paper.frontiers.map((frontier) => escapeHTML(frontier)).join(" · ");
  const authLabel = AUTHORIZATION_LABELS[paper.authorization_status] || paper.authorization_status;
  const translationLabel = TRANSLATION_LABELS[paper.translation_status] || paper.translation_status;
  const publicationLabel = paper.publication_allowed ? "具备发布基础" : "暂不可公开全文";
  const publicationClass = paper.publication_allowed ? "badge-cleared" : "badge-blocked";
  const sourceURL = safeURL(paper.source_url);

  return `
    <article class="paper-card">
      <div class="paper-card-top">
        <span class="paper-id">${escapeHTML(paper.paper_id)}</span>
        <span class="paper-year">${escapeHTML(paper.publication_year || "年份未知")}</span>
      </div>
      <h3>${escapeHTML(paper.title)}</h3>
      <p class="paper-authors">${escapeHTML(paper.authors_as_listed || "作者信息待核验")}</p>
      <div class="badges" aria-label="论文状态">
        <span class="badge ${statusClass(paper.authorization_status)}">${escapeHTML(authLabel)}</span>
        <span class="badge ${statusClass(paper.translation_status)}">${escapeHTML(translationLabel)}</span>
        <span class="badge ${publicationClass}">${publicationLabel}</span>
      </div>
      <div class="paper-meta">
        <span>${frontiers || "未分类"}</span>
        <span>${formatNumber(paper.page_count)} 页</span>
        <span>${formatNumber(paper.citation_count)} 引用</span>
      </div>
      <div class="paper-card-footer">
        <a href="${sourceURL}" target="_blank" rel="noreferrer">查看原文 ↗</a>
        <button class="paper-detail-button" type="button" data-paper="${escapeHTML(paper.record_id)}">权利与进度详情 →</button>
      </div>
    </article>`;
}

function renderPagination(totalPages) {
  if (totalPages <= 1) {
    elements.pagination.innerHTML = "";
    return;
  }
  const current = state.page;
  const candidates = new Set([1, totalPages, current - 2, current - 1, current, current + 1, current + 2]);
  const pages = [...candidates].filter((page) => page >= 1 && page <= totalPages).sort((a, b) => a - b);
  let previous = 0;
  const buttons = [];
  buttons.push(`<button type="button" data-page="${current - 1}" ${current === 1 ? "disabled" : ""} aria-label="上一页">←</button>`);
  for (const page of pages) {
    if (previous && page - previous > 1) buttons.push(`<span aria-hidden="true">…</span>`);
    buttons.push(`<button type="button" data-page="${page}" ${page === current ? 'aria-current="page"' : ""}>${page}</button>`);
    previous = page;
  }
  buttons.push(`<button type="button" data-page="${current + 1}" ${current === totalPages ? "disabled" : ""} aria-label="下一页">→</button>`);
  elements.pagination.innerHTML = buttons.join("");
}

function renderCatalog({ preserveScroll = true } = {}) {
  const papers = filteredPapers();
  const totalPages = Math.max(1, Math.ceil(papers.length / PAGE_SIZE));
  state.page = Math.min(state.page, totalPages);
  const start = (state.page - 1) * PAGE_SIZE;
  const visible = papers.slice(start, start + PAGE_SIZE);

  elements.detail.hidden = true;
  elements.catalog.hidden = false;
  elements.resultCount.textContent = `${formatNumber(papers.length)} 篇符合条件 · 第 ${state.page}/${totalPages} 页`;

  if (visible.length) {
    elements.grid.innerHTML = visible.map(renderPaperCard).join("");
  } else {
    elements.grid.innerHTML = `
      <div class="empty-state">
        <strong>没有找到符合条件的论文</strong>
        <p>可以缩短关键词，或清除一项许可证、Frontier、授权或翻译筛选。</p>
      </div>`;
  }
  renderPagination(totalPages);
  writeURLState();
  if (!preserveScroll) elements.catalog.scrollIntoView({ behavior: "smooth", block: "start" });
}

function definitionRow(term, value) {
  return `<dt>${escapeHTML(term)}</dt><dd>${value}</dd>`;
}

function renderDetail(recordId, { push = false } = {}) {
  const paper = state.papers.find((entry) => entry.record_id === recordId);
  if (!paper) {
    renderMissingPaper(recordId);
    return;
  }

  const authLabel = AUTHORIZATION_LABELS[paper.authorization_status] || paper.authorization_status;
  const translationLabel = TRANSLATION_LABELS[paper.translation_status] || paper.translation_status;
  const conditions = paper.publication_conditions.length
    ? paper.publication_conditions.map((item) => `<li>${escapeHTML(CONDITION_LABELS[item] || item)}</li>`).join("")
    : "<li>当前来源许可证没有额外列出的发布条件。</li>";
  const topics = paper.topics.length
    ? paper.topics.map((topic) => `<li>${escapeHTML(topic)}</li>`).join("")
    : "<li>专题信息待补充。</li>";
  const reviewNames = paper.human_reviewers.length
    ? paper.human_reviewers.map(escapeHTML).join("、")
    : "尚无公开署名审校者";
  const translationURL = paper.publication_allowed === true && paper.publication_translation_url
    ? safeURL(paper.publication_translation_url)
    : null;

  elements.detail.innerHTML = `
    <button type="button" class="detail-back" id="detail-back">← 返回筛选结果</button>
    <div class="detail-header">
      <div>
        <p class="eyebrow">${escapeHTML(paper.paper_id)} · ${escapeHTML(paper.frontiers.join(" / "))}</p>
        <h1 id="detail-title">${escapeHTML(paper.title)}</h1>
        <p class="detail-authors">${escapeHTML(paper.authors_as_listed || "作者信息待核验")}</p>
      </div>
      <aside class="detail-status" aria-label="当前状态">
        <p class="principle-index">CURRENT STATUS</p>
        <div class="badges">
          <span class="badge ${statusClass(paper.authorization_status)}">${escapeHTML(authLabel)}</span>
          <span class="badge ${statusClass(paper.translation_status)}">${escapeHTML(translationLabel)}</span>
          <span class="badge ${paper.publication_allowed ? "badge-cleared" : "badge-blocked"}">${paper.publication_allowed ? "具备发布基础" : "暂不可公开全文"}</span>
        </div>
        <p>${paper.publication_allowed
          ? "来源许可允许制作改编作品；公开译文仍须完整落实署名、变更说明、相同方式共享或非商业条件。"
          : "当前记录没有足够的公开改编授权。机器草稿可以内部保存，但不能作为完整译文公开。"}</p>
      </aside>
    </div>
    <div class="detail-grid">
      <section class="detail-block">
        <h2>来源与权利</h2>
        <dl class="definition-list">
          ${definitionRow("来源许可证", `<a href="${safeURL(paper.source_license_url)}" target="_blank" rel="noreferrer">${escapeHTML(paper.source_license || "未知")} ↗</a>`)}
          ${definitionRow("允许改编", paper.permits_adaptation === true ? "是（遵守许可证条件）" : paper.permits_adaptation === false ? "否（须另行授权）" : "未确认")}
          ${definitionRow("权利处置", escapeHTML(paper.license_decision || "等待人工核验"))}
          ${definitionRow("发布依据", escapeHTML(paper.publication_basis || "无"))}
          ${definitionRow("来源版本", escapeHTML(paper.source_version || "仓储记录未提供"))}
          ${definitionRow("最后核验", escapeHTML(paper.public_updated_at ? paper.public_updated_at.slice(0, 10) : "未知"))}
        </dl>
      </section>
      <section class="detail-block">
        <h2>翻译与审校</h2>
        <dl class="definition-list">
          ${definitionRow("翻译状态", escapeHTML(translationLabel))}
          ${definitionRow("译文许可证", escapeHTML(paper.translation_license || "尚未指定"))}
          ${definitionRow("机器模型", escapeHTML(paper.machine_model || "尚未生成机器草稿"))}
          ${definitionRow("人工审校者", reviewNames)}
          ${definitionRow("公开译文", translationURL
            ? `<a href="${translationURL}" target="_blank" rel="noreferrer">下载中文试译版 PDF ↗</a>`
            : "尚未公开")}
          ${definitionRow("译本版本", escapeHTML(paper.translation_version || "—"))}
          ${definitionRow("文件大小", formatBytes(paper.publication_translation_size_bytes))}
          ${definitionRow("发布校验", paper.publication_translation_sha256
            ? `<code title="${escapeHTML(paper.publication_translation_sha256)}">SHA-256 ${escapeHTML(paper.publication_translation_sha256.slice(0, 12))}…</code>`
            : "—")}
        </dl>
      </section>
      <section class="detail-block">
        <h2>公开条件</h2>
        <ul class="condition-list">${conditions}</ul>
      </section>
      <section class="detail-block">
        <h2>论文体量与影响力快照</h2>
        <dl class="definition-list">
          ${definitionRow("年份", escapeHTML(paper.publication_year || "未知"))}
          ${definitionRow("PDF 页数", formatNumber(paper.page_count))}
          ${definitionRow("文本 token 代理", formatNumber(paper.unicode_token_count))}
          ${definitionRow("INSPIRE 引用", formatNumber(paper.citation_count))}
          ${definitionRow("去自引引用", formatNumber(paper.citation_count_without_self_citations))}
          ${definitionRow("年化引用", formatDecimal(paper.citations_per_year))}
          ${definitionRow("影响力代理", paper.impact_proxy_score_0_100 == null ? "—" : `${formatDecimal(paper.impact_proxy_score_0_100)} / 100`)}
        </dl>
      </section>
      <section class="detail-block">
        <h2>Frontier 与专题</h2>
        <ul class="topic-list">${topics}</ul>
      </section>
      <section class="detail-block">
        <h2>行动</h2>
        <p>${paper.publication_allowed
          ? "这篇论文可进入机器初译与社区审校流程。提交译文前，请核对译文许可证是否兼容，并保留完整署名。"
          : "如果希望翻译这篇论文，请先由授权工作流联系通讯作者或权利人，并保存范围明确的书面回复。"}</p>
        <div class="detail-actions">
          <a class="button button-primary" href="${safeURL(paper.source_url)}" target="_blank" rel="noreferrer">查看原文 ↗</a>
          <a class="button button-secondary" href="https://github.com/JunkaiWang-TheoPhy/Snowmass-Physics-CN" target="_blank" rel="noreferrer">前往 GitHub</a>
        </div>
      </section>
    </div>`;

  elements.catalog.hidden = true;
  elements.detail.hidden = false;
  const path = paperPath(paper.record_id);
  window.history[push ? "pushState" : "replaceState"]({}, "", path);
  setCanonical(path);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function renderMissingPaper(recordId) {
  elements.catalog.hidden = true;
  elements.detail.hidden = false;
  elements.detail.innerHTML = `
    <div class="error-card">
      <p class="eyebrow">PAPER / NOT FOUND</p>
      <h1 id="detail-title">这篇论文尚未收录</h1>
      <p>目录中找不到 ${escapeHTML(recordId || "该编号")}。请检查链接，或返回目录重新检索。</p>
      <a class="button button-primary" href="/">返回论文目录</a>
    </div>`;
  setCanonical(window.location.pathname);
}

function handleFilterChange() {
  state.page = 1;
  renderCatalog();
}

async function initialize() {
  try {
    const [papersResponse, statsResponse] = await Promise.all([
      fetch("/data/papers.json"),
      fetch("/data/stats.json"),
    ]);
    if (!papersResponse.ok || !statsResponse.ok) throw new Error("public data request failed");
    [state.papers, state.stats] = await Promise.all([papersResponse.json(), statsResponse.json()]);

    const frontiers = [...new Set(state.papers.flatMap((paper) => paper.frontiers))].sort();
    const licenses = [...new Set(state.papers.map((paper) => paper.source_license).filter(Boolean))].sort();
    fillSelect(elements.frontier, frontiers, (value) => `${value} · ${FRONTIER_LABELS[value] || value}`);
    fillSelect(elements.license, licenses, (value) => value);
    readURLState();
    renderMetrics();

    const recordId = recordIdFromLocation(window.location);
    if (recordId) renderDetail(recordId);
    else renderCatalog();
  } catch (error) {
    console.error(error);
    elements.resultCount.textContent = "目录加载失败";
    elements.grid.innerHTML = `
      <div class="error-state">
        <strong>无法读取公开目录</strong>
        <p>请刷新页面；如果问题持续存在，请到 GitHub 提交 issue。</p>
      </div>`;
  }
}

elements.form.addEventListener("input", handleFilterChange);
elements.form.addEventListener("change", handleFilterChange);
elements.form.addEventListener("reset", () => {
  window.setTimeout(() => {
    state.page = 1;
    renderCatalog();
  }, 0);
});

elements.grid.addEventListener("click", (event) => {
  const button = event.target.closest("[data-paper]");
  if (button) renderDetail(button.dataset.paper, { push: true });
});

elements.pagination.addEventListener("click", (event) => {
  const button = event.target.closest("[data-page]");
  if (!button || button.disabled) return;
  state.page = Number(button.dataset.page);
  renderCatalog({ preserveScroll: false });
});

elements.detail.addEventListener("click", (event) => {
  if (event.target.closest("#detail-back")) {
    renderCatalog();
    elements.catalog.scrollIntoView({ block: "start" });
  }
});

window.addEventListener("popstate", () => {
  readURLState();
  const recordId = recordIdFromLocation(window.location);
  if (recordId) renderDetail(recordId);
  else renderCatalog();
});

initialize();
