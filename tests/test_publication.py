"""Publication checks use local synthetic repositories and mocked read-only GH."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
from model_evidence_fixture import model_evidence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import check_publication as p

HEAD = 'a' * 40
CANDIDATE = 'b' * 40


def receipt(directory, name, content=None):
    path = directory / name
    blob = (content or name).encode()
    path.write_bytes(blob)
    return {'artifact': name, 'sha256': hashlib.sha256(blob).hexdigest()}


@pytest.fixture
def evidence(tmp_path):
    commands = {'tests': ['python', '-m', 'pytest', '-q'],
                'install-windows': ['python', '-m', 'pip', 'install', '.'],
                'install-linux': ['python', '-m', 'pip', 'install', '.'],
                'demo-offline': ['python', '-m', 'worldintel.cli', 'demo'],
                'minimal-dry-run': ['python', '-m', 'worldintel.cli', 'run', '--dry'],
                'architecture': ['python', 'tools/check_architecture.py'],
                'l0': ['python', 'tools/check_l0.py'],
                'replay': ['python', 'tools/check_equivalence.py']}
    data = {'version': 1, 'head': HEAD, 'candidate': CANDIDATE,
            'runs': [{'date': f'2026-09-{day:02}', 'candidate': CANDIDATE, 'mode': 'scheduled',
                      'status': 'success', 'owner_accepted': True,
                      **receipt(tmp_path, f'run-{day}.log')} for day in range(1, 8)],
            'evidence': {name: {'head': HEAD, 'exit_code': 0, 'command': commands[name],
                                **({'platform': name.removeprefix('install-')} if name.startswith('install-') else {}),
                                **receipt(tmp_path, name + '.log')} for name in p.EVIDENCE},
            'approvals': {name: {'head': HEAD, 'approved': True, 'approved_by': 'synthetic owner',
                                 'statement': 'Reviewed this individual gate.'} for name in p.MANUAL}}
    return data, tmp_path


def test_seven_unique_consecutive_scheduled_runs(evidence):
    p.scheduled_runs(*evidence)


@pytest.mark.parametrize('change', ['duplicate', 'gap', 'reordered', 'forced', 'failed', 'unaccepted', 'other_sha', 'short_sha', 'same_receipt', 'corrupt'])
def test_invalid_acceptance_runs_fail(evidence, change):
    data, directory = evidence
    run = data['runs'][3]
    if change == 'duplicate': run['date'] = data['runs'][2]['date']
    elif change == 'gap': run['date'] = '2026-09-19'
    elif change == 'reordered': data['runs'].reverse()
    elif change == 'forced': run['mode'] = 'forced'
    elif change == 'failed': run['status'] = 'failure'
    elif change == 'unaccepted': run['owner_accepted'] = False
    elif change == 'other_sha': run['candidate'] = HEAD
    elif change == 'short_sha': run['candidate'] = CANDIDATE[:7]
    elif change == 'same_receipt': run.update({key: data['runs'][0][key] for key in ('artifact', 'sha256')})
    else: (directory / run['artifact']).write_text('changed')
    with pytest.raises(ValueError): p.scheduled_runs(data, directory)


def test_all_manual_approvals_separate_and_bound_to_head(evidence):
    data, _ = evidence
    for name in p.MANUAL: p.manual_gate(name, data, HEAD)
    del data['approvals']['install']
    with pytest.raises(ValueError, match='MANUAL pending'): p.manual_gate('install', data, HEAD)
    with pytest.raises(ValueError, match='HEAD'): p.manual_gate('cutover', data, CANDIDATE)


def test_evidence_hash_exit_commit_command_and_minimal_limit(evidence):
    data, directory = evidence
    for name in p.EVIDENCE - {'minimal-dry-run'}: p.evidence_gate(name, data, directory, HEAD)
    with pytest.raises(ValueError, match='UNSATISFIED'): p.evidence_gate('minimal-dry-run', data, directory, HEAD)
    data['evidence']['tests']['command'] = ['echo', 'success']
    with pytest.raises(ValueError): p.evidence_gate('tests', data, directory, HEAD)
    data['evidence']['l0']['exit_code'] = 1
    with pytest.raises(ValueError): p.evidence_gate('l0', data, directory, HEAD)
    with pytest.raises(ValueError): p.evidence_gate('replay', data, directory, CANDIDATE)


def minimal_report(evidence, root, *, cleanup=False):
    data, directory = evidence
    summary = {'schema_version': 1, 'dry': True, 'source_dirty': False,
               'source_review': {'status': 'accepted', 'attempted': True, 'cards_reviewed': 1, 'repair_attempted': False, 'review_calls': 1, 'corrected_cards': 0, 'timeout_s': 100, 'reason': None,
                                  'scope': 'final full story cards: headline, why_it_matters, body; not whole-brief verification'},
               'model_evidence': model_evidence(directory / 'retained-model-evidence', cleanup=cleanup),
               'source_commit': HEAD, 'source_file': str(root / 'worldintel/run.py'),
               'generation': 'rendered', 'handoff': 'disabled', 'delivery_confirmed': False,
               'quality': {'overall': 'complete', 'unresolved_coverage_events': 0,
                           'scope': 'recognized registered events; not complete major-event recall'},
               'analysis': {'overall': 'complete', 'passes': {
                   name: {'success': True, 'skipped': False, 'timed_out': False, 'error': None}
                   for name in ('world', 'finance')}},
               'rewrite': {'attempted': False, 'error': None, 'skipped': False, 'remaining': 0},
               'rendered': {'chars': 10, 'text_chars': 10, 'world_cards': 1,
                            'input_news': 1, 'sha256': 'c' * 64}}
    if cleanup:
        folder = directory / 'retained-model-evidence' / ('f' * 32) / ('2' * 32)
        material = json.loads((folder / 'stdin.bin').read_text().split('\nINPUT_JSON\n')[1])
        preflight = material['citation_preflight']
        summary['source_review'].update(citation_preflight_sha256=preflight['sha256'],
                                        citation_preflight_cards=len(preflight['cards']))
    report = {'schema_version': 2, 'kind': 'minimal-live-dry-run', 'status': 'PASS',
              'model_evidence_root': str(directory / 'retained-model-evidence' / ('f' * 32)),
              'errors': [], 'source_commit': HEAD, 'source_dirty': False,
              'source_root': str(root), 'protected_paths_unchanged': True,
              'command': ['python', '-m', 'worldintel.cli', 'run', '--config',
                          str(directory / 'profile.yaml'), '--dry', '--force'],
              'exit_code': 0, 'dry_summary': summary, 'stderr_sha256': 'd' * 64}
    entry = data['evidence']['minimal-dry-run']
    entry.update(receipt(directory, 'minimal-report.json', json.dumps(report)))
    entry['command'] = ['python', 'tools/verify_minimal_run.py', '--report',
                        str(directory / entry['artifact'])]
    return report


def test_canonical_minimal_report_is_accepted_as_attestation(evidence, tmp_path):
    root = tmp_path / 'candidate'
    minimal_report(evidence, root)
    p.evidence_gate('minimal-dry-run', *evidence, HEAD, root)


def test_modified_retained_model_evidence_fails_publication(evidence, tmp_path):
    root = tmp_path / 'candidate'
    report = minimal_report(evidence, root)
    next(Path(report['model_evidence_root']).rglob('output.bin')).write_bytes(b'changed')
    with pytest.raises(ValueError, match='model evidence'):
        p.evidence_gate('minimal-dry-run', *evidence, HEAD, root)


@pytest.mark.parametrize('kind', ['corrupt', 'wrong_sha', 'exit', 'exit_type', 'degraded',
                                 'dirty', 'protect', 'schema_type', 'wrong_root',
                                 'fake_summary', 'mismatched_report', 'demo', 'no_force'])
def test_invalid_minimal_report_fails_closed(evidence, tmp_path, kind):
    root = tmp_path / 'candidate'
    report = minimal_report(evidence, root)
    data, directory = evidence
    entry = data['evidence']['minimal-dry-run']
    if kind == 'wrong_sha': report['source_commit'] = CANDIDATE
    elif kind == 'exit': report['exit_code'] = 1
    elif kind == 'exit_type': report['exit_code'] = False
    elif kind == 'degraded': report['dry_summary']['analysis']['overall'] = 'degraded'
    elif kind == 'dirty': report['source_dirty'] = True
    elif kind == 'protect': report['protected_paths_unchanged'] = False
    elif kind == 'schema_type': report['schema_version'] = True
    elif kind == 'wrong_root': report['source_root'] = str(directory / 'other')
    elif kind == 'fake_summary': report['dry_summary'] = {'status': 'PASS'}
    elif kind == 'mismatched_report': entry['command'][-1] = str(directory / 'other.json')
    elif kind == 'demo': report['command'][3] = 'demo'
    elif kind == 'no_force': report['command'].remove('--force')
    entry.update(receipt(directory, 'minimal-report.json', json.dumps(report)))
    if kind == 'corrupt': (directory / entry['artifact']).write_text('{}')
    with pytest.raises(ValueError): p.evidence_gate('minimal-dry-run', data, directory, HEAD, root)


def test_manifest_exact_schema_and_external_location(evidence):
    data, directory = evidence
    root = directory / 'repository'
    root.mkdir()
    path = directory / 'evidence.json'
    path.write_text(json.dumps(data))
    assert p.load_manifest(path, root, HEAD)[0] == data
    data['confirm_all'] = True
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError): p.load_manifest(path, root, HEAD)
    inside = root / 'evidence.json'
    inside.write_text('{}')
    with pytest.raises(ValueError, match='outside'): p.load_manifest(inside, root, HEAD)


def ci_fixture(monkeypatch, runs=None, jobs=None):
    runs = runs if runs is not None else [{'id': 1, 'head_sha': HEAD, 'status': 'completed', 'conclusion': 'success', 'name': 'CI'}]
    jobs = jobs if jobs is not None else [{'name': name, 'status': 'completed', 'conclusion': 'success'} for name in sorted(p.MATRIX)]
    calls = []
    def command(root, *args):
        calls.append(args)
        assert args[:4] == ('gh', 'api', '--paginate', '--slurp')
        payload = {'total_count': len(jobs), 'jobs': jobs} if '/jobs?' in args[-1] else {'total_count': len(runs), 'workflow_runs': runs}
        return json.dumps([payload]).encode()
    monkeypatch.setattr(p, 'command', command)
    return runs, jobs, calls


def test_exact_matrix_ci_reads_only(monkeypatch, tmp_path):
    _, _, calls = ci_fixture(monkeypatch)
    p.ci_state(tmp_path, 'owner/repository', HEAD)
    assert len(calls) == 2 and 'head_sha=' + HEAD in calls[0][-1]


@pytest.mark.parametrize('kind', ['missing', 'red', 'pending', 'skipped', 'wrong_head', 'no_runs', 'wrong_workflow'])
def test_ci_incomplete_or_wrong_commit_fails(monkeypatch, tmp_path, kind):
    runs, jobs, _ = ci_fixture(monkeypatch)
    if kind == 'missing': jobs.pop()
    elif kind == 'red': jobs[0]['conclusion'] = 'failure'
    elif kind == 'pending': jobs[0]['status'] = 'in_progress'
    elif kind == 'skipped': jobs[0]['conclusion'] = 'skipped'
    elif kind == 'wrong_head': runs[0]['head_sha'] = CANDIDATE
    elif kind == 'wrong_workflow': runs[0]['name'] = 'Unrelated'
    else: runs.clear()
    with pytest.raises(ValueError): p.ci_state(tmp_path, 'owner/repository', HEAD)


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    p.git(root, 'init')
    p.git(root, 'config', 'user.name', 'Synthetic')
    p.git(root, 'config', 'user.email', 'test@example.com')
    (root / 'clean.txt').write_text('clean')
    p.git(root, 'add', '.')
    p.git(root, 'commit', '-m', 'clean')
    return root


def test_full_reachable_history_clean(repository):
    assert p.history_scan(repository) > 0


@pytest.mark.parametrize('location', ['deleted_blob', 'path', 'commit', 'tag', 'binary'])
def test_history_scans_deleted_content_names_and_metadata(repository, location):
    secret = 'sk-' + 'A' * 24
    if location in ('deleted_blob', 'binary'):
        (repository / 'secret.bin').write_bytes((b'\0' if location == 'binary' else b'') + secret.encode())
        p.git(repository, 'add', '.')
        p.git(repository, 'commit', '-m', 'add')
        p.git(repository, 'rm', 'secret.bin')
        p.git(repository, 'commit', '-m', 'remove')
    elif location == 'path':
        (repository / (secret + '.txt')).write_text('clean')
        p.git(repository, 'add', '.')
        p.git(repository, 'commit', '-m', 'add')
    elif location == 'commit': p.git(repository, 'commit', '--allow-empty', '-m', secret)
    else: p.git(repository, 'tag', '-a', 'synthetic', '-m', secret)
    with pytest.raises(ValueError, match='sensitive'): p.history_scan(repository)


def test_runtime_inventory_allows_only_prose_changes(repository):
    for name in ('worldintel/__init__.py', 'deploy/launcher.py', 'pyproject.toml', 'requirements.txt'):
        path = repository / name
        path.parent.mkdir(exist_ok=True)
        path.write_text('synthetic')
    p.git(repository, 'add', '.')
    p.git(repository, 'commit', '-m', 'runtime')
    candidate = p.git(repository, 'rev-parse', 'HEAD').decode().strip()
    (repository / 'README.md').write_text('documentation only')
    p.git(repository, 'add', '.')
    p.git(repository, 'commit', '-m', 'docs')
    head = p.git(repository, 'rev-parse', 'HEAD').decode().strip()
    p.runtime_equal(repository, head, candidate)
    (repository / 'requirements.txt').write_text('different runtime')
    p.git(repository, 'add', '.')
    p.git(repository, 'commit', '-m', 'dependencies')
    head = p.git(repository, 'rev-parse', 'HEAD').decode().strip()
    with pytest.raises(ValueError, match='inventory'): p.runtime_equal(repository, head, candidate)


def test_pending_minimal_gate_prevents_false_release_even_with_all_attestations(evidence, monkeypatch):
    data, directory = evidence
    monkeypatch.setattr(p, 'repository_state', lambda *a: (HEAD, 'owner/repo'))
    monkeypatch.setattr(p, 'ci_state', lambda *a: None)
    monkeypatch.setattr(p, 'history_scan', lambda *a: 1)
    monkeypatch.setattr(p, 'runtime_equal', lambda *a: None)
    for name in ('workflow_policy', 'dependency_policy', 'documentation_policy'):
        monkeypatch.setattr(p, name, lambda *a: None)
    monkeypatch.setattr(p, 'load_manifest', lambda *a: (data, directory))
    result = p.check(directory, 'owner/repo', directory / 'evidence.json')
    assert not result['publication_ready']
    failed = [row for row in result['gates'] if row['status'] == 'FAIL']
    assert [row['gate'] for row in failed] == ['minimal-dry-run']
    assert {row['gate'] for row in result['gates'] if row['status'] == 'ATTESTED'} == (
        p.EVIDENCE - {'minimal-dry-run'}) | p.MANUAL | {'seven-scheduled-runs'}


def test_valid_minimal_attestation_still_cannot_authorize_publication(evidence, monkeypatch):
    data, directory = evidence
    root = directory / 'candidate'
    minimal_report(evidence, root)
    monkeypatch.setattr(p, 'repository_state', lambda *a: (HEAD, 'owner/repo'))
    for name in ('ci_state', 'history_scan', 'runtime_equal', 'workflow_policy',
                 'dependency_policy', 'documentation_policy'):
        monkeypatch.setattr(p, name, lambda *a: None)
    monkeypatch.setattr(p, 'load_manifest', lambda *a: (data, directory))
    result = p.check(root, 'owner/repo', directory / 'evidence.json')
    assert not result['publication_ready']
    assert {row['gate'] for row in result['gates'] if row['status'] == 'ATTESTED'} == (
        p.EVIDENCE | p.MANUAL | {'seven-scheduled-runs'})
    assert not any(row['status'] == 'FAIL' for row in result['gates'])


def test_missing_git_fails_closed_with_pending_manual_gates(monkeypatch, tmp_path):
    def fail(*args): raise FileNotFoundError('private path must not print')
    monkeypatch.setattr(p, 'command', fail)
    result = p.check(tmp_path, 'owner/repo', tmp_path / 'absent.json')
    assert not result['publication_ready']
    assert 'private path' not in json.dumps(result)
    assert {row['gate'] for row in result['gates'] if row['status'] == 'PENDING'} >= p.MANUAL


@pytest.mark.parametrize('value', ['https://attacker.invalid/owner/repo', 'https://github.com/owner/repo/extra', 'owner/repository-evil'])
def test_remote_identity_is_not_substring_matching(value):
    try: actual = p.github_repo(value)
    except ValueError: return
    assert actual != 'owner/repo'


def state_fixture(monkeypatch, tmp_path):
    outputs = {
        ('status', '--porcelain=v1', '--untracked-files=all'): b'',
        ('rev-parse', '--is-shallow-repository'): b'false\n',
        ('for-each-ref', '--format=%(refname)', 'refs/replace'): b'',
        ('rev-parse', '--verify', 'HEAD^{commit}'): HEAD.encode(),
        ('symbolic-ref', '--short', 'HEAD'): b'main',
        ('config', '--get', 'branch.main.remote'): b'origin',
        ('config', '--get', 'branch.main.merge'): b'refs/heads/main',
        ('remote', 'get-url', 'origin'): b'https://github.com/owner/repo.git',
        ('remote', 'get-url', '--push', 'origin'): b'git@' + b'github.com:owner/repo.git',
        ('rev-parse', '@{upstream}'): HEAD.encode(),
        ('ls-remote', '--exit-code', 'origin', 'refs/heads/main'): (HEAD + '\trefs/heads/main\n').encode(),
    }
    for name in p.REQUIRED_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'committed')
        outputs[('show', HEAD + ':' + name)] = b'committed'
    monkeypatch.setattr(p, 'git', lambda root, *args: outputs[args])
    return outputs


def test_repository_must_be_clean_committed_and_exact_remote_head(monkeypatch, tmp_path):
    state_fixture(monkeypatch, tmp_path)
    assert p.repository_state(tmp_path, 'owner/repo') == (HEAD, 'owner/repo')


@pytest.mark.parametrize('kind', ['dirty', 'shallow', 'upstream', 'origin', 'push', 'remote_head', 'uncommitted_tool'])
def test_repository_gate_rejects_local_only_or_wrong_target(monkeypatch, tmp_path, kind):
    outputs = state_fixture(monkeypatch, tmp_path)
    if kind == 'dirty': outputs[('status', '--porcelain=v1', '--untracked-files=all')] = b' M code.py'
    elif kind == 'shallow': outputs[('rev-parse', '--is-shallow-repository')] = b'true'
    elif kind == 'upstream': outputs[('config', '--get', 'branch.main.remote')] = b'other'
    elif kind == 'origin': outputs[('remote', 'get-url', 'origin')] = b'https://github.com/owner/repo-evil.git'
    elif kind == 'push': outputs[('remote', 'get-url', '--push', 'origin')] = b'https://github.com/owner/other.git'
    elif kind == 'remote_head': outputs[('ls-remote', '--exit-code', 'origin', 'refs/heads/main')] = (CANDIDATE + '\trefs/heads/main\n').encode()
    else: (tmp_path / 'tools/check_publication.py').write_bytes(b'uncommitted')
    with pytest.raises(ValueError): p.repository_state(tmp_path, 'owner/repo')


def test_unreadable_history_object_is_not_clean(monkeypatch, tmp_path):
    def git(root, *args):
        if args[0] == 'rev-list': return HEAD.encode()
        raise subprocess.CalledProcessError(128, 'git')
    monkeypatch.setattr(p, 'git', git)
    with pytest.raises(subprocess.CalledProcessError): p.history_scan(tmp_path)


def test_manifest_cannot_confirm_everything_with_one_boolean(evidence):
    data, directory = evidence
    data['approvals'] = {'all': True}
    path = directory / 'evidence.json'
    path.write_text(json.dumps(data))
    root = directory / 'repository'
    root.mkdir()
    with pytest.raises(ValueError, match='unknown manual'): p.load_manifest(path, root, HEAD)


@pytest.mark.parametrize('args', [['--collect-only'], ['--help'], ['-k', 'single'], ['-q', '--ignore=tests'], ['--lf']])
def test_test_attestation_cannot_skip_execution(evidence, args):
    data, directory = evidence
    data['evidence']['tests']['command'] = ['python', '-m', 'pytest', *args]
    with pytest.raises(ValueError): p.evidence_gate('tests', data, directory, HEAD)


@pytest.mark.parametrize('args', [['--dry-run', '.'], ['--help', '.'], ['--no-deps', '.'], ['-e', '.']])
def test_install_attestation_cannot_skip_real_full_install(evidence, args):
    data, directory = evidence
    data['evidence']['install-windows']['command'] = ['python', '-m', 'pip', 'install', *args]
    with pytest.raises(ValueError): p.evidence_gate('install-windows', data, directory, HEAD)


def test_install_platform_claim_must_match_named_gate(evidence):
    data, directory = evidence
    data['evidence']['install-windows']['platform'] = 'linux'
    with pytest.raises(ValueError, match='platform'): p.evidence_gate('install-windows', data, directory, HEAD)


def policy_files(tmp_path):
    workflow = {'permissions': {'contents': 'read'}, 'jobs': {'test': {'steps': [
        {'uses': 'actions/checkout@' + 'a' * 40, 'with': {'persist-credentials': False}},
        {'uses': 'actions/setup-python@' + 'b' * 40}]}}}
    path = tmp_path / '.github/workflows/ci.yml'
    path.parent.mkdir(parents=True)
    path.write_text(p.yaml.safe_dump(workflow))
    (tmp_path / 'requirements.txt').write_text('requests==1.0\n')
    (tmp_path / 'requirements.lock').write_text('requests==1.0 --hash=sha256:' + 'a' * 64 + '\nsetuptools==1.0 --hash=sha256:' + 'b' * 64 + '\n')
    (tmp_path / 'pyproject.toml').write_text('[build-system]\nrequires=["setuptools==1.0"]\n')
    return workflow, path


def test_pinned_read_only_actions_and_hashed_dependencies_pass(tmp_path):
    policy_files(tmp_path)
    p.workflow_policy(tmp_path)
    p.dependency_policy(tmp_path)


@pytest.mark.parametrize('kind', ['action_tag', 'permissions', 'job_permissions', 'credentials'])
def test_workflow_policy_rejects_unpinned_or_privileged_actions(tmp_path, kind):
    workflow, path = policy_files(tmp_path)
    if kind == 'action_tag': workflow['jobs']['test']['steps'][1]['uses'] = 'actions/setup-python@v6'
    elif kind == 'permissions': workflow['permissions']['contents'] = 'write'
    elif kind == 'job_permissions': workflow['jobs']['test']['permissions'] = {'contents': 'read', 'issues': 'write'}
    else: workflow['jobs']['test']['steps'][0]['with']['persist-credentials'] = True
    path.write_text(p.yaml.safe_dump(workflow))
    with pytest.raises(ValueError): p.workflow_policy(tmp_path)


@pytest.mark.parametrize('kind', ['hash', 'direct_pin', 'backend_pin', 'backend_missing', 'direct_missing'])
def test_dependency_policy_rejects_incomplete_lock_and_backend(tmp_path, kind):
    policy_files(tmp_path)
    if kind == 'hash': (tmp_path / 'requirements.lock').write_text('requests==1.0\n')
    elif kind == 'direct_pin': (tmp_path / 'requirements.txt').write_text('requests>=1.0\n')
    elif kind == 'backend_pin': (tmp_path / 'pyproject.toml').write_text('[build-system]\nrequires=["setuptools"]\n')
    elif kind == 'backend_missing': (tmp_path / 'pyproject.toml').write_text('[build-system]\nrequires=["setuptools==9.0"]\n')
    else: (tmp_path / 'requirements.txt').write_text('requests==2.0\n')
    with pytest.raises(ValueError): p.dependency_policy(tmp_path)


def test_documentation_policy_checks_license_and_fixture_provenance(tmp_path):
    for name in ('README.md', 'CONTRIBUTING.md', 'SECURITY.md', 'docs/architecture.md'):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text('Synthetic useful documentation')
    (tmp_path / 'LICENSE').write_text('MIT License\nPermission is hereby granted, free of charge\nTHE SOFTWARE IS PROVIDED')
    examples = tmp_path / 'worldintel/data/examples'
    examples.mkdir(parents=True)
    (examples / 'sample.json').write_text('{}')
    marker = examples / 'README.md'
    marker.write_text('Invented demonstration data; not a captured briefing.')
    p.documentation_policy(tmp_path)
    marker.write_text('Captured private briefing')
    with pytest.raises(ValueError): p.documentation_policy(tmp_path)
    marker.write_text('Invented demonstration data; not a captured briefing.')
    (tmp_path / 'LICENSE').write_text('')
    with pytest.raises(ValueError): p.documentation_policy(tmp_path)


def test_standalone_publication_cleanup_uses_trusted_minimal_profile(evidence, tmp_path):
    import os
    root = tmp_path / 'candidate'
    minimal_report(evidence, root, cleanup=True)
    data, directory = evidence
    payload = directory / 'publication-input.json'
    payload.write_text(json.dumps(data))
    project = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop('WORLD_INTEL_CONFIG', None)
    env.update(PYTHONPATH=os.pathsep.join((str(project), str(project / 'tools'))),
               USERPROFILE=str(tmp_path), HOME=str(tmp_path))
    code = """
import json, os, sys
from pathlib import Path
from worldintel import config
import check_publication as p
assert config._CACHE is None and 'WORLD_INTEL_CONFIG' not in os.environ
p.evidence_gate('minimal-dry-run', json.load(open(sys.argv[1])), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]))
assert config._CACHE is None
"""
    completed = subprocess.run([sys.executable, '-c', code, str(payload), str(directory), HEAD, str(root)],
                               cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
