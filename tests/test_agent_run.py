import hashlib
import json
from pathlib import Path

import pytest

from worldintel import agent_run

REAL_INVOKE = agent_run._invoke


@pytest.fixture
def setup_run(tmp_path, monkeypatch):
    profile = tmp_path / 'profile.yaml'
    profile.write_text('version: 1\nsources:\n  filings:\n    enabled: false\n  earnings:\n    enabled: false\nruntime:\n  timezone: UTC\n  data_root: ' + str(tmp_path / 'data') + '\nanalysis:\n  source_review_enabled: true\n', encoding='utf-8')
    def invoke(config_path, force, env, diagnostics):
        import yaml
        cfg = yaml.safe_load(Path(config_path).read_text())
        assert cfg['delivery']['adapter'] == 'file'
        root = Path(cfg['runtime']['data_root'])
        media = root / 'media'
        media.mkdir(exist_ok=True)
        artifact = media / 'brief.html'
        artifact.write_bytes(b'new briefing')
        receipt = dict(run_id='new', started_at=agent_run.datetime.now(agent_run.timezone.utc).isoformat(),
                       agent_invocation_id=env['WORLD_INTEL_AGENT_INVOCATION_ID'], generation='generated',
                       artifact=dict(path=str(artifact), sha256=hashlib.sha256(artifact.read_bytes()).hexdigest()),
                       analysis=dict(overall='complete', passes={k: dict(success=True, skipped=False, timed_out=False, error=None) for k in ('world', 'finance')}),
                       quality=dict(overall='complete'), source_review=dict(status='accepted'))
        mutate(receipt)
        (root / 'run-status.json').write_text(json.dumps(receipt))
        sidecar = json.loads(json.dumps(receipt))
        mutate_sidecar(sidecar)
        artifact.with_suffix('.status.json').write_text(json.dumps(sidecar))
        return 0
    mutate = lambda receipt: None
    mutate_sidecar = lambda receipt: None
    def configure(change=None, sidecar=None):
        nonlocal mutate, mutate_sidecar
        mutate = change or mutate
        mutate_sidecar = sidecar or mutate_sidecar
    monkeypatch.setattr(agent_run, '_invoke', invoke)
    return profile, configure


