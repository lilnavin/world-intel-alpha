"""world-intel Module M — deterministic per-run KPI metrics.

Computes a flat dict of key performance indicators for a single briefing run
(`compute_metrics`) and appends it as one JSON line to a rolling log
(`append_metrics`), so runs can be compared over time (Codex health, enrichment
yield, source gaps, brief composition, hedging, market coverage).

ABSOLUTE RULE (same as Modules A/B/D'): nothing here may raise out of the module.
`compute_metrics` builds a fully-shaped record of defaults first, then fills each
KPI inside its own guarded block, so a malformed bundle/result yields a record
with sensible defaults (0 / [] / None / False) rather than an exception.
`append_metrics` is best-effort: on any error it returns None instead of raising.

Pure — no network. The only I/O is the local JSONL append in `append_metrics`.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from worldintel import config as wi_config

# Hedging/meta language: shared with the post-analysis writing gate so the
# hedging_count series counts exactly what the gate rewrites. Falls back to the
# historical narrow patterns if quality_gate is somehow unimportable.
try:
    from worldintel.core.quality_gate import META_RE as _HEDGING_RE
except Exception:
    _HEDGING_RE = re.compile(
        r"bundle (?:does not|only|gives|provides)"
        r"|only (?:gives|provides|offers).{0,20}(?:headline|summary)"
        r"|limited (?:detail|summary)",
        re.IGNORECASE,
    )

# Tokens (lowercased) that mark a brief tag as a country/region rather than a
# topic. Used to bucket each brief story into a geography for region_distribution.
_REGION_TOKENS = {
    "us", "usa", "u.s.", "united states", "america", "americas",
    "canada", "japan", "china", "korea", "south korea", "north korea",
    "uk", "u.k.", "united kingdom", "britain", "europe", "eu", "germany",
    "france", "italy", "spain", "russia", "ukraine", "india", "asia",
    "middle east", "taiwan", "israel", "iran", "brazil", "mexico", "africa",
    "australia", "world", "global",
}


# --------------------------------------------------------------------------- #
# Small safe coercion helpers (mirror compose_html.py house style)
# --------------------------------------------------------------------------- #
# `rates_stale` is a publication-freeze detector, not a "did rates print today"
# check. Rates series lag by design (FRED publishes a business day behind) and
# other groups roll on their own calendars — FX bars in particular run past the
# bond/equity session, so on 2026-08-02 rates sat at Friday 07-31 while FX
# carried Saturday 08-01. Anything up to and including this many calendar days
# behind the freshest non-rates instrument is a normal lag; it spans a weekend
# plus a holiday. Beyond it, rates really have stopped updating.
RATES_LAG_TOLERANCE_DAYS = 4


def _as_dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _asof_date(asof):
    """`asof` -> `datetime.date`, or None. Accepts both shapes the fetchers
    produce: a bare ISO date from the rates/yfinance paths and Twelve Data's
    'YYYY-MM-DD HH:MM:SS'. Never raises."""
    if not isinstance(asof, str) or not asof.strip():
        return None
    try:
        return datetime.date.fromisoformat(asof.strip()[:10])
    except ValueError:
        return None


def _as_list(x) -> list:
    return x if isinstance(x, list) else []


def _num(value):
    """Coerce to float or None (never raises); bools are not numbers here."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (ValueError, AttributeError):
            return None
    return None


def _brief_urls(story: dict) -> list:
    """All non-empty source urls (lowercased) carried by one brief story."""
    urls = []
    for src in _as_list(_as_dict(story).get("sources")):
        url = _as_dict(src).get("url")
        if isinstance(url, str) and url.strip():
            urls.append(url.lower())
    return urls


def _story_region(story: dict) -> str:
    """Prefer the schema's explicit region; use tags for legacy stored briefs."""
    story = _as_dict(story)
    explicit = story.get("region")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().upper()
    for tag in _as_list(story.get("tags")):
        if not isinstance(tag, str):
            continue
        token = tag.strip().lower()
        if token in region_tokens():
            return token.title()
    return "other"


