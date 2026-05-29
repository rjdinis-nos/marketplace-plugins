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
    def _agg(self, input, output, cache_read, cache_creation):
        a = at.Agg()
        a.input, a.output, a.cache_read, a.cache_creation = input, output, cache_read, cache_creation
        a.calls = 1
        return a

    def test_fresh_input_subtracts_cache(self):
        a = self._agg(1000, 0, 600, 300)
        self.assertEqual(a.fresh_input, 100)

    def test_fresh_input_never_negative(self):
        a = self._agg(500, 0, 600, 300)
        self.assertEqual(a.fresh_input, 0)

    def test_cost_none_without_rates(self):
        a = self._agg(1000, 100, 0, 0)
        self.assertIsNone(a.cost(at.Rates()))

    def test_cost_buckets(self):
        rates = at.Rates(input=15.0, output=75.0, cache_read=1.5, cache_write=18.75)
        a = self._agg(input=1_000_000, output=1_000_000, cache_read=600_000, cache_creation=300_000)
        # fresh = 100_000 -> 1.5 ; output 1M -> 75 ; cache_read 600k -> 0.9 ; cache_write 300k -> 5.625
        self.assertAlmostEqual(a.cost(rates), 1.5 + 75.0 + 0.9 + 5.625, places=6)

    def test_naive_cost_ignores_cache(self):
        rates = at.Rates(input=15.0, output=75.0, cache_read=1.5, cache_write=18.75)
        a = self._agg(input=1_000_000, output=1_000_000, cache_read=600_000, cache_creation=300_000)
        self.assertAlmostEqual(a.naive_cost(rates), 15.0 + 75.0, places=6)


class RatesLoadTests(unittest.TestCase):
    def test_any_false_when_empty(self):
        self.assertFalse(at.Rates().any())

    def test_any_true_with_one_rate(self):
        self.assertTrue(at.Rates(input=15.0).any())


if __name__ == "__main__":
    unittest.main()
