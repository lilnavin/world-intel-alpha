import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

from worldintel.core import fetch_sources  # noqa: E402
from worldintel.core import select_news as select  # noqa: E402


def _item(**kw):
    base = {"title": "t", "source": "s", "url": "https://e.com/a",
            "published": "2026-08-02T06:00:00+00:00", "summary": "",
            "region": "world", "category": "general", "tier": 1,
            "raw_body": "", "desk": "world"}
    base.update(kw)
    return base


def test_dedupe_exact_url():
    news = [_item(url="https://e.com/a"), _item(url="https://e.com/a"),
            _item(url="https://e.com/b")]
    assert len(select._dedupe(news)) == 2


def test_dedupe_near_identical_titles_across_outlets():
    # The real 2026-08-02 case: one recall story from three aggregators.
    t = ("Ontario shoppers warned about new food recalls: Cadbury Oreo bars, "
         "baked goods sold at Costco, cumin sold at Metro")
    news = [
        _item(title=t, url="https://bramptonguardian.com/1", source="bramptonguardian"),
        _item(title=t + " and Food Basics", url="https://cambridgetimes.ca/2",
              source="cambridgetimes"),
        _item(title="Unrelated story", url="https://e.com/z"),
    ]
    kept = select._dedupe(news)
    assert len(kept) == 2
    assert kept[0]["source"] == "bramptonguardian"  # first occurrence wins


def test_dedupe_keeps_short_distinct_titles():
    news = [_item(title="Fed holds rates", url="https://e.com/1"),
            _item(title="ECB holds rates", url="https://e.com/2")]
    assert len(select._dedupe(news)) == 2


def test_drop_stale_uses_36h_window():
    fresh = _item(published="2026-08-02T06:00:00+00:00", url="https://e.com/1")
    edge = _item(published="2026-07-31T23:00:00+00:00", url="https://e.com/2")
    old = _item(published="2026-07-28T06:00:00+00:00", url="https://e.com/3")
    kept = select._drop_stale([fresh, edge, old], "2026-08-02")
    urls = [i["url"] for i in kept]
    assert "https://e.com/1" in urls
    assert "https://e.com/3" not in urls


def test_drop_stale_window_is_anchored_to_jst():
    """`today` is a JST date (run.py's jst_today), so 23:59 must be JST too.

    With a +00:00 anchor the 07:00 JST cron (22:00 UTC the previous day) saw an
    effective ~10h lookback and the whole Japanese business day fell outside it.
    """
    business_day = _item(published="2026-08-03T18:15:00+09:00",
                         url="https://e.com/jp")
    kept = select._drop_stale([business_day], "2026-08-03")
    assert [i["url"] for i in kept] == ["https://e.com/jp"]


def test_drop_stale_keeps_undated_items():
    undated = _item(published="", url="https://e.com/1")
    assert select._drop_stale([undated], "2026-08-02") == [undated]


def test_drop_noise_removes_the_real_junk_pool():
    junk = [
        _item(title="Commonwealth Games 2026 medal table", url="https://e.com/1"),
        _item(title="These Comfy Back-to-School Skechers Were Made for Busy "
                    "Parents (& They're on Sale)", url="https://e.com/2"),
        _item(title="Pizzeria with locations in 10 states opens new shop in "
                    "Collin County suburb", url="https://e.com/3"),
        _item(title="The Stroller, Aug. 1, 2026: Events in the Alle-Kiski Valley",
              url="https://e.com/4"),
        _item(title="Josh Kerr storms to Commonwealth Games mile title",
              url="https://e.com/5", category="Sport"),
        _item(title="Lollapalooza 2026 livestream schedule: When and where to watch",
              url="https://e.com/6"),
    ]
    assert select._drop_noise(junk) == []


def test_drop_noise_keeps_hard_news():
    keep = [
        _item(title="California announces minimum wage increase as governor "
                    "taunts Trump", url="https://e.com/1"),
        _item(title="Hungary's Paks nuclear plant could be powered down this "
                    "weekend, PM says", url="https://e.com/2"),
        _item(title="Bank of Canada holds policy rate at 2.75%",
              url="https://e.com/3", desk="finance"),
    ]
    assert len(select._drop_noise(keep)) == 3


def test_drop_noise_matches_categories_on_whole_words():
    """Substring matching dropped real news: `Transport` contained `sport`,
    and `Food Contamination and Poisoning` / `Extreme weather` are hard news
    that merely borrow a noise word."""
    keep = [
        _item(title="Most Sydney road tolls to keep rising after Transurban "
                    "accused of blocking rivals", category="Transport",
              url="https://e.com/1"),
        _item(title="Two Deaths Linked to Cyclospora Outbreak in Michigan",
              category="Food Contamination and Poisoning", url="https://e.com/2"),
        _item(title="Floods displace 40,000 in Kyushu as record rain continues",
              category="Extreme weather", url="https://e.com/3"),
    ]
    assert len(select._drop_noise(keep)) == 3

    drop = [
        _item(title="Storms to Commonwealth mile title", category="Sport",
              url="https://e.com/4"),
        _item(title="Club signs record transfer", category="Sports",
              url="https://e.com/5"),
        _item(title="Ten ways to refresh your kitchen", category="Lifestyle",
              url="https://e.com/6"),
    ]
    assert select._drop_noise(drop) == []


