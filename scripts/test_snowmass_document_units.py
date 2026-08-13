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
    def test_parenthesized_reference_labels_are_one_immutable_node(self) -> None:
        units = load_units()

        protected = units.protect_translation_unit(
            "See Eq. (2.6), Eq. (B.63), and Fig. 4(b)."
        )

        self.assertEqual(
            [(node.kind, node.value) for node in protected.nodes],
            [
                ("reference_label", "(2.6)"),
                ("reference_label", "(B.63)"),
                ("number", "4"),
                ("reference_label", "(b)"),
            ],
        )
        self.assertEqual(
            units.restore_translation_unit(protected.text, protected.nodes),
            "See Eq. (2.6), Eq. (B.63), and Fig. 4(b).",
        )

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

    def test_hyphenated_scientific_identifiers_are_single_immutable_nodes(self) -> None:
        units = load_units()

        protected = units.protect_translation_unit(
            "DESI-2, PUMA-32K, and CMB-S4 span the first 1-2 years."
        )

        self.assertEqual(
            [(node.kind, node.value) for node in protected.nodes],
            [
                ("identifier", "DESI-2"),
                ("identifier", "PUMA-32K"),
                ("identifier", "CMB-S4"),
                ("number", "1"),
                ("number", "-2"),
            ],
        )
        self.assertEqual(
            units.restore_translation_unit(protected.text, protected.nodes),
            "DESI-2, PUMA-32K, and CMB-S4 span the first 1-2 years.",
        )

    def test_pdf_spaced_scientific_identifier_remains_one_immutable_node(self) -> None:
        units = load_units()

        protected = units.protect_translation_unit("PAMELA and AMS- 02 measurements")

        self.assertEqual(
            [(node.kind, node.value) for node in protected.nodes],
            [("identifier", "AMS- 02")],
        )

    def test_pdf_glued_words_do_not_hide_or_extend_scientific_identifiers(self) -> None:
        units = load_units()
        source = "9 Rubin Observatory ImagingtoEnableaDESI-2Survey"

        protected = units.protect_translation_unit(source)

        self.assertEqual(
            [(node.kind, node.value) for node in protected.nodes],
            [("number", "9"), ("identifier", "DESI-2")],
        )
        comparison = units.compare_numeric_literals(
            source,
            "9 鲁宾观测台成像以支持DESI-2巡天",
        )
        self.assertTrue(comparison.values_equal)

    def test_number_unit_literals_are_one_immutable_node(self) -> None:
        units = load_units()

        protected = units.protect_translation_unit(
            "above 400 GeV, 1Tev, 10%, and 2 TeV"
        )

        self.assertEqual(
            [(node.kind, node.value) for node in protected.nodes],
            [
                ("unit", "400 GeV"),
                ("unit", "1Tev"),
                ("unit", "10%"),
                ("unit", "2 TeV"),
            ],
        )
        self.assertEqual(
            units.restore_translation_unit(protected.text, protected.nodes),
            "above 400 GeV, 1Tev, 10%, and 2 TeV",
        )

    def test_numbers_glued_to_pdf_text_are_still_protected_without_nesting_sentinels(self) -> None:
        units = load_units()
        sentinel = "[[SM_0001_0123456789]]"

        protected = units.protect_translation_unit(
            f"with0.2 GHz, NY11973, PUMA-32K and {sentinel}"
        )

        self.assertEqual(
            [(node.kind, node.value) for node in protected.nodes],
            [
                ("unit", "0.2 GHz"),
                ("number", "11973"),
                ("identifier", "PUMA-32K"),
            ],
        )
        self.assertIn(sentinel, protected.text)
        self.assertEqual(units.restore_translation_unit(protected.text, protected.nodes), f"with0.2 GHz, NY11973, PUMA-32K and {sentinel}")

    def test_unit_glued_after_pdf_word_is_one_typed_node(self) -> None:
        units = load_units()

        protected = units.protect_translation_unit("extends down to10GeV")

        self.assertEqual(
            [(node.kind, node.value) for node in protected.nodes],
            [("unit", "10GeV")],
        )

    def test_structure_dense_unit_is_rejected_before_model_submission(self) -> None:
        units = load_units()
        source = " ".join(str(index) for index in range(41))

        with self.assertRaisesRegex(units.StructureDensityError, "41.*40"):
            units.protect_translation_unit(source, max_nodes=40)


class NumericComparisonTests(unittest.TestCase):
    def test_spin_hyphen_number_is_identifier_not_negative_numeric_literal(self) -> None:
        units = load_units()

        result = units.compare_numeric_literals(
            "Massive Spin-1 Particles",
            "大质量自旋-1粒子",
        )

        self.assertTrue(result.values_equal)
        self.assertEqual(result.missing_values, ())
        self.assertEqual(result.added_values, ())

    def test_twist_modifier_keeps_same_semantics_across_scripts(self) -> None:
        units = load_units()

        result = units.compare_numeric_literals(
            "a beyond-twist-2 phenomenon",
            "一种超越扭转-2的现象",
        )

        self.assertTrue(result.values_equal)
        self.assertEqual(result.missing_values, ())
        self.assertEqual(result.added_values, ())

    def test_lowercase_phase_modifier_adjacent_to_chinese_is_not_negative(self) -> None:
        units = load_units()

        result = units.compare_numeric_literals(
            "The Phase-1 detector is ready.",
            "该Phase-1探测器已就绪。",
        )

        self.assertTrue(result.values_equal)
        self.assertEqual(result.missing_values, ())
        self.assertEqual(result.added_values, ())

    def test_domain_word_unity_may_be_rendered_as_arabic_one(self) -> None:
        units = load_units()

        result = units.compare_numeric_literals(
            "The enhancement approaches unity.",
            "该增强效应趋近于1。",
        )

        self.assertTrue(result.values_equal)
        self.assertEqual(result.missing_values, ())
        self.assertEqual(result.added_values, ())

    def test_unity_allowance_does_not_hide_an_additional_one(self) -> None:
        units = load_units()

        result = units.compare_numeric_literals(
            "The enhancement approaches unity.",
            "该增强效应在1个额外条件下趋近于1。",
        )

        self.assertFalse(result.values_equal)
        self.assertEqual(result.added_values, ("1",))

    def test_pdf_glued_number_has_same_value_after_chinese_spacing_changes(self) -> None:
        units = load_units()

        result = units.compare_numeric_literals("with0.2 GHz at NY11973", "配备0.2 GHz，位于纽约11973")

        self.assertTrue(result.values_equal)

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

    def test_hyphenated_numeric_modifier_is_not_a_negative_number(self) -> None:
        units = load_units()

        modifier = units.compare_numeric_literals(
            "The estimate has sub-2% uncertainty.",
            "该估计的不确定度低于2%。",
        )
        negative = units.compare_numeric_literals(
            "The shift is -2%.",
            "该偏移为2%。",
        )

        self.assertTrue(modifier.values_equal)
        self.assertFalse(negative.values_equal)
        self.assertEqual(negative.missing_values, ("-2%",))

    def test_chinese_mid_decade_connector_is_not_a_negative_year(self) -> None:
        units = load_units()

        result = units.compare_numeric_literals(
            "operations begin in the mid-2030 and mid/end-2040",
            "运行始于中叶-2030和中叶/末-2040",
        )
        genuine_negative = units.compare_numeric_literals(
            "the temperature is -2040 K",
            "温度为2040 K",
        )

        self.assertTrue(result.values_equal)
        self.assertFalse(genuine_negative.values_equal)
        self.assertEqual(genuine_negative.missing_values, ("-2040",))


if __name__ == "__main__":
    unittest.main()
