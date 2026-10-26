"""world-intel — deterministic candidate selection.

Sits between `fetch_all` and `enrich_news`. Turns the ~1,200-item raw feed pool
into ~10 enrichment candidates per desk: dedupe -> staleness -> noise ->
local-significance -> score -> gov cap -> take.

Named `select_news` rather than `select` because `select` is a stdlib module and
`scripts/` is on sys.path — shadowing it would break requests/urllib3.

Pure and offline: no network, no LLM, no I/O. Never raises. Import-safe.
"""

from __future__ import annotations

import datetime
import hashlib
from worldintel.core import coverage_policy
import re
from worldintel import config as wi_config
from urllib.parse import urlparse

# Title tokens with no discriminating power, dropped before similarity compare.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "with", "as", "by", "from", "is", "are", "was", "were", "be", "been",
    "it", "its", "this", "that", "these", "those", "new", "says", "said",
    "after", "over", "into", "about", "amid",
}

# Categories that are never news for this product. Matched as whole tokens,
# case-insensitive — substring matching dropped real news, because `Transport`
# contains `sport`. Plurals are listed explicitly since tokens don't stem.
_NOISE_CATEGORIES = frozenset((
    "sport", "sports", "lifestyle", "culture", "entertainment", "travel",
    "food", "arts", "music", "film", "television", "fashion",
    "recipe", "recipes", "obituar", "obituary", "obituaries",
    "puzzle", "puzzles", "crossword", "crosswords",
    "horoscope", "horoscopes", "weather",
    # Corporate PR wires. select_news inherited no press-release filter from the
    # retired enrich._select_candidates, so GlobeNewswire items were taking ~20%
    # of the Canada desk's enrichment budget.
    "globenewswire", "businesswire", "accesswire",
))

# Multi-word PR-wire categories cannot be single tokens, so they are matched as
# case-insensitive substrings. Probed against every registered feed on
# 2026-08-02: only financialpost emits these ("Business Wire News Releases",
# "GlobeNewswire"). The central-bank press feeds emit "general" or topic
# categories, so genuine institutional releases are unaffected.
_NOISE_CATEGORY_PHRASES = ("press release", "pr newswire", "business wire")

# Title patterns that mark an item as noise regardless of its category.
#
# Removed after review, each for a demonstrated false drop or for being dead:
#   \bolympic       -> "IOC bars Russian athletes from 2028 Olympic Games over
#                      state doping"; \bmedal table\b + the Sport category
#                      already cover the actual noise (same call the owner made
#                      on \bworld cup\b).
#   \bnow open\b    -> "Rafah crossing now open for aid convoys, Egypt says"
#   \bbest \d+\b    -> "Nikkei posts best 3-day rally since 2020 as yen slides"
#   \bblack friday\b-> "Black Friday spending hits record $10.8bn as US
#                      consumers defy inflation"
#   \breview:\b     -> dead: the \b after ':' needs an adjacent word character,
#   \brecap:\b         so "Review: The Batman" never matched. Not repaired —
#                      the culture/entertainment/film categories handle these.
_NOISE_PATTERNS = (
    r"\bmedal table\b", r"\bhoroscope\b", r"\brecipe\b", r"\bcrossword\b",
    r"\bon sale\b", r"\bdeal[s]? of the\b", r"\b\d+ things\b",
    r"\blivestream schedule\b", r"\bwhat to watch\b", r"\bwhere to watch\b",
    r"\bgift guide\b",
    r"\bopens new (?:shop|store|restaurant|location|branch|outlet|cafe|bar)\b",
    r"\bgrand opening\b",
    r"\bevents in the\b", r"\bthings to do\b", r"\bcommonwealth games\b",
    r"\bpremier league\b", r"\bnfl\b",
    r"\bnba\b", r"\bmlb\b", r"\bcelebrity\b", r"\bred carpet\b",
)

_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS), re.IGNORECASE)

# Live-coverage and digest formats. These are NOT dropped: wire services use
# them for wars, elections and disasters. They are demoted by the scorer
# (Task 4) so a concrete article wins when one exists, mirroring analysis.md's
# rule to prefer hard news "when a concrete hard-news candidate is available".
_DEMOTE_PATTERNS = (
    r"\blive updates\b", r"\bliveblog\b", r"\blive blog\b",
    r"\bas it happened\b", r"\bwhat we know so far\b",
    r"\bmorning briefing\b", r"\bnewsletter\b", r"\broundup\b",
    # Two more digest formats, added when the disaster vocabulary below started
    # lifting them: a wrap and a photo gallery both quote every disaster of the
    # week, so they scored like the week's biggest story. "News Wrap:
    # Record-breaking heat wave grips Europe" and "The week around the world in
    # 20 pictures" both reached the us desk's top ten on 2026-08-15.
    r"\bnews wrap\b", r"\bin \d+ pictures\b",
    r"\bweek in pictures\b", r"\bvideo(?:-only)? programme\b",
    r"\bvideo report\b", r"\bwatch:\s", r"\bphoto gallery\b",
)
_DEMOTE_RE = re.compile("|".join(_DEMOTE_PATTERNS), re.IGNORECASE)

