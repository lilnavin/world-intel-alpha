"""Trusted argv extension protocol with bounded execution and private evidence."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import uuid

import jsonschema

from .codex_engine import CodexError


def _execute(argv, *, stdin, timeout, env, deadline=None):
    deadline = time.monotonic() + timeout if deadline is None else deadline
    options = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == 'nt' else {'start_new_session': True}
    proc = subprocess.Popen(argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, **options)
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        stdout, stderr = proc.communicate(timeout=remaining)
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as exc:
        if os.name == 'nt':
            try:
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, timeout=2)
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            proc.kill()
            stdout, stderr = proc.communicate(timeout=2)
            exc.output, exc.stderr = stdout, stderr
        except (OSError, subprocess.SubprocessError):
            pass
        raise


def _reject_constant(value):
    raise ValueError('Nonfinite JSON')


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON")
    return result


def _check_deadline(deadline, timeout):
    if time.monotonic() >= deadline:
        raise subprocess.TimeoutExpired('analysis command', timeout)


class CommandEngine:
    def __init__(self, argv, *, engine_id='command', env=None):
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or not arg or '\x00' in arg for arg in argv):
            raise CodexError('Analysis command must be a nonempty argv list')
        self.argv = list(argv)
        self.engine_id = engine_id
        self.env = dict(env or {})

    def local_status(self):
        available = shutil.which(self.argv[0]) is not None
        return {'available': available, 'detail': 'Analysis command available' if available else 'Analysis command unavailable'}

    def smoke_test(self, timeout=60):
        try:
            with tempfile.TemporaryDirectory(prefix='worldintel-smoke-') as directory:
                schema = Path(directory) / 'schema.json'
                schema.write_text(json.dumps({'type': 'object', 'properties': {'ok': {'const': True}}, 'required': ['ok'], 'additionalProperties': False}), encoding='utf-8')
                return self.run_schema('Return exactly {"ok":true}', str(schema), reasoning_effort='low', timeout=timeout) == {'ok': True}
        except Exception:
            return False

    def run_schema(self, prompt_text, schema_path, *, reasoning_effort='high', timeout=360, evidence_sink=None, pass_name=None):
        deadline = time.monotonic() + timeout
        record = {'invocation_id': uuid.uuid4().hex, 'pass_name': pass_name, 'timeout_s': timeout,
                  'returncode': None, 'outcome': 'exception', 'stdout': b'', 'stderr': b''}
        try:
            with tempfile.TemporaryDirectory(prefix='worldintel-command-') as directory:
                prompt = Path(directory) / 'stdin.txt'
                output = Path(directory) / 'output.json'
                try:
                    schema = Path(schema_path).resolve()
                    record['schema'] = schema.read_bytes()
                    schema_value = json.loads(record['schema'], parse_constant=_reject_constant, parse_float=_finite_float)
                    with prompt.open('w', encoding='utf-8') as stream:
                        stream.write(prompt_text)
                    record['stdin'] = prompt.read_bytes()
                    env = {**os.environ, **self.env,
                           'WORLD_INTEL_SCHEMA_PATH': str(schema), 'WORLD_INTEL_OUTPUT_PATH': str(output),
                           'WORLD_INTEL_TIMEOUT_S': str(timeout), 'WORLD_INTEL_REASONING_EFFORT': reasoning_effort,
                           'WORLD_INTEL_PASS_NAME': pass_name or ''}
                    _check_deadline(deadline, timeout)
                    with prompt.open('rb') as stream:
                        proc = _execute(self.argv, stdin=stream, timeout=timeout, env=env, deadline=deadline)
                    record.update(returncode=proc.returncode, stdout=proc.stdout or b'', stderr=proc.stderr or b'')
                    _check_deadline(deadline, timeout)
                    if proc.returncode:
                        record['outcome'] = 'nonzero'
                        raise CodexError('Analysis command failed')
                    if not output.is_file() or output.is_symlink():
                        record['outcome'] = 'missing'
                        raise CodexError('Analysis command produced no output file')
                    record['raw_output'] = output.read_bytes()
                    _check_deadline(deadline, timeout)
                    try:
                        result = json.loads(record['raw_output'], parse_constant=_reject_constant, parse_float=_finite_float)
                        if not isinstance(result, dict):
                            raise ValueError('Object required')
                        jsonschema.validate(result, schema_value)
                    except (ValueError, UnicodeError, jsonschema.exceptions.SchemaError, jsonschema.exceptions.ValidationError):
                        record['outcome'] = 'malformed'
                        raise CodexError('Analysis command output failed JSON schema validation') from None
                    _check_deadline(deadline, timeout)
                    record['outcome'] = 'success'
                    return result
                except subprocess.TimeoutExpired as exc:
                    record.update(outcome='timeout',
                                  stdout=exc.output if exc.output is not None else record['stdout'],
                                  stderr=exc.stderr if exc.stderr is not None else record['stderr'])
                    raise CodexError('Analysis command timed out') from None
                except CodexError:
                    raise
                except Exception:
                    raise CodexError('Analysis command failed') from None
                finally:
                    try:
                        if 'raw_output' not in record:
                            record['raw_output'] = output.read_bytes() if output.is_file() and not output.is_symlink() else None
                    except OSError:
                        record['capture_error'] = True
                    if 'stdin' not in record or 'schema' not in record:
                        record['capture_error'] = True
                    if evidence_sink is not None:
                        try:
                            evidence_sink(record)
                        except Exception:
                            raise CodexError('private model evidence capture failed') from None
        except CodexError:
            raise
        except Exception:
            raise CodexError('Analysis command failed') from None
