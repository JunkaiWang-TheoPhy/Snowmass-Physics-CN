import { FRONTIERS, summarizePapers } from "./community-stats.mjs";

const LANG_KEY = "snowmass-language";
const THEME_KEY = "snowmass-theme";
const progressCopy = {
  zh: {
    total: "已建档论文", allowed: "具备公开改编基础", blocked: "仍需额外授权", published: "已公开译文",
    translation: "翻译阶段", rights: "公开权限", frontier: "Frontier 工作体量",
    loadError: "项目进展暂时无法加载，请稍后重试。", retry: "重新加载",
    translationStates: {
      "not-started": "尚未开始", "machine-draft": "机器初译", "human-review": "人工审校",
      published: "已公开", other: "其他 / 待核验",
    },
    allowedLegend: "可公开改编", blockedLegend: "当前不可公开全文", papers: "篇",
    languageAria: "切换为英文", themeDark: "深色", themeLight: "浅色",
    themeDarkAria: "切换为深色主题", themeLightAria: "切换为浅色主题",
  },
  en: {
    total: "Cataloged papers", allowed: "Adaptation cleared", blocked: "Permission still needed", published: "Published translations",
    translation: "Translation stages", rights: "Publication rights", frontier: "Workload by Frontier",
    loadError: "Project progress could not be loaded. Please try again.", retry: "Reload",
    translationStates: {
      "not-started": "Not started", "machine-draft": "Machine draft", "human-review": "Human review",
      published: "Published", other: "Other / to verify",
    },
    allowedLegend: "Adaptation cleared", blockedLegend: "Full text currently blocked", papers: "papers",
    languageAria: "Switch to Chinese", themeDark: "Dark", themeLight: "Light",
    themeDarkAria: "Switch to dark theme", themeLightAria: "Switch to light theme",
  },
};

const state = {
  lang: "zh",
  summary: null,
  updatedAt: null,
  progressError: false,
};

function currentLanguage() {
  const query = new URLSearchParams(location.search).get("lang");
  if (["zh", "en"].includes(query)) return query;
  const saved = localStorage.getItem(LANG_KEY);
  if (["zh", "en"].includes(saved)) return saved;
  return navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en";
}

function updateLanguageLinks(lang) {
  document.querySelectorAll('a[href^="../"]').forEach((node) => {
    const currentHref = node.getAttribute("href");
    const [pathAndQuery, hash = ""] = currentHref.split("#", 2);
    const relativePath = pathAndQuery.split("?", 1)[0];
    const url = new URL(currentHref, location.href);
    url.searchParams.set("lang", lang);
    node.setAttribute("href", `${relativePath}?${url.searchParams}${hash ? `#${hash}` : ""}`);
  });
}

function updateViewControls() {
  const copy = progressCopy[state.lang];
  const languageToggle = document.querySelector("#language-toggle");
  const themeToggle = document.querySelector("#theme-toggle");
  const themeLabel = document.querySelector("#theme-label");
  const theme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";

  languageToggle.textContent = state.lang === "zh" ? "EN" : "中文";
  languageToggle.setAttribute("aria-label", copy.languageAria);
  languageToggle.setAttribute("aria-pressed", String(state.lang === "en"));
  themeLabel.textContent = theme === "light" ? copy.themeDark : copy.themeLight;
  themeToggle.setAttribute("aria-label", theme === "light" ? copy.themeDarkAria : copy.themeLightAria);
  themeToggle.setAttribute("aria-pressed", String(theme === "dark"));
}

function applyLanguage(lang) {
  state.lang = lang;
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-zh][data-en]").forEach((node) => { node.textContent = node.dataset[lang]; });
  document.querySelectorAll("[data-zh-aria][data-en-aria]").forEach((node) => { node.setAttribute("aria-label", node.dataset[`${lang}Aria`]); });
  document.querySelectorAll("[data-zh-content][data-en-content]").forEach((node) => { node.setAttribute("content", node.dataset[`${lang}Content`]); });
  localStorage.setItem(LANG_KEY, lang);
  const url = new URL(location.href); url.searchParams.set("lang", lang); history.replaceState(null, "", url);
  updateLanguageLinks(lang);
  updateViewControls();
  if (state.summary) renderProgress(state.summary, lang);
  if (state.updatedAt) renderUpdatedAt(state.updatedAt, lang);
  if (state.progressError) renderProgressError();
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem(THEME_KEY, next);
  updateViewControls();
}

