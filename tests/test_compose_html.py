"""Tests for Module D' (scripts/compose_html.py).

Pure renderer, no network. Builds a representative NEW-shape analysis + bundle and
asserts document structure, deterministic facts (markets/filings), escaping, the
biggest-mover highlight, and defensive behaviour on empty/missing input.
"""

import html
import json
import os
import re
import sys

# Make scripts/ importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from worldintel.core import compose_html as ch  # noqa: E402
from worldintel import config


def render_profile(*args, **kwargs):
    kwargs.setdefault("desks", config.get().desks)
    return ch.render_html(*args, **kwargs)


def japan_section(stories):
    desk = next(d for d in config.get().national_desks if d["id"] == "japan")
    return ch._desk_section(desk, stories)


def _doc_body(out):
    """Everything after the <style> block — the phosphor CSS carries comments
    ('Post-Earnings cards', 'Signal of the day', …) that would collide with
    absence assertions run against the whole document."""
    return out.split("</style>", 1)[-1]


def sample_analysis():
    return {
        "briefing": {
            "brief": [
                {
                    "headline": "Central banks hold the line",
                    "why_it_matters": "Rate path drives every asset class into H2.",
                    "body": "Policymakers signalled patience " * 12,
                    "sources": [
                        {"outlet": "Reuters", "url": "https://reuters.com/a"},
                        {"outlet": "Guardian", "url": "https://guardian.com/b"},
                    ],
                    "tags": ["macro", "rates"],
                },
                {
                    # Story with NO sources -> must produce no chips.
                    "headline": "Quiet story <script>alert(1)</script>",
                    "why_it_matters": "Under-covered but consequential.",
                    "body": "A short body about a buried development.",
                    "sources": [],
                    "tags": ["geopolitics"],
                },
            ],
            "finance": {
                "brief": [
                    {
                        "headline": "Chipmaker guidance lifts semis",
                        "why_it_matters": "Capex read-through for the whole supply chain.",
                        "body": "Guidance beat expectations across the board.",
                        "sources": [{"outlet": "FT", "url": "https://ft.com/c"}],
                        "tags": ["semis"],
                    },
                ],
                "scoreboard_note": "The gold slide is the only real move; rates are noise.",
                "filings_note": "Notable registration and 8-K activity:",
                "earnings_note": "Bellwethers reporting this week:",
                "ownership_note": "",
            },
            "signal": {
                "top": {"story": "Quiet supply-chain shift",
                        "reason": "Reroutes 5% of trade flows through one chokepoint. "
                                  "Nobody upstream has repriced it.",
                        "url": "https://example.com/buried"},
                "buried": [
                    {"story": "Obscure rule change",
                     "reason": "Alters bank capital treatment.",
                     "url": "https://example.com/rule"},
                ],
                "noise_line": "Ignored today: celebrity feud, viral stunt.",
            },
            "threads": [
                {"slug": "boj-normalization", "today": "Governor hints at July hike.",
                 "since": "2026-04-01"},
            ],
            "on_radar": ["US CPI print Thursday", "OPEC+ meeting"],
        },
        "kb_deltas": {},
        "source_notes": [],
    }


def sample_bundle():
    return {
        "date": "2026-07-02",
        "markets": {
            "indices": {
                "sp500": {"level": 5432.1, "change_pct": 0.42, "asof": "2026-07-01"},
                "nasdaq": {"level": 17888.0, "change_pct": -0.15, "asof": "2026-07-01"},
                "nikkei": {"level": 39500.0, "change_pct": 1.20, "asof": "2026-07-02"},
            },
            "fx": {
                "usdjpy": {"level": 161.25, "change_pct": -0.30, "asof": "2026-07-02"},
            },
            "rates": {"us10y": {"level": 4.485, "change_pct": 2.58,
                                "change_abs": 0.113, "asof": "2026-07-01",
                                "asof_prev": "2026-06-30"}},
            "commodities": {
                # Biggest absolute mover: gold at -3.10%.
                "gold": {"level": 2410.0, "change_pct": -3.10, "asof": "2026-07-01"},
            },
            "risk": {"vix": {"level": 13.2, "change_pct": 2.50, "asof": "2026-07-01"}},
        },
        "filings": [
            {"company": "Acme Corp", "form": "S-1", "filed": "2026-07-01",
             "url": "https://sec.gov/acme", "cik": "0001"},
            {"company": "NoLink Inc", "form": "8-K", "filed": "2026-07-01",
             "url": "", "cik": "0002"},
        ],
        "earnings_calendar": [
            {"ticker": "AAPL", "name": "Apple Inc.", "date": "2026-07-05"},
            {"ticker": "MSFT", "name": "Microsoft", "date": "2026-07-06"},
        ],
        "source_health": {
            "newsdata": {"ok": True, "note": ""},
            "guardian": {"ok": False, "note": "no api key"},
            "fred": {"ok": False, "note": "timeout"},
        },
    }


def test_document_shell_and_sections():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    assert out.startswith("<!DOCTYPE html>")
    assert "<html" in out
    assert "<meta charset=\"utf-8\">" in out
    for heading in ("The Brief", "Finance", "Scoreboard", "Filings",
                    "Threads", "On the Radar"):
        assert heading in out
    assert '<h2 class="section">Signal</h2>' in out
    # The old top-level sections are gone.
    assert "Signal vs Noise" not in out
    assert '<h2 class="section">Markets</h2>' not in out


def test_market_number_and_biggest_mover():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    # A known bundle number appears in the table.
    assert "5432.1" in out
    assert "161.25" in out
    # Biggest |Δ%| mover is gold (-3.10%): its row carries the mover class.
    mover_idx = out.find('class="mover"')
    assert mover_idx != -1
    row_tail = out[mover_idx:mover_idx + 200]
    assert "Gold" in row_tail
    # And a non-mover instrument row is not marked.
    assert out.count('class="mover"') == 1


def test_filing_links_and_urlless_plaintext():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    assert '<a href="https://sec.gov/acme"' in out
    # The url-less filing renders as text, never as an anchor to it.
    assert "NoLink Inc" in out
    assert 'href="">NoLink' not in out
    assert ">NoLink Inc<" not in out or 'href' not in out[out.find("NoLink Inc") - 40:out.find("NoLink Inc")]


def test_empty_sources_yields_no_chip():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    # Exactly three chips (two from the first brief story, one from the finance
    # brief story); the second brief story with empty sources adds none.
    assert out.count('class="chip"') == 3


def test_injected_script_is_escaped():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out


def test_earnings_rows_present():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    assert "AAPL" in out and "Apple Inc." in out
    assert "2026-07-05" in out


def test_diagnostics_lists_failed_sources():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    assert "data gaps" in out
    assert "fred" in out and "guardian" in out
    assert "newsdata" not in out.split("diagnostics")[-1]


def test_empty_input_does_not_raise():
    out = render_profile({}, {}, "")
    assert "<html" in out
    assert out.startswith("<!DOCTYPE html>")


def test_missing_and_wrong_types_do_not_raise():
    weird_analysis = {"briefing": {"brief": "not-a-list", "finance": None,
                                   "signal": [], "threads": 5,
                                   "on_radar": None}}
    weird_bundle = {"markets": "nope", "filings": None,
                    "earnings_calendar": {}, "source_health": None}
    out = render_profile(weird_analysis, weird_bundle, "2026-07-02")
    assert "<html" in out


def test_rates_render_basis_points_neutral():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    # us10y change_abs = 0.113 -> round(0.113*100) = 11 bp (not the 2.58% of the
    # yield's own percent-change, which is meaningless for a yield).
    assert "+11 bp" in out
    # Scope to the US 10Y row: the raw %-formatted yield change must NOT appear
    # there, and the delta must be neutral (flat), never coloured pos/neg.
    i = out.find("US 10Y")
    assert i != -1
    row = out[i:i + 200]
    assert "+11 bp" in row
    assert "2.58%" not in row
    assert 'delta flat' in row
    assert 'delta pos' not in row and 'delta neg' not in row
    # The meaningless percent form of the yield change is nowhere in the doc.
    assert "2.58%" not in out


def test_us_30y_renders_in_curve_order_and_in_basis_points():
    """The long end joins the board between the 10Y and the JGBs.

    ^TYX quotes a yield in percent units like ^TNX (5.265 on 2026-08-14, above
    the 10Y's 4.696 and the 5Y's 4.362), so `change_abs` is a yield delta and the
    bp conversion holds. A ticker that silently resolved to a bond ETF would
    still render a plausible-looking bp figure, which is why the level matters as
    much as the row."""
    bundle = sample_bundle()
    bundle["markets"]["rates"]["us30y"] = {
        "level": 5.265, "change_abs": 0.05, "asof": "2026-07-01"}
    out = render_profile(sample_analysis(), bundle, "2026-07-02")

    i = out.find("US 30Y")
    assert i != -1
    row = out[i:i + 200]
    assert "5.265" in row and "+5 bp" in row
    assert "delta flat" in row          # a rising yield is not good or bad news
    # Curve order: 5Y, 10Y, 30Y — the long end reads after the belly, not before.
    assert out.find("US 5Y") < out.find("US 10Y") < i


def test_rates_none_change_abs_renders_dash():
    analysis = sample_analysis()
    bundle = sample_bundle()
    bundle["markets"]["rates"]["us10y"] = {
        "level": 4.35, "change_pct": 1.10, "change_abs": None, "asof": "2026-07-01",
    }
    out = render_profile(analysis, bundle, "2026-07-02")
    i = out.find("US 10Y")
    assert i != -1
    row = out[i:i + 200]
    assert "—" in row
    assert "bp" not in row


def test_biggest_mover_excludes_rates():
    analysis = sample_analysis()
    bundle = sample_bundle()
    # us10y gets the largest abs(change_pct) of the whole board...
    bundle["markets"]["rates"]["us10y"] = {
        "level": 4.60, "change_pct": 9.99, "change_abs": 0.25, "asof": "2026-07-01",
    }
    # ...but among non-rates the S&P is the biggest; shrink gold so sp500 wins.
    bundle["markets"]["commodities"]["gold"]["change_pct"] = -0.50
    bundle["markets"]["indices"]["sp500"]["change_pct"] = 4.20
    out = render_profile(analysis, bundle, "2026-07-02")
    assert out.count('class="mover"') == 1
    mover_idx = out.find('class="mover"')
    row_tail = out[mover_idx:mover_idx + 200]
    # Star lands on the index, never the rates row despite its larger change_pct.
    assert "S&amp;P 500" in row_tail
    assert "US 10Y" not in row_tail


def _markets_table_region(out):
    """Slice out just the markets <table> so date assertions don't collide with
    filing/earnings dates elsewhere in the document."""
    start = out.find('<table class="markets">')
    return out[start:out.find("</table>", start)]


# --------------------------------------------------------------------------- #
# Reporting-date column + freshness (business-day age vs the brief date)
# --------------------------------------------------------------------------- #
def test_business_days_old_is_weekend_aware():
    # Mon->Tue = 1; Fri->Mon = 1 (weekend skipped); Fri->Tue = 2; same day = 0.
    assert ch._business_days_old("2026-07-06", "2026-07-07") == 1
    assert ch._business_days_old("2026-07-03", "2026-07-06") == 1
    assert ch._business_days_old("2026-07-03", "2026-07-07") == 2
    assert ch._business_days_old("2026-07-01", "2026-07-01") == 0
    # Unparseable / reversed -> None (never raises).
    assert ch._business_days_old("nope", "2026-07-07") is None
    assert ch._business_days_old("2026-07-07", "2026-07-03") is None


def test_asof_renders_as_friendly_date():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    t = _markets_table_region(out)
    assert "Jul 1" in t              # 2026-07-01 -> "Jul 1"
    assert "2026-07-01" not in t     # raw ISO no longer shown in the table


def test_markets_table_has_date_header():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    assert ">Date<" in _markets_table_region(out)


def test_stale_row_flagged_when_two_business_days_old():
    bundle = sample_bundle()
    # KOSPI stuck at Fri 07-03 on a Tue 07-07 brief = 2 business days old -> stale.
    bundle["markets"]["indices"]["kospi"] = {
        "level": 8088.34, "change_pct": 5.76, "asof": "2026-07-03"}
    out = render_profile(sample_analysis(), bundle, "2026-07-07")
    i = out.find("KOSPI")
    assert "stale" in out[i:i + 300]


def test_fresh_row_not_flagged_on_normal_spread():
    # sp500 asof 07-01 on a 07-02 brief = 1 business day = normal US spread, not stale.
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    i = out.find("S&amp;P 500")
    assert "stale" not in out[i:i + 300]


def test_non_adjacent_change_shows_comparison_basis():
    bundle = sample_bundle()
    # Mon 07-06 vs Thu 07-02 (Fri 07-03 was a holiday) -> % spans a session gap.
    bundle["markets"]["indices"]["sp500"] = {
        "level": 5432.1, "change_pct": 0.72,
        "asof": "2026-07-06", "asof_prev": "2026-07-02"}
    out = render_profile(sample_analysis(), bundle, "2026-07-07")
    i = out.find("S&amp;P 500")
    row = out[i:i + 400]
    assert "Jul 6" in row
    assert "(vs Jul 2)" in row       # the comparison basis is surfaced


def test_adjacent_change_not_annotated():
    bundle = sample_bundle()
    bundle["markets"]["indices"]["sp500"] = {
        "level": 5432.1, "change_pct": 0.10,
        "asof": "2026-07-07", "asof_prev": "2026-07-06"}
    out = render_profile(sample_analysis(), bundle, "2026-07-08")
    i = out.find("S&amp;P 500")
    assert "(vs" not in out[i:i + 400]


def test_biggest_mover_excludes_stale_instruments():
    bundle = sample_bundle()
    # A huge but STALE KOSPI must not win the highlight...
    bundle["markets"]["indices"]["kospi"] = {
        "level": 8000.0, "change_pct": 9.9, "asof": "2026-07-03"}
    # ...over a fresh, smaller Nikkei mover.
    bundle["markets"]["indices"]["nikkei"] = {
        "level": 39500.0, "change_pct": 1.2, "asof": "2026-07-07"}
    # Shrink the other fresh instruments so Nikkei is the biggest fresh mover.
    bundle["markets"]["commodities"]["gold"] = {
        "level": 2410.0, "change_pct": -0.1, "asof": "2026-07-07"}
    bundle["markets"]["fx"]["usdjpy"] = {
        "level": 161.25, "change_pct": -0.1, "asof": "2026-07-07"}
    out = render_profile(sample_analysis(), bundle, "2026-07-07")
    assert out.count('class="mover"') == 1
    # Scope to just the highlighted <tr> (KOSPI is the adjacent row in the group).
    start = out.find('class="mover"')
    mover_row = out[start:out.find("</tr>", start)]
    assert "Nikkei" in mover_row
    assert "KOSPI" not in mover_row


