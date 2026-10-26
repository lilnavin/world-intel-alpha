"""Filing index — the rolling 10-Q/10-K stream the reconciler joins against.

Reads EDGAR's daily form index for every day in a `RECONCILE_WINDOW_DAYS`
window, keeps the periodic filings, and caches each day it managed to fetch in
`filing_index.json` under the knowledge-base root. `load_window` returns the
merged `{cik: filing}` across the window — the filing side of the reconcile
join (design ref
`docs/superpowers/specs/2026-08-22-wi-filing-reconciliation-design.md` §3.1).

The parser is `ownership.parse_daily_index`, imported rather than
reimplemented: it already splits on runs of two-plus spaces instead of
fixed-width slicing, which is what keeps `SCHEDULE 13D/A` out of the
`SCHEDULE 13D` bucket. `10-Q` and `10-K` are short enough that slicing would
work today, which is exactly the kind of accident that stops being true later.
This module's own job is only the window cache and the periodic-form filter.

ABSOLUTE RULE (same house rule as ownership.py / sec_fundamentals.py): no
external call may raise out of this module. A day that cannot be fetched
degrades to `None` plus a health entry, and the rest of the window stays
usable.
"""

from __future__ import annotations

from worldintel import config as wi_config
import json
import os
import time
from datetime import datetime, timedelta

from worldintel.core import ownership

# --------------------------------------------------------------------------- #
# Tuning constants
# --------------------------------------------------------------------------- #
RECONCILE_WINDOW_DAYS = 30
INDEX_FILE = "filing_index.json"
PERIODIC_FORMS = frozenset({"10-K", "10-Q"})

# Whole-lane wall-clock budget, compared against _now() (time.monotonic()),
# NOT time.time() -- `deadline_at` throughout this module is a monotonic
# instant. A normal morning fetches 1-3 uncached days at ~0.3s each, so this is
# generous; it exists so a stalled EDGAR cannot spend the run's budget here.
INDEX_LANE_DEADLINE_S = 30.0

# Local copies, deliberately not imported from the sibling SEC lanes: the five
# lanes share one IP and a block takes all five down, so each states its own
# contact UA and its own courtesy spacing.
def _sec_ua() -> str:
    return wi_config.get().sec_user_agent()
_HTTP_TIMEOUT = 25
REQUEST_PACING_S = 0.12


def _short(exc: Exception) -> str:
    """A short, single-line reason string for health notes (same shape as the
    sibling modules' `_short`)."""
    msg = str(exc).split("\n")[0].strip()
    return f"{type(exc).__name__}: {msg}"[:120] if msg else type(exc).__name__


def _now() -> float:
    """Monotonic clock for deadline checks (mirrors `sec_fundamentals._now()`).
    A seam so deadline behaviour is testable without sleeping."""
    return time.monotonic()


# --------------------------------------------------------------------------- #
# The window cache
# --------------------------------------------------------------------------- #
def _index_path(root: str) -> str:
    return os.path.join(root, INDEX_FILE)


