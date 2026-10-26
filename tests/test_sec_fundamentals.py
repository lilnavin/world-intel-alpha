"""Tests for the SEC fundamentals layer (scripts/sec_fundamentals.py).

Fully offline: the only network seam, `_sec_get_json`, is monkeypatched (directly, or
indirectly via a fake `session.get`) so the suite exercises period anchoring without a
single external call. Fixtures model the real SEC submissions shape —
`subs["filings"]["recent"]` is a set of *parallel* arrays (`form`, `filingDate`,
`reportDate`, `accessionNumber`, `fy`, `fp`) where index i across all arrays describes
one filing.
"""

import os
import sys

# Make scripts/ importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from worldintel.core import sec_fundamentals as sf  # noqa: E402


def _recent(rows):
    """rows: list of (form, filingDate, reportDate, accession, fy, fp[, primaryDocument])
    tuples -> the parallel-array shape SEC's submissions endpoint actually returns.

    `primaryDocument` is optional so the pre-existing six-tuple fixtures keep
    working; omitting it models a filing whose document name SEC did not carry."""
    return {
        "form": [r[0] for r in rows],
        "filingDate": [r[1] for r in rows],
        "reportDate": [r[2] for r in rows],
        "accessionNumber": [r[3] for r in rows],
        "fy": [r[4] for r in rows],
        "fp": [r[5] for r in rows],
        "primaryDocument": [r[6] if len(r) > 6 else "" for r in rows],
    }


def _subs(rows):
    return {"filings": {"recent": _recent(rows)}}


AAPL_ROWS = [
    ("10-Q", "2026-07-31", "2026-06-27", "0000320193-26-000020", 2026, "Q3"),
    ("10-Q", "2026-05-01", "2026-03-28", "0000320193-26-000015", 2026, "Q2"),
    ("10-Q", "2025-07-31", "2025-06-28", "0000320193-25-000020", 2025, "Q3"),
    ("10-Q", "2025-05-01", "2025-03-29", "0000320193-25-000015", 2025, "Q2"),
    ("10-K", "2025-10-31", "2025-09-27", "0000320193-25-000030", 2025, None),
    ("8-K", "2026-07-30", "2026-07-30", "0000320193-26-000019", None, None),
]

MSFT_ROWS = [
    ("10-K", "2026-07-29", "2026-06-30", "0000789019-26-000030", 2026, "FY"),
]

AMZN_ROWS = [
    ("10-Q", "2026-07-31", "2026-06-30", "0001018724-26-000050", 2026, "Q2"),
]

# A full nine-quarter filing history — enough for the year-ago comparable AND for
# the five consecutive period ends TTM stitching needs on both sides of the YoY.
AMZN_FULL_ROWS = [
    ("10-Q", "2026-07-31", "2026-06-30", "0001018724-26-000050", 2026, "Q2"),
    ("10-Q", "2026-04-30", "2026-03-31", "0001018724-26-000030", 2026, "Q1"),
    ("10-K", "2026-02-05", "2025-12-31", "0001018724-26-000010", 2025, "FY"),
    ("10-Q", "2025-10-30", "2025-09-30", "0001018724-25-000070", 2025, "Q3"),
    ("10-Q", "2025-07-31", "2025-06-30", "0001018724-25-000050", 2025, "Q2"),
    ("10-Q", "2025-04-30", "2025-03-31", "0001018724-25-000030", 2025, "Q1"),
    ("10-K", "2025-02-06", "2024-12-31", "0001018724-25-000010", 2024, "FY"),
    ("10-Q", "2024-10-31", "2024-09-30", "0001018724-24-000070", 2024, "Q3"),
    ("10-Q", "2024-08-01", "2024-06-30", "0001018724-24-000050", 2024, "Q2"),
]


def test_aapl_anchor_yields_full_period_dict_no_calendar_arithmetic():
    subs = _subs(AAPL_ROWS)
    result = sf.resolve_period(subs, "2026-07-30")
    assert result == {
        "period_end": "2026-06-27",
        "accession": "0000320193-26-000020",
        "form": "10-Q",
        "fy": 2026,
        "fp": "Q3",
        "prior_end": "2026-03-28",
        "py_end": "2025-06-28",
        "py_prior_end": "2025-03-29",
        "fy_end": "2025-09-27",
        # Added in Task 3 to close the Task 1 -> Task 2 gap (see the recent_ends
        # tests below): resolve_ttm needs five consecutive period ends and the
        # anchor's own keys only reach one step back.
        "recent_ends": ["2026-06-27", "2026-03-28", "2025-09-27", "2025-06-28", "2025-03-29"],
        # Added in Task 12: sec_filing_text fetches the filing DOCUMENT, and an
        # accession alone does not name it. Absent from these six-tuple fixtures.
        "primary_doc": "",
    }


def test_msft_same_day_10k_anchors():
    subs = _subs(MSFT_ROWS)
    result = sf.resolve_period(subs, "2026-07-29")
    assert result["period_end"] == "2026-06-30"
    assert result["form"] == "10-K"
    assert result["accession"] == "0000789019-26-000030"


def test_amzn_filing_one_day_after_report_date_anchors():
    subs = _subs(AMZN_ROWS)
    result = sf.resolve_period(subs, "2026-07-30")
    assert result["period_end"] == "2026-06-30"
    assert result["form"] == "10-Q"


def test_no_qualifying_filing_returns_none():
    rows = [("8-K", "2026-07-30", "2026-07-30", "0000000000-26-000001", None, None)]
    subs = _subs(rows)
    assert sf.resolve_period(subs, "2026-07-30") is None


def test_10q_filed_30_days_later_is_outside_window_returns_none():
    rows = [("10-Q", "2026-08-29", "2026-06-30", "0000000000-26-000002", 2026, "Q2")]
    subs = _subs(rows)
    assert sf.resolve_period(subs, "2026-07-30") is None


def test_resolve_period_missing_filings_key_returns_none():
    assert sf.resolve_period({}, "2026-07-30") is None
    assert sf.resolve_period(None, "2026-07-30") is None