_WORD_RE = re.compile(r"[a-z0-9']+")


def _as_list(x) -> list:
    return x if isinstance(x, list) else []


def _str(x) -> str:
    return x if isinstance(x, str) else ""


def _norm_title(title: str) -> frozenset:
    """Title -> a comparable token set (lowercased, punctuation and stopwords
    stripped). Used for near-duplicate detection across outlets."""
    tokens = _WORD_RE.findall(_str(title).lower())
    return frozenset(t for t in tokens if t not in _STOPWORDS and len(t) > 2)


def _similar(a: frozenset, b: frozenset, threshold: float = 0.8) -> bool:
    """Jaccard similarity >= threshold. Only compares sets of >=5 tokens, so
    short headlines ('Fed holds rates') are never collapsed by coincidence."""
    if len(a) < 5 or len(b) < 5:
        return False
    union = a | b
    if not union:
        return False
    return len(a & b) / len(union) >= threshold


def is_demoted(item) -> bool:
    """True when an item is live-coverage or digest formatting. Task 4's scorer
    penalises these rather than dropping them. Never raises."""
    return bool(_DEMOTE_RE.search(_str((item or {}).get("title"))))


def _dedupe(news: list) -> list:
    """Drop exact-URL duplicates, then near-identical titles. First wins."""
    kept, seen_urls, seen_titles = [], set(), []
    for item in _as_list(news):
        if not isinstance(item, dict):
            continue
        url = _str(item.get("url")).strip()
        if url and url in seen_urls:
            continue
        tokens = _norm_title(item.get("title"))
        if any(_similar(tokens, prev) for prev in seen_titles):
            continue
        if url:
            seen_urls.add(url)
        if tokens:
            seen_titles.append(tokens)
        kept.append(item)
    return kept


def _drop_stale(news: list, today: str, hours: int | None = None, as_of: str = "") -> list:
    """Keep items published within `hours` of the actual run time in JST.

    `today` comes from run.py's jst_today(), so the reference must carry the JST
    offset. Pairing a JST date with +00:00 mixed calendars: the 07:00 JST cron
    runs at 22:00 UTC the previous day, cutting the effective lookback to ~10h
    and hiding the entire Japanese business day from the brief.

    An item with no parseable date is KEPT — several feeds omit dates and a
    silent drop would lose real stories.
    """
    try:
        ref = datetime.datetime.fromisoformat(
            as_of.replace("Z", "+00:00") if as_of
            else f"{today}T07:00:00"
        )
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=wi_config.get().timezone)
    except wi_config.ConfigError:

        raise

    except Exception:
        return _as_list(news)
    if hours is None:
        hours = wi_config.get().tuning.get("freshness_hours", 36)
    cutoff = ref - datetime.timedelta(hours=hours)

    kept = []
    for item in _as_list(news):
        if not isinstance(item, dict):
            continue
        raw = _str(item.get("published")).strip()
        if not raw:
            kept.append(item)
            continue
        try:
            dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
        except wi_config.ConfigError:

            raise

        except Exception:
            kept.append(item)
            continue
        if dt >= cutoff:
            kept.append(item)
    return kept


def _is_noise_category(category) -> bool:
    """True when a category is nothing but noise vocabulary.

    Whole tokens, not substrings: `Transport` must not match `sport`. And EVERY
    meaningful token must be noise, so a hard-news category that merely borrows
    a noise word — `Food Contamination and Poisoning`, `Extreme weather` — is
    kept, while the bare `Food` / `Weather` section labels still drop. An
    unrecognised or empty category is never noise.
    """
    cat = _str(category).lower()
    if any(phrase in cat for phrase in _NOISE_CATEGORY_PHRASES):
        return True
    tokens = [t for t in _WORD_RE.findall(cat) if t not in _STOPWORDS]
    if not tokens:
        return False
    return all(token in _NOISE_CATEGORIES for token in tokens)


def _drop_noise(news: list) -> list:
    """Remove sport, lifestyle, listicle, liveblog and local-colour items."""
    kept = []
    for item in _as_list(news):
        if not isinstance(item, dict):
            continue
        if _is_noise_category(item.get("category")):
            continue
        title = _str(item.get("title"))
        if not title:
            continue
        if _NOISE_RE.search(title):
            continue
        kept.append(item)
    return kept


def desk_ids() -> tuple:
    return tuple(wi_config.get().desk_ids)


def local_desk_ids() -> tuple:
    return tuple(wi_config.get().local_desk_ids)


def selection_settings() -> dict:
    tuning = wi_config.get().tuning
    return {"ranking_profile": wi_config.get().ranking,
            "ranking_profile_hash": coverage_policy.profile_hash(wi_config.get().ranking),
            "desks": list(desk_ids()), "local_desks": list(local_desk_ids()),
            "fresh_hours": tuning.get("freshness_hours", 36),
            "per_desk": tuning.get("per_desk_slots", 10),
            "per_feed_cap": tuning.get("per_outlet_cap", 3)}