def test_biggest_mover_falls_back_when_all_stale():
    # Brief far in the future -> every instrument is stale; still highlight the
    # single biggest mover rather than nothing.
    out = render_profile(sample_analysis(), sample_bundle(), "2026-08-01")
    assert out.count('class="mover"') == 1


def test_numeric_headers_carry_column_classes_for_alignment():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    # Last/Δ%/Date data cells are right-aligned; their headers must carry the same
    # column class so the CSS right-aligns the headers to sit over their numbers.
    assert '<th class="level">Last</th>' in out
    assert '<th class="delta">&Delta;%</th>' in out
    assert '<th class="asof">Date</th>' in out


def test_asof_column_visible_on_mobile():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    # The as-of/Date column is load-bearing now; it must not be hidden on mobile.
    assert "td.asof { display: none" not in out


def test_no_external_assets():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    # No external stylesheet/script/font/image references.
    assert "src=" not in out
    assert "<link" not in out
    # The only http(s) hrefs are the data-provided article/source/filing links.
    assert "https://reuters.com/a" in out
    assert "https://sec.gov/acme" in out


# --------------------------------------------------------------------------- #
# Hang Seng in the markets tracker
# --------------------------------------------------------------------------- #
def test_hang_seng_row_renders():
    bundle = sample_bundle()
    bundle["markets"]["indices"]["hangseng"] = {
        "level": 19850.5, "change_pct": 0.85, "asof": "2026-07-02"}
    out = render_profile(sample_analysis(), bundle, "2026-07-02")
    t = _markets_table_region(out)
    assert "Hang Seng" in t
    assert "19850.5" in t


# --------------------------------------------------------------------------- #
# Post-Earnings section (Codex write-ups joined to bundle candidates)
# --------------------------------------------------------------------------- #
def sample_pe_candidates():
    return [
        {"ticker": "TSM", "name": "Taiwan Semiconductor", "report_date": "2026-07-16",
         "hour": "amc", "eps_est": 2.03, "eps_act": 2.47, "surprise_pct": 21.7,
         "rev_est": 30100000000.0, "rev_act": 31900000000.0,
         "price_move_pct": 4.2, "release_excerpt": "Record quarter.", "source": "intl",
         "gates": ["size", "surprise"]},
    ]


def sample_pe_briefing():
    return {
        "writeups": [
            {"ticker": "TSM", "headline": "Blowout quarter on AI demand",
             "guidance": "Raised full-year revenue outlook.",
             "read": "The AI capex cycle is intact.",
             "tags": ["semis", "AI"]},
        ],
        "also_reported": [
            {"ticker": "JNJ", "note": "in-line quarter; guidance unchanged"},
        ],
    }


def pe_inputs(post_earnings=None, candidates=None):
    """Analysis + bundle carrying the post-earnings payloads."""
    analysis = sample_analysis()
    if post_earnings is not None:
        analysis["briefing"]["post_earnings"] = post_earnings
    bundle = sample_bundle()
    bundle["post_earnings_candidates"] = (
        candidates if candidates is not None else sample_pe_candidates())
    return analysis, bundle


def test_post_earnings_absent_without_key():
    # No post_earnings in the briefing -> section entirely absent.
    analysis, bundle = pe_inputs(post_earnings=None)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert "Post-Earnings" not in _doc_body(out)


def test_post_earnings_absent_when_writeups_empty_even_with_also_reported():
    pe = sample_pe_briefing()
    pe["writeups"] = []
    analysis, bundle = pe_inputs(post_earnings=pe)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert "Post-Earnings" not in _doc_body(out)
    assert "Also reported" not in out
    assert "JNJ" not in out


def test_post_earnings_writeup_renders_name_headline_and_chips():
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing())
    out = render_profile(analysis, bundle, "2026-07-02")
    assert "Post-Earnings" in out
    assert "Taiwan Semiconductor" in out          # name from the candidate
    assert "Blowout quarter on AI demand" in out  # Codex headline
    # EPS chip: est -> act at 2dp, from the CANDIDATE numbers.
    assert "est 2.03 &rarr; act 2.47" in out
    # Revenue chip: compact billions.
    assert "est 30.1B &rarr; act 31.9B" in out
    # Guidance line with its muted label; read paragraph; tags.
    assert "Guidance:" in out
    assert "Raised full-year revenue outlook." in out
    assert "The AI capex cycle is intact." in out
    assert "semis" in out


def test_post_earnings_surprise_chip_positive_carries_up_class():
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing())
    out = render_profile(analysis, bundle, "2026-07-02")
    i = out.find("+21.7%")
    assert i != -1
    # The enclosing chip carries the existing up/positive class.
    assert "pos" in out[max(0, i - 80):i]


def test_post_earnings_surprise_chip_negative_carries_down_class():
    cands = sample_pe_candidates()
    cands[0]["surprise_pct"] = -8.3
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing(), candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    i = out.find("-8.3%")
    assert i != -1
    assert "neg" in out[max(0, i - 80):i]


def test_post_earnings_price_reaction_arrow_and_color():
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing())
    out = render_profile(analysis, bundle, "2026-07-02")
    i = out.find("▲ +4.2%")
    assert i != -1
    assert "pos" in out[max(0, i - 80):i]
    # Negative reaction flips arrow and class.
    cands = sample_pe_candidates()
    cands[0]["price_move_pct"] = -2.6
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing(), candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    j = out.find("▼ -2.6%")
    assert j != -1
    assert "neg" in out[max(0, j - 80):j]


def test_post_earnings_none_fields_drop_chips_without_crash():
    cands = sample_pe_candidates()
    cands[0].update({"eps_est": None, "eps_act": None, "rev_est": None,
                     "rev_act": None, "surprise_pct": None, "price_move_pct": None})
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing(), candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert "Post-Earnings" in out
    assert "Blowout quarter on AI demand" in out
    # Every chip's inputs are None -> no chip markup at all (the class name in
    # the <style> block doesn't count).
    assert '<span class="pechip' not in out


def test_post_earnings_no_matching_candidate_renders_card_without_chips():
    pe = sample_pe_briefing()
    pe["writeups"][0]["ticker"] = "ZZZZ"  # nothing in the candidate list
    analysis, bundle = pe_inputs(post_earnings=pe)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert "Post-Earnings" in out
    assert "ZZZZ" in out  # falls back to the ticker as the company line
    assert "Blowout quarter on AI demand" in out
    assert '<span class="pechip' not in out


def test_post_earnings_ticker_match_is_case_insensitive():
    pe = sample_pe_briefing()
    pe["writeups"][0]["ticker"] = "tsm"
    analysis, bundle = pe_inputs(post_earnings=pe)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert "Taiwan Semiconductor" in out
    assert "est 2.03 &rarr; act 2.47" in out


def test_post_earnings_guidance_omitted_when_empty():
    pe = sample_pe_briefing()
    pe["writeups"][0]["guidance"] = ""
    analysis, bundle = pe_inputs(post_earnings=pe)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert "Post-Earnings" in out
    assert "Guidance:" not in out


def test_post_earnings_also_reported_line_capped_at_six():
    pe = sample_pe_briefing()
    pe["also_reported"] = [
        {"ticker": f"AR{i}Q", "note": f"note {i}"} for i in range(1, 8)]
    analysis, bundle = pe_inputs(post_earnings=pe)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert "Also reported:" in out
    assert "AR1Q" in out and "AR6Q" in out
    assert "AR7Q" not in out  # capped at 6


def test_post_earnings_writeups_display_capped_at_four():
    pe = sample_pe_briefing()
    pe["writeups"] = [
        {"ticker": f"WU{i}Q", "headline": f"Headline number {i}",
         "guidance": "", "read": "r", "tags": []} for i in range(1, 6)]
    analysis, bundle = pe_inputs(post_earnings=pe)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert "Headline number 4" in out
    assert "Headline number 5" not in out  # capped at 4


def test_post_earnings_malformed_writeup_does_not_raise():
    pe = {"writeups": [{}, {"ticker": "TSM"}, "not-a-dict"], "also_reported": "nope"}
    analysis, bundle = pe_inputs(post_earnings=pe)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert "<html" in out
    assert "Post-Earnings" in out


def test_post_earnings_renders_inside_finance_section():
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing())
    out = render_profile(analysis, bundle, "2026-07-02")
    f = out.find('<section id="finance">')
    p = out.find('id="post-earnings"')
    # Task 8 moved Signal into the other tab, so the old `f < p < s` bound no
    # longer holds; the section's own closing tag is the containment test.
    end = out.find("</section>", f)
    assert -1 < f < p < end
    # Subsection level inside the desk, not a top-level banner.
    assert '<h2 class="section">Post-Earnings</h2>' not in out
    assert ">Post-Earnings</h3>" in out


def test_fmt_compact_revenue_scales():
    assert ch._fmt_compact(31900000000.0) == "31.9B"
    assert ch._fmt_compact(850000000) == "850M"
    assert ch._fmt_compact(None) == "—"


# --------------------------------------------------------------------------- #
# Post-Earnings analyst enrichment (company line, context line, read-through)
# --------------------------------------------------------------------------- #
def sample_pe_candidates_enriched():
    cands = sample_pe_candidates()
    cands[0].update({
        "market_cap_m": 3063.0, "industry": "Specialty Chemicals",
        "sector": "Basic Materials",
        "description": "LEAKDESC business summary that must never render",
        "peers": ["LEAKPEERA", "LEAKPEERB"],
        "beat_streak": [43.1, 6.4, -11.4, 91.5],
        "fwd_eps_q": 9.87, "fwd_eps_fy": 8.76,
        "analyst_count": 4, "revision_30d_pct": 77.7,
    })
    return cands


def sample_pe_briefing_enriched():
    pe = sample_pe_briefing()
    pe["writeups"][0].update({
        "company": "Sole leading-edge foundry with dominant share.",
        "read_through": "Positive read for semicap and AI infrastructure names.",
    })
    return pe


def test_pe_company_line_renders_under_headline():
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing_enriched(),
                                 candidates=sample_pe_candidates_enriched())
    out = render_profile(analysis, bundle, "2026-07-02")
    h = out.find("Blowout quarter on AI demand")
    c = out.find("Sole leading-edge foundry with dominant share.")
    chips = out.find('<div class="pe-chips">')
    assert -1 < h < c < chips
    # Muted, subtitle-style line.
    p_start = out.rfind("<p", 0, c)
    assert "muted" in out[p_start:c]


def test_pe_company_line_omitted_when_empty():
    pe = sample_pe_briefing_enriched()
    pe["writeups"][0]["company"] = ""
    analysis, bundle = pe_inputs(post_earnings=pe,
                                 candidates=sample_pe_candidates_enriched())
    out = render_profile(analysis, bundle, "2026-07-02")
    assert 'class="muted pe-company"' not in out


def test_pe_context_line_full_form_exact():
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing_enriched(),
                                 candidates=sample_pe_candidates_enriched())
    out = render_profile(analysis, bundle, "2026-07-02")
    assert ('<p class="muted pe-context">$3.1B &middot; Specialty Chemicals'
            ' &middot; 4 analysts &middot; streak +43.1/+6.4/-11.4/+91.5%</p>'
            ) in out
    # Sits under the chips row, before the guidance line.
    chips = out.find('<div class="pe-chips">')
    ctx = out.find('class="muted pe-context"')
    guid = out.find("Guidance:")
    assert -1 < chips < ctx < guid


def test_pe_context_line_cap_only():
    cands = sample_pe_candidates()
    cands[0]["market_cap_m"] = 3063.0
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing(),
                                 candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert '<p class="muted pe-context">$3.1B</p>' in out


def test_pe_context_line_missing_cap_no_stray_separators():
    cands = sample_pe_candidates()
    cands[0].update({"industry": "Specialty Chemicals",
                     "beat_streak": [43.1, -11.4]})
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing(),
                                 candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert ('<p class="muted pe-context">Specialty Chemicals &middot; '
            'streak +43.1/-11.4%</p>') in out


def test_pe_context_line_absent_when_no_data():
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing())
    out = render_profile(analysis, bundle, "2026-07-02")
    assert 'class="muted pe-context"' not in out


def test_pe_market_cap_compact_from_millions():
    cands = sample_pe_candidates()
    cands[0]["market_cap_m"] = 850.0
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing(),
                                 candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert '<p class="muted pe-context">$850M</p>' in out


def test_fmt_compact_decimals_kwarg():
    # market_cap_m is in MILLIONS: scale to raw dollars before formatting.
    assert ch._fmt_compact(3063.0 * 1e6, decimals=1) == "3.1B"
    assert ch._fmt_compact(850.0 * 1e6, decimals=1) == "850M"
    # Default path unchanged.
    assert ch._fmt_compact(31900000000.0) == "31.9B"


def test_pe_read_through_labelled_line():
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing_enriched(),
                                 candidates=sample_pe_candidates_enriched())
    out = render_profile(analysis, bundle, "2026-07-02")
    i = out.find("Positive read for semicap and AI infrastructure names.")
    assert i != -1
    # Muted label, same visual pattern as the Guidance: line.
    assert '<span class="lbl">Read-through:</span>' in out[max(0, i - 120):i]
    # After the read paragraph.
    assert -1 < out.find("The AI capex cycle is intact.") < i


def test_pe_read_through_omitted_when_empty():
    pe = sample_pe_briefing_enriched()
    pe["writeups"][0]["read_through"] = ""
    analysis, bundle = pe_inputs(post_earnings=pe,
                                 candidates=sample_pe_candidates_enriched())
    out = render_profile(analysis, bundle, "2026-07-02")
    assert "Read-through:" not in out


def test_pe_old_shape_renders_unchanged():
    # Pre-enrichment writeup + candidate (no new keys anywhere): no raise, no
    # company/context/read-through lines, everything else as before.
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing())
    out = render_profile(analysis, bundle, "2026-07-02")
    assert "Post-Earnings" in out
    assert "Blowout quarter on AI demand" in out
    assert "est 2.03 &rarr; act 2.47" in out
    assert 'class="muted pe-company"' not in out
    assert 'class="muted pe-context"' not in out
    assert "Read-through:" not in out


def test_pe_non_rendered_fields_do_not_leak():
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing_enriched(),
                                 candidates=sample_pe_candidates_enriched())
    out = render_profile(analysis, bundle, "2026-07-02")
    for leak in ("LEAKDESC", "LEAKPEERA", "LEAKPEERB",   # description / peers
                 "9.87", "8.76",                          # fwd_eps_q / fwd_eps_fy
                 "77.7", "Basic Materials"):              # revision_30d_pct / sector
        assert leak not in out