def test_resolve_period_never_raises_on_malformed_subs_shapes():
    """Global Constraint 1: no function in this module may let an exception
    escape, even given a malformed (non-dict) JSON body from a flaky endpoint."""
    malformed = [
        "garbage",
        [1, 2, 3],
        {"filings": "garbage"},
        {"filings": {"recent": "garbage"}},
        {"filings": {"recent": {"form": ["10-Q"]}}},  # missing parallel arrays
    ]
    for subs in malformed:
        assert sf.resolve_period(subs, "2026-07-30") is None


def test_tie_broken_by_10k_over_10q_on_same_filing_date():
    rows = [
        ("10-Q", "2026-07-30", "2026-06-27", "0000000000-26-000003", 2026, "Q3"),
        ("10-K", "2026-07-30", "2026-06-27", "0000000000-26-000004", 2026, None),
    ]
    subs = _subs(rows)
    result = sf.resolve_period(subs, "2026-07-30")
    assert result["form"] == "10-K"
    assert result["accession"] == "0000000000-26-000004"


# --------------------------------------------------------------------------- #
# Task 12: the two fields run.py needs to call the anomaly_panel and
# sec_filing_text lanes. Both are already computed here and were being dropped.
# --------------------------------------------------------------------------- #


def test_anchor_publishes_the_primary_document_name():
    """`sec_filing_text.fetch_filing_text` builds an Archives URL from
    cik + accession + primary document. The accession alone does not name the
    document, and `recent["primaryDocument"]` is in the same dict we already
    read, so resolving it here costs nothing and saves a second fetch."""
    rows = [("10-Q", "2026-07-31", "2026-06-27", "0000320193-26-000020", 2026, "Q3",
             "aapl-20260627.htm")]
    result = sf.resolve_period(_subs(rows), "2026-07-30")
    assert result["primary_doc"] == "aapl-20260627.htm"


def test_anchor_primary_document_is_empty_not_absent_when_sec_omits_the_array():
    """Global Constraint 1: a missing parallel array degrades, never raises,
    and the key still ships so downstream `.get` chains stay uniform."""
    recent = _recent([("10-Q", "2026-07-31", "2026-06-27", "0000320193-26-000020",
                       2026, "Q3", "aapl-20260627.htm")])
    del recent["primaryDocument"]
    result = sf.resolve_period({"filings": {"recent": recent}}, "2026-07-30")
    assert result["primary_doc"] == ""


def test_anchor_primary_document_follows_the_10k_over_10q_tiebreak():
    """It must come from the SAME index the accession does, or the URL is a
    mismatched pair that 404s."""
    rows = [
        ("10-Q", "2026-07-30", "2026-06-27", "0000000000-26-000003", 2026, "Q3", "q.htm"),
        ("10-K", "2026-07-30", "2026-06-27", "0000000000-26-000004", 2026, None, "k.htm"),
    ]
    result = sf.resolve_period(_subs(rows), "2026-07-30")
    assert result["accession"] == "0000000000-26-000004"
    assert result["primary_doc"] == "k.htm"


def test_fetch_submissions_returns_subs_on_success(monkeypatch):
    subs = _subs(AAPL_ROWS)
    captured = {}

    def fake_get_json(url, session):
        captured["url"] = url
        return subs

    monkeypatch.setattr(sf, "_sec_get_json", fake_get_json)
    health = {}
    result = sf.fetch_submissions("0000320193", None, health)
    assert result == subs
    assert captured["url"] == "https://data.sec.gov/submissions/CIK0000320193.json"
    assert health == {}


def test_fetch_submissions_raising_seam_degrades_to_empty_dict(monkeypatch):
    def fake_get_json(url, session):
        raise RuntimeError("boom")

    monkeypatch.setattr(sf, "_sec_get_json", fake_get_json)
    health = {}
    result = sf.fetch_submissions("0000320193", None, health)
    assert result == {}
    assert health["sec_fundamentals"]["ok"] is False


def test_sec_get_json_sends_sec_user_agent():
    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    class FakeSession:
        def get(self, url, headers=None, timeout=None):
            self.captured_headers = headers
            return FakeResponse()

    sess = FakeSession()
    result = sf._sec_get_json("https://data.sec.gov/submissions/CIK0000320193.json", sess)
    assert result == {"ok": True}
    assert sess.captured_headers == {"User-Agent": sf._sec_ua()}


# --------------------------------------------------------------------------- #
# Task 2: fact selection and duration ladders
#
# Fixtures below feed `select_instant`/`select_duration`/`resolve_quarter`/
# `resolve_ttm` the *already-flattened* fact shape that `_facts()` produces
# (a plain dict with a "unit" key), not a raw companyconcept response. The
# ground-truth numbers (AMZN/MSFT/AAPL/META) are the exact figures fixed in
# the task-2 brief, gathered live from SEC.
# --------------------------------------------------------------------------- #
def _fact(start, end, val, unit="USD", form="10-Q", filed="2026-07-31", fy=None, fp=None):
    """A duration fact (has 'start')."""
    return {"start": start, "end": end, "val": val, "unit": unit, "form": form, "filed": filed, "fy": fy, "fp": fp}


def _instant(end, val, unit="USD", form="10-Q", filed="2026-07-31", fy=None, fp=None):
    """An instant fact (no 'start') — balance-sheet items."""
    return {"end": end, "val": val, "unit": unit, "form": form, "filed": filed, "fy": fy, "fp": fp}


def test_facts_flattens_units_with_unit_recorded():
    concept = {
        "units": {
            "USD": [
                {
                    "start": "2026-04-01",
                    "end": "2026-06-30",
                    "val": 200606000000,
                    "form": "10-Q",
                    "filed": "2026-07-31",
                    "fy": 2026,
                    "fp": "Q2",
                }
            ],
            "USD/shares": [
                {"end": "2026-06-30", "val": 1.68, "form": "10-Q", "filed": "2026-07-31", "fy": 2026, "fp": "Q2"}
            ],
        }
    }
    facts = sf._facts(concept)
    assert len(facts) == 2
    assert {f["unit"] for f in facts} == {"USD", "USD/shares"}
    usd_fact = next(f for f in facts if f["unit"] == "USD")
    assert usd_fact["val"] == 200606000000
    assert usd_fact["start"] == "2026-04-01"


