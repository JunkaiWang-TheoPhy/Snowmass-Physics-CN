# Snowmass Community Pages Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add separate bilingual Progress, Contributors, and Guide pages to the Snowmass translation site, with accessible data-derived charts and explicit community participation paths.

**Architecture:** Keep the existing static HTML/CSS/JavaScript site and Netlify publishing flow. Add three directory routes that share one subpage controller, isolate pure statistics in an importable `.mjs` module, and derive every Progress value from `site/data/papers.json` at runtime. Preserve the catalog `app.js` and the fail-closed `publication_allowed === true` rights gate.

**Tech Stack:** Semantic HTML5, existing CSS custom properties, vanilla ES modules, Python `unittest`, Node.js syntax/module checks, Netlify static hosting.

## Global Constraints

- Do not add dependencies or a chart library.
- Only `publication_allowed === true` counts as publicly adaptable; missing, `false`, and `null` are blocked.
- Do not hard-code 541, 273, or 268 in site JavaScript or page HTML.
- Do not infer contributors, sponsors, partners, or permission grants from Git or repository metadata.
- Contributors first release lists only `JunkaiWang-TheoPhy` as `项目发起人 / 维护者`.
- Sponsorship, institutional cooperation, and private rights contacts use the project email; public translation and review collaboration use GitHub.
- All three pages must support `?lang=zh` and `?lang=en`, light/dark themes, keyboard navigation, and 390px-wide layouts.
- Progress charts require equivalent text and a semantic table; Frontier cross-listing must be disclosed.
- Existing catalog filters, sorting, detail view, rights language, translation queue gate, and public manifest remain unchanged.

---

### Task 1: Lock the community-page contract with failing tests

**Files:**
- Create: `scripts/test_community_pages.py`
- Modify: `scripts/test_site_interface.py`

**Interfaces:**
- Consumes: existing `site/index.html`, `site/styles.css`, and `site/data/papers.json`.
- Produces: structural and data-contract tests that Tasks 2–4 must satisfy.

- [ ] **Step 1: Add route, navigation, privacy, and dynamic-data tests**

Create `scripts/test_community_pages.py` with these assertions:

```python
import json
import re
import subprocess
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"


class CommunityPagesTest(unittest.TestCase):
    def read(self, relative):
        return (SITE / relative).read_text()

    def test_three_independent_routes_exist(self):
        for route in ("progress", "contributors", "guide"):
            html = self.read(f"{route}/index.html")
            self.assertIn("../styles.css", html)
            self.assertIn("../community.js", html)
            self.assertIn('id="language-toggle"', html)
            self.assertIn('id="theme-toggle"', html)

    def test_home_links_to_each_subpage(self):
        html = self.read("index.html")
        for href in ("progress/", "contributors/", "guide/"):
            self.assertIn(f'href="{href}"', html)

    def test_progress_uses_public_manifest_without_fixed_counts(self):
        html = self.read("progress/index.html")
        js = self.read("community.js")
        self.assertIn("../data/papers.json", html)
        self.assertIn("summarizePapers", js)
        for fixed in ("541", "273", "268"):
            self.assertNotIn(fixed, html)
            self.assertNotIn(fixed, js)

    def test_contributors_does_not_publish_commit_email(self):
        html = self.read("contributors/index.html")
        self.assertIn("JunkaiWang-TheoPhy", html)
        self.assertIn("项目发起人 / 维护者", html)
        self.assertIsNone(re.search(r"\b\d+@qq\.com\b", html))
        self.assertIn("开放申请", html)

    def test_guide_has_four_participation_paths(self):
        html = self.read("guide/index.html")
        for text in ("参与翻译并署名", "核对、修改并提出建议", "协助申请翻译权限", "分发和宣传"):
            self.assertIn(f'data-zh="{text}"', html)

    def test_stats_module_fixture_and_fail_closed_gate(self):
        script = r'''import { summarizePapers } from "./site/community-stats.mjs";
const result = summarizePapers([
  {frontiers:["AF"], translation_status:"published", authorization_status:"license-cleared", publication_allowed:true},
  {frontiers:["AF","TF"], translation_status:"machine-draft", authorization_status:"needs-permission", publication_allowed:false},
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
```

- [ ] **Step 2: Extend the existing interface test for the new shared navigation**

In `scripts/test_site_interface.py`, extend the catalog-hook test with:

```python
for href in ("progress/", "contributors/", "guide/"):
    self.assertIn(f'href="{href}"', html)
```

