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
| `COPILOT_TOKEN_RATES` | Path to a JSON rates file used by `analyze_tokens.py` for cost estimates. Either flat (`input`/`output`/`cache_read`/`cache_write`/`currency`, `$`/Mtok) or a per-model `models` map (see `rates.copilot.json`). Read by the analyzer only — not by the CLI. |

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
- Cost: pass a `--rates FILE` (or `$COPILOT_TOKEN_RATES`) JSON file, or
  `--rate-*` flags, to add an `est_cost` column. Two file shapes are accepted:
  - **Flat** — top-level `input`/`output`/`cache_read`/`cache_write`/`currency`
    (all `$`/Mtok) applied to every model (see `rates.example.json`).
  - **Per-model** — a `models` map keyed by telemetry model id (e.g.
    `claude-opus-4.8`), each with the same rate keys, plus an optional top-level
    `default` block and `currency` (see `rates.copilot.json`). Each call is
    priced with its own model's rate; calls with no matching rate and no
    `default` are excluded and counted in a note. `--rate-*` flags override the
    `default`.
  Cost is an estimate, not billing-grade. `fresh_input = input − cache_rd −
  cache_cr` is what gets the full input rate; `cache_write` (Anthropic only)
  defaults to the input rate when omitted.
- Tests: `python3 "$SKILL_DIR/scripts/test_analyze_tokens.py"` (stdlib only).
