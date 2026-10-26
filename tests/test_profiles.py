"""Shipped profiles and additive private-profile migration stay standalone."""
import copy
from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tools"))
from worldintel import config
from worldintel.core import prompt_build
from worldintel.core import schema_build
import prepare_owner_desks


@pytest.mark.parametrize("name", ["minimal", "world", "full"])
def test_profiles_are_valid_buildable_and_use_registered_sources(name):
    cfg = config.load(ROOT / "config" / f"example.{name}.yaml")
    full = config.load(ROOT / "config/example.full.yaml")
    assert not cfg.warnings
    assert schema_build.build(cfg)["properties"]["briefing"]["additionalProperties"] is False
    assert "{{" not in prompt_build.build(cfg)
    assert all(feed in full.feeds for feed in cfg.feeds)
    assert all(row in full.market_spec[group] for group, rows in cfg.market_spec.items() for row in rows)
    assert all(feed["desk"] in cfg.desk_ids for feed in cfg.feeds)
    if name != "full":
        assert not cfg.filings_enabled and not cfg.earnings_enabled
        assert not cfg.contact_email
        assert str(cfg.timezone) == "UTC"
        assert cfg.local_desk_ids == ()
    if name == "minimal":
        assert cfg.desk_ids == ("world",)
        assert len(cfg.feeds) == 4 and cfg.market_spec == {}


def _input(tmp_path):
    raw = config.load(ROOT / "config/example.minimal.yaml").raw
    raw.pop("desks")
    raw["runtime"]["env_file"] = str(tmp_path / "separate" / ".env")
    path = tmp_path / "input.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path, raw


def test_prepare_is_additive_and_never_changes_input(tmp_path):
    source, raw = _input(tmp_path)
    before = source.read_bytes()
    output = tmp_path / "prepared.yaml"
    prepare_owner_desks.prepare(source, ROOT / "config/example.full.yaml", output)
    after = config.load(output).raw
    desks = after.pop("desks")
    assert after == raw
    assert desks == config.load(ROOT / "config/example.full.yaml").raw["desks"]
    assert source.read_bytes() == before


def test_prepare_refuses_overwrite(tmp_path):
    source, _ = _input(tmp_path)
    output = tmp_path / "exists.yaml"
    output.write_bytes(b"sentinel")
    with pytest.raises(FileExistsError):
        prepare_owner_desks.prepare(source, ROOT / "config/example.full.yaml", output)
    assert output.read_bytes() == b"sentinel"


def test_prepare_exclusive_create_survives_race(tmp_path, monkeypatch):
    source, _ = _input(tmp_path)
    output = tmp_path / "raced.yaml"
    original_open = prepare_owner_desks.os.open
    def racing_open(path, flags, mode):
        output.write_bytes(b"another writer")
        return original_open(path, flags, mode)
    monkeypatch.setattr(prepare_owner_desks.os, "open", racing_open)
    with pytest.raises(FileExistsError):
        prepare_owner_desks.prepare(source, ROOT / "config/example.full.yaml", output)
    assert output.read_bytes() == b"another writer"


def test_prepare_rejects_empty_desk_template_before_output(tmp_path):
    source, _ = _input(tmp_path)
    output = tmp_path / "prepared.yaml"
    with pytest.raises(ValueError, match="no desks"):
        prepare_owner_desks.prepare(source, source, output)
    assert not output.exists()


def test_prepare_rejects_existing_desks_and_invalid_input(tmp_path):
    source, raw = _input(tmp_path)
    output = tmp_path / "prepared.yaml"
    for mutation in ({"desks": []}, {"version": 9}):
        changed = copy.deepcopy(raw)
        changed.update(mutation)
        source.write_text(yaml.safe_dump(changed), encoding="utf-8")
        with pytest.raises((ValueError, config.ConfigError)):
            prepare_owner_desks.prepare(source, ROOT / "config/example.full.yaml", output)
        assert not output.exists()


def test_prepare_rejects_repository_output(tmp_path, monkeypatch):
    source, _ = _input(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    with pytest.raises(ValueError, match="outside"):
        prepare_owner_desks.prepare(source, ROOT / "config/example.full.yaml", repo / "prepared.yaml")
    monkeypatch.setattr(prepare_owner_desks, "_ROOT", tmp_path)
    with pytest.raises(ValueError, match="outside"):
        prepare_owner_desks.prepare(source, ROOT / "config/example.full.yaml", tmp_path / "prepared.yaml")


def test_cli_does_not_print_private_input_or_error_details(tmp_path, capsys):
    source, _ = _input(tmp_path)
    source.write_text("private-marker: [unclosed", encoding="utf-8")
    rc = prepare_owner_desks.main(["--input", str(source), "--desks-from", str(ROOT / "config/example.full.yaml"),
                                    "--output", str(tmp_path / "prepared.yaml")])
    captured = capsys.readouterr()
    assert rc == 1
    assert "private-marker" not in captured.out + captured.err
    assert str(source) not in captured.out + captured.err
    assert not (tmp_path / "prepared.yaml").exists()


def test_configured_region_tokens_match_case_insensitively(synthetic_config):
    from worldintel.core import metrics
    raw = copy.deepcopy(synthetic_config.raw)
    raw.setdefault("tuning", {})["region_tokens"] = ["Japan", " KENYA "]
    config.set_for_test(config.Config(raw, "synthetic.yaml"))
    assert metrics.region_tokens() == {"japan", "kenya"}
    assert metrics._story_region({"tags": ["JAPAN"]}) == "Japan"
    assert metrics._story_region({"tags": ["kenya"]}) == "Kenya"