def test_drop_noise_removes_corporate_press_releases():
    """The retired enrich._select_candidates excluded PR-wire categories;
    select_news inherited nothing, so GlobeNewswire items were eating ~20% of
    the Canada desk's enrichment budget."""
    pr = [
        _item(title="Beeline Signs LOI to Acquire TYTL, Expanding into "
                    "Blockchain", category="GlobeNewswire", url="https://e.com/1"),
        _item(title="Vireo Growth to Report Second Quarter 2026 Financial "
                    "Results", category="Business Wire News Releases",
              url="https://e.com/2"),
        _item(title="Acme Corp Announces Leadership Transition",
              category="Press Release", url="https://e.com/3"),
    ]
    assert select._drop_noise(pr) == []

    normal = [_item(title="Bank of Canada holds policy rate at 2.75%",
                    category="Business", url="https://e.com/4")]
    assert len(select._drop_noise(normal)) == 1


def test_drop_noise_keeps_hard_news_that_borrows_noise_phrasing():
    """Over-broad title patterns dropped real stories. Same call the owner
    already made on `\\bworld cup\\b`: the sports/consumer word is incidental."""
    keep = [
        _item(title="IOC bars Russian athletes from 2028 Olympic Games over "
                    "state doping", url="https://e.com/1"),
        _item(title="Rafah crossing now open for aid convoys, Egypt says",
              url="https://e.com/2"),
        _item(title="Nikkei posts best 3-day rally since 2020 as yen slides",
              url="https://e.com/3", desk="finance"),
        _item(title="Black Friday spending hits record $10.8bn as US consumers "
                    "defy inflation", url="https://e.com/4", desk="finance"),
    ]
    assert len(select._drop_noise(keep)) == 4


def test_drop_noise_never_raises_on_malformed():
    assert select._drop_noise([{"title": None}, {}, None]) == []


def test_drop_noise_keeps_hard_news_with_liveblog_format():
    """Liveblog format is legitimate hard-news coverage by wire services."""
    hard_news = [
        _item(title="Ukraine war: live updates on overnight strikes near Kyiv",
              url="https://e.com/1"),
        _item(title="Gaza ceasefire talks: what we know so far",
              url="https://e.com/2"),
        _item(title="Foxconn opens new $500M plant in Vietnam, creating 3,000 jobs",
              url="https://e.com/3"),
        _item(title="Qatar World Cup: UN report finds migrant-worker deaths undercounted",
              url="https://e.com/4"),
    ]
    kept = select._drop_noise(hard_news)
    assert len(kept) == 4
    urls = [i["url"] for i in kept]
    assert "https://e.com/1" in urls
    assert "https://e.com/2" in urls
    assert "https://e.com/3" in urls
    assert "https://e.com/4" in urls


def test_is_demoted_identifies_live_coverage():
    """Live-coverage and digest formats are demoted, not dropped."""
    live_updates = _item(title="Ukraine war: live updates on overnight strikes near Kyiv")
    what_we_know = _item(title="Gaza ceasefire talks: what we know so far")
    hard_news = _item(title="Bank of Canada holds policy rate at 2.75%")

    assert select.is_demoted(live_updates) is True
    assert select.is_demoted(what_we_know) is True
    assert select.is_demoted(hard_news) is False


def test_drop_local_gates_jp_ca_municipal_items():
    local = [
        _item(title="Young Cambridge man gets 9 years for gun, drug trafficking",
              desk="canada", url="https://e.com/1"),
        _item(title="Ontario shoppers warned about new food recalls at Costco",
              desk="canada", url="https://e.com/2"),
        _item(title="Cambridge has one third-party advertiser registered ahead "
                    "of municipal elections", desk="canada", url="https://e.com/3"),
        _item(title="Nara Park deer population sets record thanks to tourist "
                    "generosity", desk="japan", url="https://e.com/4"),
    ]
    assert select._drop_local(local) == []


def test_drop_local_keeps_national_jp_ca_stories():
    national = [
        _item(title="Bank of Canada holds policy rate at 2.75%",
              desk="canada", url="https://e.com/1"),
        _item(title="Carney government tables budget with defence spending hike",
              desk="canada", url="https://e.com/2"),
        _item(title="Japan's cabinet approves record defence budget",
              desk="japan", url="https://e.com/3"),
        _item(title="Bank of Japan signals end of yield curve control",
              desk="japan", url="https://e.com/4"),
    ]
    assert len(select._drop_local(national)) == 4


def test_drop_local_does_not_touch_other_desks():
    us = [_item(title="Young Cambridge man gets 9 years", desk="us",
                url="https://e.com/1")]
    assert len(select._drop_local(us)) == 1


