"""world-intel post-earnings layer — who reported, who matters, what they said.

Implements `build_post_earnings(config, raw_dir, health, today, kb_root) -> list`
plus the per-stage functions run.py wires against. The morning after companies
report quarterly earnings we (1) pull the trailing WINDOW_DAYS reporter list from
Finnhub (US) and a curated yfinance watchlist (intl), (2) prefilter to names that
matter via three independent gates (index-size membership, EPS surprise, 1-day
price reaction), (3) enrich the top US candidates with the primary EDGAR
filing text (8-K/6-K press release), and (4) add analyst context (market cap,
industry/sector, description, peers, beat streak, forward estimates, 30-day
revision) to that same top-N set.

ABSOLUTE RULE (same as fetch_sources.py): no external call may raise out of this
module. Every stage is wrapped; on failure we record
`health[source] = {"ok": False, "note": "<short reason>"}` and degrade to empty.

`prefilter_candidates` and the small normalizers are pure so they can be
table-tested offline. All HTTP goes through `_get_json`/`_get_text` and all
yfinance access through `_yf_batch_moves`/`_yf_reaction_frame`/`_yf_earnings_row`/
`_yf_company_info`/`_yf_beat_history`/`_yf_fwd_estimates`/`_yf_quarterly_revenue`/
`_yf_upgrades`, so tests stub those seams and a missing yfinance degrades to
ok:False instead of an ImportError.
"""

from __future__ import annotations

from worldintel import config as wi_config
import html
import json
import math
import os
import re
from datetime import date, datetime, timedelta

import requests

from worldintel.core import fetch_sources

# --------------------------------------------------------------------------- #
# Endpoints and tuning constants
# --------------------------------------------------------------------------- #
FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"
FINNHUB_PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"
FINNHUB_PEERS_URL = "https://finnhub.io/api/v1/stock/peers"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"

def _plain_ua(note: str = "post-earnings") -> str:
    return wi_config.get().plain_user_agent(note)
# SEC returns HTTP 403 without a descriptive, contactable User-Agent.
def _sec_ua() -> str:
    return wi_config.get().sec_user_agent()
_HTTP_TIMEOUT = 25

SURPRISE_PCT = 10.0        # |EPS surprise %| needed to trip the surprise gate
ABS_EPS_FLOOR = 0.10       # |eps_est| floor for the surprise gate (kills tiny-denominator noise)
REACTION_PCT = 7.0         # |1-day price move %| needed to trip the reaction gate
CANDIDATE_CAP = 25         # max candidates surfaced per morning
SURPRISE_SCORE_CAP = 100.0  # |surprise %| ceiling applied when ranking
REACTION_SCORE_CAP = 50.0   # |1-day move %| ceiling applied when ranking
BELLWETHER_SLOTS = 6       # cap slots the bellwethers can never be truncated out of
RELEASE_FETCH_MAX = 8      # only the top-N blended-score US names get an EDGAR fetch
RELEASE_EXCERPT_CHARS = 4000
DESCRIPTION_MAX_CHARS = 350  # company description cut on a word boundary
PEERS_MAX = 5              # competitor tickers kept per candidate (self excluded)
BEAT_STREAK_MAX = 4        # trailing reported-quarter surprises kept
WINDOW_DAYS = 5            # lookback window for "who reported" (a Friday-AMC
                           # reporter publishes Tuesday; must still be in-window)
MAX_ATTEMPTS = 4           # pending retries before a candidate expires
COVERED_RETENTION_DAYS = 14  # how long a covered/expired ledger entry is remembered
PIPELINE_FILE = "earnings_pipeline.json"
REV_AGREEMENT_PCT = 5.0    # % tolerance: finnhub agrees with ANY other candidate
REV_TIE_PCT = 1.0          # % tolerance: near-tied log-distances resolved by source rank
REV_BAND = (0.4, 2.5)      # winner/est plausibility band; nothing inside -> dropped
REV_FRESHNESS_DAYS = 60    # yfinance quarter must end within this many days of report_date

# SIZE gate membership: S&P-100 union Nasdaq-100 union major US-listed ADRs.
# Hand-refreshed roughly quarterly (index rebalances are slow); discovery of names
# outside this set stays dynamic via the surprise and reaction gates.
LARGECAP_INDEX_SET = frozenset({
    # --- S&P-100 ---
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "AIG", "AMD", "AMGN", "AMT", "AMZN",
    "AVGO", "AXP", "BA", "BAC", "BK", "BKNG", "BLK", "BMY", "BRK.B", "C",
    "CAT", "CHTR", "CL", "CMCSA", "COF", "COP", "COST", "CRM", "CSCO", "CVS",
    "CVX", "DE", "DHR", "DIS", "DOW", "DUK", "EMR", "ETN", "F", "FDX",
    "GD", "GE", "GILD", "GM", "GOOG", "GOOGL", "GS", "HD", "HON", "IBM",
    "INTC", "INTU", "ISRG", "JNJ", "JPM", "KHC", "KO", "LIN", "LLY", "LMT",
    "LOW", "MA", "MCD", "MDLZ", "MDT", "MET", "META", "MMM", "MO", "MRK",
    "MS", "MSFT", "NEE", "NFLX", "NKE", "NVDA", "ORCL", "PEP", "PFE", "PG",
    "PLTR", "PM", "PYPL", "QCOM", "RTX", "SBUX", "SCHW", "SO", "SPG", "T",
    "TGT", "TMO", "TMUS", "TSLA", "TXN", "UNH", "UNP", "UPS", "USB", "V",
    "VZ", "WFC", "WMT", "XOM",
    # --- Nasdaq-100 additions ---
    "ABNB", "ADI", "ADP", "ADSK", "AEP", "AMAT", "ANSS", "APP", "ARM", "AXON",
    "BIIB", "CDNS", "CDW", "CEG", "CPRT", "CRWD", "CSGP", "CSX", "CTAS", "CTSH",
    "DASH", "DDOG", "DXCM", "EA", "EXC", "FANG", "FAST", "FTNT", "GEHC", "GFS",
    "IDXX", "KDP", "KLAC", "LRCX", "LULU", "MAR", "MCHP", "MELI", "MNST", "MRVL",
    "MU", "NXPI", "ODFL", "ON", "ORLY", "PANW", "PAYX", "PCAR", "REGN", "ROP",
    "ROST", "SNPS", "TEAM", "TTD", "TTWO", "VRSK", "VRTX", "WBD", "WDAY", "XEL",
    "ZS",
    # --- Major US-listed ADRs / foreign large caps ---
    "TSM", "ASML", "SAP", "SONY", "TM", "AZN", "NVO", "BABA", "PDD", "JD",
    "BIDU", "NIO", "SHEL", "UL", "RIO", "BHP", "HSBC", "TTE", "BP", "SNY",
    "UBS", "MUFG", "SMFG", "HDB", "IBN", "INFY", "NVS", "GSK", "DEO", "BUD",
    "SE", "STLA", "HMC",
})

# The names the brief's forward calendar promises ("Microsoft, Meta, Apple ...
# report this week"), reused verbatim so the post-earnings lane delivers exactly
# the names the calendar advertised. These are reserved against CANDIDATE_CAP:
# their earnings are worth a line whatever the surprise % happens to be.
def bellwether_tickers():
    return frozenset(t for t, _ in fetch_sources.earnings_watchlist())

# Intl lane: curated large caps polled directly on their home exchanges via
# yfinance (Yahoo suffixes). Estimates can be missing (notably A-shares) — those
# names can still pass through the reaction gate.
INTL_WATCHLIST = [
    # Japan
    ("7203.T", "Toyota"),
    ("6758.T", "Sony"),
    ("8306.T", "MUFG"),
    ("6861.T", "Keyence"),
    ("9983.T", "Fast Retailing"),
    ("7974.T", "Nintendo"),
    ("8035.T", "Tokyo Electron"),
    # Korea
    ("005930.KS", "Samsung Electronics"),
    ("000660.KS", "SK Hynix"),
    # Europe
    ("ASML.AS", "ASML"),
    ("SAP.DE", "SAP"),
    ("MC.PA", "LVMH"),
    ("NESN.SW", "Nestle"),
    ("NOVO-B.CO", "Novo Nordisk"),
    ("SIE.DE", "Siemens"),
    # China / Hong Kong
    ("0700.HK", "Tencent"),
    ("3690.HK", "Meituan"),
    ("1211.HK", "BYD"),
    ("9988.HK", "Alibaba-HK"),
    # China A-shares
    ("300750.SZ", "CATL"),
    ("600519.SS", "Kweichow Moutai"),
]

