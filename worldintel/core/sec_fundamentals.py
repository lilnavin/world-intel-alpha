"""SEC fundamentals layer — period anchoring for post-earnings XBRL lookups.

Given a company's SEC CIK and the calendar date its earnings were reported, this module
resolves which 10-Q/10-K filing is authoritative for that quarter (the "period anchor"),
plus the prior-quarter and year-ago comparable ends a two-beat comparison needs. From
that anchor it walks ordered `companyconcept` tag ladders to extract the metrics and
assemble the published `fundamentals` dict (`build_fundamentals`).

ABSOLUTE RULE (same house rule as fetch_sources.py / earnings_report.py): no external
call may raise out of this module. `fetch_submissions` degrades to `{}` plus
`health["sec_fundamentals"] = {"ok": False, "note": "<short reason>"}` on any failure.

All HTTP goes through `_sec_get_json`, so tests stub that one seam. `_sec_ua()`
resolves the shared configured SEC identity at call time. SEC modules remain
independently loadable without importing earnings_report.
"""

from __future__ import annotations

from worldintel import config as wi_config
import time
from datetime import datetime, timedelta

import requests

# --------------------------------------------------------------------------- #
# Endpoints and tuning constants (fixed across all tasks in this plan)
# --------------------------------------------------------------------------- #
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"
ANCHOR_FORMS = ("10-Q", "10-K")
ANCHOR_WINDOW = (-1, 3)          # filingDate offset from report_date, inclusive
LANE_DEADLINE_S = 45.0
REQUEST_PACING_S = 0.12

_HTTP_TIMEOUT = 25
# SEC returns HTTP 403 without a descriptive, contactable User-Agent.
def _sec_ua() -> str:
    return wi_config.get().sec_user_agent()

# Year-ago comparable window: a calendar year is 364-366 days depending on filing-date
# drift and leap years, so this is a day-count band, never exact `+365` arithmetic.
_PY_WINDOW = (300, 430)

# Average month length in days, used to convert a `months` count into an expected
# duration-fact span. Never exact `months * 30` — real filings run 13-week quarters
# and 52/53-week fiscal years, so matches are always tolerance-banded (`tol_days`).
_AVG_MONTH_DAYS = 30.4368


def _short(exc: Exception) -> str:
    """A short, single-line reason string for health notes."""
    msg = str(exc).split("\n")[0].strip()
    return f"{type(exc).__name__}: {msg}"[:120] if msg else type(exc).__name__


def _parse_day(value):
    """'YYYY-MM-DD' -> date, or None if unparseable/missing."""
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except wi_config.ConfigError:
        raise
    except Exception:
        return None


def _fmt(d):
    return d.isoformat() if d else None


# --------------------------------------------------------------------------- #
# Network seams
# --------------------------------------------------------------------------- #
def _now() -> float:
    """Monotonic wall clock for deadline checks. A seam so deadline behaviour is
    testable without sleeping."""
    return time.monotonic()


def _sleep(seconds: float) -> None:
    """Request pacing. A seam so the offline suite never actually waits."""
    time.sleep(seconds)


