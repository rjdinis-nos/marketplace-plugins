#!/usr/bin/env python3
"""Analyze GitHub Copilot CLI token consumption from OpenTelemetry output.

Reads the JSON-lines file produced by the CLI's OTel file exporter
(COPILOT_OTEL_FILE_EXPORTER_PATH) and aggregates token usage that follows
the OTel GenAI Semantic Conventions.

Token data is read from two possible sources:
  * spans   -> attributes gen_ai.usage.input_tokens / output_tokens /
               reasoning.output_tokens / cache_read.input_tokens /
               cache_creation.input_tokens
  * metrics -> the gen_ai.client.token.usage histogram, split by the
               gen_ai.token.type attribute (input/output)

The file is walked recursively, so it works regardless of the exact
OTLP nesting. Output is a per-model / per-day / per-session summary.
"""
from __future__ import annotations

import argparse
import datetime
import glob
import gzip
import json
import os
import sys
from collections import defaultdict

USAGE_KEYS = {
    "gen_ai.usage.input_tokens": "input",
    "gen_ai.usage.output_tokens": "output",
    "gen_ai.usage.reasoning.output_tokens": "reasoning",
    "gen_ai.usage.cache_read.input_tokens": "cache_read",
    "gen_ai.usage.cache_creation.input_tokens": "cache_creation",
}
TOKEN_METRIC = "gen_ai.client.token.usage"


def _scalar(v):
    """Decode an OTLP AnyValue or a plain scalar."""
    if isinstance(v, dict):
        for k in ("intValue", "doubleValue", "stringValue", "boolValue"):
            if k in v:
                val = v[k]
                if k == "intValue":
                    try:
                        return int(val)
                    except (TypeError, ValueError):
                        return val
                return val
        return None
    return v


def attrs_to_map(attributes):
    """Normalize attributes to a flat dict. Handles OTLP array form
    [{key, value:{...}}] and plain-object form."""
    out = {}
    if isinstance(attributes, list):
        for a in attributes:
            if isinstance(a, dict) and "key" in a:
                out[a["key"]] = _scalar(a.get("value"))
    elif isinstance(attributes, dict):
        for k, v in attributes.items():
            out[k] = _scalar(v)
    return out


def model_of(amap):
    return (
        amap.get("gen_ai.response.model")
        or amap.get("gen_ai.request.model")
        or "unknown"
    )


def session_of(amap):
    return amap.get("gen_ai.conversation.id") or amap.get("session.id") or "unknown"


def _fmt_day(secs):
    return datetime.datetime.fromtimestamp(secs, datetime.timezone.utc).strftime("%Y-%m-%d")


def day_of(node):
    """Best-effort date from a span/datapoint timestamp.

    Handles both the raw OTLP/protobuf style (single integer nanoseconds under
    *UnixNano keys) and the OpenTelemetry JS SDK style used by the CLI's file
    exporter (startTime/endTime as a [seconds, nanoseconds] pair).
    """
    # OTLP/protobuf style: single integer nanoseconds since epoch.
    for key in ("startTimeUnixNano", "timeUnixNano", "endTimeUnixNano"):
        ts = node.get(key)
        if ts:
            try:
                return _fmt_day(int(ts) / 1e9)
            except (TypeError, ValueError, OverflowError):
                pass

    # OTel JS SDK style: [seconds, nanoseconds] pair.
    for key in ("startTime", "endTime"):
        ts = node.get(key)
        if isinstance(ts, (list, tuple)) and ts:
            try:
                return _fmt_day(int(ts[0]))
            except (TypeError, ValueError, OverflowError, IndexError):
                pass

    return "unknown"