def read_index(root: str) -> dict:
    """The cached window: `{"YYYY-MM-DD": {cik: filing}}`. Missing or corrupt
    file -> `{}` (worst case a run re-fetches the window, never crashes)."""
    try:
        with open(_index_path(root), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except wi_config.ConfigError:
        raise
    except Exception:
        return {}


def write_index(root: str, data: dict) -> None:
    """Persist the window cache (best-effort — never raises)."""
    try:
        os.makedirs(root, exist_ok=True)
        with open(_index_path(root), "w", encoding="utf-8") as fh:
            json.dump(data or {}, fh, ensure_ascii=False, indent=2)
    except wi_config.ConfigError:
        raise
    except Exception:
        pass


def _window_days(today: str) -> list[str]:
    """The `RECONCILE_WINDOW_DAYS` calendar days ending at `today` inclusive,
    newest first. `[]` when `today` is not a parseable 'YYYY-MM-DD'."""
    try:
        end = datetime.strptime(str(today)[:10], "%Y-%m-%d").date()
    except wi_config.ConfigError:
        raise
    except Exception:
        return []
    days = wi_config.get().tuning.get("retention_days", {}).get("filing_index", RECONCILE_WINDOW_DAYS)
    return [(end - timedelta(days=i)).isoformat() for i in range(days)]


def _is_weekend(day: str) -> bool:
    try:
        return datetime.strptime(day, "%Y-%m-%d").date().weekday() >= 5
    except wi_config.ConfigError:
        raise
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Network layer
# --------------------------------------------------------------------------- #
def fetch_day(day: str, session, health: dict, deadline_at: float) -> dict | None:
    """One day's periodic filings as `{cik: {"form","filed","company","path"}}`,
    or `None` when the day could not be fetched.

    The `None`/`{}` distinction is the whole correctness story of the cache.
    `{}` is a real answer — that day published an index and it held no 10-Q or
    10-K — and is worth caching forever. `None` means 404, timeout, network
    error or an exhausted lane budget, and the caller must NOT cache it: the
    daily index publishes around 22:00 ET (~11:00 JST), after the 07:00 JST
    run, so the newest day or two 404s at brief time every single day and has
    to be retried tomorrow. Caching a miss would put permanent holes exactly
    where the freshest filings are.

    CIKs are left-stripped of zeros to match `_load_cik_map`'s form, which is
    what the pool side of the join is keyed by. Never raises."""
    if _now() >= deadline_at:
        health["filing_index"] = {"ok": False, "note": f"{day}: deadline"}
        return None
    try:
        resp = session.get(
            ownership._daily_index_url(day), headers={"User-Agent": _sec_ua()},
            timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        rows = ownership.parse_daily_index(resp.text)
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        # Superseded by load_window's summary on the normal path; it stands on
        # its own for a direct caller, per the health-entry house rule.
        health["filing_index"] = {"ok": False, "note": f"{day}: {_short(exc)}"}
        return None

    filings: dict = {}
    for row in rows:
        if row.get("form") not in PERIODIC_FORMS:
            continue
        cik = str(row.get("cik") or "").lstrip("0")
        if not cik:
            continue
        filings[cik] = {
            "form": row.get("form", ""),
            "filed": row.get("filed", ""),
            "company": row.get("company", ""),
            "path": row.get("path", ""),
        }
    return filings


def load_window(root: str, today: str, session, health: dict,
                deadline_at: float | None = None) -> dict:
    """The merged `{cik: filing}` across the window ending at `today`.

    Drops cached days that have aged out, fetches only the in-window days that
    are not already cached successfully, and never caches a failure. Weekends
    are skipped outright — EDGAR publishes no index for them, so a request
    would buy a guaranteed 404. Past `deadline_at` (default
    `_now() + INDEX_LANE_DEADLINE_S`) fetching stops but everything already
    cached is still returned. Never raises."""
    if deadline_at is None:
        deadline_at = _now() + wi_config.get().tuning.get("deadlines_s", {}).get(
            "filing_index", INDEX_LANE_DEADLINE_S)

    days = _window_days(today)
    if not days:
        health["filing_index"] = {"ok": False, "note": f"unusable date {today!r}"}
        return {}

    keep = set(days)
    cache = {day: rows for day, rows in read_index(root).items()
             if day in keep and isinstance(rows, dict)}

    fetched = 0
    missed = 0
    requests_made = 0
    for day in days:
        if day in cache or _is_weekend(day):
            continue
        if _now() >= deadline_at:
            break
        if requests_made:
            time.sleep(REQUEST_PACING_S)
        requests_made += 1
        rows = fetch_day(day, session, health, deadline_at)
        if rows is None:
            # Deliberately not cached, so the next run retries this day.
            missed += 1
            continue
        cache[day] = rows
        fetched += 1

    write_index(root, cache)

    merged: dict = {}
    for day in sorted(cache):  # oldest first, so a later filing overwrites
        for cik, filing in (cache[day] or {}).items():
            if not isinstance(filing, dict):
                continue
            prior = merged.get(cik)
            if prior is None or str(filing.get("filed") or "") >= str(prior.get("filed") or ""):
                merged[cik] = filing

    note = f"{len(cache)} days cached, {fetched} fetched"
    if missed:
        note += f", {missed} unavailable"
    # A miss is the expected state of the newest day or two, so it does not by
    # itself make the lane unhealthy; an empty window does.
    #
    # The counts ride alongside the note as real numbers, not only inside it:
    # `metrics` wants them, and a metric recovered by regexing an English
    # sentence breaks the first time the sentence is reworded.
    health["filing_index"] = {"ok": bool(cache), "note": note,
                              "days_cached": len(cache), "days_fetched": fetched,
                              "days_unavailable": missed}
    return merged