def _sec_get_json(url: str, session) -> dict:
    """One HTTP GET against a data.sec.gov endpoint -> parsed JSON, carrying the
    SEC-required contact User-Agent. Isolated so tests can stub every request."""
    sess = session or requests
    resp = sess.get(url, headers={"User-Agent": _sec_ua()}, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_submissions(cik: str, session, health: dict) -> dict:
    """The `subs` dict from SEC's submissions endpoint for `cik`, or `{}` on any
    failure (bad CIK, network error, malformed body, ...)."""
    try:
        return _sec_get_json(SEC_SUBMISSIONS_URL.format(cik=cik), session) or {}
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["sec_fundamentals"] = {"ok": False, "note": _short(exc)}
        return {}


def fetch_concept(cik: str, tag: str, session, *, health=None) -> dict:
    """One `companyconcept` payload for `tag`, or `{}` on any failure.

    A 404 here is NORMAL, not an error: it means this filer does not use this
    tag, and the caller's tag ladder falls through to the next candidate. So
    request diagnostics distinguish absent tags from request failures; pacing lives
    here because concepts are the only high-volume request in the lane.

    The response carries the tag's ENTIRE filed history, which is why one
    request per tag serves the current period, the year-ago comparable and all
    four quarters of a TTM stitch. Never re-request a tag for another period."""
    outcome = {"cik": cik, "tag": tag, "outcome": "ok"}
    try:
        _sleep(REQUEST_PACING_S)
        return _sec_get_json(SEC_CONCEPT_URL.format(cik=cik, tag=tag), session) or {}
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        outcome["outcome"] = "tag_absent" if status == 404 else "http_error" if status else "request_error"
        if isinstance(status, int):
            outcome["http_status"] = status
        return {}
    finally:
        if isinstance(health, dict):
            entry = health.setdefault("sec_fundamentals", {"ok": True})
            entry.setdefault("concept_requests", []).append(outcome)
            if outcome["outcome"] in {"http_error", "request_error"}:
                entry["ok"] = False


# --------------------------------------------------------------------------- #
# Period anchoring (pure)
# --------------------------------------------------------------------------- #
def _walk_back(recent: dict, before, forms=ANCHOR_FORMS):
    """The largest reportDate strictly before `before`, restricted to filings whose
    form is in `forms`. Returns a date or None."""
    if before is None:
        return None
    report_dates = recent.get("reportDate") or []
    forms_arr = recent.get("form") or []
    best = None
    for i, raw in enumerate(report_dates):
        if i < len(forms_arr) and forms_arr[i] not in forms:
            continue
        d = _parse_day(raw)
        if d is None or d >= before:
            continue
        if best is None or d > best:
            best = d
    return best


def _closest_in_window(recent: dict, period_end, lo_days: int, hi_days: int):
    """Among ANCHOR_FORMS filings whose reportDate is `lo_days`-`hi_days` days before
    `period_end` (inclusive), the one closest to `period_end` (largest reportDate).
    Returns a date or None."""
    if period_end is None:
        return None
    report_dates = recent.get("reportDate") or []
    forms_arr = recent.get("form") or []
    best = None
    for i, raw in enumerate(report_dates):
        if i < len(forms_arr) and forms_arr[i] not in ANCHOR_FORMS:
            continue
        d = _parse_day(raw)
        if d is None:
            continue
        delta = (period_end - d).days
        if delta < lo_days or delta > hi_days:
            continue
        if best is None or d > best:
            best = d
    return best


def _recent_ends(recent: dict) -> list:
    """Every distinct ANCHOR_FORMS `reportDate` in the submissions index, most
    recent first. `resolve_ttm`'s five-element `quarter_ends` argument is a slice
    of this list starting at `period_end`, which is the only way to reach the two
    middle quarter ends the anchor itself does not name. Sorted by date, not by
    array position: a 10-K filed out of order must not break the sequence."""
    report_dates = recent.get("reportDate") or []
    forms_arr = recent.get("form") or []
    seen = set()
    for i, raw in enumerate(report_dates):
        if i < len(forms_arr) and forms_arr[i] not in ANCHOR_FORMS:
            continue
        d = _parse_day(raw)
        if d is not None:
            seen.add(d)
    return [_fmt(d) for d in sorted(seen, reverse=True)]


def quarter_ends_from(recent_ends, period_end) -> list:
    """The five consecutive period ends `resolve_ttm` stitches from, i.e. the
    slice of `recent_ends` starting at `period_end`. Shorter than five (or an
    unknown `period_end`) is returned as-is — `resolve_ttm` refuses it."""
    ends = recent_ends if isinstance(recent_ends, list) else []
    try:
        start = ends.index(period_end)
    except ValueError:
        return []
    return ends[start:start + 5]


def resolve_period(subs: dict, report_date: str) -> dict | None:
    """The period anchor for the filing whose `filingDate` falls in
    `[report_date - 1, report_date + 3]` (inclusive) among ANCHOR_FORMS filings in
    `subs["filings"]["recent"]`. Multiple matches: earliest `filingDate`, tied-broken
    by `10-K` over `10-Q`. No match -> None."""
    ref = _parse_day(report_date)
    if ref is None:
        return None
    subs = subs if isinstance(subs, dict) else {}
    filings = subs.get("filings")
    filings = filings if isinstance(filings, dict) else {}
    recent = filings.get("recent")
    recent = recent if isinstance(recent, dict) else {}
    forms = recent.get("form") or []
    filing_dates = recent.get("filingDate") or []
    report_dates = recent.get("reportDate") or []
    accessions = recent.get("accessionNumber") or []
    fys = recent.get("fy") or []
    fps = recent.get("fp") or []
    # The Archives URL sec_filing_text builds is cik + accession + DOCUMENT NAME;
    # an accession alone does not name the document. Read from the same `recent`
    # dict so the name and the accession can never come from different filings.
    docs = recent.get("primaryDocument") or []

    lo = ref + timedelta(days=ANCHOR_WINDOW[0])
    hi = ref + timedelta(days=ANCHOR_WINDOW[1])

    best_idx = None
    best_filed = None
    for i, form in enumerate(forms):
        if form not in ANCHOR_FORMS:
            continue
        filed = _parse_day(filing_dates[i] if i < len(filing_dates) else None)
        if filed is None or filed < lo or filed > hi:
            continue
        is_better = (
            best_filed is None
            or filed < best_filed
            or (filed == best_filed and form == "10-K" and forms[best_idx] != "10-K")
        )
        if is_better:
            best_idx = i
            best_filed = filed

    if best_idx is None:
        return None

    period_end = _parse_day(report_dates[best_idx] if best_idx < len(report_dates) else None)
    if period_end is None:
        return None

    prior_end = _walk_back(recent, period_end)
    py_end = _closest_in_window(recent, period_end, *_PY_WINDOW)
    py_prior_end = _walk_back(recent, py_end) if py_end else None
    fy_end = _walk_back(recent, period_end, forms=("10-K",))

    return {
        "period_end": _fmt(period_end),
        "accession": accessions[best_idx] if best_idx < len(accessions) else None,
        "primary_doc": docs[best_idx] if best_idx < len(docs) else "",
        "form": forms[best_idx],
        "fy": fys[best_idx] if best_idx < len(fys) else None,
        "fp": fps[best_idx] if best_idx < len(fps) else None,
        "prior_end": _fmt(prior_end),
        "py_end": _fmt(py_end),
        "py_prior_end": _fmt(py_prior_end),
        "fy_end": _fmt(fy_end),
        "recent_ends": _recent_ends(recent),
    }


# --------------------------------------------------------------------------- #
# Fact selection and duration ladders (pure)
#
# THE SINGLE SELECTION LAW: every selector below filters on `end == period_end`
# FIRST. There is no "latest fact" code path anywhere in this module — a stale
# series (e.g. one whose newest fact predates the period by years) is simply
# unreachable, never silently wrong.
# --------------------------------------------------------------------------- #
def _facts(concept: dict) -> list:
    """Flatten a companyconcept response's `units` payload
    (`{"USD": [...], "USD/shares": [...]}`) into one list of fact dicts, each
    carrying its originating unit under the `unit` key. Malformed shapes
    (non-dict concept/units, non-list unit values, non-dict fact items)
    degrade to an empty list rather than raising."""
    facts = []
    units = concept.get("units") if isinstance(concept, dict) else None
    if not isinstance(units, dict):
        return facts
    for unit, items in units.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            fact = dict(item)
            fact["unit"] = unit
            facts.append(fact)
    return facts


def is_per_share(unit: str) -> bool:
    """True when `unit` denotes a per-share XBRL unit (`.../shares` or the
    exact string `USD-per-shares`). Used to refuse derived per-share figures
    by unit type, never by company (Global Constraint 6)."""
    if not isinstance(unit, str):
        return False
    return "/shares" in unit or unit == "USD-per-shares"


def _duration_days(fact: dict):
    """Day span of a duration fact's `start`..`end`, or None if unparseable."""
    start = _parse_day(fact.get("start"))
    end = _parse_day(fact.get("end"))
    if start is None or end is None:
        return None
    return (end - start).days


def select_instant(facts: list, period_end) -> dict | None:
    """The instant fact (no `start` — a balance-sheet item) whose `end`
    equals `period_end`. None if no such fact exists."""
    for f in facts:
        if not isinstance(f, dict):
            continue
        if f.get("start"):
            continue
        if f.get("end") == period_end:
            return f
    return None


def select_duration(facts: list, period_end, months: int, tol_days: int = 20) -> dict | None:
    """The duration fact whose `end` equals `period_end` and whose `start`..`end`
    span is closest to `months` months (average month length), within
    `tol_days`. None if no fact qualifies."""
    expected = months * _AVG_MONTH_DAYS
    best = None
    best_diff = None
    for f in facts:
        if not isinstance(f, dict) or not f.get("start") or f.get("end") != period_end:
            continue
        days = _duration_days(f)
        if days is None:
            continue
        diff = abs(days - expected)
        if diff > tol_days:
            continue
        if best is None or diff < best_diff:
            best = f
            best_diff = diff
    return best


def resolve_quarter(facts: list, period_end, prior_end) -> tuple:
    """The quarterly value at `period_end`, via a one-rule ladder (no
    fiscal-Q4 special case):

    1. A ~3-month duration fact at `period_end` -> (fact, "direct").
    2. Else the largest cumulative duration fact at `period_end`, minus the
       cumulative fact at `prior_end` that shares the same `start` ->
       (synthetic_fact, "derived"). Refused (Global Constraint 6) when the
       unit is a per-share unit -> (None, "blocked"). Refused when no
       shared-`start` comparable exists at `prior_end` -> (None, "none").
    3. Else (None, "none")."""
    facts = facts if isinstance(facts, list) else []

    direct = select_duration(facts, period_end, months=3)
    if direct is not None:
        return (direct, "direct")

    candidates = [
        f for f in facts if isinstance(f, dict) and f.get("start") and f.get("end") == period_end
    ]
    if not candidates:
        return (None, "none")

    largest = max(candidates, key=lambda f: _duration_days(f) if _duration_days(f) is not None else -1)

    if is_per_share(largest.get("unit")):
        return (None, "blocked")

    prior_candidates = [
        f
        for f in facts
        if isinstance(f, dict) and f.get("start") == largest.get("start") and f.get("end") == prior_end
    ]
    if not prior_candidates:
        return (None, "none")
    prior_fact = prior_candidates[0]

    try:
        value = largest["val"] - prior_fact["val"]
    except wi_config.ConfigError:
        raise
    except Exception:
        return (None, "none")

    synthetic = {
        "start": prior_end,
        "end": period_end,
        "val": value,
        "unit": largest.get("unit"),
        "form": largest.get("form"),
        "filed": largest.get("filed"),
        "fy": largest.get("fy"),
        "fp": largest.get("fp"),
    }
    return (synthetic, "derived")


def resolve_ttm(facts: list, period_end, quarter_ends) -> tuple:
    """The trailing-twelve-month value ending at `period_end`:

    1. A ~365-day duration fact at `period_end` -> (value, "direct").
    2. Else the sum of the four quarterly values ending at `period_end`, each
       resolved via `resolve_quarter` -> (total, "stitched"). `quarter_ends`
       is the five consecutive period ends `[period_end, q-1, q-2, q-3, q-4]`
       (most recent first); the four (end, prior_end) pairs formed by
       consecutive elements are each fed to `resolve_quarter`. Any missing
       component (fewer than 5 ends, or any quarter unresolved) -> (None,
       "none")."""
    facts = facts if isinstance(facts, list) else []

    direct = select_duration(facts, period_end, months=12)
    if direct is not None:
        try:
            return (direct["val"], "direct")
        except wi_config.ConfigError:
            raise
        except Exception:
            return (None, "none")

    ends = list(quarter_ends) if isinstance(quarter_ends, (list, tuple)) else []
    if len(ends) < 5:
        return (None, "none")

    total = 0
    for i in range(4):
        fact, _basis = resolve_quarter(facts, ends[i], ends[i + 1])
        if fact is None:
            return (None, "none")
        try:
            total += fact["val"]
        except wi_config.ConfigError:
            raise
        except Exception:
            return (None, "none")

    return (total, "stitched")


# --------------------------------------------------------------------------- #
# Tag ladders and metric extraction
#
# Ordered per metric; the first tag that yields a usable fact at `period_end`
# wins. Order is evidence-driven, not alphabetical: `PaymentsToAcquireProductive
# Assets` precedes the PP&E tag because the latter went dead after 2017 for at
# least one large filer, and `Revenues` sits behind the contract-with-customer
# tag because filers who retired it left a stale series behind — unreachable
# only because every selector filters on `end == period_end` first.
#
# Balance-sheet cash and short-term investments are deliberately NOT fetched:
# they existed solely to feed a return-on-capital ratio that is cut (Global
# Constraint 7), and extracting an unused metric costs two requests per company.
# --------------------------------------------------------------------------- #
TAG_LADDERS = {
    "revenue_q": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    ),
    "operating_income_q": ("OperatingIncomeLoss",),
    "net_income_q": ("NetIncomeLoss",),
    "eps_diluted_q": ("EarningsPerShareDiluted",),
    "equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ),
    "ocf_ttm": (
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ),
    "capex_ttm": (
        "PaymentsToAcquireProductiveAssets",
        "PaymentsToAcquirePropertyPlantAndEquipment",
    ),
}

