"""Unit tests for Group B (scripts/enrich.py). No network.

The public_get transport is stubbed explicitly with a fake response provider,
so `enrich_news` never touches the wire. Pure helpers (`_extract_text`,
`_select_candidates`) are tested directly.
"""

import os
import sys

import pytest

# Make scripts/ importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from worldintel.core import enrich  # noqa: E402


@pytest.fixture
def stub_public_get(monkeypatch):
    """Install a fake at the public acquisition transport boundary."""
    def install(session):
        def get(url, *, timeout, user_agent):
            return session.get(url, timeout=timeout, headers={"User-Agent": user_agent})
        monkeypatch.setattr(enrich, "public_get", get)
        return session
    return install


# --------------------------------------------------------------------------- #
# Fake session: records calls and returns a canned response (or raises).
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, text="", ctype="text/html", status_ok=True):
        self.text = text
        self.headers = {"Content-Type": ctype}
        self._ok = status_ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("HTTP 500")


class _FakeSession:
    def __init__(self, resp=None, raises=None):
        self.resp = resp
        self.raises = raises
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if self.raises is not None:
            raise self.raises
        return self.resp


# --------------------------------------------------------------------------- #
# _extract_text
# --------------------------------------------------------------------------- #
def test_extract_text_drops_scripts_and_collapses():
    html = """
    <html><head>
      <style>.x { color: red; }</style>
      <script>var secret = 'DO_NOT_LEAK';</script>
    </head><body>
      <nav>Home About Contact</nav>
      <p>First   paragraph   of   the story.</p>
      <p>Second paragraph here.</p>
    </body></html>
    """
    text = enrich._extract_text(html)
    assert "First paragraph of the story." in text
    assert "Second paragraph here." in text
    # script/style content and nav must be gone.
    assert "DO_NOT_LEAK" not in text
    assert "color: red" not in text
    assert "Home About Contact" not in text
    # whitespace collapsed (no runs of spaces).
    assert "  " not in text


def test_extract_text_empty_on_junk():
    assert enrich._extract_text("") == ""
    assert enrich._extract_text("<html><body></body></html>") == ""


# --------------------------------------------------------------------------- #
# _select_candidates
# --------------------------------------------------------------------------- #
def _item(title, region="world", category="general", tier=1, summary="s",
          url=None, raw_body="", source="src"):
    return {
        "title": title,
        "source": source,
        "url": url if url is not None else f"http://x/{title}",
        "published": "",
        "summary": summary,
        "region": region,
        "desk": region.lower() if region.lower() in ("japan", "canada") else "world",
        "category": category,
        "tier": tier,
        "raw_body": raw_body,
    }


def test_select_excludes_tier3_and_sports():
    news = [
        _item("us-a", region="world", tier=1),
        _item("tier3", region="world", tier=3),
        _item("sports", region="world", category="Sports", tier=1),
    ]
    picked = enrich._select_candidates(news, max_items=8)
    titles = {i["title"] for i in picked}
    assert "us-a" in titles
    assert "tier3" not in titles
    assert "sports" not in titles


def test_select_reserves_japan_and_canada():
    # Pool full of higher-ranked US/world items plus one Japan and one Canada
    # item that would otherwise be crowded out at a small max_items.
    news = [_item(f"us-{n}", region="world", tier=1) for n in range(6)]
    news.append(_item("jp-story", region="Japan", tier=2))
    news.append(_item("ca-story", region="Canada", tier=2))
    picked = enrich._select_candidates(news, max_items=4)
    titles = {i["title"] for i in picked}
    assert "jp-story" in titles
    assert "ca-story" in titles
    assert len(picked) == 4


def test_select_respects_max_items_and_dedupes():
    news = [_item(f"a-{n}", tier=1) for n in range(10)]
    # Duplicate url of a-0 -> should be deduped.
    dup = _item("dup", tier=1, url="http://x/a-0")
    news.append(dup)
    picked = enrich._select_candidates(news, max_items=5)
    assert len(picked) == 5
    urls = [i["url"] for i in picked]
    assert len(urls) == len(set(urls))


