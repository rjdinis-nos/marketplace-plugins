#!/usr/bin/env python3
"""Regression tests for analyze_tokens.py.

Run: python3 plugins/token-usage/skills/token-usage/scripts/test_analyze_tokens.py
No third-party deps; uses only the stdlib unittest.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_tokens as at  # noqa: E402


class DayOfTests(unittest.TestCase):
    # 1780087173 -> 2026-05-29 UTC
    def test_otlp_unixnano_int(self):
        node = {"startTimeUnixNano": "1780087173000000000"}
        self.assertEqual(at.day_of(node), "2026-05-29")

    def test_js_sdk_sec_nano_array(self):
        # OTel JS SDK style used by the CLI file exporter.
        node = {"startTime": [1780087173, 64000000]}
        self.assertEqual(at.day_of(node), "2026-05-29")

    def test_endtime_array_fallback(self):
        node = {"endTime": [1780087234, 540631042]}
        self.assertEqual(at.day_of(node), "2026-05-29")

    def test_missing_timestamp(self):
        self.assertEqual(at.day_of({}), "unknown")

    def test_garbage_timestamp(self):
        self.assertEqual(at.day_of({"startTime": ["nope"]}), "unknown")


class AggCostTests(unittest.TestCase):
    def _span(self, input, output, cache_read, cache_creation):
        return {
            "gen_ai.usage.input_tokens": input,
            "gen_ai.usage.output_tokens": output,
            "gen_ai.usage.cache_read.input_tokens": cache_read,
            "gen_ai.usage.cache_creation.input_tokens": cache_creation,
            "gen_ai.response.model": "claude-opus-4.8",
        }

    def test_fresh_input_subtracts_cache(self):
        a = at.Agg()
        a.add_span(self._span(1000, 0, 600, 300))
        self.assertEqual(a.fresh_input, 100)

    def test_fresh_input_never_negative(self):
        a = at.Agg()
        a.add_span(self._span(500, 0, 600, 300))
        self.assertEqual(a.fresh_input, 0)

    def test_no_cost_without_pricing(self):
        a = at.Agg()
        a.add_span(self._span(1000, 100, 0, 0))
        self.assertIsNone(a.est_cost)

    def test_cost_buckets(self):
        rates = at.Rates(input=15.0, output=75.0, cache_read=1.5, cache_write=18.75)
        # fresh = 100_000 -> 1.5 ; output 1M -> 75 ; cache_read 600k -> 0.9 ; cache_write 300k -> 5.625
        self.assertAlmostEqual(
            rates.cost(1_000_000, 1_000_000, 600_000, 300_000), 1.5 + 75.0 + 0.9 + 5.625, places=6
        )

    def test_cache_write_falls_back_to_input(self):
        rates = at.Rates(input=10.0, output=0.0, cache_read=0.0, cache_write=None)
        # 200k cache-creation tokens billed at the input rate (10/Mtok) -> 2.0
        self.assertAlmostEqual(rates.cost(200_000, 0, 0, 200_000), 2.0, places=6)

    def test_naive_ignores_cache(self):
        rates = at.Rates(input=15.0, output=75.0, cache_read=1.5, cache_write=18.75)
        self.assertAlmostEqual(rates.naive(1_000_000, 1_000_000), 15.0 + 75.0, places=6)


class PricingTests(unittest.TestCase):
    def test_normalize_model(self):
        self.assertEqual(at.normalize_model("Claude Opus 4.8"), "claude-opus-4.8")
        self.assertEqual(at.normalize_model("GPT-4.1[^1]"), "gpt-4.1")

    def test_per_model_lookup(self):
        p = at.Pricing(currency="$")
        p.models["claude-opus-4.8"] = at.Rates(input=5.0, output=25.0, cache_read=0.5, cache_write=6.25)
        self.assertTrue(p.any())
        self.assertTrue(p.per_model())
        self.assertIsNotNone(p.rates_for("Claude Opus 4.8"))  # display name normalizes
        self.assertIsNone(p.rates_for("unknown-model"))

    def test_default_fallback(self):
        p = at.Pricing(currency="$", default=at.Rates(input=1.0, output=2.0))
        self.assertIsNotNone(p.rates_for("anything"))

    def test_per_model_cost_accumulation(self):
        p = at.Pricing(currency="$")
        p.models["claude-opus-4.8"] = at.Rates(input=5.0, output=25.0, cache_read=0.5, cache_write=6.25)
        a = at.Agg()
        a.add_span(
            {
                "gen_ai.usage.input_tokens": 1_000_000,
                "gen_ai.usage.output_tokens": 1_000_000,
                "gen_ai.usage.cache_read.input_tokens": 0,
                "gen_ai.usage.cache_creation.input_tokens": 0,
                "gen_ai.response.model": "claude-opus-4.8",
            },
            p,
        )
        # fresh 1M -> 5 ; output 1M -> 25
        self.assertAlmostEqual(a.est_cost, 30.0, places=6)
        self.assertEqual(a.priced_calls, 1)


if __name__ == "__main__":
    unittest.main()