def test_facts_never_raises_on_malformed_concept_shapes():
    malformed = [
        "garbage",
        None,
        [1, 2, 3],
        {"units": "garbage"},
        {"units": {"USD": "garbage"}},
        {"units": {"USD": [1, 2, "x"]}},
    ]
    for concept in malformed:
        assert sf._facts(concept) == []


def test_is_per_share_detects_shares_units():
    assert sf.is_per_share("USD/shares") is True
    assert sf.is_per_share("USD-per-shares") is True
    assert sf.is_per_share("USD") is False
    assert sf.is_per_share(None) is False


def test_select_instant_matches_end_only_no_start():
    facts = [
        _instant("2026-03-31", 111, filed="2026-05-01"),
        _instant("2026-06-30", 222, filed="2026-07-31"),
    ]
    result = sf.select_instant(facts, "2026-06-30")
    assert result["val"] == 222


def test_select_instant_no_match_returns_none():
    facts = [_instant("2026-03-31", 111)]
    assert sf.select_instant(facts, "2026-06-30") is None


def test_select_instant_ignores_duration_facts():
    """A fact with a 'start' (duration fact) is never returned as an instant."""
    facts = [_fact("2026-04-01", "2026-06-30", 999)]
    assert sf.select_instant(facts, "2026-06-30") is None


def test_select_duration_matches_three_month_window():
    facts = [_fact("2026-04-01", "2026-06-30", 200606000000)]
    result = sf.select_duration(facts, "2026-06-30", months=3)
    assert result["val"] == 200606000000


def test_select_duration_rejects_fact_ending_at_prior_end():
    """A fact whose end is prior_end is never selected for period_end."""
    facts = [_fact("2026-01-01", "2026-03-31", 555)]
    assert sf.select_duration(facts, "2026-06-30", months=3) is None


def test_select_duration_respects_tolerance():
    facts = [_fact("2026-01-01", "2026-06-30", 999)]  # ~181 days, not ~3 months
    assert sf.select_duration(facts, "2026-06-30", months=3) is None


def test_resolve_quarter_amzn_revenue_direct_three_month():
    facts = [
        _fact("2026-04-01", "2026-06-30", 200606000000, form="10-Q", filed="2026-07-31", fy=2026, fp="Q2"),
        _fact("2026-01-01", "2026-06-30", 393000000000, form="10-Q", filed="2026-07-31", fy=2026, fp="Q2"),
    ]
    fact, basis = sf.resolve_quarter(facts, "2026-06-30", "2026-03-31")
    assert basis == "direct"
    assert fact["val"] == 200606000000


def test_resolve_quarter_msft_revenue_derived_fiscal_q4():
    facts = [
        _fact("2025-07-01", "2026-06-30", 331839000000, form="10-K", filed="2026-07-29", fy=2026, fp="FY"),
        _fact("2025-07-01", "2026-03-31", 241832000000, form="10-Q", filed="2026-04-29", fy=2026, fp="Q3"),
    ]
    fact, basis = sf.resolve_quarter(facts, "2026-06-30", "2026-03-31")
    assert basis == "derived"
    assert fact["val"] == 90007000000
    assert fact["start"] == "2026-03-31"
    assert fact["end"] == "2026-06-30"


def test_resolve_quarter_msft_eps_derivation_blocked_never_produces_4_81():
    facts = [
        _fact("2025-07-01", "2026-06-30", 17.95, unit="USD/shares", form="10-K", filed="2026-07-29", fy=2026, fp="FY"),
        _fact("2025-07-01", "2026-03-31", 13.14, unit="USD/shares", form="10-Q", filed="2026-04-29", fy=2026, fp="Q3"),
    ]
    fact, basis = sf.resolve_quarter(facts, "2026-06-30", "2026-03-31")
    assert (fact, basis) == (None, "blocked")


def test_resolve_quarter_aapl_ocf_derived_from_9mo_minus_6mo():
    facts = [
        _fact("2025-10-01", "2026-06-27", 90000000000, form="10-Q", filed="2026-07-31", fy=2026, fp="Q3"),
        _fact("2025-10-01", "2026-03-28", 60000000000, form="10-Q", filed="2026-05-01", fy=2026, fp="Q2"),
    ]
    fact, basis = sf.resolve_quarter(facts, "2026-06-27", "2026-03-28")
    assert basis == "derived"
    assert fact["val"] == 30000000000


def test_resolve_quarter_mismatched_start_never_subtracts():
    facts = [
        _fact("2025-10-01", "2026-06-27", 90000000000, form="10-Q"),
        _fact("2025-07-01", "2026-03-28", 60000000000, form="10-Q"),  # different start
    ]
    fact, basis = sf.resolve_quarter(facts, "2026-06-27", "2026-03-28")
    assert (fact, basis) == (None, "none")


def test_resolve_quarter_no_duration_facts_at_period_end_returns_none():
    facts = [_fact("2025-10-01", "2026-03-28", 60000000000)]
    assert sf.resolve_quarter(facts, "2026-06-27", "2026-03-28") == (None, "none")


def test_resolve_quarter_ignores_fact_ending_at_prior_end():
    """A fact whose end is prior_end is never selected for period_end, even in
    the derived-ladder step (not just the direct 3-month step)."""
    facts = [_fact("2025-12-31", "2026-03-31", 111111, form="10-Q")]
    assert sf.resolve_quarter(facts, "2026-06-30", "2026-03-31") == (None, "none")