# The single designated home for per-company tag overrides (Global Constraint 5:
# per-company behaviour lives in ordered ladders, never in `if ticker == ...`).
# It ships EMPTY and is deliberately not wired into the ladder walk — every case
# the research surfaced is already handled by the ladders above. Populating it
# requires a dated justification and is a review failure without one.
_TAG_OVERRIDES = {}

# Which selector each metric goes through. `revenue_q` is FIRST because it is the
# keystone: the whole company is `stale` when it finds nothing at `period_end`,
# and discovering that before spending the other tags is the cheap ordering.
_METRIC_ORDER = (
    "revenue_q",
    "operating_income_q",
    "net_income_q",
    "eps_diluted_q",
    "equity",
    "ocf_ttm",
    "capex_ttm",
)
_METRIC_KIND = {
    "revenue_q": "quarter",
    "operating_income_q": "quarter",
    "net_income_q": "quarter",
    "eps_diluted_q": "quarter",
    "equity": "instant",
    "ocf_ttm": "ttm",
    "capex_ttm": "ttm",
}

# Keys of the published dict, in the order §2.2 of the architecture fixes them.
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


def _health_note(health: dict, note: str) -> None:
    """Append a machine-readable note to the lane's health record without
    clobbering `fetch_submissions`'s `{"ok": False, "note": ...}` entry or the
    notes of any other company the lane processed this morning."""
    if not isinstance(health, dict):
        return
    entry = health.get("sec_fundamentals")
    if not isinstance(entry, dict):
        entry = {}
    notes = entry.get("notes")
    if not isinstance(notes, list):
        notes = []
    notes.append(note)
    entry["ok"] = False
    entry["notes"] = notes
    health["sec_fundamentals"] = entry


