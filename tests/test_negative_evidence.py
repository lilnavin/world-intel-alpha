"""Missing quotations are bound negative feedback, never positive evidence."""
import copy
import hashlib
import json

import pytest

from test_grounding_review import fixture, response
from test_grounding_correction import BODY
from worldintel.core import grounding_review as g


def bound(verdict="unsupported"):
    result, bundle = fixture()
    material = g._material(result, bundle)
    raw = response(g._prompt("source-review", material))
    raw["cards"][0]["verdict"] = verdict
    raw["cards"][0]["claims"][0]["quote"] = ""
    return material, raw


@pytest.mark.parametrize("verdict", ["unsupported", "uncertain"])
def test_exact_empty_quote_is_immutable_rejected_feedback(verdict):
    material, raw = bound(verdict)
    before = copy.deepcopy(raw)
    assert g._validate_integrity(raw, material) == raw["cards"]
    assert g._correction_material(material, raw)["review"]["cards"] == raw["cards"]
    with pytest.raises(ValueError, match="unsupported or uncertain"):
        g._validate(raw, material)
    assert raw == before


@pytest.mark.parametrize("verdict", ["supported", "unsupported", "uncertain"])
@pytest.mark.parametrize("mutation", [
    lambda c: c.pop("quote"),
    lambda c: c.update(quote=" "),
    lambda c: c.update(quote="\t\n"),
    lambda c: c.update(quote=None),
    lambda c: c.update(quote=[]),
    lambda c: c.update(quote=0),
    lambda c: c.update(quote="Invented quotation."),
    lambda c: c.update(source_sha256="0" * 64),
    lambda c: c.update(source_id="source:99"),
    lambda c: c.update(field="other"),
    lambda c: c.update(text="Unbound claim."),
])
def test_empty_negative_feedback_preserves_all_other_integrity_checks(verdict, mutation):
    material, raw = bound(verdict)
    mutation(raw["cards"][0]["claims"][0])
    with pytest.raises(ValueError):
        g._validate_integrity(raw, material)


def test_supported_empty_quote_fails_before_temporal_derived_rejection():
    from test_temporal_grounding import pair
    _, _, material, raw = pair("Morgan sought review.", "Morgan would seek review.")
    raw["cards"][0]["claims"].append({**raw["cards"][0]["claims"][0], "quote": ""})
    with pytest.raises(ValueError, match="invalid source hash or verbatim quote"):
        g._validate_integrity(raw, material)


@pytest.mark.parametrize("verdict", ["unsupported", "uncertain"])
def test_empty_negative_quote_coexists_with_bound_temporal_rejection(verdict):
    from test_temporal_grounding import pair
    _, _, material, raw = pair("Morgan sought review.", "Morgan would seek review.")
    raw["cards"][0]["verdict"] = verdict
    raw["cards"][0]["claims"].append({**raw["cards"][0]["claims"][0], "quote": ""})
    before = copy.deepcopy(raw)
    rejected = g._validate_integrity(raw, material)
    assert rejected[0]["verdict"] == "unsupported"
    assert "Temporal evidence mismatch" in rejected[0]["reason"]
    assert raw == before


@pytest.mark.parametrize("initial", ["unsupported", "uncertain"])
@pytest.mark.parametrize("final", ["literal", "supported-empty", "unsupported-empty", "uncertain-empty"])
def test_negative_empty_quote_reaches_one_correction_with_atomic_final_gate(initial, final):
    result, bundle = fixture()
    result["briefing"]["brief"].append(copy.deepcopy(result["briefing"]["brief"][0]))
    before = copy.deepcopy(result)
    calls, raws = [], []
    def reviewer(prompt, *args):
        calls.append("review")
        out = response(prompt)
        assert len(out["cards"]) == 2
        if len(calls) == 1:
            out["cards"][0]["verdict"] = initial
            out["cards"][0]["claims"][0]["quote"] = ""
        elif final != "literal":
            out["cards"][1]["verdict"] = final.split("-")[0]
            out["cards"][1]["claims"][0]["quote"] = ""
        raws.append((out, copy.deepcopy(out)))
        return out
    def repair(prompt, *args):
        calls.append("repair")
        payload = json.loads(prompt.split("\nINPUT_JSON\n", 1)[1])
        assert [c["card_id"] for c in payload["cards"]] == ["brief:0"]
        assert payload["review"]["cards"][0]["claims"][0]["quote"] == ""
        c = payload["cards"][0]
        return {"cards": [{"card_id": c["card_id"], "prose_sha256": c["prose_sha256"],
                           "headline": "Bridge opens", "why_it_matters": "Traffic can cross.", "body": BODY}]}
    audit = g.review(result, bundle, review_fn=reviewer, repair_fn=repair, deadline=500, clock=lambda: 100)
    assert calls == ["review", "repair", "review"]
    assert audit["status"] == ("accepted" if final == "literal" else "hold")
    assert audit["review_calls"] == 2 and audit["repair_attempted"] is True
    assert all(raw == snapshot for raw, snapshot in raws)
    if final == "literal":
        assert result["briefing"]["brief"][0]["body"] == BODY
        assert result["briefing"]["brief"][1] == before["briefing"]["brief"][1]
    else:
        assert result == before and audit["corrected_cards"] == 0


@pytest.mark.parametrize("final", ["literal", "supported-empty", "unsupported-empty", "uncertain-empty"])
def test_captured_evidence_recomputes_negative_feedback_and_final_gate(tmp_path, final):
    from model_evidence_fixture import model_evidence
    from test_source_review_evidence import audit
    from tools.verify_minimal_run import validate_model_evidence
    summary = model_evidence(tmp_path, repaired=True, negative=True)
    root = tmp_path / summary["run_id"]
    initial_folder = root / summary["invocations"][2]["invocation_id"]
    initial_bytes = (initial_folder / "output.bin").read_bytes()
    if final != "literal":
        record = summary["invocations"][4]
        folder = root / record["invocation_id"]
        raw = json.loads((folder / "output.bin").read_bytes())
        raw["cards"][0]["verdict"] = final.split("-")[0]
        raw["cards"][0]["claims"][0]["quote"] = ""
        blob = json.dumps(raw).encode()
        (folder / "output.bin").write_bytes(blob)
        manifest = json.loads((folder / "manifest.json").read_bytes())
        manifest["files"]["output.bin"] = {"bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}
        manifest_bytes = json.dumps(manifest).encode()
        (folder / "manifest.json").write_bytes(manifest_bytes)
        record["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    errors = validate_model_evidence(root, summary, audit(True))
    assert (errors == []) == (final == "literal")
    assert (initial_folder / "output.bin").read_bytes() == initial_bytes
