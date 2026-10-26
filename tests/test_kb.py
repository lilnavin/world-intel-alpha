"""Tests for Module B — the world-intel knowledge base (scripts/kb.py).

Run:  python -m pytest skills/personal/world-intel/tests/test_kb.py
Uses pytest tmp_path as the KB root; never touches the user's knowledge base.
"""
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

# Make scripts/ importable regardless of CWD.
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from worldintel.core import kb  # noqa: E402


# --------------------------------------------------------------------------- #
# ensure_kb
# --------------------------------------------------------------------------- #
def test_ensure_kb_creates_tree(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)

    assert os.path.isfile(os.path.join(root, "purpose.md"))
    assert os.path.isfile(os.path.join(root, "schema.md"))
    assert os.path.isfile(os.path.join(root, "state.json"))
    assert os.path.isfile(os.path.join(root, "wiki", "overview.md"))
    assert os.path.isfile(os.path.join(root, "wiki", "index.md"))
    for d in ("raw", "briefings",
              os.path.join("wiki", "entities"),
              os.path.join("wiki", "threads"),
              os.path.join("wiki", "markets"),
              os.path.join("wiki", "daily")):
        assert os.path.isdir(os.path.join(root, d)), d

    # empty state.json is valid JSON with the documented keys
    state = kb.read_state(root)
    assert state["expectations"] == []
    assert "source_health" in state


def test_ensure_kb_idempotent_no_clobber(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)

    # user edits a file; a second ensure must not clobber it
    overview = os.path.join(root, "wiki", "overview.md")
    with open(overview, "w", encoding="utf-8") as fh:
        fh.write("CUSTOM OVERVIEW")
    state = kb.read_state(root)
    state["last_run_date"] = "2026-06-28"
    kb.write_state(root, state)

    kb.ensure_kb(root)  # must not raise, must not clobber

    with open(overview, encoding="utf-8") as fh:
        assert fh.read() == "CUSTOM OVERVIEW"
    assert kb.read_state(root)["last_run_date"] == "2026-06-28"


# --------------------------------------------------------------------------- #
# apply_deltas — create then merge
# --------------------------------------------------------------------------- #
def _seed_delta():
    return {
        "entities_upsert": [{
            "slug": "boj",
            "type": "institution",
            "summary": "The Bank of Japan, the country's central bank.",
            "facts": ["Governor since 2023", "Holds policy rate near zero"],
            "threads": ["boj-normalization"],
        }],
        "threads_upsert": [{
            "slug": "boj-normalization",
            "status": "active",
            "summary": "BOJ slowly exiting ultra-loose policy.",
            "timeline_add": [{"date": "2026-04-10", "note": "Ended YCC."}],
            "watch": "Next policy meeting in July.",
        }],
    }


def test_apply_deltas_creates_pages(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)

    changed = kb.apply_deltas(root, _seed_delta())
    assert isinstance(changed, list)

    ent = os.path.join(root, "wiki", "entities", "boj.md")
    thr = os.path.join(root, "wiki", "threads", "boj-normalization.md")
    assert os.path.isfile(ent)
    assert os.path.isfile(thr)
    assert ent in changed and thr in changed

    meta, body = kb._read_page(ent)
    assert meta["slug"] == "boj"
    assert meta["type"] == "institution"
    assert "boj-normalization" in meta["threads"]
    assert "Governor since 2023" in body


