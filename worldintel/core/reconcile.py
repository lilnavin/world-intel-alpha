"""world-intel filing reconciliation — the join between who we covered and who filed.

The old beat-2 mechanism polled forward: it opened a watch per covered company
and asked SEC every morning whether that company had filed yet. It published one
company a day against 203 eligible filings a month. This module implements the
inversion described in
`docs/superpowers/specs/2026-08-22-wi-filing-reconciliation-design.md`: the
filing index is fetched wholesale, the Post-Earnings pool is intersected with it,
and the difference is surfaced.

Three decisions live here, in order:

  `match`         — which covered companies filed inside the window
  `rank`          — which of those are worth READING (the proxy score)
  `content_score` — what the filing turned out to be worth, once read
  `select`        — which of those earn a full block today

The split between `rank` and `select` is the point of the design. The proxy is
built from beat-1 numbers, which say how material the *quarter* was; it can only
guess at the filing. So it decides who gets read, and the read itself decides who
gets depth. Nothing here judges a filing by its ticker.

Pure and offline: no network, no file I/O, no clock. Every function takes the
date it needs as an argument so a brief is reproducible from its inputs alone.
Import-safe. Standard library only.

This module deliberately does NOT import `filing_index` or `post_earnings_pool`.
It consumes their plain dict shapes and nothing else, so the join can be reasoned
about — and table-tested — without either of them existing.
"""

from __future__ import annotations

from datetime import datetime

# --------------------------------------------------------------------------- #
# Tuning constants (design §10)
# --------------------------------------------------------------------------- #
BLOCK_CAP = 6              # full blocks published per brief
SCREEN_MAX = 12            # filings READ to judge materiality before promoting.
                           # Raised from 8 on 2026-08-30 into the ~55s the
                           # retired ownership lane freed. Measured cost is
                           # ~4-7.6s each (the `filing_text` timing over 8 was
                           # 32.6-60.8s across the review week), so 12 adds
                           # ~16-30s. `screen_deadline` and
                           # PROMOTION_ELAPSED_GUARD_S remain the real guard.
                           # Reading one is not the ~1s the design assumed: the
                           # daily index carries only the full-submission path,
                           # so resolving the primary document means a
                           # build_fundamentals call first. Still far cheaper
                           # than the 22 the forward watch spent, and every one
                           # of these is a company whose filing is known to
                           # exist rather than one that may never file at all.
AGE_BONUS_PER_DAY = 0.5    # ~ one third of a card-origin bonus per day deferred
AGE_BONUS_CAP = 1.0        # ...but the term STOPS at two days' worth.
                           # Uncapped, this WAS the ranking. Measured
                           # 2026-08-30: the entire top-8 read set scored an
                           # identical 6.00 from origin + age, with no surprise
                           # and no reaction on any of them, so the real
                           # tiebreak was filing date then ticker ALPHABETICALLY
                           # -- and NVDA (+3.8% surprise, +8.74% reaction)
                           # scored 4.63, ranked 11th, and was never read while
                           # Nordson's 10-Q was. 183 of 220 pool entries carry
                           # no snapshot numbers at all, so for most of the pool
                           # materiality was absent from the score rather than
                           # merely outweighed. Deliberately BELOW
                           # CARD_ORIGIN_BONUS: waiting may reorder equals, it
                           # may not out-earn a real signal.
PROMOTION_ELAPSED_GUARD_S = 420  # past this many seconds into the run, promote fewer
PROMOTION_FLOOR = 2        # ...but never below this many

# Copied from `earnings_report._blend_score`, deliberately, not imported — the
# same call `anomaly_panel` made about sec_fundamentals' selection law. This
# module must stay stdlib-only and I/O-free, and `earnings_report` pulls in
# `requests` and the whole fetch stack at import time. `test_reconcile.py`
# asserts these three still agree with their originals, so a drift is a test
# failure rather than a silent divergence in how two rankers read the same
# snapshot.
ABS_EPS_FLOOR = 0.10
SURPRISE_SCORE_CAP = 100.0
REACTION_SCORE_CAP = 50.0

CARD_ORIGIN_BONUS = 2.0    # a write-up card outranks an also-reported line
SIZE_BONUS = 2.0           # ...and an index member outranks a micro cap.
                           # `_blend_score`'s FIRST term, restored. This
                           # module's docstring has always claimed proxy_score
                           # is "deliberately the same shape as
                           # earnings_report._blend_score"; it was not -- the
                           # size term had been REPLACED by the origin term
                           # rather than joined by it, which is why a week of
                           # briefs read Strattec and Tanger while NVDA, WMT,
                           # CRWD and MRVL sat in "Also filed". The universe is
                           # INJECTED, not imported: see proxy_score.

