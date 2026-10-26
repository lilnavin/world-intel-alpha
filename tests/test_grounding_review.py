"""Validate reviewer bindings; injected responses do not prove semantic competence."""
import copy
import hashlib
import json

import pytest


def fixture():
    card = {"headline": "A bridge opened", "why_it_matters": "Traffic can cross.",
            "body": "Officials opened a bridge on Monday.",
            "sources": [{"url": "https://example.test/bridge"}]}
    return ({"briefing": {"brief": [card], "signal": [{"headline": "ignored"}]}},
            {"news": [{"url": "https://example.test/bridge", "title": "Opening",
                       "content": "Officials opened a bridge on Monday. Traffic can cross."}]})


def response(prompt):
    material = json.loads(prompt.split("\nINPUT_JSON\n", 1)[1])
    return {"cards": [{"card_id": c["card_id"], "prose_sha256": c["prose_sha256"],
            "verdict": "supported", "reason": "Source reports the opening.",
            "claims": [{"field": "body", "text": c["body"], "source_id": c["source_ids"][0],
                        "source_sha256": material["sources"][0]["sha256"],
                        "quote": "Officials opened a bridge on Monday."}]} for c in material["cards"]]}


def review(result=None, bundle=None, transform=lambda r: r, **kwargs):
    from worldintel.core import grounding_review
    original_result, original_bundle = fixture()
    calls = []
    def model(prompt, schema, timeout):
        calls.append((prompt, schema, timeout))
        return transform(response(prompt))
    audit = grounding_review.review(result or original_result, bundle or original_bundle,
                                    review_fn=model, deadline=200, clock=lambda: 100, **kwargs)
    return audit, calls


def test_supported_card_requires_bound_prose_and_source_quote():
    audit, calls = review()
    assert audit["status"] == "accepted"
    assert audit["cards_reviewed"] == 1
    assert len(calls) == 1 and calls[0][2] == 100


@pytest.mark.parametrize("mutation", [
    lambda r: r["cards"].clear(),
    lambda r: r["cards"].append(copy.deepcopy(r["cards"][0])),
    lambda r: r["cards"][0].update(card_id="other"),
    lambda r: r["cards"][0].update(prose_sha256="0" * 64),
    lambda r: r["cards"][0].update(verdict="uncertain"),
    lambda r: r["cards"][0].update(verdict="unsupported"),
    lambda r: r["cards"][0].update(reason=""),
    lambda r: r["cards"][0].update(claims=[]),
    lambda r: r["cards"][0]["claims"][0].update(field="signal"),
    lambda r: r["cards"][0]["claims"][0].update(text="invented"),
    lambda r: r["cards"][0]["claims"][0].update(source_id="unknown"),
    lambda r: r["cards"][0]["claims"][0].update(source_sha256="0" * 64),
    lambda r: r["cards"][0]["claims"][0].update(quote="Invented evidence"),
])
def test_bad_binding_or_negative_verdict_holds(mutation):
    def transform(r):
        mutation(r)
        return r
    audit, calls = review(transform=transform)
    assert audit["status"] == "hold"
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["missing", "duplicate", "empty", "oversized", "cards", "sources"])
def test_unsafe_inputs_hold_without_model_call(change):
    result, bundle = fixture()
    if change == "missing":
        bundle["news"] = []
    elif change == "duplicate":
        bundle["news"] *= 2
    elif change == "empty":
        bundle["news"][0]["content"] = ""
    elif change == "oversized":
        bundle["news"][0]["content"] = "é" * 70000
    elif change == "cards":
        result["briefing"]["brief"] *= 25
    else:
        bundle["news"] = [{"url": f"https://example.test/{n}", "content": "text"} for n in range(49)]
        result["briefing"]["brief"][0]["sources"] = [{"url": n["url"]} for n in bundle["news"]]
    audit, calls = review(result, bundle)
    assert audit["status"] == "hold" and calls == []


def test_timeout_budget_and_exceptions_never_retry():
    from worldintel.core import grounding_review
    result, bundle = fixture()
    calls = []
    def fail(*args):
        calls.append(args)
        raise TimeoutError("private source text")
    assert grounding_review.review(result, bundle, review_fn=fail, deadline=100,
                                   clock=lambda: 100)["status"] == "hold"
    assert calls == []
    audit = grounding_review.review(result, bundle, review_fn=fail, deadline=500, clock=lambda: 100)
    assert audit["status"] == "hold" and len(calls) == 1 and calls[0][2] == 120
    assert "private source text" not in json.dumps(audit)


def test_source_review_setting_rejects_non_boolean(synthetic_config):
    from worldintel import config
    raw = synthetic_config.raw
    raw.setdefault("analysis", {})["source_review_enabled"] = "true"
    with pytest.raises(config.ConfigError, match="source_review_enabled"):
        config.Config(raw, synthetic_config.source_path)
