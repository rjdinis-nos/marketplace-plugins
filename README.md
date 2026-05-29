# rjdinis-nos marketplace

A curated marketplace of plugins for the GitHub Copilot CLI and Claude Code.

The marketplace manifest lives in [`.claude-plugin/marketplace.json`](.claude-plugin/marketplace.json).

## Adding the marketplace

```bash
/plugin marketplace add rjdinis-nos/marketplace-plugins
```

Then browse and install plugins:

```bash
/plugin install token-usage@rjdinis-nos
```

## Plugins

### token-usage

Enable OpenTelemetry token capture in the GitHub Copilot CLI and report token
consumption (input / output / cache / reasoning) per model, session, or day.
Bundles an agent, a skill, and an analyzer script. Token signals follow the OTel
GenAI Semantic Conventions, so the numbers are billing-grade, not estimates.

See [`plugins/token-usage`](plugins/token-usage) for details.

## Repository layout

```
.claude-plugin/
  marketplace.json        # marketplace manifest
plugins/
  token-usage/
    .claude-plugin/
      plugin.json         # plugin manifest
    agents/               # bundled agent
    hooks/                # hooks
    skills/               # bundled skill + scripts
```

## License

MIT
