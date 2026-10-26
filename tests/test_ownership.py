"""Tests for the ownership filings layer (scripts/ownership.py): the pure
parsers/selectors (Tasks 1-2) plus the network layer and `build_ownership`
orchestrator (Task 3).

Fully offline: fixture-backed parser tests feed reconstructed EDGAR-format
fixtures (see tests/fixtures/README.md). Other selector/deduplication cases
retain public historical name-matching inputs. Network functions use fake
sessions or stubbed seams. No network I/O.
"""

import copy
import os
import sys

import pytest

# Make scripts/ importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from worldintel.core import ownership as own  # noqa: E402
from worldintel.core import earnings_report  # noqa: E402
from worldintel.core import fetch_sources  # noqa: E402

_FIX = os.path.join(_HERE, "fixtures")


def load_fixture(name):
    with open(os.path.join(_FIX, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(autouse=True)
def _relax_scan_throttle(monkeypatch):
    """Relax the shared 10 req/s scan throttle for this module.

    `own._SCAN_LIMITER` enforces SEC's fair-access rate across every scan
    worker; left at its production interval it would add real seconds of
    sleeping to each `build_ownership` test here. The limiter's arithmetic,
    its default interval and its wiring into the scan path are covered by the
    dedicated tests at the bottom of this file, which install or configure
    their own limiter and therefore are not affected by this relaxation.

    A FRESH instance, not a relaxed copy of the live one: the limiter carries
    a monotonic `_next_at` across calls, and several tests below stub `_now()`
    with a small fake clock. Reusing an instance whose `_next_at` came from
    the real monotonic clock would make `acquire()` sleep the difference --
    hours."""
    monkeypatch.setattr(own, "_SCAN_LIMITER", own._RateLimiter(0.0))


# --------------------------------------------------------------------------- #
# classify_action
# --------------------------------------------------------------------------- #
def test_classify_action_maps_every_documented_code_plus_unknown():
    assert own.classify_action("P") == "open-market buy"
    assert own.classify_action("S") == "sale"
    assert own.classify_action("A") == "grant"
    assert own.classify_action("M") == "option exercise"
    assert own.classify_action("F") == "tax withholding"
    assert own.classify_action("G") == "gift"
    assert own.classify_action("Z") == "other"
    assert own.classify_action(None) == "other"
    assert own.classify_action("") == "other"


# --------------------------------------------------------------------------- #
# parse_daily_index
# --------------------------------------------------------------------------- #
def test_parse_daily_index_separates_13d_from_13d_amendment():
    """A fixed-width line[:12] slice merges '13D/A' into the '13D' bucket
    (both start with 'SCHEDULE 13D' in the first 12 chars). The 2+-space
    split must keep them apart."""
    rows = own.parse_daily_index(load_fixture("daily_index.idx"))
    thirteen_d = [r for r in rows if r["form"] == "SCHEDULE 13D"]
    thirteen_d_a = [r for r in rows if r["form"] == "SCHEDULE 13D/A"]
    assert len(thirteen_d) > 0
    assert len(thirteen_d_a) > 0


def test_parse_daily_index_skips_header_and_yields_well_formed_rows():
    rows = own.parse_daily_index(load_fixture("daily_index.idx"))
    assert len(rows) > 0
    for row in rows:
        assert set(row) == {"form", "company", "cik", "filed", "path"}
        assert row["filed"].count("-") == 2
        year, month, day = row["filed"].split("-")
        assert len(year) == 4 and len(month) == 2 and len(day) == 2
    # Header text must never leak through as a data row.
    assert not any("Form Type" in r["form"] for r in rows)
    assert not any("Description" in r["form"] for r in rows)


def test_parse_daily_index_path_and_cik_for_known_row():
    rows = own.parse_daily_index(load_fixture("daily_index.idx"))
    match = [r for r in rows if r["company"] == "17 Education & Technology Group Inc."]
    assert len(match) == 1
    row = match[0]
    assert row["form"] == "4"
    assert row["cik"] == "1821468"
    assert row["filed"] == "2026-07-31"
    assert row["path"] == "edgar/data/1821468/0001193125-26-326663.txt"


# --------------------------------------------------------------------------- #
# parse_form4
# --------------------------------------------------------------------------- #
def test_parse_form4_multi_txn_returns_seven_rows_with_expected_codes():
    rows = own.parse_form4(load_fixture("form4_multi_txn.xml"))
    assert len(rows) == 7
    assert [r["code"] for r in rows] == ["L"] * 6 + ["P"]


def test_parse_form4_unescapes_ampersand_in_issuer_name():
    rows = own.parse_form4(load_fixture("form4_buy.xml"))
    assert rows[0]["company"] == "Synthetic Learning & Technology Inc."


def test_parse_form4_planned_handles_all_three_aff10b5_spellings():
    # form4_buy.xml / form4_sale.xml (via form4_planned_sale.xml's untouched
    # sibling) use the string "false"; form4_grant.xml uses "0"; the edited
    # form4_planned_sale.xml fixture uses "true".
    buy_rows = own.parse_form4(load_fixture("form4_buy.xml"))
    assert all(r["planned"] is False for r in buy_rows)

    grant_rows = own.parse_form4(load_fixture("form4_grant.xml"))
    assert len(grant_rows) == 1
    assert grant_rows[0]["planned"] is False

    planned_sale_rows = own.parse_form4(load_fixture("form4_planned_sale.xml"))
    assert len(planned_sale_rows) == 1
    assert planned_sale_rows[0]["planned"] is True


def test_parse_form4_computes_value_and_zero_price_grant_is_zero():
    buy_rows = own.parse_form4(load_fixture("form4_buy.xml"))
    first = buy_rows[0]
    assert first["shares"] == 2420.0
    assert first["price"] == 2.2568
    assert first["value"] == 2420.0 * 2.2568

    grant_rows = own.parse_form4(load_fixture("form4_grant.xml"))
    grant = grant_rows[0]
    assert grant["price"] == 0.0
    assert grant["value"] == 0.0


def test_parse_form4_grant_role_and_action():
    grant_rows = own.parse_form4(load_fixture("form4_grant.xml"))
    grant = grant_rows[0]
    assert grant["role"] == "Director"
    assert grant["code"] == "A"
    assert grant["action"] == "grant"
    assert grant["derivative"] is False


def test_parse_form4_sale_role_and_action():
    rows = own.parse_form4(load_fixture("form4_planned_sale.xml"))
    row = rows[0]
    assert row["action"] == "sale"
    assert row["code"] == "S"
    assert row["ticker"] == "SYNB"
    assert row["company"] == "SYNTHETIC EXAMPLE BANK INC"
    assert row["person"] == "EXAMPLE ALEX Q"
    assert row["role"] == "Director"


def test_parse_form4_officer_title_wins_over_director_flag():
    rows = own.parse_form4(load_fixture("form4_buy.xml"))
    assert rows[0]["role"] == "Chief Executive Officer"


def test_parse_form4_distinguishes_derivative_from_non_derivative():
    rows = own.parse_form4(load_fixture("form4_multi_txn.xml"))
    assert all(r["derivative"] is False for r in rows)


def test_parse_form4_derivative_rows_parsed_correctly():
    """form4_derivative.xml (synthetic issuer) has 4 non-derivative + 2
    derivative transactions, codes ['A','A','A','D','A','D']. This is the
    positive case the multi_txn fixture above cannot cover: it has no
    populated derivativeTable at all, so `derivative: True` rows were never
    produced or verified by any test."""
    rows = own.parse_form4(load_fixture("form4_derivative.xml"))
    assert len(rows) == 6
    assert [r["code"] for r in rows] == ["A", "A", "A", "D", "A", "D"]

    derivative_rows = [r for r in rows if r["derivative"] is True]
    assert len(derivative_rows) == 2
    non_derivative_rows = [r for r in rows if r["derivative"] is False]
    assert len(non_derivative_rows) == 4

    first, second = derivative_rows
    assert first["code"] == "A"
    assert first["shares"] == 294155.0
    assert first["price"] == 0.0
    assert first["date"] == "2026-07-29"
    assert first["value"] == 0.0

    assert second["code"] == "D"
    assert second["shares"] == 538410.0
    assert second["price"] == 13.95
    assert second["date"] == "2026-07-29"
    assert second["value"] == 538410.0 * 13.95


# --------------------------------------------------------------------------- #
# parse_schedule13d
# --------------------------------------------------------------------------- #
def test_parse_schedule13d_new_takes_max_percent_and_matching_shares():
    result = own.parse_schedule13d(load_fixture("schedule13d_new.xml"))
    assert result["company"] == "Synthetic Medical Holdings Ltd"
    assert result["issuer_cik"] == "0000000001"
    assert result["filer"] == "Synthetic Capital Two Ltd"
    assert result["percent"] == 19.1
    assert result["shares"] == 7004016.0
    assert result["event_date"] == "2024-10-01"
    assert set(result["filers"]) == {
        "Synthetic Capital One LLC",
        "Synthetic Capital Two Ltd",
        "Synthetic Capital Three LLC",
    }
    assert len(result["filers"]) == 3


# In schedule13d_new.xml the max-percent reporting person also happens to hold
# the max share count, so a buggy `shares = max(aggregateAmountOwned)`
# implementation would pass the test above identically. This synthetic 13D
# deliberately makes the two diverge: the person with the smaller stake has
# the larger raw share count (illustrating a lower share class / smaller
# float), so only a correct "shares of the max-percent person" implementation
# picks the right value.
_DIVERGENT_13D_XML = """<edgarSubmission>
  <formData>
    <coverPageHeader>
      <dateOfEvent>01/02/2026</dateOfEvent>
      <issuerInfo>
        <issuerCIK>0000000001</issuerCIK>
        <issuerName>Synthetic Test Issuer</issuerName>
      </issuerInfo>
    </coverPageHeader>
    <reportingPersons>
      <reportingPersonInfo>
        <reportingPersonName>Big Share Count Small Stake LP</reportingPersonName>
        <aggregateAmountOwned>9000000.00</aggregateAmountOwned>
        <percentOfClass>5.0</percentOfClass>
      </reportingPersonInfo>
      <reportingPersonInfo>
        <reportingPersonName>Small Share Count Big Stake LLC</reportingPersonName>
        <aggregateAmountOwned>1000.00</aggregateAmountOwned>
        <percentOfClass>20.0</percentOfClass>
      </reportingPersonInfo>
    </reportingPersons>
  </formData>
</edgarSubmission>"""


def test_parse_schedule13d_shares_follows_max_percent_not_max_shares():
    result = own.parse_schedule13d(_DIVERGENT_13D_XML)
    assert result["filer"] == "Small Share Count Big Stake LLC"
    assert result["percent"] == 20.0
    assert result["shares"] == 1000.0
    assert set(result["filers"]) == {
        "Big Share Count Small Stake LP",
        "Small Share Count Big Stake LLC",
    }


def test_parse_schedule13d_purpose_truncated_on_word_boundary():
    result = own.parse_schedule13d(load_fixture("schedule13d_new.xml"))
    purpose = result["purpose"]
    assert len(purpose) <= 120
    assert purpose.endswith("…")
    # Word boundary: strip the ellipsis and confirm we didn't cut mid-word,
    # i.e. the character right before the ellipsis is not immediately
    # preceded by a partial-word split (the boundary text must reappear
    # verbatim as a prefix of the untruncated source).
    raw = purpose[:-1].rstrip()
    assert raw != "" and raw in load_fixture("schedule13d_new.xml").replace("\n", " ")


def test_parse_schedule13d_amendment_has_no_item4_purpose():
    result = own.parse_schedule13d(load_fixture("schedule13d_activist_amend.xml"))
    assert result["company"] == "Synthetic Credit Fund LLC"
    assert result["issuer_cik"] == "0000000002"
    assert result["filer"] == "Synthetic Holdings Corp."
    assert result["percent"] == 59.9
    assert result["shares"] == 1541762.06
    assert result["event_date"] == "2026-07-29"
    assert result["filers"] == ["Synthetic Holdings Corp."]
    assert result["purpose"] == ""


# --------------------------------------------------------------------------- #
# Degradation on garbage input — never raise
# --------------------------------------------------------------------------- #
def test_parsers_degrade_on_empty_string():
    assert own.parse_daily_index("") == []
    assert own.parse_form4("") == []
    result = own.parse_schedule13d("")
    assert result["company"] == ""
    assert result["percent"] == 0.0
    assert result["filers"] == []


def test_parsers_degrade_on_truncated_xml():
    assert own.parse_daily_index("<xml") == []
    assert own.parse_form4("<xml") == []
    result = own.parse_schedule13d("<xml")
    assert result["company"] == ""
    assert result["filers"] == []


def test_parsers_degrade_on_binary_decoded_text():
    garbage = bytes(range(256)).decode("latin-1")
    assert own.parse_daily_index(garbage) == []
    assert own.parse_form4(garbage) == []
    result = own.parse_schedule13d(garbage)
    assert result["company"] == ""
    assert result["filers"] == []


# --------------------------------------------------------------------------- #
# select_insider
# --------------------------------------------------------------------------- #
def _txn(**overrides):
    """A minimal parse_form4-shaped transaction row, with sane defaults."""
    row = {
        "ticker": "ACME",
        "company": "Acme Corp",
        "person": "DOE JANE",
        "role": "Director",
        "planned": False,
        "code": "P",
        "action": "open-market buy",
        "shares": 1000.0,
        "price": 10.0,
        "value": 10_000.0,
        "date": "2026-08-01",
        "derivative": False,
    }
    row.update(overrides)
    return row


def test_select_insider_watchlist_keeps_every_code_including_grant_and_derivative():
    rows = [
        _txn(ticker="AAPL", code="A", derivative=False, value=1.0, person="p1"),
        _txn(ticker="AAPL", code="P", derivative=True, value=2.0, person="p2"),
    ]
    watchlist, _buys = own.select_insider(rows, ["AAPL"])
    assert len(watchlist) == 2
    assert {r["code"] for r in watchlist} == {"A", "P"}
    assert {r["derivative"] for r in watchlist} == {False, True}


def test_select_insider_watchlist_ranks_by_value_desc_and_caps_at_four():
    rows = [
        _txn(ticker="AAPL", value=500.0, person="p1"),
        _txn(ticker="MSFT", value=400.0, person="p2"),
        _txn(ticker="GOOG", value=300.0, person="p3"),
        _txn(ticker="AMZN", value=200.0, person="p4"),
        _txn(ticker="TSLA", value=100.0, person="p5"),
    ]
    watchlist_tickers = ["AAPL", "MSFT", "GOOG", "AMZN", "TSLA"]
    watchlist, _buys = own.select_insider(rows, watchlist_tickers)
    assert [r["ticker"] for r in watchlist] == ["AAPL", "MSFT", "GOOG", "AMZN"]


def test_select_insider_buy_leg_keeps_only_code_p():
    rows = [
        _txn(ticker="XXX", code="P", derivative=False, value=2_000_000.0, person="p1"),
        _txn(ticker="YYY", code="S", derivative=False, value=2_000_000.0, person="p2"),
    ]
    _watchlist, buys = own.select_insider(rows, [])
    assert [r["ticker"] for r in buys] == ["XXX"]


def test_select_insider_buy_leg_excludes_derivative_even_when_code_is_p():
    rows = [_txn(ticker="XXX", code="P", derivative=True, value=5_000_000.0)]
    _watchlist, buys = own.select_insider(rows, [])
    assert buys == []


def test_select_insider_buy_leg_floor_boundary():
    rows = [
        _txn(ticker="A", code="P", derivative=False, value=999_999.99, person="p1"),
        _txn(ticker="B", code="P", derivative=False, value=1_000_000.00, person="p2"),
    ]
    _watchlist, buys = own.select_insider(rows, [])
    assert [r["ticker"] for r in buys] == ["B"]


def test_select_insider_buy_leg_caps_at_three():
    rows = [
        _txn(ticker="A", code="P", derivative=False, value=4_000_000.0, person="p1"),
        _txn(ticker="B", code="P", derivative=False, value=3_000_000.0, person="p2"),
        _txn(ticker="C", code="P", derivative=False, value=2_000_000.0, person="p3"),
        _txn(ticker="D", code="P", derivative=False, value=1_000_000.0, person="p4"),
    ]
    _watchlist, buys = own.select_insider(rows, [])
    assert [r["ticker"] for r in buys] == ["A", "B", "C"]


def test_select_insider_single_filing_with_p_and_s_contributes_only_p_to_buys():
    rows = [
        _txn(ticker="Z", code="P", derivative=False, value=2_000_000.0, person="Bob"),
        _txn(ticker="Z", code="S", derivative=False, value=2_000_000.0, person="Bob"),
    ]
    _watchlist, buys = own.select_insider(rows, [])
    assert len(buys) == 1
    assert buys[0]["code"] == "P"


def test_select_insider_dedup_watchlist_ticker_excluded_from_buy_leg():
    rows = [_txn(ticker="Z", code="P", derivative=False, value=5_000_000.0, person="Bob")]
    watchlist, buys = own.select_insider(rows, ["Z"])
    assert [r["ticker"] for r in watchlist] == ["Z"]
    assert buys == []


def test_select_insider_dedup_boundary_ticker_truncated_by_cap_still_eligible_for_buy():
    rows = [
        _txn(ticker="A1", code="A", derivative=False, value=5_000_000.0, person="p1"),
        _txn(ticker="A2", code="A", derivative=False, value=4_000_000.0, person="p2"),
        _txn(ticker="A3", code="A", derivative=False, value=3_000_000.0, person="p3"),
        _txn(ticker="A4", code="A", derivative=False, value=2_000_000.0, person="p4"),
        _txn(ticker="A5", code="P", derivative=False, value=1_500_000.0, person="p5"),
    ]
    watchlist_tickers = ["A1", "A2", "A3", "A4", "A5"]
    watchlist, buys = own.select_insider(rows, watchlist_tickers)
    assert [r["ticker"] for r in watchlist] == ["A1", "A2", "A3", "A4"]
    assert [r["ticker"] for r in buys] == ["A5"]


def test_select_insider_ranking_deterministic_on_value_ties():
    rows = [
        _txn(ticker="B", value=1_000_000.0, person="Zed"),
        _txn(ticker="A", value=1_000_000.0, person="Zoe"),
        _txn(ticker="A", value=1_000_000.0, person="Adam"),
    ]
    watchlist, _buys = own.select_insider(rows, ["A", "B"])
    assert [(r["ticker"], r["person"]) for r in watchlist] == [
        ("A", "Adam"),
        ("A", "Zoe"),
        ("B", "Zed"),
    ]


def test_select_insider_malformed_row_missing_value_ranks_last_without_raising():
    rows = [
        {"ticker": "Z", "code": "A", "derivative": False, "person": "p1"},
        _txn(ticker="Q", value=500.0, person="p2"),
    ]
    watchlist, _buys = own.select_insider(rows, ["Z", "Q"])
    assert [r["ticker"] for r in watchlist] == ["Q", "Z"]


def test_select_insider_watchlist_matches_tickers_case_insensitively():
    rows = [_txn(ticker="aapl", value=1.0, person="p1")]
    watchlist, _buys = own.select_insider(rows, ["AAPL"])
    assert [r["ticker"] for r in watchlist] == ["aapl"]


def test_select_insider_does_not_mutate_input_rows():
    # A deep copy, not `list(rows)`: a shallow copy shares the same dict
    # objects, so an in-place field mutation (e.g. stamping a `_rank` key
    # onto each row) would be invisible to the comparison below.
    rows = [_txn(ticker="A", value=1.0), _txn(ticker="B", value=2.0)]
    original = copy.deepcopy(rows)
    own.select_insider(rows, ["A", "B"])
    assert rows == original


# --------------------------------------------------------------------------- #
# select_13d
# --------------------------------------------------------------------------- #
def _stake(**overrides):
    """A minimal parse_schedule13d-shaped row plus the caller-added `form`/`filed`."""
    row = {
        "company": "Some Issuer Inc",
        "issuer_cik": "0000000001",
        "filer": "Some Filer LLC",
        "percent": 10.0,
        "shares": 100.0,
        "event_date": "2026-01-01",
        "purpose": "",
        "filers": ["Some Filer LLC"],
        "form": "SCHEDULE 13D",
        "filed": "2026-01-01",
    }
    row.update(overrides)
    return row


def test_select_13d_keeps_all_new_and_only_allowlisted_amendments():
    stakes = [
        _stake(company="NewCo", form="SCHEDULE 13D", filers=["Random Holdco LLC"], percent=5.0),
        _stake(
            company="AmendGood",
            form="SCHEDULE 13D/A",
            filers=["Elliott Investment Management, L.P."],
            percent=5.0,
        ),
        _stake(company="AmendBad", form="SCHEDULE 13D/A", filers=["Random Holdco LLC"], percent=99.0),
    ]
    result = own.select_13d(stakes)
    companies = [s["company"] for s in result]
    assert "NewCo" in companies
    assert "AmendGood" in companies
    assert "AmendBad" not in companies


def test_select_13d_matches_elliott_investment_management_lp():
    stakes = [_stake(company="X", form="SCHEDULE 13D/A", filers=["ELLIOTT INVESTMENT MANAGEMENT, L.P."])]
    result = own.select_13d(stakes)
    assert [s["company"] for s in result] == ["X"]


def test_select_13d_word_boundary_not_substring_janacek_does_not_match_jana():
    stakes = [_stake(company="X", form="SCHEDULE 13D/A", filers=["Janacek Capital"])]
    result = own.select_13d(stakes)
    assert result == []


def test_select_13d_multiword_pershing_square_matches():
    stakes = [_stake(company="X", form="SCHEDULE 13D/A", filers=["Pershing Square Capital Management"])]
    result = own.select_13d(stakes)
    assert [s["company"] for s in result] == ["X"]


def test_select_13d_new_filings_rank_above_amendments_regardless_of_percent():
    stakes = [
        _stake(company="LowNew", form="SCHEDULE 13D", percent=5.0),
        _stake(company="HighAmend", form="SCHEDULE 13D/A", percent=99.0, filers=["Icahn Associates"]),
    ]
    result = own.select_13d(stakes)
    assert [s["company"] for s in result] == ["LowNew", "HighAmend"]


def test_select_13d_caps_at_four():
    stakes = [
        _stake(company="A", form="SCHEDULE 13D", percent=5.0),
        _stake(company="B", form="SCHEDULE 13D", percent=4.0),
        _stake(company="C", form="SCHEDULE 13D", percent=3.0),
        _stake(company="D", form="SCHEDULE 13D", percent=2.0),
        _stake(company="E", form="SCHEDULE 13D", percent=1.0),
    ]
    result = own.select_13d(stakes)
    assert [s["company"] for s in result] == ["A", "B", "C", "D"]


def test_select_13d_malformed_row_missing_percent_ranks_last_without_raising():
    stakes = [
        {"company": "NoPercent", "form": "SCHEDULE 13D", "filers": []},
        _stake(company="HasPercent", form="SCHEDULE 13D", percent=1.0),
    ]
    result = own.select_13d(stakes)
    assert [s["company"] for s in result] == ["HasPercent", "NoPercent"]


def test_select_13d_does_not_mutate_input_stakes():
    # Deep copy for the same reason as test_select_insider_does_not_mutate_input_rows:
    # `list(stakes)` would share dict objects and miss an in-place field mutation.
    stakes = [_stake(company="A"), _stake(company="B")]
    original = copy.deepcopy(stakes)
    own.select_13d(stakes)
    assert stakes == original


# --------------------------------------------------------------------------- #
# Network layer (Task 3) — fully offline. Every test below hands a fake
# `session` object into the network functions; none touches the real network.
# --------------------------------------------------------------------------- #
class _Resp:
    """Fake `requests` response: `.text` body, `.raise_for_status()` raises
    above 400."""

    def __init__(self, text="", status=200):
        self.text = text
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise RuntimeError(f"{self._status} error")


class _JsonResp:
    """Fake `requests` response for JSON endpoints (submissions.json)."""

    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise RuntimeError(f"{self._status} error")

    def json(self):
        return self._payload


def _index_text(rows):
    """rows: list of (form, company, cik, yyyymmdd, path) -> a synthetic daily
    index body with the 9-line header `parse_daily_index` skips."""
    header = "\n".join(f"header line {i}" for i in range(9))
    body = "\n".join(
        f"{form}  {company}  {cik}  {filed}  {path}" for form, company, cik, filed, path in rows
    )
    return header + "\n" + body + "\n"


def _form4_xml(ticker, company, person, code="P", shares=1000, price=100.0,
                planned="false", is_director="1"):
    return f"""<ownershipDocument>
  <issuer>
    <issuerTradingSymbol>{ticker}</issuerTradingSymbol>
    <issuerName>{company}</issuerName>
  </issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>{person}</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>{is_director}</isDirector></reportingOwnerRelationship>
  </reportingOwner>
  <aff10b5One>{planned}</aff10b5One>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
      </transactionAmounts>
      <transactionDate><value>2026-07-31</value></transactionDate>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""


def _schedule13d_xml(issuer_name, issuer_cik, filer_name, percent=10.0, shares=1000.0,
                      event_date="01/02/2026", purpose=""):
    purpose_xml = (
        f"<item4><transactionPurpose>{purpose}</transactionPurpose></item4>" if purpose else ""
    )
    return f"""<edgarSubmission>
  <formData>
    <coverPageHeader>
      <dateOfEvent>{event_date}</dateOfEvent>
      <issuerInfo>
        <issuerCIK>{issuer_cik}</issuerCIK>
        <issuerName>{issuer_name}</issuerName>
      </issuerInfo>
    </coverPageHeader>
    <reportingPersons>
      <reportingPersonInfo>
        <reportingPersonName>{filer_name}</reportingPersonName>
        <aggregateAmountOwned>{shares}</aggregateAmountOwned>
        <percentOfClass>{percent}</percentOfClass>
      </reportingPersonInfo>
    </reportingPersons>
    {purpose_xml}
  </formData>
</edgarSubmission>"""


# ---- fetch_daily_index ---- #
def test_fetch_daily_index_builds_correct_url_with_quarter():
    good = _index_text([
        ("4", "Some Co", "1234567", "20260731", "edgar/data/1234567/0001234567-26-000001.txt"),
    ])

    class _Sess:
        def __init__(self):
            self.urls = []

        def get(self, url, headers=None, timeout=None):
            self.urls.append(url)
            return _Resp(good)

    sess = _Sess()
    health = {}
    rows = own.fetch_daily_index("2026-07-31", sess, health)
    assert sess.urls == [
        "https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/form.20260731.idx"
    ]
    assert rows and rows[0]["cik"] == "1234567"
    assert health["ownership_index"] == {"ok": True, "note": ""}


def test_fetch_daily_index_falls_back_to_previous_business_day_on_404():
    good = _index_text([
        ("4", "Some Co", "1234567", "20260730", "edgar/data/1234567/0001234567-26-000001.txt"),
    ])
    good_url = own._daily_index_url("2026-07-30")

    class _Sess:
        def __init__(self):
            self.urls = []

        def get(self, url, headers=None, timeout=None):
            self.urls.append(url)
            return _Resp(good) if url == good_url else _Resp(status=404)

    sess = _Sess()
    health = {}
    rows = own.fetch_daily_index("2026-07-31", sess, health)
    assert rows
    assert sess.urls == [own._daily_index_url("2026-07-31"), good_url]
    assert health["ownership_index"] == {"ok": True, "note": "fell back to 2026-07-30"}


def test_prev_business_day_skips_saturday_and_sunday():
    """Direct unit test of the skip logic itself. The earlier network-level
    test asserted `url not in weekend_urls` *inside* `session.get`, where a
    raised AssertionError is swallowed by `fetch_daily_index`'s blanket
    `except Exception` -- the reviewer proved that test stayed green even
    with the `while d.weekday() >= 5` loop deleted entirely, since the same
    4-attempt budget happens to land on the correct final date either way.
    This test cannot be fooled that way: it calls the pure function and
    checks its return value directly."""
    assert own._prev_business_day("2026-08-03") == "2026-07-31"  # Monday -> prior Friday
    assert own._prev_business_day("2026-08-02") == "2026-07-31"  # Sunday -> prior Friday
    assert own._prev_business_day("2026-08-01") == "2026-07-31"  # Saturday -> prior Friday
    assert own._prev_business_day("2026-07-31") == "2026-07-30"  # Friday -> Thursday, no skip


def test_fetch_daily_index_fallback_skips_saturday_and_sunday():
    good = _index_text([
        ("4", "Some Co", "1234567", "20260731", "edgar/data/1234567/0001234567-26-000001.txt"),
    ])
    good_url = own._daily_index_url("2026-07-31")
    weekend_urls = {own._daily_index_url("2026-08-01"), own._daily_index_url("2026-08-02")}

    class _Sess:
        def __init__(self):
            self.urls = []

        def get(self, url, headers=None, timeout=None):
            # Record BEFORE any assertion: fetch_daily_index's blanket
            # `except Exception` would otherwise swallow an AssertionError
            # raised here, and the test would stay green regardless of what
            # URLs were actually requested (see the reviewer's finding on
            # the previous version of this test).
            self.urls.append(url)
            return _Resp(good) if url == good_url else _Resp(status=404)

    sess = _Sess()
    health = {}
    rows = own.fetch_daily_index("2026-08-03", sess, health)
    assert rows
    assert not (set(sess.urls) & weekend_urls), f"weekend URL requested: {sess.urls}"
    assert sess.urls == [own._daily_index_url("2026-08-03"), good_url]
    assert health["ownership_index"] == {"ok": True, "note": "fell back to 2026-07-31"}


def test_fetch_daily_index_exhausts_six_attempts_then_returns_empty_and_ok_false():
    class _Sess:
        def __init__(self):
            self.urls = []

        def get(self, url, headers=None, timeout=None):
            self.urls.append(url)
            return _Resp(status=404)

    sess = _Sess()
    health = {}
    rows = own.fetch_daily_index("2026-08-05", sess, health)
    assert rows == []
    assert health["ownership_index"]["ok"] is False
    assert isinstance(health["ownership_index"]["note"], str) and health["ownership_index"]["note"]
    # Exactly 6 attempts, walking back one business day at a time (Wed -> Tue
    # -> Mon -> skip Sun/Sat -> Fri -> Thu -> Wed). Six, not four: the cron
    # runs 07:00 JST = 18:00 ET the previous day and the index publishes
    # ~22:00 ET, so the first TWO attempts are guaranteed 404s on every single
    # run. Four attempts left only two business days of real headroom.
    # Pins the attempt budget: a range(4) or range(9) mutation must fail this.
    assert sess.urls == [
        own._daily_index_url("2026-08-05"),
        own._daily_index_url("2026-08-04"),
        own._daily_index_url("2026-08-03"),
        own._daily_index_url("2026-07-31"),
        own._daily_index_url("2026-07-30"),
        own._daily_index_url("2026-07-29"),
    ]
    assert own._INDEX_FALLBACK_ATTEMPTS == 6


def test_every_request_carries_sec_user_agent():
    good = _index_text([
        ("4", "Some Co", "1234567", "20260731", "edgar/data/1234567/0001234567-26-000001.txt"),
    ])
    captured = []

    class _Sess:
        def get(self, url, headers=None, timeout=None):
            captured.append(headers)
            return _Resp(good)

    sess = _Sess()
    own.fetch_daily_index("2026-07-31", sess, {})
    own.fetch_doc("edgar/data/1234567/doc.htm", sess)
    assert captured
    assert all(h == {"User-Agent": own._sec_ua()} for h in captured)


# ---- fetch_doc ---- #
def test_fetch_doc_returns_empty_string_on_exception():
    class _Sess:
        def get(self, url, headers=None, timeout=None):
            raise ConnectionError("offline")

    assert own.fetch_doc("edgar/data/1/doc.htm", _Sess()) == ""


def test_fetch_doc_returns_body_text_on_success():
    class _Sess:
        def get(self, url, headers=None, timeout=None):
            assert url == own.ARCHIVES_BASE + "edgar/data/1/doc.htm"
            return _Resp("body text")

    assert own.fetch_doc("edgar/data/1/doc.htm", _Sess()) == "body text"


# ---- fetch_prior_13d ---- #
def test_fetch_prior_13d_returns_none_on_failure():
    class _Sess:
        def get(self, url, headers=None, timeout=None):
            raise ConnectionError("offline")

    assert own.fetch_prior_13d(
        "0001982444", "Elliott Investment Management", "0001982444-26-000200",
        _Sess(), own._now() + 60) is None


def test_fetch_prior_13d_returns_none_when_filer_never_matches(monkeypatch):
    subs = {
        "filings": {
            "recent": {
                "form": ["SCHEDULE 13D", "10-K"],
                "accessionNumber": ["0001982444-26-000200", "0001982444-25-000050"],
                "primaryDocument": ["primary_doc.xml", "doc.htm"],
                "filingDate": ["2026-07-31", "2025-03-01"],
            }
        }
    }

    class _Sess:
        def get(self, url, headers=None, timeout=None):
            return _JsonResp(subs)

    monkeypatch.setattr(
        own, "fetch_doc",
        lambda path, session: _schedule13d_xml("Issuer", "1982444", "Some Other Filer LLC", percent=8.5),
    )
    result = own.fetch_prior_13d(
        "0001982444", "Elliott Investment Management, L.P.", "0001982444-26-000200",
        _Sess(), own._now() + 60)
    assert result is None


def test_fetch_prior_13d_finds_immediately_previous_filing_and_skips_current_by_accession(monkeypatch):
    """The current filing is skipped by matching `current_accession` exactly
    (dashes-normalized), not by assuming the newest form-matching entry is
    the one being processed."""
    subs = {
        "filings": {
            "recent": {
                "form": ["SCHEDULE 13D/A", "SCHEDULE 13D", "10-K"],
                "accessionNumber": [
                    "0001982444-26-000200", "0001982444-25-000100", "0001982444-25-000050",
                ],
                "primaryDocument": ["primary_doc.xml", "primary_doc.xml", "doc.htm"],
                "filingDate": ["2026-07-31", "2025-01-15", "2025-03-01"],
            }
        }
    }
    docs = {
        "edgar/data/1982444/000198244426000200/primary_doc.xml":
            _schedule13d_xml("Issuer", "1982444", "Elliott Investment Management, L.P.", percent=8.5),
        "edgar/data/1982444/000198244425000100/primary_doc.xml":
            _schedule13d_xml("Issuer", "1982444", "Elliott Investment Management, L.P.", percent=5.1),
    }

    class _Sess:
        def get(self, url, headers=None, timeout=None):
            assert headers == {"User-Agent": own._sec_ua()}
            assert url == own.SUBMISSIONS_URL.format(cik="0001982444")
            return _JsonResp(subs)

    monkeypatch.setattr(own, "fetch_doc", lambda path, session: docs.get(path, ""))

    result = own.fetch_prior_13d(
        "0001982444", "Elliott Investment Management, L.P.", "0001982444-26-000200",
        _Sess(), own._now() + 60)
    assert result == 5.1


def test_fetch_prior_13d_does_not_skip_the_true_prior_when_current_is_not_yet_indexed(monkeypatch):
    """Regression: EDGAR's submissions index can lag the daily index. When
    the filing currently being processed is NOT present in `filings.recent`
    yet, the newest form-matching entry IS the true prior filing and must
    not be skipped. The old heuristic (always skip the first match) would
    wrongly return None here (or, with more history, the percentage from two
    filings back)."""
    subs = {
        "filings": {
            "recent": {
                "form": ["SCHEDULE 13D", "10-K"],
                "accessionNumber": ["0001982444-25-000100", "0001982444-25-000050"],
                "primaryDocument": ["primary_doc.xml", "doc.htm"],
                "filingDate": ["2025-01-15", "2025-03-01"],
            }
        }
    }
    docs = {
        "edgar/data/1982444/000198244425000100/primary_doc.xml":
            _schedule13d_xml("Issuer", "1982444", "Elliott Investment Management, L.P.", percent=5.1),
    }

    class _Sess:
        def get(self, url, headers=None, timeout=None):
            return _JsonResp(subs)

    monkeypatch.setattr(own, "fetch_doc", lambda path, session: docs.get(path, ""))

    # "0001982444-26-000200" -- the filing currently being processed -- is
    # not in the submissions history above (EDGAR lag).
    result = own.fetch_prior_13d(
        "0001982444", "Elliott Investment Management, L.P.", "0001982444-26-000200",
        _Sess(), own._now() + 60)
    assert result == 5.1


def test_fetch_prior_13d_caps_the_candidate_walk_at_eight(monkeypatch):
    n = 12
    subs = {
        "filings": {
            "recent": {
                "form": ["SCHEDULE 13D"] * n,
                "accessionNumber": [f"0001982444-25-{i:06d}" for i in range(n)],
                "primaryDocument": ["primary_doc.xml"] * n,
                "filingDate": ["2025-01-01"] * n,
            }
        }
    }

    class _Sess:
        def get(self, url, headers=None, timeout=None):
            return _JsonResp(subs)

    fetch_calls = []

    def _fake_fetch_doc(path, session):
        fetch_calls.append(path)
        # No candidate ever matches the filer, so the walk runs to the cap.
        return _schedule13d_xml("Issuer", "1982444", "Some Other Filer LLC", percent=1.0)

    monkeypatch.setattr(own, "fetch_doc", _fake_fetch_doc)

    result = own.fetch_prior_13d(
        "0001982444", "Elliott Investment Management, L.P.", "not-present",
        _Sess(), own._now() + 60)
    assert result is None
    assert len(fetch_calls) == own._PRIOR_13D_CANDIDATE_CAP


def test_fetch_prior_13d_bails_when_deadline_already_passed(monkeypatch):
    subs = {
        "filings": {
            "recent": {
                "form": ["SCHEDULE 13D"],
                "accessionNumber": ["0001982444-25-000100"],
                "primaryDocument": ["primary_doc.xml"],
                "filingDate": ["2025-01-01"],
            }
        }
    }

    class _Sess:
        def get(self, url, headers=None, timeout=None):
            return _JsonResp(subs)

    fetch_calls = []
    monkeypatch.setattr(own, "fetch_doc", lambda path, session: fetch_calls.append(path) or "")

    result = own.fetch_prior_13d(
        "0001982444", "Elliott Investment Management, L.P.", "not-present",
        _Sess(), own._now() - 1)
    assert result is None
    assert fetch_calls == []


def test_lane_deadline_s_is_90_seconds():
    assert own.LANE_DEADLINE_S == 90.0


# ---- build_ownership ---- #
def test_build_ownership_monotonic_deadline_does_not_immediately_truncate(monkeypatch):
    """Regression: `deadline_at` must be compared against the same monotonic
    clock `own._now()` (== `sec_fundamentals._now()`) derives it from, not
    `time.time()`. `time.time()` is a ~2026-epoch Unix timestamp (~1.79e9);
    `time.monotonic()` is a small number counting up from an arbitrary
    process-start origin. If the scan compares a `_now()`-based `deadline_at`
    against `time.time()`, `time.time() >= deadline_at` is true on the very
    first check of every run, so the market-wide scan truncates before
    reading a single document -- forever."""
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    idx_rows = [
        {"form": "4", "company": f"Co{i}", "cik": str(4000 + i), "filed": "2026-07-31",
         "path": f"edgar/data/{4000 + i}/doc{i}.txt"}
        for i in range(5)
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)
    monkeypatch.setattr(
        own, "fetch_doc",
        lambda path, session: _form4_xml("TICK", "Some Co", "PERSON", code="A", shares=10, price=0.0),
    )

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() + 60)
    assert bundle["scanned"]["truncated"] is False
    assert bundle["scanned"]["form4_read"] == 5


def test_build_ownership_scan_deadline_enforced_mid_flight(monkeypatch):
    """Critical regression (reviewer-measured): `executor.submit` does not
    block, so a "submit every row, then wait for all of them" loop drains
    in microseconds -- every row is in flight before the deadline check can
    ever fire a second time. Reproduces the reviewer's own numbers: 60 rows,
    ~30ms simulated fetch latency, a 90ms budget. Against the old
    submit-everything implementation this took ~300ms (60 rows / 6 workers *
    30ms) with `truncated=False` and all 60 read; a correctly deadline-aware
    rolling-window scan must stop feeding new work once the budget passes and
    finish close to it."""
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    n = 60
    idx_rows = [
        {"form": "4", "company": f"Co{i}", "cik": str(5000 + i), "filed": "2026-07-31",
         "path": f"edgar/data/{5000 + i}/doc{i}.txt"}
        for i in range(n)
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)

    def _slow_fetch_doc(path, session):
        own.time.sleep(0.03)
        return _form4_xml("TICK", "Some Co", "PERSON", code="A", shares=10, price=0.0)

    monkeypatch.setattr(own, "fetch_doc", _slow_fetch_doc)

    health = {}
    start = own._now()
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() + 0.09)
    elapsed = own._now() - start

    assert elapsed < 0.2, f"scan overran the deadline by too much: {elapsed:.3f}s (budget 0.09s)"
    assert bundle["scanned"]["truncated"] is True
    assert bundle["scanned"]["form4_read"] < n


def test_build_ownership_empty_index_returns_full_contract(monkeypatch):
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: [])
    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() + 60)
    assert bundle == {
        "insider_watchlist": [], "insider_buys": [], "activist_stakes": [],
        "scanned": {"form4_total": 0, "form4_read": 0, "truncated": False,
                    "index_date": ""},
    }
    # All three health keys are written on this path -- see
    # test_build_ownership_empty_index_writes_all_three_health_keys.
    assert health["ownership_form4"]["ok"] is False
    assert health["ownership_13d"]["ok"] is False


def test_build_ownership_routes_watchlist_and_market_wide_correctly(monkeypatch):
    monkeypatch.setattr(
        earnings_report, "_load_cik_map",
        lambda session: {"AAPL": "0000320193", "MSFT": "0000789019"},
    )
    idx_rows = [
        {"form": "4", "company": "Apple", "cik": "320193", "filed": "2026-07-31",
         "path": "edgar/data/320193/apple.txt"},
        {"form": "4", "company": "Unknown Co", "cik": "9999999999", "filed": "2026-07-31",
         "path": "edgar/data/9999999999/unknown.txt"},
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)

    docs = {
        "edgar/data/320193/apple.txt": _form4_xml(
            "AAPL", "Apple Inc", "COOK TIM", code="S", shares=100000, price=200.0),
        "edgar/data/9999999999/unknown.txt": _form4_xml(
            "ZZZZ", "Unknown Co", "SMITH JOHN", code="P", shares=100000, price=50.0),
    }
    monkeypatch.setattr(own, "fetch_doc", lambda path, session: docs.get(path, ""))

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() + 60)

    assert [r["ticker"] for r in bundle["insider_watchlist"]] == ["AAPL"]
    assert [r["ticker"] for r in bundle["insider_buys"]] == ["ZZZZ"]
    assert bundle["scanned"] == {"form4_total": 2, "form4_read": 2, "truncated": False,
                                 "index_date": "2026-07-31"}
    assert health["ownership_form4"] == {"ok": True, "note": ""}


def test_build_ownership_form4_health_false_on_total_parse_blackout(monkeypatch):
    """A blanket failure (e.g. SEC 403ing every request because `_SEC_UA` got
    dropped somewhere) must not read as a healthy, quiet day: many documents
    attempted, zero transactions parsed out of any of them."""
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    idx_rows = [
        {"form": "4", "company": f"Co{i}", "cik": str(9000 + i), "filed": "2026-07-31",
         "path": f"edgar/data/{9000 + i}/doc{i}.txt"}
        for i in range(5)
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)
    monkeypatch.setattr(own, "fetch_doc", lambda path, session: "")  # every fetch "fails"

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() + 60)

    assert bundle["scanned"]["form4_read"] == 5
    assert health["ownership_form4"]["ok"] is False
    assert "5" in health["ownership_form4"]["note"]


def test_build_ownership_form4_health_true_when_nothing_to_attempt(monkeypatch):
    """A day with only 13D filings and no Form 4 rows at all is a legitimately
    healthy, quiet day for the Form 4 leg -- distinct from attempting
    documents and parsing none of them."""
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    idx_rows = [
        {"form": "SCHEDULE 13D", "company": "Issuer", "cik": "1",
         "filed": "2026-07-31", "path": "edgar/data/1/0000000001-26-000001.txt"},
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)
    monkeypatch.setattr(
        own, "fetch_doc",
        lambda path, session: _schedule13d_xml("Issuer", "1", "New Filer LLC", percent=5.0),
    )

    health = {}
    own.build_ownership(object(), health, "2026-07-31", own._now() + 60)
    assert health["ownership_form4"] == {"ok": True, "note": ""}


def test_build_ownership_13d_health_false_on_total_parse_blackout(monkeypatch):
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    idx_rows = [
        {"form": "SCHEDULE 13D", "company": f"Issuer{i}", "cik": str(9500 + i),
         "filed": "2026-07-31", "path": f"edgar/data/{9500 + i}/000{9500 + i}-26-000001.txt"}
        for i in range(3)
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)
    monkeypatch.setattr(own, "fetch_doc", lambda path, session: "")

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() + 60)

    # Every fetch failed, so every parse degraded to the blank shell -- no
    # real company name came through any of the 3 attempted filings.
    assert all(not s.get("company") for s in bundle["activist_stakes"])
    assert health["ownership_13d"]["ok"] is False
    assert "3" in health["ownership_13d"]["note"]


def test_build_ownership_13d_health_true_when_nothing_to_attempt(monkeypatch):
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {"AAPL": "0000320193"})
    idx_rows = [
        {"form": "4", "company": "Apple", "cik": "320193", "filed": "2026-07-31",
         "path": "edgar/data/320193/apple.txt"},
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)
    monkeypatch.setattr(
        own, "fetch_doc",
        lambda path, session: _form4_xml("AAPL", "Apple Inc", "COOK TIM"),
    )

    health = {}
    own.build_ownership(object(), health, "2026-07-31", own._now() + 60)
    assert health["ownership_13d"] == {"ok": True, "note": ""}


def test_build_ownership_insider_rows_carry_the_filing_index_url(monkeypatch):
    """Company name links to the human-facing SEC filing index page, not the
    raw `.txt` or the bare structured document (both render as unreadable
    source in a browser). One watchlist row (served serially) and one
    market-wide row (served through the thread pool) so both fetch paths are
    covered."""
    monkeypatch.setattr(
        earnings_report, "_load_cik_map", lambda session: {"AAPL": "0000320193"})
    idx_rows = [
        {"form": "4", "company": "Apple", "cik": "320193", "filed": "2026-07-31",
         "path": "edgar/data/1821468/0001193125-26-326663.txt"},
        {"form": "4", "company": "Unknown Co", "cik": "9999999999", "filed": "2026-07-31",
         "path": "edgar/data/1821469/0001193125-26-326664.txt"},
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)
    docs = {
        "edgar/data/1821468/0001193125-26-326663.txt": _form4_xml(
            "AAPL", "Apple Inc", "COOK TIM", code="S", shares=100000, price=200.0),
        "edgar/data/1821469/0001193125-26-326664.txt": _form4_xml(
            "ZZZZ", "Unknown Co", "SMITH JOHN", code="P", shares=100000, price=50.0),
    }
    monkeypatch.setattr(own, "fetch_doc", lambda path, session: docs.get(path, ""))

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() + 60)

    assert bundle["insider_watchlist"][0]["url"] == (
        "https://www.sec.gov/Archives/edgar/data/1821468/"
        "000119312526326663/0001193125-26-326663-index.htm"
    )
    assert bundle["insider_buys"][0]["url"] == (
        "https://www.sec.gov/Archives/edgar/data/1821469/"
        "000119312526326664/0001193125-26-326664-index.htm"
    )


def test_build_ownership_deadline_already_passed_reads_nothing_and_truncates(monkeypatch):
    """A `deadline_at` already in the past means the lane has no budget at
    all, and EVERY leg honours it -- including the two serial ones, which used
    to run unbounded (see test_build_ownership_watchlist_leg_stops_at_the_
    deadline). The bundle still comes back whole; a deadline overrun is a
    degraded result, never an error."""
    monkeypatch.setattr(
        earnings_report, "_load_cik_map", lambda session: {"AAPL": "0000320193"})
    idx_rows = [
        {"form": "4", "company": "Apple", "cik": "320193", "filed": "2026-07-31",
         "path": "edgar/data/320193/apple.txt"},
        {"form": "4", "company": "Unknown Co", "cik": "9999999999", "filed": "2026-07-31",
         "path": "edgar/data/9999999999/unknown.txt"},
        {"form": "SCHEDULE 13D", "company": "Some Issuer", "cik": "1982444",
         "filed": "2026-07-31", "path": "edgar/data/1982444/0001982444-26-000123.txt"},
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)

    fetched = []
    monkeypatch.setattr(own, "fetch_doc", lambda path, session: fetched.append(path) or "")

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() - 100)

    assert fetched == []
    assert bundle["insider_watchlist"] == []
    assert bundle["activist_stakes"] == []
    assert bundle["scanned"]["truncated"] is True
    assert bundle["scanned"]["form4_read"] == 0
    assert bundle["scanned"]["form4_total"] == 2
    # A leg that read NOTHING because it ran out of budget is not a healthy,
    # quiet day. `_leg_health`'s "attempted 0 is fine" rule is about a day with
    # nothing to fetch; without the truncation guard both keys would report
    # ok: True here and run.py would record a false `last_ok` for the lane.
    assert health["ownership_form4"]["ok"] is False
    assert "deadline" in health["ownership_form4"]["note"]
    assert health["ownership_13d"]["ok"] is False
    assert "deadline" in health["ownership_13d"]["note"]


def test_build_ownership_leg_health_stays_true_on_a_genuinely_quiet_day(monkeypatch):
    """The truncation guard above must not turn "nothing to fetch" into a
    failure: a day with only 13D filings is still a healthy day for the Form 4
    leg, and vice versa. Only `attempted == 0` WITH that leg truncated is a
    failure."""
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    idx_rows = [
        _idx_row("SCHEDULE 13D", "1982444", "edgar/data/1982444/0001982444-26-000123.txt"),
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)
    monkeypatch.setattr(
        own, "fetch_doc",
        lambda path, session: _schedule13d_xml("Some Issuer", "1982444", "New Filer LLC",
                                               percent=6.0),
    )

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() + 60)

    assert bundle["scanned"]["truncated"] is False
    assert health["ownership_form4"] == {"ok": True, "note": ""}
    assert health["ownership_13d"] == {"ok": True, "note": ""}


def test_build_ownership_scanned_counts_reflect_reads_including_failures(monkeypatch):
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    idx_rows = [
        {"form": "4", "company": f"Co{i}", "cik": str(1000 + i), "filed": "2026-07-31",
         "path": f"edgar/data/{1000 + i}/doc{i}.txt"}
        for i in range(5)
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)

    def _fake_fetch_doc(path, session):
        if path.endswith("doc3.txt"):
            return ""  # simulates a failed / unparseable fetch
        return _form4_xml("TICK", "Some Co", "PERSON", code="A", shares=10, price=0.0)

    monkeypatch.setattr(own, "fetch_doc", _fake_fetch_doc)

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() + 60)
    assert bundle["scanned"] == {"form4_total": 5, "form4_read": 5, "truncated": False,
                                 "index_date": "2026-07-31"}


def test_build_ownership_scan_uses_a_distinct_session_per_worker_thread(monkeypatch):
    """Hazard #1: `requests.Session` is not documented thread-safe. Each
    worker must get its own session, never the caller's."""
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    idx_rows = [
        {"form": "4", "company": f"Co{i}", "cik": str(2000 + i), "filed": "2026-07-31",
         "path": f"edgar/data/{2000 + i}/doc{i}.txt"}
        for i in range(12)
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)

    caller_session = object()
    created_sessions = []
    lock = own.threading.Lock()

    class _WorkerSession:
        def get(self, url, headers=None, timeout=None):
            return _Resp(_form4_xml("TICK", "Some Co", "PERSON"))

    def _fake_new_session():
        sess = _WorkerSession()
        with lock:
            created_sessions.append(sess)
        return sess

    monkeypatch.setattr(own, "_new_session", _fake_new_session)

    seen_sessions = []
    real_fetch_doc = own.fetch_doc

    def _tracking_fetch_doc(path, session):
        assert session is not caller_session
        seen_sessions.append(session)
        return real_fetch_doc(path, session)

    monkeypatch.setattr(own, "fetch_doc", _tracking_fetch_doc)

    health = {}
    bundle = own.build_ownership(caller_session, health, "2026-07-31", own._now() + 60)

    assert bundle["scanned"]["form4_read"] == 12
    assert created_sessions  # at least one per-thread session was created
    assert all(isinstance(s, _WorkerSession) for s in seen_sessions)


def test_build_ownership_cik_map_failure_still_returns_market_wide_leg(monkeypatch):
    def _boom(session):
        raise RuntimeError("SEC company_tickers.json unreachable")

    monkeypatch.setattr(earnings_report, "_load_cik_map", _boom)
    idx_rows = [
        {"form": "4", "company": "Some Co", "cik": "5555555", "filed": "2026-07-31",
         "path": "edgar/data/5555555/doc.txt"},
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)
    monkeypatch.setattr(
        own, "fetch_doc",
        lambda path, session: _form4_xml(
            "XYZ", "Some Co", "SMITH JOHN", code="P", shares=100000, price=50.0),
    )

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() + 60)
    assert bundle["scanned"] == {"form4_total": 1, "form4_read": 1, "truncated": False,
                                 "index_date": "2026-07-31"}
    assert [r["ticker"] for r in bundle["insider_buys"]] == ["XYZ"]


def test_build_ownership_13d_doc_path_strips_dashes_from_accession(monkeypatch):
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    idx_rows = [
        {"form": "SCHEDULE 13D", "company": "Some Issuer", "cik": "1982444",
         "filed": "2026-07-31", "path": "edgar/data/1982444/0001982444-26-000123.txt"},
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)

    requested_paths = []

    def _fake_fetch_doc(path, session):
        requested_paths.append(path)
        return _schedule13d_xml("Some Issuer", "1982444", "New Filer LLC", percent=10.0)

    monkeypatch.setattr(own, "fetch_doc", _fake_fetch_doc)

    health = {}
    own.build_ownership(object(), health, "2026-07-31", own._now() + 60)

    assert requested_paths == ["edgar/data/1982444/000198244426000123/primary_doc.xml"]


def test_build_ownership_13d_stakes_carry_index_url_and_empty_ticker(monkeypatch):
    """`activist_stakes` rows must carry `url` (the human-facing filing index
    page, same construction as the Form 4 legs) and `ticker` -- the contract
    shape requires the key even though the 13D XML has no ticker to offer
    (only `issuerCusipNumber`), and the renderer never displays it."""
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    idx_rows = [
        {"form": "SCHEDULE 13D", "company": "Some Issuer", "cik": "1982444",
         "filed": "2026-07-31", "path": "edgar/data/1982444/0001982444-26-000123.txt"},
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)
    monkeypatch.setattr(
        own, "fetch_doc",
        lambda path, session: _schedule13d_xml("Some Issuer", "1982444", "New Filer LLC", percent=10.0),
    )

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() + 60)

    assert bundle["activist_stakes"][0]["url"] == (
        "https://www.sec.gov/Archives/edgar/data/1982444/"
        "000198244426000123/0001982444-26-000123-index.htm"
    )
    assert bundle["activist_stakes"][0]["ticker"] == ""


