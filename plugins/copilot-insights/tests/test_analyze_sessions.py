#!/usr/bin/env python3
"""Tests for analyze_sessions.py.

Run: python3 plugins/copilot-insights/tests/test_analyze_sessions.py
No third-party deps; uses only the stdlib unittest.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "copilot-insights", "scripts"))
import analyze_sessions as ses  # noqa: E402


# ── helper tests ──────────────────────────────────────────────────────────────

class ScalarHelperTests(unittest.TestCase):
    def test_int0_plain(self):
        self.assertEqual(ses._int0(42), 42)
        self.assertEqual(ses._int0("7"), 7)

    def test_int0_none_and_garbage(self):
        self.assertEqual(ses._int0(None), 0)
        self.assertEqual(ses._int0("foo"), 0)
        self.assertEqual(ses._int0([]), 0)

    def test_secs_list(self):
        self.assertEqual(ses._secs([1780087173, 500000000]), 1780087173.5)

    def test_secs_bad(self):
        self.assertIsNone(ses._secs(None))
        self.assertIsNone(ses._secs(123))
        self.assertIsNone(ses._secs([1]))

    def test_day(self):
        self.assertEqual(ses._day(1780087173.0), "2026-05-29")

    def test_day_none(self):
        self.assertEqual(ses._day(None), "unknown")

    def test_fmt_dt(self):
        self.assertEqual(ses._fmt_dt(1780087173.0), "2026-05-29 20:39")

    def test_fmt_dt_none(self):
        self.assertEqual(ses._fmt_dt(None), "unknown")

    def test_pct(self):
        self.assertEqual(ses._pct(0.755), "75.5%")
        self.assertEqual(ses._pct(1.0), "100.0%")
        self.assertEqual(ses._pct(None), "?")

    def test_short(self):
        self.assertEqual(ses._short("abcdefghijklmnop", 5), "abcde")
        self.assertEqual(ses._short("abc", 5), "abc")

    def test_fmt_num(self):
        self.assertEqual(ses._fmt_num(10), "+10")
        self.assertEqual(ses._fmt_num(0), "0")
        self.assertEqual(ses._fmt_num(-5), "-5")


class McpTests(unittest.TestCase):
    def test_is_mcp_hash_true(self):
        self.assertTrue(ses._is_mcp_hash("a" * 64 + "/doThing"))

    def test_is_mcp_hash_false(self):
        self.assertFalse(ses._is_mcp_hash("bash"))
        self.assertFalse(ses._is_mcp_hash(""))
        self.assertFalse(ses._is_mcp_hash("abc/def"))

    def test_mcp_label(self):
        self.assertEqual(ses._mcp_label("a" * 64 + "/doThing"), "doThing [mcp]")
        self.assertEqual(ses._mcp_label("bash"), "bash")


# ── data model tests ──────────────────────────────────────────────────────────

class ContextAggTests(unittest.TestCase):
    def test_add(self):
        a = ses.ContextAgg()
        a.add(1000, 10000, 100.0)
        self.assertEqual(a.turns, 1)

    def test_median_fill(self):
        a = ses.ContextAgg()
        a.add(1000, 10000, 100.0)
        a.add(5000, 10000, 200.0)
        a.add(8000, 10000, 300.0)
        self.assertAlmostEqual(a.median_fill, 0.5)

    def test_percentile(self):
        a = ses.ContextAgg()
        a.add(0, 100, 100.0)
        a.add(50, 100, 200.0)
        a.add(90, 100, 300.0)
        self.assertEqual(a.percentile(50), 0.5)

    def test_max_fill(self):
        a = ses.ContextAgg()
        self.assertIsNone(a.max_fill)
        a.add(8000, 10000, 100.0)
        a.add(9000, 10000, 200.0)
        self.assertAlmostEqual(a.max_fill, 0.9)

    def test_turns_above(self):
        a = ses.ContextAgg()
        a.add(7000, 10000, 1.0)
        a.add(7500, 10000, 2.0)
        a.add(5000, 10000, 3.0)
        self.assertEqual(a.turns_above(0.72), 1)  # only 7500 > 7200
        self.assertEqual(a.turns_above(0.70), 1)  # 7000 is not > 7000 (strict)

    def test_merge(self):
        a, b = ses.ContextAgg(), ses.ContextAgg()
        a.add(500, 1000, 10.0)
        b.add(800, 1000, 20.0)
        a.merge(b)
        self.assertEqual(a.turns, 2)
        self.assertEqual(a.first_ts, 10.0)
        self.assertEqual(a.last_ts, 20.0)

    def test_time_tracking(self):
        a = ses.ContextAgg()
        a.add(50, 100, 50.0)
        a.add(90, 100, 30.0)
        self.assertEqual(a.first_ts, 30.0)
        self.assertEqual(a.last_ts, 50.0)


class GrowthAggTests(unittest.TestCase):
    def test_add_turn(self):
        a = ses.GrowthAgg()
        a.add_turn(delta=50, initiator="user", model="gpt-4", cur=1000,
                   tools=["view"], ts_secs=10.0, session="s1")
        self.assertEqual(a.turns, 1)
        self.assertEqual(a.avg_delta, 50)
        self.assertEqual(a.max_delta, 50)

    def test_add_turn_no_tools(self):
        a = ses.GrowthAgg()
        a.add_turn(delta=0, initiator="agent", model="gpt-4", cur=500,
                   tools=None, ts_secs=5.0, session="s1")
        self.assertIn("(no tool)", a.tool_deltas)

    def test_total_added(self):
        a = ses.GrowthAgg()
        a.add_turn(100, "user", "m", 1000, [], 1.0, "s1")
        a.add_turn(-20, "agent", "m", 980, [], 2.0, "s1")
        a.add_turn(50, "user", "m", 1030, [], 3.0, "s1")
        self.assertEqual(a.total_added, 150)

    def test_merge(self):
        a, b = ses.GrowthAgg(), ses.GrowthAgg()
        a.add_turn(10, "user", "m", 100, [], 1.0, "s1")
        b.add_turn(20, "agent", "m", 120, [], 2.0, "s2")
        a.merge(b)
        self.assertEqual(a.turns, 2)
        self.assertEqual(a.max_delta, 20)

    def test_by_initiator(self):
        a = ses.GrowthAgg()
        a.add_turn(10, "user", "m1", 100, [], 1.0, "s")
        a.add_turn(20, "agent", "m1", 120, [], 2.0, "s")
        a.add_turn(5, "user", "m1", 125, [], 3.0, "s")
        self.assertEqual(len(a.init_deltas["user"]), 2)
        self.assertEqual(len(a.init_deltas["agent"]), 1)

    def test_by_tool(self):
        a = ses.GrowthAgg()
        a.add_turn(10, "user", "m1", 100, ["view"], 1.0, "s")
        a.add_turn(20, "agent", "m1", 120, ["bash"], 2.0, "s")
        self.assertEqual(len(a.tool_deltas["view"]), 1)
        self.assertEqual(len(a.tool_deltas["bash"]), 1)

    def test_spikes(self):
        a = ses.GrowthAgg()
        a.add_turn(100, "user", "m1", 200, ["edit"], 1.0, "s1")
        self.assertEqual(len(a.spikes), 1)
        self.assertEqual(a.spikes[0][0], 100)


class ToolAggTests(unittest.TestCase):
    def test_add(self):
        a = ses.ToolAgg()
        a.add(duration_ms=100, is_error=False, is_mcp=False, mcp_tool_name=None, ts_secs=10.0)
        self.assertEqual(a.calls, 1)
        self.assertEqual(a.avg_ms, 100)
        self.assertEqual(a.max_ms, 100)

    def test_mcp_tracking(self):
        a = ses.ToolAgg()
        a.add(50, False, True, "doThing", 1.0)
        self.assertTrue(a.is_mcp)
        self.assertEqual(a.mcp_tool_name, "doThing")

    def test_error_count(self):
        a = ses.ToolAgg()
        a.add(10, True, False, None, 1.0)
        a.add(20, False, False, None, 2.0)
        self.assertEqual(a.errors, 1)

    def test_percentile(self):
        a = ses.ToolAgg()
        for i in range(101):
            a.add(i, False, False, None, float(i))
        self.assertEqual(a.percentile(95), 95)

    def test_time_tracking(self):
        a = ses.ToolAgg()
        a.add(10, False, False, None, 50.0)
        a.add(20, False, False, None, 30.0)
        a.add(15, False, False, None, 70.0)
        self.assertEqual(a.first_ts, 30.0)
        self.assertEqual(a.last_ts, 70.0)


# ── formatting tests ──────────────────────────────────────────────────────────

class TableHelperTests(unittest.TestCase):
    def test_table_basic(self):
        t = ses._table(["a", "b"], [["1", "2"], ["10", "20"]])
        self.assertIn("a", t)
        self.assertIn("b", t)
        self.assertIn("1", t)


class GrowthFormatTests(unittest.TestCase):
    def test_fmt_growth_json(self):
        g = ses.GrowthAgg()
        g.add_turn(10, "user", "m1", 100, ["view"], 1.0, "s1")
        result = ses.fmt_growth_json({"all": g})
        self.assertEqual(result["all"]["turns"], 1)
        self.assertEqual(result["all"]["avg_delta"], 10)
        self.assertIn("by_initiator", result["all"])
        self.assertIn("by_tool", result["all"])

    def test_fmt_growth_json_with_turn_filter(self):
        g = ses.GrowthAgg()
        g.add_turn(10, "user", "m1", 100, ["view"], 1.0, "s1")
        g.add_turn(20, "agent", "m1", 120, ["bash"], 2.0, "s1")
        g.add_turn(5, "user", "m1", 125, ["edit"], 3.0, "s1")
        
        # Without filter
        result = ses.fmt_growth_json({"all": g})
        self.assertEqual(len(result["all"]["by_turn"]), 3)
        
        # Filter to turn 0 (first turn, starts at 1 in enum)
        result = ses.fmt_growth_json({"all": g}, turn_filter=0)
        self.assertEqual(len(result["all"]["by_turn"]), 1)
        self.assertEqual(result["all"]["by_turn"][0]["turn"], 0)
        self.assertEqual(result["all"]["by_turn"][0]["delta_tokens"], 10)
        
        # Filter to turn 1 (second turn)
        result = ses.fmt_growth_json({"all": g}, turn_filter=1)
        self.assertEqual(len(result["all"]["by_turn"]), 1)
        self.assertEqual(result["all"]["by_turn"][0]["turn"], 1)
        self.assertEqual(result["all"]["by_turn"][0]["delta_tokens"], 20)
        
        # Filter to non-existent turn
        result = ses.fmt_growth_json({"all": g}, turn_filter=10)
        self.assertEqual(len(result["all"]["by_turn"]), 0)

    def test_fmt_growth_table_basic(self):
        g = ses.GrowthAgg()
        g.add_turn(100, "user", "m1", 200, ["view"], 1.0, "s1")
        t = ses.fmt_growth_table({"all": g}, "all", top_spikes=5)
        self.assertIn("all", t)
        self.assertIn("turns", t)
        self.assertIn("avg_delta", t)
        self.assertIn("max_delta", t)

    def test_fmt_growth_empty(self):
        s = ses.fmt_growth_table({}, "all")
        self.assertIn("No context growth data", s)


class TurnsFormatTests(unittest.TestCase):
    def test_fmt_turns_empty(self):
        s = ses.fmt_turns_table({})
        self.assertIn("No per-turn data", s)

    def test_fmt_turns_json(self):
        turns = {
            "sess-1": [
                {
                    "input_tokens": 100, "output_tokens": 50,
                    "cache_rd": 20, "cache_cr": 10, "reasoning": 5,
                    "cur": 150, "token_limit": 1000, "delta": 150,
                    "model": "gpt-4", "initiator": "user",
                    "ts": 1780087173.0, "tools": ["view"],
                    "ttfc": None, "srv_ms": None, "turn_id": "0",
                }
            ]
        }
        result = ses.fmt_turns_json(turns)
        self.assertIn("sess-1", result)
        t = result["sess-1"][0]
        self.assertEqual(t["ctx_tokens"], 150)
        self.assertEqual(t["fresh_input_tokens"], 70)  # 100 - 20 - 10
        self.assertEqual(t["ctx_fill"], 0.15)

    def test_fmt_turns_json_with_turn_filter(self):
        turns = {
            "sess-1": [
                {
                    "input_tokens": 100, "output_tokens": 50,
                    "cache_rd": 0, "cache_cr": 0, "reasoning": 5,
                    "cur": 150, "token_limit": 1000, "delta": 150,
                    "model": "gpt-4", "initiator": "user",
                    "ts": 1780087173.0, "tools": ["view"],
                    "ttfc": None, "srv_ms": None, "turn_id": "0",
                },
                {
                    "input_tokens": 100, "output_tokens": 50,
                    "cache_rd": 0, "cache_cr": 0, "reasoning": 5,
                    "cur": 200, "token_limit": 1000, "delta": 50,
                    "model": "gpt-4", "initiator": "agent",
                    "ts": 1780087174.0, "tools": ["bash"],
                    "ttfc": None, "srv_ms": None, "turn_id": "1",
                },
            ]
        }
        # Filter to turn 1 (second turn)
        result = ses.fmt_turns_json(turns, turn_filter=1)
        self.assertEqual(len(result["sess-1"]), 1)
        self.assertEqual(result["sess-1"][0]["turn_index"], 1)
        self.assertEqual(result["sess-1"][0]["ctx_delta"], 50)
        
        # Filter to turn 0 (first turn)
        result = ses.fmt_turns_json(turns, turn_filter=0)
        self.assertEqual(len(result["sess-1"]), 1)
        self.assertEqual(result["sess-1"][0]["turn_index"], 0)
        self.assertEqual(result["sess-1"][0]["ctx_delta"], 150)
        
        # Filter to non-existent turn
        result = ses.fmt_turns_json(turns, turn_filter=5)
        self.assertEqual(len(result["sess-1"]), 0)

    def test_fmt_turns_table(self):
        turns = {
            "sess-1": [
                {
                    "input_tokens": 100, "output_tokens": 50,
                    "cache_rd": 20, "cache_cr": 10, "reasoning": 5,
                    "cur": 150, "token_limit": 1000, "delta": 150,
                    "model": "gpt-4", "initiator": "user",
                    "ts": 1780087173.0, "tools": ["view"],
                    "ttfc": 0.5, "srv_ms": 100, "turn_id": "0",
                }
            ]
        }
        t = ses.fmt_turns_table(turns)
        self.assertIn("0", t)
        self.assertIn("turn_id", t)
        self.assertIn("fill%", t)


class ToolsFormatTests(unittest.TestCase):
    def test_fmt_tools_json(self):
        a = ses.ToolAgg()
        a.add(100, False, True, "doThing", 10.0)
        result = ses.fmt_tools_json({"mcp/do": a})
        self.assertEqual(result["mcp/do"]["type"], "mcp")
        self.assertEqual(result["mcp/do"]["mcp_tool_name"], "doThing")
        self.assertEqual(result["mcp/do"]["avg_ms"], 100)

    def test_fmt_tools_table(self):
        a = ses.ToolAgg()
        a.add(50, False, False, None, 5.0)
        t = ses.fmt_tools_table({"bash": a})
        self.assertIn("bash", t)
        self.assertIn("builtin", t)

    def test_fmt_tools_empty(self):
        t = ses.fmt_tools_table({})
        self.assertIn("No tool execution data", t)


class ContextFormatTests(unittest.TestCase):
    def test_fmt_context_json(self):
        a = ses.ContextAgg()
        a.add(7000, 10000, 10.0)
        a.add(8000, 10000, 20.0)
        result = ses.fmt_context_json({"s1": a}, warn_threshold=0.7)
        self.assertEqual(result["s1"]["turns"], 2)
        self.assertEqual(result["s1"]["turns_above_threshold"], 1)

    def test_fmt_context_table(self):
        a = ses.ContextAgg()
        a.add(7100, 10000, 0.0)
        a.add(5000, 10000, 1.0)
        t = ses.fmt_context_table({"s1": a}, "session", warn_threshold=0.7)
        self.assertIn("s1", t)
        self.assertIn("71.0%", t)

    def test_fmt_context_table_warning(self):
        a = ses.ContextAgg()
        a.add(8000, 10000, 0.0)
        a.add(9000, 10000, 1.0)
        t = ses.fmt_context_table({"s1": a}, "session", warn_threshold=0.7)
        self.assertIn("⚠️", t)

    def test_fmt_context_empty(self):
        t = ses.fmt_context_table({}, "session")
        self.assertIn("No context window data", t)


# ── compaction tests ──────────────────────────────────────────────────────────

class CompactionTests(unittest.TestCase):
    def test_detect_compactions_empty(self):
        self.assertEqual(ses.detect_compactions({}), {})

    def test_detect_compactions_found(self):
        turns = {
            "s1": [
                {"cur": 10000, "token_limit": 15000, "ts": 1.0, "tools": [], "model": "m", "reasoning": 0, "cache_rd": 0, "cache_cr": 0, "input_tokens": 0, "output_tokens": 0},
                {"cur": 5000, "token_limit": 15000, "ts": 2.0, "tools": ["view"], "model": "m", "reasoning": 0, "cache_rd": 0, "cache_cr": 0, "input_tokens": 0, "output_tokens": 0},
            ]
        }
        result = ses.detect_compactions(turns)
        self.assertIn("s1", result)
        self.assertEqual(result["s1"][0]["drop_tok"], 5000)

    def test_detect_compactions_no_drop(self):
        turns = {
            "s1": [
                {"cur": 1000, "token_limit": 10000, "ts": 1.0, "tools": [], "model": "m", "reasoning": 0, "cache_rd": 0, "cache_cr": 0, "input_tokens": 0, "output_tokens": 0},
                {"cur": 1500, "token_limit": 10000, "ts": 2.0, "tools": [], "model": "m", "reasoning": 0, "cache_rd": 0, "cache_cr": 0, "input_tokens": 0, "output_tokens": 0},
            ]
        }
        result = ses.detect_compactions(turns)
        self.assertEqual(result, {})

    def test_detect_compactions_small_drop_ignored(self):
        turns = {
            "s1": [
                {"cur": 2000, "token_limit": 10000, "ts": 1.0, "tools": [], "model": "m", "reasoning": 0, "cache_rd": 0, "cache_cr": 0, "input_tokens": 0, "output_tokens": 0},
                {"cur": 1500, "token_limit": 10000, "ts": 2.0, "tools": [], "model": "m", "reasoning": 0, "cache_rd": 0, "cache_cr": 0, "input_tokens": 0, "output_tokens": 0},
            ]
        }
        result = ses.detect_compactions(turns)
        self.assertEqual(result, {})  # 500 < _MIN_COMPACTION_DROP (1_000)

    def test_fmt_compactions_table(self):
        comp = {
            "s1": [
                {"turn_index": 2, "turn_id": "t2", "time": "2026-05-29", "before_tok": 9000,
                 "after_tok": 4000, "drop_tok": 5000, "before_fill": 0.6, "after_fill": 0.267,
                 "recovered_pct": 0.556, "tools": ["view"], "model": "m"}
            ]
        }
        t = ses.fmt_compactions_table(comp)
        self.assertIn("Compaction events", t)
        self.assertIn("5,000", t)

    def test_fmt_compactions_json(self):
        comp = {
            "s1": [
                {"turn_index": 1, "turn_id": "t1", "time": "2026-05-29", "before_tok": 8000,
                 "after_tok": 3000, "drop_tok": 5000, "before_fill": 0.8, "after_fill": 0.3,
                 "recovered_pct": 0.625, "tools": [], "model": "m"}
            ]
        }
        result = ses.fmt_compactions_json(comp)
        self.assertEqual(result["s1"][0]["drop_tokens"], 5000)

    def test_fmt_compactions_table_empty(self):
        t = ses.fmt_compactions_table({})
        self.assertIn("No compaction events", t)


# ── analyze_growth tests ──────────────────────────────────────────────────────

class AnalyzeGrowthTests(unittest.TestCase):
    def test_by_session(self):
        turns = {
            "s1": [
                {"cur": 100, "token_limit": 1000, "start_ns": [1, 0], "ts": 1.0,
                 "model": "m1", "initiator": "user", "tools": ["view"], "session": "s1", "delta": 100},
                {"cur": 200, "token_limit": 1000, "start_ns": [2, 0], "ts": 2.0,
                 "model": "m1", "initiator": "agent", "tools": ["bash"], "session": "s1", "delta": 100},
            ]
        }
        result = ses.analyze_growth(turns, "session")
        self.assertIn("s1", result)
        self.assertEqual(result["s1"].turns, 2)
        self.assertEqual(result["s1"].deltas, [100, 100])  # 100, 200-100

    def test_by_model(self):
        turns = {
            "s1": [
                {"cur": 100, "token_limit": 1000, "start_ns": [1, 0], "ts": 1.0,
                 "model": "m1", "initiator": "user", "tools": ["view"], "session": "s1", "delta": 100},
            ]
        }
        result = ses.analyze_growth(turns, "model")
        self.assertIn("m1", result)

    def test_by_all(self):
        turns = {
            "s1": [
                {"cur": 100, "token_limit": 1000, "start_ns": [1, 0], "ts": 1.0,
                 "model": "m1", "initiator": "user", "tools": [], "session": "s1", "delta": 100},
            ]
        }
        result = ses.analyze_growth(turns, "all")
        self.assertIn("all", result)


if __name__ == "__main__":
    unittest.main()
