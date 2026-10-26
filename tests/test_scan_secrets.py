"""Tests for tools/scan_secrets.py.

EVERY forbidden string here is ASSEMBLED AT RUNTIME and never appears as a
literal. This file is scanned by the tool it tests and lives in a repository
that becomes public: a literal address or chat id here would add permanent
false entries to the inventory, make Plan 1's blocking CI scan impossible to
pass, and -- for a real value -- leak it into public history through the very
test written to prevent leaks.
"""

import os
import sys
import subprocess

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))

import scan_secrets  # noqa: E402

# Assembled, never written out. A reader can see what these are; the scanner
# matching this file's own source cannot.
_AT = chr(64)
_EMAIL = "someone" + _AT + "gmail" + ".com"
_APPDATA = "App" + "Data/Local/hermes"
_TELEGRAM = "telegram:" + "1" * 10          # shape only; never a real id
_ALLOWED = "you" + _AT + "example" + ".com"


def test_flags_an_email_address(tmp_path):
    f = tmp_path / "a.py"
    f.write_text(f'UA = "world-intel/1.0 {_EMAIL}"\n', encoding="utf-8")
    assert [x.pattern for x in scan_secrets.scan_paths([str(f)])] == ["email"]


def test_flags_a_windows_user_path(tmp_path):
    f = tmp_path / "b.py"
    f.write_text(f'ROOT = "~/{_APPDATA}"\n', encoding="utf-8")
    assert [x.pattern for x in scan_secrets.scan_paths([str(f)])] == ["appdata"]


def test_flags_a_telegram_target(tmp_path):
    f = tmp_path / "c.md"
    f.write_text(f'deliver="{_TELEGRAM}"\n', encoding="utf-8")
    assert [x.pattern for x in scan_secrets.scan_paths([str(f)])] == ["telegram"]


def test_example_domains_are_allowed(tmp_path):
    f = tmp_path / "example.yaml"
    f.write_text(f"contact_email: {_ALLOWED}\n", encoding="utf-8")
    assert scan_secrets.scan_paths([str(f)]) == []


def test_finds_the_address_inside_a_fixture_comment(tmp_path):
    # tests/fixtures/amd_10q.htm carries it in a hand-written provenance block,
    # not in the SEC response body.
    f = tmp_path / "fixture.htm"
    f.write_text(f"<!--\n  user-agent : world-intel/1.0 {_EMAIL}\n-->",
                 encoding="utf-8")
    assert len(scan_secrets.scan_paths([str(f)])) == 1


def test_clean_file_yields_nothing(tmp_path):
    f = tmp_path / "d.py"
    f.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    assert scan_secrets.scan_paths([str(f)]) == []


def test_this_test_file_does_not_trip_the_scanner():
    """The regression that matters. If this fails, someone inlined a literal
    and CI is now unpassable -- the scanner would report its own test fixtures
    as findings forever."""
    findings = scan_secrets.scan_paths([os.path.abspath(__file__)])
    assert findings == [], f"scanner self-trips: {findings}"


def test_the_join_form_of_the_user_path_is_caught(tmp_path):
    """os.path.join("~", "App""Data", "Local", ...) leaves no separator
    after the token. A pattern requiring one missed run.py:54, the deploy
    launcher, backfill_pool.py and test_kb.py -- four real leaks -- while
    still exiting clean."""
    token = "App" + "Data"
    src = tmp_path / "leak.py"
    src.write_text(
        "import os" + chr(10)
        + "P = os.path.join(" + chr(39) + "~" + chr(39) + ", "
        + chr(39) + token + chr(39) + ")" + chr(10),
        encoding="utf-8")
    findings = scan_secrets.scan_paths([str(src)])
    assert any(f.pattern == "appdata" for f in findings), findings


def test_the_scanner_source_does_not_trip_itself():
    """Same property for the tool. Its regexes and allow-list must not contain
    a matchable literal."""
    findings = scan_secrets.scan_paths([scan_secrets.__file__])
    assert findings == [], f"scanner source self-trips: {findings}"


def _mock_inventory(monkeypatch, inventory):
    monkeypatch.setattr(subprocess, "check_output", lambda args, **k:
                        b"" if args[1] == "rev-parse" else inventory)


def test_tracked_scan_uses_git_inventory_not_installed_packages(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "clean.py").write_text("pass\n", encoding="utf-8")
    vendor = tmp_path / ".venv"
    vendor.mkdir()
    (vendor / "vendor.py").write_text(_EMAIL, encoding="utf-8")
    _mock_inventory(monkeypatch, b"clean.py\0")
    assert scan_secrets.main(["scan", "--tracked"]) == 0
    _mock_inventory(monkeypatch, b".venv/vendor.py\0")
    assert scan_secrets.main(["scan", "--tracked"]) == 1


@pytest.mark.parametrize("inventory", [b"", b"missing.py\0", b"bad.py\0"])
def test_tracked_scan_fails_closed(tmp_path, monkeypatch, inventory):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.py").write_bytes(b"\xff")
    _mock_inventory(monkeypatch, inventory)
    assert scan_secrets.main(["scan", "--tracked"]) == 2


def test_tracked_scan_requires_working_git(monkeypatch):
    def unavailable(*a, **k):
        raise subprocess.CalledProcessError(128, "git")
    monkeypatch.setattr(subprocess, "check_output", unavailable)
    assert scan_secrets.main(["scan", "--tracked"]) == 2


def test_tracked_scan_rejects_ambiguous_arguments():
    assert scan_secrets.main(["scan", "--tracked", "clean.py"]) == 2


def test_tracked_scan_rejects_partial_subdirectory_inventory(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: b"tools/\n")
    assert scan_secrets.main(["scan", "--tracked"]) == 2