def test_build_ownership_prior_13d_lookup_budget_is_two(monkeypatch):
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    idx_rows = [
        {"form": "SCHEDULE 13D/A", "company": f"Issuer{i}", "cik": str(3000 + i),
         "filed": "2026-07-31", "path": f"edgar/data/{3000 + i}/000{3000 + i}-26-0001{i:02d}.txt"}
        for i in range(3)
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)

    def _fake_fetch_doc(path, session):
        idx = path.split("/")[2]
        return _schedule13d_xml(
            f"Issuer for {idx}", idx, "Elliott Investment Management, L.P.", percent=10.0)

    monkeypatch.setattr(own, "fetch_doc", _fake_fetch_doc)

    calls = []

    def _fake_prior(issuer_cik, filer, accession, session, deadline_at):
        calls.append((issuer_cik, filer, accession))
        return 1.0

    monkeypatch.setattr(own, "fetch_prior_13d", _fake_prior)

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() + 60)
    assert len(calls) == 2
    # The daily index's own accession is threaded through to fetch_prior_13d
    # so it can skip the current filing exactly, not heuristically.
    assert all(accession for _cik, _filer, accession in calls)
    amendments = [s for s in bundle["activist_stakes"] if s["form"].endswith("/A")]
    assert len(amendments) == 3
    prior_values = [s["prior_percent"] for s in amendments]
    assert prior_values.count(1.0) == 2
    assert prior_values.count(None) == 1
    # The internal _accession scratch key must never leak into the published
    # bundle -- the contract shape is exactly what the spec/renderer expect.
    assert all("_accession" not in s for s in bundle["activist_stakes"])


