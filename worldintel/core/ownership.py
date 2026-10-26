"""SEC ownership filings — pure parsing and selection layer for Form 4 and
Schedule 13D.

Implements the three parsers that turn raw EDGAR text into plain dicts/lists:
`parse_daily_index` (the daily form index), `parse_form4` (insider transaction
reports) and `parse_schedule13d` (5%+ activist stake filings), plus the small
`classify_action` lookup they share. `select_insider` and `select_13d` then
rank and cap those rows into what's worth surfacing in the brief.

The network layer (`fetch_daily_index`, `fetch_doc`, `fetch_prior_13d`) and
the `build_ownership` orchestrator sit in this same module — the day's index
enumeration, per-document fetch/parse and ranking are one feature, matching
the shape of `earnings_report.py` / `sec_fundamentals.py` rather than the
flat one-call-per-source layer in `fetch_sources.py`.

ABSOLUTE RULE (same house rule as fetch_sources.py / sec_fundamentals.py): no
external call may raise out of this module. The pure parsers above touch no
network themselves, but they inherit the rule's spirit — malformed, truncated
or outright garbage input degrades to empty/zero output, never an exception.
The network functions below wrap every request so a failure degrades to a
health entry plus empty/partial output, never a raised exception.
"""

from __future__ import annotations

from worldintel import config as wi_config
import html
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

import requests

from worldintel.core import earnings_report
from worldintel.core import fetch_sources

# --------------------------------------------------------------------------- #
# Tuning constants
# --------------------------------------------------------------------------- #
_INDEX_HEADER_LINES = 9
_MAX_PURPOSE_CHARS = 120

_ACTION_CODES = {
    "P": "open-market buy",
    "S": "sale",
    "A": "grant",
    "M": "option exercise",
    "F": "tax withholding",
    "G": "gift",
}

_TRUTHY_STRINGS = {"true", "1"}

_OWNERSHIP_DOC_RE = re.compile(r"<ownershipDocument\b.*?</ownershipDocument>", re.DOTALL | re.IGNORECASE)


def classify_action(code: str) -> str:
    """The plain-English label for a Form 4 `transactionCode`. Unknown, empty
    or missing codes -> "other"."""
    return _ACTION_CODES.get(code, "other")


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _as_float(value) -> float:
    """`value` as a float, or 0.0 when missing/unparseable. Never raises."""
    try:
        return float(value)
    except wi_config.ConfigError:
        raise
    except Exception:
        return 0.0


def _truthy(value) -> bool:
    """"true"/"1" (case-insensitive, whitespace-stripped) -> True. Everything
    else -- "false", "0", absent, empty, or any other spelling -- -> False.
    A naive `bool(text)` would mark every filing planned, since `"false"` is
    itself a non-empty, truthy Python string."""
    if value is None:
        return False
    return str(value).strip().lower() in _TRUTHY_STRINGS


def _find_text(elem, path: str) -> str:
    """The stripped text of the element `path` resolves to under `elem`, or
    "" when `elem`/the path/the text is missing. `path` may be a simple tag
    name, a slash-joined relative path, or an ElementTree ".//tag" search."""
    if elem is None:
        return ""
    found = elem.find(path)
    if found is None or found.text is None:
        return ""
    return found.text.strip()


# --------------------------------------------------------------------------- #
# parse_daily_index
# --------------------------------------------------------------------------- #
def _fmt_yyyymmdd(value: str):
    """'YYYYMMDD' -> 'YYYY-MM-DD', or None when not exactly 8 digits."""
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        return None
    return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"


def parse_daily_index(text: str) -> list[dict]:
    """EDGAR's daily form index -> one dict per filing row.

    Splits on runs of two or more spaces (`re.split(r'\\s{2,}', ...)`), never
    a fixed-width slice: `SCHEDULE 13D/A` is 14 characters and overflows a
    12-character form-type column, so `line[:12]` silently merges amendments
    into the `SCHEDULE 13D` bucket. Skips the 9-line header and any line that
    does not yield at least 5 fields (which also disposes of the index's
    "Date Filed  File Name" continuation line and its dashed rule, both of
    which trail the 9-line header proper). Never raises; garbage input -> []."""
    try:
        lines = text.splitlines()
    except wi_config.ConfigError:
        raise
    except Exception:
        return []

    rows: list[dict] = []
    for line in lines[_INDEX_HEADER_LINES:]:
        try:
            fields = re.split(r"\s{2,}", line.strip())
            if len(fields) < 5:
                continue
            form, company, cik, filed_raw, path = fields[:5]
            filed = _fmt_yyyymmdd(filed_raw)
            if filed is None:
                continue
            rows.append({"form": form, "company": company, "cik": cik, "filed": filed, "path": path})
        except wi_config.ConfigError:
            raise
        except Exception:
            continue
    return rows


# --------------------------------------------------------------------------- #
# parse_form4
# --------------------------------------------------------------------------- #
def _extract_ownership_xml(text: str):
    """The `<ownershipDocument>...</ownershipDocument>` substring of a Form 4
    submission `.txt` (SGML wrapper with XML inside), or None when absent."""
    if not isinstance(text, str):
        return None
    match = _OWNERSHIP_DOC_RE.search(text)
    return match.group(0) if match else None


