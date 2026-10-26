"""Tests for the post-earnings pool (scripts/post_earnings_pool.py).

Fully offline: the module's only I/O is its own JSON file, so every test runs
against a tmp_path knowledge-base root with no stubbing at all.
"""

import json
import os
import sys
from datetime import datetime, timedelta

# Make scripts/ importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from worldintel.core import earnings_report as er  # noqa: E402
from worldintel.core import post_earnings_pool as pep  # noqa: E402

TODAY = "2026-08-22"


def _cand(ticker, report_date="2026-08-21", **extra):
    """A post_earnings_candidates row with the fields the pool reads."""
    cand = {
        "ticker": ticker, "name": f"{ticker} Inc", "report_date": report_date,
        "cik": "315189", "hour": "amc", "eps_est": 1.0, "eps_act": 1.2,
        "surprise_pct": 20.0, "rev_est": 1e9, "rev_act": 1.1e9,
        "rev_act_source": "finnhub", "price_move_pct": 4.5,
        "guidance_struct": None, "fwd_eps_q": 1.3, "fwd_eps_fy": 5.2,
    }
    cand.update(extra)
    return cand


def _shift(day, days):
    return str(datetime.strptime(day, "%Y-%m-%d").date() + timedelta(days=days))


# --------------------------------------------------------------------------- #
# File handling
# --------------------------------------------------------------------------- #
def test_write_read_round_trips(tmp_path):
    pool = {"DE:2026-08-21": {"ticker": "DE", "origin": "card",
                              "first_seen": TODAY, "snapshot": {"name": "Deere"},
                              "line_shown": None, "block_shown": None}}
    pep.write_pool(str(tmp_path), pool)
    assert pep.read_pool(str(tmp_path)) == pool


def test_read_pool_missing_file(tmp_path):
    assert pep.read_pool(str(tmp_path)) == {}


def test_read_pool_corrupt_json(tmp_path):
    (tmp_path / pep.POOL_FILE).write_text("{not json", encoding="utf-8")
    assert pep.read_pool(str(tmp_path)) == {}


def test_read_pool_non_dict_json(tmp_path):
    (tmp_path / pep.POOL_FILE).write_text('["DE:2026-08-21"]', encoding="utf-8")
    assert pep.read_pool(str(tmp_path)) == {}


def test_write_pool_swallows_unserializable(tmp_path):
    pep.write_pool(str(tmp_path), {"DE:2026-08-21": object()})
    # A failed write leaves no half-file behind that read_pool would choke on.
    assert pep.read_pool(str(tmp_path)) == {}


def test_write_pool_creates_the_root(tmp_path):
    root = str(tmp_path / "kb" / "nested")
    pep.write_pool(root, {"DE:2026-08-21": {"ticker": "DE"}})
    assert os.path.exists(os.path.join(root, pep.POOL_FILE))


# --------------------------------------------------------------------------- #
# record
# --------------------------------------------------------------------------- #
def test_record_captures_card_and_also_origins(tmp_path):
    pep.record(str(tmp_path), [_cand("DE"), _cand("BILL"), _cand("XYZ")],
               {"DE"}, {"BILL"}, TODAY)
    pool = pep.read_pool(str(tmp_path))
    assert set(pool) == {"DE:2026-08-21", "BILL:2026-08-21"}
    assert pool["DE:2026-08-21"]["origin"] == "card"
    assert pool["BILL:2026-08-21"]["origin"] == "also"
    # A candidate in neither list never appeared in the section.
    assert "XYZ:2026-08-21" not in pool


def test_record_entry_shape(tmp_path):
    pep.record(str(tmp_path), [_cand("DE")], {"DE"}, set(), TODAY)
    entry = pep.read_pool(str(tmp_path))["DE:2026-08-21"]
    assert entry["ticker"] == "DE"
    assert entry["cik"] == "315189"
    assert entry["name"] == "DE Inc"
    assert entry["first_seen"] == TODAY
    assert entry["line_shown"] is None
    assert entry["block_shown"] is None


def test_record_ticker_in_both_lists_is_one_card(tmp_path):
    pep.record(str(tmp_path), [_cand("DE")], {"DE"}, {"DE"}, TODAY)
    pool = pep.read_pool(str(tmp_path))
    assert list(pool) == ["DE:2026-08-21"]
    assert pool["DE:2026-08-21"]["origin"] == "card"


def test_record_upgrades_also_to_card(tmp_path):
    pep.record(str(tmp_path), [_cand("DE")], set(), {"DE"}, TODAY)
    assert pep.read_pool(str(tmp_path))["DE:2026-08-21"]["origin"] == "also"
    pep.record(str(tmp_path), [_cand("DE")], {"DE"}, set(), TODAY)
    assert pep.read_pool(str(tmp_path))["DE:2026-08-21"]["origin"] == "card"


