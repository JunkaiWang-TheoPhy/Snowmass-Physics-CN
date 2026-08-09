# Snowmass Public Repository and Status Site Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing Snowmass 2021 rights catalog into a public-ready GitHub repository with a static status site, strict publication protocol, reproducible public-manifest generation, and safe Netlify/GitHub deployment scaffolding.

**Architecture:** Keep the existing 541-record research catalog as the internal source of truth, generate a deliberately redacted `site/data/papers.json`, and render the public catalog entirely in the browser with vanilla HTML/CSS/JavaScript. Keep authorization contacts, email text, and evidence out of the public tree; only protocol and schema documentation are public. Netlify publishes `site/` without a build step, while CI validates data leakage and site links on every push.

**Tech Stack:** Python 3 standard library for deterministic manifest generation and validation; semantic HTML; vanilla JavaScript modules; CSS; GitHub Actions; Netlify static hosting.

## Global Constraints

- The current deduplicated working catalog contains 541 records; the official Snowmass index's 548 count includes cross-Frontier duplication.
- `publication_allowed=true` is allowed only when the source license clearly permits adaptation or a scoped written permission record exists; public JSON cannot grant permission by editing itself.
- arXiv non-exclusive distribution and HAL Authorization are distribution permissions, not third-party translation/adaptation permissions.
- CC BY-ND and CC BY-NC-ND records cannot receive a public full-translation status without separate rights-holder permission.
- Public artifacts must not contain private email addresses, mail bodies, private notes, credentials, downloaded original PDFs, or unredacted authorization attachments.
- Every public paper record must retain its source URL, source license, source-license URL, translation status, authorization status, and publication basis.
- The root AGPL-3.0 license applies to project code/docs only; each third-party paper and translation remains governed by its per-record rights metadata.
- No new runtime dependency is required for the first public site; the site must work from a static file server and Netlify.

---

### Task 1: Build and validate the redacted public manifest

**Files:**
- Create: `scripts/build_public_manifest.py`
- Create: `scripts/test_public_manifest.py`
- Create: `site/data/papers.json`
- Create: `site/data/stats.json`
- Read: `output/snowmass2021/rights/snowmass2021_rights_manifest.json`
- Read: `output/snowmass2021/snowmass2021_whitepapers.json`
- Read: `output/snowmass2021/analysis/enriched_papers.json`
- Read: `output/snowmass2021/analysis/length_records.json`

**Interfaces:**
- `build_public_manifest.py` accepts `--rights`, `--catalog`, `--analysis`, `--lengths`, `--out-dir`; all paths default to the existing repository paths and `site/data`.
- It emits one deterministic record per `record_id`, with fields `paper_id`, `record_id`, `title`, `authors_as_listed`, `frontiers`, `topics`, `source_url`, `source_version`, `source_license`, `source_license_url`, `permits_adaptation`, `license_decision`, `translation_status`, `translation_license`, `human_reviewers`, `authorization_status`, `publication_allowed`, `publication_basis`, `publication_conditions`, `publication_translation_url`, `public_updated_at`, `publication_year`, `citation_count`, `citation_count_without_self_citations`, `citations_per_year`, `impact_proxy_score_0_100`, `page_count`, `unicode_token_count`, `frontier_labels`, and `primary_arxiv_category`.
- It derives authorization state from the rights decision: adaptation-permitting sources become `license-cleared`; non-exclusive/ambiguous/ND sources become `needs-permission`; publication is never enabled for a held or denied record.
- `test_public_manifest.py` exposes `test_record_count`, `test_required_fields`, `test_no_private_data`, `test_publication_gate`, and `test_deterministic_order` using `unittest`.

- [x] **Step 1: Write the failing manifest tests**

  Assert 541 records, unique `record_id` values, required keys, no email/credential-like strings, and that any `publication_allowed=true` record has `publication_basis` equal to `source-license` or `permission-granted`.

- [x] **Step 2: Run the tests to verify the new generator is absent**

  Run: `python3 -m unittest scripts.test_public_manifest -v`

  Expected: FAIL because `site/data/papers.json` and the generator do not exist yet.

- [x] **Step 3: Implement deterministic generation**

  Load the four source datasets, join on lowercase `record_id`, map license decisions to public authorization states, normalize missing values to `null`, strip internal paths and evidence fields, sort by title then `record_id`, and write UTF-8 JSON with stable indentation and a generated metadata object in `stats.json`.

