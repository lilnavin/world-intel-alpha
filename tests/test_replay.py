"""Synthetic-only fixtures exercise real isolated replay subprocesses."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import replay
import check_equivalence as checker
from worldintel.core.coverage_policy import resolve_profile, profile_hash


def record_ranking(folder, profile, fingerprint=None):
    path = folder / 'selection-input.json'
    payload = json.loads(path.read_text())
    payload['selection_settings'].update(ranking_profile=profile,
        ranking_profile_hash=profile_hash(profile) if fingerprint is None else fingerprint)
    blob = json.dumps(payload).encode()
    path.write_bytes(blob)
    manifest_path = folder / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['files'][path.name] = {'bytes': len(blob), 'sha256': replay.digest(blob)}
    manifest_path.write_text(json.dumps(manifest))


def test_recorded_ranking_settings_are_accepted(sample):
    folder = sorted(sample[0].iterdir())[0]
    profile = resolve_profile({'topics': []})
    record_ranking(folder, profile)
    assert replay.load_capsule(folder, 'frozen')['selection']['selection_settings']['ranking_profile'] == profile


@pytest.mark.parametrize('invalid', ['hash', 'profile', 'partial'])
def test_invalid_recorded_ranking_fails_closed(sample, invalid):
    folder = sorted(sample[0].iterdir())[0]
    profile = resolve_profile({'topics': []})
    if invalid == 'profile':
        profile['major_events']['max_events'] = 99
    record_ranking(folder, profile, 'tampered' if invalid != 'partial' else profile_hash(profile))
    if invalid == 'partial':
        path = folder / 'selection-input.json'
        data = json.loads(path.read_text())
        del data['selection_settings']['ranking_profile_hash']
        blob = json.dumps(data).encode()
        path.write_bytes(blob)
        manifest_path = folder / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files'][path.name] = {'bytes': len(blob), 'sha256': replay.digest(blob)}
        manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(replay.ReplayError):
        replay.load_capsule(folder, 'frozen')


SELECT = '''
DESKS = ('world',)
_LOCAL_DESKS = ('world',)
PER_FEED_CAP = 2
def _drop_stale(items, date, *, hours=24):
    return items if hours == 24 else []
def select_candidates(bundle, seen, *, per_desk=3):
    assert DESKS == ('world',) and _LOCAL_DESKS == ('world',)
    assert PER_FEED_CAP == 2 and per_desk == 3
    return {'world': _drop_stale(bundle['news'], 'unused')[:per_desk]}
'''


@pytest.fixture
def sample(tmp_path):
    roots = [tmp_path / name for name in ('baseline', 'candidate')]
    for root in roots:
        scripts = root / 'scripts'
        scripts.mkdir(parents=True)
        (scripts / 'select_news.py').write_text(SELECT)
        (scripts / 'fetch_sources.py').write_text('')
        (scripts / 'compose_html.py').write_text("def render_html(analysis, bundle, date): return '<p>' + date + '</p>'\n")
    store = tmp_path / 'capsules'
    store.mkdir()
    for day in range(1, 6):
        date = f'2026-09-{day:02d}'
        name = date + 'T070000+0900-12345678'
        folder = store / name
        folder.mkdir()
        selection = {'bundle': {'news': [{'title': 'synthetic one'}, {'title': 'synthetic two'}], 'as_of': date + 'T07:00:00+09:00'},
                     'as_of': date + 'T07:00:00+09:00', 'today': date, 'seen': [],
                     'selection_settings': {'desks': ['world'], 'local_desks': ['world'],
                                            'fresh_hours': 24, 'per_desk': 3, 'per_feed_cap': 2}}
        render = {'bundle': {}, 'analysis': {}, 'date': date,
                  'clock': {'local_date': date, 'utc_offset': '+09:00', 'generated_at': date + 'T07:01:00+09:00'}}
        files = {}
        for filename, payload in zip(replay.BOUNDARIES, (selection, render)):
            blob = json.dumps(payload).encode()
            (folder / filename).write_bytes(blob)
            files[filename] = {'bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}
        manifest = {'format_version': 1, 'capsule_id': name, 'started_at': date + 'T07:00:00+09:00',
                    'mode': 'scheduled', 'source_commit': 'frozen', 'source_dirty': False,
                    'boundaries': replay.BOUNDARIES, 'files': files, 'errors': []}
        (folder / 'manifest.json').write_text(json.dumps(manifest))
    return store, *roots


def run(sample):
    return checker.check(*sample, expected_commit='frozen')


def test_real_subprocesses_and_recorded_settings(sample):
    result = run(sample)
    assert result['equivalent'] and result['days'] == 5
    row = result['results'][0]
    assert row['baseline']['root'] != row['candidate']['root']
    assert row['baseline']['counts'] == {'world': 2}
    assert 'synthetic one' not in json.dumps(result)


@pytest.mark.parametrize('kind', ['selection', 'order', 'html', 'date'])
def test_differences_fail(sample, kind):
    scripts = sample[2] / 'scripts'
    if kind in ('selection', 'order'):
        suffix = "[:1]" if kind == 'selection' else "[::-1]"
        (scripts / 'select_news.py').write_text(SELECT.replace("[:per_desk]", suffix))
    else:
        (scripts / 'compose_html.py').write_text("def render_html(analysis, bundle, date): return 'changed' + " +
                                                ("date" if kind == 'html' else "'1999-01-01'") + '\n')
    assert not run(sample)['equivalent']


@pytest.mark.parametrize('field,value', [('source_dirty', True), ('source_commit', 'wrong'),
                                        ('errors', ['failure']), ('boundaries', {}),
                                        ('format_version', True), ('started_at', '2026-09-01')])
def test_bad_manifest(sample, field, value):
    path = sorted(sample[0].iterdir())[0] / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises((ValueError, KeyError)):
        run(sample)


@pytest.mark.parametrize('kind', ['missing', 'corrupt', 'size', 'settings', 'date', 'clock'])
def test_invalid_inputs(sample, kind):
    folder = sorted(sample[0].iterdir())[0]
    path = folder / 'selection-input.json'
    if kind == 'missing':
        path.unlink()
    elif kind == 'corrupt':
        path.write_bytes(path.read_bytes() + b' ')
    else:
        manifest = json.loads((folder / 'manifest.json').read_text())
        if kind == 'size':
            manifest['files'][path.name]['bytes'] += 1
        else:
            if kind == 'clock':
                path = folder / 'render-input.json'
            payload = json.loads(path.read_text())
            if kind == 'settings':
                del payload['selection_settings']['fresh_hours']
            elif kind == 'date':
                payload['today'] = '2026-09-10'
            else:
                payload['clock']['utc_offset'] = '+01:00'
            blob = json.dumps(payload).encode()
            path.write_bytes(blob)
            manifest['files'][path.name] = {'bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}
        (folder / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises((OSError, ValueError)):
        run(sample)


def test_duplicate_date(sample):
    import shutil
    original = sorted(sample[0].iterdir())[0]
    other = original.with_name(original.name[:-8] + '87654321')
    shutil.copytree(original, other)
    path = other / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['capsule_id'] = other.name
    path.write_text(json.dumps(manifest))
    with pytest.raises(replay.ReplayError, match='duplicate'):
        run(sample)


def test_forced_not_counted(sample):
    path = sorted(sample[0].iterdir())[0] / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['mode'] = 'forced'
    path.write_text(json.dumps(manifest))
    with pytest.raises(replay.ReplayError, match='five'):
        run(sample)


def test_identical_roots_rejected(sample):
    with pytest.raises(replay.ReplayError, match='distinct'):
        checker.check(sample[0], sample[1], sample[1], 'frozen')


@pytest.mark.parametrize('code', ["raise RuntimeError('private article data')", "__file__ = '/wrong/select_news.py'",
                                "import socket\ntry: socket.create_connection(('localhost', 9))\nexcept Exception: pass"])
def test_worker_failure_origin_and_network_fail_closed(sample, code):
    path = sample[2] / 'scripts' / 'select_news.py'
    path.write_text(SELECT + '\n' + code)
    with pytest.raises(replay.ReplayError, match='subprocess failed'):
        run(sample)


def test_candidate_config_is_explicit(sample, tmp_path, monkeypatch):
    config = tmp_path / 'owner.yaml'
    config.write_text('synthetic')
    path = sample[2] / 'scripts' / 'compose_html.py'
    path.write_text("import os\nassert os.environ.get('WORLD_INTEL_CONFIG') == " + repr(str(config)) +
                    "\ndef render_html(analysis,bundle,date): return '<p>' + date + '</p>'\n")
    monkeypatch.setenv('WORLD_INTEL_CONFIG', 'ignored ambient configuration')
    assert checker.check(*sample, expected_commit='frozen', candidate_config=config)['equivalent']


def test_distinct_baseline_and_candidate_configs_are_explicit(sample, tmp_path, monkeypatch):
    configs = [tmp_path / 'baseline.yaml', tmp_path / 'candidate.yaml']
    for root, config in zip(sample[1:], configs):
        config.write_text('synthetic')
        (root / 'scripts' / 'compose_html.py').write_text(
            "import os\nassert os.environ.get('WORLD_INTEL_CONFIG') == " + repr(str(config)) +
            "\ndef render_html(analysis,bundle,date): return '<p>' + date + '</p>'\n")
    monkeypatch.setenv('WORLD_INTEL_CONFIG', 'ignored ambient configuration')
    assert checker.check(*sample, 'frozen', configs[1], configs[0])['equivalent']


@pytest.mark.parametrize('before,after', [('hours=24', 'hours=48'), ('per_desk=3', 'per_desk=9'),
                                       ('PER_FEED_CAP = 2', 'PER_FEED_CAP = 4'),
                                       ("DESKS = ('world',)", "DESKS = ('us',)")])
def test_settings_regressions_are_not_overridden(sample, before, after):
    (sample[2] / 'scripts' / 'select_news.py').write_text(SELECT.replace(before, after))
    with pytest.raises(replay.ReplayError, match='subprocess failed'):
        run(sample)


def test_candidate_desk_accessors_take_precedence(sample):
    path = sample[2] / 'scripts' / 'select_news.py'
    path.write_text(SELECT + "\ndef desk_ids(): return ('us',)\ndef local_desk_ids(): return ()\n")
    with pytest.raises(replay.ReplayError, match='subprocess failed'):
        run(sample)


def test_explicit_effective_settings_support_configured_defaults(sample):
    path = sample[2] / 'scripts' / 'select_news.py'
    adapted = SELECT.replace('hours=24', 'hours=None').replace('per_desk=3', 'per_desk=None')
    adapted = adapted.replace('return items if hours == 24 else []', 'return items if hours in (None, 24) else []')
    adapted += "\ndef selection_settings(): return {'desks':['world'],'local_desks':['world'], 'per_feed_cap':2,'per_desk':3,'fresh_hours':24}\n"
    path.write_text(adapted)
    assert run(sample)['equivalent']
    path.write_text(adapted.replace("'fresh_hours':24", "'fresh_hours':48"))
    with pytest.raises(replay.ReplayError, match='settings differ'):
        run(sample)


def test_ephemeral_loopback_capability_probe_allowed(sample):
    path = sample[2] / 'scripts' / 'select_news.py'
    path.write_text(SELECT + "\nimport socket\nwith socket.socket() as probe: probe.bind(('127.0.0.1', 0))\n")
    assert run(sample)['equivalent']


@pytest.mark.parametrize('operation', ["probe.bind(('0.0.0.0', 0))", "probe.listen()",
                                      "probe.connect(('127.0.0.1', 9))",
                                      "socket.getaddrinfo('localhost', 9)"])
def test_network_operations_remain_blocked_even_swallowed(sample, operation):
    path = sample[2] / 'scripts' / 'select_news.py'
    path.write_text(SELECT + "\nimport socket\nwith socket.socket() as probe:\n"
                    "    try: " + operation + "\n    except Exception: pass\n")
    with pytest.raises(replay.ReplayError, match='network attempted'):
        run(sample)


def test_cli_failure_does_not_print_payload(sample, capsys):
    path = sample[2] / 'scripts' / 'select_news.py'
    path.write_text("raise ValueError('SECRET ARTICLE')")
    assert checker.main(['--store', str(sample[0]), '--baseline-root', str(sample[1]),
                         '--candidate-root', str(sample[2]), '--expected-commit', 'frozen']) == 2
    assert 'SECRET' not in capsys.readouterr().out


def test_explicit_renderer_receives_config_and_recorded_clock(sample):
    scripts = sample[2] / 'scripts'
    (scripts / 'config.py').write_text("import datetime\nclass Config:\n"
        "    desks = [{'id': 'world'}]\n"
        "    timezone = datetime.timezone(datetime.timedelta(hours=9), 'JST')\n"
        "def get(): return Config()\n")
    (scripts / 'compose_html.py').write_text(
        "def render_html(analysis,bundle,date,*,desks=(),timezone_label='wrong'):\n"
        "    assert desks == [{'id': 'world'}] and timezone_label == 'JST'\n"
        "    return '<p>' + date + '</p>'\n")
    assert run(sample)['equivalent']
    config = scripts / 'config.py'
    config.write_text(config.read_text().replace('hours=9', 'hours=8'))
    with pytest.raises(replay.ReplayError, match='configured timezone differs'):
        run(sample)


@pytest.mark.parametrize('difference', [False, True])
def test_report_is_sanitized_and_exit_status_matches(sample, tmp_path, capsys, difference):
    if difference:
        (sample[2] / 'scripts' / 'compose_html.py').write_text("def render_html(a,b,d): return 'changed'\n")
    report = tmp_path / 'report.json'
    report.write_text('old')
    status = checker.main(['--store', str(sample[0]), '--baseline-root', str(sample[1]),
                           '--candidate-root', str(sample[2]), '--expected-commit', 'frozen',
                           '--report', str(report)])
    assert status == int(difference)
    data = json.loads(report.read_text())
    assert data['days'] == 5 and data['equivalent'] is not difference
    assert all(set(row) == {'date', 'equivalent', 'baseline', 'candidate'} for row in data['results'])
    assert all(set(row['candidate']) == {'selection_sha256', 'html_sha256'} for row in data['results'])
    assert str(tmp_path) not in report.read_text() and 'synthetic' not in report.read_text()
    assert not list(tmp_path.glob('.equivalence-*.tmp'))


def test_atomic_report_failure_preserves_previous_file(tmp_path, monkeypatch):
    report = tmp_path / 'report.json'
    report.write_text('previous')
    def fail(*args):
        raise OSError('simulated')
    monkeypatch.setattr(checker.os, 'replace', fail)
    with pytest.raises(OSError):
        checker.write_report(report, {'equivalent': True, 'days': 0, 'results': []})
    assert report.read_text() == 'previous'
    assert not list(tmp_path.glob('.equivalence-*.tmp'))


def package_tree(root):
    package = root / 'worldintel'
    core = package / 'core'
    core.mkdir(parents=True)
    (package / '__init__.py').write_text('')
    (core / '__init__.py').write_text('')
    for source in (root / 'scripts').glob('*.py'):
        destination = package if source.name == 'config.py' else core
        (destination / source.name).write_bytes(source.read_bytes())
    return package


def test_legacy_baseline_and_packaged_candidate_use_separate_origins(sample):
    package = package_tree(sample[2])
    # A stale scripts tree must not influence the selected package layout.
    (sample[2] / 'scripts' / 'select_news.py').write_text("raise RuntimeError('wrong layout')")
    result = run(sample)
    assert result['equivalent']
    row = result['results'][0]
    assert row['candidate']['origins']['select_news'] == str(package / 'core/select_news.py')
    assert row['baseline']['origins']['select_news'] == str(sample[1] / 'scripts/select_news.py')


def test_both_packaged_trees_replay(sample):
    package_tree(sample[1])
    package_tree(sample[2])
    assert run(sample)['equivalent']


@pytest.mark.parametrize('recorded', [True, False])
def test_replay_restores_recorded_policy_over_ambient_configuration(sample, recorded):
    profile = resolve_profile({'topics': [{'terms': ['earthquake'], 'weight': 2}],
                               'major_events': {'enabled': False}})
    expected = profile if recorded else resolve_profile({'topics': [], 'major_events': {'enabled': False}})
    if recorded:
        for folder in sample[0].iterdir():
            record_ranking(folder, profile)
    for root in sample[1:]:
        package = package_tree(root)
        (package / 'core/coverage_policy.py').write_bytes(
            (Path(__file__).resolve().parents[1] / 'worldintel/core/coverage_policy.py').read_bytes())
        (package / 'config.py').write_text(
            "class Config:\n    ranking = " + repr(resolve_profile()) +
            "\n_cfg = Config()\ndef get(): return _cfg\n")
        selector = package / 'core/select_news.py'
        code = selector.read_text().replace("    assert DESKS", "    assert config.get().ranking == " + repr(expected) + "\n    assert DESKS")
        code += "\nfrom worldintel import config\nfrom .coverage_policy import profile_hash\n"
        code += "def selection_settings():\n    profile = config.get().ranking\n    return "
        code += "{'desks':['world'],'local_desks':['world'],'fresh_hours':24,'per_desk':3,'per_feed_cap':2,"
        code += "'ranking_profile':profile,'ranking_profile_hash':profile_hash(profile)}\n"
        selector.write_text(code)
    assert run(sample)['equivalent']


def test_incomplete_package_cannot_fall_back_to_scripts(sample):
    package = package_tree(sample[2])
    (package / 'core/fetch_sources.py').unlink()
    with pytest.raises(replay.ReplayError, match='subprocess failed'):
        run(sample)


def test_package_config_import_and_explicit_renderer(sample):
    package = package_tree(sample[2])
    (package / 'config.py').write_text("import datetime\nclass Config:\n"
        "    desks = [{'id': 'world'}]\n"
        "    timezone = datetime.timezone(datetime.timedelta(hours=9), 'JST')\n"
        "def get(): return Config()\n")
    selector = package / 'core/select_news.py'
    selector.write_text(selector.read_text() + "\nfrom worldintel import config\nassert config.get().desks\n")
    (package / 'core/compose_html.py').write_text(
        "def render_html(analysis,bundle,date,*,desks=(),timezone_label='wrong'):\n"
        "    assert desks == [{'id': 'world'}] and timezone_label == 'JST'\n"
        "    return '<p>' + date + '</p>'\n")
    assert run(sample)['equivalent']


def test_packaged_wrong_origin_is_rejected(sample):
    package = package_tree(sample[2])
    selector = package / 'core/select_news.py'
    selector.write_text(selector.read_text() + "\n__file__ = '/wrong/select_news.py'\n")
    with pytest.raises(replay.ReplayError, match='wrong module origin'):
        run(sample)