- [ ] **Step 3: Run the tests and verify the expected failure**

Run:

```bash
python3 -m unittest scripts.test_community_pages scripts.test_site_interface -v
```

Expected: failures for missing route HTML files and missing `community-stats.mjs`; existing catalog tests still pass until they reach the new navigation assertions.

- [ ] **Step 4: Commit the test contract**

```bash
git add scripts/test_community_pages.py scripts/test_site_interface.py
git commit -m "Protect the public community-page contract"
```

Use Lore trailers recording the fail-closed rights constraint and the expected missing-page failure.

---

### Task 2: Implement pure, testable project-statistics derivation

**Files:**
- Create: `site/community-stats.mjs`
- Test: `scripts/test_community_pages.py`

**Interfaces:**
- Consumes: an array of public paper records shaped like `site/data/papers.json`.
- Produces: `summarizePapers(papers: Array<object>): Summary` and `FRONTIERS`, where `Summary` contains `total`, `publication`, `translation`, `authorization`, and per-Frontier totals.

- [ ] **Step 1: Define official Frontier metadata and normalized state lists**

Create `site/community-stats.mjs` with:

```js
export const FRONTIERS = [
  ["AF", "加速器前沿", "Accelerator Frontier"],
  ["CEF", "社群参与前沿", "Community Engagement Frontier"],
  ["CompF", "计算前沿", "Computational Frontier"],
  ["CF", "宇宙前沿", "Cosmic Frontier"],
  ["EF", "能量前沿", "Energy Frontier"],
  ["IF", "仪器学前沿", "Instrumentation Frontier"],
  ["NF", "中微子前沿", "Neutrinos Frontier"],
  ["RPF", "稀有过程与精密测量前沿", "Rare Processes & Precision Measurements Frontier"],
  ["TF", "理论前沿", "Theory Frontier"],
  ["UF", "地下设施与基础设施前沿", "Underground Facilities & Infrastructure Frontier"],
];

const TRANSLATION_STATES = ["not-started", "machine-draft", "human-review", "published"];
const AUTHORIZATION_STATES = ["license-cleared", "needs-permission", "contacted", "response-pending", "permission-granted", "permission-denied"];
```

- [ ] **Step 2: Implement fail-closed summary generation**

Add:

```js
function emptyCounts(keys) {
  return Object.fromEntries([...keys, "other"].map((key) => [key, 0]));
}

export function summarizePapers(papers) {
  const records = Array.isArray(papers) ? papers : [];
  const translation = emptyCounts(TRANSLATION_STATES);
  const authorization = emptyCounts(AUTHORIZATION_STATES);
  const frontiers = Object.fromEntries(FRONTIERS.map(([code]) => [code, {
    total: 0, allowed: 0, blocked: 0,
    "machine-draft": 0, "human-review": 0, published: 0,
  }]));
  let allowed = 0;

  for (const paper of records) {
    const translationKey = TRANSLATION_STATES.includes(paper?.translation_status) ? paper.translation_status : "other";
    const authorizationKey = AUTHORIZATION_STATES.includes(paper?.authorization_status) ? paper.authorization_status : "other";
    const isAllowed = paper?.publication_allowed === true;
    translation[translationKey] += 1;
    authorization[authorizationKey] += 1;
    if (isAllowed) allowed += 1;
    const uniqueFrontiers = new Set(Array.isArray(paper?.frontiers) ? paper.frontiers : []);
    for (const code of uniqueFrontiers) {
      if (!frontiers[code]) continue;
      frontiers[code].total += 1;
      frontiers[code][isAllowed ? "allowed" : "blocked"] += 1;
      if (["machine-draft", "human-review", "published"].includes(translationKey)) {
        frontiers[code][translationKey] += 1;
      }
    }
  }

  return {
    total: records.length,
    publication: { allowed, blocked: records.length - allowed },
    translation,
    authorization,
    frontiers,
  };
}
```

- [ ] **Step 3: Run the statistics fixture test**

Run:

```bash
python3 -m unittest scripts.test_community_pages.CommunityPagesTest.test_stats_module_fixture_and_fail_closed_gate -v
node --check site/community-stats.mjs
```

Expected: both pass; the fixture reports one allowed and two blocked records.

- [ ] **Step 4: Commit the statistics module**

```bash
git add site/community-stats.mjs scripts/test_community_pages.py
git commit -m "Make community progress statistics auditable"
```

