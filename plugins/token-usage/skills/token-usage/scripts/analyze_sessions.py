#!/usr/bin/env python3
"""analyze_sessions.py — Copilot CLI session health reports from OTel signals.

Usage:
  python3 analyze_sessions.py                        # context report by session (default)
  python3 analyze_sessions.py --report context
  python3 analyze_sessions.py --by model
  python3 analyze_sessions.py --by all
  python3 analyze_sessions.py --warn 60              # warn at 60% fill instead of 70%
  python3 analyze_sessions.py --top 5                # show top 5 sessions by max fill
  python3 analyze_sessions.py --since 2026-05-29
  python3 analyze_sessions.py --json

Reads $COPILOT_OTEL_FILE_EXPORTER_PATH (or ~/.copilot/logs/otel-signals.jsonl).
Includes rotated/compressed siblings (*.gz) by default; use --current-only to skip them.
"""

import argparse
import collections
import glob
import gzip
import json
import os
import statistics
import sys
from datetime import datetime, timezone


# ── helpers ───────────────────────────────────────────────────────────────────

def _secs(ts):
    """Convert OTel [sec, nano] timestamp pair to float seconds."""
    if isinstance(ts, list) and len(ts) == 2:
        return ts[0] + ts[1] / 1e9
    return None


def _day(ts_secs):
    if ts_secs is None:
        return "unknown"
    return datetime.fromtimestamp(ts_secs, tz=timezone.utc).strftime("%Y-%m-%d")


def _fmt_dt(ts_secs):
    if ts_secs is None:
        return "unknown"
    return datetime.fromtimestamp(ts_secs, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _pct(v):
    return f"{v:.1%}" if v is not None else "?"


def _open_text(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="ignore")
    return open(path, "r", encoding="utf-8", errors="ignore")


def resolve_paths(path, include_rotated):
    paths = [path] if os.path.exists(path) else []
    if include_rotated:
        seen = set(paths)
        for sib in glob.glob(glob.escape(path) + "*"):
            if sib not in seen and os.path.isfile(sib):
                paths.append(sib)
                seen.add(sib)
    return paths


# ── data model ────────────────────────────────────────────────────────────────

class ContextAgg:
    """Aggregates context window fill samples for one group (session/model/all)."""

    def __init__(self):
        self.fills = []  # list of (current_tokens, token_limit, fill_ratio)
        self.first_ts = None
        self.last_ts = None

    def _observe_time(self, secs):
        if secs is None:
            return
        if self.first_ts is None or secs < self.first_ts:
            self.first_ts = secs
        if self.last_ts is None or secs > self.last_ts:
            self.last_ts = secs

    def add(self, current_tokens, token_limit, ts_secs):
        ratio = current_tokens / token_limit if token_limit else 0.0
        self.fills.append((current_tokens, token_limit, ratio))
        self._observe_time(ts_secs)

    def merge(self, other):
        self.fills.extend(other.fills)
        self._observe_time(other.first_ts)
        self._observe_time(other.last_ts)

    @property
    def turns(self):
        return len(self.fills)

    @property
    def median_fill(self):
        return statistics.median(r for _, _, r in self.fills) if self.fills else None

    def percentile(self, p):
        if not self.fills:
            return None
        s = sorted(r for _, _, r in self.fills)
        return s[min(int(len(s) * p / 100), len(s) - 1)]

    @property
    def max_fill(self):
        return max(r for _, _, r in self.fills) if self.fills else None

    @property
    def max_limit(self):
        return max(lim for _, lim, _ in self.fills) if self.fills else None

    def turns_above(self, threshold):
        return sum(1 for _, _, r in self.fills if r > threshold)


# ── parsing ───────────────────────────────────────────────────────────────────

def analyze_context(paths, group_by, since=None, until=None):
    groups = collections.defaultdict(ContextAgg)

    def in_window(ts_secs):
        if since is None and until is None:
            return True
        d = _day(ts_secs)
        if d == "unknown":
            return False
        if since and d < since:
            return False
        if until and d > until:
            return False
        return True

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
                if doc.get("type") != "span" or not doc.get("name", "").startswith("chat "):
                    continue

                attrs = doc.get("attributes", {})
                session = attrs.get("gen_ai.conversation.id", "unknown")
                model = attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model", "unknown")
                span_ts = _secs(doc.get("startTime"))

                if not in_window(span_ts):
                    continue

                key = {"session": session, "model": model}.get(group_by, "all")

                for ev in doc.get("events", []):
                    if ev.get("name") != "github.copilot.session.usage_info":
                        continue
                    ea = ev.get("attributes", {})
                    lim = ea.get("github.copilot.token_limit")
                    cur = ea.get("github.copilot.current_tokens")
                    ev_ts = _secs(ev.get("time")) or span_ts
                    if lim and cur:
                        groups[key].add(int(cur), int(lim), ev_ts)

    return groups


# ── formatting ────────────────────────────────────────────────────────────────

def fmt_context_table(groups, group_by, warn_threshold=0.70, top=None):
    if not groups:
        return "No context window data found."

    # Sort by max_fill descending (most at-risk sessions first)
    ordered = sorted(groups, key=lambda k: -(groups[k].max_fill or 0))
    tot = ContextAgg()
    for key in ordered:
        tot.merge(groups[key])
    if top is not None:
        ordered = ordered[:top]

    warn_col = f"turns_>{int(warn_threshold * 100)}%"
    headers = [group_by, "turns", "median_fill", "p95_fill", "max_fill", warn_col, "ctx_limit"]
    rows = []
    for key in ordered:
        g = groups[key]
        w = g.turns_above(warn_threshold)
        rows.append([
            key,
            g.turns,
            _pct(g.median_fill),
            _pct(g.percentile(95)),
            _pct(g.max_fill),
            f"{w} ⚠️" if w > 0 else "0",
            f"{g.max_limit // 1000}k" if g.max_limit else "?",
        ])

    w_tot = tot.turns_above(warn_threshold)
    rows.append([
        "TOTAL",
        tot.turns,
        _pct(tot.median_fill),
        _pct(tot.percentile(95)),
        _pct(tot.max_fill),
        f"{w_tot} ⚠️" if w_tot > 0 else "0",
        f"{tot.max_limit // 1000}k" if tot.max_limit else "?",
    ])

    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    out = []
    for r in [headers] + rows:
        out.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))
    out.insert(1, "  ".join("-" * w for w in widths))
    table = "\n".join(out)

    if w_tot > 0:
        table += (
            f"\n\n⚠️  {w_tot} turn(s) exceeded {int(warn_threshold * 100)}% context fill."
            "\n   At high fill the model silently drops oldest conversation turns."
            "\n   Recommendation: start a new session when fill approaches this threshold."
        )
    return table