# The exact per-candidate key contract run.py and the renderer are wired against.
_FINAL_KEYS = (
    "ticker", "name", "report_date", "hour", "eps_est", "eps_act", "surprise_pct",
    "rev_est", "rev_act", "rev_act_source", "price_move_pct", "release_excerpt",
    "source", "gates",
    # Analyst context (filled for the top RELEASE_FETCH_MAX only; defaults below)
    "market_cap_m", "industry", "sector", "description", "peers", "beat_streak",
    "fwd_eps_q", "fwd_eps_fy", "analyst_count", "revision_30d_pct",
    # Content layers (Spec B D1): capital returns, next-morning analyst
    # reactions, forward revenue growth consensus.
    "buyback", "dividend", "analyst_moves", "rev_growth_fy",
    # SEC fundamentals lane (Phase 1): bellwether decoration only. Never
    # required by completeness() — see Global Constraint 8.
    "fundamentals", "fundamentals_flags",
)

# Defaults every candidate carries; the enrichment pass overwrites them for the
# top-N set. List defaults are copied per candidate (never shared mutables).
_CONTEXT_DEFAULTS = {
    "market_cap_m": None, "industry": "", "sector": "", "description": "",
    "peers": [], "beat_streak": [], "fwd_eps_q": None, "fwd_eps_fy": None,
    "analyst_count": None, "revision_30d_pct": None,
    "buyback": None, "dividend": None, "analyst_moves": None, "rev_growth_fy": None,
    # `{}` would alias one dict across every candidate (this loop only copies
    # `list` defaults) -- None and [] are the only safe choices here.
    "fundamentals": None, "fundamentals_flags": [],
}

# ticker -> zero-padded 10-digit CIK, filled once per process from SEC_TICKERS_URL.
_CIK_CACHE: dict = {}


# --------------------------------------------------------------------------- #
# Small internal utilities (local copies — house rule: do not import from
# fetch_sources, the modules must stay independently loadable)
# --------------------------------------------------------------------------- #
def _short(exc: Exception) -> str:
    """A short, single-line reason string for health notes."""
    msg = str(exc).split("\n")[0].strip()
    return f"{type(exc).__name__}: {msg}"[:120] if msg else type(exc).__name__


