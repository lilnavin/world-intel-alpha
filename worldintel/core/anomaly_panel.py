"""Deterministic anomaly panel — the analytical priors behind an earnings card.

Given a reporter's CIK and the period its results cover, this module computes the
ratio and trend work an experienced reader does by hand: working-capital days,
earnings quality, the funding waterfall and margins, each against the year-ago
comparable and against the company's own twelve-quarter history. Every number in
the result is computed HERE. The model that writes the card writes prose around
these figures and asserts none of them.

The panel fires in BOTH directions. A falling collection period and a falling
inventory period are findings — a clean quarter is a result, not an absence — so
every flag carries a `direction` assigned by a static per-metric sign convention,
never inferred from text downstream.

ABSOLUTE RULE (same house rule as fetch_sources.py / sec_fundamentals.py /
ownership.py): no failure may raise out of this module. Network error, malformed
payload, missing tag — all degrade to
`{"status": "unavailable", "metrics": {}, "flags": []}` plus a
`health["anomaly_panel"]` entry. Deadline exhaustion is the one degradation that
returns `"partial"`: we cannot call a filing unavailable when we never looked.
The module is import-safe — nothing here touches the network or the clock at
import time.

THE SINGLE SELECTION LAW (copied from sec_fundamentals.py, deliberately, not
imported): every selector filters on `end == period_end` FIRST. There is no
"latest fact" path anywhere in this module, which is what makes a filer's stale
`Revenues` series unreachable rather than silently substituted. Duration facts
additionally match on a day-count window, because XBRL carries quarterly,
half-year and annual facts against the same `end` date.

ONE CALL, NOT FIFTEEN. `companyfacts` returns the filer's complete fact set in a
single response (3.67 MB / 781 us-gaap tags for AMD), which serves the current
period, the year-ago comparable and all twelve history quarters. It is cached per
run. `sec_fundamentals.py` keeps using `companyconcept`; this module does not
change it. Both share SEC infrastructure with `earnings_report` and `ownership`,
so the request carries the same contact User-Agent and the same pacing courtesy —
an IP block takes all four lanes down at once.

TWO FAMILIES ARE DELIBERATELY ABSENT. Segment revenue/operating income and
off-balance-sheet obligations are only in the filing document, which
`sec_filing_text` (not yet built) reads. They are not stubbed, mocked or
forward-declared: they are simply absent from `metrics`, and `status` says
`"partial"` because of it.
"""

from __future__ import annotations

from worldintel import config as wi_config
import time
from datetime import datetime

import requests

# --------------------------------------------------------------------------- #
# Endpoints and tuning constants
# --------------------------------------------------------------------------- #
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# This lane's OWN budget. It coincides with sec_fundamentals.LANE_DEADLINE_S;
# that coincidence is not a reason to import theirs. `deadline_at` must always be
# built from THIS module's `_now()` plus THIS constant — mixing a sibling's clock
# or budget silently empties the lane. See run._ownership_deadline() and its test
# test_ownership_deadline_uses_own_clock_and_own_budget for the precedent.
LANE_DEADLINE_S = 45.0

FLAG_CAP = 3                # flags surfaced per company, ranked by |deviation|
HISTORY_QUARTERS = 12       # own-history depth for PRECEDENT / REGIME
MIN_HISTORY_QUARTERS = 8    # below this a precedent is undated and a quintile unusable

REQUEST_PACING_S = 0.12     # same courtesy interval as sec_fundamentals.fetch_concept

_HTTP_TIMEOUT = 25
# SEC returns HTTP 403 without a descriptive, contactable User-Agent. A local
# copy of the sibling lanes' string (house rule: the four SEC modules stay
# independently loadable, so this is duplicated rather than imported).
def _sec_ua() -> str:
    return wi_config.get().sec_user_agent()

# Working-capital days are stated against a nominal 91-day quarter, NOT against
# each period's own start..end span. Filers run 13-week quarters that drift
# between 90 and 98 days; dividing by the drifting span would move DSO by a day
# for calendar reasons alone and make the YoY delta partly an artefact.
QUARTER_DAYS = 91.0

# US federal statutory rate — the benchmark an effective tax rate is read
# against when the filer's own trailing history is unusable or agrees with it.
STATUTORY_TAX_RATE_PCT = 21.0

# Day-count windows for duration facts (see THE SINGLE SELECTION LAW above).
_Q_WINDOW = (80, 100)       # a fiscal quarter: 13 weeks, plus 52/53-week drift

# Thresholds. Validated against AMD's live Q2 2026 figures so each fires exactly
# as the spec annotates it.
WORKING_CAPITAL_DAYS = 3.0      # |Δ| in days, YoY        (AMD: DSO −3.1, DIO −12.3, DPO +5.8)
MARGIN_PP = 2.0                 # |Δ| in points, YoY
TAX_RATE_PP = 5.0               # |ETR − benchmark| in points   (AMD: 9.8% vs 21.0%)
SHARE_COUNT_PCT = 1.0           # |Δ| in percent, YoY     (AMD: +1.78%)
CAPEX_TO_OCF_PCT = 80.0         # capex as a percent of operating cash flow
CAPEX_TO_OCF_STEP_PP = 15.0     # ... or that percentage moving this far YoY
BUYBACK_TO_SBC_MIN = 1.0        # buybacks below share-based comp: net dilution

# The accrual rule's scale is the filer's own interquintile range, which is data
# and can be arbitrarily narrow. Floored, because a company with a very stable
# accrual history would otherwise turn a rounding-scale excursion into a huge
# deviation and take a capped flag slot from a real finding. In points of total
# assets: below half a point, the trailing range is not a scale, it is noise.
ACCRUAL_RANGE_FLOOR_PP = 0.5

# An effective tax rate outside this band is a real filed figure but not a
# differenceable one — it comes from a quarter whose pre-tax base was near zero,
# where the ratio explodes. See `_metric_entry`.
ETR_COMPARABLE_MAX_PCT = 100.0