def test_record_never_downgrades_card_to_also(tmp_path):
    pep.record(str(tmp_path), [_cand("DE")], {"DE"}, set(), TODAY)
    pep.record(str(tmp_path), [_cand("DE")], set(), {"DE"}, TODAY)
    assert pep.read_pool(str(tmp_path))["DE:2026-08-21"]["origin"] == "card"


def test_record_preserves_first_seen_snapshot_and_shown(tmp_path):
    pep.record(str(tmp_path), [_cand("DE")], {"DE"}, set(), TODAY)
    pep.mark_shown(str(tmp_path), ["DE:2026-08-21"], ["DE:2026-08-21"], TODAY)
    later = "2026-08-24"
    # Same key, different numbers: beat 1 published the originals and beat 2
    # recalls those, so a re-record must not rewrite them.
    pep.record(str(tmp_path), [_cand("DE", eps_act=9.9, surprise_pct=900.0)],
               {"DE"}, set(), later)
    entry = pep.read_pool(str(tmp_path))["DE:2026-08-21"]
    assert entry["first_seen"] == TODAY
    assert entry["snapshot"]["eps_act"] == 1.2
    assert entry["snapshot"]["surprise_pct"] == 20.0
    assert entry["line_shown"] == TODAY
    assert entry["block_shown"] == TODAY


def test_record_snapshot_carries_exactly_the_shared_keys(tmp_path):
    pep.record(str(tmp_path), [_cand("DE")], {"DE"}, set(), TODAY)
    snapshot = pep.read_pool(str(tmp_path))["DE:2026-08-21"]["snapshot"]
    assert set(snapshot) == set(er._SNAPSHOT_KEYS)


def test_record_missing_cik_stores_empty_string(tmp_path):
    cand = _cand("DE")
    del cand["cik"]
    pep.record(str(tmp_path), [cand], {"DE"}, set(), TODAY)
    assert pep.read_pool(str(tmp_path))["DE:2026-08-21"]["cik"] == ""


def test_record_upper_cases_the_key_and_ticker(tmp_path):
    pep.record(str(tmp_path), [_cand("de")], {"de"}, set(), TODAY)
    pep.record(str(tmp_path), [_cand("DE")], {"DE"}, set(), TODAY)
    pool = pep.read_pool(str(tmp_path))
    assert list(pool) == ["DE:2026-08-21"]
    assert pool["DE:2026-08-21"]["ticker"] == "DE"


def test_record_same_ticker_two_quarters_are_two_entries(tmp_path):
    pep.record(str(tmp_path), [_cand("DE", report_date="2026-05-15"),
                               _cand("DE", report_date="2026-08-21")],
               {"DE"}, set(), TODAY)
    assert set(pep.read_pool(str(tmp_path))) == {"DE:2026-05-15", "DE:2026-08-21"}


def test_record_survives_junk_input(tmp_path):
    pep.record(str(tmp_path), [None, "DE", {}, _cand("DE")], {"DE"}, set(), TODAY)
    assert list(pep.read_pool(str(tmp_path))) == ["DE:2026-08-21"]


def test_record_onto_a_corrupt_pool_still_records(tmp_path):
    (tmp_path / pep.POOL_FILE).write_text("{not json", encoding="utf-8")
    pep.record(str(tmp_path), [_cand("DE")], {"DE"}, set(), TODAY)
    assert list(pep.read_pool(str(tmp_path))) == ["DE:2026-08-21"]


def test_record_prunes_before_writing(tmp_path):
    stale = _shift(TODAY, -(pep.POOL_WINDOW_DAYS + 1))
    pep.record(str(tmp_path), [_cand("OLD", report_date="2026-07-01")],
               {"OLD"}, set(), stale)
    pep.record(str(tmp_path), [_cand("DE")], {"DE"}, set(), TODAY)
    assert list(pep.read_pool(str(tmp_path))) == ["DE:2026-08-21"]


# --------------------------------------------------------------------------- #
# mark_shown
# --------------------------------------------------------------------------- #
def test_mark_shown_line_records_first_sighting_only(tmp_path):
    pep.record(str(tmp_path), [_cand("DE")], {"DE"}, set(), TODAY)
    pep.mark_shown(str(tmp_path), ["DE:2026-08-21"], [], TODAY)
    pep.mark_shown(str(tmp_path), ["DE:2026-08-21"], [], "2026-08-25")
    assert pep.read_pool(str(tmp_path))["DE:2026-08-21"]["line_shown"] == TODAY


