import json
from pathlib import Path
import sys

import pytest
from model_evidence_fixture import model_evidence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def evidence():
    return {"schema_version": 1, "dry": True, "source_commit": "a" * 40,
            "source_review": {"status": "accepted", "attempted": True, "cards_reviewed": 1, "repair_attempted": False, "review_calls": 1, "corrected_cards": 0, "timeout_s": 100, "reason": None,
                              "scope": "final full story cards: headline, why_it_matters, body; not whole-brief verification"},
            "model_evidence": model_evidence(),
            "source_dirty": False, "source_file": str(Path('worldintel/run.py').resolve()),
            "generation": "rendered", "handoff": "disabled", "delivery_confirmed": False,
            "quality": {"overall": "complete", "unresolved_coverage_events": 0,
                        "scope": "recognized registered events; not complete major-event recall"},
            "analysis": {"overall": "complete", "passes": {
                n: {"success": True, "skipped": False, "timed_out": False, "error": None}
                for n in ("world", "finance")}},
            "rewrite": {"attempted": False, "error": None, "skipped": False, "remaining": 0},
            "rendered": {"chars": 1000, "text_chars": 200, "sha256": "a" * 64,
                         "world_cards": 3, "input_news": 10}}


def test_missing_summary_rejected_even_with_exit_zero():
    from tools.verify_minimal_run import validate_result
    assert validate_result(0, '', '', 'a' * 40, Path.cwd())


def test_complete_summary_accepted():
    from tools.verify_minimal_run import validate_result, PREFIX
    assert validate_result(0, '', PREFIX + json.dumps(evidence()), 'a' * 40, Path.cwd()) == []


@pytest.mark.parametrize('field,value', [('source_dirty', True), ('source_commit', 'bad'),
    ('handoff', 'handed_off'), ('delivery_confirmed', True), ('generation', 'failed')])
def test_bad_summary_rejected(field, value):
    from tools.verify_minimal_run import validate_result, PREFIX
    item = evidence()
    item[field] = value
    assert validate_result(0, '', PREFIX + json.dumps(item), 'a' * 40, Path.cwd())


@pytest.mark.parametrize('field,value', [('success', False), ('skipped', True), ('timed_out', True), ('error', 'failed')])
def test_incomplete_model_rejected(field, value):
    from tools.verify_minimal_run import validate_result, PREFIX
    item = evidence()
    item['analysis']['passes']['world'][field] = value
    assert validate_result(0, '', PREFIX + json.dumps(item), 'a' * 40, Path.cwd())


@pytest.mark.parametrize('field', ['chars', 'text_chars', 'world_cards', 'input_news'])
def test_empty_render_rejected(field):
    from tools.verify_minimal_run import validate_result, PREFIX
    item = evidence()
    item['rendered'][field] = 0
    assert validate_result(0, '', PREFIX + json.dumps(item), 'a' * 40, Path.cwd())


def test_duplicate_summary_stdout_and_wrong_source_rejected():
    from tools.verify_minimal_run import validate_result, PREFIX
    item = evidence()
    line = PREFIX + json.dumps(item)
    assert validate_result(0, '', line + '\n' + line, 'a' * 40, Path.cwd())
    assert validate_result(0, 'MEDIA:secret', line, 'a' * 40, Path.cwd())
    item['source_file'] = str(Path('/other/run.py').resolve())
    assert validate_result(0, '', PREFIX + json.dumps(item), 'a' * 40, Path.cwd())


def test_pipeline_emits_metadata_without_brief_content(offline, monkeypatch, capsys):
    from worldintel import run
    monkeypatch.setattr(sys, 'argv', ['run.py', '--dry', '--force'])
    assert run.main() == 0
    output = capsys.readouterr()
    lines = [line for line in output.err.splitlines() if line.startswith('WORLD_INTEL_DRY_SUMMARY:')]
    assert len(lines) == 1
    item = json.loads(lines[0].split(':', 1)[1])
    assert item['handoff'] == 'disabled'
    assert item['rendered']['sha256']
    assert 'offline rendered briefing' not in lines[0]


from test_dry_isolation import offline  # noqa: E402,F401


