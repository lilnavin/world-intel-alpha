import json
from pathlib import Path
import sys
import subprocess

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import deploy


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "source"
    (root / "worldintel/data").mkdir(parents=True)
    (root / "worldintel/run.py").write_text("def main(): return 0\n", encoding="utf-8")
    (root / "worldintel/data/example.json").write_bytes(b'{"synthetic": true}\r\n')
    (root / "deploy").mkdir()
    (root / "deploy/world_intel_brief.py").write_text("# template\n", encoding="utf-8")
    (root / ".gitignore").write_text(".env\n*.local.yaml\n", encoding="utf-8")
    (root / ".gitattributes").write_text("* -text\n", encoding="utf-8")
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Synthetic", "-c", "user.email=tester@example.org",
                    "commit", "-m", "synthetic deployment fixture"], check=True, capture_output=True)
    return root


def test_scratch_copy_contains_package_assets_and_provenance(source, tmp_path):
    destination = tmp_path / "installed"
    stamp = deploy.deploy(source, destination, scratch=True)
    assert stamp["scratch"]
    assert (destination / "worldintel/data/example.json").read_bytes() == b'{"synthetic": true}\r\n'
    assert (destination / "world_intel_brief.py").is_file()
    assert json.loads((destination / "worldintel/.provenance.json").read_text())["commit"] == deploy.provenance(source)[0]
    assert "world_intel_brief.py" in stamp["files"]
    assert not (destination / ".deployment-incomplete").exists()


def test_existing_destination_is_preserved(source, tmp_path):
    destination = tmp_path / "live"
    destination.mkdir()
    old = destination / "run.py"
    old.write_bytes(b"existing live file")
    with pytest.raises(FileExistsError):
        deploy.deploy(source, destination)
    assert old.read_bytes() == b"existing live file"


def test_dirty_source_requires_explicit_scratch(source, tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "provenance", lambda root: ("a" * 40, True))
    with pytest.raises(ValueError, match="clean"):
        deploy.deploy(source, tmp_path / "installed")
    assert not (tmp_path / "installed").exists()


def test_source_overlap_is_rejected(source):
    with pytest.raises(ValueError, match="overlap"):
        deploy.deploy(source, source / "worldintel/installed", scratch=True)


def test_ignored_private_files_are_never_copied(source, tmp_path):
    (source / "worldintel/.env").write_text("PRIVATE=synthetic\n", encoding="utf-8")
    (source / "worldintel/data/private.local.yaml").write_text("private: synthetic\n", encoding="utf-8")
    assert not deploy.provenance(source)[1]
    for scratch in (False, True):
        target = tmp_path / f"installed-{scratch}"
        stamp = deploy.deploy(source, target, scratch=scratch)
        assert not (target / "worldintel/.env").exists()
        assert not (target / "worldintel/data/private.local.yaml").exists()
        assert len(stamp["files"]) == 3


def test_clean_deploy_rechecks_committed_bytes_after_status(source, tmp_path, monkeypatch):
    commit, _ = deploy.provenance(source)
    monkeypatch.setattr(deploy, "provenance", lambda root: (commit, False))
    (source / "worldintel/run.py").write_text("changed after status\n", encoding="utf-8")
    with pytest.raises(ValueError, match="committed"):
        deploy.deploy(source, tmp_path / "installed")
    assert not (tmp_path / "installed").exists()


def test_scratch_allows_nonignored_new_runtime_only(source, tmp_path):
    (source / "worldintel/new.py").write_text("# synthetic\n", encoding="utf-8")
    (source / "worldintel/private.txt").write_text("not runtime\n", encoding="utf-8")
    target = tmp_path / "scratch"
    stamp = deploy.deploy(source, target, scratch=True)
    assert stamp["dirty"] and stamp["scratch"]
    assert (target / "worldintel/new.py").is_file()
    assert not (target / "worldintel/private.txt").exists()


def test_atomic_directory_reservation_preserves_racing_destination(source, tmp_path, monkeypatch):
    target = tmp_path / "raced"
    original_mkdir = Path.mkdir
    def racing_mkdir(path, *args, **kwargs):
        if path == target:
            original_mkdir(path)
        return original_mkdir(path, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    with pytest.raises(FileExistsError):
        deploy.deploy(source, target)
    assert list(target.iterdir()) == []


def test_failed_copy_leaves_incomplete_marker_without_stamp(source, tmp_path, monkeypatch):
    target = tmp_path / "failed"
    original_open = Path.open
    def failing_open(path, *args, **kwargs):
        if path == target / "worldintel/run.py":
            raise OSError("synthetic disk error")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(OSError):
        deploy.deploy(source, target)
    assert (target / ".deployment-incomplete").is_file()
    assert not (target / "worldintel/.provenance.json").exists()