# Content weights. Segments are worth more than every other signal combined
# (max 1.5 + 0.25): a parsed segment table is the one thing a block can show that
# no other lane produces, and `sec_filing_text.parse_segments` returns [] rather
# than a guess, so a non-empty list is already evidence rather than a hint.
SEGMENT_SCORE = 3.0
DISCLOSURE_SCORE = 0.5     # per disclosure found...
DISCLOSURE_COUNT_CAP = 3   # ...clamped, so a boilerplate-heavy filer can't sweep
SECTION_SCORE = 0.25       # deliberately below ONE disclosure: `slice_sections`
                           # is best-effort and may hand back a cross-reference
                           # fragment, so sliced prose is the faintest evidence
                           # a screen can return — a place to look, not a finding


# --------------------------------------------------------------------------- #
# Small tolerant helpers
# --------------------------------------------------------------------------- #
def _to_float(value):
    """Same tolerance as `earnings_report._to_float`. A snapshot field can be
    absent, None, or a string no upstream source ever cleaned; none of those may
    reach an `abs()` and take the whole brief down."""
    try:
        if value is None or value in ("", "."):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_day(value):
    """'YYYY-MM-DD' -> date, or None if unparseable (None included)."""
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _count(value) -> int:
    """len() of a list/dict/tuple, 0 for anything without one."""
    try:
        return len(value)
    except TypeError:
        return 0


def _norm_cik(value) -> str:
    """CIKs are compared zero-stripped, matching `_load_cik_map`'s form. Both
    sides are normalized rather than trusting either: a zero-padded CIK on one
    side of the join fails silently and looks exactly like a company that never
    filed."""
    return str(value or "").strip().lstrip("0")


def _days_deferred(line_shown, today) -> int:
    """Calendar days from the day this filing first showed as a line to `today`.

    Zero when `line_shown` is null: an unseen match has not been deferred, it has
    not been *seen*, and paying it an age bonus on day one would let brand-new
    also-reported names jump the queue they were meant to wait in. Clamped at
    zero so a pool entry stamped in the future cannot pay a negative bonus."""
    seen, ref = _parse_day(line_shown), _parse_day(today)
    if seen is None or ref is None:
        return 0
    return max((ref - seen).days, 0)


def _stored_proxy(entry: dict) -> float:
    """The proxy `match` already attached, or 0.0. Used where no date is in hand."""
    val = _to_float(entry.get("proxy"))
    return val if val is not None else 0.0


def _neg_date(filed) -> str:
    """A sort key that orders ISO dates NEWEST first inside an otherwise
    ascending tuple. Dates are strings, so they cannot simply be negated;
    complementing each digit reverses their order without parsing, and anything
    unparseable sorts last rather than crashing the ranking."""
    text = str(filed or "")
    if len(text) != 10 or not text.replace("-", "").isdigit():
        return "~"  # sorts after every digit, so junk goes to the back
    return "".join(str(9 - int(c)) if c.isdigit() else c for c in text)


def _proxy_of(entry: dict, today: str) -> float:
    """The attached proxy if there is one, else compute it. Ranking must never
    silently reorder against a value the caller adjusted after `match`."""
    val = _to_float(entry.get("proxy"))
    return val if val is not None else proxy_score(entry, today)


# --------------------------------------------------------------------------- #
# The join
# --------------------------------------------------------------------------- #
def match(pool: dict, index: dict, today: str, largecap=frozenset()) -> list:
    """Pool entries whose CIK appears in the filing index and whose filing has
    not already had its block.

    `pool` is `{"TICKER:report_date": {ticker, cik, name, origin, first_seen,
    snapshot, line_shown, block_shown}}`; `index` is `{cik: {form, filed,
    company, path}}`. Each result is the pool entry merged with `key`, the
    filing's `form`/`filed`/`path`, `days_deferred` and `proxy`.

    Three exclusions, each load-bearing:
      - no CIK (2 of 243 tickers, both Tokyo listings) — no join is possible
      - CIK absent from the index — the company has not filed inside the window.
        This is also why an ADR-only pool costs nothing: a 6-K filer never
        appears in a 10-Q/10-K index, so the 12 unresolvable watches that
        motivated this rewrite cannot be constructed here.
      - `block_shown` set — a filing gets exactly one block, ever. Without this
        the window would re-surface the same 10-K every morning for 30 days.

    Deterministic in pool-key order; `rank` imposes the material order. Pure."""
    by_cik = {}
    for cik, filing in (index or {}).items():
        norm = _norm_cik(cik)
        if norm and isinstance(filing, dict):
            by_cik[norm] = filing

    out = []
    for key in sorted((pool or {}).keys()):
        entry = (pool or {}).get(key)
        if not isinstance(entry, dict) or entry.get("block_shown"):
            continue
        filing = by_cik.get(_norm_cik(entry.get("cik")))
        if filing is None:
            continue
        merged = dict(entry)
        merged["key"] = key
        merged["form"] = filing.get("form") or ""
        merged["filed"] = filing.get("filed") or ""
        merged["path"] = filing.get("path") or ""
        merged["days_deferred"] = _days_deferred(entry.get("line_shown"), today)
        merged["proxy"] = proxy_score(merged, today, largecap)
        out.append(merged)
    return out