def _value_of(fact):
    """A fact's numeric value as a float, or None."""
    if not isinstance(fact, dict):
        return None
    try:
        return float(fact["val"])
    except wi_config.ConfigError:
        raise
    except Exception:
        return None


def _as_float(value):
    try:
        return float(value)
    except wi_config.ConfigError:
        raise
    except Exception:
        return None


def _yoy_pct(current, prior):
    """Percent change against the year-ago comparable. `abs(prior)` is the
    denominator so a negative base does not silently flip the sign of the
    change; a zero or missing base yields None rather than a division error."""
    if current is None or prior is None or prior == 0:
        return None
    try:
        return (current - prior) / abs(prior) * 100.0
    except wi_config.ConfigError:
        raise
    except Exception:
        return None


def _metric_obj(value, basis, tag, py_value, definition=None) -> dict:
    obj = {
        "value": _as_float(value),
        "basis": basis,
        "tag": tag,
        "yoy_pct": _yoy_pct(_as_float(value), _as_float(py_value)),
    }
    if definition is not None:
        obj["definition"] = definition
    return obj


def _blank_fundamentals(status: str, anchor=None, notes=None) -> dict:
    """The §2.2 dict with every key present and every metric None. `status` is
    never null; the anchor fields survive a `stale` verdict because a stale
    company HAS an identified filing — it just has no facts in it yet."""
    anchor = anchor if isinstance(anchor, dict) else {}
    fiscal = None
    if anchor:
        fiscal = {"fy": anchor.get("fy"), "fp": anchor.get("fp")}
    out = {
        "status": status,
        "source": "companyconcept",
        "period_end": anchor.get("period_end"),
        "fiscal": fiscal,
        "accession": anchor.get("accession"),
        "form": anchor.get("form"),
        # Published for the two lanes run.py drives after this one. Both are
        # resolved by the anchor above and were previously dropped here, which
        # left neither lane callable from run.py.
        #
        # NAMING TRAP: `py_end` is the YEAR-AGO quarter end. The anchor's
        # `prior_end` is the IMMEDIATELY PRECEDING quarter (the cumulative-
        # difference partner) -- a different date entirely. `anomaly_panel`
        # calls its year-ago argument `prior_end`, so run.py maps py_end onto
        # that argument at the call site. Do not rename this key to match it.
        "py_end": anchor.get("py_end"),
        "primary_doc": anchor.get("primary_doc"),
    }
    for key in _METRIC_KEYS:
        out[key] = None
    out["segments"] = []          # Phase 2 populates this; Phase 1 ships [].
    out["notes"] = list(notes) if notes else []
    return out


