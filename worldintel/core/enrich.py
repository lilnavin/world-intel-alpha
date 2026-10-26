"""world-intel Group B — deterministic article-enrichment layer.

Implements `enrich_news(bundle, *, session, max_items, per_item_chars, timeout)`,
run between `fetch_all` and Codex so the Brief is written from ARTICLE SUBSTANCE
rather than headlines.

ABSOLUTE RULE (same as Module A): nothing here may raise out of the module. Every
network touch is wrapped; on any failure the affected item is simply left without
`content` (it falls back to its `summary` downstream). A single health entry is
recorded at `bundle["source_health"]["enrich"]`.

Pure helpers (`_extract_text`, `_select_candidates`) are kept separate from the
network-touching `_fetch_html` so they can be unit-tested without the network.

Standard library + `requests` + `bs4`. `bs4` is imported lazily inside
`_extract_text` and MUST use the builtin parser (``BeautifulSoup(html,
"html.parser")``) — lxml is NOT installed. If bs4 is somehow missing, extraction
degrades to "" rather than raising.
"""

from __future__ import annotations

from worldintel import config as wi_config
import re
import concurrent.futures
import time

import requests

# Deliberately the same browser-shaped string `feeds.py` sends, and for the same
# reason its `_fetch_one` documents: several publishers refuse a bare tool UA.
# Measured 2026-08-15 on the same URLs under each UA — japantoday HTTP 403 (383
# bytes) -> HTTP 200 (3,473 extracted chars); japannews.yomiuri.co.jp a ZERO-BYTE
# HTTP 202 -> HTTP 200 (2,187 chars). Yomiuri is a REGISTERED feed, so it was
# enriching at 0 chars every run, and the Brief's `content` gate meant it could
# never lead a story. Not copied from feeds by import: these are two independent
# clients that happen to need the same header, and coupling them would make one
# module's politeness policy silently govern the other's.
def _browser_ua() -> str:
    """Browser shape is required by publishers serving article bodies."""
    return wi_config.get().browser_user_agent()

# A real article body. Below this, the "content" is a teaser or an
# extraction failure: NPR's RSS-linked pages yield 155-230 chars, and the
# analysis prompt treats any non-empty `content` as a full article, so a
# teaser silently becomes Brief-eligible padding.
MIN_CONTENT_CHARS = 800

# Obvious noise categories to exclude from enrichment (case-insensitive substring
# match against the item's category). "sport" also covers "sports".
_NOISE_CATEGORIES = (
    "sport", "lifestyle", "culture", "entertainment", "travel", "food",
    "advice", "opinion", "press release",
)

_LOW_CONSEQUENCE_TERMS = (
    "celebrity", "recipe", "horoscope", "travel", "holiday", "restaurant",
    "dating", "shopping", "deal", "discount", "lottery", "sports", "football",
    "baseball", "soccer", "tennis", "golf", "crime blotter", "arrested",
    "missing dog", "press release", "sponsored", "pr newswire", "globenewswire",
)

_HARD_NEWS_TERMS = (
    "earthquake", "quake", "tsunami", "wildfire", "flood", "storm", "heatwave",
    "disaster", "war", "missile", "drone", "strike", "ceasefire", "invasion",
    "gaza", "ukraine", "iran", "russia", "israel", "hamas", "hezbollah",
    "election", "vote", "parliament", "president", "prime minister", "cabinet",
    "government", "coup", "sanction", "tariff", "diplomatic", "embassy",
    "shipping", "red sea", "strait", "suez", "oil", "gas", "market",
    "central bank", "rate decision", "inflation",
)

_PREFERRED_SOURCE_MARKERS = (
    "bbc", "nytimes", "new york times", "al jazeera", "aljazeera",
    "reuters", "apnews", "ap.org", "associated press", "bloomberg",
    "financial times", "ft.com", "gdelt", "newsdata", "nikkei", "kyodo",
)

_GUARDIAN_MARKERS = ("guardian", "theguardian.com", "the guardian")

_GLOBAL_REGIONS = (
    "world", "japan", "canada", "europe", "middle east", "gaza", "ukraine",
    "iran", "peru", "asia", "africa", "latin america",
)

# Configured national desks receive representation when candidates exist.
def reserved_desk_ids() -> tuple:
    """Reserve by feed desk identity, which remains valid for custom desks."""
    return tuple(wi_config.get().local_desk_ids)


