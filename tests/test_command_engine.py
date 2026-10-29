import json
import sys
import time
from pathlib import Path

import pytest
from worldintel.core import codex_engine, command_engine

_REAL_EXECUTE = command_engine._execute


@pytest.fixture(autouse=True)
def synthetic_command_only(monkeypatch):
    monkeypatch.setattr(command_engine, "_execute", _REAL_EXECUTE)


@pytest.fixture
def schema(tmp_path):
    path = tmp_path / 'schema space ÃƒÂ¼.json'
    path.write_text(json.dumps({'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'], 'additionalProperties': False}))
    return path


def engine(tmp_path, body):
    from worldintel.core.command_engine import CommandEngine
    helper = tmp_path / 'helper space ÃƒÂ¼.py'
    helper.write_text('import os, sys, pathlib, time\n' + body, encoding='utf-8')
    return CommandEngine([sys.executable, str(helper)])


def test_exact_protocol_and_evidence(tmp_path, schema):
    records = []
    obj = engine(tmp_path, "p=sys.stdin.buffer.read(); assert p.replace(bytes([13,10]), bytes([10])) == 'hello ÃƒÂ¼\\n'.encode(); assert os.environ['WORLD_INTEL_PASS_NAME']=='world'; assert os.environ['WORLD_INTEL_REASONING_EFFORT']=='low'; assert pathlib.Path(os.environ['WORLD_INTEL_SCHEMA_PATH']).read_bytes(); pathlib.Path(os.environ['WORLD_INTEL_OUTPUT_PATH']).write_bytes(b'{\\n  \"ok\": true\\n}'); print('diagnostic')")
    assert obj.run_schema('hello ÃƒÂ¼\n', str(schema), reasoning_effort='low', evidence_sink=records.append, pass_name='world') == {'ok': True}
    assert records[0]['stdin'].replace(bytes([13,10]), bytes([10])) == 'hello ÃƒÂ¼\n'.encode()
    assert records[0]['schema'] == schema.read_bytes()
    assert records[0]['raw_output'] == b'{\n  "ok": true\n}'
    assert records[0]['stdout'].strip() == b'diagnostic'
    assert records[0]['outcome'] == 'success'


@pytest.mark.parametrize('raw', ['[]', '{"ok": 1}', '{"ok": NaN}', 'bad'])
def test_invalid_output_fails_safely(tmp_path, schema, raw):
    records = []
    obj = engine(tmp_path, f"pathlib.Path(os.environ['WORLD_INTEL_OUTPUT_PATH']).write_text({raw!r})")
    with pytest.raises(codex_engine.CodexError) as exc:
        obj.run_schema('x', str(schema), evidence_sink=records.append)
    assert raw not in str(exc.value)
    assert records[0]['outcome'] == 'malformed'


@pytest.mark.parametrize('body', ["print('secret sk-hello'); sys.exit(4)", "print('{\\\"ok\\\":true}')", "raise RuntimeError('secret sk-hello')"])
def test_missing_or_failed_output_has_fixed_error(tmp_path, schema, body):
    with pytest.raises(codex_engine.CodexError) as exc:
        engine(tmp_path, body).run_schema('x', str(schema))
    assert 'secret' not in str(exc.value)
    assert 'sk-hello' not in str(exc.value)


def test_timeout_captures_partial_diagnostics(tmp_path, schema):
    records = []
    start = time.monotonic()
    with pytest.raises(codex_engine.CodexError, match='timed out'):
        engine(tmp_path, "print('partial', flush=True); time.sleep(10)").run_schema('x', str(schema), timeout=.2, evidence_sink=records.append)
    assert time.monotonic() - start < 4
    assert b'partial' in records[0]['stdout']
    assert records[0]['outcome'] == 'timeout'


def test_capture_failure_is_safe(tmp_path, schema):
    def broken(record):
        raise RuntimeError('private-secret')
    with pytest.raises(codex_engine.CodexError, match='private model evidence capture failed'):
        engine(tmp_path, "sys.exit(9)").run_schema('x', str(schema), evidence_sink=broken)


def test_missing_command_and_nonlist_rejected():
    from worldintel.core.command_engine import CommandEngine
    for argv in (None, [], 'python secret', ['']):
        with pytest.raises(codex_engine.CodexError):
            CommandEngine(argv)
    assert CommandEngine(['worldintel-nonexistent-command']).local_status()['available'] is False


def test_fresh_output_path_each_call_and_no_stdout_fallback(tmp_path, schema):
    marker = tmp_path / 'prior.txt'
    body = f"marker=pathlib.Path({str(marker)!r}); out=pathlib.Path(os.environ['WORLD_INTEL_OUTPUT_PATH']); assert not out.exists(); prior=marker.read_text() if marker.exists() else ''; assert prior != str(out); marker.write_text(str(out)); out.write_text('{{\"ok\":true}}')"
    obj = engine(tmp_path, body)
    assert obj.run_schema('x', str(schema)) == {'ok': True}
    assert obj.run_schema('x', str(schema)) == {'ok': True}


