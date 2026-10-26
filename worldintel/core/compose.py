"""world-intel Module D — compose the layered briefing text.

Pure formatter: takes the fetched BUNDLE (authoritative numbers) and the Codex
analysis RESULT (judgment/narrative) and renders the Telegram briefing string.
Numbers come from the bundle (deterministic); prose comes from the analysis.

IMPORTANT — encoding: the Hermes cron `no_agent` script runner captures stdout with
`text=True`, which on Windows decodes as cp1252. Any character outside cp1252 (emoji,
box-drawing bars, ▲/▼/→ arrows) makes subprocess's reader thread raise UnicodeDecodeError
and return EMPTY stdout (silent delivery). So this module deliberately uses only
cp1252-safe characters (ASCII plus bullet •, dashes — –, middot ·, guillemet »). Do not
reintroduce emoji or arrow glyphs here.
"""

from __future__ import annotations

_SCOREBOARD = [
    ("indices", "Indices", [
        ("sp500", "S&P"), ("nasdaq", "Nasdaq"), ("nikkei", "Nikkei"),
        ("kospi", "KOSPI"), ("hangseng", "Hang Seng"), ("stoxx600", "STOXX600"),
        ("dax", "DAX"),
    ]),
    ("fx", "FX", [("usdjpy", "USDJPY"), ("usdkrw", "USDKRW"), ("eurusd", "EURUSD")]),
    ("rates", "Rates", [("us10y", "US10Y")]),
    ("commodities", "Commod", [("brent", "Brent"), ("wti", "WTI"), ("gold", "Gold")]),
    ("risk", "Risk", [("vix", "VIX")]),
]


def _sign(pct) -> str:
    if pct is None:
        return ""
    if pct > 0:
        return "+"
    if pct < 0:
        return "-"
    return "~"


def _fmt_quote(label: str, q) -> str | None:
    if not isinstance(q, dict):
        return None
    lvl = q.get("level")
    ch = q.get("change_pct")
    if lvl is None and ch is None:
        return None
    out = label
    if lvl is not None:
        try:
            out += f" {lvl:g}"
        except (ValueError, TypeError):
            out += f" {lvl}"
    if ch is not None:
        try:
            out += f" {_sign(ch)}{abs(ch):.2f}%"
        except (ValueError, TypeError):
            pass
    return out


def _scoreboard_lines(markets: dict) -> list:
    lines = []
    markets = markets or {}
    for key, header, fields in _SCOREBOARD:
        cat = markets.get(key) or {}
        toks = [t for sym, label in fields for t in [_fmt_quote(label, cat.get(sym))] if t]
        if toks:
            lines.append(f"{header}: " + " · ".join(toks))  # middot separator (cp1252)
    return lines


def _clean(s) -> str:
    return (s or "").strip() if isinstance(s, str) else ""


def _h(title: str) -> str:
    """Section header — guillemet marker is cp1252-safe."""
    return f"» {title}"  # »


def compose(bundle: dict, result: dict, date: str) -> str:
    bundle = bundle or {}
    briefing = (result or {}).get("briefing") or {}
    out = [f"WORLD INTELLIGENCE — {date} (JST)", ""]  # em dash (cp1252)

    top = briefing.get("top") or []
    if top:
        out.append(_h("TOP"))
        for t in top:
            hd, w = _clean(t.get("headline")), _clean(t.get("why"))
            out.append(f"• {hd}" + (f" — {w}" if w else ""))  # bullet, em dash
        out.append("")

    sb = _scoreboard_lines(bundle.get("markets"))
    markets = briefing.get("markets") or {}
    trans = _clean(markets.get("transmission"))
    if sb or trans:
        out.append(_h("MARKETS"))
        out.extend(sb)
        if trans:
            out.append(f"Transmission: {trans}")
        out.append("")

    svn = briefing.get("signal_vs_noise") or {}
    noise, buried = svn.get("noise") or [], svn.get("buried") or []
    if noise or buried:
        out.append(_h("SIGNAL vs NOISE"))
        for n in noise:
            out.append(f"[noise]  {_clean(n.get('story'))} — {_clean(n.get('reason'))}")
        for b in buried:
            out.append(f"[buried] {_clean(b.get('story'))} — {_clean(b.get('reason'))}")
        out.append("")

    threads = briefing.get("threads") or []
    if threads:
        out.append(_h("THREADS"))
        for th in threads:
            slug, upd, conn = _clean(th.get("slug")), _clean(th.get("update")), _clean(th.get("connects_to"))
            tail = f"  (connects to {conn})" if conn else ""
            head = f"{slug}: " if slug else ""
            out.append(f"• {head}{upd}{tail}")
        out.append("")

    radar = briefing.get("on_radar") or []
    if radar:
        out.append(_h("ON THE RADAR"))
        for r in radar:
            out.append(f"• {_clean(r)}")
        out.append("")

    health = bundle.get("source_health") or {}
    failed = sorted(s for s, v in health.items() if not (isinstance(v, dict) and v.get("ok")))
    if failed:
        out.append(f"— data gaps: {', '.join(failed)}")

    return "\n".join(out).strip()
