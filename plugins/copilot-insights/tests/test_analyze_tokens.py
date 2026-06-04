#!/usr/bin/env python3
"""Regression tests for analyze_tokens.py.

Run: python3 plugins/copilot-insights/skills/copilot-insights/scripts/test_analyze_tokens.py
No third-party deps; uses only the stdlib unittest.
"""
import os
import json
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "copilot-insights", "scripts"))
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


class TimeTrackingTests(unittest.TestCase):
    def test_node_secs_unixnano(self):
        self.assertAlmostEqual(at.node_secs({"startTimeUnixNano": "1780087173000000000"}), 1780087173.0, places=3)

    def test_node_secs_array(self):
        self.assertAlmostEqual(at.node_secs({"startTime": [1780087173, 500000000]}), 1780087173.5, places=3)

    def test_node_secs_missing(self):
        self.assertIsNone(at.node_secs({}))

    def test_observe_time_tracks_first_last(self):
        a = at.Agg()
        a.observe_time(200.0)
        a.observe_time(100.0)
        a.observe_time(300.0)
        self.assertEqual(a.first_ts, 100.0)
        self.assertEqual(a.last_ts, 300.0)

    def test_merge_spans_time_range(self):
        a, b = at.Agg(), at.Agg()
        a.observe_time(100.0)
        b.observe_time(50.0)
        b.observe_time(400.0)
        a.merge(b)
        self.assertEqual(a.first_ts, 50.0)
        self.assertEqual(a.last_ts, 400.0)


class ScalarAndAttrsTests(unittest.TestCase):
    def test_scalar_plain(self):
        self.assertEqual(at._scalar(42), 42)
        self.assertEqual(at._scalar("hello"), "hello")
        self.assertEqual(at._scalar(3.14), 3.14)

    def test_scalar_otlp_int(self):
        self.assertEqual(at._scalar({"intValue": "123"}), 123)
        self.assertEqual(at._scalar({"intValue": 456}), 456)

    def test_scalar_otlp_double(self):
        self.assertEqual(at._scalar({"doubleValue": 2.5}), 2.5)

    def test_scalar_otlp_string(self):
        self.assertEqual(at._scalar({"stringValue": "x"}), "x")

    def test_scalar_otlp_bool(self):
        self.assertEqual(at._scalar({"boolValue": True}), True)

    def test_scalar_unhandled_returns_none(self):
        self.assertIsNone(at._scalar({"unknownKey": 1}))

    def test_attrs_list_form(self):
        attrs = [{"key": "k1", "value": {"stringValue": "v1"}}, {"key": "k2", "value": {"intValue": "42"}}]
        result = at.attrs_to_map(attrs)
        self.assertEqual(result, {"k1": "v1", "k2": 42})

    def test_attrs_dict_form(self):
        attrs = {"k1": "v1", "k2": 42}
        result = at.attrs_to_map(attrs)
        self.assertEqual(result, {"k1": "v1", "k2": 42})


class ModelAndSessionTests(unittest.TestCase):
    def test_model_of_response_model(self):
        self.assertEqual(at.model_of({"gen_ai.response.model": "gpt-4"}), "gpt-4")

    def test_model_of_request_model_fallback(self):
        self.assertEqual(
            at.model_of({"gen_ai.request.model": "gpt-3.5"}), "gpt-3.5"
        )

    def test_model_of_unknown(self):
        self.assertEqual(at.model_of({}), "unknown")

    def test_session_of_conversation_id(self):
        self.assertEqual(
            at.session_of({"gen_ai.conversation.id": "abc-123"}), "abc-123"
        )

    def test_session_of_session_id_fallback(self):
        self.assertEqual(at.session_of({"session.id": "xyz-789"}), "xyz-789")

    def test_session_of_unknown(self):
        self.assertEqual(at.session_of({}), "unknown")


