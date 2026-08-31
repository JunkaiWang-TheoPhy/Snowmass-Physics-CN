#!/usr/bin/env python3
"""Contract tests for the isolated PDFMathTranslate-next A/B runner."""

from __future__ import annotations

import asyncio
import csv
import importlib.util
import json
import math
import multiprocessing
import os
import sys
import tempfile
import types
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

MODULE_PATH = Path(__file__).with_name("run_snowmass_pdf2zh_next_ab.py")


def report_spawned_transport(settings, queue, send_probe: bool = False) -> None:
    import openai
    from pdf2zh_next.translator.rate_limiter.qps_rate_limiter import QPSRateLimiter
    from pdf2zh_next.translator.translator_impl.openai import OpenAITranslator

    translator = OpenAITranslator(settings, QPSRateLimiter(1))
    report = {
        "settings_base_url": settings.translate_engine_settings.openai_base_url,
        "client_base_url": str(translator.client.base_url),
        "proxy_environment_names": sorted(
            name
            for name in (
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "http_proxy",
                "https_proxy",
                "all_proxy",
                "NO_PROXY",
                "no_proxy",
            )
            if name in os.environ
        ),
    }
    if send_probe:
        try:
            translator.client.chat.completions.create(
                model="deepseek-v4-flash",
                messages=[{"role": "user", "content": "diagnostic"}],
            )
        except openai.APIError as error:
            report["probe_error_type"] = type(error).__name__
            report["probe_status_code"] = getattr(error, "status_code", None)
        else:
            report["probe_completed"] = True
    queue.put(report)


def load_module():
    if not MODULE_PATH.exists():
        raise AssertionError("pdf2zh-next A/B runner is not implemented")
    spec = importlib.util.spec_from_file_location(
        "run_snowmass_pdf2zh_next_ab", MODULE_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class DeepSeekConnectivityTests(unittest.TestCase):
    def test_zero_paid_models_check_passes(self) -> None:
        module = load_module()
        calls = []

        def requester(url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(status_code=200)

        result = module.check_deepseek_connectivity("secret-key", requester=requester)
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["zero_paid"])
        self.assertEqual(calls[0][0], "https://api.deepseek.com/models")
        self.assertEqual(calls[0][1]["timeout"], 10)
        self.assertFalse(calls[0][1]["trust_env"])
        self.assertIn("secret-key", calls[0][1]["headers"]["Authorization"])

    def test_non_success_is_fail_closed_without_key_in_error(self) -> None:
        module = load_module()
        with self.assertRaisesRegex(RuntimeError, "HTTP 503") as context:
            module.check_deepseek_connectivity(
                "secret-key", requester=lambda *_args, **_kwargs: SimpleNamespace(status_code=503)
            )
        self.assertNotIn("secret-key", str(context.exception))

    def test_transport_failure_is_sanitized(self) -> None:
        module = load_module()
        with self.assertRaisesRegex(RuntimeError, "before paid work") as context:
            module.check_deepseek_connectivity(
                "secret-key",
                requester=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("Authorization: secret-key; timed out")
                ),
                sleeper=lambda _delay: None,
            )
        self.assertNotIn("secret-key", str(context.exception))

    def test_transient_connectivity_failure_retries_before_paid_work(self) -> None:
        module = load_module()
        statuses = iter([503, 503, 200])
        delays = []

        result = module.check_deepseek_connectivity(
            "secret-key",
            requester=lambda *_args, **_kwargs: SimpleNamespace(
                status_code=next(statuses)
            ),
            sleeper=delays.append,
        )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["attempt"], 3)
        self.assertEqual(delays, [2.0, 5.0])


class HttpxTimeoutContractTests(unittest.TestCase):
    def test_async_client_isolated_and_clamped_to_upstream_timeout(self) -> None:
        module = load_module()
        seen = {}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                seen.update(kwargs)

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                seen.update(kwargs)

        fake_httpx = types.ModuleType("httpx")
        fake_httpx.Client = FakeClient
        fake_httpx.AsyncClient = FakeAsyncClient
        with mock.patch.dict(sys.modules, {"httpx": fake_httpx}):
            with module.httpx_disable_environment_proxy():
                fake_httpx.AsyncClient(timeout=600, trust_env=True)

        self.assertEqual(seen["timeout"], module.UPSTREAM_REQUEST_TIMEOUT_SECONDS)
        self.assertFalse(seen["trust_env"])


