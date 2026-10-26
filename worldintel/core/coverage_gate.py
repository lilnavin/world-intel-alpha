"""Enforce final substantive coverage of selected, corroborated major events."""

import json
import re
import time
from html import unescape
from html.parser import HTMLParser


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        if not self.hidden:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _clean(value):
    from .enrich import clean_reporting_text
    parser = _Text()
    parser.feed(value if isinstance(value, str) else "")
    return clean_reporting_text(unescape(" ".join(parser.parts)))


def _cards(briefing):
    for value in briefing.values():
        if isinstance(value, list):
            yield from value
        if isinstance(value, dict):
            yield from value.get("brief", [])


def _valid(card, urls):
    if not isinstance(card, dict) or set(card) != {"headline", "why_it_matters", "body", "sources", "tags", "region"}:
        return False
    if not all(isinstance(card[k], str) and card[k].strip() for k in ("headline", "why_it_matters", "body")):
        return False
    if not 100 <= len(_clean(card["body"]).split()) <= 200:
        return False
    if card["region"] not in ("US", "AMERICAS", "ASIA", "MENA", "EUROPE", "GLOBAL"):
        return False
    if not isinstance(card["tags"], list) or not all(isinstance(t, str) for t in card["tags"]):
        return False
    sources = card["sources"]
    if not isinstance(sources, list) or not sources:
        return False
    if not all(isinstance(s, dict) and set(s) == {"outlet", "url"} and all(isinstance(v, str) and v.strip() for v in s.values()) for s in sources):
        return False
    return any(s["url"] in urls for s in sources)


def _insert_card(briefing, card, events, audit):
    cards = briefing["brief"]
    if len(cards) >= 5:
        protected = {u for e in events for u in e.get("selected_urls", [])}
        victim = next((i for i in range(len(cards) - 1, -1, -1)
                       if not any(s.get("url") in protected for s in cards[i].get("sources", []))), None)
        if victim is None:
            return False
        removed = cards.pop(victim)
        audit.setdefault("capacity_replacements", []).append({
            "section": "world", "headline": removed.get("headline", ""),
            "source_urls": [s.get("url", "") for s in removed.get("sources", [])],
            "reason": "protected_event_story_budget"})
    cards.append(card)
    return True