class AggPropertyTests(unittest.TestCase):
    def test_total(self):
        a = at.Agg()
        a.input = 100
        a.output = 50
        self.assertEqual(a.total, 150)

    def test_naive_cost_none_when_unpriced(self):
        a = at.Agg()
        self.assertIsNone(a.naive_cost)

    def test_naive_cost_when_priced(self):
        a = at.Agg()
        a.naive_acc = 10.5
        a.priced_calls = 1
        self.assertEqual(a.naive_cost, 10.5)

    def test_naive_cost_zero_when_priced_but_zero(self):
        a = at.Agg()
        a.naive_acc = 0.0
        a.priced_calls = 1
        self.assertEqual(a.naive_cost, 0.0)

    def test_est_cost_none_when_unpriced(self):
        a = at.Agg()
        a.cost_acc = 5.0
        a.priced_calls = 0
        self.assertIsNone(a.est_cost)

    def test_row(self):
        a = at.Agg()
        a.calls = 5
        a.input = 100
        a.output = 50
        a.reasoning = 10
        a.cache_read = 20
        a.cache_creation = 5
        self.assertEqual(a.row(), [5, 100, 50, 10, 20, 5, 150])

    def test_merge_sums_all(self):
        a, b = at.Agg(), at.Agg()
        a.input = 100
        a.output = 50
        b.input = 30
        b.output = 20
        a.merge(b)
        self.assertEqual(a.input, 130)
        self.assertEqual(a.output, 70)

    def test_merge_time(self):
        a, b = at.Agg(), at.Agg()
        a.observe_time(200.0)
        b.observe_time(100.0)
        a.merge(b)
        self.assertEqual(a.first_ts, 100.0)


class WalkTests(unittest.TestCase):
    def test_walk_finds_span(self):
        spans = []
        metrics = []
        doc = {"traceId": "t1", "spanId": "s1", "name": "chat", "attributes": {"gen_ai.usage.input_tokens": 10}}
        at.walk(doc, spans.append, metrics.append)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0]["name"], "chat")

    def test_walk_finds_metric(self):
        spans = []
        metrics = []
        doc = {"name": "gen_ai.client.token.usage", "sum": {"dataPoints": []}}
        at.walk(doc, spans.append, metrics.append)
        self.assertEqual(len(metrics), 1)

    def test_walk_nested(self):
        spans = []
        metrics = []
        doc = {"root": {"spanId": "s1", "name": "x", "attributes": {}}}
        at.walk(doc, spans.append, metrics.append)
        self.assertEqual(len(spans), 1)

    def test_walk_no_match(self):
        spans = []
        metrics = []
        doc = {"just": "data"}
        at.walk(doc, spans.append, metrics.append)
        self.assertEqual(len(spans), 0)
        self.assertEqual(len(metrics), 0)