Use Lore trailers noting that Frontier rows cross-list records while global totals remain deduplicated.

---

### Task 3: Build the three semantic bilingual subpages and shared controller

**Files:**
- Create: `site/progress/index.html`
- Create: `site/contributors/index.html`
- Create: `site/guide/index.html`
- Create: `site/community.js`
- Test: `scripts/test_community_pages.py`

**Interfaces:**
- Consumes: `summarizePapers` and `FRONTIERS` from `./community-stats.mjs`; Progress reads the manifest URL from `body[data-papers-url]`.
- Produces: language/theme behavior for all child pages and Progress DOM rendering into `#progress-metrics`, `#translation-chart`, `#rights-chart`, `#frontier-chart`, and `#frontier-table-body`.

- [ ] **Step 1: Create the shared document shell on all routes**

Each page must include:

```html
<link rel="stylesheet" href="../styles.css">
<script src="../theme-init.js"></script>
<header class="site-header subpage-header">
  <a class="brand" href="../"><span class="brand-mark" aria-hidden="true">△</span><span><strong data-zh="Snowmass 中文翻译计划" data-en="Snowmass Chinese Translation">Snowmass 中文翻译计划</strong><small>Open Translation Atlas</small></span></a>
  <nav aria-label="主要导航" data-zh-aria="主要导航" data-en-aria="Primary navigation">
    <a href="../?lang=zh" data-zh="论文目录" data-en="Catalog">论文目录</a>
    <a href="../progress/" data-zh="项目进展" data-en="Progress">项目进展</a>
    <a href="../contributors/" data-zh="同行者" data-en="Contributors">同行者</a>
    <a href="../guide/" data-zh="参与指南" data-en="Guide">参与指南</a>
    <div class="view-controls">
      <button type="button" class="view-toggle" id="language-toggle" aria-pressed="false">EN</button>
      <button type="button" class="view-toggle" id="theme-toggle" aria-pressed="false"><span aria-hidden="true">◐</span><span id="theme-label">深色</span></button>
    </div>
  </nav>
</header>
<script type="module" src="../community.js"></script>
```

Set exactly one matching navigation link to `aria-current="page"`. Use page-specific `<title>`, meta description, heading, and Open Graph text. Progress body carries `data-page="progress" data-papers-url="../data/papers.json"`; Contributors and Guide use their own `data-page` value.

- [ ] **Step 2: Build Progress semantic containers**

Use empty data targets rather than fixed numbers:

```html
<section id="progress-metrics" class="progress-metrics" aria-label="项目核心指标"></section>
<section class="progress-visuals">
  <article id="translation-chart" class="progress-chart"></article>
  <article id="rights-chart" class="progress-chart"></article>
</section>
<article id="frontier-chart" class="frontier-chart-panel"></article>
<div class="progress-table-wrap" tabindex="0">
  <table class="progress-table">
    <thead><tr>
      <th data-zh="研究前沿" data-en="Frontier">研究前沿</th>
      <th data-zh="论文" data-en="Papers">论文</th>
      <th data-zh="可改编" data-en="Adaptation allowed">可改编</th>
      <th data-zh="待授权" data-en="Permission needed">待授权</th>
      <th data-zh="机器初译" data-en="Machine draft">机器初译</th>
      <th data-zh="人工审校" data-en="Human review">人工审校</th>
      <th data-zh="已公开" data-en="Published">已公开</th>
    </tr></thead>
    <tbody id="frontier-table-body"></tbody>
  </table>
</div>
<div id="progress-error" class="error-state" hidden></div>
```

Include a visible cross-listing note and a `<time id="progress-updated">` populated from the manifest response `Last-Modified` header when present, otherwise the current load date.

- [ ] **Step 3: Build Contributors content without inferred identities**

Create one verified contributor card linking to `https://github.com/JunkaiWang-TheoPhy`. Add separate sponsorship and institution application cards. Their buttons use:

```html
href="mailto:WangTheoPhys@outlook.com?subject=Snowmass%20友情赞助申请"
href="mailto:WangTheoPhys@outlook.com?subject=Snowmass%20合作机构申请"
```

Add the non-endorsement disclosure from the design. Do not include commit emails, fabricated logos, contributor counts, or unverified organizations.

- [ ] **Step 4: Build the four Guide participation paths**

Use numbered `<article>` elements for translation, review, rights outreach, and sharing. Link public work to the repository `CONTRIBUTING.md`, Issues, and Pull Requests. Link private permission contact to:

