"""Bounded correction enforcement; callbacks do not establish model competence."""
import copy
import json
from pathlib import Path
import subprocess

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


@pytest.mark.parametrize("disposition", ["supported-first", "supported-after-correction", "untouched-uncertain"])
def test_three_finance_corrections_require_final_acceptance_of_untouched_card(
        monkeypatch, tmp_path, disposition):
    """Fictional callbacks test scope/atomicity, including a mistaken final judgment."""
    from worldintel.core import codex_engine as engine
    from worldintel.core.model_evidence import Collector
    cards, news, corrected = [], [], {}
    for index, company in enumerate(("Larch Works", "Mica Tools", "Cedar Motors", "Flint Parts")):
        body = (
            f"{company} plans to issue $40 million of notes in November at a fixed 5% rate. "
            "Its board separately approved an equipment project, without identifying its funding. "
            f"Management described {company} as a regional leader; the report gave no market ranking. "
            "The company also has a floating-rate loan. The loan resets against a benchmark, "
            "while the proposed notes would carry the stated fixed rate if issued on those terms. "
            "Management has not announced a completed sale or a final allocation of proceeds. "
            "The board's approval allows planning to continue while procurement remains pending. "
            "If the loan benchmark rises, interest costs on that floating-rate borrowing could increase. "
            "Watch whether the next loan reset changes its interest cost."
        )
        assert 100 <= len(body.split()) <= 200
        url = f"https://example.test/fictional-financing/{index}"
        card = {"headline": f"{company} plans notes", "why_it_matters": "Financing terms remain prospective.",
                "body": body, "sources": [{"url": url}]}
        corrected[f"finance.brief:{index}"] = {f: card[f] for f in g.FIELDS}
        cards.append(card)
        news.append({"url": url, "title": card["headline"], "content": body})
    if disposition != "supported-first":
        cards[0]["body"] = cards[0]["body"].replace(
            "without identifying its funding", "funded using the notes")
        cards[1]["body"] = cards[1]["body"].replace(
            "Management described Mica Tools as a regional leader; the report gave no market ranking.",
            "Mica Tools is the largest regional supplier.")
        cards[2]["body"] = cards[2]["body"].replace("plans to issue", "issued")
    result = {"briefing": {"finance": {"brief": cards}}, "kb_deltas": {"unreviewed": "proposal"}}
    original_bytes = json.dumps(result, ensure_ascii=False).encode("utf-8")
    original = copy.deepcopy(result)
    collector = Collector(tmp_path / "private", "fictional-finance")
    reviewed, raw_outputs, parsed_outputs, calls = [], [], [], []
    monkeypatch.setattr(engine, "_resolve_codex", lambda: "codex")

    def execute(args, *, stdin, **kwargs):
        prompt = stdin.read().decode("utf-8").replace("\r\n", "\n")
        material = json.loads(prompt.split("\nINPUT_JSON\n", 1)[1])
        reviewed.append(material)
        sources = {s["source_id"]: s for s in material["sources"]}
        records = []
        for index, card in enumerate(material["cards"]):
            verdict = "supported"
            if len(reviewed) == 1 and disposition != "supported-first" and index < 3:
                verdict = "unsupported"
            elif len(reviewed) == 2 and disposition == "untouched-uncertain" and index == 3:
                verdict = "uncertain"
            source = sources[card["source_ids"][0]]
            reason = ("Conditional financing mechanism lacks support." if verdict == "uncertain"
                      else "Injected semantic judgment for orchestration only.")
            records.append({"card_id": card["card_id"], "prose_sha256": card["prose_sha256"],
                            "verdict": verdict, "reason": reason,
                            "claims": [{"field": "body", "text": card["body"],
                                        "source_id": source["source_id"], "source_sha256": source["sha256"],
                                        "quote": source["content"]}]})
        raw = (json.dumps({"cards": records}, indent=2) + "\n").encode("utf-8")
        raw_outputs.append(raw)
        Path(args[args.index("-o") + 1]).write_bytes(raw)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(engine, "_execute", execute)

    def reviewer(prompt, schema, timeout):
        calls.append("review")
        out = engine.run_schema(prompt, schema, timeout=timeout, evidence_sink=collector,
                                pass_name="source-review" if len(calls) == 1 else "source-review-final")
        parsed_outputs.append(out)
        return out

    def repair(prompt, schema, timeout):
        calls.append("repair")
        material = json.loads(prompt.split("\nINPUT_JSON\n", 1)[1])
        assert [c["card_id"] for c in material["cards"]] == [f"finance.brief:{i}" for i in range(3)]
        assert len(material["sources"]) == 3
        return {"cards": [{"card_id": c["card_id"], "prose_sha256": c["prose_sha256"],
                           **corrected[c["card_id"]]} for c in material["cards"]]}

    audit = g.review(result, {"news": news}, review_fn=reviewer, repair_fn=repair,
                     deadline=500, clock=lambda: 100)
    expected_calls = ["review"] if disposition == "supported-first" else ["review", "repair", "review"]
    assert calls == expected_calls
    assert audit["review_calls"] == len(reviewed) == (1 if disposition == "supported-first" else 2)
    assert audit["repair_attempted"] == (disposition != "supported-first")
    assert [c["card_id"] for c in reviewed[-1]["cards"]] == [f"finance.brief:{i}" for i in range(4)]
    assert result["briefing"]["finance"]["brief"][3] == original["briefing"]["finance"]["brief"][3]
    if disposition == "untouched-uncertain":
        assert audit["status"] == "hold" and audit["corrected_cards"] == 0
        assert json.dumps(result, ensure_ascii=False).encode("utf-8") == original_bytes
        assert all(c["verdict"] == "supported" for c in parsed_outputs[-1]["cards"][:3])
        assert parsed_outputs[-1]["cards"][3]["verdict"] == "uncertain"
    else:
        assert audit["status"] == "accepted" and audit["cards_reviewed"] == 4
        assert audit["corrected_cards"] == (0 if disposition == "supported-first" else 3)
        for index, card in enumerate(result["briefing"]["finance"]["brief"]):
            assert {f: card[f] for f in g.FIELDS} == corrected[f"finance.brief:{index}"]
            assert card["sources"] == original["briefing"]["finance"]["brief"][index]["sources"]
    if len(reviewed) == 2:
        for index, card in enumerate(reviewed[-1]["cards"][:3]):
            assert card["body"] == corrected[f"finance.brief:{index}"]["body"]
            assert card["prose_sha256"] != reviewed[0]["cards"][index]["prose_sha256"]
        assert reviewed[-1]["cards"][3] == reviewed[0]["cards"][3]
    summary = collector.summary()
    assert summary["complete"] is True
    assert len(summary["invocations"]) == len(raw_outputs) == len(parsed_outputs) == len(reviewed)
    for entry, raw, parsed in zip(summary["invocations"], raw_outputs, parsed_outputs):
        assert (collector.root / entry["invocation_id"] / "output.bin").read_bytes() == raw
        assert parsed == json.loads(raw)  # Validation never rewrites even a mistaken reviewer verdict.