def test_resolve_ttm_amzn_direct_365_day_fact():
    facts = [_fact("2025-07-01", "2026-06-30", 161403000000, form="10-Q", filed="2026-07-31", fy=2026, fp="Q2")]
    value, basis = sf.resolve_ttm(facts, "2026-06-30", [])
    assert basis == "direct"
    assert value == 161403000000


def test_resolve_ttm_meta_stitched_from_four_quarters():
    facts = [
        _fact("2026-04-01", "2026-06-30", 100, form="10-Q", filed="2026-07-31"),
        _fact("2026-01-01", "2026-03-31", 90, form="10-Q", filed="2026-04-30"),
        _fact("2025-10-01", "2025-12-31", 120, form="10-K", filed="2026-01-30"),
        _fact("2025-07-01", "2025-09-30", 80, form="10-Q", filed="2025-10-30"),
    ]
    quarter_ends = ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30"]
    value, basis = sf.resolve_ttm(facts, "2026-06-30", quarter_ends)
    assert basis == "stitched"
    assert value == 100 + 90 + 120 + 80


def test_resolve_ttm_meta_missing_quarter_returns_none():
    facts = [
        _fact("2026-04-01", "2026-06-30", 100, form="10-Q"),
        _fact("2026-01-01", "2026-03-31", 90, form="10-Q"),
        # missing the quarter ending 2025-12-31
        _fact("2025-07-01", "2025-09-30", 80, form="10-Q"),
    ]
    quarter_ends = ["2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30", "2025-06-30"]
    value, basis = sf.resolve_ttm(facts, "2026-06-30", quarter_ends)
    assert (value, basis) == (None, "none")


def test_resolve_ttm_non_iterable_quarter_ends_never_raises():
    """Global Constraint 1: a malformed (truthy, non-iterable) quarter_ends
    must degrade to (None, "none"), not raise TypeError from list(...)."""
    facts = [_fact("2026-04-01", "2026-06-30", 100, form="10-Q")]
    assert sf.resolve_ttm(facts, "2026-06-30", 5) == (None, "none")


# --------------------------------------------------------------------------- #
# Task 3a: `recent_ends` — the quarter-end index resolve_ttm needs
#
# `resolve_ttm(facts, period_end, quarter_ends)` (Task 2) wants FIVE consecutive
# period ends, most recent first, whose consecutive pairs are the four quarters
# it stitches. Task 1's anchor exposed only one step back (`prior_end`), so the
# anchor now also carries the full ordered list the 5-element slice comes from.
# --------------------------------------------------------------------------- #
def test_recent_ends_is_most_recent_first_and_excludes_non_anchor_forms():
    result = sf.resolve_period(_subs(AAPL_ROWS), "2026-07-30")
    # The 8-K's reportDate (2026-07-30) is not a period end and must not appear;
    # the 10-K's 2025-09-27 sorts by DATE, not by its position in the arrays.
    assert result["recent_ends"] == [
        "2026-06-27",
        "2026-03-28",
        "2025-09-27",
        "2025-06-28",
        "2025-03-29",
    ]


def test_recent_ends_deduplicates_repeated_report_dates():
    rows = [
        ("10-Q", "2026-07-31", "2026-06-30", "a-1", 2026, "Q2"),
        ("10-Q", "2026-07-31", "2026-06-30", "a-2", 2026, "Q2"),  # amended, same period
        ("10-Q", "2026-04-30", "2026-03-31", "a-3", 2026, "Q1"),
    ]
    result = sf.resolve_period(_subs(rows), "2026-07-30")
    assert result["recent_ends"] == ["2026-06-30", "2026-03-31"]


def test_recent_ends_slice_from_period_end_feeds_resolve_ttm():
    """The contract that closes the Task 1 / Task 2 gap: the 5-element
    `quarter_ends` argument is simply a slice of `recent_ends` starting at
    `period_end`."""
    result = sf.resolve_period(_subs(AMZN_FULL_ROWS), "2026-07-30")
    ends = result["recent_ends"]
    start = ends.index(result["period_end"])
    quarter_ends = ends[start:start + 5]
    assert quarter_ends == [
        "2026-06-30",
        "2026-03-31",
        "2025-12-31",
        "2025-09-30",
        "2025-06-30",
    ]
    assert quarter_ends[1] == result["prior_end"]
    assert quarter_ends[4] == result["py_end"]


# --------------------------------------------------------------------------- #
# Task 3b: tag ladders, metric extraction and build_fundamentals
#
# Fixtures are per-company `{tag: companyconcept_payload}` maps. `fetch_concept`
# is stubbed with a lookup into one of them, so a tag that is absent from the map
# models a real SEC 404 (which `fetch_concept` turns into `{}`) and must make the
# ladder fall through rather than fail the metric.
#
# ONE REQUEST PER TAG, NEVER PER PERIOD: each payload carries BOTH the current
# period's facts and the year-ago comparable's, because that is exactly what the
# real endpoint returns. The tests assert the request count to lock this in.
# --------------------------------------------------------------------------- #
def _units(*facts):
    """Already-built fact dicts -> a companyconcept response, grouped by unit."""
    units = {}
    for fact in facts:
        fact = dict(fact)
        unit = fact.pop("unit", "USD")
        units.setdefault(unit, []).append(fact)
    return {"units": units}


def _far_deadline():
    return sf._now() + 600.0


def _stub_lane(monkeypatch, rows, concepts):
    """Wire both network seams to fixtures; returns the list of fetched tags."""
    fetched = []

    def fake_fetch_submissions(cik, session, health):
        return _subs(rows)

    def fake_fetch_concept(cik, tag, session, **kwargs):
        fetched.append(tag)
        return concepts.get(tag, {})

    monkeypatch.setattr(sf, "fetch_submissions", fake_fetch_submissions)
    monkeypatch.setattr(sf, "fetch_concept", fake_fetch_concept)
    return fetched


