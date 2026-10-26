"""Offline anomaly-panel regressions using retained SEC numeric facts.

amd_companyfacts.json records its inherited source URL and capture date;
the 2026-10-26 fixture cleanup did not re-fetch or independently verify that
capture. All numerical facts/period metadata remain unchanged. Taxonomy prose
was removed. See tests/fixtures/README.md.
"""

import copy
import json
import os
import sys

import pytest

# Make scripts/ importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from worldintel.core import anomaly_panel as ap  # noqa: E402
from worldintel.core import sec_fundamentals as sf  # noqa: E402

_FIX = os.path.join(_HERE, "fixtures")

AMD_CIK = "0000002488"
PERIOD_END = "2026-06-27"
PRIOR_END = "2025-06-28"


def load_companyfacts():
    with open(os.path.join(_FIX, "amd_companyfacts.json"), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(autouse=True)
def _clear_cache():
    """The per-run companyfacts cache is module state; each test starts clean."""
    ap._FACTS_CACHE.clear()
    yield
    ap._FACTS_CACHE.clear()


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    """Records every GET so the caching and User-Agent assertions have evidence."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        return _FakeResponse(self.payload)


_UNSET = object()      # so `payload=None` can mean "the endpoint returned null"


def _panel(payload=_UNSET, health=None, deadline_at=None, reaction=None, session=None,
           period_end=PERIOD_END, prior_end=PRIOR_END):
    session = session or _FakeSession(load_companyfacts() if payload is _UNSET else payload)
    health = {} if health is None else health
    if deadline_at is None:
        deadline_at = ap._now() + ap.LANE_DEADLINE_S
    return ap.build_panel(AMD_CIK, "AMD", period_end, prior_end, session, health,
                          deadline_at, reaction=reaction), session, health


def _metric(value, prior=None, delta=None, unit="pp", since=None, benchmark=None):
    """One `metrics` entry in the published shape, for the pure flag tests."""
    if delta is None and value is not None and prior is not None:
        delta = value - prior
    return {"value": value, "prior": prior, "delta": delta, "delta_unit": unit,
            "since": since, "rank": None,
            "benchmark": benchmark or {"median": None, "p20": None, "p80": None}}


def _flag_for(flags, metric):
    for flag in flags:
        if flag.get("metric") == metric:
            return flag
    return None


# --------------------------------------------------------------------------- #
# Constants and lane budget
# --------------------------------------------------------------------------- #
def test_lane_deadline_is_this_modules_own_budget_and_own_clock():
    """45.0 coincides with sec_fundamentals' budget; it is NOT borrowed from it.
    The module owns both halves of `deadline_at` — its own monotonic clock and
    its own constant — mirroring `run._ownership_deadline`'s precedent."""
    assert ap.LANE_DEADLINE_S == 45.0
    assert ap.FLAG_CAP == 3
    assert ap.HISTORY_QUARTERS == 12
    # Own function objects, not re-exports of the sibling module's.
    assert ap._now is not sf._now
    assert ap.LANE_DEADLINE_S is not None
    # A monotonic clock, not a wall clock: two reads never go backwards and are
    # nowhere near the time.time() epoch.
    first = ap._now()
    assert ap._now() >= first


# --------------------------------------------------------------------------- #
# The AMD worked example
# --------------------------------------------------------------------------- #
def test_amd_working_capital_days_reproduce_the_filed_quarter():
    panel, _session, _health = _panel()
    metrics = panel["metrics"]
    assert round(metrics["dso"]["value"], 1) == 57.4
    assert round(metrics["dso"]["prior"], 1) == 60.6
    assert round(metrics["dso"]["delta"], 1) == -3.1
    assert metrics["dso"]["delta_unit"] == "days"

    assert round(metrics["dio"]["value"], 1) == 66.8
    assert round(metrics["dio"]["prior"], 1) == 79.1
    assert round(metrics["dio"]["delta"], 1) == -12.3

    assert round(metrics["dpo"]["value"], 1) == 42.3
    assert round(metrics["dpo"]["prior"], 1) == 36.5
    assert round(metrics["dpo"]["delta"], 1) == 5.8


def test_amd_revenue_matches_the_filed_quarter_and_yoy_is_a_percent():
    panel, _s, _h = _panel()
    revenue = panel["metrics"]["revenue"]
    assert revenue["value"] == 11536000000.0
    assert revenue["prior"] == 7685000000.0
    assert round(revenue["delta"], 1) == 50.1
    assert revenue["delta_unit"] == "pct"


def test_amd_net_margin_sits_above_operating_margin():
    panel, _s, _h = _panel()
    metrics = panel["metrics"]
    assert round(metrics["net_margin_pct"]["value"], 1) == 19.9
    assert round(metrics["operating_margin_pct"]["value"], 1) == 17.3
    assert round(metrics["gross_margin_pct"]["value"], 1) == 53.8
    assert metrics["net_margin_pct"]["value"] > metrics["operating_margin_pct"]["value"]
    assert round(metrics["net_less_operating_pp"]["value"], 1) == 2.7


def test_amd_effective_tax_rate_comes_from_the_reported_rate_tag():
    panel, _s, _h = _panel()
    assert round(panel["metrics"]["effective_tax_rate_pct"]["value"], 1) == 9.8


def test_amd_diluted_share_count_rises_one_point_seven_eight_percent():
    panel, _s, _h = _panel()
    shares = panel["metrics"]["diluted_shares"]
    assert shares["value"] == 1659000000.0
    assert shares["prior"] == 1630000000.0
    assert round(shares["delta"], 2) == 1.78
    assert shares["delta_unit"] == "pct"


def test_every_published_metric_carries_the_full_entry_shape():
    panel, _s, _h = _panel()
    assert panel["ticker"] == "AMD"
    assert panel["period_end"] == PERIOD_END
    assert panel["prior_end"] == PRIOR_END
    assert isinstance(panel["flags"], list)
    assert isinstance(panel["contradictions"], list)
    for name, entry in panel["metrics"].items():
        assert set(entry) == {"value", "prior", "delta", "delta_unit", "since", "rank",
                              "benchmark"}, name
        assert entry["delta_unit"] in ("pct", "days", "pp", "abs"), name


def test_amd_panel_carries_dated_precedents_from_its_own_history():
    """`since` is the dated precedent the PRECEDENT operator cites. AMD's DSO is
    the lowest of the trailing window bar one quarter, so at least one metric
    must carry a date rather than every metric carrying None."""
    panel, _s, _h = _panel()
    dated = [name for name, entry in panel["metrics"].items() if entry["since"]]
    assert dated, "no metric reached an extreme of its own 12-quarter history"
    for name in dated:
        assert panel["metrics"][name]["since"] < PERIOD_END


# --------------------------------------------------------------------------- #
# Selection law: `end == period_end` first, always
# --------------------------------------------------------------------------- #
def _payload(tag, rows, unit="USD"):
    return {"facts": {"us-gaap": {tag: {"units": {unit: rows}}}}}


def test_instant_selector_refuses_a_stale_series_rather_than_substituting():
    """A tag whose newest fact predates the period by years must be unreachable,
    not silently substituted. There is no latest-fact path in this module."""
    facts = _payload("Assets", [
        {"end": "2019-12-31", "val": 1.0},
        {"end": "2020-12-31", "val": 2.0},
    ])
    assert ap._instant(facts, "Assets", "2026-06-27") is None
    assert ap._instant(facts, "Assets", "2020-12-31") == 2.0


def test_instant_selector_ignores_duration_facts_at_the_same_end():
    facts = _payload("Assets", [
        {"start": "2026-03-29", "end": "2026-06-27", "val": 99.0},
        {"end": "2026-06-27", "val": 7.0},
    ])
    assert ap._instant(facts, "Assets", "2026-06-27") == 7.0


def test_duration_selector_matches_the_day_count_window_not_just_the_end():
    """XBRL carries quarterly, half-year and annual facts against the SAME end
    date; only the lo/hi day-count window separates them."""
    facts = _payload("Revenues", [
        {"start": "2026-03-29", "end": "2026-06-27", "val": 90.0},    # 90 days
        {"start": "2025-12-28", "end": "2026-06-27", "val": 181.0},   # 181 days
        {"start": "2025-06-28", "end": "2026-06-27", "val": 364.0},   # 364 days
    ])
    assert ap._duration(facts, "Revenues", "2026-06-27", 80, 100) == 90.0
    assert ap._duration(facts, "Revenues", "2026-06-27", 160, 200) == 181.0
    assert ap._duration(facts, "Revenues", "2026-06-27", 350, 380) == 364.0
    assert ap._duration(facts, "Revenues", "2026-06-27", 200, 300) is None


def test_duration_selector_refuses_a_matching_span_at_the_wrong_end():
    facts = _payload("Revenues", [
        {"start": "2025-03-30", "end": "2025-06-28", "val": 90.0},
    ])
    assert ap._duration(facts, "Revenues", "2026-06-27", 80, 100) is None


def test_selectors_never_raise_on_a_malformed_payload():
    for junk in (None, [], {"facts": None}, {"facts": {"us-gaap": {"Assets": None}}},
                 {"facts": {"us-gaap": {"Assets": {"units": {"USD": "nope"}}}}},
                 {"facts": {"us-gaap": {"Assets": {"units": {"USD": [None, 3]}}}}}):
        assert ap._instant(junk, "Assets", "2026-06-27") is None
        assert ap._duration(junk, "Assets", "2026-06-27", 80, 100) is None


# --------------------------------------------------------------------------- #
# _precedent
# --------------------------------------------------------------------------- #
def test_precedent_dates_the_last_time_a_high_was_seen():
    series = [("2026-03-28", 5.0), ("2025-12-27", 9.0), ("2025-09-27", 4.0),
              ("2025-06-28", 3.0), ("2025-03-29", 2.0), ("2024-12-28", 1.0),
              ("2024-09-28", 6.0), ("2024-06-29", 0.5)]
    out = ap._precedent(series, 10.0)
    assert out["rank"] == 1
    assert out["since"] == "2024-06-29"          # not seen this high inside the window

    out = ap._precedent(series, 8.0)
    assert out["rank"] == 2
    assert out["since"] is None                  # not an extreme -> nothing to date


def test_precedent_dates_the_last_time_a_low_was_seen():
    series = [("2026-03-28", 5.0), ("2025-12-27", 9.0), ("2025-09-27", 4.0),
              ("2025-06-28", 3.0), ("2025-03-29", 2.0), ("2024-12-28", 1.0),
              ("2024-09-28", 6.0), ("2024-06-29", 0.5)]
    out = ap._precedent(series, 0.1)
    assert out["rank"] == len(series) + 1
    assert out["since"] == "2024-06-29"


def test_precedent_is_undated_and_unranked_below_eight_quarters_of_history():
    """`rank` is blanked WITH `since`, not left standing beside it: a renderer
    keying on `rank == 1` would otherwise print "highest since" off a window
    that never earned a precedent."""
    series = [("2026-03-28", 5.0), ("2025-12-27", 9.0), ("2025-09-27", 4.0),
              ("2025-06-28", 3.0), ("2025-03-29", 2.0), ("2024-12-28", 1.0),
              ("2024-09-28", 6.0)]
    assert len(series) < ap.MIN_HISTORY_QUARTERS
    assert ap._precedent(series, 99.0) == {"rank": None, "since": None}
    assert ap._precedent(series, 0.0) == {"rank": None, "since": None}
    assert ap._precedent(series[:1], 99.0) == {"rank": None, "since": None}


def test_precedent_keeps_rank_without_a_date_once_the_window_is_long_enough():
    """Above the threshold the pairing is one-way: "3rd highest of twelve" is a
    real reading even when the value is not at an extreme and carries no date."""
    series = [("2026-03-28", 5.0), ("2025-12-27", 9.0), ("2025-09-27", 4.0),
              ("2025-06-28", 3.0), ("2025-03-29", 2.0), ("2024-12-28", 1.0),
              ("2024-09-28", 6.0), ("2024-06-29", 0.5)]
    assert len(series) >= ap.MIN_HISTORY_QUARTERS
    assert ap._precedent(series, 8.0) == {"rank": 2, "since": None}


def test_a_short_history_metric_publishes_neither_rank_nor_since():
    """AMD's `debt_issued` resolves for too few quarters to earn a precedent —
    it must publish both as None, not a rank off a three-quarter window."""
    panel, _s, _h = _panel()
    debt = panel["metrics"]["debt_issued"]
    assert debt["since"] is None
    assert debt["rank"] is None
    assert debt["benchmark"] == {"median": None, "p20": None, "p80": None}


def test_precedent_never_raises_on_junk():
    assert ap._precedent(None, 1.0) == {"rank": None, "since": None}
    assert ap._precedent([("x", None)], None) == {"rank": None, "since": None}


# --------------------------------------------------------------------------- #
# compute_flags — thresholds fire AT the boundary and not below
# --------------------------------------------------------------------------- #
def test_working_capital_fires_at_three_days_and_not_below():
    fired = ap.compute_flags({"dso": _metric(57.0, 60.0, unit="days")})
    assert _flag_for(fired, "dso") is not None
    quiet = ap.compute_flags({"dso": _metric(57.1, 60.0, unit="days")})
    assert _flag_for(quiet, "dso") is None


def test_working_capital_direction_is_computed_per_metric_in_both_directions():
    metrics = {
        "dso": _metric(57.4, 60.6, unit="days"),
        "dio": _metric(66.8, 79.1, unit="days"),
        "dpo": _metric(42.3, 36.5, unit="days"),
        "revenue": _metric(11536000000.0, 7685000000.0, delta=50.1, unit="pct"),
    }
    flags = ap.compute_flags(metrics)
    assert _flag_for(flags, "dso")["direction"] == "favourable"
    assert _flag_for(flags, "dio")["direction"] == "favourable"
    assert _flag_for(flags, "dpo")["direction"] == "unfavourable"
    # A rising collection period is the mirror image, not a second favourable.
    rising = ap.compute_flags({"dso": _metric(60.6, 57.4, unit="days")})
    assert _flag_for(rising, "dso")["direction"] == "unfavourable"


def test_working_capital_statement_carries_the_days_and_the_revenue_clause():
    metrics = {
        "dpo": _metric(42.3, 36.5, unit="days"),
        "revenue": _metric(11536000000.0, 7685000000.0, delta=50.1, unit="pct"),
    }
    statement = _flag_for(ap.compute_flags(metrics), "dpo")["statement"]
    assert "DPO" in statement
    assert "+5.8 days" in statement
    assert "50.1%" in statement


def test_share_count_fires_at_one_percent_and_direction_follows_the_sign():
    up = ap.compute_flags({"diluted_shares": _metric(1659e6, 1630e6, delta=1.78, unit="pct")})
    assert _flag_for(up, "diluted_shares")["direction"] == "unfavourable"
    assert _flag_for(up, "diluted_shares")["operator"] == "FUNDING LEG"
    down = ap.compute_flags({"diluted_shares": _metric(1600e6, 1630e6, delta=-1.0, unit="pct")})
    assert _flag_for(down, "diluted_shares")["direction"] == "favourable"
    quiet = ap.compute_flags({"diluted_shares": _metric(1630e6, 1630e6, delta=0.99, unit="pct")})
    assert _flag_for(quiet, "diluted_shares") is None


def test_margin_fires_at_two_points_and_not_below():
    fired = ap.compute_flags({"operating_margin_pct": _metric(17.3, 15.3)})
    assert _flag_for(fired, "operating_margin_pct")["direction"] == "favourable"
    falling = ap.compute_flags({"operating_margin_pct": _metric(15.3, 17.3)})
    assert _flag_for(falling, "operating_margin_pct")["direction"] == "unfavourable"
    quiet = ap.compute_flags({"operating_margin_pct": _metric(17.2, 15.3)})
    assert _flag_for(quiet, "operating_margin_pct") is None


def test_net_above_operating_inversion_fires_as_a_regime_flag():
    fired = ap.compute_flags({"net_less_operating_pp": _metric(2.7, -1.0)})
    flag = _flag_for(fired, "net_less_operating_pp")
    assert flag["operator"] == "REGIME"
    assert flag["direction"] == "unfavourable"
    assert "below the line" in flag["statement"]
    quiet = ap.compute_flags({"net_less_operating_pp": _metric(-3.0, -1.0)})
    assert _flag_for(quiet, "net_less_operating_pp") is None


def test_effective_tax_rate_fires_five_points_from_the_statutory_rate():
    assert ap.STATUTORY_TAX_RATE_PCT == 21.0
    fired = ap.compute_flags({"effective_tax_rate_pct": _metric(9.8, 12.0)})
    flag = _flag_for(fired, "effective_tax_rate_pct")
    assert flag["operator"] == "PRECEDENT"
    assert flag["direction"] == "neutral"          # no settled reading; spec §3
    quiet = ap.compute_flags({"effective_tax_rate_pct": _metric(16.1, 12.0)})
    assert _flag_for(quiet, "effective_tax_rate_pct") is None


def test_effective_tax_rate_also_fires_five_points_from_its_trailing_median():
    metric = _metric(16.1, 12.0, benchmark={"median": 3.0, "p20": None, "p80": None})
    fired = ap.compute_flags({"effective_tax_rate_pct": metric})
    assert _flag_for(fired, "effective_tax_rate_pct") is not None


def test_capex_intensity_fires_above_the_ratio_or_on_the_yoy_step():
    high = ap.compute_flags({"capex_to_ocf_pct": _metric(80.1, 40.0, delta=0.0)})
    assert _flag_for(high, "capex_to_ocf_pct")["direction"] == "neutral"
    step = ap.compute_flags({"capex_to_ocf_pct": _metric(34.2, 14.1, delta=15.0)})
    assert _flag_for(step, "capex_to_ocf_pct") is not None
    quiet = ap.compute_flags({"capex_to_ocf_pct": _metric(34.2, 20.3, delta=14.9)})
    assert _flag_for(quiet, "capex_to_ocf_pct") is None


def test_buybacks_below_share_based_compensation_are_unfavourable():
    fired = ap.compute_flags({"buyback_to_sbc": _metric(0.44, 1.3, unit="abs")})
    flag = _flag_for(fired, "buyback_to_sbc")
    assert flag["operator"] == "FUNDING LEG"
    assert flag["direction"] == "unfavourable"
    quiet = ap.compute_flags({"buyback_to_sbc": _metric(1.0, 1.3, unit="abs")})
    assert _flag_for(quiet, "buyback_to_sbc") is None


def test_accrual_ratio_fires_only_outside_its_own_trailing_quintiles():
    bench = {"median": -1.0, "p20": -1.6, "p80": -0.4}
    high = ap.compute_flags({"accrual_ratio_pct": _metric(-0.1, -1.5, benchmark=bench)})
    assert _flag_for(high, "accrual_ratio_pct")["direction"] == "unfavourable"
    low = ap.compute_flags({"accrual_ratio_pct": _metric(-2.0, -1.5, benchmark=bench)})
    assert _flag_for(low, "accrual_ratio_pct")["direction"] == "favourable"
    inside = ap.compute_flags({"accrual_ratio_pct": _metric(-0.9, -1.5, benchmark=bench)})
    assert _flag_for(inside, "accrual_ratio_pct") is None
    # No history -> no quintile -> no flag, never a guessed one.
    blind = ap.compute_flags({"accrual_ratio_pct": _metric(-0.1, -1.5)})
    assert _flag_for(blind, "accrual_ratio_pct") is None


def test_flag_cap_is_honoured_and_flags_are_ranked_by_absolute_deviation():
    """Each family contributes a champion before any family contributes a
    second, and the surfaced list stays ordered by |deviation|."""
    metrics = {
        "dso": _metric(50.0, 60.0, unit="days"),                 # 10 days -> 3.33
        "dio": _metric(66.8, 79.1, unit="days"),                 # 12.3 days -> 4.09
        "dpo": _metric(42.3, 36.5, unit="days"),                 # 5.8 days -> 1.93
        "operating_margin_pct": _metric(17.3, 15.0),             # 2.3pp -> 1.15
        "diluted_shares": _metric(1659e6, 1630e6, delta=1.78, unit="pct"),   # 1.78
    }
    flags = ap.compute_flags(metrics)
    assert len(flags) == ap.FLAG_CAP
    deviations = [flag["deviation"] for flag in flags]
    assert deviations == sorted(deviations, reverse=True)
    # dso (3.33) outranks both survivors below but is the working-capital
    # family's SECOND flag, so it yields to the other two families' champions.
    assert [f["metric"] for f in flags] == ["dio", "diluted_shares",
                                            "operating_margin_pct"]
    for flag in flags:
        assert flag["deviation"] >= 0.0


def test_compute_flags_is_pure_deterministic_and_non_mutating():
    metrics = {
        "dso": _metric(57.4, 60.6, unit="days"),
        "dpo": _metric(42.3, 36.5, unit="days"),
        "revenue": _metric(11536000000.0, 7685000000.0, delta=50.1, unit="pct"),
    }
    snapshot = copy.deepcopy(metrics)
    first = ap.compute_flags(metrics)
    second = ap.compute_flags(metrics)
    assert first == second
    assert metrics == snapshot


def test_compute_flags_never_raises_on_malformed_metrics():
    for junk in (None, [], "nope", {"dso": None}, {"dso": {"value": "x", "delta": "y"}},
                 {"dso": {}}, {"dso": _metric(None, None, unit="days")}):
        assert ap.compute_flags(junk) == []


# --------------------------------------------------------------------------- #
# contradictions — computed, never composed
# --------------------------------------------------------------------------- #
def test_contradiction_emitted_for_a_beat_with_a_negative_price_reaction():
    flags = ap.compute_flags({"dso": _metric(57.4, 60.6, unit="days")})
    pairs = ap.compute_contradictions(flags, {"surprise_pct": 1.78, "price_move_pct": -7.04})
    assert len(pairs) == 1
    assert pairs[0]["a"] == "surprise_pct"
    assert pairs[0]["b"] == "price_move_pct"
    assert "1.78" in pairs[0]["statement"]
    assert "7.04" in pairs[0]["statement"]


def test_no_contradiction_when_the_beat_and_the_reaction_agree():
    flags = ap.compute_flags({"dso": _metric(57.4, 60.6, unit="days")})
    assert ap.compute_contradictions(flags, {"surprise_pct": 1.78, "price_move_pct": 4.0}) == []
    assert ap.compute_contradictions(flags, {"surprise_pct": -3.0, "price_move_pct": -7.04}) == []
    assert ap.compute_contradictions(flags, None) == []


def test_no_contradiction_when_flags_share_a_direction():
    flags = ap.compute_flags({
        "dso": _metric(57.4, 60.6, unit="days"),
        "dio": _metric(66.8, 79.1, unit="days"),
    })
    assert len(flags) == 2
    assert {f["direction"] for f in flags} == {"favourable"}
    assert ap.compute_contradictions(flags) == []


def test_contradiction_emitted_for_two_flags_of_opposing_direction():
    flags = ap.compute_flags({
        "dso": _metric(57.4, 60.6, unit="days"),          # favourable
        "dpo": _metric(42.3, 36.5, unit="days"),          # unfavourable
    })
    pairs = ap.compute_contradictions(flags)
    assert len(pairs) == 1
    assert {pairs[0]["a"], pairs[0]["b"]} == {"dso", "dpo"}
    assert pairs[0]["statement"]


def test_neutral_flags_never_generate_a_contradiction():
    flags = ap.compute_flags({
        "effective_tax_rate_pct": _metric(9.8, 12.0),      # neutral
        "dso": _metric(57.4, 60.6, unit="days"),           # favourable
    })
    assert {f["direction"] for f in flags} == {"neutral", "favourable"}
    assert ap.compute_contradictions(flags) == []


def test_compute_contradictions_never_raises_on_junk():
    assert ap.compute_contradictions(None) == []
    assert ap.compute_contradictions([{"nope": 1}]) == []
    assert ap.compute_contradictions([], {"surprise_pct": "x", "price_move_pct": None}) == []


# --------------------------------------------------------------------------- #
# build_panel — degradation, status, network discipline
# --------------------------------------------------------------------------- #
def test_status_is_partial_while_the_filing_text_families_are_absent():
    """Segment and off-balance-sheet depend on `sec_filing_text`, which does not
    exist yet. Absent families are absent from `metrics` — never stubbed — and
    the panel says so by reporting `partial`."""
    panel, _s, _h = _panel()
    assert panel["status"] == "partial"
    assert "segment_revenue_share_pp" not in panel["metrics"]
    assert "off_balance_sheet_ratio" not in panel["metrics"]


def test_malformed_payload_yields_unavailable_without_raising():
    panel, _s, health = _panel(payload={"facts": {"us-gaap": {"Nonsense": {}}}})
    assert panel == {"status": "unavailable", "ticker": "AMD", "period_end": PERIOD_END,
                     "prior_end": PRIOR_END, "metrics": {}, "flags": [], "contradictions": []}
    assert health["anomaly_panel"]["notes"]


def test_garbage_payload_types_yield_unavailable_without_raising():
    for junk in (None, [], "nope", {"facts": "nope"}, {"facts": {"us-gaap": []}}):
        panel, _s, health = _panel(payload=junk)
        assert panel["status"] == "unavailable"
        assert panel["metrics"] == {}
        assert health["anomaly_panel"]


def test_network_failure_yields_unavailable_plus_a_health_entry():
    class _Boom:
        def get(self, *args, **kwargs):
            raise RuntimeError("connection reset")

    health = {}
    panel = ap.build_panel(AMD_CIK, "AMD", PERIOD_END, PRIOR_END, _Boom(), health,
                           ap._now() + ap.LANE_DEADLINE_S)
    assert panel["status"] == "unavailable"
    assert panel["metrics"] == {}
    assert panel["flags"] == []
    assert health["anomaly_panel"]["ok"] is False
    assert "connection reset" in health["anomaly_panel"]["note"]


def test_a_period_absent_from_xbrl_yields_unavailable_not_a_nearby_quarter():
    panel, _s, health = _panel(period_end="2026-09-26", prior_end="2025-09-27")
    assert panel["status"] == "unavailable"
    assert panel["metrics"] == {}
    assert health["anomaly_panel"]["notes"]


def test_deadline_exhaustion_returns_partial_and_never_touches_the_network():
    session = _FakeSession(load_companyfacts())
    health = {}
    panel = ap.build_panel(AMD_CIK, "AMD", PERIOD_END, PRIOR_END, session, health,
                           ap._now() - 1.0)
    assert panel["status"] == "partial"
    assert panel["metrics"] == {}
    assert panel["flags"] == []
    assert session.calls == []
    assert health["anomaly_panel"]["notes"]


def test_companyfacts_is_fetched_once_per_cik_per_run():
    session = _FakeSession(load_companyfacts())
    for _ in range(3):
        ap.build_panel(AMD_CIK, "AMD", PERIOD_END, PRIOR_END, session, {},
                       ap._now() + ap.LANE_DEADLINE_S)
    assert len(session.calls) == 1


def test_the_single_request_carries_the_sec_contact_user_agent():
    panel, session, _h = _panel()
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == ap.COMPANYFACTS_URL.format(cik=AMD_CIK)
    assert "@" in call["headers"]["User-Agent"]
    assert call["headers"]["User-Agent"] == ap._sec_ua()
    assert call["timeout"]


def test_build_panel_reaction_arm_reaches_the_published_contradictions():
    panel, _s, _h = _panel(reaction={"surprise_pct": 1.78, "price_move_pct": -7.04})
    assert any(pair["a"] == "surprise_pct" for pair in panel["contradictions"])
    quiet, _s2, _h2 = _panel()
    assert all(pair["a"] != "surprise_pct" for pair in quiet["contradictions"])


def test_build_panel_paces_its_request_like_the_sibling_sec_lanes(monkeypatch):
    slept = []
    monkeypatch.setattr(ap, "_sleep", lambda seconds: slept.append(seconds))
    _panel()
    assert slept == [ap.REQUEST_PACING_S]


# --------------------------------------------------------------------------- #
# The deviation scale — one convention across every arm
# --------------------------------------------------------------------------- #
def _deviation_for(metrics, metric):
    flag = _flag_for(ap.compute_flags(metrics), metric)
    return None if flag is None else flag["deviation"]


def test_every_rule_scores_one_at_its_own_boundary():
    """The declared convention: the tested quantity in multiples of the rule's
    own threshold, so a reading exactly at any boundary scores 1.0. Without it a
    boundary breach in one family outranks a severe breach in another purely
    because the two arms chose different scales."""
    at_boundary = {
        "dso": _deviation_for({"dso": _metric(57.0, 60.0, unit="days")}, "dso"),
        "operating_margin_pct": _deviation_for(
            {"operating_margin_pct": _metric(17.0, 15.0)}, "operating_margin_pct"),
        "diluted_shares": _deviation_for(
            {"diluted_shares": _metric(1e9, 1e9, delta=1.0, unit="pct")}, "diluted_shares"),
        "effective_tax_rate_pct": _deviation_for(
            {"effective_tax_rate_pct": _metric(16.0, 16.0)}, "effective_tax_rate_pct"),
        "buyback_to_sbc": _deviation_for(
            {"buyback_to_sbc": _metric(0.999999, 1.3, unit="abs")}, "buyback_to_sbc"),
        "capex_to_ocf_pct": _deviation_for(
            {"capex_to_ocf_pct": _metric(80.001, 80.0, delta=0.0)}, "capex_to_ocf_pct"),
    }
    for metric, deviation in at_boundary.items():
        assert deviation is not None, metric
        assert round(deviation, 3) == 1.0, (metric, deviation)


def test_severity_climbs_above_the_boundary_on_every_arm():
    assert _deviation_for({"dso": _metric(54.0, 60.0, unit="days")}, "dso") == 2.0
    assert _deviation_for({"capex_to_ocf_pct": _metric(160.0, 10.0, delta=0.0)},
                          "capex_to_ocf_pct") == 2.0
    assert _deviation_for({"buyback_to_sbc": _metric(0.0, 1.3, unit="abs")},
                          "buyback_to_sbc") == 2.0


def test_a_maximal_buyback_breach_outranks_a_bare_minimum_margin_move():
    """A company that repurchased NOTHING against real share-based compensation
    has committed the maximal possible breach of that rule. Scored as a shortfall
    over the threshold it was mathematically capped at 1.0 and merely tied with a
    bare-minimum 2.0pp margin move; it must outrank it."""
    metrics = {
        "buyback_to_sbc": _metric(0.0, 1.3, unit="abs"),
        "operating_margin_pct": _metric(17.0, 15.0),
    }
    flags = ap.compute_flags(metrics)
    assert [flag["metric"] for flag in flags] == ["buyback_to_sbc", "operating_margin_pct"]
    assert (_flag_for(flags, "buyback_to_sbc")["deviation"]
            > _flag_for(flags, "operating_margin_pct")["deviation"])


def test_capex_at_full_operating_cash_flow_outranks_a_bare_minimum_margin_move():
    """Capex consuming 100% of operating cash flow scored 0.25 under an
    excess-over-threshold scale — four times BELOW a minimum margin move."""
    metrics = {
        "capex_to_ocf_pct": _metric(100.0, 40.0, delta=0.0),
        "operating_margin_pct": _metric(17.0, 15.0),
    }
    assert [flag["metric"] for flag in ap.compute_flags(metrics)] == [
        "capex_to_ocf_pct", "operating_margin_pct"]


def test_accrual_deviation_is_floored_against_a_narrow_trailing_range():
    """The accrual scale is the filer's own interquintile range — data, which can
    approach zero. Unfloored, a filer with a very stable accrual history turns a
    rounding-scale excursion into an enormous deviation and takes a capped slot."""
    narrow = {"median": -1.0, "p20": -1.005, "p80": -0.995}          # range 0.01
    metrics = {
        "accrual_ratio_pct": _metric(-0.895, -1.0, benchmark=narrow),   # 0.1 past p80
        "operating_margin_pct": _metric(17.0, 15.0),                    # boundary: 1.0
    }
    flags = ap.compute_flags(metrics)
    accrual = _flag_for(flags, "accrual_ratio_pct")
    assert accrual is not None
    # Unfloored this was 0.1 / 0.01 = 10.0 and would have topped the panel.
    assert accrual["deviation"] == pytest.approx(1.0 + 0.1 / ap.ACCRUAL_RANGE_FLOOR_PP)
    assert [flag["metric"] for flag in flags] == ["accrual_ratio_pct",
                                                  "operating_margin_pct"]


def test_accrual_deviation_uses_the_real_range_when_it_is_wide_enough():
    wide = {"median": -1.0, "p20": -1.6, "p80": -0.4}                 # range 1.2 > floor
    metrics = {"accrual_ratio_pct": _metric(-0.1, -1.5, benchmark=wide)}
    flag = _flag_for(ap.compute_flags(metrics), "accrual_ratio_pct")
    assert flag["deviation"] == pytest.approx(1.0 + 0.3 / 1.2)


# --------------------------------------------------------------------------- #
# `rank` travels with `since`
# --------------------------------------------------------------------------- #
def test_amd_metrics_carry_rank_alongside_since():
    """"Lowest/highest since YYYY-MM-DD" is not renderable from a bare date — the
    direction has to come from the panel, not from a downstream guess."""
    panel, _s, _h = _panel()
    revenue = panel["metrics"]["revenue"]
    assert revenue["rank"] == 1                      # highest of the window
    assert revenue["since"] == "2023-07-01"
    for name, entry in panel["metrics"].items():
        if entry["since"] is not None:
            assert entry["rank"] is not None, name


def test_a_dated_low_is_distinguishable_from_a_dated_high_by_rank():
    series = [("2026-03-28", 5.0), ("2025-12-27", 9.0), ("2025-09-27", 4.0),
              ("2025-06-28", 3.0), ("2025-03-29", 2.0), ("2024-12-28", 1.0),
              ("2024-09-28", 6.0), ("2024-06-29", 0.5)]
    high = ap._metric_entry("dso", 10.0, 9.0, series)
    low = ap._metric_entry("dso", 0.1, 9.0, series)
    assert high["since"] == low["since"] == "2024-06-29"    # same date, opposite meaning
    assert high["rank"] == 1
    assert low["rank"] == len(series) + 1


# --------------------------------------------------------------------------- #
# Health accumulation, and the tax-rate delta guard
# --------------------------------------------------------------------------- #
def test_a_fetch_failure_preserves_notes_from_earlier_companies_in_the_run():
    """The lane processes several reporters per run against one `health` dict."""
    class _Boom:
        def get(self, *args, **kwargs):
            raise RuntimeError("connection reset")

    health = {}
    ap._health_note(health, "cik=0000320193 earlier note")
    ap.build_panel(AMD_CIK, "AMD", PERIOD_END, PRIOR_END, _Boom(), health,
                   ap._now() + ap.LANE_DEADLINE_S)
    entry = health["anomaly_panel"]
    assert entry["ok"] is False
    assert "connection reset" in entry["note"]
    assert "cik=0000320193 earlier note" in entry["notes"]


def test_effective_tax_rate_delta_is_suppressed_when_the_prior_is_not_a_rate():
    """AMD's year-ago quarter filed a real 1263.6% effective rate on a near-zero
    pre-tax base. `value` stands — it is what was filed — but differencing it
    yields −1253.8pp, which is not a number this module may hand a renderer."""
    panel, _s, _h = _panel()
    entry = panel["metrics"]["effective_tax_rate_pct"]
    assert round(entry["value"], 1) == 9.8
    assert round(entry["prior"], 1) == 1263.6
    assert entry["delta"] is None
    assert ap.ETR_COMPARABLE_MAX_PCT == 100.0


def test_effective_tax_rate_delta_survives_two_ordinary_rates():
    assert ap._metric_entry("effective_tax_rate_pct", 9.8, 14.8, [])["delta"] == \
        pytest.approx(-5.0)


def test_the_tax_flag_still_fires_when_its_delta_is_suppressed():
    """The rule reads the value against a benchmark, never against `delta`, so
    suppressing the delta must not silence the finding."""
    panel, _s, _h = _panel()
    assert panel["metrics"]["effective_tax_rate_pct"]["delta"] is None
    fired = ap._tax_flag(panel["metrics"])
    assert fired and fired[0]["metric"] == "effective_tax_rate_pct"


# --------------------------------------------------------------------------- #
# One flag per family first, then backfill by deviation
# --------------------------------------------------------------------------- #
def test_amd_surfaces_inventory_days_alongside_a_margin_flag():
    """The regression this rule exists to prevent. Ranked purely by severity AMD
    surfaced three mechanically linked margin lines and dropped a genuinely clean
    working-capital quarter, which spec §2 requires the brief to be able to
    report."""
    panel, _s, _h = _panel()
    surfaced = [flag["metric"] for flag in panel["flags"]]
    assert surfaced == ["operating_margin_pct", "dio", "effective_tax_rate_pct"]
    dio = _flag_for(panel["flags"], "dio")
    assert dio["direction"] == "favourable"
    assert "-12.3 days" in dio["statement"]
    # ... and never more than one line from the same family.
    families = [ap._FAMILY_OF[metric] for metric in surfaced]
    assert len(set(families)) == len(families)


def test_the_single_most_severe_flag_always_survives_family_diversity():
    """Diversity may break up a monoculture behind the leader; it may never
    displace the leader itself."""
    metrics = {
        "operating_margin_pct": _metric(40.0, 10.0),                     # 15.0 — the leader
        "gross_margin_pct": _metric(60.0, 40.0),                         # 10.0, same family
        "dso": _metric(57.0, 60.0, unit="days"),                         # 1.0
        "diluted_shares": _metric(1e9, 1e9, delta=1.0, unit="pct"),      # 1.0
        "effective_tax_rate_pct": _metric(16.0, 16.0),                   # 1.0
    }
    flags = ap.compute_flags(metrics)
    assert flags[0]["metric"] == "operating_margin_pct"
    assert flags[0]["deviation"] == max(flag["deviation"] for flag in flags)
    # The second margin line yields even though it outranks all three champions.
    assert "gross_margin_pct" not in [flag["metric"] for flag in flags]


def test_one_family_can_still_fill_every_slot():
    """Fewer families than slots is not a reason to publish a short panel."""
    metrics = {
        "dso": _metric(50.0, 60.0, unit="days"),
        "dio": _metric(66.8, 79.1, unit="days"),
        "dpo": _metric(42.3, 36.5, unit="days"),
    }
    flags = ap.compute_flags(metrics)
    assert len(flags) == ap.FLAG_CAP
    assert [flag["metric"] for flag in flags] == ["dio", "dso", "dpo"]
    assert {ap._FAMILY_OF[flag["metric"]] for flag in flags} == {"working_capital"}


def test_backfill_takes_the_most_severe_of_what_is_left():
    """Two families, three slots: both champions, then the better runner-up."""
    metrics = {
        "dio": _metric(66.8, 79.1, unit="days"),                 # 4.09  wc champion
        "dpo": _metric(42.3, 36.5, unit="days"),                 # 1.93  wc runner-up
        "dso": _metric(57.4, 60.6, unit="days"),                 # 1.04  wc, last
        "operating_margin_pct": _metric(17.3, 11.3),             # 3.00  margin champion
    }
    assert [flag["metric"] for flag in ap.compute_flags(metrics)] == [
        "dio", "operating_margin_pct", "dpo"]


def test_family_selection_is_pure_and_deterministic():
    metrics = {
        "dio": _metric(66.8, 79.1, unit="days"),
        "operating_margin_pct": _metric(17.3, 11.3),
        "diluted_shares": _metric(1659e6, 1630e6, delta=1.78, unit="pct"),
        "effective_tax_rate_pct": _metric(9.8, 12.0),
    }
    snapshot = copy.deepcopy(metrics)
    assert ap.compute_flags(metrics) == ap.compute_flags(metrics)
    assert metrics == snapshot
