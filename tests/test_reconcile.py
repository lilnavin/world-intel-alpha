"""Tests for the filing-reconciliation join (scripts/reconcile.py).

Pure unit tests: no tmp dirs, no fixtures, no network, no clock. Every function
under test takes its date as an argument, so every case here is a table.

The module is stdlib-only by design and therefore COPIES three constants out of
`earnings_report._blend_score` instead of importing them. `test_score_floors_
still_agree_with_earnings_report` is what keeps that copy honest.
"""

import os
import sys

# Make scripts/ importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from worldintel.core import earnings_report  # noqa: E402
from worldintel.core import reconcile  # noqa: E402

TODAY = "2026-08-22"


def _entry(**kw):
    """A pool entry: `{"TICKER:report_date": {...}}`'s value half."""
    base = {
        "ticker": "DE", "cik": "315189", "name": "Deere & Co",
        "origin": "also", "first_seen": "2026-08-21",
        "snapshot": {"eps_est": 5.00, "eps_act": 5.50, "surprise_pct": 10.0,
                     "price_move_pct": 2.0},
        "line_shown": None, "block_shown": None,
    }
    base.update(kw)
    return base


def _pool(*entries):
    return {"%s:2026-08-20" % e["ticker"]: e for e in entries}


def _filing(**kw):
    base = {"form": "10-Q", "filed": "2026-08-21", "company": "DEERE & CO",
            "path": "edgar/data/315189/0000315189-26-000042.txt"}
    base.update(kw)
    return base


def _screened(ticker, proxy, **kw):
    """A match after the caller read its filing."""
    base = {"ticker": ticker, "proxy": proxy, "key": "%s:2026-08-20" % ticker,
            "segments": [], "disclosures": [], "filing_sections": {}}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# match — the join itself
# --------------------------------------------------------------------------- #
def test_match_joins_pool_entry_to_its_filing():
    pool = _pool(_entry(ticker="DE", cik="315189"))
    out = reconcile.match(pool, {"315189": _filing()}, TODAY)
    assert len(out) == 1
    assert out[0]["key"] == "DE:2026-08-20"
    assert out[0]["ticker"] == "DE"
    assert out[0]["form"] == "10-Q"
    assert out[0]["filed"] == "2026-08-21"
    assert out[0]["path"].endswith("0000315189-26-000042.txt")
    assert out[0]["days_deferred"] == 0
    assert isinstance(out[0]["proxy"], float)


def test_match_skips_a_cik_absent_from_the_index():
    """No filing in the window means no match — nothing is invented for it."""
    pool = _pool(_entry(ticker="DE", cik="315189"),
                 _entry(ticker="COST", cik="909832"))
    out = reconcile.match(pool, {"315189": _filing()}, TODAY)
    assert [m["ticker"] for m in out] == ["DE"]


def test_match_never_resurfaces_a_filing_that_already_had_its_block():
    """A filing gets exactly one block, ever. Without this the 30-day window
    would re-publish the same 10-K every morning for a month."""
    pool = _pool(_entry(ticker="DE", block_shown="2026-08-21"))
    assert reconcile.match(pool, {"315189": _filing()}, TODAY) == []


def test_match_ignores_an_entry_with_no_cik_and_does_not_raise():
    """2 of 243 tickers resolve no CIK (both Tokyo listings). They age out
    quietly rather than joining against a blank index key."""
    pool = _pool(_entry(ticker="7203", cik=""))
    assert reconcile.match(pool, {"": _filing(), "315189": _filing()}, TODAY) == []


def test_match_tolerates_a_zero_padded_cik_on_either_side():
    """A padded CIK on one side of the join fails silently and looks exactly
    like a company that never filed."""
    pool = _pool(_entry(ticker="DE", cik="0000315189"))
    assert len(reconcile.match(pool, {"315189": _filing()}, TODAY)) == 1
    pool = _pool(_entry(ticker="DE", cik="315189"))
    assert len(reconcile.match(pool, {"0000315189": _filing()}, TODAY)) == 1