def enforce(result, bundle, events, *, repair_fn=None, deadline=None, clock=time.monotonic, max_events=None):
    """Mutate final story cards; return private audit. Callback gets prompt, timeout."""
    from .coverage_policy import bounded_events
    from .enrich import substantive_content
    from . import source_diversity

    events = bounded_events(events, max_events)
    briefing = result.setdefault("briefing", {})
    briefing.setdefault("brief", [])
    audit = {"events": [], "repair_attempted": False,
             "max_events": max_events if max_events is not None else
             next((e.get("max_events") for e in events if e.get("max_events") is not None), 2)}
    missing = []
    covered = []
    replacements = {}
    for event in events:
        selected = event.get("selected_urls", [])
        sources = []
        for item in bundle.get("news", []):
            if not isinstance(item, dict) or item.get("url") not in selected:
                continue
            try:
                tier = int(item.get("tier"))
            except (TypeError, ValueError):
                continue
            content = _clean(item.get("content"))
            if tier in (1, 2) and len(content) >= 800:
                sources.append((item, content))
        sources.sort(key=lambda pair: source_diversity._is_guardian_item(pair[0]))
        urls = {item["url"] for item, content in sources if len(content.split()) >= 100}
        record = {"id": event.get("id"), "title": event.get("title", "Major event"),
                  "evidence": event.get("evidence", []), "selected_url": None,
                  "source_urls": list(selected), "stage": "analysis",
                  "initial_reason": "model_omission", "status": "omitted", "reason": ""}
        audit["events"].append(record)
        existing = next((c for c in _cards(briefing) if _valid(c, urls)), None)
        if existing:
            covered.append((event, record, sources, urls, existing))
            record.update(status="covered", initial_reason="", selected_url=next(s["url"] for s in existing["sources"] if s["url"] in urls))
        else:
            missing.append((event, record, sources, urls))
    # Reserve the shared source budget before the one repair call. A grounded
    # rewrite of an already-covered protected card can release a Guardian slot.
    # Keep its original card until a valid replacement has actually been built.
    budget = source_diversity.finance_guardian_budget(bundle, result)
    guardian_demand = sum(source_diversity._is_guardian_story(c)
                          for _, _, c in source_diversity._combined_pick_positions(briefing))
    guardian_demand += sum(source_diversity._is_guardian_item(next(
        (i for i, c in src if substantive_content(c)), {})) for _, _, src, _ in missing)
    if not budget["scarcity_exception_allowed"]:
        for event, record, sources, urls, existing in reversed(covered):
            if guardian_demand <= budget["guardian_cap"]:
                break
            alternatives = [(i, c) for i, c in sources
                            if not source_diversity._is_guardian_item(i) and substantive_content(c)]
            # Only replace published world cards; finance has its separate limit.
            if not alternatives or not source_diversity._is_guardian_story(existing) or not any(
                    c is existing for c in briefing["brief"]):
                continue
            record.update(initial_reason="source_policy_rewrite")
            replacements[id(record)] = existing
            missing.append((event, record, alternatives, {i["url"] for i, _ in alternatives}))
            guardian_demand -= 1
    eligible = [entry for entry in missing if any(len(c.split()) >= 100 for _, c in entry[2])]
    timeout = min(60.0, max(0.0, deadline - clock())) if deadline is not None else 60.0
    repaired = []
    selected_sources = {item.get("url"): str(item.get("source") or "Selected source")
                        for item in bundle.get("news", [])
                        if isinstance(item, dict) and item.get("url") in
                        {url for event in events for url in event.get("selected_urls", [])}
                        and isinstance(item.get("content"), str) and len(_clean(item["content"])) >= 800
                        and len(_clean(item["content"]).split()) >= 100
                        and str(item.get("tier")) in ("1", "2")}
    repair_reason = "repair unavailable" if repair_fn is None else "analysis budget exhausted"
    if eligible and repair_fn is not None and timeout > 0:
        audit["repair_attempted"] = True
        prompt = ("Return a JSON array of story cards with exactly headline, why_it_matters, body, "
                  "sources [{outlet,url}], tags, region. Write 100-200 body words per event, using "
                  "only exact selected URLs and supplied source facts. Preserve allegations and "
                  "attribution; keywords never establish terrorism. Events and source text: " +
                  json.dumps([{"event": e, "sources": [dict(i, content=c) for i, c in src
                                                        if substantive_content(c)]}
                              for e, _, src, _ in eligible]))
        try:
            response = repair_fn(prompt, timeout)
            if isinstance(response, list):
                repaired = response
            repair_reason = "repair returned no valid grounded card"
        except Exception as exc:
            repair_reason = "repair failed: " + type(exc).__name__
    def publish(card, record):
        old = replacements.get(id(record))
        if old is not None:
            index = next((n for n, c in enumerate(briefing["brief"]) if c is old), None)
            if index is not None:
                briefing["brief"][index] = card
                audit.setdefault("source_policy_rewrites", []).append({
                    "section": "world", "headline": old.get("headline", ""),
                    "source_urls": [s.get("url", "") for s in old.get("sources", [])],
                    "replacement_urls": [s["url"] for s in card["sources"]],
                    "reason": "protected_event_source_budget"})
                return True
        return _insert_card(briefing, card, events, audit)

    for event, record, sources, urls in missing:
        card = next((c for c in repaired if _valid(c, urls) and
                     all(s["url"] in urls and s["url"] in selected_sources and
                         s["outlet"].casefold() == selected_sources[s["url"]].casefold()
                         for s in c["sources"])), None)
        if card:
            if card not in briefing["brief"] and not publish(card, record):
                record.update(stage="selection", reason="excluded_by_story_capacity")
                continue
            record.update(status="repaired", stage="repair", selected_url=next(s["url"] for s in card["sources"] if s["url"] in urls))
            continue
        source = next(((i, c) for i, c in sources if len(c.split()) >= 100), None)
        if source:
            item, content = source
            words = content.split()[:200]
            excerpt = " ".join(words)
            # Prefer the last complete sentence without dropping below the word floor.
            endings = list(re.finditer(r'[.!?](?:["\u201d\u2019])?(?=\s|$)', excerpt))
            if endings and len(excerpt[:endings[-1].end()].split()) >= 100:
                excerpt = excerpt[:endings[-1].end()]
            outlet = str(item.get("source") or "Selected source")
            card = {"headline": "Source excerpt: " + str(item.get("title") or event.get("title", "Major event")),
                    "why_it_matters": "Excerpt from " + outlet + " reporting on " + str(item.get("title") or event.get("title", "this event")) + ".",
                    "body": excerpt,
                    "sources": [{"outlet": outlet, "url": item["url"]}],
                    "tags": ["source-excerpt"], "region": "GLOBAL"}
            if not publish(card, record):
                record.update(stage="selection", reason="excluded_by_story_capacity")
                continue
            record.update(status="excerpt_fallback", stage="fallback", selected_url=item["url"], reason=repair_reason)
        else:
            reason = "no selected tier-1/2 source with substantive content" if not sources else "selected source has fewer than 100 excerpt words"
            record["reason"] = reason
            record["stage"] = "enrichment"
            result.setdefault("source_notes", []).append("Coverage gap: " + str(record["title"]) + " — " + reason + ".")
    # This is the final mutation stage: enforce once after repair, without a
    # cap/repair loop. Preserve both filter stages and explain conflicts explicitly.
    from . import source_diversity
    initial_filter = result.get("_debug", {}).get("guardian_selection")
    final_filter = source_diversity.enforce_guardian_soft_cap(bundle, result)
    audit["source_policy"] = {"initial": initial_filter, "final": final_filter}
    for event, record in zip(events, audit["events"]):
        urls = set(event.get("selected_urls", []))
        if record["status"] != "omitted" and not any(
                _valid(c, {record["selected_url"]}) for c in _cards(briefing)):
            record.update(status="omitted", stage="source_policy", reason="source_policy_conflict")
            result.setdefault("source_notes", []).append(
                "Coverage gap: " + str(record["title"]) + " — source policy conflict.")
        removals = [r for policy in (initial_filter, final_filter) if policy
                    for r in policy.get("removals", []) if urls.intersection(r.get("source_urls", []))]
        if removals:
            record["source_policy_removals"] = removals
            if record["status"] == "omitted":
                record.update(stage="source_policy", reason="source_policy_conflict")
    for decision in bundle.get("coverage_eligibility_audit", []):
        if decision.get("reason") != "protected":
            record = dict(decision, status="omitted" if decision.get("eligible") else "not_eligible")
            record["selection_reason"] = decision.get("reason")
            if decision.get("eligible"):
                urls = {i.get("url") for i in bundle.get("news", []) if isinstance(i, dict)
                        and i.get("url") in decision.get("urls", [])
                        and str(i.get("tier")) in ("1", "2")
                        and substantive_content(_clean(i.get("content")))}
                existing = next((c for c in _cards(briefing) if _valid(c, urls)), None)
                if existing:
                    record.update(status="covered", stage="final_validation", reason="",
                                  selected_url=next(s["url"] for s in existing["sources"] if s["url"] in urls))
            audit["events"].append(record)
    from .coverage_policy import recognition_reason
    items = {i.get("url"): i for i in bundle.get("news", []) if isinstance(i, dict)}
    registered_urls = {u for e in events for u in e.get("urls", e.get("selected_urls", []))}
    for decision in bundle.get("selection_audit", []):
        item = items.get(decision.get("url"), {})
        if not (decision.get("recognition_reason") or recognition_reason(item)) or decision.get("url") in registered_urls:
            continue
        reason = ("stale_material" if decision.get("outcome") == "stale" else
                  "seen_article" if decision.get("seen") else None)
        if reason:
            audit["events"].append({"title": decision.get("title") or item.get("title", "Major event"),
                                    "source_urls": [decision.get("url")],
                                    "selection_outcome": decision.get("outcome"),
                                    "status": "not_eligible" if reason == "stale_material" else "observed",
                                    "stage": "selection", "reason": reason})
    unresolved = sum(e.get("status") == "omitted" for e in audit["events"])
    audit["unresolved"] = unresolved
    audit["scope"] = "recognized registered events; not complete major-event recall"
    result["quality_status"] = {"overall": "incomplete" if unresolved else "complete",
                                "unresolved_coverage_events": unresolved, "scope": audit["scope"]}
    return audit