- [x] **Step 4: Run the tests and inspect generated counts**

  Run: `python3 scripts/build_public_manifest.py && python3 -m unittest scripts.test_public_manifest -v`

  Expected: 541 records; no private-data failure; stats include catalog, license, translation, authorization, Frontier, year, page, citation, and token totals.

- [x] **Step 5: Commit the data pipeline**

  Run:

  ```bash
  git add scripts/build_public_manifest.py scripts/test_public_manifest.py site/data/papers.json site/data/stats.json
  git commit -m "Publish a redacted Snowmass status manifest"
  ```

### Task 2: Build the public status website

**Files:**
- Create: `site/index.html`
- Create: `site/app.js`
- Create: `site/styles.css`
- Create: `site/404.html`

**Interfaces:**
- `app.js` loads `data/papers.json` and `data/stats.json`, renders dashboard statistics and paper cards, and preserves filters in the query string.
- The public URL shape is `/?paper=<record_id>` for a detail view and `/` for the catalog view; paper IDs are escaped before insertion into HTML.
- Search covers title, author string, paper ID, Frontier, and topic; filters cover license, authorization state, translation state, publication gate, year, and Frontier.
- The UI always labels the project as an unofficial community translation and links to the original source before any translation link.

- [x] **Step 1: Add the semantic page shell**

  Create an accessible page with a header, project notice, dashboard cards, filter form, results region, detail region, and footer containing official Snowmass/arXiv/HAL links and the per-paper-rights disclaimer.

- [x] **Step 2: Add the responsive visual system**

  Use a dark editorial palette, high-contrast text, visible keyboard focus, fluid cards, mobile single-column layout, and no external fonts or images. Include status badges with text labels so color is never the only signal.

- [x] **Step 3: Add catalog and detail behavior**

  Implement rendering, filter composition, sort options (`title`, `year`, `citations`, `pages`), pagination, empty states, URL state, and a detail panel showing source license, authorization decision, publication basis, metrics, and translation status.

- [x] **Step 4: Run a static smoke test**

  Run: `python3 -m http.server 4173 --directory site` and use `curl -fsS http://127.0.0.1:4173/ | rg "Snowmass|translation"` plus `curl -fsS http://127.0.0.1:4173/data/papers.json | python3 -m json.tool >/dev/null`.

- [x] **Step 5: Commit the site**

  Run:

  ```bash
  git add site/index.html site/app.js site/styles.css site/404.html
  git commit -m "Add the public Snowmass translation catalog"
  ```

### Task 3: Publish the strict rights and contribution protocol

**Files:**
- Modify: `README.md`
- Create: `CONTRIBUTING.md`
- Create: `docs/RIGHTS_PROTOCOL.md`
- Create: `docs/PUBLIC_DATA_MODEL.md`
- Create: `private/README.md`
- Create: `private/schema.sql`
- Create: `.env.example`

**Interfaces:**
- `docs/RIGHTS_PROTOCOL.md` is the normative public protocol for source-license review, translation states, authorization requests, publication gates, evidence retention, takedowns, and ambiguous responses.
- `private/schema.sql` defines `papers`, `contacts`, `authorization_requests`, and `authorization_events` with no seed contact data; private data must never be committed.
- `CONTRIBUTING.md` defines a PR checklist requiring rights metadata, source links, translation license compatibility, attribution, and no original-PDF mirroring.
- `README.md` explains the project, current 541-record scope, the deployed-site URL slot, data-generation commands, rights caveat, and project-vs-third-party license split.

- [x] **Step 1: Write the normative rights protocol**

  Define the allowed license mapping, required manifest fields, state transitions, human approval gate, email request scope, reply classification, public data redaction rules, takedown process, and prohibited actions. Include the official arXiv translation policy and HAL authorization links.

- [x] **Step 2: Add the public schema and contributor checklist**

  Document every public field, permitted values, provenance rules, and a PR checklist that rejects unlicensed full translations, private contact data, and unsupported permission claims.

- [x] **Step 3: Add the private backend boundary**

  Add SQL DDL with restrictive comments and `private/.gitignore` so contact emails, mail bodies, and evidence are intentionally kept outside Git. Add `.env.example` containing only variable names for future Supabase/email adapters.