# JP/CA national-significance gate: municipal, regional and consumer items that
# are not national news. Conservative — when in doubt, keep.
_LOCAL_PATTERNS = (
    r"\bfood recall", r"\brecalls?:", r"\bshoppers warned\b",
    r"\bcity council\b", r"\btown council\b", r"\bmunicipal election",
    r"\bschool board\b", r"\bcity hall\b", r"\bmayor of\b",
    r"\blocal business\b", r"\bcommunity event", r"\broad closure",
    r"\bconstruction detour\b", r"\bsentenced to \d+ years?\b",
    r"\bgets \d+ years?\b", r"\bpleads guilty\b", r"\bcharged with\b",
    r"\bdrug trafficking\b", r"\bmissing (?:man|woman|teen|boy|girl)\b",
    r"\bhouse fire\b", r"\bcar crash\b", r"\bcollision on\b",
    r"\bdeer\b", r"\btourist generosity\b", r"\bfestival\b",
    r"\blisted for \$", r"\bsells for \$",
)
_LOCAL_RE = re.compile("|".join(_LOCAL_PATTERNS), re.IGNORECASE)


# National-significance override: a local-pattern match is IGNORED when the
# headline names a national office, institution or figure. Without this,
# "Former deputy PM charged with bribery" is gated as police-blotter copy —
# the exact reporting the Japan and Canada desks exist to carry.
_NATIONAL_PATTERNS = (
    r"\bprime minister\b", r"\bdeputy pm\b",
    # Deliberately broad: "Premier" also appears in business names, so this can
    # let a local crime item through. Kept anyway — narrowing it would risk
    # gating "Ontario Premier charged with...", and a leak costs a score
    # comparison while a false drop costs the story entirely.
    r"\bpremier\b",
    r"\bminister\b", r"\bcabinet\b", r"\bparliament\b",
    r"\bnational diet\b",
    r"\bdiet (?:session|member|committee|building|passed|approved|enacted|dissolved)\b",
    r"\bsenator\b", r"\bsenate\b", r"\bmp\b", r"\blawmaker\b",
    r"\bgovernor\b", r"\bambassador\b", r"\bparty leader\b",
    r"\bopposition leader\b", r"\bchief executive\b", r"\bceo\b",
    r"\bfederal\b", r"\bsupreme court\b",
    r"\bespionage\b", r"\bforeign interference\b", r"\bcorruption\b",
    r"\bbribery\b", r"\bprocurement\b", r"\bcampaign finance\b",
    # Corporate-institutional: `charged with`/`pleads guilty` in _LOCAL_PATTERNS
    # gated the Nissan chairman, Nippon Steel trade-secrets, SNC-Lavalin and
    # Loblaws price-fixing stories — all national business news.
    r"\bchairman\b", r"\bexecutive\b", r"\binsider trading\b",
    r"\bprice-fixing\b", r"\bantitrust\b", r"\btrade secrets\b",
    r"\bshareholder", r"\bregulator", r"\bmoney laundering\b", r"\bfraud\b",
    # Mass casualty: `\bfestival\b` still gates ordinary local festivals, but a
    # crowd crush that kills 11 is national news wherever it happened.
    r"\bkills\b", r"\bkilled\b", r"\binjured\b", r"\bcasualties\b",
    r"\bcrush\b", r"\bdisaster\b", r"\bevacuat",
    # Terrorism and attacks on places of worship: national by nature, but the
    # long sentences they carry trip `\bsentenced to \d+ years?\b`.
    r"\bterror", r"\bmassacre\b", r"\bmass shooting\b", r"\bhate crime\b",
    r"\bmosque\b", r"\bsynagogue\b",
)
_NATIONAL_RE = re.compile("|".join(_NATIONAL_PATTERNS), re.IGNORECASE)

# Salience lift: language that marks policy, security and macro consequence.
# Matched as plain substrings against title+summary — kept exactly as-is, since
# every term here is long enough for that to be safe and rewriting them would
# churn today's output for no gain.
_SALIENT_TERMS = (
    "sanction", "tariff", "treaty", "ceasefire", "invasion", "missile",
    "strike", "election", "parliament", "cabinet", "prime minister",
    "president", "central bank", "rate decision", "inflation", "gdp",
    "budget", "deficit", "regulation", "antitrust", "indictment", "ruling",
    "supreme court", "defence", "defense", "nuclear", "energy", "oil",
    "trade deal", "export control", "immigration", "coup", "protest",
)

