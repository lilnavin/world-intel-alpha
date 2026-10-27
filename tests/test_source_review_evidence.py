"""Release validation binds captured initial/corrected/final prose, never semantics."""
import copy
import json

import pytest
from model_evidence_fixture import model_evidence


def audit(repaired=False):
    from worldintel.core.grounding_review import SCOPE
    return {"status": "accepted", "attempted": True, "cards_reviewed": 1, "scope": SCOPE,
            "timeout_s": 100, "reason": None, "repair_attempted": repaired,
            "review_calls": 2 if repaired else 1, "corrected_cards": 1 if repaired else 0}


def test_raw_review_must_bind_real_schema_and_material(tmp_path):
    from tools.verify_minimal_run import validate_model_evidence
    summary = model_evidence(tmp_path)
    assert validate_model_evidence(tmp_path / summary['run_id'], summary, audit()) == []


def test_repaired_raw_evidence_binds_final_prose_instead_of_rejected_original(tmp_path):
    from tools.verify_minimal_run import validate_model_evidence
    summary = model_evidence(tmp_path, repaired=True)
    assert validate_model_evidence(tmp_path / summary['run_id'], summary, audit(True)) == []


@pytest.mark.parametrize('change', ['initial-forgery', 'wrong-patch-hash', 'supported-patch', 'final-rejected',
                                    'changed-citation', 'extra-final', 'wrong-schema', 'wrong-prompt', 'final-timeout'])
def test_rehashed_but_invalid_raw_review_chain_rejected(tmp_path, change):
    import hashlib
    from tools.verify_minimal_run import validate_model_evidence
    summary = model_evidence(tmp_path, repaired=True)
    root = tmp_path / summary['run_id']
    index, filename = 2, 'output.bin'
    if change == 'wrong-patch-hash': index = 3
    elif change in ('final-rejected', 'changed-citation', 'wrong-schema', 'wrong-prompt', 'final-timeout'): index = 4
    if change == 'changed-citation': filename = 'stdin.bin'
    elif change == 'wrong-schema': filename = 'schema.bin'
    elif change == 'wrong-prompt': filename = 'stdin.bin'
    record = summary['invocations'][index]
    folder = root / record['invocation_id']
    if change == 'extra-final':
        summary['invocations'].append(copy.deepcopy(summary['invocations'][-1]))
    elif change == 'final-timeout':
        manifest = json.loads((folder / 'manifest.json').read_bytes())
        manifest['timeout_s'] = 99
        (folder / 'manifest.json').write_text(json.dumps(manifest))
    else:
        raw = (folder / filename).read_bytes()
        if change == 'wrong-schema': raw = b'{}'
        elif change == 'wrong-prompt': raw = b'untrusted replacement prefix\nINPUT_JSON\n' + raw.split(b'\nINPUT_JSON\n')[1]
        elif change == 'changed-citation': raw = raw.replace(b'https://example.test/bridge', b'https://example.test/forged')
        else:
            out = json.loads(raw)
            if change == 'initial-forgery': out['cards'][0]['prose_sha256'] = 'f' * 64
            elif change == 'wrong-patch-hash': out['cards'][0]['prose_sha256'] = 'f' * 64
            elif change == 'supported-patch': out['cards'][0]['verdict'] = 'supported'
            else: out['cards'][0]['verdict'] = 'uncertain'
            raw = json.dumps(out).encode()
        (folder / filename).write_bytes(raw)
        manifest = json.loads((folder / 'manifest.json').read_bytes())
        manifest['files'][filename] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        (folder / 'manifest.json').write_text(json.dumps(manifest))
    record['manifest_sha256'] = hashlib.sha256((folder / 'manifest.json').read_bytes()).hexdigest()
    assert validate_model_evidence(root, summary, audit(True))