def test_hard_news_ranking_beats_local_crime_sports_and_pr():
    news = [
        _item("Local police arrest man after neighborhood fight",
              category="crime", tier=1,
              summary="A city crime blotter item with no broader consequence."),
        _item("Club wins baseball final", category="Sports", tier=1),
        _item("Company announces product award", category="press release", tier=1),
        _item("Japan earthquake forces evacuation and tsunami warnings",
              region="Japan", category="disaster", tier=1,
              summary="A major quake disrupts transport and energy infrastructure."),
        _item("Ukraine war talks collapse after new missile strike",
              region="Europe", category="world", tier=1,
              summary="Security officials warned of escalation."),
    ]

    picked = enrich._select_candidates(news, max_items=3)
    titles = [i["title"] for i in picked]

    assert titles[:2] == [
        "Japan earthquake forces evacuation and tsunami warnings",
        "Ukraine war talks collapse after new missile strike",
    ]
    assert "Club wins baseball final" not in titles
    assert "Company announces product award" not in titles


def test_recall_backfills_non_guardian_global_hard_news_beyond_normal_window(stub_public_get):
    guardian_items = [
        _item(f"Guardian politics analysis {n}", source="guardian",
              url=f"https://theguardian.com/{n}", tier=1,
              raw_body=f"Guardian body {n}")
        for n in range(8)
    ]
    non_guardian = [
        _item("BBC Japan quake displaces thousands", source="bbc",
              region="Japan", category="disaster", tier=1,
              url="https://bbc.com/quake",
              summary="A major earthquake damaged ports and power plants.",
              raw_body="BBC quake full text. " * 50),
        _item("New York Times Peru election result challenged", source="nytimes",
              region="Peru", category="politics", tier=1,
              url="https://nytimes.com/peru",
              summary="The election dispute raised transition risk.",
              raw_body="NYT election full text. " * 50),
        _item("Al Jazeera Gaza ceasefire talks break down", source="aljazeera",
              region="Gaza", category="world", tier=1,
              url="https://aljazeera.com/gaza",
              summary="War and diplomacy story with regional stakes.",
              raw_body="Al Jazeera Gaza full text. " * 50),
        _item("Reuters Red Sea shipping attack lifts oil", source="reuters",
              region="Middle East", category="markets", tier=1,
              url="https://reuters.com/red-sea",
              summary="A market-moving shipping disruption hit oil flows.",
              raw_body="Reuters shipping full text. " * 50),
    ]
    bundle = {"news": guardian_items + non_guardian, "source_health": {}}

    enrich.enrich_news(bundle, session=stub_public_get(_FakeSession()), max_items=2,
                       recall_non_guardian=4)

    enriched_non_guardian = [
        i for i in bundle["news"]
        if i.get("source") != "guardian" and i.get("content")
    ]
    assert len(enriched_non_guardian) == 4
    assert {i["source"] for i in enriched_non_guardian} == {
        "bbc", "nytimes", "aljazeera", "reuters",
    }
    assert bundle["source_health"]["enrich"]["note"] == "4/4 enriched"


# --------------------------------------------------------------------------- #
# enrich_news
# --------------------------------------------------------------------------- #
def test_raw_body_used_without_http_call(stub_public_get):
    body = "Full article text. " * 50  # ~1000 chars
    news = [_item("guardian", tier=1, raw_body=body)]
    bundle = {"news": news, "source_health": {}}
    session = stub_public_get(_FakeSession(raises=AssertionError("must not fetch")))
    enrich.enrich_news(bundle, session=session, max_items=8, per_item_chars=1000)
    assert session.calls == []  # no HTTP for a raw_body item
    assert news[0]["content"]
    assert 800 <= len(news[0]["content"]) <= 1000


def test_truncation_respects_per_item_chars(stub_public_get):
    body = "word " * 500  # 2500 chars
    news = [_item("g", tier=1, raw_body=body)]
    bundle = {"news": news, "source_health": {}}
    enrich.enrich_news(bundle, session=stub_public_get(_FakeSession()), per_item_chars=1200)
    assert 800 <= len(news[0]["content"]) <= 1200


def test_fetch_failure_leaves_content_empty_and_no_raise(stub_public_get):
    news = [
        _item("fetch-me", tier=1, raw_body="", url="http://x/fetch-me"),
        _item("also", tier=1, raw_body="already have body text here. " * 30),
    ]
    bundle = {"news": news, "source_health": {}}
    session = stub_public_get(_FakeSession(raises=ConnectionError("boom")))
    # Must not raise even though the fetch blows up.
    enrich.enrich_news(bundle, session=session)
    assert news[0].get("content", "") == ""      # fetch failed -> empty
    assert news[1]["content"]                     # raw_body item still processed


