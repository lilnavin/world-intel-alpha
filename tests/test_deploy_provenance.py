"""Installed source identity is verified, not trusted from a stale stamp."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worldintel import run


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root = tmp_path / "installed"
    package = root / "worldintel"
    (package / "data").mkdir(parents=True)
    blobs = {"worldintel/run.py": b"# synthetic runner\n", "worldintel/data/item.yaml": b"synthetic: true\n",
             "world_intel_brief.py": b"# synthetic launcher\n"}
    for name, blob in blobs.items():
        (root / name).write_bytes(blob)
    stamp = {"commit": "a" * 40, "dirty": False, "scratch": False,
             "files": {name: hashlib.sha256(blob).hexdigest() for name, blob in blobs.items()}}
    (package / ".provenance.json").write_text(json.dumps(stamp), encoding="utf-8")
    monkeypatch.setattr(run, "__file__", str(package / "run.py"))
    return root, stamp


def test_verified_install_is_clean(installed):
    assert run._source_provenance() == ("a" * 40, False)


@pytest.mark.parametrize("mutation", ["modified", "missing", "added_python", "added_asset", "launcher", "incomplete", "legacy"])
def test_changed_or_incomplete_install_is_dirty(installed, mutation):
    root, stamp = installed
    if mutation == "modified":
        (root / "worldintel/run.py").write_bytes(b"changed")
    elif mutation == "missing":
        (root / "worldintel/data/item.yaml").unlink()
    elif mutation == "added_python":
        (root / "worldintel/extra.py").write_bytes(b"added")
    elif mutation == "added_asset":
        (root / "worldintel/data/extra.json").write_bytes(b"{}")
    elif mutation == "launcher":
        (root / "world_intel_brief.py").write_bytes(b"changed")
    elif mutation == "incomplete":
        (root / ".deployment-incomplete").touch()
    else:
        stamp.pop("files")
        (root / "worldintel/.provenance.json").write_text(json.dumps(stamp))
    assert run._source_provenance()[1] is True


@pytest.mark.parametrize("name", ["../outside.py", "/absolute.py", "worldintel/../outside.py", "C:/outside.py", "worldintel\\outside.py"])
def test_manifest_paths_cannot_escape_installation(installed, name):
    root, stamp = installed
    stamp["files"][name] = "a" * 64
    (root / "worldintel/.provenance.json").write_text(json.dumps(stamp))
    assert run._source_provenance()[1] is True


def test_scratch_stamp_never_becomes_clean(installed):
    root, stamp = installed
    stamp["scratch"] = True
    (root / "worldintel/.provenance.json").write_text(json.dumps(stamp))
    assert run._source_provenance()[1] is True


def test_bytecode_cache_does_not_change_runtime_inventory(installed):
    root, _ = installed
    (root / "worldintel/__pycache__").mkdir()
    (root / "worldintel/__pycache__/run.pyc").write_bytes(b"cache")
    assert run._source_provenance()[1] is False


def test_symlink_traversal_is_dirty(installed, monkeypatch):
    root, _ = installed
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == root / "worldintel/data" or original(path))
    assert run._source_provenance()[1] is True


@pytest.mark.parametrize("key,value", [("dirty", None), ("scratch", "false"), ("commit", "")])
def test_malformed_stamp_fails_closed(installed, key, value):
    root, stamp = installed
    stamp[key] = value
    (root / "worldintel/.provenance.json").write_text(json.dumps(stamp))
    assert run._source_provenance()[1] is True