# --------------------------------------------------------------------------- #
# Guidance chip (Spec B D4): deterministic midpoint-vs-consensus verdict,
# skipped entirely when guidance_struct is null/absent.
# --------------------------------------------------------------------------- #
def _writeup_with_guidance(gs):
    pe = sample_pe_briefing()
    pe["writeups"][0]["guidance_struct"] = gs
    return pe


def test_guidance_chip_absent_when_null():
    pe = _writeup_with_guidance(None)
    analysis, bundle = pe_inputs(post_earnings=pe)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert '<span class="lbl">Guide</span>' not in out


def test_guidance_chip_absent_when_key_missing():
    # Old-briefing regression: no guidance_struct key anywhere in the writeup.
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing())
    out = render_profile(analysis, bundle, "2026-07-02")
    assert '<span class="lbl">Guide</span>' not in out
    assert "est 2.03 &rarr; act 2.47" in out  # existing chips untouched


def test_guidance_chip_both_null_renders_nothing():
    pe = _writeup_with_guidance({"metric": "eps_fy", "low": None, "high": None})
    analysis, bundle = pe_inputs(post_earnings=pe)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert '<span class="lbl">Guide</span>' not in out


def test_guidance_chip_wkc_case_renders_below_neg():
    cands = sample_pe_candidates()
    cands[0]["fwd_eps_fy"] = 3.55
    pe = _writeup_with_guidance({"metric": "eps_fy", "low": 3.20, "high": 3.40})
    analysis, bundle = pe_inputs(post_earnings=pe, candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert ('<span class="pechip neg"><span class="lbl">Guide</span> '
            'FY EPS 3.20-3.40 vs cons 3.55 ▼</span>') in out


def test_guidance_chip_above_consensus_pos():
    cands = sample_pe_candidates()
    cands[0]["fwd_eps_q"] = 1.00
    pe = _writeup_with_guidance({"metric": "eps_q", "low": 1.03, "high": 1.03})
    analysis, bundle = pe_inputs(post_earnings=pe, candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert ('<span class="pechip pos"><span class="lbl">Guide</span> '
            'Q EPS 1.03 vs cons 1.00 ▲</span>') in out


def test_guidance_chip_inline_at_exact_2pct_boundary():
    cands = sample_pe_candidates()
    cands[0]["fwd_eps_q"] = 1.00
    pe = _writeup_with_guidance({"metric": "eps_q", "low": 1.02, "high": 1.02})
    analysis, bundle = pe_inputs(post_earnings=pe, candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert ('<span class="pechip flat"><span class="lbl">Guide</span> '
            'Q EPS 1.02 vs cons 1.00 ·</span>') in out


def test_guidance_chip_below_consensus_neg():
    cands = sample_pe_candidates()
    cands[0]["fwd_eps_q"] = 1.00
    pe = _writeup_with_guidance({"metric": "eps_q", "low": 0.97, "high": 0.97})
    analysis, bundle = pe_inputs(post_earnings=pe, candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert ('<span class="pechip neg"><span class="lbl">Guide</span> '
            'Q EPS 0.97 vs cons 1.00 ▼</span>') in out


def test_guidance_chip_missing_consensus_renders_range_only():
    cands = sample_pe_candidates()  # no fwd_eps_fy key at all
    pe = _writeup_with_guidance({"metric": "eps_fy", "low": 3.20, "high": 3.40})
    analysis, bundle = pe_inputs(post_earnings=pe, candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert ('<span class="pechip flat"><span class="lbl">Guide</span> '
            'FY EPS 3.20-3.40</span>') in out
    assert "vs cons" not in out


def test_guidance_chip_revenue_metric_range_only_via_fmt_compact():
    cands = sample_pe_candidates()
    cands[0]["fwd_eps_fy"] = 999.0  # must NOT leak in as revenue consensus
    pe = _writeup_with_guidance({"metric": "revenue_fy", "low": 3.8e9, "high": 4.0e9})
    analysis, bundle = pe_inputs(post_earnings=pe, candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert ('<span class="pechip flat"><span class="lbl">Guide</span> '
            'FY Rev 3.8B-4B</span>') in out
    assert "vs cons" not in out


def test_guidance_chip_point_guide_single_value():
    cands = sample_pe_candidates()
    cands[0]["fwd_eps_q"] = 2.50
    pe = _writeup_with_guidance({"metric": "eps_q", "low": None, "high": 2.50})
    analysis, bundle = pe_inputs(post_earnings=pe, candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert ('<span class="pechip flat"><span class="lbl">Guide</span> '
            'Q EPS 2.50 vs cons 2.50 ·</span>') in out


def test_guidance_chip_appended_after_existing_chips_in_same_row():
    cands = sample_pe_candidates()
    cands[0]["fwd_eps_fy"] = 3.55
    pe = _writeup_with_guidance({"metric": "eps_fy", "low": 3.20, "high": 3.40})
    analysis, bundle = pe_inputs(post_earnings=pe, candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    row_start = out.find('<div class="pe-chips">')
    row_end = out.find("</div>", row_start)
    row = out[row_start:row_end]
    assert row.index("EPS</span> est 2.03") < row.index('<span class="lbl">Guide</span>')


# --------------------------------------------------------------------------- #
# Context-line segments (Spec B D4): analyst moves / buyback / dividend,
# appended after the streak segment, each skipped when its data is absent.
# --------------------------------------------------------------------------- #
def test_pe_context_analyst_moves_segment():
    cands = sample_pe_candidates()
    cands[0]["analyst_moves"] = {"up": 2, "down": 1, "init": 0}
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing(), candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert '<p class="muted pe-context">2↑ 1↓</p>' in out


def test_pe_context_analyst_moves_init_only_nonzero_component():
    cands = sample_pe_candidates()
    cands[0]["analyst_moves"] = {"up": 0, "down": 0, "init": 1}
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing(), candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert '<p class="muted pe-context">1i</p>' in out


def test_pe_context_analyst_moves_all_zero_segment_omitted():
    cands = sample_pe_candidates()
    cands[0]["analyst_moves"] = {"up": 0, "down": 0, "init": 0}
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing(), candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert 'class="muted pe-context"' not in out


def test_pe_context_buyback_and_dividend_segments():
    cands = sample_pe_candidates()
    cands[0]["buyback"] = "$500M"
    cands[0]["dividend"] = "$0.31/sh"
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing(), candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert ('<p class="muted pe-context">buyback $500M &middot; div $0.31/sh</p>'
            in out)


def test_pe_context_segments_order_after_streak():
    cands = sample_pe_candidates_enriched()
    cands[0]["analyst_moves"] = {"up": 2, "down": 1, "init": 0}
    cands[0]["buyback"] = "$500M"
    cands[0]["dividend"] = "$0.31/sh"
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing_enriched(),
                                 candidates=cands)
    out = render_profile(analysis, bundle, "2026-07-02")
    assert ('<p class="muted pe-context">$3.1B &middot; Specialty Chemicals'
            ' &middot; 4 analysts &middot; streak +43.1/+6.4/-11.4/+91.5%'
            ' &middot; 2↑ 1↓ &middot; buyback $500M'
            ' &middot; div $0.31/sh</p>') in out


def test_pe_context_all_new_segments_absent_line_unchanged():
    # Regression: existing enriched context line renders exactly as before when
    # analyst_moves/buyback/dividend are absent from the candidate.
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing_enriched(),
                                 candidates=sample_pe_candidates_enriched())
    out = render_profile(analysis, bundle, "2026-07-02")
    assert ('<p class="muted pe-context">$3.1B &middot; Specialty Chemicals'
            ' &middot; 4 analysts &middot; streak +43.1/+6.4/-11.4/+91.5%</p>'
            ) in out


# --------------------------------------------------------------------------- #
# Finance desk (finance brief -> scoreboard -> post-earnings ->
# filings & calendar under one banner)
# --------------------------------------------------------------------------- #
def test_finance_banner_internal_order():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    f = out.find('<section id="finance">')
    story = out.find("Chipmaker guidance lifts semis")
    board = out.find(">Scoreboard<")
    table = out.find('<table class="markets">')
    note = out.find("The gold slide is the only real move")
    filings = out.find("Filings &amp; Calendar")
    assert -1 < f < story < board < table < note < filings


def test_finance_brief_empty_keeps_banner_and_scoreboard():
    analysis = sample_analysis()
    analysis["briefing"]["finance"]["brief"] = []
    out = render_profile(analysis, sample_bundle(), "2026-07-02")
    assert "Finance Brief" not in _doc_body(out)
    assert '<h2 class="section">Finance</h2>' in out
    assert '<table class="markets">' in out


def test_the_world_markets_bridge_is_gone():
    """The World -> Markets block was retired on 2026-08-16: it, the
    scoreboard_note and scoreboard_read.contradiction each stated the same
    observation, so the panel opened by saying one thing three times over."""
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    assert "World &rarr; Markets" not in out
    assert '<h2 class="section">Finance</h2>' in out


def test_finance_brief_cards_capped_at_three():
    analysis = sample_analysis()
    analysis["briefing"]["finance"]["brief"] = [
        {"headline": f"Finance headline {i}", "why_it_matters": "w",
         "body": "b", "sources": [], "tags": []} for i in range(1, 6)]
    out = render_profile(analysis, sample_bundle(), "2026-07-02")
    assert "Finance headline 3" in out
    assert "Finance headline 4" not in out  # capped at 3


def test_finance_brief_malformed_story_does_not_raise():
    analysis = sample_analysis()
    analysis["briefing"]["finance"]["brief"] = [
        "not-a-dict", {"headline": None, "sources": "x", "tags": 3}]
    out = render_profile(analysis, sample_bundle(), "2026-07-02")
    assert "<html" in out


def test_filings_and_earnings_notes_read_from_finance():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    assert "Notable registration and 8-K activity:" in out
    assert "Bellwethers reporting this week:" in out


def test_scoreboard_note_omitted_when_empty():
    analysis = sample_analysis()
    analysis["briefing"]["finance"]["scoreboard_note"] = ""
    out = render_profile(analysis, sample_bundle(), "2026-07-02")
    assert "The gold slide" not in out
    assert '<table class="markets">' in out


# --------------------------------------------------------------------------- #
# Signal section (top card + also-buried rows + one-line noise)
# --------------------------------------------------------------------------- #
def test_signal_top_card_renders_with_link():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    assert "Signal of the day" in out
    i = out.find("<article class='card signal-top'>")
    assert i != -1
    card = out[i:out.find("</article>", i)]
    assert '<a href="https://example.com/buried"' in card
    assert "Quiet supply-chain shift" in card
    assert "Nobody upstream has repriced it." in card


def test_signal_top_all_empty_no_card_but_rest_renders():
    analysis = sample_analysis()
    analysis["briefing"]["signal"]["top"] = {"story": "", "reason": "", "url": ""}
    out = render_profile(analysis, sample_bundle(), "2026-07-02")
    assert "Signal of the day" not in _doc_body(out)
    assert "<article class='card signal-top'>" not in out
    assert '<section id="signal">' in out
    assert "Obscure rule change" in out


def test_signal_buried_rows_render_linked_with_reason():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    assert "Also buried" in out
    i = out.find("Obscure rule change")
    assert i != -1
    assert '<a href="https://example.com/rule"' in out[max(0, i - 120):i]
    assert "&mdash; Alters bank capital treatment." in out[i:i + 200]


def test_noise_line_is_single_muted_line_without_links():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    i = out.find("Ignored today:")
    assert i != -1
    p_start = out.rfind("<p", 0, i)
    para = out[p_start:out.find("</p>", i)]
    assert "muted" in para
    assert "<a " not in para
    # Exactly one noise line.
    assert out.count("Ignored today:") == 1


def test_noise_line_empty_omitted():
    analysis = sample_analysis()
    analysis["briefing"]["signal"]["noise_line"] = ""
    out = render_profile(analysis, sample_bundle(), "2026-07-02")
    assert "Ignored today" not in out
    assert '<p class="muted noise">' not in out


def test_signal_section_absent_when_fully_empty():
    analysis = sample_analysis()
    analysis["briefing"]["signal"] = {
        "top": {"story": "", "reason": "", "url": ""},
        "buried": [], "noise_line": ""}
    out = render_profile(analysis, sample_bundle(), "2026-07-02")
    assert '<section id="signal">' not in out
    assert '<h2 class="section">Signal</h2>' not in out


# --------------------------------------------------------------------------- #
# Region color-coding (data-region / data-region-label on story cards)
# --------------------------------------------------------------------------- #
def test_derive_region_from_keywords():
    assert ch._derive_region({"headline": "Strait of Hormuz tanker seized",
                              "tags": [], "why_it_matters": ""}) == ("MENA", "MENA")
    assert ch._derive_region({"headline": "Nikkei rallies on BOJ hold",
                              "tags": [], "why_it_matters": ""}) == ("ASIA", "ASIA")
    assert ch._derive_region({"headline": "Something",
                              "tags": ["NATO", "defence"],
                              "why_it_matters": ""}) == ("EUROPE", "EUR")
    # No keyword anywhere -> GLOBAL fallback.
    assert ch._derive_region({"headline": "Quiet story", "tags": [],
                              "why_it_matters": "n/a"}) == ("GLOBAL", "GLOBAL")


def test_region_for_prefers_explicit_valid_bucket():
    # Explicit region wins (case-insensitive), even over keyword hits.
    story = {"headline": "Iran talks stall", "region": "us"}
    assert ch._region_for(story) == ("US", "US")
    # Invalid explicit value falls back to derivation.
    story = {"headline": "Iran talks stall", "region": "MARS"}
    assert ch._region_for(story) == ("MENA", "MENA")


def test_brief_and_finance_cards_carry_region_attributes():
    analysis = sample_analysis()
    analysis["briefing"]["brief"][0]["region"] = "US"
    out = render_profile(analysis, sample_bundle(), "2026-07-02")
    assert ("<article class='card' data-region='US' "
            "data-region-label='US'>") in out
    # Every plain story card carries the two attributes (the CSS block also
    # mentions data-region in its selectors, so count markup only). The label
    # is repeated on the <h3> because the CSS reads it via attr() there.
    body = _doc_body(out)
    assert body.count("<article class='card' data-region") == 3  # 2 brief + 1 finance
    assert body.count("data-region-label=") == 6  # article + h3, per card
    assert "<h3 data-region-label='US'>" in body


def test_pe_and_signal_cards_carry_no_region_attributes():
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing())
    out = render_profile(analysis, bundle, "2026-07-02")
    assert "<article class='card pe'>" in out
    assert "<article class='card signal-top'>" in out
    i = out.find("<article class='card pe'")
    assert "data-region" not in out[i:out.find(">", i) + 1]


# --------------------------------------------------------------------------- #
# Phosphor theme (CSS swap)
# --------------------------------------------------------------------------- #
def test_style_is_phosphor_dark_only():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    assert "color-scheme: dark" in out
    assert "@media (prefers-color-scheme: dark)" not in out  # old block gone
    assert "--rc-mena" in out  # region palette present
    assert "@font-face" not in out and "googleapis" not in out


# --------------------------------------------------------------------------- #
# Document ordering + degraded shapes
# --------------------------------------------------------------------------- #
def test_document_order_world_panel_then_finance_panel():
    """Spec E Task 8 CHANGED this order deliberately. Finance no longer sits
    between The Brief and Signal: it is a second tab panel, and every panel is
    a sibling AFTER the radio inputs so `:checked ~ .panel` can reach it."""
    body = _doc_body(render_profile(sample_analysis(), sample_bundle(), "2026-07-02"))
    b = body.find('<section id="brief">')
    s = body.find('<section id="signal">')
    t = body.find('<section id="threads">')
    r = body.find('<section id="on-radar">')
    f = body.find('<section id="finance">')
    assert -1 < b < s < t < r < f


def test_missing_finance_and_signal_keys_render_without_raising():
    analysis = sample_analysis()
    del analysis["briefing"]["finance"]
    del analysis["briefing"]["signal"]
    out = render_profile(analysis, sample_bundle(), "2026-07-02")
    assert "<html" in out
    # Finance banner still renders (the scoreboard is bundle-side)...
    assert '<h2 class="section">Finance</h2>' in out
    assert '<table class="markets">' in out
    # ...but the prose subsections and the signal section are simply absent.
    assert "Finance Brief" not in _doc_body(out)
    assert '<section id="signal">' not in out


# --------------------------------------------------------------------------- #
# Scoreboard long-horizon context sub-lines (YTD / 90d / 52w-high / percentile)
# --------------------------------------------------------------------------- #
def test_scoreboard_long_horizon_context_lines():
    bundle = sample_bundle()
    bundle["markets"]["indices"]["sp500"].update(
        ytd_pct=10.0, d90_pct=4.8, off_high_pct=8.3)
    bundle["markets"]["rates"]["us10y"].update(ytd_abs=0.48, d90_abs=0.12)
    bundle["markets"]["rates"]["us5y"] = {
        "level": 4.31, "change_abs": -0.02, "asof": "2026-07-01"}
    bundle["markets"]["rates"]["jp10y"] = {
        "level": 2.75, "change_abs": 0.039, "asof": "2026-07-01", "ytd_abs": 0.75}
    bundle["markets"]["rates"]["us30y"] = {
        "level": 5.265, "change_abs": 0.05, "asof": "2026-07-01",
        "ytd_abs": 0.428, "d90_abs": 0.137}
    bundle["markets"]["rates"]["jp30y"] = {
        "level": 3.88, "change_abs": -0.003, "asof": "2026-07-01"}
    bundle["markets"]["risk"]["vix"]["pctl_1y"] = 86
    bundle["markets"]["fx"]["usdjpy"]["ytd_pct"] = 5.0
    bundle["markets"]["fx"]["cadjpy"] = {"level": 118.4, "change_pct": 0.2,
                                         "asof": "2026-07-01"}
    bundle["markets"]["fx"]["usdcad"] = {"level": 1.36, "change_pct": -0.1,
                                         "asof": "2026-07-01"}
    bundle["markets"]["fx"]["usdkrw"] = {"level": 1380.0, "change_pct": 0.3,
                                         "asof": "2026-07-01"}   # dropped pair

    body = _doc_body(render_profile(sample_analysis(), bundle, "2026-07-02"))
    assert "YTD +10.0% · 90d +4.8% · 8.3% off high" in body
    assert "YTD +48 bp · 90d +12 bp" in body          # US 10Y in bp, not %
    assert "YTD +43 bp · 90d +14 bp" in body          # US 30Y, likewise
    assert "US 5Y" in body and "JP 10Y" in body and "JP 30Y" in body
    assert "YTD +75 bp" in body                       # JGB horizon in bp
    assert "86th pctile (1y)" in body                 # VIX regime stat
    assert "YTD +5.0%" in body                        # FX carries % horizons too
    assert "CAD/JPY" in body and "USD/CAD" in body    # new FX pairs
    assert "USD/KRW" not in body                      # removed from the board


def test_fx_context_line_ranks_the_pair_instead_of_measuring_off_the_high():
    """FX rows read YTD · 90d · percentile.

    A currency pair has no drawdown: off-high lands in the same 2-4% band for
    every pair every day, and two pairs at opposite ends of their own years look
    identical through it. The percentile is the stat that separates them, and it
    is already computed for every quote."""
    bundle = sample_bundle()
    bundle["markets"]["fx"]["usdjpy"].update(
        ytd_pct=1.8, d90_pct=0.3, off_high_pct=2.8, pctl_1y=75)
    bundle["markets"]["fx"]["eurusd"] = {
        "level": 1.1573, "change_pct": -0.14, "asof": "2026-07-02",
        "ytd_pct": -1.5, "d90_pct": -0.8, "off_high_pct": 3.7, "pctl_1y": 29}

    body = _doc_body(render_profile(sample_analysis(), bundle, "2026-07-02"))
    assert "YTD +1.8% · 90d +0.3% · 75th pctile (1y)" in body
    assert "YTD -1.5% · 90d -0.8% · 29th pctile (1y)" in body
    # The stat the pairs were indistinguishable through never reaches the page.
    assert "off high" not in body


def test_fx_context_line_omits_the_percentile_when_the_quote_lacks_one():
    """Twelve Data (the fallback) serves level/change only — no horizon fields.
    A gap-filled pair degrades to whatever it has rather than rendering a rank
    it never computed."""
    bundle = sample_bundle()
    bundle["markets"]["fx"]["usdjpy"].update(ytd_pct=1.8, d90_pct=0.3)
    body = _doc_body(render_profile(sample_analysis(), bundle, "2026-07-02"))
    assert "YTD +1.8% · 90d +0.3%</span>" in body
    assert "pctile" not in body


def test_scoreboard_no_ctx_line_when_fields_absent():
    body = _doc_body(render_profile(sample_analysis(), sample_bundle(), "2026-07-02"))
    assert 'class="ctx"' not in body and "class='ctx'" not in body


def test_scoreboard_at_52w_high_wording():
    bundle = sample_bundle()
    bundle["markets"]["indices"]["sp500"].update(ytd_pct=10.0, off_high_pct=0.0)
    body = _doc_body(render_profile(sample_analysis(), bundle, "2026-07-02"))
    assert "at 52w high" in body


# --------------------------------------------------------------------------- #
# SEC fundamentals rendering (Spec C D6, Task 8). Every figure here is rendered
# from the candidate; Codex supplies prose only. The prohibitions in the
# companion doc 8.2 are asserted as rendering invariants.
# --------------------------------------------------------------------------- #
def _fund(**over):
    """A resolved `fundamentals` dict, AAPL's live 2026-Q3 values by default."""
    fund = {
        "status": "ok", "source": "companyconcept", "period_end": "2026-06-27",
        "fiscal": {"fy": 2026, "fp": "Q3"}, "accession": "0000320193-26-000020",
        "form": "10-Q",
        "revenue_q": {"value": 109417000000, "basis": "direct",
                      "tag": "Revenues", "yoy_pct": 16.4},
        "operating_income_q": None, "net_income_q": None, "eps_diluted_q": None,
        "equity": None,
        "ocf_ttm": {"value": 146700000000, "basis": "direct",
                    "tag": "NetCashProvidedByUsedInOperatingActivities",
                    "yoy_pct": None},
        "capex_ttm": {"value": 10000000000, "basis": "direct",
                      "tag": "PaymentsToAcquireProductiveAssets",
                      "yoy_pct": None, "definition": "gaap_cashflow"},
        "capex_company_ttm": None,
        "fcf_ttm": {"value": 136700000000, "basis": "derived", "tag": None,
                    "yoy_pct": None, "definition": "gaap_cashflow"},
        "rpo": None,
        "op_margin_pct": {"value": 32.6, "yoy_delta_pp": 2.7},
        "capex_intensity_pct": None,
        "segments": [], "notes": [],
    }
    fund.update(over)
    return fund


def _pe_cand_with_fundamentals(**over):
    cand = dict(sample_pe_candidates()[0])
    cand["fundamentals"] = _fund()
    cand["fundamentals_flags"] = []
    cand.update(over)
    return cand


# --- the op-margin chip ----------------------------------------------------- #
def test_op_margin_chip_renders_with_value_and_yoy_delta():
    chips = " ".join(ch._pe_chips(_pe_cand_with_fundamentals()))
    assert "Op margin" in chips
    assert "32.6%" in chips
    assert "+2.7pp" in chips


def test_op_margin_chip_absent_without_yoy_delta():
    cand = _pe_cand_with_fundamentals()
    cand["fundamentals"]["op_margin_pct"] = {"value": 32.6, "yoy_delta_pp": None}
    assert "Op margin" not in " ".join(ch._pe_chips(cand))


def test_op_margin_chip_absent_when_value_out_of_range():
    # A triple-digit margin is an extraction artefact, never a fact to chip.
    cand = _pe_cand_with_fundamentals()
    cand["fundamentals"]["op_margin_pct"] = {"value": 140.0, "yoy_delta_pp": 2.7}
    assert "Op margin" not in " ".join(ch._pe_chips(cand))


def test_op_margin_chip_absent_when_fundamentals_missing():
    cand = dict(sample_pe_candidates()[0])
    assert "Op margin" not in " ".join(ch._pe_chips(cand))
    cand["fundamentals"] = None
    assert "Op margin" not in " ".join(ch._pe_chips(cand))


def test_op_margin_chip_absent_when_status_not_ok():
    cand = _pe_cand_with_fundamentals()
    cand["fundamentals"]["status"] = "stale"
    assert "Op margin" not in " ".join(ch._pe_chips(cand))


def test_fcf_is_never_a_chip():
    """8.2: FCF is the least authoritative number in the set, muted line only."""
    chips = " ".join(ch._pe_chips(_pe_cand_with_fundamentals()))
    assert "FCF" not in chips


# --- the muted fundamentals line ------------------------------------------- #
def test_fundamentals_line_full():
    line = ch._pe_fundamentals_line(_pe_cand_with_fundamentals())
    assert "SEC 10-Q" in line
    assert "Rev 109.4B (+16.4% y/y)" in line
    assert "OCF TTM 146.7B" in line
    assert "Capex TTM 10B (SEC cash-flow basis)" in line
    assert "FCF TTM 136.7B (SEC cash-flow basis)" in line
    assert line.count("&middot;") == 4


def test_fundamentals_line_skips_missing_segments():
    cand = _pe_cand_with_fundamentals()
    cand["fundamentals"].update(ocf_ttm=None, capex_ttm=None, fcf_ttm=None)
    line = ch._pe_fundamentals_line(cand)
    assert line == "SEC 10-Q &middot; Rev 109.4B (+16.4% y/y)"


def test_fundamentals_line_empty_when_no_numeric_segment():
    # The form alone is not information; a line reading "SEC 10-Q" is noise.
    cand = _pe_cand_with_fundamentals()
    cand["fundamentals"].update(revenue_q=None, ocf_ttm=None, capex_ttm=None,
                                fcf_ttm=None)
    assert ch._pe_fundamentals_line(cand) == ""


def test_fundamentals_line_empty_when_status_not_ok():
    cand = _pe_cand_with_fundamentals()
    cand["fundamentals"]["status"] = "stale"
    assert ch._pe_fundamentals_line(cand) == ""


def test_fundamentals_line_empty_without_fundamentals():
    assert ch._pe_fundamentals_line(dict(sample_pe_candidates()[0])) == ""
    assert ch._pe_fundamentals_line({"fundamentals": None}) == ""
    assert ch._pe_fundamentals_line(None) == ""


def test_fundamentals_line_revenue_without_yoy():
    cand = _pe_cand_with_fundamentals()
    cand["fundamentals"]["revenue_q"]["yoy_pct"] = None
    line = ch._pe_fundamentals_line(cand)
    assert "Rev 109.4B" in line
    assert "y/y" not in line


def test_capex_and_fcf_never_render_without_the_basis_qualifier():
    """8.2: the label is the claim we can defend."""
    body = ch._pe_fundamentals_line(_pe_cand_with_fundamentals())
    for token in ("Capex TTM", "FCF TTM"):
        idx = body.find(token)
        assert idx != -1
        assert "(SEC cash-flow basis)" in body[idx:idx + 60]


# --- the referee line ------------------------------------------------------- #
def _referee_cand():
    return _pe_cand_with_fundamentals(fundamentals_flags=[
        {"kind": "eps_disagreement", "sec": 2.02, "vendor": 1.91, "form": "10-Q"}])


def test_referee_line_presents_both_figures():
    line = ch._pe_referee_line(_referee_cand())
    assert "SEC 10-Q diluted EPS $2.02" in line
    assert "vendor actual $1.91" in line


def test_referee_line_adjudicates_nothing():
    line = ch._pe_referee_line(_referee_cand()).lower()
    for verdict in ("wrong", "correct", "error", "should be", "actually",
                    "incorrect", "overstat", "understat"):
        assert verdict not in line


def test_referee_line_empty_without_flags():
    assert ch._pe_referee_line(_pe_cand_with_fundamentals()) == ""
    assert ch._pe_referee_line({"fundamentals_flags": None}) == ""
    assert ch._pe_referee_line(None) == ""


def test_referee_line_ignores_unknown_flag_kinds():
    cand = _pe_cand_with_fundamentals(fundamentals_flags=[{"kind": "something_else"}])
    assert ch._pe_referee_line(cand) == ""


def test_card_renders_fundamentals_and_referee_lines():
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing(),
                                 candidates=[_referee_cand()])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-17"))
    assert "SEC 10-Q" in body
    assert "OCF TTM 146.7B" in body
    # Spec acceptance 3: BOTH figures on screen, neither adjudicated.
    assert "diluted EPS $2.02" in body
    assert "vendor actual $1.91" in body


def test_derived_per_share_figure_never_rendered():
    """Spec acceptance 2: MSFT's fiscal-Q4 4.81 must be unreachable. The lane
    refuses to publish it (basis "blocked" -> value None), and the renderer has
    no path that could resurrect it."""
    cand = _pe_cand_with_fundamentals(ticker="MSFT", name="Microsoft")
    cand["fundamentals"]["eps_diluted_q"] = None
    cand["fundamentals"]["notes"] = ["eps_derived_suppressed"]
    analysis, bundle = pe_inputs(post_earnings={
        "writeups": [{"ticker": "MSFT", "headline": "Cloud carries the quarter",
                      "read": "Azure re-accelerated."}]}, candidates=[cand])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "4.81" not in body
    assert "eps_derived_suppressed" not in body  # internal note, not reader-facing


def test_roic_never_rendered():
    """Spec acceptance 9."""
    cand = _pe_cand_with_fundamentals()
    cand["fundamentals"]["equity"] = {"value": 66800000000, "basis": "instant",
                                      "tag": "StockholdersEquity", "yoy_pct": None}
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing(),
                                 candidates=[cand])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-17")).lower()
    for token in ("roic", "return on invested", "equity &minus; cash",
                  "equity - cash"):
        assert token not in body


def test_stale_fundamentals_say_nothing_at_all():
    """8.2: the reader does not care about our pipeline's internals."""
    cand = _pe_cand_with_fundamentals()
    cand["fundamentals"] = _fund(status="stale", revenue_q=None, ocf_ttm=None,
                                 capex_ttm=None, fcf_ttm=None, op_margin_pct=None)
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing(),
                                 candidates=[cand])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-17"))
    assert "SEC" not in body
    assert "pending" not in body.lower()


# --- the follow-up section -------------------------------------------------- #
def _followup(**over):
    fu = {
        "ticker": "META", "report_date": "2026-07-29", "name": "Meta Platforms Inc",
        "hour": "amc", "eps_est": 7.3594, "eps_act": 6.18, "surprise_pct": -16.0,
        "rev_est": 61372015054, "rev_act": 60801000000, "rev_act_source": "finnhub",
        "price_move_pct": -8.0,
        "fundamentals": _fund(form="10-Q"), "fundamentals_flags": [],
    }
    fu.update(over)
    return fu


def fu_inputs(followups=None, prose=None, post_earnings=None):
    analysis = sample_analysis()
    pe = sample_pe_briefing() if post_earnings is None else post_earnings
    if prose is not None:
        pe["followups"] = prose
    analysis["briefing"]["post_earnings"] = pe
    bundle = sample_bundle()
    bundle["post_earnings_candidates"] = sample_pe_candidates()
    if followups is not None:
        bundle["post_earnings_followups"] = followups
    return analysis, bundle


def test_followup_section_absent_when_list_empty():
    for followups in (None, []):
        analysis, bundle = fu_inputs(followups=followups)
        body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
        assert "From the filings" not in body


def test_followup_section_renders_snapshot_fundamentals_and_prose():
    analysis, bundle = fu_inputs(
        followups=[_followup()],
        prose=[{"ticker": "META", "headline": "The filing shows the capex bill",
                "body": "Operating cash flow covered the build, barely."}])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "From the filings" in body
    assert "Meta Platforms Inc" in body
    assert "6.18" in body                       # recalled beat-1 numbers
    assert "SEC 10-Q" in body                   # what the filing adds
    assert "OCF TTM 146.7B" in body
    assert "The filing shows the capex bill" in body
    assert "Operating cash flow covered the build, barely." in body


def test_followup_never_renders_as_a_write_up_card():
    """A reader must never mistake a follow-up for a fresh reporter."""
    analysis, bundle = fu_inputs(followups=[_followup()], prose=[
        {"ticker": "META", "headline": "h", "body": "b"}])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    section = body[body.find("From the filings"):]
    assert "card pe" not in section
    assert "pe-chips" not in section  # the recalled numbers are muted, not chipped


def test_followup_renders_numbers_without_prose():
    # The numbers are deterministic; Codex prose is optional garnish.
    analysis, bundle = fu_inputs(followups=[_followup()], prose=None)
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "From the filings" in body
    assert "SEC 10-Q" in body


def test_followup_prose_joined_by_ticker_case_insensitively():
    analysis, bundle = fu_inputs(followups=[_followup()], prose=[
        {"ticker": "meta", "headline": "matched anyway", "body": "b"}])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "matched anyway" in body


def test_followup_prose_for_unknown_ticker_is_dropped():
    analysis, bundle = fu_inputs(followups=[_followup()], prose=[
        {"ticker": "ZZZQ", "headline": "not a follow-up", "body": "b"}])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "not a follow-up" not in body


def test_followup_section_carries_referee_line():
    fu = _followup(fundamentals_flags=[
        {"kind": "eps_disagreement", "sec": 2.02, "vendor": 1.91, "form": "10-Q"}])
    analysis, bundle = fu_inputs(followups=[fu])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "vendor actual $1.91" in body


def test_followup_section_survives_malformed_entries():
    analysis, bundle = fu_inputs(followups=["not-a-dict", {}, _followup()])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))  # must not raise
    assert "From the filings" in body