class Rates:
    """Per-Mtok pricing used to estimate cost. All values are $/Mtok (or the
    chosen currency unit). Cost figures are ESTIMATES, never billing-grade."""

    FIELDS = ("input", "output", "cache_read", "cache_write")

    def __init__(self, input=0.0, output=0.0, cache_read=0.0, cache_write=0.0, currency="$"):
        self.input = float(input or 0.0)
        self.output = float(output or 0.0)
        self.cache_read = float(cache_read or 0.0)
        self.cache_write = float(cache_write or 0.0)
        self.currency = currency

    def any(self):
        return any(getattr(self, f) for f in self.FIELDS)

    @classmethod
    def from_args(cls, args):
        data = {}
        if getattr(args, "rates", None):
            with open(os.path.expanduser(args.rates), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        elif os.environ.get("COPILOT_TOKEN_RATES"):
            with open(os.path.expanduser(os.environ["COPILOT_TOKEN_RATES"]), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        # Explicit CLI flags override file/env values.
        merged = {
            "input": args.rate_input if args.rate_input is not None else data.get("input"),
            "output": args.rate_output if args.rate_output is not None else data.get("output"),
            "cache_read": args.rate_cache_read if args.rate_cache_read is not None else data.get("cache_read"),
            "cache_write": args.rate_cache_write if args.rate_cache_write is not None else data.get("cache_write"),
        }
        currency = args.currency or data.get("currency") or "$"
        return cls(currency=currency, **{k: v for k, v in merged.items() if v is not None})


class Agg:
    def __init__(self):
        self.input = 0
        self.output = 0
        self.reasoning = 0
        self.cache_read = 0
        self.cache_creation = 0
        self.calls = 0

    def add_span(self, amap):
        found = False
        for key, field in USAGE_KEYS.items():
            if key in amap and amap[key] is not None:
                try:
                    setattr(self, field, getattr(self, field) + int(amap[key]))
                    found = True
                except (TypeError, ValueError):
                    pass
        if found:
            self.calls += 1
        return found

    @property
    def fresh_input(self):
        """Uncached input tokens (full price). cache_read/cache_creation are
        subsets of input_tokens, so the freshly-processed remainder is
        input - cache_read - cache_creation."""
        return max(self.input - self.cache_read - self.cache_creation, 0)

    @property
    def total(self):
        return self.input + self.output

    def cost(self, rates):
        """Estimated cost given a Rates ($/Mtok). Returns None if no rates."""
        if rates is None or not rates.any():
            return None
        return (
            self.fresh_input / 1e6 * rates.input
            + self.cache_creation / 1e6 * rates.cache_write
            + self.cache_read / 1e6 * rates.cache_read
            + self.output / 1e6 * rates.output
        )

    def naive_cost(self, rates):
        """Cost if every input token were billed at the full input rate
        (i.e. no prompt-cache discount)."""
        if rates is None or not rates.any():
            return None
        return self.input / 1e6 * rates.input + self.output / 1e6 * rates.output

    def row(self):
        return [
            self.calls,
            self.input,
            self.output,
            self.reasoning,
            self.cache_read,
            self.cache_creation,
            self.total,
        ]


def walk(node, on_span, on_metric):
    if isinstance(node, dict):
        # A span/log record carries an attributes collection.
        if "attributes" in node and ("startTimeUnixNano" in node or "spanId" in node or "name" in node):
            on_span(node)
        # A metric object.
        if node.get("name") == TOKEN_METRIC or ("sum" in node and node.get("name") == TOKEN_METRIC):
            on_metric(node)
        for v in node.values():
            walk(v, on_span, on_metric)
    elif isinstance(node, list):
        for v in node:
            walk(v, on_span, on_metric)


def _open_text(path):
    """Open a log file as text, transparently decompressing .gz."""
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="ignore")
    return open(path, "r", encoding="utf-8", errors="ignore")


def resolve_paths(path, include_rotated):
    """Return the list of files to read. With include_rotated, also pick up
    rotated/compressed siblings produced by the rotator (e.g. otel-signals.jsonl.1,
    otel-signals.jsonl-20260529.gz)."""
    paths = [path] if os.path.exists(path) else []
    if include_rotated:
        seen = set(paths)
        for sib in glob.glob(glob.escape(path) + "*"):
            if sib not in seen and os.path.isfile(sib):
                paths.append(sib)
                seen.add(sib)
    return paths


def analyze(paths, group_by, since=None, until=None):
    groups = defaultdict(Agg)
    span_usage_found = False
    metric_input = 0
    metric_output = 0

    def in_window(node):
        if since is None and until is None:
            return True
        d = day_of(node)
        if d == "unknown":
            return False
        if since and d < since:
            return False
        if until and d > until:
            return False
        return True

    def on_span(node):
        nonlocal span_usage_found
        amap = attrs_to_map(node.get("attributes"))
        if not any(k in amap for k in USAGE_KEYS):
            return
        if not in_window(node):
            return
        if group_by == "model":
            key = model_of(amap)
        elif group_by == "session":
            key = session_of(amap)
        elif group_by == "day":
            key = day_of(node)
        else:
            key = "all"
        if groups[key].add_span(amap):
            span_usage_found = True

    def on_metric(node):
        nonlocal metric_input, metric_output
        dps = (node.get("histogram") or node.get("sum") or {}).get("dataPoints", [])
        for dp in dps:
            amap = attrs_to_map(dp.get("attributes"))
            ttype = amap.get("gen_ai.token.type")
            val = dp.get("sum")
            if val is None:
                val = _scalar(dp.get("asInt") or dp.get("asDouble"))
            try:
                val = int(val)
            except (TypeError, ValueError):
                continue
            if ttype == "input":
                metric_input += val
            elif ttype == "output":
                metric_output += val

    for path in paths:
        with _open_text(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError:
                    continue
                walk(doc, on_span, on_metric)

    return groups, span_usage_found, metric_input, metric_output


def fmt_table(groups, group_by, rates=None, top=None):
    show_cost = rates is not None and rates.any()
    headers = [group_by, "calls", "input", "output", "reasoning", "cache_rd", "cache_cr", "total"]
    if show_cost:
        headers.append("est_cost")
    ordered = sorted(groups, key=lambda k: -groups[k].total)
    tot = Agg()
    for key in ordered:
        g = groups[key]
        for f in ("input", "output", "reasoning", "cache_read", "cache_creation", "calls"):
            setattr(tot, f, getattr(tot, f) + getattr(g, f))
    if top is not None:
        ordered = ordered[:top]
    rows = []
    for key in ordered:
        g = groups[key]
        row = [key] + g.row()
        if show_cost:
            row.append(f"{rates.currency}{g.cost(rates):,.2f}")
        rows.append(row)
    total_row = ["TOTAL"] + tot.row()
    if show_cost:
        total_row.append(f"{rates.currency}{tot.cost(rates):,.2f}")
    rows.append(total_row)
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    out = []
    for r in [headers] + rows:
        out.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))
    out.insert(1, "  ".join("-" * w for w in widths))
    table = "\n".join(out)
    if show_cost:
        naive = tot.naive_cost(rates)
        cached = tot.cost(rates)
        table += (
            f"\n\nCost estimate uses rates ({rates.currency}/Mtok): "
            f"input={rates.input} output={rates.output} "
            f"cache_read={rates.cache_read} cache_write={rates.cache_write}"
            f"\nWithout cache discount: {rates.currency}{naive:,.2f}  |  "
            f"saved by caching: {rates.currency}{naive - cached:,.2f}"
            f"\n(estimate only — not billing-grade; supply your real rates)"
        )
    return table


