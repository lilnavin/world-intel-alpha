# Hermes

Observed 2026-10-29: [official skill guide](https://hermes-agent.nousresearch.com/docs/guides/work-with-skills) documents `~/.hermes/skills/`. Copy the complete source folder to `~/.hermes/skills/world-intel/` (or the active profile's configured skills home), then confirm discovery with `hermes skills list` in a new session. This is separate from installing World Intel into its Python environment.

Select the explicit installed Hermes interpreter and, only if needed for module importability, its trusted source root:

```yaml
analysis:
  engine: hermes
  hermes:
    python: /absolute/hermes/venv/bin/python
    source_root: /absolute/trusted/hermes-source
```

On Windows use the installed environment's absolute `Scripts/python.exe` path. Locate interpreter/import paths locally without dumping environment/authentication files. Omit `source_root` when host modules are already importable. World Intel runs a packaged standalone bridge under this interpreter and reuses the configured host model/auth resolver; no separate provider fields are needed.

The bridge requires inspected host helper capabilities and supported direct completion routes. Unsupported versions/routes fail closed; no ordinary chat-agent fallback is attempted. Host auth resolution can perform its normal refresh or configuration backup behavior. Mocked/source compatibility is not a guarantee for every installed Hermes/provider/model combination.

Use the helper's JSON result for an on-demand local artifact. Only an explicitly authorized Hermes cron integration should add `--emit-media`; configure that integration separately and confirm actual transport independently. This recipe creates no job and sends no message.

The bridge sends the actual call-specific JSON Schema in a dedicated system format-contract message while preserving the original user prompt unchanged. World Intel always validates the returned JSON locally; unknown provider schema-format capabilities do not trigger a retry. Evidence captures the wrapper input/schema and actual SDK-decoded response text, including text from rejected responses when available, rather than claiming to capture original provider wire bytes.