# --------------------------------------------------------------------------- #
# Final whole-branch review — fix wave.
#
# These cover the seams *between* layers that the per-task reviews each saw
# only one side of. Every test below is offline.
# --------------------------------------------------------------------------- #
def _idx_row(form, cik, path, filed="2026-07-31", company="Co"):
    return {"form": form, "company": company, "cik": cik, "filed": filed, "path": path}


# ---- 1. dedupe the daily index by accession ---- #
def test_build_ownership_dedupes_form4_index_rows_by_accession(monkeypatch):
    """CRITICAL regression. EDGAR's daily index carries one row per filer CIK,
    not per filing: a Form 4 is listed once under the issuer and once per
    reporting owner, and every row points at the SAME document. Measured on
    the real 2026-07-31 index: 712 Form 4 lines = 333 filings (2.14x), with
    group filings reaching 8 copies. Undeduped, the same transaction is
    fetched, parsed and ranked once per row -- one real filing rendered
    `Occidental Petroleum - Hollub Vicki A` twice and consumed 2 of BUY_CAP's
    3 slots."""
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    idx_rows = [
        _idx_row("4", "1821468", "edgar/data/1821468/0001193125-26-326663.txt"),
        _idx_row("4", "1843196", "edgar/data/1843196/0001193125-26-326663.txt"),
        _idx_row("4", "2098", "edgar/data/2098/0001193125-26-328699.txt"),
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)

    fetched = []
    lock = own.threading.Lock()

    def _fake_fetch_doc(path, session):
        with lock:
            fetched.append(path)
        if "326663" in path:
            return _form4_xml("OXY", "Occidental Petroleum Corp", "Hollub Vicki A",
                              code="P", shares=100000, price=125.0)
        return _form4_xml("ACU", "Acme United Corp", "Smith John",
                          code="P", shares=100000, price=50.0)

    monkeypatch.setattr(own, "fetch_doc", _fake_fetch_doc)

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() + 60)

    assert len(fetched) == 2, f"the duplicated filing was fetched twice: {fetched}"
    assert bundle["scanned"]["form4_total"] == 2
    assert bundle["scanned"]["form4_read"] == 2
    # One slot per filing, not one per index row.
    assert sorted(r["ticker"] for r in bundle["insider_buys"]) == ["ACU", "OXY"]