AMZN_CONCEPTS = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": _units(
        _fact("2026-04-01", "2026-06-30", 200606000000),
        _fact("2025-04-01", "2025-06-30", 167702000000),
    ),
    "OperatingIncomeLoss": _units(
        _fact("2026-04-01", "2026-06-30", 27461000000),
        _fact("2025-04-01", "2025-06-30", 19169000000),
    ),
    "NetIncomeLoss": _units(
        _fact("2026-04-01", "2026-06-30", 62647000000),
        _fact("2025-04-01", "2025-06-30", 18164000000),
    ),
    "EarningsPerShareDiluted": _units(
        _fact("2026-04-01", "2026-06-30", 5.75, unit="USD/shares"),
        _fact("2025-04-01", "2025-06-30", 1.68, unit="USD/shares"),
    ),
    "StockholdersEquity": _units(
        _instant("2026-06-30", 551620000000),
        _instant("2025-06-30", 379988000000),
    ),
    "NetCashProvidedByUsedInOperatingActivities": _units(
        _fact("2025-07-01", "2026-06-30", 161403000000),
        _fact("2024-07-01", "2025-06-30", 121108000000),
    ),
    # AMZN tags capex as PaymentsToAcquireProductiveAssets; the PP&E tag is dead
    # after 2017 for this filer, which is why it is SECOND in the ladder.
    "PaymentsToAcquireProductiveAssets": _units(
        _fact("2025-07-01", "2026-06-30", 173030000000),
        _fact("2024-07-01", "2025-06-30", 92700000000),
    ),
}


def test_amzn_full_fundamentals_ok(monkeypatch):
    _stub_lane(monkeypatch, AMZN_FULL_ROWS, AMZN_CONCEPTS)
    health = {}
    fund = sf.build_fundamentals("0001018724", "2026-07-30", None, health, _far_deadline())

    assert fund["status"] == "ok"
    assert fund["source"] == "companyconcept"
    assert fund["period_end"] == "2026-06-30"
    assert fund["fiscal"] == {"fy": 2026, "fp": "Q2"}
    assert fund["accession"] == "0001018724-26-000050"

    assert fund["revenue_q"]["value"] == 200606000000
    assert fund["revenue_q"]["basis"] == "direct"
    assert fund["revenue_q"]["tag"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert round(fund["revenue_q"]["yoy_pct"], 1) == 19.6

    assert fund["operating_income_q"]["value"] == 27461000000
    assert fund["net_income_q"]["value"] == 62647000000
    assert fund["eps_diluted_q"]["value"] == 5.75
    assert fund["equity"]["value"] == 551620000000
    assert fund["ocf_ttm"]["value"] == 161403000000
    assert fund["capex_ttm"]["value"] == 173030000000
    assert fund["capex_ttm"]["tag"] == "PaymentsToAcquireProductiveAssets"

    assert fund["segments"] == []
    assert fund["rpo"] is None
    assert fund["capex_company_ttm"] is None


def test_amzn_negative_fcf_is_published_not_suppressed(monkeypatch):
    """OCF < capex is a real, publishable result, not a failure to be nulled."""
    _stub_lane(monkeypatch, AMZN_FULL_ROWS, AMZN_CONCEPTS)
    fund = sf.build_fundamentals("0001018724", "2026-07-30", None, {}, _far_deadline())
    assert fund["fcf_ttm"]["value"] == 161403000000 - 173030000000
    assert fund["fcf_ttm"]["value"] == -11627000000
    assert fund["fcf_ttm"]["definition"] == "gaap_cashflow"
    assert fund["capex_ttm"]["definition"] == "gaap_cashflow"


def test_amzn_ratios_use_the_specified_denominators(monkeypatch):
    _stub_lane(monkeypatch, AMZN_FULL_ROWS, AMZN_CONCEPTS)
    fund = sf.build_fundamentals("0001018724", "2026-07-30", None, {}, _far_deadline())

    assert round(fund["op_margin_pct"]["value"], 2) == round(27461000000 / 200606000000 * 100, 2)
    assert round(fund["op_margin_pct"]["yoy_delta_pp"], 2) == round(
        27461000000 / 200606000000 * 100 - 19169000000 / 167702000000 * 100, 2
    )
    # capex intensity is capex / (OCF + capex) -- NOT a thin Equity-minus-cash
    # denominator, which is what produced a 259.9% ROIC for AAPL.
    assert round(fund["capex_intensity_pct"]["value"], 2) == round(
        173030000000 / (161403000000 + 173030000000) * 100, 2
    )


def test_one_request_per_tag_never_per_period(monkeypatch):
    """The year-ago comparable comes out of the SAME payload as the current
    period. A second request for another period is what blows the 45s budget."""
    fetched = _stub_lane(monkeypatch, AMZN_FULL_ROWS, AMZN_CONCEPTS)
    fund = sf.build_fundamentals("0001018724", "2026-07-30", None, {}, _far_deadline())
    assert fund["revenue_q"]["yoy_pct"] is not None
    assert len(fetched) == len(set(fetched))
    # 7 fetched metrics, each resolving on the first tag of its ladder: 7
    # requests total, not 7 x (the number of periods the dict reports on).
    assert len(fetched) == 7


MSFT_FULL_ROWS = [
    ("10-K", "2026-07-29", "2026-06-30", "0000789019-26-000030", 2026, "FY"),
    ("10-Q", "2026-04-28", "2026-03-31", "0000789019-26-000020", 2026, "Q3"),
    ("10-Q", "2026-01-27", "2025-12-31", "0000789019-26-000010", 2026, "Q2"),
    ("10-Q", "2025-10-28", "2025-09-30", "0000789019-25-000070", 2026, "Q1"),
    ("10-K", "2025-07-30", "2025-06-30", "0000789019-25-000050", 2025, "FY"),
    ("10-Q", "2025-04-29", "2025-03-31", "0000789019-25-000030", 2025, "Q3"),
]

MSFT_CONCEPTS = {
    # Fiscal Q4: no standalone 3-month fact exists, only the cumulative FY and 9M.
    "RevenueFromContractWithCustomerExcludingAssessedTax": _units(
        _fact("2025-07-01", "2026-06-30", 340007000000, form="10-K"),
        _fact("2025-07-01", "2026-03-31", 250000000000, form="10-Q"),
        _fact("2024-07-01", "2025-06-30", 305000000000, form="10-K"),
        _fact("2024-07-01", "2025-03-31", 224000000000, form="10-Q"),
    ),
    # 13.50 - 8.69 = 4.81, the per-share figure that must be unreachable.
    "EarningsPerShareDiluted": _units(
        _fact("2025-07-01", "2026-06-30", 13.50, unit="USD/shares", form="10-K"),
        _fact("2025-07-01", "2026-03-31", 8.69, unit="USD/shares", form="10-Q"),
    ),
}


def test_msft_fiscal_q4_revenue_is_derived_and_eps_is_none(monkeypatch):
    _stub_lane(monkeypatch, MSFT_FULL_ROWS, MSFT_CONCEPTS)
    fund = sf.build_fundamentals("0000789019", "2026-07-29", None, {}, _far_deadline())

    assert fund["status"] == "ok"
    assert fund["period_end"] == "2026-06-30"
    assert fund["form"] == "10-K"
    assert fund["revenue_q"]["value"] == 90007000000
    assert fund["revenue_q"]["basis"] == "derived"

    assert fund["eps_diluted_q"] is None
    assert "eps_derived_suppressed" in fund["notes"]
    # The number itself must appear nowhere in the published dict.
    values = [v.get("value") for v in fund.values() if isinstance(v, dict)]
    assert 4.81 not in values


def test_msft_missing_concepts_do_not_break_the_company(monkeypatch):
    """A tag with no payload at all yields None for that metric while the rest
    of the dict still publishes."""
    _stub_lane(monkeypatch, MSFT_FULL_ROWS, MSFT_CONCEPTS)
    fund = sf.build_fundamentals("0000789019", "2026-07-29", None, {}, _far_deadline())
    assert fund["status"] == "ok"
    assert fund["operating_income_q"] is None
    assert fund["ocf_ttm"] is None
    assert fund["capex_ttm"] is None
    assert fund["fcf_ttm"] is None
    assert fund["op_margin_pct"] is None
    assert fund["capex_intensity_pct"] is None


AAPL_CONCEPTS = {
    # The first ladder tag 404s for this filer; `Revenues` carries the fact.
    "Revenues": _units(
        _fact("2026-03-29", "2026-06-27", 109417000000),
        _fact("2025-03-30", "2025-06-28", 94036000000),
    ),
}


def test_aapl_404_on_first_ladder_tag_falls_through(monkeypatch):
    fetched = _stub_lane(monkeypatch, AAPL_ROWS, AAPL_CONCEPTS)
    fund = sf.build_fundamentals("0000320193", "2026-07-30", None, {}, _far_deadline())

    assert fund["status"] == "ok"
    assert fund["period_end"] == "2026-06-27"
    assert fund["form"] == "10-Q"
    assert fund["revenue_q"]["value"] == 109417000000
    assert fund["revenue_q"]["tag"] == "Revenues"
    assert fetched[0] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert fetched[1] == "Revenues"


META_ROWS = [
    ("10-Q", "2026-07-30", "2026-06-30", "0001326801-26-000050", 2026, "Q2"),
    ("10-Q", "2026-04-29", "2026-03-31", "0001326801-26-000030", 2026, "Q1"),
    ("10-K", "2026-01-29", "2025-12-31", "0001326801-26-000010", 2025, "FY"),
    ("10-Q", "2025-10-29", "2025-09-30", "0001326801-25-000070", 2025, "Q3"),
    ("10-Q", "2025-07-30", "2025-06-30", "0001326801-25-000050", 2025, "Q2"),
]

# The filing is anchored, but XBRL has not backfilled it: every fact stops one
# quarter short of period_end. This is META-day-1.
META_CONCEPTS = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": _units(
        _fact("2026-01-01", "2026-03-31", 47000000000),
    ),
    "OperatingIncomeLoss": _units(_fact("2026-01-01", "2026-03-31", 17000000000)),
    "NetIncomeLoss": _units(_fact("2026-01-01", "2026-03-31", 14000000000)),
    "StockholdersEquity": _units(_instant("2026-03-31", 200000000000)),
}

