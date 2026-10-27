"""Model KB proposals remain evidence, outside accepted card review scope."""
import hashlib
import json
import sys
from unittest.mock import Mock

import pytest

from test_dry_isolation import HTML, TODAY, offline, snapshot
from test_grounding_runtime import enable
from worldintel import run


POISON = {
    "overview_replace": "Unsupported overview assertion: café certainty.",
    "threads_upsert": [{"slug": "existing", "summary": "Unsupported thread certainty.",
                        "timeline_add": [{"date": TODAY, "note": "Unsupported timeline."}]}],
}


def metadata(deltas):
    return {"status": "held_unreviewed" if deltas else "none", "proposed": bool(deltas),
            "applied": False,
            "sha256": hashlib.sha256(json.dumps(deltas, ensure_ascii=False, sort_keys=True,
                                                separators=(",", ":")).encode("utf-8")).hexdigest()
            if deltas else None}


def prepare(offline, monkeypatch, *, review=True, deltas=POISON, status="accepted"):
    root, media, fetch, analysis, renderer, _ = offline
    if review:
        enable()
    analysis.return_value["world"]["kb_deltas"] = deltas
    monkeypatch.setattr(run.grounding_review, "review", lambda *a, **k: {
        "status": status, "scope": "cards", "cards_reviewed": 1})
    writer = Mock(wraps=run.kb.apply_deltas)
    monkeypatch.setattr(run.kb, "apply_deltas", writer)
    return root, media, analysis, renderer, writer


def test_accepted_cards_do_not_apply_poisoned_kb_but_retain_proposals(offline, monkeypatch, capsys):
    from test_grounding_review import fixture, response
    review = run.grounding_review.review
    root, media, analysis, renderer, writer = prepare(offline, monkeypatch)
    card_result, source_bundle = fixture()
    card = card_result["briefing"]["brief"][0]
    analysis.return_value["world"]["briefing"] = {"brief": [card]}
    monkeypatch.setattr(run.grounding_review, "review", lambda result, bundle, **kwargs:
                        review(result, source_bundle, review_fn=lambda prompt, *a: response(prompt),
                               deadline=200, clock=lambda: 100))
    run.kb.ensure_kb(str(root))
    overview = root / "wiki" / "overview.md"
    thread = root / "wiki" / "threads" / "existing.md"
    thread.write_text("Existing thread baseline", encoding="utf-8")
    before = {p: p.read_bytes() for p in (overview, thread)}
    run.kb.write_state(str(root), {"seen_story_hashes": [{"hash": "seen-before",
                                                        "date": TODAY}]})
    seen = Mock(wraps=run.kb.record_seen_articles)
    monkeypatch.setattr(run.kb, "record_seen_articles", seen)
    capture = Mock(wraps=run.capsule.snapshot)
    monkeypatch.setattr(run.capsule, "snapshot", capture)
    raw_world = json.dumps(analysis.return_value["world"], ensure_ascii=False).encode("utf-8")
    passes = analysis.return_value

    def analyze(*args, **kwargs):
        kwargs["evidence_sink"]({"invocation_id": "world-test", "pass_name": "world",
                                 "outcome": "success", "raw_output": raw_world})
        return passes

    analysis.side_effect = analyze
    monkeypatch.setattr(sys, "argv", ["run.py", "--force"])
    assert run.main() == 0
    assert {p: p.read_bytes() for p in before} == before
    writer.assert_not_called()
    result = renderer.call_args.args[0]
    assert result["source_review"]["status"] == "accepted"
    assert result["briefing"]["brief"] == [card]
    assert result["kb_deltas"] == POISON
    assert result["kb_proposals"] == metadata(POISON)
    receipt = json.loads((root / "run-status.json").read_text())
    assert receipt["kb_proposals"] == metadata(POISON)
    assert (root / "model-evidence" / receipt["run_id"] / "world-test" / "output.bin").read_bytes() == raw_world
    render_input = next(c.args[0] for c in capture.call_args_list if "analysis" in c.args[0])
    assert render_input["analysis"]["kb_deltas"] == POISON
    assert render_input["analysis"]["kb_proposals"] == metadata(POISON)
    assert (media / f"brief-{TODAY}.html").read_text() == HTML
    assert (root / "wiki" / "daily" / f"{TODAY}.md").read_text() == HTML
    seen.assert_called_once()
    assert run.kb.load_seen(str(root), TODAY) == {"seen-before"}
    assert run.kb.read_state(str(root))["last_run_date"] == TODAY
    assert "outside reviewed card scope" in capsys.readouterr().err


@pytest.mark.parametrize("deltas", [POISON, {}])
@pytest.mark.parametrize("dry", [False, True])
def test_review_proposal_metadata_in_receipt_result_and_dry_summary(
        offline, monkeypatch, capsys, tmp_path, deltas, dry):
    root, _, _, renderer, writer = prepare(offline, monkeypatch, deltas=deltas)
    before = snapshot(tmp_path)
    monkeypatch.setattr(sys, "argv", ["run.py", "--force"] + (["--dry"] if dry else []))
    assert run.main() == 0
    writer.assert_not_called()
    assert renderer.call_args.args[0]["kb_proposals"] == metadata(deltas)
    if dry:
        summary = json.loads(next(line.split(":", 1)[1] for line in capsys.readouterr().err.splitlines()
                                  if line.startswith("WORLD_INTEL_DRY_SUMMARY:")))
        assert summary["kb_proposals"] == metadata(deltas)
        assert snapshot(tmp_path) == before
    else:
        assert json.loads((root / "run-status.json").read_text())["kb_proposals"] == metadata(deltas)


@pytest.mark.parametrize("dry", [False, True])
def test_disabled_review_preserves_legacy_delta_application_and_metadata(offline, monkeypatch, capsys, dry):
    root, _, _, renderer, writer = prepare(offline, monkeypatch, review=False)
    monkeypatch.setattr(sys, "argv", ["run.py", "--force"] + (["--dry"] if dry else []))
    assert run.main() == 0
    assert writer.call_count == int(not dry)
    assert "kb_proposals" not in renderer.call_args.args[0]
    if dry:
        summary = json.loads(next(line.split(":", 1)[1] for line in capsys.readouterr().err.splitlines()
                                  if line.startswith("WORLD_INTEL_DRY_SUMMARY:")))
        assert "kb_proposals" not in summary
        assert not root.exists()
    else:
        assert (root / "wiki" / "overview.md").read_text(encoding="utf-8") == POISON["overview_replace"]
        assert POISON["threads_upsert"][0]["summary"] in (root / "wiki" / "threads" / "existing.md").read_text()
        assert "kb_proposals" not in json.loads((root / "run-status.json").read_text())


@pytest.mark.parametrize("dry", [False, True])
def test_hold_with_proposals_remains_atomic(offline, monkeypatch, tmp_path, dry):
    root, media, _, renderer, writer = prepare(offline, monkeypatch, status="hold")
    before = snapshot(tmp_path)
    monkeypatch.setattr(sys, "argv", ["run.py", "--force"] + (["--dry"] if dry else []))
    assert run.main() == int(dry)
    writer.assert_not_called()
    renderer.assert_not_called()
    assert not media.exists()
    if dry:
        assert snapshot(tmp_path) == before
    else:
        assert not (root / "wiki").exists() and not (root / "state.json").exists()
        assert json.loads((root / "run-status.json").read_text())["generation"] == "held"