def test_windows_prompt_stdin_bytes_preserve_platform_line_endings(tmp_path):
    import hashlib
    from tools.verify_minimal_run import validate_model_evidence
    summary = model_evidence(tmp_path, repaired=True)
    root = tmp_path / summary['run_id']
    for record in summary['invocations'][2:]:
        folder = root / record['invocation_id']
        raw = (folder / 'stdin.bin').read_bytes().replace(b'\n', b'\r\n')
        (folder / 'stdin.bin').write_bytes(raw)
        manifest = json.loads((folder / 'manifest.json').read_bytes())
        manifest['files']['stdin.bin'] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        (folder / 'manifest.json').write_text(json.dumps(manifest))
        record['manifest_sha256'] = hashlib.sha256((folder / 'manifest.json').read_bytes()).hexdigest()
    assert validate_model_evidence(root, summary, audit(True)) == []


@pytest.mark.parametrize('repaired', [False, True])
def test_cleanup_chain_recomputes_actual_selected_input_and_raw_cards(tmp_path, repaired):
    from tools.verify_minimal_run import validate_model_evidence
    summary = model_evidence(tmp_path, repaired=repaired, cleanup=True)
    status = cleanup_status(tmp_path / summary['run_id'], summary, repaired)
    assert validate_model_evidence(tmp_path / summary['run_id'], summary, status) == []


@pytest.mark.parametrize('change', ['audit-removal', 'invent-original', 'available-source', 'missing-anchor',
                                    'raw-prose', 'raw-citation', 'selected-duplicate', 'final-audit'])
