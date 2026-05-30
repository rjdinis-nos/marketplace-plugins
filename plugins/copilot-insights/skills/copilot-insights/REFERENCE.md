# Reference: Copilot CLI OpenTelemetry & GenAI token attributes

## Activation

OTel is **off by default**. It activates when any one of these is set:

- `COPILOT_OTEL_ENABLED=true`
- `OTEL_EXPORTER_OTLP_ENDPOINT=<url>`
- `COPILOT_OTEL_FILE_EXPORTER_PATH=<file>`

Only sessions started *after* activation are captured. Built-in help:
`copilot help monitoring`.

## Environment variables

| Variable | Purpose |
|---|---|
| `COPILOT_OTEL_ENABLED` | `"true"` to explicitly enable OTel. Default `false`. |
| `COPILOT_OTEL_EXPORTER_TYPE` | `otlp-http` (default) or `file`. Auto-selects `file` when the path below is set. |
| `COPILOT_OTEL_FILE_EXPORTER_PATH` | Write all signals to this file as JSON-lines. Auto-enables OTel. |
| `COPILOT_OTEL_SOURCE_NAME` | Instrumentation scope name. Default `github.copilot`. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP collector URL. Auto-enables OTel. |
| `OTEL_EXPORTER_OTLP_HEADERS` | Auth headers, e.g. `Authorization=Bearer <token>`. |
| `OTEL_SERVICE_NAME` | Service name in resource attributes. Default `github-copilot`. |
| `OTEL_RESOURCE_ATTRIBUTES` | Extra resource attributes, comma-separated `key=value`. |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | `"true"` to capture full prompt/response content (sensitive). Default `false`. |
| `OTEL_LOG_LEVEL` | OTel diagnostic log level: NONE, ERROR, WARN, INFO, DEBUG, VERBOSE, ALL. |
| `COPILOT_TOKEN_RATES` | Path to a per-model JSON rates file used by `analyze_tokens.py` for cost estimates (a `models` map keyed by model id; see `rates.copilot.json`). Read by the analyzer only — not by the CLI. |

## GenAI signals emitted

### Metrics
- `gen_ai.client.token.usage` — token-usage histogram, split by `gen_ai.token.type` (`input` / `output`).
- `gen_ai.client.operation.duration` — latency.

### Token attributes (on spans)
- `gen_ai.usage.input_tokens`
- `gen_ai.usage.output_tokens`
- `gen_ai.usage.reasoning.output_tokens`
- `gen_ai.usage.cache_read.input_tokens`
- `gen_ai.usage.cache_creation.input_tokens`

### Report column meanings
- **input** — tokens sent *to* the model (prompt: system instructions, history, tool defs, file context).
- **output** — tokens the model *generated* (reply + tool calls).
- **reasoning** — output tokens spent on internal "thinking" before the final answer (subset of output-side work).
- **cache_rd** (cache read) — input tokens served from the prompt cache instead of reprocessing; usually billed at a discount.
- **cache_cr** (cache creation) — input tokens written into the cache the first time; often a small premium, recouped on later reads.
- **total** — `input + output`. reasoning is part of output; cache_rd/cache_cr describe *how* input was billed and are not re-added into total.

### Request / response / agent attributes
- `gen_ai.operation.name`, `gen_ai.provider.name`
- `gen_ai.request.model`, `gen_ai.request.stream`
- `gen_ai.response.id`, `gen_ai.response.model`, `gen_ai.response.finish_reasons`
- `gen_ai.conversation.id` (session grouping key)
- `gen_ai.agent.{id,name,version,description}`
- `gen_ai.response.time_to_first_chunk`

### Tool-call attributes
- `gen_ai.tool.{name,type,description,definitions}`
- `gen_ai.tool.call.{id,arguments,result}`

### Content (opt-in only)
- `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`

## OTLP JSON shape (file exporter)

Each line is an OTLP export object. Token data appears either as span
attributes inside `resourceSpans[].scopeSpans[].spans[].attributes[]` (each
attribute is `{key, value:{intValue|stringValue|...}}`), or as metric data
points inside `resourceMetrics[].scopeMetrics[].metrics[]`. The analyzer
handles both array-form and plain-object attributes.

## analyze_tokens.py

```
python3 "$SKILL_DIR/scripts/analyze_tokens.py" [PATH] [--by model|session|day|all] [--json]
        [--top N] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--show-time] [--current-only]
        [--rates FILE] [--rate-input N] [--rate-output N] [--rate-cache-read N] [--rate-cache-write N] [--currency SYM]
```
(`$SKILL_DIR` is this skill's directory, i.e. the folder containing this file.)
- `PATH` defaults to `$COPILOT_OTEL_FILE_EXPORTER_PATH`, then `~/.copilot/logs/otel-signals.jsonl`.
- By default also reads rotated/compressed siblings (`PATH*`, including `.gz`); use `--current-only` to read just the active file.
- `--show-time` adds `first`/`last` activity datetime columns (UTC, derived from span timestamps); in `--json` these appear as `first_ts`/`last_ts` ISO-8601 strings.
- Prefers per-call span attributes; falls back to the token metric if no span usage is present.
- Cost: pass a `--rates FILE` (or `$COPILOT_TOKEN_RATES`) JSON file to add an
  `est_cost` column. The file is a `models` map keyed by telemetry model id
  (e.g. `claude-opus-4.8`), each with `input`/`output`/`cache_read`/`cache_write`
  rates (all `$`/Mtok), plus optional top-level `currency` and a `default` block
  (see `rates.copilot.json`). Each call is priced with its own model's rate;
  calls with no matching rate are excluded and counted in a note unless a
  `default` block or `--rate-*` flags supply a fallback (`--rate-*` overrides the
  `default`). Cost is an estimate, not billing-grade. `fresh_input = input −
  cache_rd − cache_cr` is what gets the full input rate; `cache_write` (Anthropic
  only) defaults to the input rate when omitted.
- Tests: `python3 "$SKILL_DIR/scripts/test_analyze_tokens.py"` (stdlib only).

## analyze_sessions.py

```
python3 "$SKILL_DIR/scripts/analyze_sessions.py" --report context
         [PATH] [--by session|model|all] [--warn PCT] [--top N]
         [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--json] [--current-only]
```
(`$SKILL_DIR` is this skill's directory, i.e. the folder containing this file.)
- `PATH` defaults to `$COPILOT_OTEL_FILE_EXPORTER_PATH`, then `~/.copilot/logs/otel-signals.jsonl`.
- By default also reads rotated/compressed siblings; use `--current-only` to read just the active file.
- `--report context` — context window fill analysis. Reads `github.copilot.session.usage_info`
  events inside `chat` spans; computes `current_tokens / token_limit` per turn.
- Output columns:
  - **group** — session UUID (truncated), model name, or `all` depending on `--by`
  - **turns** — number of LLM round-trips with context usage data
  - **median_fill** — median context fill % across turns
  - **p95_fill** — 95th-percentile fill %
  - **max_fill** — peak fill % (most at-risk turn)
  - **turns_>70%** — number of turns that exceeded the warn threshold
  - **ctx_limit** — context window token limit (from telemetry)
- Sorted by `max_fill` descending (most at-risk first).
- `--warn PCT` — warn threshold in % for the `turns_>70%` column (default 70).
- `--json` emits a JSON array; keys mirror the column names (snake_case).
- When max_fill is high, the model silently drops oldest turns, causing repeated
  work and extra token spend. Recommend starting a new session around 70% fill.