```html
href="mailto:WangTheoPhys@outlook.com?subject=Snowmass%20翻译授权协助"
```

State that introducers cannot grant permission on behalf of rights holders and that blocked full translations must not be distributed.

- [ ] **Step 5: Implement shared language, theme, and chart rendering**

In `site/community.js`:

```js
import { FRONTIERS, summarizePapers } from "./community-stats.mjs";

const LANG_KEY = "snowmass-language";
const THEME_KEY = "snowmass-theme";
const progressCopy = {
  zh: {
    total: "已建档论文", allowed: "具备公开改编基础", blocked: "仍需额外授权", published: "已公开译文",
    translation: "翻译阶段", rights: "公开权限", frontier: "Frontier 工作体量",
    loadError: "项目进展暂时无法加载，请稍后重试。", retry: "重新加载",
  },
  en: {
    total: "Cataloged papers", allowed: "Adaptation cleared", blocked: "Permission still needed", published: "Published translations",
    translation: "Translation stages", rights: "Publication rights", frontier: "Workload by Frontier",
    loadError: "Project progress could not be loaded. Please try again.", retry: "Reload",
  },
};

function currentLanguage() {
  const query = new URLSearchParams(location.search).get("lang");
  if (["zh", "en"].includes(query)) return query;
  const saved = localStorage.getItem(LANG_KEY);
  if (["zh", "en"].includes(saved)) return saved;
  return navigator.language.toLowerCase().startsWith("zh") ? "zh" : "en";
}

function applyLanguage(lang) {
  document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
  document.querySelectorAll("[data-zh][data-en]").forEach((node) => { node.textContent = node.dataset[lang]; });
  document.querySelectorAll("[data-zh-aria][data-en-aria]").forEach((node) => { node.setAttribute("aria-label", node.dataset[`${lang}Aria`]); });
  localStorage.setItem(LANG_KEY, lang);
  const url = new URL(location.href); url.searchParams.set("lang", lang); history.replaceState(null, "", url);
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem(THEME_KEY, next);
}

function renderProgress(summary, lang) {
  renderMetrics(summary, progressCopy[lang]);
  renderTranslationChart(summary.translation, lang);
  renderRightsChart(summary.publication, lang);
  renderFrontierChartsAndTable(summary.frontiers, lang);
}

async function loadProgress() {
  const response = await fetch(document.body.dataset.papersUrl, { cache: "no-cache" });
  if (!response.ok) throw new Error(`papers.json: ${response.status}`);
  const papers = await response.json();
  if (!Array.isArray(papers)) throw new TypeError("papers.json must contain an array");
  renderProgress(summarizePapers(papers), currentLanguage());
}
```

Use DOM APIs (`createElement`, `textContent`, `setAttribute`) for data-derived content rather than interpolating untrusted manifest strings into `innerHTML`. The donut receives `style.setProperty("--allowed-angle", `${allowed / total * 360}deg`)`; textual counts remain visible beside it.

- [ ] **Step 6: Run page and controller tests**

Run:

```bash
python3 -m unittest scripts.test_community_pages -v
node --check site/community.js
node --check site/community-stats.mjs
```

Expected: route, privacy, Guide, and dynamic-data tests pass.

- [ ] **Step 7: Commit the functional pages**

```bash
git add site/progress site/contributors site/guide site/community.js site/community-stats.mjs scripts/test_community_pages.py
git commit -m "Give progress and participation separate public pages"
```

Use Lore trailers recording the public/private contact split and no-fabrication rule.

---

### Task 4: Integrate the pages into the Snowmass visual system

**Files:**
- Modify: `site/index.html`
- Modify: `site/styles.css`
- Modify: `scripts/test_site_interface.py`
- Test: `scripts/test_community_pages.py`

**Interfaces:**
- Consumes: semantic class hooks and IDs created in Task 3.
- Produces: responsive light/dark layouts for the new routes and homepage navigation links.

- [ ] **Step 1: Replace the homepage workflow links with the shared route navigation**

In `site/index.html`, keep the catalog anchor and GitHub CTA, and add:

```html
<a href="progress/" data-i18n="navProgress">项目进展</a>
<a href="contributors/" data-i18n="navContributors">同行者</a>
<a href="guide/" data-i18n="navGuide">参与指南</a>
```