- [x] **Step 4: Update the root README**

  Make the project read as an open-source Snowmass translation/status project rather than a generic writing repository. Include quick start, site development, data refresh, rights policy, contribution workflow, and citation/source links.

- [x] **Step 5: Commit the protocol docs**

  Run:

  ```bash
  git add README.md CONTRIBUTING.md docs/RIGHTS_PROTOCOL.md docs/PUBLIC_DATA_MODEL.md private .env.example
  git commit -m "Document the Snowmass rights and contribution protocol"
  ```

### Task 4: Add hosting and continuous validation scaffolding

**Files:**
- Create: `netlify.toml`
- Create: `.github/workflows/validate-public-site.yml`
- Modify: `.gitignore`

**Interfaces:**
- Netlify publishes `site/` directly, with no server-side secrets and no original PDFs.
- CI runs manifest generation, unit tests, JSON validation, private-data scans, and a local HTTP smoke test on pushes and pull requests.
- `.gitignore` ignores private environment files, caches, downloaded PDFs, and local backend state while retaining the generated public manifest.

- [x] **Step 1: Add Netlify configuration**

  Set `publish = "site"`, define security headers for a static informational site, and add a redirect preserving `/paper` query URLs without exposing filesystem paths.

- [x] **Step 2: Add GitHub Actions validation**

  Use the repository's Python 3 runtime, run the generator and tests, validate JSON, scan tracked public files for email/key patterns, start `python3 -m http.server` in the background, and curl the homepage/data endpoint before stopping the server.

- [x] **Step 3: Run local CI-equivalent commands**

  Run:

  ```bash
  python3 -m unittest scripts.test_public_manifest -v
  python3 -m json.tool site/data/papers.json >/dev/null
  python3 -m json.tool site/data/stats.json >/dev/null
  git diff --check
  ```

- [x] **Step 4: Commit hosting and CI**

  Run:

  ```bash
  git add netlify.toml .github/workflows/validate-public-site.yml .gitignore
  git commit -m "Add static hosting and public-data CI checks"
  ```

### Task 5: Verify repository readiness and publish when credentials permit

**Files:**
- Modify: none unless verification discovers a defect.
- External: GitHub repository visibility and remote URL; Netlify site configuration only if authenticated.

**Interfaces:**
- The final repository must have a public-safe default branch, a documented deployment path, and no private contact data in tracked files.
- GitHub creation/push uses the authenticated GitHub CLI or an explicitly provided remote; no token is written to Git config, files, logs, or URLs.

- [x] **Step 1: Audit tracked content**

  Run `git ls-files -z | xargs -0 rg -n -I -e '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' -e 'sk-[A-Za-z0-9]'` and verify no matches outside protocol examples.

- [x] **Step 2: Run the full validation suite**

  Run `python3 -m unittest scripts.test_public_manifest -v`, JSON validation, `git diff --check`, and the static HTTP smoke test. Record the 541-record count and the redaction result.

- [ ] **Step 3: Check GitHub authentication and repository state**

  Run `gh auth status` and `gh repo view`; if authentication is valid, create or update the public repository without overwriting unrelated history, push the verified default branch, and verify the public URL. If authentication remains invalid, leave the exact local remote and required re-authentication command in the handoff without exposing credentials.

  Current result: GitHub CLI reports the stored token for `JunkaiWang-TheoPhy` is invalid. No repository creation or push has been attempted with an unauthenticated client. Netlify production deployment succeeded independently at `https://snowmass-zh.netlify.app/`.

- [x] **Step 4: Commit the final verification metadata**

  Add no secrets or private evidence. Use the existing Lore commit format for any final correction and report the external publishing blocker separately if credentials are unavailable.

## Verification Matrix

| Requirement | Evidence |
|---|---|
| 541 public records | Generator output and `test_record_count` |
| No private data | Redaction unit test, CI regex scan, tracked-file audit |
| Publication gate | `test_publication_gate` and protocol rules |
| Translation/authorization progress UI | Static smoke test plus manual page interaction |
| Netlify-ready static hosting | `netlify.toml` and successful local HTTP smoke test |
| Contributor safety | `CONTRIBUTING.md` and rights protocol checklist |
| No original-PDF mirroring | `.gitignore`, protocol, and tracked-file audit |