def test_drop_local_keeps_national_corruption_stories():
    """A local-pattern match (charged with/pleads guilty/sentenced to N years)
    is overridden when the headline names a national office or institution —
    otherwise national political-corruption reporting is gated as if it were
    police-blotter copy."""
    national_corruption = [
        _item(title="Former deputy PM charged with bribery over defence contract",
              desk="canada", url="https://e.com/1"),
        _item(title="Ottawa lobbyist charged with foreign interference under "
                    "new espionage law", desk="canada", url="https://e.com/2"),
        _item(title="Ex-cabinet minister pleads guilty to campaign finance "
                    "violations", desk="japan", url="https://e.com/3"),
        _item(title="Former senator sentenced to 3 years for corruption in "
                    "procurement scandal", desk="japan", url="https://e.com/4"),
    ]
    assert len(select._drop_local(national_corruption)) == 4

    # The override must not be so broad it swallows the police-blotter case
    # this gate exists for — that fixture must still drop unchanged.
    local = [
        _item(title="Young Cambridge man gets 9 years for gun, drug trafficking",
              desk="canada", url="https://e.com/1"),
        _item(title="Ontario shoppers warned about new food recalls at Costco",
              desk="canada", url="https://e.com/2"),
        _item(title="Cambridge has one third-party advertiser registered ahead "
                    "of municipal elections", desk="canada", url="https://e.com/3"),
        _item(title="Nara Park deer population sets record thanks to tourist "
                    "generosity", desk="japan", url="https://e.com/4"),
    ]
    assert select._drop_local(local) == []


def test_drop_local_national_override_does_not_leak_local_items():
    """`_NATIONAL_PATTERNS` must not be so broad that it re-admits local
    police-blotter copy the gate exists to remove. 'national holiday parade'
    is local colour, not a national-institution reference — it must still
    drop despite containing the word 'national'."""
    leak = [
        _item(title="Local man charged with assault after national holiday "
                    "parade", desk="canada", url="https://e.com/1"),
    ]
    assert select._drop_local(leak) == []

    # Round-1 behaviour must still hold: national headlines still survive.
    national_corruption = [
        _item(title="Former deputy PM charged with bribery over defence contract",
              desk="canada", url="https://e.com/1"),
        _item(title="Ottawa lobbyist charged with foreign interference under "
                    "new espionage law", desk="canada", url="https://e.com/2"),
        _item(title="Ex-cabinet minister pleads guilty to campaign finance "
                    "violations", desk="japan", url="https://e.com/3"),
        _item(title="Former senator sentenced to 3 years for corruption in "
                    "procurement scandal", desk="japan", url="https://e.com/4"),
    ]
    assert len(select._drop_local(national_corruption)) == 4


def test_drop_local_diet_override_does_not_leak_diet_pill_scams():
    """`\bdiet\b` collided with ordinary diet/nutrition usage, letting local
    fraud items through. Split into `national diet` and `diet
    session/member/committee/...` so real Diet coverage still survives while
    'diet pill'/'diet supplements' consumer-fraud items still drop."""
    leaks = [
        _item(title="Ottawa man charged with running fake diet pill scheme",
              desk="canada", url="https://e.com/1"),
        _item(title="Tokyo man sentenced to 2 years for selling fraudulent "
                    "diet supplements", desk="japan", url="https://e.com/2"),
    ]
    assert select._drop_local(leaks) == []

    diet_coverage = [
        _item(title="National Diet passes record defence budget",
              desk="japan", url="https://e.com/3"),
        _item(title="Diet session opens with PM under pressure over funds "
                    "scandal", desk="japan", url="https://e.com/4"),
    ]
    assert len(select._drop_local(diet_coverage)) == 2

    # Round 1 and round 2 behaviour must still hold — assert explicitly.
    national_corruption = [
        _item(title="Former deputy PM charged with bribery over defence contract",
              desk="canada", url="https://e.com/5"),
        _item(title="Ottawa lobbyist charged with foreign interference under "
                    "new espionage law", desk="canada", url="https://e.com/6"),
        _item(title="Ex-cabinet minister pleads guilty to campaign finance "
                    "violations", desk="japan", url="https://e.com/7"),
        _item(title="Former senator sentenced to 3 years for corruption in "
                    "procurement scandal", desk="japan", url="https://e.com/8"),
    ]
    assert len(select._drop_local(national_corruption)) == 4

    municipal = [
        _item(title="Young Cambridge man gets 9 years for gun, drug trafficking",
              desk="canada", url="https://e.com/9"),
        _item(title="Ontario shoppers warned about new food recalls at Costco",
              desk="canada", url="https://e.com/10"),
        _item(title="Cambridge has one third-party advertiser registered ahead "
                    "of municipal elections", desk="canada", url="https://e.com/11"),
        _item(title="Nara Park deer population sets record thanks to tourist "
                    "generosity", desk="japan", url="https://e.com/12"),
    ]
    assert select._drop_local(municipal) == []


