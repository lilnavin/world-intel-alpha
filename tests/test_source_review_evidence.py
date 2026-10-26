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
