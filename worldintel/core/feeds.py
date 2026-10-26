"""world-intel — per-desk RSS fetch layer.

Replaces the NewsData/Guardian-API/MarketAux news path with targeted publisher
feeds routed to five desks (world, us, japan, canada, finance).

ABSOLUTE RULE (same as Module A): source failures degrade; ConfigError propagates.
Every feed is wrapped; a failure records `health["feed:<name>"]` and contributes
nothing. Pure parsing (`_parse_feed`) is separate from the network-touching
`fetch_feeds` so it unit-tests offline.

Standard library + `requests`. Feed URLs come from configuration. The example
registry preserves the probed roster; user-provided feeds need their own checks.
"""

from __future__ import annotations

from worldintel import config as wi_config
import concurrent.futures
import email.utils
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import requests

def _browser_ua() -> str:
    """Browser shape is required by publishers serving article bodies."""
    return wi_config.get().browser_user_agent()
_HTTP_TIMEOUT = 20
_MAX_WORKERS = 8

_ATOM = "{http://www.w3.org/2005/Atom}"
_RDF = "{http://purl.org/rss/1.0/}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

# desk: world | us | japan | canada | finance
# tier: 1 = wire/national broadsheet, 2 = other, 3 = aggregator/low-trust
def feed_registry():
    """Resolve the configured source table at call time."""
    return wi_config.get().feeds

# Desk -> the `region` label carried on the item (kept compatible with the
# existing region vocabulary used by enrich/metrics).
_DESK_REGION = {"world": "world", "us": "Us", "japan": "Japan",
                "canada": "Canada", "finance": "world"}


def _text(node) -> str:
    """Element text as a stripped string; '' for None/missing. Never raises."""
    if node is None:
        return ""
    try:
        return (node.text or "").strip()
    except wi_config.ConfigError:
        raise
    except Exception:
        return ""


def _first(item, *paths):
    """First non-None child matching any of the given tag paths."""
    for path in paths:
        try:
            found = item.find(path)
        except wi_config.ConfigError:
            raise
        except Exception:
            found = None
        if found is not None:
            return found
    return None


