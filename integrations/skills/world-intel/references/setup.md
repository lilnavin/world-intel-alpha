# Setup

Install Python 3.11+ and World Intel into the chosen environment, for example `<python> -m pip install <trusted-source-checkout>`. Python package installation installs commands and runtime resources; it does not install this skill. From the trusted source checkout, copy the entire `integrations/skills/world-intel` directory, including `references`, to the selected host's skill directory. See the host recipe for the destination. Do not overwrite another skill without reviewing it.

Collect host/OS, the absolute Python executable, absolute profile path, private data/media directories outside Git, timezone, enabled desks/sources, selected analysis engine and requested output handling. Do not request credentials in chat. Create the profile's parent directory, then run:

```text
<python> -m worldintel.cli init --profile minimal --out <absolute-profile.yaml>
<python> -m worldintel.cli validate --config <absolute-profile.yaml> --strict
<python> -m worldintel.cli doctor --config <absolute-profile.yaml>
```

Edit the profile to choose `runtime.timezone`, `runtime.data_root`, `runtime.media_dir`, desks and sources. Minimal uses RSS and disables SEC lanes; full requires the owner's actual SEC contact. Source API keys, when needed for enabled sources, are separate from model authentication and belong in the private configured env file.

Select `analysis.engine`: `codex` (default), `hermes`, `openclaw`, or `command`. Use the relevant host recipe. `command` is an advanced trusted argv bridge: exact prompt on stdin, schema/output paths in `WORLD_INTEL_SCHEMA_PATH`/`WORLD_INTEL_OUTPUT_PATH`; the bridge must write actual returned JSON text to the new output file. Ordinary chat CLIs do not automatically satisfy this protocol.

Doctor checks local prerequisites; it does not verify live authentication. `demo` renders invented offline data. Live `run --dry` still uses network/models and creates no deliverable. After configuration review, perform one explicitly requested manual helper run before considering a separately authorized schedule. Native adapters are feature guarded and tested with mocked contracts; installed host versions and models need a bounded live check before being called live qualified.