def test_mark_shown_block_is_idempotent(tmp_path):
    pep.record(str(tmp_path), [_cand("DE")], {"DE"}, set(), TODAY)
    pep.mark_shown(str(tmp_path), [], ["DE:2026-08-21"], TODAY)
    first = pep.read_pool(str(tmp_path))
    pep.mark_shown(str(tmp_path), [], ["DE:2026-08-21"], TODAY)
    assert pep.read_pool(str(tmp_path)) == first
    assert first["DE:2026-08-21"]["block_shown"] == TODAY


def test_mark_shown_line_and_block_are_independent(tmp_path):
    pep.record(str(tmp_path), [_cand("DE"), _cand("BILL")], {"DE"}, {"BILL"}, TODAY)
    pep.mark_shown(str(tmp_path), ["BILL:2026-08-21"], ["DE:2026-08-21"], TODAY)
    pool = pep.read_pool(str(tmp_path))
    assert pool["BILL:2026-08-21"]["line_shown"] == TODAY
    assert pool["BILL:2026-08-21"]["block_shown"] is None
    assert pool["DE:2026-08-21"]["line_shown"] is None
    assert pool["DE:2026-08-21"]["block_shown"] == TODAY


def test_mark_shown_matches_a_lower_case_ticker_key(tmp_path):
    pep.record(str(tmp_path), [_cand("DE")], {"DE"}, set(), TODAY)
    pep.mark_shown(str(tmp_path), ["de:2026-08-21"], [], TODAY)
    pool = pep.read_pool(str(tmp_path))
    assert list(pool) == ["DE:2026-08-21"]
    assert pool["DE:2026-08-21"]["line_shown"] == TODAY


def test_mark_shown_unknown_key_is_a_no_op(tmp_path):
    pep.record(str(tmp_path), [_cand("DE")], {"DE"}, set(), TODAY)
    before = pep.read_pool(str(tmp_path))
    pep.mark_shown(str(tmp_path), ["NOPE:2026-08-21"], ["NOPE:2026-08-21"], TODAY)
    assert pep.read_pool(str(tmp_path)) == before


def test_mark_shown_on_a_missing_pool_does_not_raise(tmp_path):
    pep.mark_shown(str(tmp_path), ["DE:2026-08-21"], ["DE:2026-08-21"], TODAY)
    assert pep.read_pool(str(tmp_path)) == {}


def test_mark_shown_prunes_before_writing(tmp_path):
    stale = _shift(TODAY, -(pep.POOL_WINDOW_DAYS + 1))
    pep.write_pool(str(tmp_path), {
        "OLD:2026-07-01": {"ticker": "OLD", "first_seen": stale,
                           "line_shown": None, "block_shown": None},
        "DE:2026-08-21": {"ticker": "DE", "first_seen": TODAY,
                          "line_shown": None, "block_shown": None},
    })
    pep.mark_shown(str(tmp_path), ["DE:2026-08-21"], [], TODAY)
    assert list(pep.read_pool(str(tmp_path))) == ["DE:2026-08-21"]


# --------------------------------------------------------------------------- #
# prune
# --------------------------------------------------------------------------- #
def test_prune_keeps_inside_and_drops_outside_the_window(tmp_path):
    edge = _shift(TODAY, -pep.POOL_WINDOW_DAYS)
    past = _shift(TODAY, -(pep.POOL_WINDOW_DAYS + 1))
    pool = {
        "FRESH:1": {"first_seen": TODAY},
        "EDGE:1": {"first_seen": edge},
        "GONE:1": {"first_seen": past},
    }
    assert set(pep.prune(pool, TODAY)) == {"FRESH:1", "EDGE:1"}


def test_prune_drops_an_unparseable_first_seen(tmp_path):
    pool = {"BAD:1": {"first_seen": "not-a-date"},
            "NONE:1": {"first_seen": None},
            "ABSENT:1": {"ticker": "ABSENT"},
            "DE:2026-08-21": {"first_seen": TODAY}}
    assert set(pep.prune(pool, TODAY)) == {"DE:2026-08-21"}


def test_prune_drops_non_dict_entries(tmp_path):
    assert pep.prune({"JUNK:1": "nope", "DE:2026-08-21": {"first_seen": TODAY}},
                     TODAY) == {"DE:2026-08-21": {"first_seen": TODAY}}


def test_prune_returns_a_new_dict(tmp_path):
    pool = {"DE:2026-08-21": {"first_seen": TODAY}}
    out = pep.prune(pool, TODAY)
    assert out is not pool
    assert out["DE:2026-08-21"] is pool["DE:2026-08-21"]


def test_prune_empty_and_none(tmp_path):
    assert pep.prune({}, TODAY) == {}
    assert pep.prune(None, TODAY) == {}


def test_pool_file_lives_at_the_kb_root(tmp_path):
    pep.record(str(tmp_path), [_cand("DE")], {"DE"}, set(), TODAY)
    path = tmp_path / "post_earnings_pool.json"
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["DE:2026-08-21"]["ticker"] == "DE"
