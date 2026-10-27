"""Bounded independent source review of final full story cards.

Bindings prove which prose and source quotes were reviewed. They cannot prove
that the model enumerated every factual claim or that a quote entails a claim.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time

from worldintel import config, data
from worldintel.core.quality_gate import _citation_identity, META_RE
from worldintel.core.coverage_gate import _clean
from worldintel.core.enrich import substantive_content

SCOPE = "final full story cards: headline, why_it_matters, body; not whole-brief verification"
FIELDS = ("headline", "why_it_matters", "body")


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _material(result, bundle, *, cfg=None):
    briefing = result["briefing"]
    sections = [(key, briefing.get(key, [])) for key in ("brief", *(cfg if cfg is not None else config.get()).local_desk_ids)]
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


def _preflight(result, bundle, *, cfg=None):
    """Stage only uniquely selected empty extras; preserve exact citation objects."""
    candidate, changes = copy.deepcopy(result), []
    by_url = {}
    for index, source in enumerate(bundle.get("news", [])):
        if isinstance(source, dict) and isinstance(source.get("url"), str):
            by_url.setdefault(_citation_identity(source["url"]), []).append((index, source))
    briefing = candidate["briefing"]
    sections = [(key, briefing.get(key, [])) for key in ("brief", *(cfg if cfg is not None else config.get()).local_desk_ids)]
    sections.append(("finance.brief", briefing.get("finance", {}).get("brief", [])))
    for section, cards in sections:
        if not isinstance(cards, list):
            raise ValueError("invalid card section")
        for index, card in enumerate(cards):
            citations = card.get("sources") if isinstance(card, dict) else None
            if not isinstance(citations, list) or not citations:
                raise ValueError("missing cited source")
            retained, removed, anchor, substantive = [], [], False, True
            for citation in citations:
                url = citation.get("url") if isinstance(citation, dict) else None
                matches = by_url.get(_citation_identity(url), []) if isinstance(url, str) else []
                if len(matches) != 1:
                    raise ValueError("missing or ambiguous cited selected source")
                source_index, source = matches[0]
                text = source.get("content")
                if text is None or (isinstance(text, str) and not text.strip()):
                    removed.append({"source_id": f"source:{source_index}", "selected_source": copy.deepcopy(source)})
                else:
                    if not isinstance(text, str):
                        raise ValueError("malformed cited full text")
                    retained.append(citation)
                    substantive &= substantive_content(text)
                    tier = source.get("tier")
                    anchor |= type(tier) is int and tier in (1, 2) and substantive_content(text)
            if removed:
                if not substantive:
                    raise ValueError("citation cleanup retains inadequate full text")
                if not anchor:
                    raise ValueError("citation cleanup lacks substantive tier-1/2 anchor")
                prose = {f: card[f] for f in FIELDS}
                changes.append({"card_id": f"{section}:{index}",
                                "prose_sha256": _digest(json.dumps(prose, ensure_ascii=False, sort_keys=True)),
                                "original_sources": copy.deepcopy(citations), "removed": removed,
                                "retained_sources": copy.deepcopy(retained)})
                card["sources"] = retained
    audit = {"cards": changes, "sha256": _digest(json.dumps(changes, ensure_ascii=False, sort_keys=True))} if changes else None
    return candidate, audit


def validate_cleanup_audit(material, audit):
    """Bind optional captured cleanup to the independently retained run status."""
    cleanup = material.get("citation_preflight")
    if cleanup is None:
        if audit.get("citation_preflight_sha256") is not None or audit.get("citation_preflight_cards", 0) != 0:
            raise ValueError("missing status-bound citation cleanup")
        return
    if (not isinstance(cleanup, dict) or set(cleanup) != {"cards", "sha256"}
            or not isinstance(cleanup["cards"], list) or not cleanup["cards"]
            or cleanup["sha256"] != _digest(json.dumps(cleanup["cards"], ensure_ascii=False, sort_keys=True))
            or audit.get("citation_preflight_sha256") != cleanup["sha256"]
            or type(audit.get("citation_preflight_cards")) is not int
            or audit["citation_preflight_cards"] != len(cleanup["cards"])):
        raise ValueError("citation cleanup status binding mismatch")


def validate_cleanup_evidence(material, rawcaptures, *, cfg=None):
    """Recompute cleanup from captured selected input and exact generation cards.

    Non-exact rewrite/coverage derivations are deliberately refused. Byte/manifest
    integrity of rawcaptures must be checked by the caller before this function.
    cfg must describe the captured run; omitted cfg uses the existing context.
    """
    if "citation_preflight" not in material:
        return
    try:
        world = [r for r in rawcaptures if r["pass_name"] == "world"]
        finance = [r for r in rawcaptures if r["pass_name"] == "finance"]
        if len(world) != 1 or len(finance) != 1:
            raise ValueError("cleanup generation capture missing or ambiguous")
        marker = "\n## TODAY (BUNDLE)\n"
        prompt = world[0]["prompt"].replace("\r\n", "\n")
        if prompt.count(marker) != 1:
            raise ValueError("cleanup selected input ambiguous")
        bundle = json.loads(prompt.split(marker)[1])
        raw = {"briefing": copy.deepcopy(world[0]["output"]["briefing"])}
        for key in ("finance", "post_earnings"):
            if key in finance[0]["output"]["briefing"]:
                raw["briefing"][key] = copy.deepcopy(finance[0]["output"]["briefing"][key])
        candidate, preflight = _preflight(raw, bundle, cfg=cfg)
        expected = _material(candidate, bundle, cfg=cfg)
        if preflight is None:
            raise ValueError("cleanup absent from generation")
        expected["citation_preflight"] = preflight
        if material != expected:
            raise ValueError("cleanup does not bind exact captured generation and selected input")
    except (KeyError, TypeError, IndexError, AttributeError) as exc:
        raise ValueError("malformed cleanup evidence") from exc


# This is negative evidence for a narrow simple-clause grammar, not an entailment
# parser. Unknown subjects, objects, qualifications and tense forms abstain.
_ACTION_FORMS = {"seek": "sought", "file": "filed", "submit": "submitted",
                 "request": "requested", "challenge": "challenged", "appeal": "appealed"}
_SUBJECT = r"(?:[A-Z][a-z]+(?: [A-Z][a-z]+){0,3}|he|she|they|we|it)"
_ADVERB = r"(?:(?:initially|later|already|formally|previously|then) )?"
_CONDITIONAL = re.compile(r"\b(?:if|unless|whether|would|could|might|may|should)\b", re.I)
_OBJECT_GRAMMAR = {"no", "not", "never", "to", "of", "for", "against", "by", "with", "on", "in",
                   "at", "from", "after", "before", "as", "and", "or", "but", "that", "which", "who",
                   "if", "unless", "when", "while", "because", "without", "only"}


def _action_object(tail):
    # Require the whole bounded direct object, rather than sharing just a noun
    # head (e.g. a qualified "review of X" is deliberately outside this grammar).
    words = tail.strip().lower().split()
    if words and words[0] in {"a", "an", "the", "his", "her", "its", "their", "our", "your", "my"}:
        words = words[1:]
    if (not 1 <= len(words) <= 5 or any(not re.fullmatch(r"[a-z]+", w)
            or w in _OBJECT_GRAMMAR for w in words)):
        return None
    return tuple(words)


def _completed_actions(text, *, quote=False):
    actions = set()
    for sentence in re.split(r"[.!?;]", text):
        # Conditional scope is deliberately not inferred across clauses.
        if (_CONDITIONAL.search(sentence) if not quote else
                re.search(r"\b(?:if|unless|whether)\b", sentence, re.I)):
            continue
        for clause in re.split(r",|\bbut\b", sentence):
            clause = clause.strip()
            if (_CONDITIONAL.search(clause) or re.match(r"(?:Nobody|Neither|Nothing|None|No)\b", clause)):
                continue
            for base, past in _ACTION_FORMS.items():
                perfect = r"(?:had )?" if quote else ""
                match = re.fullmatch(_SUBJECT + " " + _ADVERB + perfect + past + r" (.+)", clause)
                if match:
                    obj = _action_object(match[1])
                    if obj:
                        actions.add((base, obj))
    return actions


def _possible_quote_completion(text, candidates):
    # Unknown subject/reporting grammar is not positive completion proof. It
    # nevertheless prevents a confident "only prospective" rejection when the
    # same past action/object is present without an explicit negative/modal scope.
    possible = set()
    for sentence in re.split(r"[.!?;]", text):
        if re.search(r"\b(?:if|unless|whether)\b", sentence, re.I):
            continue
        for clause in re.split(r",|\bbut\b", sentence):
            if (_CONDITIONAL.search(clause) or re.search(r"n['\u2019]t\b", clause, re.I) or re.search(
                    r"\b(?:not|never|no|nobody|neither|nothing|none|deny|denies|denied)\b", clause, re.I)):
                continue
            for base, past in _ACTION_FORMS.items():
                match = re.search(r"\b" + past + r" (.+)$", clause)
                if match:
                    words = match[1].strip().lower().split()
                    if words and words[0] in {"a", "an", "the", "his", "her", "its", "their", "our", "your", "my"}:
                        words = words[1:]
                    for action, obj in candidates:
                        # A known whole object prefix followed by unparsed
                        # qualification/time/coordination is enough to abstain;
                        # it is not a claim that the rest proves completion.
                        if action == base and tuple(words[:len(obj)]) == obj:
                            possible.add((base, obj))
    return possible


def _temporal_contradiction(claim, quote):
    completed = _completed_actions(claim)
    if not completed:
        return False
    prospective = set()
    for sentence in re.split(r"[.!?;]", quote):
        if re.search(r"\b(?:if|unless|whether|when|whenever|not|never|no)\b", sentence, re.I):
            continue
        for clause in re.split(r",|\bbut\b|\band (?=said\b)", sentence):
            clause = clause.strip()
            if re.match(r"(?:Nobody|Neither|Nothing|None|No)\b", clause):
                continue
            # A bare "would" clause loses possible habitual context when
            # detached from another clause. Only explicit reported intention
            # remains eligible in that case; do not maintain a time-word list.
            if (clause != sentence.strip()
                    and re.match(_SUBJECT + r" would\b", clause)):
                continue
            for base in _ACTION_FORMS:
                # Bounded simple subjects or explicit reported intention;
                # arbitrary embedded verbs ("denied he would") abstain.
                marker = r"(?:will|would|plans? to|planned to|intends? to|intended to|(?:is|was|are|were) going to)"
                subject = r"(?:" + _SUBJECT + r" |(?:" + _SUBJECT + r" )?said (?:he|she|they|we|it) )"
                match = re.fullmatch(subject + marker + " " + base + r" (.+)", clause)
                if match:
                    obj = _action_object(match[1])
                    if obj:
                        prospective.add((base, obj))
    return bool((completed & prospective) - _completed_actions(quote, quote=True)
                - _possible_quote_completion(quote, completed & prospective))


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

    # Interpret only after every original record has passed all binding checks.
    # Return derived feedback; captured model responses remain byte-faithful.
    rejected = []
    for record in response["cards"]:
        temporal = any(_temporal_contradiction(c["text"], c["quote"]) for c in record["claims"])
        if temporal:
            derived = copy.deepcopy(record)
            derived["verdict"] = "unsupported"
            derived["reason"] = (record["reason"] + " Temporal evidence mismatch: an affirmative completed action "
                                 "is paired with only prospective/intended evidence for the same action and object.")
            rejected.append(derived)
        elif record["verdict"] != "supported":
            rejected.append(record)
    return rejected


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
             "corrected_cards": 0, "citation_preflight_sha256": None, "citation_preflight_cards": 0}
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
        candidate, preflight = _preflight(result, bundle)
        material = _material(candidate, bundle)
        if preflight is not None:
            material["citation_preflight"] = preflight
            audit.update(citation_preflight_sha256=preflight["sha256"], citation_preflight_cards=len(preflight["cards"]))
        response = call("source-review", material, review_fn)
        rejected = _validate_integrity(response, material)
        if rejected:
            if repair_fn is None:
                raise ValueError("unsupported or uncertain card")
            patches = call("source-correction", _correction_material(material, response), repair_fn)
            candidate = _apply_corrections(candidate, material, rejected, patches)
            final_material = _material(candidate, bundle)
            if preflight is not None:
                final_material["citation_preflight"] = preflight
            _validate(call("source-review", final_material, review_fn), final_material)
            audit["corrected_cards"] = len(rejected)
        # Adopt all staged prose/citations only after every card is accepted.
        for card in material["cards"]:
            cid = card["card_id"]
            _card(result, cid).update({f: _card(candidate, cid)[f] for f in (*FIELDS, "sources")})
        audit.update(status="accepted", cards_reviewed=len(material["cards"]))
    except ValueError as exc:
        # Locally generated messages contain no source text; model errors stay private.
        audit["reason"] = str(exc)[:160] if not audit["attempted"] else "review validation failed"
    except Exception:
        audit["reason"] = "review unavailable or malformed input"
    return audit
