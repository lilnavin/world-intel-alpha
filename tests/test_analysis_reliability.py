"""Offline reliability paths: real merge/render/state, substituted model boundary."""
import json
import hashlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_dry_isolation import offline, TODAY
from worldintel import config, run
from worldintel.core import codex_engine as engine, compose_html, metrics


@pytest.mark.parametrize("world_ok,finance_ok,overall", [
    (True, True, "complete"), (False, True, "partial"),
    (True, False, "partial"), (False, False, "failed")])
def test_pass_outcomes_and_distinct_timeouts(monkeypatch, world_ok, finance_ok, overall):
    calls = []
    def analyze(name, ok):
        def invoke(*args, **kwargs):
            calls.append((name, kwargs["timeout"]))
            if not ok:
                raise engine.CodexError("codex timed out after 600s")
            return {"briefing": {}}
        return invoke
    monkeypatch.setattr(engine, "analyze_world", analyze("world", world_ok))
    monkeypatch.setattr(engine, "analyze_finance", analyze("finance", finance_ok))
    passes = engine.run_two_pass({}, {}, "world", "finance", concurrent=True)
    assert calls == [("world", 600), ("finance", 360)]
    status = passes["analysis_status"]
    assert status["overall"] == overall
    assert status["mode"] == "sequential"
    for name, success in (("world", world_ok), ("finance", finance_ok)):
        record = status["passes"][name]
        assert record["success"] is success
        assert record["timed_out"] is (not success)
        assert record["skipped"] is False
        assert record["duration_s"] >= 0


