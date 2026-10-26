"""Public fixtures use synthetic identities and article text only."""
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import check_coverage_history as history
from test_replay import sample


def pin(folder, mode='forced'):
    path = folder / 'manifest.json'
    manifest = json.loads(path.read_bytes())
    manifest.update(mode=mode, source_commit='a' * 40)
    path.write_text(json.dumps(manifest))
    return {'capsule': str(folder), 'date': manifest['started_at'][:10], 'mode': mode,
            'expected_source_commit': 'a' * 40,
            'files': {name: history.digest((folder / name).read_bytes())
                      for name in ('manifest.json', 'selection-input.json', 'render-input.json')}}


def test_forced_capture_is_verified_but_never_becomes_scheduled(sample):
    folder = sorted(sample[0].iterdir())[0]
    entry = pin(folder)
    assert history.load_capture(entry)['mode'] == 'forced'
    assert json.loads((folder / 'manifest.json').read_bytes())['mode'] == 'forced'


@pytest.mark.parametrize('mutation', ['date', 'source', 'dirty', 'clock', 'body'])
def test_semantic_or_content_changes_rejected_even_with_updated_pin(sample, mutation):
    folder = sorted(sample[0].iterdir())[0]
    entry = pin(folder)
    if mutation == 'date': entry['date'] = '2026-09-31'
    elif mutation == 'source': entry['expected_source_commit'] = 'b' * 40
    elif mutation == 'dirty':
        path = folder / 'manifest.json'
        data = json.loads(path.read_bytes()); data['source_dirty'] = True
        path.write_text(json.dumps(data)); entry['files']['manifest.json'] = history.digest(path.read_bytes())
    elif mutation == 'clock':
        path = folder / 'render-input.json'
        data = json.loads(path.read_bytes()); data['clock']['local_date'] = '2026-09-02'
        blob = json.dumps(data).encode(); path.write_bytes(blob)
        path = folder / 'manifest.json'; data = json.loads(path.read_bytes())
        data['files']['render-input.json'] = {'bytes': len(blob), 'sha256': history.digest(blob)}
        path.write_text(json.dumps(data))
        entry['files']['manifest.json'] = history.digest(path.read_bytes())
        entry['files']['render-input.json'] = history.digest(blob)
    else: (folder / 'selection-input.json').write_text('modified')
    with pytest.raises(ValueError): history.load_capture(entry)


def test_positive_expectations_reject_empty_recognition_or_wrong_groups():
    case = {'case_id': 'case', 'candidate': {
        'selection_audit': [{'url': 'a', 'recognition_reason': 'shooting'},
                            {'url': 'b', 'recognition_reason': 'shooting'}],
        'protected_events': [{'urls': ['a', 'b']}], 'coverage_eligibility_audit': []}}
    assertions = [{'case_id': 'case', 'kind': k, 'urls': ['a', 'b']} for k in ('recognized', 'protected')]
    assert all(row['passed'] for row in history.check_expectations([case], assertions))
    case['candidate']['protected_events'] = []
    assert not all(row['passed'] for row in history.check_expectations([case], assertions))
    case['candidate']['selection_audit'] = []
    assert not any(row['passed'] for row in history.check_expectations([case], assertions))


