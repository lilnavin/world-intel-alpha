"""Exact subprocess input/output artifacts, including failures, remain private."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from worldintel.core import codex_engine as engine


@pytest.mark.parametrize("outcome", ["success", "malformed", "nonzero", "missing", "timeout"])
def test_actual_stdin_schema_raw_and_process_evidence_survive_cleanup(tmp_path, monkeypatch, outcome):
    from worldintel.core.model_evidence import Collector
    collector = Collector(tmp_path / "private", "run1")
    schema = tmp_path / "schema.json"
    schema.write_bytes(b'{"type":"object"}\n')
    observed = []
    monkeypatch.setattr(engine, "_resolve_codex", lambda: "codex")
    def execute(args, *, stdin, **kwargs):
        observed.append(stdin.read())
        if outcome != "missing":
            Path(args[args.index("-o") + 1]).write_bytes(b'{"ok": true}' if outcome == "success" else b'partial raw')
        if outcome == "timeout":
            raise subprocess.TimeoutExpired("codex", 2, output=b"partial stdout", stderr=b"private stderr")
        return subprocess.CompletedProcess(args, 7 if outcome == "nonzero" else 0, b"private stdout", b"private stderr")
    monkeypatch.setattr(engine, "_execute", execute)
    def invoke():
        return engine.run_schema("line one\n日本", str(schema), timeout=2,
                                 evidence_sink=collector, pass_name="source-review")
    if outcome == "success":
        assert invoke() == {"ok": True}
    else:
        with pytest.raises(engine.CodexError):
            invoke()
    invocation = next((tmp_path / "private" / "model-evidence" / "run1").iterdir())
    assert (invocation / "stdin.bin").read_bytes() == observed[0]
    assert (invocation / "schema.bin").read_bytes() == schema.read_bytes()
    assert (invocation / "stderr.bin").read_bytes() == b"private stderr"
    assert (invocation / "stdout.bin").read_bytes() == (b"partial stdout" if outcome == "timeout" else b"private stdout")
    manifest = json.loads((invocation / "manifest.json").read_text())
    assert manifest["pass_name"] == "source-review"
    assert manifest["outcome"] == outcome
    assert manifest["files"]["stdin.bin"]["sha256"] == hashlib.sha256(observed[0]).hexdigest()
    assert manifest["returncode"] == (None if outcome == "timeout" else 7 if outcome == "nonzero" else 0)
    assert collector.summary()["complete"] is True


def test_evidence_io_failure_cannot_attest_completeness(tmp_path):
    from worldintel.core.model_evidence import Collector
    blocked = tmp_path / "blocked"
    blocked.write_text("file")
    collector = Collector(blocked, "run")
    collector({"invocation_id": "unique", "pass_name": "world", "stdin": b"x",
               "schema": b"{}", "raw_output": b"{}", "stdout": b"", "stderr": b"",
               "returncode": 0, "outcome": "success"})
    assert collector.summary()["complete"] is False


def test_repeated_pass_names_have_unique_invocation_ids(tmp_path, monkeypatch):
    records = []
    monkeypatch.setattr(engine, "_resolve_codex", lambda: "codex")
    def execute(args, **kwargs):
        Path(args[args.index("-o") + 1]).write_bytes(b'{}')
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr(engine, "_execute", execute)
    schema = tmp_path / "schema"
    schema.write_text('{}')
    for _ in range(2):
        engine.run_schema("prompt", str(schema), evidence_sink=records.append, pass_name="world")
    assert len(records) == 2 and records[0]["invocation_id"] != records[1]["invocation_id"]


def test_capture_callback_failure_still_removes_prompt_file(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_resolve_codex", lambda: "codex")
    monkeypatch.setattr(engine.tempfile, "tempdir", str(tmp_path))
    def execute(args, **kwargs):
        Path(args[args.index("-o") + 1]).write_bytes(b'{}')
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr(engine, "_execute", execute)
    def failed_capture(record):
        raise OSError("private storage unavailable")
    with pytest.raises(engine.CodexError, match="evidence capture"):
        engine._run_codex("private prompt", str(tmp_path / "out"), schema_path=None,
                          reasoning_effort="low", timeout=1, evidence_sink=failed_capture)
    assert not list(tmp_path.glob('worldintel-prompt-*'))


@pytest.mark.parametrize('inside', ['git', 'media'])
def test_collector_refuses_repository_or_media_target(tmp_path, inside):
    from worldintel.core.model_evidence import Collector
    if inside == 'git':
        (tmp_path / '.git').mkdir()
    collector = Collector(tmp_path, 'run', media_dir=tmp_path if inside == 'media' else tmp_path / 'separate-media')
    collector({"invocation_id": "unique", "pass_name": "world", "stdin": b"x",
               "schema": b"{}", "raw_output": b"{}", "stdout": b"", "stderr": b"",
               "returncode": 0, "outcome": "success"})
    assert collector.summary()['complete'] is False
    assert not (tmp_path / 'model-evidence').exists()


@pytest.mark.parametrize('outcome', ['nonzero', 'malformed', 'timeout'])
@pytest.mark.parametrize('api', ['run_schema', 'analyze'])
def test_private_capture_errors_do_not_repeat_raw_prose_in_ordinary_diagnostics(tmp_path, monkeypatch, outcome, api):
    secret = b'PROSE_SHOULD_STAY_PRIVATE'
    records = []
    monkeypatch.setattr(engine, '_resolve_codex', lambda: 'codex')
    def execute(args, **kwargs):
        Path(args[args.index('-o') + 1]).write_bytes(secret)
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired('codex', 1, output=secret, stderr=secret)
        return subprocess.CompletedProcess(args, 7 if outcome == 'nonzero' else 0, secret, secret)
    monkeypatch.setattr(engine, '_execute', execute)
    schema = tmp_path / 'schema'
    schema.write_text('{}')
    with pytest.raises(engine.CodexError) as failure:
        if api == 'run_schema':
            engine.run_schema('prompt', str(schema), evidence_sink=records.append)
        else:
            engine.analyze({}, {}, str(schema), evidence_sink=records.append)
    assert secret.decode() not in str(failure.value)
    assert records[0]['raw_output'] == secret
    assert records[0]['stderr'] == secret
