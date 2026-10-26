import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

from worldintel.core import feeds  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

ITEM_KEYS = {"title", "source", "url", "published", "summary",
             "region", "category", "tier", "raw_body", "desk"}


def _fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as fh:
        return fh.read()


def test_registry_shape():
    assert len(feeds.feed_registry()) >= 30
    desks = {f["desk"] for f in feeds.feed_registry()}
    assert desks == {"world", "us", "japan", "canada", "finance"}
    for f in feeds.feed_registry():
        assert f["url"].startswith("http")
        assert f["tier"] in (1, 2, 3)
        assert f["name"]


def test_parse_rss20_full_field_contract():
    feed = {"name": "bbc-world", "url": "x", "desk": "world", "tier": 1}
    items = feeds._parse_feed(_fixture("rss_bbc.xml"), feed)
    assert len(items) == 2
    first = items[0]
    assert set(first) == ITEM_KEYS
    assert first["title"] == "Synthetic energy policy update"
    assert first["url"] == "https://example.com/rss/world-1"
    assert first["summary"].startswith("Synthetic summary describes")
    assert first["published"] == "2026-08-02T06:30:00+00:00"
    assert first["category"] == "World"
    assert first["desk"] == "world"
    assert first["tier"] == 1
    assert first["source"] == "bbc-world"
    assert first["raw_body"] == ""


def test_parse_rdf():
    feed = {"name": "dw-world", "url": "x", "desk": "world", "tier": 1}
    items = feeds._parse_feed(_fixture("rss_dw.rdf"), feed)
    assert len(items) == 1
    assert items[0]["title"] == "Synthetic budget amendment update"
    assert items[0]["url"] == "https://example.com/rdf/story-1"
    assert items[0]["published"].startswith("2026-08-02T04:15")


def test_parse_atom_link_href_and_summary():
    feed = {"name": "boc", "url": "x", "desk": "finance", "tier": 1}
    items = feeds._parse_feed(_fixture("rss_atom.xml"), feed)
    assert len(items) == 1
    assert items[0]["url"] == "https://example.com/atom/story-1"
    assert items[0]["summary"].startswith("Synthetic bank example leaves")
    assert items[0]["desk"] == "finance"


def test_parse_garbage_returns_empty_not_raise():
    feed = {"name": "x", "url": "x", "desk": "world", "tier": 2}
    assert feeds._parse_feed(b"not xml at all", feed) == []
    assert feeds._parse_feed(b"", feed) == []


def test_parse_item_without_title_or_link_is_dropped():
    xml = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><description>no title</description></item>
      <item><title>Has title</title><link>https://e.com/a</link></item>
    </channel></rss>"""
    feed = {"name": "x", "url": "x", "desk": "us", "tier": 2}
    items = feeds._parse_feed(xml, feed)
    assert len(items) == 1
    assert items[0]["title"] == "Has title"


def test_parse_malformed_date_returns_empty_published():
    xml = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item>
        <title>Story with malformed date</title>
        <link>https://e.com/story-1</link>
        <description>Summary text.</description>
        <pubDate>not a date</pubDate>
      </item>
      <item>
        <title>Story with empty date</title>
        <link>https://e.com/story-2</link>
        <description>Another summary.</description>
        <pubDate>   </pubDate>
      </item>
    </channel></rss>"""
    feed = {"name": "test-feed", "url": "x", "desk": "world", "tier": 2}
    items = feeds._parse_feed(xml, feed)
    assert len(items) == 2
    assert items[0]["title"] == "Story with malformed date"
    assert items[0]["published"] == ""
    assert items[0]["url"] == "https://e.com/story-1"
    assert items[1]["title"] == "Story with empty date"
    assert items[1]["published"] == ""
    assert items[1]["url"] == "https://e.com/story-2"


