# Architecture and verification boundaries

`worldintel.config` validates trusted YAML and exposes call-time configuration.
`worldintel.run` orchestrates the existing two-pass pipeline. `worldintel.core`
contains source fetching, selection, enrichment, analysis, grounding, rendering
and persistence. `worldintel.delivery` selects output policy;
`worldintel.adapters.hermes` formats the scheduler's MEDIA handoff. It does not
prove Telegram receipt or implement a separate Bot API client.

`worldintel.cli` exposes init, validate, demo, doctor and run. `worldintel.data`
loads packaged schema/prompt templates, profiles and invented examples through
`importlib.resources`; installed wheels must work outside the checkout.

The package move is separately recorded as an exact deterministic import-only
transform of 26 modules. Resource and command-line adaptations are subsequent
changes, not falsely counted as move-only. `tools/check_architecture.py` rejects
core imports crossing the adapter/delivery boundary, including nested imports.

L0 compares generated prompt/schema bytes with frozen reference artifacts. L1/L2
replay compares full selection and rendered output in isolated processes using
private recorded inputs. It does not exercise live providers, model variability,
delivery receipt or timing guards. Five matching recordings are necessary but
not sufficient for release.

`tools/deploy.py` has no live default destination. It builds a new installation
and launcher, verifies hashes and records provenance. Scratch installations are
marked dirty. Scheduler cutover and rollback require separate approval.

`tools/check_publication.py` never publishes. CI, history, installed-product
checks, the real minimal dry-run gate, seven consecutive accepted owner runs
from one frozen candidate, and separate human approvals remain release gates.
Do not treat attested log files as independently authenticated execution.
