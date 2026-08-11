import json
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

    def test_progress_uses_public_manifest_without_fixed_counts(self):
        html = self.read("progress/index.html")
        js = self.read("community.js")
        self.assertIn("../data/papers.json", html)
        self.assertIn("summarizePapers", js)
        for fixed in ("541", "273", "268"):
            self.assertNotIn(fixed, html)
            self.assertNotIn(fixed, js)

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

    def test_contributors_does_not_publish_commit_email(self):
        html = self.read("contributors/index.html")
        self.assertIn("JunkaiWang-TheoPhy", html)
        self.assertIn("项目发起人 / 维护者", html)
        self.assertNotIn("1181100960@qq.com", html)
        self.assertIn("开放申请", html)

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

    def test_controller_supports_direct_language_theme_safe_rendering_and_retry(self):
        js = self.read("community.js")
        self.assertIn('new URLSearchParams(location.search).get("lang")', js)
        self.assertIn('localStorage.setItem(LANG_KEY, lang)', js)
        self.assertIn('localStorage.setItem(THEME_KEY, next)', js)
        self.assertIn('response.headers.get("Last-Modified")', js)
        self.assertIn('style.setProperty("--allowed-angle"', js)
        self.assertIn("createElement", js)
        self.assertIn("textContent", js)
        self.assertNotIn("innerHTML", js)
        self.assertIn("loadProgress", js)
        self.assertIn("retry", js)

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