def _resolve_metric(kind: str, facts: list, anchor: dict, quarter_ends, py_quarter_ends):
    """(value, basis, py_value) for one already-fetched tag. The year-ago
    comparable is resolved from the SAME fact list — no second request."""
    if kind == "instant":
        current = select_instant(facts, anchor.get("period_end"))
        prior = select_instant(facts, anchor.get("py_end"))
        basis = "direct" if current is not None else "none"
        return (_value_of(current), basis, _value_of(prior))

    if kind == "quarter":
        current, basis = resolve_quarter(facts, anchor.get("period_end"), anchor.get("prior_end"))
        prior, _prior_basis = resolve_quarter(facts, anchor.get("py_end"), anchor.get("py_prior_end"))
        return (_value_of(current), basis, _value_of(prior))

    value, basis = resolve_ttm(facts, anchor.get("period_end"), quarter_ends)
    py_value, _py_basis = resolve_ttm(facts, anchor.get("py_end"), py_quarter_ends)
    return (value, basis, py_value)


def _ratio(numerator, denominator):
    """`numerator / denominator * 100`, or None when the denominator is zero,
    negative or missing. A ratio over a vanishing denominator is noise, not a
    number worth publishing."""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    try:
        return numerator / denominator * 100.0
    except wi_config.ConfigError:
        raise
    except Exception:
        return None