def global_regions() -> tuple:
    extra = [value.lower() for desk in wi_config.get().national_desks
             for value in (desk["id"], desk["label"])]
    return tuple(dict.fromkeys([*_GLOBAL_REGIONS, *extra]))


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested without the network)
# --------------------------------------------------------------------------- #
_BOILERPLATE = re.compile(
    r"(?:\s*(?:to play this video\s+please enable javascript(?:\s+and upgrade your browser)?|"
    r"please enable javascript(?:\s+and upgrade your browser|\s+to play this video)?|"
    r"accept (?:all )?cookies to continue|manage (?:your )?cookie preferences)"
    r"[.!?]?\s*)+",
    re.I,
)


_LEADING_CONTROLS = re.compile(
    r"\s*(?:advertisement|advertising|cookie settings|privacy preferences|"
    r"please disable (?:the )?ad block(?:er|ing feature)|"
    r"accept (?:all )?cookies|reject (?:all )?cookies|manage consent preferences)\s*[.!?]?\s*", re.I)


_CONSENT_PANEL = re.compile(
    r"\s*(?:to (?:use|access) this site,? please disable (?:the )?ad block(?:er|ing feature) and reload the page|"
    r"this (?:website|site) uses cookies to (?:collect|store) [^.!?]+|"
    r"by clicking [^.!?]+,? you (?:will allow|allow|consent to) [^.!?]*cookies|"
    r"users accessing this site [^.!?]*(?:unable to|cannot) view this site without (?:your )?consent|"
    r"we apologize for (?:any|the) inconvenience(?: caused)?)\s*[.!?]?\s*", re.I)


def clean_reporting_text(text: str) -> str:
    """Remove recognized player/cookie text and programme metadata, not facts."""
    # Match whole structural units: control sentences or repeated unpunctuated
    # controls. Embedded/quoted descriptions remain reporting, not UI controls.
    units = re.split(r'(?<=[.!?])\s+|[\r\n]+', text or '')
    # An unmarked panel needs both a direct access instruction and its cookie
    # disclosure; either sentence alone can be genuine quoted reporting.
    control_prefix = bool(len(units) > 1
        and re.match(r'^\s*to (?:use|access) this site\b', units[0], re.I)
        and _CONSENT_PANEL.fullmatch(units[0])
        and re.match(r'^\s*this (?:website|site) uses cookies\b', units[1], re.I)
        and _CONSENT_PANEL.fullmatch(units[1]))
    while units and (_LEADING_CONTROLS.fullmatch(units[0]) or
                     (control_prefix and _CONSENT_PANEL.fullmatch(units[0]))):
        control_prefix = True
        units.pop(0)
    text = ' '.join(unit for unit in units if not _BOILERPLATE.fullmatch(unit))
    if re.match(r'^\s*(?:in this programme|in this program|coming up|programme summary)\s*:', text, re.I):
        return ''
    return re.sub(r'\s+', ' ', text).strip()


def substantive_content(text: str) -> bool:
    text = clean_reporting_text(text)
    return len(text) >= MIN_CONTENT_CHARS and len(text.split()) >= 100


def _extract_text(html: str) -> str:
    """Extract main article text from raw HTML.

    Parses with bs4's builtin ``html.parser`` (lxml is not installed), drops
    non-content tags (script/style/nav/header/footer/aside), joins the visible
    ``<p>`` text and collapses whitespace. Returns "" if bs4 is missing or the
    parse fails — never raises.
    """
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
    except wi_config.ConfigError:
        raise
    except Exception:
        # bs4 absent — degrade gracefully rather than break enrichment.
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        parts = []
        for p in soup.find_all("p"):
            text = clean_reporting_text(p.get_text(" ", strip=True))
            if text and text.casefold() not in ('transcript', 'share', 'sign in', 'subscribe', 'menu', 'related topics'):
                parts.append(text)
        joined = " ".join(parts)
    except wi_config.ConfigError:
        raise
    except Exception:
        return ""
    return clean_reporting_text(joined)


def _truncate(text: str, limit: int) -> str:
    """Truncate to at most `limit` chars, cutting on a word boundary."""
    if not text or limit <= 0 or len(text) <= limit:
        return text or ""
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip()


def _item_key(item: dict) -> str:
    """Dedupe signature for a news item: url, falling back to title."""
    return (item.get("url") or "").strip() or (item.get("title") or "").strip()


