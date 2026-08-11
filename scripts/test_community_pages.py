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
        self.assertNotIn("1181100960@qq.com", html)
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
