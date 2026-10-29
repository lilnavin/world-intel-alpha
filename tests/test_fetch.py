"""Unit + smoke tests for Module A (scripts/fetch_sources.py).

Unit tests never touch the network: recorded sample JSON fixtures are fed into
the pure normalization helpers, and resilience is proven by forcing failures.
One live smoke test hits a no-key source (Federal Register) and SKIPS offline.
"""

import json
import os
import sys

import pytest

# Make scripts/ importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from worldintel.core import fetch_sources as fs  # noqa: E402

_FIX = os.path.join(_HERE, "fixtures")


def load_fixture(name):
    with open(os.path.join(_FIX, name), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(autouse=True)
def _block_live_yfinance(monkeypatch):
    """yfinance is installed in the runtime, so ensure no unit test silently hits
    the live Yahoo network via fetch_markets. Tests that want yfinance data stub
    _yf_history themselves."""
    def _offline(*a, **k):
        raise ConnectionError("yfinance disabled in unit tests")
    monkeypatch.setattr(fs, "_yf_history", _offline)
    monkeypatch.setattr(fs, "_yf_earnings_date", _offline)
    monkeypatch.setattr(fs, "_yf_intraday_last_session", _offline)


# Top-level bundle keys downstream depends on.
BUNDLE_KEYS = {
    "date", "news", "coverage", "markets", "macro",
    "gov", "gdelt", "filings", "earnings_calendar", "source_health",
}
MARKET_GROUPS = {"indices", "fx", "rates", "commodities", "risk"}


# --------------------------------------------------------------------------- #
# Normalization: markets (Twelve Data)
# --------------------------------------------------------------------------- #
def test_normalize_td_quote_ok():
    q = fs._normalize_td_quote(load_fixture("twelvedata_quote.json"))
    assert set(q) == {"level", "change_pct", "asof"}
    assert q["level"] == pytest.approx(5500.12)
    assert q["change_pct"] == pytest.approx(0.367)
    assert q["asof"] == "2026-06-27"


def test_normalize_td_quote_error_returns_none():
    assert fs._normalize_td_quote(load_fixture("twelvedata_error.json")) is None
    assert fs._normalize_td_quote({}) is None
    assert fs._normalize_td_quote({"close": ""}) is None


def test_empty_markets_full_structure():
    m = fs._empty_markets()
    assert set(m) == MARKET_GROUPS
    for group in MARKET_GROUPS:
        for key, cell in m[group].items():
            assert set(cell) == {
                "level", "change_pct", "change_abs", "asof", "asof_prev",
            }
            assert cell["level"] is None
            assert cell["change_abs"] is None
            assert cell["asof_prev"] is None


# --------------------------------------------------------------------------- #
# Normalization: FRED, GDELT, gov
# --------------------------------------------------------------------------- #
def test_normalize_fred_ok():
    obs = fs._normalize_fred(load_fixture("fred.json"), "Federal funds effective rate")
    assert set(obs) == {"label", "value", "asof"}
    assert obs["value"] == pytest.approx(4.33)
    assert obs["asof"] == "2026-06-27"


def test_normalize_fred_skips_missing_value():
    assert fs._normalize_fred({"observations": [{"date": "x", "value": "."}]}, "l") is None
    assert fs._normalize_fred({}, "l") is None


def test_normalize_gdelt_tone_and_volume():
    tone = fs._normalize_gdelt_tone(load_fixture("gdelt_timelinetone.json"))
    assert tone == pytest.approx(-1.0)  # mean of -1.5, -0.5, -1.0
    vol = fs._normalize_gdelt_volume(load_fixture("gdelt_artlist.json"))
    assert vol == 3
    assert fs._normalize_gdelt_tone({}) is None
    assert fs._normalize_gdelt_volume({}) == 0


def test_normalize_gov_shape():
    items = fs._normalize_gov(load_fixture("federal_register.json"))
    assert len(items) == 1
    doc = items[0]
    assert set(doc) == {"title", "url", "agency", "date"}
    assert doc["agency"] == "Energy Department"
    assert doc["date"] == "2026-06-28"


# --------------------------------------------------------------------------- #
# Coverage (signal-vs-noise) builder
# --------------------------------------------------------------------------- #
def test_build_coverage_counts_distinct_outlets():
    news = [
        {"title": "Bank of Japan holds interest rates steady", "source": "reuters"},
        {"title": "Bank of Japan holds interest rates steady today", "source": "guardian"},
        {"title": "Completely unrelated weather forecast story", "source": "bbc"},
    ]
    gdelt = {"themes": [{"theme": "monetary policy", "volume": 10, "tone": -0.8}]}
    coverage = fs._build_coverage(news, gdelt)
    assert all(set(c) == {"story", "count", "tone"} for c in coverage)
    top = coverage[0]
    assert top["count"] == 2  # two outlets carried the BOJ story
    # tone matched from a GDELT theme via the "interest"/"rates" overlap... or None
    assert top["tone"] in (-0.8, None)


CHIBA_HEADLINES = [
    ("'Unprecedented' rain in Japan kills eight people", "bbc-world"),
    ("‘Unprecedented’ rain kills five in Japan as thousands stranded at airport",
     "guardian-world"),
    ("Japan: Record rains kill eight, strand thousands at Narita airport",
     "france24"),
    ("Authorities urge continued caution after heavy rain in Chiba leaves eight "
     "dead", "japantimes"),
    ("Rain, Floods in Japan’s Chiba Prefecture Leave 6 Dead", "japannews-yomiuri"),
    ("Torrential rain in Japan's Chiba Pref. prompts warnings over cars trapped "
     "on flooded roads", "mainichi-en"),
    ("Mainichi reporter stranded 7 hours on flooded roads after torrential rain "
     "hit Chiba Pref.", "mainichi-en"),
]


def _coverage_by_title(news):
    return {row["story"]: row["count"] for row in fs._build_coverage(news, {})}


def test_build_coverage_clusters_one_story_across_differently_worded_headlines():
    """2026-08-15 regression. The old key was a title's three longest tokens, so
    these seven Chiba-flood headlines produced seven distinct keys and every one
    of them scored corroboration=1. Measured across the whole 1,604-item pool,
    94% of items counted 1 — the signal was inert."""
    news = [{"title": t, "source": s} for t, s in CHIBA_HEADLINES]
    counts = _coverage_by_title(news)
    assert counts["Authorities urge continued caution after heavy rain in Chiba "
                  "leaves eight dead"] >= 3


def test_build_coverage_counts_outlets_not_articles():
    """The Mainichi filed twice. One outlet is one outlet: corroboration measures
    cross-outlet pickup, which is why _dedupe's separate 0.8 threshold is left
    alone — those articles are genuinely distinct."""
    news = [{"title": t, "source": s} for t, s in CHIBA_HEADLINES[5:]]
    counts = _coverage_by_title(news)
    assert set(counts.values()) == {1}


def test_build_coverage_needs_more_than_one_shared_word():
    """Lowering the old Jaccard fallback to the measured best cross-outlet pair
    (0.27) would have merged the pool wholesale. One shared word is a topic, not
    a story."""
    news = [
        {"title": "Ukraine war: Grain attacks raise food price fears",
         "source": "france24"},
        {"title": "Ukraine's war-hit steel industry faces new obstacle: EU rules",
         "source": "reuters-gnews"},
    ]
    assert set(_coverage_by_title(news).values()) == {1}


def test_build_coverage_does_not_merge_a_beat_into_one_story():
    """The shared words must include a rare one. On 2026-08-15 "white" and
    "house" appeared in 24 and 30 of 1,604 headlines, and matching on that pair
    alone welded the ballroom lawsuit, the press secretary's resignation and a
    staff shuffle into a single 22-item "story" carried by 14 outlets."""
    news = [{"title": t, "source": s} for t, s in (
        ("Trump's White House remodel tab keeps growing", "axios"),
        ("White House press secretary Karoline Leavitt to step down", "nbc-politics"),
        ("Karoline Leavitt exiting as White House press secretary", "cbs-politics"),
        ("Trump announces staff secretary Will Scharf will be his new White "
         "House counsel", "abc-politics"),
        ("White House officials tamped down efforts to oust Max Miller in final "
         "days", "politico"),
        ("China rattled by Trump White House chaos weeks ahead of Xi visit", "scmp"),
        ("Trump's new White House counsel could soon face a subpoena fight", "axios"),
        ("White House revives unprecedented effort to remove Fed governor Lisa "
         "Cook", "axios"),
        ("A letter to investors from the White House Opportunities Fund",
         "economist-finance"),
        ("Who could replace White House Press Secretary Karoline Leavitt?",
         "bbc-world"),
        ("Trump asks Supreme Court to let White House ballroom construction "
         "continue", "dw-world"),
        ("Trump asks Supreme Court to intervene in White House ballroom legal "
         "fight", "cbs-politics"),
        ("Trump asks US Supreme Court to allow $400m ballroom project to "
         "proceed", "aljazeera"),
        ("Zambia suspends vote count nationwide amid reports of violence",
         "france24"),
        ("Strong 7.7-magnitude earthquake strikes eastern Indonesia, killing "
         "two", "guardian-world"),
        ("Royal Parks gardeners to strike over pay", "bbc-business"),
        ("US inflation eases as food and fuel costs cool", "bbc-business"),
        ("ECB to start implementing enhanced repo facility", "ecb-press"),
        ("Malaysia Q2 GDP growth accelerates to 6.0%", "nikkei-asia"),
        ("Japan defense minister visits war-linked Yasukuni Shrine", "japantimes"),
    )]
    counts = _coverage_by_title(news)
    # The three ballroom filings are one story and do cluster, on "ballroom".
    assert counts["Trump asks Supreme Court to let White House ballroom "
                  "construction continue"] == 3
    # The press-secretary departure is a different one, and clusters separately
    # on "leavitt" — three outlets, not welded to the ballroom filings.
    assert counts["White House press secretary Karoline Leavitt to step down"] == 3
    assert counts["White House revives unprecedented effort to remove Fed "
                  "governor Lisa Cook"] == 1


def test_build_coverage_is_deterministic():
    news = [{"title": t, "source": s} for t, s in CHIBA_HEADLINES]
    assert fs._build_coverage(news, {}) == fs._build_coverage(news, {})


# --------------------------------------------------------------------------- #
# Resilience — the absolute rule
# --------------------------------------------------------------------------- #
def test_every_source_failing_still_yields_valid_bundle(monkeypatch, tmp_path):
    """Force ALL network calls to raise; bundle must still be well-formed."""
    def boom(*a, **k):
        raise ConnectionError("network down")

    monkeypatch.setattr(fs.requests.Session, "get", boom)
    monkeypatch.setattr(fs.feeds, "public_get", boom)
    config = {"twelvedata_key": "x", "fred_key": "x"}
    bundle = fs.fetch_all(config, str(tmp_path), "2026-06-28")

    assert set(bundle) == BUNDLE_KEYS
    assert bundle["date"] == "2026-06-28"
    assert bundle["news"] == []
    assert set(bundle["markets"]) == MARKET_GROUPS
    health = bundle["source_health"]
    for src in ("twelvedata", "fred", "gdelt", "federal_register"):
        assert health[src]["ok"] is False
        assert isinstance(health[src]["note"], str) and health[src]["note"]


def test_missing_keys_skip_with_no_api_key(tmp_path, monkeypatch):
    """With an empty config, keyed sources skip; no exception escapes."""
    # Block the no-key sources' network so this stays offline-safe.
    monkeypatch.setattr(fs.requests.Session, "get",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("offline")))
    monkeypatch.setattr(fs.feeds, "public_get", fs.requests.Session.get)
    bundle = fs.fetch_all({}, str(tmp_path), "2026-06-28")
    for src in ("twelvedata", "fred"):
        assert bundle["source_health"][src]["note"] == "no api key"
        assert bundle["source_health"][src]["ok"] is False


