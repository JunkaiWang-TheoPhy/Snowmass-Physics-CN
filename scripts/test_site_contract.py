"""Static contract tests for bilingual and eye-care UI behavior."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "site/index.html").read_text(encoding="utf-8")
JAVASCRIPT = (ROOT / "site/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "site/styles.css").read_text(encoding="utf-8")


class SiteContractTests(unittest.TestCase):
    def test_every_static_translation_key_has_two_dictionary_entries(self) -> None:
        keys = set(re.findall(r'data-i18n(?:-html|-placeholder|-aria)?="([A-Za-z0-9]+)"', HTML))
        self.assertGreater(len(keys), 40)
        for key in keys:
            self.assertGreaterEqual(
                len(re.findall(rf"\b{re.escape(key)}\s*:", JAVASCRIPT)),
                2,
                key,
            )

    def test_independent_accessible_view_controls_exist(self) -> None:
        self.assertIn('id="language-toggle"', HTML)
        self.assertIn('id="theme-toggle"', HTML)
        self.assertIn('localStorage.getItem("snowmass-theme")', HTML)
        self.assertIn('localStorage.setItem(LANG_KEY', JAVASCRIPT)
        self.assertIn('localStorage.setItem(THEME_KEY', JAVASCRIPT)

    def test_bilingual_titles_are_rendered_and_searchable(self) -> None:
        self.assertIn("paper.title_zh", JAVASCRIPT)
        self.assertIn('"paper-title-zh"', JAVASCRIPT)
        self.assertIn('"detail-title-zh"', JAVASCRIPT)
        self.assertIn('state.lang === "zh"', JAVASCRIPT)

    def test_slate_light_and_dark_tokens_exist(self) -> None:
        self.assertIn("--snow: #dce6ea", CSS)
        self.assertIn(':root[data-theme="dark"]', CSS)
        self.assertIn("--snow: #17242c", CSS)


if __name__ == "__main__":
    unittest.main()
