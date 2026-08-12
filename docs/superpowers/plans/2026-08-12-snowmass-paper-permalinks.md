# Snowmass Paper Permalinks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every catalog record a stable `/paper/<arXiv-id>/` page and make the Chinese-edition cover link and QR target that page.

**Architecture:** Netlify rewrites permanent paths to the existing data-driven application while preserving the public URL. The browser derives a canonical `record_id` from the pathname and renders the existing detail component from `papers.json`; cover tooling derives the same URL from record metadata.

**Tech Stack:** Static HTML/CSS/JavaScript, Netlify redirects, Python/Pillow/PyMuPDF, Python unittest.

## Global Constraints

- `site/data/papers.json` remains the only paper metadata source.
- Permanent URL format is `/paper/<arXiv-id>/`.
- Legacy `?paper=arxiv:<id>` URLs remain readable.
- Invalid IDs fail visibly rather than returning the catalog silently.
- Cover field, QR, and PDF links must share the same paper URL.

---

### Task 1: Permanent paper route

**Files:**
- Modify: `scripts/test_site_interface.py`
- Modify: `site/index.html`
- Modify: `site/app.js`
- Modify: `netlify.toml`

**Interfaces:**
- Produces: `paperPath(recordId) -> string` and `recordIdFromLocation(location) -> string | null`.

- [ ] Add failing assertions for a 200 rewrite, root-relative assets, route parsing hooks, canonical links, and legacy-query support.
- [ ] Run `python3 -m unittest scripts.test_site_interface -v` and confirm the new assertions fail.
- [ ] Implement root-relative assets, permanent link generation/path parsing, explicit missing-record rendering, and the Netlify rewrite.
- [ ] Run the site interface tests and the full Python test suite.

### Task 2: Cover permanent URL

**Files:**
- Modify: `tmp/pdfs/render_cover_variants_b3_luxe.py`
- Modify: `tmp/pdfs/build_b3_2_interactive_preview.py`
- Modify: `docs/superpowers/specs/2026-08-11-snowmass-chinese-cover-design.md`

**Interfaces:**
- Consumes: arXiv record ID `arxiv:2203.07506`.
- Produces: `https://snowmass-physics-cn.netlify.app/paper/2203.07506/` in field 06, QR, and PDF links.

- [ ] Add a failing artifact-level check for the translation-page label and permanent URL.
- [ ] Update the preview renderer and interactive PDF link rectangles to use one shared paper URL.
- [ ] Regenerate PNG and PDF, decode/inspect the QR and enumerate PDF URI annotations.
- [ ] Run visual verdict and persist the result.

### Task 3: End-to-end verification

**Files:**
- Verify: `site/`, `netlify.toml`, generated preview artifacts.

- [ ] Serve the site with a rewrite-aware local test server and request `/paper/2203.07506/`.
- [ ] Run all repository unit tests relevant to site and cover packaging.
- [ ] Inspect the final cover at original resolution and confirm layout remains legible.
- [ ] Review the scoped diff and report deployment as a separate external action if not performed.
