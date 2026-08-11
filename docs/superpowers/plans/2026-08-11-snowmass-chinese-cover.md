# Snowmass Chinese Cover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate and prepend an auditable Chinese-edition cover page to each rights-cleared Snowmass translation PDF.

**Architecture:** A standalone post-render packager reads the public rights manifest, renders one A4 cover from existing project artwork and fixed collaboration identity, then prepends it without overwriting the BabelDOC input. It emits a JSON receipt containing source and output hashes.

**Tech Stack:** Python 3, PyMuPDF, pypdf, existing SVG/PNG assets, unittest, Poppler visual rendering.

## Global Constraints

- Fail closed unless `publication_allowed` is exactly `true`.
- Never invent a DOI; render `未提供` when absent.
- Preserve the source PDF and create a separate publication PDF.
- Use `Snowmass White Paper Chinese Translation Collaboration`, `WangTheoPhys*`, `snowmass-physics-cn.netlify.app`, and `WangTheoPhys@outlook.com` exactly.
- QR target is `https://snowmass-physics-cn.netlify.app/`.

---

### Task 1: Cover rendering and packaging

**Files:**
- Create: `scripts/package_snowmass_translation_pdf.py`
- Create: `scripts/test_package_snowmass_translation_pdf.py`
- Create: `site/assets/snowmass-site-qr.png`

**Interfaces:**
- Consumes: rights-manifest record, Chinese title, source PDF, version, date.
- Produces: `package_translation_pdf(...) -> dict[str, object]` and a cover-prefixed PDF plus JSON receipt.

- [ ] **Step 1: Write failing tests** for the rights gate, required Chinese title, one-page prepend, visible first-page fields, clickable URI annotations, QR image presence, and receipt hashes.
- [ ] **Step 2: Run tests and confirm RED** because the packager module does not exist.
- [ ] **Step 3: Implement the minimal packager** with fixed collaboration identity, existing mountain SVG, metadata layout, and pypdf prepend.
- [ ] **Step 4: Run focused tests and confirm GREEN.**
- [ ] **Step 5: Run the full relevant Snowmass test suite.**

### Task 2: Pilot artifact and visual QA

**Files:**
- Generate: `output/pdf/snowmass_cover_preview/arxiv_2203.07506.cover.pdf`
- Generate: `output/pdf/snowmass_cover_preview/arxiv_2203.07506.covered.pdf`
- Generate: `output/pdf/snowmass_cover_preview/arxiv_2203.07506.cover-page.png`
- Generate: `output/pdf/snowmass_cover_preview/arxiv_2203.07506.package.json`

**Interfaces:**
- Consumes: the completed Task 1 CLI and the current A/B pilot PDF.
- Produces: a user-reviewable first-page image and packaged PDF.

- [ ] **Step 1: Run the packager** for `arxiv:2203.07506` with a manually verified Chinese title.
- [ ] **Step 2: Render page 1 with Poppler.**
- [ ] **Step 3: Inspect the PNG** for clipping, overlap, typography, link placement, artwork and QR legibility.
- [ ] **Step 4: Revise only if visual QA is below the acceptance threshold, then rerender.**
- [ ] **Step 5: Verify page count, PDF opening, URI annotations and receipt hashes.**