class AnalyzeTests(unittest.TestCase):
    def _span_node(self, input_tok=100, output_tok=50, model="claude-opus-4.8", attrs=None):
        amap = attrs or {
            "gen_ai.usage.input_tokens": input_tok,
            "gen_ai.usage.output_tokens": output_tok,
            "gen_ai.response.model": model,
            "gen_ai.conversation.id": "sess-1",
        }
        attr_list = [{"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": str(v)}} for k, v in amap.items()]
        return {"startTimeUnixNano": "1780087173000000000", "attributes": attr_list}

    def _metric_node(self, input_val=0, output_val=0):
        return {
            "name": "gen_ai.client.token.usage",
            "sum": {
                "dataPoints": [
                    {"attributes": [{"key": "gen_ai.token.type", "value": {"stringValue": "input"}}], "asInt": str(input_val)},
                    {"attributes": [{"key": "gen_ai.token.type", "value": {"stringValue": "output"}}], "asInt": str(output_val)},
                ]
            },
        }

    def _write_temp(self, docs):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        try:
            with os.fdopen(fd, "w") as f:
                for doc in docs:
                    f.write(json.dumps(doc) + "\n")
            return path
        except Exception:
            os.close(fd)
            raise

    def test_analyze_empty_file(self):
        path = self._write_temp([])
        try:
            groups, span_found, m_in, m_out = at.analyze([path], "all")
            self.assertFalse(span_found)
            self.assertEqual(m_in, 0)
            self.assertEqual(m_out, 0)
        finally:
            os.unlink(path)

    def test_analyze_spans_by_model(self):
        doc = self._span_node(input_tok=100, output_tok=50)
        path = self._write_temp([doc, doc])
        try:
            groups, span_found, m_in, m_out = at.analyze([path], "model")
            self.assertTrue(span_found)
            self.assertEqual(groups["claude-opus-4.8"].calls, 2)
            self.assertEqual(groups["claude-opus-4.8"].input, 200)
            self.assertEqual(groups["claude-opus-4.8"].output, 100)
        finally:
            os.unlink(path)

    def test_analyze_spans_by_session(self):
        doc = self._span_node(input_tok=100, output_tok=50)
        path = self._write_temp([doc])
        try:
            groups, span_found, m_in, m_out = at.analyze([path], "session")
            self.assertTrue(span_found)
            self.assertIn("sess-1", groups)
        finally:
            os.unlink(path)

    def test_analyze_spans_by_day(self):
        doc = self._span_node(input_tok=100, output_tok=50)
        path = self._write_temp([doc])
        try:
            groups, span_found, m_in, m_out = at.analyze([path], "day")
            self.assertTrue(span_found)
            self.assertIn("2026-05-29", groups)
        finally:
            os.unlink(path)

    def test_analyze_with_pricing(self):
        p = at.Pricing(currency="$")
        p.models["claude-opus-4.8"] = at.Rates(input=15.0, output=75.0)
        doc = self._span_node(input_tok=1_000_000, output_tok=1_000_000)
        path = self._write_temp([doc])
        try:
            groups, span_found, m_in, m_out = at.analyze([path], "model", pricing=p)
            self.assertTrue(span_found)
            self.assertIsNotNone(groups["claude-opus-4.8"].est_cost)
            self.assertAlmostEqual(groups["claude-opus-4.8"].est_cost, 90.0, places=6)
        finally:
            os.unlink(path)

    def test_analyze_metrics(self):
        doc = self._metric_node(input_val=500, output_val=200)
        path = self._write_temp([doc])
        try:
            groups, span_found, m_in, m_out = at.analyze([path], "all")
            self.assertFalse(span_found)
            self.assertEqual(m_in, 500)
            self.assertEqual(m_out, 200)
        finally:
            os.unlink(path)

    def test_analyze_span_no_usage_keys_ignored(self):
        doc = {"startTimeUnixNano": "1780087173000000000", "spanId": "s1", "attributes": [{"key": "other", "value": {"stringValue": "x"}}]}
        path = self._write_temp([doc])
        try:
            groups, span_found, m_in, m_out = at.analyze([path], "all")
            self.assertFalse(span_found)
        finally:
            os.unlink(path)

    def test_analyze_since_filter(self):
        doc = self._span_node(input_tok=100)
        path = self._write_temp([doc])
        try:
            groups, span_found, m_in, m_out = at.analyze([path], "all", since="2026-06-01")
            self.assertFalse(span_found)
        finally:
            os.unlink(path)

    def test_analyze_until_filter(self):
        doc = self._span_node(input_tok=100)
        path = self._write_temp([doc])
        try:
            groups, span_found, m_in, m_out = at.analyze([path], "all", until="2026-05-01")
            self.assertFalse(span_found)
        finally:
            os.unlink(path)

    def test_analyze_window_inclusive(self):
        doc = self._span_node(input_tok=100)
        path = self._write_temp([doc])
        try:
            groups, span_found, m_in, m_out = at.analyze([path], "all", since="2026-05-29", until="2026-05-29")
            self.assertTrue(span_found)
        finally:
            os.unlink(path)


