"""Tests for the filing index layer (scripts/filing_index.py): the periodic-form
filter over EDGAR's daily index, and the rolling window cache `load_window`
maintains for the reconcile join.

Fully offline: every test feeds a recorded daily-index fixture through a fake
`session`, so no network I/O and no real clock waits. `daily_index.idx` is the
real recorded index reused from the ownership tests (it carries no 10-Q/10-K,
which is exactly the "fetched, nothing periodic" case); `daily_index_periodic.idx`
adds the periodic rows that one lacks, in the same column layout.
"""

import os
import sys

import pytest
import requests

# Make scripts/ importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from worldintel.core import filing_index as fi  # noqa: E402

_FIX = os.path.join(_HERE, "fixtures")

# A Friday, so the window ending here (2026-07-23 .. 2026-08-21) holds eight
# weekend days and twenty-two weekdays.
TODAY = "2026-08-21"
_WINDOW_WEEKDAYS = 22


def load_fixture(name):
    with open(os.path.join(_FIX, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    """Drop the inter-request courtesy delay for this module.

    A full uncached window is twenty-two requests; at the production 0.12s
    spacing that is real seconds of sleeping per test for behaviour none of
    them assert."""
    monkeypatch.setattr(fi, "REQUEST_PACING_S", 0.0)


class FakeResp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} for url")


class FakeSession:
    """Serves recorded index bodies keyed by day; every other day 404s.

    `bodies` values may be an `Exception` instance, which is raised instead —
    the network-error path. `default` (when given) answers any day not in
    `bodies`."""

    def __init__(self, bodies=None, default=None):
        self.bodies = dict(bodies or {})
        self.default = default
        self.requested = []

    def get(self, url, **kwargs):
        day = _day_from_url(url)
        self.requested.append(day)
        body = self.bodies.get(day, self.default)
        if isinstance(body, Exception):
            raise body
        if body is None:
            return FakeResp("", 404)
        return FakeResp(body, 200)


def _day_from_url(url):
    """'.../form.20260817.idx' -> '2026-08-17'."""
    stamp = url.rsplit("form.", 1)[-1].split(".idx")[0]
    return f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"


def _periodic_body(day=None):
    """The periodic-row fixture, optionally restamped onto another filing day."""
    text = load_fixture("daily_index_periodic.idx")
    if day:
        text = text.replace("20260817", day.replace("-", ""))
    return text


# --------------------------------------------------------------------------- #
# fetch_day
# --------------------------------------------------------------------------- #
def test_fetch_day_keeps_only_10q_and_10k():
    """The fixture carries 10-Q/A, NT 10-Q, 10-D, 8-K and Form 4 rows alongside
    the periodic ones; an amendment or a late-filing notice is a different
    document and must not enter the stream."""
    session = FakeSession({"2026-08-17": _periodic_body()})
    health = {}
    day = fi.fetch_day("2026-08-17", session, health, fi._now() + 10)
    assert sorted(f["form"] for f in day.values()) == ["10-K", "10-Q", "10-Q"]
    assert all(f["form"] in fi.PERIODIC_FORMS for f in day.values())
    assert "1786352" not in day   # the 10-Q/A
    assert "1234567" not in day   # the NT 10-Q
    assert "1318605" not in day   # the 8-K


def test_fetch_day_strips_leading_zeros_from_cik():
    """The pool side of the join is keyed by `_load_cik_map`'s zero-stripped
    form; Alphabet's row is printed zero-padded."""
    session = FakeSession({"2026-08-17": _periodic_body()})
    day = fi.fetch_day("2026-08-17", session, {}, fi._now() + 10)
    assert "1652044" in day
    assert "0001652044" not in day
    assert day["1652044"]["company"] == "Alphabet Inc."
    assert day["1652044"]["filed"] == "2026-08-17"
    assert day["1652044"]["path"].startswith("edgar/data/1652044/")


def test_fetch_day_404_is_none_not_empty():
    """404 means "no answer yet", which must stay distinguishable from "no
    periodic filings"."""
    session = FakeSession()          # every day 404s
    health = {}
    assert fi.fetch_day("2026-08-21", session, health, fi._now() + 10) is None
    assert health["filing_index"]["ok"] is False


def test_fetch_day_network_error_is_none():
    session = FakeSession({"2026-08-21": requests.ConnectionError("boom")})
    health = {}
    assert fi.fetch_day("2026-08-21", session, health, fi._now() + 10) is None
    assert "ConnectionError" in health["filing_index"]["note"]


def test_fetch_day_past_deadline_is_none_and_spends_no_request():
    session = FakeSession({"2026-08-17": _periodic_body()})
    health = {}
    assert fi.fetch_day("2026-08-17", session, health, fi._now() - 1) is None
    assert session.requested == []
    assert health["filing_index"]["ok"] is False


def test_fetch_day_with_no_periodic_filings_is_empty_dict():
    """The recorded index the ownership tests use holds only 1-A/10-D/4/13D
    rows — a real day with nothing periodic on it."""
    session = FakeSession({"2026-07-31": load_fixture("daily_index.idx")})
    assert fi.fetch_day("2026-07-31", session, {}, fi._now() + 10) == {}


# --------------------------------------------------------------------------- #
# read_index / write_index
# --------------------------------------------------------------------------- #
def test_read_index_missing_file_is_empty(tmp_path):
    assert fi.read_index(str(tmp_path)) == {}


def test_read_index_corrupt_file_is_empty_and_load_window_still_runs(tmp_path):
    root = str(tmp_path)
    with open(fi._index_path(root), "w", encoding="utf-8") as fh:
        fh.write("{not json at all")
    assert fi.read_index(root) == {}

    session = FakeSession({"2026-08-20": _periodic_body("2026-08-20")})
    health = {}
    merged = fi.load_window(root, TODAY, session, health)
    assert "789019" in merged
    assert health["filing_index"]["ok"] is True


def test_write_index_round_trips(tmp_path):
    root = os.path.join(str(tmp_path), "created", "on", "demand")
    fi.write_index(root, {"2026-08-20": {"789019": {"form": "10-K"}}})
    assert fi.read_index(root) == {"2026-08-20": {"789019": {"form": "10-K"}}}


# --------------------------------------------------------------------------- #
# load_window
# --------------------------------------------------------------------------- #
def test_load_window_caches_an_empty_day(tmp_path):
    """An empty day is a real, cacheable answer: fetched, nothing periodic."""
    root = str(tmp_path)
    session = FakeSession(default=load_fixture("daily_index.idx"))
    fi.load_window(root, TODAY, session, {})
    cache = fi.read_index(root)
    assert len(cache) == _WINDOW_WEEKDAYS
    assert cache["2026-08-21"] == {}

    session2 = FakeSession(default=load_fixture("daily_index.idx"))
    assert fi.load_window(root, TODAY, session2, {}) == {}
    assert session2.requested == []


def test_load_window_never_caches_a_failure_and_retries_it(tmp_path):
    """The single most important property of the cache: the newest day or two
    404s at brief time every morning, so a cached miss would be a permanent
    hole exactly where the freshest filings are."""
    root = str(tmp_path)
    bodies = {d: _periodic_body(d) for d in ("2026-08-20", "2026-08-19")}
    session = FakeSession(bodies, default=load_fixture("daily_index.idx"))
    session.bodies["2026-08-21"] = None          # today's index has not published
    fi.load_window(root, TODAY, session, {})

    assert "2026-08-21" in session.requested
    assert "2026-08-21" not in fi.read_index(root)

    session2 = FakeSession({"2026-08-21": _periodic_body("2026-08-21")})
    merged = fi.load_window(root, TODAY, session2, {})
    assert session2.requested == ["2026-08-21"]   # retried, and only that day
    assert fi.read_index(root)["2026-08-21"] != {}
    assert merged["789019"]["filed"] == "2026-08-21"


def test_load_window_prunes_days_outside_the_window(tmp_path):
    root = str(tmp_path)
    fi.write_index(root, {
        "2026-07-22": {"111": {"form": "10-K", "filed": "2026-07-22",
                               "company": "Aged Out Inc", "path": "p"}},
        "2026-07-23": {"222": {"form": "10-Q", "filed": "2026-07-23",
                               "company": "Oldest In Window", "path": "p"}},
    })
    session = FakeSession()          # everything else 404s
    merged = fi.load_window(root, TODAY, session, {})
    assert "2026-07-22" not in fi.read_index(root)
    assert "2026-07-23" in fi.read_index(root)
    assert "111" not in merged
    assert "222" in merged
    assert "2026-07-22" not in session.requested


def test_load_window_does_not_refetch_cached_days(tmp_path):
    root = str(tmp_path)
    session = FakeSession(default=load_fixture("daily_index.idx"))
    fi.load_window(root, TODAY, session, {})
    assert len(session.requested) == _WINDOW_WEEKDAYS

    session2 = FakeSession({"2026-08-24": _periodic_body("2026-08-24")},
                           default=load_fixture("daily_index.idx"))
    fi.load_window(root, "2026-08-24", session2, {})   # the following Monday
    assert session2.requested == ["2026-08-24"]        # 08-22/08-23 are the weekend


def test_load_window_never_requests_a_weekend(tmp_path):
    """EDGAR publishes no index on Saturday or Sunday; a request buys a
    guaranteed 404."""
    session = FakeSession(default=load_fixture("daily_index.idx"))
    fi.load_window(str(tmp_path), TODAY, session, {})
    assert session.requested
    assert session.requested[0] == TODAY               # newest day first
    weekend = {"2026-08-16", "2026-08-15", "2026-08-09", "2026-08-08",
               "2026-08-02", "2026-08-01", "2026-07-26", "2026-07-25"}
    assert weekend.isdisjoint(session.requested)
    assert len(session.requested) == _WINDOW_WEEKDAYS


def test_load_window_past_deadline_returns_the_cache_without_fetching(tmp_path):
    root = str(tmp_path)
    fi.write_index(root, {
        "2026-08-20": {"789019": {"form": "10-K", "filed": "2026-08-20",
                                  "company": "MICROSOFT CORP", "path": "p"}},
    })
    session = FakeSession(default=load_fixture("daily_index_periodic.idx"))
    health = {}
    merged = fi.load_window(root, TODAY, session, health, deadline_at=fi._now() - 1)
    assert session.requested == []
    assert "789019" in merged
    # The counts ride alongside the note as real numbers so `metrics` never has
    # to regex an English sentence to recover them.
    assert health["filing_index"]["ok"] is True
    assert health["filing_index"]["note"] == "1 days cached, 0 fetched"
    assert health["filing_index"]["days_cached"] == 1
    assert health["filing_index"]["days_fetched"] == 0


def test_load_window_keeps_the_most_recent_filing_for_a_duplicate_cik(tmp_path):
    root = str(tmp_path)
    fi.write_index(root, {
        "2026-08-12": {"789019": {"form": "10-Q", "filed": "2026-08-12",
                                  "company": "MICROSOFT CORP", "path": "old"}},
        "2026-08-19": {"789019": {"form": "10-K", "filed": "2026-08-19",
                                  "company": "MICROSOFT CORP", "path": "new"}},
    })
    merged = fi.load_window(root, TODAY, FakeSession(), {}, deadline_at=fi._now() - 1)
    assert merged["789019"]["filed"] == "2026-08-19"
    assert merged["789019"]["path"] == "new"


def test_load_window_health_counts_cached_and_fetched_days(tmp_path):
    root = str(tmp_path)
    session = FakeSession({"2026-08-20": _periodic_body("2026-08-20"),
                           "2026-08-19": _periodic_body("2026-08-19")})
    health = {}
    fi.load_window(root, TODAY, session, health, deadline_at=fi._now() + 30)
    assert health["filing_index"]["ok"] is True
    assert health["filing_index"]["note"] == "2 days cached, 2 fetched, 20 unavailable"


def test_load_window_unusable_date_is_empty_and_does_not_raise(tmp_path):
    health = {}
    assert fi.load_window(str(tmp_path), "not-a-date", FakeSession(), health) == {}
    assert health["filing_index"]["ok"] is False
    assert not os.path.exists(fi._index_path(str(tmp_path)))