# _SALIENT_TERMS above is policy/macro vocabulary and nothing else. On
# 2026-08-15 that blinded the scorer to two whole categories:
#
#   Disaster. The Chiba floods — 8 dead, 351mm of rain (a record since 1966),
#   7,359 people in shelters, the first JMA Level 5 warning ever issued for the
#   prefecture — scored salient=[] on all seven items and missed the japan desk
#   by 0.1. The Indonesia earthquake that DID get picked scored only by
#   accident, on the substring "strike" inside "earthquake strikes".
#
#   Company and market. Nvidia's $500bn AI financing initiative scored
#   salient=[] too: no word for chip, financing, private credit or data center.
#
# So salience is now desk-aware: the shared list above, plus the vocabulary of
# the desk the item is being scored on. The disaster list is lifted from
# enrich._HARD_NEWS_TERMS, which has been dead code on the live path since
# run.py started passing `candidates=` — copied rather than imported, to keep
# the two modules independent.
#
# These are REGEXES, not substrings. Short tokens are catastrophic under the
# plain `in` test _SALIENT_TERMS uses: "ai" matches said, again, against and
# campaign. Each pattern is anchored with \b and left open at the end so it
# picks up plurals and inflections ("flood" -> floods, flooding) the way
# _NATIONAL_PATTERNS already does with \bevacuat and \bterror.
_DISASTER_PATTERNS = (
    r"\bearthquake", r"\bquake", r"\btsunami", r"\btyphoon", r"\bhurricane",
    r"\bcyclone", r"\bwildfire", r"\bflood", r"\blandslide", r"\bmudslide",
    r"\bvolcan", r"\berupt", r"\bstorm", r"\btorrential", r"\bdownpour",
    # The list had no word for rain, which is what the Chiba story WAS: the
    # japantimes filing matched on \bdead alone. \brain is safe with the
    # boundary — "train" and "terrain" do not match.
    r"\bheavy rain\b", r"\brainfall", r"\brecord rains?\b",
    r"\bheatwave", r"\bheat wave\b", r"\bdrought", r"\bdisaster",
    r"\bevacuat", r"\bshelters?\b", r"\bstate of emergency\b",
    r"\bkills?\b", r"\bkilled\b", r"\bdead\b", r"\bdeath", r"\bfatalit",
    r"\bcasualt", r"\bstranded\b", r"\brescue", r"\bmagnitude\b",
    r"\bfamine", r"\bepidemic", r"\boutbreak",
)

# The finance desk's own vocabulary: the language of a company, market or deal
# story, which the shared macro list has no word for. The Wall Street names are
# listed the way _NATIONAL_PATTERNS lists national institutions — "Goldman" in a
# headline is a fact about what kind of story it is.
_MARKET_PATTERNS = (
    r"\bchips?\b", r"\bchipmaker", r"\bsemiconductor", r"\bai\b",
    r"\bartificial intelligence\b", r"\bdata cent(?:er|re)",
    r"\bfinancing\b", r"\bprivate credit\b", r"\bprivate equity\b",
    r"\bhedge fund", r"\bventure capital\b", r"\bbuyback", r"\bdividend",
    r"\bmerger", r"\bacquisition", r"\btakeover", r"\bipo\b",
    r"\bbonds?\b", r"\byields?\b", r"\bdefault", r"\bbubble\b",
    r"\bdebt\b", r"\blender", r"\bvaluation", r"\bearnings\b",
    r"\bshares?\b", r"\bstakes?\b", r"\bwall street\b", r"\bnasdaq\b",
    r"\binvestors?\b", r"\bdeal\b", r"\bbillions?\b", r"\btrillions?\b",
    r"\bgoldman\b", r"\bblackrock\b", r"\bblackstone\b", r"\bapollo\b",
    r"\bkkr\b", r"\bbrookfield\b", r"\bmorgan stanley\b", r"\bjpmorgan\b",
    r"\bberkshire\b",
)

_DISASTER_RE = tuple(re.compile(p, re.IGNORECASE) for p in _DISASTER_PATTERNS)
_MARKET_RE = tuple(re.compile(p, re.IGNORECASE) for p in _MARKET_PATTERNS)

# Which vocabulary each desk reads, and how far its lift may run. Disaster and
# casualty language is hard news on every geographic desk; market language
# belongs to finance, which is where the re-route below puts company stories
# that arrived on a world-desk feed.
#
# The caps differ because the two vocabularies behave differently, and they can
# differ freely because desks are ranked independently — a score is only ever
# compared with other scores on the same desk.
#
# A disaster headline says ONE thing many ways: a flood that kills is
# "torrential", "flood", "killed", "dead" and "stranded", five words for one
# fact. Counting all five over-counts, so the disaster lift stops at two terms.
# Measured on the 2026-08-15 pool: at three terms the world desk's top ten went
# eight-tenths disaster (Etna three times, two wildfires, two quakes, an oil
# spill), which is not a world budget; at one term the day's 7.7 Indonesia
# earthquake fell out of the top ten altogether, tied on 5.10 with seven other
# guardian-world items and losing the per-feed cap on the url tie-break. Two
# terms puts the quake fourth.
#
# Market terms are closer to independent — financing, chips, data centres and
# private credit are four different things to know about a story — so the
# finance lift is allowed to run to four of them.
_DESK_VOCABULARY = {
    "world": (_DISASTER_RE, 0.8), "us": (_DISASTER_RE, 0.8),
    "finance": (_MARKET_RE, 1.6),
}

# The largest lift any desk vocabulary can add, whichever desk it is.
_SALIENCE_LIFT_CAP = max(cap for _, cap in _DESK_VOCABULARY.values())