def test_match_counts_days_deferred_from_line_shown():
    pool = _pool(_entry(ticker="DE", line_shown="2026-08-17"))
    out = reconcile.match(pool, {"315189": _filing()}, TODAY)
    assert out[0]["days_deferred"] == 5


def test_match_on_empty_inputs_returns_empty():
    assert reconcile.match({}, {}, TODAY) == []
    assert reconcile.match(None, None, TODAY) == []


def test_adr_only_pool_produces_zero_matches():
    """The regression test for the 12 wasted watches: a 6-K/20-F filer never
    appears in a 10-Q/10-K index, so it cannot cost a request. It passes by
    construction, which is the point."""
    pool = {"SAP:2026-08-20": _entry(ticker="SAP", cik="1000184"),
            "NVO:2026-08-20": _entry(ticker="NVO", cik="353278")}
    index = {"315189": _filing(), "789019": _filing(form="10-K")}
    assert reconcile.match(pool, index, TODAY) == []


# --------------------------------------------------------------------------- #
# proxy_score — who gets READ
# --------------------------------------------------------------------------- #
def test_card_origin_outranks_also_reported_at_equal_surprise_and_reaction():
    snap = {"eps_est": 5.00, "surprise_pct": 10.0, "price_move_pct": 2.0}
    card = reconcile.proxy_score(_entry(origin="card", snapshot=snap), TODAY)
    also = reconcile.proxy_score(_entry(origin="also", snapshot=snap), TODAY)
    assert card - also == reconcile.CARD_ORIGIN_BONUS


def test_a_match_deferred_five_days_outranks_an_equally_material_fresh_one():
    """THE STARVATION GUARD — the single most important behaviour in this file.

    Without the age term a moderately material filing is outranked by fresher
    arrivals every single morning and never gets read: a slower, quieter version
    of the bug this module exists to fix. It is also the only reason carry-over
    is a queue rather than a graveyard.
    """
    snap = {"eps_est": 5.00, "surprise_pct": 10.0, "price_move_pct": 2.0}
    deferred = _entry(ticker="AAA", snapshot=snap, line_shown="2026-08-17")
    fresh = _entry(ticker="BBB", snapshot=snap, line_shown=None)

    assert reconcile.proxy_score(deferred, TODAY) > reconcile.proxy_score(fresh, TODAY)
    assert (reconcile.proxy_score(deferred, TODAY)
            - reconcile.proxy_score(fresh, TODAY)) == min(
                5 * reconcile.AGE_BONUS_PER_DAY, reconcile.AGE_BONUS_CAP)
    assert [m["ticker"] for m in reconcile.rank([fresh, deferred], TODAY)] == ["AAA", "BBB"]


def test_waiting_can_no_longer_pass_provenance():
    """INVERTED 2026-08-30, deliberately. This test used to assert the opposite
    -- that at 5 days an also-reported line (+2.5) passed a fresh card (+2.0).
    That law is what produced the measured failure: age was the only term that
    grew without bound, so after ~5 days ANY zero-information entry outranked
    every real signal. On 2026-08-30 the entire top-8 read set scored an
    identical 6.00 from origin + age with no surprise and no reaction on any of
    them, tiebroken by filing date then ticker ALPHABETICALLY.

    The cap sits below CARD_ORIGIN_BONUS so waiting reorders equals and nothing
    more. Carry-over survives as a queue -- the term still accrues below the
    cap, see test_age_bonus_still_accrues_below_the_cap -- it just cannot buy
    provenance."""
    snap = {"eps_est": 5.00, "surprise_pct": 10.0, "price_move_pct": 2.0}
    waited = _entry(ticker="AAA", origin="also", snapshot=snap,
                    line_shown="2026-08-01")   # 21 days, pinned at the cap
    fresh_card = _entry(ticker="BBB", origin="card", snapshot=snap)
    assert reconcile.proxy_score(waited, TODAY) < reconcile.proxy_score(fresh_card, TODAY)


