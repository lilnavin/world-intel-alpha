"""Synthetic release evidence; never owner acceptance."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import check_publication as publication
import verify_minimal_run
from test_verify_minimal_run import evidence

HEAD = 'a' * 40


def test_minimal_requires_full_exact_commit_and_separate_quality():
    item = evidence()
    item['source_commit'] = HEAD
    item['quality'] = {'overall': 'complete', 'unresolved_coverage_events': 0,
                       'scope': 'recognized registered events; not complete major-event recall'}
    line = lambda: verify_minimal_run.PREFIX + json.dumps(item)
    assert verify_minimal_run.validate_result(0, '', line(), HEAD, Path.cwd()) == []
    item['source_commit'] = HEAD[:7]
    assert verify_minimal_run.validate_result(0, '', line(), HEAD, Path.cwd())
    item['source_commit'] = HEAD
    item['quality']['overall'] = 'incomplete'
    assert verify_minimal_run.validate_result(0, '', line(), HEAD, Path.cwd())
    del item['quality']
    assert verify_minimal_run.validate_result(0, '', line(), HEAD, Path.cwd())


def policy():
    return {'kind': 'two-consecutive-scheduled-days', 'days': 2, 'candidate': HEAD,
            'profile_sha256': 'b' * 64, 'frozen_at': '2026-09-11T12:00:00+09:00',
            'approved': True, 'approved_by': 'synthetic owner',
            'statement': 'Explicitly selected two future scheduled days.'}


def test_v2_two_days_requires_explicit_policy_and_frozen_profile(tmp_path):
    data = {'version': 2, 'head': HEAD, 'candidate': HEAD, 'acceptance_policy': policy(),
            'runs': [], 'evidence': {}, 'approvals': {}, 'coverage_regression': {}}
    for day in (12, 13):
        path = tmp_path / f'run-{day}.json'
        path.write_text(str(day))
        data['runs'].append({'date': f'2026-09-{day}', 'candidate': HEAD,
            'mode': 'scheduled', 'status': 'success', 'owner_accepted': True,
            'profile_sha256': 'b' * 64, 'quality_complete': True,
            'transport_confirmed': True, 'artifact': str(path),
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    publication.scheduled_runs(data, tmp_path)
    for mutation in ('not_approved', 'old_date', 'different_profile', 'incomplete', 'no_transport'):
        bad = copy.deepcopy(data)
        if mutation == 'not_approved': bad['acceptance_policy']['approved'] = False
        elif mutation == 'old_date': bad['runs'][0]['date'] = '2026-09-11'
        elif mutation == 'different_profile': bad['runs'][0]['profile_sha256'] = 'c' * 64
        elif mutation == 'incomplete': bad['runs'][0]['quality_complete'] = False
        else: bad['runs'][0]['transport_confirmed'] = False
        with pytest.raises(ValueError): publication.scheduled_runs(bad, tmp_path)


def test_checkout_provenance_requests_full_sha(monkeypatch):
    from types import SimpleNamespace
    from worldintel import run
    import subprocess
    seen = []
    monkeypatch.setattr(run.os.path, 'exists', lambda p: True)
    def execute(args, **kwargs):
        seen.append(args)
        return SimpleNamespace(returncode=0, stdout=HEAD if args[1] == 'rev-parse' else '')
    monkeypatch.setattr(subprocess, 'run', execute)
    assert run._source_provenance() == (HEAD, False)
    assert ['git', 'rev-parse', 'HEAD'] in seen


def test_v2_evidence_never_automatically_becomes_publication_pass(tmp_path, monkeypatch):
    data = {'version': 2, 'candidate': HEAD}
    monkeypatch.setattr(publication, 'repository_state', lambda *args: (HEAD, 'synthetic/repo'))
    monkeypatch.setattr(publication, 'load_manifest', lambda *args: (data, tmp_path))
    for name in ('history_scan', 'workflow_policy', 'dependency_policy', 'documentation_policy',
                 'ci_state', 'runtime_equal', 'scheduled_runs', 'coverage_regression', 'evidence_gate', 'manual_gate'):
        monkeypatch.setattr(publication, name, lambda *args: None)
    result = publication.check(tmp_path, 'synthetic/repo', tmp_path / 'manifest.json')
    assert result['publication_ready'] is False
    statuses = {r['gate']: r['status'] for r in result['gates']}
    assert statuses['two-scheduled-runs'] == statuses['coverage-regression'] == 'ATTESTED'
    assert 'seven-scheduled-runs' not in statuses and 'replay' not in statuses


def test_v2_unknown_policy_or_version_fail_closed(tmp_path):
    root = tmp_path / 'repo'; root.mkdir()
    path = tmp_path / 'manifest.json'
    data = {'version': 2, 'head': HEAD, 'candidate': HEAD, 'runs': [], 'evidence': {}, 'approvals': {},
            'coverage_regression': {}, 'acceptance_policy': policy()}
    path.write_text(json.dumps(data))
    assert publication.load_manifest(path, root, HEAD)[0] == data
    data['acceptance_policy']['days'] = True
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError): publication.load_manifest(path, root, HEAD)
