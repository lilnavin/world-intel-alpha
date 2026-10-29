import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from test_dry_isolation import offline, snapshot, TODAY
from worldintel import config, run


def enable():
    cfg = config.get()
    raw = cfg.raw
    raw.setdefault("analysis", {})["source_review_enabled"] = True
    config.set_for_test(config.Config(raw, cfg.source_path))


@pytest.mark.parametrize("dry", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_hold_prevents_render_kb_and_handoff_and_records_failure(
        offline, tmp_path, monkeypatch, capsys, dry, existing):
    root, media, _, _, renderer, _ = offline
    enable()
    if existing:
        run.kb.ensure_kb(str(root))
        run.kb.write_state(str(root), {"last_run_date": TODAY})
        run.earnings_report.write_pipeline(str(root), {"old": {"status": "covered"}})
        run.filing_index.write_index(str(root), {"2026-09-30": {"old": "entry"}})
        daily = root / "wiki" / "daily" / f"{TODAY}.md"
        daily.parent.mkdir(parents=True, exist_ok=True)
        daily.write_bytes(b"existing KB brief")
        media.mkdir()
        (media / f"brief-{TODAY}.html").write_bytes(b"existing media brief")
    before = snapshot(tmp_path)
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
    assert media.exists() == existing and renderer.call_count == 0
    assert all(w.call_count == 0 for w in writers)
    if dry:
        summary = json.loads(next(l.split(":", 1)[1] for l in output.err.splitlines() if l.startswith("WORLD_INTEL_DRY_SUMMARY:")))
        assert summary["generation"] == "held" and summary["source_review"]["status"] == "hold"
        assert root.exists() == existing
        assert snapshot(tmp_path) == before
    else:
        receipt = json.loads((root / "run-status.json").read_text())
        assert receipt["generation"] == "held" and receipt["handoff"] == "held"
        assert receipt["source_review"]["status"] == "hold"
        assert receipt["artifact"] is None
        # This fixture performs no model invocation. The status receipt and
        # fetched-source cache persist; model evidence is permitted in real calls.
        after = snapshot(tmp_path)
        permitted = {str(Path("data") / "run-status.json"),
                     str(Path("data") / "raw" / TODAY / "fetch.json")}
        assert set(after) - set(before) == permitted
        assert (root / "raw" / TODAY / "fetch.json").read_bytes() == b"fresh cache"
        assert {name: value for name, value in after.items()
                if name not in permitted} == before


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


@pytest.mark.parametrize("review_enabled,dry,status,persist", [
    (False, False, "disabled", True),
    (False, True, "disabled", False),
    (True, False, "accepted", True),
    (True, True, "accepted", False),
    (True, False, "hold", False),
    (True, True, "hold", False),
])
def test_scoreboard_persistence_respects_review_and_dry_barriers(
        offline, monkeypatch, review_enabled, dry, status, persist):
    root, _, fetch, analysis, _, _ = offline
    if review_enabled:
        enable()
    original_fetch = fetch.side_effect
    markets = {"indices": {"TEST": {"level": 123, "asof": TODAY}}}

    def fetch_markets(*args, **kwargs):
        bundle = original_fetch(*args, **kwargs)
        bundle["markets"] = markets
        return bundle

    fetch.side_effect = fetch_markets
    scoreboard = root / "wiki" / "markets" / "scoreboard.md"
    history = root / "wiki" / "markets" / "history.jsonl"
    original_analysis = analysis.return_value

    def analyze(*args, **kwargs):
        # Disabled profiles retain scoreboard context before analysis; enabled
        # profiles must wait until the source review accepts the result.
        assert scoreboard.exists() == (not review_enabled and not dry)
        assert history.exists() == (not review_enabled and not dry)
        return original_analysis

    analysis.side_effect = analyze

    def review(*args, **kwargs):
        assert not scoreboard.exists() and not history.exists()
        return {"status": status, "cards_reviewed": 1}

    monkeypatch.setattr(run.grounding_review, "review", review)
    update = Mock(wraps=run.kb.update_scoreboard)
    append = Mock(wraps=run.kb.append_scoreboard_history)
    monkeypatch.setattr(run.kb, "update_scoreboard", update)
    monkeypatch.setattr(run.kb, "append_scoreboard_history", append)
    monkeypatch.setattr(sys, "argv", ["run.py", "--force"] + (["--dry"] if dry else []))
    assert run.main() == (1 if dry and status == "hold" else 0)
    assert update.call_count == append.call_count == int(persist)
    assert scoreboard.exists() == history.exists() == persist
    if persist:
        update.assert_called_once_with(str(root), markets)
        append.assert_called_once_with(str(root), markets, TODAY)
        assert "TEST" in scoreboard.read_text(encoding="utf-8")
        assert [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()] == [
            {"date": TODAY, "instruments": {"TEST": {"level": 123, "asof": TODAY}}}]


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


def test_runtime_names_initial_correction_and_final_invocations(offline, monkeypatch):
    enable()
    cfg = config.get()
    raw = cfg.raw
    raw["analysis"]["source_review_repair_enabled"] = True
    config.set_for_test(config.Config(raw, cfg.source_path))
    calls = []
    monkeypatch.setattr(run.codex_engine, "run_schema", lambda *a, **k: calls.append(k["pass_name"]) or {})
    def bounded(result, bundle, *, review_fn, repair_fn, deadline):
        review_fn("initial", "schema", 10)
        repair_fn("correction", "schema", 9)
        review_fn("final", "schema", 8)
        return {"status": "accepted", "cards_reviewed": 1}
    monkeypatch.setattr(run.grounding_review, "review", bounded)
    monkeypatch.setattr(sys, "argv", ["run.py", "--force"])
    assert run.main() == 0
    assert calls == ["source-review", "source-correction", "source-review-final"]


def test_runtime_capture_failure_holds_before_review_or_correction(offline, monkeypatch):
    enable()
    from test_grounding_review import fixture
    material = run.grounding_review._material(*fixture())
    monkeypatch.setattr(run.grounding_review, "_material", lambda *a: material)
    monkeypatch.setattr(run.model_evidence.Collector, "__init__", lambda self, *a, **k: (
        setattr(self, "failed", True), setattr(self, "invocations", []), setattr(self, "root", __import__('pathlib').Path('f' * 32))) and None)
    monkeypatch.setattr(run.codex_engine, "run_schema", lambda *a, **k: pytest.fail("capture failure must hold"))
    monkeypatch.setattr(sys, "argv", ["run.py", "--dry", "--force"])
    assert run.main() == 1
