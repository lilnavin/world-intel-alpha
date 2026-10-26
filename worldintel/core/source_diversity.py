"""Deterministic source-diversity guards for world-intel selection.

The analysis model writes the brief, but owner-approved source caps are pipeline
policy and must be enforced after schema-valid JSON comes back. Helpers here
mutate the result in place, never raise, and leave the brief otherwise unchanged.
"""

from __future__ import annotations

GUARDIAN_CAP = 2
_DEBUG_KEY = "guardian_selection"

_NOISE_CATEGORIES = (
    "sport", "lifestyle", "culture", "entertainment", "travel", "food",
    "advice", "opinion", "press release",
)

_LOW_CONSEQUENCE_TERMS = (
    "celebrity", "recipe", "horoscope", "shopping", "deal", "discount",
    "sports", "football", "baseball", "soccer", "tennis", "golf",
    "crime blotter", "press release", "sponsored", "pr newswire",
    "globenewswire",
)

_HARD_NEWS_TERMS = (
    "hard news", "earthquake", "quake", "tsunami", "wildfire", "flood", "storm",
    "disaster", "war", "missile", "drone", "strike", "ceasefire",
    "gaza", "ukraine", "iran", "russia", "israel", "election", "vote",
    "parliament", "president", "prime minister", "cabinet", "government",
    "sanction", "tariff", "diplomatic", "shipping", "red sea", "oil",
    "gas", "market", "central bank", "inflation",
)


def _as_dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _as_list(x) -> list:
    return x if isinstance(x, list) else []


def _text(value) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _is_guardian_text(value) -> bool:
    text = _text(value)
    return "guardian" in text or "theguardian.com" in text


def _is_guardian_story(story: dict) -> bool:
    for src in _as_list(_as_dict(story).get("sources")):
        src = _as_dict(src)
        if _is_guardian_text(src.get("outlet")) or _is_guardian_text(src.get("url")):
            return True
    return False


def _is_guardian_item(item: dict) -> bool:
    item = _as_dict(item)
    return _is_guardian_text(item.get("source")) or _is_guardian_text(item.get("url"))


def _item_key(item: dict) -> str:
    item = _as_dict(item)
    key = _text(item.get("url")) or _text(item.get("title"))
    return key


def _tier(item: dict) -> int | None:
    try:
        return int(_as_dict(item).get("tier"))
    except (TypeError, ValueError):
        return None


def _is_noise_item(item: dict) -> bool:
    item = _as_dict(item)
    category = _text(item.get("category"))
    hay = " ".join(
        _text(item.get(k))
        for k in ("title", "summary", "source", "url", "category")
    )
    return (
        any(word in category for word in _NOISE_CATEGORIES)
        or any(word in hay for word in _LOW_CONSEQUENCE_TERMS)
    )


def _is_hard_news_item(item: dict) -> bool:
    item = _as_dict(item)
    hay = " ".join(
        _text(item.get(k))
        for k in ("title", "summary", "source", "url", "category", "region")
    )
    if _is_noise_item(item):
        return False
    return any(word in hay for word in _HARD_NEWS_TERMS)


def _eligible_non_guardian_count(bundle: dict) -> int:
    """Count deduped non-Guardian tier-1/2 enriched news candidates."""
    seen: set[str] = set()
    count = 0
    for item in _as_list(_as_dict(bundle).get("news")):
        item = _as_dict(item)
        if _is_guardian_item(item):
            continue
        if _tier(item) not in (1, 2):
            continue
        if not _is_hard_news_item(item):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        key = _item_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        count += 1
    return count


def _combined_pick_positions(briefing: dict) -> list[tuple[str, int, dict]]:
    briefing = _as_dict(briefing)
    positions: list[tuple[str, int, dict]] = []
    for idx, story in enumerate(_as_list(briefing.get("brief"))):
        positions.append(("world", idx, _as_dict(story)))
    finance = _as_dict(briefing.get("finance"))
    for idx, story in enumerate(_as_list(finance.get("brief"))):
        positions.append(("finance", idx, _as_dict(story)))
    return positions


def _remove_positions(briefing: dict, positions: list[tuple[str, int, dict]]) -> None:
    by_section: dict[str, set[int]] = {}
    for section, idx, _story in positions:
        by_section.setdefault(section, set()).add(idx)

    if by_section.get("world"):
        remove = by_section["world"]
        briefing["brief"] = [
            story for idx, story in enumerate(_as_list(briefing.get("brief")))
            if idx not in remove
        ]

    if by_section.get("finance"):
        finance = _as_dict(briefing.get("finance"))
        remove = by_section["finance"]
        finance["brief"] = [
            story for idx, story in enumerate(_as_list(finance.get("brief")))
            if idx not in remove
        ]


