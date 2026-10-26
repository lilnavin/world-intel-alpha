"""Tests for Module M (scripts/metrics.py).

Pure KPI computation + best-effort JSONL append, no network. Builds a
representative NEW-shape bundle + result and asserts the derived KPIs, then
exercises the resilience contract (defaults on empty input, append idempotency,
no-raise on a bad root).
"""

import json
import os
import sys

# Make scripts/ importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from worldintel.core import metrics as m  # noqa: E402


def sample_bundle():
    return {
        "date": "2026-07-04",
        "news": [
            {"title": "A", "source": "reuters", "url": "https://reuters.com/a",
             "tier": 1, "content": "Full article body here."},
            {"title": "B", "source": "guardian", "url": "https://guardian.com/b",
             "tier": 1, "content": ""},  # empty content -> not enriched
            {"title": "C", "source": "blogspot", "url": "https://x.blogspot.com/c",
             "tier": 3, "content": "Another real body."},
            {"title": "D", "source": "somewire", "url": "https://sw.com/d",
             "tier": 2},  # no content field at all
        ],
        "markets": {
            "indices": {
                "sp500": {"level": 5100.0, "change_pct": 0.4, "asof": "2026-07-03"},
                "nikkei": {"level": 39000.0, "change_pct": -2.1, "asof": "2026-07-03"},
            },
            "fx": {
                "usdjpy": {"level": 152.3, "change_pct": 0.9, "asof": "2026-07-03"},
            },
            "rates": {
                # Bigger |Δ%| than any instrument, but must be EXCLUDED as a mover.
                # Its asof lags the newest by 2 days -> a normal publication lag,
                # so rates_stale stays False (see the rates_stale block below).
                "us10y": {"level": 4.3, "change_pct": 5.0, "asof": "2026-07-01"},
            },
        },
        "filings": [
            {"company": "Acme", "form": "S-1", "filed": "2026-07-03"},
            {"company": "Beta", "form": "8-K", "filed": "2026-07-02"},
        ],
        "earnings_calendar": [
            {"ticker": "AAPL", "name": "Apple", "date": "2026-07-05"},
        ],
        "source_health": {
            "newsdata": {"ok": True, "note": ""},
            "guardian": {"ok": True, "note": ""},
            "edgar": {"ok": False, "note": "HTTPError: 403"},
            "twelvedata": {"ok": False, "note": "no api key"},
            "enrich": {"ok": True, "note": "2/3 enriched"},
        },
    }


def sample_result():
    return {
        "analysis_status": {"overall": "complete", "passes": {"world": {"success": True}, "finance": {"success": True}}},
        "briefing": {
            "brief": [
                {
                    "headline": "Fed rule change",
                    "why_it_matters": "Shifts compliance burden.",
                    "body": "A substantive body explaining the new rule in depth.",
                    "sources": [
                        {"outlet": "Federal Register",
                         "url": "https://www.federalregister.gov/documents/x"},
                    ],
                    "tags": ["US", "regulation"],
                },
                {
                    "headline": "BOJ watch",
                    "why_it_matters": "Yen path.",
                    # Hedging language must be caught.
                    "body": "Honestly the bundle only gives a headline here.",
                    "sources": [
                        {"outlet": "Guardian", "url": "https://guardian.com/b"},
                    ],
                    "tags": ["Japan", "macro"],
                },
                {
                    "headline": "Chip supply",
                    "why_it_matters": "Hardware pricing.",
                    "body": "Deeper reporting on foundry capacity constraints.",
                    "sources": [
                        {"outlet": "Reuters", "url": "https://reuters.com/a"},
                    ],
                    "tags": ["semiconductors"],  # no region tag -> "other"
                },
            ],
            "finance": {
                "brief": [
                    {
                        "headline": "Bank earnings beat",
                        "why_it_matters": "Credit cycle read.",
                        # Hedging in a FINANCE body must be caught too.
                        "body": "Solid quarter, though limited detail on provisions.",
                        "sources": [
                            {"outlet": "Reuters", "url": "https://reuters.com/fin"},
                        ],
                        "tags": ["US", "banks"],
                    },
                ],
                "scoreboard_note": "Nikkei led losses.",
                "filings_note": "",
                "earnings_note": "",
                "ownership_note": "",
            },
            "signal": {
                "top": {"story": "Grid rule buried", "reason": "Hits utilities",
                        "url": "https://example.com/grid"},
                "buried": [
                    {"story": "Supply shift", "reason": "Reroutes trade", "url": ""},
                    {"story": "Quiet merger", "reason": "Consolidation", "url": ""},
                ],
                "noise_line": "Ignored today: celebrity wedding, retail listicles.",
            },
            "threads": [
                {"slug": "boj", "today": "Hint at hike.", "since": "2026-06-01"},
            ],
            "on_radar": ["CPI print Friday"],
        }
    }


