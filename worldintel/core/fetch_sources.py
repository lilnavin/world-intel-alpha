"""world-intel Module A — resilient data fetch layer.

Implements `fetch_all(config, raw_dir, today) -> dict` plus per-source fetchers.

ABSOLUTE RULE: no external call may raise out of this module. Every source is
wrapped; on any failure we record `source_health[source] = {"ok": False,
"note": "<short reason>"}` and omit that data. A missing API key skips the
source with note "no api key".

Pure normalization helpers (prefixed `_normalize_`) are kept separate from the
network-touching `fetch_*` functions so they can be unit-tested against recorded
sample JSON without hitting the network.

Standard library + `requests`. `yfinance` is imported lazily, only inside its
helpers (`_yf_history`, `_yf_earnings_date`), so its absence never breaks the module.
"""

from __future__ import annotations

from worldintel import config as wi_config
import json
import os
import re
from datetime import date, datetime, timedelta

import requests

from worldintel.core import feeds

# --------------------------------------------------------------------------- #
# Endpoints (do not guess — fixed per CONTRACT.md Module A)
# --------------------------------------------------------------------------- #
TWELVEDATA_URL = "https://api.twelvedata.com/quote"
FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
FEDREG_URL = "https://www.federalregister.gov/api/v1/documents.json"
# SEC EDGAR full-text search JSON backend (verified live 2026-07-02: HTTP 200,
# hits at data["hits"]["hits"][*]["_source"], _id = "<accession>:<primary-doc>").
EDGAR_URL = "https://efts.sec.gov/LATEST/search-index"

def _plain_ua(note: str = "resilient-fetch") -> str:
    return wi_config.get().plain_user_agent(note)
# SEC returns HTTP 403 without a descriptive, contactable User-Agent.
def _sec_ua() -> str:
    return wi_config.get().sec_user_agent()
_HTTP_TIMEOUT = 25

# EDGAR notable forms: registration statements, IPO pricing, material events.
def edgar_forms():
    """Resolve the configured source table at call time."""
    return wi_config.get().edgar_forms

# Earnings: forward window (days) and the bellwether watchlist as (ticker, name).
# Intl tickers use Yahoo suffixes (.KS Korea, .T Tokyo).
def earnings_window_days():
    """Resolve the configured source table at call time."""
    return wi_config.get().earnings_window_days
def earnings_watchlist():
    """Resolve the configured source table at call time."""
    return wi_config.get().earnings_watchlist

# Markets: (bundle_key, twelve_data_symbol, yfinance_ticker). yfinance is the
# primary; Twelve Data only fills gaps it left (see fetch_markets for why the
# original order was reversed). The Twelve Data symbols are kept because they
# are still the gap-filler's addresses, but index/commodity coverage on the free
# tier is best-effort and some symbols mis-resolve outright.
def market_spec():
    """Resolve the configured source table at call time."""
    return wi_config.get().market_spec

# FRED series: (series_id, human label). DFF=fed funds, DGS10=10Y, CPIAUCSL=CPI.
def fred_series():
    """Resolve the configured source table at call time."""
    return wi_config.get().fred_series

# GDELT themes: (label, DOC query). One tone call + one artlist call per theme.
def gdelt_themes():
    """Resolve the configured source table at call time."""
    return wi_config.get().gdelt_themes

_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "after", "over",
    "into", "amid", "says", "said", "will", "have", "has", "was", "are",
    "but", "not", "you", "your", "his", "her", "their", "they", "its",
    "new", "more", "than", "out", "how", "why", "what", "who", "when",
    "about", "could", "would", "should", "amid", "near", "amid",
}


# --------------------------------------------------------------------------- #
# Small internal utilities
# --------------------------------------------------------------------------- #
def _short(exc: Exception) -> str:
    """A short, single-line reason string for source_health notes."""
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


def _empty_markets() -> dict:
    out = {}
    for group, items in market_spec().items():
        out[group] = {
            key: {
                "level": None, "change_pct": None, "change_abs": None,
                "asof": None, "asof_prev": None,
            }
            for key, _td, _yf in items
        }
    return out


# --------------------------------------------------------------------------- #
# Normalization helpers (pure — unit-tested with recorded fixtures)
# --------------------------------------------------------------------------- #
# Tier-1 reputable outlets / wires. Matched as case-insensitive substrings against
# the item's source name AND its url. Some entries are deliberately domain-shaped
# (e.g. "ft.com", "nytimes") to avoid over-matching short tokens.
_TIER1_MARKERS = (
    "reuters", "apnews", "ap.org", "associated press", "bloomberg", "wsj",
    "wall street journal", "ft.com", "financial times", "theguardian",
    "the guardian", "guardian", "bbc", "cnbc", "nytimes", "new york times",
    "washingtonpost", "washington post", "the economist", "economist.com",
    "nikkei", "japan times", "japantimes", "kyodo", "cbc", "politico", "axios",
    "npr",
)
# Tier-3 blogs / aggregators / low-trust hosts.
_TIER3_MARKERS = (
    "blogspot", "blogger.com", "medium.com", "substack", "wordpress",
    "share tips", "sharetips", "seekingalpha", "reddit",
)