def test_followup_section_renders_even_without_writeups():
    # A morning whose only earnings news is a filing that finally became
    # readable still has something to say.
    analysis, bundle = fu_inputs(followups=[_followup()],
                                 post_earnings={"writeups": []})
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "Post-Earnings" not in body
    assert "From the filings" in body


# --------------------------------------------------------------------------- #
# The compact "Also filed" tier (filing-reconciliation spec §6). Every field
# comes from the filing index and no model is involved, so these tests build
# their input from index fields alone and call the section directly — nothing
# in the analysis or the candidate list can reach this tier.
# --------------------------------------------------------------------------- #
def _filed_line(**over):
    line = {"ticker": "BILL", "name": "BILL Holdings", "form": "10-K",
            "filed": "2026-08-20"}
    line.update(over)
    return line


def _fu_section(followups=None, lines=None, prose=None):
    analysis, bundle = fu_inputs(followups=followups or [], prose=prose)
    return ch._pe_followup_section(analysis["briefing"], bundle, lines)


def test_also_filed_renders_company_ticker_form_and_date_from_index_fields():
    out = _fu_section(lines=[_filed_line()])
    assert "Also filed" in out
    assert "BILL Holdings" in out
    assert '<span class="ticker">BILL</span>' in out
    assert "10-K" in out
    assert "Aug 20" in out          # _fmt_date, not the raw ISO string
    assert "2026-08-20" not in out


