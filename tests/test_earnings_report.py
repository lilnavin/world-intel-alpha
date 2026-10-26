"""Tests for the post-earnings data layer (scripts/earnings_report.py).

Fully offline: every network- or yfinance-touching helper (_get_json, _get_text,
_yf_batch_moves, _yf_earnings_row, _finnhub_profile, _yf_company_info,
_finnhub_peers, _yf_beat_history, _yf_fwd_estimates, _yf_upgrades) is
monkeypatched — or, for
the yfinance helpers' own logic, a fake yfinance module is injected — so the
suite exercises normalization, gating, EDGAR routing, analyst-context
enrichment, covered-set persistence, and the build orchestration without a
single external call.
"""

import copy
import json
import os
import sys
import types
from datetime import date, timedelta

# Make scripts/ importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from worldintel.core import earnings_report as er  # noqa: E402
from worldintel.core import post_earnings_pool as pep  # noqa: E402
from worldintel.core import sec_fundamentals as sf  # noqa: E402

TODAY = "2026-07-11"

CONTEXT_DEFAULTS = {
    "market_cap_m": None, "industry": "", "sector": "", "description": "",
    "peers": [], "beat_streak": [], "fwd_eps_q": None, "fwd_eps_fy": None,
    "analyst_count": None, "revision_30d_pct": None,
    "buyback": None, "dividend": None, "analyst_moves": None, "rev_growth_fy": None,
    "fundamentals": None, "fundamentals_flags": [],
}

FINAL_KEYS = {
    "ticker", "name", "report_date", "hour", "eps_est", "eps_act", "surprise_pct",
    "rev_est", "rev_act", "rev_act_source", "price_move_pct", "release_excerpt",
    "source", "gates",
} | set(CONTEXT_DEFAULTS)

EMPTY_PROFILE = {"name": "", "market_cap_m": None, "industry": ""}


def _fake_yf(monkeypatch, ticker_cls):
    """Inject a fake yfinance module so the lazy `import yfinance` inside the
    helpers resolves to our stub (fully offline)."""
    mod = types.ModuleType("yfinance")
    mod.Ticker = ticker_cls
    monkeypatch.setitem(sys.modules, "yfinance", mod)


def _stub_context(monkeypatch, fail_tickers=()):
    """Stub the four per-ticker context routes with fixed values; tickers in
    fail_tickers raise from every route."""
    def guard(ticker):
        if ticker in fail_tickers:
            raise RuntimeError("context down: " + ticker)

    def fake_info(t):
        guard(t)
        return {"sector": "Technology", "description": "Makes things."}

    def fake_peers(t, c, s):
        guard(t)
        return ["MSFT", "GOOG"]

    def fake_streak(t):
        guard(t)
        return [43.0, 6.0, -11.0, 91.0]

    def fake_est(t):
        guard(t)
        return {"fwd_eps_q": 2.15, "fwd_eps_fy": 9.1,
                "analyst_count": 12, "revision_30d_pct": 7.5}

    monkeypatch.setattr(er, "_yf_company_info", fake_info)
    monkeypatch.setattr(er, "_finnhub_peers", fake_peers)
    monkeypatch.setattr(er, "_yf_beat_history", fake_streak)
    monkeypatch.setattr(er, "_yf_fwd_estimates", fake_est)
    # No analyst-moves data by default; individual tests override this to
    # exercise the deferred-name path.
    monkeypatch.setattr(er, "_yf_upgrades", lambda t: None)


def _reporter(ticker="ZZZQ", report_date="2026-07-10", eps_est=None, eps_act=None,
              surprise=None, source="us"):
    """Minimal normalized reporter row, as fetch_finnhub_calendar would emit."""
    return {
        "ticker": ticker, "name": "", "report_date": report_date, "hour": "",
        "eps_est": eps_est, "eps_act": eps_act, "rev_est": None, "rev_act": None,
        "surprise_pct": surprise, "source": source,
    }


# --------------------------------------------------------------------------- #
# _FINAL_KEYS / _CONTEXT_DEFAULTS contract (Spec B D1)
# --------------------------------------------------------------------------- #
def test_final_keys_contract_includes_content_layer_fields():
    for key in ("buyback", "dividend", "analyst_moves", "rev_growth_fy"):
        assert key in er._FINAL_KEYS


def test_context_defaults_contract_includes_content_layer_fields():
    assert er._CONTEXT_DEFAULTS["buyback"] is None
    assert er._CONTEXT_DEFAULTS["dividend"] is None
    assert er._CONTEXT_DEFAULTS["analyst_moves"] is None
    assert er._CONTEXT_DEFAULTS["rev_growth_fy"] is None


def test_final_keys_contract_includes_fundamentals_fields():
    for key in ("fundamentals", "fundamentals_flags"):
        assert key in er._FINAL_KEYS


def test_context_defaults_fundamentals_are_none_and_empty_list():
    # Global aliasing trap: a `list` default is copied per candidate by the
    # existing defaults loop; anything else (e.g. `{}`) would be shared by
    # reference. None and [] are the only safe choices here.
    assert er._CONTEXT_DEFAULTS["fundamentals"] is None
    assert er._CONTEXT_DEFAULTS["fundamentals_flags"] == []


# --------------------------------------------------------------------------- #
# fetch_finnhub_calendar
# --------------------------------------------------------------------------- #
def test_finnhub_no_key_returns_empty_with_health():
    health = {}
    out = er.fetch_finnhub_calendar({}, "unused", health, TODAY)
    assert out == []
    assert health["finnhub"] == {"ok": False, "note": "no api key"}


def test_finnhub_normalizes_and_drops_unreported(monkeypatch, tmp_path):
    captured = {}

    def fake_get_json(url, session, params=None, headers=None):
        captured["url"] = url
        captured["params"] = params
        return {"earningsCalendar": [
            {"symbol": "aapl", "date": "2026-07-10", "hour": "amc", "quarter": 3,
             "year": 2026, "epsActual": 1.2, "epsEstimate": 1.0,
             "revenueActual": 100.0, "revenueEstimate": 90.0},
            # Not yet reported (epsActual missing) -> dropped.
            {"symbol": "MSFT", "date": "2026-07-13", "hour": "amc",
             "epsActual": None, "epsEstimate": 2.0},
            # Negative estimate: denominator must be abs(est).
            {"symbol": "NEG", "date": "2026-07-09", "hour": "bmo",
             "epsActual": -1.5, "epsEstimate": -1.0},
            # No estimate -> surprise_pct None but row kept.
            {"symbol": "NOEST", "date": "2026-07-09", "hour": "",
             "epsActual": 0.5, "epsEstimate": None},
        ]}

    monkeypatch.setattr(er, "_get_json", fake_get_json)
    health = {}
    out = er.fetch_finnhub_calendar({"finnhub_key": "k"}, str(tmp_path), health, TODAY)

    assert captured["url"] == er.FINNHUB_URL
    assert captured["params"]["token"] == "k"
    assert captured["params"]["from"] == "2026-07-06"  # today - WINDOW_DAYS
    assert captured["params"]["to"] == TODAY

    assert [r["ticker"] for r in out] == ["AAPL", "NEG", "NOEST"]
    aapl = out[0]
    assert aapl["surprise_pct"] == 20.0
    assert aapl["hour"] == "amc"
    assert aapl["rev_act"] == 100.0 and aapl["rev_est"] == 90.0
    assert aapl["source"] == "us"
    assert out[1]["surprise_pct"] == -50.0  # (-1.5 - -1.0) / |-1.0| * 100
    assert out[2]["surprise_pct"] is None
    assert health["finnhub"]["ok"] is True
    assert os.path.exists(os.path.join(str(tmp_path), "finnhub_earnings.json"))