class _FakeResp:
    def __init__(self, content=b"", status=200, url=""):
        self.content = content
        self.status_code = status
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """Maps url-substring -> _FakeResp (or an Exception instance to raise)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        for frag, resp in self.routes.items():
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _FakeResp(b"", 404)


class _HeaderRecordingSession:
    """Records the `headers` kwarg of every request it is asked to make."""

    def __init__(self, content=b""):
        self.content = content
        self.headers_seen = []

    def get(self, url, **kwargs):
        self.headers_seen.append(kwargs.get("headers"))
        return _FakeResp(self.content, 200, url=url)


def test_fetch_one_sends_the_crawler_user_agent_on_the_request():
    """The UA must ride on the request, not just on a session feeds.py owns.

    fetch_sources.fetch_all supplies its own session, so a session-level default
    never reaches the cron — which is how japannews.yomiuri.co.jp ended up
    serving a zero-byte HTTP 202 in production.
    """
    session = _HeaderRecordingSession(_fixture("rss_bbc.xml"))
    feed = {"name": "bbc-world", "desk": "world", "tier": 1,
            "url": "https://ok.test/rss"}
    items = feeds._fetch_one(session, feed, {})

    assert len(items) == 2
    assert session.headers_seen == [{"User-Agent": feeds._browser_ua()}]


def test_fetch_feeds_merges_and_records_health():
    registry = [
        {"name": "ok-feed", "desk": "world", "tier": 1, "url": "https://ok.test/rss"},
        {"name": "dead-feed", "desk": "us", "tier": 2, "url": "https://dead.test/rss"},
    ]
    session = _FakeSession({
        "ok.test": _FakeResp(_fixture("rss_bbc.xml")),
        "dead.test": RuntimeError("boom"),
    })
    health = {}
    items = feeds.fetch_feeds({}, "", health, session=session, registry=registry)

    assert len(items) == 2
    assert all(i["desk"] == "world" for i in items)
    assert health["feed:ok-feed"]["ok"] is True
    assert health["feed:dead-feed"]["ok"] is False
    assert "boom" in health["feed:dead-feed"]["note"]


def test_fetch_feeds_http_error_is_not_fatal():
    registry = [{"name": "f404", "desk": "world", "tier": 2,
                 "url": "https://x.test/rss"}]
    session = _FakeSession({"x.test": _FakeResp(b"", 404)})
    health = {}
    assert feeds.fetch_feeds({}, "", health, session=session, registry=registry) == []
    assert health["feed:f404"]["ok"] is False


def test_gnews_items_are_flagged_without_a_resolution_request():
    """fetch_feeds must NOT try to resolve news.google.com URLs.

    Google serves an HTTP 200 JS-redirect page rather than a 302, so resolution
    measured 0/100 while costing up to 10s per item, serially, inside one pool
    worker on an unattended cron. The item is flagged instead.
    """
    registry = [{"name": "reuters-gnews", "desk": "world", "tier": 1,
                 "url": "https://news.google.com/rss/search?q=x"}]
    gnews_xml = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Wire story</title>
      <link>https://news.google.com/rss/articles/ABC123</link>
      <pubDate>Sat, 02 Aug 2026 06:00:00 GMT</pubDate></item>
    </channel></rss>"""
    session = _FakeSession({
        "news.google.com/rss/search": _FakeResp(gnews_xml),
        "news.google.com/rss/articles": _FakeResp(
            b"", 200, url="https://www.reuters.com/world/real-story"),
    })
    health = {}
    items = feeds.fetch_feeds({}, "", health, session=session, registry=registry)
    assert items[0]["url"] == "https://news.google.com/rss/articles/ABC123"
    assert items[0]["gnews_unresolved"] is True
    # The feed URL only — no per-article resolution request was made.
    assert session.calls == ["https://news.google.com/rss/search?q=x"]


def test_gnews_resolution_failure_keeps_url_and_flags_it():
    registry = [{"name": "reuters-gnews", "desk": "world", "tier": 1,
                 "url": "https://news.google.com/rss/search?q=x"}]
    gnews_xml = b"""<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>Wire story</title>
      <link>https://news.google.com/rss/articles/ABC123</link></item>
    </channel></rss>"""
    session = _FakeSession({
        "news.google.com/rss/search": _FakeResp(gnews_xml),
        "news.google.com/rss/articles": RuntimeError("timeout"),
    })
    health = {}
    items = feeds.fetch_feeds({}, "", health, session=session, registry=registry)
    assert items[0]["url"] == "https://news.google.com/rss/articles/ABC123"
    assert items[0]["gnews_unresolved"] is True