def test_also_filed_heading_never_implies_the_filing_followed_the_coverage():
    """COTY filed its 10-K on 08-20 and was a card on 08-22. The window is a
    date range, not a causal order."""
    out = _fu_section(lines=[_filed_line()])
    assert '<p class="muted">Also filed</p>' in out
    assert "since we covered them" not in out


def test_also_filed_folds_past_the_line_limit_and_counts_the_remainder():
    n = ch.LINE_FOLD_AFTER + 3
    out = _fu_section(lines=[_filed_line(ticker=f"T{i}", name=f"Co {i}")
                             for i in range(n)])
    assert "<summary>3 more filings</summary>" in out
    assert "details class='more'" in out
    # The fold splits the list; every entry is still on the page.
    assert out.count("<li>") == n
    head, tail = out.split("<details", 1)
    assert head.count("<li>") == ch.LINE_FOLD_AFTER
    assert tail.count("<li>") == 3


def test_also_filed_does_not_fold_at_or_below_the_line_limit():
    for n in (1, ch.LINE_FOLD_AFTER - 1, ch.LINE_FOLD_AFTER):
        out = _fu_section(lines=[_filed_line(ticker=f"T{i}", name=f"Co {i}")
                                 for i in range(n)])
        assert "<details" not in out, n
        assert out.count("<li>") == n


def test_also_filed_row_without_a_name_still_renders_under_its_ticker():
    out = _fu_section(lines=[_filed_line(name=None)])
    assert "<li>BILL &middot; 10-K &middot; Aug 20</li>" in out
    assert '<span class="ticker">' not in out  # no name, so nothing to qualify


def test_also_filed_row_naming_nobody_is_skipped():
    out = _fu_section(lines=[_filed_line(ticker="", name=""), _filed_line()])
    assert out.count("<li>") == 1
    assert "BILL Holdings" in out


def test_also_filed_escapes_a_company_name():
    out = _fu_section(lines=[_filed_line(name="Smucker & <b>Co</b>")])
    assert "Smucker &amp; &lt;b&gt;Co&lt;/b&gt;" in out
    assert "<b>" not in out


def test_also_filed_survives_a_malformed_list():
    for lines in (None, [], "garbage", ["not-a-dict", None, 7], [{}]):
        assert _fu_section(lines=lines) == ""


def test_a_day_with_blocks_but_no_matches_renders_no_list():
    out = _fu_section(followups=[_followup()])
    assert "From the filings" in out
    assert "Also filed" not in out


def test_a_day_with_matches_but_no_promotions_still_publishes_its_list():
    """The behaviour change: the section used to require a surviving block, so
    a morning of matched filings with nothing promoted published nothing."""
    out = _fu_section(lines=[_filed_line()])
    assert "From the filings" in out      # one heading for the whole section
    assert out.count("From the filings") == 1
    assert "BILL Holdings" in out


def test_a_day_with_neither_tier_renders_nothing_at_all():
    assert _fu_section() == ""
    assert "From the filings" not in _fu_section()  # no orphaned heading


def test_six_blocks_render_and_the_seventh_is_truncated():
    """All three statements of the cap agree at 6; the lowest would silently
    become the real one."""
    assert ch.FOLLOWUP_DISPLAY_MAX == 6
    out = _fu_section(followups=[_followup(ticker=f"T{i}", name=f"Filer {i}")
                                 for i in range(7)])
    for i in range(6):
        assert f"Filer {i}" in out
    assert "Filer 6" not in out
    assert out.count('class="pe-fu"') == 6


# --------------------------------------------------------------------------- #
# Ownership REMOVED 2026-08-30 (roadmap Phase 0). The lane cost 43-67s of a
# 600s hard ceiling and its own Codex prose called the result no signal. What
# replaces ~25 rendering tests is one absence guard: the section, its three
# group labels and its EDGAR footnote must not come back by accident.
#
# `scripts/ownership.py` itself SURVIVES -- `filing_index` imports
# `parse_daily_index` and `_daily_index_url` from it. So this is a render-layer
# assertion, not a module-existence one.
# --------------------------------------------------------------------------- #
def test_no_ownership_block_renders_even_when_the_bundle_carries_one():
    """The strong form of the guard. A stale deployed copy, or a resumed run
    holding an older bundle, can still carry `bundle["ownership"]` fully
    populated; the renderer must ignore it rather than quietly resurrect the
    block. Passing the data in is the whole point of the test."""
    analysis = sample_analysis()
    analysis["briefing"]["finance"]["ownership_note"] = "Insiders keep buying."
    bundle = sample_bundle()
    bundle["ownership"] = {
        "insider_watchlist": [{
            "ticker": "AAPL", "company": "Apple", "person": "Timothy D. Cook",
            "role": "CEO", "code": "S", "action": "Sale", "shares": 100000.0,
            "price": 412.0, "value": 41200000.0, "planned": True,
            "date": "2026-07-29", "url": "https://sec.gov/aapl-form4",
        }],
        "insider_buys": [{
            "ticker": "RSG", "company": "Republic Services",
            "person": "Cascade Investment", "role": "10% owner",
            "action": "Purchase", "value": 51800000.0, "date": "2026-08-27",
            "url": "https://sec.gov/rsg-form4",
        }],
        "activist_stakes": [{
            "company": "Phillips 66", "ticker": "PSX",
            "filer": "Elliott Investment Management", "form": "13D/A",
            "percent": 8.5, "prior_percent": 5.1,
            "event_date": "2026-07-29", "filed": "2026-07-30",
            "purpose": "...intends to nominate directors...",
            "url": "https://sec.gov/psx-13d",
        }],
        "scanned": {"form4_total": 900, "form4_read": 900,
                    "truncated": False, "index_date": "2026-08-28"},
    }
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))

    for label in ("Ownership", "Watchlist insiders", "Largest open-market buys",
                  "Activist stakes"):
        assert label not in body, f"retired ownership label rendered: {label}"
    # Row-level content, not just the headings -- a group label could be
    # dropped while the rows themselves still leaked into a neighbouring block.
    for leaked in ("Timothy D. Cook", "Cascade Investment",
                   "Elliott Investment Management", "Insiders keep buying"):
        assert leaked not in body, f"ownership row content rendered: {leaked}"


def test_ownership_section_helper_is_gone_from_the_renderer():
    """The call site AND the function. Leaving `_ownership_section` defined but
    uncalled is the exact built-tested-never-called shape this codebase has hit
    five times; here it would also keep ~100 lines of CSS alive."""
    assert not hasattr(ch, "_ownership_section")

# --------------------------------------------------------------------------- #
# Spec E Task 7 — the follow-up card's filing-lane extensions: panel flag chips,
# the segment table, the guidance line with its deterministic verdict, the
# risk-change line and the quote-grounded MD&A reads.
#
# THE UNIT RULE, pinned below and stated once here. `sec_filing_text.
# parse_segments` publishes each cell AS PRINTED, under a caption
# ("In millions") that sits outside the row and which that module deliberately
# refuses to apply. `sec_fundamentals` and `anomaly_panel` publish ABSOLUTE
# DOLLARS. This renderer therefore publishes NO segment magnitude at all — only
# scale-invariant ratios computed from the segment table against itself. There
# is consequently no arithmetic anywhere in which a 1e6 mismatch could hide.
# --------------------------------------------------------------------------- #
def _flag(**over):
    flag = {"operator": "TWO CLOCKS", "metric": "dso", "direction": "favourable",
            "statement": "DSO -3.1 days YoY to 57.4 while revenue grew 50.1%",
            "deviation": 1.03}
    flag.update(over)
    return flag


def _panel(**over):
    panel = {"status": "partial", "ticker": "META", "period_end": "2026-06-27",
             "prior_end": "2025-06-28", "metrics": {},
             "flags": [_flag()], "contradictions": []}
    panel.update(over)
    return panel


# AMD's live Q2 2026 table, in the table's own printed unit (millions).
def _segments():
    return [
        {"segment": "Data Center", "revenue": 6718.0, "revenue_prior": 3240.0,
         "op_income": 2200.0, "op_income_prior": 1264.0},
        {"segment": "Client and Gaming", "revenue": 3841.0, "revenue_prior": 3621.0,
         "op_income": 446.0, "op_income_prior": 89.0},
        {"segment": "Embedded", "revenue": 977.0, "revenue_prior": 824.0,
         "op_income": 366.0, "op_income_prior": 345.0},
    ]


_MDNA = ("Management's Discussion and Analysis of Financial Condition and "
         "Results of Operations. Data Center segment revenue increased 107% "
         "year-over-year driven by AMD Instinct GPU shipments. Client and "
         "Gaming revenue grew modestly on a richer mix.")
_RISK = ("Item 1A. Risk Factors. There have been material changes to the risk "
         "factors previously disclosed in our Annual Report on Form 10-K for "
         "the fiscal year ended December 28, 2025.")


