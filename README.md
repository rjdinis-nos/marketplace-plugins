# rjdinis-nos marketplace

A curated marketplace of plugins for the GitHub Copilot CLI and Claude Code.

The marketplace manifest lives in [`.claude-plugin/marketplace.json`](.claude-plugin/marketplace.json).

## Adding the marketplace

```bash
/plugin marketplace add rjdinis-nos/marketplace-plugins
```

Then browse and install plugins:

```bash
/plugin install copilot-insights@rjdinis-nos
```

## Plugins

### copilot-insights

Enable OpenTelemetry token capture in the GitHub Copilot CLI and report token
consumption (input / output / cache / reasoning) per model, session, or day,
plus session health (context window pressure, latency). Bundles an agent, a
skill, and analyzer scripts. Token signals follow the OTel GenAI Semantic
Conventions, so the numbers are billing-grade, not estimates.

See [`plugins/copilot-insights`](plugins/copilot-insights) for details.

## Repository layout

```
.claude-plugin/
  marketplace.json        # marketplace manifest
plugins/
  copilot-insights/
    .claude-plugin/
      plugin.json         # plugin manifest
    agents/               # bundled agent
    hooks/                # hooks
    skills/               # bundled skill + scripts
```

## License

MIT