def test_apply_deltas_merges_no_duplicate(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.apply_deltas(root, _seed_delta())

    # second apply, same slugs: must MERGE, not duplicate
    second = {
        "entities_upsert": [{
            "slug": "boj",
            "type": "institution",
            "summary": "Updated summary of the BOJ.",
            "facts": ["Holds policy rate near zero", "Owns >50% of JGBs"],  # 1 dup, 1 new
            "threads": ["boj-normalization"],
        }],
        "threads_upsert": [{
            "slug": "boj-normalization",
            "status": "active",
            "summary": "BOJ continues a cautious exit.",
            "timeline_add": [{"date": "2026-06-28", "note": "Hiked 25bp."}],
            "watch": "Watch JGB yields.",
        }],
    }
    kb.apply_deltas(root, second)

    # exactly one page each
    ent_dir = os.path.join(root, "wiki", "entities")
    thr_dir = os.path.join(root, "wiki", "threads")
    assert os.listdir(ent_dir) == ["boj.md"]
    assert os.listdir(thr_dir) == ["boj-normalization.md"]

    # entity: summary updated, facts merged (dedup), no duplicate fact
    emeta, ebody = kb._read_page(os.path.join(ent_dir, "boj.md"))
    assert "Updated summary" in ebody
    assert ebody.count("Holds policy rate near zero") == 1
    assert "Owns >50% of JGBs" in ebody

    # thread: timeline grew to 2 entries, summary updated
    ctx = kb.load_context(root)
    thr = next(t for t in ctx["active_threads"] if t["slug"] == "boj-normalization")
    assert "cautious exit" in thr["summary"]
    tmeta, tbody = kb._read_page(os.path.join(thr_dir, "boj-normalization.md"))
    assert "2026-04-10" in tbody and "2026-06-28" in tbody
    assert tbody.count("2026-04-10") == 1  # original entry not duplicated


# --------------------------------------------------------------------------- #
# expectations round-trip through state.json
# --------------------------------------------------------------------------- #
def test_expectations_add_and_resolve_roundtrip(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)

    add = {
        "threads_upsert": [{
            "slug": "us-inflation",
            "status": "active",
            "summary": "US disinflation path.",
            "expectations_add": [{
                "id": "us-cpi-jun",
                "metric": "US CPI YoY",
                "consensus": "2.9%",
                "release_date": "2026-07-10",
                "source": "consensus",
            }],
        }],
    }
    kb.apply_deltas(root, add)

    state = kb.read_state(root)
    ids = [e["id"] for e in state["expectations"]]
    assert "us-cpi-jun" in ids

    ctx = kb.load_context(root)
    assert any(e["id"] == "us-cpi-jun" for e in ctx["expectations"])

    # resolve it
    kb.apply_deltas(root, {"expectations_resolved": ["us-cpi-jun"]})
    state2 = kb.read_state(root)
    assert all(e["id"] != "us-cpi-jun" for e in state2["expectations"])


def test_expectations_no_duplicate_on_readd(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    delta = {"threads_upsert": [{
        "slug": "us-inflation", "status": "active", "summary": "x",
        "expectations_add": [{"id": "us-cpi-jun", "metric": "CPI",
                              "consensus": "2.9%", "release_date": "2026-07-10",
                              "source": "x"}],
    }]}
    kb.apply_deltas(root, delta)
    kb.apply_deltas(root, delta)  # same id again
    state = kb.read_state(root)
    assert [e["id"] for e in state["expectations"]].count("us-cpi-jun") == 1


# --------------------------------------------------------------------------- #
# overview_replace
# --------------------------------------------------------------------------- #
def test_overview_replace(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    changed = kb.apply_deltas(root, {"overview_replace": "# State of the World\n\nNew text."})
    overview = os.path.join(root, "wiki", "overview.md")
    assert overview in changed
    with open(overview, encoding="utf-8") as fh:
        assert fh.read() == "# State of the World\n\nNew text."


# --------------------------------------------------------------------------- #
# malformed delta is skipped, valid ones applied; never raises
# --------------------------------------------------------------------------- #
def test_malformed_delta_skipped(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)

    deltas = {
        "entities_upsert": [
            {"type": "country", "summary": "no slug here"},   # malformed: missing slug
            "not even a dict",                                  # malformed: wrong type
            {"slug": "japan", "type": "country",
             "summary": "Island nation.", "facts": [], "threads": []},  # valid
        ],
    }
    changed = kb.apply_deltas(root, deltas)  # must not raise

    assert os.path.isfile(os.path.join(root, "wiki", "entities", "japan.md"))
    assert any("japan.md" in c for c in changed)
    # the malformed ones produced no page
    assert os.listdir(os.path.join(root, "wiki", "entities")) == ["japan.md"]


def test_apply_deltas_never_raises_on_garbage(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    # totally malformed top-level shapes should be tolerated
    assert kb.apply_deltas(root, {}) == []
    kb.apply_deltas(root, {"entities_upsert": "nope", "threads_upsert": None,
                           "expectations_resolved": "x"})


# --------------------------------------------------------------------------- #
# load_context returns only ACTIVE threads
# --------------------------------------------------------------------------- #
def test_load_context_only_active_threads(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.apply_deltas(root, {"threads_upsert": [
        {"slug": "active-one", "status": "active", "summary": "live",
         "watch": "watch me", "timeline_add": [{"date": "2026-06-01", "note": "n"}]},
        {"slug": "done-one", "status": "resolved", "summary": "over",
         "timeline_add": [{"date": "2026-05-01", "note": "n"}]},
    ]})
    kb.apply_deltas(root, {"overview_replace": "world overview"})
    kb.update_scoreboard(root, {"indices": {"sp500": {"level": 5200.0,
                                                      "change_pct": 0.4,
                                                      "asof": "2026-06-28"}}})

    ctx = kb.load_context(root)
    slugs = [t["slug"] for t in ctx["active_threads"]]
    assert "active-one" in slugs
    assert "done-one" not in slugs

    active = ctx["active_threads"][0]
    assert active["title"]
    assert active["summary"] == "live"
    assert active["watch"] == "watch me"
    assert ctx["overview"] == "world overview"
    assert "sp500" in ctx["scoreboard"]
    assert isinstance(ctx["expectations"], list)


# --------------------------------------------------------------------------- #
# small writers
# --------------------------------------------------------------------------- #
def test_write_daily_and_append_log(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.write_daily(root, "2026-06-28", "daily brief body")
    p = os.path.join(root, "wiki", "daily", "2026-06-28.md")
    assert os.path.isfile(p)
    with open(p, encoding="utf-8") as fh:
        assert fh.read() == "daily brief body"

    kb.append_log(root, "line one")
    kb.append_log(root, "line two")
    with open(os.path.join(root, "wiki", "log.md"), encoding="utf-8") as fh:
        text = fh.read()
    assert "line one" in text and "line two" in text
    assert text.index("line one") < text.index("line two")


def test_update_scoreboard_handles_missing(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    # None values and missing sections must not crash
    kb.update_scoreboard(root, {
        "indices": {"nikkei": {"level": None, "change_pct": None, "asof": "2026-06-28"}},
        "fx": {"usdjpy": {"level": 157.2, "change_pct": -0.1, "asof": "2026-06-28"}},
    })
    p = os.path.join(root, "wiki", "markets", "scoreboard.md")
    with open(p, encoding="utf-8") as fh:
        text = fh.read()
    assert "nikkei" in text and "usdjpy" in text


# --------------------------------------------------------------------------- #
# record_buried — novelty-gate memory for the Signal section (state.json)
# --------------------------------------------------------------------------- #
def test_record_buried_roundtrips_through_load_context(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.record_buried(root, ["EEOC rescission", "Live Nation probe"], "2026-07-11")

    ctx = kb.load_context(root)
    assert ctx["recent_buried"] == [
        {"story": "EEOC rescission", "date": "2026-07-11"},
        {"story": "Live Nation probe", "date": "2026-07-11"},
    ]


def test_record_buried_prunes_entries_older_than_seven_days(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.record_buried(root, ["old story"], "2026-07-01")     # 10 days before
    kb.record_buried(root, ["edge story"], "2026-07-04")    # exactly 7 days before
    kb.record_buried(root, ["new story"], "2026-07-11")

    stories = [e["story"] for e in kb.load_context(root)["recent_buried"]]
    assert "old story" not in stories   # older than 7 days -> pruned
    assert "edge story" in stories      # exactly 7 days -> kept
    assert "new story" in stories


def test_record_buried_drops_malformed_old_entries(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    state = kb.read_state(root)
    state["recent_buried"] = [{"story": "bad date", "date": "not-a-date"},
                              {"story": 42, "date": None},
                              "not even a dict"]
    kb.write_state(root, state)

    kb.record_buried(root, ["fresh"], "2026-07-11")  # must not raise
    entries = kb.load_context(root)["recent_buried"]
    assert entries == [{"story": "fresh", "date": "2026-07-11"}]


def test_record_buried_skips_blank_nonstring_and_duplicates(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.record_buried(root, ["real", "", "   ", None, 42, "real"], "2026-07-11")
    kb.record_buried(root, ["real"], "2026-07-11")  # same (story, date) again

    entries = kb.load_context(root)["recent_buried"]
    assert entries == [{"story": "real", "date": "2026-07-11"}]


def test_record_buried_never_raises_on_missing_or_corrupt_state(tmp_path):
    root = str(tmp_path)
    # state.json missing entirely (no ensure_kb): must not raise
    kb.record_buried(root, ["a"], "2026-07-11")
    # corrupt state.json: must not raise
    with open(os.path.join(root, "state.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    kb.record_buried(root, ["b"], "2026-07-11")
    # garbage arguments: must not raise
    kb.record_buried(root, "not-a-list", None)


def test_load_context_recent_buried_empty_when_absent(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    assert kb.load_context(root)["recent_buried"] == []

    # corrupt state.json -> read_state falls back to empty state -> []
    with open(os.path.join(root, "state.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert kb.load_context(root)["recent_buried"] == []


def test_record_buried_preserves_other_state_keys(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    state = kb.read_state(root)
    state["expectations"] = [{"id": "us-cpi-jun", "metric": "CPI"}]
    state["last_run_date"] = "2026-07-10"
    kb.write_state(root, state)

    kb.record_buried(root, ["a"], "2026-07-11")

    after = kb.read_state(root)
    assert after["expectations"] == [{"id": "us-cpi-jun", "metric": "CPI"}]
    assert after["last_run_date"] == "2026-07-10"


# --------------------------------------------------------------------------- #
# load_context entity dossiers — matched against today's news
# --------------------------------------------------------------------------- #
def _seed_entity(root, slug, name=None, aliases=None, facts=None, summary="s"):
    kb.apply_deltas(root, {"entities_upsert": [{
        "slug": slug, "type": "institution", "summary": summary,
        "facts": facts or [], "threads": [],
    }]})
    # apply_deltas defaults name to slug and can't set aliases; patch frontmatter
    path = os.path.join(root, "wiki", "entities", f"{slug}.md")
    meta, body = kb._read_page(path)
    if name:
        meta["name"] = name
    if aliases:
        meta["aliases"] = aliases
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(kb._render_page(meta, body))


def _news(*titles):
    return [{"title": t, "summary": ""} for t in titles]


def test_load_context_includes_entities_matched_by_name_or_alias(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    _seed_entity(root, "boj", name="Bank of Japan", aliases=["BOJ"],
                 facts=["Governor since 2023"], summary="Japan's central bank.")
    _seed_entity(root, "apple", name="Apple", summary="iPhone maker.")

    ctx = kb.load_context(root, news=_news("BOJ holds rates steady"))
    slugs = [e["slug"] for e in ctx["entities"]]
    assert slugs == ["boj"]
    ent = ctx["entities"][0]
    assert ent["name"] == "Bank of Japan"
    assert ent["summary"] == "Japan's central bank."
    assert ent["facts"] == ["Governor since 2023"]

    # full name matches too, case-insensitively, and in summaries not just titles
    ctx2 = kb.load_context(root, news=[{"title": "x", "summary": "the bank of japan met"}])
    assert [e["slug"] for e in ctx2["entities"]] == ["boj"]


def test_entity_match_word_boundaries_and_short_acronym_case(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    _seed_entity(root, "united-states", name="United States", aliases=["US"])
    _seed_entity(root, "fed", name="Fed")

    # lowercase word "us" must NOT match the acronym alias "US"
    ctx = kb.load_context(root, news=_news("give us a break, fed the dog"))
    assert ctx["entities"] == []

    # substring inside a word must not match ("USAID" contains "US")
    ctx = kb.load_context(root, news=_news("USAID program review"))
    assert ctx["entities"] == []

    ctx = kb.load_context(root, news=_news("US and Fed policy diverge"))
    assert sorted(e["slug"] for e in ctx["entities"]) == ["fed", "united-states"]


def test_entity_match_on_raw_pipeline_pages(tmp_path):
    """Pages as the pipeline actually writes them: name defaults to the slug
    (lowercase, hyphenated) and aliases are empty — matching must still work."""
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.apply_deltas(root, {"entities_upsert": [
        {"slug": "fast-retailing", "type": "company", "summary": "Uniqlo parent."},
        {"slug": "iran", "type": "country", "summary": "Central to escalation."},
    ]})

    ctx = kb.load_context(root, news=_news("Fast Retailing raises guidance",
                                           "Iran signals openness to talks"))
    assert sorted(e["slug"] for e in ctx["entities"]) == ["fast-retailing", "iran"]

    # short lowercase name only matches a capitalized word: "iran" the entity
    # must not fire on an accidental lowercase substring-free word elsewhere
    ctx2 = kb.load_context(root, news=_news("we ran fast, retailing nothing"))
    assert ctx2["entities"] == []


def test_entities_ranked_by_mentions_and_capped(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    for i in range(10):
        _seed_entity(root, f"corp-{i}", name=f"Zencorp{i}")
    _seed_entity(root, "star", name="Starco")

    news = _news(*[f"Zencorp{i} update" for i in range(10)])
    news += _news("Starco rises", "Starco again", "more Starco")
    ctx = kb.load_context(root, news=news)

    assert len(ctx["entities"]) == 8          # capped
    assert ctx["entities"][0]["slug"] == "star"  # most-mentioned first


def test_entity_facts_capped_to_newest_eight(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    _seed_entity(root, "boj", name="Bank of Japan",
                 facts=[f"fact {i}" for i in range(12)])
    ctx = kb.load_context(root, news=_news("Bank of Japan meets"))
    facts = ctx["entities"][0]["facts"]
    assert len(facts) == 8
    assert facts[-1] == "fact 11"   # facts append over time -> tail is newest
    assert "fact 0" not in facts


def test_load_context_entities_empty_without_news_and_never_raises(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    assert kb.load_context(root)["entities"] == []
    assert kb.load_context(root, news=None)["entities"] == []
    assert kb.load_context(root, news=[])["entities"] == []

    # garbage news items and a garbage entity page must not raise
    with open(os.path.join(root, "wiki", "entities", "junk.md"), "w",
              encoding="utf-8") as fh:
        fh.write("no frontmatter, no name")
    ctx = kb.load_context(root, news=["not a dict", {"title": None}, 42])
    assert ctx["entities"] == []


# --------------------------------------------------------------------------- #
# frontmatter round-trip (internal helper) incl. unicode + lists
# --------------------------------------------------------------------------- #
def test_frontmatter_roundtrip_unicode_and_lists(tmp_path):
    meta = {"slug": "boj", "name": "Bank of Japan", "type": "institution",
            "aliases": ["BOJ", "日銀"], "threads": ["boj-normalization"]}
    text = kb._render_page(meta, "Body text.\n")
    meta2, body2 = kb._parse_page(text)
    assert meta2["slug"] == "boj"
    assert meta2["aliases"] == ["BOJ", "日銀"]
    assert meta2["threads"] == ["boj-normalization"]
    assert body2.strip() == "Body text."


def test_update_scoreboard_includes_horizon_context(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.update_scoreboard(root, {
        "indices": {"sp500": {"level": 5200.0, "change_pct": 0.4,
                              "asof": "2026-07-11", "ytd_pct": 10.0,
                              "d90_pct": 4.8, "off_high_pct": 8.3}},
        "rates": {"us10y": {"level": 4.37, "change_pct": 2.58, "change_abs": 0.11,
                            "asof": "2026-07-11", "ytd_abs": 0.48, "d90_abs": 0.12}},
        "risk": {"vix": {"level": 14.2, "change_pct": 1.0, "asof": "2026-07-11",
                         "pctl_1y": 32}},
    })
    with open(os.path.join(root, "wiki", "markets", "scoreboard.md"),
              encoding="utf-8") as fh:
        text = fh.read()
    assert "YTD +10.0%" in text and "90d +4.8%" in text and "8.3% off high" in text
    assert "YTD +48bp" in text and "90d +12bp" in text
    assert "1y pctile 32" in text


def test_update_scoreboard_ranks_fx_rather_than_measuring_it_off_the_high(tmp_path):
    """The model's copy of the scoreboard has to carry the same FX stats the
    rendered table does, or the prose cites a figure the reader cannot check."""
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.update_scoreboard(root, {
        "fx": {"usdjpy": {"level": 159.305, "change_pct": 0.31,
                          "asof": "2026-08-16", "ytd_pct": 1.8, "d90_pct": 0.3,
                          "off_high_pct": 2.8, "pctl_1y": 75}},
    })
    with open(os.path.join(root, "wiki", "markets", "scoreboard.md"),
              encoding="utf-8") as fh:
        text = fh.read()
    assert "YTD +1.8%" in text and "90d +0.3%" in text
    assert "1y pctile 75" in text
    assert "off high" not in text


# --------------------------------------------------------------------------- #
# record_seen_articles / load_seen — article-level novelty memory
# --------------------------------------------------------------------------- #
def test_record_seen_articles_round_trips(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    articles = [
        {"title": "Cabinet approves emergency energy package",
         "url": "https://bbc.co.uk/news/1"},
        {"title": "Bank of Japan signals policy shift",
         "url": "https://asia.nikkei.com/2"},
    ]
    kb.record_seen_articles(root, articles, "2026-08-02")
    seen = kb.load_seen(root, "2026-08-02")
    assert len(seen) == 2
    from worldintel.core import select_news
    assert select_news.article_hash(articles[0]) in seen


def test_record_seen_articles_does_not_duplicate(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    art = [{"title": "Same story every day", "url": "https://bbc.co.uk/news/1"}]
    kb.record_seen_articles(root, art, "2026-08-02")
    kb.record_seen_articles(root, art, "2026-08-02")
    state = kb.read_state(root)
    assert len(state["seen_story_hashes"]) == 1


def test_load_seen_prunes_outside_14_day_window(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.record_seen_articles(root, [{"title": "Old story here now",
                                    "url": "https://e.com/old"}], "2026-07-01")
    kb.record_seen_articles(root, [{"title": "Fresh story here now",
                                    "url": "https://e.com/new"}], "2026-08-02")
    assert len(kb.load_seen(root, "2026-08-02")) == 1


def test_record_seen_articles_survives_malformed_input(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.record_seen_articles(root, [None, {}, {"title": "Real story here now",
                                             "url": "https://e.com/a"}],
                            "2026-08-02")
    assert len(kb.load_seen(root, "2026-08-02")) == 1


def test_load_seen_on_missing_state_returns_empty(tmp_path):
    assert kb.load_seen(str(tmp_path / "nothing"), "2026-08-02") == set()


# --------------------------------------------------------------------------- #
# append_scoreboard_history / load_scoreboard_history — persisted market series
# --------------------------------------------------------------------------- #
def _sample_markets():
    return {
        "indices": {"sp500": {"level": 7753.11, "change_pct": -0.058,
                              "asof": "2026-08-10"}},
        "rates": {"us10y": {"level": 4.21, "change_abs": 0.03,
                            "asof": "2026-08-10"}},
    }


def test_append_scoreboard_history_round_trips(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.append_scoreboard_history(root, _sample_markets(), "2026-08-11")

    rows = kb.load_scoreboard_history(root)
    assert len(rows) == 1
    row = rows[0]
    assert row["date"] == "2026-08-11"
    assert row["instruments"]["sp500"] == {"level": 7753.11,
                                           "change_pct": -0.058,
                                           "asof": "2026-08-10"}
    assert row["instruments"]["us10y"] == {"level": 4.21, "change_abs": 0.03,
                                           "asof": "2026-08-10"}
    # absent keys are omitted, not written as null
    assert "change_abs" not in row["instruments"]["sp500"]
    assert "change_pct" not in row["instruments"]["us10y"]


def test_append_scoreboard_history_idempotent_on_date(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.append_scoreboard_history(root, _sample_markets(), "2026-08-11")

    updated = {"indices": {"sp500": {"level": 7800.0, "change_pct": 0.6,
                                     "asof": "2026-08-11"}}}
    kb.append_scoreboard_history(root, updated, "2026-08-11")  # --force re-run

    rows = kb.load_scoreboard_history(root)
    assert len(rows) == 1  # never two rows for one date
    assert rows[0]["instruments"] == {"sp500": {"level": 7800.0,
                                                "change_pct": 0.6,
                                                "asof": "2026-08-11"}}


def test_append_scoreboard_history_prunes_beyond_retention(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    today = "2026-08-11"
    cutoff = datetime.strptime(today, "%Y-%m-%d") - timedelta(days=kb.HISTORY_RETENTION_DAYS)
    old_date = (cutoff - timedelta(days=1)).strftime("%Y-%m-%d")   # 1 day beyond retention
    kept_date = (cutoff + timedelta(days=1)).strftime("%Y-%m-%d")  # 1 day within retention

    kb.append_scoreboard_history(root, _sample_markets(), old_date)
    kb.append_scoreboard_history(root, _sample_markets(), kept_date)
    kb.append_scoreboard_history(root, _sample_markets(), today)  # today's run prunes

    rows = kb.load_scoreboard_history(root, days=10000)
    dates = [r["date"] for r in rows]
    assert old_date not in dates
    assert kept_date in dates
    assert today in dates


def test_load_scoreboard_history_oldest_first_and_windowed(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    # Appended out of chronological order. Note append_scoreboard_history
    # rewrites the whole file sorted on every call, so by itself this does
    # NOT isolate load_scoreboard_history's own sort (its resort masks /
    # is masked by append's resort) — see the two on-disk tests below for
    # that. This test still covers the end-to-end oldest-first + windowing
    # contract through the normal read/write path.
    for d in ("2026-08-10", "2026-08-08", "2026-08-11", "2026-08-09"):
        kb.append_scoreboard_history(root, _sample_markets(), d)

    all_rows = kb.load_scoreboard_history(root, days=10000)
    assert [r["date"] for r in all_rows] == [
        "2026-08-08", "2026-08-09", "2026-08-10", "2026-08-11"]

    windowed = kb.load_scoreboard_history(root, days=2)
    assert [r["date"] for r in windowed] == ["2026-08-10", "2026-08-11"]


def _write_raw_history(root, rows):
    """Write history.jsonl directly (bypassing append_scoreboard_history) so
    a test can put genuinely unsorted rows on disk without going through
    either function's own sort."""
    path = os.path.join(root, "wiki", "markets", "history.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return path


def test_load_scoreboard_history_sorts_an_unsorted_file(tmp_path):
    """Pins load_scoreboard_history's own sort in isolation: the file on
    disk is unsorted and was never touched by append_scoreboard_history, so
    only load's sort can produce oldest-first output."""
    root = str(tmp_path)
    kb.ensure_kb(root)
    _write_raw_history(root, [
        {"date": "2026-08-10", "instruments": {}},
        {"date": "2026-08-08", "instruments": {}},
        {"date": "2026-08-11", "instruments": {}},
        {"date": "2026-08-09", "instruments": {}},
    ])

    rows = kb.load_scoreboard_history(root, days=10000)
    assert [r["date"] for r in rows] == [
        "2026-08-08", "2026-08-09", "2026-08-10", "2026-08-11"]


def test_append_scoreboard_history_sorts_file_on_disk(tmp_path):
    """Pins append_scoreboard_history's own sort in isolation: seed an
    unsorted file directly, call append once, then read the raw file bytes
    (NOT via load_scoreboard_history, which would mask this with its own
    sort) and assert the on-disk order is oldest-first."""
    root = str(tmp_path)
    kb.ensure_kb(root)
    path = _write_raw_history(root, [
        {"date": "2026-08-10", "instruments": {}},
        {"date": "2026-08-08", "instruments": {}},
        {"date": "2026-08-11", "instruments": {}},
    ])

    kb.append_scoreboard_history(root, _sample_markets(), "2026-08-09")

    with open(path, encoding="utf-8") as fh:
        on_disk = [json.loads(line) for line in fh if line.strip()]
    assert [r["date"] for r in on_disk] == [
        "2026-08-08", "2026-08-09", "2026-08-10", "2026-08-11"]


def test_load_scoreboard_history_missing_file_returns_empty(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    assert kb.load_scoreboard_history(root) == []


def test_load_scoreboard_history_skips_malformed_line(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.append_scoreboard_history(root, _sample_markets(), "2026-08-10")
    path = os.path.join(root, "wiki", "markets", "history.jsonl")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("not valid json\n")

    rows = kb.load_scoreboard_history(root)
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-08-10"


def test_append_scoreboard_history_never_raises_on_garbage(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.append_scoreboard_history(root, "not a dict", "2026-08-11")
    kb.append_scoreboard_history(root, None, None)
    kb.append_scoreboard_history(root, {"indices": "not a dict"}, "2026-08-12")
    # no exception raised; whatever state resulted is still readable
    assert isinstance(kb.load_scoreboard_history(root), list)


def test_append_scoreboard_history_leaves_markdown_unchanged(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.update_scoreboard(root, _sample_markets())
    md_path = os.path.join(root, "wiki", "markets", "scoreboard.md")
    with open(md_path, encoding="utf-8") as fh:
        before = fh.read()

    kb.append_scoreboard_history(root, _sample_markets(), "2026-08-11")

    with open(md_path, encoding="utf-8") as fh:
        after = fh.read()
    assert before == after


# --------------------------------------------------------------------------- #
# record_company_quarter / load_company_history — companies/ KB wing
# --------------------------------------------------------------------------- #
def _sample_quarter(period_end="2026-06-30", revenue=7440.0):
    return {"period_end": period_end, "ticker": "AMD", "metrics": {"revenue": revenue},
            "flags": []}


def test_record_company_quarter_round_trips(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.record_company_quarter(root, "AMD", _sample_quarter())

    history = kb.load_company_history(root, "AMD")
    assert len(history) == 1
    assert history[0]["period_end"] == "2026-06-30"
    assert history[0]["metrics"]["revenue"] == 7440.0

    path = os.path.join(root, "wiki", "companies", "AMD.json")
    assert os.path.isfile(path)


def test_record_company_quarter_upserts_without_duplicating(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.record_company_quarter(root, "AMD", _sample_quarter(revenue=7440.0))
    kb.record_company_quarter(root, "AMD", _sample_quarter(revenue=7500.0))  # same period_end

    history = kb.load_company_history(root, "AMD")
    assert len(history) == 1  # never two rows for one period_end
    assert history[0]["metrics"]["revenue"] == 7500.0


def test_load_company_history_oldest_first_and_windowed(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    for period_end in ("2026-03-31", "2025-09-30", "2026-06-30", "2025-12-31"):
        kb.record_company_quarter(root, "AMD", _sample_quarter(period_end=period_end))

    all_rows = kb.load_company_history(root, "AMD", quarters=10000)
    assert [r["period_end"] for r in all_rows] == [
        "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]

    windowed = kb.load_company_history(root, "AMD", quarters=2)
    assert [r["period_end"] for r in windowed] == ["2026-03-31", "2026-06-30"]


def test_load_company_history_unknown_ticker_returns_empty(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.record_company_quarter(root, "AMD", _sample_quarter())
    assert kb.load_company_history(root, "MSFT") == []


def test_load_company_history_malformed_json_degrades_to_empty(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    path = os.path.join(root, "wiki", "companies", "AMD.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("not valid json")

    assert kb.load_company_history(root, "AMD") == []


def test_record_company_quarter_never_raises_on_garbage(tmp_path):
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.record_company_quarter(root, "AMD", "not a dict")
    kb.record_company_quarter(root, "AMD", None)
    kb.record_company_quarter(root, "", {"period_end": "2026-06-30"})
    kb.record_company_quarter(root, "AMD", {"no_period_end": True})
    # no exception raised; whatever state resulted is still readable
    assert isinstance(kb.load_company_history(root, "AMD"), list)


def test_record_company_quarter_writes_entity_frontmatter_cross_link(tmp_path):
    """The entity page gains exactly one frontmatter key, `panel:
    companies/<TICKER>.json`, as the cross-link (spec §6.1/§7) -- when a page
    matching the ticker-derived slug already exists. The cross-link write is
    gated on existence (see test_record_company_quarter_skips_link_when_no_matching_entity_page),
    so this test seeds `amd.md` first rather than relying on the ticker's
    lowercased form accidentally coinciding with a create."""
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.apply_deltas(root, {"entities_upsert": [{"slug": "amd", "type": "company"}]})

    kb.record_company_quarter(root, "AMD", _sample_quarter())

    entity_path = os.path.join(root, "wiki", "entities", "amd.md")
    assert os.path.isfile(entity_path)
    with open(entity_path, encoding="utf-8") as fh:
        text = fh.read()
    assert "panel: companies/AMD.json" in text


def test_record_company_quarter_preserves_existing_entity_body(tmp_path):
    """Writing the cross-link must not disturb an existing entity page's body
    or other frontmatter (structured data must never touch the markdown the
    _ENTITY_MARKERS parser and apply_deltas own)."""
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.apply_deltas(root, {"entities_upsert": [
        {"slug": "amd", "type": "company", "summary": "Chipmaker.",
         "facts": ["Q2 2026 revenue beat."]}]})

    kb.record_company_quarter(root, "AMD", _sample_quarter())

    entity_path = os.path.join(root, "wiki", "entities", "amd.md")
    with open(entity_path, encoding="utf-8") as fh:
        text = fh.read()
    assert "Chipmaker." in text
    assert "Q2 2026 revenue beat." in text
    assert "panel: companies/AMD.json" in text


def test_record_company_quarter_skips_link_when_no_matching_entity_page(tmp_path):
    """The real production scenario: entity slugs are company names, not
    tickers, so `advanced-micro-devices.md` exists but `amd.md` never did.
    The ticker-derived slug guess misses, and the cross-link write must be
    skipped entirely rather than fabricating an orphan `amd.md` stub beside
    the genuine dossier -- the exact defect flagged in review. The sidecar
    (the load-bearing artifact) must still be written correctly regardless."""
    root = str(tmp_path)
    kb.ensure_kb(root)
    kb.apply_deltas(root, {"entities_upsert": [
        {"slug": "advanced-micro-devices", "type": "company",
         "summary": "Chipmaker."}]})

    kb.record_company_quarter(root, "AMD", _sample_quarter())

    amd_stub = os.path.join(root, "wiki", "entities", "amd.md")
    assert not os.path.isfile(amd_stub), "no orphan entity page must be created"

    real_dossier = os.path.join(root, "wiki", "entities", "advanced-micro-devices.md")
    with open(real_dossier, encoding="utf-8") as fh:
        text = fh.read()
    assert "panel:" not in text, "the real dossier must not be touched either"

    history = kb.load_company_history(root, "AMD")
    assert len(history) == 1
    assert history[0]["period_end"] == "2026-06-30"
