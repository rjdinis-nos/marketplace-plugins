---
name: token-usage
description: Enable OpenTelemetry token capture in GitHub Copilot CLI and analyze/report token consumption (input, output, cache, reasoning) per model, session, or day. Use when the user wants to measure, track, log, audit, or report Copilot CLI token usage or LLM cost, or asks about OTel/OpenTelemetry token metrics.
---

# Copilot CLI Token Usage

Set up the CLI's built-in OpenTelemetry (OTel) file exporter and summarize token
consumption from its output. Token signals follow the OTel GenAI Semantic
Conventions, so the numbers are exact (billing-grade), not estimates.

## Quick start

1. Enable the OTel file exporter (writes JSON-lines of all signals):

   ```bash
   export COPILOT_OTEL_FILE_EXPORTER_PATH="$HOME/.copilot/logs/otel-signals.jsonl"
   ```

   Add it to `~/.bashrc`/`~/.zshrc` to persist, then start a new `copilot`
   session. OTel auto-enables when this variable is set.

2. After using the CLI, summarize usage:

   ```bash
   python3 "$SKILL_DIR/scripts/analyze_tokens.py" --by model
   ```

   `$SKILL_DIR` is this skill's own directory (the bundled `scripts/`
   directory sits next to this `SKILL.md`). With no path argument the script
   reads `$COPILOT_OTEL_FILE_EXPORTER_PATH`.

## Workflows

### Report token usage
- `--by model` (default), `--by session`, `--by day`, or `--by all`
- `--json` for machine-readable output
- `--top N` to show only the largest N groups; `--since`/`--until YYYY-MM-DD`
  to restrict to a UTC date window
- `--show-time` adds `first`/`last` activity datetime (UTC) columns — handy with
  `--by session` to see when each session ran
- Output columns: calls, input, output, reasoning, cache_rd (cache read),
  cache_cr (cache creation), total. `total = input + output`.

### Estimate cost
Cost is an **estimate** from per-Mtok rates (rates are not in the telemetry):
```bash
# Per-model: prices each model with its own rate automatically (recommended)
python3 "$SKILL_DIR/scripts/analyze_tokens.py" --by model --rates "$SKILL_DIR/scripts/rates.copilot.json"
# Flat single rate for every model:
python3 "$SKILL_DIR/scripts/analyze_tokens.py" --by model --rates "$SKILL_DIR/scripts/rates.example.json"
# or per-flag default/fallback: --rate-input 5 --rate-output 25 --rate-cache-read 0.5 --rate-cache-write 6.25
# or: export COPILOT_TOKEN_RATES=/path/to/rates.json
```
Adds an `est_cost` column and a cache-savings summary. `cache_rd`/`cache_cr` are
subsets of `input`, so full-price tokens are `fresh_input = input − cache_rd −
cache_cr`.

`rates.copilot.json` is a **snapshot** of GitHub's published Copilot pricing
(per 1M tokens). It is a `models` map keyed by the telemetry model id (e.g.
`claude-opus-4.8`), each with `input`/`cache_read`/`cache_write`/`output`; an
optional top-level `default` block prices any model not in the map (otherwise
those calls are excluded and counted in a note). Anthropic models have a
distinct `cache_write` rate; for others omit it and cache-creation tokens fall
back to the input rate. Prices change — verify against the `_source` URL inside
the file. For a flat single rate, copy `rates.example.json`.

### Verify OTel is active
Confirm at least one model call has been logged:
```bash
grep -c gen_ai. "$COPILOT_OTEL_FILE_EXPORTER_PATH"
```
If empty: the exporter wasn't set before the session started, or no model call
happened yet. Re-export the variable and restart `copilot`.

### Send to a collector instead of a file
For dashboards (Grafana, Jaeger, Honeycomb, Langfuse, Datadog, Azure Monitor):
```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:4318"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <token>"
```

## How it reads tokens

The analyzer walks the JSONL recursively and reads token counts from span
attributes `gen_ai.usage.*` (per call, with model + conversation id), and
cross-checks against the `gen_ai.client.token.usage` metric. It works
regardless of OTLP nesting.

## Manage log growth (rotation)

The file exporter only **appends**, so `otel-signals.jsonl` grows without bound.

**Automatic (default).** This plugin ships a `SessionStart` hook
(`hooks/hooks.json`) that runs the rotator at the start of every Copilot CLI
session. It is size-gated, so it only actually rotates once the log reaches
`OTEL_LOG_MAX_BYTES` (default 50 MiB) and is otherwise a near-instant no-op. The
hook runs `async`, so it never blocks session startup. No cron or external tool
is required.

**Manual / scheduled.** You can also trigger the bundled rotator
`scripts/rotate_otel_log.sh` yourself or from cron. Each run gzips the current
log to a timestamped sibling and **truncates the original in place** (the CLI
holds the file open, so it must be truncated, not renamed):

```bash
sh "$SKILL_DIR/scripts/rotate_otel_log.sh"          # rotates only if >= 50 MiB
OTEL_LOG_FORCE=1 sh "$SKILL_DIR/scripts/rotate_otel_log.sh"   # rotate now
```

Or schedule it — add to crontab (`crontab -e`) to check hourly:

```cron
0 * * * * sh "$HOME/<path-to-skill>/scripts/rotate_otel_log.sh" >/dev/null 2>&1
```

Tunables (env): `OTEL_LOG_MAX_BYTES` (default 50 MiB), `OTEL_LOG_KEEP`
(generations to keep, default 8), `OTEL_LOG_FORCE=1`. Uses only POSIX `sh` +
coreutils + `gzip` — no external rotation daemon required.

`analyze_tokens.py` reads rotated/compressed siblings (`*.jsonl.*.gz`) by
default, so totals survive rotation. Pass `--current-only` to read just the
active log.

## Notes
- OTel is off by default and only captures sessions started *after* the env var
  is set. It cannot retroactively recover token data from past sessions.
- Prompt/response *content* is NOT captured unless
  `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true` is also set.

See [REFERENCE.md](REFERENCE.md) for all env vars and GenAI attribute names.
