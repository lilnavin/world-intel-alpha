"""Synthetic privacy checks: sensitive examples are assembled, never literals."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import prepare_public as public  # noqa: E402


def _tree(tmp_path, content=b"print('safe')\r\n"):
    source = tmp_path / "source"
    (source / "scripts").mkdir(parents=True)
    (source / "scripts" / "run.py").write_bytes(content)
    return source


def test_export_is_allowlisted_deterministic_and_source_unchanged(tmp_path):
    source = _tree(tmp_path)
    for relative in (".env", "worldintel.yaml", "scripts/private.py",
                     "scripts/__pycache__/run.pyc", "data/private.json"):
        path = source / relative
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(b"excluded")
    before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    inventory = public.prepare(source, tmp_path / "report.json", tmp_path / "export")
    assert [f["path"] for f in inventory["files"]] == ["scripts/run.py"]
    assert (tmp_path / "export/scripts/run.py").read_bytes() == before[Path("scripts/run.py")]
    assert before == {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
    assert public.preflight(source)[0] == inventory
    assert not (tmp_path / "export/.git").exists()


def test_findings_block_export_and_report_is_redacted(tmp_path):
    secret = "private" + "@" + "mail.invalid"
    source = _tree(tmp_path, secret.encode())
    with pytest.raises(public.PreparationError, match="Privacy"):
        public.prepare(source, tmp_path / "report.json", tmp_path / "export")
    assert not (tmp_path / "export").exists()
    report = (tmp_path / "report.json").read_text()
    assert secret not in report
    assert json.loads(report)["status"] == "blocked"


def test_exact_approved_substitution_only_changes_copy(tmp_path):
    secret = "private" + "@" + "mail.invalid"
    original = ("contact=" + secret).encode()
    source = _tree(tmp_path, original)
    public.prepare(source, tmp_path / "report.json", tmp_path / "export",
                   {secret: "you@example.com"})
    assert (source / "scripts/run.py").read_bytes() == original
    assert (tmp_path / "export/scripts/run.py").read_text() == "contact=you@example.com"


def test_structural_path_substitution_is_refused(tmp_path):
    token = "App" + "Data"
    source = _tree(tmp_path, token.encode())
    with pytest.raises(public.PreparationError, match="exact detected"):
        public.preflight(source, {token: ".generic"})


@pytest.mark.parametrize("entry", ["../outside.py", "/absolute.py", "scripts\\run.py", "C:/secret.py"])
def test_traversal_is_refused(tmp_path, entry):
    with pytest.raises(public.PreparationError, match="Invalid"):
        public.preflight(_tree(tmp_path), allowlist=[entry])


def test_existing_destination_and_overlap_and_report_are_refused(tmp_path):
    source = _tree(tmp_path)
    existing = tmp_path / "existing"
    existing.mkdir()
    for destination in (existing, source / "nested", tmp_path):
        with pytest.raises(public.PreparationError):
            public.prepare(source, tmp_path / "report.json", destination)
    with pytest.raises(public.PreparationError, match="Report"):
        public.prepare(source, source / "report.json")
    with pytest.raises(public.PreparationError, match="Report"):
        public.prepare(source, tmp_path / "export/report.json", tmp_path / "export")


def test_symlink_source_file_is_refused(tmp_path):
    source = _tree(tmp_path)
    target = tmp_path / "outside.py"
    target.write_text("safe")
    path = source / "scripts/run.py"
    path.unlink()
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("Symlink privilege unavailable")
    with pytest.raises(public.PreparationError, match="Symlinks"):
        public.preflight(source)


def test_report_and_destination_symlink_ancestors_are_refused(tmp_path):
    source = _tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink privilege unavailable")
    with pytest.raises(public.PreparationError, match="Symlinks"):
        public.prepare(source, link / "report.json")
    with pytest.raises(public.PreparationError, match="Symlinks"):
        public.prepare(source, tmp_path / "report.json", link / "export")