def _resolve_role(root) -> str:
    """`officerTitle` when present; else "Director" if `isDirector` is
    truthy; else "10% owner" if `isTenPercentOwner` is truthy; else ""."""
    base = "reportingOwner/reportingOwnerRelationship/"
    officer_title = _find_text(root, base + "officerTitle")
    if officer_title:
        return officer_title
    if _truthy(_find_text(root, base + "isDirector")):
        return "Director"
    if _truthy(_find_text(root, base + "isTenPercentOwner")):
        return "10% owner"
    return ""


def _parse_transactions(root, table_tag: str, txn_tag: str, derivative: bool, common: dict) -> list[dict]:
    table = root.find(table_tag)
    if table is None:
        return []
    rows = []
    for txn in table.findall(txn_tag):
        code = _find_text(txn, "transactionCoding/transactionCode")
        shares = _as_float(_find_text(txn, "transactionAmounts/transactionShares/value"))
        price = _as_float(_find_text(txn, "transactionAmounts/transactionPricePerShare/value"))
        value = shares * price if price else 0.0
        row = dict(common)
        row.update({
            "code": code,
            "action": classify_action(code),
            "shares": shares,
            "price": price,
            "value": value,
            "date": _find_text(txn, "transactionDate/value"),
            "derivative": derivative,
        })
        rows.append(row)
    return rows


def parse_form4(text: str) -> list[dict]:
    """A Form 4 submission `.txt` -> one row per transaction (both
    non-derivative and derivative tables). Issuer/owner fields are shared
    across every row; `html`-entity-escaped issuer names (e.g. `&amp;`) come
    out decoded because the XML parser resolves entities itself. Never
    raises; unparseable input -> []."""
    try:
        return _parse_form4(text)
    except wi_config.ConfigError:
        raise
    except Exception:
        return []


def _parse_form4(text: str) -> list[dict]:
    xml_text = _extract_ownership_xml(text)
    if xml_text is None:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    common = {
        "ticker": _find_text(root, "issuer/issuerTradingSymbol"),
        "company": _find_text(root, "issuer/issuerName"),
        "person": _find_text(root, "reportingOwner/reportingOwnerId/rptOwnerName"),
        "role": _resolve_role(root),
        "planned": _truthy(_find_text(root, "aff10b5One")),
    }

    rows = _parse_transactions(root, "nonDerivativeTable", "nonDerivativeTransaction", False, common)
    rows += _parse_transactions(root, "derivativeTable", "derivativeTransaction", True, common)

    return [r for r in rows if r["company"] or r["person"]]


# --------------------------------------------------------------------------- #
# parse_schedule13d
# --------------------------------------------------------------------------- #
def _strip_namespaces(root):
    """Rewrite every element's tag from Clark notation (`{uri}tag`) to its
    local name in place. Schedule 13D XML mixes a default namespace with
    per-element `com:`/inline `xmlns` overrides on address fields we never
    read; stripping once up front means every `find()` below can use plain
    tag names regardless of which namespace form a filing used."""
    for elem in root.iter():
        if isinstance(elem.tag, str) and "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]
    return root


def _fmt_mmddyyyy(value: str) -> str:
    """'MM/DD/YYYY' -> 'YYYY-MM-DD', or "" when not in that shape."""
    if not isinstance(value, str):
        return ""
    match = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", value.strip())
    if not match:
        return ""
    mm, dd, yyyy = match.groups()
    return f"{yyyy}-{mm}-{dd}"


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(text))


def _truncate_on_word_boundary(text: str, limit: int) -> str:
    """`text` truncated to at most `limit` characters, cut on a word
    boundary, with a trailing "…" when truncation happened. Unchanged when
    already within the limit."""
    if len(text) <= limit:
        return text
    truncated = text[: limit - 1]
    cut = truncated.rfind(" ")
    if cut > 0:
        truncated = truncated[:cut]
    return truncated.rstrip() + "…"


def _purpose_text(root) -> str:
    raw = _find_text(root, ".//item4/transactionPurpose")
    if not raw:
        return ""
    collapsed = re.sub(r"\s+", " ", _strip_html(raw)).strip()
    return _truncate_on_word_boundary(collapsed, _MAX_PURPOSE_CHARS)


def _reporting_persons(root) -> list[dict]:
    persons = []
    for rp in root.findall(".//reportingPersons/reportingPersonInfo"):
        name = _find_text(rp, "reportingPersonName")
        if not name:
            continue
        persons.append({
            "name": name,
            "percent": _as_float(_find_text(rp, "percentOfClass")),
            "shares": _as_float(_find_text(rp, "aggregateAmountOwned")),
        })
    return persons


def _blank_schedule13d() -> dict:
    return {
        "company": "",
        "issuer_cik": "",
        "filer": "",
        "percent": 0.0,
        "shares": 0.0,
        "event_date": "",
        "purpose": "",
        "filers": [],
    }


def parse_schedule13d(xml: str) -> dict:
    """A Schedule 13D / 13D-A submission XML -> one summary dict. `filer`/
    `percent`/`shares` describe the reporting person with the maximum
    `percentOfClass`; `filers` lists every reporting person's name (a 13D
    group commonly has several). Never raises; unparseable input -> a dict
    with every field empty/zero."""
    try:
        return _parse_schedule13d(xml)
    except wi_config.ConfigError:
        raise
    except Exception:
        return _blank_schedule13d()