class FormattingTests(unittest.TestCase):
    def test_money_with_value(self):
        self.assertEqual(at._money("$", 123.456), "$123.46")

    def test_money_none(self):
        self.assertEqual(at._money("$", None), "n/a")

    def test_money_euro(self):
        self.assertEqual(at._money("€", 50.0), "€50.00")

    def test_dt_with_value(self):
        self.assertEqual(at._dt(1780087173.0), "2026-05-29 20:39")

    def test_dt_none(self):
        self.assertEqual(at._dt(None), "unknown")


class TableTests(unittest.TestCase):
    def test_fmt_table_model_view(self):
        groups = {"model-a": at.Agg()}
        groups["model-a"].calls = 2
        groups["model-a"].input = 100
        groups["model-a"].output = 50
        table = at.fmt_table(groups, "model")
        self.assertIn("model-a", table)
        self.assertIn("TOTAL", table)
        self.assertIn("calls", table)

    def test_fmt_table_session_view(self):
        groups = {"sess-1": at.Agg()}
        groups["sess-1"].calls = 5
        groups["sess-1"].first_ts = 1780087173.0
        groups["sess-1"].last_ts = 1780087200.0
        table = at.fmt_table(groups, "session")
        self.assertIn("sess-1", table)
        self.assertIn("session", table)

    def test_fmt_table_with_cost(self):
        p = at.Pricing(currency="$")
        p.models["model-a"] = at.Rates(input=10.0, output=20.0)
        groups = {"model-a": at.Agg()}
        groups["model-a"].calls = 1
        groups["model-a"].input = 1_000_000
        groups["model-a"].output = 0
        groups["model-a"].cost_acc = 10.0
        groups["model-a"].naive_acc = 10.0
        groups["model-a"].priced_calls = 1
        table = at.fmt_table(groups, "model", pricing=p)
        self.assertIn("est_cost", table)
        self.assertIn("$10.00", table)

    def test_fmt_table_show_time(self):
        groups = {"model-a": at.Agg()}
        groups["model-a"].first_ts = 1000.0
        groups["model-a"].last_ts = 2000.0
        table = at.fmt_table(groups, "model", show_time=True)
        self.assertIn("first (UTC)", table)

    def test_fmt_table_top_n(self):
        groups = {f"model-{i}": at.Agg() for i in range(5)}
        for i, g in groups.items():
            g.input = int(i.split("-")[1])
            g.output = 0
        table = at.fmt_table(groups, "model", top=2)
        # top 2 models by total + TOTAL row = 3 data rows
        lines = [l for l in table.split("\n") if l and not l.startswith("-")]
        # headers + 2 data + TOTAL = should contain model-4, model-3 (highest totals)
        self.assertIn("model-4", table)

    def test_fmt_table_empty(self):
        groups = {}
        table = at.fmt_table(groups, "model")
        self.assertIn("TOTAL", table)