def _source_tier(source: str, url: str) -> int:
    """Credibility tier for a news item: 1 = major/wire, 3 = blog/aggregator, 2 = other.

    Pure and case-insensitive; matches marker substrings against the source name and
    the url domain. Tier-3 is checked first so an aggregator reposting under a wire
    byline isn't over-credited."""
    hay = f"{source or ''} {url or ''}".lower()
    if any(m in hay for m in _TIER3_MARKERS):
        return 3
    if any(m in hay for m in _TIER1_MARKERS):
        return 1
    return 2


def _normalize_td_quote(data: dict):
    """Twelve Data /quote -> {level, change_pct, asof} or None on bad/error data."""
    if not isinstance(data, dict):
        return None
    if data.get("status") == "error" or "code" in data and "close" not in data:
        return None
    level = _to_float(data.get("close"))
    if level is None:
        return None
    asof = data.get("datetime") or data.get("timestamp")
    return {
        "level": level,
        "change_pct": _to_float(data.get("percent_change")),
        "asof": str(asof) if asof not in (None, "") else None,
    }


def _horizon_fields(pairs) -> dict:
    """Long-horizon stats from [(date, close), ...] pairs spanning up to ~1 year.

    ytd_* compare against the last close of the PRIOR calendar year; d90_*
    against the last close on/before asof-90d; off_high_pct is the % distance
    below the window high (0.0 = at the high); pctl_1y is the percentile rank
    of the latest close within the window. Each field is None when the window
    can't support it, so short histories degrade instead of lying."""
    out = {"ytd_pct": None, "ytd_abs": None, "d90_pct": None, "d90_abs": None,
           "off_high_pct": None, "pctl_1y": None}
    pairs = sorted((d, c) for d, c in pairs if c is not None)
    if not pairs:
        return out
    asof, level = pairs[-1]

    def _delta(ref):
        pct = round((level / ref - 1.0) * 100.0, 1) if ref else None
        return pct, round(level - ref, 4)

    prior_year = [c for d, c in pairs if d.year < asof.year]
    if prior_year:
        out["ytd_pct"], out["ytd_abs"] = _delta(prior_year[-1])
    cutoff = asof - timedelta(days=90)
    older = [c for d, c in pairs if d <= cutoff]
    if older:
        out["d90_pct"], out["d90_abs"] = _delta(older[-1])
    closes = [c for _, c in pairs]
    high = max(closes)
    if high:
        out["off_high_pct"] = round((1.0 - level / high) * 100.0, 1)
    out["pctl_1y"] = round(100.0 * sum(1 for c in closes if c <= level) / len(closes))
    return out


def _yf_quote_from_history(hist, extra_bar=None):
    """Build a quote from a yfinance history DataFrame.

    NaN closes are dropped, then the two most-recent *available* daily closes are
    compared. `change_abs` (level - prev_level, in the instrument's own units) lets
    a downstream renderer show rate moves in basis points; `asof_prev` exposes the
    date of the prior close, so a non-adjacent-session gap (holiday/partial bar) is
    visible and auditable rather than silently corrupting `change_pct`.

    `extra_bar` is an optional (date, close) appended only when STRICTLY newer
    than the last daily bar — the intraday freshness patch for non-US indices
    whose consolidated daily bar lags the close (see fetch_markets). When the
    daily series already has that date, the consolidated bar wins.
    """
    try:
        closes = hist["Close"].dropna()
    except wi_config.ConfigError:
        raise
    except Exception:
        return None
    pairs = []
    for ts, close in closes.items():
        try:
            d = ts.date()
            if isinstance(d, str):
                d = date.fromisoformat(d)
            pairs.append((d, float(close)))
        except wi_config.ConfigError:
            raise
        except Exception:
            continue
    if extra_bar:
        try:
            bar_date, bar_close = extra_bar
            if isinstance(bar_date, str):
                bar_date = date.fromisoformat(bar_date)
            if pairs and bar_date > pairs[-1][0]:
                pairs.append((bar_date, float(bar_close)))
        except wi_config.ConfigError:
            raise
        except Exception:
            pass
    if not pairs:
        return None

    asof_d, level = pairs[-1]
    change_pct = None
    change_abs = None
    asof_prev = None
    if len(pairs) >= 2:
        prev_d, prev = pairs[-2]
        change_abs = round(level - prev, 4)
        if prev:
            change_pct = round((level / prev - 1.0) * 100.0, 3)
        asof_prev = prev_d.isoformat()
    quote = {
        "level": round(level, 4),
        "change_pct": change_pct,
        "change_abs": change_abs,
        "asof": asof_d.isoformat(),
        "asof_prev": asof_prev,
    }
    quote.update(_horizon_fields(pairs))
    return quote