def test_build_ownership_dedupe_prefers_the_watchlist_cik_row_even_when_second(monkeypatch):
    """When several index rows share an accession, the survivor must be the
    one whose CIK is in the watchlist set -- otherwise leg A's pre-selection
    silently loses watchlist filings whose issuer row happens to sort second
    (the reporting owner's CIK can come first)."""
    monkeypatch.setattr(
        earnings_report, "_load_cik_map", lambda session: {"AAPL": "0000320193"})
    idx_rows = [
        _idx_row("4", "1843196", "edgar/data/1843196/0001193125-26-326663.txt",
                 company="Cook Tim"),
        _idx_row("4", "320193", "edgar/data/320193/0001193125-26-326663.txt",
                 company="Apple Inc"),
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)

    fetched = []
    monkeypatch.setattr(
        own, "fetch_doc",
        lambda path, session: fetched.append(path) or _form4_xml(
            "AAPL", "Apple Inc", "COOK TIM", code="S", shares=100000, price=200.0),
    )

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() + 60)

    assert fetched == ["edgar/data/320193/0001193125-26-326663.txt"]
    assert [r["ticker"] for r in bundle["insider_watchlist"]] == ["AAPL"]


def test_build_ownership_dedupes_13d_rows_against_the_committed_index_fixture(monkeypatch):
    """The committed fixture reproduces the real index's one-row-per-filer-CIK
    shape: 14 SCHEDULE 13D-type lines covering 9 distinct filings (5 duplicate
    accession pairs). Both `scanned` counters and the 13D leg must count
    filings, not lines."""
    rows = own.parse_daily_index(load_fixture("daily_index.idx"))
    thirteen_d_lines = [r for r in rows if r["form"] in own._SCHEDULE_13D_FORMS]
    assert len(thirteen_d_lines) == 14  # lines, before dedupe
    assert len({own._split_index_path(r["path"])[1] for r in thirteen_d_lines}) == 9

    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: rows)

    fetched = []
    lock = own.threading.Lock()

    def _fake_fetch_doc(path, session):
        with lock:
            fetched.append(path)
        if path.endswith("primary_doc.xml"):
            return _schedule13d_xml("Some Issuer", "1", "New Filer LLC", percent=6.0)
        return _form4_xml("TICK", "Some Co", "PERSON", code="A", shares=10, price=0.0)

    monkeypatch.setattr(own, "fetch_doc", _fake_fetch_doc)

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() + 60)

    doc_paths = [p for p in fetched if p.endswith("primary_doc.xml")]
    assert len(doc_paths) == 9
    assert len(set(doc_paths)) == 9
    assert bundle["scanned"]["form4_total"] == 8
    assert bundle["scanned"]["form4_read"] == 8
    assert health["ownership_13d"] == {"ok": True, "note": ""}