# --------------------------------------------------------------------------- #
# compute_metrics KPI correctness
# --------------------------------------------------------------------------- #
def test_fedreg_share_one_of_three():
    rec = m.compute_metrics(sample_bundle(), sample_result(), "2026-07-04")
    # 1 of 3 brief stories cites federalregister.gov -> ~0.33.
    assert rec["brief_count"] == 3
    assert round(rec["fedreg_share"], 2) == 0.33


def test_guardian_share():
    rec = m.compute_metrics(sample_bundle(), sample_result(), "2026-07-04")
    # 1 of 3 brief stories cites a guardian url.
    assert round(rec["guardian_share"], 2) == 0.33
    assert rec["guardian_cap_overridden"] is False


def test_hedging_count_catches_brief_and_finance_bodies():
    rec = m.compute_metrics(sample_bundle(), sample_result(), "2026-07-04")
    # 1 hedged Brief body ("the bundle only gives a headline") plus
    # 1 hedged finance.brief body ("limited detail").
    assert rec["hedging_count"] == 2


def test_enriched_count_counts_nonempty_content():
    rec = m.compute_metrics(sample_bundle(), sample_result(), "2026-07-04")
    # Items A and C have non-empty content; B is "" and D has none.
    assert rec["enriched_count"] == 2
    assert rec["news_pool_size"] == 4
    assert rec["enrich_note"] == "2/3 enriched"


def test_biggest_mover_excludes_rates():
    rec = m.compute_metrics(sample_bundle(), sample_result(), "2026-07-04")
    mover = rec["biggest_mover"]
    # Nikkei (-2.1%) is the largest |Δ%| once rates (+5.0%) is excluded.
    assert mover is not None
    assert mover["group"] == "indices"
    assert mover["key"] == "nikkei"
    assert mover["change_pct"] == -2.1


def test_tier_distribution_sums_to_pool():
    rec = m.compute_metrics(sample_bundle(), sample_result(), "2026-07-04")
    dist = rec["tier_distribution"]
    assert sum(dist.values()) == rec["news_pool_size"] == 4
    assert dist["1"] == 2 and dist["3"] == 1 and dist["2"] == 1


def test_data_gaps_lists_failed_sources():
    rec = m.compute_metrics(sample_bundle(), sample_result(), "2026-07-04")
    assert rec["data_gaps"] == ["edgar", "twelvedata"]
    assert rec["sources_failed"] == ["edgar", "twelvedata"]
    assert "newsdata" in rec["sources_ok"] and "guardian" in rec["sources_ok"]
    # enrich is reported separately, not as a source gap.
    assert "enrich" not in rec["data_gaps"]


def test_region_distribution_and_counts():
    rec = m.compute_metrics(sample_bundle(), sample_result(), "2026-07-04")
    dist = rec["region_distribution"]
    assert dist.get("Us") == 1  # "US" tag -> canonical title form
    assert dist.get("Japan") == 1
    assert dist.get("other") == 1
    assert rec["buried_count"] == 3  # 2 buried + signal-of-the-day top
    assert rec["noise_count"] == 1
    assert rec["threads_count"] == 1
    assert rec["filings_count"] == 2 == rec["filings_list_len"]
    assert rec["earnings_count"] == 1
    assert rec["codex_ok"] is True


def test_markets_covered_and_rates_stale():
    rec = m.compute_metrics(sample_bundle(), sample_result(), "2026-07-04")
    # 4 instruments carry a non-None level (sp500, nikkei, usdjpy, us10y).
    assert rec["markets_covered"] == 4
    # us10y is 2 days behind the newest instrument — normal publication lag.
    assert rec["rates_stale"] is False


def test_timings_echoed():
    timings = {"fetch": 1.2, "enrich": 0.4, "codex": 8.1, "total": 9.9}
    rec = m.compute_metrics(sample_bundle(), sample_result(), "2026-07-04", timings)
    assert rec["timings"] == timings