def _derive_ratios(fund: dict, py_values: dict) -> None:
    """Fill the two published ratios in place. Both operands of a ratio must
    have resolved AND share a basis — mixing a directly reported figure with a
    derived one produces a number whose provenance cannot be stated."""
    revenue = fund.get("revenue_q")
    op_income = fund.get("operating_income_q")
    if revenue and op_income and revenue["basis"] == op_income["basis"]:
        current = _ratio(op_income["value"], revenue["value"])
        prior = _ratio(py_values.get("operating_income_q"), py_values.get("revenue_q"))
        if current is not None:
            fund["op_margin_pct"] = {
                "value": current,
                "yoy_delta_pp": (current - prior) if prior is not None else None,
            }

    ocf = fund.get("ocf_ttm")
    capex = fund.get("capex_ttm")
    if ocf and capex:
        py_ocf = py_values.get("ocf_ttm")
        py_capex = py_values.get("capex_ttm")

        if ocf["basis"] == capex["basis"]:
            # `capex / (OCF + capex)` on purpose: cash generated plus cash
            # reinvested is a broad, stable base. The thin alternative
            # denominators are what produced a triple-digit percentage for a
            # company that had no such thing, and are not to be reintroduced.
            current = _ratio(capex["value"], ocf["value"] + capex["value"])
            prior = None
            if py_ocf is not None and py_capex is not None:
                prior = _ratio(py_capex, py_ocf + py_capex)
            if current is not None:
                fund["capex_intensity_pct"] = {
                    "value": current,
                    "yoy_delta_pp": (current - prior) if prior is not None else None,
                }

        # FCF is a subtraction, so its basis is "derived" by construction and it
        # has no XBRL tag of its own. A negative result is a real, publishable
        # fact about a company out-investing its operating cash flow.
        fcf = ocf["value"] - capex["value"]
        py_fcf = None
        if py_ocf is not None and py_capex is not None:
            py_fcf = py_ocf - py_capex
        fund["fcf_ttm"] = _metric_obj(fcf, "derived", None, py_fcf, definition="gaap_cashflow")