# --------------------------------------------------------------------------- #
# correct_forward_calendar. Regression: yfinance's next-earnings date lags the
# actual report, so on 2026-07-30 the forward calendar still advertised
# "Microsoft and Meta report today" the morning after they reported AMC on
# 2026-07-29. Finnhub had the right date and the actuals; trust it.
# --------------------------------------------------------------------------- #
def _finnhub_dump(tmp_path, rows):
    with open(os.path.join(str(tmp_path), "finnhub_earnings.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"earningsCalendar": rows}, fh)
    return str(tmp_path)


def test_forward_calendar_drops_a_name_that_already_reported(tmp_path):
    raw = _finnhub_dump(tmp_path, [
        {"symbol": "MSFT", "date": "2026-07-29", "hour": "amc",
         "epsActual": 4.74, "epsEstimate": 4.3274},
    ])
    rows = [{"ticker": "MSFT", "name": "Microsoft", "date": "2026-07-30"}]
    assert er.correct_forward_calendar(rows, raw, "2026-07-30") == []


def test_forward_calendar_drops_a_past_report_still_awaiting_actuals(tmp_path):
    # Reported last night, vendor hasn't filled epsActual yet — the date alone
    # already proves it is behind us.
    raw = _finnhub_dump(tmp_path, [
        {"symbol": "META", "date": "2026-07-29", "hour": "amc",
         "epsActual": None, "epsEstimate": 7.3594},
    ])
    rows = [{"ticker": "META", "name": "Meta Platforms", "date": "2026-07-30"}]
    assert er.correct_forward_calendar(rows, raw, "2026-07-30") == []


def test_forward_calendar_keeps_a_genuine_same_day_reporter(tmp_path):
    # Finnhub lists it for today and it has not reported yet — still upcoming.
    raw = _finnhub_dump(tmp_path, [
        {"symbol": "AAPL", "date": "2026-07-30", "hour": "amc",
         "epsActual": None, "epsEstimate": 1.9271},
    ])
    rows = [{"ticker": "AAPL", "name": "Apple", "date": "2026-07-30"}]
    assert er.correct_forward_calendar(rows, raw, "2026-07-30") == rows


def test_forward_calendar_keeps_names_absent_from_the_us_calendar(tmp_path):
    # Intl tickers never appear in the Finnhub US calendar — untouched.
    raw = _finnhub_dump(tmp_path, [{"symbol": "MSFT", "date": "2026-07-29",
                                    "epsActual": 4.74, "epsEstimate": 4.3}])
    rows = [{"ticker": "7203.T", "name": "Toyota", "date": "2026-08-04"}]
    assert er.correct_forward_calendar(rows, raw, "2026-07-30") == rows


def test_forward_calendar_without_a_dump_changes_nothing(tmp_path):
    # No Finnhub key / failed fetch: never make the calendar worse than it was.
    rows = [{"ticker": "MSFT", "name": "Microsoft", "date": "2026-07-30"}]
    assert er.correct_forward_calendar(rows, str(tmp_path), "2026-07-30") == rows


def test_forward_calendar_corrupt_dump_changes_nothing(tmp_path):
    path = os.path.join(str(tmp_path), "finnhub_earnings.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    rows = [{"ticker": "MSFT", "name": "Microsoft", "date": "2026-07-30"}]
    assert er.correct_forward_calendar(rows, str(tmp_path), "2026-07-30") == rows


def test_forward_calendar_empty_input():
    assert er.correct_forward_calendar([], "unused", "2026-07-30") == []
    assert er.correct_forward_calendar(None, "unused", "2026-07-30") == []


def test_finnhub_http_error_degrades(monkeypatch):
    def boom(url, session, params=None, headers=None):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(er, "_get_json", boom)
    health = {}
    out = er.fetch_finnhub_calendar({"finnhub_key": "k"}, "unused", health, TODAY)
    assert out == []
    assert health["finnhub"]["ok"] is False
    assert "RuntimeError" in health["finnhub"]["note"]


# --------------------------------------------------------------------------- #
# prefilter_candidates (pure)
# --------------------------------------------------------------------------- #
def test_prefilter_size_gate_passes_index_member():
    out = er.prefilter_candidates([_reporter(ticker="AAPL")], {}, set())
    assert len(out) == 1
    assert out[0]["gates"] == ["size"]
    assert out[0]["price_move_pct"] is None


def test_prefilter_surprise_gate():
    out = er.prefilter_candidates(
        [_reporter(ticker="ZZZQ", eps_est=1.0, surprise=15.0)], {}, set())
    assert len(out) == 1
    assert "surprise" in out[0]["gates"]


def test_prefilter_abs_eps_floor_rejects_small_denominator():
    # +40% "surprise" off a $0.03 estimate is noise, not signal.
    out = er.prefilter_candidates(
        [_reporter(ticker="ZZZQ", eps_est=0.03, surprise=40.0)], {}, set())
    assert out == []


def test_prefilter_reaction_gate():
    out = er.prefilter_candidates([_reporter(ticker="ZZZQ")], {"ZZZQ": -8.0}, set())
    assert len(out) == 1
    assert out[0]["gates"] == ["reaction"]
    assert out[0]["price_move_pct"] == -8.0


def test_prefilter_nothing_trips():
    out = er.prefilter_candidates(
        [_reporter(ticker="ZZZQ", eps_est=1.0, surprise=3.0)], {"ZZZQ": 1.0}, set())
    assert out == []


def test_prefilter_covered_dedup_skips():
    covered = {"AAPL:2026-07-10"}
    out = er.prefilter_candidates([_reporter(ticker="AAPL")], {}, covered)
    assert out == []


def test_prefilter_cap_keeps_highest_scores():
    reporters = [
        _reporter(ticker=f"T{i:02d}", eps_est=1.0, surprise=11.0 + i)
        for i in range(er.CANDIDATE_CAP + 5)
    ]
    out = er.prefilter_candidates(reporters, {}, set())
    assert len(out) == er.CANDIDATE_CAP
    kept = {c["ticker"] for c in out}
    # The five lowest-surprise names fell off; the top name sorted first.
    assert {"T00", "T01", "T02", "T03", "T04"}.isdisjoint(kept)
    assert out[0]["ticker"] == f"T{er.CANDIDATE_CAP + 4:02d}"


def test_prefilter_records_all_tripped_gates():
    out = er.prefilter_candidates(
        [_reporter(ticker="NVDA", eps_est=1.0, surprise=20.0)], {"NVDA": 9.0}, set())
    assert out[0]["gates"] == ["size", "surprise", "reaction"]


# --------------------------------------------------------------------------- #
# Ranking scale and the bellwether reserve. Regression: on 2026-07-29 Microsoft
# (+9.5% surprise) and Meta (-16.0%) reported into the busiest night of the
# season and never reached the ledger — CANDIDATE_CAP truncated them behind
# microcaps whose surprise % was a division artifact off a near-zero estimate.
# --------------------------------------------------------------------------- #
def test_blend_score_ignores_surprise_off_sub_floor_estimate():
    # A 5194% "surprise" off a $0.005 estimate is the same noise ABS_EPS_FLOOR
    # keeps out of the surprise gate; it must not decide the ranking either.
    noise = _reporter(ticker="ZZZQ", eps_est=0.0051, surprise=5194.1)
    assert er._blend_score(noise) == 0.0


def test_blend_score_clamps_extreme_surprise():
    cand = _reporter(ticker="ZZZQ", eps_est=1.0, surprise=4000.0)
    assert er._blend_score(cand) == er.SURPRISE_SCORE_CAP / 10.0


def test_blend_score_clamps_extreme_reaction():
    cand = _reporter(ticker="ZZZQ")
    cand["price_move_pct"] = 400.0
    assert er._blend_score(cand) == er.REACTION_SCORE_CAP / 5.0


def test_bellwether_set_covers_the_forward_calendar_watchlist():
    # The brief's forward calendar promises these names report; the
    # post-earnings lane has to deliver the same names once they do.
    from worldintel.core import fetch_sources
    assert {t for t, _ in fetch_sources.earnings_watchlist()} <= er.bellwether_tickers()


def test_prefilter_keeps_bellwether_behind_a_full_cap_of_noise():
    noise = [
        _reporter(ticker=f"N{i:02d}", eps_est=0.0051, surprise=5000.0 + i)
        for i in range(er.CANDIDATE_CAP + 10)
    ]
    moves = {f"N{i:02d}": 40.0 for i in range(er.CANDIDATE_CAP + 10)}
    reporters = noise + [
        _reporter(ticker="MSFT", report_date="2026-07-29", eps_est=4.3274,
                  eps_act=4.74, surprise=9.5),
        _reporter(ticker="META", report_date="2026-07-29", eps_est=7.3594,
                  eps_act=6.18, surprise=-16.0),
    ]
    out = er.prefilter_candidates(reporters, moves, set())
    kept = {c["ticker"] for c in out}
    assert "MSFT" in kept
    assert "META" in kept
    assert len(out) == er.CANDIDATE_CAP


def test_prefilter_bellwethers_lead_the_kept_order():
    # The EDGAR/context budget takes the first RELEASE_FETCH_MAX US names, so a
    # reserved bellwether has to sit at the front, not merely inside the cap.
    noise = [_reporter(ticker=f"N{i:02d}", eps_est=1.0, surprise=90.0)
             for i in range(5)]
    out = er.prefilter_candidates(
        noise + [_reporter(ticker="MSFT", eps_est=4.0, surprise=9.5)], {}, set())
    assert out[0]["ticker"] == "MSFT"


def test_prefilter_bellwether_reserve_is_bounded():
    # The reserve is a floor, not the whole brief: only BELLWETHER_SLOTS
    # bellwethers displace higher-scoring names.
    bells = [_reporter(ticker=t, eps_est=5.0, surprise=1.0) for t in
             ("AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "TSLA", "NFLX", "ORCL")]
    strong = [_reporter(ticker=f"S{i:02d}", eps_est=1.0, surprise=95.0)
              for i in range(er.CANDIDATE_CAP)]
    out = er.prefilter_candidates(bells + strong, {}, set())
    kept_bells = [c["ticker"] for c in out if c["ticker"] in er.bellwether_tickers()]
    assert len(kept_bells) == er.BELLWETHER_SLOTS
    assert len(out) == er.CANDIDATE_CAP


def test_prefilter_reports_truncation_in_health():
    reporters = [_reporter(ticker=f"S{i:02d}", eps_est=1.0, surprise=20.0)
                 for i in range(er.CANDIDATE_CAP + 7)]
    health = {}
    out = er.prefilter_candidates(reporters, {}, set(), health)
    assert len(out) == er.CANDIDATE_CAP
    assert health["pe_prefilter"]["ok"] is True
    assert "7 truncated" in health["pe_prefilter"]["note"]


def test_prefilter_health_note_records_a_clean_pass():
    health = {}
    er.prefilter_candidates([_reporter(ticker="AAPL")], {}, set(), health)
    assert health["pe_prefilter"] == {"ok": True, "note": "1 kept, 0 truncated"}


# --------------------------------------------------------------------------- #
# fetch_price_moves
# --------------------------------------------------------------------------- #
def test_price_moves_passthrough(monkeypatch):
    monkeypatch.setattr(er, "_yf_batch_moves", lambda ts: {t: 1.5 for t in ts})
    health = {}
    out = er.fetch_price_moves(["AAPL", "MSFT", "AAPL"], health)
    assert out == {"AAPL": 1.5, "MSFT": 1.5}
    assert health["price_moves"]["ok"] is True


def test_price_moves_missing_module(monkeypatch):
    def boom(tickers):
        raise ModuleNotFoundError("No module named 'yfinance'")

    monkeypatch.setattr(er, "_yf_batch_moves", boom)
    health = {}
    assert er.fetch_price_moves(["AAPL"], health) == {}
    assert health["price_moves"] == {"ok": False, "note": "yfinance not installed"}


def test_price_moves_generic_failure(monkeypatch):
    def boom(tickers):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(er, "_yf_batch_moves", boom)
    health = {}
    assert er.fetch_price_moves(["AAPL"], health) == {}
    assert health["price_moves"]["ok"] is False


# --------------------------------------------------------------------------- #
# _reaction_move (pure) — timing-aware BMO/AMC price reaction
# --------------------------------------------------------------------------- #
_RD = date(2026, 7, 10)
_REPORT_DATE = "2026-07-10"


def test_reaction_move_bmo_same_day():
    bars = [(_RD - timedelta(days=1), 100.0), (_RD, 110.0)]
    assert er._reaction_move(bars, _REPORT_DATE, "bmo") == 10.0


def test_reaction_move_amc_next_session():
    bars = [(_RD, 100.0), (_RD + timedelta(days=1), 90.0)]
    assert er._reaction_move(bars, _REPORT_DATE, "amc") == -10.0


def test_reaction_move_amc_day_one_no_post_bar_returns_none():
    # AMC report, but the price feed only has bars through report_date itself
    # (the reaction session hasn't happened yet) -> None, not a stale guess.
    bars = [(_RD - timedelta(days=1), 100.0), (_RD, 105.0)]
    assert er._reaction_move(bars, _REPORT_DATE, "amc") is None


def test_reaction_move_unknown_hour_uses_bmo_rule():
    bars = [(_RD - timedelta(days=1), 100.0), (_RD, 110.0)]
    assert er._reaction_move(bars, _REPORT_DATE, "dmh") == 10.0
    assert er._reaction_move(bars, _REPORT_DATE, "") == 10.0


def test_reaction_move_missing_bars_returns_none():
    assert er._reaction_move([], _REPORT_DATE, "bmo") is None
    assert er._reaction_move([], _REPORT_DATE, "amc") is None


def test_reaction_move_bmo_report_day_gap_uses_first_bar_geq():
    # No bar exactly on report_date (e.g. holiday) -> first bar >= report_date.
    bars = [(_RD - timedelta(days=1), 100.0), (_RD + timedelta(days=2), 120.0)]
    assert er._reaction_move(bars, _REPORT_DATE, "bmo") == 20.0


def test_reaction_move_bmo_no_earlier_bar_returns_none():
    bars = [(_RD, 100.0)]
    assert er._reaction_move(bars, _REPORT_DATE, "bmo") is None


def test_reaction_move_amc_no_exact_base_uses_last_bar_leq():
    bars = [(_RD - timedelta(days=1), 100.0), (_RD + timedelta(days=1), 130.0)]
    assert er._reaction_move(bars, _REPORT_DATE, "amc") == 30.0


def test_reaction_move_unparseable_date_returns_none():
    bars = [(_RD, 100.0), (_RD + timedelta(days=1), 90.0)]
    assert er._reaction_move(bars, "not-a-date", "bmo") is None
    assert er._reaction_move(bars, None, "amc") is None


# --------------------------------------------------------------------------- #
# fetch_price_reactions (seam + pure core wrapper)
# --------------------------------------------------------------------------- #
def _rep(ticker, report_date=_REPORT_DATE, hour="bmo"):
    return {"ticker": ticker, "report_date": report_date, "hour": hour}


def test_price_reactions_passthrough(monkeypatch):
    frames = {
        "AAPL": [(_RD - timedelta(days=1), 100.0), (_RD, 110.0)],
        "MSFT": [(_RD, 50.0), (_RD + timedelta(days=1), 55.0)],
    }
    monkeypatch.setattr(er, "_yf_reaction_frame", lambda ts: frames)
    health = {}
    out = er.fetch_price_reactions(
        [_rep("AAPL", hour="bmo"), _rep("MSFT", hour="amc")], health)
    assert out == {"AAPL": 10.0, "MSFT": 10.0}
    assert health["price_moves"]["ok"] is True


def test_price_reactions_missing_module(monkeypatch):
    def boom(tickers):
        raise ModuleNotFoundError("No module named 'yfinance'")

    monkeypatch.setattr(er, "_yf_reaction_frame", boom)
    health = {}
    assert er.fetch_price_reactions([_rep("AAPL")], health) == {}
    assert health["price_moves"] == {"ok": False, "note": "yfinance not installed"}


def test_price_reactions_generic_failure(monkeypatch):
    def boom(tickers):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(er, "_yf_reaction_frame", boom)
    health = {}
    assert er.fetch_price_reactions([_rep("AAPL")], health) == {}
    assert health["price_moves"]["ok"] is False


def test_price_reactions_empty_tickers():
    health = {}
    assert er.fetch_price_reactions([], health) == {}
    assert health["price_moves"] == {"ok": True, "note": "no tickers"}


def test_price_reactions_missing_frame_for_ticker_returns_none(monkeypatch):
    monkeypatch.setattr(er, "_yf_reaction_frame", lambda ts: {})
    health = {}
    out = er.fetch_price_reactions([_rep("AAPL")], health)
    assert out == {"AAPL": None}


# --------------------------------------------------------------------------- #
# fetch_earnings_release (stubbed 3-step EDGAR chain)
# --------------------------------------------------------------------------- #
_TICKER_MAP = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}


def _edgar_stubs(monkeypatch, form="8-K", filed=None, doc_html=None):
    """Wire the 3-step chain: ticker map -> submissions -> primary document."""
    filed = filed or str(date.today())
    doc_html = doc_html if doc_html is not None else (
        "<html><head><style>p{color:red}</style></head><body>"
        "<h1>Q3 Results</h1><p>Revenue &amp; EPS beat.</p>"
        "<script>var a=1;</script></body></html>"
    )
    calls = {}

    def fake_get_json(url, session, params=None, headers=None):
        assert headers and er.wi_config.get().contact_email in headers.get("User-Agent", "")
        if "company_tickers" in url:
            return _TICKER_MAP
        if "submissions/CIK0000320193" in url:
            return {"filings": {"recent": {
                "form": ["4", form],
                "accessionNumber": ["0000320193-26-000001", "0000320193-26-000055"],
                "primaryDocument": ["ownership.xml", "aapl-ex991.htm"],
                "filingDate": [filed, filed],
            }}}
        raise AssertionError("unexpected url: " + url)

    def fake_get_text(url, session, headers=None):
        assert headers and er.wi_config.get().contact_email in headers.get("User-Agent", "")
        calls["doc_url"] = url
        return doc_html

    monkeypatch.setattr(er, "_CIK_CACHE", {})
    monkeypatch.setattr(er, "_get_json", fake_get_json)
    monkeypatch.setattr(er, "_get_text", fake_get_text)
    return calls


def test_release_happy_path_strips_html(monkeypatch):
    calls = _edgar_stubs(monkeypatch)
    health = {}
    out = er.fetch_earnings_release("AAPL", None, health)
    assert "Q3 Results" in out and "Revenue & EPS beat." in out
    assert "<" not in out and "var a" not in out and "color:red" not in out
    assert calls["doc_url"] == (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000055/aapl-ex991.htm"
    )


def test_release_truncates_to_excerpt_chars(monkeypatch):
    _edgar_stubs(monkeypatch, doc_html="<p>" + ("word " * 3000) + "</p>")
    out = er.fetch_earnings_release("AAPL", None, {})
    assert len(out) == er.RELEASE_EXCERPT_CHARS


def test_release_unknown_ticker_empty(monkeypatch):
    _edgar_stubs(monkeypatch)
    assert er.fetch_earnings_release("NOSUCH", None, {}) == ""


def test_release_no_recent_filing_empty(monkeypatch):
    stale = str(date.today() - timedelta(days=30))
    _edgar_stubs(monkeypatch, filed=stale)
    assert er.fetch_earnings_release("AAPL", None, {}) == ""


def test_release_6k_accepted(monkeypatch):
    _edgar_stubs(monkeypatch, form="6-K")
    out = er.fetch_earnings_release("AAPL", None, {})
    assert "Q3 Results" in out


def test_release_never_raises(monkeypatch):
    def boom(url, session, params=None, headers=None):
        raise RuntimeError("SEC down")

    monkeypatch.setattr(er, "_CIK_CACHE", {})
    monkeypatch.setattr(er, "_get_json", boom)
    health = {}
    assert er.fetch_earnings_release("AAPL", None, health) == ""
    assert health["edgar_release"]["ok"] is False


# --------------------------------------------------------------------------- #
# fetch_intl_watchlist
# --------------------------------------------------------------------------- #
def test_intl_window_and_missing_estimates(monkeypatch, tmp_path):
    rows = {
        "7203.T": {"date": "2026-07-10", "eps_est": 1.0, "eps_act": 1.3},   # in window
        "6758.T": {"date": "2026-06-01", "eps_est": 1.0, "eps_act": 1.0},   # too old
        "300750.SZ": {"date": "2026-07-09", "eps_est": None, "eps_act": None},  # A-share
    }
    monkeypatch.setattr(er, "_yf_earnings_row", lambda t: rows.get(t))
    monkeypatch.setattr(er, "_yf_batch_moves", lambda ts: {t: 2.5 for t in ts})
    health = {}
    out = er.fetch_intl_watchlist(str(tmp_path), health, TODAY)

    tickers = {r["ticker"] for r in out}
    assert tickers == {"7203.T", "300750.SZ"}
    toyota = next(r for r in out if r["ticker"] == "7203.T")
    assert toyota["surprise_pct"] == 30.0
    assert toyota["price_move_pct"] == 2.5
    assert toyota["source"] == "intl" and toyota["name"] == "Toyota"
    catl = next(r for r in out if r["ticker"] == "300750.SZ")
    assert catl["surprise_pct"] is None  # reaction gate is still available downstream
    assert health["intl_earnings"]["ok"] is True


def test_intl_missing_module(monkeypatch, tmp_path):
    def boom(ticker):
        raise ModuleNotFoundError("No module named 'yfinance'")

    monkeypatch.setattr(er, "_yf_earnings_row", boom)
    health = {}
    assert er.fetch_intl_watchlist(str(tmp_path), health, TODAY) == []
    assert health["intl_earnings"] == {"ok": False, "note": "yfinance not installed"}


# --------------------------------------------------------------------------- #
# _finnhub_profile (generalized profile2: name + market cap + industry)
# --------------------------------------------------------------------------- #
def test_finnhub_profile_full_dict_from_one_call(monkeypatch):
    calls = []

    def fake_get_json(url, session, params=None, headers=None):
        calls.append((url, params))
        return {"name": "Apple Inc.", "marketCapitalization": 2950000.5,
                "finnhubIndustry": "Technology"}

    monkeypatch.setattr(er, "_get_json", fake_get_json)
    out = er._finnhub_profile("AAPL", {"finnhub_key": "k"}, None)
    # name + market cap + industry all come from the SAME profile2 request.
    assert out == {"name": "Apple Inc.", "market_cap_m": 2950000.5,
                   "industry": "Technology"}
    assert calls == [(er.FINNHUB_PROFILE_URL, {"symbol": "AAPL", "token": "k"})]


def test_finnhub_profile_no_key_or_failure_defaults(monkeypatch):
    def boom(url, session, params=None, headers=None):
        raise RuntimeError("HTTP 429")

    monkeypatch.setattr(er, "_get_json", boom)
    assert er._finnhub_profile("AAPL", {"finnhub_key": "k"}, None) == EMPTY_PROFILE
    assert er._finnhub_profile("AAPL", {}, None) == EMPTY_PROFILE


# --------------------------------------------------------------------------- #
# _yf_company_info (sector + truncated description)
# --------------------------------------------------------------------------- #
def test_company_info_truncates_description_on_word_boundary(monkeypatch):
    summary = ("alpha " * 80).strip()  # 479 chars of whole words

    class FakeTicker:
        def __init__(self, ticker):
            pass

        def get_info(self):
            return {"sector": "Technology", "longBusinessSummary": summary}

    _fake_yf(monkeypatch, FakeTicker)
    out = er._yf_company_info("AAPL")
    assert out["sector"] == "Technology"
    desc = out["description"]
    assert 0 < len(desc) <= 350
    # Word boundary: the cut is a prefix of the source ending on a full word.
    assert summary.startswith(desc)
    assert summary[len(desc)] == " "


def test_company_info_short_description_untouched(monkeypatch):
    class FakeTicker:
        def __init__(self, ticker):
            pass

        def get_info(self):
            return {"sector": "Energy", "longBusinessSummary": "Drills wells."}

    _fake_yf(monkeypatch, FakeTicker)
    assert er._yf_company_info("XOM") == {
        "sector": "Energy", "description": "Drills wells."}


def test_company_info_missing_fields_empty(monkeypatch):
    class FakeTicker:
        def __init__(self, ticker):
            pass

        def get_info(self):
            return {}

    _fake_yf(monkeypatch, FakeTicker)
    assert er._yf_company_info("AAPL") == {"sector": "", "description": ""}


# --------------------------------------------------------------------------- #
# _finnhub_peers
# --------------------------------------------------------------------------- #
def test_peers_excludes_self_and_caps_at_five(monkeypatch):
    calls = []

    def fake_get_json(url, session, params=None, headers=None):
        calls.append((url, params))
        return ["AAPL", "MSFT", "GOOG", "META", "AMZN", "NVDA", "TSLA"]

    monkeypatch.setattr(er, "_get_json", fake_get_json)
    out = er._finnhub_peers("AAPL", {"finnhub_key": "k"}, None)
    assert out == ["MSFT", "GOOG", "META", "AMZN", "NVDA"]  # self out, cap 5
    assert calls == [(er.FINNHUB_PEERS_URL, {"symbol": "AAPL", "token": "k"})]


def test_peers_deduped_preserving_order(monkeypatch):
    # Finnhub can return duplicate tickers (seen live: BYRN -> VWAV twice);
    # keep first occurrence, order preserved, cap applied after dedupe.
    def fake_get_json(url, session, params=None, headers=None):
        return ["VWAV", "VWAV", "SIF", "MNTS", "SIF", "OPXS", "PURE", "ODC"]

    monkeypatch.setattr(er, "_get_json", fake_get_json)
    out = er._finnhub_peers("BYRN", {"finnhub_key": "k"}, None)
    assert out == ["VWAV", "SIF", "MNTS", "OPXS", "PURE"]


def test_peers_failure_or_no_key_empty(monkeypatch):
    def boom(url, session, params=None, headers=None):
        raise RuntimeError("HTTP 500")

    monkeypatch.setattr(er, "_get_json", boom)
    assert er._finnhub_peers("AAPL", {"finnhub_key": "k"}, None) == []
    assert er._finnhub_peers("AAPL", {}, None) == []  # no key -> no call


# --------------------------------------------------------------------------- #
# _yf_beat_history
# --------------------------------------------------------------------------- #
def test_beat_streak_recent_first_skips_unreported_caps_four(monkeypatch):
    import pandas as pd

    nan = float("nan")
    df = pd.DataFrame(
        {
            "EPS Estimate": [2.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
            "Reported EPS": [nan, 1.1, 1.4, 0.9, 1.2, 1.05, 1.0],
            "Surprise(%)": [nan, 10.04, 43.0, nan, 20.0, 5.0, 0.4],
        },
        index=pd.to_datetime([
            "2026-10-20",  # scheduled, not yet reported -> skipped
            "2026-01-20", "2026-07-10", "2026-04-15",  # deliberately unordered
            "2025-10-15", "2025-07-15", "2025-04-15",
        ]),
    )

    class FakeTicker:
        def __init__(self, ticker):
            pass

        def get_earnings_dates(self, limit=8):
            assert limit == 8
            return df

    _fake_yf(monkeypatch, FakeTicker)
    # 2026-04-15 reported but has no surprise -> skipped; most recent first,
    # rounded to 1dp, capped at 4 (0.4 from 2025-04-15 falls off).
    assert er._yf_beat_history("AAPL") == [43.0, 10.0, 20.0, 5.0]


def test_beat_streak_empty_frame(monkeypatch):
    class FakeTicker:
        def __init__(self, ticker):
            pass

        def get_earnings_dates(self, limit=8):
            return None

    _fake_yf(monkeypatch, FakeTicker)
    assert er._yf_beat_history("AAPL") == []


# --------------------------------------------------------------------------- #
# _yf_fwd_estimates (forward consensus + 30-day revision)
# --------------------------------------------------------------------------- #
def _est_ticker(est, trend, rev=None):
    class FakeTicker:
        earnings_estimate = est
        eps_trend = trend
        revenue_estimate = rev

        def __init__(self, ticker):
            pass

    return FakeTicker


def test_fwd_estimates_happy_path(monkeypatch):
    import pandas as pd

    est = pd.DataFrame(
        {"avg": [2.15, 2.4, 9.1, 10.2],
         "numberOfAnalysts": [12.0, 11.0, 30.0, 28.0]},
        index=["0q", "+1q", "0y", "+1y"])
    trend = pd.DataFrame(
        {"current": [2.15, 2.4, 9.1, 10.2],
         "30daysAgo": [2.0, 2.4, 9.0, 10.0]},
        index=["0q", "+1q", "0y", "+1y"])
    _fake_yf(monkeypatch, _est_ticker(est, trend))
    out = er._yf_fwd_estimates("AAPL")
    assert out["fwd_eps_q"] == 2.15
    assert out["fwd_eps_fy"] == 9.1
    assert out["analyst_count"] == 12 and isinstance(out["analyst_count"], int)
    assert out["revision_30d_pct"] == 7.5  # (2.15 - 2.0) / 2.0 * 100, 1dp


def test_fwd_estimates_zero_denominator_revision_none(monkeypatch):
    import pandas as pd

    est = pd.DataFrame(
        {"avg": [1.0, 4.0], "numberOfAnalysts": [5.0, 5.0]}, index=["0q", "0y"])
    trend = pd.DataFrame(
        {"current": [1.0], "30daysAgo": [0.0]}, index=["0q"])
    _fake_yf(monkeypatch, _est_ticker(est, trend))
    out = er._yf_fwd_estimates("AAPL")
    assert out["revision_30d_pct"] is None
    assert out["fwd_eps_q"] == 1.0 and out["fwd_eps_fy"] == 4.0


def test_fwd_estimates_missing_row_revision_none(monkeypatch):
    import pandas as pd

    trend = pd.DataFrame({"current": [9.1], "30daysAgo": [9.0]}, index=["0y"])
    _fake_yf(monkeypatch, _est_ticker(None, trend))
    out = er._yf_fwd_estimates("AAPL")
    assert out == {"fwd_eps_q": None, "fwd_eps_fy": None,
                   "analyst_count": None, "revision_30d_pct": None,
                   "rev_growth_fy": None}


def test_fwd_estimates_missing_frames_all_none(monkeypatch):
    _fake_yf(monkeypatch, _est_ticker(None, None))
    assert er._yf_fwd_estimates("AAPL") == {
        "fwd_eps_q": None, "fwd_eps_fy": None,
        "analyst_count": None, "revision_30d_pct": None, "rev_growth_fy": None}


def test_fwd_estimates_includes_rev_growth_fy(monkeypatch):
    import pandas as pd

    est = pd.DataFrame({"avg": [2.15], "numberOfAnalysts": [12.0]}, index=["0q"])
    trend = pd.DataFrame({"current": [2.15], "30daysAgo": [2.0]}, index=["0q"])
    rev = pd.DataFrame({"growth": [0.0786]}, index=["0y"])
    _fake_yf(monkeypatch, _est_ticker(est, trend, rev))
    out = er._yf_fwd_estimates("AAPL")
    assert out["rev_growth_fy"] == 0.0786


def test_fwd_estimates_missing_revenue_frame_rev_growth_none(monkeypatch):
    import pandas as pd

    est = pd.DataFrame({"avg": [2.15], "numberOfAnalysts": [12.0]}, index=["0q"])
    _fake_yf(monkeypatch, _est_ticker(est, None, None))
    out = er._yf_fwd_estimates("AAPL")
    assert out["rev_growth_fy"] is None


def test_fwd_estimates_empty_revenue_frame_rev_growth_none(monkeypatch):
    import pandas as pd

    est = pd.DataFrame({"avg": [2.15], "numberOfAnalysts": [12.0]}, index=["0q"])
    _fake_yf(monkeypatch, _est_ticker(est, None, pd.DataFrame()))
    out = er._yf_fwd_estimates("AAPL")
    assert out["rev_growth_fy"] is None


# --------------------------------------------------------------------------- #
# detect_capital_returns (pure)
# --------------------------------------------------------------------------- #
def test_detect_capital_returns_buyback_forward_clause_order():
    text = ("In connection with today's results, the Board authorized a "
            "share repurchase program of up to $500 million.")
    out = er.detect_capital_returns(text)
    assert out == {"buyback": "$500M", "dividend": None}


def test_detect_capital_returns_buyback_reverse_clause_order():
    text = ("The Board authorized a new $500 million share repurchase "
            "program to return capital to shareholders.")
    out = er.detect_capital_returns(text)
    assert out["buyback"] == "$500M"


def test_detect_capital_returns_buyback_billion_amount():
    text = "The Board approved a $2.5 billion increase to the repurchase authorization."
    out = er.detect_capital_returns(text)
    assert out["buyback"] == "$2.5B"


def test_detect_capital_returns_dividend_increase():
    text = ("The company also declared a quarterly dividend of $0.31 per "
            "share, an 8% increase over the prior quarter.")
    out = er.detect_capital_returns(text)
    assert out == {"buyback": None, "dividend": "$0.31/sh"}


def test_detect_capital_returns_both_present():
    text = ("The Board authorized a share repurchase program of up to $500 "
            "million. Separately, the company declared a quarterly dividend "
            "of $0.31 per share.")
    out = er.detect_capital_returns(text)
    assert out == {"buyback": "$500M", "dividend": "$0.31/sh"}


def test_detect_capital_returns_dividend_policy_discussion_no_match():
    text = "Management reiterated that our dividend policy remains unchanged."
    assert er.detect_capital_returns(text) == {"buyback": None, "dividend": None}


def test_detect_capital_returns_no_dividend_declared_no_match():
    text = "The board reviewed capital allocation; no dividend was declared this quarter."
    assert er.detect_capital_returns(text) == {"buyback": None, "dividend": None}


def test_detect_capital_returns_buyback_without_authorization_no_match():
    text = "The company repurchased shares in the open market during the quarter."
    assert er.detect_capital_returns(text) == {"buyback": None, "dividend": None}


def test_detect_capital_returns_empty_and_none_text():
    assert er.detect_capital_returns("") == {"buyback": None, "dividend": None}
    assert er.detect_capital_returns(None) == {"buyback": None, "dividend": None}


# --------------------------------------------------------------------------- #
# count_analyst_moves (pure)
# --------------------------------------------------------------------------- #
def test_count_analyst_moves_none_rows_is_none():
    assert er.count_analyst_moves(None, "2026-07-10") is None


def test_count_analyst_moves_filters_by_report_date_and_maps_actions():
    rows = [
        (date(2026, 7, 9), "upgrade"),    # before report_date -> excluded
        (date(2026, 7, 10), "upgrade"),   # on report_date -> included
        (date(2026, 7, 11), "downgrade"),
        (date(2026, 7, 12), "initiated"),
    ]
    out = er.count_analyst_moves(rows, "2026-07-10")
    assert out == {"up": 1, "down": 1, "init": 1}


def test_count_analyst_moves_unrecognized_action_ignored():
    rows = [(date(2026, 7, 10), "maintains")]
    assert er.count_analyst_moves(rows, "2026-07-10") == {"up": 0, "down": 0, "init": 0}


def test_count_analyst_moves_empty_rows_returns_zero_dict():
    assert er.count_analyst_moves([], "2026-07-10") == {"up": 0, "down": 0, "init": 0}


# --------------------------------------------------------------------------- #
# _yf_upgrades (seam)
# --------------------------------------------------------------------------- #
def _upgrades_ticker(frame):
    class FakeTicker:
        upgrades_downgrades = frame

        def __init__(self, ticker):
            pass

    return FakeTicker


def test_yf_upgrades_happy_path(monkeypatch):
    import pandas as pd

    frame = pd.DataFrame(
        {"Action": ["up", "down"]},
        index=pd.to_datetime(["2026-07-10", "2026-07-11"]))
    _fake_yf(monkeypatch, _upgrades_ticker(frame))
    out = er._yf_upgrades("AAPL")
    assert out == [(date(2026, 7, 10), "up"), (date(2026, 7, 11), "down")]


def test_yf_upgrades_none_frame_is_none(monkeypatch):
    _fake_yf(monkeypatch, _upgrades_ticker(None))
    assert er._yf_upgrades("AAPL") is None


def test_yf_upgrades_empty_frame_is_none(monkeypatch):
    import pandas as pd

    _fake_yf(monkeypatch, _upgrades_ticker(pd.DataFrame()))
    assert er._yf_upgrades("AAPL") is None


# --------------------------------------------------------------------------- #
# pipeline ledger persistence
# --------------------------------------------------------------------------- #
def test_read_pipeline_missing_file(tmp_path):
    assert er.read_pipeline(str(tmp_path)) == {}


def test_read_pipeline_corrupt_file(tmp_path):
    (tmp_path / er.PIPELINE_FILE).write_text("{not json", encoding="utf-8")
    assert er.read_pipeline(str(tmp_path)) == {}


def test_read_pipeline_migrates_legacy_covered_set(tmp_path):
    (tmp_path / "earnings_covered.json").write_text(
        json.dumps({"AAPL:2026-07-10": "2026-07-11",
                    "7203.T:2026-07-09": "2026-07-11"}),
        encoding="utf-8")
    pipeline = er.read_pipeline(str(tmp_path))
    assert pipeline == {
        "AAPL:2026-07-10": {"status": "covered", "first_seen": "2026-07-11",
                             "last_seen": "2026-07-11", "attempts": 0, "missing": []},
        "7203.T:2026-07-09": {"status": "covered", "first_seen": "2026-07-11",
                               "last_seen": "2026-07-11", "attempts": 0, "missing": []},
    }
    # Legacy file left in place, untouched; migration doesn't write a real ledger.
    assert (tmp_path / "earnings_covered.json").exists()
    assert not (tmp_path / er.PIPELINE_FILE).exists()


def test_read_pipeline_prefers_real_ledger_over_legacy(tmp_path):
    (tmp_path / "earnings_covered.json").write_text(
        json.dumps({"OLD:2026-07-01": "2026-07-01"}), encoding="utf-8")
    (tmp_path / er.PIPELINE_FILE).write_text(
        json.dumps({"NEW:2026-07-10": {
            "status": "pending", "first_seen": "2026-07-10", "last_seen": "2026-07-10",
            "attempts": 1, "missing": ["price_move_pct"]}}),
        encoding="utf-8")
    pipeline = er.read_pipeline(str(tmp_path))
    assert set(pipeline) == {"NEW:2026-07-10"}


def test_blocked_keys_covered_and_expired_block_pending_does_not():
    pipeline = {
        "A:2026-07-10": {"status": "covered"},
        "B:2026-07-10": {"status": "expired"},
        "C:2026-07-10": {"status": "pending"},
    }
    assert er.blocked_keys(pipeline) == {"A:2026-07-10", "B:2026-07-10"}


def test_note_pending_creates_entry():
    pipeline = {}
    er.note_pending(pipeline, "WRLD:2026-07-24", ["price_move_pct"], "2026-07-25")
    assert pipeline == {
        "WRLD:2026-07-24": {"status": "pending", "first_seen": "2026-07-25",
                             "last_seen": "2026-07-25", "attempts": 1,
                             "missing": ["price_move_pct"]},
    }


def test_note_pending_updates_existing_increments_attempts_keeps_first_seen():
    pipeline = {"WRLD:2026-07-24": {
        "status": "pending", "first_seen": "2026-07-24", "last_seen": "2026-07-24",
        "attempts": 1, "missing": ["price_move_pct"]}}
    er.note_pending(pipeline, "WRLD:2026-07-24", ["revenue"], "2026-07-25")
    assert pipeline["WRLD:2026-07-24"] == {
        "status": "pending", "first_seen": "2026-07-24", "last_seen": "2026-07-25",
        "attempts": 2, "missing": ["revenue"],
    }


def test_write_pipeline_round_trips(tmp_path):
    pipeline = {"A:2026-07-10": {"status": "covered", "first_seen": "2026-07-10",
                                  "last_seen": "2026-07-10", "attempts": 0, "missing": []}}
    er.write_pipeline(str(tmp_path), pipeline)
    assert er.read_pipeline(str(tmp_path)) == pipeline


def test_mark_covered_writes_ledger_entries(tmp_path):
    cands = [{"ticker": "AAPL", "report_date": "2026-07-10"},
             {"ticker": "7203.T", "report_date": "2026-07-09"}]
    er.mark_covered(str(tmp_path), cands, TODAY)
    pipeline = er.read_pipeline(str(tmp_path))
    assert pipeline["AAPL:2026-07-10"]["status"] == "covered"
    assert pipeline["7203.T:2026-07-09"]["status"] == "covered"
    assert er.blocked_keys(pipeline) == {"AAPL:2026-07-10", "7203.T:2026-07-09"}


def test_mark_covered_blocks_resurfacing_in_prefilter(tmp_path):
    er.mark_covered(str(tmp_path), [{"ticker": "AAPL", "report_date": "2026-07-10"}], TODAY)
    pipeline = er.read_pipeline(str(tmp_path))
    out = er.prefilter_candidates(
        [_reporter(ticker="AAPL")], {}, er.blocked_keys(pipeline))
    assert out == []


def test_mark_covered_prunes_stale_covered_but_keeps_pending(tmp_path):
    old = str(date(2026, 7, 11) - timedelta(days=er.COVERED_RETENTION_DAYS + 1))
    fresh = "2026-07-05"
    er.write_pipeline(str(tmp_path), {
        "OLD:2026-06-01": {"status": "covered", "first_seen": old, "last_seen": old,
                           "attempts": 0, "missing": []},
        "FRESH:2026-07-04": {"status": "covered", "first_seen": fresh, "last_seen": fresh,
                             "attempts": 0, "missing": []},
        "STALE_PENDING:2026-06-01": {"status": "pending", "first_seen": old,
                                      "last_seen": old, "attempts": 2,
                                      "missing": ["price_move_pct"]},
    })
    er.mark_covered(str(tmp_path), [{"ticker": "NEW", "report_date": "2026-07-10"}], TODAY)
    pipeline = er.read_pipeline(str(tmp_path))
    assert set(pipeline) == {
        "FRESH:2026-07-04", "NEW:2026-07-10", "STALE_PENDING:2026-06-01"}
    assert pipeline["STALE_PENDING:2026-06-01"]["status"] == "pending"


# --------------------------------------------------------------------------- #
# Prune exemptions (filing-reconciliation design Sec.3.2/Sec.8). This ledger
# answers one question -- has this TICKER:report_date been covered? -- and
# keeps exactly one exemption from the recency prune: pending. Whether a
# company's periodic filing was ever read is a separate question with a
# separate 30-day lifetime, and it lives in post_earnings_pool.
# --------------------------------------------------------------------------- #
def test_an_entry_awaiting_its_filing_no_longer_survives_the_recency_prune(tmp_path):
    """The exemption that let this file grow without bound: a covered entry
    with an open filing watch was exempt from the prune, so an entry whose
    filing never landed sat here forever. Ledgers written before the rewrite
    still carry those `filing_watch` keys, and such an entry must now age out
    with every other covered one. `pending` keeps its exemption -- only expiry
    retires a retry."""
    old = str(date(2026, 7, 11) - timedelta(days=er.COVERED_RETENTION_DAYS + 6))
    er.write_pipeline(str(tmp_path), {
        "WATCHED:2026-06-01": {"status": "covered", "first_seen": old,
                               "last_seen": old, "attempts": 0, "missing": [],
                               "filing_watch": {"state": "open", "opened": old,
                                                "snapshot": {}}},
        "COVERED:2026-06-01": {"status": "covered", "first_seen": old,
                               "last_seen": old, "attempts": 0, "missing": []},
        "PENDING:2026-06-01": {"status": "pending", "first_seen": old,
                               "last_seen": old, "attempts": 2,
                               "missing": ["price_move_pct"]},
    })
    er.mark_covered(str(tmp_path), [], TODAY)
    assert set(er.read_pipeline(str(tmp_path))) == {"PENDING:2026-06-01"}


# --------------------------------------------------------------------------- #
# The beat-1 snapshot post_earnings_pool carries forward (_SNAPSHOT_KEYS)
# --------------------------------------------------------------------------- #
def test_snapshot_keys_carries_guidance_struct():
    """Beat 2 restates the guide without re-fetching the 8-K (spec Sec.6.2)."""
    assert "guidance_struct" in er._SNAPSHOT_KEYS


# --------------------------------------------------------------------------- #
# build_post_earnings (orchestration, all helpers stubbed)
# --------------------------------------------------------------------------- #
def test_build_end_to_end(monkeypatch, tmp_path):
    calendar = {"earningsCalendar": [
        # Size + surprise gates.
        {"symbol": "AAPL", "date": "2026-07-10", "hour": "amc",
         "epsActual": 2.4, "epsEstimate": 2.0,
         "revenueActual": 90.0, "revenueEstimate": 85.0},
        # No gate trips (small surprise, small move) -> filtered out.
        {"symbol": "ZZZQ", "date": "2026-07-10", "hour": "bmo",
         "epsActual": 1.02, "epsEstimate": 1.0},
    ]}

    def fake_get_json(url, session, params=None, headers=None):
        if url == er.FINNHUB_URL:
            return calendar
        raise AssertionError("unexpected url: " + url)

    monkeypatch.setattr(er, "_get_json", fake_get_json)
    monkeypatch.setattr(
        er, "_yf_earnings_row",
        lambda t: ({"date": "2026-07-10", "eps_est": None, "eps_act": 1.1}
                   if t == "0700.HK" else None))
    monkeypatch.setattr(
        er, "fetch_price_reactions",
        lambda reporters, health: {r["ticker"]: 1.0 for r in reporters})
    monkeypatch.setattr(er, "_yf_batch_moves", lambda ts: {t: 9.0 for t in ts})
    monkeypatch.setattr(er, "fetch_earnings_release", lambda t, s, h: "RELEASE " + t)
    monkeypatch.setattr(
        er, "_finnhub_profile",
        lambda t, c, s: ({"name": "Apple Inc.", "market_cap_m": 2950000.5,
                          "industry": "Consumer Electronics"}
                         if t == "AAPL" else dict(EMPTY_PROFILE)))
    monkeypatch.setattr(er, "_yf_quarterly_revenue", lambda t: None)
    _stub_context(monkeypatch)

    health = {}
    out = er.build_post_earnings(
        {"finnhub_key": "k"}, str(tmp_path / "raw"), health, TODAY, str(tmp_path))

    assert {c["ticker"] for c in out} == {"AAPL", "0700.HK"}
    for c in out:
        assert set(c.keys()) == FINAL_KEYS  # every candidate carries every key
    aapl = next(c for c in out if c["ticker"] == "AAPL")
    assert aapl["release_excerpt"] == "RELEASE AAPL"  # us + top of the score ranking
    assert aapl["name"] == "Apple Inc."
    assert "size" in aapl["gates"] and "surprise" in aapl["gates"]
    # Enriched (same set as the release fetch): context filled from the stubs;
    # market cap + industry rode on the SAME profile2 call that named it.
    assert aapl["market_cap_m"] == 2950000.5
    assert aapl["industry"] == "Consumer Electronics"
    assert aapl["sector"] == "Technology"
    assert aapl["description"] == "Makes things."
    assert aapl["peers"] == ["MSFT", "GOOG"]
    assert aapl["beat_streak"] == [43.0, 6.0, -11.0, 91.0]
    assert aapl["fwd_eps_q"] == 2.15 and aapl["fwd_eps_fy"] == 9.1
    assert aapl["analyst_count"] == 12 and aapl["revision_30d_pct"] == 7.5
    # rev_act (90.0) is within band of rev_est (85.0) and no yf/release
    # candidate to override it with -> stays finnhub, not overridden.
    assert aapl["rev_act"] == 90.0 and aapl["rev_act_source"] == "finnhub"
    tencent = next(c for c in out if c["ticker"] == "0700.HK")
    assert tencent["release_excerpt"] == ""  # intl: EDGAR does not apply
    assert tencent["name"] == "Tencent"
    assert tencent["gates"] == ["reaction"]
    # Not in the enrichment set -> every context key at its default.
    assert {k: tencent[k] for k in CONTEXT_DEFAULTS} == CONTEXT_DEFAULTS
    assert tencent["rev_act_source"] == "finnhub"  # default; intl rev fields None
    assert health["post_earnings"]["ok"] is True
    assert health["pe_context"] == {"ok": True, "note": "1/1 enriched"}
    assert health["rev_check"] == {"ok": True, "note": "0 overridden, 0 dropped"}
    # Both candidates complete on the first pass -> nothing deferred.
    assert health["pe_pipeline"] == {"ok": True, "note": "2 published, 0 pending, 0 expired"}


def test_build_release_cap_and_us_only(monkeypatch, tmp_path):
    # More passing US names than RELEASE_FETCH_MAX: only the top-N get an excerpt.
    n = er.RELEASE_FETCH_MAX + 3
    calendar = {"earningsCalendar": [
        {"symbol": f"T{i:02d}", "date": "2026-07-10", "hour": "amc",
         "epsActual": 1.0 + (0.11 + 0.01 * i), "epsEstimate": 1.0}
        for i in range(n)
    ]}

    def fake_get_json(url, session, params=None, headers=None):
        if url == er.FINNHUB_URL:
            return calendar
        raise AssertionError("unexpected url: " + url)

    monkeypatch.setattr(er, "_get_json", fake_get_json)
    monkeypatch.setattr(er, "_yf_earnings_row", lambda t: None)
    # A real move (not {}) for every reporter — completeness now requires
    # price_move_pct; the uniform value doesn't perturb the surprise-driven
    # ranking under test.
    monkeypatch.setattr(
        er, "fetch_price_reactions",
        lambda reporters, health: {r["ticker"]: 9.0 for r in reporters})
    fetched = []

    def fake_release(t, s, h):
        fetched.append(t)
        return "R"

    monkeypatch.setattr(er, "fetch_earnings_release", fake_release)
    monkeypatch.setattr(er, "_finnhub_profile", lambda t, c, s: dict(EMPTY_PROFILE))
    monkeypatch.setattr(er, "_yf_quarterly_revenue", lambda t: None)
    _stub_context(monkeypatch)

    health = {}
    out = er.build_post_earnings(
        {"finnhub_key": "k"}, str(tmp_path / "raw"), health, TODAY, str(tmp_path))
    assert len(out) == n
    assert len(fetched) == er.RELEASE_FETCH_MAX
    # Highest blended scores (largest surprises) got the fetches.
    expected = {f"T{i:02d}" for i in range(n - 1, n - 1 - er.RELEASE_FETCH_MAX, -1)}
    assert set(fetched) == expected
    # Names fell back to the ticker when the profile lookup came back empty.
    assert all(c["name"] == c["ticker"] for c in out)
    # Context enrichment covers exactly the same top-N set as the release fetch;
    # everyone below the cutoff keeps every default.
    enriched = [c for c in out if c["ticker"] in expected]
    rest = [c for c in out if c["ticker"] not in expected]
    assert all(c["peers"] == ["MSFT", "GOOG"] for c in enriched)
    assert all(c["beat_streak"] == [43.0, 6.0, -11.0, 91.0] for c in enriched)
    assert all({k: c[k] for k in CONTEXT_DEFAULTS} == CONTEXT_DEFAULTS for c in rest)
    # Default lists must be fresh objects per candidate (no shared mutables).
    assert rest[0]["peers"] is not rest[1]["peers"]
    assert health["pe_context"]["note"] == (
        f"{er.RELEASE_FETCH_MAX}/{er.RELEASE_FETCH_MAX} enriched")
    assert health["pe_pipeline"] == {"ok": True, "note": f"{n} published, 0 pending, 0 expired"}


def test_build_context_helper_failure_isolated(monkeypatch, tmp_path):
    # Two US candidates, both inside the top-N; every context route raises for
    # AAAA -> it keeps all defaults, BBBB still enriched, nothing propagates.
    calendar = {"earningsCalendar": [
        {"symbol": "AAAA", "date": "2026-07-10", "hour": "amc",
         "epsActual": 1.5, "epsEstimate": 1.0},
        {"symbol": "BBBB", "date": "2026-07-10", "hour": "bmo",
         "epsActual": 1.3, "epsEstimate": 1.0},
    ]}

    def fake_get_json(url, session, params=None, headers=None):
        if url == er.FINNHUB_URL:
            return calendar
        raise AssertionError("unexpected url: " + url)

    monkeypatch.setattr(er, "_get_json", fake_get_json)
    monkeypatch.setattr(er, "_yf_earnings_row", lambda t: None)
    # Real price move + release text -> both candidates are completeness-eligible;
    # this test is about context-helper failure isolation, not deferral.
    monkeypatch.setattr(
        er, "fetch_price_reactions",
        lambda reporters, health: {r["ticker"]: 9.0 for r in reporters})
    monkeypatch.setattr(er, "fetch_earnings_release", lambda t, s, h: "RELEASE " + t)
    monkeypatch.setattr(er, "_finnhub_profile", lambda t, c, s: dict(EMPTY_PROFILE))
    monkeypatch.setattr(er, "_yf_quarterly_revenue", lambda t: None)
    _stub_context(monkeypatch, fail_tickers={"AAAA"})

    health = {}
    out = er.build_post_earnings(
        {"finnhub_key": "k"}, str(tmp_path / "raw"), health, TODAY, str(tmp_path))
    assert {c["ticker"] for c in out} == {"AAAA", "BBBB"}
    broken = next(c for c in out if c["ticker"] == "AAAA")
    assert {k: broken[k] for k in CONTEXT_DEFAULTS} == CONTEXT_DEFAULTS
    good = next(c for c in out if c["ticker"] == "BBBB")
    assert good["sector"] == "Technology" and good["peers"] == ["MSFT", "GOOG"]
    assert health["post_earnings"]["ok"] is True
    assert health["pe_context"] == {"ok": True, "note": "1/2 enriched"}


def test_build_analyst_moves_day_one_none_deferred_populated(monkeypatch, tmp_path):
    # DAY1 publishes on its own report date (today == report_date) -> no
    # analyst-moves lookup. DEFR reported the day before -> gets one.
    calendar = {"earningsCalendar": [
        {"symbol": "DAY1", "date": TODAY, "hour": "bmo",
         "epsActual": 1.5, "epsEstimate": 1.0},
        {"symbol": "DEFR", "date": "2026-07-10", "hour": "amc",
         "epsActual": 1.5, "epsEstimate": 1.0},
    ]}

    def fake_get_json(url, session, params=None, headers=None):
        if url == er.FINNHUB_URL:
            return calendar
        raise AssertionError("unexpected url: " + url)

    monkeypatch.setattr(er, "_get_json", fake_get_json)
    monkeypatch.setattr(er, "_yf_earnings_row", lambda t: None)
    monkeypatch.setattr(
        er, "fetch_price_reactions",
        lambda reporters, health: {r["ticker"]: 9.0 for r in reporters})
    monkeypatch.setattr(er, "fetch_earnings_release", lambda t, s, h: "R")
    monkeypatch.setattr(er, "_finnhub_profile", lambda t, c, s: dict(EMPTY_PROFILE))
    monkeypatch.setattr(er, "_yf_quarterly_revenue", lambda t: None)
    _stub_context(monkeypatch)

    calls = []

    def fake_upgrades(t):
        calls.append(t)
        return [(date(2026, 7, 10), "upgrade"), (date(2026, 7, 11), "downgrade")]

    monkeypatch.setattr(er, "_yf_upgrades", fake_upgrades)

    health = {}
    out = er.build_post_earnings(
        {"finnhub_key": "k"}, str(tmp_path / "raw"), health, TODAY, str(tmp_path))

    day1 = next(c for c in out if c["ticker"] == "DAY1")
    defr = next(c for c in out if c["ticker"] == "DEFR")
    assert calls == ["DEFR"]  # the seam is only called for the deferred name
    assert day1["analyst_moves"] is None
    assert defr["analyst_moves"] == {"up": 1, "down": 1, "init": 0}


def test_build_capital_returns_from_release_excerpt(monkeypatch, tmp_path):
    calendar = {"earningsCalendar": [
        {"symbol": "CAPR", "date": TODAY, "hour": "bmo",
         "epsActual": 1.5, "epsEstimate": 1.0},
    ]}

    def fake_get_json(url, session, params=None, headers=None):
        if url == er.FINNHUB_URL:
            return calendar
        raise AssertionError("unexpected url: " + url)

    monkeypatch.setattr(er, "_get_json", fake_get_json)
    monkeypatch.setattr(er, "_yf_earnings_row", lambda t: None)
    monkeypatch.setattr(
        er, "fetch_price_reactions",
        lambda reporters, health: {r["ticker"]: 9.0 for r in reporters})
    monkeypatch.setattr(
        er, "fetch_earnings_release",
        lambda t, s, h: ("The Board authorized a share repurchase program of "
                         "up to $500 million."))
    monkeypatch.setattr(er, "_finnhub_profile", lambda t, c, s: dict(EMPTY_PROFILE))
    monkeypatch.setattr(er, "_yf_quarterly_revenue", lambda t: None)
    _stub_context(monkeypatch)

    health = {}
    out = er.build_post_earnings(
        {"finnhub_key": "k"}, str(tmp_path / "raw"), health, TODAY, str(tmp_path))
    capr = next(c for c in out if c["ticker"] == "CAPR")
    assert capr["buyback"] == "$500M"
    assert capr["dividend"] is None


def test_build_never_raises(monkeypatch, tmp_path):
    def boom(config, raw_dir, health, today):
        raise RuntimeError("catastrophic")

    monkeypatch.setattr(er, "fetch_finnhub_calendar", boom)
    health = {}
    out = er.build_post_earnings({}, str(tmp_path / "raw"), health, TODAY, str(tmp_path))
    assert out == []
    assert health["post_earnings"]["ok"] is False
    assert "RuntimeError" in health["post_earnings"]["note"]


# --------------------------------------------------------------------------- #
# _yf_quarterly_revenue (seam)
# --------------------------------------------------------------------------- #
def _rev_ticker(frame):
    class FakeTicker:
        quarterly_income_stmt = frame

        def __init__(self, ticker):
            pass

    return FakeTicker


def test_yf_quarterly_revenue_picks_latest_column(monkeypatch):
    import pandas as pd

    frame = pd.DataFrame(
        {pd.Timestamp("2026-06-30"): [119796000000.0], pd.Timestamp("2026-03-31"): [111116000000.0]},
        index=["Total Revenue"],
    )
    _fake_yf(monkeypatch, _rev_ticker(frame))
    out = er._yf_quarterly_revenue("GOOGL")
    assert out == {"period_end": date(2026, 6, 30), "revenue": 119796000000.0}


def test_yf_quarterly_revenue_nan_is_none(monkeypatch):
    import pandas as pd

    frame = pd.DataFrame(
        {pd.Timestamp("2026-03-31"): [float("nan")]}, index=["Total Revenue"])
    _fake_yf(monkeypatch, _rev_ticker(frame))
    assert er._yf_quarterly_revenue("WRLD") is None


def test_yf_quarterly_revenue_missing_row_is_none(monkeypatch):
    import pandas as pd

    frame = pd.DataFrame({pd.Timestamp("2026-03-31"): [1.0]}, index=["Cost Of Revenue"])
    _fake_yf(monkeypatch, _rev_ticker(frame))
    assert er._yf_quarterly_revenue("WRLD") is None


def test_yf_quarterly_revenue_empty_frame_is_none(monkeypatch):
    import pandas as pd

    _fake_yf(monkeypatch, _rev_ticker(pd.DataFrame()))
    assert er._yf_quarterly_revenue("WRLD") is None


def test_yf_quarterly_revenue_none_frame_is_none(monkeypatch):
    _fake_yf(monkeypatch, _rev_ticker(None))
    assert er._yf_quarterly_revenue("WRLD") is None


# --------------------------------------------------------------------------- #
# parse_release_revenue (pure)
# --------------------------------------------------------------------------- #
def test_parse_revenue_wrld_sentence_matches_only_the_to_clause():
    # The live incident: a hotpatch regex grabbed the $6.4M delta instead of
    # the $139.2M total. The to-clause pattern must ONLY surface the total.
    text = "Revenues increased $6.4 million, or 4.8%, to $139.2 million."
    assert er.parse_release_revenue(text) == [139200000.0]


def test_parse_revenue_of_pattern():
    assert er.parse_release_revenue("Revenue of $95.3 million for the quarter.") == [95300000.0]


def test_parse_revenue_was_were_pattern():
    assert er.parse_release_revenue("Net sales were $2.1 billion in Q3.") == [2100000000.0]


def test_parse_revenue_decreased_to_pattern():
    assert er.parse_release_revenue("Revenues decreased to $88.5 million.") == [88500000.0]


def test_parse_revenue_no_bare_revenues_pattern():
    # Deliberately NO bare "revenues ... $X" pattern (the WRLD failure mode).
    assert er.parse_release_revenue("Total revenues $200 million for the year.") == []


def test_parse_revenue_empty_and_none():
    assert er.parse_release_revenue("") == []
    assert er.parse_release_revenue(None) == []


def test_parse_revenue_case_insensitive_and_commas():
    assert er.parse_release_revenue("NET SALES OF $1,234.5 MILLION.") == [1234500000.0]


def test_parse_revenue_multiple_matches_deduped():
    text = "Revenue of $50 million. Net sales of $50 million. Revenue was $75 million."
    assert er.parse_release_revenue(text) == [50000000.0, 75000000.0]


def test_parse_release_revenue_ignores_cross_sentence_period_digit():
    # A sentence-ending period immediately followed by a digit (no space) must
    # not let the to-clause reach into the next sentence.
    text = ("Revenue decreased 8%.2026 guidance calls for margins to expand "
            "to $50 million next year.")
    assert er.parse_release_revenue(text) == []


# --------------------------------------------------------------------------- #
# resolve_revenue_actual (pure) — Spec A D3 acceptance fixtures + extras
# --------------------------------------------------------------------------- #
def _rev_cand(ticker, report_date, rev_est, rev_act):
    return {"ticker": ticker, "report_date": report_date, "rev_est": rev_est, "rev_act": rev_act}


def test_resolve_revenue_wrld_acceptance_agreement_short_circuit():
    # est 146,778,000; finnhub 139,210,000; parsed [6.4M, 139.2M]; yf stale
    # (period_end 2026-03-31 vs report 2026-07-24, 115 days) -> excluded.
    cand = _rev_cand("WRLD", "2026-07-24", 146778000.0, 139210000.0)
    release_text = "Revenues increased $6.4 million, or 4.8%, to $139.2 million."
    yf_row = {"period_end": date(2026, 3, 31), "revenue": 164312364.0}
    out = er.resolve_revenue_actual(cand, release_text, yf_row)
    assert out == (139210000.0, "finnhub", "")


def test_resolve_revenue_googl_acceptance_yfinance_wins():
    cand = _rev_cand("GOOGL", "2026-07-23", 120361387510.0, 103617000000.0)
    yf_row = {"period_end": date(2026, 6, 30), "revenue": 119796000000.0}
    out = er.resolve_revenue_actual(cand, "", yf_row)
    assert out == (119796000000.0, "yfinance", "")


def test_resolve_revenue_googl_acceptance_yf_absent_release_wins():
    cand = _rev_cand("GOOGL", "2026-07-23", 120361387510.0, 103617000000.0)
    release_text = "Google Cloud revenues were $119.8 billion for the quarter."
    out = er.resolve_revenue_actual(cand, release_text, None)
    assert out == (119800000000.0, "release", "")


def test_resolve_revenue_both_implausible_dropped():
    cand = _rev_cand("ZZZQ", "2026-07-24", 100000000.0, 1000000000.0)  # 10x est
    yf_row = {"period_end": date(2026, 7, 20), "revenue": 5000000.0}  # 0.05x est
    out = er.resolve_revenue_actual(cand, "", yf_row)
    assert out == (None, "dropped", "no plausible revenue")


def test_resolve_revenue_stale_yf_excluded_even_when_it_would_win():
    # Without the freshness guard, yf (100.5M) would beat finnhub (90M) on
    # distance to est (100M). Stale (116 days) -> excluded; finnhub wins.
    cand = _rev_cand("ZZZQ", "2026-07-24", 100000000.0, 90000000.0)
    yf_row = {"period_end": date(2026, 3, 30), "revenue": 100500000.0}
    out = er.resolve_revenue_actual(cand, "", yf_row)
    assert out == (90000000.0, "finnhub", "")


def test_resolve_revenue_freshness_boundary_60_days_included():
    cand = _rev_cand("ZZZQ", "2026-07-24", 100000000.0, 90000000.0)
    yf_row = {"period_end": date(2026, 5, 25), "revenue": 100000000.0}  # exactly 60d
    out = er.resolve_revenue_actual(cand, "", yf_row)
    assert out == (100000000.0, "yfinance", "")


def test_resolve_revenue_no_est_passthrough():
    cand = _rev_cand("ZZZQ", "2026-07-24", None, 12345.0)
    yf_row = {"period_end": date(2026, 7, 20), "revenue": 99999.0}
    out = er.resolve_revenue_actual(cand, "Revenue of $1 million.", yf_row)
    assert out == (12345.0, "finnhub", "no referee")


def test_resolve_revenue_finnhub_none_no_candidates_at_all():
    cand = _rev_cand("ZZZQ", "2026-07-24", 500.0, None)
    out = er.resolve_revenue_actual(cand, "", None)
    assert out == (None, "finnhub", "no revenue data")


def test_resolve_revenue_finnhub_none_arbitrates_among_rest():
    cand = _rev_cand("ZZZQ", "2026-07-24", 1000000.0, None)
    out = er.resolve_revenue_actual(cand, "Revenues were $0.95 million.", None)
    assert out == (950000.0, "release", "")


def test_resolve_revenue_agreement_short_circuit_with_yfinance():
    cand = _rev_cand("ZZZQ", "2026-07-24", 100000000.0, 99000000.0)
    yf_row = {"period_end": date(2026, 7, 20), "revenue": 101000000.0}  # within 5%
    out = er.resolve_revenue_actual(cand, "", yf_row)
    assert out == (99000000.0, "finnhub", "")


def test_resolve_revenue_near_tie_prefers_yfinance_over_finnhub():
    # finnhub (800) and yfinance (1250) are equidistant (log) from est (1000)
    # and NOT within 5% of each other -> no short-circuit; tie broken
    # structured-over-parsed: yfinance beats finnhub.
    cand = _rev_cand("ZZZQ", "2026-07-24", 1000.0, 800.0)
    yf_row = {"period_end": date(2026, 7, 20), "revenue": 1250.0}
    out = er.resolve_revenue_actual(cand, "", yf_row)
    assert out == (1250.0, "yfinance", "")


def test_resolve_revenue_near_tie_prefers_finnhub_over_release():
    # finnhub (800M) and the release-parsed value (1.25B) are equidistant
    # (log) from est (1B) and NOT within 5% of each other -> no short-circuit;
    # tie broken structured-over-parsed: finnhub (rank 1) beats release (rank 2).
    cand = _rev_cand("ZZZQ", "2026-07-24", 1000000000.0, 800000000.0)
    out = er.resolve_revenue_actual(cand, "Revenue of $1.25 billion.", None)
    assert out == (800000000.0, "finnhub", "")


# --------------------------------------------------------------------------- #
# build_post_earnings wiring — revenue arbitration
# --------------------------------------------------------------------------- #
def test_build_revenue_arbitration_overrides_top_candidate(monkeypatch, tmp_path):
    calendar = {"earningsCalendar": [
        {"symbol": "GOOGL", "date": "2026-07-23", "hour": "amc",
         "epsActual": 2.0, "epsEstimate": 1.8,
         "revenueActual": 103617000000.0, "revenueEstimate": 120361387510.0},
    ]}

    def fake_get_json(url, session, params=None, headers=None):
        if url == er.FINNHUB_URL:
            return calendar
        raise AssertionError("unexpected url: " + url)

    monkeypatch.setattr(er, "_get_json", fake_get_json)
    monkeypatch.setattr(er, "_yf_earnings_row", lambda t: None)
    monkeypatch.setattr(
        er, "fetch_price_reactions",
        lambda reporters, health: {r["ticker"]: 9.0 for r in reporters})
    monkeypatch.setattr(er, "fetch_earnings_release", lambda t, s, h: "RELEASE " + t)
    monkeypatch.setattr(er, "_finnhub_profile", lambda t, c, s: dict(EMPTY_PROFILE))
    monkeypatch.setattr(
        er, "_yf_quarterly_revenue",
        lambda t: {"period_end": date(2026, 6, 30), "revenue": 119796000000.0})
    _stub_context(monkeypatch)

    health = {}
    out = er.build_post_earnings(
        {"finnhub_key": "k"}, str(tmp_path / "raw"), health, "2026-07-25", str(tmp_path))
    googl = next(c for c in out if c["ticker"] == "GOOGL")
    assert googl["rev_act"] == 119796000000.0
    assert googl["rev_act_source"] == "yfinance"
    assert health["rev_check"] == {"ok": True, "note": "1 overridden, 0 dropped"}


def test_build_revenue_band_nulls_non_enriched_candidate(monkeypatch, tmp_path):
    # More US names than RELEASE_FETCH_MAX: the last one is NOT enriched but
    # still gets the free band-only check on its raw finnhub rev_act/rev_est.
    n = er.RELEASE_FETCH_MAX + 1
    rows = []
    for i in range(n):
        row = {"symbol": f"T{i:02d}", "date": "2026-07-10", "hour": "amc",
               "epsActual": 1.0 + (0.11 + 0.01 * i), "epsEstimate": 1.0}
        if i == 0:  # lowest score -> falls outside the top-N enrichment set
            row["revenueEstimate"] = 100.0
            row["revenueActual"] = 1000.0  # 10x -> outside 0.4-2.5 band
        rows.append(row)
    calendar = {"earningsCalendar": rows}

    def fake_get_json(url, session, params=None, headers=None):
        if url == er.FINNHUB_URL:
            return calendar
        raise AssertionError("unexpected url: " + url)

    monkeypatch.setattr(er, "_get_json", fake_get_json)
    monkeypatch.setattr(er, "_yf_earnings_row", lambda t: None)
    monkeypatch.setattr(
        er, "fetch_price_reactions",
        lambda reporters, health: {r["ticker"]: 9.0 for r in reporters})
    monkeypatch.setattr(er, "fetch_earnings_release", lambda t, s, h: "RELEASE " + t)
    monkeypatch.setattr(er, "_finnhub_profile", lambda t, c, s: dict(EMPTY_PROFILE))
    monkeypatch.setattr(er, "_yf_quarterly_revenue", lambda t: None)
    _stub_context(monkeypatch)

    health = {}
    out = er.build_post_earnings(
        {"finnhub_key": "k"}, str(tmp_path / "raw"), health, TODAY, str(tmp_path))
    lowest = next(c for c in out if c["ticker"] == "T00")
    assert lowest["rev_act"] is None
    assert lowest["rev_act_source"] == "dropped"
    assert health["rev_check"] == {"ok": True, "note": "0 overridden, 1 dropped"}


# --------------------------------------------------------------------------- #
# completeness (pure) — Spec A D4
# --------------------------------------------------------------------------- #
def _us_cand(**over):
    base = {
        "ticker": "ZZZQ", "report_date": "2026-07-10", "source": "us",
        "eps_est": 1.0, "eps_act": 1.1, "rev_est": 100.0, "rev_act": 100.0,
        "rev_act_source": "finnhub", "price_move_pct": 5.0,
        "release_excerpt": "some text",
    }
    base.update(over)
    return base


def _intl_cand(**over):
    base = {
        "ticker": "7203.T", "report_date": "2026-07-10", "source": "intl",
        "eps_est": None, "eps_act": 1.1, "price_move_pct": 5.0,
    }
    base.update(over)
    return base


def test_completeness_us_complete_enriched():
    assert er.completeness(_us_cand(), True) == []


def test_completeness_us_complete_not_enriched_excerpt_not_required():
    assert er.completeness(_us_cand(release_excerpt=""), False) == []


def test_completeness_us_missing_eps_est():
    assert er.completeness(_us_cand(eps_est=None), True) == ["eps_est"]


def test_completeness_us_missing_eps_act():
    assert er.completeness(_us_cand(eps_act=None), True) == ["eps_act"]


def test_completeness_us_missing_price_move():
    assert er.completeness(_us_cand(price_move_pct=None), True) == ["price_move_pct"]


def test_completeness_us_missing_release_excerpt_only_when_enriched():
    cand = _us_cand(release_excerpt="")
    assert er.completeness(cand, True) == ["release_excerpt"]
    assert er.completeness(cand, False) == []


def test_completeness_us_missing_revenue_when_unresolved():
    cand = _us_cand(rev_act=None, rev_act_source="finnhub")
    assert er.completeness(cand, True) == ["rev_act"]


def test_completeness_us_dropped_revenue_counts_resolved():
    cand = _us_cand(rev_act=None, rev_act_source="dropped")
    assert er.completeness(cand, True) == []


def test_completeness_us_missing_rev_est_means_not_required():
    cand = _us_cand(rev_est=None, rev_act=None, rev_act_source="finnhub")
    assert er.completeness(cand, True) == []


def test_completeness_us_multiple_missing_preserves_order():
    cand = _us_cand(eps_act=None, price_move_pct=None)
    assert er.completeness(cand, True) == ["eps_act", "price_move_pct"]


def test_completeness_intl_complete():
    assert er.completeness(_intl_cand(), True) == []


def test_completeness_intl_eps_est_never_required():
    assert er.completeness(_intl_cand(eps_est=None), True) == []


def test_completeness_intl_missing_eps_act():
    assert er.completeness(_intl_cand(eps_act=None), True) == ["eps_act"]


def test_completeness_intl_missing_price_move():
    assert er.completeness(_intl_cand(price_move_pct=None), True) == ["price_move_pct"]


def test_completeness_intl_ignores_revenue_and_release():
    cand = _intl_cand(rev_act=None, rev_act_source="dropped", release_excerpt="")
    assert er.completeness(cand, True) == []


# --------------------------------------------------------------------------- #
# _current_gates (pure) — shared by prefilter_candidates and expiry routing
# --------------------------------------------------------------------------- #
def test_current_gates_size_only():
    cand = {"ticker": "AAPL", "surprise_pct": None, "eps_est": None, "price_move_pct": None}
    assert er._current_gates(cand) == ["size"]


def test_current_gates_none_trip():
    cand = {"ticker": "ZZZQ", "surprise_pct": 1.0, "eps_est": 1.0, "price_move_pct": 1.0}
    assert er._current_gates(cand) == []


def test_current_gates_matches_prefilter_gates():
    reporter = _reporter(ticker="NVDA", eps_est=1.0, surprise=20.0)
    out = er.prefilter_candidates([reporter], {"NVDA": 9.0}, set())
    assert er._current_gates(out[0]) == out[0]["gates"]


# --------------------------------------------------------------------------- #
# note_pending corrupted-attempts guard (Task-1 reviewer flag)
# --------------------------------------------------------------------------- #
def test_note_pending_corrupted_attempts_coerced_to_zero():
    pipeline = {"WRLD:2026-07-24": {
        "status": "pending", "first_seen": "2026-07-24", "last_seen": "2026-07-24",
        "attempts": "oops", "missing": ["price_move_pct"]}}
    er.note_pending(pipeline, "WRLD:2026-07-24", ["revenue"], "2026-07-25")
    assert pipeline["WRLD:2026-07-24"]["attempts"] == 1


# --------------------------------------------------------------------------- #
# _route_pending (routing core) — Spec A D5
# --------------------------------------------------------------------------- #
def _pe_cand(ticker="ZZZQ", report_date="2026-07-24", source="us", **over):
    base = {
        "ticker": ticker, "report_date": report_date, "source": source,
        "eps_est": 1.0, "eps_act": 1.1, "rev_est": None, "rev_act": None,
        "rev_act_source": "finnhub", "price_move_pct": 5.0,
        "release_excerpt": "text", "surprise_pct": 10.0,
    }
    base.update(over)
    return base


def test_route_pending_complete_candidate_published_untouched():
    pipeline = {}
    cand = _pe_cand()
    published, pending, expired, missing_labels = er._route_pending(
        [cand], [cand], pipeline, "2026-07-25")
    assert published == [cand]
    assert pending == 0 and expired == 0 and missing_labels == []
    assert pipeline == {}


def test_route_pending_incomplete_first_attempt_deferred_and_excluded():
    pipeline = {}
    cand = _pe_cand(price_move_pct=None)
    published, pending, expired, missing_labels = er._route_pending(
        [cand], [cand], pipeline, "2026-07-25")
    assert published == []
    assert pending == 1 and missing_labels == ["price_move_pct"]
    key = "ZZZQ:2026-07-24"
    assert pipeline[key]["status"] == "pending"
    assert pipeline[key]["attempts"] == 1
    assert pipeline[key]["missing"] == ["price_move_pct"]


def test_route_pending_max_attempts_gate_trips_best_effort_publish():
    key = "ZZZQ:2026-07-24"
    pipeline = {key: {"status": "pending", "first_seen": "2026-07-21",
                       "last_seen": "2026-07-24", "attempts": er.MAX_ATTEMPTS - 1,
                       "missing": ["price_move_pct"]}}
    cand = _pe_cand(price_move_pct=None, surprise_pct=20.0)  # surprise gate still trips
    published, pending, expired, missing_labels = er._route_pending(
        [cand], [cand], pipeline, "2026-07-25")
    assert published == [cand]  # best-effort publish
    assert pending == 0 and expired == 0
    # Ledger entry left as-is — mark_covered flips it once the brief succeeds.
    assert pipeline[key]["status"] == "pending"
    assert pipeline[key]["attempts"] == er.MAX_ATTEMPTS - 1


def test_route_pending_max_attempts_no_gate_expires():
    key = "ZZZQ:2026-07-24"
    pipeline = {key: {"status": "pending", "first_seen": "2026-07-21",
                       "last_seen": "2026-07-24", "attempts": er.MAX_ATTEMPTS - 1,
                       "missing": ["price_move_pct"]}}
    cand = _pe_cand(price_move_pct=None, surprise_pct=1.0, eps_est=1.0)  # no gate trips
    published, pending, expired, missing_labels = er._route_pending(
        [cand], [cand], pipeline, "2026-07-25")
    assert published == []
    assert pending == 0 and expired == 1
    assert pipeline[key] == {
        "status": "expired", "first_seen": "2026-07-21", "last_seen": "2026-07-25",
        "attempts": er.MAX_ATTEMPTS, "missing": ["price_move_pct"],
    }


def test_route_pending_corrupted_attempts_coerced_to_zero():
    key = "ZZZQ:2026-07-24"
    pipeline = {key: {"status": "pending", "first_seen": "2026-07-20",
                       "last_seen": "2026-07-24", "attempts": "not-a-number",
                       "missing": ["eps_act"]}}
    cand = _pe_cand(eps_act=None)
    published, pending, expired, missing_labels = er._route_pending(
        [cand], [cand], pipeline, "2026-07-25")
    assert published == []
    assert pending == 1
    assert pipeline[key]["attempts"] == 1  # coerced 0 -> +1, no raise


def test_route_pending_enriched_flag_uses_enrich_membership_not_content():
    # A candidate present in `enrich` but whose release fetch came back empty
    # is missing release_excerpt; the same candidate outside `enrich` is not.
    cand_in = _pe_cand(ticker="AAAA", release_excerpt="")
    cand_out = _pe_cand(ticker="BBBB", release_excerpt="")
    published, pending, expired, missing_labels = er._route_pending(
        [cand_in, cand_out], [cand_in], {}, "2026-07-25")
    assert published == [cand_out]  # not enriched -> excerpt not required
    assert pending == 1 and missing_labels == ["release_excerpt"]


# --------------------------------------------------------------------------- #
# _pipeline_note (pure wording) — Spec A D5
# --------------------------------------------------------------------------- #
def test_pipeline_note_all_zero():
    assert er._pipeline_note(0, 0, 0, []) == "0 published, 0 pending, 0 expired"


def test_pipeline_note_pending_breakdown_multi_label():
    labels = ["price_move_pct", "price_move_pct", "rev_act"]
    assert er._pipeline_note(5, 3, 1, labels) == (
        "5 published, 3 pending (2 awaiting price_move_pct, 1 awaiting rev_act), 1 expired")


def test_pipeline_note_no_pending_omits_breakdown():
    assert er._pipeline_note(2, 0, 0, []) == "2 published, 0 pending, 0 expired"


# --------------------------------------------------------------------------- #
# build_post_earnings — completeness routing integration (Spec A D5)
# --------------------------------------------------------------------------- #
def _stub_build_common(monkeypatch, calendar, price_moves_map):
    """Wire the standard build_post_earnings stub set, with a caller-supplied
    calendar and a fixed {ticker: price_move_pct} map for fetch_price_reactions."""
    def fake_get_json(url, session, params=None, headers=None):
        if url == er.FINNHUB_URL:
            return calendar
        raise AssertionError("unexpected url: " + url)

    monkeypatch.setattr(er, "_get_json", fake_get_json)
    monkeypatch.setattr(er, "_yf_earnings_row", lambda t: None)
    monkeypatch.setattr(
        er, "fetch_price_reactions", lambda reporters, health: dict(price_moves_map))
    monkeypatch.setattr(er, "fetch_earnings_release", lambda t, s, h: "RELEASE " + t)
    monkeypatch.setattr(er, "_finnhub_profile", lambda t, c, s: dict(EMPTY_PROFILE))
    monkeypatch.setattr(er, "_yf_quarterly_revenue", lambda t: None)
    _stub_context(monkeypatch)


def test_build_complete_bmo_candidate_publishes_unchanged(monkeypatch, tmp_path):
    # Acceptance 5 / "no behavior change" check: a fully-complete candidate
    # publishes exactly as before, nothing deferred.
    calendar = {"earningsCalendar": [
        {"symbol": "AAPL", "date": "2026-07-10", "hour": "bmo",
         "epsActual": 2.4, "epsEstimate": 2.0,
         "revenueActual": 90.0, "revenueEstimate": 85.0},
    ]}
    _stub_build_common(monkeypatch, calendar, {"AAPL": 5.0})
    health = {}
    out = er.build_post_earnings(
        {"finnhub_key": "k"}, str(tmp_path / "raw"), health, "2026-07-11", str(tmp_path))
    assert {c["ticker"] for c in out} == {"AAPL"}
    aapl = out[0]
    assert aapl["price_move_pct"] == 5.0
    assert aapl["rev_act"] == 90.0 and aapl["rev_act_source"] == "finnhub"
    assert aapl["release_excerpt"] == "RELEASE AAPL"
    assert health["pe_pipeline"] == {"ok": True, "note": "1 published, 0 pending, 0 expired"}
    assert er.read_pipeline(str(tmp_path)) == {}  # nothing deferred


def test_build_amc_round_trip_pending_then_published(monkeypatch, tmp_path):
    # Acceptance 4: day 1, no post-report bar yet -> pending, excluded, ledger
    # written; day 2, the real next-session close arrives -> publishes, gates
    # evaluated on the REAL move.
    ticker = "WRLD"
    kb = str(tmp_path)
    calendar = {"earningsCalendar": [
        {"symbol": ticker, "date": "2026-07-24", "hour": "amc",
         "epsActual": 15.0, "epsEstimate": 1.0},  # huge surprise -> a gate trips regardless
    ]}

    _stub_build_common(monkeypatch, calendar, {ticker: None})
    health = {}
    out = er.build_post_earnings(
        {"finnhub_key": "k"}, str(tmp_path / "raw"), health, "2026-07-25", kb)
    assert out == []
    key = f"{ticker}:2026-07-24"
    pipeline = er.read_pipeline(kb)
    assert pipeline[key]["status"] == "pending"
    assert pipeline[key]["missing"] == ["price_move_pct"]
    assert pipeline[key]["attempts"] == 1
    assert health["pe_pipeline"]["note"] == (
        "0 published, 1 pending (1 awaiting price_move_pct), 0 expired")

    _stub_build_common(monkeypatch, calendar, {ticker: 12.0})
    health2 = {}
    out2 = er.build_post_earnings(
        {"finnhub_key": "k"}, str(tmp_path / "raw"), health2, "2026-07-26", kb)
    assert {c["ticker"] for c in out2} == {ticker}
    wrld = out2[0]
    assert wrld["price_move_pct"] == 12.0
    assert "reaction" in wrld["gates"]  # gates evaluated on the REAL move
    pipeline2 = er.read_pipeline(kb)
    assert pipeline2[key]["status"] == "pending"  # only mark_covered flips this


def test_build_unknown_hour_publishes_day_one(monkeypatch, tmp_path):
    # Acceptance 5: unknown hour -> bmo rule -> price present day 1 -> no
    # systematic deferral.
    calendar = {"earningsCalendar": [
        {"symbol": "ZZZQ", "date": "2026-07-24", "hour": "",
         "epsActual": 1.5, "epsEstimate": 1.0},
    ]}

    def fake_get_json(url, session, params=None, headers=None):
        if url == er.FINNHUB_URL:
            return calendar
        raise AssertionError("unexpected url: " + url)

    monkeypatch.setattr(er, "_get_json", fake_get_json)
    monkeypatch.setattr(er, "_yf_earnings_row", lambda t: None)
    bars = {"ZZZQ": [(date(2026, 7, 23), 100.0), (date(2026, 7, 24), 108.0)]}
    monkeypatch.setattr(er, "_yf_reaction_frame", lambda ts: bars)
    monkeypatch.setattr(er, "fetch_earnings_release", lambda t, s, h: "RELEASE " + t)
    monkeypatch.setattr(er, "_finnhub_profile", lambda t, c, s: dict(EMPTY_PROFILE))
    monkeypatch.setattr(er, "_yf_quarterly_revenue", lambda t: None)
    _stub_context(monkeypatch)

    health = {}
    out = er.build_post_earnings(
        {"finnhub_key": "k"}, str(tmp_path / "raw"), health, "2026-07-25", str(tmp_path))
    assert {c["ticker"] for c in out} == {"ZZZQ"}
    assert out[0]["price_move_pct"] == 8.0  # bmo rule applied to unknown hour
    assert er.read_pipeline(str(tmp_path)) == {}  # nothing deferred


def test_build_expiry_gate_trips_best_effort_publish(monkeypatch, tmp_path):
    # Acceptance 7, branch 1: attempts exhausted but a gate still trips on
    # current data -> publish best-effort despite the gap.
    ticker = "ZZZQ"
    key = f"{ticker}:2026-07-24"
    kb = str(tmp_path)
    er.write_pipeline(kb, {key: {
        "status": "pending", "first_seen": "2026-07-21", "last_seen": "2026-07-24",
        "attempts": er.MAX_ATTEMPTS - 1, "missing": ["price_move_pct"]}})
    calendar = {"earningsCalendar": [
        {"symbol": ticker, "date": "2026-07-24", "hour": "amc",
         "epsActual": 15.0, "epsEstimate": 1.0},  # surprise gate still trips
    ]}
    _stub_build_common(monkeypatch, calendar, {ticker: None})  # price still missing
    health = {}
    out = er.build_post_earnings(
        {"finnhub_key": "k"}, str(tmp_path / "raw"), health, "2026-07-25", kb)
    assert {c["ticker"] for c in out} == {ticker}
    assert out[0]["price_move_pct"] is None
    pipeline = er.read_pipeline(kb)
    assert pipeline[key]["status"] == "pending"  # left for mark_covered to flip


# NOTE on acceptance 7, branch 2 (no gate trips -> expired): this branch is
# structurally unreachable via the full build_post_earnings orchestration —
# prefilter_candidates already requires a tripped gate for a row to become a
# candidate at all, so anything that reaches the routing step necessarily has
# a non-empty "gates". The routing core is exercised directly instead; see
# test_route_pending_max_attempts_no_gate_expires above.


# --------------------------------------------------------------------------- #
# expire_stale_pending: a pending entry whose gate condition disappeared (a
# vendor revision means prefilter_candidates no longer routes it at all) must
# still retire instead of sitting pending forever (pending entries are exempt
# from mark_covered's recency prune).
# --------------------------------------------------------------------------- #
def test_expire_stale_pending_boundary_exactly_window_days_stays_pending():
    pipeline = {"A:2026-07-10": {
        "status": "pending", "first_seen": "2026-07-10", "last_seen": "2026-07-20",
        "attempts": 2, "missing": ["price_move_pct"]}}
    er.expire_stale_pending(pipeline, "2026-07-25")  # exactly WINDOW_DAYS (5) old
    assert pipeline["A:2026-07-10"]["status"] == "pending"


def test_expire_stale_pending_one_day_past_window_expires():
    pipeline = {"A:2026-07-10": {
        "status": "pending", "first_seen": "2026-07-10", "last_seen": "2026-07-19",
        "attempts": 2, "missing": ["price_move_pct"]}}
    er.expire_stale_pending(pipeline, "2026-07-25")  # WINDOW_DAYS + 1 old
    assert pipeline["A:2026-07-10"]["status"] == "expired"


def test_expire_stale_pending_covered_and_expired_untouched_regardless_of_age():
    pipeline = {
        "A:2026-07-10": {"status": "covered", "first_seen": "2020-01-01",
                          "last_seen": "2020-01-01", "attempts": 0, "missing": []},
        "B:2026-07-10": {"status": "expired", "first_seen": "2020-01-01",
                          "last_seen": "2020-01-01", "attempts": 4, "missing": []},
    }
    er.expire_stale_pending(pipeline, "2026-07-25")
    assert pipeline["A:2026-07-10"]["status"] == "covered"
    assert pipeline["B:2026-07-10"]["status"] == "expired"


def test_expire_stale_pending_unparseable_last_seen_expires():
    pipeline = {"A:2026-07-10": {
        "status": "pending", "first_seen": "2026-07-10", "last_seen": "not-a-date",
        "attempts": 1, "missing": ["price_move_pct"]}}
    er.expire_stale_pending(pipeline, "2026-07-25")
    assert pipeline["A:2026-07-10"]["status"] == "expired"


def test_expire_stale_pending_non_dict_entry_ignored_without_raising():
    pipeline = {"A:2026-07-10": "not-a-dict"}
    er.expire_stale_pending(pipeline, "2026-07-25")  # must not raise
    assert pipeline == {"A:2026-07-10": "not-a-dict"}


def test_build_expires_stale_pending_entry_dropped_by_gate(monkeypatch, tmp_path):
    # End-to-end: a stale pending entry whose gate condition no longer trips
    # (so prefilter_candidates never routes it back through _route_pending)
    # must be expired by build_post_earnings, not left pending forever.
    kb = str(tmp_path)
    key = "WRLD:2026-07-10"
    er.write_pipeline(kb, {key: {
        "status": "pending", "first_seen": "2026-07-10", "last_seen": "2026-07-19",
        "attempts": 2, "missing": ["price_move_pct"]}})
    monkeypatch.setattr(er, "_yf_earnings_row", lambda t: None)  # quiet intl lane

    health = {}
    out = er.build_post_earnings(
        {}, str(tmp_path / "raw"), health, "2026-07-25", kb)  # no finnhub_key -> quiet US lane
    assert out == []
    pipeline = er.read_pipeline(kb)
    assert pipeline[key]["status"] == "expired"
    assert key in er.blocked_keys(pipeline)


# --------------------------------------------------------------------------- #
# _attach_fundamentals — the SEC fundamentals lane (Global Constraint 8: the
# lane decorates and never defers/blocks/alters a candidate)
# --------------------------------------------------------------------------- #
def _fund_cand(ticker, source="us", report_date="2026-07-10"):
    """A candidate row already carrying its fundamentals defaults, as
    build_post_earnings would hand it to _attach_fundamentals."""
    cand = _reporter(ticker=ticker, report_date=report_date, source=source)
    cand["fundamentals"] = None
    cand["fundamentals_flags"] = []
    return cand


def test_attach_fundamentals_skips_non_bellwether(monkeypatch):
    calls = []
    monkeypatch.setattr(sf, "build_fundamentals",
                         lambda *a, **k: calls.append(a) or {"status": "ok"})
    monkeypatch.setattr(er, "_CIK_CACHE", {"ZZZQ": "0000000001"})
    cand = _fund_cand("ZZZQ")  # not in BELLWETHER_TICKERS
    health = {}
    er._attach_fundamentals([cand], [cand], {}, None, health)
    assert cand["fundamentals"] is None
    assert cand["fundamentals_flags"] == []
    assert calls == []
    assert "sec_fundamentals" not in health


def test_attach_fundamentals_skips_bellwether_outside_enrich(monkeypatch):
    calls = []
    monkeypatch.setattr(sf, "build_fundamentals",
                         lambda *a, **k: calls.append(a) or {"status": "ok"})
    monkeypatch.setattr(er, "_CIK_CACHE", {"AAPL": "0000320193"})
    cand = _fund_cand("AAPL")
    health = {}
    er._attach_fundamentals([cand], [], {}, None, health)  # enrich is empty
    assert cand["fundamentals"] is None
    assert calls == []
    assert "sec_fundamentals" not in health


def test_attach_fundamentals_health_note_counts_by_status_and_names_tickers(monkeypatch):
    cik_map = {
        "AAPL": "0000320193", "MSFT": "0000789019",
        "NVDA": "0001045810", "GOOGL": "0001652044",
    }
    statuses = {"AAPL": "ok", "MSFT": "ok", "NVDA": "ok", "GOOGL": "stale"}
    seen_deadlines = set()

    def fake_build(cik, report_date, session, health, deadline_at):
        ticker = next(t for t, c in cik_map.items() if c == cik)
        seen_deadlines.add(deadline_at)
        return {"status": statuses[ticker]}

    monkeypatch.setattr(sf, "build_fundamentals", fake_build)
    monkeypatch.setattr(er, "_CIK_CACHE", dict(cik_map))
    cands = [_fund_cand(t) for t in cik_map]
    health = {}
    er._attach_fundamentals(cands, cands, {}, None, health)

    for cand in cands:
        assert cand["fundamentals"]["status"] == statuses[cand["ticker"]]
    assert health["sec_fundamentals"] == {"ok": True, "note": "3 ok, 1 stale, 0 unavailable"}
    # One deadline for the whole lane, computed once — not per candidate.
    assert len(seen_deadlines) == 1
    # Diagnostics must name tickers, not CIKs (a reader can't use "0000789019").
    for cik in cik_map.values():
        assert cik not in health["sec_fundamentals"]["note"]


def test_attach_fundamentals_missing_cik_leaves_default_and_is_not_called(monkeypatch):
    calls = []
    monkeypatch.setattr(sf, "build_fundamentals",
                         lambda *a, **k: calls.append(a) or {"status": "ok"})
    # Non-empty (so _load_cik_map returns it as-is, no HTTP call) but AAPL unmapped.
    monkeypatch.setattr(er, "_CIK_CACHE", {"ZZZQ": "0000000001"})
    cand = _fund_cand("AAPL")
    health = {}
    er._attach_fundamentals([cand], [cand], {}, None, health)
    assert cand["fundamentals"] is None
    assert calls == []


def test_attach_fundamentals_raise_never_escapes_and_marks_ok_false(monkeypatch):
    def boom(cik, report_date, session, health, deadline_at):
        raise RuntimeError("boom")

    monkeypatch.setattr(sf, "build_fundamentals", boom)
    monkeypatch.setattr(er, "_CIK_CACHE", {"AAPL": "0000320193"})
    cand = _fund_cand("AAPL")
    health = {}
    er._attach_fundamentals([cand], [cand], {}, None, health)  # must not raise
    assert cand["fundamentals"] is None  # untouched — the lane never got a result
    assert cand["fundamentals_flags"] == []
    assert health["sec_fundamentals"]["ok"] is False


def test_attach_fundamentals_zero_eligible_is_not_a_failure(monkeypatch):
    # No bellwethers reported this morning is the common case — an idle lane
    # must not look like a broken one.
    calls = []
    monkeypatch.setattr(sf, "build_fundamentals",
                         lambda *a, **k: calls.append(a) or {"status": "ok"})
    cand = _fund_cand("ZZZQ")  # not a bellwether -> zero eligible
    health = {}
    er._attach_fundamentals([cand], [cand], {}, None, health)
    assert calls == []
    assert "sec_fundamentals" not in health


def test_attach_fundamentals_all_unavailable_reports_ok_false(monkeypatch):
    # The lane ran to completion and produced nothing usable for any
    # candidate — distinct from "no eligible candidates" and from a raise.
    cik_map = {"AAPL": "0000320193", "MSFT": "0000789019"}
    monkeypatch.setattr(sf, "build_fundamentals",
                         lambda *a, **k: {"status": "unavailable"})
    monkeypatch.setattr(er, "_CIK_CACHE", dict(cik_map))
    cands = [_fund_cand(t) for t in cik_map]
    health = {}
    er._attach_fundamentals(cands, cands, {}, None, health)
    assert health["sec_fundamentals"] == {"ok": False, "note": "0 ok, 0 stale, 2 unavailable"}


def test_attach_fundamentals_now_attribute_error_is_contained(monkeypatch):
    # The deadline computation and the per-iteration deadline check read
    # sec_fundamentals attributes OUTSIDE the guarded import/CIK-lookup
    # try/excepts. If sec_fundamentals ever lost `_now`, that AttributeError
    # must not escape _attach_fundamentals.
    def boom():
        raise AttributeError("module 'sec_fundamentals' has no attribute '_now'")

    monkeypatch.setattr(sf, "_now", boom)
    monkeypatch.setattr(er, "_CIK_CACHE", {"AAPL": "0000320193"})
    cand = _fund_cand("AAPL")
    health = {}
    er._attach_fundamentals([cand], [cand], {}, None, health)  # must not raise
    assert cand["fundamentals"] is None
    assert health["sec_fundamentals"]["ok"] is False


def test_build_fundamentals_now_attribute_error_brief_survives(monkeypatch, tmp_path):
    # Full-pipeline version: the failure mode the constraint exists to make
    # impossible is an AttributeError escaping into build_post_earnings'
    # blanket handler, which would return [] — the whole section vanishes.
    calendar = {"earningsCalendar": [
        {"symbol": "AAPL", "date": "2026-07-10", "hour": "bmo",
         "epsActual": 2.4, "epsEstimate": 2.0,
         "revenueActual": 90.0, "revenueEstimate": 85.0},
    ]}

    def boom():
        raise AttributeError("module 'sec_fundamentals' has no attribute '_now'")

    _stub_build_common(monkeypatch, calendar, {"AAPL": 5.0})
    monkeypatch.setattr(er, "_CIK_CACHE", {"AAPL": "0000320193"})
    monkeypatch.setattr(sf, "_now", boom)
    health = {}
    out = er.build_post_earnings(
        {"finnhub_key": "k"}, str(tmp_path / "raw"), health, "2026-07-11", str(tmp_path))
    assert {c["ticker"] for c in out} == {"AAPL"}  # brief survives, not []
    assert health["sec_fundamentals"]["ok"] is False


# --------------------------------------------------------------------------- #
# build_post_earnings integration — Global Constraint 8 acceptance:
# fundamentals decorate and can never alter/defer/block a candidate.
# --------------------------------------------------------------------------- #
def test_build_fundamentals_raising_is_byte_identical_to_no_lane_run(monkeypatch, tmp_path):
    real_attach_fundamentals = er._attach_fundamentals  # capture before any patching
    calendar = {"earningsCalendar": [
        {"symbol": "AAPL", "date": "2026-07-10", "hour": "bmo",
         "epsActual": 2.4, "epsEstimate": 2.0,
         "revenueActual": 90.0, "revenueEstimate": 85.0},
    ]}

    def boom(cik, report_date, session, health, deadline_at):
        raise RuntimeError("SEC down")

    _stub_build_common(monkeypatch, calendar, {"AAPL": 5.0})
    monkeypatch.setattr(er, "_attach_fundamentals", lambda *a, **k: None)
    kb_no_lane = str(tmp_path / "no_lane")
    health_no_lane = {}
    out_no_lane = er.build_post_earnings(
        {"finnhub_key": "k"}, str(tmp_path / "no_lane_raw"), health_no_lane,
        "2026-07-11", kb_no_lane)

    _stub_build_common(monkeypatch, calendar, {"AAPL": 5.0})
    monkeypatch.setattr(er, "_attach_fundamentals", real_attach_fundamentals)
    monkeypatch.setattr(sf, "build_fundamentals", boom)
    monkeypatch.setattr(er, "_CIK_CACHE", {"AAPL": "0000320193"})
    kb_raising = str(tmp_path / "raising")
    health_raising = {}
    out_raising = er.build_post_earnings(
        {"finnhub_key": "k"}, str(tmp_path / "raising_raw"), health_raising,
        "2026-07-11", kb_raising)

    assert out_raising == out_no_lane  # byte-identical candidate list
    assert er.read_pipeline(kb_raising) == er.read_pipeline(kb_no_lane) == {}
    assert health_raising["sec_fundamentals"]["ok"] is False


def test_fundamentals_flags_default_not_shared_between_candidates(monkeypatch, tmp_path):
    # Global aliasing trap: mutate one candidate's fundamentals_flags and
    # confirm another candidate's list is a distinct object.
    calendar = {"earningsCalendar": [
        {"symbol": "AAA", "date": "2026-07-10", "hour": "bmo",
         "epsActual": 2.0, "epsEstimate": 1.0},
        {"symbol": "BBB", "date": "2026-07-10", "hour": "bmo",
         "epsActual": 2.0, "epsEstimate": 1.0},
    ]}
    _stub_build_common(monkeypatch, calendar, {"AAA": 1.0, "BBB": 1.0})
    health = {}
    out = er.build_post_earnings(
        {"finnhub_key": "k"}, str(tmp_path / "raw"), health, "2026-07-11", str(tmp_path))
    assert {c["ticker"] for c in out} == {"AAA", "BBB"}
    a = next(c for c in out if c["ticker"] == "AAA")
    b = next(c for c in out if c["ticker"] == "BBB")
    assert a["fundamentals_flags"] == [] and b["fundamentals_flags"] == []
    a["fundamentals_flags"].append("mutated")
    assert b["fundamentals_flags"] == []


# --------------------------------------------------------------------------- #
# referee_flags — SEC-filed figures cross-checking the vendor's reported
# actuals (Task 5). The referee REPORTS disagreement; it never adjudicates:
# eps_act, surprise_pct and gates must come out byte-identical regardless of
# what it finds. All four preconditions must hold or it returns [].
# --------------------------------------------------------------------------- #
def _aapl_fund(eps_value=2.02, eps_basis="direct", revenue_value=109417000000,
               status="ok", form="10-Q"):
    """The live AAPL case fixed in the brief: SEC diluted EPS 2.02 vs
    Finnhub's reported actual 1.91, with revenue matching exactly."""
    return {
        "status": status,
        "revenue_q": {"value": revenue_value, "basis": "direct",
                      "tag": "Revenues", "yoy_pct": None},
        "eps_diluted_q": {"value": eps_value, "basis": eps_basis,
                          "tag": "EarningsPerShareDiluted", "yoy_pct": None},
        "form": form,
    }


def _aapl_cand(eps_act=1.91, rev_act=109417000000):
    cand = _reporter(ticker="AAPL", eps_act=eps_act, surprise=-5.4)
    cand["rev_act"] = rev_act
    cand["gates"] = ["surprise"]
    return cand


def test_referee_flags_aapl_disagreement_one_flag_candidate_untouched():
    cand = _aapl_cand()
    fund = _aapl_fund()
    before = copy.deepcopy(cand)
    flags = er.referee_flags(cand, fund)
    assert flags == [{"kind": "eps_disagreement", "sec": 2.02, "vendor": 1.91,
                       "form": "10-Q"}]
    # The referee reports, never adjudicates -- byte-identical, explicitly.
    assert cand == before
    assert cand["eps_act"] == before["eps_act"]
    assert cand["surprise_pct"] == before["surprise_pct"]
    assert cand["gates"] == before["gates"]


def test_referee_flags_revenue_disagreeing_5pct_returns_empty():
    # Revenue mismatch means the two sources may not describe the same
    # period -- the referee must not accuse the vendor on a misalignment.
    cand = _aapl_cand(rev_act=109417000000 * 1.05)
    assert er.referee_flags(cand, _aapl_fund()) == []


def test_referee_flags_derived_eps_basis_returns_empty():
    # Precondition 3: a derived per-share figure (MSFT's 4.81-vs-4.74 case)
    # may never accuse a vendor of being wrong.
    cand = _aapl_cand()
    assert er.referee_flags(cand, _aapl_fund(eps_basis="derived")) == []


def test_referee_flags_blocked_eps_basis_returns_empty():
    cand = _aapl_cand()
    assert er.referee_flags(cand, _aapl_fund(eps_basis="blocked")) == []


def test_referee_flags_stitched_eps_basis_returns_empty():
    cand = _aapl_cand()
    assert er.referee_flags(cand, _aapl_fund(eps_basis="stitched")) == []


def test_referee_flags_eps_agreement_within_1pct_returns_empty():
    cand = _aapl_cand(eps_act=2.015)  # within 1% of the SEC figure, 2.02
    assert er.referee_flags(cand, _aapl_fund()) == []


def test_referee_flags_status_not_ok_returns_empty():
    cand = _aapl_cand()
    assert er.referee_flags(cand, _aapl_fund(status="stale")) == []


def test_referee_flags_missing_eps_diluted_q_returns_empty():
    cand = _aapl_cand()
    fund = _aapl_fund()
    fund["eps_diluted_q"] = None
    assert er.referee_flags(cand, fund) == []


def test_referee_flags_missing_revenue_q_returns_empty():
    cand = _aapl_cand()
    fund = _aapl_fund()
    fund["revenue_q"] = None
    assert er.referee_flags(cand, fund) == []


def test_referee_flags_missing_cand_rev_act_returns_empty():
    cand = _aapl_cand(rev_act=None)
    assert er.referee_flags(cand, _aapl_fund()) == []


def test_referee_flags_missing_cand_eps_act_returns_empty():
    cand = _aapl_cand(eps_act=None)
    assert er.referee_flags(cand, _aapl_fund()) == []


def test_referee_flags_zero_rev_act_does_not_raise():
    cand = _aapl_cand(rev_act=0)
    assert er.referee_flags(cand, _aapl_fund()) == []  # guarded, not a ZeroDivisionError


def test_referee_flags_zero_eps_act_does_not_raise():
    cand = _aapl_cand(eps_act=0)
    assert er.referee_flags(cand, _aapl_fund()) == []  # guarded, not a ZeroDivisionError


def test_referee_flags_none_fundamentals_returns_empty():
    assert er.referee_flags(_aapl_cand(), None) == []


def test_referee_flags_non_dict_fundamentals_does_not_raise():
    assert er.referee_flags(_aapl_cand(), "not-a-dict") == []


# --------------------------------------------------------------------------- #
# _attach_fundamentals wires referee_flags: cand["fundamentals_flags"] is
# populated for every eligible candidate immediately after cand["fundamentals"]
# is set, so beat-1 rendering (Task 8) has it without a second pass.
# --------------------------------------------------------------------------- #
def test_attach_fundamentals_wires_referee_flags_for_disagreement(monkeypatch):
    fund = _aapl_fund()
    monkeypatch.setattr(sf, "build_fundamentals", lambda *a, **k: fund)
    monkeypatch.setattr(er, "_CIK_CACHE", {"AAPL": "0000320193"})
    cand = _fund_cand("AAPL")
    cand["eps_act"] = 1.91
    cand["rev_act"] = 109417000000
    cand["surprise_pct"] = -5.4
    cand["gates"] = ["surprise"]
    before = copy.deepcopy(cand)
    health = {}
    er._attach_fundamentals([cand], [cand], {}, None, health)
    assert cand["fundamentals_flags"] == [
        {"kind": "eps_disagreement", "sec": 2.02, "vendor": 1.91, "form": "10-Q"}]
    assert cand["eps_act"] == before["eps_act"]
    assert cand["surprise_pct"] == before["surprise_pct"]
    assert cand["gates"] == before["gates"]


def test_referee_flags_end_to_end_form_10q_via_real_build_fundamentals(monkeypatch):
    # resolve_period -> build_fundamentals -> referee_flags, no hand-rolled
    # fundamentals dict: proves `form` really threads through the real
    # sec_fundamentals pipeline, not just a test fixture that assumes it.
    subs = {"filings": {"recent": {
        "form": ["10-Q"],
        "filingDate": ["2026-07-31"],
        "reportDate": ["2026-06-27"],
        "accessionNumber": ["0000320193-26-000020"],
        "fy": [2026], "fp": ["Q3"],
    }}}
    concepts = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            {"start": "2026-03-29", "end": "2026-06-27", "val": 109417000000,
             "form": "10-Q", "filed": "2026-07-31", "fy": 2026, "fp": "Q3"},
        ]}},
        "EarningsPerShareDiluted": {"units": {"USD/shares": [
            {"start": "2026-03-29", "end": "2026-06-27", "val": 2.02,
             "form": "10-Q", "filed": "2026-07-31", "fy": 2026, "fp": "Q3"},
        ]}},
    }
    monkeypatch.setattr(sf, "fetch_submissions", lambda cik, session, health: subs)
    monkeypatch.setattr(sf, "fetch_concept", lambda cik, tag, session, **kw: concepts.get(tag, {}))
    fund = sf.build_fundamentals("0000320193", "2026-07-30", None, {}, sf._now() + 600.0)
    assert fund["status"] == "ok"
    assert fund["form"] == "10-Q"

    flags = er.referee_flags(_aapl_cand(), fund)
    assert flags == [{"kind": "eps_disagreement", "sec": 2.02, "vendor": 1.91, "form": "10-Q"}]


def test_referee_flags_end_to_end_form_10k_via_real_build_fundamentals(monkeypatch):
    # Synthetic company whose anchor is a 10-K (fiscal Q4) that nonetheless
    # carries a discrete ~3-month duration EPS fact at period_end --
    # resolve_quarter's "direct" rule is duration-based, not form-based, so
    # this is a legitimate combination. Proves `form` follows the anchor's
    # real filing type rather than a hardcoded "10-Q" label -- exactly the
    # MSFT fiscal-Q4-is-a-10-K case a hardcoded label would get wrong.
    subs = {"filings": {"recent": {
        "form": ["10-K"],
        "filingDate": ["2026-09-30"],
        "reportDate": ["2026-09-27"],
        "accessionNumber": ["0000999999-26-000001"],
        "fy": [2026], "fp": ["FY"],
    }}}
    concepts = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"units": {"USD": [
            {"start": "2026-06-28", "end": "2026-09-27", "val": 109417000000,
             "form": "10-K", "filed": "2026-09-30", "fy": 2026, "fp": "FY"},
        ]}},
        "EarningsPerShareDiluted": {"units": {"USD/shares": [
            {"start": "2026-06-28", "end": "2026-09-27", "val": 2.02,
             "form": "10-K", "filed": "2026-09-30", "fy": 2026, "fp": "FY"},
        ]}},
    }
    monkeypatch.setattr(sf, "fetch_submissions", lambda cik, session, health: subs)
    monkeypatch.setattr(sf, "fetch_concept", lambda cik, tag, session, **kw: concepts.get(tag, {}))
    fund = sf.build_fundamentals("0000999999", "2026-09-28", None, {}, sf._now() + 600.0)
    assert fund["status"] == "ok"
    assert fund["form"] == "10-K"

    flags = er.referee_flags(_aapl_cand(), fund)
    assert flags == [{"kind": "eps_disagreement", "sec": 2.02, "vendor": 1.91, "form": "10-K"}]