def test_real_offline_worker_uses_explicit_ranking_override(tmp_path, synthetic_config):
    import yaml
    from worldintel.core import select_news
    root = Path(__file__).resolve().parents[1]
    folder = tmp_path / '2026-09-01T070000+0900-12345678'
    folder.mkdir()
    settings = select_news.selection_settings()
    selection = {'today': '2026-09-01', 'as_of': '2026-09-01T07:00:00+09:00', 'seen': [],
                 'selection_settings': settings, 'bundle': {'date': '2026-09-01',
                 'as_of': '2026-09-01T07:00:00+09:00', 'ranking_profile': settings['ranking_profile'], 'news': []}}
    render = {'date': '2026-09-01', 'bundle': {}, 'analysis': {},
              'clock': {'local_date': '2026-09-01', 'generated_at': '2026-09-01T07:01:00+09:00', 'utc_offset': '+09:00'}}
    files = {}
    for name, payload in zip(history.replay.BOUNDARIES, (selection, render)):
        blob = json.dumps(payload).encode(); (folder / name).write_bytes(blob)
        files[name] = {'bytes': len(blob), 'sha256': history.digest(blob)}
    (folder / 'manifest.json').write_text(json.dumps({'format_version': 1, 'capsule_id': folder.name,
        'started_at': '2026-09-01T07:00:00+09:00', 'mode': 'forced', 'source_commit': 'a' * 40,
        'source_dirty': False, 'errors': [], 'boundaries': history.replay.BOUNDARIES, 'files': files}))
    entry = pin(folder)
    profile = tmp_path / 'profile.yaml'
    raw = copy.deepcopy(synthetic_config.raw)
    raw['ranking'] = {'major_events': {'max_events': 3}}
    profile.write_text(yaml.safe_dump(raw))
    result = history.run_worker(root, entry, profile)
    assert result['ranking_profile']['major_events']['max_events'] == 3
    assert result['profile_changed_from_capture'] is True
    assert result['counts'] == {desk: 0 for desk in settings['desks']}


def test_worker_blocks_network_attempt_on_import(sample, tmp_path):
    folder = sorted(sample[0].iterdir())[0]
    entry = pin(folder)
    root = sample[1]
    (root / 'scripts/select_news.py').write_text(
        "import socket\nsocket.getaddrinfo('example.org', 443)\n")
    profile = tmp_path / 'synthetic.yaml'; profile.write_text('synthetic: true')
    with pytest.raises(history.HistoryError, match='network prohibited'):
        history.run_worker(root, entry, profile)


def test_every_desk_and_order_change_needs_exact_review():
    before = {'world': ['https://example.org/a'], 'finance': ['https://example.org/b']}
    after = {'world': ['https://example.org/c'], 'finance': ['https://example.org/d']}
    differences = history.selection_differences(before, after)
    assert {d['desk'] for d in differences} == {'world', 'finance'}
    assert history.review_differences('case', differences, []) == 2
    reviews = [{'case_id': 'case', 'desk': d['desk'], 'change_sha256': d['change_sha256'],
                'decision': 'intended', 'reason': 'Reviewed specific replacement.',
                'reviewed_by': 'synthetic reviewer'} for d in differences]
    assert history.review_differences('case', differences, reviews) == 0
    reviews[0]['change_sha256'] = 'a' * 64
    assert history.review_differences('case', differences, reviews) == 1
    assert history.selection_differences({'world': ['a', 'b']}, {'world': ['b', 'a']})


def test_missing_and_short_archived_bodies_are_unavailable():
    assert history.body_availability(['a', 'b', 'c'], {'b': 'navigation', 'c': 'reported ' * 120}) == {
        'a': 'unavailable', 'b': 'unavailable', 'c': 'available'}


def test_capture_pin_rejects_changed_manifest_before_parsing(tmp_path):
    path = tmp_path / 'case'
    path.mkdir()
    (path / 'manifest.json').write_text('{}')
    entry = {'capsule': str(path), 'date': '2026-10-11', 'mode': 'forced',
             'expected_source_commit': 'a' * 40,
             'files': {'manifest.json': 'b' * 64, 'selection-input.json': 'c' * 64,
                       'render-input.json': 'd' * 64}}
    with pytest.raises(ValueError, match='integrity'): history.load_capture(entry)


def test_report_summary_cannot_hide_unreviewed_selection():
    report = {'schema_version': 1, 'kind': 'coverage-regression',
              'candidate_commit': 'a' * 40, 'candidate_profile_sha256': 'b' * 64,
              'status': 'PASS', 'historical_end_to_end': 'NOT_EXECUTED',
              'cases': [{'case_id': 'case', 'date': '2026-10-11', 'mode': 'scheduled',
                         'differences': [{'desk': 'world', 'before': ['a'], 'after': ['b'],
                                          'change_sha256': 'c' * 64}], 'reviews': [],
                         'body_availability': {}}]}
    with pytest.raises(ValueError): history.validate_report(report, 'a' * 40, 'b' * 64)