def _is_noise_category(category: str) -> bool:
    cat = (category or "").lower()
    return any(word in cat for word in _NOISE_CATEGORIES)


def _haystack(item: dict) -> str:
    return " ".join(
        str(item.get(k) or "")
        for k in ("title", "summary", "source", "url", "category", "region")
    ).lower()


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _is_guardian(item: dict) -> bool:
    return _contains_any(_haystack(item), _GUARDIAN_MARKERS)


def _is_preferred_source(item: dict) -> bool:
    return _contains_any(_haystack(item), _PREFERRED_SOURCE_MARKERS)


def _is_global_region(item: dict) -> bool:
    region = (item.get("region") or "").strip().lower()
    return not region or region in global_regions()


def _hard_news_score(item: dict) -> int:
    text = _haystack(item)
    score = 0
    if _contains_any(text, _HARD_NEWS_TERMS):
        score += 2
    if _is_global_region(item):
        score += 1
    if _is_preferred_source(item):
        score += 1
    if _contains_any(text, _LOW_CONSEQUENCE_TERMS):
        score -= 3
    return score


def _is_hard_news_candidate(item: dict) -> bool:
    return _hard_news_score(item) > 0


def _rank_key(item: dict):
    """Sort key (ascending = better): tier first (1 best), then prefer high-stakes
    global hard news and items already carrying `raw_body`; longer summaries are a
    light quality signal."""
    try:
        tier = int(item.get("tier"))
    except (TypeError, ValueError):
        tier = 2
    has_body = 0 if (item.get("raw_body") or "").strip() else 1
    summary_len = len(item.get("summary") or "")
    return (tier, -_hard_news_score(item), has_body, -summary_len)


def _select_candidates(news: list, max_items: int) -> list:
    """Pick up to `max_items` enrichment candidates from `news` (deterministic).

    Excludes tier-3 items and obvious noise categories, ranks the rest by
    `_rank_key`, then RESERVES one item per national desk (if present
    in the pool) ahead of filling the remaining slots by rank. Deduped by url
    (fallback title).
    """
    pool = [
        item for item in (news or [])
        if item.get("tier") != 3 and not _is_noise_category(item.get("category"))
    ]
    pool.sort(key=_rank_key)

    selected: list = []
    seen: set = set()

    def _add(item: dict) -> None:
        key = _item_key(item)
        if not key or key in seen or len(selected) >= max_items:
            return
        seen.add(key)
        selected.append(item)

    # Desk identity survives feed parsing even when its region label is generic.
    for desk in reserved_desk_ids():
        for item in pool:
            if (item.get("desk") or "").strip().lower() == desk:
                _add(item)
                break

    # Fill the remaining slots by rank.
    for item in pool:
        if len(selected) >= max_items:
            break
        _add(item)

    return selected


def _select_recall_candidates(news: list, current: list,
                              target_non_guardian: int) -> list:
    """Backfill with deduped non-Guardian tier-1/2 global hard-news candidates.

    The normal selector still determines the lead candidates. This guard adds
    only the highest-ranked non-Guardian hard-news items needed to give the
    downstream Guardian cap enough enriched alternatives.
    """
    if target_non_guardian <= 0:
        return []

    selected_keys = {_item_key(item) for item in current or []}
    current_non_guardian = sum(
        1 for item in current or []
        if _item_key(item) and not _is_guardian(item)
        and item.get("tier") in (1, 2)
        and not _is_noise_category(item.get("category"))
        and _is_hard_news_candidate(item)
    )
    needed = max(0, target_non_guardian - current_non_guardian)
    if needed <= 0:
        return []

    pool = []
    for item in news or []:
        key = _item_key(item)
        if not key or key in selected_keys:
            continue
        if _is_guardian(item) or item.get("tier") not in (1, 2):
            continue
        if _is_noise_category(item.get("category")):
            continue
        if not _is_hard_news_candidate(item):
            continue
        pool.append(item)
    pool.sort(key=_rank_key)
    return pool[:needed]


