# OpenClaw

Observed 2026-10-29: [official skills configuration](https://docs.openclaw.ai/skills-config) documents managed skills in `~/.openclaw/skills`. Copy the complete source folder to `~/.openclaw/skills/world-intel/` and confirm discovery through the installed host's skills inventory. Python package installation remains separate.

Use an existing owner-configured Gateway with the `llm-task` plugin already enabled and permitted for the selected agent. [The official llm-task contract](https://docs.openclaw.ai/tools/llm-task) and [Gateway invocation contract](https://docs.openclaw.ai/gateway/tools-invoke-http-api) describe these prerequisites. Do not start a Gateway or widen permissions during onboarding.

```yaml
analysis:
  engine: openclaw
  openclaw:
    gateway_url: http://127.0.0.1:18789
    token_env: OPENCLAW_GATEWAY_TOKEN
    # agent_id: owner-selected-agent
```

The token is read from the existing environment by name; do not place it in the profile, command line or chat. Reuse the owner's chosen agent/model/authentication. The adapter invokes `llm-task` through `/tools/invoke` with prompt/schema and an isolated runtime, not ordinary agent chat. It requires one actual JSON text result; unsupported response envelopes fail closed. Insecure remote HTTP, redirects and credentials in URLs are refused.

After a requested manual helper run, attach only the JSON result's verified generated artifact using an authorized, available host file attachment operation. Do not assume a universal attachment command or claim delivery from the helper result. Gateway/plugin/model compatibility still requires a bounded live check; this implementation's host tests use mocks.