LARGECAP = frozenset({"NVDA", "CRWD", "WMT"})


def test_age_bonus_is_capped():
    """Patience must stop accruing. Uncapped at 0.5/day a zero-information
    entry passed every real materiality signal after ~5 days: measured
    2026-08-30, the whole top-8 read set scored 6.00 from origin+age with no
    surprise and no reaction, while NVDA (+3.8% surprise, +8.74% reaction)
    scored 4.63 and ranked 11th, outside SCREEN_MAX."""
    blank = {"ticker": "NDSN", "origin": "card", "snapshot": {},
             "line_shown": "2026-08-02"}          # 20 days deferred
    assert reconcile._days_deferred(blank["line_shown"], TODAY) == 20
    assert reconcile.proxy_score(blank, TODAY) == (
        reconcile.CARD_ORIGIN_BONUS + reconcile.AGE_BONUS_CAP)


def test_age_bonus_still_accrues_below_the_cap():
    """The cap must not flatten the term into a constant -- carry-over stays a
    queue rather than a graveyard, which is the whole reason the term exists."""
    one_day = {"ticker": "AAA", "origin": "also", "snapshot": {},
               "line_shown": "2026-08-21"}
    assert reconcile.proxy_score(one_day, TODAY) == reconcile.AGE_BONUS_PER_DAY


def test_age_cap_is_below_the_card_origin_bonus():
    """A structural guarantee, not a tuning preference: no amount of waiting
    may out-earn a single real editorial signal."""
    assert reconcile.AGE_BONUS_CAP < reconcile.CARD_ORIGIN_BONUS


def test_size_bonus_applies_to_index_members():
    """proxy_score's docstring has always claimed it is 'deliberately the same
    shape as earnings_report._blend_score'. It was not: _blend_score's FIRST
    term is 2.0 for membership of LARGECAP_INDEX_SET, and proxy_score replaced
    it with an origin term instead of adding one. This restores a documented
    term rather than inventing a new one."""
    big = {"ticker": "NVDA", "origin": "also", "snapshot": {}, "line_shown": None}
    small = {"ticker": "NDSN", "origin": "also", "snapshot": {}, "line_shown": None}
    assert reconcile.proxy_score(big, TODAY, LARGECAP) == reconcile.SIZE_BONUS
    assert reconcile.proxy_score(small, TODAY, LARGECAP) == 0.0


def test_size_bonus_absent_when_no_universe_is_supplied():
    """The default must be inert, so a caller that has not been updated scores
    exactly as it did before rather than silently losing every large cap."""
    big = {"ticker": "NVDA", "origin": "also", "snapshot": {}, "line_shown": None}
    assert reconcile.proxy_score(big, TODAY) == 0.0


def test_size_bonus_matches_tickers_case_insensitively():
    """The pool stores whatever case the upstream source used; the index set is
    upper. A case mismatch fails silently and looks exactly like a small cap."""
    lower = {"ticker": "nvda", "origin": "also", "snapshot": {}, "line_shown": None}
    assert reconcile.proxy_score(lower, TODAY, LARGECAP) == reconcile.SIZE_BONUS


def test_the_2026_08_30_inversion_is_fixed():
    """The exact live case this change exists for. NDSN carried no surprise and
    no reaction and sat deferred; NVDA carried +3.8% and +8.74% and had been
    seen one day. The old score put NDSN 6.00 over NVDA 4.63, so NVDA's 10-Q
    went unread while Nordson's was read."""
    ndsn = {"ticker": "NDSN", "origin": "card", "snapshot": {},
            "line_shown": "2026-08-14"}
    nvda = {"ticker": "NVDA", "origin": "card", "line_shown": "2026-08-21",
            "snapshot": {"eps_est": 1.20, "surprise_pct": 3.8,
                         "price_move_pct": 8.74}}
    assert (reconcile.proxy_score(nvda, TODAY, LARGECAP)
            > reconcile.proxy_score(ndsn, TODAY, LARGECAP))