# --------------------------------------------------------------------------- #
# signal / finance KPIs (new shape)
# --------------------------------------------------------------------------- #
def test_top_signal_bumps_buried_count():
    rec = m.compute_metrics(sample_bundle(), sample_result(), "2026-07-04")
    assert rec["buried_count"] == 3  # len(buried)=2 + non-empty top story

    result = sample_result()
    result["briefing"]["signal"]["top"] = {"story": "", "reason": "", "url": ""}
    rec2 = m.compute_metrics(sample_bundle(), result, "2026-07-04")
    assert rec2["buried_count"] == 2  # all-empty top does not count


def test_noise_count_from_noise_line():
    rec = m.compute_metrics(sample_bundle(), sample_result(), "2026-07-04")
    assert rec["noise_count"] == 1

    result = sample_result()
    result["briefing"]["signal"]["noise_line"] = ""
    rec2 = m.compute_metrics(sample_bundle(), result, "2026-07-04")
    assert rec2["noise_count"] == 0


def test_finance_brief_count():
    rec = m.compute_metrics(sample_bundle(), sample_result(), "2026-07-04")
    assert rec["finance_brief_count"] == 1


# --------------------------------------------------------------------------- #
# scoreboard_read story citations (2026-08-16). `story` is nullable everywhere,
# so the model's cheapest output is null on every item -- in which case the
# World -> Markets bridge was retired for nothing. These two series are how that
# is noticed instead of assumed, and `_dropped` is how a model quietly inventing
# headlines is noticed at all: the renderer drops those items silently.
# --------------------------------------------------------------------------- #
def _with_read(read):
    result = sample_result()
    result["briefing"]["finance"]["scoreboard_read"] = read
    return m.compute_metrics(sample_bundle(), result, "2026-08-16")


def test_resolved_story_citations_are_counted():
    rec = _with_read({
        "contradiction": {"statement": "s", "story": "Chip supply"},
        "precedent": None,
        "watch": [{"condition": "c", "story": "Bank earnings beat"}]})
    assert rec["scoreboard_story_refs"] == 2
    assert rec["scoreboard_story_refs_dropped"] == 0


def test_invented_story_citations_are_counted_separately():
    rec = _with_read({
        "contradiction": {"statement": "s", "story": "A headline nobody published"},
        "precedent": None,
        "watch": [{"condition": "c", "story": "Chip supply"},
                  {"condition": "c2", "story": "Nor this one"}]})
    assert rec["scoreboard_story_refs"] == 1
    assert rec["scoreboard_story_refs_dropped"] == 2


def test_uncited_operator_items_count_as_neither():
    """`null` is the correct answer for an item with no story behind it, so it
    must not inflate either series — a zero on BOTH is the signal that the
    citation is going unused."""
    rec = _with_read({"contradiction": {"statement": "s", "story": None},
                      "precedent": "p",
                      "watch": [{"condition": "c", "story": None}]})
    assert rec["scoreboard_story_refs"] == 0
    assert rec["scoreboard_story_refs_dropped"] == 0


def test_story_citation_counters_default_to_zero_without_raising():
    for read in (None, {}, "garbage", [], {"watch": "nope"},
                 {"contradiction": "a pre-citation string", "watch": ["and one here"]}):
        rec = _with_read(read)
        assert rec["scoreboard_story_refs"] == 0
        assert rec["scoreboard_story_refs_dropped"] == 0


def test_missing_signal_and_finance_default_to_zero():
    result = sample_result()
    del result["briefing"]["signal"]
    del result["briefing"]["finance"]
    rec = m.compute_metrics(sample_bundle(), result, "2026-07-04")  # must not raise
    assert rec["buried_count"] == 0
    assert rec["noise_count"] == 0
    assert rec["finance_brief_count"] == 0
    # hedging still computed from the Brief bodies alone
    assert rec["hedging_count"] == 1


# --------------------------------------------------------------------------- #
# Resilience contract
# --------------------------------------------------------------------------- #
def test_empty_inputs_return_defaults_without_raising():
    rec = m.compute_metrics({}, {}, "2026-07-04")
    assert rec["date"] == "2026-07-04"
    assert rec["codex_ok"] is False
    assert rec["brief_count"] == 0
    assert rec["fedreg_share"] == 0.0
    assert rec["news_pool_size"] == 0
    assert rec["biggest_mover"] is None
    assert rec["rates_stale"] is False
    assert rec["data_gaps"] == []
    assert rec["timings"] is None
    assert rec["finance_brief_count"] == 0