def test_real_report_validation_recomputes_positive_and_difference_results(sample, tmp_path, monkeypatch):
    entry = pin(sorted(sample[0].iterdir())[0])
    pin_path = tmp_path / 'pin.json'
    pin_path.write_text(json.dumps({'version': 1, 'baseline_commit': 'a' * 40, 'capsules': [entry]}))
    baseline_profile, candidate_profile = [tmp_path / name for name in ('baseline.yaml', 'candidate.yaml')]
    baseline_profile.write_text('synthetic baseline'); candidate_profile.write_text('synthetic candidate')
    expectations = tmp_path / 'expectations.json'
    expectations.write_text(json.dumps({'version': 1, 'input_pin_sha256': history.digest(pin_path.read_bytes()),
        'candidate_profile_sha256': history.digest(candidate_profile.read_bytes()),
        'assertions': [{'case_id': Path(entry['capsule']).name, 'kind': k, 'urls': ['a', 'b']}
                       for k in ('recognized', 'protected')]}))
    strict = tmp_path / 'strict.json'
    strict.write_text(json.dumps({'equivalent': False, 'days': 5, 'results': [{}] * 5}))
    monkeypatch.setattr(history, 'source', lambda root, commit: Path(root))
    def worker(*args):
        return {'selection': {'world': []}, 'selection_sha256': history.digest({'world': []}),
                'counts': {'world': 0}, 'ranking_profile': {}, 'ranking_profile_hash': 'c' * 64,
                'profile_changed_from_capture': True, 'origin': 'synthetic',
                'protected_events': [{'urls': ['a', 'b']}], 'coverage_eligibility_audit': [],
                'selection_audit': [{'url': u, 'recognition_reason': 'serious_shooting'} for u in ('a', 'b')]}
    monkeypatch.setattr(history, 'run_worker', worker)
    report = history.evaluate(pin_path, sample[1], 'a' * 40, baseline_profile, sample[2], 'b' * 40,
                              candidate_profile, expectations, strict)
    assert report['status'] == 'PASS'
    assert history.validate_report(report, 'b' * 40, history.digest(candidate_profile.read_bytes()),
                                   expectations=json.loads(expectations.read_bytes()))
    bad = copy.deepcopy(report)
    bad['cases'][0]['candidate']['protected_events'] = []
    with pytest.raises(ValueError, match='expectations'): history.validate_report(bad, 'b' * 40, report['candidate_profile_sha256'])
    bad = copy.deepcopy(report)
    bad['cases'][0]['candidate']['selection'] = {'world': ['unrelated']}
    with pytest.raises(ValueError, match='integrity'): history.validate_report(bad, 'b' * 40, report['candidate_profile_sha256'])
    strict.write_text('modified after evaluation')
    with pytest.raises(ValueError): history.validate_report(report, 'b' * 40, report['candidate_profile_sha256'])


def test_hashed_external_expectations_cannot_be_substituted_in_report():
    original = [{'case_id': 'case', 'kind': k, 'urls': ['a', 'b']} for k in ('recognized', 'protected')]
    report = {'input_pin_sha256': 'a' * 64, 'candidate_profile_sha256': 'b' * 64,
              'expectations': [{'case_id': 'case', 'kind': k, 'urls': ['x', 'y'], 'passed': True}
                               for k in ('recognized', 'protected')],
              'cases': [{'case_id': 'case', 'candidate': {'protected_events': [{'urls': ['x', 'y']}],
                         'selection_audit': [{'url': u, 'recognition_reason': 'shooting'} for u in ('x', 'y')],
                         'coverage_eligibility_audit': []}, 'differences': []}]}
    payload = {'version': 1, 'input_pin_sha256': 'a' * 64, 'candidate_profile_sha256': 'b' * 64, 'assertions': original}
    with pytest.raises(ValueError): history.validate_review_inputs(report, payload, None)