def test_drop_local_keeps_corporate_and_mass_casualty_stories():
    """`charged with`/`pleads guilty`/`sentenced to N years`/`festival` gated
    corporate-crime and mass-casualty reporting because _NATIONAL_PATTERNS only
    covered political office. These are exactly what the JP/CA desks exist for."""
    national = [
        _item(title="Nissan ex-chairman charged with under-reporting income "
                    "by 9bn yen", desk="japan", url="https://e.com/1"),
        _item(title="Nippon Steel executive charged with leaking trade secrets "
                    "to China", desk="japan", url="https://e.com/2"),
        _item(title="SNC-Lavalin pleads guilty to fraud over Libya contracts",
              desk="canada", url="https://e.com/3"),
        _item(title="Loblaws charged with price-fixing in national bread scandal",
              desk="canada", url="https://e.com/4"),
        _item(title="Quebec City mosque shooter sentenced to 40 years without "
                    "parole", desk="canada", url="https://e.com/5"),
        _item(title="Crowd crush at Osaka festival kills 11, injures dozens",
              desk="japan", url="https://e.com/6"),
    ]
    assert len(select._drop_local(national)) == 6

    # The added vocabulary must not be broad enough to re-admit the municipal
    # copy the gate exists for.
    municipal = [
        _item(title="Young Cambridge man gets 9 years for gun, drug trafficking",
              desk="canada", url="https://e.com/7"),
        _item(title="Ontario shoppers warned about new food recalls at Costco",
              desk="canada", url="https://e.com/8"),
        _item(title="Nara Park deer population sets record thanks to tourist "
                    "generosity", desk="japan", url="https://e.com/9"),
    ]
    assert select._drop_local(municipal) == []


def test_score_prefers_tier1_corroborated_recent():
    cov = {select._norm_title("Cabinet approves emergency energy package"): 4}
    strong = _item(title="Cabinet approves emergency energy package", tier=1,
                   published="2026-08-02T06:00:00+00:00")
    weak = _item(title="Some minor regional filing note", tier=3,
                 published="2026-08-01T06:00:00+00:00")
    assert select._score(strong, cov, set()) > select._score(weak, cov, set())


def test_score_penalises_already_seen_articles():
    item = _item(title="Cabinet approves emergency energy package", tier=1)
    seen = {select.article_hash(item)}
    assert select._score(item, {}, seen) < select._score(item, {}, set())


def test_score_penalises_demoted_live_coverage():
    """A live-coverage/digest headline must score strictly lower than an
    otherwise-identical concrete article, but not so low it's excluded."""
    concrete = _item(title="Ukraine strikes energy plant near Kyiv", tier=1)
    live = _item(title="Ukraine war: live updates on overnight strikes near Kyiv",
                 tier=1)
    assert select._score(live, {}, set()) < select._score(concrete, {}, set())


def test_cap_gov_limits_federal_register_items():
    items = [_item(source="federal_register", url=f"https://e.com/{n}")
             for n in range(5)]
    items += [_item(source="bbc-world", url="https://e.com/x")]
    capped = select._cap_gov(items, limit=2)
    fedreg = [i for i in capped if i["source"] == "federal_register"]
    assert len(fedreg) == 2
    assert len(capped) == 3


def test_cap_per_feed_tops_up_when_one_source_owns_the_desk():
    items = [_item(source="npr", url=f"https://e.com/{n}") for n in range(10)]
    capped = select._cap_per_feed(items, limit=3, target=10)
    assert len(capped) == 10
    assert [i["url"] for i in capped] == [i["url"] for i in items]


def test_cap_per_feed_limits_per_source_when_alternatives_exist():
    items = []
    for source in ("a", "b", "c", "d"):
        items += [_item(source=source, url=f"https://e.com/{source}{n}")
                  for n in range(4)]
    capped = select._cap_per_feed(items, limit=3, target=10)
    counts = {}
    for i in capped:
        counts[i["source"]] = counts.get(i["source"], 0) + 1
    assert all(c <= 3 for c in counts.values())


def test_cap_per_feed_empty_list():
    assert select._cap_per_feed([], limit=3, target=10) == []


def test_cap_per_feed_never_raises_on_malformed():
    assert select._cap_per_feed([{"source": "a"}, None, {}], limit=3, target=10) is not None


def test_select_candidates_returns_all_five_desks():
    # NOTE: differentiator must survive _norm_title's len>2 token filter, so
    # plain "us" (2 chars) can't be the only distinguishing word — it would
    # normalize away and _dedupe would treat this item as a near-duplicate of
    # "world"'s. Use a longer, desk-distinct phrase instead.
    titles = {
        "world": "Major policy decision for the world stage announced today",
        "us": "Major policy decision for Washington announced today",
        "japan": "Major policy decision for Tokyo announced today",
        "canada": "Major policy decision for Ottawa announced today",
        "finance": "Major policy decision for global markets announced today",
    }
    bundle = {"news": [_item(desk=d, url=f"https://e.com/{d}", title=titles[d])
                       for d in ("world", "us", "japan", "canada", "finance")],
              "coverage": []}
    out = select.select_candidates(bundle, set())
    assert set(out) == {"world", "us", "japan", "canada", "finance"}
    assert all(len(v) == 1 for v in out.values())


def test_select_candidates_caps_per_desk():
    # NOTE: a bare 1-2 digit number is stripped by _norm_title's len>2 filter,
    # so all 25 titles would normalize to the same token set and collapse
    # under _dedupe before the cap is ever reached. Use a glued alphanumeric
    # differentiator ("item000") that survives normalization instead.
    news = [_item(desk="world", url=f"https://e.com/{n}",
                  title=f"Distinct major world policy story about item{n:03d} today")
            for n in range(25)]
    out = select.select_candidates({"news": news, "coverage": []}, set(),
                                   per_desk=10)
    assert len(out["world"]) == 10


def test_select_candidates_never_raises_on_garbage():
    assert select.select_candidates({}, set())["world"] == []
    assert select.select_candidates({"news": None}, None)["us"] == []


