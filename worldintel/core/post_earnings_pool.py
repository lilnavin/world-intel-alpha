"""world-intel post-earnings pool — durable membership for filing reconciliation.

Every company that reached the brief's Post-Earnings section is recorded here so
a later run can intersect the pool with the 10-Q/10-K filings that have since
landed (design ref
`docs/superpowers/specs/2026-08-22-wi-filing-reconciliation-design.md` Sec.3.2).

Deliberately a sibling of `earnings_report`'s `earnings_pipeline.json` rather
than another key on it: that ledger dedupes candidates and drives pending
retries over 14 days, this one answers "did we cover them?" over 30. Different
lifetime, different purpose, different write point — and keeping them apart
means the reconciler can be reasoned about without touching prune logic a large
test suite already pins down.

ABSOLUTE RULE (same as `earnings_report.py`): nothing here raises out of the
module, and nothing here does I/O beyond this one file — no CIK lookup, no
network. A missing or corrupt pool degrades to empty and today's membership is
recorded over the top of it.
"""

from __future__ import annotations

import json
import os
from datetime import date

from worldintel.core import earnings_report

POOL_FILE = "post_earnings_pool.json"
POOL_WINDOW_DAYS = 30      # how long a covered company stays reconcilable; ~84%
                           # of the pool files a 10-Q/10-K inside this window


def _pool_path(root: str) -> str:
    return os.path.join(root, POOL_FILE)


def read_pool(root: str) -> dict:
    """Pool of "TICKER:report_date" -> entry (shape in Sec.3.2). Missing or
    corrupt file -> {} (worst case a filing goes unread, never a crash)."""
    try:
        with open(_pool_path(root), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_pool(root: str, pool: dict) -> None:
    """Persist the pool (best-effort — never raises)."""
    try:
        os.makedirs(root, exist_ok=True)
        with open(_pool_path(root), "w", encoding="utf-8") as fh:
            json.dump(pool or {}, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _norm_key(key) -> str:
    """Upper-case the ticker half of a "TICKER:report_date" key. Callers assemble
    these from Codex prose (`writeups[].ticker`) as well as from the bundle, so a
    lower-case ticker must address the entry that already exists rather than
    minting a twin the reconciler would surface twice."""
    ticker, sep, rest = str(key or "").partition(":")
    return ticker.upper() + sep + rest


def _upper_set(tickers) -> set:
    return {str(t).upper() for t in (tickers or ()) if t}


def record(root: str, candidates: list, writeup_tickers, also_tickers,
           today: str) -> None:
    """Record every candidate that reached today's Post-Earnings section — both
    write-up cards and also-reported one-liners, since the also-reported names
    are the larger half of the pool and the population the old watch mechanism
    never looked at.

    Called where `mark_covered` is called: after a brief publishes, never on a
    `--dry` run. Best-effort — never raises."""
    try:
        cards = _upper_set(writeup_tickers)
        also = _upper_set(also_tickers)
        pool = read_pool(root)
        for cand in candidates or []:
            if not isinstance(cand, dict):
                continue
            ticker = str(cand.get("ticker") or "").upper()
            if not ticker:
                continue
            # Cards win: a ticker Codex wrote up AND listed as also-reported is
            # one company, and the card is what earns it rank in the join.
            if ticker in cards:
                origin = "card"
            elif ticker in also:
                origin = "also"
            else:
                continue
            key = f'{ticker}:{cand.get("report_date")}'
            existing = pool.get(key)
            if isinstance(existing, dict):
                # An existing entry is never rewritten. `first_seen` anchors the
                # window (refreshing it would make a name immortal), the snapshot
                # is beat 1's numbers as published, and the two *_shown dates are
                # the reconciler's state. The single legal mutation is promotion:
                # a name that was a line yesterday and a card today.
                if origin == "card":
                    existing["origin"] = "card"
                continue
            pool[key] = {
                "ticker": ticker,
                # No CIK on the candidate is normal and not worth a lookup here;
                # the reconciler skips a blank one and the entry ages out quietly.
                "cik": str(cand.get("cik") or ""),
                "name": str(cand.get("name") or ""),
                "origin": origin,
                "first_seen": today,
                # Imported, not re-listed: beat 2 recalls what beat 1 said about
                # the quarter, so the two must carry the same fields or the
                # recall line silently loses one.
                "snapshot": {field: cand.get(field)
                             for field in earnings_report._SNAPSHOT_KEYS},
                "line_shown": None,
                "block_shown": None,
            }
        write_pool(root, prune(pool, today))
    except Exception:
        pass


def mark_shown(root: str, line_keys, block_keys, today: str) -> None:
    """Stamp today onto the entries the brief just rendered. Best-effort —
    never raises."""
    try:
        pool = read_pool(root)
        for key in (line_keys or ()):
            entry = pool.get(_norm_key(key))
            # First sighting only. A match that lines again tomorrow is the same
            # deferral still waiting; overwriting the date would erase how long
            # it has been waiting, which is exactly what the age bonus reads.
            if isinstance(entry, dict) and not entry.get("line_shown"):
                entry["line_shown"] = today
        for key in (block_keys or ()):
            entry = pool.get(_norm_key(key))
            if isinstance(entry, dict):
                entry["block_shown"] = today
        write_pool(root, prune(pool, today))
    except Exception:
        pass


def prune(pool: dict, today: str) -> dict:
    """The entries still inside POOL_WINDOW_DAYS, as a new dict.

    An unparseable `first_seen` is DROPPED, not kept. The mechanism this
    replaces kept such entries — its age comparison simply could never fire on
    them — so a single bad date made an entry immortal and it accumulated
    forever in a file nobody reads."""
    ref = earnings_report._parse_day(today) or date.today()
    kept = {}
    for key, entry in (pool or {}).items():
        if not isinstance(entry, dict):
            continue
        first_seen = earnings_report._parse_day(entry.get("first_seen"))
        if first_seen is None:
            continue
        if (ref - first_seen).days <= POOL_WINDOW_DAYS:
            kept[key] = entry
    return kept
