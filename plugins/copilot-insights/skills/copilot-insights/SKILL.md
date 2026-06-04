---
name: copilot-insights
description: Copilot CLI observability via built-in OpenTelemetry — token usage, cost estimates, and session health (context window pressure). Use when the user wants to measure, track, audit, or report Copilot CLI token usage, LLM cost, context window fill, or session health metrics.
---

# Copilot CLI Insights — Quick Reference

Scripts: `$SKILL_DIR/scripts/` (`$SKILL_DIR` = directory containing this file).
Default log: `$COPILOT_OTEL_FILE_EXPORTER_PATH` or `~/.copilot/logs/otel-signals.jsonl`.

## Enable OTel
```bash
export COPILOT_OTEL_FILE_EXPORTER_PATH="$HOME/.copilot/logs/otel-signals.jsonl"
```
Add to `~/.bashrc`/`~/.zshrc`. Only captures sessions started *after* this is set.

## analyze_tokens.py
```
python3 "$SKILL_DIR/scripts/analyze_tokens.py" [PATH]
  --by model|session|day|all    (default: model)
  --rates FILE                  per-model rates JSON (see rates.copilot.json)
  --top N  --since/--until YYYY-MM-DD  --show-time  --json
  --session SESSION_ID  --current-session  --last N
  --rate-input N  --rate-output N  --rate-cache-read N  --rate-cache-write N
```
Columns: calls, input, output, reasoning, cache_rd, cache_cr, total [, est_cost].
`fresh_input = input − cache_rd − cache_cr` (cache fields are subsets of input).

## analyze_sessions.py
```
python3 "$SKILL_DIR/scripts/analyze_sessions.py" --report context|growth|tools|turns|compactions|breakdown [PATH]
  --by session|model|all  --warn PCT  --top N  --turn N
  --since/--until YYYY-MM-DD  --json
  --session SESSION_ID  --current-session  --last N
```
- `context`     — fill % per group: turns, median/p95/max fill, turns above threshold, ctx_limit.
- `growth`      — delta per turn: top spikes, by-tool and by-initiator breakdown. MCP tools annotated `[mcp]`.
- `tools`       — `execute_tool` span latency: tool, type (MCP/builtin), calls, avg/p95/max ms, errors.
- `turns`       — per-turn detail: turn_id, time, model, initiator, input/output/fresh/cache tokens, ctx fill%, latency, tools.
- `compactions` — detects context-drop events (model summarised old turns); shows before/after fill%, tokens recovered.
- `breakdown`   — inferred context composition (no content capture needed): sys_instructions (cache_rd at turn 0),
                  skill_content (cache_cr on skill turns), tool_definitions (~estimated from JSON length ÷ 4),
                  conversation_history (ctx_delta sum for non-skill turns after turn 0). Shows tokens, %, ASCII fill bar.

Session selectors (`--session`, `--current-session`, `--last`) are mutually exclusive.

## chart_context.py
```
python3 "$SKILL_DIR/scripts/chart_context.py" [PATH]
  --style spark|grid  --width N  --height N  --warn PCT  --no-color
  --session SESSION_ID  --current-session  --last N
```
ASCII time-series chart of context window fill % over turns.
- `spark` (default) — one row per session, sparkline of fill% across turns.
- `grid` — 2-D plot of fill% over time for all sessions.

## Log rotation
```bash
sh "$SKILL_DIR/scripts/rotate_otel_log.sh"                     # size-gated (default 50 MiB)
OTEL_LOG_FORCE=1 sh "$SKILL_DIR/scripts/rotate_otel_log.sh"    # force now
```
Tunables: `OTEL_LOG_MAX_BYTES`, `OTEL_LOG_KEEP` (default 8 generations).

See [REFERENCE.md](REFERENCE.md) for all env vars and GenAI attribute names.
