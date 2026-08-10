# Public Contact Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display the project email directly below the homepage title as a clickable contact link.

**Architecture:** Add one semantic paragraph and `mailto:` link to the static hero. Explicitly allow only this project-owned public address in the repository audit so author-contact privacy remains fail-closed.

**Tech Stack:** Semantic HTML, CSS, Python standard library, GitHub Actions.

## Global Constraints

- Display `WangTheoPhys@outlook.com` below the homepage title, not in the footer.
- Do not add the address to paper records, authorization contacts, or private workflow data.
- Keep the repository-wide email scan blocking every address except this exact project contact.

---

### Task 1: Add and verify the project contact

**Files:**
- Modify: `site/index.html`
- Modify: `site/styles.css`
- Modify: `scripts/check_public_tree.py`
- Modify: `.github/workflows/validate-public-site.yml`

**Interfaces:**
- Produces: a visible `mailto:WangTheoPhys@outlook.com` link below `#hero-title`.
- Preserves: fail-closed scanning of all non-allowlisted email addresses.

- [x] **Step 1: Add the semantic contact link and compact hero styling**

  Insert `<p class="hero-contact">项目联系：<a href="mailto:WangTheoPhys@outlook.com">WangTheoPhys@outlook.com</a></p>` immediately after the homepage `h1`.

- [x] **Step 2: Narrowly allowlist the public project email**

  Add the lowercase address to `PUBLIC_EMAIL_ALLOWLIST` and continue reporting any email regex match not in that set.

- [x] **Step 3: Strengthen the deployment smoke test**

  Require the fetched homepage to contain `mailto:WangTheoPhys@outlook.com`.

- [x] **Step 4: Verify and commit**

  Run the manifest tests, public-tree audit, JavaScript syntax check, static HTTP check, and `git diff --check`; then commit and push to public `main`.