# Ordered per metric; the first tag that yields a usable fact AT the period end
# wins (Global Constraint 5: per-company behaviour lives in ordered ladders,
# never in `if ticker == ...`).
TAG_LADDERS = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ),
    "receivables": ("AccountsReceivableNetCurrent", "ReceivablesNetCurrent"),
    "inventory": ("InventoryNet",),
    "payables": ("AccountsPayableCurrent", "AccountsPayableAndAccruedLiabilitiesCurrent"),
    "assets": ("Assets",),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingIncomeLoss",),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "ocf": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    "capex": (
        "PaymentsToAcquireProductiveAssets",
        "PaymentsToAcquirePropertyPlantAndEquipment",
    ),
    "buyback": ("PaymentsForRepurchaseOfCommonStock",),
    # AllocatedShareBasedCompensationExpense trails the cash-flow tag because the
    # latter is the total; the former is the income-statement allocation and is
    # the rung that survives when the cash-flow statement is YTD-only.
    "sbc": ("ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"),
    "debt_issued": ("ProceedsFromIssuanceOfLongTermDebt",),
    "diluted_shares": ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    # The reported rate leads the computed one: filers publish it, and deriving
    # tax ÷ pre-tax income disagrees with the filed figure whenever equity-method
    # income or a discrete item sits between the two lines.
    "tax_rate": ("EffectiveIncomeTaxRateContinuingOperations",),
    "tax_expense": ("IncomeTaxExpenseBenefit",),
    "pretax_income": (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    ),
}

# The tags that carry balance-sheet instants — the quarter-end ladder is read off
# them, because a balance sheet exists at EVERY period end including the fiscal
# Q4 that carries no three-month income-statement fact.
_BALANCE_KEYS = ("receivables", "inventory", "payables", "assets")

# Published metric order.
_METRIC_ORDER = (
    "revenue",
    "dso", "dio", "dpo",
    "gross_margin_pct", "operating_margin_pct", "net_margin_pct", "net_less_operating_pp",
    "effective_tax_rate_pct", "accrual_ratio_pct",
    "capex_to_ocf_pct", "buyback_to_sbc", "debt_issued", "diluted_shares",
)

# How each metric's `delta` is expressed. "pct" metrics carry a year-on-year
# percentage change; everything else carries `value - prior` in its own unit.
_DELTA_UNIT = {
    "revenue": "pct",
    "dso": "days", "dio": "days", "dpo": "days",
    "gross_margin_pct": "pp", "operating_margin_pct": "pp", "net_margin_pct": "pp",
    "net_less_operating_pp": "pp",
    "effective_tax_rate_pct": "pp", "accrual_ratio_pct": "pp",
    "capex_to_ocf_pct": "pp", "buyback_to_sbc": "abs",
    "debt_issued": "pct", "diluted_shares": "pct",
}

# Which family each metric belongs to. `status` is "ok" only when every family is
# represented; the two filing-text families below never are, in this module.
_FAMILIES = {
    "working_capital": ("dso", "dio", "dpo"),
    "earnings_quality": ("effective_tax_rate_pct", "accrual_ratio_pct"),
    "funding_waterfall": ("capex_to_ocf_pct", "buyback_to_sbc", "debt_issued", "diluted_shares"),
    "margin": ("gross_margin_pct", "operating_margin_pct", "net_margin_pct",
               "net_less_operating_pp"),
}
# Families that live in the filing document, which `sec_filing_text` will read.
# Listed, not stubbed: naming them is what makes `status: partial` honest.
_FILING_TEXT_FAMILIES = ("segment", "off_balance_sheet")

# metric -> family, for the one-flag-per-family selection in `compute_flags`.
_FAMILY_OF = {name: family for family, names in _FAMILIES.items() for name in names}

_LABELS = {
    "dso": "DSO", "dio": "Inventory days", "dpo": "DPO",
    "gross_margin_pct": "Gross margin",
    "operating_margin_pct": "Operating margin",
    "net_margin_pct": "Net margin",
}

# One companyfacts response per CIK per run. The payload is megabytes and the
# whole panel reads it once; a second GET for the same filer is pure waste
# against a shared, rate-limited endpoint.
_FACTS_CACHE: dict = {}


# --------------------------------------------------------------------------- #
# Small shared helpers
# --------------------------------------------------------------------------- #
def _short(exc: Exception) -> str:
    """A short, single-line reason string for health notes (same shape as the
    sibling modules' `_short`)."""
    msg = str(exc).split("\n")[0].strip()
    return f"{type(exc).__name__}: {msg}"[:120] if msg else type(exc).__name__


def _num(value):
    """`value` as a float, or None when missing/unparseable. Never raises. A
    bool is refused: `True` is arithmetically 1.0 and would publish silently."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except wi_config.ConfigError:
        raise
    except Exception:
        return None
    return None if out != out else out          # NaN is not a publishable number


def _parse_day(value):
    """'YYYY-MM-DD' -> date, or None if unparseable/missing."""
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except wi_config.ConfigError:
        raise
    except Exception:
        return None


def _span_days(fact: dict):
    """Day span of a duration fact's `start`..`end`, or None if unparseable."""
    start = _parse_day(fact.get("start"))
    end = _parse_day(fact.get("end"))
    if start is None or end is None:
        return None
    return (end - start).days


def _health_note(health: dict, note: str) -> None:
    """Append a machine-readable note to the lane's health record without
    clobbering `_companyfacts`'s `{"ok": False, "note": ...}` entry or the notes
    of any other company the lane processed this morning."""
    if not isinstance(health, dict):
        return
    entry = health.get("anomaly_panel")
    if not isinstance(entry, dict):
        entry = {}
    notes = entry.get("notes")
    if not isinstance(notes, list):
        notes = []
    notes.append(note)
    entry["notes"] = notes
    health["anomaly_panel"] = entry


# --------------------------------------------------------------------------- #
# Network seams
# --------------------------------------------------------------------------- #
def _now() -> float:
    """Monotonic wall clock for deadline checks — THIS module's clock. A seam so
    deadline behaviour is testable without sleeping. `deadline_at` is always a
    `_now()`-comparable monotonic instant, never a `time.time()` epoch value,
    which would make every deadline look as if it had already passed."""
    return time.monotonic()