def _yf_intraday_last_session(ticker: str):
    """(date, close) of the most recent session per the INTRADAY endpoint, which
    updates right after the close — hours before the consolidated daily bar
    appears for non-US indices. Timestamps are exchange-local, so a holiday
    simply yields the same date as the daily series (and no freshening).
    Isolated in its own function so tests can stub it offline."""
    import yfinance as yf
    closes = yf.Ticker(ticker).history(period="2d", interval="60m")["Close"].dropna()
    if len(closes) == 0:
        return None
    return closes.index[-1].date(), float(closes.iloc[-1])


def _normalize_fred(data: dict, label: str):
    """FRED observations (sorted desc, limit 1) -> {label, value, asof} or None."""
    obs = (data or {}).get("observations") or []
    if not obs:
        return None
    value = _to_float(obs[0].get("value"))
    if value is None:
        return None
    return {"label": label, "value": value, "asof": obs[0].get("date") or ""}


def _normalize_gdelt_tone(data: dict):
    """GDELT timelinetone -> mean average-tone over the window (float|None)."""
    timeline = (data or {}).get("timeline") or []
    if not timeline:
        return None
    points = timeline[0].get("data") or []
    vals = [v for v in (_to_float(p.get("value")) for p in points) if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 3)


def _normalize_gdelt_volume(data: dict) -> int:
    """GDELT artlist -> article count as a coarse volume proxy."""
    return len((data or {}).get("articles") or [])


def _normalize_gov(data: dict) -> list:
    out = []
    for doc in (data or {}).get("results") or []:
        agencies = doc.get("agencies") or []
        agency = ""
        if agencies and isinstance(agencies[0], dict):
            agency = agencies[0].get("name") or ""
        out.append({
            "title": doc.get("title") or "",
            "url": doc.get("html_url") or doc.get("pdf_url") or "",
            "agency": agency,
            "date": doc.get("publication_date") or "",
        })
    return out


def _clean_edgar_name(name: str) -> str:
    """Strip the trailing "(TICKERS)" and "(CIK ...)" annotations EDGAR appends to a
    display name, e.g. 'ATLANTIC INTL CORP.  (ATLN)  (CIK 0001605888)' -> 'ATLANTIC INTL CORP.'."""
    out = re.sub(r"\s*\(CIK[^)]*\)\s*$", "", name or "")
    out = re.sub(r"\s*\([^)]*\)\s*$", "", out)
    return out.strip()


def _normalize_edgar(data: dict) -> list:
    """SEC EDGAR full-text search hits -> notable-filing rows (most-recent first, cap 15).

    Each hit's ``_id`` is "<accession>:<primary-document>"; the absolute sec.gov
    Archives URL is rebuilt from the CIK, dash-stripped accession, and that document.
    """
    hits = (((data or {}).get("hits") or {}).get("hits")) or []
    rows = []
    for hit in hits:
        src = hit.get("_source") or {}
        ciks = src.get("ciks") or []
        cik = str(ciks[0]) if ciks else ""
        adsh = src.get("adsh") or ""
        names = src.get("display_names") or []
        hit_id = hit.get("_id") or ""
        doc = hit_id.split(":", 1)[1] if ":" in hit_id else ""
        url = ""
        if cik and adsh and doc:
            url = (
                "https://www.sec.gov/Archives/edgar/data/"
                f"{cik.lstrip('0') or '0'}/{adsh.replace('-', '')}/{doc}"
            )
        rows.append({
            "company": _clean_edgar_name(names[0]) if names else "",
            "form": src.get("form") or "",
            "filed": src.get("file_date") or "",
            "url": url,
            "cik": cik,
        })
    rows.sort(key=lambda r: r["filed"], reverse=True)
    return rows[:15]


def _story_tokens(title: str) -> frozenset:
    """A title's distinctive tokens: >=4 letters, lowercased, stopwords dropped."""
    return frozenset(t for t in re.findall(r"[A-Za-z]{4,}", (title or "").lower())
                     if t not in _STOPWORDS)