Add `navProgress`, `navContributors`, and `navGuide` to both language maps in `site/app.js`. Keep the rights protocol linked from the hero and footer instead of crowding the primary navigation.

- [ ] **Step 2: Add shared subpage layout styles**

Extend `site/styles.css` with focused blocks for:

- `.subpage-main`, `.subpage-hero`, `.subpage-kicker`, `.subpage-intro`;
- `.progress-metrics`, `.progress-visuals`, `.progress-chart`;
- `.translation-stage-bar`, `.rights-donut`, `.frontier-bars`;
- `.progress-table-wrap`, `.progress-table`;
- `.community-grid`, `.contributor-card`, `.open-call-card`;
- `.guide-grid`, `.guide-step`, `.guide-actions`.

Use existing variables only. The rights donut uses:

```css
.rights-donut {
  background: conic-gradient(
    var(--pine) 0 var(--allowed-angle),
    var(--amber) var(--allowed-angle) 360deg
  );
}
```

Do not add decorative concentric rings, model-authored SVG charts, or motion. Ensure `:root[data-theme="dark"]` needs only color-variable inheritance rather than duplicated layout rules.

- [ ] **Step 3: Add responsive table and navigation behavior**

At `max-width: 860px`, reduce the primary nav to the current page plus controls. At `max-width: 720px`, stack metrics and charts, make `.progress-table-wrap { overflow-x: auto; }`, and keep `.progress-table { min-width: 760px; }`. Confirm `body` retains `overflow-x: hidden` while the table remains keyboard-scrollable.

- [ ] **Step 4: Run static and interface tests**

Run:

```bash
python3 -m unittest scripts.test_community_pages scripts.test_site_interface -v
python3 scripts/audit_public_tree.py
git diff --check
```

Expected: all pass; audit reports no credentials or unpublished translations.

- [ ] **Step 5: Commit the integrated visual system**

```bash
git add site/index.html site/app.js site/styles.css scripts/test_site_interface.py
git commit -m "Connect the community pages through one Snowmass visual language"
```

Use Lore trailers noting the narrow-screen navigation and no-new-dependency constraint.

---

### Task 5: Verify production behavior and publish

**Files:**
- Modify only if verification finds a defect in files owned by Tasks 2–4.
- Update: `.omx/state/snowmass-community-pages/ralph-progress.json` for visual verdict evidence.

**Interfaces:**
- Consumes: complete static site and Netlify build contract.
- Produces: verified commit on public `main` and a ready Netlify production deployment.

- [ ] **Step 1: Run the complete repository test suite and production build command**

Run:

```bash
python3 scripts/build_public_manifest.py
python3 -m unittest discover -s scripts -p 'test_*.py'
node --check site/app.js
node --check site/community.js
node --check site/community-stats.mjs
git diff --check
```

Expected: all tests and syntax checks pass; the manifest still reports the live eligible count derived from data.

- [ ] **Step 2: Serve and inspect the four routes**

Start `python3 -m http.server 8767 --directory site`. Inspect at 1440×1000 and 390×844:

- `/?lang=zh` and `/?lang=en`;
- `/progress/?lang=zh` in light and dark themes;
- `/contributors/?lang=zh`;
- `/guide/?lang=en`.

Verify navigation, direct language URLs, theme persistence, chart totals, table scrolling, external targets, no whole-page horizontal scroll, and fetch-error messaging.

- [ ] **Step 3: Run visual-verdict and persist the result**

Compare the Progress page against the approved “图表优先，表格收尾” mockup. Require score `>= 90`, with no mismatch in page separation, chart hierarchy, Snowmass palette, or mobile readability. Persist numeric and qualitative evidence to `.omx/state/snowmass-community-pages/ralph-progress.json`.

- [ ] **Step 4: Commit verification fixes if any**

If verification required changes, commit only those fixes with a Lore message describing the observed defect and proof. If no changes were required, do not create an empty commit.

- [ ] **Step 5: Integrate to public main and push**

Confirm the branch is a clean fast-forward descendant of `snowmass/main`, then push the verified head to `snowmass main`. Do not include unrelated translation-pipeline worktree changes.

- [ ] **Step 6: Poll Netlify and verify the live site**

Use the configured Netlify site to confirm the deployment for the pushed commit reaches `ready`. Fetch the live CSS and pages with cache-busting query parameters, verify `/progress/`, `/contributors/`, and `/guide/` return 200, and inspect the live Progress DOM. Report the public URLs only after the production deploy and live checks succeed.
