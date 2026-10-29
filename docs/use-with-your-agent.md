# Use World Intel with your agent

World Intel separates the installed Python runtime from a portable agent skill. Install the Python package in your chosen environment, then copy the complete [world-intel skill folder](../integrations/skills/world-intel/) from trusted source into your host's documented skill directory. The wheel contains the runtime/helper, not the source skill folder.

Start with [setup](../integrations/skills/world-intel/references/setup.md), then select [Hermes](../integrations/skills/world-intel/references/hermes.md) or [OpenClaw](../integrations/skills/world-intel/references/openclaw.md). Codex remains the default engine. Native engines reuse host model/authentication through feature-guarded adapters; mocked tests and source inspection do not independently establish live compatibility.

```text
world-intel-agent --config <absolute-private-profile.yaml>
```

Or use the installed environment directly: `<python> -I -m worldintel.agent_run --config <absolute-private-profile.yaml>`. Neither command depends on checkout working directory. Runs serialize per resolved data root; a busy or abandoned `.agent-run.lock` fails closed. After confirming no helper is active, an owner can investigate an abandoned lock manually.

The helper invokes the pipeline once under a temporary file-only delivery profile, preserves your original profile, and captures diagnostics privately in `<data_root>/agent-diagnostics/`. It verifies a new invocation-bound run and matching root/sidecar receipt, complete analysis/quality, accepted enabled review, media containment and unchanged artifact hash. Exit 0 means `generated`; all other outcomes return exit 1. Only `generated` includes an artifact attachment path/hash. All outcomes report `delivery_confirmed: false`.

`skipped` means no new receipt; `held` means review withheld generation; `failed` covers pipeline/configuration/verification failure; `busy` means the helper lock is already owned. An existing HTML file or zero pipeline exit never qualifies alone. No automatic retry or force is performed. Use `--force` only when explicitly requesting regeneration. `--emit-media` adds a verified MEDIA handoff after the JSON result only on success; it does not establish transport delivery.

Configure coverage, timezone and private paths before a requested manual live run. Doctor is local-only; demo data is invented; dry runs still call live sources/models and publish no attachment. Schedule creation, host configuration changes, plugin installation and external messages are separate actions.
