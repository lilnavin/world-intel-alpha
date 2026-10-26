"""Bounded correction enforcement; callbacks do not establish model competence."""
import copy
import json

import pytest
from test_grounding_review import fixture, response
from worldintel.core import grounding_review as g

BODY = " ".join(["Officials opened a bridge on Monday."] * 18)


def scenario(*, mutation=None, final_verdict="supported", clock=lambda: 100, deadline=500):
    result, bundle = fixture()
    original = copy.deepcopy(result)
    calls = []
    def reviewer(prompt, schema, timeout):
        calls.append(("review", timeout))
        out = response(prompt)
        out["cards"][0]["verdict"] = "uncertain" if len(calls) == 1 else final_verdict
        return out
    def repair(prompt, schema, timeout):
        calls.append(("repair", timeout))
        payload = json.loads(prompt.split("\nINPUT_JSON\n", 1)[1])
        patches = {"cards": [{"card_id": c["card_id"], "prose_sha256": c["prose_sha256"],
                   "headline": "Bridge opens", "why_it_matters": "Traffic can cross.", "body": BODY}
                  for c in payload["cards"]]}
        if mutation:
            mutation(patches)
        return patches
    audit = g.review(result, bundle, review_fn=reviewer, repair_fn=repair, deadline=deadline, clock=clock)
    return audit, result, original, calls


def test_one_correction_then_all_card_review_adopts_only_prose():
    audit, result, original, calls = scenario()
    assert audit["status"] == "accepted"
    assert audit["repair_attempted"] is True and audit["review_calls"] == 2 and audit["corrected_cards"] == 1
    assert [c[0] for c in calls] == ["review", "repair", "review"]
    assert all(0 < c[1] <= 120 for c in calls)
    assert result["briefing"]["brief"][0]["body"] == BODY
    assert result["briefing"]["brief"][0]["sources"] == original["briefing"]["brief"][0]["sources"]


@pytest.mark.parametrize("mutation", [
    lambda p: p["cards"][0].update(card_id="unknown"),
    lambda p: p["cards"][0].update(prose_sha256="f" * 64),
    lambda p: p["cards"].clear(),
    lambda p: p["cards"].append(copy.deepcopy(p["cards"][0])),
    lambda p: p["cards"][0].update(sources=[{"url": "https://evil.test"}]),
    lambda p: p["cards"][0].update(headline=" "),
    lambda p: p["cards"][0].update(why_it_matters=""),
    lambda p: p["cards"][0].update(body="short"),
    lambda p: p["cards"][0].update(body="word " * 201),
    lambda p: p["cards"][0].update(body="The bundle only gives a headline. " + BODY),
])
def test_invalid_patch_holds_without_mutation_or_final_call(mutation):
    audit, result, original, calls = scenario(mutation=mutation)
    assert audit["status"] == "hold" and result == original
    assert audit["repair_attempted"] is True and audit["review_calls"] == 1 and audit["corrected_cards"] == 0
    assert len(calls) == 2


def test_rejected_final_review_holds_original_without_retry():
    audit, result, original, calls = scenario(final_verdict="uncertain")
    assert audit["status"] == "hold" and result == original and len(calls) == 3
    assert audit["review_calls"] == 2 and audit["corrected_cards"] == 0


def test_supported_first_never_corrects():
    result, bundle = fixture()
    audit = g.review(result, bundle, review_fn=lambda p, *a: response(p),
                     repair_fn=lambda *a: pytest.fail("accepted cards must not be corrected"), deadline=500, clock=lambda: 100)
    assert audit["status"] == "accepted"
    assert (audit["repair_attempted"], audit["review_calls"], audit["corrected_cards"]) == (False, 1, 0)


def test_every_record_integrity_checked_before_negative_feedback_triggers_repair():
    result, bundle = fixture()
    result["briefing"]["brief"].append(copy.deepcopy(result["briefing"]["brief"][0]))
    def reviewer(prompt, *args):
        out = response(prompt)
        out["cards"][0]["verdict"] = "uncertain"
        out["cards"][1]["claims"][0]["quote"] = "forged"
        return out
    audit = g.review(result, bundle, review_fn=reviewer,
                     repair_fn=lambda *a: pytest.fail("invalid evidence must not correct"), deadline=500, clock=lambda: 100)
    assert audit["status"] == "hold" and audit["repair_attempted"] is False


