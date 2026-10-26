"""Migration proof uses exact bytes and complete relative-path inventories."""
import copy
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import prove_move_only as proof


@pytest.fixture
def migration(tmp_path):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    blobs = {'run.py': b'import os\r\nimport helper as h  # keep comment\r\nfrom config import get as cfg\r\nx = 1\r\n',
             'config.py': b'def get():\n    return 1\n',
             'delivery.py': b'"""delivery"""\n',
             'helper.py': b'def value():\n    try:\n        import delivery\n        x = 1\n    except Exception:\n        x = 2\n    return x\n'}
    for name, blob in blobs.items():
        (scripts / name).write_bytes(blob)
    snapshot = proof.record(scripts)
    package = tmp_path / 'worldintel'
    modules = {Path(path).stem for path in snapshot['files']}
    for name, blob in blobs.items():
        path = tmp_path / proof.target('scripts/' + name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(proof.transform(blob, modules))
    for rel, blob in proof.INITIALIZERS.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
    return scripts, snapshot, package


def test_exact_migration_aliases_and_from_import_pass(migration):
    _, snapshot, package = migration
    assert proof.verify(snapshot, package)['move_only']
    blob = (package / 'run.py').read_bytes()
    assert b'from worldintel.core import helper as h  # keep comment\r\n' in blob
    assert b'from worldintel.config import get as cfg\r\n' in blob


@pytest.mark.parametrize('change', ['indent', 'body', 'comment', 'blank', 'newline', 'bootstrap'])
def test_nonimport_bytes_cannot_change(migration, change):
    _, snapshot, package = migration
    path = package / 'core/helper.py'
    blob = path.read_bytes()
    changes = {'indent': blob.replace(b'        x = 1', b'    x = 1'),
               'body': blob.replace(b'x = 1', b'x = 9'),
               'comment': blob + b'# extra comment\n', 'blank': blob + b'\n',
               'newline': blob.replace(b'\n', b'\r\n'),
               'bootstrap': blob + b'import sys\nsys.path.insert(0, "scripts")\n'}
    path.write_bytes(changes[change])
    assert not proof.verify(snapshot, package)['move_only']


@pytest.mark.parametrize('change', ['try_to_except', 'earlier', 'delete', 'add', 'alias', 'wrong_namespace'])
def test_import_identity_scope_and_position_are_fixed(migration, change):
    _, snapshot, package = migration
    path = package / 'core/helper.py'
    blob = path.read_bytes()
    line = b'        from worldintel import delivery\n'
    if change == 'try_to_except':
        blob = blob.replace(line, b'').replace(b'    except Exception:\n', b'    except Exception:\n' + line)
    elif change == 'earlier':
        blob = blob.replace(line, b'').replace(b'    try:\n', b'    from worldintel import delivery\n    try:\n')
    elif change == 'delete':
        blob = blob.replace(line, b'')
    elif change == 'add':
        blob += b'import pathlib\n'
    elif change == 'alias':
        blob = blob.replace(b'import delivery', b'import delivery as changed')
    else:
        blob = blob.replace(b'from worldintel import', b'from unrelated import')
    path.write_bytes(blob)
    assert not proof.verify(snapshot, package)['move_only']


@pytest.mark.parametrize('change', ['missing', 'added', 'collision', 'initializer'])
def test_complete_inventory_rejects_missing_added_and_basename_collision(migration, change):
    _, snapshot, package = migration
    if change == 'missing':
        (package / 'core/helper.py').unlink()
    elif change == 'added':
        (package / 'core/new.py').write_bytes(b'')
    elif change == 'collision':
        (package / 'adapters/helper.py').write_bytes((package / 'core/helper.py').read_bytes())
    else:
        (package / '__init__.py').write_bytes(b'print("unexpected")\n')
    assert not proof.verify(snapshot, package)['move_only']


def test_empty_initializers_allowed(migration):
    _, snapshot, package = migration
    for name in proof.INITIALIZERS:
        (package.parent / name).write_bytes(b'')
    assert proof.verify(snapshot, package)['move_only']


def test_unicode_multiline_and_mixed_imports_preserve_binding_and_body():
    blob = ('label = "日本"; import helper as h, os\n'
            'def f():\n    from helper import (\n        first as one,\n        second,\n    )\n    return one\n').encode()
    expected = ('label = "日本"; from worldintel.core import helper as h; import os\n'
                'def f():\n    from worldintel.core.helper import first as one, second\n    return one\n').encode()
    assert proof.transform(blob, {'helper'}) == expected


def test_external_and_relative_imports_are_byte_exact():
    blob = b'import os as operating\nfrom os import (path,)\nfrom . import helper\n'
    assert proof.transform(blob, {'helper'}) == blob


def test_import_internal_comment_is_not_silently_lost():
    with pytest.raises(ValueError, match='comment'):
        proof.transform(b'from helper import (\n    one, # meaningful\n)\n', {'helper'})


def test_snapshot_hash_tampering_fails(migration):
    _, snapshot, package = migration
    altered = copy.deepcopy(snapshot)
    altered['files']['scripts/run.py']['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='integrity'):
        proof.verify(altered, package)


def test_nested_baseline_cannot_collapse_to_same_basename(migration):
    scripts, _, _ = migration
    (scripts / 'nested').mkdir()
    (scripts / 'nested/helper.py').write_bytes(b'')
    with pytest.raises(ValueError, match='flat'):
        proof.record(scripts)


def test_record_from_commit_ignores_dirty_worktree(migration):
    scripts, snapshot, package = migration
    root = scripts.parent
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    git('init')
    git('config', 'core.autocrlf', 'false')
    git('add', 'scripts')
    git('-c', 'user.name=Synthetic', '-c', 'user.email=test@example.com', 'commit', '-m', 'baseline')
    (scripts / 'helper.py').write_bytes(b'raise RuntimeError("dirty")\n')
    committed = proof.record(scripts, 'HEAD')
    assert len(committed['commit']) == 40
    assert committed['files'] == snapshot['files']
    assert proof.verify(committed, package)['move_only']


def test_empty_inventory_fails(tmp_path):
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    with pytest.raises(ValueError, match='empty'):
        proof.record(scripts)