class JsonResultTests(unittest.TestCase):
    def _group(self, **kwargs):
        a = at.Agg()
        for k, v in kwargs.items():
            setattr(a, k, v)
        return {"m": a}

    def test_source_spans(self):
        groups = self._group(calls=1, input=10, output=5)
        result = at.to_json_result(groups, span_found=True, pricing=at.Pricing(), show_time=False, m_in=0, m_out=0)
        self.assertEqual(result["source"], "spans")

    def test_source_metrics(self):
        groups = {}
        result = at.to_json_result(groups, span_found=False, pricing=at.Pricing(), show_time=False, m_in=100, m_out=50)
        self.assertEqual(result["source"], "metrics")

    def test_group_fields(self):
        groups = self._group(calls=5, input=100, output=50, reasoning=10, cache_read=20, cache_creation=5)
        g = groups["m"]
        result = at.to_json_result(groups, span_found=True, pricing=at.Pricing(), show_time=False, m_in=0, m_out=0)
        mg = result["groups"]["m"]
        self.assertEqual(mg["calls"], 5)
        self.assertEqual(mg["input_tokens"], 100)
        self.assertEqual(mg["output_tokens"], 50)
        self.assertEqual(mg["reasoning_output_tokens"], 10)
        self.assertEqual(mg["cache_read_input_tokens"], 20)
        self.assertEqual(mg["cache_creation_input_tokens"], 5)
        self.assertEqual(mg["fresh_input_tokens"], 75)  # 100 - 20 - 5
        self.assertEqual(mg["total_tokens"], 150)

    def test_no_cost_when_unpriced(self):
        groups = self._group(calls=1, input=100, output=50)
        result = at.to_json_result(groups, span_found=True, pricing=at.Pricing(), show_time=False, m_in=0, m_out=0)
        mg = result["groups"]["m"]
        self.assertNotIn("est_cost", mg)
        self.assertNotIn("priced_calls", mg)
        self.assertNotIn("pricing", result)

    def test_cost_when_priced(self):
        p = at.Pricing(currency="$")
        p.models["m"] = at.Rates(input=10.0, output=20.0)
        groups = self._group(calls=1, input=1_000_000, output=0, cost_acc=10.0, priced_calls=1)
        result = at.to_json_result(groups, span_found=True, pricing=p, show_time=False, m_in=0, m_out=0)
        mg = result["groups"]["m"]
        self.assertEqual(mg["est_cost"], 10.0)
        self.assertEqual(mg["priced_calls"], 1)
        self.assertEqual(result["pricing"]["currency"], "$")

    def test_show_time_includes_timestamps(self):
        groups = self._group(calls=1, input=10, output=5)
        groups["m"].first_ts = 1000.0
        groups["m"].last_ts = 2000.0
        result = at.to_json_result(groups, span_found=True, pricing=at.Pricing(), show_time=True, m_in=0, m_out=0)
        mg = result["groups"]["m"]
        self.assertIn("first_ts", mg)
        self.assertIn("last_ts", mg)
        self.assertIsNotNone(mg["first_ts"])
        self.assertIsNotNone(mg["last_ts"])

    def test_hide_time_excludes_timestamps(self):
        groups = self._group(calls=1, input=10, output=5)
        groups["m"].first_ts = 1000.0
        groups["m"].last_ts = 2000.0
        result = at.to_json_result(groups, span_found=True, pricing=at.Pricing(), show_time=False, m_in=0, m_out=0)
        mg = result["groups"]["m"]
        self.assertNotIn("first_ts", mg)
        self.assertNotIn("last_ts", mg)

    def test_per_model_pricing_metadata(self):
        p = at.Pricing(currency="€")
        p.models["alpha"] = at.Rates(input=5.0)
        p.models["beta"] = at.Rates(input=3.0)
        groups = self._group(calls=1, input=100, cost_acc=0.5, priced_calls=1)
        result = at.to_json_result(groups, span_found=True, pricing=p, show_time=False, m_in=0, m_out=0)
        self.assertEqual(result["pricing"]["per_model"], True)
        self.assertEqual(set(result["pricing"]["models_priced"]), {"alpha", "beta"})

    def test_default_pricing_no_models_priced(self):
        p = at.Pricing(currency="$", default=at.Rates(input=1.0))
        groups = self._group(calls=1, input=100, cost_acc=0.1, priced_calls=1)
        result = at.to_json_result(groups, span_found=True, pricing=p, show_time=False, m_in=0, m_out=0)
        self.assertEqual(result["pricing"]["per_model"], False)
        self.assertIsNone(result["pricing"]["models_priced"])

    def test_metric_token_usage(self):
        groups = {}
        result = at.to_json_result(groups, span_found=False, pricing=at.Pricing(), show_time=False, m_in=500, m_out=200)
        self.assertEqual(result["metric_token_usage"]["input"], 500)
        self.assertEqual(result["metric_token_usage"]["output"], 200)
        self.assertEqual(result["metric_token_usage"]["total"], 700)

    def test_cost_disclaimer_present(self):
        p = at.Pricing(currency="$")
        p.models["m"] = at.Rates(input=1.0)
        groups = self._group(calls=1, input=100, cost_acc=0.1, priced_calls=1)
        result = at.to_json_result(groups, span_found=True, pricing=p, show_time=False, m_in=0, m_out=0)
        self.assertEqual(result["cost_disclaimer"], "estimate only — not billing-grade")


