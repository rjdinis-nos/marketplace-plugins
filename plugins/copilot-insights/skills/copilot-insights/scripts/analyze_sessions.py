#!/usr/bin/env python3
"""analyze_sessions.py — Copilot CLI session health reports from OTel signals.

Usage:
  python3 analyze_sessions.py                        # context report by session (default)
  python3 analyze_sessions.py --report context
  python3 analyze_sessions.py --report growth        # context growth drivers
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


# ── growth data model ─────────────────────────────────────────────────────────

class GrowthAgg:
    """Aggregates per-turn context delta data for one group (session/model/all)."""

    def __init__(self):
        self.deltas = []
        self.tool_deltas = collections.defaultdict(list)
        self.init_deltas = collections.defaultdict(list)
        self.spikes = []   # (delta, initiator, model, cur, tools_str, session)
        self.first_ts = None
        self.last_ts = None

    def _observe_time(self, secs):
        if secs is None:
            return
        if self.first_ts is None or secs < self.first_ts:
            self.first_ts = secs
        if self.last_ts is None or secs > self.last_ts:
            self.last_ts = secs

    def add_turn(self, delta, initiator, model, cur, tools, ts_secs, session=""):
        self.deltas.append(delta)
        self.init_deltas[initiator].append(delta)
        for tool in (tools or ["(no tool)"]):
            self.tool_deltas[tool].append(delta)
        self.spikes.append((delta, initiator, model, cur, tools, session))
        self._observe_time(ts_secs)

    def merge(self, other):
        self.deltas.extend(other.deltas)
        for k, v in other.tool_deltas.items():
            self.tool_deltas[k].extend(v)
        for k, v in other.init_deltas.items():
            self.init_deltas[k].extend(v)
        self.spikes.extend(other.spikes)
        self._observe_time(other.first_ts)
        self._observe_time(other.last_ts)

    @property
    def turns(self):
        return len(self.deltas)

    @property
    def avg_delta(self):
        return int(sum(self.deltas) / len(self.deltas)) if self.deltas else 0

    @property
    def max_delta(self):
        return max(self.deltas) if self.deltas else 0

    @property
    def total_added(self):
        return sum(d for d in self.deltas if d > 0)


# ── growth parsing ────────────────────────────────────────────────────────────

def parse_turns(paths, since=None, until=None):
    """Return {session_id: [turn_dict sorted by start_ns]} from all chat spans."""

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

    raw = collections.defaultdict(list)
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
                model = attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model", "?")
                initiator = attrs.get("github.copilot.initiator", "?")
                span_ts = _secs(doc.get("startTime"))

                if not in_window(span_ts):
                    continue

                events = doc.get("events", [])
                usage = next(
                    (e for e in events if e.get("name") == "github.copilot.session.usage_info"),
                    None,
                )
                if not usage:
                    continue

                cur = int(usage["attributes"].get("github.copilot.current_tokens", 0))

                # tools called this turn: deduplicated from postToolUse hooks
                seen_tools, tools = set(), []
                for e in events:
                    if e.get("attributes", {}).get("github.copilot.hook.type") == "postToolUse":
                        raw_tools = e.get("attributes", {}).get("github.copilot.hook.tool_names", "[]")
                        try:
                            for t in json.loads(raw_tools):
                                if t not in seen_tools:
                                    seen_tools.add(t)
                                    tools.append(t)
                        except Exception:
                            pass

                raw[session].append({
                    "start_ns": doc.get("startTime", [0, 0]),
                    "cur": cur,
                    "initiator": initiator,
                    "tools": tools,
                    "model": model,
                    "ts": span_ts,
                    "session": session,
                })

    result = {}
    for session, turns in raw.items():
        turns.sort(key=lambda t: t["start_ns"] if isinstance(t["start_ns"], (int, float))
                   else (t["start_ns"][0] if isinstance(t["start_ns"], list) else 0))
        for i, t in enumerate(turns):
            t["delta"] = t["cur"] - (turns[i - 1]["cur"] if i > 0 else 0)
        result[session] = turns
    return result


def analyze_growth(turns_by_session, group_by):
    """Return {group_key: GrowthAgg}."""
    groups = collections.defaultdict(GrowthAgg)
    for session, turns in turns_by_session.items():
        for t in turns:
            if group_by == "session":
                key = session
            elif group_by == "model":
                key = t["model"]
            else:
                key = "all"
            groups[key].add_turn(
                delta=t["delta"],
                initiator=t["initiator"],
                model=t["model"],
                cur=t["cur"],
                tools=t["tools"],
                ts_secs=t["ts"],
                session=session,
            )
    return groups


# ── growth formatting ─────────────────────────────────────────────────────────

def _short(session_id, length=8):
    return session_id[:length] if len(session_id) > length else session_id


def _fmt_num(n):
    return f"{n:+,}" if n else "0"


def _table(headers, rows):
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    lines = []
    lines.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(headers)))
    lines.append("  ".join("-" * w for w in widths))
    for r in rows:
        lines.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))
    return "\n".join(lines)


def fmt_growth_table(groups, group_by, top_spikes=10, top=None):
    if not groups:
        return "No context growth data found."

    # ── 1. Summary table per group ────────────────────────────────────────────
    ordered = sorted(groups, key=lambda k: -groups[k].max_delta)
    tot = GrowthAgg()
    for key in ordered:
        tot.merge(groups[key])
    if top is not None:
        ordered = ordered[:top]

    summary_rows = []
    for key in ordered:
        g = groups[key]
        label = _short(key) if group_by == "session" else key
        summary_rows.append([label, g.turns, _fmt_num(g.avg_delta),
                              _fmt_num(g.max_delta), f"{g.total_added:,}"])
    summary_rows.append(["TOTAL", tot.turns, _fmt_num(tot.avg_delta),
                          _fmt_num(tot.max_delta), f"{tot.total_added:,}"])

    out = [f"Context growth report — by {group_by}\n"]
    out.append(_table([group_by, "turns", "avg_delta", "max_delta", "total_added"], summary_rows))

    # ── 2. Top spikes ─────────────────────────────────────────────────────────
    all_spikes = sorted(tot.spikes, key=lambda s: -s[0])[:top_spikes]
    if all_spikes:
        out.append(f"\n── Top {len(all_spikes)} context spikes ──")
        spike_rows = []
        for delta, initiator, model, cur, tools, session in all_spikes:
            tools_str = ",".join(tools) if tools else "-"
            sess_label = (_short(session) + "  ") if group_by != "session" else ""
            spike_rows.append([
                f"{sess_label}{initiator}",
                model[:22],
                f"{cur:,}",
                _fmt_num(delta),
                tools_str,
            ])
        out.append(_table(["initiator", "model", "cur_tok", "delta", "tools"], spike_rows))

    # ── 3. By initiator ───────────────────────────────────────────────────────
    out.append("\n── By initiator ──")
    init_rows = []
    for k, vals in sorted(tot.init_deltas.items()):
        avg = int(sum(vals) / len(vals))
        init_rows.append([k, _fmt_num(avg), _fmt_num(max(vals)), len(vals)])
    out.append(_table(["initiator", "avg_delta", "max_delta", "turns"], init_rows))

    # ── 4. By tool ────────────────────────────────────────────────────────────
    out.append("\n── By tool ──")
    tool_rows = []
    for k, vals in sorted(tot.tool_deltas.items(), key=lambda x: -(sum(x[1]) / len(x[1]))):
        avg = int(sum(vals) / len(vals))
        tool_rows.append([k[:22], _fmt_num(avg), _fmt_num(max(vals)), len(vals)])
    out.append(_table(["tool", "avg_delta", "max_delta", "turns"], tool_rows))

    return "\n".join(out)


def fmt_growth_json(groups):
    result = {}
    for key, g in groups.items():
        result[key] = {
            "turns": g.turns,
            "avg_delta": g.avg_delta,
            "max_delta": g.max_delta,
            "total_added": g.total_added,
            "by_initiator": {
                k: {"avg_delta": int(sum(v)/len(v)), "max_delta": max(v), "turns": len(v)}
                for k, v in g.init_deltas.items()
            },
            "by_tool": {
                k: {"avg_delta": int(sum(v)/len(v)), "max_delta": max(v), "turns": len(v)}
                for k, v in g.tool_deltas.items()
            },
        }
    return result




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
    p.add_argument("--report", choices=["context", "growth"], default="context",
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

    if args.report == "growth":
        turns_by_session = parse_turns(paths, since=args.since, until=args.until)
        if not turns_by_session:
            print("No context growth data found in the log.")
            sys.exit(0)
        groups = analyze_growth(turns_by_session, args.by)
        if args.json:
            print(json.dumps(fmt_growth_json(groups), indent=2))
        else:
            print(fmt_growth_table(groups, args.by, top_spikes=10, top=args.top))
            print(f"\n(source: chat span tool hooks  files: {len(paths)})")
        return

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
