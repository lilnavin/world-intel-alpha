import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

from worldintel.core import capsule  # noqa: E402


def _pending():
    p = capsule.Pending(started_at="2026-09-23T07:15:03+09:00",
                        mode="forced", source_commit="abc1234")
    p.add("selection-input", capsule.snapshot({"news": [{"title": "x"}]}))
    p.add("render-input", capsule.snapshot({"briefing": {}}))
    return p


def test_snapshot_is_immune_to_later_mutation():
    # The whole point: run.py mutates `bundle` after the L1 boundary.
    obj = {"news": [{"title": "before"}]}
    blob = capsule.snapshot(obj)
    obj["news"][0]["title"] = "after"
    assert json.loads(blob)["news"][0]["title"] == "before"


def test_snapshot_touches_no_disk(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("snapshot must not open files")
    monkeypatch.setattr("builtins.open", _boom)
    assert capsule.snapshot({"a": 1})


def test_write_produces_a_complete_capsule(tmp_path):
    path = capsule.write(_pending(), str(tmp_path))
    assert path is not None
    names = sorted(os.listdir(path))
    assert names == ["manifest.json", "render-input.json", "selection-input.json"]

    manifest = json.loads(
        open(os.path.join(path, "manifest.json"), encoding="utf-8").read())
    assert manifest["format_version"] == capsule.FORMAT_VERSION
    assert manifest["mode"] == "forced"
    assert manifest["source_commit"] == "abc1234"
    assert set(manifest["files"]) == {"selection-input.json", "render-input.json"}
    assert all(len(v["sha256"]) == 64 for v in manifest["files"].values())


def test_a_dirty_tree_is_recorded_and_changes_the_id(tmp_path):
    """Provenance, not decoration. Two runs whose only difference is
    uncommitted edits must not land on the same capsule id, and a replay must
    be able to tell that the named commit did not produce these bytes."""
    clean = capsule.write(_pending(), str(tmp_path))
    dirty_pending = _pending()
    dirty_pending.source_dirty = True
    dirty = capsule.write(dirty_pending, str(tmp_path))

    assert os.path.basename(clean) != os.path.basename(dirty)
    for path, expected in ((clean, False), (dirty, True)):
        manifest = json.loads(
            open(os.path.join(path, "manifest.json"), encoding="utf-8").read())
        assert manifest["source_dirty"] is expected


def test_boundaries_name_functions_not_line_numbers(tmp_path):
    """A capsule outlives the source layout. The first version of this manifest
    hardcoded run.py:343 and :647; both were already wrong when written, and
    wiring the capture moved them again the same day. Plan 1 and Plan 2 move
    them further. A coordinate in a persisted artifact is a lie with a
    timestamp -- the callee name is what a replay actually needs."""
    import re
    path = capsule.write(_pending(), str(tmp_path))
    manifest = json.loads(
        open(os.path.join(path, "manifest.json"), encoding="utf-8").read())
    for name, where in manifest["boundaries"].items():
        assert not re.search(r":\d+", where), (
            f"{name} cites a line number ({where}); it will rot")
        assert "." in where, f"{name} must name module.function, got {where!r}"


def test_hashes_match_the_written_bytes(tmp_path):
    import hashlib
    path = capsule.write(_pending(), str(tmp_path))
    manifest = json.loads(
        open(os.path.join(path, "manifest.json"), encoding="utf-8").read())
    for name, meta in manifest["files"].items():
        raw = open(os.path.join(path, name), "rb").read()
        assert hashlib.sha256(raw).hexdigest() == meta["sha256"], name


def test_no_partial_capsule_is_left_on_failure(tmp_path, monkeypatch):
    # An interrupted write must leave nothing a replay could mistake for valid.
    real = capsule._write_file

    def _fail_on_second(dest, name, blob):
        if name == "render-input.json":
            raise OSError("disk full")
        return real(dest, name, blob)

    monkeypatch.setattr(capsule, "_write_file", _fail_on_second)
    assert capsule.write(_pending(), str(tmp_path)) is None
    assert os.listdir(tmp_path) == []


def test_write_never_raises(tmp_path):
    # Fail-open: a capture failure must never cost a brief.
    assert capsule.write(_pending(), "\x00 not a path") is None


def test_capsule_id_is_unique_per_run():
    a = capsule.capsule_id("2026-09-23T07:15:03+09:00", "run-a")
    b = capsule.capsule_id("2026-09-23T07:15:03+09:00", "run-b")
    assert a != b
    assert a.startswith("2026-09-23T071503+0900-")


def test_config_fingerprint_excludes_personal_values():
    fp = capsule.config_fingerprint({
        "contact_email": "someone@example.com",
        "data_root": "/home/someone/data",
        "freshness_hours": 36,
        "feed_count": 53,
    })
    flat = json.dumps(fp)
    assert "someone@example.com" not in flat
    assert "/home/someone" not in flat
    assert fp["freshness_hours"] == 36
    assert fp["feed_count"] == 53


def test_capsule_never_writes_to_stdout(tmp_path, capsys):
    """run.py's delivery contract is a single `MEDIA:<path>` line on stdout,
    which the cron layer's extract_media parses. A capsule log on stdout would
    corrupt that line and break Telegram delivery silently -- the run would
    still exit 0 and nothing would say why no brief arrived."""
    p = _pending()
    assert capsule.write(p, str(tmp_path)) is not None
    capsule.prune(str(tmp_path), 0)
    captured = capsys.readouterr()
    assert captured.out == "", f"capsule wrote to stdout: {captured.out!r}"


def test_prune_removes_oldest_first(tmp_path):
    for stamp in ("2026-09-20T070000+0900-aaaaaaaa",
                  "2026-09-21T070000+0900-bbbbbbbb",
                  "2026-09-22T070000+0900-cccccccc"):
        os.makedirs(os.path.join(str(tmp_path), stamp))
    assert capsule.prune(str(tmp_path), keep=2) == 1
    assert sorted(os.listdir(tmp_path)) == [
        "2026-09-21T070000+0900-bbbbbbbb", "2026-09-22T070000+0900-cccccccc"]


def test_capsule_directory_is_not_world_readable(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX mode bits are not meaningful on Windows; "
                    "see test_windows_capsule_acl_is_user_only")
    path = capsule.write(_pending(), str(tmp_path))
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode & (stat.S_IRGRP | stat.S_IROTH) == 0


@pytest.mark.skipif(os.name != "nt", reason="NTFS ACLs")
def test_windows_capsule_acl_is_user_only(tmp_path):
    """Windows is the ACTUAL target platform, so skipping the POSIX test there
    would leave capsules -- full article bodies and config-derived values --
    with no verified protection at all.

    The store sits under the user profile, which inherits a user-only ACL by
    default. This asserts that inheritance actually held rather than assuming
    it: a store created under a relocated or redirected profile can differ.
    """
    import subprocess
    path = capsule.write(_pending(), str(tmp_path))
    out = subprocess.run(["icacls", path], capture_output=True, text=True).stdout
    for principal in ("Everyone", "BUILTIN\\Users", "Authenticated Users"):
        assert principal not in out, f"{principal} can read capsules:\n{out}"