_METRIC_KEYS = (
    "revenue_q",
    "operating_income_q",
    "net_income_q",
    "eps_diluted_q",
    "equity",
    "ocf_ttm",
    "capex_ttm",
    "capex_company_ttm",
    "fcf_ttm",
    "rpo",
    "op_margin_pct",
    "capex_intensity_pct",
)


def test_meta_no_facts_at_period_end_is_stale_with_every_metric_none(monkeypatch):
    _stub_lane(monkeypatch, META_ROWS, META_CONCEPTS)
    health = {}
    fund = sf.build_fundamentals("0001326801", "2026-07-30", None, health, _far_deadline())

    assert fund["status"] == "stale"
    for key in _METRIC_KEYS:
        assert fund[key] is None, key
    # The anchor is still known -- stale means "no facts", not "no filing".
    assert fund["period_end"] == "2026-06-30"
    assert fund["accession"] == "0001326801-26-000050"
    assert fund["form"] == "10-Q"
    note = str(health["sec_fundamentals"])
    assert "stale" in note
    assert "2026-06-30" in note


def test_meta_keystone_gate_stops_before_spending_the_other_tags(monkeypatch):
    """A stale company must not cost eight requests to discover."""
    fetched = _stub_lane(monkeypatch, META_ROWS, META_CONCEPTS)
    sf.build_fundamentals("0001326801", "2026-07-30", None, {}, _far_deadline())
    assert set(fetched) <= set(sf.TAG_LADDERS["revenue_q"])


def test_msft_style_fy_only_fact_at_period_end_is_not_stale(monkeypatch):
    """The keystone gate is duration-agnostic: an FY-only fact at period_end
    (MSFT fiscal Q4) counts as 'not stale' even with no quarterly fact."""
    _stub_lane(monkeypatch, MSFT_FULL_ROWS, MSFT_CONCEPTS)
    fund = sf.build_fundamentals("0000789019", "2026-07-29", None, {}, _far_deadline())
    assert fund["status"] == "ok"


