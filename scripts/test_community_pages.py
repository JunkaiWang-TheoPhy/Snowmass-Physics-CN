import json
import re
import subprocess
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"


CONTROLLER_HARNESS = r'''
class StubStyle {
  constructor() { this.values = new Map(); }
  setProperty(name, value) { this.values.set(name, String(value)); }
  getPropertyValue(name) { return this.values.get(name) || ""; }
}

class StubElement {
  constructor(tagName, { id = "", className = "", dataset = {}, attributes = {}, hidden = false } = {}) {
    this.tagName = tagName.toUpperCase();
    this.id = id;
    this.className = className;
    this.dataset = { ...dataset };
    this.attributes = new Map(Object.entries(attributes));
    this.children = [];
    this.hidden = hidden;
    this.style = new StubStyle();
    this.listeners = new Map();
    this.parentElement = null;
    this._textContent = "";
  }
  set textContent(value) { this._textContent = String(value); this.children = []; }
  get textContent() { return this._textContent || this.children.map((child) => child.textContent).join(""); }
  append(...children) {
    this._textContent = "";
    children.forEach((child) => { child.parentElement = this; });
    this.children.push(...children);
  }
  replaceChildren(...children) {
    this._textContent = "";
    children.forEach((child) => { child.parentElement = this; });
    this.children = [...children];
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  click() { return this.listeners.get("click")?.({ target: this }); }
}

function installPage({ search = "", savedLanguage = null, navigatorLanguage = "zh-CN", theme = "light", responses = [] } = {}) {
  const nodes = [];
  const add = (tagName, options = {}) => {
    const node = new StubElement(tagName, options);
    nodes.push(node);
    return node;
  };
  const hasClass = (node, name) => node.className.split(/\s+/).includes(name);
  const matches = (node, selector) => {
    if (selector === '[data-zh][data-en]') return node.dataset.zh !== undefined && node.dataset.en !== undefined;
    if (selector === '[data-zh-aria][data-en-aria]') return node.dataset.zhAria !== undefined && node.dataset.enAria !== undefined;
    if (selector === '[data-zh-content][data-en-content]') return node.dataset.zhContent !== undefined && node.dataset.enContent !== undefined;
    if (selector === 'a[href^="../"]') return node.tagName === "A" && (node.getAttribute("href") || "").startsWith("../");
    if (selector.startsWith("#")) return node.id === selector.slice(1);
    if (selector.startsWith(".")) return hasClass(node, selector.slice(1));
    return false;
  };

  const documentElement = new StubElement("html", { dataset: { theme } });
  documentElement.lang = "zh-CN";
  const body = new StubElement("body", { dataset: { page: "progress", papersUrl: "../data/papers.json" } });
  const document = {
    documentElement,
    body,
    createElement: (tagName) => new StubElement(tagName),
    querySelector: (selector) => nodes.find((node) => matches(node, selector)) || null,
    querySelectorAll: (selector) => nodes.filter((node) => matches(node, selector)),
  };

  const languageToggle = add("button", { id: "language-toggle" });
  const themeToggle = add("button", { id: "theme-toggle" });
  const themeLabel = add("span", { id: "theme-label" });
  const localizedText = add("h1", { dataset: { zh: "项目进展", en: "Project progress" } });
  const localizedBrand = add("a", {
    className: "brand",
    dataset: { zhAria: "返回 Snowmass 中文翻译计划首页", enAria: "Back to Snowmass Chinese Translation home" },
    attributes: { "aria-label": "返回 Snowmass 中文翻译计划首页" },
  });
  const localizedAria = add("nav", { dataset: { zhAria: "主要导航", enAria: "Primary navigation" }, attributes: { "aria-label": "主要导航" } });
  const localizedMeta = add("meta", { dataset: { zhContent: "中文说明", enContent: "English description" }, attributes: { content: "中文说明" } });
  const languageLink = add("a", { attributes: { href: "../guide/" } });
  const progressMetrics = add("section", { id: "progress-metrics" });
  add("section", { className: "progress-visuals" });
  const translationChart = add("article", { id: "translation-chart" });
  const rightsChart = add("article", { id: "rights-chart" });
  add("article", { id: "frontier-chart" });
  add("section", { className: "progress-detail" });
  add("aside", { className: "rights-note" });
  const frontierTableBody = add("tbody", { id: "frontier-table-body" });
  const sortHeaders = [];
  const sortButtons = [];
  ["name", "total", "allowed", "blocked", "machine-draft", "human-review", "published"].forEach((sortKey) => {
    const header = add("th", { className: "frontier-sort-heading", dataset: { sortKey } });
    const button = add("button", { className: "frontier-sort-button", dataset: { sortKey } });
    header.append(button);
    sortHeaders.push(header);
    sortButtons.push(button);
  });
  const sortReset = add("button", { id: "frontier-sort-reset" });
  const progressUpdated = add("time", { id: "progress-updated" });
  const progressError = add("div", { id: "progress-error", hidden: true });

  const storage = new Map();
  if (savedLanguage !== null) storage.set("snowmass-language", savedLanguage);
  const localStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
  };
  const location = { href: `https://example.test/progress/${search}`, search };
  const history = {
    replaceState: (_state, _title, nextURL) => {
      location.href = String(nextURL);
      location.search = new URL(location.href).search;
    },
  };
  const fetchCalls = [];
  const responseQueue = [...responses];
  const fetch = async (url, options) => {
    fetchCalls.push({ url, options });
    const response = responseQueue.shift();
    if (!response || response.error) throw new Error(response?.error || "missing response fixture");
    return {
      ok: response.ok ?? true,
      status: response.status ?? 200,
      json: async () => response.papers,
      headers: { get: (name) => name === "Last-Modified" ? (response.lastModified ?? null) : null },
    };
  };

  Object.assign(globalThis, { document, location, history, localStorage, fetch });
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { language: navigatorLanguage } });
  globalThis.addEventListener = () => {};
  console.error = () => {};

  return {
    documentElement, fetchCalls, frontierTableBody, languageLink, languageToggle, localizedAria, localizedBrand,
    localizedMeta, localizedText, progressError, progressMetrics, progressUpdated, rightsChart,
    sortButtons, sortHeaders, sortReset, storage, themeLabel, themeToggle, translationChart,
  };
}

const flushTasks = () => new Promise((resolve) => setTimeout(resolve, 0));
'''


