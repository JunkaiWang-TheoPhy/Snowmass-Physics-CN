# Snowmass PDFMathTranslate-next A/B

This path evaluates the pinned upstream PDF pipeline before it replaces the
project's custom parse/refill/render implementation. It is not production
promotion evidence by itself.

## Runtime lock

- Python: 3.12
- `pdf2zh-next`: 2.9.0
- `babeldoc`: 0.6.2
- Full dependency lock: `requirements/snowmass-pdf2zh-next.lock`
- Isolated runtime: `/Users/Zhuanz/.local/share/snowmass-tools/pdf2zh-next-2.9.0`

Recreate or synchronize the isolated runtime with:

```bash
scripts/install_snowmass_pdf2zh_next.sh
```

The old user-level `pdf2zh` executable is intentionally left untouched.

## Zero-paid preflight

```bash
/Users/Zhuanz/.local/share/snowmass-tools/pdf2zh-next-2.9.0/bin/python \
  scripts/run_snowmass_pdf2zh_next_ab.py --preflight-only
```

Preflight verifies the literal publication rights gate, source/glossary
hashes, exact runtime versions, finite RMB budgets, finite API call cap, and a
conservative peak-price projection before the Keychain credential is read.

## Paid A/B

Run the same command without `--preflight-only` as a `codex-bg` job. The
default sample is `arxiv:2203.07506`, and it renders source pages
`1,2,6,8,11,19-20`. Those pages cover the first page, running headers, Table 1,
Figures 1/2/5, and the start plus continuation of References.

The upstream renderer always runs with `debug=false`; debug mode is forbidden
because BabelDOC embeds diagnostic labels in the PDF. A localhost-only HTTP
proxy holds the real credential and enforces all of the following before a
request reaches DeepSeek:

- model `deepseek-v4-flash`, non-thinking mode;
- at most 125 API calls;
- at most 4,096 output tokens per call;
- at most RMB 10 for the A/B stage;
- at most RMB 1,000 for the project, including the prior project ledger;
- real token settlement per response, with failed calls charged at their
  conservative reservation.

The A/B also sets `ignore_cache=true`, so a layout rerun cannot silently reuse
translations produced by a failed prompt or renderer configuration.

Do not strip the machine's configured egress proxy from the paid command. The
runner preserves that working route for DeepSeek while forcing both
`NO_PROXY` and `no_proxy` to bypass it for `127.0.0.1`/`localhost`; removing the
bypass makes the pdf2zh-next subprocess send its loopback request to the macOS
system proxy.

The runner does not own PDF parsing, structure refill, or rendering. Those
remain inside the pinned upstream package. The child process receives only a
loopback URL and a non-secret placeholder credential; the parent-side proxy is
the sole DeepSeek network and budget boundary.

## Acceptance gate

Compare source, legacy baseline, and upstream output on all seven selected
pages. The A/B passes only if:

- the first page remains one coherent first page;
- every running header is consistent and not injected into body text;
- References numbering, line breaks, URLs, arXiv identifiers, and DOI values
  remain ordered and readable;
- text inside figures and tables remains in the source language;
- captions are translated without altering symbols, numbers, or citations;
- table geometry has no overflow, collisions, or detached cell text;
- no placeholder, clipping, overlap, or out-of-page defect is detected.

Only after all gates pass may the custom paid parse/refill/render entry point
be frozen and the official engine wired into the staged
`deepseek_probe -> pilot5 -> pilot10 -> pilot25 -> batch50 -> remainder`
campaign.

## Verified A/B result

The debug-free A/B completed on `arxiv:2203.07506` with 47 API calls and
42,898 tokens. DeepSeek reported RMB 0.02183848128 of settled usage; the
fail-closed ledger additionally holds RMB 0.062366976 for responses whose
transport did not return usage, for a conservative stage commitment of RMB
0.08420545728.

Prompt instructions alone did not preserve bibliography and table text. The
release-shaped artifact therefore passes through
`scripts/protect_snowmass_pdf2zh_output.py`, which is an auditable post-render
guard rather than an alternative translation renderer. It restores source PDF
vectors for figure XObjects, table layouts, and bibliography bodies; applies
locked running headers and short front-matter strings; and then proves that
every protected clip matches the source text exactly. The generic PDF audit
accepts a verified protection receipt so source-language words intentionally
restored inside a table are not mistaken for detached translation residue.

The second visual verdict scored 93/100 against a threshold of 90. Its source,
protection, geometry, and visual evidence is retained under
`output/snowmass2021/pdf2zh_next_ab_v3/papers/arxiv_2203.07506/`. The selected
page A/B permits the next full-paper `deepseek_probe`; it does not by itself
permit `pilot5`.