def test_fetch_writes_raw_dump(tmp_path):
    """fetch_gov dumps raw JSON when given a fake session returning a fixture."""
    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return load_fixture("federal_register.json")

    class FakeSession:
        def get(self, *a, **k):
            return FakeResp()

    health = {}
    items = fs.fetch_gov({}, FakeSession(), str(tmp_path), health)
    assert items and items[0]["agency"] == "Energy Department"
    assert health["federal_register"]["ok"] is True
    assert os.path.exists(os.path.join(str(tmp_path), "federal_register.json"))


# --------------------------------------------------------------------------- #
# Live smoke test (no-key source) — skipped when offline
# --------------------------------------------------------------------------- #
def test_live_federal_register_smoke(tmp_path):
    """Hit the real Federal Register API; skip cleanly if offline/unreachable."""
    import requests as _rq

    session = _rq.Session()
    session.headers.update({"User-Agent": fs._plain_ua()})
    try:
        session.get(fs.FEDREG_URL, params={"per_page": 1, "order": "newest"},
                    timeout=10)
    except Exception as exc:  # offline / DNS / timeout
        pytest.skip(f"network unavailable: {exc}")

    health = {}
    items = fs.fetch_gov({}, session, str(tmp_path), health)
    assert "federal_register" in health
    if health["federal_register"]["ok"]:
        assert items
        assert set(items[0]) == {"title", "url", "agency", "date"}