@pytest.mark.parametrize('mutation', ['missing', 'fabricated', 'wrong_binding', 'valid'])
def test_genuine_disposition_artifact_required_for_every_reviewed_change(mutation):
    change = history.selection_differences({'world': ['a']}, {'world': ['b']})[0]
    review = {'case_id': 'case', 'desk': 'world', 'change_sha256': change['change_sha256'],
              'decision': 'intended', 'reason': 'Evidence reviewed.', 'reviewed_by': 'synthetic owner'}
    change['review'] = review
    binding = {k: ('a' * 40 if k.endswith('commit') else 'b' * 64) for k in
               ('baseline_commit', 'candidate_commit', 'baseline_profile_sha256', 'candidate_profile_sha256',
                'input_pin_sha256', 'expectations_sha256', 'strict_equivalence_sha256')}
    candidate = {'selection': {'world': ['b']}, 'protected_events': [{'urls': ['a', 'b']}],
                 'selection_audit': [{'url': u, 'recognition_reason': 'shooting'} for u in ('a', 'b')],
                 'coverage_eligibility_audit': []}
    assertions = [{'case_id': 'case', 'kind': k, 'urls': ['a', 'b']} for k in ('recognized', 'protected')]
    report = {**binding, 'dispositions_sha256': 'c' * 64,
              'expectations': [{**a, 'passed': True} for a in assertions],
              'cases': [{'case_id': 'case', 'candidate': candidate, 'baseline': {'selection': {'world': ['a']}},
                         'differences': [change], 'unreviewed_changes': 0}]}
    payload = {'version': 1, 'input_pin_sha256': binding['input_pin_sha256'],
               'candidate_profile_sha256': binding['candidate_profile_sha256'], 'assertions': assertions}
    disposition = {'version': 1, 'binding': dict(binding), 'reviews': [dict(review)]}
    if mutation == 'missing': disposition = None
    elif mutation == 'fabricated': disposition['reviews'][0]['decision'] = 'unintended'
    elif mutation == 'wrong_binding': disposition['binding']['candidate_commit'] = 'f' * 40
    if mutation == 'valid':
        assert history.validate_review_inputs(report, payload, disposition)
    else:
        with pytest.raises(ValueError): history.validate_review_inputs(report, payload, disposition)


@pytest.mark.parametrize("as_of", ["malformed", "2026-09-01T07:00:00", "2026-08-31T07:00:00+09:00", "2026-09-01T07:00:00+08:00", "2026-09-01T07:00:00-00:00"])
def test_selection_clock_semantics_fail_closed(sample, as_of):
    folder = sorted(sample[0].iterdir())[0]
    path = folder / "selection-input.json"
    data = json.loads(path.read_bytes())
    data["as_of"] = data["bundle"]["as_of"] = as_of
    blob = json.dumps(data).encode(); path.write_bytes(blob)
    path = folder / "manifest.json"
    data = json.loads(path.read_bytes())
    data["files"]["selection-input.json"] = {"bytes": len(blob), "sha256": history.digest(blob)}
    path.write_text(json.dumps(data))
    with pytest.raises(history.HistoryError, match="selection clock"):
        history.load_capture(pin(folder))


def test_selection_clock_accepts_consistent_portable_utc_capture(sample):
    folder = sorted(sample[0].iterdir())[0]
    renamed = folder.with_name(folder.name.replace('+0900', '+0000'))
    folder.rename(renamed)
    manifest_path = renamed / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    manifest['started_at'] = manifest['started_at'].replace('+09:00', '+00:00')
    manifest['capsule_id'] = renamed.name
    for name in history.replay.BOUNDARIES:
        path = renamed / name
        blob = path.read_text().replace('+09:00', '+00:00').encode()
        path.write_bytes(blob)
        manifest['files'][name] = {'bytes': len(blob), 'sha256': history.digest(blob)}
    manifest_path.write_text(json.dumps(manifest))
    assert history.load_capture(pin(renamed))['date'] == '2026-09-01'