def test_match_forwards_the_largecap_universe_into_the_proxy():
    """match attaches `proxy` and rank sorts on what was attached -- a universe
    accepted but not forwarded scores every entry as a small cap and the whole
    change is inert."""
    pool = _pool(_entry(ticker="NVDA", cik="1045810", snapshot={}))
    index = {"1045810": _filing()}
    got = reconcile.match(pool, index, TODAY, LARGECAP)
    assert got[0]["proxy"] >= reconcile.SIZE_BONUS


def test_screen_max_reads_beyond_the_promotable_block():
    """select can only choose among what was screened, and a filing scoring 0.0
    on content is dropped from promotion outright -- so screening exactly
    BLOCK_CAP filings means one empty filing costs a block. The margin is what
    lets content, not proxy, decide depth."""
    assert reconcile.SCREEN_MAX >= reconcile.BLOCK_CAP * 2


# --------------------------------------------------------------------------- #
# expired_unread -- the silent half of the backlog drain
# --------------------------------------------------------------------------- #
def test_expired_unread_finds_the_seen_then_lost():
    """A filing shown once as a line, never promoted, whose CIK has aged out of
    the 20-day window. It is gone permanently and nothing else reports it."""
    pool = {
        "MSFT:2026-07-30": _entry(ticker="MSFT", cik="789019",
                                  line_shown="2026-08-01", block_shown=None),
        "DE:2026-08-20": _entry(ticker="DE", cik="315189",
                                line_shown="2026-08-21", block_shown=None),
    }
    index = {"315189": _filing()}          # DE still in window, MSFT is not
    assert reconcile.expired_unread(pool, index) == ["MSFT:2026-07-30"]


def test_expired_unread_ignores_the_published_and_the_never_seen():
    """A published filing left the backlog correctly; an entry never shown as a
    line was never in the queue. Neither is a leak, and counting them would
    bury the signal this metric exists for."""
    pool = {
        "AAPL:2026-07-30": _entry(ticker="AAPL", cik="320193",
                                  line_shown="2026-08-01", block_shown=True),
        "AMZN:2026-07-30": _entry(ticker="AMZN", cik="1018724",
                                  line_shown=None, block_shown=None),
    }
    assert reconcile.expired_unread(pool, {}) == []


def test_expired_unread_normalises_ciks_on_both_sides():
    """A zero-padded CIK on one side of the join fails silently and looks
    exactly like a company that never filed -- the same trap _norm_cik
    documents for match."""
    pool = {"DE:2026-08-20": _entry(cik="0000315189", line_shown="2026-08-21")}
    assert reconcile.expired_unread(pool, {"315189": _filing()}) == []


def test_expired_unread_is_sorted_and_tolerates_junk_entries():
    """Determinism: the count feeds a daily series, and a malformed pool entry
    may never take the brief down."""
    pool = {
        "ZZZ:2026-07-01": _entry(ticker="ZZZ", cik="1", line_shown="2026-07-02"),
        "AAA:2026-07-01": _entry(ticker="AAA", cik="2", line_shown="2026-07-02"),
        "JUNK:2026-07-01": "not a dict",
    }
    assert reconcile.expired_unread(pool, {}) == ["AAA:2026-07-01", "ZZZ:2026-07-01"]


def test_a_null_line_shown_pays_no_age_bonus():
    """Never seen is not the same as deferred; day-one arrivals wait their turn."""
    snap = {"eps_est": 5.00, "surprise_pct": 10.0, "price_move_pct": 2.0}
    assert reconcile.proxy_score(_entry(snapshot=snap, line_shown=None), TODAY) == \
        reconcile.proxy_score(_entry(snapshot=snap, line_shown=TODAY), TODAY)


def test_a_future_line_shown_cannot_pay_a_negative_bonus():
    snap = {"eps_est": 5.00, "surprise_pct": 10.0, "price_move_pct": 2.0}
    ahead = _entry(snapshot=snap, line_shown="2026-08-30")
    assert reconcile.proxy_score(ahead, TODAY) == \
        reconcile.proxy_score(_entry(snapshot=snap, line_shown=None), TODAY)


