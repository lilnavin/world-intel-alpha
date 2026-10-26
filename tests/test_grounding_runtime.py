import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from test_dry_isolation import offline, TODAY
from worldintel import config, run


def enable():
    cfg = config.get()
    raw = cfg.raw
    raw.setdefault("analysis", {})["source_review_enabled"] = True
    config.set_for_test(config.Config(raw, cfg.source_path))


@pytest.mark.parametrize("dry", [False, True])
def test_hold_prevents_render_kb_and_handoff_and_records_failure(offline, monkeypatch, capsys, dry):
    root, media, _, _, renderer, _ = offline
    enable()
    writers = []
    for name in ("apply_deltas", "update_scoreboard", "append_scoreboard_history", "write_daily", "write_state"):
        if hasattr(run.kb, name):
            writer = Mock(wraps=getattr(run.kb, name))
            monkeypatch.setattr(run.kb, name, writer)
            writers.append(writer)
    monkeypatch.setattr(sys, "argv", ["run.py", "--force"] + (["--dry"] if dry else []))
    assert run.main() == (1 if dry else 0)
    output = capsys.readouterr()
    assert "MEDIA:" not in output.out
    assert not media.exists() and renderer.call_count == 0
    assert all(w.call_count == 0 for w in writers)
    if dry:
        summary = json.loads(next(l.split(":", 1)[1] for l in output.err.splitlines() if l.startswith("WORLD_INTEL_DRY_SUMMARY:")))
        assert summary["generation"] == "held" and summary["source_review"]["status"] == "hold"
        assert not root.exists()
    else:
        receipt = json.loads((root / "run-status.json").read_text())
        assert receipt["generation"] == "held" and receipt["handoff"] == "held"
        assert receipt["source_review"]["status"] == "hold"
        assert receipt["artifact"] is None


def test_accepted_review_is_visible_in_status_and_occurs_after_coverage(offline, monkeypatch, capsys):
    root, _, _, _, renderer, _ = offline
    enable()
    order = []
    original = run.coverage_gate.enforce
    def coverage(*args, **kwargs):
        order.append("coverage")
        return original(*args, **kwargs)
    monkeypatch.setattr(run.coverage_gate, "enforce", coverage)
    def review(*args, **kwargs):
        order.append("review")
        return {"status": "accepted", "scope": "cards", "cards_reviewed": 1}
    monkeypatch.setattr(run.grounding_review, "review", review)
    monkeypatch.setattr(sys, "argv", ["run.py", "--force"])
    assert run.main() == 0
    assert order == ["coverage", "review"] and renderer.call_count == 1
    assert json.loads((root / "run-status.json").read_text())["source_review"]["status"] == "accepted"


def test_disabled_profile_adds_no_review_call(offline, monkeypatch):
    root, _, _, _, _, _ = offline
    monkeypatch.setattr(run.grounding_review, "review", lambda *a, **k: pytest.fail("disabled"))
    monkeypatch.setattr(sys, "argv", ["run.py", "--force"])
    assert run.main() == 0
    assert json.loads((root / "run-status.json").read_text())["source_review"]["status"] == "disabled"


@pytest.mark.parametrize('accepted', [False, True])
def test_review_stages_mutating_earnings_and_filing_ledgers_until_acceptance(offline, monkeypatch, accepted):
    root, _, _, _, _, index_roots = offline
    enable()
    root.mkdir()
    run.earnings_report.write_pipeline(str(root), {'old': {'status': 'covered'}})
    run.filing_index.write_index(str(root), {'2026-09-30': {'old': 'entry'}})
    before = {name: (root / name).read_bytes() for name in ('earnings_pipeline.json', 'filing_index.json')}
    if accepted:
        monkeypatch.setattr(run.grounding_review, 'review', lambda *a, **k: {
            'status': 'accepted', 'attempted': True, 'cards_reviewed': 1})
    monkeypatch.setattr(sys, 'argv', ['run.py', '--force'])
    assert run.main() == 0
    if accepted:
        assert run.earnings_report.read_pipeline(str(root)) == {'fresh': {'status': 'pending'}}
        assert run.filing_index.read_index(str(root)) == {TODAY: {}}
    else:
        assert {name: (root / name).read_bytes() for name in before} == before
    assert index_roots and index_roots[0] != root
    assert not index_roots[0].exists()