# --------------------------------------------------------------------------- #
# EDGAR (SEC filings) — pure normalization
# --------------------------------------------------------------------------- #
def test_normalize_edgar_shape_and_urls():
    items = fs._normalize_edgar(load_fixture("edgar.json"))
    assert len(items) == 3
    assert all(set(it) == {"company", "form", "filed", "url", "cik"} for it in items)
    # Sorted most-recent first (the 2026-07-01 8-K leads).
    assert [it["filed"] for it in items] == ["2026-07-01", "2026-06-30", "2026-06-28"]
    top = items[0]
    assert top["form"] == "8-K"
    # Ticker + CIK annotations stripped from the display name.
    assert top["company"] == "ATLANTIC INTERNATIONAL CORP."
    assert top["cik"] == "0001605888"
    # Absolute sec.gov Archives URL: CIK (no leading zeros)/accession (no dashes)/doc.
    assert top["url"] == (
        "https://www.sec.gov/Archives/edgar/data/"
        "1605888/000160588826000037/altn-20260629.htm"
    )
    assert all(it["url"].startswith("https://www.sec.gov/") for it in items)


def test_normalize_edgar_caps_at_15():
    hits = [
        {
            "_id": f"000000000{i:01d}-26-000001:doc{i}.htm",
            "_source": {
                "ciks": [f"000000000{i}"],
                "display_names": [f"Company {i}  (CIK 000000000{i})"],
                "form": "8-K",
                "file_date": f"2026-06-{(i % 28) + 1:02d}",
                "adsh": f"000000000{i}-26-000001",
            },
        }
        for i in range(30)
    ]
    items = fs._normalize_edgar({"hits": {"hits": hits}})
    assert len(items) == 15