def _desk_salience(item, desk: str) -> float:
    """The desk-vocabulary half of the salience lift. Never raises."""
    try:
        patterns, cap = (_DISASTER_RE, 0.8) if desk in local_desk_ids() else (
            _DESK_VOCABULARY.get(desk) or ((), 0.0))
        if not patterns:
            return 0.0
        text = f"{_str((item or {}).get('title'))} {_str((item or {}).get('summary'))}"
        hits = sum(1 for pattern in patterns if pattern.search(text))
        return min(hits * 0.4, cap)
    except wi_config.ConfigError:

        raise

    except Exception:
        return 0.0


# Content-based desk re-route. `desk` comes from the feed registry, not from the
# story, and that created a dead zone: `reuters-gnews` is desk="world", so
# Reuters' Nvidia AI-financing stories landed on the world desk — where
# codex_engine._finance_bundle (which narrows the finance pass to desk ==
# "finance") could not see them, and where prompts/world.md tells the world pass
# that company and market stories are the finance pass's job. Neither pass could
# write them. Symmetrically, only ~52% of the japan desk mentioned Japan:
# `nikkei-asia` is a pan-Asian business wire that contributed 50 of 120 items
# (Indonesia's budget, Malaysia's GDP) and crowded out the Chiba floods.
#
# The item is MOVED rather than tagged, so _finance_bundle's existing filter
# picks it up with no change in codex_engine.
#
# Deliberately conservative — a wrong move is worse than no move — so the
# signature is read from the TITLE only. Summaries are long and would trip the
# weak terms on any story that happens to mention a bank.
_MARKET_STRONG_PATTERNS = (
    r"\bprivate credit\b", r"\bprivate equity\b", r"\bhedge fund",
    r"\bventure capital\b", r"\bshort seller\b", r"\basset manager",
    r"\binvestment bank", r"\bunderwrit", r"\bipo\b", r"\bbuyback",
    r"\bdividend", r"\bearnings\b", r"\bquarterly (?:results|profit|earnings)\b",
    r"\bshare price\b", r"\bstock market\b", r"\bwall street\b",
    r"\bnasdaq\b", r"\bs&p 500\b", r"\bbond market\b", r"\bmarket cap",
    r"\bhostile bid\b", r"\bprofit warning\b",
)
_MARKET_WEAK_PATTERNS = (
    r"\bfinancing\b", r"\binvestors?\b", r"\bstakes?\b", r"\bdeal\b",
    r"\bfunds?\b", r"\bvaluation", r"\bdebt\b", r"\blender",
    r"\bchips?\b", r"\bsemiconductor", r"\bdata cent(?:er|re)",
    r"\brevenue", r"\bprofits?\b", r"\bacquisition", r"\bshares\b",
    r"\bbillion\b", r"\btrillion\b", r"\bshareholder",
)

# Never re-route a story that is primarily war, diplomacy, politics or disaster,
# whatever money words it also carries: "Canada announces sanctions on five
# Iranian officials over Strait of Hormuz blockade" is not a markets story.
_ROUTE_VETO_PATTERNS = (
    r"\bwar\b", r"\btroops\b", r"\bmissile", r"\bstrikes? on\b", r"\bairstrike",
    r"\bceasefire", r"\binvasion", r"\bsanction", r"\bblockade",
    r"\belection", r"\bballot", r"\bparliament", r"\bsupreme court\b",
    r"\bkilled\b", r"\bkills\b", r"\bdead\b", r"\bearthquake", r"\bflood",
    r"\bwildfire", r"\btsunami", r"\bhurricane", r"\bprotest",
    # Weapons and violent crime. `buyback` is a STRONG market term, so one match
    # moved "Australian state to launch gun buyback after Bondi Beach attack"
    # onto the finance desk from two outlets at once on 2026-08-16 — and since
    # those two enriched where the day's real finance stories could not, they
    # became the only Brief-eligible finance candidates and the finance brief
    # shipped empty. A buyback of weapons is a public-safety story.
    r"\bgun\b", r"\bguns\b", r"\bfirearm", r"\bshooting\b", r"\bstabbing\b",
    r"\bknife\b", r"\bhostage",
)

_MARKET_STRONG_RE = re.compile("|".join(_MARKET_STRONG_PATTERNS), re.IGNORECASE)
_MARKET_WEAK_RE = tuple(re.compile(p, re.IGNORECASE) for p in _MARKET_WEAK_PATTERNS)
_ROUTE_VETO_RE = re.compile("|".join(_ROUTE_VETO_PATTERNS), re.IGNORECASE)


def _route_desk(item, *, security=True) -> str:
    """The desk an item's CONTENT belongs to. Never raises; unknown -> world."""
    try:
        desk = _str((item or {}).get("desk"))
        desk = desk if desk in desk_ids() else "world"
        title = _str((item or {}).get("title"))
        if not title:
            return desk

        # National mandates win in declaration order; finance is never overridden.
        if desk != "finance":
            for national in wi_config.get().national_desks:
                signature = national["_signature_re"]
                if signature.search(title) or signature.search(_str((item or {}).get("summary"))):
                    return national["id"]

        if security and coverage_policy.route_security(item) and "world" in desk_ids():
            return "world"

        if ("finance" in desk_ids() and desk != "finance"
                and not _ROUTE_VETO_RE.search(title)):
            strong = bool(_MARKET_STRONG_RE.search(title))
            weak = sum(1 for pattern in _MARKET_WEAK_RE if pattern.search(title))
            if strong or weak >= 2:
                return "finance"

        # A japan-desk item with no Japan in it is pan-Asian wire copy taking a
        # slot the desk exists to give to Japan.
        if desk in local_desk_ids():
            return "world"
        return desk
    except wi_config.ConfigError:

        raise

    except Exception:
        return "world"