def test_timeout_kills_descendant(tmp_path, schema, monkeypatch):
    marker = tmp_path / 'descendant alive.txt'
    child = f"import time,pathlib;time.sleep(2);pathlib.Path({str(marker)!r}).write_text('alive')"
    body = f"import subprocess; subprocess.Popen([sys.executable, '-c', {child!r}]); print('spawned',flush=True); time.sleep(10)"
    records = []
    executor_errors = []
    def traced_execute(*args, **kwargs):
        try:
            return _REAL_EXECUTE(*args, **kwargs)
        except Exception as error:
            executor_errors.append((type(error).__name__, str(error)))
            raise
    monkeypatch.setattr(command_engine, '_execute', traced_execute)
    with pytest.raises(codex_engine.CodexError) as exc:
        engine(tmp_path, body).run_schema('x', str(schema), timeout=.3, evidence_sink=records.append)
    assert 'timed out' in str(exc.value), (repr(records), executor_errors)
    time.sleep(2.2)
    assert not marker.exists()


def test_numeric_overflow_is_not_json(tmp_path, schema):
    schema.write_text('{"type":"object"}')
    with pytest.raises(codex_engine.CodexError):
        engine(tmp_path, "pathlib.Path(os.environ['WORLD_INTEL_OUTPUT_PATH']).write_text('{\"x\":1e999}')").run_schema('x', str(schema))


def test_launch_failure_captures_inputs_without_public_argv(tmp_path, schema):
    from worldintel.core.command_engine import CommandEngine
    records = []
    with pytest.raises(codex_engine.CodexError) as exc:
        CommandEngine(['worldintel-nonexistent-command', 'private-secret']).run_schema('prompt', str(schema), evidence_sink=records.append)
    assert str(exc.value) == 'Analysis command failed'
    assert records[0]['outcome'] == 'exception'
    assert records[0]['stdin'] == b'prompt'
    assert records[0]['schema'] == schema.read_bytes()


@pytest.mark.parametrize('launch_elapsed,communicate_elapsed,expected_timeout', [(3, 0, 7), (11, 0, None), (3, 8, 7)])
def test_absolute_deadline_deducts_launch_and_rejects_late_process(monkeypatch, launch_elapsed, communicate_elapsed, expected_timeout):
    import io
    import subprocess
    import types
    clock = [100.0]
    calls = []
    class Process:
        pid = 123
        returncode = 0
        def communicate(self, *, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                clock[0] += communicate_elapsed
            return b'partial stdout', b'partial stderr'
        def kill(self):
            pass
    def launch(*args, **kwargs):
        clock[0] += launch_elapsed
        return Process()
    monkeypatch.setattr(command_engine, 'time', types.SimpleNamespace(monotonic=lambda: clock[0]), raising=False)
    monkeypatch.setattr(command_engine.subprocess, 'Popen', launch)
    monkeypatch.setattr(command_engine.subprocess, 'run', lambda *a, **k: None)
    monkeypatch.setattr(command_engine.os, 'killpg', lambda *a: None, raising=False)
    if launch_elapsed + communicate_elapsed >= 10:
        with pytest.raises(subprocess.TimeoutExpired) as exc:
            _REAL_EXECUTE(['fake'], stdin=io.BytesIO(), timeout=10, env={})
        assert exc.value.output == b'partial stdout'
        assert exc.value.stderr == b'partial stderr'
    else:
        assert _REAL_EXECUTE(['fake'], stdin=io.BytesIO(), timeout=10, env={}).returncode == 0
    if expected_timeout is None:
        assert calls == [2]  # Only bounded cleanup communication after launch exhausted budget.
    else:
        assert calls[0] == expected_timeout


@pytest.mark.parametrize('late_phase', ['setup', 'executor', 'read', 'validation'])
def test_late_valid_output_is_timeout_with_exact_evidence(monkeypatch, schema, late_phase):
    import subprocess
    import types
    clock = [100.0]
    records = []
    calls = []
    original_read = Path.read_bytes
    def read(path):
        value = original_read(path)
        if late_phase == 'setup' and path == schema.resolve():
            clock[0] = 111
        if late_phase == 'read' and path.name == 'output.json':
            clock[0] = 111
        return value
    def execute(argv, *, stdin, timeout, env, **kwargs):
        calls.append(timeout)
        Path(env['WORLD_INTEL_OUTPUT_PATH']).write_bytes(b'{"ok":true}')
        if late_phase == 'executor':
            clock[0] = 111
        return subprocess.CompletedProcess(argv, 0, b'diagnostic', b'')
    def validate(*args):
        if late_phase == 'validation':
            clock[0] = 111
    monkeypatch.setattr(command_engine, 'time', types.SimpleNamespace(monotonic=lambda: clock[0]), raising=False)
    monkeypatch.setattr(Path, 'read_bytes', read)
    monkeypatch.setattr(command_engine, '_execute', execute)
    monkeypatch.setattr(command_engine.jsonschema, 'validate', validate)
    with pytest.raises(codex_engine.CodexError, match='Analysis command timed out'):
        command_engine.CommandEngine(['fake']).run_schema('prompt', str(schema), timeout=10, evidence_sink=records.append)
    assert records[0]['outcome'] == 'timeout'
    assert records[0]['timeout_s'] == 10
    assert records[0]['stdin'] == b'prompt'
    if late_phase != 'setup':
        assert records[0]['raw_output'] == b'{"ok":true}'
        assert records[0]['stdout'] == b'diagnostic'
    else:
        assert calls == []