def expired_unread(pool: dict, index: dict) -> list:
    """Pool keys that were shown as a line, never got a block, and whose CIK has
    left the filing index — the silent half of the backlog drain.

    `filing_index.load_window` keeps a 20-day window. When a filing ages off the
    back its CIK leaves the index, `match` stops returning it, and it disappears
    with no error and no trace: a company we covered, whose filing we listed,
    that nobody ever read. Measured 2026-08-30 the pool held 220 entries — 54
    published, 81 still matchable, 85 gone, and **43 of those had been seen as a
    line and then lost**, MSFT, AMZN, META and COIN among them. Against a 6/day
    publication rate the backlog drained ~11/day, so roughly HALF the drain was
    expiry rather than coverage, and no series showed it.

    `line_shown` is the qualifier that makes this a LEAK rather than a
    non-event: an entry never shown was never queued, and a `block_shown` entry
    left correctly. Counting either would bury the signal.

    An entry with NO CIK is excluded, the same exclusion `match` makes and for
    the same reason: no join is possible, so it never expired — it was never
    matchable. This is not hypothetical. The pool PERSISTS an empty `cik` for
    most entries (NVDA, CRWD, MRVL and SNPS all carry `""` on disk); `run.py`
    fills them from the SEC ticker map at runtime, in place, before calling
    this, and never writes them back. Without this guard a failed ticker-map
    fetch would not degrade the counter, it would INVERT it — every unresolved
    name reported as a lost filing, and the quiet day indistinguishable from the
    catastrophe.

    Sorted for determinism; a malformed entry is skipped rather than raising —
    this feeds a daily counter and may never take the brief down. Pure."""
    in_window = {_norm_cik(cik) for cik in (index or {})}
    out = []
    for key, entry in (pool or {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("block_shown") or not entry.get("line_shown"):
            continue
        cik = _norm_cik(entry.get("cik"))
        if not cik:
            continue
        if cik not in in_window:
            out.append(key)
    return sorted(out)


def proxy_score(entry: dict, today: str, largecap=frozenset()) -> float:
    """How likely this filing is to be worth reading — NOT how deep it gets.

    Deliberately the same shape as `earnings_report._blend_score`, which the
    codebase already uses for exactly this kind of triage:

        2.0  if origin == "card"
          + 2.0 if ticker in largecap
          + min(|surprise_pct|, SURPRISE_SCORE_CAP) / 10   when |eps_est| >= ABS_EPS_FLOOR
          + min(|price_move_pct|, REACTION_SCORE_CAP) / 5
          + min(AGE_BONUS_PER_DAY * days_deferred, AGE_BONUS_CAP)

    `largecap` is `earnings_report.LARGECAP_INDEX_SET`, passed in rather than
    imported: this module is stdlib-only and I/O-free (earnings_report pulls
    `requests` and the whole fetch stack at import time), and a second copy of a
    198-ticker list refreshed quarterly would drift from the one the rest of the
    pipeline ranks on. The empty default is inert, so a caller that has not been
    updated scores exactly as it did before rather than silently losing every
    index member. Restoring this term is the point: `_blend_score` opens with it
    and this function had swapped it for the origin term.

    The `eps_est` floor is `_blend_score`'s, for `_blend_score`'s reason: off a
    $0.005 estimate a penny beat reads as a 5000% "surprise" worth 500 points,
    which is how Microsoft and Meta ranked 96th and 87th on 2026-07-30. Both
    market terms stay clamped so no single input can own the ordering.

    The age term is not decoration. Without it a moderately material filing is
    outranked by fresher arrivals every single morning and starves permanently —
    a slower, quieter version of the bug this whole module exists to fix. It is
    the only reason carry-over is a queue rather than a graveyard. But it is
    CAPPED, because unbounded it became the whole ranking: see AGE_BONUS_CAP.

    Reads the numbers from `entry["snapshot"]`, tolerating a missing, None or
    non-numeric value on every one of them. Pure."""
    snapshot = entry.get("snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {}

    score = CARD_ORIGIN_BONUS if entry.get("origin") == "card" else 0.0
    if str(entry.get("ticker") or "").upper() in (largecap or frozenset()):
        score += SIZE_BONUS

    surprise = _to_float(snapshot.get("surprise_pct"))
    est = _to_float(snapshot.get("eps_est"))
    if surprise is not None and est is not None and abs(est) >= ABS_EPS_FLOOR:
        score += min(abs(surprise), SURPRISE_SCORE_CAP) / 10.0

    move = _to_float(snapshot.get("price_move_pct"))
    if move is not None:
        score += min(abs(move), REACTION_SCORE_CAP) / 5.0

    return score + min(
        AGE_BONUS_PER_DAY * _days_deferred(entry.get("line_shown"), today),
        AGE_BONUS_CAP)


def rank(matches: list, today: str) -> list:
    """`matches` most material first — the read order, capped by SCREEN_MAX.

    Ties break on the FILING DATE, most recent first, and only then on ticker.
    Recency is not decoration here. A pool entry whose beat-1 snapshot is empty
    scores on origin alone, so large numbers of entries tie exactly — every
    backfilled entry does, and so does any candidate whose surprise and reaction
    were never resolved. Ties broken on ticker alone are ties broken by the
    alphabet: measured against the seeded pool, the eight filings read on day
    one were AAP, AIRO, AMAT, AMKR, AMZN, APLD, AREN and ARX, and Microsoft's
    10-K sat behind the letter M. Recency is a real signal available for every
    match, and this is a daily news product.

    Ticker remains the final tiebreak so the same pool and index always produce
    the same brief regardless of dict ordering upstream. Returns a new list.
    Pure."""
    return sorted(
        matches or [],
        key=lambda m: (-_proxy_of(m, today),
                       _neg_date(m.get("filed")),
                       str(m.get("ticker") or "")),
    )


# --------------------------------------------------------------------------- #
# What the filing turned out to contain
# --------------------------------------------------------------------------- #
def content_score(screened: dict) -> float:
    """What this filing is worth as a block, judged on what reading it produced.

    Expects the keys the caller attaches after the read: `segments` (list, from
    `sec_filing_text.parse_segments`), `disclosures` (list/dict, from
    `find_disclosures`) and `filing_sections` (dict, from `slice_sections`).
    Anything absent or the wrong type simply scores nothing.

    A filing that yielded none of the three scores 0.0 and — by `select`'s rule —
    is never promoted. That is the whole content tier in one sentence: an empty
    filing gets a line, not a block."""
    if not isinstance(screened, dict):
        return 0.0
    score = SEGMENT_SCORE if _count(screened.get("segments")) else 0.0
    score += DISCLOSURE_SCORE * min(_count(screened.get("disclosures")),
                                    DISCLOSURE_COUNT_CAP)
    if _count(screened.get("filing_sections")):
        score += SECTION_SCORE
    return score


def select(screened: list, cap: int = BLOCK_CAP,
           elapsed_s: float | None = None) -> tuple:
    """`(promoted, deferred)` — who gets a full block today.

    `select` decides among WHAT IT IS GIVEN. The caller screens only the top
    `SCREEN_MAX` of `rank`, so `deferred` here is the unpromoted part of the
    screened subset; every match that was never screened is deferred too and the
    caller must add it to the line tier itself. This function has no way to know
    such matches exist.

    Ordering is content first, proxy second, ticker last — a low-proxy filing
    that parsed a segment table beats a high-proxy one that yielded nothing,
    which is the difference between content-tiered and proxy-tiered output.
    Anything scoring 0.0 is dropped from promotion outright.

    `elapsed_s` is seconds into the run, or None to disable the guard. Past
    `PROMOTION_ELAPSED_GUARD_S` the cap collapses to `PROMOTION_FLOOR`, because
    the 600s cron ceiling is a hard kill, not a degrade: `subprocess.run` kills
    the process and the runner discards stdout, and delivery reads the `MEDIA:`
    line off stdout — so an overrun loses the ENTIRE briefing, world and Japan
    tabs included, to buy a seventh filing block. Carry-over makes that trade
    free: a filing deferred by this guard is still inside the window tomorrow
    and simply returns, one day older and ranked higher for it. Pure."""
    limit = max(int(cap), 0)
    elapsed = _to_float(elapsed_s)
    if elapsed is not None and elapsed > PROMOTION_ELAPSED_GUARD_S:
        # min(), not assignment: a caller that already asked for fewer than the
        # floor asked for a reason, and the guard only ever promotes fewer.
        limit = min(limit, PROMOTION_FLOOR)

    scored = [(content_score(entry), _stored_proxy(entry),
               str(entry.get("ticker") or ""), entry)
              for entry in (screened or []) if isinstance(entry, dict)]
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]))

    promoted = [row[3] for row in scored if row[0] > 0.0][:limit]
    taken = {id(entry) for entry in promoted}
    deferred = [row[3] for row in scored if id(row[3]) not in taken]
    return promoted, deferred