def test_normalize_edgar_handles_empty_or_missing():
    assert fs._normalize_edgar({}) == []
    assert fs._normalize_edgar({"hits": {"hits": []}}) == []
    assert fs._normalize_edgar(None) == []


def test_fetch_edgar_uses_descriptive_user_agent(tmp_path):
    """fetch_edgar overrides the session UA per-request (SEC 403s otherwise)."""
    seen = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return load_fixture("edgar.json")

    class FakeSession:
        def get(self, url, params=None, headers=None, timeout=None):
            seen["headers"] = headers or {}
            return FakeResp()

    health = {}
    items = fs.fetch_edgar({}, FakeSession(), str(tmp_path), health)
    assert seen["headers"].get("User-Agent") == fs._sec_ua()
    assert health["edgar"]["ok"] is True
    assert items and items[0]["url"].startswith("https://www.sec.gov/")
    assert os.path.exists(os.path.join(str(tmp_path), "edgar.json"))


# --------------------------------------------------------------------------- #
# Earnings (yfinance) — forward-window filter, sorting, resilience
# --------------------------------------------------------------------------- #
def test_fetch_earnings_window_filter_and_sort(tmp_path, monkeypatch):
    """Only dates in [today, today+10d] survive; out-of-window and None dropped."""
    monkeypatch.setattr(fs, "earnings_watchlist", lambda: [
        ("AAPL", "Apple"),      # in window (+3d)
        ("MSFT", "Microsoft"),  # in window, boundary (+10d)
        ("NVDA", "Nvidia"),     # out: past
        ("TSLA", "Tesla"),      # out: beyond horizon (+11d)
        ("AMZN", "Amazon"),     # None -> dropped
        ("META", "Meta"),       # in window, same day (+0d)
    ])
    dates = {
        "AAPL": "2026-07-05",
        "MSFT": "2026-07-12",
        "NVDA": "2026-06-30",
        "TSLA": "2026-07-13",
        "AMZN": None,
        "META": "2026-07-02",
    }
    monkeypatch.setattr(fs, "_yf_earnings_date", lambda t: dates[t])

    rows = fs.fetch_earnings({}, None, str(tmp_path), {}, today="2026-07-02")
    assert [r["ticker"] for r in rows] == ["META", "AAPL", "MSFT"]  # sorted by date
    assert all(set(r) == {"ticker", "name", "date"} for r in rows)
    assert rows[0] == {"ticker": "META", "name": "Meta", "date": "2026-07-02"}


def test_fetch_earnings_missing_yfinance_no_raise(tmp_path, monkeypatch):
    """yfinance absent (helper raises ModuleNotFoundError) -> ok False, no raise."""
    def _no_yf(_t):
        raise ModuleNotFoundError("No module named 'yfinance'")
    monkeypatch.setattr(fs, "_yf_earnings_date", _no_yf)

    health = {}
    rows = fs.fetch_earnings({}, None, str(tmp_path), health, today="2026-07-02")
    assert rows == []
    assert health["earnings"] == {"ok": False, "note": "yfinance not installed"}


# --------------------------------------------------------------------------- #
# New sources wired into fetch_all resiliently
# --------------------------------------------------------------------------- #
def test_new_sources_present_and_isolated_on_failure(monkeypatch, tmp_path):
    """EDGAR network raising + yfinance offline still yields a valid bundle with
    filings/earnings_calendar present and both sources ok False."""
    monkeypatch.setattr(fs.requests.Session, "get",
                        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("offline")))
    monkeypatch.setattr(fs.feeds, "public_get", fs.requests.Session.get)
    # _yf_earnings_date is already stubbed offline by the autouse fixture.
    bundle = fs.fetch_all({}, str(tmp_path), "2026-07-02")

    assert set(bundle) == BUNDLE_KEYS
    assert bundle["filings"] == []
    assert bundle["earnings_calendar"] == []
    assert bundle["source_health"]["edgar"]["ok"] is False
    assert bundle["source_health"]["earnings"]["ok"] is False


# --------------------------------------------------------------------------- #
# Markets: yfinance quote-from-history (change_abs / asof_prev, robust to gaps)
# --------------------------------------------------------------------------- #
class _FakeSeries:
    """Minimal stand-in for the pandas Series returned by hist['Close'] — just the
    bits _yf_quote_from_history uses: .dropna(), len(), .iloc[-1/-2], .index[-1/-2]."""
    class _Idx:
        def __init__(self, d):
            self._d = d

        def date(self):
            return self._d

    def __init__(self, pairs):
        # pairs: list of (date_string, close_or_None)
        self._pairs = pairs

    def dropna(self):
        return _FakeSeries([(d, v) for d, v in self._pairs if v is not None])

    def __len__(self):
        return len(self._pairs)

    @property
    def iloc(self):
        return [v for _d, v in self._pairs]

    @property
    def index(self):
        return [self._Idx(d) for d, _v in self._pairs]

    def items(self):
        return [(self._Idx(d), v) for d, v in self._pairs]


class _FakeHist:
    def __init__(self, pairs):
        self._pairs = pairs

    def __getitem__(self, col):
        assert col == "Close"
        return _FakeSeries(self._pairs)


