"""The human CLI boundary uses conventional arguments and restores process state."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from worldintel import cli, config, data


def _profile(tmp_path, name="minimal"):
    path = tmp_path / "profile.yaml"
    path.write_bytes(data.read_bytes(f"profiles/example.{name}.yaml"))
    return path


def test_argument_convention_and_help(capsys):
    assert cli.main([]) == 2
    assert cli.main(["--help"]) == 0
    assert set(cli.VERBS) == {"init", "validate", "demo", "doctor", "run"}
    assert cli.main(["not-a-verb"]) == 2


@pytest.mark.parametrize("name", ["minimal", "world", "full"])
def test_init_outside_checkout_validates_and_does_not_clobber(tmp_path, name):
    output = tmp_path / "worldintel.yaml"
    args = ["init", "--profile", name, "--out", str(output)]
    assert cli.main(args) == 0
    assert output.read_bytes() == data.read_bytes(f"profiles/example.{name}.yaml")
    assert cli.main(["validate", "--config", str(output), "--strict", "--explain"]) == 0
    before = output.read_bytes()
    assert cli.main(args) == 1
    assert output.read_bytes() == before


def test_init_rejects_git_tree(tmp_path):
    (tmp_path / ".git").mkdir()
    output = tmp_path / "worldintel.yaml"
    assert cli.main(["init", "--out", str(output)]) == 1
    assert not output.exists()


def test_validate_errors_warnings_and_explain(tmp_path, capsys):
    path = _profile(tmp_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\nunknown_key: true\n")
    assert cli.main(["validate", "--config", str(path)]) == 0
    assert cli.main(["validate", "--config", str(path), "--strict", "--explain"]) == 1
    assert "ignored" in capsys.readouterr().out
    path.write_text("version: 99\n", encoding="utf-8")
    assert cli.main(["validate", "--config", str(path)]) == 1
    assert "version" in capsys.readouterr().err


def test_demo_is_offline_and_ignores_global_configuration(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("demo attempted a configuration or process dependency")
    monkeypatch.setattr(config, "get", forbidden)
    monkeypatch.setattr(config, "load", forbidden)
    import subprocess
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setenv("WORLD_INTEL_CONFIG", str(tmp_path / "missing.yaml"))
    assert cli.main(["demo", "--out", str(tmp_path)]) == 0
    output = (tmp_path / "world-intel-demo.html").read_text(encoding="utf-8")
    for text in ("Harbor Island", "Example Components", "Signal", "Threads", "On the Radar", "Scoreboard", "EXMP", "8-K"):
        assert text in output
    assert len(output) > 10000
    assert cli.main(["demo", "--out", str(tmp_path)]) == 1


def test_doctor_missing_codex_fails_and_does_not_claim_auth(tmp_path, monkeypatch, capsys):
    path = _profile(tmp_path)
    monkeypatch.setattr(cli.importlib, "import_module", lambda name: SimpleNamespace(__version__="synthetic"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.main(["doctor", "--config", str(path)]) == 1
    out = capsys.readouterr().out
    assert "NOT FOUND" in out and "authentication: not checked" in out


def test_doctor_reports_all_failures(tmp_path, monkeypatch, capsys):
    def missing(name):
        raise ImportError(name)
    monkeypatch.setattr(cli.importlib, "import_module", missing)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "synthetic-codex")
    assert cli.main(["doctor", "--config", str(tmp_path / "missing.yaml")]) == 1
    out = capsys.readouterr().out
    assert all(name in out for name in cli._RUNTIME_DEPS)
    assert "configuration: unavailable" in out


@pytest.mark.parametrize("fails", [False, True])
def test_run_adapts_and_restores_state(tmp_path, monkeypatch, fails):
    from worldintel import run
    path = _profile(tmp_path)
    old_cache = config._CACHE
    old_argv = sys.argv
    monkeypatch.setenv("WORLD_INTEL_CONFIG", "previous-config")
    def fake_main():
        assert sys.argv == ["world-intel-run", "--dry", "--force", "--concurrent-passes"]
        assert os.environ["WORLD_INTEL_CONFIG"] == str(path.resolve())
        assert config.get().source_path == str(path.resolve())
        if fails:
            raise RuntimeError("synthetic failure")
        return 0
    monkeypatch.setattr(run, "main", fake_main)
    assert cli.main(["run", "--config", str(path), "--dry", "--force", "--concurrent-passes"]) == int(fails)
    assert sys.argv is old_argv and config._CACHE is old_cache
    assert os.environ["WORLD_INTEL_CONFIG"] == "previous-config"


def test_offline_run_fails_before_loading_config(capsys):
    assert cli.main(["run", "--offline"]) == 1
    assert "use world-intel demo" in capsys.readouterr().err


def test_telegram_alias_reaches_runtime_handoff(tmp_path, monkeypatch):
    from worldintel import run
    path = _profile(tmp_path)
    path.write_text(path.read_text(encoding="utf-8").replace("adapter: stdout", "adapter: telegram"), encoding="utf-8")
    reached = []
    def fake_main():
        reached.append(config.get().raw["delivery"]["adapter"])
        return 0
    monkeypatch.setattr(run, "main", fake_main)
    assert cli.main(["run", "--config", str(path), "--dry"]) == 0
    assert reached == ["telegram"]
    monkeypatch.setattr(cli.importlib, "import_module", lambda name: SimpleNamespace(__version__="synthetic"))
    monkeypatch.setattr(cli.shutil, "which", lambda name: "synthetic-codex")
    assert cli.main(["doctor", "--config", str(path)]) == 0