def test_successful_fetch_sets_content(stub_public_get):
    filler = "More article body text. " * 40  # pad past MIN_CONTENT_CHARS
    html = f"<html><body><p>Fetched paragraph one.</p><p>{filler}</p></body></html>"
    news = [_item("f", tier=1, raw_body="", url="http://x/f")]
    bundle = {"news": news, "source_health": {}}
    session = stub_public_get(_FakeSession(resp=_Resp(text=html)))
    enrich.enrich_news(bundle, session=session)
    assert session.calls == ["http://x/f"]
    assert "Fetched paragraph one." in news[0]["content"]


def test_raw_body_removed_and_health_recorded(stub_public_get):
    news = [
        _item("a", tier=1, raw_body="body a text. " * 65),
        _item("b", tier=1, raw_body="body b text. " * 65),
        _item("noise", tier=3, raw_body="junk"),
    ]
    bundle = {"news": news, "source_health": {}}
    enrich.enrich_news(bundle, session=stub_public_get(_FakeSession()), max_items=8)
    # raw_body gone from EVERY item, including the excluded tier-3 one.
    assert all("raw_body" not in i for i in news)
    health = bundle["source_health"]["enrich"]
    assert health["ok"] is True
    assert health["note"] == "2/2 enriched"


def test_non_html_response_skipped(stub_public_get):
    news = [_item("pdf", tier=1, raw_body="", url="http://x/pdf")]
    bundle = {"news": news, "source_health": {}}
    session = stub_public_get(_FakeSession(resp=_Resp(text="%PDF-1.4", ctype="application/pdf")))
    enrich.enrich_news(bundle, session=session)
    assert news[0].get("content", "") == ""
    assert bundle["source_health"]["enrich"]["ok"] is False


def test_enrich_news_never_raises_on_bad_bundle(stub_public_get):
    # No 'news' key, weird shapes -> must not raise.
    enrich.enrich_news({}, session=stub_public_get(_FakeSession()))
    enrich.enrich_news({"news": None}, session=stub_public_get(_FakeSession()))


# --------------------------------------------------------------------------- #
# candidates= (Phase 2 desk-map path)
# --------------------------------------------------------------------------- #
def test_candidates_argument_bypasses_internal_selection(monkeypatch):
    picked = [{"title": "A", "url": "https://e.com/a",
               "raw_body": "<p>Body A. " + ("More text. " * 80) + "</p>"},
              {"title": "B", "url": "https://e.com/b",
               "raw_body": "<p>Body B. " + ("More text. " * 80) + "</p>"}]
    ignored = {"title": "Z", "url": "https://e.com/z", "raw_body": "<p>Zed</p>"}
    bundle = {"news": picked + [ignored]}

    def _boom(*a, **k):
        raise AssertionError("_select_candidates must not run")

    monkeypatch.setattr(enrich, "_select_candidates", _boom)
    enrich.enrich_news(bundle, candidates={"world": [picked[0]],
                                           "us": [picked[1]]})
    assert picked[0]["content"].startswith("Body A")
    assert picked[1]["content"].startswith("Body B")
    assert "content" not in ignored


class _NoNetSession:
    """Stands in for requests.Session so no unit test can touch the network.
    `_fetch_html` swallows the error and the item ends up without content."""

    def get(self, *a, **k):
        raise RuntimeError("network disabled in unit tests")


def test_health_note_carries_per_desk_breakdown(stub_public_get):
    a = {"title": "A", "url": "https://e.com/a",
         "raw_body": "<p>Body A. " + ("More text. " * 80) + "</p>"}
    b = {"title": "B", "url": "https://e.com/b", "raw_body": ""}
    bundle = {"news": [a, b]}
    # Must pass a session: item b has no raw_body, so enrich_news would
    # otherwise open a real requests.Session and hit the network.
    enrich.enrich_news(bundle, candidates={"world": [a], "japan": [b]},
                       session=stub_public_get(_NoNetSession()))
    note = bundle["source_health"]["enrich"]["note"]
    assert "world 1/1" in note
    assert "japan 0/1" in note


def test_parallel_matches_sequential_result():
    def _mk():
        return [{"title": f"T{n}", "url": f"https://e.com/{n}",
                 "raw_body": f"<p>Body {n}. " + ("More text. " * 80) + "</p>"}
                for n in range(6)]

    seq_items, par_items = _mk(), _mk()
    enrich.enrich_news({"news": seq_items},
                       candidates={"world": seq_items}, max_workers=1)
    enrich.enrich_news({"news": par_items},
                       candidates={"world": par_items}, max_workers=6)
    assert [i["content"] for i in seq_items] == [i["content"] for i in par_items]


