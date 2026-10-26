"""Real Git index tests, with only synthetic sensitive-shaped data."""
from pathlib import Path
import subprocess
import sys
import shutil

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import check_staged


def git(*args, data=None):
    return subprocess.run(["git", *args], input=data, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


@pytest.fixture
def repository(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    git("init")
    return tmp_path


def secret():
    return "sk-" + "A" * 20


def stage(path, content):
    path.write_text(content, encoding="utf-8")
    git("add", "--", str(path))


def test_secret_staged_clean_worktree_is_blocked(repository, capsys):
    path = repository / "sample.txt"
    stage(path, secret())
    path.write_text("clean")
    assert check_staged.main() == 1
    assert secret() not in capsys.readouterr().err


def test_clean_staged_secret_worktree_is_allowed(repository):
    path = repository / "sample.txt"
    stage(path, "clean")
    path.write_text(secret())
    assert check_staged.main() == 0


def test_no_staged_changes_is_allowed(repository):
    assert check_staged.main() == 0


def test_rename_destination_is_scanned(repository):
    old = repository / "old.txt"
    stage(old, "clean")
    git("-c", "user.name=Synthetic", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    git("mv", "old.txt", "new name.txt")
    stage(repository / "new name.txt", secret())
    assert check_staged.main() == 1


def test_deletion_has_no_outgoing_blob(repository):
    path = repository / "old.txt"
    stage(path, "clean")
    git("-c", "user.name=Synthetic", "-c", "user.email=test@example.com", "commit", "-m", "initial")
    git("rm", "old.txt")
    assert check_staged.main() == 0


@pytest.mark.parametrize('blob', [b'\x00PNG', b'\xff\xfe'])
def test_binary_and_undecodable_blobs_fail_closed(repository, blob):
    path = repository / "picture.png"
    path.write_bytes(blob)
    git("add", "picture.png")
    assert check_staged.main() == 2


def test_symlink_index_entry_fails_closed(repository):
    oid = git("hash-object", "-w", "--stdin", data=b"target.txt").strip().decode()
    git("update-index", "--add", "--cacheinfo", "120000," + oid + ",link")
    assert check_staged.main() == 2


@pytest.mark.parametrize('error', [FileNotFoundError(), subprocess.CalledProcessError(128, 'git')])
def test_missing_git_or_unreadable_index_fails_closed(repository, monkeypatch, error):
    def fail(*args):
        raise error
    monkeypatch.setattr(check_staged, 'git', fail)
    assert check_staged.main() == 2


def test_nonrepository_fails_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert check_staged.main() == 2


def test_empty_staged_file_is_allowed(repository):
    stage(repository / "empty.txt", "")
    assert check_staged.main() == 0


def test_secret_filename_is_blocked(repository):
    stage(repository / (secret() + '.txt'), 'clean')
    assert check_staged.main() == 1


def test_shell_hook_invokes_staged_scanner(repository):
    shell = shutil.which('sh')
    if not shell and shutil.which('git'):
        bundled = Path(shutil.which('git')).resolve().parents[1] / 'bin' / 'sh.exe'
        if bundled.is_file():
            shell = str(bundled)
    if not shell:
        pytest.skip('POSIX shell unavailable; Python scanner exercised separately')
    tools = repository / 'tools'
    tools.mkdir()
    source = Path(check_staged.__file__).parent
    for name in ('check_staged.py', 'scan_secrets.py', 'pre-commit'):
        shutil.copyfile(source / name, tools / name)
    stage(repository / 'sample.txt', secret())
    result = subprocess.run([shell, str(tools / 'pre-commit')], capture_output=True, timeout=30)
    assert result.returncode == 1
    assert b'BLOCKED' in result.stderr