def _fu_filing(**over):
    """A follow-up decorated exactly as run.py's filing-text lane leaves it."""
    fu = _followup(
        guidance_struct={"metric": "eps_fy", "low": 5.30, "high": 5.50},
        panel=_panel(),
        segments=_segments(),
        filing_sections={"mdna": _MDNA, "risk_changes": _RISK},
        disclosures=[],
    )
    fu.update(over)
    return fu


# --- _panel_chips: styling from `direction`, never from the text ------------- #
def test_panel_chips_empty_without_flags():
    for panel in (None, {}, 3, "x", {"flags": []}, {"flags": "not-a-list"},
                  {"flags": ["not-a-dict"]}):
        assert ch._panel_chips(panel) == ""


def test_panel_chips_class_derives_from_direction_field():
    chips = ch._panel_chips(_panel(flags=[
        _flag(metric="dso", direction="favourable", statement="DSO fell"),
        _flag(metric="dpo", direction="unfavourable", statement="DPO rose"),
        _flag(metric="effective_tax_rate_pct", direction="neutral",
              statement="Tax rate 9.8%"),
    ]))
    assert 'class="flagchip fav"' in chips
    assert 'class="flagchip unfav"' in chips
    assert 'class="flagchip neu"' in chips
    for statement in ("DSO fell", "DPO rose", "Tax rate 9.8%"):
        assert statement in chips


def test_panel_chip_styling_never_inferred_from_statement_text():
    """A favourable flag whose statement reads like bad news still styles
    favourable: the panel computed the direction, the renderer obeys it."""
    chips = ch._panel_chips(_panel(flags=[
        _flag(direction="favourable",
              statement="Inventory days fell 12.3 and the shares dropped")]))
    assert 'class="flagchip fav"' in chips
    assert "unfav" not in chips


def test_panel_chip_unknown_or_missing_direction_is_neutral():
    for direction in (None, "", "bearish", 7):
        chips = ch._panel_chips(_panel(flags=[_flag(direction=direction)]))
        assert 'class="flagchip neu"' in chips


def test_panel_chip_carries_the_operator_name():
    chips = ch._panel_chips(_panel(flags=[_flag(operator="FUNDING LEG")]))
    assert "FUNDING LEG" in chips


def test_panel_chip_deviation_is_never_rendered():
    """`deviation` is a ranking key (anomaly_panel.compute_flags), not a fact."""
    chips = ch._panel_chips(_panel(flags=[_flag(deviation=4.28217)]))
    assert "4.28" not in chips
    assert "deviation" not in chips


def test_followup_card_renders_panel_chips():
    analysis, bundle = fu_inputs(followups=[_fu_filing()])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "flagchip fav" in body
    assert "DSO -3.1 days YoY to 57.4" in body


def test_writeup_card_renders_panel_chips():
    """Beat 1 gets flags too (spec Sec.3, "When the panel runs") — run.py sets
    `cand["panel"]` on every in-scope reporter."""
    cand = dict(sample_pe_candidates()[0])
    cand["panel"] = _panel(flags=[_flag(statement="Diluted share count +1.78% YoY",
                                        direction="unfavourable")])
    analysis, bundle = pe_inputs(post_earnings=sample_pe_briefing(),
                                 candidates=[cand])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-17"))
    assert "Diluted share count +1.78% YoY" in body
    assert "flagchip unfav" in body


# --- the segment table: ratios only, never a magnitude ---------------------- #
def _segment_region(body):
    start = body.find('<table class="segments">')
    assert start != -1
    return body[start:body.find("</table>", start)]


def test_segment_table_renders_share_yoy_and_op_margin():
    analysis, bundle = fu_inputs(followups=[_fu_filing()])
    table = _segment_region(_doc_body(render_profile(analysis, bundle, "2026-07-31")))
    assert "Data Center" in table
    assert "+107.3%" in table           # (6718 - 3240) / 3240
    assert "58.2%" in table             # 6718 / 11536, the table's own total
    assert "+16.1pp" in table           # 58.235 - 42.160
    assert "32.7%" in table             # 2200 / 6718
    assert "Client and Gaming" in table
    assert "Embedded" in table


def test_segment_magnitudes_are_never_rendered():
    """THE UNIT HAZARD, pinned. `parse_segments` returns the cell as printed in
    the filing table's own unit; the caption carrying that unit is outside the
    row. Publishing 6718 would read as dollars, 6.7B as a scaled guess. This
    renderer publishes neither — only ratios of the table against itself."""
    analysis, bundle = fu_inputs(followups=[_fu_filing()])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    table = _segment_region(body)
    for magnitude in ("6718", "6,718", "6.718", "6.7B", "6718000000",
                      "3240", "3,240", "11536", "11,536", "11.5B"):
        assert magnitude not in table, magnitude
    assert "$" not in table


def test_segment_share_uses_the_segment_total_not_sec_dollars():
    """A follow-up carries BOTH the filing table (millions, as printed) and
    `fundamentals.revenue_q` (absolute dollars). Dividing one by the other is
    the silent 1e6 error this test exists to catch: it would put Data Center's
    share at 0.0% rather than 58.2%."""
    fu = _fu_filing()
    fu["fundamentals"]["revenue_q"] = {"value": 11536000000, "basis": "direct",
                                       "tag": "Revenues", "yoy_pct": 50.1}
    analysis, bundle = fu_inputs(followups=[fu])
    table = _segment_region(_doc_body(render_profile(analysis, bundle, "2026-07-31")))
    assert "58.2%" in table
    assert "0.0%" not in table


def test_segment_op_margin_is_a_same_unit_ratio():
    """op_income and revenue are cells of the SAME table, so their ratio is
    exact and unit-free whatever the caption says."""
    rows = ch._segment_rows([
        {"segment": "Solo", "revenue": 1000.0, "revenue_prior": 800.0,
         "op_income": 250.0, "op_income_prior": 160.0}])
    assert len(rows) == 1
    assert round(rows[0]["op_margin"], 4) == 25.0
    assert round(rows[0]["op_margin_delta"], 4) == 5.0     # 25.0 - 20.0
    assert round(rows[0]["share"], 4) == 100.0


def test_segment_table_absent_when_unparseable_or_malformed():
    for segments in (None, [], "x", [{}], ["not-a-dict"],
                     [{"segment": "X", "revenue": None, "revenue_prior": 1.0}]):
        analysis, bundle = fu_inputs(followups=[_fu_filing(segments=segments)])
        body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
        assert '<table class="segments">' not in body


def test_segment_op_margin_cell_empty_when_op_income_absent():
    analysis, bundle = fu_inputs(followups=[_fu_filing(segments=[
        {"segment": "Data Center", "revenue": 6718.0, "revenue_prior": 3240.0,
         "op_income": None, "op_income_prior": None}])])
    table = _segment_region(_doc_body(render_profile(analysis, bundle, "2026-07-31")))
    assert "+107.3%" in table
    assert "100.0%" in table            # sole segment, the whole of the table
    assert '<td class="segmargin">—</td>' in table


# --- the guidance line: verdict is midpoint-vs-consensus arithmetic --------- #
def test_followup_guidance_line_restates_the_guide():
    analysis, bundle = fu_inputs(followups=[_fu_filing()])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "5.30-5.50" in body


def test_followup_guidance_verdict_is_computed_not_composed():
    fu = _fu_filing(fwd_eps_fy=5.10)                 # midpoint 5.40 -> above
    analysis, bundle = fu_inputs(followups=[fu])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "vs cons 5.10" in body
    assert "▲" in body


def test_followup_guidance_absent_without_a_struct():
    for gs in (None, {}, "x", {"metric": "eps_fy", "low": None, "high": None}):
        analysis, bundle = fu_inputs(followups=[_fu_filing(guidance_struct=gs)])
        body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
        assert "pe-fu-guide" not in body


# --- the risk-change line --------------------------------------------------- #
def test_followup_risk_change_line_renders_when_the_filing_asserts_one():
    analysis, bundle = fu_inputs(followups=[_fu_filing()])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "Risk factors:" in body
    assert "There have been material changes to the risk" in body


def test_followup_risk_line_absent_when_the_section_is_absent():
    """`slice_sections` publishes `risk_changes` only when the filing asserts a
    change, so its absence is the no-change case and must render nothing."""
    for sections in (None, {}, {"mdna": _MDNA}, "x"):
        analysis, bundle = fu_inputs(
            followups=[_fu_filing(filing_sections=sections)])
        body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
        assert "Risk factors:" not in body


def test_followup_never_dumps_a_whole_filing_section():
    long_mdna = "Results of operations. " + ("filler sentence. " * 4000)
    analysis, bundle = fu_inputs(followups=[_fu_filing(
        filing_sections={"mdna": long_mdna, "risk_changes": _RISK + " " + long_mdna})])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert body.count("filler sentence.") < 20


# --- the grounding rule: a qualitative claim without a quote is dropped ------ #
_GROUNDED = "Data Center segment revenue increased 107% year-over-year"


def test_panel_read_is_dropped_without_a_quote():
    analysis, bundle = fu_inputs(followups=[_fu_filing()], prose=[
        {"ticker": "META", "headline": "h", "body": "b",
         "panel_read": "The collection period did the work here.",
         "segment_read": "Data Center carried the quarter.",
         "quotes": []}])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "The collection period did the work here." not in body
    assert "Data Center carried the quarter." not in body
    assert "h" in body  # the pre-existing headline/body fields are unaffected


def test_panel_read_renders_when_its_quote_is_in_the_filing_text():
    analysis, bundle = fu_inputs(followups=[_fu_filing()], prose=[
        {"ticker": "META", "headline": "h", "body": "b",
         "panel_read": "The collection period did the work here.",
         "quotes": [{"section": "mdna", "text": _GROUNDED}]}])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "The collection period did the work here." in body
    assert _GROUNDED in body            # the quote itself is shown, verbatim


def test_claim_dropped_when_the_quote_is_not_in_the_filing():
    analysis, bundle = fu_inputs(followups=[_fu_filing()], prose=[
        {"ticker": "META", "headline": "h", "body": "b",
         "segment_read": "Embedded is quietly compounding.",
         "quotes": [{"section": "mdna",
                     "text": "Embedded revenue tripled on record bookings"}]}])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "Embedded is quietly compounding." not in body
    assert "Embedded revenue tripled" not in body


def test_a_trivially_short_quote_does_not_ground_a_claim():
    analysis, bundle = fu_inputs(followups=[_fu_filing()], prose=[
        {"ticker": "META", "headline": "h", "body": "b",
         "panel_read": "A sweeping claim.",
         "quotes": [{"section": "mdna", "text": "revenue"}]}])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "A sweeping claim." not in body


def test_a_disclosure_quote_also_grounds_a_claim():
    fu = _fu_filing(disclosures=[
        {"term": "purchase obligations",
         "quote": "We have purchase obligations of $4.5 billion not yet commenced.",
         "amount": 4500000000.0}])
    analysis, bundle = fu_inputs(followups=[fu], prose=[
        {"ticker": "META", "headline": "h", "body": "b",
         "panel_read": "The commitments sit outside the balance sheet.",
         "quotes": [{"section": "notes",
                     "text": "purchase obligations of $4.5 billion not yet commenced"}]}])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "The commitments sit outside the balance sheet." in body


def test_grounding_is_insensitive_to_whitespace_only_differences():
    analysis, bundle = fu_inputs(followups=[_fu_filing()], prose=[
        {"ticker": "META", "headline": "h", "body": "b",
         "panel_read": "A grounded claim.",
         "quotes": [{"section": "mdna",
                     "text": "Data Center   segment\n revenue increased 107% "
                             "year-over-year"}]}])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "A grounded claim." in body


def test_followup_extensions_survive_malformed_shapes():
    fu = _fu_filing(panel="not-a-dict", segments={"a": 1},
                    filing_sections=7, disclosures="x", guidance_struct=[])
    analysis, bundle = fu_inputs(followups=[fu], prose=[
        {"ticker": "META", "panel_read": 3, "segment_read": None, "quotes": "x"}])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "From the filings" in body


def test_followup_extensions_keep_the_document_self_contained():
    analysis, bundle = fu_inputs(followups=[_fu_filing()], prose=[
        {"ticker": "META", "headline": "h", "body": "b",
         "panel_read": "p", "segment_read": "s",
         "quotes": [{"section": "mdna", "text": _GROUNDED}]}])
    out = render_profile(analysis, bundle, "2026-07-31")
    assert "<script" not in out
    assert "src=" not in out
    assert "<link" not in out


# --------------------------------------------------------------------------- #
# Spec E Task 8 — two-tab chrome. Hidden radio inputs plus `:checked ~ .panel`
# sibling selectors, ZERO JavaScript, one self-contained document. The tab list
# is data-driven: two entries today, six later.
# --------------------------------------------------------------------------- #
def _panels(n=2):
    return [{"id": f"t{i}", "label": f"Tab {i}", "count": i,
             "html": f"<section id='s{i}'>content {i}</section>"}
            for i in range(1, n + 1)]


def test_render_html_keeps_positional_api_and_adds_pure_keyword_inputs():
    import inspect
    assert list(inspect.signature(ch.render_html).parameters) == [
        "analysis", "bundle", "date", "desks", "timezone_label"]
    assert inspect.signature(ch.render_html).parameters["desks"].kind == inspect.Parameter.KEYWORD_ONLY


def test_document_renders_exactly_four_panels():
    body = _doc_body(render_profile(sample_analysis(), sample_bundle(), "2026-07-02"))
    assert body.count('class="panel"') == 4
    assert body.count('type="radio"') == 4


def test_world_news_is_the_default_checked_tab():
    body = _doc_body(render_profile(sample_analysis(), sample_bundle(), "2026-07-02"))
    world = body.find('id="tab-1"')
    second = body.find('id="tab-2"')
    assert -1 < world < second
    # Exactly one input is pre-selected, and it is the first (World News).
    assert body.count(" checked>") == 1
    assert " checked>" in body[world:second]
    assert "World News" in body
    assert "Finance" in body


def test_no_javascript_anywhere_in_the_document():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    for token in ("<script", "javascript:", "onclick", "onchange", "onload",
                  "addEventListener"):
        assert token not in out


def test_tab_switching_is_pure_css_sibling_selection():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    assert "#tab-1:checked ~ #panel-1" in out
    assert "#tab-2:checked ~ #panel-2" in out
    assert ".panel { display: none" in out


def test_tab_labels_carry_counts():
    analysis = sample_analysis()
    body = _doc_body(render_profile(analysis, sample_bundle(), "2026-07-02"))
    bar = body[body.find('<nav class="tabbar">'):body.find("</nav>")]
    assert "World News" in bar and "Finance" in bar
    # Two brief stories; one finance-brief story and no earnings blocks.
    assert '<span class="tabcount">2</span>' in bar
    assert '<span class="tabcount">1</span>' in bar
    # Both labels point at their own input — that is what makes them clickable.
    assert 'for="tab-1"' in bar and 'for="tab-2"' in bar