def test_remaining_budget_skips_finance_without_retry(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(engine.time, "monotonic", lambda: clock[0])
    def world(*args, **kwargs):
        assert kwargs["timeout"] == 10
        clock[0] += 10
        return {"briefing": {}}
    monkeypatch.setattr(engine, "analyze_world", world)
    finance = Mock(side_effect=AssertionError("budget exhausted"))
    monkeypatch.setattr(engine, "analyze_finance", finance)
    result = engine.run_two_pass({}, {}, "w", "f", deadline=110)
    finance.assert_not_called()
    status = result["analysis_status"]["passes"]["finance"]
    assert status["skipped"] is True and status["timed_out"] is False
    assert status["error"] == "analysis budget exhausted"


@pytest.mark.parametrize("setting,value", [("world_timeout_s", 0), ("finance_timeout_s", True),
    ("pipeline_timeout_s", 120), ("finish_reserve_s", 1500), ("rewrite_timeout_s", -1)])
def test_deadline_config_rejects_invalid_settings(synthetic_config, setting, value):
    raw = synthetic_config.raw
    raw.setdefault("analysis", {})[setting] = value
    with pytest.raises(config.ConfigError, match="analysis"):
        config.Config(raw, synthetic_config.source_path)


def test_raw_fallback_is_plain_text_with_unavailable_dynamic_desks(synthetic_config):
    passes = {"world": None, "finance": {"briefing": {}}, "errors": {"world": "private https://provider/?token=secret"}}
    bundle = {"news": [{"title": "<b>Headline</b>", "summary": "<p>Lead &amp; detail</p><script>bad()</script>",
                        "source": "Wire", "url": "javascript:alert(1)"}]}
    result = run._merge_passes(passes, bundle)
    story = result["briefing"]["brief"][0]
    assert story["headline"] == "Headline"
    assert story["body"] == "Lead & detail"
    assert story["sources"] == []
    html = compose_html.render_html(result, bundle, TODAY, desks=synthetic_config.desks)
    assert "Degraded briefing" in html
    assert "private https://provider" not in html
    assert "&lt;p&gt;" not in html and "bad()" not in html
    for desk in synthetic_config.national_desks:
        assert f'{desk["label"]} coverage unavailable' in html
    assert metrics.compute_metrics(bundle, result, TODAY, {})["codex_ok"] is False


@pytest.mark.parametrize("world_ok,finance_ok", [(False, True), (True, False), (False, False), (True, True)])
def test_runtime_receipt_and_last_ok(offline, monkeypatch, world_ok, finance_ok):
    root, media, _, analysis, _, _ = offline
    run.kb.ensure_kb(str(root))
    run.kb.write_state(str(root), {"codex": {"last_ok": "2026-09-30"}})
    analysis.return_value = {"world": {"briefing": {}} if world_ok else None,
                             "finance": {"briefing": {}} if finance_ok else None,
                             "errors": {"world": None if world_ok else "timeout", "finance": None if finance_ok else "error"}}
    monkeypatch.setattr(sys, "argv", ["run.py", "--force"])
    assert run.main() == 0
    state = run.kb.read_state(str(root))
    assert state["codex"]["last_ok"] == (TODAY if world_ok and finance_ok else "2026-09-30")
    assert state["last_run_date"] == TODAY
    receipt = json.loads((root / "run-status.json").read_text())
    assert receipt["schema_version"] == 1 and receipt["run_id"]
    assert receipt["date"] == TODAY and receipt["artifact"]["path"] == str(media / f"brief-{TODAY}.html")
    assert len(receipt["artifact"]["sha256"]) == 64
    assert receipt["generation"] == "generated" and receipt["handoff"] == "handed_off"
    assert receipt["delivery_confirmed"] is False


def test_timeout_diagnostics_are_bounded_and_redacted(monkeypatch, tmp_path):
    monkeypatch.setattr(engine.shutil, "which", lambda _: "codex")
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("codex", 2, output=b"Bearer abc123 https://provider/?token=abc123", stderr=b"API_KEY=abc123 " + b"x" * 4000)
    monkeypatch.setattr(engine, "_execute", timeout)
    with pytest.raises(engine.CodexError) as failure:
        engine._run_codex("secret prompt", str(tmp_path / "out"), schema_path=None, reasoning_effort="high", timeout=2)
    error = str(failure.value)
    assert "abc123" not in error and "provider" not in error
    assert "stderr" in error and len(error) < 1800


def test_writing_gate_budget_is_exhausted_before_model_boundary(offline, monkeypatch):
    root, _, _, analysis, _, _ = offline
    clock = [10.0]
    monkeypatch.setattr(run.time, "monotonic", lambda: clock[0])
    original = analysis.return_value
    def consume(*args, **kwargs):
        clock[0] += 1380
        return original
    analysis.side_effect = consume
    monkeypatch.setattr(run.quality_gate, "scan", lambda _: ["body"])
    rewrite = Mock(side_effect=AssertionError("rewrite must not start"))
    monkeypatch.setattr(run.quality_gate, "rewrite", rewrite)
    monkeypatch.setattr(sys, "argv", ["run.py", "--force"])
    assert run.main() == 0
    rewrite.assert_not_called()
    assert json.loads((root / "run-status.json").read_text())["rewrite"]["skipped"] is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows cleanup")
def test_timeout_runner_kills_its_windows_process_tree(monkeypatch, tmp_path):
    process = Mock(pid=123, returncode=1)
    process.communicate.side_effect = subprocess.TimeoutExpired("codex", 2)
    monkeypatch.setattr(engine.shutil, "which", lambda _: "codex")
    monkeypatch.setattr(engine.subprocess, "Popen", Mock(return_value=process))
    cleanup = Mock(return_value=subprocess.CompletedProcess([], 0))
    monkeypatch.setattr(engine.subprocess, "run", cleanup)
    with pytest.raises(engine.CodexError):
        engine._run_codex("prompt", str(tmp_path / "out"), schema_path=None, reasoning_effort="high", timeout=2)
    assert cleanup.call_args.args[0] == ["taskkill", "/PID", "123", "/T", "/F"]
    assert cleanup.call_args.kwargs["timeout"] == 5
    process.kill.assert_called_once()
    assert not list(tmp_path.glob("worldintel-prompt-*"))


def test_rewrite_failure_is_recorded_even_when_writing_gate_fails_open(offline, monkeypatch):
    root, _, _, analysis, _, _ = offline
    analysis.return_value["world"]["briefing"] = {"brief": [{"body": "The bundle only gives a headline."}]}
    model = Mock(side_effect=engine.CodexError("codex timed out; API_KEY=private"))
    monkeypatch.setattr(engine, "run_schema", model)
    monkeypatch.setattr(sys, "argv", ["run.py", "--force"])
    assert run.main() == 0
    receipt = json.loads((root / "run-status.json").read_text())
    assert receipt["rewrite"]["attempted"] is True
    assert "timed out" in receipt["rewrite"]["error"]
    assert "private" not in receipt["rewrite"]["error"]


def test_receipt_hash_matches_actual_multiline_file_bytes(offline, monkeypatch):
    root, media, _, _, renderer, _ = offline
    renderer.return_value = "<html>\n<p>multiline briefing</p>\n</html>"
    monkeypatch.setattr(sys, "argv", ["run.py", "--force"])
    assert run.main() == 0
    receipt = json.loads((root / "run-status.json").read_text())
    actual = (media / f"brief-{TODAY}.html").read_bytes()
    assert receipt["artifact"]["sha256"] == hashlib.sha256(actual).hexdigest()


@pytest.mark.parametrize("raw", [
    '{"api_key": "SUPER_SECRET"}', "{'token': 'SUPER_SECRET'}",
    'Authorization: Basic SUPER_SECRET', '"password" = "SUPER_SECRET"',
])
def test_diagnostics_redact_quoted_credentials_and_basic_auth(raw):
    assert "SUPER_SECRET" not in engine.diagnostic(raw)


def test_timeout_captures_post_cleanup_partial_output(monkeypatch, tmp_path):
    process = Mock(pid=123, returncode=1)
    process.communicate.side_effect = [subprocess.TimeoutExpired("codex", 2),
                                      (b"partial progress", b"model busy token=private")]
    monkeypatch.setattr(engine.shutil, "which", lambda _: "codex")
    monkeypatch.setattr(engine.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(engine.subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0)))
    if sys.platform != "win32":
        monkeypatch.setattr(engine.os, "killpg", lambda *args: None)
    with pytest.raises(engine.CodexError) as failure:
        engine._run_codex("prompt", str(tmp_path / "out"), schema_path=None, reasoning_effort="high", timeout=2)
    assert "partial progress" in str(failure.value)
    assert "model busy" in str(failure.value)
    assert "private" not in str(failure.value)


def test_real_offline_process_timeout_retains_both_streams(tmp_path):
    script = "import time,sys; print('stdout-marker',flush=True); print('stderr-marker',file=sys.stderr,flush=True); time.sleep(30)"
    with (tmp_path / "stdin").open("wb") as output:
        output.write(b"")
    with (tmp_path / "stdin").open("rb") as stdin:
        with pytest.raises(subprocess.TimeoutExpired) as failure:
            engine._execute([sys.executable, "-c", script], stdin=stdin, capture_output=True, timeout=0.5)
    assert b"stdout-marker" in (failure.value.output or b"")
    assert b"stderr-marker" in (failure.value.stderr or b"")