def main():
    default_path = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH") or os.path.expanduser(
        "~/.copilot/logs/otel-signals.jsonl"
    )
    p = argparse.ArgumentParser(description="Summarize Copilot CLI token usage from OTel output.")
    p.add_argument("path", nargs="?", default=default_path, help="OTel JSONL file (default: $COPILOT_OTEL_FILE_EXPORTER_PATH)")
    p.add_argument("--by", choices=["model", "session", "day", "all"], default="model", help="grouping dimension")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    p.add_argument("--top", type=int, default=None, metavar="N", help="show only the top N groups by total tokens")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="only count calls on/after this UTC date")
    p.add_argument("--until", metavar="YYYY-MM-DD", help="only count calls on/before this UTC date")
    p.add_argument("--rates", metavar="FILE", help="JSON file with per-Mtok rates (keys: input/output/cache_read/cache_write/currency); also via $COPILOT_TOKEN_RATES")
    p.add_argument("--rate-input", type=float, default=None, help="$/Mtok for fresh input tokens")
    p.add_argument("--rate-output", type=float, default=None, help="$/Mtok for output tokens")
    p.add_argument("--rate-cache-read", type=float, default=None, help="$/Mtok for cache-read input tokens")
    p.add_argument("--rate-cache-write", type=float, default=None, help="$/Mtok for cache-creation input tokens")
    p.add_argument("--currency", default=None, help="currency symbol for cost output (default: $)")
    p.add_argument(
        "--current-only",
        action="store_true",
        help="read only the active log; by default rotated/compressed siblings (PATH*, .gz) are included",
    )
    args = p.parse_args()

    try:
        rates = Rates.from_args(args)
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"Could not read rates: {e}")

    paths = resolve_paths(args.path, include_rotated=not args.current_only)
    if not paths:
        sys.exit(
            f"OTel file not found: {args.path}\n"
            "Enable the exporter first, e.g.:\n"
            '  export COPILOT_OTEL_FILE_EXPORTER_PATH="$HOME/.copilot/logs/otel-signals.jsonl"\n'
            "then run copilot and retry."
        )

    groups, span_found, m_in, m_out = analyze(paths, args.by, since=args.since, until=args.until)

    if args.json:
        show_cost = rates.any()
        result = {
            "source": "spans" if span_found else "metrics",
            "groups": {
                k: {
                    "calls": g.calls,
                    "input_tokens": g.input,
                    "output_tokens": g.output,
                    "reasoning_output_tokens": g.reasoning,
                    "cache_read_input_tokens": g.cache_read,
                    "cache_creation_input_tokens": g.cache_creation,
                    "fresh_input_tokens": g.fresh_input,
                    "total_tokens": g.total,
                    **({"est_cost": round(g.cost(rates), 6)} if show_cost else {}),
                }
                for k, g in groups.items()
            },
            "metric_token_usage": {"input": m_in, "output": m_out, "total": m_in + m_out},
        }
        if show_cost:
            result["rates"] = {f: getattr(rates, f) for f in Rates.FIELDS}
            result["rates"]["currency"] = rates.currency
            result["cost_disclaimer"] = "estimate only — not billing-grade"
        print(json.dumps(result, indent=2))
        return

    if span_found:
        print(fmt_table(groups, args.by, rates=rates, top=args.top))
        print(f"\n(source: span attributes gen_ai.usage.*  files: {len(paths)})")
    elif m_in or m_out:
        print("No per-call span usage found; reporting gen_ai.client.token.usage metric:")
        print(f"  input tokens : {m_in}")
        print(f"  output tokens: {m_out}")
        print(f"  total tokens : {m_in + m_out}")
        print(f"\n(source: token metric  files: {len(paths)})")
    else:
        print("No GenAI token usage found in the file.")
        print("Make sure OTel is enabled and at least one model call has occurred.")


if __name__ == "__main__":
    main()
