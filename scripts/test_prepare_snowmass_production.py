#!/usr/bin/env python3
"""Integration tests for Snowmass production preparation."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("prepare_snowmass_production.py")
SPEC = importlib.util.spec_from_file_location("prepare_snowmass_production", MODULE_PATH)
PRODUCTION = None
if SPEC and SPEC.loader and MODULE_PATH.exists():
    PRODUCTION = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(PRODUCTION)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PrepareSnowmassProductionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rights_manifest = self.root / "papers.json"
        self.source_root = self.root / "sources"
        self.output_root = self.root / "production"
        self.source_root.mkdir()
        self.output_root.mkdir()

    def require_module(self):
        if PRODUCTION is None:
            self.fail(f"Missing production preparer module: {MODULE_PATH}")
        return PRODUCTION

    def write_rights_manifest(self, rows: list[dict[str, object]]) -> None:
        self.rights_manifest.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def write_source_manifest(self, rows: list[dict[str, object]]) -> None:
        (self.source_root / "manifest.json").write_text(
            json.dumps({"schema_version": 1, "records": rows}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def write_tar_source(self, directory: str, members: list[tuple[str, bytes]]) -> None:
        item_dir = self.source_root / directory
        item_dir.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, payload in members:
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        (item_dir / "source.tar.gz").write_bytes(gzip.compress(buffer.getvalue()))

    def write_single_tex_gzip(self, directory: str, text: str) -> None:
        item_dir = self.source_root / directory
        item_dir.mkdir(parents=True, exist_ok=True)
        (item_dir / "source.tar.gz").write_bytes(gzip.compress(text.encode("utf-8")))

    def write_invalid_archive(self, directory: str, payload: bytes = b"not a tex archive") -> None:
        item_dir = self.source_root / directory
        item_dir.mkdir(parents=True, exist_ok=True)
        (item_dir / "source.tar.gz").write_bytes(gzip.compress(payload))

    def write_pdf_text(self, directory: str, text: str) -> None:
        item_dir = self.source_root / directory
        item_dir.mkdir(parents=True, exist_ok=True)
        (item_dir / "source.txt").write_text(text, encoding="utf-8")

    def load_report(self) -> dict[str, object]:
        return json.loads((self.output_root / "preparation_report.json").read_text(encoding="utf-8"))

    def record_by_id(self, report: dict[str, object], record_id: str) -> dict[str, object]:
        for record in report["records"]:
            if record["record_id"] == record_id:
                return record
        self.fail(f"Missing record {record_id!r} in preparation report")

    def fixture_rows(self) -> list[dict[str, str]]:
        return [
            {"record_id": "arxiv:tar", "directory": "papers/arxiv_tar"},
            {"record_id": "arxiv:single", "directory": "papers/arxiv_single"},
            {"record_id": "arxiv:pdf", "directory": "papers/arxiv_pdf"},
            {"record_id": "arxiv:ambiguous", "directory": "papers/arxiv_ambiguous"},
            {"record_id": "arxiv:failure", "directory": "papers/arxiv_failure"},
            {"record_id": "arxiv:blocked", "directory": "papers/arxiv_blocked"},
        ]

    def seed_fixture(self) -> None:
        rows = self.fixture_rows()
        self.write_source_manifest(rows)
        self.write_rights_manifest(
            [
                {"record_id": "arxiv:tar", "publication_allowed": True},
                {"record_id": "arxiv:single", "publication_allowed": True},
                {"record_id": "arxiv:pdf", "publication_allowed": True},
                {"record_id": "arxiv:ambiguous", "publication_allowed": True},
                {"record_id": "arxiv:failure", "publication_allowed": True},
                {"record_id": "arxiv:blocked", "publication_allowed": False},
            ]
        )

        self.write_pdf_text("papers/arxiv_tar", "tar fallback should not be used\n")
        self.write_tar_source(
            "papers/arxiv_tar",
            [
                (
                    "main.tex",
                    (
                        "\\documentclass{article}\n"
                        "\\title{Tar Source}\n"
                        "\\begin{document}\n"
                        "Tar body text.\n"
                        "\\input{sections/intro}\n"
                        "\\end{document}\n"
                    ).encode("utf-8"),
                ),
                ("sections/intro.tex", b"Included tar section.\n"),
            ],
        )

        self.write_single_tex_gzip(
            "papers/arxiv_single",
            (
                "\\documentclass{article}\n"
                "\\title{Single TeX}\n"
                "\\begin{document}\n"
                "Single file TeX payload.\n"
                "\\end{document}\n"
            ),
        )

        self.write_invalid_archive("papers/arxiv_pdf")
        self.write_pdf_text("papers/arxiv_pdf", "PDF fallback text survives.\n")

        self.write_tar_source(
            "papers/arxiv_ambiguous",
            [
                (
                    "main.tex",
                    (
                        "\\documentclass{article}\n"
                        "\\title{Main Candidate}\n"
                        "\\begin{document}\n"
                        "Chosen ambiguous main body.\n"
                        "\\end{document}\n"
                    ).encode("utf-8"),
                ),
                (
                    "overview.tex",
                    (
                        "\\documentclass{article}\n"
                        "\\title{Overview Candidate}\n"
                        "\\begin{document}\n"
                        "Alternative ambiguous body.\n"
                        "\\end{document}\n"
                    ).encode("utf-8"),
                ),
            ],
        )

        self.write_invalid_archive("papers/arxiv_failure")

        self.write_invalid_archive("papers/arxiv_blocked")
        self.write_pdf_text("papers/arxiv_blocked", "Blocked record text.\n")

    def artifact_hashes(self, directory: str) -> dict[str, str]:
        article_dir = self.output_root / directory
        return {
            path.name: sha256_file(path)
            for path in sorted(article_dir.glob("source.md")) + sorted(article_dir.glob("manifest.json")) + sorted(article_dir.glob("chunk*.md"))
        }

    def test_main_prepares_allowed_records_writes_reports_and_reuses_valid_artifacts(self) -> None:
        preparer = self.require_module()
        self.seed_fixture()

        exit_code = preparer.main(
            [
                "--rights-manifest",
                str(self.rights_manifest),
                "--source-root",
                str(self.source_root),
                "--output-root",
                str(self.output_root),
            ]
        )

        self.assertEqual(exit_code, 2)
        rights_snapshot = json.loads((self.output_root / "rights_snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [row["record_id"] for row in rights_snapshot["records"]],
            ["arxiv:tar", "arxiv:single", "arxiv:pdf", "arxiv:ambiguous", "arxiv:failure"],
        )

        report = self.load_report()
        self.assertEqual(report["counts"], {"complete": 3, "ambiguous": 1, "failed": 1, "reused": 0})
        self.assertEqual(
            [row["record_id"] for row in report["records"]],
            ["arxiv:tar", "arxiv:single", "arxiv:pdf", "arxiv:ambiguous", "arxiv:failure"],
        )
        self.assertFalse((self.output_root / "papers/arxiv_blocked").exists())

        tar_record = self.record_by_id(report, "arxiv:tar")
        single_record = self.record_by_id(report, "arxiv:single")
        pdf_record = self.record_by_id(report, "arxiv:pdf")
        ambiguous_record = self.record_by_id(report, "arxiv:ambiguous")
        failed_record = self.record_by_id(report, "arxiv:failure")

        self.assertEqual(tar_record["status"], "complete")
        self.assertEqual(tar_record["source_kind"], "expanded_tex")
        self.assertEqual(tar_record["source_package_type"], "tar")
        self.assertFalse(tar_record["reused"])

        self.assertEqual(single_record["status"], "complete")
        self.assertEqual(single_record["source_kind"], "expanded_tex")
        self.assertEqual(single_record["source_package_type"], "single_tex")

        self.assertEqual(pdf_record["status"], "complete")
        self.assertEqual(pdf_record["source_kind"], "pdf_text")
        self.assertEqual(pdf_record["fallback_reason"], "archive_unusable")

        self.assertEqual(ambiguous_record["status"], "ambiguous")
        self.assertEqual(ambiguous_record["ambiguity_reasons"], ["multiple_main_tex_candidates"])
        self.assertGreaterEqual(len(ambiguous_record["main_tex_candidates"]), 2)

        self.assertEqual(failed_record["status"], "failed")
        self.assertIn("Missing PDF text fallback", failed_record["error"])

        for record_id, directory in (
            ("arxiv:tar", "papers/arxiv_tar"),
            ("arxiv:single", "papers/arxiv_single"),
            ("arxiv:pdf", "papers/arxiv_pdf"),
            ("arxiv:ambiguous", "papers/arxiv_ambiguous"),
            ("arxiv:failure", "papers/arxiv_failure"),
        ):
            record = self.record_by_id(report, record_id)
            report_path = Path(record["report_path"])
            self.assertTrue(report_path.is_file(), record_id)
            self.assertEqual(
                json.loads(report_path.read_text(encoding="utf-8"))["record_id"],
                record_id,
            )

        original_hashes = {
            directory: self.artifact_hashes(directory)
            for directory in ("papers/arxiv_tar", "papers/arxiv_single", "papers/arxiv_pdf", "papers/arxiv_ambiguous")
        }

        rerun_exit_code = preparer.main(
            [
                "--rights-manifest",
                str(self.rights_manifest),
                "--source-root",
                str(self.source_root),
                "--output-root",
                str(self.output_root),
            ]
        )

        self.assertEqual(rerun_exit_code, 2)
        rerun_report = self.load_report()
        self.assertEqual(rerun_report["counts"], {"complete": 3, "ambiguous": 1, "failed": 1, "reused": 4})
        for record_id in ("arxiv:tar", "arxiv:single", "arxiv:pdf", "arxiv:ambiguous"):
            self.assertTrue(self.record_by_id(rerun_report, record_id)["reused"], record_id)
        for directory, expected_hashes in original_hashes.items():
            self.assertEqual(self.artifact_hashes(directory), expected_hashes, directory)

    def test_main_resumes_after_keyboard_interrupt_without_rewriting_completed_artifacts(self) -> None:
        preparer = self.require_module()
        rows = self.fixture_rows()[:2]
        self.write_source_manifest(rows)
        self.write_rights_manifest(
            [
                {"record_id": "arxiv:tar", "publication_allowed": True},
                {"record_id": "arxiv:single", "publication_allowed": True},
            ]
        )
        self.write_tar_source(
            "papers/arxiv_tar",
            [
                (
                    "main.tex",
                    (
                        "\\documentclass{article}\n"
                        "\\begin{document}\n"
                        "Interrupt-safe tar body.\n"
                        "\\end{document}\n"
                    ).encode("utf-8"),
                )
            ],
        )
        self.write_single_tex_gzip(
            "papers/arxiv_single",
            (
                "\\documentclass{article}\n"
                "\\begin{document}\n"
                "Interrupt-safe single body.\n"
                "\\end{document}\n"
            ),
        )

        original_prepare_record = preparer.prepare_record
        calls = {"count": 0}

        def interrupting_prepare_record(record, source_root, output_root):
            calls["count"] += 1
            if calls["count"] == 2:
                raise KeyboardInterrupt()
            return original_prepare_record(record, source_root, output_root)

        with mock.patch.object(preparer, "prepare_record", side_effect=interrupting_prepare_record):
            exit_code = preparer.main(
                [
                    "--rights-manifest",
                    str(self.rights_manifest),
                    "--source-root",
                    str(self.source_root),
                    "--output-root",
                    str(self.output_root),
                ]
            )

        self.assertEqual(exit_code, 130)
        interrupted_report = self.load_report()
        self.assertEqual(interrupted_report["counts"], {"complete": 1, "ambiguous": 0, "failed": 0, "reused": 0})
        tar_hashes = self.artifact_hashes("papers/arxiv_tar")

        resumed_exit_code = preparer.main(
            [
                "--rights-manifest",
                str(self.rights_manifest),
                "--source-root",
                str(self.source_root),
                "--output-root",
                str(self.output_root),
            ]
        )

        self.assertEqual(resumed_exit_code, 0)
        resumed_report = self.load_report()
        self.assertEqual(resumed_report["counts"], {"complete": 2, "ambiguous": 0, "failed": 0, "reused": 1})
        self.assertTrue(self.record_by_id(resumed_report, "arxiv:tar")["reused"])
        self.assertEqual(self.artifact_hashes("papers/arxiv_tar"), tar_hashes)
        self.assertEqual(
            [row["record_id"] for row in resumed_report["records"]],
            ["arxiv:tar", "arxiv:single"],
        )


if __name__ == "__main__":
    unittest.main()
