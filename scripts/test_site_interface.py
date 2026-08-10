from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SiteInterfaceTest(unittest.TestCase):
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
        self.assertIn("没有明确改编许可的全文不会公开", html)

    def test_hero_uses_local_image_and_light_color_scheme(self):
        html = (ROOT / "site/index.html").read_text()
        css = (ROOT / "site/styles.css").read_text()
        self.assertIn('content="light"', html)
        self.assertIn("assets/snowmass-mountain", css)
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
