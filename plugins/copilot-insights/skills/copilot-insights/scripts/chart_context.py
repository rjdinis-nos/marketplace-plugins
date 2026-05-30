#!/usr/bin/env python3
"""chart_context.py — ASCII time-series chart of context window fill % per session.

Usage:
  python3 chart_context.py
  python3 chart_context.py ~/.copilot/logs/otel-signals.jsonl
  python3 chart_context.py --session fe612bf2
  python3 chart_context.py --width 100 --height 25 --warn 60
  python3 chart_context.py --no-color

Reads $COPILOT_OTEL_FILE_EXPORTER_PATH (or ~/.copilot/logs/otel-signals.jsonl).
Includes rotated/compressed siblings (*.gz) by default; use --current-only to skip them.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone


# ── helpers ───────────────────────────────────────────────────────────────────

def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")


# ── rendering ─────────────────────────────────────────────────────────────────

CHARS = "ABCDEFGHIJKLMNOP"
ANSI_COLORS = [
    "\033[94m", "\033[93m", "\033[92m", "\033[96m",
    "\033[95m", "\033[91m", "\033[33m", "\033[36m",
    "\033[34m", "\033[32m", "\033[35m", "\033[31m",
]
RESET = "\033[0m"
BOLD  = "\033[1m"
RED   = "\033[91m"
DIM   = "\033[2m"


def render(sessions, width, height, warn_pct, use_color):

    def c(code):
        return code if use_color else ""

    all_ts = [parse_ts(t["time"]) for _, turns in sessions for t in turns]
    if not all_ts:
        print("No turn data found.", file=sys.stderr)
        sys.exit(1)
    t_min, t_max = min(all_ts), max(all_ts)
    t_range = t_max - t_min or 1

    # grid[row][col] = (char_index, fill_pct)  — row 0 = top (100%)
    grid = [[(-1, 0.0)] * width for _ in range(height)]

    def fill_to_row(pct):
        row = height - 1 - int(pct / 100.0 * height)
        return max(0, min(height - 1, row))

    def ts_to_col(ts):
        col = int((ts - t_min) / t_range * (width - 1))
        return max(0, min(width - 1, col))

    for s_idx, (sid, turns) in enumerate(sessions):
        for t in turns:
            col = ts_to_col(parse_ts(t["time"]))
            row = fill_to_row(t["ctx_fill"] * 100)
            if grid[row][col][0] == -1:
                grid[row][col] = (s_idx, t["ctx_fill"] * 100)

    warn_row = fill_to_row(warn_pct)
    lines = []

    lines.append(c(BOLD) + "  Context window fill % over time" + c(RESET))
    lines.append("")

    for row in range(height):
        pct = round((height - row) / height * 100)
        if row == warn_row:
            label = f" {c(RED)}─{int(warn_pct):2d}%─╪{c(RESET)}"
        else:
            label = f" {pct:3d}% │"

        row_str = ""
        for col in range(width):
            s_idx, fill = grid[row][col]
            if s_idx == -1:
                row_str += (c(RED) + "─" + c(RESET)) if row == warn_row else (c(DIM) + "·" + c(RESET))
            else:
                ch = CHARS[s_idx % len(CHARS)]
                color = c(ANSI_COLORS[s_idx % len(ANSI_COLORS)])
                bold = c(BOLD) if fill >= warn_pct else ""
                row_str += f"{color}{bold}{ch}{c(RESET)}"

        lines.append(label + row_str)

    lines.append("     └" + "─" * width)

    # x-axis time labels
    t_mid = (t_min + t_max) / 2
    l1, l2, l3 = fmt_ts(t_min), fmt_ts(t_mid), fmt_ts(t_max)
    mid_pad   = max(1, width // 2 - len(l1) - len(l2) // 2)
    right_pad = max(1, width - len(l1) - len(l2) - len(l3) - mid_pad)
    lines.append(f"       {c(DIM)}{l1}{' ' * mid_pad}{l2}{' ' * right_pad}{l3}{c(RESET)}")
    lines.append("")

    # legend
    lines.append(c(BOLD) + "Legend:" + c(RESET))
    for s_idx, (sid, turns) in enumerate(sessions):
        ch = CHARS[s_idx % len(CHARS)]
        color = c(ANSI_COLORS[s_idx % len(ANSI_COLORS)])
        max_fill = max(t["ctx_fill"] for t in turns) * 100
        warn_flag = f"  {c(RED)}⚠ above {warn_pct}%{c(RESET)}" if max_fill >= warn_pct else ""
        t_start, t_end = turns[0]["time"], turns[-1]["time"]
        n = len(turns)
        lines.append(f"  {color}{ch}{c(RESET)}  {sid[:8]}  {t_start} → {t_end}  {n} turns  max {max_fill:.1f}%{warn_flag}")

    print("\n".join(lines))


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    default_path = os.environ.get("COPILOT_OTEL_FILE_EXPORTER_PATH") or os.path.expanduser(
        "~/.copilot/logs/otel-signals.jsonl"
    )
    script_dir = os.path.dirname(os.path.abspath(__file__))
    analyze = os.path.join(script_dir, "analyze_sessions.py")

    p = argparse.ArgumentParser(description="ASCII time-series chart of context window fill %.")
    p.add_argument("path", nargs="?", default=default_path,
                   help="OTel JSONL file (default: $COPILOT_OTEL_FILE_EXPORTER_PATH)")
    p.add_argument("--session", metavar="SESSION_ID",
                   help="filter to one session (prefix match)")
    p.add_argument("--width",  type=int, default=80,  help="chart width in chars (default: 80)")
    p.add_argument("--height", type=int, default=20,  help="chart height in rows (default: 20)")
    p.add_argument("--warn",   type=float, default=70.0, metavar="PCT",
                   help="warning threshold %% (default: 70)")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colours")
    p.add_argument("--current-only", action="store_true",
                   help="read only the active log; skip rotated/compressed siblings")
    args = p.parse_args()

    cmd = [sys.executable, analyze, args.path, "--report", "turns", "--json"]
    if args.session:
        cmd += ["--session", args.session]
    if args.current_only:
        cmd.append("--current-only")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        print(e.stderr.strip(), file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"Failed to parse turns JSON: {e}", file=sys.stderr)
        sys.exit(1)

    sessions = sorted(data.items(), key=lambda kv: kv[1][0]["time"] if kv[1] else "")
    use_color = not args.no_color and sys.stdout.isatty()
    render(sessions, args.width, args.height, args.warn, use_color)


if __name__ == "__main__":
    main()
