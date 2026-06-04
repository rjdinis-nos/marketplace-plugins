#!/usr/bin/env python3
"""analyze_sessions.py — Copilot CLI session health reports from OTel signals.

Usage:
  python3 analyze_sessions.py                        # context report by session (default)
  python3 analyze_sessions.py --report context
  python3 analyze_sessions.py --report growth        # context growth drivers
  python3 analyze_sessions.py --report tools         # tool call latency (builtin + MCP)
  python3 analyze_sessions.py --report turns         # per-turn token & latency detail
  python3 analyze_sessions.py --report compactions   # context compaction events
  python3 analyze_sessions.py --by model
  python3 analyze_sessions.py --by all
  python3 analyze_sessions.py --session fe612bf2     # filter to one session (prefix ok)
  python3 analyze_sessions.py --current-session      # filter to the active copilot session
  python3 analyze_sessions.py --last 3               # restrict to the 3 most recent sessions
  python3 analyze_sessions.py --turn 1               # filter to specific turn number (growth/json only)
  python3 analyze_sessions.py --warn 60              # warn at 60% fill instead of 70%
  python3 analyze_sessions.py --top 5                # show top 5 groups
  python3 analyze_sessions.py --since 2026-05-29
  python3 analyze_sessions.py --json

Reads $COPILOT_OTEL_FILE_EXPORTER_PATH (or ~/.copilot/logs/otel-signals.jsonl).
Includes rotated/compressed siblings (*.gz) automatically.
"""

import argparse
import collections
import glob
import gzip
import json
import os
import re
import statistics
import sys
from datetime import datetime, timezone


# ── helpers ───────────────────────────────────────────────────────────────────

