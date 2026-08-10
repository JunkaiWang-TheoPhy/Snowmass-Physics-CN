# Snowfield Site Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the existing dark fluorescent interface with the approved bright Snowmass visual system while preserving all catalog, rights-gate, filtering, sorting, pagination, and detail behavior.

**Architecture:** Keep the current static HTML/CSS/JavaScript architecture and data contract. Treat `site/index.html` as semantic content, `site/styles.css` as the complete responsive design layer, and `site/app.js` as the existing catalog behavior; only add markup hooks needed for the new hero and Frontier entry section.

**Tech Stack:** Semantic HTML5, CSS custom properties and responsive layout, vanilla ES modules, Python `unittest`, Netlify static hosting.

## Global Constraints

- Do not add a runtime dependency or external font dependency.
- The public rights gate must continue to derive from `site/data/papers.json`; never hard-code the live eligible count.
- Preserve the existing query parameter contract for search, filters, sorting, pagination, and paper detail.
- Keep the project’s non-official notice and per-paper copyright/authorization messaging prominent.
- Use a local optimized image asset with source and license metadata before production deployment; no third-party image hotlink.
- Meet WCAG AA contrast, retain visible keyboard focus, and never communicate status through color alone.
- Respect `prefers-reduced-motion` and avoid autoplay media or parallax.

---

### Task 1: Lock the public interface contract

**Files:**
- Create: `scripts/test_site_interface.py`
- Test: `scripts/test_site_interface.py`

**Interfaces:**
- Consumes: `site/index.html`, `site/styles.css`, and `site/app.js` as text fixtures.
- Produces: regression checks for required DOM IDs, URL-state fields, rights copy, responsive breakpoints, and local hero assets.

- [ ] **Step 1: Write the regression test**

```python
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class SiteInterfaceTest(unittest.TestCase):
    def test_catalog_hooks_and_rights_language_remain(self):
        html = (ROOT / "site/index.html").read_text()
        for hook in ("stat-catalog", "stat-cleared", "stat-permission", "stat-pages", "filters", "paper-grid", "pagination", "detail-panel"):
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
```

- [ ] **Step 2: Run the test and verify that the new visual assertions fail**

Run: `python -m unittest scripts.test_site_interface -v`

Expected: catalog hook test passes; light color scheme/local image/reduced-motion assertions fail.

- [ ] **Step 3: Commit the contract test**

```bash
git add scripts/test_site_interface.py
git commit -m "Protect the catalog contract during the visual redesign"
```

### Task 2: Build the Snowfield homepage shell

**Files:**
- Modify: `site/index.html`
- Modify: `site/styles.css`
- Create: `site/assets/snowmass-mountain.svg`
- Create: `site/assets/IMAGE_CREDITS.md`
- Test: `scripts/test_site_interface.py`

**Interfaces:**
- Consumes: existing DOM IDs used by `site/app.js` and the palette in `docs/DESIGN_SYSTEM.md`.
- Produces: `.hero-landscape`, `.hero-copy`, `.rights-principle`, `.metrics`, and `.protocol-strip` styled with local imagery.

- [ ] **Step 1: Add the local hero artwork and its provenance**

Create `site/assets/snowmass-mountain.svg` as an original project-owned vector illustration using layered pale sky, mountain silhouettes, snowcaps, and contour paths. Record it in `site/assets/IMAGE_CREDITS.md` as “Original vector illustration created for Snowmass-Physics-CN; project code/artwork license AGPL-3.0; no third-party source.”

- [ ] **Step 2: Update semantic homepage copy and structure**

Set `<meta name="color-scheme" content="light">`, retain all current metric IDs, and use the approved heading “翻越语言的雪线，抵达开放知识。” Add the class `hero-landscape` to the hero, retain the rights principle inside it, and keep the explicit sentence “没有明确改编许可的全文不会公开”。

- [ ] **Step 3: Replace the dark palette and header/hero/metric styles**

Define `--snow`, `--ice`, `--ink`, `--glacier`, `--pine`, `--amber`, and `--red`. Reference the local asset with `url("assets/snowmass-mountain.svg")`; layer a readable pale gradient over it. Use serif display typography for headings and system sans/monospace stacks for interface metadata.

- [ ] **Step 4: Run the interface test**

Run: `python -m unittest scripts.test_site_interface -v`

Expected: all tests pass.

- [ ] **Step 5: Commit the homepage shell**

```bash
git add site/index.html site/styles.css site/assets/snowmass-mountain.svg site/assets/IMAGE_CREDITS.md scripts/test_site_interface.py
git commit -m "Give the project a bright Snowmass discovery surface"
```

### Task 3: Adapt catalog and archive surfaces

**Files:**
- Modify: `site/styles.css`
- Test: `scripts/test_site_interface.py`

**Interfaces:**
- Consumes: existing `.filters`, `.paper-card`, `.badge-*`, `.detail-*`, `.definition-list`, and `.pagination` markup emitted by `site/app.js`.
- Produces: light catalog cards and a warm-white archive treatment without changing JavaScript behavior.

- [ ] **Step 1: Restyle search, filters, and paper cards**

Use white cards on `--snow`, fine `--line` borders, subtle lift on hover, and 44px minimum interactive heights. Preserve distinct textual status badges using pine, amber, and red foreground/background pairs.

- [ ] **Step 2: Apply the Polar Archive language to details**

Give `.detail-panel` and `.detail-block` a warm white surface, editorial serif title, compact archival metadata, and highly visible source/license action buttons. Keep the existing two-column desktop and single-column mobile content order.

- [ ] **Step 3: Add reduced-motion and high-contrast focus handling**

Use `@media (prefers-reduced-motion: reduce)` to disable smooth scrolling and transitions. Style `:focus-visible` with a dark glacier outline that meets contrast requirements on white and ice surfaces.

- [ ] **Step 4: Run behavior and interface regression tests**

Run: `python -m unittest scripts.test_site_interface scripts.test_public_manifest -v`

Expected: all tests pass.

- [ ] **Step 5: Commit the catalog/archive treatment**

```bash
git add site/styles.css scripts/test_site_interface.py
git commit -m "Make catalog and rights records read like a polar archive"
```

### Task 4: Verify responsive rendering and production output

**Files:**
- Modify if required: `site/styles.css`
- Modify if required: `site/index.html`
- Test: `scripts/test_site_interface.py`

**Interfaces:**
- Consumes: the completed static site.
- Produces: verified desktop and mobile screenshots plus a clean public-tree check.

- [ ] **Step 1: Run the full repository checks**

Run: `python -m unittest discover -s scripts -p 'test_*.py' -v`

Expected: all discovered tests pass.

- [ ] **Step 2: Verify the public tree**

Run: `python scripts/check_public_tree.py`

Expected: success with no blocked translation outputs or private artifacts in the public site.

- [ ] **Step 3: Serve and inspect desktop/mobile views**

Run: `python -m http.server 4173 -d site`

Inspect at 1440×1000 and 390×844. Confirm the hero image/copy remain legible, navigation does not overflow, filters are usable, cards collapse to one column, detail view returns correctly, and the page has no horizontal scrollbar.

- [ ] **Step 4: Run final whitespace and status checks**

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only intended tracked files and the user’s pre-existing untracked translation scripts remain.

- [ ] **Step 5: Commit verification fixes if any were required**

```bash
git add site/index.html site/styles.css scripts/test_site_interface.py
git commit -m "Polish the snowfield experience across screen sizes"
```
