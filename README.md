# rjdinis-nos marketplace

A curated marketplace of plugins for the GitHub Copilot CLI and Claude Code.

```bash
/plugin marketplace add rjdinis-nos/marketplace-plugins
```

## Plugins

### copilot-insights

> Copilot CLI observability via built-in OpenTelemetry — token usage, cost estimates, and session health.

**Why:** The Copilot CLI emits rich OpenTelemetry signals that include per-LLM-call token counts, context window fill, and tool latency. This plugin turns those signals into actionable reports — without relying on third-party telemetry services.

**Highlights:**

| Feature | What it does |
|---|---|
| 📊 Token reports | Input, output, reasoning, cache read, cache creation — grouped by model, session, or day |
| 💰 Cost estimates | Per-model pricing using the bundled `rates.copilot.json`; cache discounts applied automatically |
| 🪟 Context health | Per-session fill %, growth drivers, spikes, and turns approaching the limit |
| 🛠️ Tool latency | Avg / p95 / max ms for each tool call, distinguishing MCP from builtin tools |
| 📈 JSON output | `--json` flag for all reports; pipe into jq, dashboards, or your own scripts |
| 🔄 Log rotation | Automatic size-gated rotation of OTel log files on session start |


**Zero dependencies** — `analyze_tokens.py` and `analyze_sessions.py` run on the standard library (Python 3.8+).

---

#### Quick start

##### Install in bash terminal
```bash
# 1. Add Marketplace and Plugin
copilot plugin marketplace add rjdinis-nos/marketplace-plugins
copilot plugin install copilot-insights@rjdinis-nos

# 2. Enable OTel in the Copilot CLI (one-time setup)
export COPILOT_OTEL_FILE_EXPORTER_PATH="$HOME/.copilot/logs/otel-signals.jsonl"
# Add the line above to ~/.bashrc or ~/.zshrc to persist across sessions.
# ⚠️ Only new copilot sessions started after this is set will be captured.
```

##### Uninstall in bash terminal
```bash
# 1. Delete Marketplace and Plugin
copilot plugin uninstall copilot-insights@rjdinis-nos
copilot plugin marketplace remove rjdinis-nos/marketplace-plugins

# 2. Enable OTel in the Copilot CLI (one-time setup)
unset COPILOT_OTEL_FILE_EXPORTER_PATH="$HOME/.copilot/logs/otel-signals.jsonl"
# Remove the line above from ~/.bashrc or ~/.zshrc
```

##### Install in Copilot CLI
```bash
/plugin marketplace add rjdinis-nos/marketplace-plugins
/plugin install copilot-insights@rjdinis-nos
```

##### Unistall in Copilot CLI
```bash
/plugin uninstall copilot-insights@rjdinis-nos
/plugin marketplace remove rjdinis-nos/marketplace-plugins
```

##### Test Skill Scripts
```bash
cd /path/to/marketplace-plugins/plugins/copilot-insights/skills/copilot-insights/scripts
python3 analyze_tokens.py --by model
python3 analyze_sessions.py --report context
python3 analyze_sessions.py --report growth --json
```

#### What's bundled

| Component | Purpose |
|---|---|
| `agents/copilot-insights.agent.md` | Agent — checks OTel status, runs reports, helps interpret results |
| `skills/copilot-insights/SKILL.md` | Skill definition + quick-reference card (CLI flags, env vars, report types) |
| `skills/copilot-insights/scripts/` | `analyze_tokens.py`, `analyze_sessions.py`, `rotate_otel_log.sh`, `rates.copilot.json` |
| `hooks/hooks.json` | Auto-runs log rotation on every Copilot CLI session start |
| `tests/` | 150+ unit + integration tests for the analyzer scripts |

#### Repository layout

```
.claude-plugin/
  marketplace.json              # marketplace manifest
plugins/
  copilot-insights/
    .claude-plugin/
      plugin.json               # plugin manifest
    agents/
      copilot-insights.agent.md # bundled agent
    hooks/
      hooks.json                # session-start hooks (log rotation)
    skills/
      copilot-insights/
        SKILL.md                # skill + quick-reference
        REFERENCE.md            # full env-var and OTel attribute reference
        scripts/
          analyze_tokens.py     # token & cost aggregation
          analyze_sessions.py   # session health, growth, tool latency
          rotate_otel_log.sh    # size-gated log rotation
          rates.copilot.json    # per-model pricing snapshot
    tests/
      sample.jsonl              # 100-line stratified fixture from real OTel data (no PII)
      test_analyze_tokens.py    # 78 unit tests
      test_analyze_sessions.py  # 57 unit tests
      test_sample_integration.py # 15 integration tests against real data
```

---

## License

MIT