def test_resolve_gnews_returns_publisher_url():
    """_resolve_gnews is no longer called by _fetch_one but stays correct and
    available for if Google restores real redirects."""
    session = _FakeSession({"news.google.com/rss/articles": _FakeResp(
        b"", 200, url="https://www.reuters.com/world/real-story")})
    resolved = feeds._resolve_gnews(
        session, "https://news.google.com/rss/articles/ABC123")
    assert resolved == "https://www.reuters.com/world/real-story"


def test_resolve_gnews_returns_empty_when_it_never_leaves_google():
    """Today's actual behaviour: HTTP 200 with a JS redirect, final URL still
    on news.google.com — which is why the call was removed from _fetch_one."""
    session = _FakeSession({"news.google.com/rss/articles": _FakeResp(
        b"", 200, url="https://news.google.com/rss/articles/ABC123")})
    assert feeds._resolve_gnews(
        session, "https://news.google.com/rss/articles/ABC123") == ""


def test_resolve_gnews_never_raises():
    session = _FakeSession({"news.google.com": RuntimeError("timeout")})
    assert feeds._resolve_gnews(
        session, "https://news.google.com/rss/articles/ABC123") == ""
    assert feeds._resolve_gnews(session, "https://www.reuters.com/x") == ""
    assert feeds._resolve_gnews(None, "https://news.google.com/x") == ""


def test_check_feeds_reports_per_feed_status():
    registry = [{"name": "ok-feed", "desk": "world", "tier": 1,
                 "url": "https://ok.test/rss"}]
    session = _FakeSession({"ok.test": _FakeResp(_fixture("rss_bbc.xml"))})
    rows = feeds.check_feeds(session=session, registry=registry)
    assert rows == [{"feed": "ok-feed", "desk": "world",
                     "status": "OK", "count": 2}]


# --------------------------------------------------------------------------- #
# Supply. Every URL below was probed live on 2026-08-15: HTTP 200, parsed by
# _parse_feed, items inside the 36h staleness window, and — the gate that
# actually matters — an article body clearing enrich.MIN_CONTENT_CHARS, since
# the Brief refuses any story without `content`.
# --------------------------------------------------------------------------- #
def test_registry_names_and_urls_are_unique():
    """A duplicate name silently overwrites the other's health entry; a
    duplicate URL doubles an outlet's weight against the per-feed cap."""
    names = [f["name"] for f in feeds.feed_registry()]
    urls = [f["url"] for f in feeds.feed_registry()]
    assert len(names) == len(set(names))
    assert len(urls) == len(set(urls))


def test_thin_desks_gained_enrichable_supply():
    """The finance desk ran on 64 live items/day from five outlets, and the
    japan desk on 113 of which only ~52% mentioned Japan. That thinness is how
    Nvidia's $500bn AI financing initiative and the Chiba floods (8 dead,
    record rainfall since 1966) both missed the brief in the same week.

    Feeds whose ARTICLES cannot be extracted were deliberately not added, however
    good their headlines: FT section feeds (652-char stubs), Bloomberg (403 to
    every UA), Reuters/Google-News proxies (`gnews_unresolved`), investing.com
    (no `<description>` at all). Such a feed still consumes one of a desk's ten
    slots while being permanently Brief-ineligible.
    """
    by_desk = {}
    for f in feeds.feed_registry():
        by_desk.setdefault(f["desk"], set()).add(f["name"])
    assert {"pymnts", "fortune", "techcrunch", "semafor", "coindesk",
            "cnbc-tech"} <= by_desk["finance"]
    assert {"kyodo-english", "japantoday", "yomiuri-society"} <= by_desk["japan"]


def test_wsj_world_is_not_the_frozen_dow_jones_archive():
    """`feeds.a.dj.com/rss/RSSWorldNews.xml` still answers HTTP 200, which is why
    it never showed up as a health failure — but its newest item is dated
    2025-01-27. Twenty items of 19-month-old news, none surviving the 36h gate:
    a second `economist-finance`, measured independently twice."""
    wsj = next(f for f in feeds.feed_registry() if f["name"] == "wsj-world")
    assert "feeds.a.dj.com" not in wsj["url"]
