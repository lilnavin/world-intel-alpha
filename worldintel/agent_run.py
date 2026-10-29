"""Run once and expose only a fresh, receipt-bound local artifact."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

import yaml


def _invoke(config_path, force, env, diagnostics):
    argv = [sys.executable, '-I', '-m', 'worldintel.cli', 'run', '--config', str(config_path)]
    if force:
        argv.append('--force')
    return subprocess.run(argv, env=env, stdout=diagnostics, stderr=subprocess.STDOUT,
                          shell=False, check=False).returncode


def _read(path):
    blob = path.read_bytes()
    return blob, json.loads(blob)


def _verified(cfg, receipt_path, previous, nonce, started):
    blob, receipt = _read(receipt_path)
    if receipt.get('run_id') == previous.get('run_id'):
        return {'outcome': 'skipped', 'reason': 'no new run receipt'}
    generation = receipt.get('generation')
    if generation in ('held', 'failed'):
        return {'outcome': generation, 'reason': 'pipeline did not generate an accepted artifact'}
    if generation != 'generated':
        raise ValueError('generation incomplete')
    stamp = datetime.fromisoformat(receipt['started_at'])
    if (receipt['started_at'] == previous.get('started_at')
            or not started <= stamp <= datetime.now(timezone.utc)):
        raise ValueError('invalid run start')
    if not receipt.get('run_id') or receipt.get('agent_invocation_id') != nonce:
        raise ValueError('receipt ownership mismatch')
    analysis = receipt.get('analysis') or {}
    if analysis.get('overall') != 'complete':
        raise ValueError('analysis incomplete')
    # Runtime always executes world and finance passes, including empty desks.
    for name in ('world', 'finance'):
        state = (analysis.get('passes') or {}).get(name) or {}
        if (state.get('success') is not True or state.get('skipped') is not False
                or state.get('timed_out') is not False or state.get('error')):
            raise ValueError('required analysis incomplete')
    if (receipt.get('quality') or {}).get('overall') != 'complete':
        raise ValueError('quality incomplete')
    if cfg.raw.get('analysis', {}).get('source_review_enabled', False):
        if (receipt.get('source_review') or {}).get('status') != 'accepted':
            raise ValueError('review incomplete')
    identity = receipt['artifact']
    media_root = Path(cfg.media_dir).resolve()
    path = Path(identity['path']).resolve(strict=True)
    if not path.is_relative_to(media_root):
        raise ValueError('artifact outside media directory')
    sidecar = path.with_suffix('.status.json')
    sidecar_target = sidecar.resolve(strict=True)
    if not sidecar_target.is_relative_to(media_root):
        raise ValueError('sidecar outside media directory')
    side_blob, side = _read(sidecar_target)
    for key in ('run_id', 'started_at', 'agent_invocation_id', 'artifact', 'generation',
                'analysis', 'quality', 'source_review'):
        if side.get(key) != receipt.get(key):
            raise ValueError('sidecar mismatch')
    artifact = path.read_bytes()
    digest = hashlib.sha256(artifact).hexdigest()
    if digest != identity['sha256']:
        raise ValueError('artifact hash mismatch')
    if (receipt_path.read_bytes() != blob or sidecar_target.read_bytes() != side_blob
            or path.read_bytes() != artifact or Path(identity['path']).resolve(strict=True) != path
            or not path.resolve(strict=True).is_relative_to(media_root)
            or sidecar.resolve(strict=True) != sidecar_target
            or not sidecar_target.resolve(strict=True).is_relative_to(media_root)):
        raise ValueError('artifact changed during verification')
    return {'outcome': 'generated', 'run_id': receipt['run_id'],
            'artifact': {'path': str(path), 'sha256': digest}}


def main(argv=None):
    parser = argparse.ArgumentParser(prog='world-intel-agent')
    parser.add_argument('--config', required=True)
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--emit-media', action='store_true')
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    result = {'outcome': 'failed', 'reason': 'configuration or invocation failed'}
    lock = None
    acquired = False
    try:
        from worldintel import config
        profile = Path(args.config)
        if not profile.is_absolute():
            raise ValueError('absolute configuration path required')
        cfg = config.load(str(profile))
        root = Path(cfg.data_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        lock = root / '.agent-run.lock'
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            result = {'outcome': 'busy', 'reason': 'another helper owns the data root'}
        else:
            acquired = True
            os.close(descriptor)
            receipt_path = root / 'run-status.json'
            try:
                _, previous = _read(receipt_path)
            except (OSError, ValueError):
                previous = {}
            nonce = uuid.uuid4().hex
            started = datetime.now(timezone.utc)
            raw = dict(cfg.raw)
            raw['delivery'] = {'adapter': 'file'}
            env = dict(os.environ, WORLD_INTEL_AGENT_INVOCATION_ID=nonce)
            # A private profile ensures pipeline imports never see the owner's
            # transport policy. Diagnostics persist privately without stdout leaks.
            with tempfile.TemporaryDirectory(prefix='world-intel-agent-') as temporary:
                private = Path(temporary) / 'profile.yaml'
                private.write_text(yaml.safe_dump(raw), encoding='utf-8')
                diagnostics_dir = root / 'agent-diagnostics'
                diagnostics_dir.mkdir(mode=0o700, exist_ok=True)
                diagnostic_path = diagnostics_dir / (nonce + '.log')
                fd = os.open(diagnostic_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, 'wb') as diagnostics:
                    code = _invoke(private, args.force, env, diagnostics)
                if code:
                    result = {'outcome': 'failed', 'reason': 'pipeline process failed'}
                elif not receipt_path.exists():
                    result = {'outcome': 'skipped', 'reason': 'no run receipt'}
                else:
                    result = _verified(cfg, receipt_path, previous, nonce, started)
    except Exception:
        result = {'outcome': 'failed', 'reason': 'configuration, invocation or artifact verification failed'}
    finally:
        if acquired:
            try:
                lock.unlink()
            except OSError:
                result = {'outcome': 'failed', 'reason': 'helper lock cleanup failed'}
    result['delivery_confirmed'] = False
    print(json.dumps(result, ensure_ascii=True))
    if result['outcome'] == 'generated' and args.emit_media:
        print('MEDIA:' + result['artifact']['path'])
    return 0 if result['outcome'] == 'generated' else 1


if __name__ == '__main__':
    raise SystemExit(main())