def test_yf_quote_from_history_change_abs_and_prev():
    # A rate: yield 4.372 -> 4.485. change_abs 0.113 (=> 11.3 bp downstream).
    q = fs._yf_quote_from_history(_FakeHist([
        ("2026-07-01", 4.372),
        ("2026-07-02", 4.485),
    ]))
    assert set(q) == {"level", "change_pct", "change_abs", "asof", "asof_prev",
                      "ytd_pct", "ytd_abs", "d90_pct", "d90_abs",
                      "off_high_pct", "pctl_1y"}
    assert q["level"] == pytest.approx(4.485)
    assert q["change_abs"] == pytest.approx(0.113)
    assert q["change_pct"] == pytest.approx(round((4.485 / 4.372 - 1) * 100, 3))
    assert q["asof"] == "2026-07-02"
    assert q["asof_prev"] == "2026-07-01"


def test_yf_quote_from_history_uses_two_most_recent_available_closes():
    # A NaN gap (missing/partial session) must be dropped, then the two most-recent
    # AVAILABLE closes compared — not a stale row. asof_prev exposes the real prior date.
    q = fs._yf_quote_from_history(_FakeHist([
        ("2026-06-30", 100.0),
        ("2026-07-01", None),   # holiday / partial bar -> dropped
        ("2026-07-02", 110.0),
    ]))
    assert q["level"] == pytest.approx(110.0)
    assert q["change_abs"] == pytest.approx(10.0)
    assert q["asof"] == "2026-07-02"
    assert q["asof_prev"] == "2026-06-30"


def test_yf_quote_from_history_single_close_has_none_changes():
    q = fs._yf_quote_from_history(_FakeHist([("2026-07-02", 42.0)]))
    assert q["level"] == pytest.approx(42.0)
    assert q["change_pct"] is None
    assert q["change_abs"] is None
    assert q["asof_prev"] is None
    assert q["asof"] == "2026-07-02"


def test_yf_quote_from_history_all_nan_returns_none():
    assert fs._yf_quote_from_history(_FakeHist([("d", None), ("e", None)])) is None


# --------------------------------------------------------------------------- #
# News: source-tier tagging
# --------------------------------------------------------------------------- #
def test_source_tier_mapping():
    assert fs._source_tier("reuters", "https://www.reuters.com/x") == 1
    assert fs._source_tier("The Guardian", "https://www.theguardian.com/y") == 1
    assert fs._source_tier("Nikkei", "https://www.nikkei.com/z") == 1
    assert fs._source_tier("Some Blog", "https://foo.blogger.com/p") == 3
    assert fs._source_tier("random", "https://x.blogspot.com/p") == 3
    assert fs._source_tier("Indian Share Tips", "https://sharetips.example/p") == 3
    assert fs._source_tier("Springfield Local Herald", "https://sfherald.example/p") == 2


# --------------------------------------------------------------------------- #
# _horizon_fields — long-horizon stats from (date, close) pairs
# --------------------------------------------------------------------------- #
def test_horizon_fields_full_year():
    import datetime as dt
    pairs = [
        (dt.date(2025, 8, 1), 90.0),
        (dt.date(2025, 12, 30), 100.0),   # last close of the prior year -> YTD ref
        (dt.date(2026, 4, 10), 105.0),    # last close <= asof-90d -> 90d ref
        (dt.date(2026, 6, 1), 120.0),     # 52w high
        (dt.date(2026, 7, 10), 110.0),    # asof
    ]
    h = fs._horizon_fields(pairs)
    assert h["ytd_pct"] == 10.0
    assert h["ytd_abs"] == 10.0
    assert h["d90_pct"] == 4.8            # 110/105 - 1 = 4.76% -> 1dp
    assert h["d90_abs"] == 5.0
    assert h["off_high_pct"] == 8.3       # (1 - 110/120) = 8.33% below the high
    assert h["pctl_1y"] == 80             # 4 of 5 closes <= 110


def test_horizon_fields_insufficient_history():
    import datetime as dt
    pairs = [(dt.date(2026, 6, 20), 100.0), (dt.date(2026, 7, 10), 102.0)]
    h = fs._horizon_fields(pairs)
    assert h["ytd_pct"] is None and h["ytd_abs"] is None
    assert h["d90_pct"] is None and h["d90_abs"] is None
    assert h["off_high_pct"] == 0.0       # latest close IS the window high
    assert h["pctl_1y"] == 100
    assert fs._horizon_fields([]) == {
        "ytd_pct": None, "ytd_abs": None, "d90_pct": None, "d90_abs": None,
        "off_high_pct": None, "pctl_1y": None,
    }


# --------------------------------------------------------------------------- #
# JGB — MOF daily yield-curve CSV parsing + quote assembly
# --------------------------------------------------------------------------- #
_JGB_CURRENT = """Interest Rate (July 2026),,,,,,,,,,,,,,,(Unit : %)
Date,1Y,2Y,3Y,4Y,5Y,6Y,7Y,8Y,9Y,10Y,15Y,20Y,25Y,30Y,40Y
2026/7/1,1.164,1.4,1.554,1.777,1.959,2.099,2.255,2.421,2.572,2.711,3.286,3.632,3.889,3.883,3.782
2026/7/2,1.16,1.39,1.55,1.78,1.96,2.1,2.26,2.42,2.57,2.75,3.29,3.64,3.89,3.88,3.78
,,,,,,,,,,,,,,,
"  If you cannot download the latest csv data please clear the cache.",,,,,,,,,,,,,,,
"""