def build_fundamentals(cik: str, report_date: str, session, health: dict, deadline_at: float) -> dict:
    """The §2.2 `fundamentals` dict for one company. Never raises.

    `report_date` is the calendar date earnings were announced; `deadline_at` is
    a `_now()`-comparable monotonic instant the whole lane must respect.
    Returns `status == "unavailable"` when no filing can be identified,
    `"stale"` when the filing exists but XBRL carries no fact at its
    `period_end`, otherwise `"ok"`."""
    try:
        return _build_fundamentals(cik, report_date, session, health, deadline_at)
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        _health_note(health, "cik={0} build failed: {1}".format(cik, _short(exc)))
        return _blank_fundamentals("unavailable")


def _build_fundamentals(cik, report_date, session, health, deadline_at) -> dict:
    if not cik:
        _health_note(health, "no cik, fundamentals skipped")
        return _blank_fundamentals("unavailable")

    anchor = resolve_period(fetch_submissions(cik, session, health), report_date)
    if not anchor:
        _health_note(health, "cik={0} unavailable (no anchor filing near {1})".format(cik, report_date))
        return _blank_fundamentals("unavailable")

    period_end = anchor.get("period_end")
    recent_ends = anchor.get("recent_ends") or []
    quarter_ends = quarter_ends_from(recent_ends, period_end)
    py_quarter_ends = quarter_ends_from(recent_ends, anchor.get("py_end"))

    metrics = {}
    py_values = {}
    notes = []
    keystone_fact_seen = False
    truncated = False
    request_start = len(health.get("sec_fundamentals", {}).get("concept_requests", []))

    for metric in _METRIC_ORDER:
        kind = _METRIC_KIND[metric]
        for tag in TAG_LADDERS[metric]:
            if _now() >= deadline_at:
                truncated = True
                _health_note(health, "cik={0} deadline reached at {1}".format(cik, metric))
                break

            facts = _facts(fetch_concept(cik, tag, session, health=health))

            if metric == "revenue_q" and not keystone_fact_seen:
                # The keystone gate is duration-agnostic on purpose: a fiscal Q4
                # can carry only the full-year fact, and that still proves XBRL
                # has caught up with this period.
                keystone_fact_seen = any(
                    isinstance(f, dict) and f.get("end") == period_end for f in facts
                )

            value, basis, py_value = _resolve_metric(
                kind, facts, anchor, quarter_ends, py_quarter_ends
            )

            if metric == "eps_diluted_q" and basis == "blocked" and "eps_derived_suppressed" not in notes:
                # A per-share figure produced by subtraction is arithmetically
                # wrong (share counts move between periods), so it is refused.
                notes.append("eps_derived_suppressed")

            if value is None:
                continue      # this tag missed; try the next rung of the ladder

            definition = "gaap_cashflow" if metric == "capex_ttm" else None
            metrics[metric] = _metric_obj(value, basis, tag, py_value, definition=definition)
            py_values[metric] = py_value
            break

        if truncated:
            break

        if metric == "revenue_q" and metrics.get("revenue_q") is None:
            requests_seen = health.get("sec_fundamentals", {}).get("concept_requests", [])[request_start:]
            if any(entry.get("outcome") in {"http_error", "request_error"} for entry in requests_seen):
                _health_note(health, "cik={0} unavailable (revenue requests failed)".format(cik))
                return _blank_fundamentals("unavailable", anchor)
            if requests_seen and all(entry.get("outcome") == "tag_absent" for entry in requests_seen):
                _health_note(health, "cik={0} unavailable (no supported revenue tags)".format(cik))
                return _blank_fundamentals("unavailable", anchor)
            # §2.3: "ok" means the keystone RESOLVED, not merely that a fact for
            # it exists. A cumulative at period_end with no same-start
            # comparable at prior_end leaves nothing publishable, so it is stale
            # too — but the note distinguishes the two causes, because "XBRL has
            # not backfilled this period" and "the facts are there but will not
            # difference" are different problems with different fixes.
            reason = (
                "revenue fact at {0} did not resolve".format(period_end)
                if keystone_fact_seen
                else "no facts at {0}".format(period_end)
            )
            _health_note(health, "cik={0} stale ({1})".format(cik, reason))
            return _blank_fundamentals("stale", anchor)

    if metrics.get("revenue_q") is None:
        # Only reachable via the deadline break above: truncated before the
        # keystone resolved. We cannot claim the filing is stale — we never got
        # far enough to say.
        return _blank_fundamentals("unavailable", anchor, notes)

    fund = _blank_fundamentals("ok", anchor, notes)
    fund.update(metrics)
    _derive_ratios(fund, py_values)
    return fund