def test_rehashed_cleanup_forgery_rejected(tmp_path, change):
    import hashlib
    from tools.verify_minimal_run import validate_model_evidence
    from worldintel.core import grounding_review as g
    summary = model_evidence(tmp_path, repaired=True, cleanup=True)
    root = tmp_path / summary['run_id']
    status = cleanup_status(root, summary, True)
    index = 4 if change == 'final-audit' else 2 if change in ('audit-removal', 'invent-original') else 0
    filename = 'output.bin' if change in ('raw-prose', 'raw-citation') else 'stdin.bin'
    record = summary['invocations'][index]
    folder = root / record['invocation_id']
    raw = (folder / filename).read_text()
    if index in (2, 4):
        prefix, body = raw.split('\nINPUT_JSON\n')
        material = json.loads(body)
        entry = material['citation_preflight']['cards'][0]
        if change == 'audit-removal': entry['removed'] = []
        elif change == 'invent-original': entry['original_sources'].append({'url': 'https://invented.test'})
        else: entry['retained_sources'].reverse(); entry['prose_sha256'] = 'a' * 64
        material['citation_preflight']['sha256'] = g._digest(json.dumps(material['citation_preflight']['cards'], ensure_ascii=False, sort_keys=True))
        raw = prefix + '\nINPUT_JSON\n' + json.dumps(material)
    elif filename == 'output.bin':
        value = json.loads(raw)
        card = value['briefing']['brief'][0]
        if change == 'raw-prose': card['body'] = 'Invented original prose.'
        else: card['sources'].reverse()
        raw = json.dumps(value)
    else:
        prefix, body = raw.split('\n## TODAY (BUNDLE)\n')
        bundle = json.loads(body)
        if change == 'available-source': bundle['news'][1]['content'] = 'Available evidence'
        elif change == 'missing-anchor': bundle['news'][0]['tier'] = 3
        else: bundle['news'].append(copy.deepcopy(bundle['news'][1]))
        raw = prefix + '\n## TODAY (BUNDLE)\n' + json.dumps(bundle)
    blob = raw.encode()
    (folder / filename).write_bytes(blob)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    manifest['files'][filename] = {'bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}
    (folder / 'manifest.json').write_text(json.dumps(manifest))
    record['manifest_sha256'] = hashlib.sha256((folder / 'manifest.json').read_bytes()).hexdigest()
    assert validate_model_evidence(root, summary, status)


def cleanup_status(root, summary, repaired):
    status = audit(repaired)
    folder = root / summary['invocations'][2]['invocation_id']
    material = json.loads((folder / 'stdin.bin').read_text().split('\nINPUT_JSON\n')[1])
    cleanup = material['citation_preflight']
    status.update(citation_preflight_sha256=cleanup['sha256'], citation_preflight_cards=len(cleanup['cards']))
    return status


def test_cleanup_audit_cannot_be_stripped_from_rehashed_captures():
    from worldintel.core import grounding_review as g
    status = audit()
    status.update(citation_preflight_sha256='a' * 64, citation_preflight_cards=1)
    with pytest.raises(ValueError):
        g.validate_cleanup_audit({'cards': [], 'sources': []}, status)


def test_stripping_all_cleanup_capture_fields_still_fails_retained_status(tmp_path):
    import hashlib
    from tools.verify_minimal_run import validate_model_evidence
    summary = model_evidence(tmp_path, repaired=True, cleanup=True)
    root = tmp_path / summary['run_id']
    status = cleanup_status(root, summary, True)
    for record in summary['invocations'][2:]:
        folder = root / record['invocation_id']
        prefix, body = (folder / 'stdin.bin').read_text().split('\nINPUT_JSON\n')
        material = json.loads(body)
        material.pop('citation_preflight', None)
        blob = (prefix + '\nINPUT_JSON\n' + json.dumps(material)).encode()
        (folder / 'stdin.bin').write_bytes(blob)
        manifest = json.loads((folder / 'manifest.json').read_bytes())
        manifest['files']['stdin.bin'] = {'bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}
        (folder / 'manifest.json').write_text(json.dumps(manifest))
        record['manifest_sha256'] = hashlib.sha256((folder / 'manifest.json').read_bytes()).hexdigest()
    assert validate_model_evidence(root, summary, status)


@pytest.mark.parametrize('tier', [True, 1.9, 2.9, '1'])
def test_cleanup_evidence_replay_rejects_malformed_anchor_tier(tmp_path, tier):
    import hashlib
    from tools.verify_minimal_run import validate_model_evidence
    summary = model_evidence(tmp_path, cleanup=True)
    root = tmp_path / summary['run_id']
    status = cleanup_status(root, summary, False)
    record = summary['invocations'][0]
    folder = root / record['invocation_id']
    prefix, body = (folder / 'stdin.bin').read_text().split('\n## TODAY (BUNDLE)\n')
    bundle = json.loads(body)
    bundle['news'][0]['tier'] = tier
    blob = (prefix + '\n## TODAY (BUNDLE)\n' + json.dumps(bundle)).encode()
    (folder / 'stdin.bin').write_bytes(blob)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    manifest['files']['stdin.bin'] = {'bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}
    (folder / 'manifest.json').write_text(json.dumps(manifest))
    record['manifest_sha256'] = hashlib.sha256((folder / 'manifest.json').read_bytes()).hexdigest()
    assert validate_model_evidence(root, summary, status)


def test_standalone_cleanup_validation_uses_explicit_profile_without_global_config(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from worldintel import config
    summary = model_evidence(tmp_path, cleanup=True)
    root = tmp_path / summary['run_id']
    payload = tmp_path / 'validation.json'
    payload.write_text(json.dumps({'summary': summary, 'audit': cleanup_status(root, summary, False)}))
    profile = Path(__file__).resolve().parents[1] / 'worldintel/data/profiles/example.minimal.yaml'
    env = os.environ.copy()
    env.pop('WORLD_INTEL_CONFIG', None)
    env.update(PYTHONPATH=str(profile.parents[3]), USERPROFILE=str(tmp_path), HOME=str(tmp_path))
    code = """
import json, os, sys
from worldintel import config
from tools.verify_minimal_run import validate_model_evidence
assert config._CACHE is None and 'WORLD_INTEL_CONFIG' not in os.environ
payload = json.load(open(sys.argv[1]))
cfg = config.load(sys.argv[3])
assert config._CACHE is None
errors = validate_model_evidence(sys.argv[2], payload['summary'], payload['audit'], cfg=cfg)
assert errors == [], errors
assert config._CACHE is None
"""
    completed = subprocess.run([sys.executable, '-c', code, str(payload), str(root), str(profile)],
                               cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
