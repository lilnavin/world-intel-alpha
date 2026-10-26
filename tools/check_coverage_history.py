"""Offline selection-policy evaluation from immutable private captured inputs.

This never generates new model output, fetches articles, approves differences,
or replaces strict equivalence evidence. Full text is not written to reports.
Reports and dispositions contain private URL identities and stay outside Git.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as dt
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay


class HistoryError(ValueError):
    """Safe diagnostics, without captured content."""


def need(condition, message):
    if not condition:
        raise HistoryError(message)


def digest(value):
    blob = value if isinstance(value, bytes) else json.dumps(
        value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(blob).hexdigest()


def sha(value, length=64):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{' + str(length) + '}', value) is not None


def external(path):
    path = Path(path).resolve()
    need(not any((p / '.git').exists() for p in path.parents), 'private evidence must be outside repositories')
    return path


def load_capture(entry):
    """Validate forced audit captures as strictly as scheduled captures, in place.

    Unlike replay.load_capsule, forced mode is evaluated but never eligible for
    scheduled acceptance. No mode is rewritten and no original file is changed.
    """
    need(set(entry) == {'capsule', 'date', 'mode', 'expected_source_commit', 'files'}, 'invalid capsule pin')
    path = external(entry['capsule'])
    need(path.is_dir() and not Path(entry['capsule']).is_symlink(), 'invalid capsule directory')
    need(set(entry['files']) == {'manifest.json', *replay.BOUNDARIES}, 'incomplete capsule pin')
    payload = {}
    for name, expected in entry['files'].items():
        file = path / name
        need(file.is_file() and not file.is_symlink(), 'capsule input unavailable')
        blob = file.read_bytes()
        need(sha(expected) and digest(blob) == expected, 'capsule pin integrity mismatch')
        payload[name] = json.loads(blob)
    manifest = payload['manifest.json']
    need(type(manifest.get('format_version')) is int and manifest['format_version'] == 1,
         'invalid capsule format')
    need(entry['mode'] in {'scheduled', 'forced'} and manifest.get('mode') == entry['mode'], 'capture mode mismatch')
    need(sha(entry['expected_source_commit'], 40) and manifest.get('source_commit') == entry['expected_source_commit']
         and manifest.get('source_dirty') is False and manifest.get('errors') == [], 'capture provenance mismatch')
    started = dt.datetime.fromisoformat(manifest['started_at'])
    date = dt.date.fromisoformat(entry['date'])
    need(entry['date'] == date.isoformat() and started.utcoffset() is not None
         and started.date() == date, 'capture date mismatch')
    need(manifest.get('capsule_id') == path.name and re.fullmatch(
        re.escape(started.strftime('%Y-%m-%dT%H%M%S%z')) + r'-[0-9a-f]{8}', path.name), 'capture timestamp mismatch')
    need(manifest.get('boundaries') == replay.BOUNDARIES and set(manifest.get('files', {})) == set(replay.BOUNDARIES),
         'capture boundary mismatch')
    for name in replay.BOUNDARIES:
        blob = (path / name).read_bytes()
        need(manifest['files'][name] == {'bytes': len(blob), 'sha256': digest(blob)}, 'capture input integrity mismatch')
    selection, render = (payload[n] for n in replay.BOUNDARIES)
    need(selection.get('today') == render.get('date') == date.isoformat(), 'recorded date mismatch')
    need(isinstance(selection.get('bundle'), dict) and isinstance(render.get('bundle'), dict)
         and isinstance(render.get('analysis'), dict), 'missing captured boundary payload')
    need(isinstance(selection.get('seen'), list) and all(isinstance(s, str) for s in selection['seen']), 'invalid seen set')
    need(selection.get('as_of') == selection['bundle'].get('as_of'), 'as_of mismatch')
    as_of = selection.get('as_of')
    try:
        selected_at = dt.datetime.fromisoformat(as_of)
    except (TypeError, ValueError):
        raise HistoryError('invalid selection clock') from None
    need(isinstance(as_of, str) and not as_of.endswith('-00:00')
         and selected_at.utcoffset() is not None and selected_at.date() == date
         and selected_at.utcoffset() == started.utcoffset(), 'selection clock mismatch')
    settings = selection.get('selection_settings')
    required = {'desks', 'local_desks', 'fresh_hours', 'per_desk', 'per_feed_cap'}
    need(isinstance(settings, dict) and set(settings) in (required, required | {'ranking_profile', 'ranking_profile_hash'}),
         'invalid selection settings')
    for key in ('desks', 'local_desks'):
        need(isinstance(settings[key], list) and all(isinstance(v, str) for v in settings[key])
             and len(set(settings[key])) == len(settings[key]), 'invalid desk settings')
    need(bool(settings['desks']) and set(settings['local_desks']) <= set(settings['desks']), 'invalid desks')
    need(all(type(settings[k]) is int and settings[k] > 0 for k in ('fresh_hours', 'per_desk', 'per_feed_cap')),
         'invalid numeric settings')
    if 'ranking_profile' in settings:
        policy = replay.ranking_policy()
        need(policy.resolve_profile(settings['ranking_profile']) == settings['ranking_profile']
             and policy.profile_hash(settings['ranking_profile']) == settings['ranking_profile_hash'], 'recorded ranking mismatch')
        need(selection['bundle'].get('ranking_profile', settings['ranking_profile']) == settings['ranking_profile'],
             'bundle ranking differs from recording')
    clock = render.get('clock', {})
    generated = dt.datetime.fromisoformat(clock['generated_at'])
    need(clock.get('local_date') == date.isoformat() and generated.utcoffset() is not None
         and generated.date() == date and generated.utcoffset() == selected_at.utcoffset()
         and generated.strftime('%z') == clock.get('utc_offset', '').replace(':', ''),
         'render clock mismatch')
    return {'case_id': path.name, 'date': date.isoformat(), 'mode': entry['mode'],
            'selection': selection, 'render': render}


def selection_differences(before, after):
    rows = []
    for desk in sorted(set(before) | set(after)):
        if before.get(desk, []) != after.get(desk, []):
            change = {'desk': desk, 'before': before.get(desk, []), 'after': after.get(desk, [])}
            rows.append({**change, 'change_sha256': digest(change)})
    return rows


def review_differences(case_id, differences, reviews):
    unreviewed = 0
    for change in differences:
        matching = [r for r in reviews if r.get('case_id') == case_id and r.get('desk') == change['desk']
                    and r.get('change_sha256') == change['change_sha256']]
        accepted = len(matching) == 1 and matching[0].get('decision') == 'intended' and all(
            isinstance(matching[0].get(k), str) and matching[0][k].strip() for k in ('reason', 'reviewed_by'))
        change['review'] = matching[0] if len(matching) == 1 else None
        unreviewed += not accepted
    return unreviewed


def body_availability(urls, bodies):
    # Import the reviewed candidate cleaner, never substitute feed summaries.
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location('_history_enrich', root / 'worldintel/core/enrich.py')
    cleaner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cleaner)
    return {url: 'available' if cleaner.substantive_content(bodies.get(url, '')) else 'unavailable' for url in urls}


def check_expectations(cases, assertions):
    need(isinstance(assertions, list) and bool(assertions), 'positive coverage expectations required')
    by_id = {case['case_id']: case for case in cases}
    outcomes = []
    kinds = set()
    for assertion in assertions:
        need(set(assertion) == {'case_id', 'kind', 'urls'} and assertion['case_id'] in by_id
             and assertion['kind'] in {'recognized', 'protected', 'not_grouped'}
             and isinstance(assertion['urls'], list) and len(assertion['urls']) >= 2
             and all(isinstance(u, str) and u for u in assertion['urls'])
             and len(set(assertion['urls'])) == len(assertion['urls']), 'invalid coverage expectation')
        kinds.add(assertion['kind'])
        candidate = by_id[assertion['case_id']]['candidate']
        urls = set(assertion['urls'])
        events = candidate['protected_events']
        if assertion['kind'] == 'recognized':
            recognized = {d['url'] for d in candidate['selection_audit'] if d.get('recognition_reason')}
            passed = urls <= recognized
        elif assertion['kind'] == 'protected':
            passed = any(urls <= set(e.get('urls', [])) for e in events)
        else:
            groups = [set(e.get('urls', [])) for e in events]
            groups += [set(e.get('urls', [])) for e in candidate['coverage_eligibility_audit']]
            all_inputs = {d.get('url') for d in candidate['selection_audit']}
            passed = urls <= all_inputs and not any(urls <= group for group in groups)
        outcomes.append({**assertion, 'passed': passed})
    need({'recognized', 'protected'} <= kinds, 'recognition and protection expectations both required')
    return outcomes


def validate_review_inputs(report, expectations, dispositions):
    """Recompute against the hashed external oracle and actual review payload.

    Embedded assertions/reviews are output evidence, never the acceptance oracle.
    The caller verifies file hashes before passing these parsed external objects.
    """
    need(set(expectations) == {'version', 'input_pin_sha256', 'candidate_profile_sha256', 'assertions'}
         and type(expectations['version']) is int and expectations['version'] == 1
         and expectations['input_pin_sha256'] == report['input_pin_sha256']
         and expectations['candidate_profile_sha256'] == report['candidate_profile_sha256'],
         'external expectations binding mismatch')
    outcomes = check_expectations(report['cases'], expectations['assertions'])
    need(outcomes == report['expectations'] and all(e['passed'] for e in outcomes),
         'embedded expectations differ from external oracle')
    changed = any(case['differences'] for case in report['cases'])
    if changed or report.get('dispositions_sha256') is not None:
        need(dispositions is not None and sha(report.get('dispositions_sha256')), 'genuine disposition artifact required')
    reviews = []
    if dispositions is not None:
        binding = {k: report[k] for k in ('baseline_commit', 'candidate_commit', 'baseline_profile_sha256',
                   'candidate_profile_sha256', 'input_pin_sha256', 'expectations_sha256', 'strict_equivalence_sha256')}
        need(set(dispositions) == {'version', 'binding', 'reviews'}
             and type(dispositions['version']) is int and dispositions['version'] == 1
             and dispositions['binding'] == binding and isinstance(dispositions['reviews'], list),
             'external disposition binding mismatch')
        reviews = dispositions['reviews']
        need(all(isinstance(r, dict) and set(r) == {'case_id', 'desk', 'change_sha256', 'decision', 'reason', 'reviewed_by'}
             for r in reviews), 'invalid external review schema')
    known = set()
    for case in report['cases']:
        changes = selection_differences(case['baseline']['selection'], case['candidate']['selection'])
        known.update((case['case_id'], d['desk'], d['change_sha256']) for d in changes)
        need(review_differences(case['case_id'], changes, reviews) == 0 and changes == case['differences']
             and case['unreviewed_changes'] == 0, 'embedded reviews differ from genuine dispositions')
    need(all((r['case_id'], r['desk'], r['change_sha256']) in known for r in reviews), 'extraneous external review')
    return True


def worker(root, entry, profile):
    root = Path(root).resolve(strict=True)
    network = []
    def offline(event, args):
        if event == 'socket.bind' and len(args) == 2 and args[1] in (('::1', 0), ('127.0.0.1', 0)):
            return
        if event in {'socket.connect', 'socket.getaddrinfo', 'socket.bind', 'socket.sendto', 'socket.listen'}:
            network.append(event)
            raise HistoryError('network prohibited')
    sys.addaudithook(offline)
    import socket
    socket.socket.listen = lambda *args: offline('socket.listen', ())
    packaged, origins = replay.module_layout(root)
    sys.path.insert(0, str(root if packaged else root / 'scripts'))
    prefix = 'worldintel.core.' if packaged else ''
    selection = importlib.import_module(prefix + 'select_news')
    need(Path(selection.__file__).resolve() == origins['select_news'].resolve(), 'wrong selection module origin')
    config = importlib.import_module('worldintel.config' if packaged else 'config')
    cfg = config.get()
    policy = importlib.import_module(prefix + 'coverage_policy')
    captured = load_capture(entry)
    settings = captured['selection']['selection_settings']
    effective = selection.selection_settings()
    nonranking = {'desks', 'local_desks', 'fresh_hours', 'per_desk', 'per_feed_cap'}
    need({k: effective[k] for k in nonranking} == {k: settings[k] for k in nonranking},
         'nonranking profile differs from recorded settings')
    bundle = copy.deepcopy(captured['selection']['bundle'])
    bundle['ranking_profile'] = policy.resolve_profile(cfg.ranking)
    selected = selection.select_candidates(bundle, set(captured['selection']['seen']), per_desk=settings['per_desk'])
    need(set(selected) == set(settings['desks']) and all(isinstance(v, list) for v in selected.values()),
         'invalid selection output')
    urls = {desk: [item.get('url') for item in items] for desk, items in selected.items()}
    need(all(all(isinstance(u, str) and u for u in values) and len(values) <= settings['per_desk']
             for values in urls.values()), 'selection capacity or identity failure')
    need(not network, 'network attempted during offline evaluation')
    return {'selection': urls, 'selection_sha256': digest(urls), 'counts': {k: len(v) for k, v in urls.items()},
            'ranking_profile': bundle['ranking_profile'], 'ranking_profile_hash': policy.profile_hash(bundle['ranking_profile']),
            'profile_changed_from_capture': bundle['ranking_profile'] != settings.get('ranking_profile'),
            'protected_events': bundle.get('protected_events', []),
            'coverage_eligibility_audit': bundle.get('coverage_eligibility_audit', []),
            'selection_audit': bundle.get('selection_audit', []), 'origin': str(Path(selection.__file__).resolve())}


def run_worker(root, entry, profile):
    env = {k: v for k, v in os.environ.items() if not k.startswith('WORLD_INTEL_') and k not in {'PYTHONPATH', 'PYTHONHOME'}}
    env.update(WORLD_INTEL_CONFIG=str(profile), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).resolve()), '--worker',
                             str(root), json.dumps(entry), str(profile)], cwd=root, env=env,
                            capture_output=True, text=True, encoding='utf-8', timeout=120)
    if result.returncode:
        try:
            diagnostic = json.loads(result.stdout)
        except ValueError:
            diagnostic = {}
        detail = diagnostic.get('detail')
        need(False, 'offline selection worker failed: ' + detail if isinstance(detail, str)
             and re.fullmatch(r'[A-Za-z_ ]{1,100}', detail) else 'offline selection worker failed')
    data = json.loads(result.stdout)
    need(data['origin'] == str(replay.module_layout(root)[1]['select_news'].resolve()), 'worker origin mismatch')
    return data


def source(root, expected):
    root = Path(root).resolve(strict=True)
    need(sha(expected, 40), 'full source commit required')
    def git(*args):
        return subprocess.check_output(['git', '--no-replace-objects', *args], cwd=root, timeout=60).decode().strip()
    need(git('rev-parse', 'HEAD') == expected and not git('status', '--porcelain', '--untracked-files=all'),
         'source must be exact committed clean tree')
    return root


def evaluate(pin_path, baseline_root, baseline_commit, baseline_profile, candidate_root, candidate_commit,
             candidate_profile, expectations, strict_equivalence_report, dispositions=None):
    pin_path = external(pin_path)
    pin = json.loads(pin_path.read_bytes())
    need(pin.get('version') == 1 and pin.get('baseline_commit') == baseline_commit, 'invalid baseline input pin')
    roots = [source(baseline_root, baseline_commit), source(candidate_root, candidate_commit)]
    need(roots[0] != roots[1], 'code roots must be distinct')
    profiles = [external(baseline_profile), external(candidate_profile)]
    hashes = [digest(p.read_bytes()) for p in profiles]
    expected_path = external(expectations)
    expected = json.loads(expected_path.read_bytes())
    need(expected.get('version') == 1 and expected.get('input_pin_sha256') == digest(pin_path.read_bytes())
         and expected.get('candidate_profile_sha256') == hashes[1], 'coverage expectations binding mismatch')
    strict_path = external(strict_equivalence_report)
    strict = json.loads(strict_path.read_bytes())
    need(type(strict.get('equivalent')) is bool and type(strict.get('days')) is int
         and strict['days'] >= 5 and isinstance(strict.get('results'), list)
         and len(strict['results']) == strict['days'], 'immutable strict equivalence report invalid')
    binding = {'baseline_commit': baseline_commit, 'candidate_commit': candidate_commit,
               'baseline_profile_sha256': hashes[0], 'candidate_profile_sha256': hashes[1],
               'input_pin_sha256': digest(pin_path.read_bytes()),
               'expectations_sha256': digest(expected_path.read_bytes()),
               'strict_equivalence_sha256': digest(strict_path.read_bytes())}
    reviews = []
    disposition_hash = None
    if dispositions:
        path = external(dispositions)
        review = json.loads(path.read_bytes())
        need(review.get('version') == 1 and review.get('binding') == binding and isinstance(review.get('reviews'), list),
             'dispositions not bound to current comparison')
        reviews = review['reviews']
        disposition_hash = digest(path.read_bytes())
    cases = []
    need(isinstance(pin.get('capsules'), list) and bool(pin['capsules']), 'empty capsule pin')
    seen = set()
    for entry in pin['capsules']:
        capture = load_capture(entry)
        need(capture['case_id'] not in seen, 'duplicate capsule pin')
        seen.add(capture['case_id'])
        before, after = [run_worker(root, entry, profile) for root, profile in zip(roots, profiles)]
        changes = selection_differences(before['selection'], after['selection'])
        count = review_differences(capture['case_id'], changes, reviews)
        bodies = {i['url']: i.get('content', '') for i in capture['render']['bundle'].get('news', [])
                  if isinstance(i, dict) and isinstance(i.get('url'), str)}
        selected_urls = {u for values in after['selection'].values() for u in values}
        case = {'case_id': capture['case_id'], 'date': capture['date'], 'mode': capture['mode'],
                'capture_source_commit': entry['expected_source_commit'], 'input_hashes': entry['files'],
                'baseline': before, 'candidate': after, 'differences': changes, 'unreviewed_changes': count,
                'body_availability': body_availability(sorted(selected_urls), bodies),
                'scheduled_acceptance_eligible': False,
                'historical_end_to_end': 'NOT_EXECUTED', 'model_output': 'not regenerated'}
        cases.append(case)
    known = {(c['case_id'], d['desk'], d['change_sha256']) for c in cases for d in c['differences']}
    need(all((r.get('case_id'), r.get('desk'), r.get('change_sha256')) in known for r in reviews), 'extraneous disposition')
    expectation_results = check_expectations(cases, expected.get('assertions'))
    return {'schema_version': 1, 'kind': 'coverage-regression', **binding, 'dispositions_sha256': disposition_hash,
            'expectations_sha256': digest(expected_path.read_bytes()), 'expectations': expectation_results,
            'status': 'PASS' if not any(c['unreviewed_changes'] for c in cases)
                               and all(e['passed'] for e in expectation_results) else 'HOLD',
            'acceptance_scope': 'reviewed offline selection policy only; not generated brief acceptance',
            'historical_end_to_end': 'UNAVAILABLE' if any('unavailable' in c['body_availability'].values() for c in cases)
                                     else 'NOT_EXECUTED',
            'strict_equivalence': {'artifact': str(strict_path), 'sha256': digest(strict_path.read_bytes()),
                                   'equivalent': strict['equivalent'], 'days': strict['days'],
                                   'limitation': 'immutable old comparison; deliberate V2 changes are not byte equivalence'},
            'cases': cases}


def validate_report(report, candidate_commit, candidate_profile_sha256, *, expectations=None, dispositions=None):
    need(set(report) == {'schema_version', 'kind', 'candidate_commit', 'baseline_commit',
         'candidate_profile_sha256', 'baseline_profile_sha256', 'input_pin_sha256', 'expectations_sha256',
         'strict_equivalence_sha256', 'dispositions_sha256', 'expectations', 'status', 'acceptance_scope',
         'historical_end_to_end', 'strict_equivalence', 'cases'}, 'invalid coverage report schema')
    need(report.get('schema_version') == 1 and report.get('kind') == 'coverage-regression'
         and report.get('candidate_commit') == candidate_commit and sha(candidate_commit, 40)
         and report.get('candidate_profile_sha256') == candidate_profile_sha256 and sha(candidate_profile_sha256),
         'coverage report binding mismatch')
    need(report.get('status') == 'PASS' and report.get('acceptance_scope') ==
         'reviewed offline selection policy only; not generated brief acceptance', 'coverage policy report not accepted')
    need(sha(report.get('baseline_commit'), 40) and sha(report.get('baseline_profile_sha256'))
         and sha(report.get('input_pin_sha256')) and sha(report.get('expectations_sha256')),
         'coverage report input binding missing')
    strict = report['strict_equivalence']
    strict_path = external(strict['artifact'])
    strict_data = json.loads(strict_path.read_bytes())
    need(sha(strict['sha256']) and digest(strict_path.read_bytes()) == strict['sha256']
         and strict['sha256'] == report['strict_equivalence_sha256']
         and strict_data.get('equivalent') == strict['equivalent'] and strict_data.get('days') == strict['days']
         and type(strict['days']) is int and strict['days'] >= 5, 'strict historical evidence mismatch')
    need(isinstance(report.get('cases'), list) and bool(report['cases']), 'coverage cases missing')
    ids = set()
    missing = False
    for case in report['cases']:
        need(case['case_id'] not in ids, 'duplicate coverage case')
        ids.add(case['case_id'])
        date = dt.date.fromisoformat(case['date'])
        need(date.isoformat() == case['date'] and case['mode'] in {'scheduled', 'forced'}
             and case['scheduled_acceptance_eligible'] is False and case['model_output'] == 'not regenerated'
             and case['historical_end_to_end'] == 'NOT_EXECUTED', 'invalid historical scope')
        need(sha(case['capture_source_commit'], 40) and set(case['input_hashes']) == {'manifest.json', *replay.BOUNDARIES}
             and all(sha(h) for h in case['input_hashes'].values()), 'invalid captured input binding')
        for side in ('baseline', 'candidate'):
            result = case[side]
            need(result['selection_sha256'] == digest(result['selection']) and result['counts'] ==
                 {k: len(v) for k, v in result['selection'].items()}, 'selection result integrity mismatch')
        changes = selection_differences(case['baseline']['selection'], case['candidate']['selection'])
        need([{k: d[k] for k in ('desk', 'before', 'after', 'change_sha256')} for d in case['differences']] == changes,
             'coverage differences omitted or modified')
        reviews = [d['review'] for d in case['differences'] if d.get('review') is not None]
        need(case['unreviewed_changes'] == 0 and review_differences(case['case_id'], changes, reviews) == 0,
             'unreviewed coverage changes')
        available = case['body_availability']
        need(set(available) == {u for v in case['candidate']['selection'].values() for u in v}
             and all(v in {'available', 'unavailable'} for v in available.values()), 'body availability incomplete')
        missing |= 'unavailable' in available.values()
    need(report.get('historical_end_to_end') == ('UNAVAILABLE' if missing else 'NOT_EXECUTED'), 'historical scope misrepresented')
    assertions = [{k: e[k] for k in ('case_id', 'kind', 'urls')} for e in report['expectations']]
    verified = check_expectations(report['cases'], assertions)
    need(verified == report['expectations'] and all(e['passed'] for e in verified), 'positive coverage expectations failed')
    need(expectations is not None, 'hashed external expectations required for validation')
    validate_review_inputs(report, expectations, dispositions)
    return True


def main(argv=None):
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        try:
            with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                result = worker(sys.argv[2], json.loads(sys.argv[3]), sys.argv[4])
            print(json.dumps(result))
            return 0
        except Exception as exc:
            print(json.dumps({'error': 'offline selection evaluation failed',
                              'detail': str(exc) if isinstance(exc, HistoryError) else type(exc).__name__}))
            return 2
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('pin-path', 'baseline-root', 'baseline-commit', 'baseline-profile',
                 'candidate-root', 'candidate-commit', 'candidate-profile', 'expectations', 'strict-equivalence-report'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--dispositions')
    parser.add_argument('--report', required=True)
    args = vars(parser.parse_args(argv))
    report = external(args.pop('report'))
    need(not report.exists(), 'report must be new; preserve prior comparisons')
    result = evaluate(**args)
    with report.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({k: result[k] for k in ('status', 'candidate_commit', 'historical_end_to_end')}))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
