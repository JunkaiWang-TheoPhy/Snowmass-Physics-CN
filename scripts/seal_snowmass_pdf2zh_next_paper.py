#!/usr/bin/env python3
"""Fail-closed, live-artifact seal for one completed pdf2zh-next paper."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

try:
    import audit_snowmass_translation_pdf as structural_audit
    import run_snowmass_pdf2zh_next_ab as adapter
    import snowmass_production_contract as production
    import snowmass_qc_contract as qc_contract
except ModuleNotFoundError:
    from scripts import audit_snowmass_translation_pdf as structural_audit
    from scripts import run_snowmass_pdf2zh_next_ab as adapter
    from scripts import snowmass_production_contract as production
    from scripts import snowmass_qc_contract as qc_contract


SEAL_SCHEMA_VERSION = 1
QC_CONTRACT_VERSION = 1
ROOT = Path(__file__).resolve().parents[1]
SYSTEM_CJK_FONT = Path("/System/Library/Fonts/STHeiti Medium.ttc")
MOUNTAIN_ASSET = ROOT / "site/assets/snowmass-mountain.png"
QR_ASSET = ROOT / "site/assets/snowmass-site-qr.png"
PACKAGING_CONTRACT_VERSION = 4


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _load(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as error:
        raise RuntimeError(f"invalid {label}: {type(error).__name__}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"invalid {label}: expected a JSON object")
    return value


def _relative(article: Path, path: Path, *, label: str) -> str:
    root = Path(article).resolve()
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as error:
        raise RuntimeError(f"{label} escapes article directory") from error


def _require_hash(path: Path, expected: Any, *, label: str) -> str:
    if not Path(path).is_file():
        raise RuntimeError(f"missing {label}")
    actual = _sha256(Path(path))
    if not isinstance(expected, str) or expected != actual:
        raise RuntimeError(f"{label} hash mismatch")
    return actual


def _positive(value: Any, maximum: float, *, label: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0 or parsed > maximum:
        raise RuntimeError(f"{label} must be finite, positive, and <= {maximum}")
    return parsed


def _require_report_fields(
    report: Mapping[str, Any], required: tuple[str, ...], *, label: str
) -> None:
    if report.get("schema_version") != 1 and not (
        label == "protection receipt" and report.get("schema_version") == 2
    ):
        raise RuntimeError(f"{label} schema version mismatch")
    missing = [field for field in required if field not in report]
    if missing:
        raise RuntimeError(f"{label} evidence is incomplete: {missing[0]}")


def _validate_environment(path: Path) -> dict[str, Any]:
    environment = _load(path, label="environment lock")
    expected = environment.get("lock_sha256")
    payload = {key: value for key, value in environment.items() if key != "lock_sha256"}
    if not isinstance(expected, str) or expected != _json_sha256(payload):
        raise RuntimeError("environment lock content mismatch")
    contracts = environment.get("contracts")
    if not isinstance(contracts, dict):
        raise RuntimeError("environment lock contracts are missing")
    if contracts.get("model") != "deepseek-v4-flash":
        raise RuntimeError("environment lock model mismatch")
    if contracts.get("provider") != "deepseek":
        raise RuntimeError("environment lock provider mismatch")
    return environment


def build_current_environment_lock() -> dict[str, Any]:
    """Bind the pinned runtime and every production-facing contract file."""

    versions = adapter.assert_runtime_versions()
    contract_paths = (
        Path(__file__),
        Path(adapter.__file__),
        Path(production.__file__),
        Path(qc_contract.__file__),
        ROOT / "scripts/protect_snowmass_pdf2zh_output.py",
        ROOT / "scripts/audit_snowmass_pdf2zh_semantics.py",
        ROOT / "scripts/audit_snowmass_translation_pdf.py",
        ROOT / "scripts/extract_snowmass_pdf2zh_ir.py",
        ROOT / "scripts/package_snowmass_translation_pdf.py",
        ROOT / "requirements/snowmass-pdf2zh-next.lock",
        ROOT / "translations/snowmass-production-engine.json",
    )
    missing = [path for path in contract_paths if not path.is_file()]
    if missing:
        raise RuntimeError("production contract file is missing: " + missing[0].name)
    secondary_extractor = structural_audit.secondary_extractor_identity()
    return production.build_environment_lock(
        root=ROOT,
        babeldoc_version=versions["babeldoc"],
        ir_version="pdf2zh-next-official-ir-v1",
        model=adapter.MODEL,
        provider="deepseek",
        pricing_contract=adapter.conservative_pricing(),
        execution_binding={
            "protocol": "openai_chat_completions",
            "transport": "localhost_hard_budget_proxy",
            "thinking_mode": "disabled",
        },
        contract_versions={
            "pdf2zh_next_seal": SEAL_SCHEMA_VERSION,
            "qc_receipt": qc_contract.SCHEMA_VERSION,
            "packaging": PACKAGING_CONTRACT_VERSION,
            "secondary_pdf_text_extractor": secondary_extractor,
            "source_sha256": {
                path.relative_to(ROOT).as_posix(): _sha256(path)
                for path in contract_paths
            },
        },
        font_paths=[SYSTEM_CJK_FONT],
        cover_asset_paths=[MOUNTAIN_ASSET, QR_ASSET],
    )


def write_visual_review(
    *,
    output: Path,
    record_id: str,
    source_pdf: Path,
    protected_pdf: Path,
    contact_sheet: Path,
    expected_pages: int,
    reviewer: str,
    review_kind: str,
    verdict: str,
    findings: tuple[str, ...] = (),
) -> dict[str, Any]:
    if expected_pages <= 0:
        raise ValueError("expected_pages must be positive")
    if not reviewer.strip() or not review_kind.strip():
        raise ValueError("visual review attribution must be non-empty")
    if verdict not in {"pass", "fail"}:
        raise ValueError("visual verdict must be pass or fail")
    if verdict == "pass" and findings:
        raise ValueError("a passing visual review cannot contain findings")
    review = {
        "schema_version": 1,
        "record_id": adapter.normalize_record_id(record_id),
        "pdf_sha256": _sha256(Path(protected_pdf)),
        "source_pdf_sha256": _sha256(Path(source_pdf)),
        "contact_sheet_sha256": _sha256(Path(contact_sheet)),
        "expected_pages": expected_pages,
        "coverage": "all-pages",
        "review_kind": review_kind.strip(),
        "reviewer": reviewer.strip(),
        "verdict": verdict,
        "findings": list(findings),
    }
    _atomic_json(Path(output), review)
    return review


def _load_valid_ir_receipt(ir_dir: Path, source_pdf: Path) -> dict[str, Any] | None:
    receipt_path = ir_dir / "receipt.json"
    if not receipt_path.is_file():
        return None
    try:
        receipt = _load(receipt_path, label="IR receipt")
        if receipt.get("zero_paid") is not True:
            return None
        if receipt.get("babeldoc_version") != "0.6.4":
            return None
        if receipt.get("source_pdf_sha256") != _sha256(source_pdf):
            return None
        _require_hash(
            ir_dir / str(receipt.get("ir_json") or ""),
            receipt.get("ir_json_sha256"),
            label="IR JSON",
        )
        _require_hash(
            ir_dir / str(receipt.get("ir_xml") or ""),
            receipt.get("ir_xml_sha256"),
            label="IR XML",
        )
    except RuntimeError:
        return None
    return receipt


def _source_pages_by_xobj_texts(
    ir_document: Mapping[str, Any], source_document: Any
) -> dict[str, list[int]]:
    """Locate figure text while reusing one MuPDF text page per source page."""

    xobj_texts = {
        str(item.get("unicode") or "").strip()
        for page in ir_document.get("page") or []
        for paragraph in page.get("pdf_paragraph") or []
        for item in paragraph.get("pdf_paragraph") or []
        if item.get("xobj_id") and str(item.get("unicode") or "").strip()
    }
    source_page_cache: list[tuple[Any, Any, str, tuple[str, ...]]] = []
    for source_page in source_document:
        textpage = source_page.get_textpage()
        source_page_cache.append(
            (
                source_page,
                textpage,
                "".join(str(textpage.extractTEXT(sort=True)).split()),
                tuple(
                    "".join(str(word[4]).split())
                    for word in textpage.extractWORDS()
                ),
            )
        )

    source_pages_by_text: dict[str, list[int]] = {}
    for text in xobj_texts:
        normalized_text = "".join(text.split())
        for page_number, (source_page, textpage, page_text, words) in enumerate(
            source_page_cache, start=1
        ):
            word_match = any(normalized_text in word for word in words)
            if normalized_text not in page_text and not word_match:
                continue
            if textpage.search(text) or word_match:
                source_pages_by_text.setdefault(text, []).append(page_number)
    return source_pages_by_text


def prepare_paper_qc(
    *,
    article: Path,
    source_pdf: Path,
    raw_pdf: Path,
    glossary_csv: Path,
    selected_source_pages: tuple[int, ...],
    allowed_untranslated: tuple[str, ...] = (),
    allowed_untranslated_phrases: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Run every deterministic QC step and stop before visual attestation."""

    try:
        from audit_snowmass_pdf2zh_semantics import audit_semantics
        from audit_snowmass_translation_pdf import audit_pdf
        from extract_snowmass_pdf2zh_ir import extract_ir
        from protect_snowmass_pdf2zh_output import protect_pdf
    except ModuleNotFoundError:
        from scripts.audit_snowmass_pdf2zh_semantics import audit_semantics
        from scripts.audit_snowmass_translation_pdf import audit_pdf
        from scripts.extract_snowmass_pdf2zh_ir import extract_ir
        from scripts.protect_snowmass_pdf2zh_output import protect_pdf

    article = Path(article).resolve()
    source_pdf = Path(source_pdf).resolve()
    raw_pdf = Path(raw_pdf).resolve()
    glossary_csv = Path(glossary_csv).resolve()
    if not selected_source_pages or selected_source_pages[0] != 1:
        raise RuntimeError("production QC requires a full paper beginning at page 1")
    if tuple(sorted(set(selected_source_pages))) != selected_source_pages:
        raise RuntimeError("selected source pages must be unique and sorted")
    ir_dir = article / "ir"
    ir_receipt = _load_valid_ir_receipt(ir_dir, source_pdf)
    if ir_receipt is None:
        ir_receipt = extract_ir(source_pdf, ir_dir)
    ir_xml = ir_dir / str(ir_receipt["ir_xml"])
    ir_json = ir_dir / str(ir_receipt["ir_json"])
    ir_document = _load(ir_json, label="IR JSON")
    import fitz

    with fitz.open(source_pdf) as source_document:
        source_pages_by_text = _source_pages_by_xobj_texts(
            ir_document, source_document
        )
    verbatim_texts: list[tuple[int, str]] = []
    output_placeholder_repairs: list[tuple[int, str, str]] = []
    with fitz.open(raw_pdf) as translated_document:
        placeholder_pattern = re.compile(r"\{v\d+\}(?:::\{v\d+\})?")
        for page_index, page in enumerate(ir_document.get("page") or [], start=1):
            for paragraph in page.get("pdf_paragraph") or []:
                text = str(paragraph.get("unicode") or "").strip()
                if not text or ("[" not in text and "::" not in text):
                    continue
                candidates = source_pages_by_text.get(text, [])
                for source_page in candidates:
                    if source_page not in selected_source_pages:
                        continue
                    output_index = selected_source_pages.index(source_page)
                    output_text = translated_document[output_index].get_text()
                    if text in output_text and not placeholder_pattern.search(output_text):
                        continue
                    match = placeholder_pattern.search(output_text)
                    if match:
                        output_placeholder_repairs.append(
                            (source_page, match.group(0), text)
                        )
                        break
    for page_index, page in enumerate(ir_document.get("page") or [], start=1):
        for paragraph in page.get("pdf_paragraph") or []:
            xobj_id = int(paragraph.get("xobj_id") or 0)
            if xobj_id == 0:
                continue
            text = str(paragraph.get("unicode") or "").strip()
            if text:
                candidates = source_pages_by_text.get(text, [])
                source_page = page_index if page_index in candidates else (
                    candidates[0] if candidates else page_index
                )
                # Same-page xobject text is covered by the complete raster
                # region.  Keep only text whose source occurrence proves that
                # the IR attached it to a different page (a known BabelDOC
                # cross-page ownership artifact such as a prose IPv6 token).
                if source_page != page_index and not (
                    len(candidates) > 1
                    and ("[" in text or "]" in text or "::" in text)
                ):
                    verbatim_texts.append((source_page, text))

    protected_dir = article / "protected"
    protected_pdf = protected_dir / f"{raw_pdf.stem}.protected.pdf"
    qc_dir = article / "qc"
    protection_path = qc_dir / "protection.json"
    auto_header = len(selected_source_pages) > 2
    protection_receipt = protect_pdf(
        source_pdf=source_pdf,
        translated_pdf=raw_pdf,
        output_pdf=protected_pdf,
        selected_source_pages=selected_source_pages,
        ir_xml=ir_xml,
        auto_header=auto_header,
        auto_front_matter=True,
        auto_header_min_recurrence=2,
        verbatim_texts=tuple(verbatim_texts),
        output_replacements=tuple(output_placeholder_repairs),
    )
    _atomic_json(protection_path, protection_receipt)
    if protection_receipt.get("verified") is not True:
        raise RuntimeError("deterministic PDF protection failed")

    semantic_path = qc_dir / "semantic-report.json"
    semantic_report = audit_semantics(
        protected_pdf,
        glossary_csv=glossary_csv,
        protection_receipt=protection_path,
        allowed_untranslated=allowed_untranslated,
        allowed_untranslated_phrases=allowed_untranslated_phrases,
    )
    _atomic_json(semantic_path, semantic_report)
    if semantic_report.get("ok") is not True:
        raise RuntimeError("semantic PDF audit failed")

    ignored: dict[int, list[tuple[float, float, float, float]]] = {}
    for region in protection_receipt.get("protected_regions", []):
        page = int(region["output_page"])
        ignored.setdefault(page, []).append(tuple(map(float, region["bbox"])))
    structural_path = qc_dir / "structural-report.json"
    contact_sheet = qc_dir / "contact-sheet.jpg"
    structural_report = audit_pdf(
        protected_pdf,
        source_pdf=source_pdf,
        expected_pages=len(selected_source_pages),
        contact_sheet_path=contact_sheet,
        ignored_text_regions=ignored,
        protection_receipt_sha256=_sha256(protection_path),
    )
    _atomic_json(structural_path, structural_report)
    if structural_report.get("ok") is not True:
        raise RuntimeError("structural PDF audit failed")

    review_request = {
        "schema_version": 1,
        "status": "awaiting_all_pages_visual_review",
        "source_pdf_sha256": _sha256(source_pdf),
        "protected_pdf_sha256": _sha256(protected_pdf),
        "contact_sheet_sha256": _sha256(contact_sheet),
        "expected_pages": len(selected_source_pages),
        "required_coverage": "all-pages",
    }
    review_request_path = qc_dir / "visual-review-request.json"
    _atomic_json(review_request_path, review_request)
    return {
        "status": "awaiting_visual_review",
        "protected_pdf": protected_pdf,
        "ir_receipt": ir_dir / "receipt.json",
        "protection": protection_path,
        "semantic": semantic_path,
        "structural": structural_path,
        "contact_sheet": contact_sheet,
        "visual_review_request": review_request_path,
    }