def _parse_schedule13d(xml: str) -> dict:
    if not isinstance(xml, str):
        return _blank_schedule13d()
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return _blank_schedule13d()
    root = _strip_namespaces(root)

    persons = _reporting_persons(root)
    if persons:
        best = max(persons, key=lambda p: p["percent"])
        filer, percent, shares = best["name"], best["percent"], best["shares"]
    else:
        filer, percent, shares = "", 0.0, 0.0

    return {
        "company": _find_text(root, ".//issuerName"),
        "issuer_cik": _find_text(root, ".//issuerCIK"),
        "filer": filer,
        "percent": percent,
        "shares": shares,
        "event_date": _fmt_mmddyyyy(_find_text(root, ".//dateOfEvent")),
        "purpose": _purpose_text(root),
        "filers": [p["name"] for p in persons],
    }


# --------------------------------------------------------------------------- #
# Selection layer
# --------------------------------------------------------------------------- #
BUY_FLOOR_USD = 1_000_000
WATCHLIST_CAP = 4
BUY_CAP = 3
STAKE_CAP = 4
ACTIVIST_FILERS = [
    "elliott", "starboard", "trian", "valueact", "icahn",
    "pershing square", "third point", "jana", "engaged capital",
]

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _norm_ticker(value) -> str:
    return str(value or "").strip().casefold()


def _insider_sort_key(row: dict):
    return (-_as_float(row.get("value")), str(row.get("ticker") or ""), str(row.get("person") or ""))


def select_insider(rows: list[dict], watchlist_tickers, floor=BUY_FLOOR_USD) -> tuple[list, list]:
    """`rows` (parse_form4-shaped transaction dicts) -> `(insider_watchlist,
    insider_buys)`. The watchlist leg keeps every transaction whose `ticker`
    is in `watchlist_tickers` (case-insensitive) regardless of `code` or
    `derivative` -- a scheduled CEO sale is legitimate brief material -- and
    ranks by `value` descending, capped at `WATCHLIST_CAP`. The buy leg keeps
    only open-market buys (`code == "P"`, non-derivative, `value >= floor`),
    ranked the same way and capped at `BUY_CAP`, excluding any ticker already
    claimed by the (capped) watchlist leg so a company never renders twice.
    Ties on `value` break by `ticker` then `person`, both ascending, for
    deterministic output. Never raises; malformed rows (missing `value`) sort
    as 0.0. Does not mutate `rows`."""
    try:
        return _select_insider(rows, watchlist_tickers, floor)
    except wi_config.ConfigError:
        raise
    except Exception:
        return [], []


def _select_insider(rows, watchlist_tickers, floor):
    watch_set = {_norm_ticker(t) for t in (watchlist_tickers or [])}

    watchlist_rows = [
        row for row in (rows or [])
        if isinstance(row, dict) and _norm_ticker(row.get("ticker")) in watch_set
    ]
    watchlist_sorted = sorted(watchlist_rows, key=_insider_sort_key)[:WATCHLIST_CAP]
    claimed = {_norm_ticker(row.get("ticker")) for row in watchlist_sorted}

    buy_rows = [
        row for row in (rows or [])
        if isinstance(row, dict)
        and row.get("code") == "P"
        and row.get("derivative") is False
        and _as_float(row.get("value")) >= floor
        and _norm_ticker(row.get("ticker")) not in claimed
    ]
    buy_sorted = sorted(buy_rows, key=_insider_sort_key)[:BUY_CAP]

    return watchlist_sorted, buy_sorted


def _normalize_filer_name(text) -> str:
    """casefold -> non-alphanumeric runs to single spaces -> stripped."""
    return _NON_ALNUM_RE.sub(" ", str(text or "").casefold()).strip()


def _matches_allowlist(filer_name, allowlist) -> bool:
    normalized = _normalize_filer_name(filer_name)
    if not normalized:
        return False
    for entry in allowlist:
        pattern = r"\b" + re.escape(_normalize_filer_name(entry)) + r"\b"
        if re.search(pattern, normalized):
            return True
    return False


def _is_amendment(stake: dict) -> bool:
    return str(stake.get("form") or "").endswith("/A")


def _stake_sort_key(stake: dict):
    return (1 if _is_amendment(stake) else 0, -_as_float(stake.get("percent")), str(stake.get("company") or ""))


def select_13d(stakes: list[dict], allowlist=ACTIVIST_FILERS) -> list:
    """`stakes` (parse_schedule13d-shaped dicts, each carrying the caller-added
    `form`/`filed`) -> the filings worth surfacing. Every non-amendment
    (`form` not ending in `/A`) is kept; an amendment is kept only when a name
    in its `filers` list matches `allowlist` on word boundaries after both
    sides are casefolded, non-alphanumeric-collapsed-to-space normalization
    (so `"elliott"` matches `"ELLIOTT INVESTMENT MANAGEMENT, L.P."` but not a
    filer like `"Janacek Capital"`). New filings rank above amendments, then
    by `percent` descending, ties by `company` ascending; capped at
    `STAKE_CAP`. Never raises; malformed rows (missing `percent`) sort as
    0.0. Does not mutate `stakes`."""
    try:
        return _select_13d(stakes, allowlist)
    except wi_config.ConfigError:
        raise
    except Exception:
        return []


def _select_13d(stakes, allowlist):
    kept = []
    for stake in stakes or []:
        if not isinstance(stake, dict):
            continue
        if _is_amendment(stake):
            filers = stake.get("filers") or []
            if not any(_matches_allowlist(name, allowlist) for name in filers):
                continue
        kept.append(stake)

    return sorted(kept, key=_stake_sort_key)[:STAKE_CAP]