def test_no_anchor_filing_is_unavailable(monkeypatch):
    _stub_lane(monkeypatch, AMZN_FULL_ROWS, AMZN_CONCEPTS)
    fund = sf.build_fundamentals("0001018724", "2026-11-30", None, {}, _far_deadline())
    assert fund["status"] == "unavailable"
    assert fund["period_end"] is None
    assert fund["fiscal"] is None
    assert fund["form"] is None
    for key in _METRIC_KEYS:
        assert fund[key] is None, key


def test_published_dict_carries_py_end_and_primary_doc_on_the_ok_path(monkeypatch):
    """Task 12. `anomaly_panel.build_panel` matches its year-ago comparable on an
    EXACT quarter end -- `snaps.get(prior_end)` against real filing dates -- so an
    approximate date does not degrade, it silently empties every YoY metric and no
    YoY flag can fire. The anchor already computes this; it was just being dropped."""
    _stub_lane(monkeypatch, AMZN_FULL_ROWS, AMZN_CONCEPTS)
    fund = sf.build_fundamentals("0001018724", "2026-07-30", None, {}, _far_deadline())
    assert fund["status"] == "ok"
    assert fund["period_end"] == "2026-06-30"
    assert fund["py_end"] == "2025-06-30"
    assert "primary_doc" in fund


def test_py_end_and_primary_doc_present_and_none_on_the_unavailable_path(monkeypatch):
    """Required-AND-nullable, as everywhere else in this dict: a caller reads
    `fund["py_end"]` without first proving the lane succeeded."""
    _stub_lane(monkeypatch, AMZN_FULL_ROWS, AMZN_CONCEPTS)
    fund = sf.build_fundamentals("0001018724", "2026-11-30", None, {}, _far_deadline())
    assert fund["status"] == "unavailable"
    assert fund["py_end"] is None
    assert fund["primary_doc"] is None


def test_py_end_is_none_when_the_filer_has_no_year_ago_comparable(monkeypatch):
    """A newly-public filer anchors fine but has nothing a year back. The key
    still ships, so `build_panel` gets None and returns an honest empty prior
    rather than a wrong one."""
    _stub_lane(monkeypatch, AMZN_ROWS, AMZN_CONCEPTS)
    fund = sf.build_fundamentals("0001018724", "2026-07-30", None, {}, _far_deadline())
    assert fund["period_end"] == "2026-06-30"
    assert fund["py_end"] is None


def test_empty_submissions_is_unavailable(monkeypatch):
    monkeypatch.setattr(sf, "fetch_submissions", lambda cik, session, health: {})
    fund = sf.build_fundamentals("0001018724", "2026-07-30", None, {}, _far_deadline())
    assert fund["status"] == "unavailable"


def test_missing_cik_is_unavailable_without_any_request(monkeypatch):
    calls = []
    monkeypatch.setattr(sf, "fetch_submissions", lambda *a: calls.append(a) or {})
    monkeypatch.setattr(sf, "fetch_concept", lambda *a, **kw: calls.append(a) or {})
    fund = sf.build_fundamentals(None, "2026-07-30", None, {}, _far_deadline())
    assert fund["status"] == "unavailable"
    assert calls == []


def test_deadline_truncates_and_returns_what_resolved(monkeypatch):
    _stub_lane(monkeypatch, AMZN_FULL_ROWS, AMZN_CONCEPTS)
    ticks = iter([0, 10, 20, 30, 40, 50, 60, 70, 80, 90])
    monkeypatch.setattr(sf, "_now", lambda: next(ticks))
    health = {}
    fund = sf.build_fundamentals("0001018724", "2026-07-30", None, health, 25.0)

    # Three tag fetches happen at t=0,10,20; the fourth check trips the deadline.
    assert fund["status"] == "ok"
    assert fund["revenue_q"]["value"] == 200606000000
    assert fund["net_income_q"]["value"] == 62647000000
    assert fund["eps_diluted_q"] is None
    assert fund["ocf_ttm"] is None
    assert "deadline" in str(health["sec_fundamentals"])


def test_deadline_already_past_yields_unavailable(monkeypatch):
    _stub_lane(monkeypatch, AMZN_FULL_ROWS, AMZN_CONCEPTS)
    monkeypatch.setattr(sf, "_now", lambda: 1000.0)
    fund = sf.build_fundamentals("0001018724", "2026-07-30", None, {}, 10.0)
    assert fund["status"] == "unavailable"


def test_build_fundamentals_never_raises(monkeypatch):
    """Global Constraint 1, at the orchestrator level."""

    def boom(*args, **kwargs):
        raise RuntimeError("submissions exploded")

    monkeypatch.setattr(sf, "fetch_submissions", boom)
    health = {}
    fund = sf.build_fundamentals("0001018724", "2026-07-30", None, health, _far_deadline())
    assert fund["status"] == "unavailable"
    assert health["sec_fundamentals"]


def test_zero_denominator_ratios_return_none_not_a_crash(monkeypatch):
    """OCF + capex == 0 must yield None, never ZeroDivisionError or infinity."""
    concepts = dict(AMZN_CONCEPTS)
    concepts["NetCashProvidedByUsedInOperatingActivities"] = _units(
        _fact("2025-07-01", "2026-06-30", -173030000000),
    )
    _stub_lane(monkeypatch, AMZN_FULL_ROWS, concepts)
    fund = sf.build_fundamentals("0001018724", "2026-07-30", None, {}, _far_deadline())
    assert fund["capex_intensity_pct"] is None
    assert fund["fcf_ttm"]["value"] == -173030000000 - 173030000000