# Corroboration saturates. Until the story key was fixed the count was 1 for 94%
# of the pool, so `min(count, 5) * 0.5` was a flat +0.5 and its shape had never
# actually been exercised. Live, it handed +2.5 to whatever the herd covered and
# pushed the finance desk's cutoff from 3.5 to 4.2 — filling it with the day's
# wire consensus (a guilty plea, an obituary) and burying the desk's own beat.
# Two independent outlets is the confirmation the signal exists to give; beyond
# that it only measures herd size, which every prompt in prompts/ explicitly
# tells the passes NOT to rank by ("Rank by real-world impact, not coverage
# volume"; "Buried signals: LOW-coverage but HIGH-impact items ... your
# highest-value finds").
_CORROBORATION_CAP = 2

_GOV_SOURCES = ("federal_register", "fedreg")

# Feeds whose ARTICLES cannot be extracted, however good their headlines. A
# candidate without `content` is barred from the Brief by the prompt's own
# eligibility rule, so one of these occupies a desk slot it can never use —
# which is why unenrichable feeds were rejected outright when the registry grew.
# These three are incumbents that pre-date that policy, and all three are tier
# 1, so they start 1.5 points ahead of the tier-2 feeds that do serve bodies.
# Measured 2026-08-16: ft.com yields 652-char stubs (the gate is 800), Reuters
# via Google News is a DataDome 401 on 99 of 100 fetches, and
# feeds.content.dowjones.io 401s too. That morning they held six of the finance
# desk's ten slots and the finance brief came back EMPTY.
#
# Penalised rather than dropped: their headlines remain real signal for Signal,
# Threads and buried, which have no `content` requirement. The penalty must be
# larger than the 1.5-point tier gap; otherwise a tier-1 source known to yield
# zero bodies still outranks a tier-2 source that can support publishable copy.
# Production enrichment telemetry is the authority here, not whether the RSS
# feed itself works. Across the five briefs ending 2026-09-21, Globe and Mail
# was 0/17, National Post 0/11, Financial Post 0/4 and Nikkei Asia 0/12 for
# article bodies. Their headlines remain useful for signals/threads, but they
# must not outrank sources that can support a 100-200 word story.
_NO_BODY_SOURCES = frozenset((
    "ft-home", "reuters-gnews", "wsj-world",
    "globemail-canada", "nationalpost", "financialpost", "nikkei-asia",
))
_NO_BODY_PENALTY = 2.0

# Stop one outlet owning a desk (npr-politics held 8/10 on `us`,
# guardian-business held 8/10 on `finance`).
PER_FEED_CAP = 3

# Stop one STORY owning a desk. Repairing corroboration made the scorer reward
# stories many outlets ran, which is right — and which mechanically stacks the
# same event at the top of a desk. On 2026-08-15 the Chiba floods took 4 of
# japan's 10 slots and the Indonesia earthquake landed at world #4 and #8,
# leaving the japan desk about five distinct stories to choose four from.
# _dedupe cannot help: those are genuinely different articles (best
# cross-outlet title Jaccard 0.27, well under its 0.8), which is why this is a
# variety cap at selection time rather than a deduplication threshold.
SAME_STORY_CAP = 2

# How far the cap will top back up when a desk is genuinely thin. Deliberately
# NOT the per-desk target: topping up to 10 restored the very duplicates the cap
# had just removed, because a desk holding ten items and four copies of one
# story keeps eight and 8 < 10. Eight distinct stories is a better candidate
# list than ten containing four of the same, so the floor is about having
# anything to work with at all — the sections ask for 3-4 stories, and roughly a
# third of candidates fail to enrich, which an unenriched one cannot lead.
SAME_STORY_FLOOR = 4


def article_hash(item: dict) -> str:
    """Stable id for cross-day novelty: normalized title + URL host.

    Host (not full URL) so a syndicated repost of the same story collapses onto
    the original. Never raises.
    """
    try:
        tokens = " ".join(sorted(_norm_title(item.get("title"))))
        host = urlparse(_str(item.get("url"))).netloc.lower()
        return hashlib.sha1(f"{tokens}|{host}".encode("utf-8")).hexdigest()
    except wi_config.ConfigError:

        raise

    except Exception:
        return ""


def _drop_local(news: list) -> list:
    """Apply the national-significance gate to the japan and canada desks."""
    kept = []
    for item in _as_list(news):
        if not isinstance(item, dict):
            continue
        title = _str(item.get("title"))
        if (_str(item.get("desk")) in local_desk_ids() and _LOCAL_RE.search(title)
                and not _NATIONAL_RE.search(title)):
            continue
        kept.append(item)
    return kept