# --------------------------------------------------------------------------- #
# Network layer
# --------------------------------------------------------------------------- #
DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{qtr}/form.{yyyymmdd}.idx"
ARCHIVES_BASE = "https://www.sec.gov/Archives/"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SCAN_WORKERS = 6
# SEC's fair-access guidance caps automated traffic at 10 requests/second per
# IP. SCAN_WORKERS bounds CONCURRENCY, not rate -- 6 workers against a healthy
# EDGAR measured 137 req/s, 13x over. An explicit limiter is what enforces the
# rate; the worker count only caps how many sockets are open at once. This
# module is the first here to fetch SEC concurrently, so nothing else throttles
# and an IP block would take earnings_report (CIK map, 8-K text) and
# sec_fundamentals (XBRL) down with it -- they share the host.
SCAN_MAX_RPS = 10.0
_SCAN_MIN_INTERVAL_S = 1.0 / SCAN_MAX_RPS
# SEC returns HTTP 403 without a descriptive, contactable User-Agent.
def _sec_ua() -> str:
    return wi_config.get().sec_user_agent()
_HTTP_TIMEOUT = 25

# Nice-to-have enrichment (fetch_prior_13d) — bounded so it can never cost the
# run materially even when several allowlisted amendments qualify.
_PRIOR_13D_LOOKUP_BUDGET = 2

# Per-call cap inside fetch_prior_13d itself: at most this many form-matching
# candidates are fetched/parsed while walking one issuer's filing history,
# regardless of how many SCHEDULE 13D-type entries that issuer has on file
# (up to ~1,000 for a heavily-13D'd issuer). Without this, a single lookup
# could cost dozens of serial requests.
_PRIOR_13D_CANDIDATE_CAP = 8

# Both SCHEDULE 13D forms feed the same leg (SCHEDULE 13D and SCHEDULE 13D/A).
# The ONE predicate for "is this a 13D-type row" -- `_build_ownership` and
# `fetch_prior_13d` both read this tuple rather than each spelling their own
# `startswith` test.
_SCHEDULE_13D_FORMS = ("SCHEDULE 13D", "SCHEDULE 13D/A")

# How many business days back fetch_daily_index will walk. Six, not four:
# the cron runs 07:00 JST = 18:00 ET the PREVIOUS day, and the index publishes
# ~22:00 ET, so at run time the newest published index is for ET day D-2 and
# two attempts are structurally wasted on guaranteed 404s every single run.
# Six attempts preserves the intended four business days of real headroom.
_INDEX_FALLBACK_ATTEMPTS = 6

# Whole-lane wall-clock budget for build_ownership, in seconds. Compared
# against _now() (time.monotonic()), NOT time.time() -- deadline_at anywhere
# in this module is a monotonic instant. Sized at ~333 unique Form 4 filings
# for a normal day (the 712 figure that once stood here counted index LINES,
# 2.14 per filing) which is ~33s at SCAN_MAX_RPS=10; heavy days run several
# times that, so this is deliberately NOT
# sec_fundamentals.LANE_DEADLINE_S (45.0) reused --
# 45s would truncate this lane routinely. Kept local so this module stays
# self-contained; Task 5 derives deadline_at = ownership._now() +
# ownership.LANE_DEADLINE_S when calling build_ownership, mirroring the
# sibling pattern at earnings_report.py's sf._now() + sf.LANE_DEADLINE_S.
LANE_DEADLINE_S = 90.0


def _short(exc: Exception) -> str:
    """A short, single-line reason string for health notes (same shape as the
    sibling modules' `_short`)."""
    msg = str(exc).split("\n")[0].strip()
    return f"{type(exc).__name__}: {msg}"[:120] if msg else type(exc).__name__


def _now() -> float:
    """Monotonic clock for deadline checks (mirrors
    `sec_fundamentals._now()`). A seam so deadline behaviour is testable
    without sleeping. `deadline_at` throughout this module is a
    `_now()`-comparable monotonic instant -- NOT `time.time()`, which is a
    different epoch entirely and would make every deadline look as if it had
    already passed."""
    return time.monotonic()


class _RateLimiter:
    """A minimum-interval throttle shared by every market-wide scan worker.

    Hands out request slots no closer together than `min_interval` seconds,
    which is what actually holds the scan inside SEC's 10 req/s fair-access
    guidance -- `SCAN_WORKERS` only caps how many requests are in flight.

    Thread safety: `_next_at` is shared state touched by every worker, so the
    slot arithmetic runs under a lock. The `time.sleep` deliberately happens
    OUTSIDE it: holding the lock across the sleep would serialize the workers
    behind one another for the full wait instead of letting each one wait out
    its own reserved slot in parallel.

    Uses `_now()` (monotonic), never `time.time()` -- same clock convention as
    every deadline in this module."""

    def __init__(self, min_interval: float):
        self.min_interval = float(min_interval)
        self._lock = threading.Lock()
        self._next_at = 0.0  # monotonic; 0.0 means "the first slot is free"

    def acquire(self) -> float:
        """Reserve the next slot, sleeping until it opens. Returns the number
        of seconds waited (0.0 when the slot was already free)."""
        with self._lock:
            now = _now()
            wait_s = self._next_at - now
            if wait_s < 0.0:
                wait_s = 0.0
            self._next_at = now + wait_s + self.min_interval
        if wait_s > 0.0:
            time.sleep(wait_s)
        return wait_s


# One limiter per process, shared by every scan worker.
_SCAN_LIMITER = _RateLimiter(_SCAN_MIN_INTERVAL_S)


