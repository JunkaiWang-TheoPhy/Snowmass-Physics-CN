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
        self.assertIn('rel="icon" href="/favicon.svg"', html)
        self.assertTrue((ROOT / "site/favicon.svg").is_file())
        self.assertIn("assets/snowmass-mountain", css)
        self.assertNotIn("images.unsplash.com", css)

    def test_reduced_motion_and_responsive_rules_exist(self):
        css = (ROOT / "site/styles.css").read_text()
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn("@media (max-width: 720px)", css)

    def test_paper_routes_are_permanent_and_nested_path_safe(self):
        html = (ROOT / "site/index.html").read_text()
        app = (ROOT / "site/app.js").read_text()
        netlify = (ROOT / "netlify.toml").read_text()

        self.assertIn('href="/styles.css"', html)
        self.assertIn('src="/app.js"', html)
        self.assertIn('from = "/paper/*"', netlify)
        self.assertIn('to = "/index.html"', netlify)
        self.assertIn("status = 200", netlify)
        self.assertIn("function paperPath(recordId)", app)
        self.assertIn("function recordIdFromLocation(location)", app)
        self.assertIn('slug.startsWith("cds-")', app)
        self.assertIn('slug.startsWith("hal-")', app)
        self.assertIn('fetch("/data/papers.json")', app)
        self.assertIn('.get("paper")', app)
        self.assertIn('rel="canonical"', html)

    def test_missing_paper_route_has_explicit_state(self):
        app = (ROOT / "site/app.js").read_text()
        self.assertIn("这篇论文尚未收录", app)
        self.assertIn("renderMissingPaper", app)

    def test_published_translation_exposes_versioned_pdf_metadata(self):
        app = (ROOT / "site/app.js").read_text()
        netlify = (ROOT / "netlify.toml").read_text()

        self.assertIn("下载中文试译版 PDF", app)
        self.assertIn("paper.translation_version", app)
        self.assertIn("paper.publication_translation_sha256", app)
        self.assertIn("paper.publication_allowed === true && paper.publication_translation_url", app)
        self.assertNotIn('for = "/pdfs/*"', netlify)


if __name__ == "__main__":
    unittest.main()