def test_attach_fundamentals_wires_referee_flags_empty_on_agreement(monkeypatch):
    fund = _aapl_fund(eps_value=1.91)  # SEC agrees with vendor -- no flag
    monkeypatch.setattr(sf, "build_fundamentals", lambda *a, **k: fund)
    monkeypatch.setattr(er, "_CIK_CACHE", {"AAPL": "0000320193"})
    cand = _fund_cand("AAPL")
    cand["eps_act"] = 1.91
    cand["rev_act"] = 109417000000
    health = {}
    er._attach_fundamentals([cand], [cand], {}, None, health)
    assert cand["fundamentals_flags"] == []


def test_snapshot_keys_carry_the_forward_consensus_for_the_beat2_verdict():
    """Spec §6.4 wants the follow-up's guidance line to carry a DETERMINISTIC
    above/inline/below verdict. That verdict is the guide's midpoint against
    forward consensus -- but the snapshot carried `guidance_struct` without
    either consensus field, so at beat 2 the renderer had a range and nothing
    to judge it against, and the verdict half of the line silently never
    rendered. Candidates already carry both fields (see _CONTEXT_DEFAULTS)."""
    assert "fwd_eps_q" in er._SNAPSHOT_KEYS
    assert "fwd_eps_fy" in er._SNAPSHOT_KEYS


def test_forward_consensus_reaches_the_recorded_pool_snapshot(tmp_path):
    """_SNAPSHOT_KEYS listing the two consensus fields only matters if the
    VALUES survive the copy into the pool entry the filing block later recalls
    -- the same regression as above, one step further along: a snapshot that
    carries the keys with None in them renders exactly as badly as one missing
    them."""
    cand = {"ticker": "NFLX", "report_date": "2026-08-11", "source": "us",
            "name": "Netflix", "fwd_eps_q": 4.5, "fwd_eps_fy": 19.2}
    pep.record(str(tmp_path), [cand], {"NFLX"}, set(), "2026-08-12")
    snapshot = pep.read_pool(str(tmp_path))["NFLX:2026-08-11"]["snapshot"]
    assert snapshot["fwd_eps_q"] == 4.5
    assert snapshot["fwd_eps_fy"] == 19.2