def test_malformed_inputs_do_not_raise():
    # Deliberately wrong types everywhere.
    rec = m.compute_metrics(
        {"news": "nope", "markets": 5, "source_health": [1, 2]},
        {"briefing": "not a dict"},
        None,
    )
    assert isinstance(rec, dict)
    assert rec["date"] is None
    assert rec["news_pool_size"] == 0


# --------------------------------------------------------------------------- #
# append_metrics
# --------------------------------------------------------------------------- #
def test_append_writes_jsonl_and_is_append_idempotent(tmp_path):
    root = str(tmp_path)
    rec = m.compute_metrics(sample_bundle(), sample_result(), "2026-07-04")

    p1 = m.append_metrics(root, rec)
    p2 = m.append_metrics(root, rec)
    assert p1 is not None and p1 == p2

    path = os.path.join(root, "metrics", "runs.jsonl")
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
    # Two calls -> two lines, each a valid JSON object.
    assert len(lines) == 2
    parsed = json.loads(lines[0])
    assert parsed["date"] == "2026-07-04"
    assert parsed["fedreg_share"] == rec["fedreg_share"]


def test_append_bad_root_returns_none(tmp_path):
    # A file (not a dir) where the metrics dir would go -> makedirs fails.
    bad = tmp_path / "afile"
    bad.write_text("x", encoding="utf-8")
    assert m.append_metrics(str(bad), {"date": "2026-07-04"}) is None


# --------------------------------------------------------------------------- #
# compute_pool — audit of candidate pool
# --------------------------------------------------------------------------- #
def test_compute_pool_marks_picked_and_enriched():
    bundle = {"news": [
        {"title": "A", "source": "guardian", "tier": 1, "content": "full text",
         "url": "https://x.com/a"},
        {"title": "B", "source": "newsdata", "tier": 2, "url": "https://x.com/b"},
        {"title": "C", "source": "marketaux", "tier": 2, "content": "txt",
         "url": "https://x.com/c"},
    ]}
    result = {"briefing": {
        "brief": [{"sources": [{"outlet": "Guardian", "url": "https://x.com/A"}]}],
        "finance": {"brief": [{"sources": [{"outlet": "M", "url": "https://x.com/c"}]}]},
    }}
    pool = m.compute_pool(bundle, result)
    assert [p["picked"] for p in pool] == ["world", None, "finance"]
    assert [p["enriched"] for p in pool] == [True, False, True]
    assert pool[0] == {"title": "A", "source": "guardian", "tier": 1,
                       "enriched": True, "picked": "world"}


def test_compute_pool_never_raises_on_garbage():
    assert m.compute_pool(None, None) == []
    assert m.compute_pool({"news": "nope"}, {}) == []
    assert m.compute_pool({"news": [None, 42]}, {})[0]["title"] == ""


def test_compute_metrics_includes_pool():
    bundle = {"news": [{"title": "A", "source": "s", "tier": 1,
                        "url": "https://x.com/a"}]}
    rec = m.compute_metrics(bundle, {}, "2026-07-18")
    assert isinstance(rec["pool"], list) and len(rec["pool"]) == 1
    assert rec["pool"][0]["picked"] is None


def test_compute_metrics_includes_guardian_cap_debug():
    result = sample_result()
    result["_debug"] = {"guardian_selection": {
        "guardian_cap_overridden": True,
        "guardian_cap_override_reason": (
            "fewer than 2 non-Guardian tier-1/tier-2 enriched candidates "
            "available after dedupe"
        ),
        "guardian_cap_removed": 0,
        "guardian_picks_after": 3,
        "eligible_non_guardian_candidates": 1,
    }}
    rec = m.compute_metrics(sample_bundle(), result, "2026-07-04")
    assert rec["guardian_cap_overridden"] is True
    assert rec["guardian_cap_override_reason"].startswith("fewer than 2")
    assert rec["guardian_cap_removed"] == 0
    assert rec["guardian_picks"] == 3
    assert rec["eligible_non_guardian_candidates"] == 1


def test_hedging_count_uses_widened_meta_patterns():
    briefing = {"brief": [
        {"body": "It is unclear from the reporting who ordered the strike."},
        {"body": "Iran struck two tankers on Monday."},
    ], "finance": {"brief": []}}
    rec = m.compute_metrics({}, {"briefing": briefing}, "2026-07-18")
    assert rec["hedging_count"] == 1