_JGB_ALL = """基準日,1年,2年,3,4,5,6,7,8,9,10,15,20,25,30,40
S49.9.24,10.327,9.362,8.83,8.515,8.348,8.29,8.24,8.121,8.127,-,-,-,-,-,-
R7.12.30,1.05,1.2,1.3,1.4,1.5,1.6,1.7,1.8,1.9,2.0,2.5,2.9,3.1,3.2,3.3
R8.4.1,1.1,1.3,1.4,1.5,1.6,1.7,1.8,1.9,2.0,2.4,2.9,3.3,3.5,3.6,3.6
R8.6.30,1.165,1.382,1.531,1.755,1.937,2.075,2.231,2.398,2.553,2.69,3.271,3.625,3.883,3.873,3.792
"""


def test_parse_jgb_slash_era_dates_and_missing_values():
    rows = dict(fs._parse_jgb(_JGB_CURRENT))
    assert rows["2026-07-01"] == {"jp10y": 2.711, "jp30y": 3.883}
    assert rows["2026-07-02"] == {"jp10y": 2.75, "jp30y": 3.88}

    rows = dict(fs._parse_jgb(_JGB_ALL))
    assert rows["2026-06-30"] == {"jp10y": 2.69, "jp30y": 3.873}  # R8.6.30
    assert rows["2025-12-30"] == {"jp10y": 2.0, "jp30y": 3.2}     # R7.12.30
    assert "1974-09-24" not in rows   # S49 row: '-' for both tenors -> dropped


class _FakeResp:
    def __init__(self, content):
        self.content = content
        self.status_code = 200

    def raise_for_status(self):
        pass


class _FakeSession:
    def get(self, url, **kw):
        text = _JGB_CURRENT if "english" in url else _JGB_ALL
        return _FakeResp(text.encode("utf-8"))


def test_fetch_jgb_quotes_merge_current_and_archive(tmp_path):
    health = {}
    quotes = fs.fetch_jgb({}, _FakeSession(), str(tmp_path), health)
    assert health["jgb"]["ok"] is True

    q = quotes["jp10y"]
    assert q["level"] == 2.75                 # newest row (2026-07-02, current file)
    assert q["asof"] == "2026-07-02"
    assert q["asof_prev"] == "2026-07-01"
    assert q["change_abs"] == round(2.75 - 2.711, 4)
    assert q["ytd_abs"] == 0.75               # vs R7.12.30 close of 2.0
    assert q["d90_abs"] == round(2.75 - 2.4, 4)  # vs R8.4.1 (last close <= asof-90d)

    q30 = quotes["jp30y"]
    assert q30["level"] == 3.88
    assert q30["change_abs"] == round(3.88 - 3.883, 4)
    assert q30["ytd_abs"] == round(3.88 - 3.2, 4)


def test_fetch_jgb_degrades_on_network_failure(tmp_path):
    class _Boom:
        def get(self, url, **kw):
            raise ConnectionError("offline")
    health = {}
    quotes = fs.fetch_jgb({}, _Boom(), str(tmp_path), health)
    assert quotes == {}
    assert health["jgb"]["ok"] is False


# --------------------------------------------------------------------------- #
# Intraday freshness patch — non-US index daily bars lag at 07:00 JST
# --------------------------------------------------------------------------- #
def test_yf_quote_extra_bar_appends_when_newer():
    import datetime as dt
    hist = _FakeHist([("2026-07-09", 100.0), ("2026-07-10", 102.0)])
    q = fs._yf_quote_from_history(hist, extra_bar=(dt.date(2026, 7, 13), 105.0))
    assert q["level"] == 105.0
    assert q["asof"] == "2026-07-13"
    assert q["asof_prev"] == "2026-07-10"
    assert q["change_abs"] == pytest.approx(3.0)
    assert q["change_pct"] == pytest.approx(round((105.0 / 102.0 - 1) * 100, 3))


def test_yf_quote_extra_bar_ignored_when_not_newer():
    import datetime as dt
    hist = _FakeHist([("2026-07-09", 100.0), ("2026-07-10", 102.0)])
    # same date -> the consolidated daily bar wins; older date -> ignored
    for bar in ((dt.date(2026, 7, 10), 999.0), (dt.date(2026, 7, 9), 999.0), None):
        q = fs._yf_quote_from_history(hist, extra_bar=bar)
        assert q["level"] == 102.0
        assert q["asof"] == "2026-07-10"


