#!/usr/bin/env python3
"""Tests for the Snowmass production pipeline helpers."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).with_name("snowmass_pipeline.py")
CHUNK_MODULE_PATH = Path(__file__).with_name("prepare_snowmass_chunks.py")


def load_pipeline(test_case: unittest.TestCase):
    if not MODULE_PATH.exists():
        test_case.fail(f"missing module under test: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("snowmass_pipeline", MODULE_PATH)
    test_case.assertIsNotNone(spec)
    test_case.assertIsNotNone(spec.loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_chunk_preparer(test_case: unittest.TestCase):
    if not CHUNK_MODULE_PATH.exists():
        test_case.fail(f"missing module under test: {CHUNK_MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("prepare_snowmass_chunks", CHUNK_MODULE_PATH)
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

    def test_detect_source_package_rejects_non_tex_utf8_gzip(self) -> None:
        pipeline = load_pipeline(self)
        archive = self.gzip_path("notes.txt.gz", b"This is ordinary UTF-8 text, not a TeX source file.\n")

        with self.assertRaisesRegex(ValueError, "Unsupported gzip payload"):
            pipeline.detect_source_package(archive)

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

    def test_safe_extract_renames_single_tex_payload_from_source_tar_gz_to_tex(self) -> None:
        pipeline = load_pipeline(self)
        archive = self.gzip_path("source.tar.gz", b"\\documentclass{article}\n\\begin{document}Hi\\end{document}\n")

        extracted = pipeline.safe_extract_source(archive, self.destination)

        self.assertEqual([path.name for path in extracted], ["source.tex"])
        self.assertEqual(
            (self.destination / "source.tex").read_text(encoding="utf-8"),
            "\\documentclass{article}\n\\begin{document}Hi\\end{document}\n",
        )


class MainTexSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pipeline = load_pipeline(self)

    def write_tex(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_rank_main_tex_prefers_document_with_title_abstract_and_main_name(self) -> None:
        main_path = self.write_tex(
            "main.tex",
            "\\documentclass{article}\n"
            "\\title{Signal}\n"
            "\\begin{document}\n"
            "\\begin{abstract}Summary\\end{abstract}\n"
            "\\input{sections/intro}\n"
            "\\end{document}\n",
        )
        self.write_tex("sections/intro.tex", "first section\n")
        self.write_tex(
            "supplement.tex",
            "\\documentclass{article}\n\\begin{document}\nSupplement\n\\end{document}\n",
        )

        candidates = self.pipeline.rank_main_tex(self.root)

        self.assertEqual(candidates[0].path, main_path)
        self.assertGreater(candidates[0].score, candidates[1].score)

    def test_rank_main_tex_keeps_multiple_candidates_visible(self) -> None:
        main_path = self.write_tex(
            "main.tex",
            "\\documentclass{article}\n"
            "\\title{Primary}\n"
            "\\begin{document}\n"
            "\\begin{abstract}Primary abstract\\end{abstract}\n"
            "Long enough body to win the tiebreaker.\n"
            "\\end{document}\n",
        )
        alternative_path = self.write_tex(
            "draft.tex",
            "\\documentclass{article}\n"
            "\\title{Alternative}\n"
            "\\begin{document}\n"
            "Alternative body.\n"
            "\\end{document}\n",
        )

        candidates = self.pipeline.rank_main_tex(self.root)

        self.assertEqual([candidate.path for candidate in candidates[:2]], [main_path, alternative_path])

    def test_rank_main_tex_excludes_backup_filenames(self) -> None:
        self.write_tex(
            "main.tex",
            "\\documentclass{article}\n\\begin{document}\nLive paper\n\\end{document}\n",
        )
        self.write_tex(
            "main.bak.tex",
            "\\documentclass{article}\n\\begin{document}\nBackup paper\n\\end{document}\n",
        )
        self.write_tex(
            "draft_backup.tex",
            "\\documentclass{article}\n\\begin{document}\nAnother backup\n\\end{document}\n",
        )

        candidates = self.pipeline.rank_main_tex(self.root)

        self.assertEqual([candidate.path.name for candidate in candidates], ["main.tex"])

    def test_rank_main_tex_ignores_commented_document_markers(self) -> None:
        active_path = self.write_tex(
            "active.tex",
            "\\documentclass{article}\n"
            "\\title{Active paper}\n"
            "\\begin{document}\n"
            "\\begin{abstract}Summary\\end{abstract}\n",
        )
        commented_path = self.write_tex(
            "commented.tex",
            "% \\documentclass{article}\n"
            "% \\title{Commented paper}\n"
            "% \\begin{document}\n"
            "% \\begin{abstract}Commented summary\\end{abstract}\n",
        )

        candidates = self.pipeline.rank_main_tex(self.root)
        by_path = {candidate.path: candidate for candidate in candidates}

        self.assertEqual(candidates[0].path, active_path)
        self.assertFalse(by_path[commented_path].has_document_marker)
        self.assertFalse(by_path[commented_path].has_title)
        self.assertFalse(by_path[commented_path].has_abstract)

    def test_rank_main_tex_prefers_include_rich_whole_paper_over_short_standalone(self) -> None:
        whole_paper = self.write_tex(
            "WhitePaper.tex",
            "\\documentclass{article}\n"
            "\\begin{document}\n"
            "\\input{sections/one}\n"
            "\\input{sections/two}\n"
            "\\input{sections/three}\n"
            "\\input{sections/four}\n"
            "\\input{sections/five}\n"
            "\\end{document}\n",
        )
        standalone = self.write_tex(
            "Standalone.tex",
            "\\documentclass{article}\n"
            "\\title{Short standalone}\n"
            "\\begin{document}\n"
            "Short body.\n"
            "\\end{document}\n",
        )
        for name in ("one", "two", "three", "four", "five"):
            self.write_tex(f"sections/{name}.tex", f"{name} section body\n")

        candidates = self.pipeline.rank_main_tex(self.root)

        self.assertEqual(candidates[0].path, whole_paper)
        self.assertEqual(candidates[0].outgoing_includes, 5)
        self.assertGreater(candidates[0].score, next(item.score for item in candidates if item.path == standalone))

    def test_rank_main_tex_prefers_root_main_over_nested_duplicate(self) -> None:
        content = (
            "\\documentclass{article}\n"
            "\\title{Whole paper}\n"
            "\\begin{document}\n"
            "Same complete body.\n"
            "\\end{document}\n"
        )
        root_main = self.write_tex("main.tex", content)
        nested_main = self.write_tex("archive/copy/main.tex", content)

        candidates = self.pipeline.rank_main_tex(self.root)

        self.assertEqual(candidates[0].path, root_main)
        self.assertEqual(candidates[0].path_depth, 0)
        self.assertEqual(next(item.path_depth for item in candidates if item.path == nested_main), 2)
        self.assertGreater(candidates[0].score, next(item.score for item in candidates if item.path == nested_main))


class ExpandTexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pipeline = load_pipeline(self)

    def write_tex(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_expand_tex_preserves_nested_include_order(self) -> None:
        main_path = self.write_tex("main.tex", "start\n\\input{sections/first}\n\\include{sections/second}\nend\n")
        self.write_tex("sections/first.tex", "first section\n\\input{sub/third}\n")
        self.write_tex("sections/sub/third.tex", "third nested\n")
        self.write_tex("sections/second.tex", "second section\n")

        result = self.pipeline.expand_tex(main_path, self.root)

        self.assertLess(result.text.index("first section"), result.text.index("third nested"))
        self.assertLess(result.text.index("third nested"), result.text.index("second section"))

    def test_expand_tex_reports_cycle_targets(self) -> None:
        main_path = self.write_tex("main.tex", "start\n\\input{loop}\nend\n")
        self.write_tex("loop.tex", "loop body\n\\input{main}\n")

        result = self.pipeline.expand_tex(main_path, self.root)

        self.assertEqual(result.cycles, (main_path,))
        self.assertEqual(result.text.count("loop body"), 1)

    def test_expand_tex_detects_cycle_through_parent_alias(self) -> None:
        main_path = self.write_tex("main.tex", "main body\n\\input{sub/../main}\n")
        (self.root / "sub").mkdir()

        result = self.pipeline.expand_tex(main_path, self.root)

        self.assertEqual(result.cycles, (main_path,))
        self.assertEqual(result.includes, ())
        self.assertEqual(result.text.count("main body"), 1)

    def test_expand_tex_reports_missing_includes(self) -> None:
        main_path = self.write_tex("main.tex", "start\n\\input{missing}\nend\n")

        result = self.pipeline.expand_tex(main_path, self.root)

        self.assertEqual(result.missing_includes, (self.root / "missing.tex",))
        self.assertIn("start", result.text)
        self.assertIn("end", result.text)

    def test_expand_tex_appends_tex_to_dotted_stem(self) -> None:
        main_path = self.write_tex("main.tex", "\\input{sections/03.1_results}\n")
        section = self.write_tex("sections/03.1_results.tex", "dotted section body\n")

        result = self.pipeline.expand_tex(main_path, self.root)

        self.assertEqual(result.includes, (section,))
        self.assertEqual(result.missing_includes, ())
        self.assertIn("dotted section body", result.text)

    def test_expand_tex_does_not_report_missing_optional_iffileexists_input(self) -> None:
        main_path = self.write_tex(
            "main.tex",
            "before\n\\IfFileExists{optional.tex}{\\input{optional.tex}}{}\nafter\n",
        )

        result = self.pipeline.expand_tex(main_path, self.root)

        self.assertEqual(result.missing_includes, ())
        self.assertIn("before", result.text)
        self.assertIn("after", result.text)

    def test_expand_tex_does_not_report_multiline_missing_optional_iffileexists_input(self) -> None:
        main_path = self.write_tex(
            "main.tex",
            "before\n\\IfFileExists{optional.tex}{\n  \\input{optional.tex}\n}{}\nafter\n",
        )

        result = self.pipeline.expand_tex(main_path, self.root)

        self.assertEqual(result.missing_includes, ())

    def test_expand_tex_expands_present_optional_iffileexists_input(self) -> None:
        main_path = self.write_tex(
            "main.tex",
            "\\IfFileExists{optional.tex}{\\input{optional.tex}}{}\n",
        )
        optional = self.write_tex("optional.tex", "optional body\n")

        result = self.pipeline.expand_tex(main_path, self.root)

        self.assertEqual(result.includes, (optional,))
        self.assertIn("optional body", result.text)

    def test_expand_tex_ignores_macro_parameter_and_known_external_input(self) -> None:
        main_path = self.write_tex(
            "main.tex",
            "\\def\\scaled#1{\\input #1pt.rtx}\n"
            "\\def\\sectionfile#1{\\input section#1}\n"
            "\\input epsf\n\\input epsf.tex\nbody\n",
        )

        result = self.pipeline.expand_tex(main_path, self.root)

        self.assertEqual(result.missing_includes, ())
        self.assertIn("body", result.text)

    def test_expand_tex_rejects_include_outside_root(self) -> None:
        main_path = self.write_tex("main.tex", "\\input{../escape}\n")

        with self.assertRaises(self.pipeline.UnsafeIncludeError):
            self.pipeline.expand_tex(main_path, self.root)

    def test_expand_tex_supports_unbraced_input_and_include(self) -> None:
        main_path = self.write_tex(
            "main.tex",
            "start\n\\input first\n\\include sections/second\nend\n",
        )
        first = self.write_tex("first.tex", "first body\n")
        second = self.write_tex("sections/second.tex", "second body\n")

        result = self.pipeline.expand_tex(main_path, self.root)

        self.assertIn("first body", result.text)
        self.assertIn("second body", result.text)
        self.assertEqual(result.includes, (first, second))

    def test_expand_tex_resolves_main_prefixed_subfiles_from_root(self) -> None:
        main_path = self.write_tex(
            "report.tex",
            "\\newcommand{\\main}{.}\n\\subfile{\\main/sections/first}\n",
        )
        first = self.write_tex(
            "sections/first.tex",
            "first body\n\\subfile{\\main/sections/second}\n",
        )
        second = self.write_tex("sections/second.tex", "second body\n")

        result = self.pipeline.expand_tex(main_path, self.root)

        self.assertIn("first body", result.text)
        self.assertIn("second body", result.text)
        self.assertEqual(result.includes, (first, second))

    def test_expand_tex_falls_back_to_root_for_nested_master_relative_input(self) -> None:
        main_path = self.write_tex("main.tex", "\\input chapters/overview\n")
        overview = self.write_tex(
            "chapters/overview.tex",
            "overview body\n\\input chapters/detail\n",
        )
        detail = self.write_tex("chapters/detail.tex", "detail body\n")

        result = self.pipeline.expand_tex(main_path, self.root)

        self.assertIn("overview body", result.text)
        self.assertIn("detail body", result.text)
        self.assertEqual(result.includes, (overview, detail))

    def test_expand_tex_rejects_main_prefixed_subfile_traversal(self) -> None:
        main_path = self.write_tex("report.tex", "\\subfile{\\main/../escape}\n")

        with self.assertRaises(self.pipeline.UnsafeIncludeError):
            self.pipeline.expand_tex(main_path, self.root)

    def test_expand_tex_ignores_commented_unbraced_inputs(self) -> None:
        main_path = self.write_tex(
            "main.tex",
            "% \\input hidden\n\\input visible % \\input hidden\n",
        )
        self.write_tex("hidden.tex", "hidden body\n")
        self.write_tex("visible.tex", "visible body\n")

        result = self.pipeline.expand_tex(main_path, self.root)

        self.assertIn("visible body", result.text)
        self.assertNotIn("hidden body", result.text)


class StructureProtectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pipeline = load_pipeline(self)

    def mapping_for_chunk(self, chunk: str, mapping: dict[str, str]) -> dict[str, str]:
        return {sentinel: value for sentinel, value in mapping.items() if sentinel in chunk}

    def test_protect_structures_round_trips_math_citations_refs_and_links(self) -> None:
        text = (
            "Inline math $p_T$ and display:\n"
            "\\[\nE = mc^2\n\\]\n"
            "See \\cite{atlas}, \\ref{sec:intro}, \\label{sec:intro}, "
            "https://example.com/paper?ref=1 and author@example.com.\n"
        )

        protected = self.pipeline.protect_structures(text)
        restored = self.pipeline.validate_and_restore(protected.text, protected.mapping)

        self.assertEqual(restored, text)
        for literal in (
            "$p_T$",
            "\\[\nE = mc^2\n\\]",
            "\\cite{atlas}",
            "\\ref{sec:intro}",
            "\\label{sec:intro}",
            "https://example.com/paper?ref=1",
            "author@example.com",
        ):
            self.assertNotIn(literal, protected.text)
        self.assertEqual(len(protected.mapping), 7)

    def test_protect_structures_round_trips_babeldoc_style_placeholders(self) -> None:
        text = "{v1}First style{v2}second style{v37}"

        protected = self.pipeline.protect_structures(text)

        self.assertNotIn("{v1}", protected.text)
        self.assertEqual(list(protected.mapping.values()), ["{v1}", "{v2}", "{v37}"])
        self.assertEqual(
            self.pipeline.validate_and_restore(protected.text, protected.mapping),
            text,
        )

    def test_parenthesized_babeldoc_placeholder_group_is_one_protected_structure(self) -> None:
        text = "Correction ({v1}({v2})) remains small."

        protected = self.pipeline.protect_structures(text)

        self.assertIn("({v1}({v2}))", protected.mapping.values())
        self.assertEqual(
            self.pipeline.validate_and_restore(protected.text, protected.mapping),
            text,
        )

    def test_protect_structures_keeps_url_separate_from_immediately_following_math(self) -> None:
        text = "See https://example.org/model$^{#3}$ next."

        protected = self.pipeline.protect_structures(text)

        self.assertEqual(
            list(protected.mapping.values()),
            ["https://example.org/model", "$^{#3}$"],
        )
        self.assertEqual(self.pipeline.validate_and_restore(protected.text, protected.mapping), text)

    def test_protect_structures_keeps_math_separate_from_immediately_following_url(self) -> None:
        text = "See $^{#3}$https://example.org/model next."

        protected = self.pipeline.protect_structures(text)

        self.assertEqual(
            list(protected.mapping.values()),
            ["$^{#3}$", "https://example.org/model"],
        )
        self.assertEqual(self.pipeline.validate_and_restore(protected.text, protected.mapping), text)

    def test_protect_structures_excludes_sentence_punctuation_from_url_sentinel(self) -> None:
        text = "See https://example.org/paper. Then continue."

        protected = self.pipeline.protect_structures(text)

        self.assertEqual(list(protected.mapping.values()), ["https://example.org/paper"])
        self.assertIn(next(iter(protected.mapping)) + ".", protected.text)
        self.assertEqual(self.pipeline.validate_and_restore(protected.text, protected.mapping), text)

    def test_protect_structures_balances_tex_url_without_outer_brace_or_chinese(self) -> None:
        text = r"Source {\url{https://example.org/a}}中文。"

        protected = self.pipeline.protect_structures(text)

        self.assertEqual(list(protected.mapping.values()), [r"\url{https://example.org/a}"])
        self.assertEqual(self.pipeline.validate_and_restore(protected.text, protected.mapping), text)

    def test_protect_structures_round_trips_tex_escaped_percent_outside_math(self) -> None:
        text = "The forecast reaches 1\\% precision.\n"

        protected = self.pipeline.protect_structures(text)

        self.assertEqual(list(protected.mapping.values()), [r"\%"])
        self.assertNotIn(r"\%", protected.text)
        self.assertEqual(self.pipeline.validate_and_restore(protected.text, protected.mapping), text)

    def test_protect_structures_round_trips_numeric_dimension_labels(self) -> None:
        text = "Compare 2D示踪物 with a 3D survey.\n"

        protected = self.pipeline.protect_structures(text)

        self.assertEqual(list(protected.mapping.values()), ["2D", "3D"])
        self.assertEqual(self.pipeline.validate_and_restore(protected.text, protected.mapping), text)

    def test_protect_structures_round_trips_decimal_and_unit_literals(self) -> None:
        text = "A 11.25 m mirror spans 1.1GHz and samples 2.9 billion objects.\n"

        protected = self.pipeline.protect_structures(text)

        self.assertEqual(list(protected.mapping.values()), ["11.25 m", "1.1GHz", "2.9"])
        self.assertEqual(self.pipeline.validate_and_restore(protected.text, protected.mapping), text)

    def test_restore_rejects_missing_sentinel(self) -> None:
        protected = self.pipeline.protect_structures("See \\cite{atlas} and $p_T$.")
        damaged = protected.text.replace(next(iter(protected.mapping)), "")

        with self.assertRaises(self.pipeline.StructureMismatchError):
            self.pipeline.validate_and_restore(damaged, protected.mapping)

    def test_restore_rejects_duplicate_sentinel(self) -> None:
        protected = self.pipeline.protect_structures("See \\cite{atlas} and $p_T$.")
        sentinel = next(iter(protected.mapping))
        duplicated = protected.text.replace(sentinel, f"{sentinel} {sentinel}", 1)

        with self.assertRaises(self.pipeline.StructureMismatchError):
            self.pipeline.validate_and_restore(duplicated, protected.mapping)

    def test_restore_accepts_reordered_distinct_sentinels_when_each_occurs_once(self) -> None:
        text = "See \\cite{atlas} before $p_T$."
        protected = self.pipeline.protect_structures(text)
        sentinels = sorted(protected.mapping, key=protected.text.index)
        swapped = protected.text.replace(sentinels[0], "<<SWAP>>", 1)
        swapped = swapped.replace(sentinels[1], sentinels[0], 1).replace("<<SWAP>>", sentinels[1], 1)

        self.assertEqual(self.pipeline.validate_and_restore(protected.text, protected.mapping), text)
        restored = self.pipeline.validate_and_restore(swapped, protected.mapping)
        self.assertIn(r"\cite{atlas}", restored)
        self.assertIn("$p_T$", restored)

    def test_semantic_chunks_keep_paragraphs_and_lists_whole(self) -> None:
        first_paragraph = "alpha beta gamma delta epsilon zeta"
        list_block = "- bullet one two\n- bullet three four"
        second_paragraph = "eta theta iota kappa lambda mu"
        text = f"{first_paragraph}\n\n{list_block}\n\n{second_paragraph}\n"

        chunks = self.pipeline.semantic_chunks(text, target_words=6, min_words=4, max_words=8)

        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0].strip(), first_paragraph)
        self.assertEqual(chunks[1].strip(), list_block)
        self.assertEqual(chunks[2].strip(), second_paragraph)

    def test_semantic_chunks_do_not_split_protected_spans(self) -> None:
        text = (
            "alpha beta gamma delta epsilon zeta\n\n"
            "This paragraph keeps $p_T$ with \\cite{atlas} intact across chunking.\n\n"
            "eta theta iota kappa lambda mu\n"
        )
        protected = self.pipeline.protect_structures(text)

        chunks = self.pipeline.semantic_chunks(protected.text, target_words=8, min_words=4, max_words=10)

        sentinel_hits = {
            sentinel: sum(1 for chunk in chunks if sentinel in chunk) for sentinel in protected.mapping
        }
        self.assertEqual(set(sentinel_hits.values()), {1})
        restored = [
            self.pipeline.validate_and_restore(chunk, self.mapping_for_chunk(chunk, protected.mapping)) for chunk in chunks
        ]
        self.assertIn("$p_T$", restored[1])
        self.assertIn("\\cite{atlas}", restored[1])


class PrepareSnowmassChunksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "sources"
        self.translation_root = Path(self.temporary.name) / "translation"
        self.root.mkdir()
        self.translation_root.mkdir()
        self.preparer = load_chunk_preparer(self)

    def write_source_manifest(self, records: list[dict[str, object]]) -> None:
        (self.root / "manifest.json").write_text(
            json.dumps({"schema_version": 1, "records": records}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def write_rights_snapshot(self, record_ids: list[str]) -> Path:
        snapshot_path = Path(self.temporary.name) / "rights_snapshot.json"
        snapshot_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_manifest_path": "site/data/papers.json",
                    "source_manifest_sha256": "deadbeef",
                    "created_at": "2026-08-10T00:00:00+00:00",
                    "eligible_count": len(record_ids),
                    "records": [
                        {"record_id": record_id, "publication_allowed": True} for record_id in record_ids
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return snapshot_path

    def test_rights_snapshot_requires_literal_publication_allowed_true(self) -> None:
        snapshot_path = Path(self.temporary.name) / "mixed_rights_snapshot.json"
        snapshot_path.write_text(
            json.dumps(
                {
                    "records": [
                        {"record_id": "arxiv:allowed", "publication_allowed": True},
                        {"record_id": "arxiv:false", "publication_allowed": False},
                        {"record_id": "arxiv:null", "publication_allowed": None},
                        {"record_id": "arxiv:missing"},
                    ]
                }
            )
            + "\n",
            encoding="utf-8",
        )

        self.assertEqual(
            self.preparer.load_rights_snapshot_record_ids(snapshot_path),
            {"arxiv:allowed"},
        )

    def write_archive(self, directory: str, members: list[tuple[str, bytes]]) -> None:
        item_dir = self.root / directory
        item_dir.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for member_name, payload in members:
                info = tarfile.TarInfo(member_name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        (item_dir / "source.tar.gz").write_bytes(gzip.compress(buffer.getvalue()))

    def write_invalid_archive(self, directory: str, payload: bytes) -> None:
        item_dir = self.root / directory
        item_dir.mkdir(parents=True, exist_ok=True)
        (item_dir / "source.tar.gz").write_bytes(gzip.compress(payload))

    def write_pdf_text(self, directory: str, text: str) -> None:
        item_dir = self.root / directory
        item_dir.mkdir(parents=True, exist_ok=True)
        (item_dir / "source.txt").write_text(text, encoding="utf-8")

    def test_build_one_prefers_source_archive_and_records_source_kind(self) -> None:
        record = {"record_id": "arxiv:allowed", "directory": "papers/arxiv_allowed"}
        self.write_pdf_text(record["directory"], "PDF fallback text only.\n")
        self.write_archive(
            record["directory"],
            [
                (
                    "main.tex",
                    (
                        "\\documentclass{article}\n"
                        "\\begin{document}\n"
                        "Archive preferred body.\n"
                        "\\input{sections/intro}\n"
                        "\\end{document}\n"
                    ).encode("utf-8"),
                ),
                ("sections/intro.tex", b"Nested section body.\n"),
            ],
        )

        status = self.preparer.build_one(record, self.root, self.translation_root)
        article_dir = self.translation_root / record["directory"]

        self.assertEqual(status["source_kind"], "expanded_tex")
        self.assertIsNone(status["fallback_reason"])
        source_md = (article_dir / "source.md").read_text(encoding="utf-8")
        self.assertIn("Archive preferred body.", source_md)
        self.assertIn("Nested section body.", source_md)
        self.assertNotIn("PDF fallback text only.", source_md)

    def test_build_one_falls_back_to_pdf_text_and_records_reason(self) -> None:
        record = {"record_id": "arxiv:fallback", "directory": "papers/arxiv_fallback"}
        self.write_pdf_text(record["directory"], "PDF fallback survives.\n")
        self.write_invalid_archive(record["directory"], b"not a tex source archive")

        status = self.preparer.build_one(record, self.root, self.translation_root)
        article_dir = self.translation_root / record["directory"]

        self.assertEqual(status["source_kind"], "pdf_text")
        self.assertEqual(status["fallback_reason"], "archive_unusable")
        self.assertEqual((article_dir / "source.md").read_text(encoding="utf-8"), "PDF fallback survives.\n")

    def test_build_one_rechunks_when_chunk_size_contract_changes(self) -> None:
        record = {"record_id": "arxiv:sized", "directory": "papers/arxiv_sized"}
        self.write_pdf_text(record["directory"], "One two three four five six seven eight nine ten.\n")

        first = self.preparer.build_one(
            record,
            self.root,
            self.translation_root,
            target_words=8,
            min_words=4,
            max_words=10,
        )
        second = self.preparer.build_one(
            record,
            self.root,
            self.translation_root,
            target_words=4,
            min_words=2,
            max_words=5,
        )

        self.assertFalse(first["reused"])
        self.assertFalse(second["reused"])
        self.assertEqual(second["chunk_target_words"], 4)
        self.assertEqual(second["chunk_min_words"], 2)
        self.assertEqual(second["chunk_max_words"], 5)

    def test_build_one_refuses_to_replace_translated_outputs_when_source_hash_changes(self) -> None:
        record = {"record_id": "arxiv:translated", "directory": "papers/arxiv_translated"}
        self.write_pdf_text(record["directory"], "PDF fallback text.\n")
        self.write_archive(
            record["directory"],
            [("main.tex", b"\\documentclass{article}\n\\begin{document}\nFirst archive text.\n\\end{document}\n")],
        )

        self.preparer.build_one(record, self.root, self.translation_root)
        article_dir = self.translation_root / record["directory"]
        (article_dir / "output_chunk0001.md").write_text("Existing translation stays put.\n", encoding="utf-8")

        self.write_archive(
            record["directory"],
            [("main.tex", b"\\documentclass{article}\n\\begin{document}\nSecond archive text.\n\\end{document}\n")],
        )

        with self.assertRaisesRegex(RuntimeError, "manual review required"):
            self.preparer.build_one(record, self.root, self.translation_root)

        self.assertEqual(
            (article_dir / "output_chunk0001.md").read_text(encoding="utf-8"),
            "Existing translation stays put.\n",
        )
        self.assertIn("First archive text.", (article_dir / "source.md").read_text(encoding="utf-8"))

    def test_main_processes_only_records_from_rights_snapshot(self) -> None:
        allowed = {"record_id": "arxiv:allowed", "directory": "papers/arxiv_allowed"}
        blocked = {"record_id": "arxiv:blocked", "directory": "papers/arxiv_blocked"}
        self.write_source_manifest([allowed, blocked])
        self.write_pdf_text(allowed["directory"], "Allowed record text.\n")
        self.write_pdf_text(blocked["directory"], "Blocked record text.\n")
        self.write_invalid_archive(allowed["directory"], b"invalid archive")
        self.write_invalid_archive(blocked["directory"], b"invalid archive")
        rights_snapshot = self.write_rights_snapshot(["arxiv:allowed"])

        with mock.patch.object(
            sys,
            "argv",
            [
                "prepare_snowmass_chunks.py",
                "--root",
                str(self.root),
                "--translation-root",
                str(self.translation_root),
                "--rights-snapshot",
                str(rights_snapshot),
                "--workers",
                "1",
            ],
        ):
            exit_code = self.preparer.main()

        self.assertEqual(exit_code, 0)
        summary = json.loads((self.translation_root / "chunk_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["total_records"], 1)
        self.assertTrue((self.translation_root / allowed["directory"]).exists())
        self.assertFalse((self.translation_root / blocked["directory"]).exists())

    def test_main_can_select_one_allowed_record_and_custom_chunk_sizes(self) -> None:
        first = {"record_id": "arxiv:first", "directory": "papers/arxiv_first"}
        second = {"record_id": "arxiv:second", "directory": "papers/arxiv_second"}
        self.write_source_manifest([first, second])
        self.write_pdf_text(first["directory"], "First paper text.\n")
        self.write_pdf_text(second["directory"], "Second paper text.\n")
        rights_snapshot = self.write_rights_snapshot(["arxiv:first", "arxiv:second"])

        exit_code = self.preparer.main(
            [
                "--root", str(self.root),
                "--translation-root", str(self.translation_root),
                "--rights-snapshot", str(rights_snapshot),
                "--record-id", "arxiv:second",
                "--target-words", "600",
                "--min-words", "400",
                "--max-words", "800",
                "--workers", "1",
            ]
        )

        self.assertEqual(exit_code, 0)
        summary = json.loads((self.translation_root / "chunk_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["total_records"], 1)
        status = json.loads(
            (self.translation_root / second["directory"] / "chunking_status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(status["chunk_target_words"], 600)
        self.assertFalse((self.translation_root / first["directory"]).exists())


if __name__ == "__main__":
    unittest.main()
