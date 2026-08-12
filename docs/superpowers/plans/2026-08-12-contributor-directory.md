# Contributor Directory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the oversized single-person contributor card with a compact, extensible directory row.

**Architecture:** Keep contributor facts in the existing static page, but change the semantic container from an article card to a list. Add contributor-specific CSS so open-call cards and other community pages retain their current design.

**Tech Stack:** Semantic HTML, existing CSS variables, Python unittest, in-app browser visual QA.

## Global Constraints

- List only verified contributors.
- Do not infer identity or expose commit email addresses.
- Keep Chinese/English copy and theme support.
- Add no dependencies.

---

### Task 1: Compact contributor directory

**Files:**
- Modify: `scripts/test_community_pages.py`
- Modify: `scripts/test_site_interface.py`
- Modify: `site/contributors/index.html`
- Modify: `site/styles.css`

**Interfaces:**
- Consumes: existing bilingual `data-zh` / `data-en` controller behavior.
- Produces: `.contributor-list`, `.contributor-row`, `.contributor-name`, `.contributor-role`, and `.contributor-profile` presentation hooks.

- [ ] **Step 1: Write a failing structural test**

Assert that the page contains a semantic contributor list and does not contain `.contributor-card`.

- [ ] **Step 2: Verify the test fails**

Run: `python3 -m unittest scripts.test_community_pages.CommunityPagesTest.test_contributors_are_presented_as_a_compact_directory`

- [ ] **Step 3: Implement the directory row**

Use one `<ul>` containing one `<li>` with name, bilingual role, and a subdued GitHub link. Add responsive CSS that collapses the columns cleanly on narrow screens.

- [ ] **Step 4: Verify locally and visually**

Run the full Python suite, public-tree audit, Node syntax checks, and rendered browser comparison in both themes.

- [ ] **Step 5: Commit and deploy**

Commit with Lore trailers, push to the public `main`, wait for Netlify `ready`, and verify the live `/contributors/` route.