def test_zero_revenue_does_not_crash_op_margin(monkeypatch):
    concepts = dict(AMZN_CONCEPTS)
    concepts["RevenueFromContractWithCustomerExcludingAssessedTax"] = _units(
        _fact("2026-04-01", "2026-06-30", 0),
    )
    _stub_lane(monkeypatch, AMZN_FULL_ROWS, concepts)
    fund = sf.build_fundamentals("0001018724", "2026-07-30", None, {}, _far_deadline())
    assert fund["status"] == "ok"
    assert fund["op_margin_pct"] is None


def test_fundamentals_dict_has_every_contract_key(monkeypatch):
    _stub_lane(monkeypatch, AMZN_FULL_ROWS, AMZN_CONCEPTS)
    fund = sf.build_fundamentals("0001018724", "2026-07-30", None, {}, _far_deadline())
    expected = {
        "status", "source", "period_end", "fiscal", "accession", "form",
        # Task 12: what run.py needs to drive the anomaly_panel and
        # sec_filing_text lanes. py_end is the YEAR-AGO end, not the anchor's
        # `prior_end` (previous quarter) -- see the note in _blank_fundamentals.
        "py_end", "primary_doc",
        "revenue_q", "operating_income_q", "net_income_q", "eps_diluted_q",
        "equity", "ocf_ttm", "capex_ttm", "capex_company_ttm", "fcf_ttm", "rpo",
        "op_margin_pct", "capex_intensity_pct", "segments", "notes",
    }
    assert set(fund) == expected


def test_no_roic_and_no_equity_minus_cash_anywhere(monkeypatch):
    """Global Constraint 7, asserted against BOTH the output and the source."""
    _stub_lane(monkeypatch, AMZN_FULL_ROWS, AMZN_CONCEPTS)
    fund = sf.build_fundamentals("0001018724", "2026-07-30", None, {}, _far_deadline())
    assert not [k for k in fund if "roic" in k.lower()]

    with open(sf.__file__, encoding="utf-8") as fh:
        source = fh.read()
    assert "roic" not in source.lower()
    # The cash tags that fed the cut ROIC denominator are not fetched at all.
    assert "CashAndCashEquivalents" not in source
    assert "MarketableSecurities" not in source


def test_tag_overrides_ships_empty():
    """Global Constraint 5: the designated per-company escape hatch stays empty."""
    assert sf._TAG_OVERRIDES == {}


def test_fetch_concept_404_degrades_to_empty_dict(monkeypatch):
    def fake_get_json(url, session):
        raise RuntimeError("404 Client Error")

    monkeypatch.setattr(sf, "_sec_get_json", fake_get_json)
    monkeypatch.setattr(sf, "_sleep", lambda seconds: None)
    assert sf.fetch_concept("0000320193", "Revenues", None) == {}


def test_fetch_concept_builds_the_concept_url(monkeypatch):
    captured = {}

    def fake_get_json(url, session):
        captured["url"] = url
        return {"units": {}}

    monkeypatch.setattr(sf, "_sec_get_json", fake_get_json)
    monkeypatch.setattr(sf, "_sleep", lambda seconds: None)
    sf.fetch_concept("0000320193", "Revenues", None)
    assert captured["url"] == (
        "https://data.sec.gov/api/xbrl/companyconcept/CIK0000320193/us-gaap/Revenues.json"
    )


# A revenue fact EXISTS at period_end but cannot be turned into a quarter value:
# the FY cumulative has no same-`start` cumulative at prior_end to subtract. The
# fact-presence gate alone would call this "ok" and publish a dict with nothing
# in it; §2.3 defines "ok" as the keystone having RESOLVED, so it is stale.
UNRESOLVABLE_REVENUE_CONCEPTS = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": _units(
        _fact("2025-07-01", "2026-06-30", 340007000000, form="10-K"),
        # No cumulative at prior_end (2026-03-31) sharing that start.
    ),
}


def test_revenue_fact_at_period_end_that_cannot_resolve_is_not_ok(monkeypatch):
    _stub_lane(monkeypatch, MSFT_FULL_ROWS, UNRESOLVABLE_REVENUE_CONCEPTS)
    health = {}
    fund = sf.build_fundamentals("0000789019", "2026-07-29", None, health, _far_deadline())

    assert fund["status"] == "stale"
    for key in _METRIC_KEYS:
        assert fund[key] is None, key
    # The two stale causes must be distinguishable in diagnostics.
    assert "did not resolve" in str(health["sec_fundamentals"])


def test_the_two_stale_causes_have_distinct_health_notes(monkeypatch):
    unresolved = {}
    _stub_lane(monkeypatch, MSFT_FULL_ROWS, UNRESOLVABLE_REVENUE_CONCEPTS)
    sf.build_fundamentals("0000789019", "2026-07-29", None, unresolved, _far_deadline())

    absent = {}
    _stub_lane(monkeypatch, META_ROWS, META_CONCEPTS)
    sf.build_fundamentals("0001326801", "2026-07-30", None, absent, _far_deadline())

    assert "stale" in str(unresolved["sec_fundamentals"])
    assert "stale" in str(absent["sec_fundamentals"])
    assert unresolved["sec_fundamentals"]["notes"] != absent["sec_fundamentals"]["notes"]
    assert "did not resolve" not in str(absent["sec_fundamentals"])


def test_truncation_mid_keystone_ladder_is_unavailable_not_ok(monkeypatch):
    """The deadline path must not publish `ok` with a null keystone either: the
    first rung yields a fact at period_end that will not resolve, and the
    deadline trips before the rest of the ladder can be tried. A fact was seen,
    so `keystone_fact_seen` is True -- but nothing is publishable, and we never
    finished the walk, so this is `unavailable` rather than `stale`."""
    _stub_lane(monkeypatch, MSFT_FULL_ROWS, UNRESOLVABLE_REVENUE_CONCEPTS)
    ticks = iter([0, 100, 200, 300])
    monkeypatch.setattr(sf, "_now", lambda: next(ticks))
    fund = sf.build_fundamentals("0000789019", "2026-07-29", None, {}, 50.0)
    assert fund["status"] == "unavailable"
    assert fund["revenue_q"] is None