def _iso(raw: str) -> str:
    """Normalize an RSS/Atom date to an ISO-8601 string. '' if unparseable.

    Handles RFC-822 (`Sat, 02 Aug 2026 06:30:00 GMT`, RSS 2.0) and ISO-8601
    (`2026-08-02T04:15:00Z`, Atom and dc:date). Never raises.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
    except wi_config.ConfigError:
        raise
    except Exception:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except wi_config.ConfigError:
        raise
    except Exception:
        return ""


def _link_of(item) -> str:
    """Item URL across RSS (<link>text</link>), RDF and Atom (<link href=...>)."""
    node = _first(item, "link", f"{_RDF}link", f"{_ATOM}link")
    if node is None:
        return ""
    text = _text(node)
    if text:
        return text
    try:
        return (node.get("href") or "").strip()
    except wi_config.ConfigError:
        raise
    except Exception:
        return ""


def _parse_feed(xml_bytes: bytes, feed: dict) -> list:
    """Parse one feed's XML into normalized news items. Never raises.

    Supports RSS 2.0 (`<item>`), RDF/RSS 1.0 and Atom (`<entry>`). Items without
    both a title and a URL are dropped.
    """
    if not xml_bytes:
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except wi_config.ConfigError:
        raise
    except Exception:
        return []

    try:
        nodes = (root.findall(".//item") or root.findall(f".//{_RDF}item")
                 or root.findall(f".//{_ATOM}entry"))
    except wi_config.ConfigError:
        raise
    except Exception:
        return []

    desk = feed.get("desk") or "world"
    out = []
    for node in nodes:
        try:
            title = _text(_first(node, "title", f"{_RDF}title", f"{_ATOM}title"))
            url = _link_of(node)
            if not title or not url:
                continue
            summary = _text(_first(
                node, "description", f"{_RDF}description",
                f"{_ATOM}summary", f"{_ATOM}content"))
            published = _iso(_text(_first(
                node, "pubDate", f"{_DC}date",
                f"{_ATOM}published", f"{_ATOM}updated")))
            category = _text(_first(node, "category", f"{_ATOM}category")) or "general"
            body = _text(_first(node, f"{_CONTENT}encoded"))
            out.append({
                "title": title,
                "source": feed.get("name") or "",
                "url": url,
                "published": published,
                "summary": summary,
                "region": _DESK_REGION.get(desk, "world"),
                "category": category,
                "tier": feed.get("tier") or 2,
                "raw_body": body,
                "desk": desk,
            })
        except wi_config.ConfigError:
            raise
        except Exception:
            continue
    return out


def _short(exc: Exception) -> str:
    """One-line exception summary for a health note (mirrors fetch_sources)."""
    return f"{type(exc).__name__}: {exc}"[:120]


def _resolve_gnews(session, url: str, timeout: int = 10) -> str:
    """Resolve a news.google.com redirect to the publisher URL.

    Returns the resolved URL, or "" when resolution fails or the response never
    left the Google domain. Never raises.
    """
    if not session or "news.google.com" not in (url or ""):
        return ""
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True,
                           headers={"User-Agent": _browser_ua()})
        final = (getattr(resp, "url", "") or "").strip()
    except wi_config.ConfigError:
        raise
    except Exception:
        return ""
    if not final or "news.google.com" in final:
        return ""
    return final


def _fetch_one(session, feed: dict, health: dict) -> list:
    """Fetch and parse ONE feed. Records health; never raises."""
    name = feed.get("name") or "?"
    try:
        # UA goes on the request, not only on a session we created: fetch_all
        # passes in its own session, so a session-level default would never
        # reach the cron. japannews.yomiuri.co.jp serves a zero-byte HTTP 202
        # without this UA and a full HTTP 200 with it.
        resp = session.get(feed["url"], timeout=_HTTP_TIMEOUT,
                           headers={"User-Agent": _browser_ua()})
        resp.raise_for_status()
        items = _parse_feed(resp.content, feed)
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        health[f"feed:{name}"] = {"ok": False, "note": _short(exc)}
        return []

    # Google News proxies the publisher behind its own URL. We flag those items
    # instead of resolving them: Google no longer 302-redirects, it serves an
    # HTTP 200 JS-redirect page, so _resolve_gnews measured 0/100 successes over
    # 35.9s. The loop was serial inside one pool worker at 10s per item with no
    # overall cap — up to ~17 minutes blocking an unattended cron for nothing.
    # _resolve_gnews is kept, correct and unit-tested, for if Google reverts.
    #
    # 2026-08-15: the "so it is unresolvable" reading of that measurement is
    # WRONG, and the flag stays anyway. The splash page carries data-n-a-id/-ts/
    # -sg, and POSTing those to /_/DotsSplashUi/data/batchexecute (rpcid Fbv4je)
    # resolves 100/100 in ~7s batched. It buys nothing that matters: the resolved
    # publisher is Reuters, which returns HTTP 401 from DataDome on 99/100
    # article fetches — no UA, cookie, AMP path or internal API gets past it —
    # so 0/100 reach MIN_CONTENT_CHARS and no Reuters story can satisfy the
    # Brief's `content` gate however the URL is spelled. Resolving would only buy
    # tidier citation URLs and correct cross-feed dedupe.
    for item in items:
        if "news.google.com" in item["url"]:
            item["gnews_unresolved"] = True

    health[f"feed:{name}"] = {
        "ok": bool(items),
        "note": "" if items else "no items",
    }
    return items


def _session(session):
    """Return (session, owns_it) — create one with our UA when not supplied."""
    if session is not None:
        return session, False
    s = requests.Session()
    s.headers.update({"User-Agent": _browser_ua()})
    return s, True


def fetch_feeds(config: dict, raw_dir: str, health: dict, *,
                session=None, registry=None) -> list:
    """Fetch every registered feed concurrently; return merged news items.

    Never raises. Each feed records `health["feed:<name>"]`. `config` and
    `raw_dir` are accepted for signature symmetry with the other fetchers.
    """
    registry = registry if registry is not None else feed_registry()
    health = health if isinstance(health, dict) else {}
    sess, owns = _session(session)

    merged = []
    try:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=_MAX_WORKERS) as pool:
            futures = [pool.submit(_fetch_one, sess, f, health) for f in registry]
            for fut in futures:
                try:
                    merged.extend(fut.result())
                except wi_config.ConfigError:
                    raise
                except Exception:
                    continue
    except wi_config.ConfigError:
        raise
    except Exception:
        pass
    finally:
        if owns:
            try:
                sess.close()
            except wi_config.ConfigError:
                raise
            except Exception:
                pass
    return merged


def check_feeds(session=None, registry=None) -> list:
    """Probe every registered feed; return one status row each.

    Diagnostic helper for `python feeds.py --check`. Never raises.
    """
    registry = registry if registry is not None else feed_registry()
    sess, owns = _session(session)
    rows = []
    try:
        for feed in registry:
            health = {}
            items = _fetch_one(sess, feed, health)
            entry = health.get(f"feed:{feed.get('name')}") or {}
            rows.append({
                "feed": feed.get("name"),
                "desk": feed.get("desk"),
                "status": "OK" if entry.get("ok") else (entry.get("note") or "empty"),
                "count": len(items),
            })
    finally:
        if owns:
            try:
                sess.close()
            except wi_config.ConfigError:
                raise
            except Exception:
                pass
    return rows


if __name__ == "__main__":  # pragma: no cover - diagnostic entrypoint
    import sys
    if "--check" in sys.argv:
        for row in check_feeds():
            print(f"{row['feed']:24} {row['desk']:8} {row['status'][:40]:40} "
                  f"{row['count']}")