def _validate_visual_review(
    path: Path,
    *,
    record_id: str,
    source_pdf: Path,
    protected_pdf: Path,
    contact_sheet: Path,
    expected_pages: int,
) -> dict[str, Any]:
    review = _load(path, label="visual review")
    if review.get("schema_version") != 1:
        raise RuntimeError("visual review schema mismatch")
    if adapter.normalize_record_id(
        str(review.get("record_id") or "")
    ) != adapter.normalize_record_id(record_id):
        raise RuntimeError("visual review record mismatch")
    if review.get("verdict") != "pass" or review.get("findings"):
        raise RuntimeError("visual review did not pass cleanly")
    if review.get("coverage") != "all-pages":
        raise RuntimeError("visual review must cover all pages")
    if not str(review.get("reviewer") or "").strip() or not str(
        review.get("review_kind") or ""
    ).strip():
        raise RuntimeError("visual review attribution is incomplete")
    if int(review.get("expected_pages") or -1) != expected_pages:
        raise RuntimeError("visual review page count mismatch")
    _require_hash(source_pdf, review.get("source_pdf_sha256"), label="visual source PDF")
    _require_hash(protected_pdf, review.get("pdf_sha256"), label="visual protected PDF")
    _require_hash(
        contact_sheet,
        review.get("contact_sheet_sha256"),
        label="visual contact sheet",
    )
    return review