def _int0(value):
    """Coerce an OTel attribute to int, defaulting to 0 on None/non-numeric."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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


def resolve_paths(path):
    paths = [path] if os.path.exists(path) else []
    seen = set(paths)
    for sib in glob.glob(glob.escape(path) + "*"):
        if sib not in seen and os.path.isfile(sib):
            paths.append(sib)
            seen.add(sib)
    return paths


def _find_last_n_session_ids(paths, n, since=None, until=None):
    """Return a frozenset of the n most recent session IDs by first-turn timestamp."""
    turns = parse_turns(paths, since=since, until=until)
    session_first = {
        sid: turn_list[0]["ts"]
        for sid, turn_list in turns.items()
        if turn_list and turn_list[0].get("ts") is not None
    }
    sorted_sids = sorted(session_first, key=lambda s: session_first[s])
    return frozenset(sorted_sids[-n:])


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

def parse_turns(paths, since=None, until=None, session_filter=None, session_ids=None):
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
                if session_filter and not session.startswith(session_filter):
                    continue
                if session_ids is not None and session not in session_ids:
                    continue

                model = attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model", "?")
                initiator = attrs.get("github.copilot.initiator", "?")
                span_ts = _secs(doc.get("startTime"))

                if not in_window(span_ts):
                    continue

                # token usage fields from span attributes
                input_tokens = _int0(attrs.get("gen_ai.usage.input_tokens", 0))
                output_tokens = _int0(attrs.get("gen_ai.usage.output_tokens", 0))
                cache_rd = _int0(attrs.get("gen_ai.usage.cache_read.input_tokens", 0))
                cache_cr = _int0(attrs.get("gen_ai.usage.cache_creation.input_tokens", 0))
                reasoning = _int0(attrs.get("gen_ai.usage.reasoning.output_tokens", 0))
                ttfc = attrs.get("gen_ai.response.time_to_first_chunk")
                srv_ms = attrs.get("github.copilot.server_duration")
                turn_id = attrs.get("github.copilot.turn_id")

                events = doc.get("events", [])
                usage = next(
                    (e for e in events if e.get("name") == "github.copilot.session.usage_info"),
                    None,
                )
                if not usage:
                    continue

                cur = _int0(usage["attributes"].get("github.copilot.current_tokens", 0))
                token_limit = _int0(usage["attributes"].get("github.copilot.token_limit", 0))

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
                        except (json.JSONDecodeError, TypeError):
                            pass

                raw[session].append({
                    "start_ns": doc.get("startTime", [0, 0]),
                    "cur": cur,
                    "token_limit": token_limit,
                    "initiator": initiator,
                    "tools": tools,
                    "model": model,
                    "ts": span_ts,
                    "session": session,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_rd": cache_rd,
                    "cache_cr": cache_cr,
                    "reasoning": reasoning,
                    "ttfc": ttfc,
                    "srv_ms": srv_ms,
                    "turn_id": turn_id,
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
        label = k[:22]
        if _is_mcp_hash(k):
            label = _mcp_label(k)[:22]
        tool_rows.append([label, _fmt_num(avg), _fmt_num(max(vals)), len(vals)])
    out.append(_table(["tool", "avg_delta", "max_delta", "turns"], tool_rows))

    return "\n".join(out)


def fmt_growth_json(groups, turn_filter=None):
    result = {}
    for key, g in groups.items():
        by_turn = []
        for i, (delta, initiator, model, cur, tools, session) in enumerate(g.spikes):
            by_turn.append({
                "turn": i,
                "delta_tokens": delta,
                "current_context_tokens": cur,
                "initiator": initiator,
                "model": model,
                "tools": tools or [],
                "session": session,
            })
        # Filter by_turn if --turn is specified
        if turn_filter is not None:
           by_turn = [t for t in by_turn if t["turn"] == turn_filter]
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
           "by_turn": by_turn,
        }
    return result


# ── turns formatting ──────────────────────────────────────────────────────────

def fmt_turns_table(turns_by_session):
    all_turns = []
    for session in sorted(turns_by_session.keys()):
        for i, t in enumerate(turns_by_session[session]):
            all_turns.append((session, i, t))
    if not all_turns:
        return "No per-turn data found."

    multi_session = len(turns_by_session) > 1
    headers = ["#", "turn_id", "time", "model", "init",
               "input", "output", "fresh", "cache_rd", "cache_cr", "rsn",
               "ctx_tok", "fill%", "ttfc_ms", "srv_ms", "tools"]
    if multi_session:
        headers = ["session"] + headers

    rows = []
    for session, i, t in all_turns:
        fresh = t["input_tokens"] - t["cache_rd"] - t["cache_cr"]
        lim = t.get("token_limit") or 0
        fill = t["cur"] / lim if lim else 0
        ttfc_str = f"{int(t['ttfc'] * 1000):,}" if t.get("ttfc") is not None else "-"
        srv_str = f"{int(t['srv_ms']):,}" if t.get("srv_ms") is not None else "-"
        tools_str = ",".join(t["tools"]) if t["tools"] else "-"
        rsn_str = f"{t['reasoning']:,}" if t.get("reasoning") else "-"
        row = [
            i,
            t.get("turn_id", "?"),
            _fmt_dt(t["ts"]),
            t["model"][:20],
            t["initiator"][:5],
            f"{t['input_tokens']:,}",
            f"{t['output_tokens']:,}",
            f"{fresh:,}",
            f"{t['cache_rd']:,}",
            f"{t['cache_cr']:,}",
            rsn_str,
            f"{t['cur']:,}",
            _pct(fill),
            ttfc_str,
            srv_str,
            tools_str,
        ]
        if multi_session:
            row = [_short(session)] + row
        rows.append(row)

    out = ["Per-turn detail\n"]
    out.append(_table(headers, rows))
    return "\n".join(out)


def fmt_turns_json(turns_by_session, turn_filter=None):
    result = {}
    for session, turns in turns_by_session.items():
        result[session] = []
        for i, t in enumerate(turns):
            # Filter by turn index if --turn is specified
            if turn_filter is not None and i != turn_filter:
                continue
            fresh = t["input_tokens"] - t["cache_rd"] - t["cache_cr"]
            lim = t.get("token_limit") or 0
            fill = t["cur"] / lim if lim else 0
            result[session].append({
                "turn_index": i,
                "turn_id": t.get("turn_id"),
                "time": _fmt_dt(t["ts"]),
                "model": t["model"],
                "initiator": t["initiator"],
                "input_tokens": t["input_tokens"],
                "output_tokens": t["output_tokens"],
                "fresh_input_tokens": fresh,
                "cache_read_tokens": t["cache_rd"],
                "cache_creation_tokens": t["cache_cr"],
                "reasoning_tokens": t["reasoning"],
                "ctx_tokens": t["cur"],
                "ctx_fill": round(fill, 4),
                "ctx_delta": t["delta"],
                "ttfc_ms": int(t["ttfc"] * 1000) if t.get("ttfc") is not None else None,
                "server_duration_ms": int(t["srv_ms"]) if t.get("srv_ms") is not None else None,
                "tools": t["tools"],
            })
    return result


# ── tool latency data model ───────────────────────────────────────────────────

_MCP_TOOL_RE = re.compile(r"^([0-9a-f]{20,})/(.+)$")


def _is_mcp_hash(name):
    """Detect MCP tool entries: hook uses '<server_hash>/<tool_name>' format."""
    return bool(_MCP_TOOL_RE.match(name))


def _mcp_label(name):
    """Return a short readable label for an MCP tool entry."""
    m = _MCP_TOOL_RE.match(name)
    if m:
        return f"{m.group(2)} [mcp]"
    return name


class ToolAgg:
    """Aggregates execute_tool span data for one tool name."""

    def __init__(self):
        self.durations_ms = []
        self.errors = 0
        self.is_mcp = False
        self.mcp_tool_name = None
        self.first_ts = None
        self.last_ts = None

    def _observe_time(self, secs):
        if secs is None:
            return
        if self.first_ts is None or secs < self.first_ts:
            self.first_ts = secs
        if self.last_ts is None or secs > self.last_ts:
            self.last_ts = secs

    def add(self, duration_ms, is_error, is_mcp, mcp_tool_name, ts_secs):
        self.durations_ms.append(duration_ms)
        if is_error:
            self.errors += 1
        if is_mcp:
            self.is_mcp = True
        if mcp_tool_name:
            self.mcp_tool_name = mcp_tool_name
        self._observe_time(ts_secs)

    @property
    def calls(self):
        return len(self.durations_ms)

    @property
    def avg_ms(self):
        return int(sum(self.durations_ms) / len(self.durations_ms)) if self.durations_ms else 0

    def percentile(self, p):
        if not self.durations_ms:
            return 0
        s = sorted(self.durations_ms)
        return s[min(int(len(s) * p / 100), len(s) - 1)]

    @property
    def max_ms(self):
        return max(self.durations_ms) if self.durations_ms else 0


# ── tool latency parsing ──────────────────────────────────────────────────────

def parse_tools(paths, since=None, until=None, session_filter=None, session_ids=None):
    """Return {tool_name: ToolAgg} from execute_tool spans."""

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

    tools = collections.defaultdict(ToolAgg)
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
                if doc.get("type") != "span":
                    continue
                name = doc.get("name", "")
                if not name.startswith("execute_tool "):
                    continue

                attrs = doc.get("attributes", {})
                sess = attrs.get("gen_ai.conversation.id", "")
                if session_filter and not sess.startswith(session_filter):
                    continue
                if session_ids is not None and sess not in session_ids:
                    continue

                tool_name = attrs.get("gen_ai.tool.name", name[len("execute_tool "):])
                mcp_hash = attrs.get("github.copilot.tool.parameters.mcp_server_name_hash")
                mcp_tool = attrs.get("github.copilot.tool.parameters.mcp_tool_name")
                is_mcp = bool(mcp_hash or mcp_tool)

                start = _secs(doc.get("startTime"))
                end = _secs(doc.get("endTime"))
                if not in_window(start):
                    continue
                duration_ms = int((end - start) * 1000) if (start and end) else 0
                is_error = doc.get("status", {}).get("code", 0) != 0

                tools[tool_name].add(duration_ms, is_error, is_mcp, mcp_tool, start)

    return tools


# ── tool latency formatting ───────────────────────────────────────────────────

def fmt_tools_table(tools, top=None):
    if not tools:
        return "No tool execution data found."

    ordered = sorted(tools, key=lambda k: -tools[k].calls)
    if top is not None:
        ordered = ordered[:top]

    rows = []
    for name in ordered:
        t = tools[name]
        label = (t.mcp_tool_name or name)[:34]
        kind = "MCP" if t.is_mcp else "builtin"
        rows.append([
            label,
            kind,
            t.calls,
            f"{t.avg_ms:,}",
            f"{t.percentile(95):,}",
            f"{t.max_ms:,}",
            t.errors if t.errors else "-",
        ])

    out = ["Tool execution latency report\n"]
    out.append(_table(["tool", "type", "calls", "avg_ms", "p95_ms", "max_ms", "errors"], rows))

    mcp_count = sum(1 for t in tools.values() if t.is_mcp)
    if mcp_count:
        out.append(f"\n  {mcp_count} MCP tool(s) detected — latency includes network round-trip to MCP server.")
    return "\n".join(out)


def fmt_tools_json(tools):
    return {
        name: {
            "type": "mcp" if t.is_mcp else "builtin",
            "mcp_tool_name": t.mcp_tool_name,
            "calls": t.calls,
            "avg_ms": t.avg_ms,
            "p95_ms": t.percentile(95),
            "max_ms": t.max_ms,
            "errors": t.errors,
            "first_ts": _fmt_dt(t.first_ts),
            "last_ts": _fmt_dt(t.last_ts),
        }
        for name, t in tools.items()
    }




def analyze_context(paths, group_by, since=None, until=None, session_filter=None, session_ids=None):
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
                if session_filter and not session.startswith(session_filter):
                    continue
                if session_ids is not None and session not in session_ids:
                    continue

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
                    if lim is not None and cur is not None and _int0(lim) > 0:
                        groups[key].add(_int0(cur), _int0(lim), ev_ts)

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


# ── compaction detection ──────────────────────────────────────────────────────

# Any drop > MIN_DROP_TOKENS between consecutive turns is flagged as a compaction.
# ctx_tokens normally only grows, so any meaningful drop is a genuine event.
_MIN_COMPACTION_DROP = 1_000


def detect_compactions(turns_by_session):
    """Return {session_id: [compaction_dict]} for all sessions."""
    result = {}
    for session, turns in turns_by_session.items():
        events = []
        for i, t in enumerate(turns):
            if i == 0:
                continue
            prev = turns[i - 1]
            drop = prev["cur"] - t["cur"]
            if drop < _MIN_COMPACTION_DROP:
                continue
            lim = t.get("token_limit") or prev.get("token_limit") or 0
            events.append({
                "turn_index":   i,
                "turn_id":      t.get("turn_id"),
                "time":         _fmt_dt(t["ts"]) if t["ts"] else "?",
                "before_tok":   prev["cur"],
                "after_tok":    t["cur"],
                "drop_tok":     drop,
                "before_fill":  prev["cur"] / lim if lim else 0.0,
                "after_fill":   t["cur"] / lim if lim else 0.0,
                "recovered_pct": drop / prev["cur"] if prev["cur"] else 0.0,
                "tools":        t["tools"],
                "model":        t["model"],
                "session":      session,
            })
        if events:
            result[session] = events
    return result


def fmt_compactions_table(compactions_by_session):
    if not compactions_by_session:
        return "No compaction events found. (A compaction is a ctx drop > 1,000 tokens between turns.)"

    # ── 1. Event list ─────────────────────────────────────────────────────────
    all_events = [
        (sid, ev)
        for sid, evs in sorted(compactions_by_session.items())
        for ev in evs
    ]
    event_rows = []
    for sid, ev in all_events:
        tools_str = ",".join(ev["tools"]) if ev["tools"] else "-"
        event_rows.append([
            _short(sid),
            ev["turn_index"],
            ev["time"],
            _pct(ev["before_fill"]),
            _pct(ev["after_fill"]),
            f"-{ev['drop_tok']:,}",
            f"{ev['recovered_pct']:.0%}",
            tools_str[:30],
        ])
    headers = ["session", "turn#", "time", "before", "after", "drop_tok", "recov%", "tools"]
    out = ["Compaction events\n"]
    out.append(_table(headers, event_rows))

    # ── 2. Summary per session ────────────────────────────────────────────────
    out.append("\n── Summary ──")
    summary_rows = []
    total_events, total_recovered = 0, 0
    for sid, evs in sorted(compactions_by_session.items()):
        n = len(evs)
        recovered = sum(e["drop_tok"] for e in evs)
        max_drop = max(e["drop_tok"] for e in evs)
        total_events += n
        total_recovered += recovered
        summary_rows.append([_short(sid), n, f"{recovered:,}", f"{max_drop:,}"])
    summary_rows.append(["TOTAL", total_events, f"{total_recovered:,}", ""])
    out.append(_table(["session", "compactions", "total_recovered_tok", "max_single_drop"], summary_rows))

    out.append(
        "\n  A compaction is triggered automatically when context pressure is high."
        "\n  The model summarises old turns, freeing space — but earlier detail is lost."
    )
    return "\n".join(out)


def fmt_compactions_json(compactions_by_session):
    result = {}
    for sid, evs in compactions_by_session.items():
        result[sid] = [
            {
                "turn_index":    ev["turn_index"],
                "turn_id":       ev["turn_id"],
                "time":          ev["time"],
                "before_tokens": ev["before_tok"],
                "after_tokens":  ev["after_tok"],
                "drop_tokens":   ev["drop_tok"],
                "before_fill":   round(ev["before_fill"], 4),
                "after_fill":    round(ev["after_fill"], 4),
                "recovered_pct": round(ev["recovered_pct"], 4),
                "tools":         ev["tools"],
                "model":         ev["model"],
            }
            for ev in evs
        ]
    return result


# ── context breakdown ─────────────────────────────────────────────────────────

def _parse_tool_defs_by_session(paths, session_filter=None, session_ids=None):
    """Return {session_id: tool_defs_json_str} from the first chat span per session."""
    result = {}
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
                if session_filter and not session.startswith(session_filter):
                    continue
                if session_ids is not None and session not in session_ids:
                    continue
                if session not in result:
                    tool_defs = attrs.get("gen_ai.tool.definitions", "")
                    if tool_defs:
                        result[session] = tool_defs
    return result


def _estimate_tool_defs(tool_defs_str):
    """Return (token_est, tool_count) for a tool_definitions JSON string."""
    token_est = len(tool_defs_str) // 4
    try:
        tool_count = len(json.loads(tool_defs_str))
    except (json.JSONDecodeError, TypeError):
        tool_count = 0
    return token_est, tool_count


def analyze_breakdown(turns_by_session, tool_defs_by_session):
    """Return {session_id: breakdown_dict} with inferred context composition.

    Methodology (no message content required):
    - sys_instructions : cache_rd at turn 0  — content already cached before this session
                         (system prompt + agent instructions pre-loaded from prior session)
    - skill_content    : sum of cache_cr for turns where the 'skill' tool ran
                         (newly cached content = skill payload injected this session)
    - tool_definitions : len(tool_defs_json) // 4  — character-based token estimate
    - conversation     : sum of ctx_delta for non-skill turns after turn 0
                         (organic growth: user messages + model replies + tool outputs)
    """
    result = {}
    for session, turns in turns_by_session.items():
        if not turns:
            continue

        first_turn = turns[0]
        latest_turn = turns[-1]
        latest_ctx = latest_turn["cur"]
        token_limit = latest_turn.get("token_limit", 0)

        # ── tool definitions estimate ─────────────────────────────────────────
        tool_defs_str = tool_defs_by_session.get(session, "")
        tool_defs_est, tool_count = _estimate_tool_defs(tool_defs_str)

        # ── skill content: cache_cr on skill-tool turns ───────────────────────
        skill_turns = [t for t in turns if "skill" in t.get("tools", [])]
        skill_tokens = sum(t["cache_cr"] for t in skill_turns)

        # ── system + agent instructions: cache_rd at turn 0 ──────────────────
        # Prefer cache_rd (prompt already cached from a prior session).
        # Fall back to cache_cr at turn 0 when cache_rd is 0: this means the
        # system prompt was written to cache for the first time this session.
        # Note: in this case skill content loaded at startup is blended in and
        # cannot be separated without message-content capture.
        sys_tokens = first_turn.get("cache_rd", 0)
        sys_first_write = False
        if sys_tokens == 0 and first_turn.get("cache_cr", 0) > 0:
            sys_tokens = first_turn.get("cache_cr", 0)
            sys_first_write = True

        # ── conversation history: ctx_delta for non-skill turns after turn 0 ──
        conv_tokens = sum(
            t["delta"] for i, t in enumerate(turns)
            if i > 0 and "skill" not in t.get("tools", [])
        )

        result[session] = {
            "session": session,
            "model": latest_turn["model"],
            "turns": len(turns),
            "latest_ctx_tokens": latest_ctx,
            "token_limit": token_limit,
            "ctx_fill": round(latest_ctx / token_limit, 4) if token_limit else None,
            "skill_turns": len(skill_turns),
            "skill_first_write": sys_first_write,
            "tool_count": tool_count,
            "components": {
                "sys_instructions": {
                    "tokens": sys_tokens,
                    "pct": round(sys_tokens / latest_ctx, 4) if latest_ctx else 0.0,
                    "source": (
                        "cache_cr at turn 0 (first write this session)"
                        if sys_first_write
                        else "cache_rd at turn 0"
                    ),
                    "estimated": False,
                },
                "skill_content": {
                    "tokens": skill_tokens,
                    "pct": round(skill_tokens / latest_ctx, 4) if latest_ctx else 0.0,
                    "source": f"cache_cr on {len(skill_turns)} skill turn(s)",
                    "estimated": False,
                },
                "tool_definitions": {
                    "tokens": tool_defs_est,
                    "pct": round(tool_defs_est / latest_ctx, 4) if latest_ctx else 0.0,
                    "source": f"tool_defs JSON ÷ 4 ({tool_count} tools) ~estimated",
                    "estimated": True,
                },
                "conversation_history": {
                    "tokens": conv_tokens,
                    "pct": round(conv_tokens / latest_ctx, 4) if latest_ctx else 0.0,
                    "source": "ctx_delta sum for non-skill turns after turn 0",
                    "estimated": False,
                },
            },
        }
    return result


def fmt_breakdown_table(breakdown_by_session, top=None):
    if not breakdown_by_session:
        return "No breakdown data found."

    COMPONENT_ORDER = ["sys_instructions", "skill_content", "tool_definitions", "conversation_history"]
    LABELS = {
        "sys_instructions":   "System + instructions",
        "skill_content":      "Skills loaded",
        "tool_definitions":   "Tool definitions ~est",
        "conversation_history": "Conversation history",
    }

    ordered = sorted(breakdown_by_session, key=lambda k: -(breakdown_by_session[k]["latest_ctx_tokens"]))
    if top is not None:
        ordered = ordered[:top]

    out = []
    multi = len(ordered) > 1

    for session in ordered:
        bd = breakdown_by_session[session]
        ctx = bd["latest_ctx_tokens"]
        lim = bd["token_limit"]
        fill_str = _pct(bd["ctx_fill"]) if bd["ctx_fill"] is not None else "?"
        limit_str = f"{lim // 1000}k" if lim else "?"

        header = f"Session {_short(session)}  ({bd['model']}, {bd['turns']} turns)"
        if multi:
            out.append(f"\n{'─' * len(header)}")
        out.append(header)
        out.append(f"Context: {ctx:,} / {limit_str} tokens  ({fill_str} fill)\n")

        rows = []
        total_shown = 0
        for key in COMPONENT_ORDER:
            comp = bd["components"][key]
            bar_width = int(comp["pct"] * 30)
            bar = "█" * bar_width + "░" * (30 - bar_width)
            rows.append([
                LABELS[key],
                f"{comp['tokens']:>7,}",
                f"{comp['pct']:.1%}",
                bar,
                comp["source"],
            ])
            total_shown += comp["tokens"]

        rows.append(["─" * 22, "─" * 7, "─" * 5, "─" * 30, ""])
        rows.append(["Components total", f"{total_shown:>7,}", "", "", ""])

        headers = ["Component", " Tokens", "  %", "Fill bar (30 cols)", "Source"]
        widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
        out.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(headers)))
        out.append("  ".join("─" * w for w in widths))
        for r in rows:
            out.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)))

        if bd["skill_turns"] == 0:
            if bd.get("skill_first_write"):
                out.append(
                    "\n  ⚠️  System prompt was written to cache on turn 0 (first-session write)."
                    "\n      Skills loaded at startup are blended into 'System + instructions'"
                    "\n      and cannot be separated without message-content capture."
                )
            else:
                out.append("\n  ℹ️  No skill tool detected — skill_content will be 0.")
        out.append(
            "\n  ~est = character-based estimate (len ÷ 4); all other values are exact OTel measurements."
        )

    return "\n".join(out)


def fmt_breakdown_json(breakdown_by_session):
    return breakdown_by_session


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    default_path = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH") or os.path.expanduser(
        "~/.copilot/logs/otel-signals.jsonl"
    )
    p = argparse.ArgumentParser(description="Copilot CLI session health reports from OTel signals.")
    p.add_argument("path", nargs="?", default=default_path,
                   help="OTel JSONL file (default: $COPILOT_OTEL_FILE_EXPORTER_PATH)")
    p.add_argument("--report", choices=["context", "growth", "tools", "turns", "compactions", "breakdown"],
                   default="context", help="report type (default: context)")
    p.add_argument("--by", choices=["session", "model", "all"], default="session",
                   help="grouping dimension (default: session)")
    p.add_argument("--session", metavar="SESSION_ID",
                   help="filter to a single session (prefix match, e.g. fe612bf2)")
    p.add_argument("--current-session", action="store_true",
                   help="filter to the active Copilot session (reads COPILOT_AGENT_SESSION_ID)")
    p.add_argument("--last", type=int, default=None, metavar="N",
                   help="restrict to the N most recent sessions by first activity")
    p.add_argument("--turn", type=int, metavar="N",
                   help="filter by_turn array to specific turn number (--report growth --json only)")
    p.add_argument("--warn", type=float, default=70.0, metavar="PCT",
                   help="warning threshold %% for context fill (default: 70)")
    p.add_argument("--top", type=int, default=None, metavar="N",
                   help="show only top N groups by max fill")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="only include data on/after this UTC date")
    p.add_argument("--until", metavar="YYYY-MM-DD", help="only include data on/before this UTC date")
    p.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = p.parse_args()

    # Validate mutually exclusive session selectors
    if sum([bool(args.session), args.current_session, args.last is not None]) > 1:
        sys.exit("--session, --current-session, and --last are mutually exclusive")

    if args.current_session:
        env_sid = os.environ.get("COPILOT_AGENT_SESSION_ID")
        if not env_sid:
            sys.exit(
                "--current-session requires COPILOT_AGENT_SESSION_ID to be set.\n"
                "This is set automatically when running inside a copilot session."
            )
        args.session = env_sid

    paths = resolve_paths(args.path)
    if not paths:
        sys.exit(
            f"OTel file not found: {args.path}\n"
            "Enable the exporter first:\n"
            '  export COPILOT_OTEL_FILE_EXPORTER_PATH="$HOME/.copilot/logs/otel-signals.jsonl"\n'
            "then start a new copilot session and retry."
        )

    # Resolve --last N into an explicit set of session IDs
    session_ids = None
    if args.last is not None:
        session_ids = _find_last_n_session_ids(paths, args.last, since=args.since, until=args.until)

    warn_threshold = args.warn / 100.0

    if args.report == "breakdown":
        turns_by_session = parse_turns(paths, since=args.since, until=args.until,
                                       session_filter=args.session, session_ids=session_ids)
        if not turns_by_session:
            print("No breakdown data found in the log.")
            sys.exit(0)
        tool_defs_by_session = _parse_tool_defs_by_session(
            paths, session_filter=args.session, session_ids=session_ids)
        breakdown = analyze_breakdown(turns_by_session, tool_defs_by_session)
        if args.json:
            print(json.dumps(fmt_breakdown_json(breakdown), indent=2))
        else:
            print(fmt_breakdown_table(breakdown, top=args.top))
            print(f"\n(source: chat spans  files: {len(paths)})")
        return

    if args.report == "compactions":
        turns_by_session = parse_turns(paths, since=args.since, until=args.until,
                                       session_filter=args.session, session_ids=session_ids)
        compactions = detect_compactions(turns_by_session)
        if args.json:
            print(json.dumps(fmt_compactions_json(compactions), indent=2))
        else:
            print(fmt_compactions_table(compactions))
            print(f"\n(source: chat spans  files: {len(paths)})")
        return

    if args.report == "turns":
        turns_by_session = parse_turns(paths, since=args.since, until=args.until,
                                       session_filter=args.session, session_ids=session_ids)
        if not turns_by_session:
            print("No per-turn data found in the log.")
            sys.exit(0)
        if args.json:
            print(json.dumps(fmt_turns_json(turns_by_session, turn_filter=args.turn), indent=2))
        else:
            print(fmt_turns_table(turns_by_session))
            print(f"\n(source: chat spans  files: {len(paths)})")
        return

    if args.report == "growth":
        turns_by_session = parse_turns(paths, since=args.since, until=args.until,
                                       session_filter=args.session, session_ids=session_ids)
        if not turns_by_session:
            print("No context growth data found in the log.")
            sys.exit(0)
        groups = analyze_growth(turns_by_session, args.by)
        if args.json:
            print(json.dumps(fmt_growth_json(groups, turn_filter=args.turn), indent=2))
        else:
            print(fmt_growth_table(groups, args.by, top_spikes=10, top=args.top))
            print(f"\n(source: chat span tool hooks  files: {len(paths)})")
        return

    if args.report == "tools":
        tools = parse_tools(paths, since=args.since, until=args.until,
                            session_filter=args.session, session_ids=session_ids)
        if not tools:
            print("No tool execution data found in the log.")
            sys.exit(0)
        if args.json:
            print(json.dumps(fmt_tools_json(tools), indent=2))
        else:
            print(fmt_tools_table(tools, top=args.top))
            print(f"\n(source: execute_tool spans  files: {len(paths)})")
        return

    groups = analyze_context(paths, args.by, since=args.since, until=args.until,
                             session_filter=args.session, session_ids=session_ids)

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