# --------------------------------------------------------------------------- #
# Network touch (isolated so tests can stub the session / this function)
# --------------------------------------------------------------------------- #
def _fetch_html(session, url: str, timeout: int, outcome=None) -> str:
    """GET `url` via the shared session with a descriptive UA; return HTML text or
    "" if the response errors or is not HTML. Never raises."""
    if outcome is None:
        outcome = {}
    if not session or not url:
        outcome["reason"] = "request_unavailable"
        return ""
    resp = None
    try:
        resp = session.get(
            url, headers={"User-Agent": _browser_ua()}, timeout=timeout
        )
        code = getattr(resp, "status_code", None)
        if type(code) is int and 100 <= code <= 599:
            outcome["http_status"] = code
        resp.raise_for_status()
    except wi_config.ConfigError:
        raise
    except Exception:
        outcome["reason"] = "http_error" if resp is not None else "request_error"
        return ""
    try:
        ctype = (resp.headers.get("Content-Type") or "").lower()
    except wi_config.ConfigError:
        raise
    except Exception:
        ctype = ""
    # Skip clearly non-HTML responses; tolerate a missing Content-Type header
    # (many servers omit it) by attempting a parse anyway.
    if ctype and "html" not in ctype:
        outcome["reason"] = "non_html_response"
        return ""
    try:
        return resp.text or ""
    except wi_config.ConfigError:
        raise
    except Exception:
        outcome["reason"] = "response_read_error"
        return ""


def _content_for(item: dict, session, timeout: int, outcome=None) -> str:
    """Resolve source text for one candidate: prefer `raw_body`, else fetch+extract.
    Returns "" on any failure — never raises."""
    raw = (item.get("raw_body") or "").strip()
    if raw:
        # Guardian bodyText may be HTML (has <p> tags) or plain text; extract when
        # possible, else fall back to the collapsed raw text.
        text = _extract_text(raw)
        if not text and not re.search(r'<[a-z][^>]*>', raw, re.I):
            text = re.sub(r"\s+", " ", raw).strip()
        return clean_reporting_text(text)
    html = _fetch_html(session, item.get("url") or "", timeout, outcome)
    return _extract_text(html)


# --------------------------------------------------------------------------- #
# Public entrypoint
# --------------------------------------------------------------------------- #
def enrich_news(bundle: dict, *, session=None, candidates=None,
                max_items: int | None = None, per_item_chars: int | None = None,
                timeout: int = 8, recall_non_guardian: int = 8,
                max_workers: int | None = None) -> None:
    """Mutate `bundle['news']` IN PLACE, giving candidates a `content` field of
    real article text (<= per_item_chars). Never raises. Import-safe.

    `candidates` is the {desk: [items]} map from `select_news.select_candidates`.
    When supplied, exactly those items are enriched and the module's own
    selection is skipped. When None, the legacy self-selection path runs
    unchanged (see CONTRACT.md ADDENDUM 2026-07-04 (B1)). Fetches run in a
    thread pool (`max_workers`); the result is identical to the sequential path
    because each worker only writes to its own item.
    """
    settings = wi_config.get().tuning.get("enrichment", {})
    max_items = settings.get("max_items", 8) if max_items is None else max_items
    per_item_chars = settings.get("max_chars", 2000) if per_item_chars is None else per_item_chars
    max_workers = settings.get("concurrency", 8) if max_workers is None else max_workers
    min_chars = max(MIN_CONTENT_CHARS, settings.get("min_chars", MIN_CONTENT_CHARS))
    try:
        news = bundle.get("news") or []
    except wi_config.ConfigError:
        raise
    except Exception:
        return

    legacy = candidates is None
    if not legacy:
        try:
            desk_map = {d: list(v or []) for d, v in (candidates or {}).items()}
        except wi_config.ConfigError:
            raise
        except Exception:
            desk_map = {}
        flat = [item for items in desk_map.values() for item in items]
    else:
        try:
            flat = _select_candidates(news, max_items)
            flat += _select_recall_candidates(news, flat, recall_non_guardian)
        except wi_config.ConfigError:
            raise
        except Exception:
            flat = []
        desk_map = {}

    # Only open a session if we actually need to fetch (some candidates lack raw_body).
    owns_session = False
    if session is None and any(
        not (c.get("raw_body") or "").strip() for c in flat
    ):
        try:
            session = requests.Session()
            session.headers.update({"User-Agent": _browser_ua()})
            owns_session = True
        except wi_config.ConfigError:
            raise
        except Exception:
            session = None

    def _fill(item: dict) -> None:
        outcome = {"status": "failed"}
        item["enrichment"] = outcome
        try:
            text = _content_for(item, session, timeout, outcome)
        except wi_config.ConfigError:
            raise
        except Exception:
            text = ""
            outcome["reason"] = "extraction_error"
        outcome["extracted_chars"] = len(text)
        item.pop("content", None)
        # Below MIN_CONTENT_CHARS the extracted text is a teaser or an
        # extraction failure, not a real article body — leave `content`
        # unset so the item falls back to its `summary` downstream, exactly
        # as a fetch failure does today.
        if len(text) >= min_chars and len(text.split()) >= 100:
            retained = _truncate(text, per_item_chars)
            if len(retained) >= min_chars and len(retained.split()) >= 100:
                item["content"] = retained
                outcome.update(status="enriched", reason="content_available")
            else:
                outcome["reason"] = "retained_content_below_threshold"
        elif "reason" not in outcome:
            outcome["reason"] = "below_content_threshold" if text else "extraction_empty"

    try:
        workers = max(1, min(int(max_workers), len(flat) or 1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_fill, flat))
    except wi_config.ConfigError:
        raise
    except Exception:
        for item in flat:
            _fill(item)

    if owns_session:
        try:
            session.close()
        except wi_config.ConfigError:
            raise
        except Exception:
            pass

    # Keep the bundle lean for the Codex prompt: drop raw_body from every item.
    try:
        for item in news:
            item.pop("raw_body", None)
    except wi_config.ConfigError:
        raise
    except Exception:
        pass

    enriched = sum(1 for item in flat if (item.get("content") or "").strip())
    total = len(flat)
    try:
        if legacy:
            # Unchanged legacy note shape — downstream/tests key off this exact
            # format when candidates is not supplied.
            note = f"{enriched}/{total} enriched"
        else:
            parts = []
            for desk in sorted(desk_map):
                items = desk_map[desk]
                got = sum(1 for i in items if (i.get("content") or "").strip())
                parts.append(f"{desk} {got}/{len(items)}")
            note = f"{enriched}/{total} enriched ({', '.join(parts)})"
        bundle.setdefault("source_health", {})["enrich"] = {
            "ok": enriched >= 1,
            "note": note,
        }
    except wi_config.ConfigError:
        raise
    except Exception:
        pass

