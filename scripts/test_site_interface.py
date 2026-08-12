from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
import unittest


ROOT = Path(__file__).resolve().parents[1]


class _PaperResourceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        for attribute in ("href", "src", "data-papers-url"):
            if values.get(attribute):
                self.urls.append(values[attribute])


class SiteInterfaceTest(unittest.TestCase):
    def test_independent_paper_route_is_data_driven_and_keeps_legacy_query_detail(self):
        paper_html = (ROOT / "site/paper/index.html").read_text()
        paper_script = (ROOT / "site/paper-page.js").read_text()
        app_script = (ROOT / "site/app.js").read_text()
        netlify = (ROOT / "netlify.toml").read_text()

        self.assertIn('id="paper-page"', paper_html)
        self.assertIn('/data/papers.json', paper_html)
        self.assertIn('src="/paper-page.js"', paper_html)
        self.assertIn("window.location.pathname", paper_script)
        self.assertIn("publication_translation_url", paper_script)
        self.assertIn('from = "/paper/*"', netlify)
        self.assertIn('to = "/paper/index.html"', netlify)
        self.assertIn('status = 200', netlify)
        self.assertIn('params.get("paper")', app_script)

    def test_paper_template_resources_resolve_from_a_permanent_nested_url(self):
        parser = _PaperResourceParser()
        parser.feed((ROOT / "site/paper/index.html").read_text())
        origin = "https://snowmass-physics-cn.netlify.app"
        permanent_url = f"{origin}/paper/2203.07506/"
        internal_paths = {
            urlparse(urljoin(permanent_url, value)).path
            for value in parser.urls
            if not value.startswith(("mailto:", "https://github.com"))
        }

        self.assertIn("/styles.css", internal_paths)
        self.assertIn("/paper-page.js", internal_paths)
        self.assertIn("/data/papers.json", internal_paths)
        self.assertNotIn("/paper/styles.css", internal_paths)
        self.assertNotIn("/paper/paper-page.js", internal_paths)
        self.assertNotIn("/paper/data/papers.json", internal_paths)

    def test_catalog_cards_link_to_permanent_paper_pages(self):
        script = (ROOT / "site/app.js").read_text()
        self.assertIn('class="paper-detail-button"', script)
        self.assertIn('href="${paperPermalink(paper)}"', script)
        self.assertNotIn('<button class="paper-detail-button"', script)

    def test_catalog_hooks_and_rights_language_remain(self):
        html = (ROOT / "site/index.html").read_text()
        for hook in (
            "stat-catalog",
            "stat-cleared",
            "stat-permission",
            "stat-pages",
            "filters",
            "paper-grid",
            "pagination",
            "detail-panel",
        ):
            self.assertIn(f'id="{hook}"', html)
        for href in ("progress/", "contributors/", "guide/"):
            self.assertIn(f'href="{href}"', html)
        self.assertIn("没有明确改编许可的全文不会公开", html)

    def test_home_navigation_exposes_bilingual_community_routes(self):
        html = (ROOT / "site/index.html").read_text()
        script = (ROOT / "site/app.js").read_text()
        expected_links = (
            '<a href="#catalog" aria-current="page" data-i18n="navCatalog">论文目录</a>',
            '<a href="progress/" data-i18n="navProgress">项目进展</a>',
            '<a href="contributors/" data-i18n="navContributors">同行者</a>',
            '<a href="guide/" data-i18n="navGuide">参与指南</a>',
        )
        for link in expected_links:
            self.assertIn(link, html)
        for copy in (
            'navProgress: "项目进展"',
            'navContributors: "同行者"',
            'navGuide: "参与指南"',
            'navProgress: "Progress"',
            'navContributors: "Contributors"',
            'navGuide: "Guide"',
        ):
            self.assertIn(copy, script)

        header_navigation = html.split("<nav data-i18n-aria=", 1)[1].split("</nav>", 1)[0]
        self.assertNotIn("RIGHTS_PROTOCOL.md", header_navigation)
        self.assertIn("RIGHTS_PROTOCOL.md", html.split('<div class="footer-links">', 1)[1])

    def test_community_layout_styles_cover_dynamic_and_semantic_hooks(self):
        css = (ROOT / "site/styles.css").read_text()
        for selector in (
            ".subpage-main",
            ".subpage-hero",
            ".subpage-kicker",
            ".subpage-intro",
            ".progress-metrics",
            ".progress-visuals",
            ".progress-chart",
            ".translation-stage-bar",
            ".rights-donut",
            ".frontier-bars",
            ".progress-table-wrap",
            ".progress-table",
            ".community-grid",
            ".contributor-list",
            ".contributor-row",
            ".open-call-card",
            ".guide-grid",
            ".guide-step",
            ".guide-actions",
            ".participation-translation",
            ".participation-rights",
            ".participation-outreach",
        ):
            self.assertIn(selector, css)
        self.assertIn(".participation-grid {", css)
        self.assertIn("flex-direction: column;", css)
        self.assertIn(".participation-grid > .participation-card { width: 100%; }", css)
        self.assertIn(
            "background: conic-gradient(\n"
            "    var(--pine) 0 var(--allowed-angle),\n"
            "    var(--amber) var(--allowed-angle) 360deg\n"
            "  );",
            css,
        )
        self.assertNotIn("animation:", css)

    def test_community_layout_contains_wide_tables_on_narrow_screens(self):
        css = (ROOT / "site/styles.css").read_text()
        self.assertIn("overflow-x: hidden;", css.split("body {", 1)[1].split("}", 1)[0])
        compact = css.split("@media (max-width: 860px)", 1)[1].split("@media (max-width: 720px)", 1)[0]
        self.assertIn('.site-header nav a:not([aria-current="page"])', compact)
        narrow = css.split("@media (max-width: 720px)", 1)[1].split("@media (prefers-reduced-motion", 1)[0]
        self.assertIn(".progress-metrics, .progress-visuals", narrow)
        self.assertIn(".progress-table-wrap { overflow-x: auto;", narrow)
        self.assertIn(".progress-table { min-width: 760px;", narrow)

    def test_hero_uses_local_image_and_light_color_scheme(self):
        html = (ROOT / "site/index.html").read_text()
        css = (ROOT / "site/styles.css").read_text()
        self.assertIn('content="light dark"', html)
        self.assertIn("assets/snowmass-hero-mountains.webp", css)
        self.assertNotIn("images.unsplash.com", css)

    def test_reduced_motion_and_responsive_rules_exist(self):
        css = (ROOT / "site/styles.css").read_text()
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("@media (max-width: 720px)", css)

    def test_top_frontier_navigation_lists_all_official_frontiers(self):
        html = (ROOT / "site/index.html").read_text()
        expected = {
            "AF": "加速器前沿",
            "CEF": "社群参与前沿",
            "CompF": "计算前沿",
            "CF": "宇宙前沿",
            "EF": "能量前沿",
            "IF": "仪器学前沿",
            "NF": "中微子前沿",
            "RPF": "稀有过程与精密测量前沿",
            "TF": "理论前沿",
            "UF": "地下设施与基础设施前沿",
        }
        self.assertIn('class="frontier-nav"', html)
        for code, label in expected.items():
            self.assertIn(f'href="?frontier={code}#catalog"', html)
            self.assertIn(f"<b>{code}</b><span>{label}</span>", html)

    def test_paper_surfaces_render_chinese_frontier_labels(self):
        script = (ROOT / "site/app.js").read_text()
        self.assertIn("function formatFrontiers(frontiers, separator)", script)
        self.assertIn('formatFrontiers(paper.frontiers, " · ")', script)
        self.assertIn('formatFrontiers(paper.frontiers, " / ")', script)


if __name__ == "__main__":
    unittest.main()