@pytest.mark.parametrize("phase", ["initial", "repair", "final"])
def test_timeout_at_any_phase_never_adopts_or_retries(phase):
    result, bundle = fixture()
    before = copy.deepcopy(result)
    calls = []
    def reviewer(prompt, *args):
        current = "initial" if not calls else "final"
        calls.append(current)
        if current == phase:
            raise TimeoutError()
        out = response(prompt)
        out["cards"][0]["verdict"] = "uncertain" if current == "initial" else "supported"
        return out
    def repair(prompt, *args):
        calls.append("repair")
        if phase == "repair":
            raise TimeoutError()
        c = json.loads(prompt.split("\nINPUT_JSON\n")[1])["cards"][0]
        return {"cards": [{"card_id": c["card_id"], "prose_sha256": c["prose_sha256"],
                           "headline": "Bridge opens", "why_it_matters": "Traffic can cross.", "body": BODY}]}
    audit = g.review(result, bundle, review_fn=reviewer, repair_fn=repair, deadline=500, clock=lambda: 100)
    assert audit["status"] == "hold" and result == before and len(calls) <= 3


def test_original_deadline_applies_to_each_call_without_extension():
    ticks = iter([100, 101, 103, 104, 105, 106])
    audit, result, original, calls = scenario(clock=lambda: next(ticks), deadline=105)
    assert audit["status"] == "hold" and result == original
    assert calls == [("review", 5), ("repair", 2)]


@pytest.mark.parametrize("review_enabled,repair_enabled", [(False, True), (True, "true")])
def test_repair_config_requires_boolean_and_enabled_review(synthetic_config, review_enabled, repair_enabled):
    from worldintel import config
    raw = synthetic_config.raw
    raw.setdefault("analysis", {}).update(source_review_enabled=review_enabled, source_review_repair_enabled=repair_enabled)
    with pytest.raises(config.ConfigError, match="source_review_repair_enabled"):
        config.Config(raw, synthetic_config.source_path)


def test_only_rejected_card_changes_and_final_review_includes_supported_cards():
    result, bundle = fixture()
    result['briefing']['brief'].append(copy.deepcopy(result['briefing']['brief'][0]))
    before = copy.deepcopy(result)
    reviewed = []
    def reviewer(prompt, *args):
        material = json.loads(prompt.split('\nINPUT_JSON\n', 1)[1])
        reviewed.append(material)
        out = response(prompt)
        if len(reviewed) == 1:
            out['cards'][1]['verdict'] = 'unsupported'
        return out
    def repair(prompt, *args):
        material = json.loads(prompt.split('\nINPUT_JSON\n', 1)[1])
        assert [c['card_id'] for c in material['cards']] == ['brief:1']
        c = material['cards'][0]
        return {'cards': [{'card_id': c['card_id'], 'prose_sha256': c['prose_sha256'],
                          'headline': 'Corrected', 'why_it_matters': 'Traffic can cross.', 'body': BODY}]}
    audit = g.review(result, bundle, review_fn=reviewer, repair_fn=repair, deadline=500, clock=lambda: 100)
    assert audit['status'] == 'accepted' and len(reviewed) == 2
    assert len(reviewed[1]['cards']) == 2
    assert result['briefing']['brief'][0] == before['briefing']['brief'][0]
    assert result['briefing']['brief'][1]['body'] == BODY


@pytest.mark.parametrize('failure', ['missing-source', 'budget'])
def test_missing_source_or_no_budget_never_corrects(failure):
    result, bundle = fixture()
    if failure == 'missing-source':
        bundle['news'] = []
    audit = g.review(result, bundle, review_fn=lambda *a: pytest.fail('must not review'),
                     repair_fn=lambda *a: pytest.fail('must not correct'), deadline=100, clock=lambda: 100)
    assert audit['status'] == 'hold'
    assert (audit['attempted'], audit['repair_attempted'], audit['review_calls']) == (False, False, 0)