# --------------------------------------------------------------------------- #
# Two-beat fundamentals KPIs (Spec C D8, Task 9). The lapse count is what
# decides whether FILING_WATCH_DAYS is right, and the stale -> resurfaced
# latency is the companion doc's top uncertainty; both start being measured
# from day one.
# --------------------------------------------------------------------------- #
def test_followup_and_fundamentals_counts():
    bundle = {
        "post_earnings_followups": [{"ticker": "META"}, {"ticker": "AAPL"}],
        "post_earnings_candidates": [
            {"ticker": "MSFT", "fundamentals": {"status": "ok"}},
            {"ticker": "AMZN", "fundamentals": {"status": "ok"}},
            {"ticker": "GOOGL", "fundamentals": {"status": "ok"}},
            {"ticker": "NVDA", "fundamentals": {"status": "stale"}},
            {"ticker": "TSLA", "fundamentals": None},
            {"ticker": "ZZZQ"},
        ],
    }
    rec = m.compute_metrics(bundle, {}, "2026-07-31")
    assert rec["followup_count"] == 2
    assert rec["fundamentals_ok_count"] == 3


def test_followup_and_fundamentals_counts_default_to_zero():
    rec = m.compute_metrics({}, {}, "2026-07-31")
    assert rec["followup_count"] == 0
    assert rec["fundamentals_ok_count"] == 0


def test_filings_expired_unread_is_recorded():
    """The 20-day filing-index window drops a matched filing with no error and
    no trace, so nothing in the run ever said it happened. Measured 2026-08-30:
    ~5/day expired unread against ~6/day published — half the backlog drain was
    invisible."""
    rec = m.compute_metrics({"filings_expired_unread": 5}, {}, "2026-08-30")
    assert rec["filings_expired_unread"] == 5


def test_filings_expired_unread_defaults_to_zero_not_none():
    """A None hole makes the series incomparable day to day, and day-to-day is
    the only way an expiry rate near the publish rate becomes visible — so an
    absent or malformed count reads as 0, never None."""
    for bundle in ({}, {"filings_expired_unread": None},
                   {"filings_expired_unread": "five"},
                   {"filings_expired_unread": [5]}):
        rec = m.compute_metrics(bundle, {}, "2026-08-30")
        assert rec["filings_expired_unread"] == 0


def test_followup_and_fundamentals_counts_survive_malformed_bundle():
    rec = m.compute_metrics(
        {"post_earnings_followups": "not-a-list",
         "post_earnings_candidates": ["not-a-dict", 7, None]},
        {}, "2026-07-31")
    assert rec["followup_count"] == 0
    assert rec["fundamentals_ok_count"] == 0


# --------------------------------------------------------------------------- #
# rates_stale â€” measures a genuine publication freeze, not a normal lag
#
# The old definition was `max(rates_asofs) < max(other_asofs)`: rates were stale
# whenever they were behind the single freshest instrument in ANY other group.
# FX bars roll later in the week than bond/equity series, so on 2026-08-02 rates
# sat at 2026-07-31 (Friday â€” the correct last session) while FX carried
# 2026-08-01, and the metric read True. It had been True for 9 consecutive runs
# without a single day of actually stale data.
# --------------------------------------------------------------------------- #
def _markets_with(rates_asof: str, other_asof: str) -> dict:
    return {
        "markets": {
            "indices": {"sp500": {"level": 7489.7, "change_pct": 0.7,
                                  "asof": other_asof}},
            "rates": {"us10y": {"level": 4.3, "change_pct": 0.1,
                                "asof": rates_asof}},
        }
    }


def test_rates_not_stale_when_fx_merely_rolls_past_the_bond_session():
    """The live 2026-08-02 case: rates on Friday, another group on Saturday."""
    rec = m.compute_metrics(_markets_with("2026-07-31", "2026-08-01"), {},
                            "2026-08-02")
    assert rec["rates_stale"] is False


def test_rates_not_stale_across_a_long_weekend():
    """Thursday rates vs a Monday equity close is a normal publication lag."""
    rec = m.compute_metrics(_markets_with("2026-07-30", "2026-08-03"), {},
                            "2026-08-03")
    assert rec["rates_stale"] is False


def test_rates_stale_on_a_genuine_freeze():
    """A week with no new rates print is the signal the metric exists for."""
    rec = m.compute_metrics(_markets_with("2026-07-24", "2026-07-31"), {},
                            "2026-07-31")
    assert rec["rates_stale"] is True


def test_rates_not_stale_when_current():
    rec = m.compute_metrics(_markets_with("2026-07-31", "2026-07-31"), {},
                            "2026-07-31")
    assert rec["rates_stale"] is False