def test_finance_count_includes_writeups_and_followups():
    analysis, bundle = fu_inputs(followups=[_followup()])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    bar = body[body.find('<nav class="tabbar">'):body.find("</nav>")]
    # 1 finance-brief story + 1 write-up + 1 follow-up.
    assert '<span class="tabcount">3</span>' in bar


def test_tabs_are_data_driven_a_three_entry_list_renders_three_panels():
    out = ch._tabs(_panels(3))
    assert out.count('class="panel"') == 3
    assert out.count('type="radio"') == 3
    for i in (1, 2, 3):
        assert f'id="tab-{i}"' in out
        assert f'id="panel-{i}"' in out
        assert f"content {i}" in out


def test_an_empty_tab_renders_an_explicit_empty_state():
    out = ch._tabs([{"id": "world", "label": "World News", "count": 0, "html": ""},
                    {"id": "fin", "label": "Finance", "count": 1,
                     "html": "<p>something</p>"}])
    assert "No stories cleared the bar today." in out
    assert out.count('class="panel"') == 2


def test_tabs_carry_their_caller_id_for_addressability():
    out = ch._tabs(_panels(2))
    assert 'data-tab="t1"' in out
    assert 'data-tab="t2"' in out


def test_tab_labels_are_escaped():
    out = ch._tabs([{"id": "x", "label": "<script>alert(1)</script>",
                     "count": 1, "html": "<p>a</p>"}])
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_tabs_degrade_on_malformed_input():
    for panels in (None, [], "x", 7, ["not-a-dict"], [{}]):
        assert isinstance(ch._tabs(panels), str)     # never raises
    assert ch._tabs(None) == ""
    assert ch._tabs(["not-a-dict"]) == ""


def test_tabs_are_not_truncated_at_six():
    out = ch._tabs(_panels(9))
    assert out.count('class="panel"') == 9


def test_diagnostics_stay_outside_both_panels():
    body = _doc_body(render_profile(sample_analysis(), sample_bundle(), "2026-07-02"))
    opened = body.find('<div class="tabs">')
    closed = body.find("<!-- /tabs -->")
    diag = body.find('<footer class="diagnostics">')
    assert -1 < opened < closed < diag
    assert "World Intelligence &middot; 2026-07-02 (JST)" in body[diag:]


def test_the_masthead_sits_outside_the_panels():
    body = _doc_body(render_profile(sample_analysis(), sample_bundle(), "2026-07-02"))
    assert body.find('<header class="masthead">') < body.find('<div class="tabs">')


def test_the_tab_bar_is_sticky():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    bar_css = out[out.find(".tabbar {"):]
    assert "position: sticky" in bar_css[:400]


def test_both_tabs_still_render_every_existing_section():
    body = _doc_body(render_profile(sample_analysis(), sample_bundle(), "2026-07-02"))
    for marker in ('<section id="brief">', '<section id="signal">',
                   '<section id="threads">', '<section id="on-radar">',
                   '<section id="finance">'):
        assert marker in body


def test_tabbed_document_remains_self_contained():
    out = render_profile(sample_analysis(), sample_bundle(), "2026-07-02")
    assert "src=" not in out
    assert "<link" not in out
    assert "@import" not in out
    assert out.count("<style>") == 1


# --------------------------------------------------------------------------- #
# The Japan tab.
#
# `briefing.japan` items are Brief stories MINUS `region` — every one of them
# is Japan, so the schema drops the field rather than have the model restate
# the section name. That is the one thing this section has to get right on the
# renderer side: `_region_for` would otherwise keyword-derive them into the
# ASIA bucket and the panel would wear the same rail as a Taiwan story.
# --------------------------------------------------------------------------- #
def _japan_stories():
    return [
        {"headline": "Diet passes the supplementary budget",
         "why_it_matters": "It front-loads reconstruction spending into H2.",
         "body": "The lower house approved the package on Tuesday. " * 8,
         "sources": [{"outlet": "NHK", "url": "https://nhk.or.jp/a"}],
         "tags": ["Japan", "fiscal"]},
        {"headline": "Evacuation order lifted in Ishikawa",
         "why_it_matters": "Thousands return to homes still without water.",
         "body": "The prefecture lifted the order at dawn.",
         "sources": [],
         "tags": ["disaster"]},
    ]


_UNSET = object()   # so a test can pass japan=None and mean it


def _japan_analysis(stories=_UNSET):
    analysis = sample_analysis()
    analysis["briefing"]["japan"] = (
        _japan_stories() if stories is _UNSET else stories)
    return analysis


def test_canada_tab_renders_country_cards_with_its_own_bucket():
    analysis = sample_analysis()
    analysis["briefing"]["canada"] = [{
        "headline": "Ottawa introduces federal labour reform",
        "why_it_matters": "The bill changes strike intervention nationwide.",
        "body": "Ottawa introduced the bill on Monday. " * 8,
        "sources": [{"outlet": "iPolitics", "url": "https://example.ca/a"}],
        "tags": ["Canada", "labour"],
    }]
    body = _doc_body(render_profile(analysis, sample_bundle(), "2026-07-02"))
    assert 'data-tab="canada"' in body
    assert '<section id="canada">' in body
    assert "Ottawa introduces federal labour reform" in body
    assert "data-region='CANADA'" in body


def test_japan_is_the_middle_tab_between_world_news_and_finance():
    """Order is deliberate: the reader's own country sits between the world
    and the markets, not tacked on after them."""
    body = _doc_body(render_profile(_japan_analysis(), sample_bundle(), "2026-07-02"))
    bar = body[body.find('<nav class="tabbar">'):body.find("</nav>")]
    assert bar.find("World News") < bar.find("Japan") < bar.find("Finance")
    assert 'for="tab-1"' in bar and 'for="tab-2"' in bar and 'for="tab-3"' in bar
    assert body.find('id="panel-2"') < body.find('id="panel-3"')
    # The Japan panel is the second one, and Finance keeps the whole desk.
    japan = body.find('data-tab="japan"')
    finance = body.find('data-tab="finance"')
    assert -1 < japan < finance


def test_japan_tab_count_is_the_number_of_japan_stories():
    body = _doc_body(render_profile(_japan_analysis(), sample_bundle(), "2026-07-02"))
    bar = body[body.find('<nav class="tabbar">'):body.find("</nav>")]
    label = bar[bar.find("Japan"):]
    assert label.startswith('Japan <span class="tabcount">2</span>')


def test_japan_cards_render_headline_why_body_sources_and_tags():
    body = _doc_body(render_profile(_japan_analysis(), sample_bundle(), "2026-07-02"))
    section = body[body.find('<section id="japan">'):body.find("</section>",
                                                              body.find('<section id="japan">'))]
    assert '<h2 class="section">Japan</h2>' in section
    assert "Diet passes the supplementary budget" in section
    assert "It front-loads reconstruction spending into H2." in section
    assert "The lower house approved the package on Tuesday." in section
    assert 'href="https://nhk.or.jp/a"' in section
    assert ">NHK<" in section
    assert '<span class="pill">fiscal</span>' in section
    # Second story has no sources -> no chip block, but it still renders.
    assert "Evacuation order lifted in Ishikawa" in section


def test_japan_cards_carry_their_own_region_bucket_not_the_asia_fallback():
    """`_derive_region`'s keyword table maps "japan" to ASIA. Without an
    explicit bucket every card in this panel would silently inherit the rail
    colour of the China/Taiwan/Korea stories in The Brief."""
    section = japan_section(_japan_stories())
    assert section.count("data-region='JAPAN'") == 2
    assert "data-region='ASIA'" not in section
    assert "data-region-label='JAPAN'" in section


def test_japan_cards_reuse_the_shared_story_card():
    """Not a second card implementation: the same escaping, the same chips."""
    story = dict(_japan_stories()[0], headline="Diet <script>alert(1)</script>")
    section = japan_section([story])
    assert "<script>" not in section
    assert "&lt;script&gt;" in section
    assert section.count("<article class='card'") == 1


def test_an_empty_japan_list_renders_the_tab_empty_state_not_a_broken_box():
    """`[]` is the correct answer on a quiet domestic day (the prompt says so),
    so it must read as a deliberate empty panel, not a missing one."""
    for empty in ([], None):
        body = _doc_body(render_profile(_japan_analysis(empty),
                                        sample_bundle(), "2026-07-02"))
        assert '<section id="japan">' not in body
        assert body.count('class="panel"') == 4
        assert "No stories cleared the bar today." in body
        bar = body[body.find('<nav class="tabbar">'):body.find("</nav>")]
        assert 'Japan <span class="tabcount">0</span>' in bar


def test_japan_section_never_raises_on_garbage():
    for japan in (None, [], "x", 7, {"a": 1}, ["not-a-dict"], [None], [{}],
                  [{"headline": None, "sources": "nope", "tags": 3}]):
        assert isinstance(japan_section(japan), str)


def test_a_briefing_with_no_japan_key_at_all_still_renders():
    """The degraded world shape supplies `japan: []`, but a stored brief from
    before this section existed does not carry the key at all."""
    analysis = sample_analysis()
    assert "japan" not in analysis["briefing"]
    out = render_profile(analysis, sample_bundle(), "2026-07-02")
    assert "<html" in out
    assert _doc_body(out).count('class="panel"') == 4


def test_the_japan_panel_gets_its_own_rail_colour():
    """The one piece of CSS this section genuinely needs. The rail rule is
    scoped `#brief .card` + the finance-brief card, so without a third scope
    the Japan cards would be the only flat ones in the document; and without a
    JAPAN colour the bucket would resolve to the green GLOBAL fallback it is
    meant to be distinguishable from. Asserted against the <style> block on
    purpose — this is the stylesheet's contract, not the body's."""
    style = ch._style_for_desks(config.get().national_desks, 4)
    assert "--rc-japan:" in style
    assert '.card[data-region="JAPAN"]' in style
    assert "#japan .card::after" in style


def test_the_japan_panel_adds_no_javascript():
    out = render_profile(_japan_analysis(), sample_bundle(), "2026-07-02")
    for token in ("<script", "javascript:", "onclick", "onchange", "onload",
                  "addEventListener"):
        assert token not in out


# --------------------------------------------------------------------------- #
# Spec E Task 11 — the analytical rubric.
#
# The rubric lives in `prompts/finance.md` and the fields it fills are declared
# in `schema/finance.schema.json`, but BOTH are load-bearing for this renderer
# and neither is checkable from its own side. `_grounded_quotes` reads
# `panel_read`, `segment_read` and `quotes` BY NAME, so a rename on the schema
# or the prompt side breaks the join SILENTLY: every qualitative claim is
# dropped and the page merely gets quieter. Nothing raises and nothing logs.
#
# These tests pin the three-way agreement — what the prompt tells the model to
# write, what the schema lets it return, and what this file reads — plus the
# two rubric rules this renderer enforces itself: rule 2 (the verbatim quote,
# or the claim is dropped) and rule 8 (a contradiction is computed, never
# composed, so an empty panel frames nothing).
# --------------------------------------------------------------------------- #
_SCHEMA_DIR = os.path.join(os.path.dirname(_HERE), "worldintel", "data", "schema")
_PROMPT_DIR = os.path.join(os.path.dirname(_HERE), "worldintel", "data", "prompts")