def test_generated(setup_run, capsys):
    profile, _ = setup_run
    original = profile.read_bytes()
    assert agent_run.main(['--config', str(profile)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['outcome'] == 'generated'
    assert Path(result['artifact']['path']).is_file()
    assert result['delivery_confirmed'] is False
    assert profile.read_bytes() == original


@pytest.mark.parametrize('change', [
    lambda r: r.update(generation='held'),
    lambda r: r.update(generation='pending'),
    lambda r: r.update(generation='failed'),
    lambda r: r.update(agent_invocation_id='wrong'),
    lambda r: r['artifact'].update(sha256='wrong'),
    lambda r: r['artifact'].update(path='/outside.html'),
    lambda r: r['analysis'].update(overall='partial'),
    lambda r: r['analysis']['passes'].pop('finance'),
    lambda r: r['analysis']['passes']['world'].update(success=False),
    lambda r: r['analysis']['passes']['world'].update(timed_out=True),
    lambda r: r['analysis']['passes']['world'].update(skipped=True),
    lambda r: r.update(started_at='2000-01-01T00:00:00+00:00'),
    lambda r: r['quality'].update(overall='incomplete'),
    lambda r: r['source_review'].update(status='pending'),
])
def test_refuses_unverified(setup_run, capsys, change):
    profile, configure = setup_run
    configure(change)
    assert agent_run.main(['--config', str(profile), '--emit-media']) != 0
    output = capsys.readouterr().out
    assert 'MEDIA:' not in output
    assert 'artifact' not in json.loads(output)


@pytest.mark.parametrize('field', ['run_id', 'agent_invocation_id', 'artifact'])
def test_sidecar_mismatch(setup_run, capsys, field):
    profile, configure = setup_run
    configure(sidecar=lambda r: r.update({field: None}))
    assert agent_run.main(['--config', str(profile)]) != 0
    assert 'artifact' not in json.loads(capsys.readouterr().out)


def test_duplicate_stale(setup_run, monkeypatch, capsys):
    profile, _ = setup_run
    assert agent_run.main(['--config', str(profile)]) == 0
    capsys.readouterr()
    monkeypatch.setattr(agent_run, '_invoke', lambda *args: 0)
    assert agent_run.main(['--config', str(profile)]) != 0
    assert json.loads(capsys.readouterr().out)['outcome'] == 'skipped'


def test_busy(setup_run, capsys):
    profile, _ = setup_run
    root = profile.parent / 'data'
    root.mkdir()
    (root / '.agent-run.lock').write_text('held')
    assert agent_run.main(['--config', str(profile)]) != 0
    assert json.loads(capsys.readouterr().out)['outcome'] == 'busy'


def test_bad_config(tmp_path, capsys):
    assert agent_run.main(['--config', str(tmp_path / 'missing')]) != 0
    assert json.loads(capsys.readouterr().out)['outcome'] == 'failed'


def test_emit_only_after_verification(setup_run, capsys):
    profile, _ = setup_run
    assert agent_run.main(['--config', str(profile), '--emit-media']) == 0
    lines = capsys.readouterr().out.splitlines()
    receipt = json.loads(lines[0])
    assert lines[1] == 'MEDIA:' + receipt['artifact']['path']


def test_actual_child_boundary_captures_stdout(tmp_path, monkeypatch):
    import subprocess
    import sys
    seen = {}
    def fake_run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        kwargs['stdout'].write(b'MEDIA:unverified\n')
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(subprocess, 'run', fake_run)
    monkeypatch.setattr(agent_run, '_invoke', REAL_INVOKE)
    with (tmp_path / 'diagnostics').open('wb') as diagnostics:
        assert agent_run._invoke(tmp_path / 'profile with spaces.yaml', True, {'NONCE': 'x'}, diagnostics) == 0
    assert seen['argv'] == [sys.executable, '-I', '-m', 'worldintel.cli', 'run', '--config', str(tmp_path / 'profile with spaces.yaml'), '--force']
    assert seen['shell'] is False
    assert seen['stderr'] == subprocess.STDOUT
    assert (tmp_path / 'diagnostics').read_bytes() == b'MEDIA:unverified\n'


def test_exception_releases_lock(setup_run, monkeypatch, capsys):
    profile, _ = setup_run
    def fail(*args):
        raise RuntimeError('private provider details')
    monkeypatch.setattr(agent_run, '_invoke', fail)
    assert agent_run.main(['--config', str(profile)]) == 1
    assert not (profile.parent / 'data' / '.agent-run.lock').exists()
    assert 'private provider details' not in capsys.readouterr().out


def test_nested_invocation_is_busy(setup_run, monkeypatch, capsys):
    profile, _ = setup_run
    original = agent_run._invoke
    def nested(*args):
        assert agent_run.main(['--config', str(profile)]) == 1
        assert json.loads(capsys.readouterr().out)['outcome'] == 'busy'
        return original(*args)
    monkeypatch.setattr(agent_run, '_invoke', nested)
    assert agent_run.main(['--config', str(profile)]) == 0
    assert json.loads(capsys.readouterr().out)['outcome'] == 'generated'


def test_artifact_changes_during_verification(setup_run, monkeypatch, capsys):
    profile, _ = setup_run
    original = Path.read_bytes
    reads = 0
    def read_bytes(path):
        nonlocal reads
        if path.name == 'brief.html':
            reads += 1
            # Fixture hashes once; verification hashes again then rechecks.
            if reads == 3:
                path.write_bytes(b'changed after hashing')
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', read_bytes)
    assert agent_run.main(['--config', str(profile)]) == 1
    assert 'artifact' not in json.loads(capsys.readouterr().out)


def test_child_uses_installed_runtime_despite_cwd_and_pythonpath(tmp_path, monkeypatch):
    import os
    import sys
    import venv
    monkeypatch.setattr(agent_run, '_invoke', REAL_INVOKE)
    environment = tmp_path / 'isolated environment'
    venv.EnvBuilder(with_pip=False).create(environment)
    executable = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    site = (environment / 'Lib/site-packages' if os.name == 'nt' else
            environment / f'lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages')
    def package(root, marker):
        folder = root / 'worldintel'
        folder.mkdir(parents=True)
        (folder / '__init__.py').write_text('')
        (folder / 'cli.py').write_text('print(' + repr(marker) + ')\n')
    package(site, 'INSTALLED_SENTINEL_ONLY')
    cwd = tmp_path / 'caller checkout'
    pythonpath = tmp_path / 'injected pythonpath'
    package(cwd, 'CWD_HIJACK')
    package(pythonpath, 'PYTHONPATH_HIJACK')
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(agent_run.sys, 'executable', str(executable))
    env = dict(os.environ, PYTHONPATH=str(pythonpath))
    diagnostics_path = tmp_path / 'private-diagnostics'
    with diagnostics_path.open('wb') as diagnostics:
        assert agent_run._invoke(tmp_path / 'unused.yaml', False, env, diagnostics) == 0
    assert diagnostics_path.read_text().strip() == 'INSTALLED_SENTINEL_ONLY'
    # Prove PYTHONPATH cannot inject when no caller package masks it either.
    monkeypatch.chdir(tmp_path)
    with diagnostics_path.open('wb') as diagnostics:
        assert agent_run._invoke(tmp_path / 'unused.yaml', False, env, diagnostics) == 0
    assert diagnostics_path.read_text().strip() == 'INSTALLED_SENTINEL_ONLY'


def test_sidecar_same_bytes_resolution_swap_is_rejected(setup_run, monkeypatch, capsys):
    profile, _ = setup_run
    original_read = Path.read_bytes
    original_resolve = Path.resolve
    swapped = False
    outside = profile.parent / 'outside.status.json'
    def read_bytes(path):
        nonlocal swapped
        blob = original_read(path)
        if path.name == 'brief.status.json':
            outside.write_bytes(blob)
            swapped = True
        return blob
    def resolve(path, *args, **kwargs):
        if path.name == 'brief.status.json' and swapped:
            return original_resolve(outside, *args, **kwargs)
        return original_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_bytes', read_bytes)
    monkeypatch.setattr(Path, 'resolve', resolve)
    assert agent_run.main(['--config', str(profile)]) == 1
    assert 'artifact' not in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize('swap_during_read', [False, True])
def test_sidecar_symlink_outside_media(setup_run, monkeypatch, capsys, swap_during_read):
    profile, _ = setup_run
    original_invoke = agent_run._invoke
    original_read = Path.read_bytes
    outside = profile.parent / 'outside.status.json'
    def swap(sidecar):
        outside.write_bytes(original_read(sidecar))
        sidecar.unlink()
        try:
            sidecar.symlink_to(outside)
        except OSError as exc:
            if getattr(exc, 'winerror', None) == 1314 or exc.errno in (1, 38, 95):
                pytest.skip('filesystem does not permit symlinks: ' + str(exc))
            raise
    def invoke(*args):
        code = original_invoke(*args)
        if not swap_during_read:
            swap(profile.parent / 'data/media/brief.status.json')
        return code
    swapped = False
    def read_bytes(path):
        nonlocal swapped
        blob = original_read(path)
        if swap_during_read and path.name == 'brief.status.json' and not swapped:
            swapped = True
            swap(path)
        return blob
    monkeypatch.setattr(agent_run, '_invoke', invoke)
    monkeypatch.setattr(Path, 'read_bytes', read_bytes)
    assert agent_run.main(['--config', str(profile)]) == 1
    assert 'artifact' not in json.loads(capsys.readouterr().out)


def test_artifact_symlink_initially_outside_media(setup_run, monkeypatch, capsys):
    profile, _ = setup_run
    original_invoke = agent_run._invoke
    def invoke(*args):
        code = original_invoke(*args)
        artifact = profile.parent / 'data/media/brief.html'
        outside = profile.parent / 'outside.html'
        outside.write_bytes(artifact.read_bytes())
        artifact.unlink()
        try:
            artifact.symlink_to(outside)
        except OSError as exc:
            if getattr(exc, 'winerror', None) == 1314 or exc.errno in (1, 38, 95):
                pytest.skip('filesystem does not permit symlinks: ' + str(exc))
            raise
        return code
    monkeypatch.setattr(agent_run, '_invoke', invoke)
    assert agent_run.main(['--config', str(profile)]) == 1
    assert 'artifact' not in json.loads(capsys.readouterr().out)