def test_select_candidates_is_deterministic():
    # NOTE: a bare 1-2 digit number is stripped by _norm_title's len>2 filter,
    # so all 15 titles would normalize to the same token set and collapse
    # under _dedupe to a single item — proving nothing about the (-score, url)
    # tie-break. Use a glued alphanumeric differentiator that survives
    # normalization instead.
    news = [_item(desk="us", url=f"https://e.com/{n}",
                  title=f"Congress advances major appropriations measure "
                        f"item{n:03d}")
            for n in range(15)]
    bundle = {"news": news, "coverage": []}
    first = select.select_candidates(bundle, set())
    second = select.select_candidates(bundle, set())
    assert len(first["us"]) > 1
    assert [i["url"] for i in first["us"]] == [i["url"] for i in second["us"]]


# --------------------------------------------------------------------------- #
# Desk-aware salience (D1) — the 2026-08-15 Chiba/Nvidia misses
# --------------------------------------------------------------------------- #
def test_salience_reads_disaster_vocabulary_on_the_news_desks():
    """The 2026-08-15 Chiba floods (8 dead, 351mm of rain, first-ever JMA Level
    5 warning for the prefecture) scored `salient=[]`: _SALIENT_TERMS was
    policy/macro vocabulary only, with no word for flood, storm, killed or
    evacuate. The Indonesia earthquake that DID get selected scored only by
    accident, on the substring 'strike' inside 'earthquake strikes'."""
    flood = _item(desk="japan",
                  title="Torrential rain in Japan's Chiba Pref. floods roads, "
                        "eight dead as thousands evacuate")
    assert select._desk_salience(flood, "japan") > 0
    assert select._desk_salience(flood, "world") > 0


def test_salience_reads_market_vocabulary_on_the_finance_desk():
    """Nvidia's $500bn AI financing initiative scored `salient=[]` too: no term
    for chip, financing, private credit or data center."""
    deal = _item(desk="finance",
                 title="Private credit roundup: Nvidia's half trillion for "
                       "chips financing, plus others")
    assert select._desk_salience(deal, "finance") > 0


def test_salience_matches_new_terms_on_word_boundaries():
    """_SALIENT_TERMS is a naive `in` substring test, catastrophic for short
    tokens: 'ai' matches said/again/against/campaign. The desk vocabulary is
    therefore anchored on word boundaries."""
    decoy = _item(title="Campaign against the new bill, he said again")
    assert select._desk_salience(decoy, "finance") == 0
    real = _item(title="Chipmakers race to build AI data centers")
    assert select._desk_salience(real, "finance") > 0


def test_salience_lift_is_capped_so_word_count_cannot_own_a_desk():
    """A headline is not more consequential for using nine disaster words than
    two — they are one fact seen many ways. Measured on the 2026-08-15 pool,
    letting the disaster lift run to three terms took the world desk's top ten
    to eight-tenths disaster: Etna three times, two wildfires, two quakes and an
    oil spill."""
    piled = _item(title="Flood, storm, wildfire, earthquake, tsunami and "
                        "landslide disaster kills dozens, thousands evacuate")
    two = _item(title="Eight dead after flooding")
    assert select._desk_salience(piled, "world") == select._desk_salience(two, "world")
    assert select._desk_salience(piled, "world") <= select._SALIENCE_LIFT_CAP


# --------------------------------------------------------------------------- #
# Content-based desk re-route (D3/D4)
# --------------------------------------------------------------------------- #
def test_reroute_moves_a_world_feed_finance_story_to_the_finance_desk():
    """`desk` comes from the feed registry, so Reuters' AI-financing stories
    landed on `world`. codex_engine._finance_bundle narrows the finance pass to
    desk == 'finance', and prompts/world.md tells the world pass that company
    and market stories belong to the finance pass — so the story was unwritable
    by either pass."""
    item = _item(desk="world", title="Goldman in talks with investors on Nvidia "
                                     "financing deal after landing prized role")
    assert select._route_desk(item) == "finance"


def test_reroute_leaves_geopolitics_and_disaster_on_the_world_desk():
    """A wrong move is worse than no move: these carry money words but are not
    market stories."""
    for title in (
        "Trump asks US Supreme Court to allow $400m ballroom project to proceed",
        "Strong 7.7-magnitude earthquake strikes eastern Indonesia, killing two",
        "BP to develop offshore gasfield in Venezuela with firms linked to "
        "Trump administration",
    ):
        assert select._route_desk(_item(desk="world", title=title)) == "world"


def test_world_feed_canada_story_routes_to_canada_desk():
    item = _item(
        desk="world",
        title="Canada announces sanctions on five Iranian officials over Strait of Hormuz blockade",
    )
    assert select._route_desk(item) == "canada"


def test_foreign_wire_story_does_not_consume_canada_capacity():
    item = _item(
        desk="canada",
        source="torontostar",
        title="Dutch police break up far-right protest in The Hague",
    )
    assert select._route_desk(item) == "world"


def test_drop_stale_uses_actual_run_time_for_full_36_hours():
    prior_business_day = _item(published="2026-08-01T00:00:00+00:00")
    kept = select._drop_stale(
        [prior_business_day], "2026-08-02",
        as_of="2026-08-02T07:00:00+09:00",
    )
    assert kept == [prior_business_day]


