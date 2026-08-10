#!/usr/bin/env python3
"""Tests for the Snowmass production pipeline helpers."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("snowmass_pipeline.py")


def load_pipeline(test_case: unittest.TestCase):
    if not MODULE_PATH.exists():
        test_case.fail(f"missing module under test: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("snowmass_pipeline", MODULE_PATH)
    test_case.assertIsNotNone(spec)
    test_case.assertIsNotNone(spec.loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RightsSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = self.root / "papers.json"
        self.output = self.root / "rights_snapshot.json"

    def write_manifest(self, records: list[dict[str, object]]) -> None:
        self.manifest.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_rights_snapshot_only_accepts_literal_true(self) -> None:
        pipeline = load_pipeline(self)
        self.write_manifest(
            [
                {"record_id": "arxiv:allowed", "publication_allowed": True},
                {"record_id": "arxiv:int", "publication_allowed": 1},
                {"record_id": "arxiv:string", "publication_allowed": "true"},
                {"record_id": "arxiv:false", "publication_allowed": False},
                {"record_id": "arxiv:null", "publication_allowed": None},
                {"record_id": "arxiv:missing"},
            ]
        )

        snapshot = pipeline.build_rights_snapshot(self.manifest, self.output)

        self.assertEqual([row["record_id"] for row in snapshot["records"]], ["arxiv:allowed"])
        self.assertEqual(snapshot["eligible_count"], 1)

    def test_rights_snapshot_records_manifest_hash_and_writes_output(self) -> None:
        pipeline = load_pipeline(self)
        self.write_manifest(
            [
                {"record_id": "arxiv:allowed", "publication_allowed": True, "title": "Allowed"},
                {"record_id": "arxiv:blocked", "publication_allowed": False, "title": "Blocked"},
            ]
        )
        expected_hash = hashlib.sha256(self.manifest.read_bytes()).hexdigest()

        snapshot = pipeline.build_rights_snapshot(self.manifest, self.output)
        written = json.loads(self.output.read_text(encoding="utf-8"))

        self.assertEqual(snapshot, written)
        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["source_manifest_path"], str(self.manifest))
        self.assertEqual(snapshot["source_manifest_sha256"], expected_hash)
        self.assertEqual(snapshot["eligible_count"], 1)
        self.assertEqual(snapshot["records"][0]["title"], "Allowed")
        datetime.fromisoformat(snapshot["created_at"])

    def test_rights_snapshot_rejects_duplicate_record_ids(self) -> None:
        pipeline = load_pipeline(self)
        self.write_manifest(
            [
                {"record_id": "arxiv:dupe", "publication_allowed": True},
                {"record_id": "arxiv:dupe", "publication_allowed": False},
            ]
        )

        with self.assertRaisesRegex(ValueError, "Duplicate record_id"):
            pipeline.build_rights_snapshot(self.manifest, self.output)

        self.assertFalse(self.output.exists())


class SourcePackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "extract"

    def gzip_path(self, name: str, payload: bytes) -> Path:
        path = self.root / name
        path.write_bytes(gzip.compress(payload))
        return path

    def tar_gzip_path(
        self,
        name: str,
        members: list[tuple[str, bytes, str | None]],
    ) -> Path:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for member_name, payload, link_target in members:
                info = tarfile.TarInfo(member_name)
                if link_target is None:
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
                else:
                    info.type = tarfile.SYMTYPE
                    info.linkname = link_target
                    archive.addfile(info)
        return self.gzip_path(name, buffer.getvalue())

    def test_detect_source_package_identifies_tar_archive(self) -> None:
        pipeline = load_pipeline(self)
        archive = self.tar_gzip_path("source.tar.gz", [("main.tex", b"\\documentclass{article}\n", None)])

        self.assertEqual(pipeline.detect_source_package(archive), "tar")

    def test_detect_source_package_identifies_single_tex_gzip(self) -> None:
        pipeline = load_pipeline(self)
        archive = self.gzip_path("single.tex.gz", b"\\documentclass{article}\n\\begin{document}Hi\\end{document}\n")

        self.assertEqual(pipeline.detect_source_package(archive), "single_tex")

    def test_safe_extract_rejects_parent_traversal(self) -> None:
        pipeline = load_pipeline(self)
        archive = self.tar_gzip_path("escape.tar.gz", [("../escape.tex", b"bad\n", None)])

        with self.assertRaises(pipeline.UnsafeArchiveError):
            pipeline.safe_extract_source(archive, self.destination)

    def test_safe_extract_rejects_escaping_symlink(self) -> None:
        pipeline = load_pipeline(self)
        archive = self.tar_gzip_path("symlink.tar.gz", [("paper.tex", b"ok\n", None), ("jump", b"", "../outside.tex")])

        with self.assertRaises(pipeline.UnsafeArchiveError):
            pipeline.safe_extract_source(archive, self.destination)

    def test_safe_extract_extracts_normal_tar_members(self) -> None:
        pipeline = load_pipeline(self)
        archive = self.tar_gzip_path(
            "paper.tar.gz",
            [
                ("main.tex", b"\\documentclass{article}\n", None),
                ("figures/plot.pdf", b"%PDF-1.4\n", None),
            ],
        )

        extracted = pipeline.safe_extract_source(archive, self.destination)

        self.assertEqual(
            [path.relative_to(self.destination).as_posix() for path in extracted],
            ["figures/plot.pdf", "main.tex"],
        )
        self.assertEqual((self.destination / "main.tex").read_text(encoding="utf-8"), "\\documentclass{article}\n")
        self.assertEqual((self.destination / "figures" / "plot.pdf").read_bytes(), b"%PDF-1.4\n")

    def test_safe_extract_extracts_single_tex_gzip(self) -> None:
        pipeline = load_pipeline(self)
        archive = self.gzip_path("single.tex.gz", b"\\documentclass{article}\n\\begin{document}Hi\\end{document}\n")

        extracted = pipeline.safe_extract_source(archive, self.destination)

        self.assertEqual([path.name for path in extracted], ["single.tex"])
        self.assertEqual(
            (self.destination / "single.tex").read_text(encoding="utf-8"),
            "\\documentclass{article}\n\\begin{document}Hi\\end{document}\n",
        )


if __name__ == "__main__":
    unittest.main()