class CommunityPagesTest(unittest.TestCase):
    def read(self, relative):
        return (SITE / relative).read_text()

    def run_controller(self, scenario):
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", CONTROLLER_HARNESS + scenario],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )
        return json.loads(completed.stdout)

    def test_three_independent_routes_exist(self):
        for route in ("progress", "contributors", "guide"):
            html = self.read(f"{route}/index.html")
            self.assertIn("../styles.css", html)
            self.assertIn("../community.js", html)
            self.assertIn('id="language-toggle"', html)
            self.assertIn('id="theme-toggle"', html)

    def test_routes_have_bilingual_metadata_and_one_current_navigation_item(self):
        expected = {
            "progress": "项目进展",
            "contributors": "同行者",
            "guide": "参与指南",
        }
        for route, title in expected.items():
            html = self.read(f"{route}/index.html")
            self.assertIn(f'data-page="{route}"', html)
            self.assertIn(f">{title} |", html)
            self.assertIn('name="description"', html)
            self.assertIn('property="og:title"', html)
            self.assertIn('property="og:description"', html)
            self.assertEqual(html.count('aria-current="page"'), 1)
            self.assertIn('data-zh="', html)
            self.assertIn('data-en="', html)

    def test_home_links_to_each_subpage(self):
        html = self.read("index.html")
        for href in ("progress/", "contributors/", "guide/"):
            self.assertIn(f'href="{href}"', html)

    def test_progress_uses_public_manifest(self):
        html = self.read("progress/index.html")
        js = self.read("community.js")
        self.assertIn("../data/papers.json", html)
        self.assertIn("summarizePapers", js)

    def test_public_html_and_javascript_do_not_hardcode_live_counts(self):
        fixed_count = re.compile(r"(?<!\d)(541|273|268)(?!\d)")
        checked = []
        for path in SITE.rglob("*"):
            relative = path.relative_to(SITE)
            if not path.is_file() or path.suffix not in {".html", ".js", ".mjs"}:
                continue
            if relative.parts and relative.parts[0] == "data":
                continue
            checked.append(str(relative))
            self.assertIsNone(
                fixed_count.search(path.read_text()),
                f"fixed live count found in public file: {relative}",
            )
        self.assertTrue(checked)

    def test_subpage_navigation_stays_named_and_available_when_header_links_collapse(self):
        routes = {
            "progress": ("../?lang=zh", "../contributors/", "../guide/"),
            "contributors": ("../?lang=zh", "../progress/", "../guide/"),
            "guide": ("../?lang=zh", "../progress/", "../contributors/"),
        }
        for route, compact_hrefs in routes.items():
            html = self.read(f"{route}/index.html")
            self.assertRegex(
                html,
                r'<a class="brand"[^>]+data-zh-aria="[^"]+"[^>]+data-en-aria="[^"]+"',
            )
            self.assertEqual(html.count('class="nav-cta"'), 1)
            compact_nav = re.search(
                r'<nav class="subpage-route-links".*?</nav>', html, flags=re.DOTALL,
            )
            self.assertIsNotNone(compact_nav)
            for href in compact_hrefs:
                self.assertIn(f'href="{href}"', compact_nav.group())
            self.assertEqual(compact_nav.group().count("<a "), 3)

        css = self.read("styles.css")
        base, mobile = css.split("@media (max-width: 860px)", 1)
        mobile_860 = mobile.split("@media (max-width: 720px)", 1)[0]
        self.assertIn(".subpage-route-links { display: none;", base)
        self.assertIn(".subpage-route-links { display: flex;", mobile_860)

    def test_frontier_table_exposes_keyboard_sort_controls(self):
        html = self.read("progress/index.html")
        self.assertEqual(html.count('class="frontier-sort-button"'), 7)
        for key in (
            "name", "total", "allowed", "blocked", "machine-draft",
            "human-review", "published",
        ):
            self.assertRegex(
                html,
                rf'<th[^>]+data-sort-key="{re.escape(key)}"[^>]*>\s*<button type="button" class="frontier-sort-button" data-sort-key="{re.escape(key)}"',
            )
        self.assertIn('id="frontier-sort-reset"', html)
        self.assertNotIn('aria-sort="ascending"', html)
        self.assertNotIn('aria-sort="descending"', html)

    def test_progress_exposes_semantic_dynamic_targets_and_cross_listing_note(self):
        html = self.read("progress/index.html")
        for target in (
            "progress-metrics", "translation-chart", "rights-chart",
            "frontier-chart", "frontier-table-body", "progress-error",
            "progress-updated",
        ):
            self.assertIn(f'id="{target}"', html)
        self.assertIn("跨 Frontier", html)
        self.assertIn("Cross-listed papers", html)
        self.assertIn("<table", html)
        self.assertIn("<thead>", html)
        self.assertIn("OFFICIAL FRONTIERS", html)
        self.assertNotIn("10 OFFICIAL FRONTIERS", html)

    def test_contributors_does_not_publish_commit_email(self):
        html = self.read("contributors/index.html")
        self.assertIn("JunkaiWang-TheoPhy", html)
        self.assertIn("项目发起人 / 维护者", html)
        self.assertIsNone(re.search(r"\b\d+@qq\.com\b", html))
        self.assertIn("开放申请", html)

    def test_contributors_are_presented_as_a_compact_directory(self):
        html = self.read("contributors/index.html")
        self.assertIn('<ul class="contributor-list"', html)
        self.assertIn('<li class="contributor-row">', html)
        self.assertIn('class="contributor-name"', html)
        self.assertIn('class="contributor-role"', html)
        self.assertIn('class="contributor-profile"', html)
        self.assertNotIn('class="contributor-card"', html)

    def test_contributor_claims_and_private_applications_use_verified_channels(self):
        html = self.read("contributors/index.html")
        self.assertIn('href="https://github.com/JunkaiWang-TheoPhy"', html)
        self.assertIn(
            'href="mailto:WangTheoPhys@outlook.com?subject=Snowmass%20友情赞助申请"',
            html,
        )
        self.assertIn(
            'href="mailto:WangTheoPhys@outlook.com?subject=Snowmass%20合作机构申请"',
            html,
        )
        self.assertIn("不代表 Snowmass、SLAC、arXiv、论文作者或任何机构为项目背书", html)

    def test_guide_has_four_participation_paths(self):
        html = self.read("guide/index.html")
        for text in ("参与翻译并署名", "核对、修改并提出建议", "协助申请翻译权限", "分发和宣传"):
            self.assertIn(f'data-zh="{text}"', html)

    def test_guide_splits_public_work_from_private_rights_contact(self):
        html = self.read("guide/index.html")
        repository = "https://github.com/JunkaiWang-TheoPhy/Snowmass-Physics-CN"
        for href in (
            f"{repository}/blob/main/CONTRIBUTING.md",
            f"{repository}/issues",
            f"{repository}/pulls",
        ):
            self.assertIn(f'href="{href}"', html)
        self.assertIn(
            'href="mailto:WangTheoPhys@outlook.com?subject=Snowmass%20翻译授权协助"',
            html,
        )
        self.assertIn("介绍人不能代替权利人作出授权", html)
        self.assertIn("不得分发等待授权的完整译文", html)

    def test_controller_executes_language_theme_and_progress_rendering(self):
        result = self.run_controller(r'''
const papers = [
  { frontiers: ["AF"], translation_status: "published", authorization_status: "license-cleared", publication_allowed: true },
  { frontiers: ["AF", "TF", "AF"], translation_status: "machine-draft", authorization_status: "needs-permission", publication_allowed: false },
  { frontiers: ["TF"], translation_status: "human-review", authorization_status: "contacted" },
];
const page = installPage({
  search: "?lang=en", savedLanguage: "zh", navigatorLanguage: "zh-CN", theme: "light",
  responses: [{ papers, lastModified: "Sun, 10 Aug 2026 12:00:00 GMT" }],
});
await import("./site/community.js?behavior-success");
await flushTasks();
const metricValues = page.progressMetrics.children.map((card) => card.children[1].textContent);
const donut = page.rightsChart.children[1];
const { FRONTIERS } = await import("./site/community-stats.mjs");
const initialLanguage = {
  documentLanguage: page.documentElement.lang,
  localizedText: page.localizedText.textContent,
  localizedMeta: page.localizedMeta.getAttribute("content"),
  localizedAria: page.localizedAria.getAttribute("aria-label"),
  localizedBrand: page.localizedBrand.getAttribute("aria-label"),
  languageLink: page.languageLink.getAttribute("href"),
  storedLanguage: page.storage.get("snowmass-language"),
};
await page.languageToggle.click();
const switchedLanguage = {
  documentLanguage: page.documentElement.lang,
  localizedText: page.localizedText.textContent,
  localizedMeta: page.localizedMeta.getAttribute("content"),
  localizedAria: page.localizedAria.getAttribute("aria-label"),
  localizedBrand: page.localizedBrand.getAttribute("aria-label"),
  languageLink: page.languageLink.getAttribute("href"),
  storedLanguage: page.storage.get("snowmass-language"),
};
await page.languageToggle.click();
await page.themeToggle.click();
console.log(JSON.stringify({
  initialLanguage,
  switchedLanguage,
  fetchCall: page.fetchCalls[0],
  metricValues,
  donutAngle: donut.style.getPropertyValue("--allowed-angle"),
  rightsText: page.rightsChart.textContent,
  translationNote: page.translationChart.children[2]?.textContent ?? null,
  frontierRowsMatchDefinitions: page.frontierTableBody.children.length === FRONTIERS.length,
  updatedDateTime: page.progressUpdated.dateTime,
  theme: page.documentElement.dataset.theme,
  storedTheme: page.storage.get("snowmass-theme"),
  themeLabel: page.themeLabel.textContent,
  themePressed: page.themeToggle.getAttribute("aria-pressed"),
}));
''')
        self.assertEqual(result["initialLanguage"], {
            "documentLanguage": "en",
            "localizedText": "Project progress",
            "localizedMeta": "English description",
            "localizedAria": "Primary navigation",
            "localizedBrand": "Back to Snowmass Chinese Translation home",
            "languageLink": "../guide/?lang=en",
            "storedLanguage": "en",
        })
        self.assertEqual(result["switchedLanguage"], {
            "documentLanguage": "zh-CN",
            "localizedText": "项目进展",
            "localizedMeta": "中文说明",
            "localizedAria": "主要导航",
            "localizedBrand": "返回 Snowmass 中文翻译计划首页",
            "languageLink": "../guide/?lang=zh",
            "storedLanguage": "zh",
        })
        self.assertEqual(result["fetchCall"], {
            "url": "../data/papers.json", "options": {"cache": "no-cache"},
        })
        self.assertEqual(result["metricValues"], ["3", "1", "2", "1"])
        self.assertEqual(result["donutAngle"], "120deg")
        self.assertIn("Adaptation cleared1", result["rightsText"])
        self.assertIn("Full text currently blocked2", result["rightsText"])
        self.assertIsNone(result["translationNote"])
        self.assertTrue(result["frontierRowsMatchDefinitions"])
        self.assertEqual(result["updatedDateTime"], "2026-08-10T12:00:00.000Z")
        self.assertEqual(result["theme"], "dark")
        self.assertEqual(result["storedTheme"], "dark")
        self.assertEqual(result["themeLabel"], "Light")
        self.assertEqual(result["themePressed"], "true")

    def test_frontier_table_sorts_numeric_and_names_then_restores_official_order(self):
        result = self.run_controller(r'''
const papers = [
  { frontiers: ["AF"], translation_status: "published", publication_allowed: true },
  { frontiers: ["TF"], translation_status: "published", publication_allowed: true },
  { frontiers: ["TF"], translation_status: "not-started", publication_allowed: false },
  { frontiers: ["UF"], translation_status: "not-started", publication_allowed: false },
  { frontiers: ["UF"], translation_status: "not-started", publication_allowed: false },
  { frontiers: ["UF"], translation_status: "not-started", publication_allowed: false },
];
const page = installPage({ search: "?lang=en", responses: [{ papers }] });
await import("./site/community.js?behavior-frontier-sort");
await flushTasks();
const rowCodes = () => page.frontierTableBody.children.map((row) => row.children[0].children[0].textContent);
const button = (key) => page.sortButtons.find((node) => node.dataset.sortKey === key);
const header = (key) => page.sortHeaders.find((node) => node.dataset.sortKey === key);
const official = rowCodes();
await button("total").click();
const numericAscending = rowCodes();
const numericAscendingAria = header("total").getAttribute("aria-sort");
await button("total").click();
const numericDescending = rowCodes();
const numericDescendingAria = header("total").getAttribute("aria-sort");
await button("name").click();
await button("name").click();
const nameDescending = rowCodes();
const nameDescendingAria = header("name").getAttribute("aria-sort");
const staleNumericAria = header("total").getAttribute("aria-sort");
await page.sortReset.click();
console.log(JSON.stringify({
  official,
  numericAscending,
  numericAscendingAria,
  numericDescending,
  numericDescendingAria,
  nameDescending,
  nameDescendingAria,
  staleNumericAria,
  restored: rowCodes(),
  restoredNameAria: header("name").getAttribute("aria-sort"),
}));
''')
        official = ["AF", "CEF", "CompF", "CF", "EF", "IF", "NF", "RPF", "TF", "UF"]
        self.assertEqual(result["official"], official)
        self.assertEqual(result["numericAscending"][-3:], ["AF", "TF", "UF"])
        self.assertEqual(result["numericAscendingAria"], "ascending")
        self.assertEqual(result["numericDescending"][:3], ["UF", "TF", "AF"])
        self.assertEqual(result["numericDescendingAria"], "descending")
        self.assertEqual(result["nameDescending"][:3], ["UF", "TF", "RPF"])
        self.assertEqual(result["nameDescendingAria"], "descending")
        self.assertIsNone(result["staleNumericAria"])
        self.assertEqual(result["restored"], official)
        self.assertIsNone(result["restoredNameAria"])

    def test_empty_manifest_uses_neutral_rights_state(self):
        result = self.run_controller(r'''
const page = installPage({ search: "?lang=en", responses: [{ papers: [] }] });
await import("./site/community.js?behavior-empty-rights");
await flushTasks();
const donut = page.rightsChart.children[1];
console.log(JSON.stringify({
  className: donut.className,
  angle: donut.style.getPropertyValue("--allowed-angle"),
  accessibleName: donut.getAttribute("aria-label"),
  center: donut.children[0].textContent,
}));
''')
        self.assertEqual(result["className"], "rights-donut rights-donut-empty")
        self.assertEqual(result["angle"], "")
        self.assertIn("No rights data", result["accessibleName"])
        self.assertEqual(result["center"], "—")

    def test_all_not_started_manifest_gets_bilingual_first_wave_note(self):
        result = self.run_controller(r'''
const papers = [
  { frontiers: ["AF"], translation_status: "not-started", publication_allowed: true },
  { frontiers: ["TF"], translation_status: "not-started", publication_allowed: false },
];
const page = installPage({ search: "?lang=en", responses: [{ papers }] });
await import("./site/community.js?behavior-first-wave-note");
await flushTasks();
const english = page.translationChart.children[2]?.textContent ?? null;
await page.languageToggle.click();
const chinese = page.translationChart.children[2]?.textContent ?? null;
console.log(JSON.stringify({ english, chinese }));
''')
        self.assertEqual(
            result["english"],
            "No record has advanced beyond not started; the project is entering its first production translation wave.",
        )
        self.assertEqual(
            result["chinese"],
            "目前尚无记录进入“尚未开始”之后的阶段；项目正进入首批生产翻译阶段。",
        )

    def test_controller_language_precedence_falls_back_to_storage_then_browser(self):
        result = self.run_controller(r'''
const savedPage = installPage({ savedLanguage: "en", navigatorLanguage: "zh-CN", responses: [{ papers: [] }] });
await import("./site/community.js?behavior-saved-language");
await flushTasks();
const savedLanguage = savedPage.documentElement.lang;
const browserPage = installPage({ navigatorLanguage: "en-GB", responses: [{ papers: [] }] });
await import("./site/community.js?behavior-browser-language");
await flushTasks();
console.log(JSON.stringify({ savedLanguage, browserLanguage: browserPage.documentElement.lang }));
''')
        self.assertEqual(result, {"savedLanguage": "en", "browserLanguage": "en"})

    def test_controller_does_not_parse_data_as_html(self):
        self.assertNotIn("innerHTML", self.read("community.js"))

    def test_controller_uses_current_load_time_without_last_modified(self):
        result = self.run_controller(r'''
const before = Date.now();
const page = installPage({ responses: [{ papers: [] }] });
await import("./site/community.js?behavior-fallback-date");
await flushTasks();
const after = Date.now();
console.log(JSON.stringify({ before, after, updated: Date.parse(page.progressUpdated.dateTime) }));
''')
        self.assertLessEqual(result["before"], result["updated"])
        self.assertLessEqual(result["updated"], result["after"])

    def test_controller_shows_failure_and_successfully_retries(self):
        result = self.run_controller(r'''
const page = installPage({
  search: "?lang=en",
  responses: [
    { error: "offline" },
    { papers: [{ frontiers: ["AF"], translation_status: "not-started", publication_allowed: true }] },
  ],
});
await import("./site/community.js?behavior-retry");
await flushTasks();
const errorMessage = page.progressError.children[0].textContent;
const retryButton = page.progressError.children[1];
const firstState = {
  errorHidden: page.progressError.hidden,
  metricsHidden: page.progressMetrics.hidden,
  retryLabel: retryButton.textContent,
};
await retryButton.click();
await flushTasks();
console.log(JSON.stringify({
  firstState,
  fetchCount: page.fetchCalls.length,
  finalErrorHidden: page.progressError.hidden,
  finalMetricsHidden: page.progressMetrics.hidden,
  finalMetricValues: page.progressMetrics.children.map((card) => card.children[1].textContent),
  errorMessage,
}));
''')
        self.assertEqual(result["firstState"], {
            "errorHidden": False, "metricsHidden": True, "retryLabel": "Reload",
        })
        self.assertEqual(result["errorMessage"], "Project progress could not be loaded. Please try again.")
        self.assertEqual(result["fetchCount"], 2)
        self.assertTrue(result["finalErrorHidden"])
        self.assertFalse(result["finalMetricsHidden"])
        self.assertEqual(result["finalMetricValues"], ["1", "1", "0", "0"])

    def test_stats_module_fixture_and_fail_closed_gate(self):
        script = r'''import { summarizePapers } from "./site/community-stats.mjs";
const result = summarizePapers([
  {frontiers:["AF"], translation_status:"published", authorization_status:"license-cleared", publication_allowed:true},
  {frontiers:["AF","TF","AF"], translation_status:"machine-draft", authorization_status:"needs-permission", publication_allowed:false},
  {frontiers:["TF"], translation_status:"unknown", authorization_status:"contacted"}
]);
console.log(JSON.stringify(result));'''
        completed = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            cwd=ROOT, check=True, capture_output=True, text=True,
        )
        result = json.loads(completed.stdout)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["publication"]["allowed"], 1)
        self.assertEqual(result["publication"]["blocked"], 2)
        self.assertEqual(result["translation"]["published"], 1)
        self.assertEqual(result["translation"]["other"], 1)
        self.assertEqual(result["frontiers"]["AF"]["total"], 2)
        self.assertEqual(result["frontiers"]["TF"]["total"], 2)


if __name__ == "__main__":
    unittest.main()
