#!/usr/bin/env python3
"""Package a Snowmass translation PDF with a cover page and receipt."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.parse import urlparse

import fitz
from pypdf import PdfReader, PdfWriter

try:
    from snowmass_publication_qc import validate_pdf_forbidden_translations
except ModuleNotFoundError:
    from scripts.snowmass_publication_qc import validate_pdf_forbidden_translations


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MOUNTAIN_SVG_PATH = ROOT / "site" / "assets" / "snowmass-mountain.png"
DEFAULT_QR_IMAGE_PATH = ROOT / "site" / "assets" / "snowmass-site-qr.png"
QR_GENERATOR_PATH = ROOT / "scripts" / "make_snowmass_qr.swift"
SYSTEM_CJK_FONT = Path("/System/Library/Fonts/STHeiti Medium.ttc")

PROJECT_NAME = "Snowmass White Paper Chinese Translation Collaboration"
CONTRIBUTOR_LABEL = "中文翻译贡献者：WangTheoPhys*"
WEBSITE_ORIGIN = "https://snowmass-physics-cn.netlify.app"
DISCLAIMER_TEXT = "本译文由中文翻译协作项目制作，不代表原作者审定或认可；如有歧义，以英文原文为准。"
CONTACT_TEXT = "*Contact: WangTheoPhys@outlook.com"
LICENSE_CONDITION_LABELS = {
    "attribution": "署名",
    "indicate-changes": "注明修改",
}


def package_translation_pdf(
    *,
    record: dict[str, Any],
    chinese_title: str,
    source_pdf_path: str | Path,
    output_pdf_path: str | Path,
    version: str,
    packaged_on: str | dt.date | dt.datetime,
    qr_image_path: str | Path = DEFAULT_QR_IMAGE_PATH,
    mountain_svg_path: str | Path = DEFAULT_MOUNTAIN_SVG_PATH,
) -> dict[str, object]:
    """Create a cover-prefixed PDF and a JSON receipt for one translation."""

    if record.get("publication_allowed") is not True:
        raise ValueError("publication_allowed must be literal True")
    if not chinese_title or not chinese_title.strip():
        raise ValueError("Chinese title is required")

    source_pdf = Path(source_pdf_path)
    output_pdf = Path(output_pdf_path)
    if source_pdf.resolve() == output_pdf.resolve():
        raise ValueError("source_pdf_path and output_pdf_path must differ")
    cover_pdf = output_pdf.with_name(f"{output_pdf.stem}.cover.pdf")
    receipt_path = output_pdf.with_suffix(".json")
    qr_image = Path(qr_image_path)
    mountain_asset = Path(mountain_svg_path)

    if not source_pdf.is_file():
        raise FileNotFoundError(f"source PDF not found: {source_pdf}")
    if not qr_image.is_file():
        raise FileNotFoundError(f"QR fallback image not found: {qr_image}")
    if not mountain_asset.is_file():
        raise FileNotFoundError(f"mountain cover asset not found: {mountain_asset}")
    if not SYSTEM_CJK_FONT.is_file():
        raise FileNotFoundError(f"required system font not found: {SYSTEM_CJK_FONT}")

    validate_pdf_forbidden_translations(
        source_pdf,
        ROOT / "translations" / "snowmass-hard-constraints.json",
    )

    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    packaged_on_text = _normalize_packaged_on(packaged_on)
    paper_qr_image = output_pdf.with_name(f".{output_pdf.stem}.paper-qr.png")
    _generate_paper_qr(_translation_page_url(record), paper_qr_image)
    try:
        _render_cover_pdf(
            record=record,
            chinese_title=chinese_title.strip(),
            cover_pdf_path=cover_pdf,
            version=version,
            packaged_on=packaged_on_text,
            qr_image_path=paper_qr_image,
            mountain_svg_path=mountain_asset,
        )
    finally:
        paper_qr_image.unlink(missing_ok=True)
    _prepend_cover_pdf(cover_pdf, source_pdf, output_pdf)

    receipt = {
        "record_id": record.get("record_id"),
        "translation_page_url": _translation_page_url(record),
        "version": version,
        "packaged_on": packaged_on_text,
        "source_pdf_path": _portable_artifact_reference(source_pdf),
        "cover_pdf_path": _portable_artifact_reference(cover_pdf),
        "output_pdf_path": _portable_artifact_reference(output_pdf),
        "receipt_path": _portable_artifact_reference(receipt_path),
        "source_pdf_sha256": _sha256_file(source_pdf),
        "cover_pdf_sha256": _sha256_file(cover_pdf),
        "packaged_pdf_sha256": _sha256_file(output_pdf),
    }
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return receipt


def _normalize_packaged_on(value: str | dt.date | dt.datetime) -> str:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def _translation_page_url(record: dict[str, Any]) -> str:
    """Return the stable public page for a manifest record."""

    arxiv_id = _derive_arxiv_identifier(record)
    if arxiv_id:
        slug = arxiv_id
    else:
        source_url = str(record.get("source_url", ""))
        cds_match = re.search(r"cds\.cern\.ch/record/(\d+)", source_url)
        hal_match = re.search(r"hal-(\d+)", source_url)
        if cds_match:
            slug = f"cds-{cds_match.group(1)}"
        elif hal_match:
            slug = f"hal-{hal_match.group(1)}"
        else:
            raise ValueError("record does not have a supported permanent paper-page identifier")
    return f"{WEBSITE_ORIGIN}/paper/{slug}/"


def _generate_paper_qr(url: str, output_path: Path) -> None:
    """Generate a QR code containing the paper permalink without network access."""

    if not QR_GENERATOR_PATH.is_file():
        raise FileNotFoundError(f"QR generator not found: {QR_GENERATOR_PATH}")
    subprocess.run(
        ["/usr/bin/swift", str(QR_GENERATOR_PATH), url, str(output_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if not output_path.is_file():
        raise RuntimeError("QR generator did not create an image")


def _render_cover_pdf(
    *,
    record: dict[str, Any],
    chinese_title: str,
    cover_pdf_path: Path,
    version: str,
    packaged_on: str,
    qr_image_path: Path,
    mountain_svg_path: Path,
) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.draw_rect((0, 0, 595, 842), color=None, fill=(0.972, 0.98, 0.985))

    text_color = (0.149, 0.216, 0.298)
    muted_color = (0.282, 0.361, 0.443)
    link_color = (0.071, 0.349, 0.6)
    banner_rect = fitz.Rect(48, 36, 547, 174)
    page.insert_image(banner_rect, filename=str(mountain_svg_path), keep_proportion=False)

    y = 192.0
    y = _write_textbox(page, (48, y, 547, y + 24), PROJECT_NAME, 14, text_color) + 12
    y = _write_textbox(page, (48, y, 547, y + 56), chinese_title, 20, text_color) + 10
    y = _write_textbox(page, (48, y, 547, y + 56), str(record.get("title", "")), 13, muted_color) + 10
    y = _write_textbox(page, (48, y, 547, y + 20), f"原作者：{record.get('authors_as_listed', '')}", 12, text_color) + 20

    source_url = str(record.get("source_url", ""))
    arxiv_id = _derive_arxiv_identifier(record)
    arxiv_url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None
    doi_value = str(record.get("doi", "")).strip()
    doi_url = f"https://doi.org/{doi_value}" if doi_value else None
    license_name = str(record.get("source_license", "未提供"))
    license_url = str(record.get("source_license_url", "")).strip()
    conditions = _format_conditions(record.get("publication_conditions"))
    translation_page_url = _translation_page_url(record)
    translation_page_label = translation_page_url.removeprefix("https://")

    left_column_right = 364
    metadata_top = max(y, 396.0)
    qr_rect = fitz.Rect(404, metadata_top, 524, metadata_top + 120)

    metadata_y = metadata_top
    metadata_y = _write_link_line(
        page,
        (48, metadata_y, left_column_right, metadata_y + 18),
        f"arXiv: {arxiv_id}" if arxiv_id else "arXiv: 未提供",
        arxiv_url,
        10,
        link_color if arxiv_url else muted_color,
    ) + 8
    metadata_y = _write_link_line(
        page,
        (48, metadata_y, 398, metadata_y + 20),
        f"原文链接：{source_url}",
        source_url,
        8.5,
        link_color,
    ) + 8
    metadata_y = _write_link_line(
        page,
        (48, metadata_y, left_column_right, metadata_y + 18),
        f"DOI: {doi_value}" if doi_value else "DOI: 未提供 / Not Provided",
        doi_url,
        10,
        link_color if doi_url else muted_color,
    ) + 8
    metadata_y = _write_textbox(page, (48, metadata_y, left_column_right, metadata_y + 18), CONTRIBUTOR_LABEL, 11, text_color) + 8
    metadata_y = _write_textbox(page, (48, metadata_y, left_column_right, metadata_y + 18), "TRANSLATION PAGE / 本论文译文页", 10, muted_color) + 4
    metadata_y = _write_link_line(page, (48, metadata_y, 398, metadata_y + 22), translation_page_label, translation_page_url, 9.5, link_color) + 8
    metadata_y = _write_textbox(
        page,
        (48, metadata_y, left_column_right, metadata_y + 18),
        f"翻译版本：{version}    日期：{packaged_on}",
        11,
        text_color,
    )

    page.insert_image(qr_rect, filename=str(qr_image_path), keep_proportion=True)
    page.insert_link({"kind": fitz.LINK_URI, "from": qr_rect, "uri": translation_page_url})

    footer_y = max(metadata_y + 24, qr_rect.y1 + 24)
    footer_y = _write_textbox(page, (48, footer_y, 547, footer_y + 48), DISCLAIMER_TEXT, 11, text_color) + 10
    footer_y = _write_textbox(page, (48, footer_y, 547, footer_y + 18), f"原文许可证：{license_name}", 11, text_color) + 8
    footer_y = _write_textbox(page, (48, footer_y, 547, footer_y + 18), f"适用条件：{conditions}", 11, text_color) + 8
    if license_url:
        footer_y = _write_link_line(page, (48, footer_y, 547, footer_y + 24), f"许可证链接：{license_url}", license_url, 10, link_color)
    y = _write_textbox(page, (48, 782, 547, 812), CONTACT_TEXT, 10, muted_color)

    document.set_metadata(
        {
            "title": "",
            "author": "",
            "subject": "",
            "keywords": "",
            "creator": "",
            "producer": "",
            "creationDate": "",
            "modDate": "",
            "trapped": "",
        }
    )
    document.save(
        cover_pdf_path,
        garbage=4,
        deflate=1,
        deflate_images=1,
        deflate_fonts=1,
        no_new_id=1,
        use_objstms=0,
    )
    document.close()


def _write_textbox(
    page: fitz.Page,
    rect: tuple[float, float, float, float],
    text: str,
    fontsize: float,
    color: tuple[float, float, float],
) -> float:
    box = fitz.Rect(rect)
    spare_height = page.insert_textbox(
        box,
        text,
        fontfile=str(SYSTEM_CJK_FONT),
        fontname="snowmasscover",
        fontsize=fontsize,
        lineheight=1.25,
        color=color,
    )
    if spare_height < 0:
        preview = text if len(text) <= 80 else f"{text[:77]}..."
        raise ValueError(f"text overflow for rect {tuple(box)}: {preview}")
    return box.y1


def _write_link_line(
    page: fitz.Page,
    rect: tuple[float, float, float, float],
    text: str,
    url: str | None,
    fontsize: float,
    color: tuple[float, float, float],
) -> float:
    box = fitz.Rect(rect)
    bottom = _write_textbox(page, rect, text, fontsize, color)
    if url:
        page.insert_link({"kind": fitz.LINK_URI, "from": box, "uri": url})
    return bottom


def _format_conditions(raw_conditions: Any) -> str:
    if not raw_conditions:
        return "未注明"
    if not isinstance(raw_conditions, list):
        raw_conditions = [raw_conditions]
    labels = [LICENSE_CONDITION_LABELS.get(str(item), str(item)) for item in raw_conditions]
    return "；".join(labels)


def _portable_artifact_reference(path: Path) -> str:
    return path.name


def _derive_arxiv_identifier(record: dict[str, Any]) -> str | None:
    record_id = str(record.get("record_id", "")).strip()
    if record_id.lower().startswith("arxiv:"):
        identifier = record_id.split(":", 1)[1].strip()
        if identifier:
            return identifier

    source_url = str(record.get("source_url", "")).strip()
    parsed = urlparse(source_url)
    if not re.search(r"(^|\.)arxiv\.org$", parsed.netloc, re.IGNORECASE):
        return None
    path = parsed.path or ""
    remainder = None
    for prefix in ("/abs/", "/pdf/"):
        if path.startswith(prefix):
            remainder = path[len(prefix):]
            break
    if not remainder:
        return None
    identifier = remainder.rstrip("/")
    if identifier.lower().endswith(".pdf"):
        identifier = identifier[:-4]
    if not identifier:
        return None
    return _strip_valid_arxiv_version(identifier)


def _strip_valid_arxiv_version(identifier: str) -> str:
    old_style = re.fullmatch(r"([A-Za-z.-]+/\d{7})(v\d+)?", identifier)
    if old_style:
        return old_style.group(1)

    modern = re.fullmatch(r"(\d{4}\.\d{4,5})(v\d+)?", identifier)
    if modern:
        return modern.group(1)

    return identifier


def _prepend_cover_pdf(cover_pdf_path: Path, source_pdf_path: Path, output_pdf_path: Path) -> None:
    writer = PdfWriter()
    for page in PdfReader(str(cover_pdf_path)).pages:
        writer.add_page(page)
    for page in PdfReader(str(source_pdf_path)).pages:
        writer.add_page(page)
    with output_pdf_path.open("wb") as handle:
        writer.write(handle)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
