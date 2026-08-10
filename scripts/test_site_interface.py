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


if __name__ == "__main__":
    unittest.main()