# ---- 2. SEC fair-access rate limiter ---- #
def test_scan_rate_limiter_computes_ten_requests_per_second(monkeypatch):
    """Deterministic against a stubbed clock -- no wall-clock assertions. The
    first slot is free; every later one waits out the remainder of the minimum
    interval, so N requests cost (N-1) intervals."""
    clock = {"t": 1000.0}
    slept = []
    monkeypatch.setattr(own, "_now", lambda: clock["t"])

    def _fake_sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(own.time, "sleep", _fake_sleep)

    limiter = own._RateLimiter(1.0 / own.SCAN_MAX_RPS)
    for _ in range(5):
        limiter.acquire()

    assert slept == pytest.approx([0.1, 0.1, 0.1, 0.1])
    assert clock["t"] - 1000.0 == pytest.approx(0.4)


def test_scan_rate_limiter_does_not_sleep_when_the_slot_is_already_free(monkeypatch):
    """A caller that arrives after the interval has already elapsed must not
    be delayed -- the limiter is a floor on spacing, not a fixed tax."""
    clock = {"t": 1000.0}
    slept = []
    monkeypatch.setattr(own, "_now", lambda: clock["t"])
    monkeypatch.setattr(own.time, "sleep", lambda s: slept.append(s))

    limiter = own._RateLimiter(0.1)
    limiter.acquire()
    clock["t"] += 5.0
    limiter.acquire()

    assert slept == []