def test_rates_never_stale_when_ahead_of_the_other_groups():
    rec = m.compute_metrics(_markets_with("2026-08-01", "2026-07-24"), {},
                            "2026-08-01")
    assert rec["rates_stale"] is False


def test_rates_stale_tolerates_datetime_and_date_asof_formats():
    """Twelve Data returns 'YYYY-MM-DD HH:MM:SS'; the rates path returns a bare
    date. Same calendar day must not read as a gap."""
    rec = m.compute_metrics(
        _markets_with("2026-07-31", "2026-07-31 16:00:00"), {}, "2026-07-31")
    assert rec["rates_stale"] is False


def test_rates_stale_defaults_false_on_unparseable_asof():
    rec = m.compute_metrics(_markets_with("not-a-date", "2026-07-31"), {},
                            "2026-07-31")
    assert rec["rates_stale"] is False


def test_rates_stale_needs_both_sides_present():
    only_rates = {"markets": {"rates": {"us10y": {"level": 4.3,
                                                  "asof": "2026-07-01"}}}}
    assert m.compute_metrics(only_rates, {}, "2026-07-31")["rates_stale"] is False


def test_desk_counts_from_bundle_news():
    bundle = {"news": [
        {"title": "a", "desk": "world"}, {"title": "b", "desk": "world"},
        {"title": "c", "desk": "japan"}, {"title": "d", "desk": "finance"},
        {"title": "e"},  # no desk -> not counted
    ]}
    rec = m.compute_metrics(bundle, {"briefing": {}}, "2026-08-02")
    assert rec["desk_counts"] == {"world": 2, "japan": 1, "finance": 1}


def test_feed_health_counts_ok_and_total():
    bundle = {"source_health": {
        "feed:bbc-world": {"ok": True}, "feed:nyt-home": {"ok": True},
        "feed:dead": {"ok": False}, "edgar": {"ok": True},  # non-feed ignored
    }}
    rec = m.compute_metrics(bundle, {"briefing": {}}, "2026-08-02")
    assert rec["feed_health"] == {"ok": 2, "total": 3}


def test_pool_size_raw_is_echoed():
    bundle = {"news_pool_size_raw": 1187, "news": [{"title": "x"}]}
    rec = m.compute_metrics(bundle, {"briefing": {}}, "2026-08-02")
    assert rec["pool_size_raw"] == 1187
    assert rec["news_pool_size"] == 1


def test_new_metrics_default_safely_on_garbage():
    rec = m.compute_metrics({}, {}, "2026-08-02")
    assert rec["desk_counts"] == {}
    assert rec["feed_health"] == {"ok": 0, "total": 0}
    assert rec["pool_size_raw"] == 0


def test_hedging_count_and_body_average_include_japan_bodies():
    """Japan stories share the Brief's shape and style contract, so their
    bodies join the same quality pool. Without this the Japan tab could pad
    freely and every quality series would stay flat."""
    result = {"briefing": {
        "brief": [{"body": "Iran struck two tankers on Monday."}],
        "japan": [{"body": "The bundle only gives a headline for this."}],
        "finance": {"brief": []},
    }}
    rec = m.compute_metrics({}, result, "2026-08-15")
    assert rec["hedging_count"] == 1


def test_japan_brief_count_is_recorded():
    result = {"briefing": {
        "brief": [], "finance": {"brief": []},
        "japan": [{"body": "a"}, {"body": "b"}, {"body": "c"}],
    }}
    assert m.compute_metrics({}, result, "2026-08-15")["japan_brief_count"] == 3


def test_japan_brief_count_defaults_to_zero_without_a_japan_section():
    assert m.compute_metrics({}, {"briefing": {"brief": []}},
                             "2026-08-15")["japan_brief_count"] == 0


def test_canada_brief_count_and_pool_attribution_are_recorded():
    result = {"briefing": {
        "brief": [], "finance": {"brief": []}, "japan": [],
        "canada": [{"body": "word " * 120,
                    "sources": [{"url": "https://example.ca/story"}]}],
    }}
    bundle = {"news": [{"title": "Canada acts", "source": "ipolitics",
                        "url": "https://example.ca/story", "tier": 2,
                        "content": "x" * 800}]}
    rec = m.compute_metrics(bundle, result, "2026-08-15")
    assert rec["canada_brief_count"] == 1
    assert rec["pool"][0]["picked"] == "canada"