def _dump(raw_dir: str, source: str, payload) -> None:
    """Write a source's raw JSON to raw_dir/<source>.json (best-effort)."""
    try:
        os.makedirs(raw_dir, exist_ok=True)
        path = os.path.join(raw_dir, f"{source}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except wi_config.ConfigError:
        raise
    except Exception:
        # Persisting raw data is best-effort; never let it break a run.
        pass


def _to_float(value):
    try:
        if value in (None, "", "."):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_day(value: str):
    """'YYYY-MM-DD' -> date, or None if unparseable."""
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except wi_config.ConfigError:
        raise
    except Exception:
        return None


def _get_json(url: str, session, params=None, headers=None):
    """One HTTP GET -> parsed JSON. Isolated so tests can stub every request."""
    sess = session or requests
    resp = sess.get(url, params=params, headers=headers, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _get_text(url: str, session, headers=None) -> str:
    """One HTTP GET -> body text. Isolated so tests can stub every request."""
    sess = session or requests
    resp = sess.get(url, headers=headers, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _strip_html(text: str) -> str:
    """Crude but dependency-free HTML -> text: drop script/style bodies, drop
    tags, unescape entities, collapse whitespace. Good enough for an LLM excerpt;
    we deliberately avoid a parser dependency."""
    out = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text or "")
    out = re.sub(r"(?s)<[^>]+>", " ", out)
    out = html.unescape(out)
    return re.sub(r"\s+", " ", out).strip()


def _surprise(eps_est, eps_act):
    """EPS surprise % with an abs() denominator (a negative estimate must not flip
    the sign of the surprise). None when either side is missing or est is 0."""
    if eps_est is None or eps_act is None or abs(eps_est) == 0:
        return None
    return round((eps_act - eps_est) / abs(eps_est) * 100.0, 1)


def _blend_score(cand: dict) -> float:
    """Ranking score used for both the candidate cap and the EDGAR fetch budget:
    index membership is worth a flat 2.0; surprise and reaction scale linearly
    up to their score caps.

    Surprise only counts off an estimate at or above ABS_EPS_FLOOR — the same
    rule the surprise gate applies. Without it a $0.005 estimate turns a penny
    beat into a 5000% "surprise" worth 500 points, which is how Microsoft (2.9)
    and Meta (3.6) ended up ranked 96th and 87th on 2026-07-30. Both terms are
    clamped so no single input can dominate the ranking outright."""
    score = 2.0 if cand["ticker"] in LARGECAP_INDEX_SET else 0.0
    surprise, est = cand.get("surprise_pct"), cand.get("eps_est")
    if (surprise is not None and est is not None and abs(est) >= ABS_EPS_FLOOR):
        score += min(abs(surprise), SURPRISE_SCORE_CAP) / 10.0
    move = cand.get("price_move_pct")
    if move is not None:
        score += min(abs(move), REACTION_SCORE_CAP) / 5.0
    return score


def _apply_cap(candidates: list) -> list:
    """Trim a blended-score-sorted candidate list to CANDIDATE_CAP, reserving up
    to BELLWETHER_SLOTS places for bellwether reporters.

    The cap runs before the pipeline ledger, so anything it drops leaves no
    pending/expired trace and never retries — it is simply gone, and after
    WINDOW_DAYS the name is out of the calendar window for good. Reserved
    bellwethers are also moved to the front: the EDGAR release fetch and the
    analyst-context lookups take the first RELEASE_FETCH_MAX US names, so a
    bellwether that merely scraped into the cap would publish without a
    write-up. The reserve is a floor, not a takeover — beyond
    BELLWETHER_SLOTS, bellwethers compete on score like anything else."""
    reserved, rest = [], []
    for cand in candidates:
        if cand["ticker"] in bellwether_tickers() and len(reserved) < BELLWETHER_SLOTS:
            reserved.append(cand)
        else:
            rest.append(cand)
    return reserved + rest[:max(CANDIDATE_CAP - len(reserved), 0)]


# --------------------------------------------------------------------------- #
# US lane: Finnhub earnings calendar (trailing window, reported rows only)
# --------------------------------------------------------------------------- #
def fetch_finnhub_calendar(config: dict, raw_dir: str, health: dict, today: str) -> list:
    """Who reported in [today - WINDOW_DAYS, today] per Finnhub. Rows without an
    epsActual have not actually reported yet (the calendar lists upcoming dates
    too) and are dropped. Never raises."""
    key = config.get("finnhub_key")
    if not key:
        health["finnhub"] = {"ok": False, "note": "no api key"}
        return []
    ref = _parse_day(today) or date.today()
    params = {
        "from": str(ref - timedelta(days=WINDOW_DAYS)),
        "to": str(ref),
        "token": key,
    }
    try:
        data = _get_json(FINNHUB_URL, None, params=params)
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["finnhub"] = {"ok": False, "note": _short(exc)}
        return []
    _dump(raw_dir, "finnhub_earnings", data)

    rows = []
    for row in (data or {}).get("earningsCalendar") or []:
        eps_act = _to_float(row.get("epsActual"))
        if eps_act is None:
            continue  # scheduled but not yet reported
        eps_est = _to_float(row.get("epsEstimate"))
        rows.append({
            "ticker": str(row.get("symbol") or "").upper(),
            "name": "",  # filled from the profile endpoint for final candidates only
            "report_date": row.get("date") or "",
            "hour": row.get("hour") or "",
            "eps_est": eps_est,
            "eps_act": eps_act,
            "rev_est": _to_float(row.get("revenueEstimate")),
            "rev_act": _to_float(row.get("revenueActual")),
            "surprise_pct": _surprise(eps_est, eps_act),
            "source": "us",
        })
    health["finnhub"] = {"ok": bool(rows), "note": "" if rows else "no reporters"}
    return rows


def correct_forward_calendar(rows: list, raw_dir: str, today: str) -> list:
    """Drop forward-calendar entries for names that have already reported.

    The brief's "who reports next" list comes from yfinance's next-earnings
    date, which lags: on 2026-07-30 it still carried Microsoft and Meta at
    2026-07-30 when both had reported AMC on 2026-07-29, so the brief announced
    earnings the morning after they landed. Finnhub had the right date, so this
    re-reads the calendar dump `fetch_finnhub_calendar` already wrote for this
    run (no extra API call) and removes any watchlist name it shows as reported:
    a report date behind us, or actuals already published for today's date. A
    Finnhub row dated today with no actuals yet is a genuine same-day reporter
    and stays. Intl tickers never appear in the US calendar and pass through.

    Best-effort and non-destructive: a missing or unreadable dump returns `rows`
    untouched — an unavailable referee must never empty the calendar."""
    rows = rows or []
    try:
        with open(os.path.join(raw_dir, "finnhub_earnings.json"), "r",
                  encoding="utf-8") as fh:
            data = json.load(fh)
    except wi_config.ConfigError:
        raise
    except Exception:
        return rows

    ref = _parse_day(today)
    reported = set()
    for row in (data or {}).get("earningsCalendar") or []:
        when = _parse_day(row.get("date") or "")
        if when is None or ref is None:
            continue
        if when < ref or _to_float(row.get("epsActual")) is not None:
            reported.add(str(row.get("symbol") or "").upper())
    return [r for r in rows if str(r.get("ticker") or "").upper() not in reported]


# --------------------------------------------------------------------------- #
# Price reaction: batched 1-day close-over-close moves via yfinance
# --------------------------------------------------------------------------- #
def _yf_batch_moves(tickers: list) -> dict:
    """Batched yfinance download -> {ticker: 1-day % move | None}. Isolated in its
    own function so tests can stub it offline and an import failure is easy to
    detect. A wider 1-month window buffers holidays/missing bars; only the two
    most-recent available closes are compared."""
    import yfinance as yf
    data = yf.download(
        tickers=" ".join(tickers), period="1mo", interval="1d",
        group_by="ticker", progress=False, auto_adjust=False, threads=True,
    )
    out = {}
    for t in tickers:
        try:
            frame = data[t] if len(tickers) > 1 else data
            closes = frame["Close"].dropna()
            if len(closes) >= 2 and float(closes.iloc[-2]):
                out[t] = round(
                    (float(closes.iloc[-1]) / float(closes.iloc[-2]) - 1.0) * 100.0, 2)
            else:
                out[t] = None
        except wi_config.ConfigError:
            raise
        except Exception:
            out[t] = None
    return out


def fetch_price_moves(tickers: list, health: dict) -> dict:
    """1-day % price moves for `tickers` -> {ticker: float|None}. Never raises;
    a missing yfinance or any batch failure degrades to {} with a health note."""
    unique = [t for t in dict.fromkeys(tickers or []) if t]
    if not unique:
        health["price_moves"] = {"ok": True, "note": "no tickers"}
        return {}
    try:
        moves = _yf_batch_moves(unique)
    except ModuleNotFoundError:
        health["price_moves"] = {"ok": False, "note": "yfinance not installed"}
        return {}
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["price_moves"] = {"ok": False, "note": _short(exc)}
        return {}
    health["price_moves"] = {"ok": bool(moves), "note": "" if moves else "no moves"}
    return moves


# --------------------------------------------------------------------------- #
# Timing-aware price reaction (US lane): the brief runs ~17:00 ET the prior
# day, so an AMC reporter's close-over-close "1-day move" predates its own
# press release. These compute the reaction relative to the report's BMO/AMC
# timing instead of just the two most recent closes.
# --------------------------------------------------------------------------- #
def _yf_reaction_frame(tickers: list) -> dict:
    """Batched yfinance download -> {ticker: [(date, close), ...]} date-sorted,
    NaN closes skipped. Isolated so tests can stub it offline; mirrors
    _yf_batch_moves's single-vs-multi ticker frame handling."""
    import yfinance as yf
    data = yf.download(
        tickers=" ".join(tickers), period="1mo", interval="1d",
        group_by="ticker", progress=False, auto_adjust=False, threads=True,
    )
    out = {}
    for t in tickers:
        try:
            frame = data[t] if len(tickers) > 1 else data
            closes = frame["Close"].dropna()
            bars = []
            for idx, close in closes.items():
                try:
                    when = idx.date()
                except wi_config.ConfigError:
                    raise
                except Exception:
                    when = _parse_day(str(idx))
                if when is None:
                    continue
                bars.append((when, float(close)))
            bars.sort(key=lambda b: b[0])
            out[t] = bars
        except wi_config.ConfigError:
            raise
        except Exception:
            out[t] = []
    return out


def _reaction_move(bars: list, report_date, hour) -> float:
    """Report-timing-aware % price reaction from date-sorted (date, close)
    bars. bmo (or unknown/empty hour): close ON report_date — or the first
    bar >= report_date if the report day itself has no bar — over the
    PREVIOUS bar's close. amc: close of the first bar strictly AFTER
    report_date over the close ON report_date (or, absent an exact match,
    the last bar <= report_date). Any needed bar missing -> None."""
    day = _parse_day(report_date)
    if day is None or not bars:
        return None
    ordered = sorted(bars, key=lambda b: b[0])

    if hour == "amc":
        base = None
        after = None
        for when, close in ordered:
            if when <= day:
                base = close
            elif after is None:
                after = close
        if base is None or after is None or not base:
            return None
        return round((after / base - 1.0) * 100.0, 2)

    # bmo (or unknown/empty hour) rule.
    idx = None
    for i, (when, _close) in enumerate(ordered):
        if when >= day:
            idx = i
            break
    if idx is None or idx == 0:
        return None
    prev_close = ordered[idx - 1][1]
    cur_close = ordered[idx][1]
    if not prev_close:
        return None
    return round((cur_close / prev_close - 1.0) * 100.0, 2)


def fetch_price_reactions(reporters: list, health: dict) -> dict:
    """Timing-aware % price reactions for US reporters -> {ticker: float|None}.
    Never raises; a missing yfinance or any batch failure degrades to {} with
    a health note (same "price_moves" key as fetch_price_moves)."""
    tickers = [r.get("ticker") for r in reporters or [] if r.get("ticker")]
    unique = list(dict.fromkeys(tickers))
    if not unique:
        health["price_moves"] = {"ok": True, "note": "no tickers"}
        return {}
    try:
        frames = _yf_reaction_frame(unique)
    except ModuleNotFoundError:
        health["price_moves"] = {"ok": False, "note": "yfinance not installed"}
        return {}
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["price_moves"] = {"ok": False, "note": _short(exc)}
        return {}
    out = {}
    for r in reporters or []:
        ticker = r.get("ticker")
        if not ticker or ticker in out:
            continue
        out[ticker] = _reaction_move(
            frames.get(ticker) or [], r.get("report_date"), r.get("hour"))
    health["price_moves"] = {"ok": bool(out), "note": "" if out else "no moves"}
    return out


# --------------------------------------------------------------------------- #
# Intl lane: curated watchlist polled on home exchanges via yfinance
# --------------------------------------------------------------------------- #
def _yf_earnings_row(ticker: str):
    """Most recent REPORTED earnings row for `ticker` via yfinance ->
    {"date": "YYYY-MM-DD", "eps_est": float|None, "eps_act": float|None} or None.
    Isolated so tests can stub it offline; ModuleNotFoundError is left to the
    caller so a missing yfinance is distinguishable from a per-ticker failure."""
    import yfinance as yf
    df = yf.Ticker(ticker).get_earnings_dates(limit=12)
    if df is None or len(df) == 0:
        return None
    ref = date.today()
    best = None  # (date, eps_est, eps_act) of the most recent past row
    for idx, row in df.iterrows():
        try:
            when = idx.date()
        except wi_config.ConfigError:
            raise
        except Exception:
            when = _parse_day(str(idx))
        if when is None or when > ref:
            continue
        est = _to_float(row.get("EPS Estimate"))
        act = _to_float(row.get("Reported EPS"))
        # pandas NaN survives float(): normalize NaN -> None.
        est = est if est == est else None
        act = act if act == act else None
        if best is None or when > best[0]:
            best = (when, est, act)
    if best is None:
        return None
    return {"date": str(best[0]), "eps_est": best[1], "eps_act": best[2]}


def fetch_intl_watchlist(raw_dir: str, health: dict, today: str) -> list:
    """Tier-1 international reporters: keep watchlist names whose most recent
    reported date falls inside [today - WINDOW_DAYS, today]. Estimates may be
    missing (A-shares) -> surprise_pct None; the reaction gate still applies.
    Degrades silently per-ticker; a missing yfinance fails the whole lane."""
    ref = _parse_day(today) or date.today()
    start = ref - timedelta(days=WINDOW_DAYS)
    kept = []
    raw = {}
    for ticker, name in INTL_WATCHLIST:
        try:
            row = _yf_earnings_row(ticker)
        except ModuleNotFoundError:
            health["intl_earnings"] = {"ok": False, "note": "yfinance not installed"}
            return []
        except wi_config.ConfigError:
            raise
        except Exception:
            continue  # one bad ticker must not sink the lane
        raw[ticker] = row
        when = _parse_day((row or {}).get("date"))
        if when is None or not (start <= when <= ref):
            continue
        eps_est = row.get("eps_est")
        eps_act = row.get("eps_act")
        kept.append({
            "ticker": ticker,
            "name": name,
            "report_date": row["date"],
            "hour": "",
            "eps_est": eps_est,
            "eps_act": eps_act,
            "rev_est": None,
            "rev_act": None,
            "surprise_pct": _surprise(eps_est, eps_act),
            "source": "intl",
        })
    # Intl rows carry their own moves (build_post_earnings only batches US names).
    moves = fetch_price_moves([r["ticker"] for r in kept], health) if kept else {}
    for r in kept:
        r["price_move_pct"] = moves.get(r["ticker"])
    _dump(raw_dir, "intl_earnings", raw)
    health["intl_earnings"] = {
        "ok": bool(kept),
        "note": "" if kept else "no recent reporters",
    }
    return kept


# --------------------------------------------------------------------------- #
# Prefilter (pure — table-tested)
# --------------------------------------------------------------------------- #
def prefilter_candidates(reporters: list, price_moves: dict, covered: set,
                         health: dict | None = None) -> list:
    """Reduce the raw reporter list to the names that matter. A reporter becomes a
    candidate if ANY gate trips (recorded in "gates"):

      size     — ticker in LARGECAP_INDEX_SET (suffixed intl tickers are simply
                 not in the set, so this gate is US/ADR-only by construction)
      surprise — |surprise_pct| >= SURPRISE_PCT with |eps_est| >= ABS_EPS_FLOOR
                 (the floor kills huge-looking beats off near-zero estimates)
      reaction — |1-day price move| >= REACTION_PCT

    Already-covered TICKER:date pairs are skipped so a name never appears in two
    consecutive briefs. Output is blended-score sorted then trimmed by
    `_apply_cap`, which floats the reserved bellwethers to the front. `health`,
    when given, records how many gated candidates the cap discarded — that drop
    is otherwise invisible (it happens before the ledger).
    """
    candidates = []
    for reporter in reporters or []:
        cand = dict(reporter)
        ticker = cand.get("ticker") or ""
        # Merge the price move first: an intl row may already carry its own.
        if ticker in (price_moves or {}):
            cand["price_move_pct"] = price_moves[ticker]
        else:
            cand["price_move_pct"] = cand.get("price_move_pct")
        if f"{ticker}:{cand.get('report_date')}" in (covered or set()):
            continue

        gates = _current_gates(cand)
        if not gates:
            continue
        cand["gates"] = gates
        candidates.append(cand)

    candidates.sort(key=_blend_score, reverse=True)
    kept = _apply_cap(candidates)
    if health is not None:
        health["pe_prefilter"] = {
            "ok": True,
            "note": f"{len(kept)} kept, {len(candidates) - len(kept)} truncated",
        }
    return kept


# --------------------------------------------------------------------------- #
# EDGAR: primary filing text for top candidates
# --------------------------------------------------------------------------- #
def _load_cik_map(session) -> dict:
    """ticker -> zero-padded 10-digit CIK from SEC's company_tickers.json, cached
    per-process (the file is ~1MB and identical for every candidate)."""
    if _CIK_CACHE:
        return _CIK_CACHE
    data = _get_json(SEC_TICKERS_URL, session, headers={"User-Agent": _sec_ua()})
    for row in (data or {}).values():
        ticker = str(row.get("ticker") or "").upper()
        cik = row.get("cik_str")
        if ticker and cik is not None:
            _CIK_CACHE[ticker] = str(int(cik)).zfill(10)
    return _CIK_CACHE


def fetch_earnings_release(ticker: str, session, health: dict) -> str:
    """Primary-document text of the candidate's most recent 8-K/6-K filed within
    the last WINDOW_DAYS+2 days (earnings press releases ride on those forms),
    stripped of HTML and truncated to RELEASE_EXCERPT_CHARS. Best-effort: any
    miss (unknown ticker, no recent filing, HTTP failure) returns ""."""
    try:
        cik = _load_cik_map(session).get((ticker or "").upper())
        if not cik:
            return ""
        subs = _get_json(
            SEC_SUBMISSIONS_URL.format(cik=cik), session,
            headers={"User-Agent": _sec_ua()},
        )
        recent = ((subs or {}).get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        accessions = recent.get("accessionNumber") or []
        docs = recent.get("primaryDocument") or []
        filed_dates = recent.get("filingDate") or []
        cutoff = date.today() - timedelta(days=WINDOW_DAYS + 2)

        best = None  # (filed, accession, primaryDocument), most recent wins
        for i, form in enumerate(forms):
            if form not in ("8-K", "6-K"):
                continue
            filed = _parse_day(filed_dates[i] if i < len(filed_dates) else "")
            if filed is None or filed < cutoff:
                continue
            if best is None or filed > best[0]:
                best = (
                    filed,
                    accessions[i] if i < len(accessions) else "",
                    docs[i] if i < len(docs) else "",
                )
        if not best or not best[1] or not best[2]:
            return ""
        url = SEC_ARCHIVES_URL.format(
            cik=int(cik), accession=best[1].replace("-", ""), doc=best[2])
        text = _get_text(url, session, headers={"User-Agent": _sec_ua()})
        return _strip_html(text)[:RELEASE_EXCERPT_CHARS]
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["edgar_release"] = {"ok": False, "note": _short(exc)}
        return ""


# --------------------------------------------------------------------------- #
# Revenue arbitration: Finnhub vs yfinance's quarterly income statement vs
# parsed release text, refereed by the consensus estimate. Two live incidents
# motivate this: a wrong Finnhub revenueActual (GOOGL, SEC release said
# $119.796B, Finnhub said $103.617B) and a regex that grabbed the wrong
# dollar figure out of a release (WRLD: "...increased $6.4 million ... to
# $139.2 million" — the delta, not the total).
# --------------------------------------------------------------------------- #
_REV_SOURCE_RANK = {"yfinance": 0, "finnhub": 1, "release": 2}

_REV_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"revenues?\s+(?:of|was|were)\s+\$([\d,]+(?:\.\d+)?)\s*(billion|million)\b",
    # [^.] alone would also exclude decimal points (e.g. the "4.8" in "or
    # 4.8%,"), truncating the match before it ever reaches "to $X"; the
    # (?<=\d)\.(?=\d) branch allows a period only when it's a decimal point (digit on BOTH sides),
    # so a real sentence-ending period still stops the match.
    r"(?:increased|decreased|grew|declined)(?:[^.]|(?<=\d)\.(?=\d)){0,80}?\bto\s+"
    r"\$([\d,]+(?:\.\d+)?)\s*(billion|million)\b",
    r"net\s+sales\s+(?:of|was|were)\s+\$([\d,]+(?:\.\d+)?)\s*(billion|million)\b",
)]
_REV_SCALE = {"billion": 1e9, "million": 1e6}


def _yf_quarterly_revenue(ticker: str):
    """Latest reported quarter's Total Revenue via yfinance ->
    {"period_end": date, "revenue": float} or None (missing frame/row, NaN, or
    an unparseable column). Import inside so a missing yfinance is an
    ImportError the caller can catch; exceptions otherwise left to the caller
    (mirrors _yf_company_info / _yf_fwd_estimates)."""
    import yfinance as yf
    frame = yf.Ticker(ticker).quarterly_income_stmt
    if frame is None or frame.empty or "Total Revenue" not in frame.index:
        return None
    row = frame.loc["Total Revenue"]
    if row is None or len(row) == 0:
        return None
    col = row.index.max()
    revenue = _to_float(row[col])
    if revenue is None or revenue != revenue:  # pandas NaN survives float()
        return None
    period_end = col.date() if hasattr(col, "date") else _parse_day(str(col))
    if period_end is None:
        return None
    return {"period_end": period_end, "revenue": revenue}


def parse_release_revenue(text) -> list:
    """ALL total-shaped revenue mentions in `text`, scaled to dollars, deduped
    (order preserved). Deliberately NO bare "revenues ... $X" pattern — a live
    hotpatch once grabbed "revenues increased $6.4 million" (the delta)
    instead of "...to $139.2 million" (the total); every pattern here requires
    an of/was/were/net-sales/to anchor so a bare mention can never match.
    Pure — [] on no match, empty, or None text."""
    if not text:
        return []
    values = []
    for pattern in _REV_PATTERNS:
        for match in pattern.finditer(text):
            try:
                amount = (float(match.group(1).replace(",", ""))
                          * _REV_SCALE[match.group(2).lower()])
            except (TypeError, ValueError, KeyError):
                continue
            values.append(amount)
    seen = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def _rev_near_tie(dist: float, best: float) -> bool:
    """True when `dist` is within REV_TIE_PCT% of `best` (the minimum)."""
    if best == 0:
        return dist == 0
    return abs(dist - best) <= best * (REV_TIE_PCT / 100.0)


def resolve_revenue_actual(cand: dict, release_text, yf_row):
    """Cross-validate one candidate's revenue actual across Finnhub, a fresh
    yfinance quarterly row, and parsed release text, refereed by the Street
    estimate. Pure — table-tested; touches nothing external. Order: freshness
    guard on yf_row -> no-referee passthrough -> 5% finnhub-agreement
    short-circuit -> closest-to-estimate pick (log-ratio distance, near-ties
    broken structured-over-parsed) inside a 0.4-2.5x plausibility band.
    Returns (rev_act, source, note); source in
    "finnhub" | "yfinance" | "release" | "dropped"."""
    finnhub = cand.get("rev_act")
    est = cand.get("rev_est")

    others = []  # (value, source), excludes finnhub
    report_date = _parse_day(cand.get("report_date"))
    if yf_row and report_date is not None:
        period_end = yf_row.get("period_end")
        if period_end is not None:
            try:
                delta = (report_date - period_end).days
            except TypeError:
                delta = None
            if delta is not None and 0 <= delta <= REV_FRESHNESS_DAYS:
                yf_value = _to_float(yf_row.get("revenue"))
                if yf_value is not None:
                    others.append((yf_value, "yfinance"))
    for value in parse_release_revenue(release_text):
        others.append((value, "release"))

    if est is None:
        return (finnhub, "finnhub", "no referee")  # never override without a referee

    candidates = ([(finnhub, "finnhub")] if finnhub is not None else []) + others
    if not candidates:
        return (None, "finnhub", "no revenue data")

    if finnhub is not None:
        for value, _source in others:
            if value and abs(finnhub / value - 1.0) <= REV_AGREEMENT_PCT / 100.0:
                return (finnhub, "finnhub", "")  # common case, zero churn

    low, high = REV_BAND
    scored = []
    for value, source in candidates:
        if not value or value <= 0 or not est:
            continue
        ratio = value / est
        if not (low <= ratio <= high):
            continue
        scored.append((abs(math.log(ratio)), value, source))

    if not scored:
        return (None, "dropped", "no plausible revenue")

    best_dist = min(s[0] for s in scored)
    tied = [s for s in scored if _rev_near_tie(s[0], best_dist)]
    tied.sort(key=lambda s: _REV_SOURCE_RANK.get(s[2], 3))
    _, winner_value, winner_source = tied[0]
    return (winner_value, winner_source, "")


def _finnhub_profile(ticker: str, config: dict, session) -> dict:
    """Company profile via Finnhub profile2 ->
    {"name": str, "market_cap_m": float|None, "industry": str}. ONE call serves
    both the display name and the analyst-context enrichment; called for final
    candidates only to respect the free-tier call budget. Best-effort: any miss
    returns the empty defaults."""
    out = {"name": "", "market_cap_m": None, "industry": ""}
    key = (config or {}).get("finnhub_key")
    if not key:
        return out
    try:
        data = _get_json(
            FINNHUB_PROFILE_URL, session,
            params={"symbol": ticker, "token": key}) or {}
        out["name"] = str(data.get("name") or "")
        out["market_cap_m"] = _to_float(data.get("marketCapitalization"))
        out["industry"] = str(data.get("finnhubIndustry") or "")
    except wi_config.ConfigError:
        raise
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
# Analyst-context enrichment (top RELEASE_FETCH_MAX candidates only)
# --------------------------------------------------------------------------- #
def _truncate_words(text: str, limit: int) -> str:
    """Truncate to at most `limit` chars without cutting a word in half."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if text[limit] != " " and " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip()


def _yf_company_info(ticker: str) -> dict:
    """Sector + short business description via yfinance get_info ->
    {"sector": str, "description": str} ("" when missing; description truncated
    to ~DESCRIPTION_MAX_CHARS on a word boundary). Isolated so tests can stub it
    offline; exceptions are left to the caller (_enrich_context)."""
    import yfinance as yf
    info = yf.Ticker(ticker).get_info() or {}
    return {
        "sector": str(info.get("sector") or ""),
        "description": _truncate_words(
            str(info.get("longBusinessSummary") or ""), DESCRIPTION_MAX_CHARS),
    }


def _finnhub_peers(ticker: str, config: dict, session) -> list:
    """Competitor tickers via Finnhub /stock/peers, the ticker itself excluded,
    deduped (Finnhub returns repeats; seen live), capped at PEERS_MAX.
    Best-effort: [] on any miss."""
    key = (config or {}).get("finnhub_key")
    if not key:
        return []
    try:
        data = _get_json(
            FINNHUB_PEERS_URL, session, params={"symbol": ticker, "token": key})
        self_upper = (ticker or "").upper()
        peers, seen = [], set()
        for p in data or []:
            sym = str(p).upper() if p else ""
            if sym and sym != self_upper and sym not in seen:
                seen.add(sym)
                peers.append(sym)
        return peers[:PEERS_MAX]
    except wi_config.ConfigError:
        raise
    except Exception:
        return []


def _yf_beat_history(ticker: str) -> list:
    """EPS Surprise(%) of the last <=BEAT_STREAK_MAX REPORTED quarters via
    yfinance, most recent first, rounded to 1dp. Rows without a Reported EPS
    (scheduled, not yet reported) or without a surprise value are skipped.
    Isolated so tests can stub it offline; exceptions are left to the caller."""
    import yfinance as yf
    df = yf.Ticker(ticker).get_earnings_dates(limit=8)
    if df is None or len(df) == 0:
        return []
    rows = []
    for idx, row in df.iterrows():
        act = _to_float(row.get("Reported EPS"))
        sp = _to_float(row.get("Surprise(%)"))
        act = act if act == act else None  # pandas NaN -> None
        sp = sp if sp == sp else None
        if act is None or sp is None:
            continue
        try:
            when = idx.date()
        except wi_config.ConfigError:
            raise
        except Exception:
            when = _parse_day(str(idx))
        rows.append((when or date.min, round(sp, 1)))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [sp for _, sp in rows[:BEAT_STREAK_MAX]]


# --------------------------------------------------------------------------- #
# Content layers (Spec B D1): capital-return flags parsed from the release
# excerpt, and next-morning analyst reactions for deferred (day-2+) names.
# --------------------------------------------------------------------------- #
_BUYBACK_RE = [re.compile(p, re.IGNORECASE) for p in (
    r"(?:authorized|approved)[^.]{0,120}?(?:repurchase|buyback)[^.]{0,80}?"
    r"\$\s*([\d.,]+)\s*(billion|million)",
    # Same two clauses, reverse order: the $ amount named before the
    # repurchase/buyback word (e.g. "authorized $500 million ... repurchase").
    r"(?:authorized|approved)[^.]{0,120}?\$\s*([\d.,]+)\s*(billion|million)"
    r"[^.]{0,80}?(?:repurchase|buyback)",
)]

_DIVIDEND_RE = re.compile(
    r"(?:declared|increased|raised|initiated)[^.]{0,80}?dividend[^.]{0,60}?"
    r"\$\s*([\d.]+)",
    re.IGNORECASE,
)


def _compact_amount(raw: str, unit: str):
    """"500", "million" -> "$500M"; "2.5", "billion" -> "$2.5B". None on an
    unparseable raw value."""
    value = _to_float(str(raw).replace(",", ""))
    if value is None:
        return None
    letter = "B" if unit[:1].lower() == "b" else "M"
    return f"${value:g}{letter}"


def detect_capital_returns(text) -> dict:
    """Conservative capital-return flags parsed from a release excerpt ->
    {"buyback": "$500M"|None, "dividend": "$0.31/sh"|None}. Buybacks require an
    authorization verb (authorized/approved) tied to a repurchase/buyback
    mention, either clause order. Dividends require an action verb
    (declared/increased/raised/initiated) tied to a dollar figure, so
    discussion-only mentions ("dividend policy", "no dividend was declared")
    never match. Pure — empty/None text, or no match, -> None for that field."""
    if not text:
        return {"buyback": None, "dividend": None}
    buyback = None
    for pattern in _BUYBACK_RE:
        match = pattern.search(text)
        if match:
            buyback = _compact_amount(match.group(1), match.group(2))
            break
    dividend = None
    match = _DIVIDEND_RE.search(text)
    if match:
        value = _to_float(match.group(1))
        dividend = f"${value:g}/sh" if value is not None else None
    return {"buyback": buyback, "dividend": dividend}


def _yf_upgrades(ticker: str):
    """Analyst grade actions via yfinance ->
    [(date, action_lowercase), ...] in the frame's own row order, or None
    (missing or empty frame). Import inside so a missing yfinance is an
    ImportError the caller can catch; exceptions otherwise left to the caller
    (mirrors _yf_fwd_estimates)."""
    import yfinance as yf
    frame = yf.Ticker(ticker).upgrades_downgrades
    if frame is None or frame.empty:
        return None
    rows = []
    for idx, row in frame.iterrows():
        try:
            when = idx.date()
        except wi_config.ConfigError:
            raise
        except Exception:
            when = _parse_day(str(idx))
        action = str(row.get("Action") or "").lower()
        if when is not None and action:
            rows.append((when, action))
    return rows or None


def count_analyst_moves(rows, report_date):
    """Tally analyst grade actions dated on/after report_date ->
    {"up": n, "down": n, "init": n}. `rows` None -> None (no data / seam
    failure) — the caller distinguishes "no data" from "zero moves". Action
    strings are matched by substring: contains "up" -> up, "down" -> down,
    "init" -> init, else ignored. Pure — an all-zero tally still returns the
    zero dict; the renderer decides whether to display it."""
    if rows is None:
        return None
    ref = _parse_day(report_date)
    counts = {"up": 0, "down": 0, "init": 0}
    for when, action in rows:
        if ref is not None and when is not None and when < ref:
            continue
        text = (action or "").lower()
        if "up" in text:
            counts["up"] += 1
        elif "down" in text:
            counts["down"] += 1
        elif "init" in text:
            counts["init"] += 1
    return counts


def _cell(frame, row: str, col: str):
    """frame.loc[row, col] -> float|None (missing row/col or NaN -> None)."""
    try:
        val = _to_float(frame.loc[row, col])
    except wi_config.ConfigError:
        raise
    except Exception:
        return None
    return val if val == val else None  # pandas NaN -> None


def _yf_fwd_estimates(ticker: str) -> dict:
    """Forward consensus + 30-day revision via yfinance ->
    {"fwd_eps_q", "fwd_eps_fy", "analyst_count", "revision_30d_pct",
    "rev_growth_fy"} (None for whatever is unavailable). One Ticker serves the
    earnings_estimate, eps_trend, and revenue_estimate frames — no extra
    network call. Isolated so tests can stub it offline; exceptions are left
    to the caller (_enrich_context)."""
    import yfinance as yf
    tk = yf.Ticker(ticker)
    out = {"fwd_eps_q": None, "fwd_eps_fy": None, "analyst_count": None,
           "revision_30d_pct": None, "rev_growth_fy": None}
    est = tk.earnings_estimate
    if est is not None and len(est):
        out["fwd_eps_q"] = _cell(est, "0q", "avg")
        out["fwd_eps_fy"] = _cell(est, "0y", "avg")
        count = _cell(est, "0q", "numberOfAnalysts")
        out["analyst_count"] = int(count) if count is not None else None
    trend = tk.eps_trend
    if trend is not None and len(trend):
        cur = _cell(trend, "0q", "current")
        ago = _cell(trend, "0q", "30daysAgo")
        if cur is not None and ago is not None and ago != 0:
            out["revision_30d_pct"] = round((cur - ago) / abs(ago) * 100.0, 1)
    rev = tk.revenue_estimate
    if rev is not None and len(rev):
        out["rev_growth_fy"] = _cell(rev, "0y", "growth")
    return out


def _enrich_context(cand: dict, config: dict, session, profile: dict) -> bool:
    """Fill the analyst-context fields on one top-N candidate: market cap and
    industry from the ALREADY-fetched profile2 dict (never a second request),
    the rest via the yfinance/Finnhub helpers. Every route is independently
    best-effort — a failing helper leaves that route's defaults in place.
    Returns True when any field was filled (health accounting)."""
    filled = False
    if profile.get("market_cap_m") is not None:
        cand["market_cap_m"] = profile["market_cap_m"]
        filled = True
    if profile.get("industry"):
        cand["industry"] = profile["industry"]
        filled = True
    ticker = cand.get("ticker") or ""
    try:
        info = _yf_company_info(ticker) or {}
        if info.get("sector"):
            cand["sector"] = info["sector"]
            filled = True
        if info.get("description"):
            cand["description"] = info["description"]
            filled = True
    except wi_config.ConfigError:
        raise
    except Exception:
        pass
    try:
        peers = _finnhub_peers(ticker, config, session)
        if peers:
            cand["peers"] = peers
            filled = True
    except wi_config.ConfigError:
        raise
    except Exception:
        pass
    try:
        streak = _yf_beat_history(ticker)
        if streak:
            cand["beat_streak"] = streak
            filled = True
    except wi_config.ConfigError:
        raise
    except Exception:
        pass
    try:
        est = _yf_fwd_estimates(ticker) or {}
        for field in ("fwd_eps_q", "fwd_eps_fy", "analyst_count",
                      "revision_30d_pct", "rev_growth_fy"):
            if est.get(field) is not None:
                cand[field] = est[field]
                filled = True
    except wi_config.ConfigError:
        raise
    except Exception:
        pass
    return filled


# --------------------------------------------------------------------------- #
# Pipeline ledger persistence (pending/covered/expired across mornings)
# --------------------------------------------------------------------------- #
def _covered_path(kb_root: str) -> str:
    return os.path.join(kb_root, "earnings_covered.json")


def _pipeline_path(kb_root: str) -> str:
    return os.path.join(kb_root, PIPELINE_FILE)


def read_pipeline(kb_root: str) -> dict:
    """Ledger of "TICKER:YYYY-MM-DD" -> {status, first_seen, last_seen, attempts,
    missing}. Missing or corrupt file -> {} (worst case we re-cover a name, never
    crash). One-time best-effort migration: if earnings_pipeline.json does not
    exist yet but the legacy earnings_covered.json does, convert each legacy key
    into a covered entry; the legacy file is left in place, untouched, and
    ignored once a real ledger has been written (by mark_covered)."""
    path = _pipeline_path(kb_root)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except wi_config.ConfigError:
            raise
        except Exception:
            return {}
    try:
        with open(_covered_path(kb_root), "r", encoding="utf-8") as fh:
            legacy = json.load(fh)
        if not isinstance(legacy, dict):
            return {}
        return {
            key: {"status": "covered", "first_seen": recorded, "last_seen": recorded,
                  "attempts": 0, "missing": []}
            for key, recorded in legacy.items()
        }
    except wi_config.ConfigError:
        raise
    except Exception:
        return {}


def blocked_keys(pipeline: dict) -> set:
    """"TICKER:date" keys that must not resurface: covered (already published)
    or expired (gave up after MAX_ATTEMPTS). Pending keys are NOT blocked — they
    retry next morning."""
    return {
        key for key, entry in (pipeline or {}).items()
        if isinstance(entry, dict) and entry.get("status") in ("covered", "expired")
    }


def _int_attempts(value) -> int:
    """Coerce a ledger entry's "attempts" to int; a corrupted/non-numeric
    value degrades to 0 rather than raising."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def note_pending(pipeline: dict, key: str, missing: list, today: str) -> None:
    """Create or update a pending ledger entry in place: increments attempts,
    sets missing/last_seen, sets first_seen only when the key is new. Pure dict
    mutation (no I/O) so a run can batch mutations before one write."""
    entry = pipeline.get(key)
    if not isinstance(entry, dict):
        entry = {"first_seen": today}
        pipeline[key] = entry
    entry["status"] = "pending"
    entry["attempts"] = _int_attempts(entry.get("attempts")) + 1
    entry["missing"] = list(missing or [])
    entry["last_seen"] = today


def expire_stale_pending(pipeline: dict, today: str) -> None:
    """Sweep entries that stopped being routed: a pending candidate's
    last_seen only refreshes via note_pending on a run prefilter_candidates
    still routes it through (e.g. its gate condition tripped again). If the
    gate condition disappears for good (a vendor revision, say), prefilter
    drops it before routing and last_seen goes stale forever — pending
    entries are exempt from mark_covered's recency prune, so nothing else
    would ever retire it. Once last_seen is more than WINDOW_DAYS behind
    today, expire it. Unparseable last_seen is treated as stale. Pure dict
    mutation (no I/O), same contract as note_pending."""
    ref = _parse_day(today) or date.today()
    for entry in (pipeline or {}).values():
        if not isinstance(entry, dict) or entry.get("status") != "pending":
            continue
        last_seen = _parse_day(entry.get("last_seen"))
        if last_seen is None or (ref - last_seen).days > WINDOW_DAYS:
            entry["status"] = "expired"


def write_pipeline(kb_root: str, pipeline: dict) -> None:
    """Persist the ledger (best-effort — never raises)."""
    try:
        os.makedirs(kb_root, exist_ok=True)
        with open(_pipeline_path(kb_root), "w", encoding="utf-8") as fh:
            json.dump(pipeline or {}, fh, ensure_ascii=False, indent=2)
    except wi_config.ConfigError:
        raise
    except Exception:
        pass


_SNAPSHOT_KEYS = ("name", "hour", "eps_est", "eps_act", "surprise_pct",
                  "rev_est", "rev_act", "rev_act_source", "price_move_pct",
                  # guidance_struct carries the guide; the two forward
                  # consensus figures are what the guide is JUDGED against.
                  # Without them beat 2 renders the range with no
                  # above/inline/below verdict -- half of spec Sec.6.4's
                  # guidance line, silently missing rather than broken.
                  "guidance_struct", "fwd_eps_q", "fwd_eps_fy")

def mark_covered(kb_root: str, candidates: list, today: str) -> None:
    """Record today's candidates as covered and prune covered/expired entries
    recorded more than COVERED_RETENTION_DAYS ago (report dates cycle quarterly;
    two weeks of memory is plenty and keeps the file tiny). Pending entries are
    exempt from this recency prune — only expiry retires them. Best-effort —
    never raises.

    This ledger answers one question: has this TICKER:report_date already been
    covered? Whether a company's periodic filing was later read is a separate
    question with a separate lifetime, and lives in `post_earnings_pool`. They
    were briefly the same file, which is how an entry awaiting its filing came
    to be exempt from the prune and could sit here indefinitely."""
    try:
        pipeline = read_pipeline(kb_root)
        for cand in candidates or []:
            key = f'{cand["ticker"]}:{cand["report_date"]}'
            existing = pipeline.get(key)
            first_seen = (existing.get("first_seen")
                          if isinstance(existing, dict) and existing.get("first_seen")
                          else today)
            pipeline[key] = {
                "status": "covered", "first_seen": first_seen, "last_seen": today,
                "attempts": 0, "missing": [],
            }
        ref = _parse_day(today) or date.today()
        pruned = {}
        for key, entry in pipeline.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("status") == "pending":
                pruned[key] = entry
                continue
            when = _parse_day(entry.get("last_seen"))
            if when is not None and (ref - when).days <= wi_config.get().tuning.get(
                    "retention_days", {}).get("post_earnings", COVERED_RETENTION_DAYS):
                pruned[key] = entry
        write_pipeline(kb_root, pruned)
    except wi_config.ConfigError:
        raise
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# SEC fundamentals lane (Phase 1, Global Constraint 8): decorates bellwether
# US candidates with sec_fundamentals.build_fundamentals output. Runs after
# completeness-relevant data is settled and before _route_pending, and never
# defers, blocks, or alters a candidate -- completeness()/_route_pending never
# look at these fields. sec_fundamentals is imported lazily so a missing or
# broken module degrades the lane, never earnings_report itself.
# --------------------------------------------------------------------------- #
def _attach_fundamentals(merged: list, enrich: list, config: dict, session,
                          health: dict) -> None:
    """Attach `fundamentals` (the full dict from
    sec_fundamentals.build_fundamentals) to eligible candidates in place.
    Eligible: source == "us", ticker in bellwether_tickers(), and the candidate
    is in `enrich`. Never raises: any failure at all (import, CIK lookup, an
    attribute read on `sec_fundamentals`, or a build_fundamentals call that
    violates its own never-raise contract) degrades to
    health["sec_fundamentals"] = {"ok": False, "note": ...} and leaves the
    candidate's fundamentals/fundamentals_flags at their _CONTEXT_DEFAULTS
    values.

    health["sec_fundamentals"]["ok"] semantics: no eligible candidates -> the
    key is left untouched (an idle lane is not a failure); any exception ->
    False; otherwise True iff at least one candidate resolved to "ok" or
    "stale" (a legitimate determination), False when every candidate came
    back "unavailable" (the lane ran but produced nothing usable)."""
    enrich_ids = {id(c) for c in enrich}
    eligible = [
        cand for cand in merged
        if cand.get("source") == "us" and cand.get("ticker") in bellwether_tickers()
        and id(cand) in enrich_ids
    ]
    if not eligible:
        return

    try:
        from worldintel.core import sec_fundamentals as sf
        cik_map = _load_cik_map(session)
        deadline_at = sf._now() + sf.LANE_DEADLINE_S  # one deadline for the whole lane
        counts = {"ok": 0, "stale": 0, "unavailable": 0}
        errors = []
        for cand in eligible:
            if sf._now() >= deadline_at:
                break  # lane out of time; remaining candidates keep their defaults
            cik = cik_map.get(cand["ticker"])
            if not cik:
                continue  # unmapped ticker; candidate keeps its defaults
            try:
                result = sf.build_fundamentals(
                    cik, cand["report_date"], session, health, deadline_at)
            except wi_config.ConfigError:
                raise
            except Exception as exc:
                errors.append(f'{cand["ticker"]}: {_short(exc)}')
                continue
            cand["fundamentals"] = result
            cand["fundamentals_flags"] = referee_flags(cand, result)
            status = result.get("status") if isinstance(result, dict) else None
            counts[status if status in counts else "unavailable"] += 1
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["sec_fundamentals"] = {"ok": False, "note": _short(exc)}
        return

    if errors:
        health["sec_fundamentals"] = {
            "ok": False,
            "note": f"{len(errors)} errored ({'; '.join(errors)})",
        }
    else:
        health["sec_fundamentals"] = {
            "ok": (counts["ok"] + counts["stale"]) > 0,
            "note": (f'{counts["ok"]} ok, {counts["stale"]} stale, '
                     f'{counts["unavailable"]} unavailable'),
        }


# --------------------------------------------------------------------------- #
# The referee (Task 5): SEC-filed figures cross-checking the vendor's
# reported actuals. It REPORTS a disagreement; it never adjudicates --
# eps_act, surprise_pct and gates are read here but never written, and come
# out byte-identical to how they went in. [] whenever any precondition
# fails; never raises (guards both tolerance comparisons against a zero or
# missing vendor figure).
# --------------------------------------------------------------------------- #
def referee_flags(cand: dict, fundamentals: dict) -> list:
    """Flag dicts (usually 0 or 1) when a filed SEC diluted EPS disagrees
    with the vendor's reported actual. All four preconditions must hold:
    1. fundamentals["status"] == "ok".
    2. SEC revenue_q and cand["rev_act"] both present and within 1% of each
       other -- this proves period alignment, the reason a disagreement is
       trustworthy rather than a fiscal-calendar artefact.
    3. SEC eps_diluted_q present with basis == "direct" -- never "derived",
       "stitched" or "blocked". A per-share figure we ourselves distrust
       (MSFT's fiscal-Q4 4.81-vs-4.74 case) may not accuse a vendor.
    4. cand["eps_act"] present and differing from the SEC figure by more
       than 1%.
    """
    try:
        if not isinstance(fundamentals, dict) or fundamentals.get("status") != "ok":
            return []

        revenue_q = fundamentals.get("revenue_q")
        rev_act = cand.get("rev_act")
        if not isinstance(revenue_q, dict) or not rev_act:
            return []
        sec_rev = revenue_q.get("value")
        if sec_rev is None:
            return []
        if abs(sec_rev - rev_act) / abs(rev_act) > 0.01:
            return []

        eps_diluted_q = fundamentals.get("eps_diluted_q")
        if not isinstance(eps_diluted_q, dict) or eps_diluted_q.get("basis") != "direct":
            return []
        sec_eps = eps_diluted_q.get("value")
        eps_act = cand.get("eps_act")
        if sec_eps is None or not eps_act:
            return []
        if abs(sec_eps - eps_act) / abs(eps_act) <= 0.01:
            return []

        return [{"kind": "eps_disagreement", "sec": sec_eps, "vendor": eps_act,
                 "form": fundamentals.get("form")}]
    except wi_config.ConfigError:
        raise
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Beat 2 (Spec C D3): filing follow-ups. This is a SECOND producer of
# candidates, not a modification of the first -- normal discovery
# (prefilter_candidates, the gates, _blend_score, _apply_cap) is untouched and
# blocked_keys keeps its exact meaning, which keeps the riskiest code in the
# system out of the change. A follow-up therefore bypasses the gates, the
# candidate cap, completeness() and _route_pending: beat 1 already earned the
# coverage, and re-gating on a stale price move would be meaningless.
# --------------------------------------------------------------------------- #
def _has_resolved_metric(fundamentals) -> bool:
    """True when at least one metric or ratio carries a value. status == "ok"
    already implies the revenue keystone resolved, so this is belt-and-braces:
    a follow-up with nothing new to say must never be emitted."""
    if not isinstance(fundamentals, dict) or fundamentals.get("status") != "ok":
        return False
    return any(isinstance(value, dict) and value.get("value") is not None
               for value in fundamentals.values())


# --------------------------------------------------------------------------- #
# Completeness gate, deferral routing, and expiry (Spec A D4-D5): a candidate
# only enters the brief once its key metrics are complete. Incomplete ones
# defer into the pipeline ledger and retry the next morning; once MAX_ATTEMPTS
# is exhausted we publish best-effort if a gate still trips on current data,
# else give up and mark the ledger entry expired. Nothing is silently lost.
# --------------------------------------------------------------------------- #
def completeness(cand: dict, enriched: bool) -> list:
    """Missing-metric labels for one candidate (lane-aware, pure). US lane
    requires eps_est, eps_act, a resolved revenue actual (rev_act_source ==
    "dropped" counts RESOLVED — an implausible-everywhere revenue must not
    defer forever; a missing rev_est means no referee exists, so revenue is
    never required), price_move_pct, and a non-empty release_excerpt — but
    only when `enriched` is True (the candidate went through the EDGAR
    release-fetch step). Intl lane requires only eps_act and price_move_pct
    (A-share eps_est is legitimately absent -> never required)."""
    missing = []
    if cand.get("source") == "us":
        if cand.get("eps_est") is None:
            missing.append("eps_est")
        if cand.get("eps_act") is None:
            missing.append("eps_act")
        rev_est = cand.get("rev_est")
        rev_act = cand.get("rev_act")
        if (rev_act is None and cand.get("rev_act_source") != "dropped"
                and rev_est is not None):
            missing.append("rev_act")
        if cand.get("price_move_pct") is None:
            missing.append("price_move_pct")
        if enriched and not cand.get("release_excerpt"):
            missing.append("release_excerpt")
    else:
        if cand.get("eps_act") is None:
            missing.append("eps_act")
        if cand.get("price_move_pct") is None:
            missing.append("price_move_pct")
    return missing


def _current_gates(cand: dict) -> list:
    """Which of the three prefilter gates (size/surprise/reaction) trip on a
    candidate's PRESENT values. Shared by prefilter_candidates and the
    MAX_ATTEMPTS expiry check (which re-derives gates on current data rather
    than trusting the possibly-stale "gates" the candidate already carries)."""
    gates = []
    if (cand.get("ticker") or "") in LARGECAP_INDEX_SET:
        gates.append("size")
    sp = cand.get("surprise_pct")
    est = cand.get("eps_est")
    if (sp is not None and abs(sp) >= SURPRISE_PCT
            and est is not None and abs(est) >= ABS_EPS_FLOOR):
        gates.append("surprise")
    pm = cand.get("price_move_pct")
    if pm is not None and abs(pm) >= REACTION_PCT:
        gates.append("reaction")
    return gates


def _route_pending(merged: list, enrich: list, pipeline: dict, today: str) -> tuple:
    """Completeness-gate every candidate in `merged`. Complete ones stay
    published; incomplete ones defer into `pipeline` (pure dict mutation, same
    contract as note_pending) unless MAX_ATTEMPTS is exhausted, in which case
    a still-tripping gate publishes best-effort and a fully-quiet candidate is
    marked "expired". Returns
    (published, pending_count, expired_count, pending_first_missing) — the
    last a list of each newly-pending candidate's first missing label, used
    to build the health["pe_pipeline"] breakdown."""
    enrich_ids = {id(c) for c in enrich}
    published = []
    pending_count = 0
    expired_count = 0
    pending_first_missing = []
    for cand in merged:
        key = f'{cand["ticker"]}:{cand["report_date"]}'
        missing = completeness(cand, id(cand) in enrich_ids)
        if not missing:
            published.append(cand)
            continue
        entry = pipeline.get(key)
        attempts = _int_attempts(entry.get("attempts")) if isinstance(entry, dict) else 0
        if attempts + 1 < MAX_ATTEMPTS:
            note_pending(pipeline, key, missing, today)
            pending_count += 1
            pending_first_missing.append(missing[0])
            continue
        if _current_gates(cand):
            published.append(cand)  # best-effort: still relevant despite gaps
        else:
            first_seen = (entry.get("first_seen")
                          if isinstance(entry, dict) and entry.get("first_seen") else today)
            pipeline[key] = {
                "status": "expired", "first_seen": first_seen, "last_seen": today,
                "attempts": attempts + 1, "missing": missing,
            }
            expired_count += 1
    return published, pending_count, expired_count, pending_first_missing


def _pipeline_note(published: int, pending: int, expired: int, missing_labels: list) -> str:
    """Compact ASCII health["pe_pipeline"] note. The parenthetical
    missing-label breakdown is included only when there's at least one
    pending candidate this run; published/pending/expired counts always
    appear, even at zero."""
    if pending:
        counts = {}
        for label in missing_labels:
            counts[label] = counts.get(label, 0) + 1
        breakdown = ", ".join(f"{n} awaiting {label}" for label, n in counts.items())
        return f"{published} published, {pending} pending ({breakdown}), {expired} expired"
    return f"{published} published, {pending} pending, {expired} expired"


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def build_post_earnings(config: dict, raw_dir: str, health: dict, today: str,
                        kb_root: str) -> list:
    """Full post-earnings pass: gather (US + intl) -> prefilter -> enrich the top
    US names with EDGAR release text -> fill display names. Never raises; a
    failure anywhere degrades to [] with a health note. Marking candidates as
    covered is left to run.py, which only records them after a successful brief.

    Every returned dict carries exactly the _FINAL_KEYS contract.

    Beat 2 is no longer produced here. It used to ride back on a `followups`
    out-parameter fed by a forward watch that polled SEC for filings that had
    not landed; it is now a reconciliation against the filings that actually
    did land, driven from `run.py` (see `reconcile`)."""
    try:
        config = config or {}
        session = requests.Session()
        session.headers.update({"User-Agent": _plain_ua()})

        pipeline = read_pipeline(kb_root)
        covered = blocked_keys(pipeline)
        us = fetch_finnhub_calendar(config, raw_dir, health, today)
        intl = fetch_intl_watchlist(raw_dir, health, today)
        moves = fetch_price_reactions(us, health)
        merged = prefilter_candidates(us + intl, moves, covered, health)

        # merged leads with the reserved bellwethers, then runs blended-score
        # sorted, so the first RELEASE_FETCH_MAX US names are the ones most worth
        # a write-up. EDGAR only covers SEC filers -> US lane only.
        fetched = 0
        enrich = []  # the same top-N set also gets the analyst-context lookups
        for cand in merged:
            if cand.get("source") == "us" and fetched < RELEASE_FETCH_MAX:
                cand["release_excerpt"] = fetch_earnings_release(
                    cand["ticker"], session, health)
                fetched += 1
                enrich.append(cand)
                try:
                    yf_row = _yf_quarterly_revenue(cand["ticker"])
                except wi_config.ConfigError:
                    raise
                except Exception:
                    yf_row = None
                rev_act, rev_source, _note = resolve_revenue_actual(
                    cand, cand["release_excerpt"], yf_row)
                cand["rev_act"] = rev_act
                cand["rev_act_source"] = rev_source
            else:
                cand["release_excerpt"] = ""
                cand["rev_act_source"] = "finnhub"
                rev_est, rev_act = cand.get("rev_est"), cand.get("rev_act")
                if (rev_est is not None and rev_act is not None and rev_est != 0
                        and not (REV_BAND[0] <= rev_act / rev_est <= REV_BAND[1])):
                    cand["rev_act"] = None
                    cand["rev_act_source"] = "dropped"

        overridden = sum(
            1 for c in merged if c.get("rev_act_source") in ("yfinance", "release"))
        dropped_rev = sum(1 for c in merged if c.get("rev_act_source") == "dropped")
        health["rev_check"] = {
            "ok": True, "note": f"{overridden} overridden, {dropped_rev} dropped",
        }

        # Display names for final candidates only (spares the Finnhub budget).
        # Profiles are kept so enrichment reuses market cap / industry from the
        # SAME profile2 call — never a second request per ticker.
        profiles = {}
        for cand in merged:
            if not cand.get("name"):
                prof = _finnhub_profile(cand["ticker"], config, session)
                profiles[cand["ticker"]] = prof
                cand["name"] = prof.get("name") or cand["ticker"]

        # Analyst context: every candidate carries every context key (defaults);
        # only the release-fetch set gets the real lookups.
        for cand in merged:
            for field, default in _CONTEXT_DEFAULTS.items():
                cand[field] = list(default) if isinstance(default, list) else default
        enriched = 0
        today_day = _parse_day(today)
        for cand in enrich:
            try:
                if _enrich_context(cand, config, session,
                                   profiles.get(cand["ticker"]) or {}):
                    enriched += 1
            except wi_config.ConfigError:
                raise
            except Exception:
                pass  # one candidate's context must never sink the build
            cap_returns = detect_capital_returns(cand.get("release_excerpt"))
            cand["buyback"] = cap_returns["buyback"]
            cand["dividend"] = cap_returns["dividend"]
            # Analyst reactions need at least one session after the report to
            # exist; day-one candidates keep the None default.
            report_day = _parse_day(cand.get("report_date"))
            if today_day and report_day and today_day > report_day:
                try:
                    rows = _yf_upgrades(cand["ticker"])
                except wi_config.ConfigError:
                    raise
                except Exception:
                    rows = None
                cand["analyst_moves"] = count_analyst_moves(
                    rows, cand.get("report_date"))
        health["pe_context"] = {
            "ok": True, "note": f"{enriched}/{len(enrich)} enriched",
        }

        _attach_fundamentals(merged, enrich, config, session, health)

        # Completeness gate: incomplete candidates defer via the pipeline
        # ledger (or expire at MAX_ATTEMPTS) instead of publishing half-empty.
        merged, pending_count, expired_count, pending_missing = _route_pending(
            merged, enrich, pipeline, today)
        health["pe_pipeline"] = {
            "ok": True,
            "note": _pipeline_note(len(merged), pending_count, expired_count,
                                    pending_missing),
        }
        expire_stale_pending(pipeline, today)
        write_pipeline(kb_root, pipeline)

        out = [{key: cand.get(key) for key in _FINAL_KEYS} for cand in merged]
        health["post_earnings"] = {
            "ok": True,
            "note": f"{len(out)} candidates" if out else "no candidates",
        }
        return out
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["post_earnings"] = {"ok": False, "note": _short(exc)}
        return []