def _coverage_map(bundle: dict) -> dict:
    """{normalized-title-token-set: cross-source count} from bundle['coverage']."""
    out = {}
    for row in _as_list((bundle or {}).get("coverage")):
        if not isinstance(row, dict):
            continue
        try:
            out[_norm_title(row.get("story"))] = int(row.get("count") or 0)
        except wi_config.ConfigError:

            raise

        except Exception:
            continue
    return out


def _corroboration(item: dict, coverage: dict) -> int:
    """How many sources carried this story, per bundle['coverage']."""
    tokens = _norm_title(item.get("title"))
    if not tokens:
        return 1
    best = coverage.get(tokens)
    if best:
        return best
    for key, count in coverage.items():
        if _similar(tokens, key, threshold=0.6):
            return count
    return 1


def _score(item: dict, coverage: dict, seen: set, desk: str = "") -> float:
    """Rank score, higher is better. Deterministic; never raises.

    `desk` selects which salience vocabulary applies; omitted, the item's own
    desk is used, so the existing callers keep working unchanged.
    """
    try:
        tier = int(item.get("tier") or 2)
    except (TypeError, ValueError):
        tier = 2
    score = {1: 3.0, 2: 1.5, 3: 0.3}.get(tier, 1.0)

    score += min(_corroboration(item, coverage), _CORROBORATION_CAP) * 0.5

    text = f"{_str(item.get('title'))} {_str(item.get('summary'))}".lower()
    score += sum(0.4 for term in _SALIENT_TERMS if term in text)
    score += _desk_salience(item, desk or _str(item.get("desk")))

    if _str(item.get("raw_body")).strip():
        score += 0.5          # feed already gave us body text — free enrichment
    if item.get("gnews_unresolved"):
        score -= 1.0          # cannot be enriched; usable only as a source chip
    elif _str(item.get("source")) in _NO_BODY_SOURCES:
        score -= _NO_BODY_PENALTY   # same reason, measured per source not per item
                                    # (elif: never charge one item both penalties)
    if is_demoted(item):
        score -= 1.5         # live-coverage/digest format: loses to a concrete
                             # article, but still eligible when nothing better exists
    if len(_str(item.get("summary"))) > 200:
        score += 0.3

    if seen and article_hash(item) in seen:
        score -= 4.0          # already used within the 14-day window

    return round(score, 4)


def _cap_gov(items: list, limit: int = 2) -> list:
    """Allow at most `limit` Federal Register items through, keeping order."""
    kept, gov = [], 0
    for item in _as_list(items):
        if not isinstance(item, dict):
            continue
        if _str(item.get("source")).lower() in _GOV_SOURCES:
            if gov >= limit:
                continue
            gov += 1
        kept.append(item)
    return kept


def _cap_per_feed(items: list, limit: int = PER_FEED_CAP, target: int = 10) -> list:
    """Stop one outlet owning a desk. Keep at most `limit` items per `source`,
    preserving score order; if that leaves fewer than `target`, top up with the
    highest-scoring remaining items regardless of source. Never raises."""
    kept, overflow, counts = [], [], {}
    try:
        for item in _as_list(items):
            if not isinstance(item, dict):
                continue
            source = _str(item.get("source"))
            count = counts.get(source, 0)
            if count < limit:
                kept.append(item)
                counts[source] = count + 1
            else:
                overflow.append(item)
        if len(kept) < target:
            kept.extend(overflow[:target - len(kept)])
    except wi_config.ConfigError:

        raise

    except Exception:
        return _as_list(items)
    return kept


def _cap_same_story(items: list, same_story, limit: int = SAME_STORY_CAP,
                    floor: int = SAME_STORY_FLOOR) -> list:
    """Keep at most `limit` articles about any one story, preserving score
    order. Unlike `_cap_per_feed` this tops up only to `floor`, not to the
    desk's target — see SAME_STORY_FLOOR for why filling every slot defeats
    the cap outright.

    `same_story(a, b)` comes from `fetch_sources.story_matcher`. Never raises;
    a falsy matcher leaves `items` untouched."""
    if not callable(same_story):
        return _as_list(items)
    kept, overflow = [], []
    try:
        for item in _as_list(items):
            if not isinstance(item, dict):
                continue
            if sum(1 for k in kept if same_story(item, k)) < limit:
                kept.append(item)
            else:
                overflow.append(item)
        if len(kept) < floor:
            kept.extend(overflow[:floor - len(kept)])
    except wi_config.ConfigError:

        raise

    except Exception:
        return _as_list(items)
    return kept


