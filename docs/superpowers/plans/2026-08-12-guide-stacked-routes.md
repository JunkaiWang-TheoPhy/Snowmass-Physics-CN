# Stacked Participation Routes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge translation and review into one route, stack all guide routes vertically, and give each route a restrained visual color identity.

**Architecture:** Retain static bilingual HTML and existing links. Replace four equal grid cards with three modifier-class cards; CSS owns layout and theme-safe color accents without adding scripts or dependencies.

**Tech Stack:** Semantic HTML, CSS variables and `color-mix`, Python unittest, in-app browser visual QA.

## Global Constraints

- Preserve all translation, review, rights, and distribution guidance.
- Keep rights outreach separate from public GitHub collaboration.
- Support Chinese, English, light theme, dark theme, and narrow screens.
- Add no dependencies.

---

### Task 1: Three stacked participation routes

**Files:**
- Modify: `scripts/test_community_pages.py`
- Modify: `scripts/test_site_interface.py`
- Modify: `site/guide/index.html`
- Modify: `site/styles.css`

**Interfaces:**
- Consumes: existing `data-zh` / `data-en` localization behavior.
- Produces: three `.participation-card` elements with translation, rights, and outreach modifier classes.

- [ ] **Step 1: Add failing structure and styling tests**

Assert three cards, sequential 01–03 numbering, merged translation/review title, three color modifier hooks, and one-column layout.

- [ ] **Step 2: Verify the tests fail**

Run the two focused unittest methods and confirm the current four-card grid violates the new contract.

- [ ] **Step 3: Merge copy and implement vertical layout**

Move all review bullets and actions into route 01, renumber the other routes, and set `.participation-grid` to one column.

- [ ] **Step 4: Add accessible color differentiation**

Use glacier, amber, and pine accents with matching subtle backgrounds. Retain route numbers and headings so meaning does not depend on color.

- [ ] **Step 5: Verify, commit, and deploy**

Run all tests and audits, render desktop and mobile light/dark previews, commit with Lore trailers, push public `main`, wait for Netlify `ready`, and verify live HTML.
