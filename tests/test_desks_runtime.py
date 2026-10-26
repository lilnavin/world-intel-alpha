"""Configured national desks reach selection, enrichment, publication and HTML."""
import copy
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from worldintel import config
from worldintel.core import enrich
from worldintel.core import feeds
from worldintel.core import metrics
from worldintel.core import quality_gate
from worldintel.core import select_news
from worldintel.core import compose_html


def national(did="kenya", order=10):
    return {"id": did, "kind": "national", "label": did.title(),
            "target_stories": [1, 4], "signature_patterns": [did, "nairobi"],
            "prompt_section": f"Write briefing.{did} from fully grounded articles.",
            "tab": {"order": order, "colour": "#56ab78"}}


def install_desks(base, nationals):
    raw = copy.deepcopy(base.raw)
    raw["desks"] = [{"id": "world", "kind": "global", "label": "World News"},
                    {"id": "us", "kind": "global", "label": "US"},
                    *nationals,
                    {"id": "finance", "kind": "finance", "label": "Finance"}]
    raw["sources"]["feeds"] = []
    cfg = config.Config(raw, base.source_path)
    config.set_for_test(cfg)
    return cfg


def test_feed_parsed_kenya_item_gets_reserved_enrichment_slot(synthetic_config):
    install_desks(synthetic_config, [national()])
    xml = b'<rss><channel><item><title>Nairobi parliament approves bill</title><link>https://example.org/kenya</link></item></channel></rss>'
    item = feeds._parse_feed(xml, {"name": "kenya-news", "desk": "kenya", "tier": 2})[0]
    assert item["desk"] == "kenya"
    assert item["region"] == "world"  # unknown legacy region must not defeat reservation
    world = {"title": "War sanctions deal", "desk": "world", "tier": 1,
             "region": "world", "url": "https://example.org/world"}
    assert enrich._select_candidates([world, item], 1) == [item]


def test_kenya_quality_rewrite_grounding_and_metrics(synthetic_config):
    install_desks(synthetic_config, [national()])
    story = {"headline": "Nairobi passes bill", "body": "The bundle only gives a headline.",
             "sources": [{"url": "https://example.org/kenya", "outlet": "Kenya News"}]}
    result = {"briefing": {"kenya": [story]}}
    flagged = quality_gate.scan(result)
    assert flagged == [("kenya", 0, story["body"])]
    body = "Parliament approved the bill after debate. " * 20
    assert quality_gate.rewrite(result, flagged, lambda *a: {"bodies": [body]}, "unused") == 1
    bundle = {"news": [{"title": story["headline"], "url": "https://example.org/kenya",
                        "desk": "kenya", "tier": 2, "content": "x" * 900}]}
    assert quality_gate.enforce_country_grounding(result, bundle) == {"kenya_removed": 0}
    assert metrics.compute_pool(bundle, result)[0]["picked"] == "kenya"
    assert metrics.compute_metrics(bundle, result, "2026-10-01")["kenya_brief_count"] == 1


def test_zero_nationals_has_no_country_publication_or_reservation(synthetic_config):
    install_desks(synthetic_config, [])
    assert enrich.reserved_desk_ids() == ()
    result = {"briefing": {"japan": [{"body": "The bundle only gives a headline."}]}}
    assert quality_gate.scan(result) == []
    assert quality_gate.enforce_country_grounding(result, {}) == {}
    rec = metrics.compute_metrics({}, result, "2026-10-01")
    assert "japan_brief_count" not in rec
    assert "canada_brief_count" not in rec