def _daily_index_url(day: str) -> str:
    """'YYYY-MM-DD' -> the daily-index URL for that date. The quarter is
    `(month - 1) // 3 + 1`."""
    year, month = day[0:4], day[5:7]
    qtr = (int(month) - 1) // 3 + 1
    return DAILY_INDEX_URL.format(year=year, qtr=qtr, yyyymmdd=day.replace("-", ""))


def _prev_business_day(day: str) -> str:
    """The nearest calendar day before `day` that is not a Saturday or Sunday."""
    d = datetime.strptime(day, "%Y-%m-%d").date() - timedelta(days=1)
    while d.weekday() >= 5:  # Saturday=5, Sunday=6
        d -= timedelta(days=1)
    return d.isoformat()


def fetch_daily_index(today: str, session, health: dict) -> list[dict]:
    """The parsed daily form index for `today`.

    The index publishes around 22:00 ET, so a run before that gets a 404 for
    "today". On a 404 (or an empty parse), retries the previous business day,
    walking back one business day at a time (Saturday/Sunday skipped), up to
    `_INDEX_FALLBACK_ATTEMPTS` attempts. Records which date actually succeeded
    in `health["ownership_index"]`: `{"ok": True, "note": ""}` when the first
    choice worked, `{"ok": True, "note": "fell back to <date>"}` otherwise, or
    `{"ok": False, "note": "<short reason>"}` on total failure. Never raises;
    total failure -> []."""
    day = today
    reason = "unknown failure"
    for attempt in range(_INDEX_FALLBACK_ATTEMPTS):
        try:
            resp = session.get(
                _daily_index_url(day), headers={"User-Agent": _sec_ua()}, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
            rows = parse_daily_index(resp.text)
            if rows:
                note = "" if attempt == 0 else f"fell back to {day}"
                health["ownership_index"] = {"ok": True, "note": note}
                return rows
            reason = f"empty index for {day}"
        except wi_config.ConfigError:
            raise
        except Exception as exc:
            reason = _short(exc)
        day = _prev_business_day(day)
    health["ownership_index"] = {"ok": False, "note": reason}
    return []


def fetch_doc(path: str, session) -> str:
    """The body text at `ARCHIVES_BASE + path`, or "" on any failure. Never
    raises."""
    try:
        resp = session.get(
            ARCHIVES_BASE + path, headers={"User-Agent": _sec_ua()}, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except wi_config.ConfigError:
        raise
    except Exception:
        return ""


def _new_session():
    """A fresh `requests.Session` — the seam the market-wide scan's per-thread
    sessions are created through. `requests.Session` is not documented
    thread-safe, so each `ThreadPoolExecutor` worker gets its own via this
    function rather than sharing the caller's session. Isolated so tests can
    stub it and never touch the real network."""
    return requests.Session()


def _strip_dashes(accession) -> str:
    return str(accession or "").replace("-", "")


def _split_index_path(index_path: str) -> tuple:
    """`edgar/data/{cik}/{accession}.txt` -> `(prefix, accession)`, where
    `prefix` is `edgar/data/{cik}` and `accession` keeps its dashes."""
    prefix, _, filename = str(index_path or "").rpartition("/")
    accession = filename[:-4] if filename.endswith(".txt") else filename
    return prefix, accession


def _schedule13d_doc_path(index_path: str) -> str:
    """The daily index gives `edgar/data/{cik}/{accession}.txt`; the
    structured Schedule 13D document lives at
    `edgar/data/{cik}/{accession-with-dashes-removed}/primary_doc.xml`."""
    prefix, accession = _split_index_path(index_path)
    return f"{prefix}/{_strip_dashes(accession)}/primary_doc.xml"


def _filing_index_url(index_path: str) -> str:
    """The human-facing SEC filing index page for a daily-index `.txt` path,
    e.g. `edgar/data/1821468/0001193125-26-326663.txt` ->
    `https://www.sec.gov/Archives/edgar/data/1821468/000119312526326663/
    0001193125-26-326663-index.htm`. Both the raw `.txt` and the bare
    structured document (`primary_doc.xml` / Form 4 XML) render as unreadable
    source in a browser; this is the page the company-name link in the brief
    should point to. "" on a malformed path."""
    prefix, accession = _split_index_path(index_path)
    if not prefix or not accession:
        return ""
    return f"{ARCHIVES_BASE}{prefix}/{_strip_dashes(accession)}/{accession}-index.htm"


def _zfill_cik(value) -> str:
    """`value` as a zero-padded 10-digit CIK string, or "" if unparseable."""
    try:
        return str(int(value)).zfill(10)
    except wi_config.ConfigError:
        raise
    except Exception:
        return ""


def fetch_prior_13d(issuer_cik: str, filer: str, current_accession: str, session,
                     deadline_at: float) -> float | None:
    """The filer's immediately-previous `SCHEDULE 13D`/`SCHEDULE 13D/A`
    `percent` on the same issuer, or `None` on any failure, miss, or empty
    result.

    Fetches `SUBMISSIONS_URL` for the issuer CIK and walks `filings.recent`
    newest-first, skipping `current_accession` EXACTLY (dashes-normalized)
    rather than heuristically assuming the newest form-matching entry is the
    filing currently being processed. EDGAR's submissions index can lag the
    daily index; when the current filing is not present there yet, the
    newest match IS the true prior filing, and a heuristic skip would return
    the percentage from two filings back -- a wrong number is worse than no
    number. Returns the first remaining match whose `filers` list contains a
    name matching `filer` under `select_13d`'s normalization.

    Bounded best-effort: checks at most `_PRIOR_13D_CANDIDATE_CAP`
    form-matching candidates and bails as soon as `_now() >= deadline_at`, so
    a heavily-13D'd issuer can never cost more than a bounded handful of
    serial requests. Never raises, never retried."""
    try:
        cik = _zfill_cik(issuer_cik)
        if not cik or not filer:
            return None
        resp = session.get(
            SUBMISSIONS_URL.format(cik=cik), headers={"User-Agent": _sec_ua()},
            timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        subs = resp.json()
        recent = ((subs or {}).get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        accessions = recent.get("accessionNumber") or []
        docs = recent.get("primaryDocument") or []

        current = _strip_dashes(current_accession)
        checked = 0
        for i, form in enumerate(forms):
            if form not in _SCHEDULE_13D_FORMS:
                continue
            if checked >= _PRIOR_13D_CANDIDATE_CAP or _now() >= deadline_at:
                break
            checked += 1
            accession = accessions[i] if i < len(accessions) else ""
            doc = docs[i] if i < len(docs) else ""
            if not accession or not doc:
                continue
            if current and _strip_dashes(accession) == current:
                continue  # the filing currently being processed -- skip exactly
            path = f"edgar/data/{int(cik)}/{_strip_dashes(accession)}/{doc}"
            parsed = parse_schedule13d(fetch_doc(path, session))
            filers = parsed.get("filers") or []
            if any(_matches_allowlist(name, [filer]) for name in filers):
                percent = parsed.get("percent")
                # A falsy percent means the prior document parsed but carried
                # no usable `percentOfClass` -- parse_schedule13d's degraded
                # 0.0, not a real prior stake (a 13D exists precisely because
                # someone crossed 5%). Returning it renders "0.0% -> 8.5%",
                # a fabricated "was zero". Absent beats invented.
                return percent if percent else None
        return None
    except wi_config.ConfigError:
        raise
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# build_ownership orchestration
# --------------------------------------------------------------------------- #
def _empty_ownership_bundle() -> dict:
    return {
        "insider_watchlist": [],
        "insider_buys": [],
        "activist_stakes": [],
        "scanned": {"form4_total": 0, "form4_read": 0, "truncated": False,
                    "index_date": ""},
    }


def _leg_health(attempted: int, parsed: int, truncated: bool = False) -> dict:
    """`{"ok": bool, "note": str}` for one leg's health entry.

    `attempted == 0` (nothing to fetch today) is legitimately `ok: True` --
    distinct from attempting `attempted` documents and parsing zero of them,
    which is the signature of a blanket failure (e.g. SEC 403ing every
    request because `_sec_ua()` got dropped somewhere) and must not read as a
    quiet, healthy day in the diagnostics.

    `attempted == 0` WITH `truncated` is a third case: the leg had work but
    ran out of lane budget before reading anything. That is not a quiet day
    either -- reporting it `ok: True` would have `run.py` record a `last_ok`
    for a lane that fetched nothing."""
    if attempted == 0:
        if truncated:
            return {"ok": False, "note": "deadline reached before any document was read"}
        return {"ok": True, "note": ""}
    if parsed == 0:
        return {"ok": False, "note": f"attempted {attempted}, parsed {parsed}"}
    return {"ok": True, "note": ""}


def _watchlist_cik_set(session) -> set:
    """Zero-padded CIKs for the 24 `fetch_sources.earnings_watchlist()` tickers,
    resolved once via `earnings_report._load_cik_map`. A failure there (e.g.
    SEC's company_tickers.json unreachable) degrades to an empty set -- the
    market-wide leg then simply carries every Form 4 row instead."""
    try:
        cik_map = earnings_report._load_cik_map(session)
    except wi_config.ConfigError:
        raise
    except Exception:
        return set()
    out = set()
    for ticker, _name in fetch_sources.earnings_watchlist():
        cik = (cik_map or {}).get(str(ticker).upper())
        if cik:
            out.add(str(cik))
    return out


def _tag_url(txns: list, url: str) -> list:
    """Stamp every parsed transaction row with the filing's index-page `url`
    -- `parse_form4` sees only document text, never the archive path, so this
    happens where the path is known."""
    for txn in txns:
        txn["url"] = url
    return txns


def _index_date(rows: list) -> str:
    """The EDGAR filing day the index rows cover ('YYYY-MM-DD'), or "".

    Read off the rows, not from `today`: `fetch_daily_index` walks back
    business days until one publishes, so the day actually covered is
    routinely NOT `today`. At 07:00 JST (= 18:00 ET the previous day) the
    newest published index is always for ET day D-2, and because
    `_prev_business_day` collapses the weekend, Monday's and Tuesday's briefs
    both resolve to Friday's index. Publishing the resolved day is what stops
    the repeat from reading as new movement -- the repeat itself is
    unavoidable without cross-day state, an explicit spec non-goal.

    `parse_daily_index` only emits rows whose `filed` parsed cleanly, so the
    first non-empty one is authoritative for the whole file."""
    for row in rows or []:
        filed = str((row or {}).get("filed") or "")
        if filed:
            return filed
    return ""


def _dedupe_index_rows(rows: list, watchlist_ciks: set) -> list:
    """One row per accession number, first-seen order preserved.

    EDGAR's daily index carries one row per FILER CIK, not one per filing: a
    Form 4 is listed once under the issuer and once per reporting owner, and
    every one of those rows points at the same document. Measured on the real
    2026-07-31 index: 712 Form 4 lines = 333 filings (2.14x), `SCHEDULE 13D`
    4 lines = 2, `SCHEDULE 13D/A` 36 lines = 18; group filings reach 8 copies.

    Left undeduped this costs 2.14x the network and wall clock, and -- worse
    -- emits the same transaction as several identical rows, so one filing
    consumes several of the `BUY_CAP`/`STAKE_CAP` slots and the prior-13D
    lookup budget is spent twice on one amendment.

    When several rows share an accession the survivor is the one whose CIK is
    in `watchlist_ciks` if any row has one -- otherwise leg A's pre-selection
    would silently lose watchlist filings whose issuer row is not first.

    A row whose path yields no accession keeps a per-row key, so malformed
    paths never collapse into each other."""
    keep: dict = {}
    order: list = []
    for i, row in enumerate(rows or []):
        _prefix, accession = _split_index_path((row or {}).get("path", ""))
        key = accession or f"\x00row{i}"
        if key not in keep:
            keep[key] = row
            order.append(key)
        elif (_zfill_cik(row.get("cik")) in watchlist_ciks
              and _zfill_cik(keep[key].get("cik")) not in watchlist_ciks):
            keep[key] = row
    return [keep[k] for k in order]


def _fetch_form4_watchlist(rows: list, session, deadline_at: float) -> tuple:
    """Watchlist Form 4 documents, fetched serially with the caller's session
    -- cheap (<=15 requests).

    Bounded by `deadline_at` like every other leg (house convention, see
    `earnings_report.py`'s post-earnings loop): serial and unbounded, a
    degraded-but-not-failing EDGAR answering at the 25s socket timeout would
    let this one leg run far past the lane's 90s contract. Partial output is
    valid; a deadline overrun is not an error.

    Returns `(txns, read_count, truncated)` -- the same shape as
    `_fetch_form4_scan`, so `form4_read` stays an honest count of documents
    actually read."""
    txns = []
    read = 0
    for row in rows:
        if _now() >= deadline_at:
            return txns, read, True
        path = row.get("path", "")
        txns.extend(_tag_url(parse_form4(fetch_doc(path, session)), _filing_index_url(path)))
        read += 1
    return txns, read, False


def _fetch_form4_scan(rows: list, deadline_at: float) -> tuple:
    """The market-wide Form 4 documents, fetched through a bounded thread
    pool using a rolling submission window -- NEVER a submit-everything loop.
    `executor.submit` does not block, so a `for row in rows: executor.submit(
    ...)` loop drains in microseconds: every row is already in flight before
    a single result arrives, and the deadline check that runs between
    submissions never gets a second chance to fire. Instead this seeds
    `SCAN_WORKERS * 2` futures, then waits for the next one to complete
    (`concurrent.futures.wait(..., return_when=FIRST_COMPLETED)`) and
    replaces it with the next row -- but only if `_now() < deadline_at` at
    that moment. On overrun it stops feeding new work, sets `truncated`, and
    lets the (bounded number of) already in-flight futures finish.

    Each worker uses its own `requests.Session` via `threading.local` --
    never the caller's `session`, which is not documented thread-safe. Every
    accumulator (`txns`, `read`, `truncated`) is touched on the main thread
    only. The one piece of shared mutable state the workers DO touch is
    `_SCAN_LIMITER`, which guards its own slot arithmetic with a lock.
    Returns `(txns, read_count, truncated)`."""
    txns: list = []
    read = 0
    truncated = False
    if not rows:
        return txns, read, truncated

    local = threading.local()

    def _worker_session():
        sess = getattr(local, "session", None)
        if sess is None:
            sess = _new_session()
            local.session = sess
        return sess

    def _fetch_and_parse(row):
        path = row.get("path", "")
        # Held to SEC's 10 req/s fair-access rate across ALL workers; the
        # worker count alone bounds only concurrency (measured 137 req/s).
        _SCAN_LIMITER.acquire()
        return _tag_url(parse_form4(fetch_doc(path, _worker_session())), _filing_index_url(path))

    row_iter = iter(rows)
    window = SCAN_WORKERS * 2

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
        pending: set = set()

        def _submit_next() -> None:
            nonlocal truncated
            if truncated:
                return
            # Pull FIRST, then check the clock. Checking the deadline before
            # knowing whether a row remains marks the scan truncated on a late
            # completion after the iterator is already empty -- yielding the
            # self-contradictory `read == total` with `truncated = True`.
            # `truncated` must mean a row was actually dropped.
            row = next(row_iter, None)
            if row is None:
                return
            if _now() >= deadline_at:
                truncated = True
                return
            pending.add(executor.submit(_fetch_and_parse, row))

        for _ in range(window):
            _submit_next()

        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    txns.extend(future.result())
                except wi_config.ConfigError:
                    raise
                except Exception:
                    pass  # a single worker failure must not sink the scan
                read += 1
                _submit_next()

    return txns, read, truncated


def _fetch_13d_stakes(rows: list, session, deadline_at: float) -> tuple:
    """`primary_doc.xml` for each 13D-type index row, parsed and tagged with
    the index row's `form`/`filed` (parse_schedule13d itself carries neither),
    the filing's index-page `url`, and `ticker`. `ticker` ships "" always: the
    13D XML carries `issuerCusipNumber`, not a ticker, and there is no cheap
    source for one here -- the key exists to hold the row-shape contract, but
    the renderer never displays it.

    Also stamps the internal `_accession` key (the daily index's own
    accession number for this filing) so `build_ownership` can later hand it
    to `fetch_prior_13d` for an exact, non-heuristic skip of the filing
    currently being processed. `build_ownership` pops this key before
    publishing -- it must never leak into the public bundle.

    Serial and therefore bounded by `deadline_at`, same as the watchlist leg:
    the real index carries ~40 13D-type lines a day, and at the 25s socket
    timeout an unbounded loop here is worth ~17 minutes in a lane that
    promises 90s. Returns `(stakes, truncated)`."""
    stakes = []
    for row in rows:
        if _now() >= deadline_at:
            return stakes, True
        index_path = row.get("path", "")
        _prefix, accession = _split_index_path(index_path)
        parsed = parse_schedule13d(fetch_doc(_schedule13d_doc_path(index_path), session))
        parsed["form"] = row.get("form", "")
        parsed["filed"] = row.get("filed", "")
        parsed["url"] = _filing_index_url(index_path)
        parsed["ticker"] = ""
        parsed["_accession"] = accession
        stakes.append(parsed)
    return stakes, False


def build_ownership(session, health: dict, today: str, deadline_at: float) -> dict:
    """The `ownership` bundle: watchlist + market-wide Form 4 insider activity
    and Schedule 13D/13D-A activist stakes for `today`'s daily index.
    `deadline_at` is a `_now()`-comparable monotonic instant (same convention
    as `sec_fundamentals.build_fundamentals`) the market-wide scan must
    respect -- NOT a `time.time()` wall-clock value. Never raises -- every
    leg degrades independently to a health entry plus empty/partial output."""
    try:
        return _build_ownership(session, health, today, deadline_at)
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        note = _short(exc)
        health["ownership_form4"] = {"ok": False, "note": note}
        health["ownership_13d"] = {"ok": False, "note": note}
        return _empty_ownership_bundle()


def _build_ownership(session, health, today, deadline_at) -> dict:
    rows = fetch_daily_index(today, session, health)
    if not rows:
        # All THREE health keys, always -- both the spec's failure table and
        # the CONTRACT.md addendum promise three, and an absent key cannot be
        # told apart from a leg that was never wired up.
        note = ((health.get("ownership_index") or {}).get("note")
                or "daily index unavailable")
        health["ownership_form4"] = {"ok": False, "note": note}
        health["ownership_13d"] = {"ok": False, "note": note}
        return _empty_ownership_bundle()

    watchlist_ciks = _watchlist_cik_set(session)

    # Dedupe BEFORE the watchlist/scan split so the preference rule can keep
    # the watchlist row of a duplicated accession, and so every count below
    # (`form4_total`, `form4_read`, both `_leg_health` denominators) is in
    # filings rather than index lines.
    form4_rows = _dedupe_index_rows(
        [r for r in rows if r.get("form") == "4"], watchlist_ciks)
    thirteen_d_rows = _dedupe_index_rows(
        [r for r in rows if r.get("form") in _SCHEDULE_13D_FORMS], watchlist_ciks)

    watchlist_rows = [r for r in form4_rows if _zfill_cik(r.get("cik")) in watchlist_ciks]
    scan_rows = [r for r in form4_rows if _zfill_cik(r.get("cik")) not in watchlist_ciks]

    watchlist_txns, watchlist_read, watchlist_truncated = _fetch_form4_watchlist(
        watchlist_rows, session, deadline_at)
    scan_txns, scan_read, scan_truncated = _fetch_form4_scan(scan_rows, deadline_at)

    all_txns = watchlist_txns + scan_txns
    watchlist_tickers = [t for t, _n in fetch_sources.earnings_watchlist()]
    insider_watchlist, insider_buys = select_insider(all_txns, watchlist_tickers)
    form4_attempted = watchlist_read + scan_read
    form4_truncated = watchlist_truncated or scan_truncated
    health["ownership_form4"] = _leg_health(
        form4_attempted, len(all_txns), form4_truncated)

    raw_stakes, stakes_truncated = _fetch_13d_stakes(
        thirteen_d_rows, session, deadline_at)
    activist_stakes = select_13d(raw_stakes)

    budget = _PRIOR_13D_LOOKUP_BUDGET
    for stake in activist_stakes:
        stake["prior_percent"] = None
        accession = stake.pop("_accession", "")
        if _is_amendment(stake) and budget > 0:
            stake["prior_percent"] = fetch_prior_13d(
                stake.get("issuer_cik"), stake.get("filer"), accession, session, deadline_at)
            budget -= 1
    parsed_13d = sum(1 for s in raw_stakes if s.get("company"))
    # Denominator is what was actually attempted, not what was listed: a leg
    # cut short by the deadline must not read as a parse blackout. The
    # truncation flag covers the other side of that -- attempting nothing
    # because time ran out is not a quiet day.
    health["ownership_13d"] = _leg_health(len(raw_stakes), parsed_13d, stakes_truncated)

    return {
        "insider_watchlist": insider_watchlist,
        "insider_buys": insider_buys,
        "activist_stakes": activist_stakes,
        "scanned": {
            "form4_total": len(form4_rows),
            "form4_read": form4_attempted,
            "truncated": watchlist_truncated or scan_truncated or stakes_truncated,
            "index_date": _index_date(rows),
        },
    }