def test_fetch_markets_freshens_stale_indices_from_intraday(monkeypatch, tmp_path):
    import datetime as dt
    intraday_calls = []

    monkeypatch.setattr(fs, "_yf_history", lambda t: _FakeHist(
        [("2026-07-09", 100.0), ("2026-07-10", 102.0)]))

    def fake_intraday(ticker):
        intraday_calls.append(ticker)
        return (dt.date(2026, 7, 13), 105.0)
    monkeypatch.setattr(fs, "_yf_intraday_last_session", fake_intraday)

    health = {}
    markets = fs.fetch_markets({}, None, str(tmp_path), health)

    # indices got Monday's session appended from the intraday feed...
    sp = markets["indices"]["sp500"]
    assert sp["asof"] == "2026-07-13" and sp["level"] == 105.0
    # ...but non-index groups keep the plain daily history
    assert markets["fx"]["usdjpy"]["asof"] == "2026-07-10"
    assert markets["rates"]["us10y"]["asof"] == "2026-07-10"

    # intraday endpoint consulted only for index tickers
    index_syms = {yf_sym for _k, _td, yf_sym in fs.market_spec()["indices"]}
    assert set(intraday_calls) == index_syms
    assert "intraday" in health["yfinance"]["note"]


def test_fetch_markets_intraday_failure_keeps_daily_quote(monkeypatch, tmp_path):
    monkeypatch.setattr(fs, "_yf_history", lambda t: _FakeHist(
        [("2026-07-09", 100.0), ("2026-07-10", 102.0)]))

    def boom(ticker):
        raise ConnectionError("intraday down")
    monkeypatch.setattr(fs, "_yf_intraday_last_session", boom)

    health = {}
    markets = fs.fetch_markets({}, None, str(tmp_path), health)
    assert markets["indices"]["sp500"]["asof"] == "2026-07-10"  # unfreshened, intact
    assert health["yfinance"]["ok"] is True


# --------------------------------------------------------------------------- #
# News: sourced from the feed layer (Task 7)
# --------------------------------------------------------------------------- #
def test_fetch_all_sources_news_from_feeds(monkeypatch):
    captured = {}

    def _fake_feeds(config, raw_dir, health, session=None, registry=None):
        captured["called"] = True
        health["feed:fake"] = {"ok": True, "note": ""}
        return [{"title": "Feed story", "source": "bbc-world",
                 "url": "https://bbc.co.uk/1", "published": "",
                 "summary": "", "region": "world", "category": "general",
                 "tier": 1, "raw_body": "", "desk": "world"}]

    monkeypatch.setattr(fs.feeds, "fetch_feeds", _fake_feeds)
    # Bind the return value per-iteration: a bare `lambda: ... name ...` would
    # late-bind and give every stub the last loop value.
    for name, empty in (("fetch_markets", {}), ("fetch_fred", {}),
                        ("fetch_jgb", {}), ("fetch_gdelt", {"themes": []}),
                        ("fetch_gov", []), ("fetch_edgar", []),
                        ("fetch_earnings", [])):
        monkeypatch.setattr(fs, name,
                            lambda *a, _v=empty, **k: _v)

    bundle = fs.fetch_all({}, "", "2026-08-02")
    assert captured["called"] is True
    assert bundle["news"][0]["desk"] == "world"
    assert bundle["news"][0]["tier"] == 1   # registry tier survives


def test_retired_news_fetchers_are_gone():
    for name in ("fetch_newsdata", "fetch_guardian", "fetch_marketaux",
                 "_normalize_newsdata", "_normalize_guardian",
                 "_normalize_marketaux", "_fetch_newsdata_slice"):
        assert not hasattr(fs, name), f"{name} should be retired"


def test_fetch_all_assigns_tier_to_untiered_feed_items(monkeypatch):
    """Items that arrive from feeds with NO tier get one computed via
    _source_tier; this is the fallback branch of the fetch_all tier loop."""
    def _fake_feeds(config, raw_dir, health, session=None, registry=None):
        return [
            {"title": "A", "source": "reuters", "url": "https://www.reuters.com/x",
             "published": "", "summary": "", "region": "world",
             "category": "general", "raw_body": "", "desk": "world"},
            {"title": "B", "source": "Springfield Local Herald",
             "url": "https://sfherald.example/p", "published": "", "summary": "",
             "region": "world", "category": "general", "raw_body": "", "desk": "world"},
            {"title": "C", "source": "Some Blog", "url": "https://foo.blogspot.com/p",
             "published": "", "summary": "", "region": "world",
             "category": "general", "raw_body": "", "desk": "world"},
        ]

    monkeypatch.setattr(fs.feeds, "fetch_feeds", _fake_feeds)
    # Bind the return value per-iteration: a bare `lambda: ... name ...` would
    # late-bind and give every stub the last loop value.
    for name, empty in (("fetch_markets", {}), ("fetch_fred", {}),
                        ("fetch_jgb", {}), ("fetch_gdelt", {"themes": []}),
                        ("fetch_gov", []), ("fetch_edgar", []),
                        ("fetch_earnings", [])):
        monkeypatch.setattr(fs, name,
                            lambda *a, _v=empty, **k: _v)

    bundle = fs.fetch_all({}, "", "2026-08-02")
    assert all(isinstance(it["tier"], int) for it in bundle["news"])
    tiers = {it["source"]: it["tier"] for it in bundle["news"]}
    assert tiers["reuters"] == 1
    assert tiers["Springfield Local Herald"] == 2
    assert tiers["Some Blog"] == 3