def test_candidates_with_no_items_is_safe():
    bundle = {"news": []}
    enrich.enrich_news(bundle, candidates={"world": [], "us": []})
    assert bundle["source_health"]["enrich"]["ok"] is False


# --------------------------------------------------------------------------- #
# MIN_CONTENT_CHARS floor: a teaser must not become Brief-eligible padding.
# --------------------------------------------------------------------------- #
def test_teaser_body_below_floor_gets_no_content():
    short_body = "x" * 200
    long_body = "reported fact " * 150
    short_item = {"title": "short", "url": "https://e.com/short",
                  "raw_body": short_body}
    long_item = {"title": "long", "url": "https://e.com/long",
                 "raw_body": long_body}
    bundle = {"news": [short_item, long_item]}
    enrich.enrich_news(bundle, candidates={"world": [short_item, long_item]})
    assert "content" not in short_item
    assert long_item["content"]


def test_health_note_counts_only_items_above_the_floor():
    short_item = {"title": "short", "url": "https://e.com/short",
                  "raw_body": "x" * 200}
    long_item = {"title": "long", "url": "https://e.com/long",
                 "raw_body": "reported fact " * 150}
    bundle = {"news": [short_item, long_item]}
    enrich.enrich_news(bundle, candidates={"world": [short_item, long_item]})
    health = bundle["source_health"]["enrich"]
    assert health["note"] == "1/2 enriched (world 1/2)"


# --------------------------------------------------------------------------- #
# User-Agent. `feeds.py` already documents this exact failure and works around
# it in `_fetch_one`; the workaround was never copied here, so an
# ALREADY-REGISTERED feed has been enriching at 0 chars in production.
# --------------------------------------------------------------------------- #
class _HeaderRecordingSession:
    def __init__(self, resp):
        self.resp = resp
        self.headers_seen = []

    def get(self, url, **kwargs):
        self.headers_seen.append((kwargs.get("headers") or {}))
        return self.resp


def test_fetch_html_sends_a_browser_user_agent(stub_public_get):
    """Measured live on 2026-08-15, same article URL under each UA:

        japantoday         "hermes-world-intel/1.0 (+enrich)" -> HTTP 403, 383 bytes
                           browser UA                         -> HTTP 200, 3,473 chars
        japannews-yomiuri  "hermes-world-intel/1.0 (+enrich)" -> HTTP 202, ZERO bytes
                           browser UA                         -> HTTP 200, 2,187 chars

    `japannews-yomiuri` is in FEED_REGISTRY today, so every Yomiuri item was
    reaching Codex with no `content` — and the Brief's eligibility rule requires
    `content`, which made a registered tier-1 Japan source permanently unable to
    lead a story. `feeds._fetch_one` carries a comment describing precisely this
    zero-byte-202 behaviour; only enrich was left behind.
    """
    session = stub_public_get(_HeaderRecordingSession(_Resp("<html><body>ok</body></html>")))
    enrich._fetch_html(session, "https://japantoday.com/x", 5)
    ua = session.headers_seen[0].get("User-Agent", "")
    assert ua.startswith("Mozilla/"), (
        f"enrich must send a browser User-Agent; got {ua!r}")
    # Still identifies us and stays contactable — politeness, not disguise.
    assert enrich.wi_config.get().user_agent_product in ua

@pytest.mark.parametrize('response, error, reason', [
    (_Resp(status_ok=False), None, 'http_error'),
    (_Resp(text='<html></html>'), None, 'extraction_empty'),
    (_Resp(text='<p>short report</p>'), None, 'below_content_threshold'),
    (None, RuntimeError('secret URL ?token=private'), 'request_error'),
])
def test_per_item_failure_diagnostics_are_safe(stub_public_get, response, error, reason):
    item = _item('airport attack', url='https://private.example/report?token=secret')
    bundle = {'news': [item]}
    enrich.enrich_news(bundle, session=stub_public_get(_FakeSession(response, error)), candidates={'world': [item]})
    outcome = item['enrichment']
    assert outcome['reason'] == reason
    assert outcome['status'] == 'failed'
    assert 'secret' not in str(outcome) and 'private' not in str(outcome)
    assert not item.get('content')

