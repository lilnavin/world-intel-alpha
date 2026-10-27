# World Intel configurable briefing candidate

Private development candidate for the configurable World Intel briefing skill.
This snapshot is not ready for production or public release.

Configuration now drives runtime paths, sources, SEC lane switches, tuning, and
news desks. The prompt/schema generators preserve the reference profile's exact
bytes; five recorded scheduled days pass story-selection and HTML replay.
Dry runs do not publish or replace briefing files or knowledge state. Private
model evidence can be retained separately for diagnosis and release review.
The audited predecessor passed clean installation and Windows/Linux CI, with
historical minimal live dry-run evidence. Those results do not qualify changed
code for release. This candidate still needs final-commit checks, fresh content
acceptance, a frozen scheduled trial, and explicit publication approval.
Do not deploy this candidate yet.

Three example profiles live in `config/`: `minimal` (world news, no SEC), `world`
(world/US/finance, no SEC), and `full` (multi-country reference profile). Copy a
profile outside Git and set `WORLD_INTEL_CONFIG` to its path. Replace the full
profile's synthetic contact before enabling real SEC requests. Source feeds and
analysis require network access; `--dry` is not an offline mode.

Desk configuration order controls selection and prompt order; `tab.order`
controls national-tab display order. `target_stories[1]` is the schema maximum;
the lower bound remains an editorial target, not permission to invent stories.
Normal runs use `tuning.per_desk_slots` for candidate counts.
`tuning.enrichment.max_items` applies only to the legacy enrichment helper when
called without preselected candidates, not to the normal pipeline.

Known limitations: built-in desk vocabulary still gives those desks a salience
advantage; feed desk strings are not yet cross-validated against configured IDs.
Review feed desk spelling carefully. Prompt normalization remains deferred.

## Development setup

Use Python 3.11 or 3.12. From this checkout, create an isolated environment.
Windows PowerShell:

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install --require-hashes -r requirements.lock
.venv/Scripts/python.exe -m pip install --no-deps --no-build-isolation .
.venv/Scripts/python.exe -m pip check
.venv/Scripts/python.exe -m pytest tests -q
.venv/Scripts/python.exe tools/scan_secrets.py --tracked
```

Linux:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
.venv/bin/python -m pip check
.venv/bin/python -m pytest tests -q
.venv/bin/python tools/scan_secrets.py --tracked
```

Dependencies, including transitive and build dependencies, are pinned and
hash-locked. `python tools/verify_install.py` separately creates a fresh venv,
installs the package, and checks actual console commands outside the checkout.
No API keys, private owner configuration or model subscription are needed for
these tests. Installing dependencies requires internet access.

Three explicit live-service
tests are skipped; socket/DNS, curl-backed requests and live Codex invocations
are blocked during tests. This test guard is not a runtime `--offline` feature.
It is also not an operating-system network sandbox for arbitrary subprocesses.

GitHub Actions runs dependency checks, tests and the tracked-content pattern
scan on Windows and Ubuntu, with Python 3.11 and 3.12, on pushes and pull
requests. A missing/unreadable tracked file fails the scan. It scans the current
checkout, not Git history, and does not replace the publication security audit.

The source export excludes private configuration, captures, live launchers
and historical planning documents. A generic scratch-deployment tool and launcher
template are included. Owner data belongs outside this repository.
The release requires installed-package tests, Windows/Linux CI, the applicable
replay or reviewed coverage-regression evidence, and a clean history audit.
The version 2 acceptance policy requires two consecutive scheduled days after
freezing the candidate and profile, independently verified delivery, and owner
content acceptance. Legacy version 1 manifests retain their seven-run policy.
Manual runs and successful model passes do not replace scheduled acceptance.

## Try the installed commands

Activate your virtual environment, or use its `Scripts/world-intel.exe` on
Windows / `bin/world-intel` on Linux. The following demo is fully offline and
uses invented stories and figures:

```text
world-intel --help
world-intel demo --out out/demo
```

Choose a configuration path outside all Git repositories. For a normal clone,
its parent directory is usually suitable:

```text
world-intel init --profile minimal --out ../worldintel.yaml
world-intel validate --config ../worldintel.yaml --strict
world-intel validate --config ../worldintel.yaml --explain
```

Commands never overwrite an existing profile or demo. Read and edit your profile
before a live run. `doctor` reports local prerequisites, not successful login:

```text
world-intel doctor --config ../worldintel.yaml
```

After configuring and authenticating Codex, a live dry run is:

```text
world-intel run --config ../worldintel.yaml --dry --force
```

This last command uses external providers and model access; it is not part of
the offline demo proof. `run --offline` is explicitly unsupported.
`telegram` and `hermes-cron` delivery policies both emit the Hermes MEDIA
handoff; standalone users should choose `file` or `stdout`. No direct Telegram
Bot API transport or delivery receipt is implied.

## Source review and private evidence

Coverage checks establish registered-event coverage and source eligibility;
they do not establish factual correctness. To request one additional bounded
review of final full story cards against their captured cited sources, add this
setting to your private profile's existing `analysis` section:

```yaml
analysis:
  source_review_enabled: true
  source_review_repair_enabled: true
```

Existing profiles omit this option and keep their previous model-call behavior.
Review consumes model access or subscription quota and uses the remaining
analysis deadline, with at most 120 seconds per call. With only review enabled,
it adds at most one invocation and holds unsupported or uncertain cards. The
optional repair setting permits one correction of identified unsupported cards
and one final review of every card: at most three added invocations in total.
Before review, a staged preflight can remove an extra citation only when it
uniquely matches a selected source with no captured article body and the card
retains a substantive full-text tier-1/2 source. Unknown or ambiguous citations,
and cards without a usable remaining source, still hold the run. The private
evidence binds the original and retained citation lists; all claims must then
pass review against the retained sources. Citation cleanup is adopted only
after acceptance. It does not remove a card or change its prose.
Release validation refuses cleanup whose original card cannot be reconstructed
exactly from the retained generation evidence.

The optional prose correction preserves the remaining citations and card
membership, and adopts corrected prose only after final acceptance. There is
no further correction or retry if it fails.
Some explicit claim/quote contradictions, including simple cases where a stated
plan is presented as a completed action, are rejected deterministically and use
that same correction limit. This narrow check abstains on unsupported grammar;
it is not general proof that every claim follows from its source.
Missing sources, invalid responses, unsupported final claims, or exhausted
budget hold the run before briefing, knowledge updates, or delivery. Release
validation enables both checks; disabled review cannot count as a factual pass.

The review covers full world, configured national, and finance story cards'
headlines, explanations, and bodies. It does not certify radar, threads, signal,
scoreboard data, other finance narrative, or knowledge facts. Valid evidence
quotes and a model's supported verdict reduce risk; they do not prove that all
claims were identified or correctly checked. Inspect cited reporting before
owner content acceptance.

Exact CLI stdin/schema bytes and raw outputs are private evidence, separate from replay
capsules and briefing attachments. Keep the configured data directory outside
Git and restrict access to it. Never upload evidence stores to public issues,
release archives, or CI artifacts. An incomplete evidence capture cannot qualify
a release even if generation succeeds. Offline `demo` does not invoke review.

See [architecture](docs/architecture.md), [contribution guidance](CONTRIBUTING.md)
and [security boundaries](SECURITY.md). No command here makes the repository public.