def test_reroute_clears_pan_asian_business_off_the_japan_desk():
    """Only ~52% of the japan desk mentioned Japan: nikkei-asia is a pan-Asian
    business wire and contributed 50 of 120 items — Indonesia's budget,
    Malaysia's GDP. Those crowded out the Chiba floods."""
    for title in ("Indonesia's Prabowo targets 6% growth, lower budget deficit "
                  "in 2027",
                  "Malaysia Q2 GDP growth accelerates to 6.0%"):
        assert select._route_desk(_item(desk="japan", title=title)) != "japan"


def test_reroute_keeps_anything_with_a_japan_signature_on_the_japan_desk():
    for title in ("Authorities urge continued caution after heavy rain in Chiba "
                  "leaves eight dead",
                  "Japan weighs state reinsurance for tankers to keep Gulf oil "
                  "flowing",
                  "Bank of Japan holds rates as the yen slides"):
        assert select._route_desk(_item(desk="japan", title=title)) == "japan"


def test_reroute_never_raises_on_malformed():
    assert select._route_desk(None) == "world"
    assert select._route_desk({}) == "world"
    assert select._route_desk({"desk": 7, "title": None}) == "world"


# --------------------------------------------------------------------------- #
# Regression: the two stories the 2026-08-15 pipeline missed
# --------------------------------------------------------------------------- #
def _bundle(news):
    return {"news": news, "date": "2026-08-15",
            "coverage": fetch_sources._build_coverage(news, {})}


def _chiba_pool():
    """The seven real 2026-08-15 Chiba-flood items (8 dead, 351mm of rain — a
    record since 1966 — 7,359 people in shelters, the first JMA Level 5 warning
    ever issued for the prefecture), plus the japan-desk field they lost to."""
    def n(title, source, url, desk):
        return _item(title=title, source=source, url=url, desk=desk, tier=1,
                     published="2026-08-14T10:00:00+00:00")

    return [
        n("'Unprecedented' rain in Japan kills eight people", "bbc-world",
          "https://bbc.co.uk/1", "world"),
        n("‘Unprecedented’ rain kills five in Japan as thousands "
          "stranded at airport", "guardian-world", "https://guardian.com/1",
          "world"),
        n("Japan: Record rains kill eight, strand thousands at Narita airport",
          "france24", "https://france24.com/1", "world"),
        n("Authorities urge continued caution after heavy rain in Chiba leaves "
          "eight dead", "japantimes", "https://japantimes.co.jp/1", "japan"),
        n("Rain, Floods in Japan’s Chiba Prefecture Leave 6 Dead",
          "japannews-yomiuri", "https://japannews.yomiuri.co.jp/1", "japan"),
        n("Torrential rain in Japan's Chiba Pref. prompts warnings over cars "
          "trapped on flooded roads", "mainichi-en", "https://mainichi.jp/1",
          "japan"),
        n("Mainichi reporter stranded 7 hours on flooded roads after torrential "
          "rain hit Chiba Pref.", "mainichi-en", "https://mainichi.jp/2",
          "japan"),
        # The japan-desk field that beat them on 2026-08-15.
        n("Indonesia's Prabowo targets 6% growth, lower budget deficit in 2027",
          "nikkei-asia", "https://asia.nikkei.com/1", "japan"),
        n("Malaysia Q2 GDP growth accelerates to 6.0%", "nikkei-asia",
          "https://asia.nikkei.com/2", "japan"),
        n("Trump's tariffs unevenly reshape American manufacturing",
          "nikkei-asia", "https://asia.nikkei.com/3", "japan"),
        n("China EVs close in on Japan automakers' Australia stronghold, led by "
          "BYD", "nikkei-asia", "https://asia.nikkei.com/4", "japan"),
        n("Tencent to become top shareholder of Manus in unwinding of Meta deal",
          "nikkei-asia", "https://asia.nikkei.com/5", "japan"),
        n("Japan weighs state reinsurance for tankers to keep Gulf oil flowing",
          "nikkei-asia", "https://asia.nikkei.com/6", "japan"),
        n("Major U.S. defense firms race to make cheaper missiles", "japantimes",
          "https://japantimes.co.jp/2", "japan"),
        n("Japan defense minister visits war-linked Yasukuni Shrine as PM stays "
          "away", "japantimes", "https://japantimes.co.jp/3", "japan"),
        n("Visit to Northern Territories: Russian President Commits "
          "Unacceptable, Outrageous Act", "japannews-yomiuri",
          "https://japannews.yomiuri.co.jp/2", "japan"),
        n("Japan's Takaichi sends offering to Yasukuni Shrine on WWII "
          "anniversary", "nikkei-asia", "https://asia.nikkei.com/7", "japan"),
        n("North Korea threatens to exercise self-defense as it slams US-South "
          "Korea military drills", "mainichi-en", "https://mainichi.jp/3",
          "japan"),
    ]