def test_live_runner_uses_isolated_profile_and_scrubs_keys(tmp_path, monkeypatch):
    from tools import verify_minimal_run as verifier
    from types import SimpleNamespace
    monkeypatch.setattr(verifier, 'git', lambda *args: 'a' * 40 if args[0] == 'rev-parse' else '')
    monkeypatch.setenv('FINNHUB_API_KEY', 'must-not-propagate')
    calls = []

    def execute(command, **kwargs):
        import yaml
        calls.append(command)
        assert command[1:4] == ['-m', 'worldintel.cli', 'run']
        assert command[-2:] == ['--dry', '--force']
        assert 'FINNHUB_API_KEY' not in kwargs['env']
        cfg = yaml.safe_load(Path(command[5]).read_text())
        assert cfg['delivery'] == {'adapter': 'file'}
        assert cfg['analysis']['source_review_enabled'] is True
        assert cfg['analysis']['source_review_repair_enabled'] is True
        assert cfg['sources']['filings']['enabled'] is False
        assert cfg['sources']['earnings']['enabled'] is False
        assert cfg['sources']['markets'] == {}
        assert len(cfg['desks']) == 1
        assert all(Path(value).parent == Path(command[5]).parent
                   for key, value in cfg['runtime'].items() if key != 'timezone')
        item = evidence()
        item['model_evidence'] = model_evidence(Path(cfg['runtime']['data_root']) / 'model-evidence')
        return SimpleNamespace(returncode=0, stdout='', stderr=verifier.PREFIX + json.dumps(item))

    monkeypatch.setattr(verifier.subprocess, 'run', execute)
    report_path = tmp_path / 'report.json'
    assert verifier.main(['--report', str(report_path)]) == 0
    report = json.loads(report_path.read_text())
    assert report['status'] == 'PASS'
    assert Path(report['model_evidence_root']).is_dir()
    assert len(list(Path(report['model_evidence_root']).rglob('manifest.json'))) == 3
    assert len(calls) == 1


def test_dirty_candidate_rejected_before_live_call(tmp_path, monkeypatch):
    from tools import verify_minimal_run as verifier
    monkeypatch.setattr(verifier, 'git', lambda *args: 'a' * 40 if args[0] == 'rev-parse' else ' M source.py')
    monkeypatch.setattr(verifier.subprocess, 'run', lambda *a, **k: pytest.fail('must not execute live'))
    report_path = tmp_path / 'report.json'
    assert verifier.main(['--report', str(report_path)]) == 1
    assert json.loads(report_path.read_text())['status'] == 'FAIL'


@pytest.mark.parametrize('rewrite', [{}, {'attempted': False, 'skipped': False, 'error': None},
    {'attempted': False, 'skipped': False, 'error': None, 'remaining': 1},
    {'attempted': False, 'skipped': False, 'error': None, 'remaining': False},
    {'attempted': 0, 'skipped': False, 'error': None, 'remaining': 0},
    {'attempted': False, 'skipped': 0, 'error': None, 'remaining': 0}])
def test_incomplete_or_malformed_rewrite_rejected(rewrite):
    from tools.verify_minimal_run import validate_result, PREFIX
    item = evidence()
    item['rewrite'] = rewrite
    assert validate_result(0, '', PREFIX + json.dumps(item), 'a' * 40, Path.cwd())


def test_boolean_schema_version_rejected():
    from tools.verify_minimal_run import validate_result, PREFIX
    item = evidence()
    item['schema_version'] = True
    assert validate_result(0, '', PREFIX + json.dumps(item), 'a' * 40, Path.cwd())


def test_dry_summary_reports_final_remaining_writing_violations(offline, monkeypatch, capsys):
    from worldintel import run
    monkeypatch.setattr(sys, 'argv', ['run.py', '--dry', '--force'])
    monkeypatch.setattr(run.quality_gate, 'scan', lambda result: [{'body': 'still meta'}])
    monkeypatch.setattr(run.quality_gate, 'rewrite', lambda *args: args[0])
    assert run.main() == 0
    line = next(line for line in capsys.readouterr().err.splitlines()
                if line.startswith('WORLD_INTEL_DRY_SUMMARY:'))
    assert json.loads(line.split(':', 1)[1])['rewrite']['remaining'] == 1


def test_unexpected_configured_write_rejects_successful_summary(tmp_path, monkeypatch):
    from tools import verify_minimal_run as verifier
    from types import SimpleNamespace
    import yaml
    monkeypatch.setattr(verifier, 'git', lambda *args: 'a' * 40 if args[0] == 'rev-parse' else '')

    def execute(command, **kwargs):
        cfg = yaml.safe_load(Path(command[5]).read_text())
        root = Path(cfg['runtime']['data_root'])
        root.mkdir(exist_ok=True)
        (root / 'unexpected').write_text('write')
        return SimpleNamespace(returncode=0, stdout='', stderr=verifier.PREFIX + json.dumps(evidence()))

    monkeypatch.setattr(verifier.subprocess, 'run', execute)
    report_path = tmp_path / 'report.json'
    assert verifier.main(['--report', str(report_path)]) == 1
    assert 'isolated configured paths changed' in json.loads(report_path.read_text())['errors']