def test_scan_limiter_defaults_to_sec_fair_access_rate():
    assert own.SCAN_MAX_RPS == 10.0
    assert own._SCAN_MIN_INTERVAL_S == pytest.approx(1.0 / own.SCAN_MAX_RPS)
    assert own._SCAN_MIN_INTERVAL_S == pytest.approx(0.1)
    assert isinstance(own._SCAN_LIMITER, own._RateLimiter)


def test_scan_path_takes_a_rate_limit_slot_for_every_document(monkeypatch):
    """Worker count bounds concurrency, not rate: 6 workers against a fast
    host measured 137 req/s. Every market-wide document fetch must pass
    through the shared limiter."""
    monkeypatch.setattr(
        earnings_report, "_load_cik_map", lambda session: {"AAPL": "0000320193"})
    idx_rows = [_idx_row("4", "320193", "edgar/data/320193/watch.txt")]
    idx_rows += [
        _idx_row("4", str(7000 + i), f"edgar/data/{7000 + i}/doc{i}.txt")
        for i in range(10)
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)
    monkeypatch.setattr(
        own, "fetch_doc",
        lambda path, session: _form4_xml("TICK", "Some Co", "PERSON",
                                         code="A", shares=10, price=0.0),
    )

    class _CountingLimiter:
        def __init__(self):
            self.lock = own.threading.Lock()
            self.acquired = 0

        def acquire(self):
            with self.lock:
                self.acquired += 1
            return 0.0

    limiter = _CountingLimiter()
    monkeypatch.setattr(own, "_SCAN_LIMITER", limiter)

    bundle = own.build_ownership(object(), {}, "2026-07-31", own._now() + 60)

    assert bundle["scanned"]["form4_read"] == 11
    assert limiter.acquired == 10  # one per market-wide document


