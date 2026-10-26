"""world-intel Module D' — render the daily briefing as a self-contained HTML document.

Pure function: `render_html(analysis, bundle, date) -> str`. FACTS (markets table,
filings, earnings, diagnostics) come from the fetched `bundle` (deterministic); PROSE
(brief stories, finance brief, signal, threads, on_radar) comes from the Codex
`analysis`. No I/O, no network, import-safe.

Unlike the retired `compose.py` (a cp1252-constrained Telegram text composer), this
output is an HTML document served/delivered as UTF-8, so unicode arrows (▲ ▼) and
richer typography are fine. The ONLY external URLs emitted are the article / source /
filing links carried in the data — styling is inline, no external CSS/JS/fonts/images.
Every injected string is HTML-escaped.
"""

from __future__ import annotations

import datetime
import html

# Reused verbatim from compose.py: the deterministic scoreboard group/label mapping.
# (group_key, group_header, [(bundle_symbol, display_label), ...])
_SCOREBOARD = [
    ("indices", "Equities (Indices)", [
        ("sp500", "S&P 500"), ("nasdaq", "Nasdaq"), ("nikkei", "Nikkei 225"),
        ("kospi", "KOSPI"), ("hangseng", "Hang Seng"), ("stoxx600", "STOXX 600"),
        ("dax", "DAX"),
    ]),
    ("fx", "FX", [("usdjpy", "USD/JPY"), ("cadjpy", "CAD/JPY"),
                  ("usdcad", "USD/CAD"), ("eurusd", "EUR/USD")]),
    ("rates", "Rates", [("us5y", "US 5Y"), ("us10y", "US 10Y"),
                        ("us30y", "US 30Y"),
                        ("jp10y", "JP 10Y"), ("jp30y", "JP 30Y")]),
    ("commodities", "Commodities", [("brent", "Brent"), ("wti", "WTI"), ("gold", "Gold")]),
    ("risk", "Risk", [("vix", "VIX")]),
]


# --------------------------------------------------------------------------- #
# Small safe coercion / escaping helpers
# --------------------------------------------------------------------------- #
def _clean(s) -> str:
    """Mirror compose.py: strip strings, coerce everything else to ''."""
    return s.strip() if isinstance(s, str) else ""


def _esc(s) -> str:
    """HTML-escape any value's cleaned string form (text context)."""
    return html.escape(_clean(s))


def _esc_attr(s) -> str:
    """HTML-escape for an attribute value (quotes escaped too)."""
    return html.escape(_clean(s), quote=True)


def _as_list(x) -> list:
    return x if isinstance(x, list) else []


def _as_dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _sign(pct) -> str:
    if pct is None:
        return ""
    if pct > 0:
        return "+"
    if pct < 0:
        return "-"
    return "~"


def _num(value):
    """Coerce to float or None (never raises)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (ValueError, AttributeError):
            return None
    return None


# --------------------------------------------------------------------------- #
# Date / freshness helpers
#
# Market quotes carry an `asof` (date of the close shown) and, from the yfinance
# path, an `asof_prev` (date of the prior close the Δ% is measured against). We use
# those two dates — plus the brief date — to (a) label every row with its reporting
# date, (b) flag a row whose data is genuinely old, and (c) annotate a Δ% that spans
# a skipped session. Everything is weekend-aware but needs no holiday calendar.
# --------------------------------------------------------------------------- #
_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# A row whose close is at least this many business days behind the brief date is
# "stale": normal overnight spread (US shows yesterday at 07:00 Tokyo) is 1 business
# day and stays fresh; a quote stuck a whole extra session behind trips the flag.
_STALE_BIZ_DAYS = 2


def _parse_date(s):
    """Best-effort ISO date parse. Accepts 'YYYY-MM-DD' or a longer timestamp whose
    first 10 chars are a date. Returns a datetime.date or None (never raises)."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if len(s) < 10:
        return None
    try:
        return datetime.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _business_days_old(from_iso, to_iso):
    """Count the weekdays in the interval (from, to] — i.e. how many business days
    `to` falls after `from`. Weekend-aware, no holiday calendar. Returns None if
    either date is unparseable or `to` precedes `from`."""
    a = _parse_date(from_iso)
    b = _parse_date(to_iso)
    if a is None or b is None or b < a:
        return None
    n = 0
    d = a
    step = datetime.timedelta(days=1)
    while d < b:
        d += step
        if d.weekday() < 5:  # Mon–Fri
            n += 1
    return n


def _fmt_date(s) -> str:
    """'2026-07-01' -> 'Jul 1'. Empty -> em-dash; a non-empty non-date string is
    passed through escaped so nothing is silently dropped."""
    d = _parse_date(s)
    if d is None:
        return _esc(s) if _clean(s) else "—"
    return f"{_MONTHS[d.month]} {d.day}"


def _is_stale(asof, today) -> bool:
    """True if `asof` is >= _STALE_BIZ_DAYS business days before the brief date."""
    n = _business_days_old(asof, today)
    return n is not None and n >= _STALE_BIZ_DAYS


def _fmt_level(lvl) -> str:
    if lvl is None:
        return "—"
    try:
        return f"{lvl:g}"
    except (ValueError, TypeError):
        return _esc(lvl)


def _link(text, url, *, new_tab: bool = True) -> str:
    """A safe anchor. If url is empty/missing, return the escaped text only —
    NEVER emit an href to a missing url."""
    label = _esc(text)
    href = _clean(url)
    if not href:
        return label
    attrs = ' target="_blank" rel="noopener noreferrer"' if new_tab else ""
    return f'<a href="{_esc_attr(href)}"{attrs}>{label}</a>'


# --------------------------------------------------------------------------- #
# Markets: biggest-mover detection + scoreboard table
# --------------------------------------------------------------------------- #
def _rates_delta_cell(change_abs) -> str:
    """Render a rates (yield) delta in basis points from the absolute change.

    bp = round(change_abs * 100) — the yield is quoted in percent units, so a
    4.372 -> 4.485 move (change_abs = 0.113) is ~11 bp. Arrow by sign; NEUTRAL
    styling (flat class) so a rising yield never implies pos/neg sentiment. If
    change_abs is None, render an em-dash."""
    if change_abs is None:
        return '<td class="delta flat">—</td>'
    bp = round(change_abs * 100)
    if bp > 0:
        arrow, sign = "▲", "+"
    elif bp < 0:
        arrow, sign = "▼", "-"
    else:
        arrow, sign = "·", ""
    return f'<td class="delta flat">{arrow} {sign}{abs(bp)} bp</td>'


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _ctx_line(group_key: str, q: dict) -> str:
    """Long-horizon context for a scoreboard row (faint sub-line under the
    instrument name). Indices/commodities: % horizons + distance below the 52w
    high. FX: the same % horizons, but ranked within the year rather than
    measured off the high — see below. Rates: bp horizons (percent-of-a-yield is
    misleading). Risk (VIX): 1-year percentile — it's mean-reverting, so rank
    beats trend stats. Missing fields are simply omitted."""
    parts = []
    if group_key in ("indices", "commodities", "fx"):
        ytd, d90 = _num(q.get("ytd_pct")), _num(q.get("d90_pct"))
        if ytd is not None:
            parts.append(f"YTD {ytd:+.1f}%")
        if d90 is not None:
            parts.append(f"90d {d90:+.1f}%")
        if group_key == "fx":
            # A pair has no drawdown. Measured on 2026-08-16 the four pairs sat
            # 2.0-3.7% off their highs — one narrow band, against 25.2% for Brent
            # and 23.4% for the KOSPI — so off-high on FX reads as "nothing is
            # happening" whatever is happening, and it inverts: USD/JPY (2.8%
            # off) and EUR/USD (3.7% off) looked like neighbours while sitting in
            # the top quarter and the bottom third of their own years. The
            # percentile says which: 75th against 29th.
            p = _num(q.get("pctl_1y"))
            if p is not None:
                parts.append(f"{_ordinal(int(p))} pctile (1y)")
        else:
            off = _num(q.get("off_high_pct"))
            if off is not None:
                parts.append("at 52w high" if off <= 0.05
                             else f"{off:.1f}% off high")
    elif group_key == "rates":
        for label, key in (("YTD", "ytd_abs"), ("90d", "d90_abs")):
            v = _num(q.get(key))
            if v is not None:
                parts.append(f"{label} {round(v * 100):+d} bp")
    elif group_key == "risk":
        p = _num(q.get("pctl_1y"))
        if p is not None:
            parts.append(f"{_ordinal(int(p))} pctile (1y)")
    return " · ".join(parts)


def _biggest_mover(markets: dict, today: str = ""):
    """Return (group_key, symbol) of the single largest |Δ%| across all
    instruments, or None if nothing has a change_pct.

    Rates are excluded (they're shown in basis points, not %). Stale rows are
    excluded first so a carried-over weekend/holiday move can't be re-crowned for
    days; if every candidate is stale we fall back to the unfiltered winner so the
    table always highlights something."""
    markets = _as_dict(markets)

    def _pick(require_fresh: bool):
        best = None
        best_abs = -1.0
        for group_key, _hdr, fields in _SCOREBOARD:
            if group_key == "rates":
                continue
            cat = _as_dict(markets.get(group_key))
            for sym, _label in fields:
                q = _as_dict(cat.get(sym))
                change = _num(q.get("change_pct"))
                if change is None:
                    continue
                if require_fresh and _is_stale(q.get("asof"), today):
                    continue
                if abs(change) > best_abs:
                    best_abs = abs(change)
                    best = (group_key, sym)
        return best

    return _pick(True) or _pick(False)


