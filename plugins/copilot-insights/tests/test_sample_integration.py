#!/usr/bin/env python3
"""Integration tests against real sample.jsonl oTel data.

Run: python3 plugins/copilot-insights/tests/test_sample_integration.py
No third-party deps; uses only the stdlib unittest.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "copilot-insights", "scripts"))
import analyze_tokens as at  # noqa: E402
import analyze_sessions as ses  # noqa: E402

SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.jsonl")


class AnalyzeTokensSampleTests(unittest.TestCase):
    def test_sample_is_present(self):
        self.assertTrue(os.path.exists(SAMPLE), f"{SAMPLE} not found; run tests from repo root")

    def test_analyze_by_model(self):
        groups, span_found, m_in, m_out = at.analyze([SAMPLE], "model")
        self.assertTrue(span_found)
        self.assertGreater(len(groups), 0)
        # At least one model group should exist
        for model, g in groups.items():
            self.assertGreater(g.calls, 0)
            self.assertGreater(g.input, 0)
            self.assertGreaterEqual(g.output, 0)

    def test_analyze_by_session(self):
        groups, span_found, m_in, m_out = at.analyze([SAMPLE], "session")
        self.assertTrue(span_found)
        self.assertGreater(len(groups), 0)
        # Sessions should have timestamps
        for sess, g in groups.items():
            self.assertIsNotNone(g.first_ts)

    def test_analyze_by_day(self):
        groups, span_found, m_in, m_out = at.analyze([SAMPLE], "day")
        self.assertTrue(span_found)
        self.assertGreater(len(groups), 0)
        for day, g in groups.items():
            self.assertNotEqual(day, "unknown")
            self.assertGreater(g.calls, 0)

    def test_analyze_metrics(self):
        groups, span_found, m_in, m_out = at.analyze([SAMPLE], "all")
        self.assertTrue(span_found)
        # Both spans and metrics may be present
        total = sum(g.total for g in groups.values())
        metric_total = m_in + m_out
        self.assertTrue(total > 0 or metric_total > 0)

    def test_json_output(self):
        groups, span_found, m_in, m_out = at.analyze([SAMPLE], "model")
        result = at.to_json_result(groups, span_found, at.Pricing(), False, m_in, m_out)
        self.assertIn("source", result)
        self.assertIn("groups", result)
        self.assertIn("metric_token_usage", result)
        for g in result["groups"].values():
            self.assertIn("calls", g)
            self.assertIn("total_tokens", g)


class AnalyzeSessionsSampleTests(unittest.TestCase):
    def test_parse_turns(self):
        turns = ses.parse_turns([SAMPLE])
        self.assertGreater(len(turns), 0)
        for session, tlist in turns.items():
            self.assertIsInstance(session, str)
            self.assertGreater(len(tlist), 0)
            for t in tlist:
                self.assertIn("cur", t)
                self.assertIn("delta", t)
                self.assertIn("model", t)

    def test_parse_turns_sorted(self):
        turns = ses.parse_turns([SAMPLE])
        for tlist in turns.values():
            start_ns = [t["start_ns"] for t in tlist]
            # Verify ascending order
            for i in range(1, len(start_ns)):
                self.assertLessEqual(start_ns[i - 1], start_ns[i])

    def test_parse_tools(self):
        tools = ses.parse_tools([SAMPLE])
        # Real data may or may not have tools; just verify structure
        for name, agg in tools.items():
            self.assertIsInstance(name, str)
            self.assertGreaterEqual(agg.calls, 1)

    def test_analyze_context(self):
        groups = ses.analyze_context([SAMPLE], "session")
        self.assertGreater(len(groups), 0)
        for key, g in groups.items():
            self.assertGreater(g.turns, 0)
            self.assertIsNotNone(g.max_fill)
            self.assertIsNotNone(g.median_fill)

    def test_analyze_context_by_model(self):
        groups = ses.analyze_context([SAMPLE], "model")
        self.assertGreater(len(groups), 0)
        for model, g in groups.items():
            self.assertNotEqual(model, "unknown")
            self.assertGreater(g.turns, 0)

    def test_fmt_context_json(self):
        groups = ses.analyze_context([SAMPLE], "session")
        result = ses.fmt_context_json(groups)
        for key, g in result.items():
            self.assertIn("turns", g)
            self.assertIn("median_fill", g)
            self.assertIn("max_fill", g)
            self.assertIn("turns_above_threshold", g)

    def test_fmt_tools_json(self):
        tools = ses.parse_tools([SAMPLE])
        result = ses.fmt_tools_json(tools)
        for name, t in result.items():
            self.assertIn("type", t)
            self.assertIn("avg_ms", t)

    def test_analyze_growth_from_sample(self):
        turns = ses.parse_turns([SAMPLE])
        if not turns:
            self.skipTest("no turns in sample")
        groups = ses.analyze_growth(turns, "session")
        self.assertGreater(len(groups), 0)
        for key, g in groups.items():
            self.assertGreaterEqual(g.turns, 1)
            self.assertIsInstance(g.deltas, list)

    def test_fmt_growth_json(self):
        turns = ses.parse_turns([SAMPLE])
        if not turns:
            self.skipTest("no turns in sample")
        groups = ses.analyze_growth(turns, "session")
        result = ses.fmt_growth_json(groups)
        for key, g in result.items():
            self.assertIn("turns", g)
            self.assertIn("by_initiator", g)
            self.assertIn("by_tool", g)


if __name__ == "__main__":
    unittest.main()
