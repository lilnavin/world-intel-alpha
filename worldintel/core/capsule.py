"""world-intel — replay capsule capture.

A capsule is everything needed to replay one run offline: the exact state
entering selection (L1) and the exact state entering the renderer (L2).

ABSOLUTE RULE, TWICE OVER.

1. Nothing here may raise out of the module. A capture failure logs and
   contributes nothing; it never costs a brief.

2. Nothing here may touch disk while the run's timing guards are live.
   `run.py` measures elapsed time at :516 against a 420s guard (:518,
   reconcile.py:404-407) that collapses filing promotion, and the L1 boundary
   at :343 is inside that window. So `snapshot()` serializes to memory and
   `write()` runs only after the timed section finishes. Splitting capture into
   these two halves is the entire design, not an optimization.

Capsules hold full article bodies and config-derived values. They live outside
every repository, with the directory mode set restrictively.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile

FORMAT_VERSION = 1

# Keys whose VALUES never enter a manifest fingerprint, matched case-insensitively
# as substrings: paths, addresses and anything credential-shaped.
_PERSONAL_KEY_RE = re.compile(
    r"email|root|dir|path|file|token|key|secret|chat|deliver", re.IGNORECASE)


def log(msg: str) -> None:
    print(f"[capsule] {msg}", file=sys.stderr, flush=True)


def snapshot(obj) -> bytes:
    """Serialize to memory NOW, so later mutation cannot reach the capture.

    `run.py` mutates `bundle` after the L1 boundary (`news` at :354 among
    others), so holding a reference would capture the wrong state and a
    deepcopy would cost more than these bytes.
    """
    return json.dumps(obj, ensure_ascii=False, default=str,
                      sort_keys=True).encode("utf-8")


def capsule_id(started_at: str, run_hash: str) -> str:
    """`2026-09-23T071503+0900-<8 hex>`.

    Unique per run, not per day: `--force` permits several runs a day and a
    shared date directory would let them overwrite each other. The layout sorts
    chronologically as a plain string, which is what `prune` relies on.
    """
    dt = datetime.datetime.fromisoformat(started_at)
    return (f"{dt.strftime('%Y-%m-%dT%H%M%S%z')}-"
            f"{hashlib.sha256(run_hash.encode('utf-8')).hexdigest()[:8]}")


def config_fingerprint(values: dict) -> dict:
    """Behaviour-bearing settings only. Personal values are dropped, not hashed:
    a hash of a short known string is not anonymous."""
    return {k: v for k, v in (values or {}).items()
            if not _PERSONAL_KEY_RE.search(str(k))}


class Pending:
    """Snapshots plus manifest facts, held in memory until the run is past its
    timing guards."""

    def __init__(self, started_at: str, mode: str, source_commit: str = "",
                 source_dirty: bool = False, config: dict | None = None):
        self.started_at = started_at
        self.mode = mode                      # scheduled | forced | synthetic
        self.source_commit = source_commit
        # A commit id describes the last COMMIT, not the code that ran. Without
        # this flag a capsule taken from a dirty tree claims provenance it does
        # not have, and Plan 1 replays it against source that never produced it.
        self.source_dirty = bool(source_dirty)
        self.config = config_fingerprint(config or {})
        self.blobs: dict[str, bytes] = {}
        self.facts: dict = {}
        self.errors: list[str] = []

    def add(self, name: str, blob: bytes) -> None:
        self.blobs[f"{name}.json"] = blob

    def note(self, **facts) -> None:
        self.facts.update(facts)

    def fail(self, stage: str, exc: Exception) -> None:
        self.errors.append(f"{stage}: {exc}")


def _write_file(dest: str, name: str, blob: bytes) -> dict:
    path = os.path.join(dest, name)
    with open(path, "wb") as fh:
        fh.write(blob)
    return {"bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}


def write(pending: Pending, store_root: str):
    """Write the whole capsule atomically. Returns its path, or None.

    Atomic via a sibling temp directory and a single rename: a replay must never
    find a half-written capsule and treat it as a fixture.
    """
    tmp = None
    try:
        os.makedirs(store_root, exist_ok=True)
        name = capsule_id(pending.started_at,
                          pending.source_commit + str(pending.source_dirty)
                          + pending.started_at
                          + str(sorted(pending.facts.items())))
        final = os.path.join(store_root, name)
        tmp = tempfile.mkdtemp(prefix=".partial-", dir=store_root)

        files = {}
        for fname, blob in sorted(pending.blobs.items()):
            files[fname] = _write_file(tmp, fname, blob)

        manifest = {
            "format_version": FORMAT_VERSION,
            "capsule_id": name,
            "started_at": pending.started_at,
            "mode": pending.mode,
            "source_commit": pending.source_commit,
            "source_dirty": pending.source_dirty,
            "config": pending.config,
            # Semantic anchors, never line numbers. A capsule outlives the
            # source layout: these two were written as run.py:343 and :647,
            # were already stale when written, and the capture wiring moved
            # them again the same day. A replay harness needs to know WHICH
            # boundary a blob came from, which the callee names exactly and a
            # coordinate only approximates until the next edit.
            "boundaries": {
                "selection-input.json": "before select_news.select_candidates",
                "render-input.json": "before compose_html.render_html",
            },
            "files": files,
            "facts": pending.facts,
            "errors": pending.errors,
        }
        _write_file(tmp, "manifest.json",
                    json.dumps(manifest, indent=2,
                               ensure_ascii=False).encode("utf-8"))

        if os.name != "nt":
            os.chmod(tmp, stat.S_IRWXU)       # 0700
        os.rename(tmp, final)
        tmp = None
        log(f"wrote {name} ({sum(f['bytes'] for f in files.values())} bytes)")
        return final
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        log(f"write failed (non-fatal): {exc}")
        return None
    finally:
        if tmp and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


def prune(store_root: str, keep: int) -> int:
    """Keep the newest `keep` capsules. Names sort chronologically by construction."""
    try:
        names = sorted(d for d in os.listdir(store_root)
                       if os.path.isdir(os.path.join(store_root, d))
                       and not d.startswith(".partial-"))
        removed = 0
        for name in names[:max(0, len(names) - keep)]:
            shutil.rmtree(os.path.join(store_root, name), ignore_errors=True)
            removed += 1
        return removed
    except Exception as exc:  # noqa: BLE001
        log(f"prune failed (non-fatal): {exc}")
        return 0