def test_a_huge_surprise_off_a_sub_floor_estimate_does_not_inflate_the_score():
    """The near-zero-denominator trap, inherited from `_blend_score`: off a
    $0.005 estimate a penny beat reads as a 5000% surprise worth 500 points.
    That is how Microsoft and Meta ranked 96th and 87th on 2026-07-30."""
    tiny = _entry(snapshot={"eps_est": 0.005, "surprise_pct": 5000.0,
                            "price_move_pct": 0.0})
    real = _entry(snapshot={"eps_est": 5.00, "surprise_pct": 10.0,
                            "price_move_pct": 0.0})
    assert reconcile.proxy_score(tiny, TODAY) == 0.0
    assert reconcile.proxy_score(real, TODAY) > reconcile.proxy_score(tiny, TODAY)


def test_surprise_at_the_floor_still_counts():
    at_floor = _entry(snapshot={"eps_est": reconcile.ABS_EPS_FLOOR,
                                "surprise_pct": 20.0, "price_move_pct": 0.0})
    assert reconcile.proxy_score(at_floor, TODAY) == 2.0


def test_score_terms_are_clamped_so_no_single_input_dominates():
    wild = _entry(snapshot={"eps_est": 5.00, "surprise_pct": 100000.0,
                            "price_move_pct": 100000.0})
    expected = (reconcile.SURPRISE_SCORE_CAP / 10.0
                + reconcile.REACTION_SCORE_CAP / 5.0)
    assert reconcile.proxy_score(wild, TODAY) == expected


def test_a_negative_estimate_does_not_flip_the_surprise_sign():
    loss_maker = _entry(snapshot={"eps_est": -2.00, "surprise_pct": -50.0,
                                  "price_move_pct": 0.0})
    assert reconcile.proxy_score(loss_maker, TODAY) == 5.0


def test_missing_none_and_garbage_snapshot_values_do_not_raise():
    cases = [
        _entry(snapshot=None),
        _entry(snapshot={}),
        _entry(snapshot="not a dict"),
        _entry(snapshot={"eps_est": None, "surprise_pct": None,
                         "price_move_pct": None}),
        _entry(snapshot={"eps_est": "n/a", "surprise_pct": "--",
                         "price_move_pct": ""}),
        _entry(snapshot={"eps_est": "5.0", "surprise_pct": "12.5",
                         "price_move_pct": "."}),
    ]
    for entry in cases:
        assert isinstance(reconcile.proxy_score(entry, TODAY), float)
    # The string-numeric case is still read, not discarded.
    assert reconcile.proxy_score(cases[-1], TODAY) == 1.25


def test_a_garbage_today_costs_only_the_age_bonus():
    snap = {"eps_est": 5.00, "surprise_pct": 10.0, "price_move_pct": 2.0}
    entry = _entry(snapshot=snap, line_shown="2026-08-17")
    assert reconcile.proxy_score(entry, "not-a-date") == 1.4


def test_score_floors_still_agree_with_earnings_report():
    """The constants are copied, not imported, so the copy is pinned here. A
    drift must be a test failure, not two rankers quietly disagreeing about the
    same snapshot."""
    assert reconcile.ABS_EPS_FLOOR == earnings_report.ABS_EPS_FLOOR
    assert reconcile.SURPRISE_SCORE_CAP == earnings_report.SURPRISE_SCORE_CAP
    assert reconcile.REACTION_SCORE_CAP == earnings_report.REACTION_SCORE_CAP


# --------------------------------------------------------------------------- #
# rank — deterministic read order
# --------------------------------------------------------------------------- #
def test_rank_is_descending_by_proxy():
    low = _entry(ticker="AAA", origin="also")
    high = _entry(ticker="BBB", origin="card")
    assert [m["ticker"] for m in reconcile.rank([low, high], TODAY)] == ["BBB", "AAA"]


