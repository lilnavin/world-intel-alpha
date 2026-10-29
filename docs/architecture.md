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

`tools/check_publication.py` never publishes. Strict production qualification
retains CI, history, installed-product checks, the real minimal dry-run gate and
separate human approvals. Version 1 manifests retain seven scheduled runs and strict
replay; version 2 retains two consecutive frozen scheduled days and its
coverage-regression requirements. The separate
[source-publication decision](experimental-alpha.md) does not change these
checker gates. Do not treat attested log files as independently authenticated
execution. See [coverage validation](coverage-validation.md) for gate details.

## Coverage configuration

The example profiles in `config/` are also packaged for `world-intel init`.
Configuration drives runtime paths, sources, SEC lane switches, tuning and news
desks. Profiles are trusted local input; keep private configuration and all data
paths outside every Git checkout. `runtime.media_dir` defaults to
`<runtime.data_root>/media`, and `runtime.env_file` to `<runtime.data_root>/.env`.
The shipped profiles use `~/.world-intel` as their data root.

Desk configuration order controls selection and prompt order; `tab.order`
controls national-tab display order. `target_stories[1]` is the schema maximum;
the lower bound remains an editorial target, not permission to invent stories.
Normal runs use `tuning.per_desk_slots` for candidate counts.
`tuning.enrichment.max_items` applies only to the legacy enrichment helper when
called without preselected candidates, not to the normal pipeline.

Built-in desk vocabulary still gives those desks a salience advantage; feed
desk strings are not yet cross-validated against configured IDs. Review feed
desk spelling carefully. Prompt normalization remains deferred.

The minimal profile uses public RSS without source API keys. The world profile
adds US/finance feeds and selected market inputs. The full reference profile
adds Japan/Canada desks, broader markets, FRED series, GDELT themes, SEC filings
and an earnings watchlist. Replace its synthetic SEC contact before real SEC
requests. yfinance is the primary market source; Twelve Data is an optional
keyed fallback. FRED needs `FRED_API_KEY`. Other optional keyed integrations are
NewsData, Guardian, Marketaux and Finnhub. Keys are read from `runtime.env_file`;
source availability and terms remain provider-specific. All live profiles need
the installed, authenticated Codex CLI; configuration accepts only the Codex
analysis engine.

## Demo and live execution

`world-intel demo` renders bundled invented data locally without network or
model calls. Installing dependencies still needs network access. Live
`run --offline` is unsupported. `doctor` checks local dependencies, configuration
and executable availability, but makes no authentication/model call.

Live `run --dry` still uses external sources and Codex, staging working data in
a temporary directory. It renders HTML in memory and reports diagnostics
without publishing a viewable HTML file, normal receipts or persistent
knowledge updates. Normal runs can save HTML and state; inspect
[analysis status and review holds](analysis-reliability.md), since process exit
zero alone does not prove complete or accepted output.

The `file` and `stdout` delivery policies support standalone use. `telegram`
and `hermes-cron` both format a Hermes MEDIA handoff for external transport.
There is no built-in scheduler or direct Telegram Bot API client, and a handoff
is not a delivery receipt.