def test_select_candidates_surfaces_the_chiba_floods_on_the_japan_desk():
    """2026-08-15 regression. Seven items from six tier-1 outlets, all through
    every filter, all scoring 3.5-3.8 against a japan-desk cutoff of 3.9 —
    missed by 0.1. Two causes: no disaster vocabulary in the salience list, and
    a corroboration signal that never fired because the seven headlines
    produced seven distinct story keys."""
    news = _chiba_pool()
    bundle = _bundle(news)
    japan = select.select_candidates(bundle, set())["japan"]
    titles = " | ".join(i["title"].lower() for i in japan)
    assert "chiba" in titles, titles

    # The margin, not just the membership: on 2026-08-15 the japan desk's
    # 10th-place cutoff was 3.9 and the best Chiba item scored 3.8.
    coverage = select._coverage_map(bundle)
    chiba = [i for i in news
             if i["source"] == "japantimes" and "Chiba" in i["title"]][0]
    assert select._score(chiba, coverage, set()) >= 3.9

    # And the pan-Asian business wire that was taking japan-desk slots is gone.
    assert not [i for i in japan if "Malaysia Q2 GDP" in i["title"]]


def _nvidia_pool():
    """The real 2026-08-15 Nvidia AI-financing items. The three strongest come
    through `reuters-gnews`, whose feed-registry desk is `world`."""
    def n(title, source, url, desk, tier=1, unresolved=False):
        return _item(title=title, source=source, url=url, desk=desk, tier=tier,
                     published="2026-08-14T10:00:00+00:00",
                     gnews_unresolved=unresolved)

    return [
        n("Private credit roundup: Nvidia's half trillion for chips financing, "
          "plus others - Reuters", "reuters-gnews", "https://news.google.com/1",
          "world", unresolved=True),
        n("Goldman in talks with investors on Nvidia financing deal after "
          "landing prized role, sources say - Reuters", "reuters-gnews",
          "https://news.google.com/2", "world", unresolved=True),
        n("Nvidia scales back $250 billion OpenAI data center guarantee, WSJ "
          "reports - Reuters", "reuters-gnews", "https://news.google.com/3",
          "world", unresolved=True),
        n("Goldman’s latest cash cow is all about funding the AI "
          "infrastructure boom", "cnbc-top", "https://cnbc.com/1", "finance",
          tier=2),
        n("Nvidia discloses $21bn stake in SpaceX", "ft-home",
          "https://ft.com/1", "finance"),
        # The finance-desk field.
        n("Will America’s new sanctions on Russian oil force a peace deal?",
          "economist-finance", "https://economist.com/1", "finance"),
        n("How Donald Trump can dodge a Supreme Court tariff block",
          "economist-finance", "https://economist.com/2", "finance"),
        n("Would inflation-linked bonds survive an inflationary default?",
          "economist-finance", "https://economist.com/3", "finance"),
        n("US inflation eases as food and fuel costs cool", "bbc-business",
          "https://bbc.co.uk/2", "finance"),
        n("Homes near pylons to get 250 pounds a year off energy bills",
          "bbc-business", "https://bbc.co.uk/3", "finance"),
        n("Royal Parks gardeners to strike over pay", "bbc-business",
          "https://bbc.co.uk/4", "finance"),
        n("ECB to start implementing enhanced repo facility for central banks",
          "ecb-press", "https://ecb.europa.eu/1", "finance"),
    ]


def test_select_candidates_routes_nvidia_ai_financing_to_the_finance_desk():
    """2026-08-15 regression. Nvidia's $500bn AI financing initiative (Apollo,
    BlackRock, Blackstone, Brookfield, Goldman, KKR) was unwritable: the three
    strongest items arrived through `reuters-gnews`, a desk="world" feed, so
    _finance_bundle never saw them, while prompts/world.md tells the world pass
    that company and market stories are the finance pass's job."""
    finance = select.select_candidates(_bundle(_nvidia_pool()), set())["finance"]
    hits = [i for i in finance if "nvidia" in i["title"].lower()
            and ("financing" in i["title"].lower()
                 or "credit" in i["title"].lower())]
    assert hits, [i["title"] for i in finance]


# --------------------------------------------------------------------------- #
# Same-story cap. Fixing corroboration made the scorer REWARD multi-outlet
# stories, which mechanically stacks near-identical articles at the top of a
# desk: on 2026-08-15 the Chiba floods took 4 of japan's 10 slots and the
# Indonesia earthquake appeared at world #4 and #8. Deduplication cannot fix it
# — those are genuinely distinct articles (best cross-outlet title Jaccard 0.27,
# far under _dedupe's 0.8) — so the cap belongs in selection, next to the gov
# and per-feed caps it mirrors.
# --------------------------------------------------------------------------- #
def _pool_filler(n=600, desk="us"):
    """Realistic pool bulk. The clustering rule is calibrated against the POOL —
    its rare-anchor threshold is 1% of it, floor 3 — so in a 20-item fixture a
    four-outlet story's own signature token looks common and nothing clusters.
    That floor is right for the 1,600-item pool this runs on; the fixture is
    what has to be realistic. These fillers share only high-frequency tokens, so
    they never cluster with each other or with anything else."""
    # Each title must be dedupe-DISTINCT: an earlier version reused the same
    # words with only the number changing, and _norm_title drops tokens of <=2
    # chars, so all 600 collapsed to one item and the pool never grew at all.
    return [_item(title=f"Vurnex{i}pol Grathe{i}dor Silban{i}ket filing",
                  source=f"filler-{i}", url=f"https://f.example/{i}", desk=desk,
                  published="2026-08-14T10:00:00+00:00") for i in range(n)]