def enrich_protected_alternates(pool, selected, events, *, session=None, timeout=8, max_events=None,
                                deadline=None, clock=time.monotonic):
    """Replace failed protected slots using only already validated event members.

    At most two alternatives per event and four overall are fetched. Caller
    rebuilds its flattened bundle and selection audit from the selected map.
    """
    def substantive(item):
        return substantive_content(item.get('content') or '')

    remaining = 4
    from .coverage_policy import bounded_events

    for event in bounded_events(events, max_events):
        attempts = event.setdefault('enrichment_attempts', [])
        event_urls = set(event.get('urls', []))
        selected_urls = list(event.get('selected_urls', []))
        slots = [(desk, index, item) for desk, items in selected.items()
                 for index, item in enumerate(items)
                 if item.get('url') in selected_urls]
        if not slots:
            event['enrichment_stop_reason'] = 'no_selected_slot'
            continue
        used = 0
        for desk, index, failed in slots:
            if substantive(failed):
                continue
            attempts.append(dict(failed.get('enrichment') or {}, url=failed.get('url')))
            occupied = {item.get('url') for items in selected.values() for item in items}
            alternatives = sorted([item for item in pool
                                   if item.get('url') in event_urls
                                   and item.get('url') not in occupied
                                   and item.get('tier') in (1, 2)
                                   and item.get('desk', 'world') == desk],
                                  key=lambda item: (_is_guardian(item), _rank_key(item)))
            if not alternatives:
                event['enrichment_stop_reason'] = 'no_vetted_alternate'
            for alternate in alternatives:
                if used >= 2 or remaining <= 0:
                    event['enrichment_stop_reason'] = 'alternate_limit_reached'
                    break
                available = deadline - clock() if deadline is not None else timeout
                if available <= 0:
                    attempts.append({'status': 'failed', 'reason': 'enrichment_budget_exhausted'})
                    return
                used += 1
                remaining -= 1
                if not substantive(alternate):
                    enrich_news({'news': [alternate]}, session=session,
                                candidates={desk: [alternate]}, timeout=min(timeout, 8, available),
                                recall_non_guardian=0, max_workers=1)
                attempts.append(dict(alternate.get('enrichment') or {}, url=alternate.get('url')))
                if not substantive(alternate):
                    continue
                selected[desk][index] = alternate
                event['selected_urls'] = [alternate['url'] if url == failed['url'] else url
                                          for url in event['selected_urls']]
                break