@pytest.mark.parametrize('field,value', [('source_review', {'status': 'disabled'}),
    ('source_review', {'status': 'hold'}), ('model_evidence', {'complete': False}),
    ('model_evidence', {'complete': True, 'invocations': []})])
def test_unreviewed_or_uncaptured_release_never_passes(field, value):
    from tools.verify_minimal_run import validate_result, PREFIX
    item = evidence()
    item[field] = value
    assert validate_result(0, '', PREFIX + json.dumps(item), 'a' * 40, Path.cwd())


def test_private_evidence_validator_rejects_modified_raw_bytes(tmp_path):
    from tools.verify_minimal_run import validate_model_evidence
    summary = model_evidence(tmp_path)
    root = tmp_path / summary['run_id']
    assert validate_model_evidence(root, summary) == []
    next(root.rglob('stdin.bin')).write_bytes(b'changed')
    assert validate_model_evidence(root, summary)


@pytest.mark.parametrize('failure', ['hold', 'missing-summary', 'timeout'])
def test_failed_live_review_retains_private_sidecar_before_scratch_cleanup(tmp_path, monkeypatch, failure):
    from tools import verify_minimal_run as verifier
    from types import SimpleNamespace
    import yaml
    monkeypatch.setattr(verifier, 'git', lambda *args: 'a' * 40 if args[0] == 'rev-parse' else '')
    scratch_roots = []
    def execute(command, **kwargs):
        cfg = yaml.safe_load(Path(command[5]).read_text())
        private = Path(cfg['runtime']['data_root']) / 'model-evidence'
        scratch_roots.append(private.parent.parent)
        item = evidence()
        item['model_evidence'] = model_evidence(private)
        item['source_review'] = {'status': 'hold', 'attempted': True, 'cards_reviewed': 0}
        item['generation'] = 'held'
        if failure == 'timeout':
            raise verifier.subprocess.TimeoutExpired(command, 1)
        if failure == 'missing-summary':
            return SimpleNamespace(returncode=1, stdout='', stderr='pipeline stopped')
        return SimpleNamespace(returncode=1, stdout='', stderr=verifier.PREFIX + json.dumps(item))
    monkeypatch.setattr(verifier.subprocess, 'run', execute)
    report_path = tmp_path / 'failed.json'
    assert verifier.main(['--report', str(report_path)]) == 1
    report = json.loads(report_path.read_text())
    assert report['status'] == 'FAIL' and report['errors']
    assert Path(report['model_evidence_root']).is_dir()
    assert len(list(Path(report['model_evidence_root']).rglob('stdin.bin'))) == 3
    assert not scratch_roots[0].exists()


def test_extra_run_entry_prevents_success_and_preserves_private_bytes(tmp_path, monkeypatch):
    from tools import verify_minimal_run as verifier
    from types import SimpleNamespace
    import yaml
    monkeypatch.setattr(verifier, 'git', lambda *args: 'a' * 40 if args[0] == 'rev-parse' else '')
    def execute(command, **kwargs):
        cfg = yaml.safe_load(Path(command[5]).read_text())
        private = Path(cfg['runtime']['data_root']) / 'model-evidence'
        item = evidence()
        item['model_evidence'] = model_evidence(private)
        (private / ('e' * 32)).mkdir()
        return SimpleNamespace(returncode=0, stdout='', stderr=verifier.PREFIX + json.dumps(item))
    monkeypatch.setattr(verifier.subprocess, 'run', execute)
    report_path = tmp_path / 'extra.json'
    assert verifier.main(['--report', str(report_path)]) == 1
    report = json.loads(report_path.read_text())
    assert report['status'] == 'FAIL'
    assert len(list(Path(report['model_evidence_root']).rglob('stdin.bin'))) == 3


@pytest.mark.parametrize('field,value', [('repair_attempted', None), ('repair_attempted', 0),
    ('review_calls', 2), ('review_calls', True), ('corrected_cards', 1)])
def test_inconsistent_correction_audit_rejected(field, value):
    from tools.verify_minimal_run import validate_result, PREFIX
    item = evidence()
    item['source_review'].update(repair_attempted=False, review_calls=1, corrected_cards=0)
    item['source_review'][field] = value
    assert validate_result(0, '', PREFIX + json.dumps(item), 'a' * 40, Path.cwd())


def test_extra_correction_capture_without_attempt_rejected():
    from tools.verify_minimal_run import validate_result, PREFIX
    item = evidence()
    item['source_review'].update(repair_attempted=False, review_calls=1, corrected_cards=0)
    record = dict(item['model_evidence']['invocations'][-1], invocation_id='d' * 32, pass_name='source-correction')
    item['model_evidence']['invocations'].append(record)
    assert validate_result(0, '', PREFIX + json.dumps(item), 'a' * 40, Path.cwd())