# --------------------------------------------------------------------------- #
# story_matcher — the pairwise form of the clustering rule, for callers that
# need "are these two the same story?" rather than "how many outlets ran it".
# Shares the rule (and its constants) with _story_outlet_counts; only the
# traversal differs, because that one runs over the whole 1,600-item pool and
# this one over a handful of already-ranked candidates.
# --------------------------------------------------------------------------- #
def _headline(title, source):
    return {"title": title, "source": source}


_CHIBA_HEADLINES = [
    _headline("Authorities urge continued caution after heavy rain in Chiba "
              "leaves eight dead", "japantimes"),
    _headline("Rain, Floods in Japan's Chiba Prefecture Leave 6 Dead",
              "japannews-yomiuri"),
    _headline("Torrential rain in Japan's Chiba Pref. prompts warnings over "
              "cars trapped on flooded roads", "mainichi-en"),
    _headline("Flooding in Chiba Pref. leaves at least 8 dead, paralyzes "
              "transportation", "kyodo-english"),
]

# The first three are the trio that got welded into one 22-item blob. The rest
# exist so "white" and "house" are as ORDINARY here as they were in the real
# pool (df 24 and 30 of 1,604). With only three, those tokens look rare, the
# anchor rule waves them through, and the fixture proves the opposite of what
# it claims.
_WHITE_HOUSE_HEADLINES = [
    _headline("Trump asks Supreme Court to let White House resume ballroom "
              "construction during appeal", "pbs-politics"),
    _headline("White House press secretary Leavitt resigns after 14 months",
              "axios"),
    _headline("White House announces senior staff shuffle ahead of midterms",
              "politico"),
    _headline("White House defends tariff exemptions granted to two donors",
              "propublica"),
    _headline("Protesters gather outside the White House over Hormuz strikes",
              "npr-world"),
    _headline("White House budget office freezes disaster relief disbursements",
              "rollcall"),
    _headline("Ambassador summoned to the White House over export controls",
              "cbs-politics"),
    _headline("White House physician releases annual health summary",
              "nbc-politics"),
]


def test_story_matcher_pairs_headlines_about_one_event():
    news = _CHIBA_HEADLINES + _WHITE_HOUSE_HEADLINES
    same = fs.story_matcher(news)
    assert same(news[0], news[1])
    assert same(news[0], news[3])
    assert same(news[1], news[2])


def test_story_matcher_does_not_weld_distinct_white_house_stories():
    """`white` + `house` alone merged the ballroom lawsuit, the press
    secretary's resignation and a staff shuffle into one 22-item blob across 14
    outlets. The rare-anchor rule is what stops it, and it must keep stopping
    it through this second entry point too."""
    news = _CHIBA_HEADLINES + _WHITE_HOUSE_HEADLINES
    same = fs.story_matcher(news)
    ballroom, leavitt, shuffle = news[4], news[5], news[6]
    assert not same(ballroom, leavitt)
    assert not same(ballroom, shuffle)


def test_story_matcher_never_raises_and_is_symmetric():
    news = _CHIBA_HEADLINES + _WHITE_HOUSE_HEADLINES
    same = fs.story_matcher(news)
    assert same(news[0], news[1]) == same(news[1], news[0])
    assert same(news[0], news[0]) is False        # an item is not its own pair
    for bad in (None, {}, {"title": None}, {"title": 7}, "nonsense"):
        assert same(bad, news[0]) is False
        assert same(news[0], bad) is False
    assert fs.story_matcher(None)({}, {}) is False


def _padded_pool(n=500):
    """The rule is calibrated against the POOL: its rare-anchor threshold is 1%
    of it, floor 3. In a seven-item fixture a four-outlet story's own signature
    token ("chiba", df 4) already looks common, so the fixture has to be as big
    as the pool this runs against. The padding shares only one high-frequency
    token, so it clusters with nothing."""
    pad = [{"title": f"Vurnex{i}pol Grathe{i}dor Silban{i}ket filing",
            "source": f"filler-{i}"} for i in range(n)]
    return _CHIBA_HEADLINES + _WHITE_HOUSE_HEADLINES + pad


def test_loose_matcher_still_refuses_to_weld_the_white_house_stories():
    """`select_news` builds its variety cap with min_ratio=0, because four
    outlets on one flood share only "chiba" and "rain" and would otherwise form
    two disjoint pairs instead of one story. Relaxing that ratio must NOT cost
    the separation the rare-anchor rule buys — a shared token common across the
    pool still cannot bind two stories together."""
    news = _padded_pool()
    loose = fs.story_matcher(news, min_ratio=0)
    ballroom, leavitt, shuffle = news[4], news[5], news[6]
    assert not loose(ballroom, leavitt)
    assert not loose(ballroom, shuffle)
    # ...while the flood headlines the strict ratio split now hold together.
    assert loose(news[0], news[2])
    assert loose(news[0], news[3])
