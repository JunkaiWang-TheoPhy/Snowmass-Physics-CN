#!/usr/bin/env python3
"""Behavior tests for structured Snowmass translation units."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).with_name("snowmass_document_units.py")


def load_units():
    if not MODULE_PATH.exists():
        raise AssertionError("structured translation-unit module is not implemented")
    spec = importlib.util.spec_from_file_location("snowmass_document_units", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PublishedTexTests(unittest.TestCase):
    def test_comment_environment_never_enters_published_body(self) -> None:
        units = load_units()
        source = (
            "Visible introduction.\n"
            "\\begin{comment}\n"
            "Hidden duplicate table with 2800 and \\url{https://hidden.example}.\n"
            "\\end{comment}\n"
            "Visible conclusion with 34000.\n"
        )

        published = units.published_tex_body(source)

        self.assertEqual(
            published,
            "Visible introduction.\n\nVisible conclusion with 34000.\n",
        )


class TypedProtectionTests(unittest.TestCase):
    def test_balanced_tex_url_stops_before_outer_brace_and_chinese_text(self) -> None:
        units = load_units()
        source = "由 \\footnote{\\url{https://example.org/a_b}}，随后计算 2800 个样本。"

        protected = units.protect_translation_unit(source)

        self.assertEqual([node.kind for node in protected.nodes], ["tex_url", "number"])
        self.assertEqual(protected.nodes[0].value, "\\url{https://example.org/a_b}")
        self.assertEqual(protected.nodes[1].value, "2800")
        self.assertIn("}，随后计算 ", protected.text)
        self.assertNotIn("https://example.org/a_b}}，随后", protected.nodes[0].value)
        self.assertEqual(units.restore_translation_unit(protected.text, protected.nodes), source)

    def test_plain_integers_are_protected_not_only_decimals_and_units(self) -> None:
        units = load_units()

        protected = units.protect_translation_unit("2800, 34,000, 2.9 and 6e4")

        self.assertEqual(
            [(node.kind, node.value) for node in protected.nodes],
            [
                ("number", "2800"),
                ("number", "34,000"),
                ("number", "2.9"),
                ("number", "6e4"),
            ],
        )

    def test_structure_dense_unit_is_rejected_before_model_submission(self) -> None:
        units = load_units()
        source = " ".join(str(index) for index in range(41))

        with self.assertRaisesRegex(units.StructureDensityError, "41.*40"):
            units.protect_translation_unit(source, max_nodes=40)


class NumericComparisonTests(unittest.TestCase):
    def test_thousands_separator_change_is_format_drift_not_value_loss(self) -> None:
        units = load_units()

        result = units.compare_numeric_literals(
            "The samples are 2800 and 34000.",
            "样本数为 2,800 和 34,000。",
        )

        self.assertTrue(result.values_equal)
        self.assertTrue(result.format_changed)
        self.assertEqual(result.missing_values, ())
        self.assertEqual(result.added_values, ())

    def test_replacing_arabic_integer_with_chinese_word_is_value_loss(self) -> None:
        units = load_units()

        result = units.compare_numeric_literals(
            "by a factor of 2",
            "提高两倍",
        )

        self.assertFalse(result.values_equal)
        self.assertEqual(result.missing_values, ("2",))
        self.assertEqual(result.added_values, ())


if __name__ == "__main__":
    unittest.main()
