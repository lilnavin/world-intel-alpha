---
name: world-intel
description: Generate an on-demand World Intel briefing and verify its fresh local artifact before handing it to the user.
---

# World Intel

Use for an explicitly requested briefing or World Intel setup. Read [setup](references/setup.md) first for a new installation, then the selected host recipe: [Hermes](references/hermes.md) or [OpenClaw](references/openclaw.md).

Use the owner's installed Python environment and absolute private configuration path:

```text
<python> -I -m worldintel.agent_run --config <absolute-profile.yaml>
```

Run once. Do not add `--force` unless the owner explicitly requests regeneration. The default output is one JSON result. Only `outcome: generated` includes a verified local `artifact.path` and `artifact.sha256`. Attach that exact file using an authorized host attachment operation. A local generated file does not confirm delivery.

For `skipped`, `held`, `failed` or `busy`, explain the outcome without attaching an older file. Do not retry automatically, remove receipts/artifacts, bypass review, or change model settings. The helper stores captured diagnostics under the private data root. Never expose raw logs or credentials in chat.

Use `--emit-media` only for an explicitly requested compatible Hermes cron handoff. Its `MEDIA:` line is emitted after verification and still is not a delivery receipt. Creating schedules, enabling plugins, changing host permissions and sending messages require their own authorization.
