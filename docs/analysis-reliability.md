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