def _sleep(seconds: float) -> None:
    """Request pacing. A seam so the offline suite never actually waits."""
    time.sleep(seconds)


def _sec_get_json(url: str, session) -> dict:
    """One HTTP GET against a data.sec.gov endpoint -> parsed JSON, carrying the
    SEC-required contact User-Agent. Isolated so tests stub a single seam."""
    sess = session or requests
    resp = sess.get(url, headers={"User-Agent": _sec_ua()}, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _companyfacts(cik: str, session, health: dict = None) -> dict:
    """The filer's complete XBRL fact set, or `{}` on any failure. One GET per
    CIK per run: the response is cached, since every period this panel reads —
    current, year-ago and twelve history quarters — comes out of the same body."""
    if cik in _FACTS_CACHE:
        return _FACTS_CACHE[cik]
    try:
        _sleep(REQUEST_PACING_S)
        facts = _sec_get_json(COMPANYFACTS_URL.format(cik=cik), session) or {}
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        if isinstance(health, dict):
            # Update in place, never replace: the lane processes several
            # companies in one run, and a wholesale assignment here would throw
            # away every note `_health_note` accumulated for the earlier ones —
            # precisely what its docstring promises not to happen.
            entry = health.get("anomaly_panel")
            if not isinstance(entry, dict):
                entry = {}
            entry["ok"] = False
            entry["note"] = _short(exc)
            health["anomaly_panel"] = entry
        return {}
    if facts:
        _FACTS_CACHE[cik] = facts
    return facts


# --------------------------------------------------------------------------- #
# Fact selection (pure)
#
# Every selector below filters `end == period_end` FIRST. Nothing here can reach
# a fact at another date, which is the whole point.
# --------------------------------------------------------------------------- #
def _tag_facts(facts: dict, tag: str) -> list:
    """Flatten one tag's `units` payload into a list of fact dicts, each carrying
    its originating unit under `unit`. Malformed shapes degrade to `[]`."""
    try:
        gaap = ((facts or {}).get("facts") or {}).get("us-gaap")
        node = (gaap or {}).get(tag)
        units = (node or {}).get("units")
    except wi_config.ConfigError:
        raise
    except Exception:
        return []
    if not isinstance(units, dict):
        return []
    out = []
    for unit, items in units.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            fact = dict(item)
            fact["unit"] = unit
            out.append(fact)
    return out


def _instant(facts: dict, tag: str, end: str):
    """The value of the instant fact (no `start` — a balance-sheet item) for
    `tag` whose `end` equals `end`. None if no such fact exists."""
    for fact in _tag_facts(facts, tag):
        if fact.get("start"):
            continue
        if fact.get("end") == end:
            value = _num(fact.get("val"))
            if value is not None:
                return value
    return None


def _duration(facts: dict, tag: str, end: str, lo: int, hi: int):
    """The value of the duration fact for `tag` whose `end` equals `end` and
    whose `start`..`end` span falls in `[lo, hi]` days. The shortest qualifying
    span wins, so a quarterly fact is never shadowed by a half-year one filed
    against the same date. None if no fact qualifies."""
    best = None
    best_days = None
    for fact in _tag_facts(facts, tag):
        if not fact.get("start") or fact.get("end") != end:
            continue
        days = _span_days(fact)
        if days is None or days < lo or days > hi:
            continue
        value = _num(fact.get("val"))
        if value is None:
            continue
        if best_days is None or days < best_days:
            best, best_days = value, days
    return best


def _cumulative(facts: dict, tag: str, end: str, prior_end: str):
    """The quarterly value at `end` for a tag the filer reports cumulatively
    (cash-flow items, and any income-statement item in a fiscal Q4): the longest
    duration fact at `end` minus the fact at `prior_end` sharing its `start`.

    Refused for per-share units — a per-share figure produced by subtraction is
    arithmetically wrong, because the share count moves between periods."""
    candidates = [f for f in _tag_facts(facts, tag)
                  if f.get("start") and f.get("end") == end and _span_days(f) is not None]
    if not candidates:
        return None
    largest = max(candidates, key=_span_days)
    unit = largest.get("unit")
    if isinstance(unit, str) and ("/shares" in unit or unit == "USD-per-shares"):
        return None
    current = _num(largest.get("val"))
    if current is None:
        return None
    for fact in _tag_facts(facts, tag):
        if fact.get("start") == largest.get("start") and fact.get("end") == prior_end:
            prior = _num(fact.get("val"))
            if prior is not None:
                return current - prior
    return None


def _quarter(facts: dict, tag: str, end: str, prior_end: str):
    """The quarterly value at `end`: a directly reported ~3-month fact, else the
    cumulative difference against `prior_end`. One rule, no fiscal-Q4 special
    case — the Q4 gap is exactly what the second rung exists for."""
    direct = _duration(facts, tag, end, *_Q_WINDOW)
    if direct is not None:
        return direct
    return _cumulative(facts, tag, end, prior_end)


def _ladder_quarter(facts: dict, key: str, end: str, prior_end: str):
    """`_quarter` walked down `TAG_LADDERS[key]`; the first tag that resolves at
    `end` wins."""
    for tag in TAG_LADDERS.get(key, ()):
        value = _quarter(facts, tag, end, prior_end)
        if value is not None:
            return value
    return None


def _ladder_instant(facts: dict, key: str, end: str):
    for tag in TAG_LADDERS.get(key, ()):
        value = _instant(facts, tag, end)
        if value is not None:
            return value
    return None


def _ladder_duration(facts: dict, key: str, end: str):
    for tag in TAG_LADDERS.get(key, ()):
        value = _duration(facts, tag, end, *_Q_WINDOW)
        if value is not None:
            return value
    return None


# --------------------------------------------------------------------------- #
# Arithmetic (pure)
# --------------------------------------------------------------------------- #
def _pct_of(numerator, denominator):
    """`numerator / denominator * 100`, or None when the denominator is zero,
    negative or missing. A ratio over a vanishing denominator is noise, not a
    number worth publishing (same rule as sec_fundamentals._ratio)."""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    try:
        return numerator / denominator * 100.0
    except wi_config.ConfigError:
        raise
    except Exception:
        return None


def _days_of(balance, revenue):
    """A balance-sheet line expressed as days of quarterly revenue."""
    if balance is None or revenue is None or revenue <= 0:
        return None
    return balance / revenue * QUARTER_DAYS


def _yoy_pct(current, prior):
    """Percent change against the year-ago comparable. `abs(prior)` is the
    denominator so a negative base does not silently flip the sign."""
    if current is None or prior is None or prior == 0:
        return None
    try:
        return (current - prior) / abs(prior) * 100.0
    except wi_config.ConfigError:
        raise
    except Exception:
        return None


def _percentile(values: list, q: float):
    """The `q`-quantile (0..1) of `values` by linear interpolation. Stdlib only,
    and deterministic: no sampling, no randomness, same input -> same output."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _benchmark(series: list) -> dict:
    """The history-relative reference points a threshold can be measured against:
    the trailing median and the two quintile boundaries. All None below
    `MIN_HISTORY_QUARTERS` — a quintile drawn from five quarters is a guess, and
    `compute_flags` must be able to tell "inside the range" from "unknown"."""
    values = [v for _end, v in (series or []) if v is not None]
    if len(values) < MIN_HISTORY_QUARTERS:
        return {"median": None, "p20": None, "p80": None}
    return {"median": _percentile(values, 0.5),
            "p20": _percentile(values, 0.2),
            "p80": _percentile(values, 0.8)}


def _precedent(series: list, value: float) -> dict:
    """Where `value` sits in the filer's own history — the PRECEDENT operator's
    raw material. `series` is `[(end, value), ...]` for PRIOR periods, most
    recent first.

    `rank` is 1 for the highest reading. `since` is dated only at an extreme:
    the end date of the most recent prior period that reached this level, or the
    oldest date in the window when none did ("not seen since at least then").

    BOTH are None below `MIN_HISTORY_QUARTERS`, and the pairing is enforced by
    the returned shape rather than left as a rule to remember: "highest in three
    quarters" is not a precedent worth citing, and a renderer keying on
    `rank == 1` alone would otherwise print "highest since" off a window that
    never earned one. Above the threshold `rank` stands on its own — "3rd
    highest of twelve" is a real reading — so only the short-window branch
    blanks it."""
    blank = {"rank": None, "since": None}
    try:
        value = _num(value)
        if value is None or not isinstance(series, (list, tuple)):
            return blank
        pairs = [(end, _num(val)) for end, val in series]
        pairs = [(end, val) for end, val in pairs if val is not None]
        if not pairs:
            return blank
        higher = [p for p in pairs if p[1] > value]
        lower = [p for p in pairs if p[1] < value]
        rank = len(higher) + 1
        if len(pairs) < MIN_HISTORY_QUARTERS:
            return blank
        if not higher:
            matches = [end for end, val in pairs if val >= value]
        elif not lower:
            matches = [end for end, val in pairs if val <= value]
        else:
            return {"rank": rank, "since": None}
        since = matches[0] if matches else pairs[-1][0]
        return {"rank": rank, "since": since}
    except wi_config.ConfigError:
        raise
    except Exception:
        return blank


# --------------------------------------------------------------------------- #
# Per-period snapshot (pure)
# --------------------------------------------------------------------------- #
def _snapshot(facts: dict, end: str, prior_end: str) -> dict:
    """Every raw metric at one period end. `prior_end` is the immediately
    preceding quarter end, used ONLY by the cumulative-difference rung — never to
    reach a value at another date."""
    quarterly = lambda key: _ladder_quarter(facts, key, end, prior_end)   # noqa: E731

    revenue = quarterly("revenue")
    net_income = quarterly("net_income")
    operating_income = quarterly("operating_income")
    gross_profit = quarterly("gross_profit")
    ocf = quarterly("ocf")
    capex = quarterly("capex")
    buyback = quarterly("buyback")
    sbc = quarterly("sbc")

    assets = _ladder_instant(facts, "assets", end)
    gross_margin = _pct_of(gross_profit, revenue)
    operating_margin = _pct_of(operating_income, revenue)
    net_margin = _pct_of(net_income, revenue)

    tax_rate = _ladder_duration(facts, "tax_rate", end)
    if tax_rate is not None:
        tax_rate *= 100.0                       # XBRL files the rate as a fraction
    else:
        tax_rate = _pct_of(quarterly("tax_expense"), quarterly("pretax_income"))

    accrual = None
    if net_income is not None and ocf is not None and assets and assets > 0:
        accrual = (net_income - ocf) / assets * 100.0

    inversion = None
    if net_margin is not None and operating_margin is not None:
        inversion = net_margin - operating_margin

    buyback_to_sbc = None
    if buyback is not None and sbc is not None and sbc > 0:
        buyback_to_sbc = buyback / sbc

    # DAYS OF REVENUE, NOT DAYS OF COGS — read this before touching `dio`/`dpo`.
    # The textbook definitions are inventory ÷ COGS and payables ÷ COGS, and this
    # is deliberately neither: all three ratios use quarterly REVENUE as the
    # denominator, so they are one consistent family measured on one base.
    # Against COGS, AMD's Q2 2026 inventory days come out at 144.6; only the
    # revenue denominator reproduces the 66.8 / 79.1 the spec pins, and the same
    # denominator reproduces DSO 57.4 / 60.6 and DPO 42.3 / 36.5 to the decimal.
    # The names are kept because DSO/DIO/DPO is what a reader looks for; the
    # basis is stated here, at the call site, because a reader grepping for
    # `dio` lands on this line and not on `_days_of`'s docstring.
    return {
        "revenue": revenue,
        "dso": _days_of(_ladder_instant(facts, "receivables", end), revenue),
        "dio": _days_of(_ladder_instant(facts, "inventory", end), revenue),
        "dpo": _days_of(_ladder_instant(facts, "payables", end), revenue),
        "gross_margin_pct": gross_margin,
        "operating_margin_pct": operating_margin,
        "net_margin_pct": net_margin,
        "net_less_operating_pp": inversion,
        "effective_tax_rate_pct": tax_rate,
        "accrual_ratio_pct": accrual,
        "capex_to_ocf_pct": _pct_of(capex, ocf),
        "buyback_to_sbc": buyback_to_sbc,
        "debt_issued": quarterly("debt_issued"),
        "diluted_shares": _ladder_duration(facts, "diluted_shares", end),
    }


def _quarter_ends(facts: dict, period_end: str) -> list:
    """Every period end the filer has a balance sheet for, at or before
    `period_end`, most recent first. Read off balance-sheet instants rather than
    income-statement durations because a fiscal Q4 has the former and not the
    latter, and dropping Q4s would put a four-year-old date on a
    twelve-quarter precedent."""
    ends = set()
    for key in _BALANCE_KEYS:
        for tag in TAG_LADDERS.get(key, ()):
            for fact in _tag_facts(facts, tag):
                if fact.get("start"):
                    continue
                end = fact.get("end")
                if isinstance(end, str) and end <= period_end and _parse_day(end):
                    ends.add(end)
    return sorted(ends, reverse=True)


# --------------------------------------------------------------------------- #
# Flags (pure)
# --------------------------------------------------------------------------- #
def _flag(operator, metric, direction, statement, deviation) -> dict:
    return {"operator": operator, "metric": metric, "direction": direction,
            "statement": statement, "deviation": abs(float(deviation))}


# --------------------------------------------------------------------------- #
# THE DEVIATION SCALE — one convention, every arm.
#
# `deviation` ranks flags from different families against each other, so all of
# them must be on the same scale or the ranking is arbitrary. The convention:
# the tested quantity expressed in MULTIPLES OF THE RULE'S OWN THRESHOLD, such
# that a reading exactly at a rule's boundary scores 1.0 and severity climbs
# from there.
#
# It has to be stated as a rule rather than left to each arm because the two
# obvious alternatives are both silently wrong. Scoring an EXCESS over the
# threshold (`(value - limit) / limit`) puts every boundary breach at 0.0, so a
# capex ratio at 100% of operating cash flow would score 0.25 and rank below a
# bare-minimum 2.0pp margin move. Scoring a SHORTFALL for a less-than rule
# (`(limit - value) / limit`) is worse: it is mathematically capped at 1.0, so a
# company that repurchased nothing at all against real share issuance — the
# maximal possible breach of that rule — could never outrank that same minimum
# margin move. Both defects hide in a worked example and surface in production.
# --------------------------------------------------------------------------- #
def _deviation_above(quantity, threshold):
    """Severity for a rule that fires when `quantity` rises to or past
    `threshold`. 1.0 at the boundary, 2.0 at twice the threshold."""
    return abs(quantity) / abs(threshold)


def _deviation_below(value, threshold):
    """Severity for a rule that fires when `value` falls below `threshold`.
    1.0 at the boundary and rising as `value` falls away from it, mirroring
    `_deviation_above` — a ratio that collapsed to zero scores 2.0, which is
    where a maximal breach of a bounded ratio belongs."""
    return 1.0 + (abs(threshold) - value) / abs(threshold)


def _entry(metrics, name):
    """One metric entry's (value, prior, delta, benchmark), each None-safe."""
    entry = metrics.get(name) if isinstance(metrics, dict) else None
    if not isinstance(entry, dict):
        return (None, None, None, {})
    bench = entry.get("benchmark")
    return (_num(entry.get("value")), _num(entry.get("prior")), _num(entry.get("delta")),
            bench if isinstance(bench, dict) else {})


def _revenue_clause(metrics) -> str:
    """" while revenue grew 50.1%" — the second clock a working-capital move is
    read against. Empty when revenue did not resolve; never invented."""
    _value, _prior, delta, _bench = _entry(metrics, "revenue")
    if delta is None:
        return ""
    verb = "grew" if delta >= 0 else "fell"
    return " while revenue {0} {1:.1f}%".format(verb, abs(delta))


def _working_capital_flags(metrics) -> list:
    """DSO / DIO / DPO, ±3.0 days YoY.

    Direction is a static sign convention, per metric: a shortening collection
    or inventory period releases cash and is `favourable`; a lengthening one is
    `unfavourable`. A LENGTHENING payables period is `unfavourable` because it
    flatters operating cash flow with supplier credit; a shortening one carries
    no settled reading (discipline or lost leverage, indistinguishable from the
    outside) and is `neutral`."""
    out = []
    clause = _revenue_clause(metrics)
    for name in ("dso", "dio", "dpo"):
        value, _prior, delta, _bench = _entry(metrics, name)
        if value is None or delta is None or abs(delta) < WORKING_CAPITAL_DAYS:
            continue
        if name == "dpo":
            direction = "unfavourable" if delta > 0 else "neutral"
        else:
            direction = "favourable" if delta < 0 else "unfavourable"
        statement = "{0} {1:+.1f} days YoY to {2:.1f}{3}".format(
            _LABELS[name], delta, value, clause)
        out.append(_flag("TWO CLOCKS", name, direction, statement,
                         _deviation_above(delta, WORKING_CAPITAL_DAYS)))
    return out


def _margin_flags(metrics) -> list:
    """Gross / operating / net margin, ±2.0pp YoY, plus the net-above-operating
    inversion — the same metric in two regimes, which is what REGIME asks."""
    out = []
    for name in ("gross_margin_pct", "operating_margin_pct", "net_margin_pct"):
        value, _prior, delta, _bench = _entry(metrics, name)
        if value is None or delta is None or abs(delta) < MARGIN_PP:
            continue
        direction = "favourable" if delta > 0 else "unfavourable"
        statement = "{0} {1:.1f}% ({2:+.1f}pp YoY)".format(_LABELS[name], value, delta)
        out.append(_flag("REGIME", name, direction, statement,
                         _deviation_above(delta, MARGIN_PP)))

    value, _prior, _delta, _bench = _entry(metrics, "net_less_operating_pp")
    if value is not None and value > 0:
        # Net income above operating income means the result is being carried by
        # items beneath the operating line — interest, equity income, a tax
        # benefit. The earnings are real; their durability is the question, so
        # this reads `unfavourable` even in a quarter that beat.
        statement = ("Net margin {0:.1f}pp above operating margin — "
                     "that much of the result is below the line".format(value))
        # The only rule whose boundary is ZERO rather than a threshold
        # constant: an inversion either exists or does not. MARGIN_PP is its
        # scale, so a gap of one threshold's width scores 1.0 like every other
        # arm at its boundary, and a hairline inversion scores near zero — which
        # is where a hairline inversion belongs.
        out.append(_flag("REGIME", "net_less_operating_pp", "unfavourable", statement,
                         _deviation_above(value, MARGIN_PP)))
    return out


def _tax_flag(metrics) -> list:
    """Effective tax rate ≥ 5pp from its benchmark. Two benchmarks are checked
    and the larger deviation wins: the filer's own trailing-8Q median (spec §3's
    threshold) and the 21% federal statutory rate (spec §2's rendering). A rate
    can be unremarkable against a company's own recent history and still be the
    story against the statute, which is exactly AMD's quarter.

    KNOWN COST OF THE OR, and it is not small. The two benchmarks answer
    different questions: the median asks "has this CHANGED?", the statutory rate
    asks "is this UNUSUAL against the statute?". OR-ing them means a filer whose
    rate is structurally low — an IP-in-Ireland software company, say — fires
    this flag EVERY quarter forever, on a constant, consuming one of only
    `FLAG_CAP` slots with a fact that never moves. That is accepted here because
    it is the only reading under which both spec §2 and spec §3 hold; if the
    briefs come back monotonous, the fix is to make the statutory arm fire only
    on a CHANGE in the gap, which is a spec decision, not a tuning one.

    AND `_select` MADE THAT COST GUARANTEED RATHER THAN PROBABILISTIC. Under a
    pure severity ranking the constant tax flag at least had to compete on
    merit and was usually buried. Under one-flag-per-family it is
    `earnings_quality`'s champion by default — this arm and the accrual arm are
    the only two in that family — so it takes a slot unless three OTHER families
    outrank it. AMD shows it live: the tax flag at 2.240 surfaces ahead of
    `net_margin_pct` at 4.282. A filer with a permanent 5% rate and three margin
    breaches surfaces `[gross, operating, tax]` and drops a 10.0-deviation net
    margin move for a 3.2 constant. Both mechanisms are working as designed and
    they compound; the change-in-the-gap fix above is correspondingly more
    urgent, not less.

    `neutral` always: a low rate can be a one-off discrete benefit or a durable
    structural position, and the filing alone does not say which."""
    value, _prior, _delta, bench = _entry(metrics, "effective_tax_rate_pct")
    if value is None:
        return []
    candidates = [("the {0:.1f}% statutory rate".format(STATUTORY_TAX_RATE_PCT),
                   STATUTORY_TAX_RATE_PCT)]
    median = _num(bench.get("median"))
    if median is not None:
        candidates.append(("its trailing {0:.1f}% median".format(median), median))
    label, reference = max(candidates, key=lambda pair: abs(value - pair[1]))
    gap = value - reference
    if abs(gap) < TAX_RATE_PP:
        return []
    statement = "Effective tax rate {0:.1f}% vs {1} ({2:+.1f}pp)".format(value, label, gap)
    return [_flag("PRECEDENT", "effective_tax_rate_pct", "neutral", statement,
                  _deviation_above(gap, TAX_RATE_PP))]


def _accrual_flag(metrics) -> list:
    """Accrual ratio outside the filer's own trailing quintile. Net income far
    above operating cash flow is `unfavourable` (the earnings are accruals); far
    below is `favourable`. With no benchmark there is no flag — never a guess."""
    value, _prior, _delta, bench = _entry(metrics, "accrual_ratio_pct")
    p20, p80 = _num(bench.get("p20")), _num(bench.get("p80"))
    if value is None or p20 is None or p80 is None or p80 <= p20:
        return []
    if value > p80:
        direction, boundary, side = "unfavourable", p80, "above"
    elif value < p20:
        direction, boundary, side = "favourable", p20, "below"
    else:
        return []
    statement = ("Accrual ratio {0:+.2f}% of assets, {1} its trailing quintile "
                 "boundary of {2:+.2f}%".format(value, side, boundary))
    # Same convention as every other arm (boundary = 1.0), but the "threshold"
    # here is data, not a constant, so the excursion is scaled by the trailing
    # interquintile range — floored, because that range can approach zero for a
    # filer with a very stable accrual history and would otherwise manufacture a
    # top-ranked flag out of a rounding-scale move.
    scale = max(p80 - p20, ACCRUAL_RANGE_FLOOR_PP)
    return [_flag("PRECEDENT", "accrual_ratio_pct", direction, statement,
                  1.0 + abs(value - boundary) / scale)]


def _funding_flags(metrics) -> list:
    """Capex intensity, buybacks against share-based compensation, and the
    diluted share count — what was sold, borrowed or issued to pay for this."""
    out = []

    value, _prior, delta, _bench = _entry(metrics, "capex_to_ocf_pct")
    if value is not None:
        # Capex intensity is `neutral` by convention: heavy reinvestment is a
        # cost to this quarter and an asset to a later one, and the filing does
        # not settle which.
        deviations = []
        if value > CAPEX_TO_OCF_PCT:
            deviations.append(_deviation_above(value, CAPEX_TO_OCF_PCT))
        if delta is not None and abs(delta) >= CAPEX_TO_OCF_STEP_PP:
            deviations.append(_deviation_above(delta, CAPEX_TO_OCF_STEP_PP))
        if deviations:
            step = "" if delta is None else " ({0:+.1f}pp YoY)".format(delta)
            statement = "Capex {0:.1f}% of operating cash flow{1}".format(value, step)
            out.append(_flag("FUNDING LEG", "capex_to_ocf_pct", "neutral", statement,
                             max(deviations)))

    value, _prior, _delta, _bench = _entry(metrics, "buyback_to_sbc")
    if value is not None and value < BUYBACK_TO_SBC_MIN:
        statement = ("Buybacks {0:.2f}x share-based compensation — "
                     "repurchases did not cover the issuance".format(value))
        out.append(_flag("FUNDING LEG", "buyback_to_sbc", "unfavourable", statement,
                         _deviation_below(value, BUYBACK_TO_SBC_MIN)))

    _value, _prior, delta, _bench = _entry(metrics, "diluted_shares")
    if delta is not None and abs(delta) >= SHARE_COUNT_PCT:
        direction = "unfavourable" if delta > 0 else "favourable"
        statement = "Diluted share count {0:+.2f}% YoY".format(delta)
        out.append(_flag("FUNDING LEG", "diluted_shares", direction, statement,
                         _deviation_above(delta, SHARE_COUNT_PCT)))
    return out


def _select(flags: list) -> list:
    """Fill `FLAG_CAP` slots: each family's champion first, ranked by
    |deviation|, then backfill from what is left.

    WHY DIVERSITY BEATS PURE SEVERITY HERE. Gross, operating and net margin are
    mechanically linked — one move in cost of sales propagates through all
    three — so three margin flags are one story told at three depths, not three
    findings. The cap exists to spend its slots on DECORRELATED signals, and
    family membership is a crude but honest proxy for that. Ranked purely by
    severity, AMD surfaced three margins and silently dropped a genuinely clean
    working-capital quarter (inventory days −12.3, missing the cut by 0.19) that
    spec §2 requires the brief to be able to report.

    The top-ranked flag overall always survives: it is by definition its own
    family's champion, and champions are ranked among themselves by the same
    key. Only a 2nd or 3rd flag from an already-represented family can yield —
    diversity never displaces the most severe signal, it only breaks up a
    monoculture behind it.

    KNOWN INTERACTION WITH `_tax_flag`. Promoting one flag per family also
    promotes any flag that fires on a CONSTANT. `earnings_quality` has only two
    arms, so a structurally low-tax filer's permanent tax flag becomes its
    champion by default and is effectively guaranteed a slot — see the KNOWN
    COST OF THE OR paragraph in `_tax_flag`, which this rule turned from a
    probabilistic cost into a certain one. Read the two together before tuning
    either.

    `flags` arrives sorted by descending |deviation|, so the first flag seen for
    a family IS that family's champion."""
    champions = []
    seen = set()
    for flag in flags:
        family = _FAMILY_OF.get(flag["metric"], flag["metric"])
        if family not in seen:
            seen.add(family)
            champions.append(flag)
    chosen = champions[:FLAG_CAP]
    if len(chosen) < FLAG_CAP:
        # Fewer families than slots — a company whose only breaches are three
        # working-capital moves still fills the panel from that one family
        # rather than returning a short list.
        picked = {id(flag) for flag in chosen}
        chosen += [flag for flag in flags if id(flag) not in picked][:FLAG_CAP - len(chosen)]
    chosen.sort(key=lambda flag: (-flag["deviation"], flag["metric"]))
    return chosen


def compute_flags(metrics: dict) -> list:
    """Threshold breaches, one per family first and then by |deviation|, capped
    at `FLAG_CAP`.

    PURE: no network, no clock, no module state. The same `metrics` dict yields
    the same flags on every run, and the argument is never mutated.

    Positive and negative breaches are equally eligible — a quarter that improved
    on three fronts produces three flags, because the panel's job is to say what
    happened, not to find fault.

    `deviation` is a RANKING KEY and is never rendered. It is the tested quantity
    in multiples of the rule's own threshold — see THE DEVIATION SCALE above —
    which is the only way a 12-day working-capital move and a 2pp margin move can
    be compared at all; the raw magnitudes are in incommensurable units. Every
    arm scores 1.0 at its own boundary, so ties across families are real ties.
    The single exception is the net-above-operating inversion, whose boundary is
    zero by definition and which therefore scores from zero up.

    Severity alone does not fill the slots — see `_select`, which gives each
    family one flag before any family gets a second."""
    try:
        if not isinstance(metrics, dict):
            return []
        flags = (_working_capital_flags(metrics) + _margin_flags(metrics)
                 + _tax_flag(metrics) + _accrual_flag(metrics) + _funding_flags(metrics))
        flags.sort(key=lambda flag: (-flag["deviation"], flag["metric"]))
        return _select(flags)
    except wi_config.ConfigError:
        raise
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Contradictions (pure)
# --------------------------------------------------------------------------- #
_OPPOSED = {"favourable", "unfavourable"}


def compute_contradictions(flags: list, reaction: dict = None) -> list:
    """CONTRADICTION candidates — computed, never composed.

    A pair is emitted only when two facts genuinely conflict: two surfaced flags
    of OPPOSING direction, or a beat that the market marked down. `neutral` never
    contradicts anything, and flags that share a direction produce nothing. There
    is no path by which a pair is manufactured for narrative effect; when the
    list is empty the prompt writes the boring version.

    `reaction` is the earnings-ledger snapshot (`surprise_pct`, `price_move_pct`)
    the caller already holds; omitted, the price arm simply does not run."""
    out = []
    try:
        flags = flags if isinstance(flags, list) else []
        clean = [f for f in flags
                 if isinstance(f, dict) and f.get("direction") in _OPPOSED and f.get("metric")]
        for i, first in enumerate(clean):
            for second in clean[i + 1:]:
                if first["direction"] == second["direction"]:
                    continue
                out.append({
                    "a": first["metric"], "b": second["metric"],
                    "statement": "{0}; against that, {1}".format(
                        first.get("statement", ""), second.get("statement", "")),
                })

        reaction = reaction if isinstance(reaction, dict) else {}
        surprise = _num(reaction.get("surprise_pct"))
        move = _num(reaction.get("price_move_pct"))
        if surprise is not None and move is not None and surprise > 0 > move:
            out.append({
                "a": "surprise_pct", "b": "price_move_pct",
                "statement": "Beat by {0:.2f}% and the shares fell {1:.2f}%".format(
                    surprise, abs(move)),
            })
    except wi_config.ConfigError:
        raise
    except Exception:
        return out
    return out


# --------------------------------------------------------------------------- #
# build_panel
# --------------------------------------------------------------------------- #
def _blank(status: str, ticker: str, period_end: str, prior_end: str) -> dict:
    return {"status": status, "ticker": ticker, "period_end": period_end,
            "prior_end": prior_end, "metrics": {}, "flags": [], "contradictions": []}


def _metric_entry(name, current, prior, series) -> dict:
    """One published metric: the value, its year-ago comparable, the delta in the
    unit that metric is read in, a dated precedent, and the history-relative
    benchmark the threshold rules need (`compute_flags` is pure, so anything a
    rule compares against has to travel inside the metric).

    `rank` travels WITH `since`, not instead of it. The card has to render
    "lowest/highest since YYYY-MM-DD" and a bare date does not say which — rank 1
    is the highest reading of the window, `len(series) + 1` the lowest — so
    dropping it would leave the renderer to infer a direction it cannot
    reconstruct, which is exactly what this module exists to prevent."""
    unit = _DELTA_UNIT[name]
    if unit == "pct":
        delta = _yoy_pct(current, prior)
    elif current is None or prior is None:
        delta = None
    else:
        delta = current - prior

    if name == "effective_tax_rate_pct" and delta is not None:
        # A filer whose pre-tax base collapsed to near zero files a real but
        # undifferenceable rate (AMD's year-ago quarter: 1263.6%), and
        # subtracting it yields −1253.8pp. The VALUE stands — it is what was
        # filed — but the delta is suppressed rather than published, because
        # this module owns every number in the result and must not hand a
        # renderer one that means nothing.
        if (abs(current) > ETR_COMPARABLE_MAX_PCT
                or abs(prior) > ETR_COMPARABLE_MAX_PCT):
            delta = None

    precedent = _precedent(series, current)
    return {"value": current, "prior": prior, "delta": delta, "delta_unit": unit,
            "since": precedent["since"], "rank": precedent["rank"],
            "benchmark": _benchmark(series)}


def build_panel(cik: str, ticker: str, period_end: str, prior_end: str,
                session, health: dict, deadline_at: float, reaction: dict = None) -> dict:
    """Deterministic analytical priors for one reporter, from ONE companyfacts
    call. Never raises.

    `deadline_at` is a `_now()`-comparable monotonic instant built from THIS
    module's clock and `LANE_DEADLINE_S`. `reaction` is optional: the earnings
    ledger's `{"surprise_pct", "price_move_pct"}` snapshot, which lets the
    beat-versus-price contradiction be computed here with everything else rather
    than composed downstream.

    Failure degrades to `{"status": "unavailable", "metrics": {}, "flags": []}`
    plus `health["anomaly_panel"]`. Deadline exhaustion returns `"partial"` — the
    filing may be perfectly readable; we simply ran out of lane."""
    try:
        return _build_panel(cik, ticker, period_end, prior_end, session, health,
                            deadline_at, reaction)
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        _health_note(health, "cik={0} panel failed: {1}".format(cik, _short(exc)))
        return _blank("unavailable", ticker, period_end, prior_end)


def _build_panel(cik, ticker, period_end, prior_end, session, health, deadline_at, reaction):
    if not cik or not period_end:
        _health_note(health, "cik={0} skipped (no cik/period_end)".format(cik))
        return _blank("unavailable", ticker, period_end, prior_end)

    if _now() >= deadline_at:
        # Partial, not unavailable, and no request: the lane budget is exhausted,
        # which says nothing about whether this filer's XBRL is readable.
        _health_note(health, "cik={0} deadline reached before companyfacts".format(cik))
        return _blank("partial", ticker, period_end, prior_end)

    facts = _companyfacts(cik, session, health)
    ends = _quarter_ends(facts, period_end)
    if not ends or ends[0] != period_end:
        _health_note(health, "cik={0} no facts at {1}".format(cik, period_end))
        return _blank("unavailable", ticker, period_end, prior_end)

    # One snapshot per quarter end, current first. `ends[i + 1]` is only ever the
    # cumulative-difference partner; nothing reads a value from another date.
    snaps = {}
    for i in range(min(HISTORY_QUARTERS + 1, len(ends) - 1)):
        snaps[ends[i]] = _snapshot(facts, ends[i], ends[i + 1])
    if prior_end and prior_end not in snaps and prior_end in ends:
        index = ends.index(prior_end)
        if index + 1 < len(ends):
            snaps[prior_end] = _snapshot(facts, prior_end, ends[index + 1])

    current = snaps.get(period_end) or {}
    if current.get("revenue") is None:
        # The keystone. Without quarterly revenue every days-of-revenue and every
        # margin is unreachable, so there is no panel to publish.
        _health_note(health, "cik={0} revenue unresolved at {1}".format(cik, period_end))
        return _blank("unavailable", ticker, period_end, prior_end)

    prior = snaps.get(prior_end) or {}
    history_ends = [end for end in ends[1:HISTORY_QUARTERS + 1] if end in snaps]

    metrics = {}
    for name in _METRIC_ORDER:
        value = current.get(name)
        if value is None:
            continue          # absent, never zero-filled
        series = [(end, snaps[end].get(name)) for end in history_ends]
        metrics[name] = _metric_entry(name, value, prior.get(name), series)

    # `ok` is unreachable until sec_filing_text exists: the two families it owns
    # are absent from every panel this module can build, and saying so is the
    # whole content of `partial`.
    missing = [family for family, names in _FAMILIES.items()
               if not any(name in metrics for name in names)]
    absent = missing + list(_FILING_TEXT_FAMILIES)
    status = "ok" if not absent else "partial"
    if missing:
        _health_note(health, "cik={0} families absent: {1}".format(cik, ",".join(missing)))

    flags = compute_flags(metrics)
    panel = _blank(status, ticker, period_end, prior_end)
    panel["metrics"] = metrics
    panel["flags"] = flags
    panel["contradictions"] = compute_contradictions(flags, reaction)
    return panel