function element(tagName, className, text) {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function percentage(value, total) {
  return total ? Math.round((value / total) * 100) : 0;
}

function renderMetrics(summary, copy) {
  const target = document.querySelector("#progress-metrics");
  const values = [
    [copy.total, summary.total],
    [copy.allowed, summary.publication.allowed],
    [copy.blocked, summary.publication.blocked],
    [copy.published, summary.translation.published],
  ];
  const cards = values.map(([label, value]) => {
    const card = element("article", "progress-metric");
    card.append(element("span", "metric-label", label), element("strong", "", String(value)));
    return card;
  });
  target.replaceChildren(...cards);
}

function renderTranslationChart(translation, lang) {
  const target = document.querySelector("#translation-chart");
  const copy = progressCopy[lang];
  const total = Object.values(translation).reduce((sum, value) => sum + value, 0);
  const title = element("h2", "", copy.translation);
  const list = element("ul", "progress-bars");

  Object.entries(copy.translationStates).forEach(([key, label]) => {
    const count = translation[key] || 0;
    const percent = percentage(count, total);
    const item = element("li", "progress-bar-row");
    const heading = element("div", "progress-bar-label");
    heading.append(element("span", "", label), element("strong", "", `${count} · ${percent}%`));
    const track = element("div", "progress-bar-track");
    const fill = element("span", `progress-bar-fill translation-${key}`);
    fill.style.setProperty("--bar-size", `${percent}%`);
    track.append(fill);
    item.append(heading, track);
    list.append(item);
  });
  target.replaceChildren(title, list);
}

function renderRightsChart(publication, lang) {
  const target = document.querySelector("#rights-chart");
  const copy = progressCopy[lang];
  const total = publication.allowed + publication.blocked;
  const donut = element("div", "rights-donut");
  const allowedAngle = total ? publication.allowed / total * 360 : 0;
  donut.style.setProperty("--allowed-angle", `${allowedAngle}deg`);
  donut.setAttribute("role", "img");
  donut.setAttribute("aria-label", `${copy.allowedLegend}: ${publication.allowed}; ${copy.blockedLegend}: ${publication.blocked}`);
  donut.append(element("strong", "", `${percentage(publication.allowed, total)}%`));

  const legend = element("ul", "chart-legend");
  [["allowed", copy.allowedLegend, publication.allowed], ["blocked", copy.blockedLegend, publication.blocked]].forEach(([kind, label, count]) => {
    const item = element("li", `legend-${kind}`);
    item.append(element("span", "legend-swatch"), element("span", "", label), element("strong", "", String(count)));
    legend.append(item);
  });
  target.replaceChildren(element("h2", "", copy.rights), donut, legend);
}

function frontierName(frontier, lang) {
  return lang === "zh" ? frontier[1] : frontier[2];
}

function renderFrontierChartsAndTable(frontiers, lang) {
  const copy = progressCopy[lang];
  const chart = document.querySelector("#frontier-chart");
  const tableBody = document.querySelector("#frontier-table-body");
  const maximum = Math.max(0, ...FRONTIERS.map(([code]) => frontiers[code].total));
  const chartRows = element("div", "frontier-bars");
  const tableRows = [];

  FRONTIERS.forEach((frontier) => {
    const [code] = frontier;
    const counts = frontiers[code];
    const row = element("div", "frontier-bar-row");
    const label = element("div", "frontier-bar-label");
    label.append(element("strong", "", code), element("span", "", frontierName(frontier, lang)));
    const track = element("div", "progress-bar-track");
    const fill = element("span", "progress-bar-fill frontier-bar-fill");
    fill.style.setProperty("--bar-size", `${percentage(counts.total, maximum)}%`);
    track.append(fill);
    row.append(label, track, element("strong", "frontier-total", String(counts.total)));
    chartRows.append(row);

    const tableRow = document.createElement("tr");
    const rowHeading = element("th", "frontier-name");
    rowHeading.setAttribute("scope", "row");
    rowHeading.append(element("strong", "", code), element("span", "", frontierName(frontier, lang)));
    tableRow.append(rowHeading);
    ["total", "allowed", "blocked", "machine-draft", "human-review", "published"].forEach((key) => {
      tableRow.append(element("td", "", String(counts[key])));
    });
    tableRows.push(tableRow);
  });

  chart.replaceChildren(element("h2", "", copy.frontier), chartRows);
  tableBody.replaceChildren(...tableRows);
}

function renderProgress(summary, lang) {
  renderMetrics(summary, progressCopy[lang]);
  renderTranslationChart(summary.translation, lang);
  renderRightsChart(summary.publication, lang);
  renderFrontierChartsAndTable(summary.frontiers, lang);
}

function renderUpdatedAt(date, lang) {
  const target = document.querySelector("#progress-updated");
  target.dateTime = date.toISOString();
  target.textContent = new Intl.DateTimeFormat(lang === "zh" ? "zh-CN" : "en", {
    year: "numeric", month: "long", day: "numeric",
  }).format(date);
}

function setProgressVisibility(visible) {
  ["#progress-metrics", ".progress-visuals", "#frontier-chart", ".progress-detail", ".rights-note"].forEach((selector) => {
    document.querySelector(selector).hidden = !visible;
  });
}

function renderProgressError() {
  const target = document.querySelector("#progress-error");
  const copy = progressCopy[state.lang];
  const retry = element("button", "button button-primary", copy.retry);
  retry.type = "button";
  retry.addEventListener("click", loadProgress);
  target.replaceChildren(element("strong", "", copy.loadError), retry);
  target.hidden = false;
  setProgressVisibility(false);
}

async function loadProgress() {
  const errorTarget = document.querySelector("#progress-error");
  errorTarget.hidden = true;
  document.body.setAttribute("aria-busy", "true");
  state.progressError = false;
  try {
    const response = await fetch(document.body.dataset.papersUrl, { cache: "no-cache" });
    if (!response.ok) throw new Error(`papers.json: ${response.status}`);
    const papers = await response.json();
    if (!Array.isArray(papers)) throw new TypeError("papers.json must contain an array");
    const headerDate = response.headers.get("Last-Modified");
    const parsedDate = headerDate ? new Date(headerDate) : new Date();
    state.updatedAt = Number.isNaN(parsedDate.getTime()) ? new Date() : parsedDate;
    state.summary = summarizePapers(papers);
    renderProgress(state.summary, state.lang);
    renderUpdatedAt(state.updatedAt, state.lang);
    setProgressVisibility(true);
  } catch (error) {
    console.error("Unable to load community progress", error);
    state.progressError = true;
    renderProgressError();
  } finally {
    document.body.removeAttribute("aria-busy");
  }
}

document.querySelector("#language-toggle").addEventListener("click", () => applyLanguage(state.lang === "zh" ? "en" : "zh"));
document.querySelector("#theme-toggle").addEventListener("click", toggleTheme);
addEventListener("popstate", () => applyLanguage(currentLanguage()));

applyLanguage(currentLanguage());
if (document.body.dataset.page === "progress") loadProgress();
