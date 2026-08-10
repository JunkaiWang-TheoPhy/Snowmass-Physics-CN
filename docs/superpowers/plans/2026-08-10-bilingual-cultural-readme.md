# Bilingual Cultural README Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish an elegant bilingual repository landing page built around the Snowmass mountain identity and a concise, rights-aware invitation to collaborate.

**Architecture:** `README.md` remains the GitHub default and links to a parallel `README.en.md`. Both consume one repository-owned banner asset and direct detailed policy questions to existing canonical documents.

**Tech Stack:** GitHub Flavored Markdown, repository-relative WebP asset, existing static-site links.

## Global Constraints

- No new dependencies.
- Preserve rights and non-affiliation boundaries.
- Keep technical operation details outside the landing-page narrative.
- Use only quotations with linked, checked sources.

---

### Task 1: Bilingual editorial landing pages

**Files:**
- Modify: `README.md`
- Create: `README.en.md`
- Create: `site/assets/readme-mountains.webp`
- Modify: `site/assets/IMAGE_CREDITS.md`

**Interfaces:**
- Consumes: existing public-site, contribution, and rights-protocol URLs.
- Produces: reciprocal `README.md` and `README.en.md` navigation.

- [ ] Crop the project-owned mountain panorama into a wide WebP banner.
- [ ] Replace the Chinese README with the approved editorial structure.
- [ ] Add the matching English README and reciprocal language links.
- [ ] Record the banner derivative in the image credits.
- [ ] Verify relative links, asset paths, rights language, and repository checks.
- [ ] Commit with a Lore-format decision record and push to `main`.
