# Bilingual Eye-Care Site Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the Slate Mist light/dark site with complete Chinese/English UI switching and bilingual titles for all 541 papers.

**Architecture:** Keep English source titles canonical, merge a reviewed machine-title mapping during public-manifest generation, and localize the static client through one translation dictionary. Apply theme and language state at the document root, persist explicit choices locally, and synchronize language with the query string.

**Tech Stack:** Python 3 standard library, static JSON, semantic HTML, vanilla JavaScript, CSS custom properties, GitHub Actions, Netlify.

## Global Constraints

- Do not change rights decisions or the publication gate.
- Do not replace English source titles; Chinese titles are secondary machine-draft metadata.
- Do not introduce runtime dependencies or a backend.
- Keep language and theme as two independent controls in the top-right header.

---

### Task 1: Machine-title data contract

**Files:**
- Create: `data/snowmass_title_zh.json`
- Modify: `scripts/build_public_manifest.py`
- Modify: `scripts/test_public_manifest.py`
- Modify: `site/data/papers.json`

- [ ] Add failing coverage requiring `title_zh`, `title_zh_status`, and `title_zh_model` on all 541 records.
- [ ] Generate a complete ID-keyed Chinese-title mapping with the configured machine model.
- [ ] Merge the mapping deterministically and reject missing, extra, or duplicate identities.
- [ ] Regenerate public data and prove source titles and rights fields are unchanged.

### Task 2: Independent language and theme controls

**Files:**
- Modify: `site/index.html`
- Modify: `site/app.js`
- Modify: `site/styles.css`

- [ ] Add accessible language and theme buttons to the header plus pre-paint theme initialization.
- [ ] Add a complete Chinese/English UI dictionary and rerender visible state on language change.
- [ ] Persist explicit choices, honor system/browser defaults, and synchronize `?lang=`.
- [ ] Render bilingual paper cards/details in Chinese mode and English-only titles in English mode.

### Task 3: Slate Mist visual system and verification

**Files:**
- Modify: `site/styles.css`
- Modify: `.github/workflows/validate-public-site.yml`
- Modify: `README.md`

- [ ] Replace pure-bright surfaces with the approved fog-blue palette and add midnight-blue overrides.
- [ ] Verify responsive controls, keyboard focus, reduced motion, search in both titles, and no theme flash.
- [ ] Run unit, privacy, syntax, static HTTP, and visual checks in both modes.
- [ ] Push public `main`, verify GitHub Actions and Netlify, and inspect the production URL.