def test_scan_rate_limiter_throttles_across_all_worker_threads(monkeypatch):
    """The limiter is shared state touched by every worker; a per-thread or
    unlocked one would let SCAN_WORKERS parallel fetches through at once. Run
    at 50 req/s so the assertion costs a fraction of a second."""
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    n = 13
    idx_rows = [
        _idx_row("4", str(8000 + i), f"edgar/data/{8000 + i}/doc{i}.txt")
        for i in range(n)
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)
    monkeypatch.setattr(
        own, "fetch_doc",
        lambda path, session: _form4_xml("TICK", "Some Co", "PERSON",
                                         code="A", shares=10, price=0.0),
    )
    monkeypatch.setattr(own, "_SCAN_LIMITER", own._RateLimiter(0.02))

    start = own._now()
    bundle = own.build_ownership(object(), {}, "2026-07-31", own._now() + 60)
    elapsed = own._now() - start

    assert bundle["scanned"]["form4_read"] == n
    # (n - 1) intervals, minus a 10% allowance for Windows' `time.sleep`
    # returning marginally early. Without the limiter this same scan finishes
    # in ~0.000s, so the margin cannot mask a missing throttle.
    floor = 0.02 * (n - 1) * 0.9
    assert elapsed >= floor, (
        f"{n} documents finished in {elapsed:.3f}s -- faster than the shared "
        "rate limit allows, so the limiter is not shared across workers"
    )


