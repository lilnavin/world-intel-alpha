"""Synthetic paired-quote controls; no claim of general semantic entailment."""
import copy
import json

import pytest

from worldintel.core import grounding_review as g


def pair(claim, quote):
    result = {"briefing": {"brief": [{"headline": "A procedural update", "why_it_matters": "The process continues.",
               "body": claim, "sources": [{"url": "https://example.test/process"}]}]}}
    bundle = {"news": [{"url": "https://example.test/process", "content": quote}]}
    material = g._material(result, bundle)
    card, source = material["cards"][0], material["sources"][0]
    raw = {"cards": [{"card_id": card["card_id"], "prose_sha256": card["prose_sha256"],
           "verdict": "supported", "reason": "The quote supports the claim.", "claims": [
           {"field": "body", "text": claim, "source_id": source["source_id"],
            "source_sha256": source["sha256"], "quote": quote}]}]}
    return result, bundle, material, raw


@pytest.mark.parametrize("claim,quote", [
    ("Morgan initially sought its review.", "Morgan said he would seek their review."),
    ("Officials filed a petition.", "Officials will file the petition."),
    ("Morgan submitted the application.", "Morgan intended to submit an application."),
    ("Morgan requested a hearing.", "Morgan planned to request the hearing."),
    ("Morgan challenged the ruling.", "Morgan would challenge a ruling."),
    ("Morgan appealed the decision.", "Morgan was going to appeal the decision."),
])
def test_only_prospective_quote_derives_rejection_without_mutating_raw(claim, quote):
    _, _, material, raw = pair(claim, quote)
    before = copy.deepcopy(raw)
    rejected = g._validate_integrity(raw, material)
    assert len(rejected) == 1 and rejected[0]["verdict"] == "unsupported"
    assert "prospective" in rejected[0]["reason"]
    assert rejected[0]["claims"] == raw["cards"][0]["claims"]
    assert raw == before
    assert g._correction_material(material, raw)["review"]["cards"] == rejected
    assert g._validate_integrity(json.loads(json.dumps(raw)), material) == rejected
    with pytest.raises(ValueError, match="unsupported"):
        g._validate(raw, material)


@pytest.mark.parametrize("claim,quote", [
    ("Morgan sought review.", "Morgan would seek review. Morgan later sought review."),
    ("Morgan sought review.", "Morgan would seek review. The applicant later sought review."),
    ("Morgan sought review.", "Whenever challenged, Morgan would seek review."),
    ("Morgan sought review.", "Morgan would seek review, but he had sought review."),
    ("Morgan sought review.", "Morgan would seek review if needed."),
    ("Morgan sought review.", "Morgan would not seek review."),
    ("Morgan sought review.", "Morgan did not intend to seek review."),
    ("Nobody sought review.", "Morgan would seek review."),
    ("Neither sought review.", "Morgan would seek review."),
    ("Morgan sought no review.", "Morgan would seek review."),
    ("Morgan sought review.", "Morgan denied he would seek review."),
    ("Morgan sought review.", "Officials doubted Morgan would seek review."),
    ("Morgan sought review.", "Nobody said he would seek review."),
    ("Morgan did not seek review.", "Morgan would seek review."),
    ("Morgan never sought review.", "Morgan would seek review."),
    ("If Morgan sought review, a hearing would follow.", "Morgan would seek review."),
    ("Morgan would have sought review.", "Morgan would seek review."),
    ("Morgan said he sought review.", "Morgan would seek review."),
    ("Morgan sought review.", "Morgan would seek a hearing."),
    ("Morgan sought review.", "Officials sought a hearing. Morgan would seek reconsideration."),
    ("Morgan sought review of the ruling.", "Morgan would seek review of another ruling."),
    ("Morgan sought to overturn the decision.", "Morgan would seek to overturn the decision."),
    ("Morgan appealed.", "Morgan would appeal."),
    ("Morgan had sought review.", "Morgan would seek review."),
])
def test_controls_abstain(claim, quote):
    _, _, material, raw = pair(claim, quote)
    assert g._validate_integrity(raw, material) == []


@pytest.mark.parametrize("extra", ["Morgan never sought review.", "If Morgan sought review, a hearing would follow.",
                                   "Morgan denied that he sought review.", "Morgan would have sought review."])
def test_negated_or_hypothetical_past_is_not_completed_proof(extra):
    _, _, material, raw = pair("Morgan sought review.", "Morgan would seek review. " + extra)
    assert g._validate_integrity(raw, material)


@pytest.mark.parametrize("mutation", [
    lambda r: r["cards"][0].update(claims=[]),
    lambda r: r["cards"][0]["claims"][0].update(source_sha256="0" * 64),
    lambda r: r["cards"][0]["claims"][0].update(quote="Morgan would seek an invented object."),
])
def test_integrity_precedes_temporal_interpretation(mutation):
    _, _, material, raw = pair("Morgan sought review.", "Morgan would seek review.")
    mutation(raw)
    with pytest.raises(ValueError):
        g._validate_integrity(raw, material)