def test_rank_breaks_ties_on_ticker_ascending():
    """Same proxy AND same filing date — ticker is the last resort, so the
    published order never depends on upstream dict ordering."""
    same = {"eps_est": 5.00, "surprise_pct": 10.0, "price_move_pct": 2.0}
    rows = [_entry(ticker=t, snapshot=same, filed="2026-08-20")
            for t in ("MSFT", "AAPL", "ZTS")]
    assert [m["ticker"] for m in reconcile.rank(rows, TODAY)] == ["AAPL", "MSFT", "ZTS"]


def test_rank_breaks_proxy_ties_on_recency_before_the_alphabet():
    """The tie is the normal case, not the corner case.

    An entry whose beat-1 snapshot is empty scores on origin alone — every
    backfilled entry does — so large numbers of matches tie exactly. Broken on
    ticker alone, the alphabet decides: measured against the seeded pool the
    eight filings read on day one were AAP, AIRO, AMAT, AMKR, AMZN, APLD, AREN
    and ARX, while Microsoft's 10-K waited on the letter M. Newest first."""
    rows = [_entry(ticker=t, snapshot={}, filed=d) for t, d in (
        ("AAP", "2026-07-25"), ("MSFT", "2026-08-21"), ("AIRO", "2026-08-02"))]
    assert [m["ticker"] for m in reconcile.rank(rows, TODAY)] == [
        "MSFT", "AIRO", "AAP"]


def test_rank_sorts_an_unparseable_filing_date_last_without_raising():
    rows = [_entry(ticker="AAA", snapshot={}, filed="not-a-date"),
            _entry(ticker="ZZZ", snapshot={}, filed="2026-08-01")]
    assert [m["ticker"] for m in reconcile.rank(rows, TODAY)] == ["ZZZ", "AAA"]


def test_rank_is_deterministic_across_repeated_calls_on_shuffled_input():
    """Briefs must be reproducible from their inputs; upstream dict ordering
    must never leak into the published order."""
    rows = [_entry(ticker=t, snapshot={"eps_est": 5.00, "surprise_pct": 10.0,
                                       "price_move_pct": 2.0})
            for t in ("DE", "COST", "AAPL", "NVDA", "MSFT", "KO")]
    expected = [m["ticker"] for m in reconcile.rank(rows, TODAY)]
    for shift in range(1, len(rows)):
        shuffled = rows[shift:] + rows[:shift]
        assert [m["ticker"] for m in reconcile.rank(shuffled, TODAY)] == expected
    assert expected == sorted(expected)


def test_rank_uses_an_attached_proxy_over_recomputing_it():
    rows = [{"ticker": "AAA", "proxy": 0.5}, {"ticker": "BBB", "proxy": 9.0}]
    assert [m["ticker"] for m in reconcile.rank(rows, TODAY)] == ["BBB", "AAA"]


def test_rank_on_empty_input_returns_empty():
    assert reconcile.rank([], TODAY) == []
    assert reconcile.rank(None, TODAY) == []


# --------------------------------------------------------------------------- #
# content_score — what the filing turned out to contain
# --------------------------------------------------------------------------- #
def test_a_filing_that_yielded_nothing_scores_zero():
    assert reconcile.content_score(_screened("DE", 5.0)) == 0.0


def test_segments_outweigh_every_other_signal_combined():
    segments_only = _screened("AAA", 0.0, segments=[{"name": "Client"}])
    everything_else = _screened("BBB", 0.0,
                                disclosures=[1, 2, 3, 4, 5],
                                filing_sections={"mdna": "x", "risk_changes": "y"})
    assert reconcile.content_score(segments_only) > \
        reconcile.content_score(everything_else)


def test_disclosures_outweigh_bare_sections():
    disclosed = _screened("AAA", 0.0, disclosures=[{"term": "goodwill"}])
    sections = _screened("BBB", 0.0, filing_sections={"mdna": "x"})
    assert reconcile.content_score(disclosed) > reconcile.content_score(sections)
    assert reconcile.content_score(sections) > 0.0