def _finance_schema():
    with open(os.path.join(_SCHEMA_DIR, "finance.schema.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _finance_md():
    with open(os.path.join(_PROMPT_DIR, "finance.md"), encoding="utf-8") as fh:
        return fh.read()


def _schema_followup_item():
    return (_finance_schema()["properties"]["briefing"]["properties"]
            ["post_earnings"]["properties"]["followups"]["items"])


def _schema_scoreboard_read():
    return (_finance_schema()["properties"]["briefing"]["properties"]
            ["finance"]["properties"]["scoreboard_read"])


# --- the schema half of the join -------------------------------------------- #
def test_schema_followups_declare_exactly_the_fields_the_renderer_reads():
    """`_pe_followup_section` reads these six names and no others. A seventh
    would be prose the page never shows; a missing one is a dropped claim."""
    item = _schema_followup_item()
    assert set(item["properties"]) == {"ticker", "headline", "body",
                                       "panel_read", "segment_read", "quotes"}


def test_the_new_followup_fields_are_required_and_nullable():
    """Global Constraint 3. A property absent from `required` is rejected by
    strict --output-schema, which degrades the whole pass (`invalid_json_schema`,
    observed live). Nullable is what lets the model say nothing fired."""
    item = _schema_followup_item()
    assert set(item["required"]) == set(item["properties"])
    assert item["properties"]["panel_read"]["type"] == ["string", "null"]
    assert item["properties"]["segment_read"]["type"] == ["string", "null"]
    assert item["properties"]["quotes"]["type"] == ["array", "null"]


def test_quote_entries_carry_the_section_and_the_verbatim_text():
    quotes = _schema_followup_item()["properties"]["quotes"]["items"]
    assert set(quotes["properties"]) == {"section", "text"}
    assert set(quotes["required"]) == {"section", "text"}
    assert quotes["additionalProperties"] is False


def test_scoreboard_read_is_required_and_nullable_throughout():
    read = _schema_scoreboard_read()
    assert read["type"] == ["object", "null"]
    assert set(read["properties"]) == {"contradiction", "precedent", "watch"}
    assert set(read["required"]) == set(read["properties"])
    assert read["properties"]["contradiction"]["type"] == ["object", "null"]
    assert read["properties"]["precedent"]["type"] == ["string", "null"]
    assert read["properties"]["watch"]["type"] == ["array", "null"]
    assert "scoreboard_read" in (_finance_schema()["properties"]["briefing"]
                                 ["properties"]["finance"]["required"])


def test_the_cited_operators_carry_a_statement_and_a_story():
    """The contradiction and each watch item became objects on 2026-08-16 so
    they could CITE a published story — the one move the retired World →
    Markets bridge made that nothing else did. `story` is nullable because most
    operator items have no story behind them: a contradiction between two
    figures on the table is complete without one."""
    read = _schema_scoreboard_read()
    contra = read["properties"]["contradiction"]
    assert set(contra["properties"]) == {"statement", "story"}
    assert set(contra["required"]) == set(contra["properties"])
    assert contra["additionalProperties"] is False
    assert contra["properties"]["story"]["type"] == ["string", "null"]

    item = read["properties"]["watch"]["items"]
    assert set(item["properties"]) == {"condition", "story"}
    assert set(item["required"]) == set(item["properties"])
    assert item["additionalProperties"] is False
    assert item["properties"]["story"]["type"] == ["string", "null"]


def test_precedent_takes_no_story():
    """Dating a level against its own history is arithmetic over the
    scoreboard. A citation there would be decoration, and the absence is what
    marks the boundary for the model."""
    assert _schema_scoreboard_read()["properties"]["precedent"] == {
        "type": ["string", "null"]}


# --- the renderer half of the join ------------------------------------------ #
def test_a_followup_written_to_the_schemas_own_field_names_renders():
    """The prose dict is built BY KEY from the schema, so a rename there fails
    here rather than silently dropping every claim in production."""
    values = {"ticker": "META", "headline": "What the filing adds",
              "body": "Cash conversion held through the build.",
              "panel_read": "Collection days did the work here.",
              "segment_read": "Data Center is now the whole story.",
              "quotes": [{"section": "mdna", "text": _GROUNDED}]}
    prose = {name: values[name] for name in _schema_followup_item()["properties"]}
    analysis, bundle = fu_inputs(followups=[_fu_filing()], prose=[prose])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "Collection days did the work here." in body
    assert "Data Center is now the whole story." in body
    assert _GROUNDED in body


def test_a_schema_shaped_claim_without_a_matching_quote_is_dropped():
    """Rubric rule 2, enforced mechanically: the quote must occur in THIS
    follow-up's filing text. A paraphrase is not a quote."""
    values = {"ticker": "META", "headline": "What the filing adds",
              "body": "Cash conversion held through the build.",
              "panel_read": "Collection days did the work here.",
              "segment_read": "Data Center is now the whole story.",
              "quotes": [{"section": "mdna",
                          "text": "Data Center revenue roughly doubled year on year"}]}
    prose = {name: values[name] for name in _schema_followup_item()["properties"]}
    analysis, bundle = fu_inputs(followups=[_fu_filing()], prose=[prose])
    body = _doc_body(render_profile(analysis, bundle, "2026-07-31"))
    assert "Collection days did the work here." not in body
    assert "Data Center is now the whole story." not in body
    assert "roughly doubled" not in body
    assert "What the filing adds" in body      # the ungrounded fields only


def _fu_region(body):
    start = body.find('<div id="pe-followups">')
    assert start != -1
    return body[start:body.find("</div>\n", start)]


def test_a_panel_with_no_flags_produces_no_contradiction_framing():
    """Rubric rule 8, the most important one: a contradiction is COMPUTED,
    never composed. With `flags` and `contradictions` both empty the renderer
    frames nothing of its own and the boring version still renders in full."""
    fu = _fu_filing(panel=_panel(flags=[], contradictions=[]))
    analysis, bundle = fu_inputs(followups=[fu], prose=[
        {"ticker": "META", "headline": "A quiet quarter", "body": "Nothing broke.",
         "panel_read": None, "segment_read": None, "quotes": []}])
    region = _fu_region(_doc_body(render_profile(analysis, bundle, "2026-07-31")))
    assert "flagchip" not in region
    assert "contradict" not in region.lower()
    assert "A quiet quarter" in region and "Nothing broke." in region


def test_an_all_favourable_panel_renders_as_a_finding():
    """Both directions. AMD's DSO -3.1d and DIO -12.3d against 50% revenue
    growth is a clean quarter, and a clean quarter is a finding, not silence."""
    fu = _fu_filing(panel=_panel(flags=[
        _flag(metric="dso", direction="favourable",
              statement="DSO -3.1 days YoY to 57.4 while revenue grew 50.1%"),
        _flag(metric="dio", direction="favourable",
              statement="Inventory days -12.3 YoY to 66.8"),
    ], contradictions=[]))
    analysis, bundle = fu_inputs(followups=[fu])
    region = _fu_region(_doc_body(render_profile(analysis, bundle, "2026-07-31")))
    assert region.count("flagchip fav") == 2
    assert "unfav" not in region
    assert "Inventory days -12.3 YoY to 66.8" in region


# --- the prompt half of the join -------------------------------------------- #
def test_finance_prompt_names_the_eight_operators():
    finance = _finance_md()
    for operator in ("CONTRADICTION", "PRECEDENT", "REGIME", "FUNDING LEG",
                     "POSITION", "TWO CLOCKS", "STEELMAN", "WATCH"):
        assert operator in finance, operator


def test_finance_prompt_states_the_eight_hard_rules():
    finance = _finance_md()
    for rule in (
        "Never state a figure the panel did not compute",
        "Every qualitative filing claim carries a verbatim quote",
        "Motive only from exposure",
        "Never a date and never a price target",
        "Steelman before you rebut",
        "Name the strongest fact against your reading",
        "A repeat is continuity, not a discovery",
        "When nothing fires, write the boring version",
    ):
        assert rule in finance, rule


def test_finance_prompt_binds_the_quote_rule_to_the_renderers_drop():
    """If the prompt never tells the model that an unquoted claim is DROPPED,
    the model writes ungrounded prose and the page silently loses all of it."""
    finance = _finance_md()
    for token in ("`quotes`", "`panel_read`", "`segment_read`", "`scoreboard_read`"):
        assert token in finance, token
    assert "verbatim" in finance
    assert "dropped" in finance


def test_finance_prompt_forbids_composing_a_contradiction():
    finance = _finance_md()
    assert "`contradictions`" in finance
    assert "COMPUTED, never composed" in finance


def test_finance_prompt_states_direction_discipline_in_both_directions():
    finance = _finance_md()
    for token in ("`direction`", "`favourable`", "`unfavourable`", "`neutral`"):
        assert token in finance, token
    assert "clean quarter" in finance


def test_the_rubric_stays_out_of_the_world_pass_and_analysis_md():
    """The rubric is finance-only — Spec E Sec.8.2 is explicit that diluting the
    world-affairs selection rules with it is the failure mode the split exists
    to avoid — and `analysis.md` is never edited (Global Constraint 10)."""
    for name in ("world.md", "analysis.md"):
        with open(os.path.join(_PROMPT_DIR, name), encoding="utf-8") as fh:
            assert "When nothing fires, write the boring version" not in fh.read()


# --------------------------------------------------------------------------- #
# scoreboard_read (spec §6.5). Task 11 added the prompt instruction and the
# schema field; without a renderer Codex generates the read every morning and
# it never reaches the page -- no error, no raise, just silence.
# --------------------------------------------------------------------------- #
def _scoreboard_read_doc(read):
    analysis = sample_analysis()
    analysis["briefing"]["finance"]["scoreboard_read"] = read
    return _doc_body(render_profile(analysis, sample_bundle(), "2026-08-12"))


def test_scoreboard_read_renders_its_three_parts():
    body = _scoreboard_read_doc({
        "contradiction": "Ten-year yields rose while the dollar fell.",
        "precedent": "That pairing last held through the 2013 taper episode.",
        "watch": ["2s10s spread, currently +42bp", "DXY, currently 97.4"],
    })
    assert "Ten-year yields rose while the dollar fell." in body
    assert "That pairing last held through the 2013 taper episode." in body
    assert "2s10s spread, currently +42bp" in body
    assert "DXY, currently 97.4" in body


def test_scoreboard_read_absent_renders_nothing_and_does_not_raise():
    for read in (None, {}, {"contradiction": None, "precedent": None, "watch": None},
                 "garbage", [], {"contradiction": "   ", "precedent": "", "watch": []}):
        body = _scoreboard_read_doc(read)
        assert "scoreboard-read" not in body, f"empty read still rendered a block: {read!r}"


def test_scoreboard_read_renders_only_the_parts_that_are_present():
    body = _scoreboard_read_doc(
        {"contradiction": "Yields and the dollar diverged.", "precedent": None, "watch": []})
    assert "Yields and the dollar diverged." in body
    assert "scoreboard-read" in body


def test_scoreboard_read_does_not_displace_the_scoreboard_note():
    body = _scoreboard_read_doc(
        {"contradiction": "Yields and the dollar diverged.", "precedent": None, "watch": None})
    assert "Yields and the dollar diverged." in body
    assert "The gold slide" in body


# --------------------------------------------------------------------------- #
# Story citations (2026-08-16). The `World -> Markets` bridge was retired
# because it, `scoreboard_note` and `scoreboard_read.contradiction` stated the
# same observation three times over. The one thing the bridge did that nothing
# else did -- name a published story and tie it to a price -- moved here, as an
# optional citation on the contradiction and on each watch item.
#
# The grounding rule is `_grounded_quotes` applied to the scoreboard: a citation
# that does not resolve to a published headline drops the WHOLE item, not just
# the chip. "Crude rose on the Lebanon strikes" with the footnote quietly
# removed is a fabricated causal link that has lost its own evidence.
# --------------------------------------------------------------------------- #
def test_a_cited_world_story_renders_as_a_linked_chip():
    body = _scoreboard_read_doc({
        "contradiction": {"statement": "Yields rose while the VIX sank.",
                          "story": "Central banks hold the line"},
        "precedent": None, "watch": None})
    assert "Yields rose while the VIX sank." in body
    assert "https://reuters.com/a" in body
    assert "Central banks hold the line" in body


def test_a_citation_may_name_the_finance_desks_own_story():
    """Half the citable set is the world Brief, reached through
    `world_headlines`; the other half is the finance desk's own brief, which
    this pass wrote itself and can quote without being told."""
    body = _scoreboard_read_doc({
        "contradiction": None, "precedent": None,
        "watch": [{"condition": "SOX at 5,410; watch whether the guide holds",
                   "story": "Chipmaker guidance lifts semis"}]})
    assert "SOX at 5,410" in body
    assert "https://ft.com/c" in body


def test_an_operator_item_with_no_story_still_renders():
    """Most items have no story behind them — a contradiction between two
    figures on the table is complete without one — so `null` must never be
    mistaken for a failed citation."""
    body = _scoreboard_read_doc({
        "contradiction": {"statement": "Gold and the dollar rose together.",
                          "story": None},
        "precedent": None, "watch": [{"condition": "DXY at 97.4", "story": None}]})
    assert "Gold and the dollar rose together." in body
    assert "DXY at 97.4" in body
    assert "<span class=\"chip\">" not in body.split("scoreboard-read", 1)[-1][:400]


def test_an_unresolvable_citation_drops_its_whole_item():
    """Not just the chip. The sentence asserts a causal link to a story that
    was never published; stripping the citation would leave the assertion
    standing with its evidence quietly removed."""
    body = _scoreboard_read_doc({
        "contradiction": {"statement": "Crude rose on the Lebanon strikes.",
                          "story": "Israeli strikes in southern Lebanon"},
        "precedent": "The widest gap since March.", "watch": None})
    assert "Crude rose on the Lebanon strikes." not in body
    assert "Israeli strikes in southern Lebanon" not in body
    # ...and the ungrounded item takes nothing else down with it.
    assert "The widest gap since March." in body


def test_an_unresolvable_watch_citation_drops_only_that_line():
    body = _scoreboard_read_doc({
        "contradiction": None, "precedent": None,
        "watch": [{"condition": "Brent at 88.52", "story": "Central banks hold the line"},
                  {"condition": "VIX at 14.25", "story": "A headline nobody published"}]})
    assert "Brent at 88.52" in body
    assert "VIX at 14.25" not in body


def test_a_read_whose_every_citation_is_invented_renders_no_block():
    body = _scoreboard_read_doc({
        "contradiction": {"statement": "Made up.", "story": "Not a real headline"},
        "precedent": None,
        "watch": [{"condition": "Also made up", "story": "Nor is this one"}]})
    assert "scoreboard-read" not in body


def test_a_citation_is_matched_case_and_whitespace_insensitively():
    """Exact after normalisation, never fuzzy: a model that cannot reproduce a
    headline it was handed has written a new one rather than selected from the
    material it was given. Case and run-length are the only forgiveness."""
    body = _scoreboard_read_doc({
        "contradiction": {"statement": "Rates and equities disagreed.",
                          "story": "  CENTRAL   banks HOLD the line "},
        "precedent": None, "watch": None})
    assert "Rates and equities disagreed." in body
    assert "https://reuters.com/a" in body


def test_a_cited_story_with_no_url_renders_the_chip_unlinked_and_escaped():
    """`_link` already refuses to emit an href to a missing url, and the
    citation is model-supplied text like any other — it escapes."""
    body = _scoreboard_read_doc({
        "contradiction": {"statement": "The buried item moved nothing.",
                          "story": "Quiet story <script>alert(1)</script>"},
        "precedent": None, "watch": None})
    assert "The buried item moved nothing." in body
    assert "&lt;script&gt;" in body
    assert "<script>" not in body


def test_japan_stories_are_not_citable():
    """The finance pass is never shown the Japan section, so a citation naming
    one could only have been invented. A citable set the model cannot satisfy
    is a trap rather than a rule."""
    analysis = sample_analysis()
    analysis["briefing"]["japan"] = [
        {"headline": "Chiba floods leave eight dead", "why_it_matters": "w",
         "body": "b", "sources": [{"outlet": "Kyodo", "url": "https://kyodo.jp/x"}],
         "tags": ["japan"]}]
    analysis["briefing"]["finance"]["scoreboard_read"] = {
        "contradiction": {"statement": "The yen ignored the disaster.",
                          "story": "Chiba floods leave eight dead"},
        "precedent": None, "watch": None}
    body = _doc_body(render_profile(analysis, sample_bundle(), "2026-08-12"))
    assert "The yen ignored the disaster." not in body


def test_citable_stories_reads_the_briefing_not_the_bundle():
    """`world_headlines` lives only in the shallow copy `_finance_bundle` hands
    to Codex — the renderer is given the ORIGINAL bundle and never sees it. The
    published brief is the better source anyway: it is the record of what
    actually shipped, and it carries the urls the chip links to."""
    citable = ch._citable_stories(sample_analysis()["briefing"])
    assert citable["central banks hold the line"] == "https://reuters.com/a"
    assert citable["chipmaker guidance lifts semis"] == "https://ft.com/c"
    # A story that carried no sources is citable, just not linkable.
    assert citable["quiet story <script>alert(1)</script>"] == ""


def test_citable_stories_never_raises_on_a_malformed_briefing():
    for briefing in (None, {}, "garbage", [], {"brief": "nope"},
                     {"brief": [None, 7, {"headline": None}]},
                     {"finance": {"brief": [{"sources": "no"}]}}):
        assert isinstance(ch._citable_stories(briefing), dict)


def test_the_pre_citation_string_shape_still_renders():
    """The renderer is pure and coerces rather than trusts, everywhere. A
    briefing archived before the citation fields existed still renders — it
    simply carries no chips."""
    body = _scoreboard_read_doc({
        "contradiction": "Yields rose while the dollar fell.",
        "precedent": None,
        "watch": ["2s10s spread, currently +42bp"]})
    assert "Yields rose while the dollar fell." in body
    assert "2s10s spread, currently +42bp" in body