@pytest.mark.parametrize("corrected", [True, False])
def test_initial_supported_temporal_error_uses_single_correction_and_atomic_final_validation(corrected):
    claim, quote = "Morgan initially sought its review.", "Morgan said he would seek their review."
    result, bundle, _, _ = pair(claim, quote)
    before = copy.deepcopy(result)
    calls, raws = [], []
    def reviewer(prompt, *args):
        calls.append("review")
        material = json.loads(prompt.split("\nINPUT_JSON\n", 1)[1])
        text = material["cards"][0]["body"]
        _, _, _, raw = pair(quote if corrected and len(calls) > 1 else claim, quote)
        raw["cards"][0]["prose_sha256"] = material["cards"][0]["prose_sha256"]
        raws.append((raw, copy.deepcopy(raw)))
        assert raw["cards"][0]["claims"][0]["text"] in text
        return raw
    def repair(prompt, *args):
        calls.append("repair")
        material = json.loads(prompt.split("\nINPUT_JSON\n", 1)[1])
        assert material["review"]["cards"][0]["verdict"] == "unsupported"
        card = material["cards"][0]
        return {"cards": [{"card_id": card["card_id"], "prose_sha256": card["prose_sha256"],
                          "headline": "A procedural update", "why_it_matters": "The process continues.",
                          "body": " ".join([quote if corrected else claim] * 20)}]}
    audit = g.review(result, bundle, review_fn=reviewer, repair_fn=repair, deadline=500, clock=lambda: 100)
    assert calls == ["review", "repair", "review"]
    assert audit["status"] == ("accepted" if corrected else "hold")
    assert all(raw == snapshot for raw, snapshot in raws)
    if not corrected:
        assert result == before


def test_captured_evidence_chain_recomputes_derived_rejection_from_supported_raw(tmp_path):
    from model_evidence_fixture import model_evidence
    from test_source_review_evidence import audit
    from tools.verify_minimal_run import validate_model_evidence
    summary = model_evidence(tmp_path, repaired=True, temporal=True)
    root = tmp_path / summary["run_id"]
    initial_folder = root / summary["invocations"][2]["invocation_id"]
    raw_bytes = (initial_folder / "output.bin").read_bytes()
    raw = json.loads(raw_bytes)
    assert raw["cards"][0]["verdict"] == "supported"
    material = json.loads((initial_folder / "stdin.bin").read_text().split("\nINPUT_JSON\n")[1])
    correction_folder = root / summary["invocations"][3]["invocation_id"]
    correction = json.loads((correction_folder / "stdin.bin").read_text().split("\nINPUT_JSON\n")[1])
    assert correction == g._correction_material(material, raw)
    assert correction["review"]["cards"][0]["verdict"] == "unsupported"
    assert validate_model_evidence(root, summary, audit(True)) == []
    assert (initial_folder / "output.bin").read_bytes() == raw_bytes


@pytest.mark.parametrize("tail", ["on Monday", "yesterday", "and received a reply", "of the ruling"])
def test_unparsed_completion_tail_prevents_prospective_only_rejection(tail):
    _, _, material, raw = pair("Morgan sought review.",
                              "Morgan would seek review. Morgan sought the review " + tail + ".")
    assert g._validate_integrity(raw, material) == []


@pytest.mark.parametrize("past", [
    "Morgan never sought review on Monday.",
    "If Morgan sought review yesterday, a hearing would follow.",
    "Morgan denied that he sought review and received a reply.",
    "Morgan sought reconsideration on Monday.",
    "Morgan sought reviewers yesterday.",
])
def test_unparsed_completion_tail_does_not_exempt_negative_or_different_object(past):
    _, _, material, raw = pair("Morgan sought review.", "Morgan would seek review. " + past)
    assert g._validate_integrity(raw, material)


@pytest.mark.parametrize("prefix", ["Each year", "In those days", "Historically", "Earlier"])
def test_comma_context_does_not_turn_ambiguous_would_into_future(prefix):
    _, _, material, raw = pair("Morgan sought review.", prefix + ", Morgan would seek review.")
    assert g._validate_integrity(raw, material) == []


def test_unknown_subject_completion_with_tail_abstains():
    _, _, material, raw = pair("Morgan sought review.",
                              "Morgan would seek review. A senior official sought review on Monday.")
    assert g._validate_integrity(raw, material) == []


@pytest.mark.parametrize("past", ["Morgan hadn't sought review.", "Morgan hadn\u2019t sought review.",
                                  "Morgan didn't seek review.", "Morgan didn\u2019t seek review."])
def test_negated_past_contraction_is_not_possible_completion(past):
    _, _, material, raw = pair("Morgan sought review.", "Morgan would seek review. " + past)
    assert g._validate_integrity(raw, material)