def test_the_disclosure_term_is_clamped():
    """A boilerplate-heavy filer must not sweep the block cap on volume alone."""
    many = _screened("AAA", 0.0, disclosures=list(range(50)))
    capped = _screened("BBB", 0.0, disclosures=list(range(reconcile.DISCLOSURE_COUNT_CAP)))
    assert reconcile.content_score(many) == reconcile.content_score(capped)


def test_content_score_accepts_disclosures_as_a_dict():
    assert reconcile.content_score(_screened("DE", 0.0,
                                             disclosures={"goodwill": "q"})) > 0.0


def test_content_score_tolerates_missing_and_garbage_keys():
    assert reconcile.content_score({}) == 0.0
    assert reconcile.content_score(None) == 0.0
    assert reconcile.content_score({"segments": None, "disclosures": 7,
                                    "filing_sections": "text"}) > 0.0


# --------------------------------------------------------------------------- #
# select — who earns a block today
# --------------------------------------------------------------------------- #
def test_select_promotes_by_content_not_by_proxy():
    """The whole point of the screen: depth is earned by the filing, not by the
    provenance of the quarter that preceded it."""
    rich_but_dull = _screened("AAA", 0.5, segments=[{"name": "Client"}])
    loud_but_empty = _screened("BBB", 99.0)
    promoted, deferred = reconcile.select([loud_but_empty, rich_but_dull])
    assert [m["ticker"] for m in promoted] == ["AAA"]
    assert [m["ticker"] for m in deferred] == ["BBB"]


def test_a_filing_with_no_content_is_never_promoted():
    """An empty filing gets a line, not a block — even when nothing competes."""
    empties = [_screened("AAA", 9.0), _screened("BBB", 8.0)]
    promoted, deferred = reconcile.select(empties)
    assert promoted == []
    assert [m["ticker"] for m in deferred] == ["AAA", "BBB"]


def test_select_breaks_content_ties_on_proxy_then_ticker():
    rows = [_screened("CCC", 1.0, segments=[1]),
            _screened("AAA", 5.0, segments=[1]),
            _screened("BBB", 5.0, segments=[1])]
    promoted, _ = reconcile.select(rows)
    assert [m["ticker"] for m in promoted] == ["AAA", "BBB", "CCC"]


def test_select_respects_the_block_cap():
    rows = [_screened("T%02d" % i, float(i), segments=[1]) for i in range(12)]
    promoted, deferred = reconcile.select(rows)
    assert len(promoted) == reconcile.BLOCK_CAP
    assert len(deferred) == 12 - reconcile.BLOCK_CAP
    # Nothing is lost: promoted + deferred is the whole screened set.
    assert len(promoted) + len(deferred) == len(rows)


def test_select_honours_an_explicit_cap():
    rows = [_screened("T%02d" % i, float(i), segments=[1]) for i in range(5)]
    promoted, deferred = reconcile.select(rows, cap=2)
    assert len(promoted) == 2 and len(deferred) == 3


def test_the_elapsed_guard_cuts_promotion_to_the_floor():
    """Past the guard the 600s cron kill is close, and it takes the ENTIRE
    briefing — world and Japan tabs included — not just this section."""
    rows = [_screened("T%02d" % i, float(i), segments=[1]) for i in range(10)]
    promoted, deferred = reconcile.select(
        rows, elapsed_s=reconcile.PROMOTION_ELAPSED_GUARD_S + 1)
    assert len(promoted) == reconcile.PROMOTION_FLOOR
    assert len(deferred) == 10 - reconcile.PROMOTION_FLOOR


def test_the_elapsed_guard_does_not_fire_at_or_below_the_threshold():
    rows = [_screened("T%02d" % i, float(i), segments=[1]) for i in range(10)]
    promoted, _ = reconcile.select(
        rows, elapsed_s=reconcile.PROMOTION_ELAPSED_GUARD_S)
    assert len(promoted) == reconcile.BLOCK_CAP