def test_one_story_cannot_take_more_than_two_slots_on_a_desk():
    desks = select.select_candidates(
        {"news": _chiba_pool() + _pool_filler(), "date": "2026-08-15",
         "coverage": []}, set())
    japan = desks["japan"]
    chiba = [i for i in japan if "chiba" in (i["title"] or "").lower()]
    assert len(chiba) <= select.SAME_STORY_CAP, [i["title"] for i in chiba]
    # ...and the story still leads. Capping variety must not undo the rescue.
    assert chiba, [i["title"] for i in japan]


def test_same_story_cap_tops_up_rather_than_starving_a_thin_desk():
    """A desk whose whole day is one story must still hand the model something
    to work with, including a spare: roughly a third of candidates never
    enrich, and an unenriched candidate cannot lead a section."""
    only_chiba = [i for i in _chiba_pool()
                  if i["desk"] == "japan" and "chiba" in i["title"].lower()]
    japan = select.select_candidates(
        {"news": only_chiba + _pool_filler(), "date": "2026-08-15",
         "coverage": []}, set())["japan"]
    assert len(japan) >= select.SAME_STORY_FLOOR, [i["title"] for i in japan]


def test_same_story_cap_does_not_pad_a_healthy_desk_back_to_full():
    """The first version topped up to the desk target, which restored the very
    duplicates it had just removed — a desk of ten items with four copies of one
    story keeps eight, and 8 < 10. Eight distinct stories beats ten with four
    the same."""
    japan = select.select_candidates(
        {"news": _chiba_pool() + _pool_filler(), "date": "2026-08-15",
         "coverage": []}, set())["japan"]
    chiba = [i for i in japan if "chiba" in (i["title"] or "").lower()]
    assert len(chiba) <= select.SAME_STORY_CAP, [i["title"] for i in chiba]


def test_cap_same_story_is_inert_without_a_usable_matcher():
    """`select_candidates` imports the matcher lazily and passes None if it is
    unavailable; the desks must then go uncapped rather than empty."""
    items = [_item(title=f"t{i}", url=f"https://e.com/{i}") for i in range(4)]
    assert select._cap_same_story(items, None) == items
    assert select._cap_same_story(items, "not callable") == items


def test_same_story_cap_is_deterministic():
    bundle = {"news": _chiba_pool(), "date": "2026-08-15", "coverage": []}
    first = select.select_candidates(bundle, set())["japan"]
    second = select.select_candidates(bundle, set())["japan"]
    assert [i["url"] for i in first] == [i["url"] for i in second]


# --------------------------------------------------------------------------- #
# Routing veto: weapons and violent crime. 2026-08-16 regression.
# --------------------------------------------------------------------------- #
def test_a_gun_buyback_is_not_a_markets_story():
    """`buyback` is a STRONG market term, so one match was enough to move
    "Australian state to launch gun buyback after Bondi Beach attack" from world
    to finance — from two outlets at once. It compounded badly: those two were
    enrichable where the day's genuine finance items (Reuters behind DataDome,
    FT stubs) were not, so they became the only Brief-eligible finance
    candidates and the finance brief came back EMPTY, against 3 stories the day
    before. A buyback of weapons is a public-safety story."""
    for title in (
        "Australian state to launch gun buyback after Bondi Beach attack",
        "Australia news live: Pauline Hanson opposes major gun buyback agreed "
        "in national cabinet",
    ):
        assert select._route_desk(_item(title=title, desk="world")) == "world"


def test_a_share_buyback_still_routes_to_finance():
    """The veto must not cost us the financial sense of the same word."""
    assert select._route_desk(_item(
        title="Apple announces $110bn share buyback, largest in history",
        desk="world")) == "finance"
    assert select._route_desk(_item(
        title="Buyback wave lifts S&P 500 to a fresh record", desk="world")
    ) == "finance"


def test_sources_that_never_serve_a_body_are_penalised_like_gnews():
    """A candidate without `content` cannot lead the Brief, so a source that
    never yields an extractable article is worth less than its tier suggests —
    it occupies a slot it can never use. Measured 2026-08-16: ft.com serves
    652-char stubs, reuters via Google News is behind DataDome, and
    feeds.content.dowjones.io 401s. All are tier 1, so they start 1.5 points
    ahead of the tier-2 feeds that do serve bodies, and on 2026-08-16 they took
    six of the finance desk's ten slots and the finance brief shipped EMPTY."""
    body = _item(title="Chipmaker lifts revenue outlook", source="cnbc-top",
                 desk="finance", tier=1)
    no_body = _item(title="Chipmaker lifts revenue outlook", source="ft-home",
                    desk="finance", tier=1, url="https://ft.com/x")
    assert select._score(no_body, {}, set(), "finance") < \
        select._score(body, {}, set(), "finance")


def test_no_body_penalty_does_not_reach_sources_that_do_serve_bodies():
    for source in ("pymnts", "fortune", "techcrunch", "guardian-business",
                   "kyodo-english", "japantoday"):
        assert source not in select._NO_BODY_SOURCES