# ---- 3. the two serial legs respect the deadline ---- #
def test_build_ownership_watchlist_leg_stops_at_the_deadline(monkeypatch):
    """The watchlist leg loops serially over up to ~15 documents at a 25s
    socket timeout. Unbounded, a degraded-but-not-failing EDGAR blows the
    lane's 90s contract wide open."""
    monkeypatch.setattr(
        earnings_report, "_load_cik_map",
        lambda session: {"AAPL": "0000320193", "MSFT": "0000789019", "NVDA": "0001045810"},
    )
    idx_rows = [
        _idx_row("4", "320193", "edgar/data/320193/a.txt"),
        _idx_row("4", "789019", "edgar/data/789019/b.txt"),
        _idx_row("4", "1045810", "edgar/data/1045810/c.txt"),
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)

    clock = {"t": 1000.0}
    monkeypatch.setattr(own, "_now", lambda: clock["t"])
    fetched = []

    def _slow_fetch_doc(path, session):
        fetched.append(path)
        clock["t"] += 0.6
        return _form4_xml("AAPL", "Apple Inc", "COOK TIM",
                          code="S", shares=100000, price=200.0)

    monkeypatch.setattr(own, "fetch_doc", _slow_fetch_doc)

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", 1001.0)

    assert len(fetched) == 2, f"watchlist leg ignored the deadline: {fetched}"
    assert bundle["scanned"]["form4_read"] == 2
    assert bundle["scanned"]["truncated"] is True


def test_build_ownership_13d_leg_stops_at_the_deadline(monkeypatch):
    """Same guard on the 13D leg -- the real index carries 40 13D-type lines a
    day, and this loop is serial too."""
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    idx_rows = [
        _idx_row("SCHEDULE 13D", str(9600 + i),
                 f"edgar/data/{9600 + i}/000{9600 + i}-26-00000{i}.txt")
        for i in range(3)
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)

    clock = {"t": 1000.0}
    monkeypatch.setattr(own, "_now", lambda: clock["t"])
    fetched = []

    def _slow_fetch_doc(path, session):
        fetched.append(path)
        clock["t"] += 0.6
        return _schedule13d_xml("Some Issuer", "1", "New Filer LLC", percent=6.0)

    monkeypatch.setattr(own, "fetch_doc", _slow_fetch_doc)

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", 1001.0)

    assert len(fetched) == 2, f"13D leg ignored the deadline: {fetched}"
    assert len(bundle["activist_stakes"]) == 2
    assert bundle["scanned"]["truncated"] is True


# ---- 4. the block is never "today's" activity ---- #
def test_build_ownership_scanned_carries_the_index_filing_day(monkeypatch):
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    idx_rows = [_idx_row("4", "5555555", "edgar/data/5555555/doc.txt", filed="2026-07-29")]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)
    monkeypatch.setattr(
        own, "fetch_doc",
        lambda path, session: _form4_xml("XYZ", "Some Co", "SMITH JOHN",
                                         code="P", shares=100000, price=50.0),
    )

    bundle = own.build_ownership(object(), {}, "2026-07-31", own._now() + 60)
    assert bundle["scanned"]["index_date"] == "2026-07-29"


def test_build_ownership_index_date_is_the_resolved_day_not_today(monkeypatch):
    """The cron runs 07:00 JST = 18:00 ET the previous day; the index publishes
    ~22:00 ET, so the newest published index is always for ET day D-2 and
    `fetch_daily_index` falls back. `index_date` must report the day actually
    covered, not the day requested -- consecutive briefs can legitimately
    carry the same filing day and the reader has to be able to see that."""
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    good = _index_text([
        ("4", "Some Co", "1234567", "20260729",
         "edgar/data/1234567/0001234567-26-000001.txt"),
    ])
    good_url = own._daily_index_url("2026-07-29")

    class _Sess:
        def get(self, url, headers=None, timeout=None):
            return _Resp(good) if url == good_url else _Resp(status=404)

    monkeypatch.setattr(
        own, "fetch_doc",
        lambda path, session: _form4_xml("XYZ", "Some Co", "SMITH JOHN",
                                         code="P", shares=100000, price=50.0),
    )

    health = {}
    bundle = own.build_ownership(_Sess(), health, "2026-07-31", own._now() + 60)

    assert bundle["scanned"]["index_date"] == "2026-07-29"
    assert health["ownership_index"] == {"ok": True, "note": "fell back to 2026-07-29"}


# ---- 5. all three health keys on an index failure ---- #
def test_build_ownership_empty_index_writes_all_three_health_keys(monkeypatch):
    """Both the spec's failure table and the CONTRACT.md addendum promise three
    independent health keys. Returning early on an empty index left
    `ownership_form4` and `ownership_13d` absent, so the diagnostics could not
    tell "the index failed" from "those legs never ran"."""
    def _fake_index(today, session, health):
        health["ownership_index"] = {"ok": False, "note": "HTTPError: 503 Server Error"}
        return []

    monkeypatch.setattr(own, "fetch_daily_index", _fake_index)

    health = {}
    bundle = own.build_ownership(object(), health, "2026-07-31", own._now() + 60)

    assert health["ownership_form4"] == {"ok": False, "note": "HTTPError: 503 Server Error"}
    assert health["ownership_13d"] == {"ok": False, "note": "HTTPError: 503 Server Error"}
    assert bundle == {
        "insider_watchlist": [], "insider_buys": [], "activist_stakes": [],
        "scanned": {"form4_total": 0, "form4_read": 0, "truncated": False,
                    "index_date": ""},
    }


# ---- 6. a zero prior percentage is absent, not a delta ---- #
def test_fetch_prior_13d_returns_none_when_the_prior_document_has_no_percent(monkeypatch):
    """`0.0` is what `parse_schedule13d` yields when the prior document parses
    but carries no `percentOfClass`. Returned as-is it renders
    `0.0% -> 8.5%` -- a fabricated "was zero", which is the one thing this
    feature must never do."""
    subs = {
        "filings": {
            "recent": {
                "form": ["SCHEDULE 13D"],
                "accessionNumber": ["0001982444-25-000100"],
                "primaryDocument": ["primary_doc.xml"],
                "filingDate": ["2025-01-15"],
            }
        }
    }

    class _Sess:
        def get(self, url, headers=None, timeout=None):
            return _JsonResp(subs)

    monkeypatch.setattr(
        own, "fetch_doc",
        lambda path, session: _schedule13d_xml(
            "Issuer", "1982444", "Elliott Investment Management, L.P.", percent=0.0),
    )

    result = own.fetch_prior_13d(
        "0001982444", "Elliott Investment Management, L.P.", "not-present",
        _Sess(), own._now() + 60)
    assert result is None


# ---- 8. truncated must mean something was actually dropped ---- #
def test_build_ownership_scan_not_truncated_when_the_row_iterator_is_exhausted(monkeypatch):
    """`_submit_next` checked the deadline before pulling from the iterator, so
    a late completion after the last row still set `truncated = True` --
    producing the self-contradictory `read == total` with `truncated = True`.
    Here every row is submitted before the clock moves; the deadline then
    passes while they are in flight, and nothing is left to drop."""
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    idx_rows = [
        _idx_row("4", str(6000 + i), f"edgar/data/{6000 + i}/doc{i}.txt")
        for i in range(3)
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)

    clock = {"t": 1000.0}
    monkeypatch.setattr(own, "_now", lambda: clock["t"])

    # The barrier makes this deterministic: no worker returns until all three
    # rows are in flight (so the submission loop, which runs at t=1000, has
    # certainly drained the iterator), and only then does the clock jump past
    # the deadline. Nothing is left to drop when the completions come back.
    barrier = own.threading.Barrier(3, action=lambda: clock.__setitem__("t", 2000.0))

    def _fetch_doc(path, session):
        barrier.wait(timeout=10)
        return _form4_xml("TICK", "Some Co", "PERSON", code="A", shares=10, price=0.0)

    monkeypatch.setattr(own, "fetch_doc", _fetch_doc)

    bundle = own.build_ownership(object(), {}, "2026-07-31", 1010.0)

    assert bundle["scanned"]["form4_read"] == 3
    assert bundle["scanned"]["form4_total"] == 3
    assert bundle["scanned"]["truncated"] is False


# ---- 9. one spelling of the 13D predicate ---- #
def test_build_ownership_13d_selection_reads_the_shared_form_tuple(monkeypatch):
    """`_build_ownership` filtered with `startswith("SCHEDULE 13D")` while
    `fetch_prior_13d` used `_SCHEDULE_13D_FORMS`. Narrowing the constant must
    narrow the selection -- proving there is only one predicate."""
    monkeypatch.setattr(own, "_SCHEDULE_13D_FORMS", ("SCHEDULE 13D",))
    monkeypatch.setattr(earnings_report, "_load_cik_map", lambda session: {})
    idx_rows = [
        _idx_row("SCHEDULE 13D", "1982444", "edgar/data/1982444/0001982444-26-000123.txt"),
        _idx_row("SCHEDULE 13D/A", "1982445", "edgar/data/1982445/0001982445-26-000124.txt"),
    ]
    monkeypatch.setattr(own, "fetch_daily_index", lambda today, session, health: idx_rows)

    fetched = []
    monkeypatch.setattr(
        own, "fetch_doc",
        lambda path, session: fetched.append(path) or _schedule13d_xml(
            "Some Issuer", "1982444", "Elliott Investment Management, L.P.", percent=10.0),
    )

    bundle = own.build_ownership(object(), {}, "2026-07-31", own._now() + 60)

    assert fetched == ["edgar/data/1982444/000198244426000123/primary_doc.xml"]
    assert [s["form"] for s in bundle["activist_stakes"]] == ["SCHEDULE 13D"]
