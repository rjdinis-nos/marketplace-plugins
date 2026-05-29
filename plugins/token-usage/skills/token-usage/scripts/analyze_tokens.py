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


def _fmt_datetime(secs):
    return datetime.datetime.fromtimestamp(secs, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")


def _iso(secs):
    if secs is None:
        return None
    return datetime.datetime.fromtimestamp(secs, datetime.timezone.utc).isoformat()


def node_secs(node):
    """Best-effort epoch seconds (float) from a span/datapoint timestamp, or
    None. Handles both the raw OTLP/protobuf style (integer nanoseconds under
    *UnixNano keys) and the OpenTelemetry JS SDK style used by the CLI's file
    exporter (startTime/endTime as a [seconds, nanoseconds] pair)."""
    # OTLP/protobuf style: single integer nanoseconds since epoch.
    for key in ("startTimeUnixNano", "timeUnixNano", "endTimeUnixNano"):
        ts = node.get(key)
        if ts:
            try:
                return int(ts) / 1e9
            except (TypeError, ValueError, OverflowError):
                pass

    # OTel JS SDK style: [seconds, nanoseconds] pair.
    for key in ("startTime", "endTime"):
        ts = node.get(key)
        if isinstance(ts, (list, tuple)) and ts:
            try:
                return float(ts[0]) + (float(ts[1]) / 1e9 if len(ts) > 1 else 0.0)
            except (TypeError, ValueError, OverflowError, IndexError):
                pass

    return None


def day_of(node):
    """Best-effort UTC date string from a span/datapoint timestamp."""
    secs = node_secs(node)
    return _fmt_day(secs) if secs is not None else "unknown"


def normalize_model(name):
    """Normalize a model id/display name to a lookup key, e.g.
    'Claude Opus 4.8' and 'claude-opus-4.8' both -> 'claude-opus-4.8'."""
    import re

    name = re.sub(r"\[\^?\d+\]", "", str(name or ""))
    return name.strip().lower().replace(" ", "-")


class Rates:
    """Per-Mtok pricing for one model (or a default). All values are $/Mtok in
    the chosen currency. Cost figures are ESTIMATES, never billing-grade.
    `cache_write=None` means cache-creation tokens are billed at the input rate
    (e.g. non-Anthropic models that have no separate cache-write price)."""

    def __init__(self, input=0.0, output=0.0, cache_read=0.0, cache_write=None):
        self.input = float(input or 0.0)
        self.output = float(output or 0.0)
        self.cache_read = float(cache_read or 0.0)
        self.cache_write = None if cache_write is None else float(cache_write)

    def any(self):
        return any((self.input, self.output, self.cache_read, self.cache_write or 0.0))

    @property
    def _cache_write(self):
        return self.cache_write if self.cache_write is not None else self.input

    def cost(self, input, output, cache_read, cache_creation):
        fresh = max(input - cache_read - cache_creation, 0)
        return (
            fresh / 1e6 * self.input
            + cache_creation / 1e6 * self._cache_write
            + cache_read / 1e6 * self.cache_read
            + output / 1e6 * self.output
        )

    def naive(self, input, output):
        """Cost with no prompt-cache discount (all input at the input rate)."""
        return input / 1e6 * self.input + output / 1e6 * self.output

    @classmethod
    def from_dict(cls, d):
        return cls(
            input=d.get("input"),
            output=d.get("output"),
            cache_read=d.get("cache_read"),
            cache_write=d.get("cache_write"),
        )


class Pricing:
    """Resolves per-model Rates from a `models` map, with an optional `default`
    block (or CLI `--rate-*` flags) used for models not in the table."""

    def __init__(self, currency="$", default=None):
        self.currency = currency
        self.default = default
        self.models = {}

    def any(self):
        if self.default and self.default.any():
            return True
        return any(r.any() for r in self.models.values())

    def per_model(self):
        return bool(self.models)

    def rates_for(self, model):
        r = self.models.get(normalize_model(model))
        if r and r.any():
            return r
        if self.default and self.default.any():
            return self.default
        return None

    @classmethod
    def from_args(cls, args):
        data = {}
        path = getattr(args, "rates", None) or os.environ.get("COPILOT_TOKEN_RATES")
        if path:
            with open(os.path.expanduser(path), "r", encoding="utf-8") as fh:
                data = json.load(fh)

        currency = args.currency or data.get("currency") or "$"
        pricing = cls(currency=currency)

        # Per-model map.
        for key, rec in (data.get("models") or {}).items():
            pricing.models[normalize_model(key)] = Rates.from_dict(rec)

        # Fallback rate for models not in the table: a `default` file block,
        # overridden by any explicit CLI --rate-* flags.
        default_block = data.get("default") or {}
        merged = {
            "input": args.rate_input if args.rate_input is not None else default_block.get("input"),
            "output": args.rate_output if args.rate_output is not None else default_block.get("output"),
            "cache_read": args.rate_cache_read if args.rate_cache_read is not None else default_block.get("cache_read"),
            "cache_write": args.rate_cache_write if args.rate_cache_write is not None else default_block.get("cache_write"),
        }
        default = Rates.from_dict(merged)
        if default.any():
            pricing.default = default
        return pricing


class Agg:
    def __init__(self):
        self.input = 0
        self.output = 0
        self.reasoning = 0
        self.cache_read = 0
        self.cache_creation = 0
        self.calls = 0
        self.cost_acc = 0.0
        self.naive_acc = 0.0
        self.priced_calls = 0
        self.first_ts = None
        self.last_ts = None

    def observe_time(self, secs):
        if secs is None:
            return
        if self.first_ts is None or secs < self.first_ts:
            self.first_ts = secs
        if self.last_ts is None or secs > self.last_ts:
            self.last_ts = secs

    def add_span(self, amap, pricing=None):
        vals = {}
        found = False
        for key, field in USAGE_KEYS.items():
            if key in amap and amap[key] is not None:
                try:
                    v = int(amap[key])
                    vals[field] = v
                    setattr(self, field, getattr(self, field) + v)
                    found = True
                except (TypeError, ValueError):
                    pass
        if found:
            self.calls += 1
            if pricing is not None:
                r = pricing.rates_for(model_of(amap))
                if r is not None:
                    inp = vals.get("input", 0)
                    out = vals.get("output", 0)
                    crd = vals.get("cache_read", 0)
                    ccr = vals.get("cache_creation", 0)
                    self.cost_acc += r.cost(inp, out, crd, ccr)
                    self.naive_acc += r.naive(inp, out)
                    self.priced_calls += 1
        return found

    def merge(self, other):
        for f in ("input", "output", "reasoning", "cache_read", "cache_creation", "calls",
                  "cost_acc", "naive_acc", "priced_calls"):
            setattr(self, f, getattr(self, f) + getattr(other, f))
        self.observe_time(other.first_ts)
        self.observe_time(other.last_ts)

    @property
    def fresh_input(self):
        """Uncached input tokens (full price). cache_read/cache_creation are
        subsets of input_tokens, so the freshly-processed remainder is
        input - cache_read - cache_creation."""
        return max(self.input - self.cache_read - self.cache_creation, 0)

    @property
    def total(self):
        return self.input + self.output

    @property
    def est_cost(self):
        """Accumulated per-span cost estimate, or None if nothing was priced."""
        return self.cost_acc if self.priced_calls else None

    @property
    def naive_cost(self):
        return self.naive_acc if self.priced_calls else None

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


def analyze(paths, group_by, since=None, until=None, pricing=None):
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
        if groups[key].add_span(amap, pricing):
            span_usage_found = True
            groups[key].observe_time(node_secs(node))

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


def _money(currency, val):
    return f"{currency}{val:,.2f}" if val is not None else "n/a"


def _dt(secs):
    return _fmt_datetime(secs) if secs is not None else "unknown"


def fmt_table(groups, group_by, pricing=None, top=None, show_time=False):
    show_cost = pricing is not None and pricing.any()
    cur = pricing.currency if pricing is not None else "$"
    headers = [group_by, "calls", "input", "output", "reasoning", "cache_rd", "cache_cr", "total"]
    if show_cost:
        headers.append("est_cost")
    if show_time:
        headers += ["first (UTC)", "last (UTC)"]
    ordered = sorted(groups, key=lambda k: -groups[k].total)
    tot = Agg()
    for key in ordered:
        tot.merge(groups[key])
    if top is not None:
        ordered = ordered[:top]
    rows = []
    for key in ordered:
        g = groups[key]
        row = [key] + g.row()
        if show_cost:
            row.append(_money(cur, g.est_cost))
        if show_time:
            row += [_dt(g.first_ts), _dt(g.last_ts)]
        rows.append(row)
    total_row = ["TOTAL"] + tot.row()
    if show_cost:
        total_row.append(_money(cur, tot.est_cost))
    if show_time:
        total_row += [_dt(tot.first_ts), _dt(tot.last_ts)]
    rows.append(total_row)
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    out = []
    for r in [headers] + rows:
        out.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))
    out.insert(1, "  ".join("-" * w for w in widths))
    table = "\n".join(out)
    if show_cost:
        table += f"\n\nCost estimate (per-model rates, {cur}/Mtok)."
        if tot.est_cost is not None and tot.naive_cost is not None:
            table += (
                f" Without cache discount: {_money(cur, tot.naive_cost)}"
                f"  |  saved by caching: {_money(cur, tot.naive_cost - tot.est_cost)}."
            )
        if tot.priced_calls < tot.calls:
            table += f"\n{tot.calls - tot.priced_calls} of {tot.calls} calls had no matching rate (excluded from cost)."
        table += "\n(estimate only — not billing-grade; verify rates against your plan)"
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
    p.add_argument("--show-time", action="store_true", help="add first/last activity datetime (UTC) columns")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="only count calls on/after this UTC date")
    p.add_argument("--until", metavar="YYYY-MM-DD", help="only count calls on/before this UTC date")
    p.add_argument("--rates", metavar="FILE", help="per-model JSON rates file with a \"models\" map keyed by model id (e.g. scripts/rates.copilot.json), plus an optional \"default\" block for unlisted models. Also via $COPILOT_TOKEN_RATES")
    p.add_argument("--rate-input", type=float, default=None, help="$/Mtok for fresh input tokens (default/fallback rate)")
    p.add_argument("--rate-output", type=float, default=None, help="$/Mtok for output tokens (default/fallback rate)")
    p.add_argument("--rate-cache-read", type=float, default=None, help="$/Mtok for cache-read input tokens (default/fallback rate)")
    p.add_argument("--rate-cache-write", type=float, default=None, help="$/Mtok for cache-creation input tokens (default/fallback rate)")
    p.add_argument("--currency", default=None, help="currency symbol for cost output (default: $)")
    p.add_argument(
        "--current-only",
        action="store_true",
        help="read only the active log; by default rotated/compressed siblings (PATH*, .gz) are included",
    )
    args = p.parse_args()

    try:
        pricing = Pricing.from_args(args)
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

    groups, span_found, m_in, m_out = analyze(paths, args.by, since=args.since, until=args.until, pricing=pricing)

    if args.json:
        show_cost = pricing.any()
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
                    **(
                        {
                            "first_ts": _iso(g.first_ts),
                            "last_ts": _iso(g.last_ts),
                        }
                        if args.show_time
                        else {}
                    ),
                    **(
                        {
                            "est_cost": round(g.est_cost, 6) if g.est_cost is not None else None,
                            "priced_calls": g.priced_calls,
                        }
                        if show_cost
                        else {}
                    ),
                }
                for k, g in groups.items()
            },
            "metric_token_usage": {"input": m_in, "output": m_out, "total": m_in + m_out},
        }
        if show_cost:
            result["pricing"] = {
                "currency": pricing.currency,
                "per_model": pricing.per_model(),
                "models_priced": sorted(pricing.models) if pricing.per_model() else None,
            }
            result["cost_disclaimer"] = "estimate only — not billing-grade"
        print(json.dumps(result, indent=2))
        return

    if span_found:
        print(fmt_table(groups, args.by, pricing=pricing, top=args.top, show_time=args.show_time))
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
