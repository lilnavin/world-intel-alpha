"""world-intel — post-analysis writing-quality gate.

Scans the Codex briefing for meta-language (bodies that narrate the pipeline's
inputs — "the bundle only gives a headline", "limited detail" — instead of
reporting news) and, on a hit, asks Codex once to rewrite ONLY the flagged
bodies. Fail-open: any failure leaves the original bodies in place and the run
proceeds; the gate may degrade the brief's polish but never its delivery.

ABSOLUTE RULE (house style): nothing here raises out of the module.
"""

from __future__ import annotations

import re
from worldintel import config as wi_config

# Meta-language: any body that references the model's own inputs or narrates
# missing information. Superset of the historical metrics "hedging" patterns so
# the hedging_count series keeps counting everything it used to.
META_RE = re.compile(
    r"\b(?:the|this) bundle\b|bundle (?:does not|only|gives|provides)"
    r"|only (?:gives|provides|offers|carries)[^.]{0,40}(?:headline|summary)"
    r"|limited (?:detail|summary|information)"
    r"|no (?:further|additional) (?:detail|information)"
    r"|(?:is|are|remains?) unclear from the"
    r"|based on the (?:headline|summary)"
    r"|the (?:sources?|data|coverage|material) (?:provided|available|supplied)\b"
    r"|the (?:provided|available|supplied) (?:sources?|data|coverage|material)\b"
    r"|the (?:source|article|item|reporting) (?:does not|doesn't|only)",
    re.IGNORECASE,
)

_REWRITE_HEADER = """You are a wire-service news editor. Each numbered passage below is a
story body from today's intelligence brief that references its own source material
("the bundle", "limited detail", "unclear from the reporting") instead of reporting news.

Rewrite each passage so it reads like professional news copy:
- Keep every concrete fact, figure, and named actor. Invent NOTHING.
- Delete every reference to sources, data availability, or missing information —
  state what is known and silently omit the rest.
- Keep roughly the same length and the same order.

Return ONLY a JSON object {"bodies": [...]} with exactly {n} rewritten passages,
same order as given.

"""


def scan(result: dict) -> list:
    """Return [(section, index, body), ...] for every story body in
    briefing.brief / briefing.japan / briefing.canada /
    briefing.finance.brief that trips
    META_RE. Never raises."""
    flagged = []
    try:
        briefing = ((result or {}).get("briefing")) or {}
        finance = briefing.get("finance") or {}
        if not isinstance(finance, dict):
            finance = {}
        # Japan stories share the Brief's shape and style contract, and their
        # prompt is the one that warns hardest about padding — so they are
        # scanned on the same terms rather than shipping unexamined.
        sections = [(key, briefing.get(key) or [])
                    for key in ("brief", *wi_config.get().local_desk_ids)]
        sections.append(("finance", finance.get("brief") or []))
        for section, stories in sections:
            if not isinstance(stories, list):
                continue
            for i, story in enumerate(stories):
                body = story.get("body") if isinstance(story, dict) else None
                if isinstance(body, str) and META_RE.search(body):
                    flagged.append((section, i, body))
    except Exception:
        return []
    return flagged


def rewrite(result: dict, flagged: list, run_schema_fn, schema_path: str) -> int:
    """One Codex retry: rewrite the flagged bodies and patch them in place.

    ``run_schema_fn(prompt_text, schema_path) -> dict`` is injected (normally
    ``codex_engine.run_schema``). Returns the number of bodies patched; 0 on any
    failure or if a rewrite still trips META_RE (fail-open). Never raises."""
    try:
        if not flagged:
            return 0
        numbered = "\n\n".join(
            f"[{i + 1}] {body}" for i, (_s, _i, body) in enumerate(flagged)
        )
        prompt = _REWRITE_HEADER.replace("{n}", str(len(flagged))) + numbered
        try:
            out = run_schema_fn(prompt, schema_path)
        except Exception:
            return 0
        bodies = (out or {}).get("bodies") if isinstance(out, dict) else None
        if not isinstance(bodies, list) or len(bodies) != len(flagged):
            return 0

        briefing = (result or {}).get("briefing") or {}
        patched = 0
        for (section, idx, _old), new_body in zip(flagged, bodies):
            if not isinstance(new_body, str) or not new_body.strip():
                continue
            if META_RE.search(new_body):
                continue  # rewrite is still dirty — keep the original
            try:
                # Resolve the section to its OWN list. The earlier form branched
                # `brief` vs everything-else, so the first japan hit would have
                # been written into finance.brief[idx] — overwriting a different
                # section's story with another section's prose.
                if section == "finance":
                    stories = briefing["finance"]["brief"]
                else:
                    stories = briefing[section]
                stories[idx]["body"] = new_body.strip()
                patched += 1
            except Exception:
                continue
        return patched
    except Exception:
        return 0


def _citation_identity(url: str) -> str:
    """Normalize scheme/host case without changing the cited resource."""
    url = url.strip()
    match = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)([^/?#]*)", url)
    if not match:
        return url
    userinfo, separator, host = match[2].rpartition("@")
    authority = userinfo + separator + host.lower()
    return match[1].lower() + authority + url[match.end():]


def enforce_country_grounding(result: dict, bundle: dict) -> dict:
    """Remove country cards that cannot prove full-text, tier-1/2 grounding.

    The model's prompt is guidance; this is the publication boundary. A card
    survives only when one of its cited URLs exactly matches a selected news
    item carrying a real enriched body. Body length is also enforced here so a
    quota can never turn a headline or teaser into publishable copy.
    """
    audit = {f"{did}_removed": 0 for did in wi_config.get().local_desk_ids}
    try:
        eligible = {}
        for item in (bundle or {}).get("news") or []:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            content = item.get("content")
            try:
                tier = int(item.get("tier"))
            except (TypeError, ValueError):
                continue
            if (isinstance(url, str) and url.strip() and tier in (1, 2)
                    and isinstance(content, str) and len(content.strip()) >= 800):
                eligible[_citation_identity(url)] = item

        briefing = ((result or {}).get("briefing")) or {}
        for section in wi_config.get().local_desk_ids:
            kept = []
            stories = briefing.get(section) or []
            if not isinstance(stories, list):
                briefing[section] = []
                continue
            for story in stories:
                if not isinstance(story, dict):
                    audit[f"{section}_removed"] += 1
                    continue
                words = len((story.get("body") or "").split())
                urls = {
                    _citation_identity(src.get("url", ""))
                    for src in (story.get("sources") or [])
                    if isinstance(src, dict) and isinstance(src.get("url"), str)
                }
                if 100 <= words <= 200 and any(url in eligible for url in urls):
                    kept.append(story)
                else:
                    audit[f"{section}_removed"] += 1
            briefing[section] = kept
        result.setdefault("_debug", {})["country_grounding"] = audit
    except Exception:
        pass
    return audit
