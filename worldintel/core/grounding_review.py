"""Bounded independent source review of final full story cards.

Bindings prove which prose and source quotes were reviewed. They cannot prove
that the model enumerated every factual claim or that a quote entails a claim.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time

from worldintel import config, data
from worldintel.core.quality_gate import _citation_identity, META_RE
from worldintel.core.coverage_gate import _clean

SCOPE = "final full story cards: headline, why_it_matters, body; not whole-brief verification"
FIELDS = ("headline", "why_it_matters", "body")


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _material(result, bundle):
    briefing = result["briefing"]
    sections = [(key, briefing.get(key, [])) for key in ("brief", *config.get().local_desk_ids)]
    sections.append(("finance.brief", briefing.get("finance", {}).get("brief", [])))
    cards, sources, by_url = [], {}, {}
    for index, source in enumerate(bundle.get("news", [])):
        if isinstance(source, dict) and isinstance(source.get("url"), str):
            by_url.setdefault(_citation_identity(source["url"]), []).append((index, source))
    for section, items in sections:
        if not isinstance(items, list):
            raise ValueError("invalid card section")
        for index, card in enumerate(items):
            if not isinstance(card, dict) or any(not isinstance(card.get(f), str) or not card[f].strip() for f in FIELDS):
                raise ValueError("missing card prose")
            prose = {f: card[f] for f in FIELDS}
            entry = {"card_id": f"{section}:{index}", **prose,
                     "prose_sha256": _digest(json.dumps(prose, ensure_ascii=False, sort_keys=True)), "source_ids": []}
            citations = card.get("sources")
            if not isinstance(citations, list) or not citations:
                raise ValueError("missing cited source")
            for citation in citations:
                url = citation.get("url") if isinstance(citation, dict) else None
                matches = by_url.get(_citation_identity(url), []) if isinstance(url, str) else []
                if len(matches) != 1:
                    raise ValueError("missing or ambiguous cited selected source")
                source_index, source = matches[0]
                text = source.get("content")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("cited source has no full text")
                sid = f"source:{source_index}"
                sources[sid] = {"source_id": sid, "sha256": _digest(text), "url": source["url"],
                                "title": source.get("title", ""), "content": text}
                if sid not in entry["source_ids"]:
                    entry["source_ids"].append(sid)
            cards.append(entry)
            if len(cards) > 24 or len(sources) > 48:
                raise ValueError("review input cap exceeded")
    if not cards:
        raise ValueError("no full story cards to review")
    return {"cards": cards, "sources": list(sources.values())}


def _validate_integrity(response, material):
    """Validate every response binding, including evidence on rejected cards."""
    expected = {c["card_id"]: c for c in material["cards"]}
    sources = {s["source_id"]: s for s in material["sources"]}
    if not isinstance(response, dict) or set(response) != {"cards"} or not isinstance(response["cards"], list):
        raise ValueError("malformed review response")
    if len(response["cards"]) != len(expected):
        raise ValueError("missing or duplicate reviewed card")
    seen = set()
    for record in response["cards"]:
        if not isinstance(record, dict) or set(record) != {"card_id", "prose_sha256", "verdict", "reason", "claims"}:
            raise ValueError("malformed reviewed card")
        cid = record["card_id"]
        if not isinstance(cid, str) or cid not in expected or cid in seen:
            raise ValueError("missing or duplicate reviewed card")
        seen.add(cid)
        card = expected[cid]
        if record["prose_sha256"] != card["prose_sha256"]:
            raise ValueError("reviewed prose digest mismatch")
        if not isinstance(record["reason"], str) or not record["reason"].strip():
            raise ValueError("missing review reason")
        if record["verdict"] not in ("supported", "unsupported", "uncertain"):
            raise ValueError("invalid review verdict")
        claims = record["claims"]
        if not isinstance(claims, list) or (record["verdict"] == "supported" and not claims):
            raise ValueError("supported card lacks claim evidence")
        for claim in claims:
            if not isinstance(claim, dict) or set(claim) != {"field", "text", "source_id", "source_sha256", "quote"}:
                raise ValueError("malformed claim")
            field, text, sid, quote = (claim[k] for k in ("field", "text", "source_id", "quote"))
            if (not isinstance(field, str) or field not in FIELDS or not isinstance(text, str)
                    or not text.strip() or text not in card[field]):
                raise ValueError("claim is not an exact card substring")
            if not isinstance(sid, str) or sid not in card["source_ids"]:
                raise ValueError("claim source is not cited by card")
            source = sources[sid]
            if (claim["source_sha256"] != source["sha256"] or not isinstance(quote, str)
                    or not quote.strip() or quote not in source["content"]):
                raise ValueError("invalid source hash or verbatim quote")

    return [r for r in response["cards"] if r["verdict"] != "supported"]


def _validate(response, material):
    if _validate_integrity(response, material):
        raise ValueError("unsupported or uncertain card")


def _correction_material(material, response):
    rejected = _validate_integrity(response, material)
    ids = {r["card_id"] for r in rejected}
    cards = [c for c in material["cards"] if c["card_id"] in ids]
    cited = {sid for c in cards for sid in c["source_ids"]}
    return {"cards": cards, "sources": [s for s in material["sources"] if s["source_id"] in cited],
            "review": {"cards": rejected}}


def _card(result, cid):
    section, _, index = cid.rpartition(":")
    briefing = result["briefing"]
    items = briefing["finance"]["brief"] if section == "finance.brief" else briefing[section]
    return items[int(index)]


def _apply_corrections(result, material, rejected, patches):
    """Return a staged copy; exact rejected IDs/hashes and prose-only patches."""
    expected = {c["card_id"]: c for c in material["cards"] if c["card_id"] in {r["card_id"] for r in rejected}}
    if (not expected or not isinstance(patches, dict) or set(patches) != {"cards"}
            or not isinstance(patches["cards"], list) or len(patches["cards"]) != len(expected)):
        raise ValueError("invalid correction set")
    candidate, seen = copy.deepcopy(result), set()
    for patch in patches["cards"]:
        if not isinstance(patch, dict) or set(patch) != {"card_id", "prose_sha256", *FIELDS}:
            raise ValueError("invalid correction fields")
        cid = patch["card_id"]
        if not isinstance(cid, str) or cid not in expected or cid in seen:
            raise ValueError("invalid correction identity")
        seen.add(cid)
        if patch["prose_sha256"] != expected[cid]["prose_sha256"]:
            raise ValueError("correction digest mismatch")
        if any(not isinstance(patch[f], str) or not patch[f].strip() or META_RE.search(patch[f]) for f in FIELDS):
            raise ValueError("invalid corrected prose")
        if not 100 <= len(_clean(patch["body"]).split()) <= 200:
            raise ValueError("corrected body length invalid")
        _card(candidate, cid).update({f: patch[f] for f in FIELDS})
    return candidate


def _prompt(name, material):
    prompt = data.read_text(f"prompts/{name}.md") + "\nINPUT_JSON\n" + json.dumps(material, ensure_ascii=False)
    if len(prompt.encode("utf-8")) > 131072:
        raise ValueError("review prompt byte cap exceeded")
    return prompt


def review(result, bundle, *, review_fn, deadline, repair_fn=None, clock=time.monotonic):
    """One review, optionally one scoped correction and one final all-card review."""
    audit = {"status": "hold", "scope": SCOPE, "attempted": False, "cards_reviewed": 0,
             "timeout_s": 0, "reason": None, "repair_attempted": False, "review_calls": 0,
             "corrected_cards": 0}
    def call(name, material, callback):
        prompt = _prompt(name, material)
        timeout = min(120, max(0, deadline - clock()))
        if timeout <= 0:
            raise ValueError("analysis budget exhausted")
        with data.as_file(f"schema/{name}.schema.json") as schema:
            if name == "source-review":
                audit.update(attempted=True, timeout_s=timeout)
                audit["review_calls"] += 1
            else:
                audit["repair_attempted"] = True
            response = callback(prompt, str(schema), timeout)
        if clock() > deadline:
            raise ValueError("analysis deadline exceeded")
        return response
    try:
        material = _material(result, bundle)
        response = call("source-review", material, review_fn)
        rejected = _validate_integrity(response, material)
        candidate = result
        if rejected:
            if repair_fn is None:
                raise ValueError("unsupported or uncertain card")
            patches = call("source-correction", _correction_material(material, response), repair_fn)
            candidate = _apply_corrections(result, material, rejected, patches)
            final_material = _material(candidate, bundle)
            _validate(call("source-review", final_material, review_fn), final_material)
            # All checks have completed before any caller-owned prose is adopted.
            for record in rejected:
                cid = record["card_id"]
                _card(result, cid).update({f: _card(candidate, cid)[f] for f in FIELDS})
            audit["corrected_cards"] = len(rejected)
        audit.update(status="accepted", cards_reviewed=len(material["cards"]))
    except ValueError as exc:
        # Locally generated messages contain no source text; model errors stay private.
        audit["reason"] = str(exc)[:160] if not audit["attempted"] else "review validation failed"
    except Exception:
        audit["reason"] = "review unavailable or malformed input"
    return audit
