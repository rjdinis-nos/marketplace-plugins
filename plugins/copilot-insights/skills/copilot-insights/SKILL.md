---
name: copilot-insights
description: Copilot CLI observability via built-in OpenTelemetry — token usage, cost estimates, and session health (context window pressure). Use when the user wants to measure, track, audit, or report Copilot CLI token usage, LLM cost, context window fill, or session health metrics.
---

# Copilot CLI Insights

Activate the CLI's built-in OpenTelemetry (OTel) file exporter and analyze
token consumption, cost, and session health from its output. Token signals
follow the OTel GenAI Semantic Conventions — counts are exact (billing-grade),
not estimates.

## Quick start

1. Enable the OTel file exporter (writes JSON-lines of all signals):

   ```bash
   export COPILOT_OTEL_FILE_EXPORTER_PATH="$HOME/.copilot/logs/otel-signals.jsonl"
   ```

   Add it to `~/.bashrc`/`~/.zshrc` to persist, then start a new `copilot`
   session. OTel auto-enables when this variable is set.

2. After using the CLI, run a report:

   ```bash
   # Token usage and cost
   python3 "$SKILL_DIR/scripts/analyze_tokens.py" --by model

   # Session health (context window pressure)
   python3 "$SKILL_DIR/scripts/analyze_sessions.py" --report context
   ```

   `$SKILL_DIR` is this skill's directory (the bundled `scripts/` directory
   sits next to this `SKILL.md`). With no path argument both scripts read
   `$COPILOT_OTEL_FILE_EXPORTER_PATH`.

---

## Workflows — Token Usage (`analyze_tokens.py`)

### Report token usage
- `--by model` (default), `--by session`, `--by day`, or `--by all`
- `--json` for machine-readable output
- `--top N` to show only the largest N groups; `--since`/`--until YYYY-MM-DD`
  to restrict to a UTC date window
- `--show-time` adds `first`/`last` activity datetime (UTC) columns — handy
  with `--by day`
- `--by session` always shows compact columns: session, first (UTC), last (UTC),
  calls, est_cost — sorted most recent first
- Output columns: calls, input, output, reasoning, cache_rd (cache read),
  cache_cr (cache creation), total. `total = input + output`.

### Estimate cost
Cost is an **estimate** from per-Mtok rates (rates are not in the telemetry):
```bash
# Prices each model with its own rate automatically:
python3 "$SKILL_DIR/scripts/analyze_tokens.py" --by model --rates "$SKILL_DIR/scripts/rates.copilot.json"
# optional fallback for models not in the table: --rate-input 5 --rate-output 25 --rate-cache-read 0.5 --rate-cache-write 6.25
# or: export COPILOT_TOKEN_RATES=/path/to/rates.copilot.json
```
Adds an `est_cost` column and a cache-savings summary. `cache_rd`/`cache_cr` are
subsets of `input`, so full-price tokens are `fresh_input = input − cache_rd −
cache_cr`.

`rates.copilot.json` is a **snapshot** of GitHub's published Copilot pricing
(per 1M tokens). It is a `models` map keyed by the telemetry model id (e.g.
`claude-opus-4.8`), each with `input`/`cache_read`/`cache_write`/`output`; an
optional top-level `default` block (or `--rate-*` flags) prices any model not in
the map. Prices change — verify against the `_source` URL inside the file.

---

## Workflows — Session Health (`analyze_sessions.py`)

### Context window pressure report
```bash
python3 "$SKILL_DIR/scripts/analyze_sessions.py" --report context
```
Groups sessions by context fill % (current_tokens / token_limit), showing:
`turns`, `median_fill`, `p95_fill`, `max_fill`, `turns_>70%`, `ctx_limit`.
Sorted by `max_fill` descending — most at-risk session first.

At high fill the model silently drops oldest conversation turns, causing it to
repeat work and burn more tokens. **Recommendation:** start a new session when
fill approaches 70%.

Options:
- `--by session` (default), `--by model`, or `--by all`
- `--warn N` — warn threshold in % (default 70)
- `--top N`, `--since`/`--until YYYY-MM-DD`, `--json`, `--current-only`

```bash
python3 "$SKILL_DIR/scripts/analyze_sessions.py" --by model
python3 "$SKILL_DIR/scripts/analyze_sessions.py" --warn 60 --top 5
python3 "$SKILL_DIR/scripts/analyze_sessions.py" --since 2026-05-29 --json
```

---

## Verify OTel is active
```bash
grep -c gen_ai. "$COPILOT_OTEL_FILE_EXPORTER_PATH"
```
If empty: the exporter wasn't set before the session started. Re-export and
restart `copilot`.

## Send to a collector instead of a file
For dashboards (Grafana, Jaeger, Honeycomb, Langfuse, Datadog, Azure Monitor):
```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <token>"
```

## Manage log growth (rotation)

The file exporter only **appends**. This plugin ships a `SessionStart` hook that
runs `scripts/rotate_otel_log.sh` automatically — size-gated at 50 MiB, async,
zero startup penalty. Both analyzers read rotated `.gz` siblings by default.

```bash
sh "$SKILL_DIR/scripts/rotate_otel_log.sh"           # rotates only if >= 50 MiB
OTEL_LOG_FORCE=1 sh "$SKILL_DIR/scripts/rotate_otel_log.sh"    # rotate now
```

Tunables: `OTEL_LOG_MAX_BYTES` (default 50 MiB), `OTEL_LOG_KEEP` (default 8).

## Notes
- OTel is off by default and only captures sessions started *after* the env var
  is set. Past sessions cannot be recovered.
- Prompt/response *content* is NOT captured unless
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` is also set.

See [REFERENCE.md](REFERENCE.md) for all env vars and GenAI attribute names.