class CollectSessionTimesTests(unittest.TestCase):
    """Tests for collect_session_times() — the pre-scan used by --last N."""

    def _span_node(self, session_id, ts_nano_str, input_tok=100):
        amap = {
            "gen_ai.usage.input_tokens": input_tok,
            "gen_ai.conversation.id": session_id,
        }
        attr_list = [
            {"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": str(v)}}
            for k, v in amap.items()
        ]
        return {"startTimeUnixNano": ts_nano_str, "attributes": attr_list}

    def _write_temp(self, docs):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        try:
            with os.fdopen(fd, "w") as f:
                for doc in docs:
                    f.write(json.dumps(doc) + "\n")
            return path
        except Exception:
            os.close(fd)
            raise

    def test_empty_file_returns_empty_dict(self):
        path = self._write_temp([])
        try:
            result = at.collect_session_times([path])
            self.assertEqual(result, {})
        finally:
            os.unlink(path)

    def test_single_session_captured(self):
        doc = self._span_node("sess-aaa", "1780087173000000000")
        path = self._write_temp([doc])
        try:
            result = at.collect_session_times([path])
            self.assertIn("sess-aaa", result)
            self.assertAlmostEqual(result["sess-aaa"], 1780087173.0, places=1)
        finally:
            os.unlink(path)

    def test_multiple_sessions_all_captured(self):
        docs = [
            self._span_node("sess-aaa", "1780087173000000000"),
            self._span_node("sess-bbb", "1780173573000000000"),
        ]
        path = self._write_temp(docs)
        try:
            result = at.collect_session_times([path])
            self.assertIn("sess-aaa", result)
            self.assertIn("sess-bbb", result)
        finally:
            os.unlink(path)

    def test_picks_earliest_timestamp_for_session(self):
        """Two spans for the same session — earliest ts must win."""
        docs = [
            self._span_node("sess-aaa", "1780173573000000000"),  # later
            self._span_node("sess-aaa", "1780087173000000000"),  # earlier
        ]
        path = self._write_temp(docs)
        try:
            result = at.collect_session_times([path])
            self.assertAlmostEqual(result["sess-aaa"], 1780087173.0, places=1)
        finally:
            os.unlink(path)

    def test_since_filter_excludes_old_session(self):
        """Span from 2026-05-29 should be excluded when since='2026-06-01'."""
        doc = self._span_node("sess-old", "1780087173000000000")  # 2026-05-29
        path = self._write_temp([doc])
        try:
            result = at.collect_session_times([path], since="2026-06-01")
            self.assertEqual(result, {})
        finally:
            os.unlink(path)

    def test_date_window_inclusive(self):
        """Span exactly on the since=until boundary must be included."""
        doc = self._span_node("sess-x", "1780087173000000000")  # 2026-05-29
        path = self._write_temp([doc])
        try:
            result = at.collect_session_times([path], since="2026-05-29", until="2026-05-29")
            self.assertIn("sess-x", result)
        finally:
            os.unlink(path)

    def test_span_without_usage_key_ignored(self):
        """Spans lacking any usage key must not appear in the result."""
        doc = {"startTimeUnixNano": "1780087173000000000", "attributes": [
            {"key": "other.key", "value": {"stringValue": "v"}}
        ]}
        path = self._write_temp([doc])
        try:
            result = at.collect_session_times([path])
            self.assertEqual(result, {})
        finally:
            os.unlink(path)