def _seal_paper(
    *,
    record_id: str,
    article: Path,
    rights: Path,
    source_manifest: Path,
    source: Path,
    preflight: Path,
    finish: Path,
    raw: Path,
    ir: Path,
    protection: Path,
    protected: Path,
    glossary: Path,
    semantic: Path,
    structural: Path,
    contact: Path,
    visual: Path,
    environment_path: Path,
    current_environment_lock: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate live files, write canonical QC receipts, and seal visual_qc state."""

    article = Path(article).resolve()
    for label, path in (
        ("preflight", preflight),
        ("finish", finish),
        ("raw translation", raw),
        ("IR receipt", ir),
        ("protection receipt", protection),
        ("protected PDF", protected),
        ("locked glossary", glossary),
        ("semantic report", semantic),
        ("structural report", structural),
        ("contact sheet", contact),
        ("visual review", visual),
        ("environment lock", environment_path),
    ):
        _relative(article, Path(path), label=label)
        if not Path(path).is_file():
            raise RuntimeError(f"missing {label}")

    adapter.require_publication_allowed(Path(rights), record_id)
    adapter.require_source_identity(Path(source_manifest), record_id, Path(source))
    environment = _validate_environment(Path(environment_path))
    if environment.get("lock_sha256") != current_environment_lock.get("lock_sha256"):
        raise RuntimeError("environment lock drift")

    preflight_receipt = _load(Path(preflight), label="preflight receipt")
    if preflight_receipt.get("status") != "preflight_passed":
        raise RuntimeError("preflight receipt did not pass")
    rights_hash = _sha256(Path(rights))
    source_manifest_hash = _sha256(Path(source_manifest))
    if (preflight_receipt.get("rights") or {}).get("manifest_sha256") != rights_hash:
        raise RuntimeError("preflight rights manifest hash mismatch")
    if (
        (preflight_receipt.get("source_identity") or {}).get("manifest_sha256")
        != source_manifest_hash
    ):
        raise RuntimeError("preflight source manifest hash mismatch")
    source_hash = _require_hash(
        Path(source),
        (preflight_receipt.get("source") or {}).get("sha256"),
        label="preflight source PDF",
    )

    finish_receipt = _load(Path(finish), label="translation finish receipt")
    if finish_receipt.get("status") != "translated_pending_qc":
        raise RuntimeError("translation is not pending QC")
    if (finish_receipt.get("source") or {}).get("sha256") != source_hash:
        raise RuntimeError("translation source hash mismatch")
    raw_hash = _require_hash(
        Path(raw),
        (((finish_receipt.get("outputs") or {}).get("mono_pdf") or {}).get("sha256")),
        label="raw translated PDF",
    )
    budget = finish_receipt.get("budget") or {}
    _positive(budget.get("project_max_cost_rmb"), 1000.0, label="project budget")
    _positive(budget.get("stage_max_cost_rmb"), 100.0, label="stage budget")
    _positive(budget.get("stage_max_api_calls"), 100000.0, label="request cap")

    ir_receipt = _load(Path(ir), label="IR receipt")
    if ir_receipt.get("zero_paid") is not True or ir_receipt.get("babeldoc_version") != "0.6.4":
        raise RuntimeError("IR receipt is not a pinned zero-paid extraction")
    if ir_receipt.get("source_pdf_sha256") != source_hash:
        raise RuntimeError("IR source hash mismatch")
    ir_json = Path(ir).parent / str(ir_receipt.get("ir_json") or "")
    ir_xml = Path(ir).parent / str(ir_receipt.get("ir_xml") or "")
    _relative(article, ir_json, label="IR JSON")
    _relative(article, ir_xml, label="IR XML")
    _require_hash(ir_json, ir_receipt.get("ir_json_sha256"), label="IR JSON")
    ir_xml_hash = _require_hash(
        ir_xml, ir_receipt.get("ir_xml_sha256"), label="IR XML"
    )

    protection_receipt = _load(Path(protection), label="protection receipt")
    _require_report_fields(
        protection_receipt,
        (
            "protected_regions",
            "selected_source_pages",
            "figure_region_count",
            "table_region_count",
            "reference_page_count",
            "canonical_reference_heading_count",
            "canonical_header_count",
            "verbatim_text_count",
        ),
        label="protection receipt",
    )
    if protection_receipt.get("verified") is not True or protection_receipt.get("failures"):
        raise RuntimeError("protection receipt did not pass cleanly")
    _require_hash(
        Path(source),
        protection_receipt.get("source_pdf_sha256"),
        label="protected source PDF",
    )
    _require_hash(
        Path(raw),
        protection_receipt.get("translated_pdf_sha256"),
        label="protected raw PDF",
    )
    protected_hash = _require_hash(
        Path(protected),
        protection_receipt.get("output_pdf_sha256"),
        label="protected output PDF",
    )
    if protection_receipt.get("ir_xml_sha256") != ir_xml_hash:
        raise RuntimeError("protection IR hash mismatch")

    semantic_report = _load(Path(semantic), label="semantic report")
    _require_report_fields(
        semantic_report,
        (
            "findings",
            "checked_glossary_terms",
            "forbidden_terms",
            "allowed_untranslated_terms",
            "allowed_untranslated_phrases",
        ),
        label="semantic report",
    )
    if semantic_report.get("ok") is not True or semantic_report.get("failures"):
        raise RuntimeError("semantic report did not pass cleanly")
    if semantic_report.get("pdf_sha256") != protected_hash:
        raise RuntimeError("semantic report PDF hash mismatch")
    if semantic_report.get("glossary_sha256") != _sha256(Path(glossary)):
        raise RuntimeError("semantic report glossary hash mismatch")
    if semantic_report.get("protection_receipt_sha256") != _sha256(Path(protection)):
        raise RuntimeError("semantic report protection hash mismatch")

    structural_report = _load(Path(structural), label="structural report")
    _require_report_fields(
        structural_report,
        (
            "low_text_pages",
            "out_of_bounds",
            "residue",
            "english_prose_residue",
            "secondary_text_layer_excess",
            "secondary_only_latin_tokens",
            "secondary_extractor",
            "ignored_text_region_count",
            "contact_sheet_path",
        ),
        label="structural report",
    )
    if structural_report.get("ok") is not True or structural_report.get("failures"):
        raise RuntimeError("structural report did not pass cleanly")
    if structural_report.get("pdf_sha256") != protected_hash:
        raise RuntimeError("structural report PDF hash mismatch")
    if structural_report.get("protection_receipt_sha256") != _sha256(Path(protection)):
        raise RuntimeError("structural report protection hash mismatch")
    if structural_report.get("contact_sheet_sha256") != _sha256(Path(contact)):
        raise RuntimeError("structural report contact sheet hash mismatch")
    expected_pages = int(structural_report.get("expected_pages") or -1)
    if expected_pages <= 0 or int(structural_report.get("page_count") or -1) != expected_pages:
        raise RuntimeError("structural page count mismatch")
    visual_review = _validate_visual_review(
        Path(visual),
        record_id=record_id,
        source_pdf=Path(source),
        protected_pdf=Path(protected),
        contact_sheet=Path(contact),
        expected_pages=expected_pages,
    )

    manifest_path = article / "artifact-manifest.json"
    if manifest_path.is_file():
        existing_state = production.derive_paper_state(
            manifest_path,
            article_root=article,
            current_environment_lock=environment,
            rights_manifest_path=Path(rights),
        )
        if existing_state.get("ok") is not True:
            raise RuntimeError("existing artifact manifest is invalid")
        if existing_state.get("state") == "packaged":
            raise RuntimeError("paper is already packaged; refusing to regress its manifest")
    production.write_artifact_manifest(
        manifest_path=manifest_path,
        record_id=record_id,
        publication_allowed=True,
        rights_manifest_path=Path(rights),
        article_root=article,
        environment_lock=environment,
    )
    chain = (
        ("prepared", preflight, "pdf2zh-next-preflight", "receipt"),
        ("revision_ready", ir, "babeldoc-official-ir", "receipt"),
        ("translated", raw, "pdf2zh-next-2.9.0", "pdf"),
        # The adapter's raw output is already the completed render before the
        # deterministic protection pass. Keep both lifecycle stages explicit
        # even though they point to the same immutable PDF bytes.
        ("rendered", raw, "pdf2zh-next-2.9.0", "pdf"),
        ("protected", protected, "snowmass-pdf2zh-protection", "pdf"),
    )
    parent: tuple[str, ...] = ()
    for artifact_id, path, producer, artifact_type in chain:
        production.record_artifact(
            manifest_path=manifest_path,
            article_root=article,
            artifact_id=artifact_id,
            relative_path=_relative(article, Path(path), label=artifact_id),
            producer=producer,
            artifact_type=artifact_type,
            paper_stage=artifact_id,
            environment_lock=environment,
            parents=parent,
            contract_versions={"pdf2zh_next_seal": SEAL_SCHEMA_VERSION},
        )
        parent = (artifact_id,)

    qc_dir = article / "qc"
    evidence = {
        "semantic": {
            "report_sha256": _sha256(Path(semantic)),
            "glossary_sha256": _sha256(Path(glossary)),
            "protection_sha256": _sha256(Path(protection)),
        },
        "structural": {
            "report_sha256": _sha256(Path(structural)),
            "ir_receipt_sha256": _sha256(Path(ir)),
            "contact_sheet_sha256": _sha256(Path(contact)),
        },
        "visual": {
            "review_sha256": _sha256(Path(visual)),
            "contact_sheet_sha256": _sha256(Path(contact)),
            "coverage": visual_review["coverage"],
            "reviewer": visual_review["reviewer"],
        },
    }
    qc_paths: dict[str, Path] = {}
    parent_id = "protected"
    for kind, stage in (
        ("semantic", "semantic_qc"),
        ("structural", "structural_qc"),
        ("visual", "visual_qc"),
    ):
        receipt_path = qc_dir / f"{kind}.json"
        qc_contract.write_qc_receipt(
            receipt_path=receipt_path,
            article_root=article,
            record_id=record_id,
            kind=kind,
            target_artifact_id="protected",
            target_path=Path(protected),
            environment_lock_sha256=str(environment["lock_sha256"]),
            contract_version=QC_CONTRACT_VERSION,
            ok=True,
            evidence_summary=evidence[kind],
        )
        production.record_artifact(
            manifest_path=manifest_path,
            article_root=article,
            artifact_id=stage,
            relative_path=_relative(article, receipt_path, label=stage),
            producer="snowmass-qc-contract",
            artifact_type="qc_receipt",
            paper_stage=stage,
            environment_lock=environment,
            parents=(parent_id,),
            contract_versions={"qc_receipt": QC_CONTRACT_VERSION},
        )
        parent_id = stage
        qc_paths[kind] = receipt_path

    publishability = qc_contract.validate_publishability_receipts(
        qc_paths.values(),
        article_root=article,
        expected_record_id=record_id,
        current_environment_lock_sha256=str(environment["lock_sha256"]),
        required_contract_version=QC_CONTRACT_VERSION,
    )
    if publishability.get("publishable") is not True:
        raise RuntimeError("canonical QC receipts are not publishable")
    state = production.derive_paper_state(
        manifest_path,
        article_root=article,
        current_environment_lock=environment,
        rights_manifest_path=Path(rights),
    )
    if state.get("ok") is not True or state.get("state") != "visual_qc":
        raise RuntimeError("artifact chain did not reach unambiguous visual_qc")

    seal = {
        "schema_version": SEAL_SCHEMA_VERSION,
        "passed": True,
        "record_id": adapter.normalize_record_id(record_id),
        "state": "visual_qc",
        "source_pdf_sha256": source_hash,
        "rights_manifest_sha256": rights_hash,
        "source_manifest_sha256": source_manifest_hash,
        "raw_translated_pdf_sha256": raw_hash,
        "protected_pdf_sha256": protected_hash,
        "environment_lock_sha256": str(environment["lock_sha256"]),
        "environment_lock_file_sha256": _sha256(Path(environment_path)),
        "artifact_manifest_sha256": _sha256(manifest_path),
        "qc_receipt_hashes": {kind: _sha256(path) for kind, path in qc_paths.items()},
        "evidence_hashes": {
            "finish": _sha256(Path(finish)),
            "preflight": _sha256(Path(preflight)),
            "glossary": _sha256(Path(glossary)),
            "ir": _sha256(Path(ir)),
            "protection": _sha256(Path(protection)),
            "semantic": _sha256(Path(semantic)),
            "structural": _sha256(Path(structural)),
            "visual": _sha256(Path(visual)),
            "contact_sheet": _sha256(Path(contact)),
        },
    }
    seal_path = qc_dir / "paper-seal.json"
    seal_path.write_text(
        json.dumps(seal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return seal


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _input_fingerprint(arguments: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    snapshot: dict[str, Any] = {}
    for label, value in sorted(arguments.items()):
        if label == "record_id":
            snapshot[label] = adapter.normalize_record_id(str(value))
            continue
        if label == "article":
            continue
        if isinstance(value, Mapping):
            snapshot[label] = {"json_sha256": _json_sha256(value)}
            continue
        path = Path(value)
        snapshot[label] = {
            "name": path.name,
            "exists": path.is_file(),
            "sha256": _sha256(path) if path.is_file() else None,
        }
    return _json_sha256(snapshot), snapshot


def seal_paper(**arguments: Any) -> dict[str, Any]:
    """Seal one paper and persist fingerprinted quarantine on any failure."""

    article = Path(arguments["article"]).resolve()
    quarantine_path = article / "qc" / "quarantine.json"
    fingerprint, snapshot = _input_fingerprint(arguments)
    if quarantine_path.is_file():
        quarantine = _load(quarantine_path, label="quarantine receipt")
        if quarantine.get("active") is True and quarantine.get("fingerprint") == fingerprint:
            raise RuntimeError("unchanged quarantined input")
    try:
        seal = _seal_paper(**arguments)
    except Exception as error:
        _atomic_json(
            quarantine_path,
            {
                "schema_version": 1,
                "active": True,
                "record_id": adapter.normalize_record_id(str(arguments["record_id"])),
                "fingerprint": fingerprint,
                "inputs": snapshot,
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )
        raise
    if quarantine_path.is_file():
        _atomic_json(
            quarantine_path,
            {
                "schema_version": 1,
                "active": False,
                "record_id": seal["record_id"],
                "fingerprint": fingerprint,
                "resolved_by_seal_sha256": _sha256(article / "qc" / "paper-seal.json"),
            },
        )
    return seal


def _add_path(parser: argparse.ArgumentParser, name: str) -> None:
    parser.add_argument(f"--{name.replace('_', '-')}", dest=name, type=Path, required=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    environment_parser = commands.add_parser("environment-lock")
    _add_path(environment_parser, "output")

    prepare_parser = commands.add_parser("prepare")
    for name in ("article", "source_pdf", "raw_pdf", "glossary_csv"):
        _add_path(prepare_parser, name)
    prepare_parser.add_argument("--pages", required=True)
    prepare_parser.add_argument("--allow-untranslated", action="append", default=[])
    prepare_parser.add_argument(
        "--allow-untranslated-phrase", action="append", default=[]
    )

    visual_parser = commands.add_parser("attest-visual")
    for name in ("output", "source_pdf", "protected_pdf", "contact_sheet"):
        _add_path(visual_parser, name)
    visual_parser.add_argument("--record-id", required=True)
    visual_parser.add_argument("--expected-pages", type=int, required=True)
    visual_parser.add_argument("--reviewer", required=True)
    visual_parser.add_argument("--review-kind", required=True)
    visual_parser.add_argument("--verdict", choices=("pass", "fail"), required=True)
    visual_parser.add_argument("--finding", action="append", default=[])

    seal_parser = commands.add_parser("seal")
    seal_parser.add_argument("--record-id", required=True)
    for name in (
        "article",
        "rights",
        "source_manifest",
        "source",
        "preflight",
        "finish",
        "raw",
        "ir",
        "protection",
        "protected",
        "glossary",
        "semantic",
        "structural",
        "contact",
        "visual",
        "environment_path",
    ):
        _add_path(seal_parser, name)

    args = parser.parse_args(argv)
    if args.command == "environment-lock":
        receipt = build_current_environment_lock()
        _atomic_json(args.output, receipt)
    elif args.command == "prepare":
        import fitz

        with fitz.open(args.source_pdf) as source_document:
            pages = tuple(
                adapter.parse_selected_pages(args.pages, source_document.page_count)
            )
        result = prepare_paper_qc(
            article=args.article,
            source_pdf=args.source_pdf,
            raw_pdf=args.raw_pdf,
            glossary_csv=args.glossary_csv,
            selected_source_pages=pages,
            allowed_untranslated=tuple(args.allow_untranslated),
            allowed_untranslated_phrases=tuple(args.allow_untranslated_phrase),
        )
        receipt = {
            key: (
                _relative(args.article, value, label=key)
                if isinstance(value, Path)
                else value
            )
            for key, value in result.items()
        }
    elif args.command == "attest-visual":
        receipt = write_visual_review(
            output=args.output,
            record_id=args.record_id,
            source_pdf=args.source_pdf,
            protected_pdf=args.protected_pdf,
            contact_sheet=args.contact_sheet,
            expected_pages=args.expected_pages,
            reviewer=args.reviewer,
            review_kind=args.review_kind,
            verdict=args.verdict,
            findings=tuple(args.finding),
        )
    else:
        values = vars(args)
        values.pop("command")
        values["current_environment_lock"] = build_current_environment_lock()
        receipt = seal_paper(**values)
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