# The old key was a title's 3 longest tokens. Those differ per outlet, so the
# seven 2026-08-15 Chiba-flood headlines produced seven distinct keys and every
# one of them counted as uncorroborated. Measured over that day's whole
# 1,604-item pool, 94% of items counted 1 — the signal was inert, and the story
# (8 dead, first-ever JMA Level 5 warning for the prefecture) missed the japan
# desk by 0.1. Two headlines are now the same story when they share enough
# distinctive vocabulary, judged on three things at once:
#
#   _CLUSTER_MIN_SHARED  two shared tokens. One is coincidence.
#   _CLUSTER_MIN_RATIO   those tokens must be a real fraction of the shorter
#                        headline, so a long title can't absorb a short one.
#   _CLUSTER_ANCHOR_FRAC at least one shared token must be RARE in today's pool
#                        — carried by at most 1% of it. Without this, "White
#                        House" alone (df 24 and 30 in a 1,604-item pool) merged
#                        the ballroom lawsuit, the press secretary's resignation
#                        and a staff shuffle into one 22-item blob.
#
# Transitive clustering was tried and rejected: chaining weak links merged 63
# unrelated items into a single "story". Each item therefore counts only the
# outlets it matches DIRECTLY. Deterministic (no set iteration in the output),
# pure, and one pass over an inverted index.
_CLUSTER_MIN_SHARED = 2
_CLUSTER_MIN_RATIO = 0.3
_CLUSTER_ANCHOR_FRAC = 0.01
_CLUSTER_MIN_TOKENS = 3


def _is_same_story(tokens_a, tokens_b, shared, rarest_df, anchor,
                   min_ratio=_CLUSTER_MIN_RATIO) -> bool:
    """The clustering rule itself, independent of how a caller found the pair.

    Extracted so the two traversals below cannot drift apart: one sweeps the
    whole pool through an inverted index, the other answers a single pairwise
    question, and a rule that lived in both would eventually be tuned in one.
    """
    if len(tokens_a) < _CLUSTER_MIN_TOKENS or len(tokens_b) < _CLUSTER_MIN_TOKENS:
        return False
    if len(shared) < _CLUSTER_MIN_SHARED:
        return False
    if min_ratio and len(shared) / min(len(tokens_a), len(tokens_b)) < min_ratio:
        return False
    return rarest_df <= anchor