class SessionFilterAnalyzeTests(unittest.TestCase):
    """Tests for session_filter and session_ids parameters of analyze()."""

    def _span_node(self, session_id, ts_nano="1780087173000000000", input_tok=100):
        amap = {
            "gen_ai.usage.input_tokens": input_tok,
            "gen_ai.usage.output_tokens": 50,
            "gen_ai.response.model": "claude-haiku-4.5",
            "gen_ai.conversation.id": session_id,
        }
        attr_list = [
            {"key": k, "value": {"intValue": str(v)} if isinstance(v, int) else {"stringValue": str(v)}}
            for k, v in amap.items()
        ]
        return {"startTimeUnixNano": ts_nano, "attributes": attr_list}

    def _write_temp(self, docs):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        try:
            with os.fdopen(fd, "w") as f:
                for doc in docs:
                    f.write(json.dumps(doc) + "\n")
            return path
        except Exception:
            os.close(fd)
            raise

    def _make_file(self, sessions):
        """Write spans for multiple sessions. sessions = {sid: input_tok}."""
        docs = [self._span_node(sid, input_tok=tok) for sid, tok in sessions.items()]
        return self._write_temp(docs)

    def test_session_filter_includes_matching_session(self):
        path = self._make_file({"sess-aaa": 100, "sess-bbb": 200})
        try:
            groups, found, _, _ = at.analyze([path], "session", session_filter="sess-aaa")
            self.assertIn("sess-aaa", groups)
            self.assertNotIn("sess-bbb", groups)
        finally:
            os.unlink(path)

    def test_session_filter_excludes_non_matching(self):
        path = self._make_file({"sess-aaa": 100})
        try:
            groups, found, _, _ = at.analyze([path], "session", session_filter="other")
            self.assertFalse(found)
            self.assertEqual(len(groups), 0)
        finally:
            os.unlink(path)

    def test_session_filter_prefix_match(self):
        """Partial prefix 'sess' should match 'sess-aaa'."""
        path = self._make_file({"sess-aaa": 100, "other-zzz": 200})
        try:
            groups, found, _, _ = at.analyze([path], "session", session_filter="sess")
            self.assertIn("sess-aaa", groups)
            self.assertNotIn("other-zzz", groups)
        finally:
            os.unlink(path)

    def test_session_ids_frozenset_includes_matching(self):
        path = self._make_file({"sess-aaa": 100, "sess-bbb": 200})
        try:
            groups, found, _, _ = at.analyze([path], "session",
                                             session_ids=frozenset({"sess-aaa"}))
            self.assertIn("sess-aaa", groups)
            self.assertNotIn("sess-bbb", groups)
        finally:
            os.unlink(path)

    def test_session_ids_frozenset_excludes_non_matching(self):
        path = self._make_file({"sess-aaa": 100})
        try:
            groups, found, _, _ = at.analyze([path], "session",
                                             session_ids=frozenset({"other-zzz"}))
            self.assertFalse(found)
        finally:
            os.unlink(path)

    def test_session_ids_empty_frozenset_excludes_all(self):
        path = self._make_file({"sess-aaa": 100, "sess-bbb": 200})
        try:
            groups, found, _, _ = at.analyze([path], "session",
                                             session_ids=frozenset())
            self.assertFalse(found)
        finally:
            os.unlink(path)

    def test_session_ids_multiple_sessions(self):
        path = self._make_file({"sess-aaa": 100, "sess-bbb": 200, "sess-ccc": 300})
        try:
            groups, found, _, _ = at.analyze([path], "session",
                                             session_ids=frozenset({"sess-aaa", "sess-ccc"}))
            self.assertIn("sess-aaa", groups)
            self.assertIn("sess-ccc", groups)
            self.assertNotIn("sess-bbb", groups)
        finally:
            os.unlink(path)

    def test_no_session_filter_includes_all(self):
        path = self._make_file({"sess-aaa": 100, "sess-bbb": 200})
        try:
            groups, found, _, _ = at.analyze([path], "session")
            self.assertIn("sess-aaa", groups)
            self.assertIn("sess-bbb", groups)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