def test_protected_alternate_replaces_only_failed_same_event_slot(stub_public_get):
    failed = _item('airport attack', url='https://a/report')
    failed['enrichment'] = {'status': 'failed', 'reason': 'http_error', 'http_status': 403}
    alternate = _item('airport attack followup', url='https://b/report', raw_body='Authorities reported an attack on the airport. ' * 30)
    unrelated = _item('unrelated', raw_body='Unrelated article words. ' * 100)
    selected = {'world': [failed, unrelated]}
    event = {'id': 'one', 'urls': [failed['url'], alternate['url']], 'selected_urls': [failed['url']]}
    enrich.enrich_protected_alternates([failed, alternate, unrelated], selected, [event], session=stub_public_get(_FakeSession()))
    assert selected['world'] == [alternate, unrelated]
    assert event['selected_urls'] == [alternate['url']]
    assert event['enrichment_attempts'][0]['reason'] == 'http_error'
    assert event['enrichment_attempts'][1]['status'] == 'enriched'


def test_protected_alternate_does_not_use_unrelated_or_short_sources(stub_public_get):
    failed = _item('airport attack', url='https://a/report')
    short = _item('airport attack followup', url='https://b/report', raw_body='longword' * 150)
    unrelated = _item('unrelated', raw_body='Article words available. ' * 100)
    selected = {'world': [failed]}
    event = {'urls': [failed['url'], short['url']], 'selected_urls': [failed['url']]}
    enrich.enrich_protected_alternates([failed, short, unrelated], selected, [event], session=stub_public_get(_FakeSession()))
    assert selected['world'] == [failed]
    assert event['selected_urls'] == [failed['url']]

def test_protected_alternate_attempts_are_bounded_and_keep_other_event_slots(stub_public_get):
    failed = [_item('airport attack', url=f'https://failed/{n}') for n in range(2)]
    pool = list(failed)
    events = []
    for n, item in enumerate(failed):
        alternatives = [_item('airport attack followup', url=f'https://alternate/{n}/{i}') for i in range(4)]
        pool.extend(alternatives)
        events.append({'urls': [item['url']] + [a['url'] for a in alternatives], 'selected_urls': [item['url']]})
    selected = {'world': list(failed)}
    session = stub_public_get(_FakeSession(_Resp(text='<p>short report</p>')))
    enrich.enrich_protected_alternates(pool, selected, events, session=session)
    assert len(session.calls) == 4
    assert all(len(event['enrichment_attempts']) == 3 for event in events)
    assert selected['world'] == failed


def test_protected_alternate_cannot_replace_another_event_or_desk(stub_public_get):
    failed = _item('airport attack', url='https://a/report')
    protected = _item('airport attack followup', url='https://b/report', raw_body='Real article words available. ' * 100)
    wrong_desk = _item('airport attack followup', url='https://c/report', raw_body='Real article words available. ' * 100)
    wrong_desk['desk'] = 'japan'
    selected = {'world': [failed, protected]}
    events = [{'urls': [failed['url'], protected['url'], wrong_desk['url']], 'selected_urls': [failed['url']]},
              {'urls': [protected['url']], 'selected_urls': [protected['url']]}]
    enrich.enrich_protected_alternates([failed, protected, wrong_desk], selected, events, session=stub_public_get(_FakeSession()))
    assert selected['world'] == [failed, protected]
    assert events[0]['selected_urls'] == [failed['url']]


def test_third_protected_event_enrichment_honors_selection_limit(stub_public_get):
    first = _item('airport incident', url='https://a/first')
    second = _item('airport incident', url='https://b/second')
    third = _item('airport incident', url='https://c/third')
    alternate = _item('airport incident', url='https://d/alternate', raw_body='Authorities reported facts about this incident. ' * 40)
    first['content'] = second['content'] = 'Substantive incident reporting facts. ' * 60
    events = [{'urls': [x['url']], 'selected_urls': [x['url']], 'max_events': 3} for x in [first,second,third]]
    events[-1]['urls'].append(alternate['url'])
    selected = {'world': [first,second,third]}
    enrich.enrich_protected_alternates([first,second,third,alternate], selected, events, session=stub_public_get(_FakeSession()))
    assert selected['world'][-1] is alternate
    assert events[-1]['selected_urls'] == [alternate['url']]


@pytest.mark.parametrize("limit", [40, 800])
def test_retained_content_must_meet_both_floors(synthetic_config, limit):
    item = {"raw_body": ("Authorities confirmed nationwide school closures following extensive violence and serious injuries. " * 20),
            "url": "https://wire.example/report", "tier": 1, "desk": "world"}
    enrich.enrich_news({"news": [item]}, candidates={"world": [item]}, per_item_chars=limit,
                       recall_non_guardian=0, max_workers=1)
    assert "content" not in item
    assert item["enrichment"]["status"] == "failed"
    assert item["enrichment"]["reason"] == "retained_content_below_threshold"