def card_identities(briefing: dict) -> list[dict]:
    """Compact private identities; never include article bodies or prompts."""
    return [{"section": section, "index": idx,
             "headline": story.get("headline", ""),
             "source_urls": [s.get("url", "") for s in _as_list(story.get("sources"))
                             if isinstance(s, dict)]}
            for section, idx, story in _combined_pick_positions(briefing)]


def finance_guardian_budget(bundle: dict, world_result=None) -> dict:
    """Describe the shared source policy using the whole enriched input pool."""
    briefing = _as_dict(_as_dict(world_result).get("briefing"))
    consumed = sum(_is_guardian_story(story)
                   for story in _as_list(briefing.get("brief")))
    eligible = _eligible_non_guardian_count(bundle)
    return {
        "guardian_cap": GUARDIAN_CAP,
        "world_guardian_picks": consumed,
        "finance_guardian_remaining": max(0, GUARDIAN_CAP - consumed),
        "eligible_non_guardian_candidates": eligible,
        "scarcity_exception_allowed": eligible < 2,
    }


def enforce_guardian_soft_cap(
    bundle: dict,
    result: dict,
    *,
    cap: int = GUARDIAN_CAP,
) -> dict:
    """Apply the owner-approved Guardian cap across world + finance picks.

    At most `cap` Guardian-cited stories may remain across `briefing.brief` and
    `briefing.finance.brief` unless fewer than two non-Guardian tier-1/2 enriched
    candidates exist after dedupe. Excess Guardian picks are dropped from the end
    of the combined published order, preserving all non-Guardian writing and the
    highest-priority Guardian picks.
    """
    debug = {
        "guardian_cap": cap,
        "guardian_picks_before": 0,
        "guardian_picks_after": 0,
        "eligible_non_guardian_candidates": 0,
        "guardian_cap_overridden": False,
        "guardian_cap_override_reason": None,
        "guardian_cap_removed": 0,
        "section_counts_before": {"world": 0, "finance": 0},
        "section_counts_after": {"world": 0, "finance": 0},
        "removed_by_section": {"world": 0, "finance": 0},
        "removals": [],
        "before": [],
        "after": [],
    }
    try:
        result = _as_dict(result)
        briefing = _as_dict(result.get("briefing"))
        if not briefing:
            result.setdefault("_debug", {})[_DEBUG_KEY] = debug
            return debug

        positions = _combined_pick_positions(briefing)
        debug["before"] = card_identities(briefing)
        for section, _idx, _story in positions:
            debug["section_counts_before"][section] += 1
        guardian_positions = [
            pos for pos in positions if _is_guardian_story(pos[2])
        ]
        debug["guardian_picks_before"] = len(guardian_positions)
        debug["eligible_non_guardian_candidates"] = _eligible_non_guardian_count(bundle)

        if len(guardian_positions) > cap:
            if debug["eligible_non_guardian_candidates"] < 2:
                debug["guardian_cap_overridden"] = True
                debug["guardian_cap_override_reason"] = (
                    "fewer than 2 non-Guardian tier-1/tier-2 enriched "
                    "candidates available after dedupe"
                )
            else:
                excess = guardian_positions[cap:]
                _remove_positions(briefing, excess)
                debug["guardian_cap_removed"] = len(excess)
                for section, idx, story in excess:
                    debug["removed_by_section"][section] += 1
                    debug["removals"].append({"section": section, "index": idx,
                                              "headline": story.get("headline", ""),
                                              "source_urls": [s.get("url", "") for s in _as_list(story.get("sources")) if isinstance(s, dict)],
                                              "reason": "shared_guardian_cap_exceeded"})

        for section, _idx, _story in _combined_pick_positions(briefing):
            debug["section_counts_after"][section] += 1
        after = [
            pos for pos in _combined_pick_positions(briefing)
            if _is_guardian_story(pos[2])
        ]
        debug["guardian_picks_after"] = len(after)
        debug["after"] = card_identities(briefing)
        result.setdefault("_debug", {})[_DEBUG_KEY] = debug
    except Exception:
        try:
            result.setdefault("_debug", {})[_DEBUG_KEY] = debug
        except Exception:
            pass
    return debug
