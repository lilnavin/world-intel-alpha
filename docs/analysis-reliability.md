# Analysis reliability

World and finance run sequentially with no automatic retries. World keeps the
configured briefing reasoning effort (default `high`); finance uses the same
effort. The legacy `--concurrent-passes` flag and `analysis.concurrent_passes`
setting remain accepted for old launchers, but are logged and ignored.

Optional analysis settings (defaults shown):

```yaml
analysis:
  world_timeout_s: 600
  finance_timeout_s: 360
  pipeline_timeout_s: 1500
  finish_reserve_s: 120
  rewrite_timeout_s: 180
```

Each value must be an integer from 1 to 3600. The finishing reserve must be
less than the pipeline timeout. The monotonic clock starts at pipeline entry,
so fetching, enrichment and SEC work reduce the time available to model calls.
World, finance and optional rewrite calls receive the smaller of their configured
timeout and the remaining analysis time (`pipeline_timeout_s - finish_reserve_s`
minus elapsed time). Exhausted passes are skipped, never retried. Rewrite uses
its existing medium effort. The remaining budget is checked again at its call
boundary. The budget limits model stages, not a hard end-to-end process watchdog:
existing fetch/SEC/render/persistence/handoff work keeps its existing bounds.
The outer 1650-second shadow and 1800-second scheduler limits stay external.

A model timeout kills the process tree on Windows (taskkill capped at five
seconds), then kills/reaps the direct process with a five-second cap. POSIX
uses a new session and kills its process group. Prompt/output temporary files
are removed by the caller. Timeout diagnostics retain bounded stdout/stderr
excerpts locally with URLs and credential-shaped values redacted. Public HTML
shows only availability notices, never model errors or provider URLs.

`analysis_status` accompanies the merged result and metrics:

```json
{
  "overall": "complete",
  "mode": "sequential",
  "passes": {
    "world": {"success": true, "duration_s": 0.123, "timeout_s": 600,
              "timed_out": false, "skipped": false, "error": null},
    "finance": {"success": true, "duration_s": 0.123, "timeout_s": 360,
                "timed_out": false, "skipped": false, "error": null}
  }
}
```

Overall is `complete` for both successful passes, `partial` for one, `failed`
for neither. Only complete analysis sets `codex_ok` and advances `codex.last_ok`.
`codex.status` records the latest explicit outcome; `last_run_date` retains its
independent duplicate-protection behavior. Partial/failed analysis can still
generate a useful HTML file and exit zero. Raw feed summaries are converted to
plain text, scripts/styles removed, and unsafe source links dropped. All
configured national desks explicitly show unavailable coverage after world
failure without fabricated country stories.

Normal runs atomically replace `<runtime.data_root>/run-status.json` and, after
generation, `<runtime.media_dir>/brief-YYYY-MM-DD.status.json`. Dry runs write
neither. Both receipts have identical JSON after handoff:

```json
{
  "schema_version": 1,
  "run_id": "unique UUID hex",
  "started_at": "ISO timestamp with timezone",
  "date": "YYYY-MM-DD",
  "artifact": {"path": "absolute HTML path", "sha256": "64 hexadecimal characters"},
  "analysis": {"overall": "complete", "mode": "sequential", "passes": {}},
  "rewrite": {"attempted": false, "skipped": false, "timeout_s": 180, "error": null},
  "generation": "generated",
  "handoff": "handed_off",
  "delivery_confirmed": false
}
```

`generation` is `pending`, `generated`, or `failed`; `handoff` is `pending`,
`handed_off`, `saved` (file-only policy), or `failed`. `analysis`/`artifact` are
null until available. A fresh pending receipt replaces prior state before the
smoke test, and a smoke failure records failed generation. Normal duplicate
skips preserve the preceding receipt. Receipts prove generation and local
handoff only: even `MEDIA:` does not confirm transport. External launchers must
check run ID/start time, artifact path and SHA-256 before consuming a receipt,
and obtain independent transport evidence for confirmed delivery.

For verification isolation, use a separate profile with separate
`runtime.data_root`, `runtime.media_dir` and `runtime.env_file`, delivered through
`delivery.adapter: file` or the configured handoff adapter. No new CLI path
options are needed. No live-call or deployment verification is implied by the
offline tests.

## Source review and private evidence

Coverage checks establish registered-event coverage and source eligibility;
they do not establish factual correctness. For live use, both existing review
settings below are recommended. To enable bounded review of final full story
cards against captured cited sources, add these settings to the private
profile's existing `analysis` section:

```yaml
analysis:
  source_review_enabled: true
  source_review_repair_enabled: true
```

Review remains opt-in. Omitted or disabled review retains legacy model-call
behavior without this source-review barrier; normal non-dry runs may also apply
model-authored KB deltas. Repair requires review to be enabled.
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
budget hold the reviewed run before a new/replaced briefing, MEDIA handoff,
delivery, or model KB-delta application. A normal HOLD may still persist status,
raw fetch caches and private model captures; identify these writes in probe
evidence. A normal HOLD can return exit code zero, so inspect status and
artifacts rather than treating process success as accepted output. Release
validation enables both checks; disabled review cannot count as a factual pass.

The review covers full world, configured national, and finance story cards'
headlines, explanations, and bodies. It does not certify radar, threads, signal,
scoreboard data, other finance narrative, or knowledge facts. Valid evidence
quotes and a model's supported verdict reduce risk; they do not prove that all
claims were identified or correctly checked. Inspect cited reporting before
owner content acceptance. The known auxiliary-summary qualifier omission is
documented in the [release policy](experimental-alpha.md).

With source review enabled, model-authored KB delta proposals remain in private
evidence but are not applied: card review does not cover those proposals.
Deterministic state updates and daily artifact archiving continue. Existing KB
content and other unreviewed fields are not retrospectively certified.

Exact CLI stdin/schema bytes and raw outputs are private evidence, separate from
replay capsules and briefing attachments. Keep the configured data directory
outside every Git repository and restrict access to it. Never upload evidence
stores to public issues, release archives, or CI artifacts. An incomplete
evidence capture cannot qualify a release even if generation succeeds. Offline
`demo` does not invoke review.