def fmt_context_json(groups, warn_threshold=0.70):
    return {
        key: {
            "turns": g.turns,
            "median_fill": round(g.median_fill, 4) if g.median_fill is not None else None,
            "p95_fill": round(g.percentile(95), 4) if g.percentile(95) is not None else None,
            "max_fill": round(g.max_fill, 4) if g.max_fill is not None else None,
            "turns_above_threshold": g.turns_above(warn_threshold),
            "warn_threshold": warn_threshold,
            "max_limit_tokens": g.max_limit,
            "first_ts": _fmt_dt(g.first_ts),
            "last_ts": _fmt_dt(g.last_ts),
        }
        for key, g in groups.items()
    }


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    default_path = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH") or os.path.expanduser(
        "~/.copilot/logs/otel-signals.jsonl"
    )
    p = argparse.ArgumentParser(description="Copilot CLI session health reports from OTel signals.")
    p.add_argument("path", nargs="?", default=default_path,
                   help="OTel JSONL file (default: $COPILOT_OTEL_FILE_EXPORTER_PATH)")
    p.add_argument("--report", choices=["context"], default="context",
                   help="report type (default: context)")
    p.add_argument("--by", choices=["session", "model", "all"], default="session",
                   help="grouping dimension (default: session)")
    p.add_argument("--warn", type=float, default=70.0, metavar="PCT",
                   help="warning threshold %% for context fill (default: 70)")
    p.add_argument("--top", type=int, default=None, metavar="N",
                   help="show only top N groups by max fill")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="only include data on/after this UTC date")
    p.add_argument("--until", metavar="YYYY-MM-DD", help="only include data on/before this UTC date")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    p.add_argument("--current-only", action="store_true",
                   help="read only the active log; skip rotated/compressed siblings")
    args = p.parse_args()

    paths = resolve_paths(args.path, include_rotated=not args.current_only)
    if not paths:
        sys.exit(
            f"OTel file not found: {args.path}\n"
            "Enable the exporter first:\n"
            '  export COPILOT_OTEL_FILE_EXPORTER_PATH="$HOME/.copilot/logs/otel-signals.jsonl"\n'
            "then start a new copilot session and retry."
        )

    warn_threshold = args.warn / 100.0
    groups = analyze_context(paths, args.by, since=args.since, until=args.until)

    if not groups:
        print("No context window data found in the log.")
        print("Context data comes from 'chat' spans containing session.usage_info events.")
        sys.exit(0)

    if args.json:
        print(json.dumps(fmt_context_json(groups, warn_threshold), indent=2))
    else:
        print(fmt_context_table(groups, args.by, warn_threshold=warn_threshold, top=args.top))
        print(f"\n(source: github.copilot.session.usage_info events  files: {len(paths)})")


if __name__ == "__main__":
    main()