def region_tokens() -> set:
    configured = wi_config.get().tuning.get("region_tokens")
    return {token.strip().lower() for token in configured} if configured is not None else set(_REGION_TOKENS)


def compute_pool(bundle: dict, result: dict) -> list:
    """Per-item audit of the candidate pool: which stories the run COULD have
    used, whether each was enriched, and which published desk picked it
    (matched by source URL). Never raises."""
    try:
        bundle = _as_dict(bundle)
        result = _as_dict(result)
        briefing = _as_dict(result.get("briefing"))
        world_urls: set = set()
        for s in _as_list(briefing.get("brief")):
            world_urls.update(_brief_urls(s))
        national_urls = {did: {url for story in _as_list(briefing.get(did))
                               for url in _brief_urls(story)}
                         for did in wi_config.get().local_desk_ids}
        finance_urls: set = set()
        for s in _as_list(_as_dict(briefing.get("finance")).get("brief")):
            finance_urls.update(_brief_urls(s))

        pool = []
        for item in _as_list(bundle.get("news")):
            item = _as_dict(item)
            url = (item.get("url") or "").strip().lower() if isinstance(item.get("url"), str) else ""
            picked = None
            if url and url in world_urls:
                picked = "world"
            elif url:
                picked = next((did for did, urls in national_urls.items() if url in urls), None)
                if picked is None and url in finance_urls:
                    picked = "finance"
            content = item.get("content")
            title = item.get("title")
            source = item.get("source")
            pool.append({
                "title": title.strip() if isinstance(title, str) else "",
                "source": source.strip() if isinstance(source, str) else "",
                "tier": item.get("tier") if isinstance(item.get("tier"), int) else None,
                "enriched": isinstance(content, str) and bool(content.strip()),
                "picked": picked,
            })
        return pool
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def compute_metrics(bundle: dict, result: dict, date: str,
                    timings: dict | None = None) -> dict:
    """Compute a flat dict of deterministic KPIs for one brief run.

    Pure, never raises: missing/None/mis-typed fields degrade to sensible
    defaults (0 / [] / None / False). `timings` is an optional {phase: seconds}
    map (e.g. {'fetch':.., 'enrich':.., 'codex':.., 'total':..}) echoed verbatim.
    """
    # Fully-shaped defaults first; each KPI is then filled in its own guarded
    # block so any single malformed field can't blank the whole record.
    rec = {
        "date": date if isinstance(date, str) else None,
        "timings": timings if isinstance(timings, dict) else None,
        # pipeline
        "codex_ok": False,
        "sources_ok": [],
        "sources_failed": [],
        "data_gaps": [],
        # enrichment
        "enriched_count": 0,
        "news_pool_size": 0,
        "enrich_note": None,
        # desks (Phase 1)
        "desk_counts": {},
        "feed_health": {"ok": 0, "total": 0},
        "pool_size_raw": 0,
        # brief composition
        "brief_count": 0,
        "fedreg_share": 0.0,
        "guardian_share": 0.0,
        "guardian_cap_overridden": False,
        "guardian_cap_override_reason": None,
        "guardian_cap_removed": 0,
        "guardian_picks": 0,
        "eligible_non_guardian_candidates": 0,
        "region_distribution": {},
        "tier_distribution": {},
        "buried_count": 0,
        "noise_count": 0,
        "finance_brief_count": 0,
        **{f"{did}_brief_count": 0 for did in wi_config.get().local_desk_ids},
        "scoreboard_story_refs": 0,
        "scoreboard_story_refs_dropped": 0,
        "threads_count": 0,
        "filings_count": 0,
        "earnings_count": 0,
        "followup_count": 0,
        "filing_lines_count": 0,
        "filings_matched": 0,
        "filings_expired_unread": 0,
        "filing_index_days_cached": 0,
        "filing_index_days_fetched": 0,
        "fundamentals_ok_count": 0,
        # quality proxies
        "hedging_count": 0,
        "avg_body_chars": 0.0,
        "filings_list_len": 0,
        # markets
        "markets_covered": 0,
        "biggest_mover": None,
        "rates_stale": False,
        # pool audit
        "pool": [],
    }

    bundle = _as_dict(bundle)
    result = _as_dict(result)
    briefing = _as_dict(result.get("briefing"))
    brief = _as_list(briefing.get("brief"))
    news = _as_list(bundle.get("news"))
    health = _as_dict(bundle.get("source_health"))

    # --- pipeline: codex health + source health --------------------------- #
    try:
        status = result.get("analysis_status") or {}
        rec["quality_status"] = result.get("quality_status") or {}
        rec["coverage_audit"] = result.get("coverage_audit") or {}
        rec["codex_ok"] = (status.get("overall") == "complete" and
                           all((status.get("passes") or {}).get(name, {}).get("success") is True
                               for name in ("world", "finance")))
        if result.get("analysis_engine", "codex") != "codex":
            rec["codex_ok"] = None
            rec["analysis_engine"] = result["analysis_engine"]
        rec["analysis_status"] = status
    except Exception:
        pass

    try:
        ok, failed = [], []
        for src, entry in health.items():
            if src == "enrich":
                continue  # enrich health is reported separately below
            if isinstance(entry, dict) and entry.get("ok"):
                ok.append(str(src))
            else:
                failed.append(str(src))
        rec["sources_ok"] = sorted(ok)
        rec["sources_failed"] = sorted(failed)
        rec["data_gaps"] = sorted(failed)
    except Exception:
        pass

    # --- enrichment ------------------------------------------------------- #
    try:
        rec["news_pool_size"] = len(news)
    except Exception:
        pass
    try:
        rec["enriched_count"] = sum(
            1 for it in news
            if isinstance(_as_dict(it).get("content"), str)
            and _as_dict(it).get("content").strip()
        )
    except Exception:
        pass
    try:
        note = _as_dict(health.get("enrich")).get("note")
        rec["enrich_note"] = note if isinstance(note, str) else None
    except Exception:
        pass

    # --- desks (Phase 1) ---------------------------------------------------- #
    try:
        counts = {}
        for item in _as_list(bundle.get("news")):
            desk = _as_dict(item).get("desk")
            if isinstance(desk, str) and desk:
                counts[desk] = counts.get(desk, 0) + 1
        rec["desk_counts"] = counts
    except Exception:
        pass

    try:
        ok = total = 0
        for name, entry in _as_dict(bundle.get("source_health")).items():
            if not isinstance(name, str) or not name.startswith("feed:"):
                continue
            total += 1
            if _as_dict(entry).get("ok"):
                ok += 1
        rec["feed_health"] = {"ok": ok, "total": total}
    except Exception:
        pass

    try:
        raw = bundle.get("news_pool_size_raw")
        rec["pool_size_raw"] = int(raw) if isinstance(raw, int) else 0
    except Exception:
        pass

    # --- brief composition ------------------------------------------------ #
    try:
        rec["brief_count"] = len(brief)
    except Exception:
        pass

    try:
        n = len(brief)
        if n:
            fed = sum(
                1 for s in brief
                if any("federalregister.gov" in u for u in _brief_urls(s))
            )
            guardian = sum(
                1 for s in brief
                if any("guardian" in u for u in _brief_urls(s))
            )
            rec["fedreg_share"] = round(fed / n, 4)
            rec["guardian_share"] = round(guardian / n, 4)
            rec["guardian_picks"] = guardian
    except Exception:
        pass

    try:
        guard = _as_dict(_as_dict(result.get("_debug")).get("guardian_selection"))
        if guard:
            rec["guardian_cap_overridden"] = bool(
                guard.get("guardian_cap_overridden")
            )
            reason = guard.get("guardian_cap_override_reason")
            rec["guardian_cap_override_reason"] = (
                reason if isinstance(reason, str) and reason.strip() else None
            )
            removed = guard.get("guardian_cap_removed")
            rec["guardian_cap_removed"] = removed if isinstance(removed, int) else 0
            after = guard.get("guardian_picks_after")
            if isinstance(after, int):
                rec["guardian_picks"] = after
            eligible = guard.get("eligible_non_guardian_candidates")
            rec["eligible_non_guardian_candidates"] = (
                eligible if isinstance(eligible, int) else 0
            )
    except Exception:
        pass

    try:
        region_dist: dict = {}
        for s in brief:
            label = _story_region(s)
            region_dist[label] = region_dist.get(label, 0) + 1
        rec["region_distribution"] = region_dist
    except Exception:
        pass

    try:
        tier_dist: dict = {}
        for it in news:
            tier = _as_dict(it).get("tier")
            key = str(tier) if tier is not None else "unknown"
            tier_dist[key] = tier_dist.get(key, 0) + 1
        rec["tier_distribution"] = tier_dist
    except Exception:
        pass

    # KPI key names (`buried_count`/`noise_count`) predate the signal-section
    # restructure and are kept so the PM loop's historical series stays intact:
    # buried_count now counts the signal-of-the-day top alongside the list, and
    # noise_count is 1/0 for the collapsed one-line noise.
    try:
        signal = _as_dict(briefing.get("signal"))
        top_story = _as_dict(signal.get("top")).get("story")
        has_top = isinstance(top_story, str) and bool(top_story.strip())
        rec["buried_count"] = len(_as_list(signal.get("buried"))) + (1 if has_top else 0)
        noise_line = signal.get("noise_line")
        rec["noise_count"] = 1 if isinstance(noise_line, str) and noise_line.strip() else 0
    except Exception:
        pass
    try:
        finance = _as_dict(briefing.get("finance"))
        rec["finance_brief_count"] = len(_as_list(finance.get("brief")))
        for did in wi_config.get().local_desk_ids:
            rec[f"{did}_brief_count"] = len(_as_list(briefing.get(did)))
    except Exception:
        pass
    # Story citations on `scoreboard_read`. The contradiction and each watch
    # item may name a PUBLISHED headline, and one that does not resolve takes
    # its whole item off the page (compose_html._cite). `_dropped` is therefore
    # the series that matters: it is how a model quietly inventing headlines
    # becomes visible, the operational role hedging_count plays for
    # meta-language. The renderer's own helpers are imported rather than
    # reimplemented -- a second normaliser here would drift from the one that
    # actually decides what ships, and then these counters would measure
    # nothing. Lazily, so this module stays import-safe on its own.
    try:
        from worldintel.core import compose_html  # noqa: PLC0415

        read = _as_dict(_as_dict(briefing.get("finance")).get("scoreboard_read"))
        citable = compose_html._citable_stories(briefing)
        refs = dropped = 0
        for item in [read.get("contradiction")] + _as_list(read.get("watch")):
            story = _as_dict(item).get("story")
            story = story.strip() if isinstance(story, str) else ""
            if not story:
                continue
            if compose_html._norm_headline(story) in citable:
                refs += 1
            else:
                dropped += 1
        rec["scoreboard_story_refs"] = refs
        rec["scoreboard_story_refs_dropped"] = dropped
    except Exception:
        pass
    try:
        rec["threads_count"] = len(_as_list(briefing.get("threads")))
    except Exception:
        pass
    try:
        rec["filings_count"] = len(_as_list(bundle.get("filings")))
        rec["filings_list_len"] = rec["filings_count"]
    except Exception:
        pass
    try:
        rec["earnings_count"] = len(_as_list(bundle.get("earnings_calendar")))
    except Exception:
        pass
    try:
        # Beat-2 coverage. `followup_count` is what got a full block;
        # `filing_lines_count` is what was surfaced as a line instead, and the
        # two together are everything the reconciler matched — so `matched` is
        # derived rather than plumbed, and cannot disagree with the page.
        #
        # The ratio is the number that matters. The mechanism this replaced
        # surfaced 12 of 203 eligible filings in a month and nothing recorded
        # the other 191, which is precisely why a cap quietly swallowing
        # filings has to be visible here.
        rec["followup_count"] = len(_as_list(bundle.get("post_earnings_followups")))
        rec["filing_lines_count"] = len(_as_list(bundle.get("filing_lines")))
        rec["filings_matched"] = rec["followup_count"] + rec["filing_lines_count"]
        # What the 20-day index window aged out before anything read it. It
        # leaves no other trace, so a 0 here is the only evidence the drain is
        # keeping up.
        expired = bundle.get("filings_expired_unread")
        rec["filings_expired_unread"] = expired if isinstance(expired, int) else 0
        index_health = _as_dict(_as_dict(bundle.get("source_health")).get("filing_index"))
        rec["filing_index_days_cached"] = index_health.get("days_cached", 0)
        rec["filing_index_days_fetched"] = index_health.get("days_fetched", 0)
        rec["fundamentals_ok_count"] = sum(
            1 for c in _as_list(bundle.get("post_earnings_candidates"))
            if _as_dict(_as_dict(c).get("fundamentals")).get("status") == "ok"
        )
    except Exception:
        pass

    # --- quality proxies -------------------------------------------------- #
    try:
        # Finance-brief and Japan stories share the Brief's story shape and
        # deserve the same hedging scrutiny, so their bodies join the pool.
        finance_brief = _as_list(_as_dict(briefing.get("finance")).get("brief"))
        national = [story for did in wi_config.get().local_desk_ids
                    for story in _as_list(briefing.get(did))]
        bodies = [
            _as_dict(s).get("body")
            for s in brief + national + finance_brief
            if isinstance(_as_dict(s).get("body"), str)
        ]
        rec["hedging_count"] = sum(1 for b in bodies if _HEDGING_RE.search(b))
        if bodies:
            rec["avg_body_chars"] = round(
                sum(len(b) for b in bodies) / len(bodies), 1
            )
    except Exception:
        pass

    # --- markets ---------------------------------------------------------- #
    markets = _as_dict(bundle.get("markets"))
    try:
        covered = 0
        for group in markets.values():
            for inst in _as_dict(group).values():
                if _num(_as_dict(inst).get("level")) is not None:
                    covered += 1
        rec["markets_covered"] = covered
    except Exception:
        pass

    try:
        best = None
        best_abs = -1.0
        for group_key, group in markets.items():
            if group_key == "rates":
                continue  # rates move in bp, not %, so excluded from the contest
            for key, inst in _as_dict(group).items():
                change = _num(_as_dict(inst).get("change_pct"))
                if change is None:
                    continue
                if abs(change) > best_abs:
                    best_abs = abs(change)
                    best = {
                        "group": group_key,
                        "key": key,
                        "change_pct": change,
                    }
        rec["biggest_mover"] = best
    except Exception:
        pass

    try:
        rates_asofs, other_asofs = [], []
        for group_key, group in markets.items():
            for inst in _as_dict(group).values():
                day = _asof_date(_as_dict(inst).get("asof"))
                if day is None:
                    continue
                (rates_asofs if group_key == "rates" else other_asofs).append(day)
        if rates_asofs and other_asofs:
            lag_days = (max(other_asofs) - max(rates_asofs)).days
            rec["rates_stale"] = lag_days > RATES_LAG_TOLERANCE_DAYS
    except Exception:
        pass

    # --- pool audit ---------------------------------------------------- #
    try:
        rec["pool"] = compute_pool(bundle, result)
    except Exception:
        pass

    return rec


def append_metrics(root: str, record: dict) -> str | None:
    """Append `record` as one JSON line to <root>/metrics/runs.jsonl (creating
    dirs as needed). Best-effort: on any error return None, never raise. Returns
    the file path on success."""
    try:
        metrics_dir = os.path.join(root, "metrics")
        os.makedirs(metrics_dir, exist_ok=True)
        path = os.path.join(metrics_dir, "runs.jsonl")
        line = json.dumps(record, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return path
    except Exception:
        return None