def _markets_table(markets: dict, today: str = "") -> str:
    markets = _as_dict(markets)
    mover = _biggest_mover(markets, today)

    rows = []
    for group_key, header, fields in _SCOREBOARD:
        cat = _as_dict(markets.get(group_key))
        group_rows = []
        for sym, label in fields:
            q = _as_dict(cat.get(sym))
            lvl = _num(q.get("level"))
            change = _num(q.get("change_pct"))
            if lvl is None and change is None:
                continue  # no data for this instrument — skip the row

            if group_key == "rates":
                # Rates render as basis points off change_abs (yield is in
                # percent units, so 0.113 -> ~11 bp). Neutral styling: a rising
                # yield is not inherently good/bad.
                delta_cell = _rates_delta_cell(_num(q.get("change_abs")))
            elif change is None:
                # Δ% cell with arrow + colour class.
                delta_cell = '<td class="delta flat">—</td>'
            else:
                if change > 0:
                    cls, arrow = "pos", "▲"   # ▲
                elif change < 0:
                    cls, arrow = "neg", "▼"   # ▼
                else:
                    cls, arrow = "flat", "·"  # ·
                delta_cell = (
                    f'<td class="delta {cls}">{arrow} '
                    f'{_sign(change)}{abs(change):.2f}%</td>'
                )

            asof = q.get("asof")
            if _clean(asof):
                asof_cell = _fmt_date(asof)
                # If the Δ% is measured against a non-adjacent prior close (a
                # holiday or a skipped session), surface that basis so the move
                # isn't misread as a single-day change.
                prev = q.get("asof_prev")
                gap = _business_days_old(prev, asof) if _clean(prev) else None
                if gap is not None and gap >= 2:
                    asof_cell += f' <span class="basis">(vs {_fmt_date(prev)})</span>'
                if _is_stale(asof, today):
                    asof_cell += ' <span class="stale">stale</span>'
            else:
                asof_cell = "—"
            highlight = ' class="mover"' if mover == (group_key, sym) else ""
            ctx = _ctx_line(group_key, q)
            ctx_html = f'<span class="ctx">{_esc(ctx)}</span>' if ctx else ""
            group_rows.append(
                f"<tr{highlight}>"
                f'<td class="instrument">{_esc(label)}{ctx_html}</td>'
                f'<td class="level">{_fmt_level(lvl)}</td>'
                f"{delta_cell}"
                f'<td class="asof">{asof_cell}</td>'
                f"</tr>"
            )
        if group_rows:
            rows.append(
                f'<tr class="group"><td colspan="4">{_esc(header)}</td></tr>'
            )
            rows.extend(group_rows)

    if not rows:
        return '<p class="muted">No market data available.</p>'

    return (
        '<table class="markets">'
        "<thead><tr>"
        '<th>Instrument</th><th class="level">Last</th><th class="delta">&Delta;%</th>'
        '<th class="asof">Date</th>'
        "</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _filings_calendar(finance_prose: dict, bundle: dict) -> str:
    finance_prose = _as_dict(finance_prose)
    parts = ['<h3 class="sub">Filings &amp; Calendar</h3>']

    filings_note = _clean(finance_prose.get("filings_note"))
    if filings_note:
        parts.append(f'<p class="note">{_esc(filings_note)}</p>')

    filings = _as_list(bundle.get("filings"))
    filing_items = []
    for f in filings:
        f = _as_dict(f)
        company = _clean(f.get("company"))
        form = _clean(f.get("form"))
        filed = _clean(f.get("filed"))
        url = _clean(f.get("url"))
        if not (company or form or filed):
            continue
        head = _link(company or form or "filing", url)
        bits = [head]
        if form and company:
            bits.append(_esc(form))
        if filed:
            bits.append(_esc(filed))
        filing_items.append(f"<li>{' &middot; '.join(bits)}</li>")
    if filing_items:
        visible = 5
        if len(filing_items) > visible:
            extra = len(filing_items) - visible
            parts.append(f"<ul class='rows'>{''.join(filing_items[:visible])}</ul>")
            parts.append(
                # The phosphor CSS prefixes the summary with "+ " via ::before.
                f"<details class='more'><summary>{extra} more filings</summary>"
                f"<ul class='rows'>{''.join(filing_items[visible:])}</ul></details>"
            )
        else:
            parts.append(f"<ul class='rows'>{''.join(filing_items)}</ul>")
    else:
        parts.append('<p class="muted">No notable filings.</p>')

    earnings_note = _clean(finance_prose.get("earnings_note"))
    if earnings_note:
        parts.append(f'<p class="note">{_esc(earnings_note)}</p>')

    earnings = _as_list(bundle.get("earnings_calendar"))
    earn_items = []
    for e in earnings:
        e = _as_dict(e)
        ticker = _clean(e.get("ticker"))
        name = _clean(e.get("name"))
        edate = _clean(e.get("date"))
        if not (ticker or name or edate):
            continue
        head = _esc(ticker or name)
        if ticker and name:
            head += f" ({_esc(name)})"
        tail = f" &middot; {_esc(edate)}" if edate else ""
        earn_items.append(f"<li>{head}{tail}</li>")
    if earn_items:
        parts.append(f"<ul class='rows'>{''.join(earn_items)}</ul>")
    else:
        parts.append('<p class="muted">No upcoming earnings in window.</p>')

    return "".join(parts)


# --------------------------------------------------------------------------- #
# Post-Earnings: Codex write-ups joined (by ticker) to the deterministic
# candidate numbers in the bundle. Chips are built FROM THE CANDIDATE, never
# from Codex prose.
# --------------------------------------------------------------------------- #
def _fmt_compact(n, decimals=None) -> str:
    """Compact large-number format: 31900000000.0 -> '31.9B', 850000000 -> '850M'.
    With `decimals`, use that many fixed decimal places instead of 4 significant
    digits, dropping a trailing '.0' (3.063e9, decimals=1 -> '3.1B'; 8.5e8 -> '850M')."""
    n = _num(n)
    if n is None:
        return "—"
    a = abs(n)
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            if decimals is None:
                return f"{n / div:.4g}{suffix}"
            s = f"{n / div:.{decimals}f}".rstrip("0").rstrip(".")
            return f"{s}{suffix}"
    return f"{n:g}"


def _pe_chips(cand: dict) -> list:
    """Numbers-row chips for one candidate. Any chip whose value(s) are None is
    skipped; an empty list means the card renders with no chips row."""
    cand = _as_dict(cand)
    chips = []

    eps_est, eps_act = _num(cand.get("eps_est")), _num(cand.get("eps_act"))
    if eps_est is not None and eps_act is not None:
        chips.append(
            f'<span class="pechip"><span class="lbl">EPS</span> '
            f'est {eps_est:.2f} &rarr; act {eps_act:.2f}</span>'
        )

    rev_est, rev_act = _num(cand.get("rev_est")), _num(cand.get("rev_act"))
    if rev_est is not None and rev_act is not None:
        chips.append(
            f'<span class="pechip"><span class="lbl">Rev</span> '
            f'est {_fmt_compact(rev_est)} &rarr; act {_fmt_compact(rev_act)}</span>'
        )

    surprise = _num(cand.get("surprise_pct"))
    if surprise is not None:
        cls = "pos" if surprise > 0 else ("neg" if surprise < 0 else "flat")
        chips.append(
            f'<span class="pechip {cls}"><span class="lbl">Surprise</span> '
            f'{_sign(surprise)}{abs(surprise):.1f}%</span>'
        )

    move = _num(cand.get("price_move_pct"))
    if move is not None:
        if move > 0:
            cls, arrow = "pos", "▲"   # ▲
        elif move < 0:
            cls, arrow = "neg", "▼"   # ▼
        else:
            cls, arrow = "flat", "·"  # ·
        chips.append(
            f'<span class="pechip {cls}">{arrow} {_sign(move)}{abs(move):.1f}%</span>'
        )

    # The one SEC-fundamentals chip. Chips are the authoritative row, so only an
    # unambiguous ratio is admitted: both the value and its YoY delta must have
    # resolved, and a value outside (-100, 100) is an extraction artefact rather
    # than a margin. FCF and segment numbers never earn a chip (companion §8.2).
    margin = _as_dict(_pe_fundamentals(cand).get("op_margin_pct"))
    value, delta = _num(margin.get("value")), _num(margin.get("yoy_delta_pp"))
    if value is not None and delta is not None and -100 < value < 100:
        if delta > 0:
            cls, arrow = "pos", "▲"
        elif delta < 0:
            cls, arrow = "neg", "▼"
        else:
            cls, arrow = "flat", "·"
        chips.append(
            f'<span class="pechip {cls}"><span class="lbl">Op margin</span> '
            f'{value:.1f}% {arrow} {_sign(delta)}{abs(delta):.1f}pp</span>'
        )

    return chips


# --------------------------------------------------------------------------- #
# SEC fundamentals (Spec C D6): one chip above, plus two muted lines below the
# context line. Every figure is read off the candidate — the lane computed it in
# Python — and the "SEC cash-flow basis" qualifier travels with capex and FCF
# because that label is the claim we can defend.
# --------------------------------------------------------------------------- #
_SEC_CF_BASIS = " (SEC cash-flow basis)"


def _pe_fundamentals(cand) -> dict:
    """The candidate's `fundamentals` dict, or {} unless it resolved. A `stale`
    or `unavailable` verdict renders as silence: the reader does not care about
    our pipeline's internals (companion §8.2)."""
    fund = _as_dict(_as_dict(cand).get("fundamentals"))
    return fund if fund.get("status") == "ok" else {}


def _pe_fund_value(fund: dict, key: str):
    return _num(_as_dict(fund.get(key)).get("value"))


def _pe_fundamentals_line(cand) -> str:
    """Muted SEC line under the context line, middle-dot separated with missing
    segments skipped, exactly like `_pe_context_line`. '' when no numeric
    segment resolves — the form alone ("SEC 10-Q") is noise, not information."""
    fund = _pe_fundamentals(cand)
    if not fund:
        return ""

    segs = []
    revenue = _as_dict(fund.get("revenue_q"))
    value = _num(revenue.get("value"))
    if value is not None:
        seg = f"Rev {_fmt_compact(value)}"
        yoy = _num(revenue.get("yoy_pct"))
        if yoy is not None:
            seg += f" ({_sign(yoy)}{abs(yoy):.1f}% y/y)"
        segs.append(seg)

    for key, label, qualifier in (
        ("ocf_ttm", "OCF TTM", ""),
        ("capex_ttm", "Capex TTM", _SEC_CF_BASIS),
        ("fcf_ttm", "FCF TTM", _SEC_CF_BASIS),
    ):
        value = _pe_fund_value(fund, key)
        if value is not None:
            segs.append(f"{label} {_fmt_compact(value)}{qualifier}")

    if not segs:
        return ""
    form = _clean(fund.get("form"))
    if form:
        segs.insert(0, f"SEC {_esc(form)}")
    return " &middot; ".join(segs)


def _pe_referee_line(cand) -> str:
    """The referee segment: both figures, side by side, no verdict. Which of the
    two is right is not ours to declare (companion §6.2)."""
    for flag in _as_list(_as_dict(cand).get("fundamentals_flags")):
        flag = _as_dict(flag)
        if flag.get("kind") != "eps_disagreement":
            continue
        sec, vendor = _num(flag.get("sec")), _num(flag.get("vendor"))
        if sec is None or vendor is None:
            continue
        form = _clean(flag.get("form")) or "filing"
        return (f"SEC {_esc(form)} diluted EPS ${sec:.2f} "
                f"vs vendor actual ${vendor:.2f}")
    return ""


# Metric enum -> display label. "other" reuses "Guide" (no more specific term).
_GUIDANCE_METRIC_LABELS = {
    "eps_fy": "FY EPS", "eps_q": "Q EPS",
    "revenue_fy": "FY Rev", "revenue_q": "Q Rev", "other": "Guide",
}
# Metric enum -> the candidate's forward-consensus key. Revenue/other metrics
# have no consensus lookup (candidate carries no revenue-guidance consensus).
_GUIDANCE_CONSENSUS_KEYS = {"eps_fy": "fwd_eps_fy", "eps_q": "fwd_eps_q"}


def _fmt_guidance_num(metric: str, v: float) -> str:
    """EPS metrics format like the existing EPS chips (2dp); everything else
    (revenue/other) via the shared compact-number formatter."""
    if metric in ("eps_fy", "eps_q"):
        return f"{v:.2f}"
    return _fmt_compact(v)


def _guidance_parts(source, cand):
    """`(css_class, body)` for a guidance restatement, or None when there is
    nothing quantitative to render: `guidance_struct` null/absent/not-a-dict,
    or both low and high null.

    `source` is whatever carries `guidance_struct` — a beat-1 write-up, or (at
    beat 2) the follow-up itself, whose `filing_watch.snapshot` carried the
    struct forward. `cand` is whatever carries the forward consensus. The
    verdict is DETERMINISTIC midpoint-vs-consensus math, never Codex's call,
    and it is simply absent when no consensus resolves."""
    source = _as_dict(source)
    gs = source.get("guidance_struct")
    if not isinstance(gs, dict):
        return None

    metric = _clean(gs.get("metric"))
    low, high = _num(gs.get("low")), _num(gs.get("high"))
    if low is None and high is None:
        return None
    if low is None:
        low = high
    elif high is None:
        high = low
    midpoint = (low + high) / 2

    label = _GUIDANCE_METRIC_LABELS.get(metric, "Guide")
    value_txt = (
        _fmt_guidance_num(metric, low) if low == high
        else f"{_fmt_guidance_num(metric, low)}-{_fmt_guidance_num(metric, high)}"
    )
    body = f"{label} {value_txt}"

    cons_key = _GUIDANCE_CONSENSUS_KEYS.get(metric)
    cons = _num(_as_dict(cand).get(cons_key)) if cons_key else None
    cls = "flat"
    if cons is not None and cons > 0 and midpoint > 0:
        ratio = midpoint / cons
        if abs(ratio - 1) <= 0.02 + 1e-9:  # epsilon guards the exact-boundary case
            cls, marker = "flat", "·"
        elif ratio > 1:
            cls, marker = "pos", "▲"
        else:
            cls, marker = "neg", "▼"
        body += f" vs cons {_fmt_guidance_num(metric, cons)} {marker}"

    return cls, body


def _guidance_chip(writeup, cand) -> str:
    """The beat-1 guidance chip. '' when `_guidance_parts` has nothing to say,
    so old briefings render unchanged."""
    parts = _guidance_parts(writeup, cand)
    if parts is None:
        return ""
    cls, body = parts
    return f'<span class="pechip {cls}"><span class="lbl">Guide</span> {body}</span>'


def _fmt_analyst_moves(moves: dict) -> str:
    """'2↑ 1↓' — zero-valued components omitted entirely (incl. init as '1i')."""
    bits = []
    for key, suffix in (("up", "↑"), ("down", "↓"), ("init", "i")):
        n = moves.get(key)
        if isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0:
            bits.append(f"{int(n)}{suffix}")
    return " ".join(bits)


def _pe_context_line(cand) -> str:
    """Muted context line under the chips row, built ONLY from candidate numbers:
    market cap (given in USD MILLIONS) / industry / analyst count / beat streak /
    analyst moves / buyback / dividend, middle-dot separated. Each segment is
    skipped when its data is missing; '' when every segment is missing (the card
    then renders no context line)."""
    cand = _as_dict(cand)
    segs = []

    cap_m = _num(cand.get("market_cap_m"))
    if cap_m is not None:
        segs.append(f"${_fmt_compact(cap_m * 1e6, decimals=1)}")

    industry = _clean(cand.get("industry"))
    if industry:
        segs.append(_esc(industry))

    count = cand.get("analyst_count")
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        segs.append(f"{count} analysts")

    streak = [v for v in (_num(x) for x in _as_list(cand.get("beat_streak")))
              if v is not None]
    if streak:
        segs.append(
            "streak " + "/".join(f"{_sign(v)}{abs(v):.1f}" for v in streak) + "%"
        )

    moves = cand.get("analyst_moves")
    if isinstance(moves, dict) and any(
        isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
        for v in moves.values()
    ):
        moves_txt = _fmt_analyst_moves(moves)
        if moves_txt:
            segs.append(moves_txt)

    buyback = _esc(cand.get("buyback"))
    if buyback:
        segs.append(f"buyback {buyback}")

    dividend = _esc(cand.get("dividend"))
    if dividend:
        segs.append(f"div {dividend}")

    return " &middot; ".join(segs)


# --------------------------------------------------------------------------- #
# Anomaly-panel flag chips (Spec E §6.4).
#
# `anomaly_panel.build_panel` assigns every flag a `direction` from a static
# per-metric sign convention. THE STYLING READS THAT FIELD AND NOTHING ELSE —
# never the statement text. A favourable flag whose statement happens to read
# like bad news ("Inventory days fell 12.3 …") must still style favourable, and
# any keyword sniffing here would silently re-decide, in the renderer, a
# question the panel already answered in Python.
#
# `deviation` is a ranking key and is never rendered (anomaly_panel.compute_flags
# says so in as many words); the flags arrive already capped at FLAG_CAP and
# already ranked, so this function adds neither a cap nor a sort.
# --------------------------------------------------------------------------- #
_FLAG_DIRECTION_CLASS = {"favourable": "fav", "unfavourable": "unfav",
                         "neutral": "neu"}


def _panel_chips(panel) -> str:
    """The panel's flags as chips, or '' when the panel is absent, degraded
    (`status: unavailable` yields `flags: []`) or malformed."""
    chips = []
    for flag in _as_list(_as_dict(panel).get("flags")):
        flag = _as_dict(flag)
        statement = _clean(flag.get("statement"))
        if not statement:
            continue
        direction = flag.get("direction")
        cls = (_FLAG_DIRECTION_CLASS.get(direction, "neu")
               if isinstance(direction, str) else "neu")
        operator = _clean(flag.get("operator"))
        head = f'<span class="op">{_esc(operator)}</span> ' if operator else ""
        chips.append(f'<span class="flagchip {cls}">{head}{_esc(statement)}</span>')
    if not chips:
        return ""
    return f'<div class="flagchips">{"".join(chips)}</div>'


# --------------------------------------------------------------------------- #
# Segment table (Spec E §6.4), and THE UNIT RULE that governs it.
#
# `sec_filing_text.parse_segments` publishes each cell AS PRINTED, in whatever
# unit the table's own caption states — AMD's says "(In millions)", so
# `revenue: 6718.0` means $6.718bn. The caption sits OUTSIDE the row, and that
# module deliberately refuses to apply it rather than infer it
# (`SEGMENT_VALUES_ARE_AS_PRINTED`). Meanwhile `sec_fundamentals.revenue_q` and
# every `anomaly_panel` figure are ABSOLUTE DOLLARS. A single expression mixing
# the two is off by 1e6 and renders a plausible wrong number rather than an
# error.
#
# THE RULE, and it is the whole answer to that hazard: this renderer publishes
# NO SEGMENT MAGNITUDE AT ALL. Every published segment figure is a ratio of the
# table against ITSELF — YoY change, share of the segment total, operating
# margin — each of which is exactly scale-invariant and therefore correct under
# any caption the filing carries. No segment value is ever added to, divided by
# or compared with a figure from another lane, so there is no arithmetic in
# which the mismatch could hide. Restoring a magnitude here would require
# reading the caption, which is Task 6's refused inference, not this file's.
# --------------------------------------------------------------------------- #
def _segment_rows(segments) -> list:
    """`[{"segment", "yoy_pct", "share", "share_delta", "op_margin",
    "op_margin_delta"}]` — ratios only, all in percent/points. A row missing a
    name or either revenue figure is dropped; `[]` when nothing survives."""
    raw = []
    for seg in _as_list(segments):
        seg = _as_dict(seg)
        name = _clean(seg.get("segment"))
        revenue, prior = _num(seg.get("revenue")), _num(seg.get("revenue_prior"))
        if not name or revenue is None or prior is None:
            continue
        raw.append((name, revenue, prior,
                    _num(seg.get("op_income")), _num(seg.get("op_income_prior"))))
    if not raw:
        return []

    # Both denominators are sums of the SAME table's own cells, so the shares
    # are exact whatever the caption's unit is.
    total = sum(row[1] for row in raw)
    total_prior = sum(row[2] for row in raw)

    out = []
    for name, revenue, prior, op_income, op_prior in raw:
        share = revenue / total * 100.0 if total else None
        share_prior = prior / total_prior * 100.0 if total_prior else None
        margin = op_income / revenue * 100.0 if (op_income is not None and revenue) else None
        margin_prior = (op_prior / prior * 100.0
                        if (op_prior is not None and prior) else None)
        out.append({
            "segment": name,
            "yoy_pct": (revenue - prior) / abs(prior) * 100.0 if prior else None,
            "share": share,
            "share_delta": (None if share is None or share_prior is None
                            else share - share_prior),
            "op_margin": margin,
            "op_margin_delta": (None if margin is None or margin_prior is None
                                else margin - margin_prior),
        })
    return out


def _pp_tail(delta) -> str:
    return "" if delta is None else f' <span class="ctx">({delta:+.1f}pp)</span>'


def _segment_table(segments) -> str:
    rows = _segment_rows(segments)
    if not rows:
        return ""
    trs = []
    for row in rows:
        yoy = row["yoy_pct"]
        if yoy is None:
            yoy_cell = '<td class="delta flat">—</td>'
        else:
            if yoy > 0:
                cls, arrow = "pos", "▲"
            elif yoy < 0:
                cls, arrow = "neg", "▼"
            else:
                cls, arrow = "flat", "·"
            yoy_cell = f'<td class="delta {cls}">{arrow} {yoy:+.1f}%</td>'
        share = row["share"]
        share_cell = ('<td class="segshare">—</td>' if share is None else
                      f'<td class="segshare">{share:.1f}%'
                      f'{_pp_tail(row["share_delta"])}</td>')
        margin = row["op_margin"]
        margin_cell = ('<td class="segmargin">—</td>' if margin is None else
                       f'<td class="segmargin">{margin:.1f}%'
                       f'{_pp_tail(row["op_margin_delta"])}</td>')
        trs.append(f'<tr><td class="segment">{_esc(row["segment"])}</td>'
                   f"{yoy_cell}{share_cell}{margin_cell}</tr>")
    return (
        '<table class="segments">'
        "<thead><tr><th>Segment</th>"
        '<th class="delta">Rev &Delta;%</th>'
        '<th class="segshare">Share</th>'
        '<th class="segmargin">Op margin</th></tr></thead>'
        f"<tbody>{''.join(trs)}</tbody>"
        "</table>"
        '<p class="muted seg-note">Shares and changes are computed from the '
        "filing&rsquo;s own segment table; its unit caption sits outside the "
        "rows, so no segment magnitude is restated.</p>"
    )


# --------------------------------------------------------------------------- #
# The grounding rule (Spec E §6.5): every qualitative filing claim carries a
# verbatim quote or IT IS DROPPED HERE. Same discipline as the URL rule — the
# model selects from supplied material, it never invents it — and the check is
# mechanical rather than trusting: the quote must actually occur in the text
# `sec_filing_text` extracted for THIS follow-up.
#
# Whitespace is normalised on both sides (a flattened filing's spacing is an
# artefact of tag-stripping, not of the filing) and the comparison is
# case-insensitive, but nothing else is relaxed: no fuzzy matching, no
# token overlap. A quote shorter than `_QUOTE_MIN_CHARS` grounds nothing —
# "revenue" occurs in every filing ever written and would ground anything.
# --------------------------------------------------------------------------- #
_QUOTE_MIN_CHARS = 20
_FU_EXCERPT_MAX = 240


def _normalise(text) -> str:
    return " ".join(_clean(text).split()).lower()


def _excerpt(text) -> str:
    """A short VERBATIM window onto a filing section. `slice_sections` returns
    slices up to `sec_filing_text.SECTION_MAX_CHARS` (20 000 chars); a briefing
    page shows a line, never the section."""
    flat = " ".join(_clean(text).split())
    if len(flat) <= _FU_EXCERPT_MAX:
        return flat
    cut = flat.rfind(" ", 0, _FU_EXCERPT_MAX)
    return (flat[:cut] if cut > 0 else flat[:_FU_EXCERPT_MAX]) + "…"


def _filing_corpus(fu) -> str:
    """Everything the filing-text lane extracted for this follow-up — the
    section slices plus the verbatim disclosure sentences — whitespace- and
    case-normalised. A quote is grounded only if it occurs in here."""
    pieces = []
    for value in _as_dict(_as_dict(fu).get("filing_sections")).values():
        if isinstance(value, str):
            pieces.append(value)
    for disclosure in _as_list(_as_dict(fu).get("disclosures")):
        quote = _as_dict(disclosure).get("quote")
        if isinstance(quote, str):
            pieces.append(quote)
    return _normalise(" ".join(pieces))


def _grounded_quotes(prose, fu) -> list:
    """The prose's `quotes` entries that genuinely occur in this follow-up's
    filing text, in the order given. `[]` — which drops every qualitative
    claim — when the filing text is absent, so a lane that failed silences the
    claims it would otherwise have been unable to check."""
    corpus = _filing_corpus(fu)
    if not corpus:
        return []
    out = []
    for entry in _as_list(_as_dict(prose).get("quotes")):
        text = _clean(_as_dict(entry).get("text"))
        needle = _normalise(text)
        if len(needle) < _QUOTE_MIN_CHARS or needle not in corpus:
            continue
        out.append(text)
    return out


def _post_earnings_section(briefing: dict, bundle: dict) -> str:
    """The whole Post-Earnings block (a Finance-desk subsection), or '' when the
    analysis carries no write-ups — old briefings and the degraded path render
    exactly as before (also_reported alone is not enough to show the block)."""
    pe = _as_dict(_as_dict(briefing).get("post_earnings"))
    writeups = _as_list(pe.get("writeups"))
    if not writeups:
        return ""

    # Ticker -> candidate lookup (case-insensitive; first occurrence wins).
    cands = {}
    for c in _as_list(_as_dict(bundle).get("post_earnings_candidates")):
        c = _as_dict(c)
        t = _clean(c.get("ticker")).upper()
        if t and t not in cands:
            cands[t] = c

    cards = []
    for w in writeups[:4]:  # display cap
        w = _as_dict(w)
        ticker = _clean(w.get("ticker"))
        cand = cands.get(ticker.upper())
        name = _clean(_as_dict(cand).get("name")) if cand else ""

        card = ["<article class='card pe'>"]
        title = name or ticker
        if title:
            head = f"<h3>{_esc(title)}"
            if name and ticker:
                head += f' <span class="ticker">{_esc(ticker)}</span>'
            card.append(head + "</h3>")
        headline = _clean(w.get("headline"))
        if headline:
            card.append(f'<p class="pe-headline">{_esc(headline)}</p>')
        company = _clean(w.get("company"))
        if company:
            card.append(f'<p class="muted pe-company">{_esc(company)}</p>')
        chips = _pe_chips(cand) if cand else []  # no candidate -> chip-less card
        gchip = _guidance_chip(w, cand)
        if gchip:
            chips.append(gchip)
        if chips:
            card.append(f'<div class="pe-chips">{"".join(chips)}</div>')
        # Beat-1 flags. Spec §3 ("When the panel runs"): the panel is built for
        # every in-scope reporter on the 8-K morning, with the two
        # filing-dependent families absent — which is exactly what gives the
        # beat-1 card its flags. `run.py` sets `cand["panel"]`.
        card.append(_panel_chips(_as_dict(cand).get("panel")))
        context = _pe_context_line(cand) if cand else ""
        if context:
            card.append(f'<p class="muted pe-context">{context}</p>')
        for line in (_pe_fundamentals_line(cand), _pe_referee_line(cand)):
            if line:
                card.append(f'<p class="muted pe-sec">{line}</p>')
        guidance = _clean(w.get("guidance"))
        if guidance:
            card.append(
                f'<p class="pe-guidance"><span class="lbl">Guidance:</span> '
                f'{_esc(guidance)}</p>'
            )
        read = _clean(w.get("read"))
        if read:
            card.append(f'<p class="body">{_esc(read)}</p>')
        read_through = _clean(w.get("read_through"))
        if read_through:
            card.append(
                f'<p class="pe-readthrough"><span class="lbl">Read-through:</span> '
                f'{_esc(read_through)}</p>'
            )
        pills = [f'<span class="pill">{_esc(t)}</span>'
                 for t in _as_list(w.get("tags")) if _clean(t)]
        if pills:
            card.append(f'<div class="pills">{"".join(pills)}</div>')
        card.append("</article>")
        cards.append("".join(card))

    also_bits = []
    for a in _as_list(pe.get("also_reported")):
        a = _as_dict(a)
        t = _clean(a.get("ticker"))
        note = _clean(a.get("note"))
        if not (t or note):
            continue
        also_bits.append(
            f"{_esc(t)} &mdash; {_esc(note)}" if t and note else _esc(t or note)
        )
    also_bits = also_bits[:6]  # cap
    tail = (
        f'<p class="muted pe-also">Also reported: {"; ".join(also_bits)}</p>'
        if also_bits else ""
    )

    return (
        '<div id="post-earnings">\n'
        '<h3 class="sub">Post-Earnings</h3>\n'
        f'{"".join(cards)}{tail}\n'
        "</div>\n"
    )


# --------------------------------------------------------------------------- #
# Beat 2 (Spec C D6): the filing follow-ups block. Deliberately NOT a write-up
# card — a reader must never mistake a follow-up for a fresh reporter. The
# beat-1 numbers are recalled muted and unchipped (they are old news being
# restated), and only what the filing adds is new.
# --------------------------------------------------------------------------- #
# The block cap, and one of the three places the same number is stated (the
# emitter's cap and finance.md's `followups` bullet are the others). If they ever
# disagree the LOWEST silently becomes the real cap, so they move together.
FOLLOWUP_DISPLAY_MAX = 6

# Compact "Also filed" entries shown before the fold. Day one of the backfill
# carries ~190 of them; the fold is what makes that readable rather than a wall.
LINE_FOLD_AFTER = 8


def _pe_snapshot_line(fu: dict) -> str:
    """Muted restatement of the beat-1 numbers, middle-dot separated with
    missing segments skipped."""
    segs = []
    report_date = _clean(fu.get("report_date"))
    if report_date:
        segs.append(f"reported {_esc(report_date)}")

    eps_est, eps_act = _num(fu.get("eps_est")), _num(fu.get("eps_act"))
    if eps_est is not None and eps_act is not None:
        segs.append(f"EPS {eps_est:.2f} &rarr; {eps_act:.2f}")
    elif eps_act is not None:
        segs.append(f"EPS {eps_act:.2f}")

    rev_est, rev_act = _num(fu.get("rev_est")), _num(fu.get("rev_act"))
    if rev_est is not None and rev_act is not None:
        segs.append(f"Rev {_fmt_compact(rev_est)} &rarr; {_fmt_compact(rev_act)}")
    elif rev_act is not None:
        segs.append(f"Rev {_fmt_compact(rev_act)}")

    surprise = _num(fu.get("surprise_pct"))
    if surprise is not None:
        segs.append(f"surprise {_sign(surprise)}{abs(surprise):.1f}%")

    move = _num(fu.get("price_move_pct"))
    if move is not None:
        segs.append(f"reaction {_sign(move)}{abs(move):.1f}%")

    return " &middot; ".join(segs)


def _pe_fu_risk_line(fu) -> str:
    """The Item 1A line, or ''. `sec_filing_text.slice_sections` publishes
    `risk_changes` ONLY when the filing asserts a change — a 10-Q's usual "no
    material changes" boilerplate yields an absent key — so the key's presence
    IS the finding and no judgement is made here. The excerpt is verbatim."""
    sections = _as_dict(_as_dict(fu).get("filing_sections"))
    excerpt = _excerpt(sections.get("risk_changes"))
    if not excerpt:
        return ""
    return (f'<p class="muted pe-fu-risk"><span class="lbl">Risk factors:</span> '
            f"{_esc(excerpt)}</p>")


def _followup_lines(lines) -> str:
    """The 'Also filed' list: every matched filing that did not earn a full
    block. Every field comes from the filing index — no model is involved in
    this tier at all, which is what makes the extra coverage nearly free — so
    there is nothing here to ground and nothing to drop. '' when the list is
    empty or malformed; the caller decides whether the section exists."""
    items = []
    for line in _as_list(lines):
        line = _as_dict(line)
        ticker = _clean(line.get("ticker"))
        name = _clean(line.get("name"))
        if not (ticker or name):
            continue  # nothing names the filer; a bare form/date is not an entry
        head = _esc(name or ticker)
        if name and ticker:
            head += f' <span class="ticker">{_esc(ticker)}</span>'
        bits = [head]
        form = _clean(line.get("form"))
        if form:
            bits.append(_esc(form))
        filed = _clean(line.get("filed"))
        if filed:
            bits.append(_fmt_date(filed))
        items.append(f"<li>{' &middot; '.join(bits)}</li>")

    if not items:
        return ""

    # "Also filed", NOT "also filed since we covered them": a filing frequently
    # precedes the coverage (COTY filed its 10-K on 08-20 and was a card on
    # 08-22). The window is a date range, not a causal order, and the heading
    # must not imply one.
    parts = ['<p class="muted">Also filed</p>']
    if len(items) > LINE_FOLD_AFTER:
        extra = len(items) - LINE_FOLD_AFTER
        parts.append(f"<ul class='rows'>{''.join(items[:LINE_FOLD_AFTER])}</ul>")
        parts.append(
            # The phosphor CSS prefixes the summary with "+ " via ::before.
            f"<details class='more'><summary>{extra} more filings</summary>"
            f"<ul class='rows'>{''.join(items[LINE_FOLD_AFTER:])}</ul></details>"
        )
    else:
        parts.append(f"<ul class='rows'>{''.join(items)}</ul>")
    return "".join(parts)


def _pe_followup_section(briefing: dict, bundle: dict, lines=None) -> str:
    """The `From the filings` block, or '' when NEITHER tier has anything. The
    numbers are deterministic, so the block renders on the bundle's follow-up
    list alone; Codex prose is joined in by ticker where it exists. `lines` is
    the compact tier — the matched filings that earned no block — and shares
    this section's single heading."""
    followups = [f for f in _as_list(_as_dict(bundle).get("post_earnings_followups"))
                 if isinstance(f, dict)]

    pe = _as_dict(_as_dict(briefing).get("post_earnings"))
    prose = {}
    for p in _as_list(pe.get("followups")):
        p = _as_dict(p)
        ticker = _clean(p.get("ticker")).upper()
        if ticker and ticker not in prose:
            prose[ticker] = p

    blocks = []
    for fu in followups[:FOLLOWUP_DISPLAY_MAX]:
        ticker = _clean(fu.get("ticker"))
        name = _clean(fu.get("name"))

        parts = []  # everything below the heading; a heading alone is not a block
        snapshot = _pe_snapshot_line(fu)
        if snapshot:
            parts.append(f'<p class="muted pe-fu-recall">{snapshot}</p>')
        for line in (_pe_fundamentals_line(fu), _pe_referee_line(fu)):
            if line:
                parts.append(f'<p class="muted pe-sec">{line}</p>')

        # The guide, restated from the beat-1 snapshot the watch carried
        # forward, with the same deterministic midpoint-vs-consensus verdict
        # the beat-1 chip uses (spec §6.2). A line, not a chip: a follow-up's
        # recalled numbers are deliberately unchipped.
        guidance = _guidance_parts(fu, fu)
        if guidance is not None:
            parts.append(f'<p class="pe-fu-guide {guidance[0]}">'
                         f'<span class="lbl">Guide:</span> {guidance[1]}</p>')

        p = _as_dict(prose.get(ticker.upper()))
        headline = _clean(p.get("headline"))
        if headline:
            parts.append(f'<p class="pe-fu-headline">{_esc(headline)}</p>')
        body = _clean(p.get("body"))
        if body:
            parts.append(f'<p class="body">{_esc(body)}</p>')

        # Facts from the bundle; the two reads only where a quote grounds them.
        quotes = _grounded_quotes(p, fu)
        parts.append(_panel_chips(fu.get("panel")))
        panel_read = _clean(p.get("panel_read"))
        if panel_read and quotes:
            parts.append(f'<p class="body pe-fu-read">{_esc(panel_read)}</p>')

        parts.append(_segment_table(fu.get("segments")))
        segment_read = _clean(p.get("segment_read"))
        if segment_read and quotes:
            parts.append(f'<p class="body pe-fu-read">{_esc(segment_read)}</p>')

        parts.append(_pe_fu_risk_line(fu))
        for quote in quotes:
            parts.append(f'<p class="muted fu-quote">&ldquo;{_esc(quote)}&rdquo;</p>')

        parts = [part for part in parts if part]
        if not parts:
            continue

        title = name or ticker
        if title:
            head = f'<span class="pe-fu-name">{_esc(title)}</span>'
            if name and ticker:
                head += f' <span class="ticker">{_esc(ticker)}</span>'
            parts.insert(0, f'<p class="pe-fu-head">{head}</p>')
        blocks.append(f'<div class="pe-fu">{"".join(parts)}</div>')

    also = _followup_lines(lines)
    # Either tier is enough to publish: a day with matched filings but no
    # promotions still owes the reader its list. A day with neither renders
    # nothing at all, so the heading is never orphaned.
    if not (blocks or also):
        return ""
    return (
        '<div id="pe-followups">\n'
        '<h3 class="sub">From the filings</h3>\n'
        f'{"".join(blocks)}{also}\n'
        "</div>\n"
    )




# --------------------------------------------------------------------------- #
# Story cards (shared by The Brief and the Finance Brief)
#
# Each card carries data-region (the colour bucket for the CSS rail) and
# data-region-label (the short code shown next to the story number). The
# analysis may set `region` explicitly (schema); otherwise the bucket is
# derived from headline/tags/why keywords so colour-coding still works on
# days the model omits it.
# --------------------------------------------------------------------------- #
# JAPAN and CANADA are dedicated-desk buckets supplied by their section
# renderers. Their cards intentionally carry no model-written `region` field.
_REGION_BUCKETS = {
    "US", "AMERICAS", "ASIA", "MENA", "EUROPE", "GLOBAL"
}
_REGION_KEYWORDS = [
    ("MENA",     ("iran", "hormuz", "gaza", "israel", "gulf", "saudi", "yemen",
                  "syria", "middle east")),
    ("ASIA",     ("korea", "kospi", "japan", "nikkei", "china", "taiwan", "tsmc",
                  "sk hynix", "asia", "hynix")),
    ("EUROPE",   ("uk", "britain", "eu", "europe", "france", "germany", "nato",
                  "brussels")),
    ("AMERICAS", ("cuba", "mexico", "brazil", "canada", "venezuela", "latin")),
    ("US",       ("senate", "white house", "fed", "sec", "congress", "u.s.",
                  "us ", "american")),
]
_REGION_LABELS = {"US": "US", "AMERICAS": "AMER", "ASIA": "ASIA",
                  "MENA": "MENA", "EUROPE": "EUR", "GLOBAL": "GLOBAL"}


def _derive_region(story):
    """Keyword fallback: (bucket, label) from headline/tags/why text."""
    story = _as_dict(story)
    hay = " ".join([
        _clean(story.get("headline")),
        " ".join(_clean(t) for t in _as_list(story.get("tags"))),
        _clean(story.get("why_it_matters")),
    ]).lower()
    for bucket, kws in _REGION_KEYWORDS:
        if any(k in hay for k in kws):
            return bucket, _REGION_LABELS[bucket]
    return "GLOBAL", _REGION_LABELS["GLOBAL"]


def _region_for(story):
    """(bucket, label) for a story: the analysis's explicit `region` when it is
    a valid bucket, else the keyword-derived fallback."""
    bucket = _clean(_as_dict(story).get("region")).upper()
    if bucket in _REGION_BUCKETS:
        return bucket, _REGION_LABELS[bucket]
    return _derive_region(story)


def _story_card(story, *, region_override=None) -> str:
    """One story card: headline / why / body / source chips / tag pills."""
    story = _as_dict(story)
    headline = _esc(story.get("headline"))
    why = _clean(story.get("why_it_matters"))
    body = _clean(story.get("body"))

    chips = []
    for src in _as_list(story.get("sources")):
        src = _as_dict(src)
        url = _clean(src.get("url"))
        outlet = _clean(src.get("outlet"))
        if not url or not outlet:
            continue
        chips.append(f'<span class="chip">{_link(outlet, url)}</span>')

    pills = []
    for tag in _as_list(story.get("tags")):
        t = _clean(tag)
        if t:
            pills.append(f'<span class="pill">{_esc(t)}</span>')

    region, region_label = region_override or _region_for(story)
    # data-region on the <article> drives the rail colour; data-region-label is
    # repeated on the <h3> because the CSS reads it via attr() from the h3's
    # ::before (attr() cannot reach the parent article's attributes).
    label_attr = f" data-region-label='{_esc_attr(region_label)}'"
    card = [
        f"<article class='card' data-region='{_esc_attr(region)}'{label_attr}>"
    ]
    if headline:
        card.append(f"<h3{label_attr}>{headline}</h3>")
    if why:
        card.append(f'<p class="why"><strong>Why it matters:</strong> {_esc(why)}</p>')
    if body:
        card.append(f'<p class="body">{_esc(body)}</p>')
    if chips:
        card.append(f'<div class="chips">{"".join(chips)}</div>')
    if pills:
        card.append(f'<div class="pills">{"".join(pills)}</div>')
    card.append("</article>")
    return "".join(card)


def _brief_section(brief: list) -> str:
    cards = [_story_card(story) for story in _as_list(brief)]
    if not cards:
        return '<p class="muted">No lead stories today.</p>'
    return "".join(cards)


def _desk_section(desk: dict, stories) -> str:
    """Render one national section without changing its source stories."""
    did = desk["id"]
    cards = [_story_card(story, region_override=(did.upper(), did.upper()))
             for story in _as_list(stories)]
    if not cards:
        return ""
    return (f'<section id="{_esc_attr(did)}">\n'
            f'<h2 class="section">{_esc(desk["label"])}</h2>\n'
            f'{"".join(cards)}\n'
            f"</section>")


# --------------------------------------------------------------------------- #
# Finance desk: one banner composing finance brief -> scoreboard -> post-earnings
# -> filings & calendar. The banner ALWAYS renders (the scoreboard is
# bundle-side), even when every prose field is empty.
#
# There was a `World -> Markets` block above the brief until 2026-08-16, written
# from `finance.bridge`. Measured across the four briefs since `scoreboard_read`
# shipped, it, `scoreboard_note` and `scoreboard_read.contradiction` stated the
# same observation three times over. The world->markets linkage it alone carried
# -- naming a published story and tying it to a price -- moved INTO
# `scoreboard_read`, where it answers to the operator grammar and sits against
# the table it reads.
# --------------------------------------------------------------------------- #
def _norm_headline(text) -> str:
    """The form a citation is matched in: casefolded, whitespace-collapsed.
    Deliberately NOT fuzzy — a model that cannot copy back a headline it was
    handed has not selected from the material it was given, it has written a
    new one."""
    return " ".join(_clean(text).casefold().split())


def _citable_stories(briefing) -> dict:
    """`{normalised headline: url}` for every story this briefing PUBLISHED
    that the finance pass was actually shown.

    That is The Brief — which reaches the finance pass as `world_headlines` —
    plus the finance desk's own brief, which that pass wrote itself. `japan` is
    deliberately absent: the finance pass never sees those stories, so a
    citation naming one could only have been invented, and a citable set the
    model cannot satisfy is a trap rather than a rule.

    Note this reads the BRIEFING, not the bundle. `world_headlines` lives only
    in the shallow copy `codex_engine._finance_bundle` hands to Codex; the
    renderer is given the original bundle and never sees that key. The published
    brief is the better source anyway — it is the record of what actually
    shipped, and it carries the `sources` urls the chip links to.

    First url wins per story; `""` when it carried none, which `_link` already
    degrades to plain text. Never raises."""
    out = {}
    briefing = _as_dict(briefing)
    for group in (_as_list(briefing.get("brief")),
                  _as_list(_as_dict(briefing.get("finance")).get("brief"))):
        for story in group:
            story = _as_dict(story)
            key = _norm_headline(story.get("headline"))
            if not key or key in out:
                continue
            url = ""
            for src in _as_list(story.get("sources")):
                candidate = _clean(_as_dict(src).get("url"))
                if candidate:
                    url = candidate
                    break
            out[key] = url
    return out


def _cite(story, citable):
    """`(grounded, chip_html)` for one operator item's `story` citation.

    No citation is legitimate — a contradiction between two scoreboard figures
    has no story behind it — and grounds fine, with no chip. A citation that
    does NOT resolve to a published headline returns `grounded=False`, and the
    caller drops the whole operator item.

    Dropping the item rather than just the chip is the point. This is the
    `_grounded_quotes` rule applied to the scoreboard: a sentence asserting
    "crude rose on the Lebanon strikes" when no Lebanon story was published is
    a fabricated causal link, and a fabricated link with the footnote quietly
    removed is worse than one that shows its working."""
    text = _clean(story)
    if not text:
        return True, ""
    url = citable.get(_norm_headline(text))
    if url is None:
        return False, ""
    return True, f' <span class="chip">{_link(text, url)}</span>'


def _read_item(value, key: str):
    """One operator item as `(text, story)`.

    Tolerates the pre-2026-08-16 shape, where `contradiction` was a bare string
    and `watch` a list of them, for the same reason every accessor in this
    module coerces rather than trusts: the renderer is pure and must render
    whatever it is handed."""
    if isinstance(value, str):
        return _clean(value), None
    value = _as_dict(value)
    return _clean(value.get(key)), value.get("story")


def _scoreboard_read(read, citable=None) -> str:
    """The finance pass's read of the scoreboard (spec §6.5): the computed
    contradiction, its precedent, and the watch-items with their CURRENT
    values. Prose only — every figure it cites was computed upstream.

    Since 2026-08-16 the contradiction and each watch item may also CITE a
    published story, which is where the retired `World → Markets` bridge's one
    irreplaceable move went: naming a story and tying it to a price. An
    unresolvable citation drops its item (see `_cite`).

    `""` unless at least one part survives, so a quiet day — or a day whose
    every citation was invented — adds no empty furniture. Never raises: a
    malformed shape renders nothing."""
    read = _as_dict(read)
    citable = citable if isinstance(citable, dict) else {}

    contradiction = ""
    statement, story = _read_item(read.get("contradiction"), "statement")
    if statement:
        grounded, chip = _cite(story, citable)
        if grounded:
            contradiction = f'<p class="sb-contra">{_esc(statement)}{chip}</p>'

    precedent = _clean(read.get("precedent"))

    watch = []
    for raw in _as_list(read.get("watch")):
        condition, story = _read_item(raw, "condition")
        if not condition:
            continue
        grounded, chip = _cite(story, citable)
        if grounded:
            watch.append(f"<li>{_esc(condition)}{chip}</li>")

    if not (contradiction or precedent or watch):
        return ""

    parts = ['<div class="scoreboard-read">']
    if contradiction:
        parts.append(contradiction)
    if precedent:
        parts.append(f'<p class="sb-prec">{_esc(precedent)}</p>')
    if watch:
        parts.append('<ul class="sb-watch">')
        parts.extend(watch)
        parts.append("</ul>")
    parts.append("</div>")
    return "".join(parts)


def _finance_section(briefing: dict, bundle: dict, date: str) -> str:
    briefing = _as_dict(briefing)
    bundle = _as_dict(bundle)
    finance = _as_dict(briefing.get("finance"))

    parts = ['<section id="finance">\n<h2 class="section">Finance</h2>\n']

    fin_stories = _as_list(finance.get("brief"))[:3]  # display cap
    if fin_stories:
        parts.append('<h3 class="sub">Finance Brief</h3>')
        parts.extend(_story_card(story) for story in fin_stories)

    parts.append('<h3 class="sub">Scoreboard</h3>')
    parts.append(_markets_table(bundle.get("markets"), date))
    note = _clean(finance.get("scoreboard_note"))
    if note:
        parts.append(f'<p class="note">{_esc(note)}</p>')
    parts.append(_scoreboard_read(finance.get("scoreboard_read"),
                                  _citable_stories(briefing)))

    parts.append(_post_earnings_section(briefing, bundle))
    # `filing_lines` is the reconciler's compact tier — every filing matched
    # today that did not earn a full block. Deterministic, so it costs no model
    # time however long it gets.
    parts.append(_pe_followup_section(briefing, bundle,
                                      _as_list(bundle.get("filing_lines"))))
    parts.append(_filings_calendar(finance, bundle))
    parts.append("\n</section>\n")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Signal / Threads / On the Radar
# --------------------------------------------------------------------------- #
def _signal_section(briefing: dict) -> str:
    """The whole <section>, or '' when the top signal, the buried list and the
    noise line are all empty."""
    signal = _as_dict(_as_dict(briefing).get("signal"))
    top = _as_dict(signal.get("top"))
    top_story = _clean(top.get("story"))
    top_reason = _clean(top.get("reason"))
    top_url = _clean(top.get("url"))

    lis = []
    for it in _as_list(signal.get("buried")):
        it = _as_dict(it)
        story = _clean(it.get("story"))
        reason = _clean(it.get("reason"))
        url = _clean(it.get("url"))
        if not (story or reason):
            continue
        head = _link(story, url) if story else ""
        tail = f" &mdash; {_esc(reason)}" if reason else ""
        lis.append(f"<li>{head}{tail}</li>")

    noise = _clean(signal.get("noise_line"))

    if not (top_story or top_reason or lis or noise):
        return ""

    parts = ['<section id="signal">\n<h2 class="section">Signal</h2>\n']
    if top_story or top_reason:
        parts.append('<h3 class="sub">Signal of the day</h3>')
        card = ["<article class='card signal-top'>"]
        if top_story:
            card.append(f"<h3>{_link(top_story, top_url)}</h3>")
        if top_reason:
            card.append(f'<p class="body">{_esc(top_reason)}</p>')
        card.append("</article>")
        parts.append("".join(card))
    if lis:
        parts.append('<h3 class="sub">Also buried</h3>')
        parts.append(f"<ul class='rows'>{''.join(lis)}</ul>")
    if noise:
        parts.append(f'<p class="muted noise">{_esc(noise)}</p>')
    parts.append("\n</section>\n")
    return "".join(parts)


def _threads_section(threads: list) -> str:
    items = []
    for th in _as_list(threads):
        th = _as_dict(th)
        slug = _clean(th.get("slug"))
        today = _clean(th.get("today"))
        since = _clean(th.get("since"))
        if not (slug or today):
            continue
        head = f'<span class="slug">{_esc(slug)}</span>' if slug else ""
        arrow = " &rarr; " if slug and today else ""
        since_txt = f' <span class="since">since {_esc(since)}</span>' if since else ""
        items.append(f"<li>{head}{arrow}{_esc(today)}{since_txt}</li>")
    if not items:
        return '<p class="muted">No active threads.</p>'
    return f"<ul class='threads'>{''.join(items)}</ul>"


def _radar_section(on_radar: list) -> str:
    lis = [f"<li>{_esc(r)}</li>" for r in _as_list(on_radar) if _clean(r)]
    if not lis:
        return '<p class="muted">Nothing on the radar.</p>'
    return f"<ul class='radar'>{''.join(lis)}</ul>"


def _diagnostics(bundle: dict, date: str, timezone_label: str = "JST") -> str:
    health = _as_dict(bundle.get("source_health"))
    failed = sorted(
        str(src) for src, v in health.items()
        if not (isinstance(v, dict) and v.get("ok"))
    )
    gaps = (
        f"data gaps: {_esc(', '.join(failed))}" if failed
        else "all sources ok"
    )
    return (
        f'<p>World Intelligence &middot; {_esc(date)} ({_esc(timezone_label)}) &middot; {gaps}</p>'
    )


# --------------------------------------------------------------------------- #
# Tab chrome (Spec E §8.1, mechanics lifted from Spec D §7).
#
# ZERO JAVASCRIPT. The whole switch is a group of hidden `<input type="radio"
# name="tab">` elements plus `#tab-N:checked ~ #panel-N { display: block }`
# sibling rules in the static stylesheet. That is why EVERY input must precede
# EVERY label and panel and share one parent: `~` only reaches later siblings.
#
# WHY THE IDS ARE INDEXED AND NOT THE CALLER'S. The sibling rules live in
# `_STYLE`, which is a constant, so the selectors have to be written before the
# tab list is known. Indexed ids (`tab-1`/`panel-1`) let the HTML stay
# data-driven against a fixed rule set; the caller's own id rides along as
# `data-tab` so a panel is still addressable by name. `TAB_MAX` is therefore not
# a style choice — it is exactly how many rule pairs `_STYLE` carries, and a
# seventh tab means adding the seventh pair there.
# --------------------------------------------------------------------------- #


# Spec §8.1: "a tab with nothing in it renders an explicit empty-state panel
# rather than an empty box". A blank panel reads as a broken document.
_TAB_EMPTY = '<p class="muted">No stories cleared the bar today.</p>'


def _tabs(panels: list) -> str:
    """Tab chrome from `[{"id", "label", "count", "html"}]` — data-driven: two
    entries today, six later. '' when nothing renderable is passed; never
    raises on a malformed entry."""
    clean = []
    for panel in _as_list(panels):
        panel = _as_dict(panel)
        label = _clean(panel.get("label"))
        if not label:
            continue
        clean.append(panel)
    if not clean:
        return ""

    inputs, labels, bodies = [], [], []
    for i, panel in enumerate(clean, start=1):
        checked = " checked" if i == 1 else ""
        inputs.append(f'<input class="tabin" type="radio" name="tab" '
                      f'id="tab-{i}"{checked}>')

        count = panel.get("count")
        badge = ""
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            badge = f' <span class="tabcount">{count}</span>'
        labels.append(f'<label class="tablabel" for="tab-{i}">'
                      f'{_esc(panel.get("label"))}{badge}</label>')

        html = panel.get("html")
        html = html if isinstance(html, str) and html.strip() else _TAB_EMPTY
        tab_id = _clean(panel.get("id"))
        attr = f' data-tab="{_esc_attr(tab_id)}"' if tab_id else ""
        bodies.append(f'<div class="panel" id="panel-{i}"{attr}>\n{html}\n</div>')

    return ('<div class="tabs">\n'
            f"{''.join(inputs)}\n"
            f'<nav class="tabbar">{"".join(labels)}</nav>\n'
            f"{''.join(bodies)}\n"
            "</div><!-- /tabs -->\n")


def _finance_count(briefing: dict, bundle: dict) -> int:
    """Items behind the Finance tab: the finance-brief cards, the post-earnings
    write-ups and the resurfaced follow-ups, each counted at the SAME display
    cap the renderer applies, so the label never promises a card the panel does
    not contain. Spec §8.1: the label is what stops the tab being a closed
    door."""
    briefing = _as_dict(briefing)
    finance = _as_dict(briefing.get("finance"))
    pe = _as_dict(briefing.get("post_earnings"))
    writeups = _as_list(pe.get("writeups"))
    followups = [f for f in _as_list(_as_dict(bundle).get("post_earnings_followups"))
                 if isinstance(f, dict)]
    return (len(_as_list(finance.get("brief"))[:3])
            + len(writeups[:4])
            + len(followups[:FOLLOWUP_DISPLAY_MAX]))


# --------------------------------------------------------------------------- #
# Styles
# --------------------------------------------------------------------------- #
_STYLE = """
/* =====================================================================
   WORLD INTELLIGENCE — "Green Phosphor" theme
   Drop-in replacement for the _STYLE string in compose_html.py.

   This is CSS-ONLY. It restyles the EXISTING HTML your renderer already
   emits (.wrap, header.masthead, h2.section, .card, .why, .body, .chips,
   .pills, table.markets, .svn-col, ul.threads, ul.radar, footer.diagnostics).

   The ONLY new HTML hook required is a `data-region` attribute on each
   .card (see README §4). Everything else — story numbers, section "##"
   prefixes, the CRT scanlines, the blinking cursor — is generated here.

   CONSTRAINT PRESERVED: no external fonts/CSS/JS/images. Monospace comes
   from a system stack; if IBM Plex Mono happens to be installed it is used.
   ===================================================================== */

:root {
  color-scheme: dark;                 /* phosphor is always dark */
  --bg:    #050805;
  --panel: #0a110b;
  --line:  #15281a;
  --line2: #1f3a26;
  --ink:   #8bf5ad;   /* bright phosphor — headlines, values          */
  --dim:   #57b97e;   /* body copy                                    */
  --faint: #347a4f;   /* metadata, rules                              */
  --acc:   #a6ffc4;   /* brightest — wordmark, section ## , links     */
  --amber: #ffcf4a;   /* warnings / "why it matters"                  */
  --up:    #8bf5ad;   /* market up                                    */
  --dn:    #ff7a63;   /* market down                                  */

  /* Region palette — drives the per-story rail + code chip.
     Values are OKLCH (crisp on modern Safari/Chrome); hex fallback in comments. */
  --rc-us:       oklch(0.76 0.13 255);  /* #5aa2f5 blue    */
  --rc-americas: oklch(0.76 0.14 350);  /* #e58ac9 magenta */
  --rc-asia:     oklch(0.80 0.12 195);  /* #4fd6cf cyan    */
  --rc-mena:     oklch(0.76 0.15 35);   /* #f2894e orange  */
  --rc-europe:   oklch(0.76 0.13 305);  /* #c39af2 violet  */
__NATIONAL_COLOURS__  --rc-global:   #57b97e;               /* green fallback  */

  --mono: 'IBM Plex Mono', ui-monospace, SFMono-Regular, 'SF Mono',
          Menlo, Consolas, 'Liberation Mono', monospace;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  padding: 0;
  font-family: var(--mono);
  line-height: 1.55;
  color: var(--dim);
  background: var(--bg);
  background-image: radial-gradient(120% 55% at 50% -6%,
                    rgba(50,160,90,.14), rgba(5,8,5,0) 55%);
  -webkit-font-smoothing: antialiased;
}

/* CRT scanline overlay — the signature. pointer-events:none so links work. */
body::before {
  content: "";
  position: fixed;
  inset: 0;
  z-index: 9999;
  pointer-events: none;
  mix-blend-mode: multiply;
  background: repeating-linear-gradient(0deg,
              rgba(0,0,0,0) 0, rgba(0,0,0,0) 2px, rgba(0,18,7,.30) 3px);
  animation: crtflk 3.5s steps(2) infinite;
}
@keyframes crtflk { 0%,100%{opacity:.5} 48%{opacity:.42} 52%{opacity:.6} }
@keyframes curse  { 0%,49%{opacity:1} 50%,100%{opacity:0} }
@media (prefers-reduced-motion: reduce) {
  body::before { animation: none; }
  .masthead h1::after, footer.diagnostics::after { animation: none; }
}

.wrap { max-width: 640px; margin: 0 auto; padding: 26px 22px 60px; }

/* ---- Masthead ------------------------------------------------------- */
header.masthead { border: 0; padding: 0 0 4px; margin: 0 0 6px; }
header.masthead h1 {
  margin: 6px 0 4px;
  font: 700 clamp(21px, 6vw, 27px)/1 var(--mono);
  letter-spacing: .01em;
  text-transform: uppercase;
  color: var(--acc);
  text-shadow: 0 0 10px rgba(139,245,173,.45);
}
header.masthead h1::after {           /* blinking block cursor */
  content: "";
  display: inline-block;
  width: .5em; height: .9em;
  margin-left: .12em;
  vertical-align: -0.08em;
  background: var(--acc);
  box-shadow: 0 0 8px rgba(139,245,173,.6);
  animation: curse 1.1s steps(1) infinite;
}
header.masthead .date {
  font: 400 12px/1.4 var(--mono);
  color: var(--dim);
  text-transform: none;
  letter-spacing: .03em;
}
header.masthead .date::before { content: "> "; color: var(--faint); }
/* Lede — one-line lead-story subhead, amber callout */
header.masthead .lede {
  margin: 11px 0 0;
  padding: 9px 12px;
  border-left: 2px solid var(--amber);
  background: rgba(255,207,74,.05);
  font: 400 12.5px/1.55 var(--mono);
  font-style: normal;
  color: var(--ink);
}
header.masthead .lede::before {
  content: "\\25B8 LEDE  ";
  color: var(--amber);
  font-weight: 700;
  letter-spacing: .06em;
}

/* ---- Sub-section headers (h3.sub inside Finance / Signal) ------------ */
h3.sub {
  display: flex;
  align-items: center;
  gap: 8px;
  font: 600 10px/1 var(--mono);
  text-transform: uppercase;
  letter-spacing: .15em;
  color: var(--dim);
  margin: 22px 0 10px;
}
h3.sub::before { content: "\\00BB"; color: var(--acc); font-weight: 700; font-size: 11px; }
h3.sub::after  { content: ""; flex: 1; border-top: 1px dotted var(--line); }
/* Signal-of-the-day sub-header reads amber to flag the highlighted item */
#signal h3.sub:first-of-type { color: var(--amber); }
#signal h3.sub:first-of-type::before { color: var(--amber); }

/* ---- Section headers ------------------------------------------------ */
section { margin-top: 30px; }
h2.section {
  display: flex;
  align-items: center;
  gap: 8px;
  font: 700 12px/1 var(--mono);
  text-transform: uppercase;
  letter-spacing: .16em;
  color: var(--ink);
  border: 0;
  padding: 0 0 8px;
  margin: 0 0 4px;
}
h2.section::before { content: "##"; color: var(--acc); font-weight: 700; }
h2.section::after  { content: ""; flex: 1; border-top: 1px dashed var(--line2); }

/* ---- The Brief ------------------------------------------------------ */
/* Numbering + region rail apply to BRIEF cards and the FINANCE BRIEF card,
   but NOT to post-earnings (.pe) or the signal-of-the-day (.signal-top). */
#brief { counter-reset: brf; }
p { margin: 0 0 10px; }

.card {
  position: relative;
  background: transparent;
  border: 0;
  border-top: 1px dashed var(--line2);
  border-radius: 0;
  padding: 16px 0 16px 15px;
  margin: 0;
}
#brief .card { counter-increment: brf; }
/* region rail — brief cards, the Japan cards, + the single Finance Brief card */
#brief .card::after,
__NATIONAL_SELECTORS__#finance > .card:not(.pe)::after {
  content: "";
  position: absolute;
  left: 0; top: 16px; bottom: 16px;
  width: 3px; border-radius: 2px;
  background: var(--rc, var(--rc-global));
}
/* [01]  US — number + region code (brief only) */
#brief .card > h3::before {
  content: "[" counter(brf, decimal-leading-zero) "]  " attr(data-region-label);
  display: block;
  margin-bottom: 8px;
  font: 600 10px/1 var(--mono);
  letter-spacing: .06em;
  color: var(--rc, var(--rc-global));
  text-shadow: 0 0 7px currentColor;
}
/* Finance Brief card shows just the region code, no number */
#finance > .card:not(.pe) > h3::before {
  content: attr(data-region-label);
  display: block;
  margin-bottom: 8px;
  font: 600 10px/1 var(--mono);
  letter-spacing: .06em;
  color: var(--rc, var(--rc-global));
  text-shadow: 0 0 7px currentColor;
}
.card h3 {
  font: 600 clamp(17px, 4.5vw, 20px)/1.32 var(--mono);
  letter-spacing: 0;
  color: var(--ink);
  text-shadow: 0 0 9px rgba(139,245,173,.30);
  margin: 0 0 11px;
}
/* Region color + code label pulled from data-region on the <article> */
.card[data-region="US"]       { --rc: var(--rc-us); }
.card[data-region="AMERICAS"] { --rc: var(--rc-americas); }
.card[data-region="ASIA"]     { --rc: var(--rc-asia); }
.card[data-region="MENA"]     { --rc: var(--rc-mena); }
.card[data-region="EUROPE"]   { --rc: var(--rc-europe); }
__NATIONAL_REGIONS__.card[data-region="GLOBAL"]   { --rc: var(--rc-global); }

.card .why {
  font: 400 12.5px/1.6 var(--mono);
  color: var(--dim);
  margin: 0 0 11px;
  padding: 9px 11px;
  border: 1px solid var(--line2);
  border-left: 2px solid var(--amber);
  border-radius: 4px;
  background: rgba(255,207,74,.045);
}
.card .why strong {
  color: var(--amber);
  font-weight: 700;
  letter-spacing: .04em;
}
.card .body {
  font: 400 13px/1.85 var(--mono);
  letter-spacing: .005em;
  color: var(--dim);
}

/* Source chips + topic pills */
.chips, .pills { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 11px; }
.chip a {
  display: inline-block;
  font: 500 10.5px/1 var(--mono);
  text-decoration: none;
  color: var(--acc);
  border: 1px solid var(--line2);
  border-radius: 3px;
  padding: 5px 9px;
}
.chip a::before { content: "[ "; color: var(--faint); }
.chip a::after  { content: " \\2197 ]"; color: var(--faint); }
.pill {
  font: 500 10px/1 var(--mono);
  text-transform: none;
  letter-spacing: 0;
  color: var(--faint);
  border: 0;
  padding: 0;
}
.pill::before { content: "#"; }

/* ---- Markets board -------------------------------------------------- */
table.markets {
  width: 100%;
  border-collapse: collapse;
  font-variant-numeric: tabular-nums;
  background: var(--panel);
  border: 1px solid var(--line2);
  border-radius: 5px;
  overflow: hidden;
}
table.markets th {
  text-align: left;
  font: 600 9px/1 var(--mono);
  text-transform: uppercase;
  letter-spacing: .1em;
  color: var(--faint);
  padding: 8px 12px;
  border-bottom: 1px solid var(--line2);
}
table.markets th:not(:first-child) { text-align: right; }
table.markets td {
  padding: 7px 12px;
  border-bottom: 1px solid var(--line);
  font: 400 12.5px/1 var(--mono);
  color: var(--dim);
}
table.markets tr:last-child td { border-bottom: none; }
tr.group td {
  background: transparent;
  font: 600 9px/1.4 var(--mono);
  text-transform: uppercase;
  letter-spacing: .14em;
  color: var(--faint);
  padding: 6px 12px;
}
tr.group td::before { content: "\\2500\\2500 "; }   /* ── */
td.instrument { color: var(--ink); font-weight: 500; }
td.level, td.delta, td.asof { text-align: right; }
td.asof { color: var(--faint); font-size: 10.5px; }
td.asof .basis, td.asof .stale { color: var(--faint); font-style: normal; }
td.delta { font-weight: 600; }
td.delta.pos  { color: var(--up); }
td.delta.neg  { color: var(--dn); }
td.delta.flat { color: var(--dim); }
tr.mover td { background: rgba(166,255,196,.07); }
tr.mover td.instrument::after { content: "  \\2605"; color: var(--amber); }

/* Scoreboard desk-read + world→markets prose (plain <p> and p.note in #finance) */
#finance > p { font: 400 12px/1.72 var(--mono); color: var(--dim); margin: 8px 0 9px; }
.note { font: 400 11.5px/1.6 var(--mono); font-style: normal; color: var(--dim); margin-bottom: 8px; }

/* The finance pass's read of the scoreboard: computed contradiction, its
   precedent, and the watch-items with their current values. */
.scoreboard-read { border-left: 2px solid var(--line2); padding-left: 10px; margin: 10px 0 12px; }
.scoreboard-read p { margin: 0 0 6px; }
.sb-contra { font-weight: 600; }
.sb-watch { list-style: none; padding: 0; margin: 6px 0 0; }
.sb-watch li { font: 400 11.5px/1.6 var(--mono); color: var(--dim); }
.sb-watch li::before { content: "watch: "; color: var(--faint); }

/* ---- Post-Earnings cards (article.card.pe in #finance) -------------- */
.card.pe {
  border: 1px solid var(--line2);
  border-top: 1px solid var(--line2);
  border-radius: 6px;
  background: var(--panel);
  padding: 12px 13px;
  margin-bottom: 11px;
}
.card.pe > h3 {
  font: 600 13px/1.2 var(--mono);
  color: var(--ink);
  text-shadow: none;
  margin: 0 0 8px;
}
.card.pe .ticker { color: var(--faint); font-size: 11px; font-weight: 500; }
.card.pe .pe-headline { font: 600 12px/1.5 var(--mono); color: var(--ink); margin: 0 0 7px; }
.card.pe .body { font: 400 12px/1.7 var(--mono); color: var(--dim); }
.pe-chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 9px; }
.pechip {
  font: 400 10.5px/1.3 var(--mono);
  color: var(--ink);
  border: 1px solid var(--line2);
  border-radius: 3px;
  padding: 3px 7px;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.pechip .lbl, .pe-guidance .lbl { color: var(--faint); }
.pechip.pos  { color: var(--up); }
.pechip.neg  { color: var(--dn); }
.pechip.flat { color: var(--dim); }
.pe-guidance { font: 400 12px/1.6 var(--mono); color: var(--dim); }
.pe-also { font: 400 11px/1.65 var(--mono); color: var(--faint); margin-top: 10px; }
.pe-also::before { content: "> also reported \\2014 "; color: var(--dim); }

ul.rows, ul.threads, ul.radar { margin: 0; padding: 0; list-style: none; }
ul.rows li {
  font: 400 11.5px/1.5 var(--mono);
  color: var(--ink);
  padding: 3px 0;
}
ul.rows li a { color: var(--ink); }

details.more { margin: 6px 0 0; }
details.more summary {
  cursor: pointer;
  font: 500 11px/1 var(--mono);
  color: var(--faint);
  padding: 6px 0;
  list-style: none;
}
details.more summary::before { content: "+ "; }

/* ---- Signal (signal-of-the-day + also-buried) ----------------------- */
/* Signal of the day — highlighted card with gold left border */
.card.signal-top {
  border: 1px solid var(--line2);
  border-left: 3px solid var(--amber);
  border-radius: 6px;
  background: rgba(255,207,74,.05);
  padding: 12px 14px;
  margin: 0 0 4px;
}
.card.signal-top > h3 { margin: 0 0 8px; }
.card.signal-top > h3::before { content: none; }   /* no number/region */
.card.signal-top > h3 a { font: 600 13.5px/1.4 var(--mono); color: var(--acc); }
.card.signal-top .body { font: 400 12.5px/1.7 var(--mono); color: var(--dim); }
/* Also-buried list items (ul.rows > li with a leading link) */
#signal > ul.rows > li {
  font: 400 11.5px/1.55 var(--mono);
  color: var(--faint);
  padding: 8px 0;
  border-top: 1px solid var(--line);
}
#signal > ul.rows > li::before { content: "+ "; color: var(--acc); font-weight: 600; }
#signal > ul.rows > li a { color: var(--ink); font-weight: 600; }

/* ---- Threads -------------------------------------------------------- */
ul.threads li { font: 400 11.5px/1.6 var(--mono); color: var(--faint); padding: 7px 0; }
ul.threads li::before { content: "\\21B3 "; color: var(--faint); }  /* ↳ */
.slug { color: var(--acc); font-weight: 600; }
.since { color: var(--faint); font-size: 10.5px; }

/* ---- On the Radar --------------------------------------------------- */
ul.radar li {
  font: 400 12.5px/1.6 var(--mono);
  color: var(--dim);
  padding: 10px 0;
  border-top: 1px solid var(--line);
}
ul.radar li::before { content: "> "; color: var(--acc); font-weight: 600; }

/* ---- Footer --------------------------------------------------------- */
footer.diagnostics {
  margin-top: 26px;
  padding-top: 13px;
  border-top: 1px dashed var(--line2);
  color: var(--faint);
  font: 400 10px/1.6 var(--mono);
}
footer.diagnostics p::before { content: "> "; }
footer.diagnostics::after {
  content: "";
  display: inline-block;
  width: 7px; height: 12px;
  margin-left: 6px;
  vertical-align: -1px;
  background: var(--faint);
  animation: curse 1.1s steps(1) infinite;
}

a { color: var(--acc); }
a:hover { color: var(--ink); }

/* ---- Links / anchors kept subtle ------------------------------------ */
@media (max-width: 480px) {
  .wrap { padding: 22px 16px 52px; }
  .pe-chips { gap: 8px; }
  .pechip { padding: 5px 9px; font-size: 11px; }
}

/* ---- Renderer additions (not in the handoff CSS) ---------------------
   Classes this renderer emits that phosphor_style.css leaves unstyled;
   without these they'd fall back to the 16px browser default and break
   the type scale. Sized to the design's metadata/body tokens. */
.muted { font: 400 11.5px/1.6 var(--mono); color: var(--faint); }
.card.pe .pe-company { font: 400 11.5px/1.6 var(--mono); color: var(--dim); margin: 0 0 8px; }
.card.pe .pe-context { font: 400 10.5px/1.5 var(--mono); color: var(--faint); margin: 0 0 9px; }
.pe-sec { font: 400 10.5px/1.5 var(--mono); color: var(--faint); margin: 0 0 9px; }
/* Filing follow-ups: deliberately lighter than a write-up card. */
.pe-fu { border-left: 1px solid var(--line); padding-left: 12px; margin: 0 0 16px; }
.pe-fu-name { color: var(--dim); letter-spacing: .04em; text-transform: uppercase;
  font: 400 11px/1.6 var(--mono); }
.pe-fu-head { margin: 0 0 4px; }
.pe-fu-recall { margin: 0 0 6px; }
.pe-fu-headline { font: 400 12px/1.6 var(--mono); color: var(--dim); margin: 0 0 6px; }
.pe-readthrough { font: 400 12px/1.6 var(--mono); color: var(--dim); }
.pe-readthrough .lbl { color: var(--faint); }
.muted.noise { margin-top: 12px; }
.markets td.instrument .ctx {
  display: block;
  font: 400 9.5px/1.5 var(--mono);
  color: var(--faint);
  letter-spacing: .02em;
}

/* ---- Anomaly-panel flags -------------------------------------------------
   Colour is bound to the panel's own `direction` field (fav / unfav / neu);
   there is no styling path that reads the statement text. */
.flagchips { display: flex; flex-direction: column; gap: 5px; margin: 0 0 9px; }
.flagchip {
  font: 400 10.5px/1.45 var(--mono);
  color: var(--dim);
  border-left: 2px solid var(--faint);
  padding: 2px 0 2px 8px;
  font-variant-numeric: tabular-nums;
}
.flagchip .op {
  color: var(--faint);
  font-weight: 600;
  letter-spacing: .08em;
  font-size: 9px;
}
.flagchip.fav   { border-left-color: var(--up); color: var(--ink); }
.flagchip.unfav { border-left-color: var(--dn); color: var(--ink); }
.flagchip.neu   { border-left-color: var(--line2); }

/* ---- Segment ratios (no magnitudes; see the UNIT RULE in the renderer) --- */
table.segments {
  width: 100%;
  border-collapse: collapse;
  font-variant-numeric: tabular-nums;
  background: var(--panel);
  border: 1px solid var(--line2);
  border-radius: 5px;
  overflow: hidden;
  margin: 0 0 6px;
}
table.segments th {
  text-align: left;
  font: 600 9px/1 var(--mono);
  text-transform: uppercase;
  letter-spacing: .1em;
  color: var(--faint);
  padding: 7px 10px;
  border-bottom: 1px solid var(--line2);
}
table.segments th:not(:first-child) { text-align: right; }
table.segments td {
  padding: 6px 10px;
  border-bottom: 1px solid var(--line);
  font: 400 11.5px/1.35 var(--mono);
  color: var(--dim);
}
table.segments tr:last-child td { border-bottom: none; }
table.segments td.segment { color: var(--ink); font-weight: 500; }
table.segments td.segshare, table.segments td.segmargin { text-align: right; }
table.segments .ctx { color: var(--faint); font-size: 10px; }
.seg-note { margin: 0 0 9px; }

/* ---- Follow-up filing extensions ---------------------------------------- */
.pe-fu-guide { font: 400 11.5px/1.6 var(--mono); color: var(--dim); margin: 0 0 6px; }
.pe-fu-guide .lbl { color: var(--faint); }
.pe-fu-guide.pos { color: var(--up); }
.pe-fu-guide.neg { color: var(--dn); }
.pe-fu-risk { margin: 0 0 6px; }
.pe-fu-risk .lbl { color: var(--amber); font-weight: 700; }
.pe-fu-read { font: 400 12px/1.7 var(--mono); color: var(--dim); }
.fu-quote {
  border-left: 1px dotted var(--line2);
  padding-left: 10px;
  margin: 0 0 6px;
  color: var(--faint);
}

/* ---- Two-tab chrome ------------------------------------------------------
   No JavaScript: hidden radios + `:checked ~` sibling selectors. The inputs
   are visually removed but NOT display:none — a display:none input is not
   focusable, and the tabs must stay reachable from the keyboard. */
.tabin {
  position: absolute;
  width: 1px; height: 1px;
  margin: 0; padding: 0;
  opacity: 0;
}
.tabbar {
  position: sticky;
  top: 0;
  z-index: 30;
  display: flex;
  gap: 4px;
  margin: 14px 0 4px;
  padding: 6px 0 0;
  background: var(--bg);
  border-bottom: 1px dashed var(--line2);
}
.tablabel {
  cursor: pointer;
  font: 600 10px/1 var(--mono);
  text-transform: uppercase;
  letter-spacing: .14em;
  color: var(--faint);
  padding: 9px 11px;
  border: 1px solid transparent;
  border-bottom: 2px solid transparent;
  white-space: nowrap;
}
.tablabel .tabcount {
  color: var(--faint);
  font-weight: 400;
  letter-spacing: .04em;
}
.tablabel .tabcount::before { content: "\\00B7 "; }

.panel { display: none; }

/* One rule pair per tab index. TAB_MAX in the renderer is exactly this many;
   a seventh tab needs its pair added here or it can never be shown. */
#tab-1:checked ~ #panel-1,
#tab-2:checked ~ #panel-2,
#tab-3:checked ~ #panel-3,
#tab-4:checked ~ #panel-4,
#tab-5:checked ~ #panel-5,
#tab-6:checked ~ #panel-6 { display: block; }

#tab-1:checked ~ .tabbar .tablabel[for="tab-1"],
#tab-2:checked ~ .tabbar .tablabel[for="tab-2"],
#tab-3:checked ~ .tabbar .tablabel[for="tab-3"],
#tab-4:checked ~ .tabbar .tablabel[for="tab-4"],
#tab-5:checked ~ .tabbar .tablabel[for="tab-5"],
#tab-6:checked ~ .tabbar .tablabel[for="tab-6"] {
  color: var(--acc);
  border-bottom-color: var(--acc);
  text-shadow: 0 0 7px currentColor;
}
/* The inputs are off-screen, so keyboard focus has to be shown on the bar. */
.tabin:focus-visible ~ .tabbar { outline: 1px dotted var(--faint); }

/* The first section inside a panel keeps the page's own top rhythm. */
.panel > section:first-child { margin-top: 18px; }
"""


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def _style_for_desks(nationals, panel_count):
    # Keep the established colour comments and spacing for the shipped palette.
    # Colour values and identifiers themselves come only from the caller.
    comments = {"oklch(0.72 0.17 20)": "/* #f2607a crimson */",
                "oklch(0.78 0.15 350)": "/* magenta         */"}
    colours, selectors, regions = [], [], []
    for desk in nationals:
        did, colour = desk["id"], desk["tab"]["colour"]
        line = f"  --rc-{did}:".ljust(17) + colour + ";"
        comment = comments.get(colour)
        colours.append((line.ljust(40) + comment if comment else line) + "\n")
        selectors.append(f"#{did} .card::after,\n")
        regions.append(f'.card[data-region="{did.upper()}"]'.ljust(30)
                       + f"{{ --rc: var(--rc-{did}); }}\n")
    style = (_STYLE.replace("__NATIONAL_COLOURS__", "".join(colours))
             .replace("__NATIONAL_SELECTORS__", "".join(selectors))
             .replace("__NATIONAL_REGIONS__", "".join(regions)))
    # Existing six pairs retain their bytes. Additional configured panels get
    # complete visibility and selected-label rules without truncating the tabs.
    for i in range(7, panel_count + 1):
        style += (f'\n#tab-{i}:checked ~ #panel-{i} {{ display: block; }}\n'
                  f'#tab-{i}:checked ~ .tabbar .tablabel[for="tab-{i}"] {{\n'
                  '  color: var(--acc);\n'
                  '  border-bottom-color: var(--acc);\n'
                  '  text-shadow: 0 0 7px currentColor;\n}\n')
    return style


def _coverage_gap_notice(analysis: dict) -> str:
    """Show bounded coverage gaps outside article cards and tab panels."""
    reasons = {
        "no selected tier-1/2 source with substantive content":
            "A substantive report from a reliable selected source was unavailable.",
        "selected source has fewer than 100 excerpt words":
            "The available source text was too short for a substantive excerpt.",
    }
    notices = []
    limit = _as_dict(analysis.get("coverage_audit")).get("max_events", 2)
    limit = limit if isinstance(limit, int) and 0 <= limit <= 3 else 2
    if not limit:
        return ""
    for value in _as_list(_as_dict(analysis.get("coverage_audit")).get("events")):
        event = _as_dict(value)
        if event.get("status") != "omitted":
            continue
        title = _clean(event.get("title"))[:240] or "Major event"
        reason = _clean(event.get("reason"))[:320]
        reason = reasons.get(reason, reason or "Substantive source coverage was unavailable.")
        notices.append('<aside role="status" class="card"><p>Coverage gap: '
                       + _esc(title) + ' — ' + _esc(reason) + '</p></aside>')
        if len(notices) == limit:
            break
    return "\n".join(notices)


def render_html(analysis: dict, bundle: dict, date: str, *, desks=(), timezone_label="JST") -> str:
    """Render a complete, self-contained HTML briefing document. Pure; never raises
    on missing/None/mis-typed fields."""
    analysis = _as_dict(analysis)
    bundle = _as_dict(bundle)
    date = _clean(date)

    briefing = _as_dict(analysis.get("briefing"))
    brief = _as_list(briefing.get("brief"))

    # Header subtitle: first brief headline as lede, else a static tagline.
    lede = ""
    if brief:
        lede = _clean(_as_dict(brief[0]).get("headline"))
    if not lede:
        lede = "Your morning read on what moved the world and the markets."

    nationals = [d for d in desks if d.get("kind") == "national"]
    status = _as_dict(analysis.get("analysis_status"))
    outcomes = _as_dict(status.get("passes"))
    world_unavailable = status and not _as_dict(outcomes.get("world")).get("success")
    notice = ""
    if status.get("overall") in ("partial", "failed"):
        unavailable = [label for name, label in (("world", "World News"), ("finance", "Finance"))
                       if not _as_dict(outcomes.get(name)).get("success")]
        notice = ('<aside role="status" class="card"><strong>Degraded briefing</strong><p>'
                  + _esc(" and ".join(unavailable)) + ' analysis unavailable. '
                  'Available reporting and source data are shown below.</p></aside>')

    # Three tabs (spec §8.1). World News is default-checked; Japan sits between
    # the world and the markets; Finance carries the whole finance desk.
    # Diagnostics stay in the footer, outside every panel, because a data-gap
    # notice belongs to the RUN, not to any one desk.
    world = f"""<section id="brief">
<h2 class="section">The Brief</h2>
{_brief_section(brief)}
</section>

{_signal_section(briefing)}
<section id="threads">
<h2 class="section">Threads</h2>
{_threads_section(briefing.get("threads"))}
</section>

<section id="on-radar">
<h2 class="section">On the Radar</h2>
{_radar_section(briefing.get("on_radar"))}
</section>"""

    panels = [{"id": "world", "label": "World News", "count": len(_as_list(brief)),
               "html": world}]
    for desk in sorted(nationals, key=lambda d: d["tab"]["order"]):
        stories = _as_list(briefing.get(desk["id"]))
        panels.append({"id": desk["id"], "label": desk["label"],
                       "count": len(stories), "html": (
                           '<p class="muted">' + _esc(desk["label"]) + ' coverage unavailable: '
                           'country reporting could not be prepared for this briefing.</p>'
                           if world_unavailable else _desk_section(desk, stories))})
    panels.append({"id": "finance", "label": "Finance",
                   "count": _finance_count(briefing, bundle),
                   "html": _finance_section(briefing, bundle, date)})
    tabs = _tabs(panels)
    style = _style_for_desks(nationals, len(panels))

    body = f"""<div class="wrap">
<header class="masthead">
<h1>World Intelligence</h1>
<div class="date">{_esc(date)} &middot; {_esc(timezone_label)}</div>
<p class="lede">{_esc(lede)}</p>
</header>

{notice}
{_coverage_gap_notice(analysis)}
{tabs}
<footer class="diagnostics">
{_diagnostics(bundle, date, timezone_label)}
</footer>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>World Intelligence &middot; {_esc(date)}</title>
<style>{style}</style>
</head>
<body>
{body}
</body>
</html>
"""