def story_matcher(news: list, *, min_ratio: float = _CLUSTER_MIN_RATIO):
    """Return `same_story(a, b) -> bool` for two news ITEMS from `news`.

    The pairwise counterpart to `_story_outlet_counts`, for callers that need
    "are these two the same story?" rather than "how many outlets ran it" —
    `select_news` uses it to stop one event owning a desk. The document
    frequencies the rare-anchor rule needs come from `news`, so a matcher is
    only meaningful for the pool it was built from.

    `min_ratio` relaxes the share-of-headline test, and 0 disables it. Counting
    corroboration keeps the default: a false merge there inflates a score with
    outlets that never ran the story. A variety cap can afford to be looser,
    because a false merge only pushes one candidate into the overflow it can be
    topped back up from. What neither may relax is the rare-anchor rule — that
    is the whole reason three different White House stories stay three stories.

    Never raises: any failure yields a predicate that answers False throughout,
    so a caller degrades to no grouping rather than to wrong grouping.
    """
    try:
        titles = [_story_tokens((i or {}).get("title", "")) for i in news or []]
        df = {}
        for tokens in titles:
            for token in tokens:
                df[token] = df.get(token, 0) + 1
        common = {t for t, n in df.items() if n > max(10, len(titles) // 50)}
        anchor = max(3, int(len(titles) * _CLUSTER_ANCHOR_FRAC))
    except wi_config.ConfigError:
        raise
    except Exception:  # noqa: BLE001
        return lambda a, b: False

    def same_story(a, b) -> bool:
        # An item is never its own pair: the caller compares each candidate
        # against the ones already kept, and identity would cap everything at 1.
        if a is b:
            return False
        try:
            tokens_a = _story_tokens((a or {}).get("title", "") or "")
            tokens_b = _story_tokens((b or {}).get("title", "") or "")
            shared = {t for t in (tokens_a & tokens_b) if t not in common}
            if not shared:
                return False
            rarest = min(df.get(t, 0) for t in shared)
            return _is_same_story(tokens_a, tokens_b, shared, rarest, anchor,
                                  min_ratio)
        except wi_config.ConfigError:
            raise
        except Exception:  # noqa: BLE001
            return False

    return same_story


def _story_outlet_counts(news: list) -> list:
    """Per item, how many distinct outlets ran a headline about the same story.

    Returns a list parallel to `news`. Never raises.
    """
    titles = [_story_tokens((i or {}).get("title", "")) for i in news or []]
    index = {}
    for n, tokens in enumerate(titles):
        for token in tokens:
            index.setdefault(token, []).append(n)
    # A token carried by a large slice of the pool ("trump", "reuters") is not
    # a story signature; it is this pool's boilerplate.
    cap = max(10, len(titles) // 50)
    common = {t for t, rows in index.items() if len(rows) > cap}
    anchor = max(3, int(len(titles) * _CLUSTER_ANCHOR_FRAC))

    counts = []
    for n, tokens in enumerate(titles):
        outlets = {((news[n] or {}).get("source") or "")}
        if len(tokens) >= _CLUSTER_MIN_TOKENS:
            shared = {}
            for token in tokens:
                if token in common:
                    continue
                for m in index[token]:
                    if m != n:
                        shared.setdefault(m, set()).add(token)
            for m, tokens_shared in shared.items():
                if not _is_same_story(tokens, titles[m], tokens_shared,
                                      min(len(index[t]) for t in tokens_shared),
                                      anchor):
                    continue
                outlets.add((news[m] or {}).get("source") or "")
            counts.append(len(outlets))
        else:
            counts.append(1)
    return counts


def _build_coverage(news: list, gdelt: dict) -> list:
    """One row per headline: how many distinct outlets carried that story.

    A row per headline rather than per cluster because select_news looks the
    count up by the item's own title — a representative title would leave every
    other member of the story uncorroborated, which is the bug this replaced.

    `tone` is attached from a GDELT theme when a keyword overlaps, else None.
    """
    themes = (gdelt or {}).get("themes") or []
    counts = _story_outlet_counts(news)

    coverage, seen = [], set()
    for item, count in zip(news or [], counts):
        title = (item or {}).get("title", "") or ""
        key = _story_tokens(title)
        if not key or key in seen:
            continue
        seen.add(key)
        tone = None
        for theme in themes:
            theme_words = set(re.findall(r"[A-Za-z]{4,}", theme.get("theme", "").lower()))
            if key & theme_words:
                tone = theme.get("tone")
                break
        coverage.append({"story": title, "count": count, "tone": tone})
    coverage.sort(key=lambda c: c["count"], reverse=True)
    return coverage


# --------------------------------------------------------------------------- #
# Per-source fetchers (each records into `health` and returns its slice)
# --------------------------------------------------------------------------- #
def _yf_history(ticker: str):
    """Fetch recent history via yfinance. Isolated in its own function so tests can
    stub it offline and so an import failure is easy to detect."""
    import yfinance as yf
    # A full year: the last two available closes drive the daily change (a wide
    # window means a missing/holiday bar can't corrupt it), and the same history
    # feeds the long-horizon stats (YTD / 90d / 52w high / 1y percentile).
    return yf.Ticker(ticker).history(period="1y")


def fetch_markets(config: dict, session, raw_dir: str, health: dict) -> dict:
    """yfinance primary, Twelve Data fallback for any gaps.

    Twelve Data's free tier cannot serve index symbols and silently mis-resolves
    some of them to unrelated ETFs (e.g. "DAX" -> a US-listed Global X ETF at ~$44),
    so it is intentionally demoted to a gap-filler behind yfinance, which has
    correct, free index symbols (^GSPC, ^N225, ^KS11, ^GDAXI, ...).
    """
    markets = _empty_markets()
    pending = []  # (group, key, td_sym) still needing a value after yfinance

    # --- yfinance primary ---
    yf_available = True
    yf_filled = False
    freshened = 0
    for group, items in market_spec().items():
        for key, td_sym, yf_t in items:
            quote = None
            if yf_available:
                try:
                    hist = _yf_history(yf_t)
                    quote = _yf_quote_from_history(hist)
                    # Non-US indices: the consolidated daily bar lags the close
                    # by hours, so at 07:00 JST yesterday's session is often
                    # missing. The intraday endpoint has it immediately; append
                    # its session close when it is strictly newer.
                    if group == "indices" and quote:
                        try:
                            extra = _yf_intraday_last_session(yf_t)
                        except wi_config.ConfigError:
                            raise
                        except Exception:
                            extra = None
                        if extra and extra[0].isoformat() > quote["asof"]:
                            quote = _yf_quote_from_history(hist, extra_bar=extra)
                            freshened += 1
                except ModuleNotFoundError:
                    yf_available = False
                except wi_config.ConfigError:
                    raise
                except Exception:
                    quote = None
            if quote:
                markets[group][key] = quote
                yf_filled = True
            else:
                pending.append((group, key, td_sym))
    yf_note = f"{freshened} intraday-freshened" if freshened else ""
    health["yfinance"] = (
        {"ok": yf_filled, "note": yf_note if yf_filled else "no quotes"}
        if yf_available else {"ok": False, "note": "yfinance not installed"}
    )

    # --- Twelve Data fallback for any gaps yfinance left ---
    td_key = config.get("twelvedata_key")
    raw = {}
    if not td_key:
        health["twelvedata"] = {"ok": False, "note": "no api key"}
    elif not pending:
        health["twelvedata"] = {"ok": True, "note": "not needed"}
    else:
        td_filled = False
        for group, key, td_sym in pending:
            try:
                resp = session.get(
                    TWELVEDATA_URL,
                    params={"symbol": td_sym, "apikey": td_key},
                    timeout=_HTTP_TIMEOUT,
                )
                resp.raise_for_status()
                data = resp.json()
                raw[key] = data
                quote = _normalize_td_quote(data)
            except wi_config.ConfigError:
                raise
            except Exception:
                quote = None
            if quote:
                markets[group][key] = quote
                td_filled = True
        health["twelvedata"] = {
            "ok": td_filled,
            "note": "" if td_filled else "no quotes (fallback)",
        }

    _dump(raw_dir, "twelvedata", raw)
    return markets


# --- JGB: Japan MOF official daily yield-curve CSVs (no key) ---------------- #
# The English current-month file is updated daily; the full archive lags by up
# to a month but supplies the ~1y window the horizon stats need. Merged by date.
_JGB_CURRENT_URL = "https://www.mof.go.jp/english/jgbs/reference/interest_rate/jgbcme.csv"
_JGB_ALL_URL = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"

_JGB_DATE_SLASH = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})$")
_JGB_DATE_ERA = re.compile(r"^([SHR])(\d+)\.(\d+)\.(\d+)$")
# Japanese era base years: era year 1 = base + 1 (Showa 49 = 1974, Reiwa 8 = 2026).
_JGB_ERA_BASE = {"S": 1925, "H": 1988, "R": 2018}


# Fixed MOF column layout: Date,1Y..9Y,10Y,15Y,20Y,25Y,30Y,40Y.
_JGB_TENOR_COLS = {"jp10y": 10, "jp30y": 14}


def _parse_jgb(text: str) -> list:
    """[(iso_date, {"jp10y": pct, "jp30y": pct}), ...] from a MOF curve CSV.

    Handles both date styles (English `2026/7/1`, archive era `R8.6.30`).
    Non-date lines (titles, footers) are skipped; '-' values (tenor not yet
    issued) are omitted, and a row with no tracked tenor at all is dropped."""
    rows = []
    for line in text.splitlines():
        fields = [f.strip() for f in line.split(",")]
        if len(fields) <= max(_JGB_TENOR_COLS.values()):
            continue
        m = _JGB_DATE_SLASH.match(fields[0])
        if m:
            y, mo, d = (int(g) for g in m.groups())
        else:
            m = _JGB_DATE_ERA.match(fields[0])
            if not m:
                continue
            era, yy, mo, d = m.groups()
            y, mo, d = _JGB_ERA_BASE[era] + int(yy), int(mo), int(d)
        try:
            iso = date(y, mo, d).isoformat()
        except ValueError:
            continue
        tenors = {}
        for key, col in _JGB_TENOR_COLS.items():
            try:
                tenors[key] = float(fields[col])
            except ValueError:
                continue
        if tenors:
            rows.append((iso, tenors))
    return rows


def fetch_jgb(config: dict, session, raw_dir: str, health: dict) -> dict:
    """No-key. 10Y + 30Y JGB yield quotes (rates shape) from the MOF daily curve.

    Freshness matches the pipeline's overnight pattern: at 07:00 JST the latest
    row is the prior business day's close. Returns {"jp10y": quote, "jp30y":
    quote} (a tenor may be missing) or {} on failure; never raises."""
    try:
        rows = {}
        for url, enc in ((_JGB_ALL_URL, "shift_jis"),
                         (_JGB_CURRENT_URL, "utf-8")):
            resp = session.get(url, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
            for iso, tenors in _parse_jgb(resp.content.decode(enc, errors="replace")):
                rows.setdefault(iso, {}).update(tenors)
        if not rows:
            raise ValueError("no parseable rows")
        dates = sorted(rows)
        _dump(raw_dir, "jgb", {"tail": [[d, rows[d]] for d in dates[-15:]]})
        cutoff = (date.fromisoformat(dates[-1]) - timedelta(days=370)).isoformat()

        quotes = {}
        for key in _JGB_TENOR_COLS:
            window = [(date.fromisoformat(d), rows[d][key])
                      for d in dates if d >= cutoff and key in rows[d]]
            if not window:
                continue
            asof_d, level = window[-1]
            quote = {"level": round(level, 4), "change_pct": None,
                     "change_abs": None, "asof": asof_d.isoformat(),
                     "asof_prev": None}
            if len(window) >= 2:
                prev_d, prev = window[-2]
                quote["change_abs"] = round(level - prev, 4)
                if prev:
                    quote["change_pct"] = round((level / prev - 1.0) * 100.0, 3)
                quote["asof_prev"] = prev_d.isoformat()
            quote.update(_horizon_fields(window))
            quotes[key] = quote
        health["jgb"] = {"ok": bool(quotes),
                         "note": f"{len(quotes)} tenor(s)" if quotes else "no data"}
        return quotes
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["jgb"] = {"ok": False, "note": _short(exc)}
        return {}


def fetch_fred(config: dict, session, raw_dir: str, health: dict) -> dict:
    key = config.get("fred_key")
    if not key:
        health["fred"] = {"ok": False, "note": "no api key"}
        return {}
    out = {}
    raw = {}
    ok = False
    for series_id, label in fred_series():
        try:
            resp = session.get(
                FRED_URL,
                params={
                    "series_id": series_id,
                    "api_key": key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 1,
                },
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            raw[series_id] = data
            obs = _normalize_fred(data, label)
        except wi_config.ConfigError:
            raise
        except Exception:
            obs = None
        if obs:
            out[series_id] = obs
            ok = True
    health["fred"] = {"ok": ok, "note": "" if ok else "no data"}
    _dump(raw_dir, "fred", raw)
    return out


def fetch_gdelt(config: dict, session, raw_dir: str, health: dict) -> dict:
    """No-key. Per theme: one timelinetone call (tone) + one artlist call (volume)."""
    themes_out = []
    raw = {}
    any_ok = False
    for label, query in gdelt_themes():
        entry = {"theme": label, "volume": 0, "tone": None}
        try:
            resp = session.get(
                GDELT_URL,
                params={
                    "query": query,
                    "mode": "timelinetone",
                    "format": "json",
                    "timespan": "1d",
                },
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            raw[f"{label}::tone"] = data
            entry["tone"] = _normalize_gdelt_tone(data)
            any_ok = True
        except wi_config.ConfigError:
            raise
        except Exception:
            pass
        try:
            resp = session.get(
                GDELT_URL,
                params={
                    "query": query,
                    "mode": "artlist",
                    "format": "json",
                    "maxrecords": 75,
                    "timespan": "1d",
                },
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            raw[f"{label}::artlist"] = data
            entry["volume"] = _normalize_gdelt_volume(data)
            any_ok = True
        except wi_config.ConfigError:
            raise
        except Exception:
            pass
        themes_out.append(entry)
    health["gdelt"] = {"ok": any_ok, "note": "" if any_ok else "no data"}
    _dump(raw_dir, "gdelt", raw)
    return {"themes": themes_out}


def fetch_gov(config: dict, session, raw_dir: str, health: dict) -> list:
    """No-key. Federal Register newest documents."""
    try:
        resp = session.get(
            FEDREG_URL,
            params={"per_page": 20, "order": "newest"},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["federal_register"] = {"ok": False, "note": _short(exc)}
        return []
    _dump(raw_dir, "federal_register", data)
    items = _normalize_gov(data)
    health["federal_register"] = {
        "ok": bool(items),
        "note": "" if items else "no results",
    }
    return items


def fetch_edgar(config: dict, session, raw_dir: str, health: dict) -> list:
    """No-key. SEC EDGAR full-text search for recent notable filings (S-1/424B4/8-K).

    SEC returns HTTP 403 without a descriptive User-Agent; the shared session sets a
    generic UA, so we override it per-request via ``headers=`` with a contactable one.
    """
    try:
        resp = session.get(
            EDGAR_URL,
            params={"q": "", "forms": ",".join(edgar_forms())},
            headers={"User-Agent": _sec_ua()},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["edgar"] = {"ok": False, "note": _short(exc)}
        return []
    _dump(raw_dir, "edgar", data)
    items = _normalize_edgar(data)
    health["edgar"] = {"ok": bool(items), "note": "" if items else "no results"}
    return items


def _yf_earnings_date(ticker: str):
    """Next earnings date via yfinance as a 'YYYY-MM-DD' string (or None). Isolated in
    its own function — mirroring `_yf_history` — so tests can stub it offline and an
    import failure (ModuleNotFoundError) is easy to detect."""
    import yfinance as yf
    cal = yf.Ticker(ticker).calendar
    dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
    if not dates:
        return None
    first = dates[0]
    try:
        return first.strftime("%Y-%m-%d")
    except AttributeError:
        return str(first)[:10]


def fetch_earnings(config: dict, session, raw_dir: str, health: dict,
                   today: str | None = None) -> list:
    """No-key. Upcoming earnings dates (yfinance) for earnings_watchlist() bellwethers,
    keeping only dates within the forward window [today, today+earnings_window_days()].

    Design: `today` (a YYYY-MM-DD string threaded from fetch_all) is the window
    reference and is an explicit optional arg so unit tests can pin a fixed date;
    when absent we fall back to the real current date. yfinance is imported lazily
    inside `_yf_earnings_date`, so its absence degrades to ok:False without raising.
    """
    try:
        ref = datetime.strptime(today, "%Y-%m-%d").date() if today else date.today()
    except wi_config.ConfigError:
        raise
    except Exception:
        ref = date.today()
    horizon = ref + timedelta(days=earnings_window_days())

    rows = []
    raw = {}
    yf_available = True
    for ticker, name in earnings_watchlist():
        try:
            iso = _yf_earnings_date(ticker)
        except ModuleNotFoundError:
            yf_available = False
            break
        except wi_config.ConfigError:
            raise
        except Exception:
            iso = None
        raw[ticker] = iso
        if not iso:
            continue
        try:
            when = datetime.strptime(iso, "%Y-%m-%d").date()
        except wi_config.ConfigError:
            raise
        except Exception:
            continue
        if ref <= when <= horizon:
            rows.append({"ticker": ticker, "name": name, "date": iso})

    if not yf_available:
        health["earnings"] = {"ok": False, "note": "yfinance not installed"}
        return []
    rows.sort(key=lambda r: r["date"])
    _dump(raw_dir, "earnings", raw)
    health["earnings"] = {
        "ok": bool(rows),
        "note": "" if rows else "no upcoming earnings",
    }
    return rows


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def fetch_all(config: dict, raw_dir: str, today: str) -> dict:
    """Fetch every source resiliently and return a normalized BUNDLE.

    Never raises. Each source is wrapped twice: once inside its own fetcher and
    again here, so even a programming error in a fetcher degrades to an
    ``ok == False`` entry rather than aborting the run.
    """
    cfg = wi_config.get()
    config = config or {}
    try:
        os.makedirs(raw_dir, exist_ok=True)
    except wi_config.ConfigError:
        raise
    except Exception:
        pass

    session = requests.Session()
    session.headers.update({"User-Agent": _plain_ua()})

    health: dict = {}
    bundle = {
        "date": today,
        "news": [],
        "coverage": [],
        "markets": _empty_markets(),
        "macro": {},
        "gov": [],
        "gdelt": {"themes": []},
        "filings": [],
        "earnings_calendar": [],
        "source_health": health,
    }

    try:
        bundle["news"] = feeds.fetch_feeds(config, raw_dir, health, session=session)
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["feeds"] = {"ok": False, "note": _short(exc)}
        bundle["news"] = []

    # Feeds carry a registry tier; only score items that arrived without one.
    for item in bundle["news"]:
        if not item.get("tier"):
            item["tier"] = _source_tier(item.get("source", ""),
                                        item.get("url", ""))

    try:
        bundle["markets"] = fetch_markets(config, session, raw_dir, health)
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["twelvedata"] = {"ok": False, "note": _short(exc)}

    try:
        for key, quote in fetch_jgb(config, session, raw_dir, health).items():
            bundle["markets"].setdefault("rates", {})[key] = quote
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["jgb"] = {"ok": False, "note": _short(exc)}

    try:
        bundle["macro"] = fetch_fred(config, session, raw_dir, health)
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["fred"] = {"ok": False, "note": _short(exc)}

    try:
        bundle["gdelt"] = fetch_gdelt(config, session, raw_dir, health)
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["gdelt"] = {"ok": False, "note": _short(exc)}

    try:
        bundle["gov"] = fetch_gov(config, session, raw_dir, health)
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["federal_register"] = {"ok": False, "note": _short(exc)}

    try:
        if cfg.filings_enabled:
            bundle["filings"] = fetch_edgar(config, session, raw_dir, health)
        else:
            health["edgar"] = {"ok": True, "note": "disabled by configuration"}
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["edgar"] = {"ok": False, "note": _short(exc)}

    try:
        # fetch_earnings takes the standard 4 args plus the window reference date
        # threaded from `today` (kept optional so unit tests can pin a fixed date).
        if cfg.earnings_enabled:
            bundle["earnings_calendar"] = fetch_earnings(
                config, session, raw_dir, health, today
            )
        else:
            health["earnings"] = {"ok": True, "note": "disabled by configuration"}
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health["earnings"] = {"ok": False, "note": _short(exc)}

    try:
        bundle["coverage"] = _build_coverage(bundle["news"], bundle["gdelt"])
    except wi_config.ConfigError:
        raise
    except Exception:
        bundle["coverage"] = []

    return bundle