def select_candidates(bundle: dict, seen: set, *, per_desk: int | None = None) -> dict:
    """Return {desk: [enrichment candidates]} for the five desks.

    Pure, deterministic, offline. Never raises — a malformed bundle yields five
    empty lists rather than an exception.
    """
    if per_desk is None:
        per_desk = wi_config.get().tuning.get("per_desk_slots", 10)
    profile = coverage_policy.resolve_profile((bundle or {}).get("ranking_profile", wi_config.get().ranking))
    result = {desk: [] for desk in desk_ids()}
    try:
        news = _as_list((bundle or {}).get("news"))
        seen = seen if isinstance(seen, set) else set()
        coverage = _coverage_map(bundle)

        # Route before first-wins dedupe. Otherwise an earlier world-feed copy
        # can erase the later local-feed copy and strand a country story outside
        # the desk whose capacity exists to cover it.
        for item in news:
            if isinstance(item, dict):
                item["desk"] = _route_desk(item, security=profile["major_events"]["enabled"])
        fresh_pool = _drop_stale(news, _str((bundle or {}).get("date")),
                                 as_of=_str((bundle or {}).get("as_of")))
        clean_pool = _drop_noise(fresh_pool)
        evidence_pool = _drop_local(clean_pool)
        pool = _dedupe(news)
        pool = _drop_stale(pool, _str((bundle or {}).get("date")),
                           as_of=_str((bundle or {}).get("as_of")))
        pool = _drop_noise(pool)
        pool = _drop_local(pool)

        by_desk = {desk: [] for desk in desk_ids()}
        for item in pool:
            # Content, not the feed registry, decides the desk (see _route_desk).
            # `desk` is rewritten on the item itself, not on a copy: the item is
            # the same object as bundle['news'][i], which is what
            # codex_engine._finance_bundle filters and what enrich_news writes
            # `content` back into. A copy here would strip both.
            desk = _route_desk(item, security=profile["major_events"]["enabled"])
            if _str(item.get("desk")) != desk:
                item["desk"] = desk
            by_desk.setdefault(desk, []).append(item)

        # Built once over the WHOLE filtered pool, not per desk: the rare-anchor
        # rule reads document frequencies, and a single desk is too small a
        # sample to tell a story signature from ordinary vocabulary. Imported
        # lazily so this module stays import-safe and dependency-free on its own
        # (the same reason enrich defers bs4 and fetch_sources defers yfinance);
        # if it is unavailable the desks simply go uncapped.
        # min_ratio=0 deliberately: four outlets on one flood share "chiba" and
        # "rain" but little else, so at the corroboration rule's 0.3 share-of-
        # headline they form two disjoint pairs rather than one story, and a cap
        # counting matches would keep all four. A shared RARE token still has to
        # be there, which is what keeps the ballroom lawsuit, the press
        # secretary's resignation and a staff shuffle three separate stories.
        try:
            from worldintel.core import fetch_sources  # noqa: PLC0415
            same_story = fetch_sources.story_matcher(pool, min_ratio=0)
        except wi_config.ConfigError:

            raise

        except Exception:  # noqa: BLE001
            same_story = None

        for desk in desk_ids():
            items = by_desk.get(desk) or []
            # Sort by score desc, then url for a stable tie-break.
            items.sort(key=lambda i, d=desk: (-(_score(i, coverage, seen, d) + coverage_policy.score_bonus(i, profile)),
                                              _str(i.get("url"))))
            items = _cap_gov(items)
            # Before the per-feed cap: that one tops up from its own overflow,
            # which by then no longer holds a third copy of anything.
            items = _cap_same_story(items, same_story)
            items = _cap_per_feed(items, limit=wi_config.get().tuning.get("per_outlet_cap", 3), target=per_desk)
            result[desk] = items[:per_desk]
        score_fn = lambda i: (_score(i, coverage, seen, _str(i.get("desk")))
                              + coverage_policy.score_bonus(i, profile))
        protection_pool = [i for i in evidence_pool if article_hash(i) not in seen]
        eligibility_audit = []
        result, events = coverage_policy.protect(
            protection_pool, result, profile, same_story, score_fn, audit=eligibility_audit)
        bundle["protected_events"] = events
        bundle["coverage_eligibility_audit"] = eligibility_audit
        bundle["ranking_profile"] = profile
        bundle["ranking_profile_hash"] = coverage_policy.profile_hash(profile)
        selected_urls = {i.get("url") for items in result.values() for i in items}
        eligible_urls = {i.get("url") for i in evidence_pool}
        candidate_urls = {i.get("url") for i in pool}
        fresh_urls = {i.get("url") for i in fresh_pool}
        clean_urls = {i.get("url") for i in clean_pool}
        bundle["selection_audit"] = [
            {"url": i.get("url"), "source": i.get("source"), "desk": i.get("desk"),
             "title": i.get("title", ""), "recognition_reason": coverage_policy.recognition_reason(i),
             "score": score_fn(i), "outcome": "selected" if i.get("url") in selected_urls
             else "outside_shortlist" if i.get("url") in candidate_urls
             else "deduplicated" if i.get("url") in eligible_urls
             else "local_significance" if i.get("url") in clean_urls
             else "noise" if i.get("url") in fresh_urls else "stale",
             "seen": article_hash(i) in seen}
            for i in news if isinstance(i, dict)]

    except wi_config.ConfigError:

        raise

    except Exception:
        return {desk: [] for desk in desk_ids()}
    return result