class TranslationPromptContractTests(unittest.TestCase):
    def test_requires_citation_markers_to_keep_source_order(self) -> None:
        module = load_module()

        self.assertIn(
            "citation markers in their exact source order",
            module.SYSTEM_PROMPT,
        )

    def test_requires_translation_of_prose_quotations(self) -> None:
        module = load_module()

        self.assertIn("Translate prose quotations", module.SYSTEM_PROMPT)

    def test_declares_transport_citation_tokens_immutable(self) -> None:
        module = load_module()

        self.assertIn("Transport citation-anchor tokens are immutable", module.SYSTEM_PROMPT)
        self.assertIn("exactly once, in the same", module.SYSTEM_PROMPT)


class CitationLockTests(unittest.TestCase):
    def test_locks_babeldoc_rich_placeholders_in_the_same_structure_sequence(self) -> None:
        module = load_module()
        locked, structure_lock = module.lock_numeric_citations(
            [
                {
                    "role": "user",
                    "content": "Claim {v1} [58], formula {v2}:::{v3}, end {v4}.",
                }
            ]
        )

        self.assertEqual(structure_lock.markers, ("[58]",))
        self.assertEqual(
            structure_lock.placeholders,
            ("{v1}", "{v2}:::{v3}", "{v4}"),
        )
        self.assertIn("{v1}", locked[0]["content"])
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": locked[0]["content"],
                    }
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        restored = module.unlock_numeric_citations_response(
            json.dumps(response).encode(), structure_lock
        )
        self.assertIn(
            "{v2}:::{v3}",
            json.loads(restored)["choices"][0]["message"]["content"],
        )

        reordered = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Claim {v4} " + structure_lock.tokens[0] + " end {v1} {v2}:::{v3}",
                    }
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        with self.assertRaises(module.CitationLockError):
            module.unlock_numeric_citations_response(
                json.dumps(reordered).encode(), structure_lock
            )

    def test_locks_and_restores_numeric_citations_byte_exactly_in_source_order(self) -> None:
        module = load_module()
        messages = [
            {"role": "system", "content": "Preserve citations."},
            {
                "role": "user",
                "content": "First [58]; second [21, 59]; range [30–31].",
            },
        ]

        locked, citation_lock = module.lock_numeric_citations(messages)

        locked_text = locked[1]["content"]
        self.assertNotIn("[58]", locked_text)
        self.assertEqual(citation_lock.markers, ("[58]", "[21, 59]", "[30–31]"))
        response = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": locked_text.replace("First", "第一").replace("second", "第二"),
                    }
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        restored = module.unlock_numeric_citations_response(
            json.dumps(response).encode("utf-8"), citation_lock
        )
        restored_text = json.loads(restored)["choices"][0]["message"]["content"]
        self.assertIn("[58]", restored_text)
        self.assertIn("[21, 59]", restored_text)
        self.assertIn("[30–31]", restored_text)

    def test_rejects_missing_reordered_or_invented_citation_markers(self) -> None:
        module = load_module()
        locked, citation_lock = module.lock_numeric_citations(
            [{"role": "user", "content": "A [58], then B [21]."}]
        )
        tokens = citation_lock.tokens
        for content in (
            f"A {tokens[1]}, then B {tokens[0]}.",
            f"A {tokens[0]} only.",
            f"A {tokens[0]}, then B {tokens[1]} and invented [99].",
        ):
            response = {
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            with self.assertRaises(module.CitationLockError):
                module.unlock_numeric_citations_response(
                    json.dumps(response).encode("utf-8"), citation_lock
                )


class RightsGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.manifest = Path(self.temporary.name) / "papers.json"

    def write_records(self, records: list[dict]) -> None:
        self.manifest.write_text(json.dumps(records), encoding="utf-8")

    def test_only_literal_true_is_publishable(self) -> None:
        module = load_module()
        self.write_records(
            [
                {"record_id": "arxiv:allowed", "publication_allowed": True},
                {"record_id": "arxiv:false", "publication_allowed": False},
                {"record_id": "arxiv:null", "publication_allowed": None},
                {"record_id": "arxiv:missing"},
            ]
        )

        self.assertEqual(
            module.require_publication_allowed(self.manifest, "arXiv:allowed")[
                "record_id"
            ],
            "arxiv:allowed",
        )
        for blocked in ("arxiv:false", "arxiv:null", "arxiv:missing", "arxiv:unknown"):
            with (
                self.subTest(blocked=blocked),
                self.assertRaises(module.PublicationBlockedError),
            ):
                module.require_publication_allowed(self.manifest, blocked)

    def test_source_pdf_must_match_trusted_source_manifest(self) -> None:
        module = load_module()
        source = Path(self.temporary.name) / "allowed.pdf"
        source.write_bytes(b"trusted source")
        source_manifest = Path(self.temporary.name) / "sources.json"
        source_manifest.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "record_id": "arxiv:allowed",
                            "pdf_status": "complete",
                            "pdf_bytes": source.stat().st_size,
                            "pdf_sha256": module.sha256_file(source),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        identity = module.require_source_identity(
            source_manifest, "arxiv:allowed", source
        )
        self.assertEqual(identity["pdf_sha256"], module.sha256_file(source))
        source.write_bytes(b"different paper renamed as allowed.pdf")
        with self.assertRaises(module.PublicationBlockedError):
            module.require_source_identity(source_manifest, "arxiv:allowed", source)
        with self.assertRaises(module.PublicationBlockedError):
            module.require_source_identity(source_manifest, "arxiv:blocked", source)


class BudgetAndPricingTests(unittest.TestCase):
    def test_budgets_are_finite_positive_and_capped(self) -> None:
        module = load_module()
        for project, stage in (
            (0, 1),
            (math.nan, 1),
            (math.inf, 1),
            (100.01, 1),
            (100, 0),
            (100, math.inf),
            (100, 100.01),
            (5, 5.01),
        ):
            with (
                self.subTest(project=project, stage=stage),
                self.assertRaises(ValueError),
            ):
                module.validate_budgets(project, stage)
        self.assertEqual(module.validate_budgets(100, 100), (100.0, 100.0))

    def test_request_cap_is_finite_positive_integer(self) -> None:
        module = load_module()
        for value in (0, -1, 1.5, math.nan, math.inf, True, "unlimited"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.validate_request_cap(value)
        self.assertEqual(module.validate_request_cap(250), 250)

    def test_v4_flash_price_schedule_uses_official_cutover_and_windows(self) -> None:
        module = load_module()
        before = module.pricing_for_utc(
            datetime(2026, 8, 16, 15, 59, 59, tzinfo=timezone.utc)
        )
        off_peak = module.pricing_for_utc(
            datetime(2026, 8, 16, 16, 0, 0, tzinfo=timezone.utc)
        )
        peak = module.pricing_for_utc(
            datetime(2026, 8, 17, 1, 30, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(before["input_cache_miss_usd_per_million"], 0.14)
        self.assertEqual(off_peak["output_usd_per_million"], 0.66)
        self.assertEqual(peak["output_usd_per_million"], 1.32)
        self.assertEqual(
            module.conservative_pricing()["input_cache_miss_usd_per_million"], 0.44
        )

    def test_cost_uses_cached_and_uncached_input_separately(self) -> None:
        module = load_module()
        pricing = {
            "input_cache_hit_usd_per_million": 0.01,
            "input_cache_miss_usd_per_million": 0.5,
            "output_usd_per_million": 1.0,
        }
        cost = module.cost_rmb(
            prompt_tokens=1_000_000,
            cache_hit_prompt_tokens=250_000,
            completion_tokens=500_000,
            pricing=pricing,
            usd_cny_rate=7.2,
        )
        self.assertAlmostEqual(cost, ((0.25 * 0.01) + (0.75 * 0.5) + 0.5) * 7.2)


class SafeConfigurationTests(unittest.TestCase):
    def test_official_engine_spec_is_locked_and_secret_free(self) -> None:
        module = load_module()
        spec = module.build_safe_settings_spec(
            output_dir=Path("/tmp/out"),
            glossary_csv=Path("/tmp/glossary.csv"),
            pages="1,2,6,8,11,19-20",
            qps=2,
            pool_max_workers=2,
        )
        serialized = json.dumps(spec, sort_keys=True)
        self.assertEqual(spec["engine"]["model"], "deepseek-v4-flash")
        self.assertEqual(spec["engine"]["thinking_mode"], "disabled")
        self.assertEqual(spec["translation"]["qps"], 2)
        self.assertEqual(spec["translation"]["pool_max_workers"], 2)
        self.assertTrue(spec["translation"]["no_auto_extract_glossary"])
        self.assertEqual(spec["translation"]["primary_font_family"], "serif")
        self.assertFalse(spec["pdf"]["translate_table_text"])
        self.assertEqual(spec["pdf"]["figure_table_protection_threshold"], 0.95)
        self.assertTrue(spec["pdf"]["only_include_translated_page"])
        self.assertTrue(spec["basic"]["debug"])
        self.assertTrue(spec["translation"]["ignore_cache"])
        self.assertNotIn("api_key", serialized.lower())
        self.assertNotIn("sk-", serialized.lower())

    def test_glossary_conversion_is_deterministic_and_csv_safe(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "glossary.json"
            target = Path(temporary) / "glossary.csv"
            source.write_text(
                json.dumps(
                    {
                        "terms": [
                            {"source": "dark energy", "target": "暗能量"},
                            {"source": "quoted, term", "target": "含“引号”,术语"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            first_hash = module.materialize_glossary_csv(source, target)
            first = target.read_bytes()
            second_hash = module.materialize_glossary_csv(source, target)
            self.assertEqual(first, target.read_bytes())
            self.assertEqual(first_hash, second_hash)
            self.assertTrue(first.startswith(b"source,target,tgt_lng\r\n"))

    def test_glossary_conversion_merges_per_paper_aliases(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            global_glossary = root / "global.json"
            paper_glossary = root / "paper.json"
            target = root / "locked.csv"
            global_glossary.write_text(
                json.dumps(
                    {
                        "terms": [
                            {"source": "dark matter", "target": "暗物质"},
                            {"source": "observatory", "target": "观测台"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            paper_glossary.write_text(
                json.dumps(
                    {
                        "terms": [
                            {
                                "source": "light relic",
                                "target": "轻遗迹粒子",
                                "aliases": ["light relics", "light relic particles"],
                            },
                            {"source": "observatory", "target": "天文台"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            module.materialize_glossary_csv(
                (global_glossary, paper_glossary), target
            )

            with target.open(encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(
                [row["source"] for row in rows],
                [
                    "dark matter",
                    "observatory",
                    "light relic",
                    "light relics",
                    "light relic particles",
                ],
            )
            self.assertEqual(rows[1]["target"], "天文台")

    def test_secret_redaction_is_recursive(self) -> None:
        module = load_module()
        secret = "sk-secret-value"
        value = {
            "api_key": secret,
            "nested": [{"authorization": f"Bearer {secret}"}],
            "usage": {"total_tokens": 123},
            "safe": "ok",
        }
        sanitized = module.sanitize_for_receipt(value, secrets=[secret])
        encoded = json.dumps(sanitized)
        self.assertNotIn(secret, encoded)
        self.assertEqual(sanitized["safe"], "ok")
        self.assertEqual(sanitized["usage"]["total_tokens"], 123)


class RequestBudgetGateTests(unittest.TestCase):
    def test_transport_gate_injects_output_cap_and_enforces_request_cap(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            captured: list[dict] = []

            def create(**kwargs):
                captured.append(kwargs)
                return SimpleNamespace(
                    usage=SimpleNamespace(
                        prompt_tokens=100,
                        completion_tokens=20,
                        total_tokens=120,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=40),
                    )
                )

            gate = module.RequestBudgetGate(
                ledger_path=Path(temporary) / "ledger.jsonl",
                stage_max_cost_rmb=1,
                project_max_cost_rmb=100,
                project_commitment_before_rmb=10,
                request_cap=1,
                usd_cny_rate=7.2,
            )
            guarded = gate.wrap_create(create)
            guarded(
                messages=[{"role": "user", "content": "translate"}], max_tokens=99_999
            )
            self.assertEqual(
                captured[0]["max_tokens"], module.MAX_OUTPUT_TOKENS_PER_REQUEST
            )
            self.assertEqual(gate.snapshot()["api_calls"], 1)
            self.assertEqual(gate.snapshot()["usage"]["cache_hit_prompt_tokens"], 40)
            with self.assertRaises(module.RequestCapExceededError):
                guarded(messages=[{"role": "user", "content": "again"}])

    def test_transport_gate_blocks_before_network_when_budget_is_too_small(
        self,
    ) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            create = mock.Mock()
            gate = module.RequestBudgetGate(
                ledger_path=Path(temporary) / "ledger.jsonl",
                stage_max_cost_rmb=0.000001,
                project_max_cost_rmb=100,
                project_commitment_before_rmb=10,
                request_cap=1,
                usd_cny_rate=7.2,
            )
            with self.assertRaises(module.BudgetExceededError):
                gate.wrap_create(create)(
                    messages=[{"role": "user", "content": "translate"}]
                )
            create.assert_not_called()

    def test_restart_recovers_prior_calls_cost_and_usage(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "ledger.jsonl"

            def create(**_kwargs):
                return SimpleNamespace(
                    usage=SimpleNamespace(
                        prompt_tokens=100,
                        completion_tokens=20,
                        total_tokens=120,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                    )
                )

            first = module.RequestBudgetGate(
                ledger_path=ledger,
                stage_max_cost_rmb=1,
                project_max_cost_rmb=1000,
                project_commitment_before_rmb=10,
                request_cap=1,
                usd_cny_rate=7.2,
            )
            first.wrap_create(create)(messages=[{"role": "user", "content": "one"}])
            restarted = module.RequestBudgetGate(
                ledger_path=ledger,
                stage_max_cost_rmb=1,
                project_max_cost_rmb=1000,
                project_commitment_before_rmb=10,
                request_cap=1,
                usd_cny_rate=7.2,
            )
            self.assertEqual(restarted.snapshot()["api_calls"], 1)
            self.assertGreater(restarted.snapshot()["actual_cost_rmb"], 0)
            self.assertEqual(restarted.snapshot()["usage"]["total_tokens"], 120)
            with self.assertRaises(module.RequestCapExceededError):
                restarted.wrap_create(create)(
                    messages=[{"role": "user", "content": "two"}]
                )

    def test_restart_charges_unsettled_reservation_conservatively(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "ledger.jsonl"
            ledger.write_text(
                json.dumps(
                    {
                        "kind": "reserve",
                        "call_index": 1,
                        "reserved_cost_rmb": 0.25,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            restarted = module.RequestBudgetGate(
                ledger_path=ledger,
                stage_max_cost_rmb=1,
                project_max_cost_rmb=1000,
                project_commitment_before_rmb=10,
                request_cap=5,
                usd_cny_rate=7.2,
            )
            self.assertEqual(restarted.snapshot()["uncertain_cost_rmb"], 0.25)
            self.assertIn("recover_uncertain", ledger.read_text(encoding="utf-8"))

    def test_two_live_gate_instances_share_one_request_cap_transaction(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "ledger.jsonl"
            first = module.RequestBudgetGate(
                ledger_path=ledger,
                stage_max_cost_rmb=1,
                project_max_cost_rmb=1000,
                project_commitment_before_rmb=10,
                request_cap=1,
                usd_cny_rate=7.2,
            )
            stale_second = module.RequestBudgetGate(
                ledger_path=ledger,
                stage_max_cost_rmb=1,
                project_max_cost_rmb=1000,
                project_commitment_before_rmb=10,
                request_cap=1,
                usd_cny_rate=7.2,
            )
            reservation = first.reserve_request(
                messages=[{"role": "user", "content": "one"}]
            )
            with self.assertRaises(module.RequestCapExceededError):
                stale_second.reserve_request(
                    messages=[{"role": "user", "content": "two"}]
                )
            first.commit_uncertain(reservation, error_type="test_cleanup")


class LocalBudgetProxyTests(unittest.TestCase):
    def test_proxy_locks_citations_before_forwarding_and_restores_exact_markers(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            forwarded: list[dict] = []

            def forward(body: bytes, _api_key: str):
                payload = json.loads(body)
                forwarded.append(payload)
                locked_text = payload["messages"][-1]["content"]
                response = {
                    "choices": [
                        {"message": {"role": "assistant", "content": locked_text}}
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 10,
                        "total_tokens": 20,
                    },
                }
                return 200, {"Content-Type": "application/json"}, json.dumps(response).encode()

            gate = module.RequestBudgetGate(
                ledger_path=Path(temporary) / "ledger.jsonl",
                stage_max_cost_rmb=1,
                project_max_cost_rmb=10,
                project_commitment_before_rmb=0,
                request_cap=1,
                usd_cny_rate=7.2,
            )
            with module.DeepSeekBudgetProxy(
                api_key="test-key", gate=gate, forwarder=forward
            ) as proxy:
                request = urllib.request.Request(
                    proxy.base_url + "/chat/completions",
                    data=json.dumps(
                        {
                            "messages": [
                                {
                                    "role": "user",
                                    "content": "First {v1} [58], then {v2}:::{v3} [21, 59].",
                                }
                            ]
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    content = json.loads(response.read())["choices"][0]["message"]["content"]
                metrics = proxy.snapshot()

            self.assertNotIn("[58]", forwarded[0]["messages"][0]["content"])
            self.assertIn("{v1}", forwarded[0]["messages"][0]["content"])
            self.assertEqual(
                content,
                "First {v1} [58], then {v2}:::{v3} [21, 59].",
            )
            self.assertEqual(metrics["locked_marker_count"], 4)
            self.assertEqual(metrics["locked_numeric_citation_count"], 2)
            self.assertEqual(metrics["locked_rich_placeholder_count"], 2)
            self.assertEqual(metrics["validated_response_count"], 1)
            self.assertEqual(metrics["failure_count"], 0)

    def test_proxy_fails_closed_when_model_reorders_citation_tokens(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            def forward(body: bytes, _api_key: str):
                payload = json.loads(body)
                tokens = module._CITATION_TOKEN_RE.findall(
                    payload["messages"][0]["content"]
                )
                response = {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": f"B {tokens[1]}, A {tokens[0]}",
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 10,
                        "total_tokens": 20,
                    },
                }
                return 200, {"Content-Type": "application/json"}, json.dumps(response).encode()

            gate = module.RequestBudgetGate(
                ledger_path=Path(temporary) / "ledger.jsonl",
                stage_max_cost_rmb=1,
                project_max_cost_rmb=10,
                project_commitment_before_rmb=0,
                request_cap=1,
                usd_cny_rate=7.2,
            )
            with module.DeepSeekBudgetProxy(
                api_key="test-key", gate=gate, forwarder=forward
            ) as proxy:
                request = urllib.request.Request(
                    proxy.base_url + "/chat/completions",
                    data=json.dumps(
                        {
                            "messages": [
                                {"role": "user", "content": "A [58], B [21]."}
                            ]
                        }
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=5)
                metrics = proxy.snapshot()

            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
            self.assertEqual(gate.snapshot()["api_calls"], 1)
            self.assertEqual(metrics["failure_count"], 1)

    def test_proxy_forces_model_output_cap_and_non_thinking_mode(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            forwarded: list[dict] = []

            def forward(body: bytes, _api_key: str):
                forwarded.append(json.loads(body))
                response = {
                    "id": "test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": module.MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "译文"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 100,
                        "prompt_cache_hit_tokens": 40,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                    },
                }
                return (
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps(response).encode("utf-8"),
                )

            gate = module.RequestBudgetGate(
                ledger_path=Path(temporary) / "ledger.jsonl",
                stage_max_cost_rmb=1,
                project_max_cost_rmb=1000,
                project_commitment_before_rmb=10,
                request_cap=1,
                usd_cny_rate=7.2,
            )
            with module.DeepSeekBudgetProxy(
                api_key="sk-never-persist-this",
                gate=gate,
                forwarder=forward,
            ) as proxy:
                request = urllib.request.Request(
                    proxy.base_url + "/chat/completions",
                    data=json.dumps(
                        {
                            "model": "wrong-model",
                            "messages": [{"role": "user", "content": "translate"}],
                            "max_tokens": 99_999,
                            "stream": True,
                            "thinking": {"type": "enabled"},
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    response_payload = json.loads(response.read())
                    self.assertEqual(
                        response_payload["choices"][0]["message"]["content"], "译文"
                    )

            self.assertEqual(len(forwarded), 1)
            self.assertEqual(forwarded[0]["model"], module.MODEL)
            self.assertEqual(
                forwarded[0]["max_tokens"], module.MAX_OUTPUT_TOKENS_PER_REQUEST
            )
            self.assertFalse(forwarded[0]["stream"])
            self.assertEqual(forwarded[0]["thinking"], {"type": "disabled"})
            self.assertEqual(gate.snapshot()["api_calls"], 1)
            self.assertEqual(gate.snapshot()["usage"]["total_tokens"], 120)
            self.assertNotIn(
                "sk-never-persist-this",
                (Path(temporary) / "ledger.jsonl").read_text(encoding="utf-8"),
            )


class SharedProjectReservationTests(unittest.TestCase):
    def test_read_project_commitment_reconciles_dead_reservations(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary)
            (control / "budget_ledger.jsonl").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "reserve",
                        "run_id": "dead-run",
                        "reservation_id": "dead-reservation",
                        "owner_pid": 999_999_999,
                        "estimated_cost_rmb": 0.6,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertAlmostEqual(module.read_project_commitment(control), 0.0)
            events = (control / "budget_ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn('"kind": "recover_orphan"', events)

    def test_active_stage_reservations_share_one_project_cap(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary)
            first = module.SharedProjectReservation(
                control_dir=control,
                run_id="first",
                project_max_cost_rmb=1,
            )
            second = module.SharedProjectReservation(
                control_dir=control,
                run_id="second",
                project_max_cost_rmb=1,
            )
            first.reserve(0.6)
            with self.assertRaises(module.BudgetExceededError):
                second.reserve(0.6)
            first.settle(0.1)
            second.reserve(0.6)
            self.assertGreaterEqual(module.read_project_commitment(control), 0.7)
            second.settle(0.2)
            self.assertAlmostEqual(module.read_project_commitment(control), 0.3)

    def test_dead_same_run_reservation_is_resumed_without_double_hold(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary)
            restarted = module.SharedProjectReservation(
                control_dir=control,
                run_id="same-run",
                project_max_cost_rmb=1,
            )
            original_reservation_id = "orphan-reservation"
            (control / "budget_ledger.jsonl").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "reserve",
                        "run_id": "same-run",
                        "reservation_id": original_reservation_id,
                        "owner_pid": 999_999_999,
                        "estimated_cost_rmb": 0.6,
                        "uncertainty_key": "pdf2zh-next:same-run",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            restarted.reserve(0.6)
            self.assertEqual(restarted.reservation_id, original_reservation_id)
            self.assertAlmostEqual(module.read_project_commitment(control), 0.6)
            restarted.settle(0.1)
            self.assertAlmostEqual(module.read_project_commitment(control), 0.1)

    def test_reconciles_dead_stage_from_hashed_request_ledger_conservatively(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            control = root / "control"
            control.mkdir()
            request_ledger = root / "api-cost-ledger.jsonl"
            request_ledger.write_text(
                "\n".join(
                    json.dumps(event)
                    for event in (
                        {
                            "kind": "reserve",
                            "call_index": 1,
                            "owner_pid": 999_999_999,
                            "reserved_cost_rmb": 0.2,
                        },
                        {"kind": "settle", "call_index": 1, "cost_rmb": 0.03},
                        {
                            "kind": "reserve",
                            "call_index": 2,
                            "owner_pid": 999_999_999,
                            "reserved_cost_rmb": 0.25,
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            (control / "budget_ledger.jsonl").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "reserve",
                        "run_id": "dead-run",
                        "reservation_id": "dead-reservation",
                        "owner_pid": 999_999_999,
                        "estimated_cost_rmb": 0.8,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            reservation = module.SharedProjectReservation(
                control_dir=control,
                run_id="dead-run",
                project_max_cost_rmb=1,
            )
            receipt = reservation.reconcile_terminated(
                request_ledger_path=request_ledger,
                expected_request_ledger_sha256=module.sha256_file(request_ledger),
            )
            self.assertAlmostEqual(receipt["settled_cost_rmb"], 0.03)
            self.assertAlmostEqual(receipt["unresolved_cost_rmb"], 0.25)
            self.assertAlmostEqual(receipt["conservative_cost_rmb"], 0.28)
            self.assertAlmostEqual(module.read_project_commitment(control), 0.28)
            event = json.loads(
                (control / "budget_ledger.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
            self.assertEqual(event["kind"], "settle")
            self.assertEqual(
                event["reconciliation"]["request_ledger_sha256"],
                module.sha256_file(request_ledger),
            )

    def test_reconciliation_rejects_live_owner_and_hash_mismatch(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request_ledger = root / "api-cost-ledger.jsonl"
            request_ledger.write_text(
                json.dumps(
                    {
                        "kind": "reserve",
                        "call_index": 1,
                        "owner_pid": os.getpid(),
                        "reserved_cost_rmb": 0.2,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            for owner_pid, expected_hash, message in (
                (os.getpid(), module.sha256_file(request_ledger), "live owner"),
                (999_999_999, "0" * 64, "hash mismatch"),
            ):
                control = root / str(owner_pid)
                control.mkdir()
                (control / "budget_ledger.jsonl").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "kind": "reserve",
                            "run_id": "target",
                            "reservation_id": "reservation",
                            "owner_pid": owner_pid,
                            "estimated_cost_rmb": 0.8,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                reservation = module.SharedProjectReservation(
                    control_dir=control,
                    run_id="target",
                    project_max_cost_rmb=1,
                )
                with self.assertRaisesRegex(RuntimeError, message):
                    reservation.reconcile_terminated(
                        request_ledger_path=request_ledger,
                        expected_request_ledger_sha256=expected_hash,
                    )


@unittest.skipUnless(
    os.environ.get("SNOWMASS_RUN_PDF2ZH_INTEGRATION") == "1",
    "set SNOWMASS_RUN_PDF2ZH_INTEGRATION=1 for subprocess transport check",
)
class SubprocessTransportIntegrationTests(unittest.TestCase):
    def test_debug_false_child_reaches_parent_localhost_proxy(self) -> None:
        module = load_module()
        import fitz
        from pdf2zh_next import high_level

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 72), "Hello")
            document.save(source)
            document.close()
            glossary = root / "glossary.csv"
            glossary.write_text("source,target,tgt_lng\r\n", encoding="utf-8")
            gate = module.RequestBudgetGate(
                ledger_path=root / "ledger.jsonl",
                stage_max_cost_rmb=1,
                project_max_cost_rmb=1000,
                project_commitment_before_rmb=0,
                request_cap=10,
                usd_cny_rate=7.2,
            )

            def successful_translation(body: bytes, _api_key: str):
                request_payload = json.loads(body)
                prompt = request_payload["messages"][-1]["content"]
                translated = (
                    '[{"id":0,"output":"你好"}]'
                    if '"layout_label"' in prompt
                    else "你好"
                )
                response_payload = {
                    "id": "diagnostic",
                    "object": "chat.completion",
                    "created": 1,
                    "model": module.MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": translated,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "prompt_cache_hit_tokens": 0,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                }
                return (
                    200,
                    {"Content-Type": "application/json"},
                    json.dumps(response_payload).encode("utf-8"),
                )

            async def invoke(proxy_url: str) -> None:
                spec = module.build_safe_settings_spec(
                    output_dir=root / "rendered",
                    glossary_csv=glossary,
                    pages="1",
                    qps=1,
                    pool_max_workers=1,
                )
                settings = module._build_official_settings(spec, proxy_url)
                context = multiprocessing.get_context("spawn")
                queue = context.Queue()
                process = context.Process(
                    target=report_spawned_transport, args=(settings, queue, True)
                )
                process.start()
                report = queue.get(timeout=10)
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
                self.assertEqual(report["settings_base_url"], proxy_url)
                self.assertEqual(report["client_base_url"], proxy_url + "/")
                self.assertIn("NO_PROXY", report["proxy_environment_names"])
                self.assertIn("no_proxy", report["proxy_environment_names"])
                self.assertTrue(report["probe_completed"])
                calls_before_high_level = gate.snapshot()["api_calls"]
                self.assertEqual(calls_before_high_level, 1)
                async for _event in high_level.do_translate_async_stream(
                    settings, source
                ):
                    pass

            with (
                module.localhost_proxy_bypass_environment(),
                module.DeepSeekBudgetProxy(
                    api_key="diagnostic-placeholder",
                    gate=gate,
                    forwarder=successful_translation,
                ) as proxy,
            ):
                asyncio.run(invoke(proxy.base_url))
            self.assertGreaterEqual(gate.snapshot()["api_calls"], 2)


class ZeroPaidPreflightTests(unittest.TestCase):
    def test_preflight_never_loads_api_key_or_runs_translator(self) -> None:
        module = load_module()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.pdf"
            source.write_bytes(b"%PDF-1.4\n%%EOF\n")
            rights = root / "papers.json"
            rights.write_text(
                json.dumps(
                    [{"record_id": "arxiv:sample", "publication_allowed": True}]
                ),
                encoding="utf-8",
            )
            source_manifest = root / "sources.json"
            source_manifest.write_text(
                json.dumps(
                    {
                        "records": [
                            {
                                "record_id": "arxiv:sample",
                                "pdf_status": "complete",
                                "pdf_bytes": source.stat().st_size,
                                "pdf_sha256": module.sha256_file(source),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            glossary = root / "glossary.json"
            glossary.write_text(json.dumps({"terms": []}), encoding="utf-8")
            config = module.RunConfig(
                record_id="arxiv:sample",
                source_pdf=source,
                rights_manifest=rights,
                source_manifest=source_manifest,
                glossary_json=glossary,
                output_root=root / "out",
                pages="1",
                project_max_cost_rmb=100,
                stage_max_cost_rmb=10,
                stage_max_api_calls=20,
                qps=1,
                pool_max_workers=1,
            )
            with (
                mock.patch.object(
                    module, "load_api_key", side_effect=AssertionError("key accessed")
                ),
                mock.patch.object(
                    module,
                    "run_official_translation",
                    side_effect=AssertionError("translator accessed"),
                ),
            ):
                receipt = module.execute(
                    config,
                    preflight_only=True,
                    inspector=lambda _path, _pages: {
                        "selected_page_count": 1,
                        "text_utf8_bytes": 1000,
                        "text_blocks": 10,
                    },
                )
            self.assertEqual(receipt["status"], "preflight_passed")
            self.assertLessEqual(receipt["projection"]["max_cost_rmb"], 10)
            self.assertTrue((root / "out" / "preflight.json").is_file())


if __name__ == "__main__":
    unittest.main()