def test_elapsed_s_none_disables_the_guard():
    rows = [_screened("T%02d" % i, float(i), segments=[1]) for i in range(10)]
    promoted, _ = reconcile.select(rows, elapsed_s=None)
    assert len(promoted) == reconcile.BLOCK_CAP


def test_the_elapsed_guard_only_ever_promotes_fewer():
    """A caller that already asked for fewer than the floor asked for a reason."""
    rows = [_screened("T%02d" % i, float(i), segments=[1]) for i in range(10)]
    promoted, _ = reconcile.select(rows, cap=1, elapsed_s=9999)
    assert len(promoted) == 1


def test_the_guard_defers_rather_than_drops():
    """Carry-over is what makes the trade free: a filing the guard skips is
    still inside the window tomorrow and returns one day older."""
    rows = [_screened("T%02d" % i, float(i), segments=[1]) for i in range(10)]
    promoted, deferred = reconcile.select(rows, elapsed_s=9999)
    assert len(promoted) + len(deferred) == len(rows)
    assert {m["ticker"] for m in promoted} & {m["ticker"] for m in deferred} == set()


def test_select_on_empty_input_returns_two_empty_lists():
    assert reconcile.select([]) == ([], [])
    assert reconcile.select(None) == ([], [])


def test_select_output_is_deterministic_across_shuffled_input():
    rows = [_screened("T%02d" % i, float(i % 3), segments=[1] if i % 2 else [],
                      disclosures=[1] if i % 3 else [])
            for i in range(9)]
    expected = [m["ticker"] for m in reconcile.select(rows)[0]]
    for shift in range(1, len(rows)):
        shuffled = rows[shift:] + rows[:shift]
        assert [m["ticker"] for m in reconcile.select(shuffled)[0]] == expected


def test_match_rank_select_compose_end_to_end():
    """The whole flow on plain dicts, with no module under it but this one."""
    pool = {
        "DE:2026-08-20": _entry(ticker="DE", cik="315189", origin="card"),
        "COST:2026-08-19": _entry(ticker="COST", cik="909832", origin="also",
                                  line_shown="2026-08-19"),
        "SAP:2026-08-18": _entry(ticker="SAP", cik="1000184"),   # never filed
        "KO:2026-08-15": _entry(ticker="KO", cik="21344",
                                block_shown="2026-08-16"),       # already had it
    }
    index = {"315189": _filing(), "909832": _filing(form="10-K"),
             "21344": _filing()}

    matches = reconcile.match(pool, index, TODAY)
    assert sorted(m["ticker"] for m in matches) == ["COST", "DE"]

    ranked = reconcile.rank(matches, TODAY)[:reconcile.SCREEN_MAX]
    assert [m["ticker"] for m in ranked] == ["DE", "COST"]   # card bonus 2.0 > 3d age 1.5

    # COST's filing parsed a segment table; DE's yielded nothing.
    for m in ranked:
        m["segments"] = [{"name": "Warehouse"}] if m["ticker"] == "COST" else []
        m["disclosures"] = []
        m["filing_sections"] = {}

    promoted, deferred = reconcile.select(ranked)
    assert [m["ticker"] for m in promoted] == ["COST"]
    assert [m["ticker"] for m in deferred] == ["DE"]


def test_expired_unread_excludes_entries_with_no_cik():
    """MEASURED, not hypothetical. The pool persists an empty `cik` for most
    entries -- NVDA, CRWD, MRVL and SNPS all carry "" on disk -- and run.py
    fills them from the SEC ticker map in place at runtime, before this call,
    without writing them back.

    No CIK means no join, which is exactly why `match` excludes them too. Such
    an entry never expired; it was never matchable. Without this guard a failed
    ticker-map fetch would not degrade the counter, it would INVERT it: every
    unresolved name reported as a lost filing."""
    pool = {"NVDA:2026-08-26": _entry(ticker="NVDA", cik="",
                                      line_shown="2026-08-29")}
    assert reconcile.expired_unread(pool, {}) == []