def test_kenya_signature_routes_and_renders_without_country_code(synthetic_config):
    cfg = install_desks(synthetic_config, [national()])
    item = {"desk": "world", "title": "Nairobi parliament approves finance bill",
            "url": "https://example.org/kenya", "source": "kenya-news", "tier": 2}
    desks = select_news.select_candidates({"news": [item], "date": "2026-10-01"}, set())
    assert desks["kenya"] == [item]
    assert select_news._route_desk({"desk": "kenya", "title": "Generic wire copy"}) == "world"
    assert select_news._route_desk({"desk": "finance", "title": item["title"]}) == "finance"
    story = {"headline": item["title"], "body": "Reported facts."}
    html = compose_html.render_html({"briefing": {"kenya": [story]}}, {}, "2026-10-01",
                                   desks=cfg.desks, timezone_label="EAT")
    assert 'id="kenya"' in html and "data-region='KENYA'" in html
    assert "--rc-kenya:" in html and "&middot; EAT" in html
    assert "region" not in story


def test_world_only_profile_retains_financial_story(synthetic_config):
    raw = copy.deepcopy(synthetic_config.raw)
    raw["desks"] = [{"id": "world", "kind": "global", "label": "World News"}]
    raw["sources"]["feeds"] = []
    config.set_for_test(config.Config(raw, synthetic_config.source_path))
    item = {"desk": "world", "title": "Company announces share buyback and earnings",
            "url": "https://example.org/finance", "source": "news", "tier": 1}
    assert select_news._route_desk(item) == "world"
    selected = select_news.select_candidates({"news": [item], "date": "2026-10-01"}, set())
    assert selected == {"world": [item]}


def test_zero_national_renderer_has_no_legacy_country_rules(synthetic_config):
    cfg = install_desks(synthetic_config, [])
    assert select_news.local_desk_ids() == ()
    assert set(select_news.select_candidates({}, set())) == {"world", "us", "finance"}
    html = compose_html.render_html({}, {}, "2026-10-01", desks=cfg.desks)
    for marker in ('data-tab="japan"', 'data-tab="canada"', '--rc-japan:', '--rc-canada:',
                   '#japan .card', '#canada .card'):
        assert marker not in html
    assert html.count('class="panel"') == 2


def test_five_national_tabs_are_all_visible_and_follow_tab_order(synthetic_config):
    countries = [national(did, order) for did, order in
                 [("kenya", 5), ("uganda", 4), ("tanzania", 3), ("rwanda", 2), ("ghana", 1)]]
    cfg = install_desks(synthetic_config, countries)
    html = compose_html.render_html({}, {}, "2026-10-01", desks=cfg.desks)
    assert html.count('class="panel"') == 7
    assert '#tab-7:checked ~ #panel-7 { display: block; }' in html
    assert '#tab-7:checked ~ .tabbar .tablabel[for="tab-7"]' in html
    assert html.index('data-tab="ghana"') < html.index('data-tab="kenya"')
    assert html.count('--rc-us:') == 1


def test_configured_enrichment_limits_and_region_vocabulary(synthetic_config, monkeypatch):
    raw = copy.deepcopy(synthetic_config.raw)
    raw.setdefault("tuning", {})["enrichment"] = {"max_items": 1, "concurrency": 2, "min_chars": 800, "max_chars": 1200}
    raw["tuning"]["region_tokens"] = ["kenya"]
    cfg = config.Config(raw, synthetic_config.source_path)
    config.set_for_test(cfg)
    items = [{"title": f"Story {i}", "url": f"https://example.org/{i}", "tier": 1,
              "desk": "world", "raw_body": "reporting " * 200} for i in range(3)]
    bundle = {"news": items}
    enrich.enrich_news(bundle, recall_non_guardian=0)
    assert sum(bool(item.get("content")) for item in items) == 1
    assert 800 <= len(items[0]["content"]) <= 1200
    assert len(items[0]["content"].split()) >= 100
    assert metrics.region_tokens() == {"kenya"}
    workers = []
    class Pool:
        def __init__(self, max_workers): workers.append(max_workers)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def map(self, fn, values): return list(map(fn, values))
    monkeypatch.setattr(enrich.concurrent.futures, "ThreadPoolExecutor", Pool)
    items = [{"raw_body": "reporting " * 200} for _ in range(3)]
    enrich.enrich_news({"news": items}, candidates={"world": items})
    assert workers == [2]
