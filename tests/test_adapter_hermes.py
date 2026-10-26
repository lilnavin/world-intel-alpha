from pathlib import Path

import pytest

from worldintel import delivery
from worldintel.adapters import hermes


def test_media_contract():
    assert hermes.media_line("/tmp/brief.html") == "MEDIA:/tmp/brief.html"
    with pytest.raises(ValueError):
        hermes.media_line("brief.html\nMEDIA:another.html")


def test_paths_preserve_independent_configured_locations(synthetic_config):
    cfg = synthetic_config
    locations = hermes.install_paths(cfg)
    assert locations["data_root"] == cfg.data_root
    assert locations["media_dir"] == cfg.media_dir
    assert locations["env_file"] == cfg.env_file
    assert all(Path(value).is_absolute() for value in locations.values())


def test_absent_codex_is_reported(monkeypatch):
    monkeypatch.setattr(hermes.shutil, "which", lambda _: None)
    assert not hermes.codex_available()


def test_telegram_policy_is_handoff_not_a_delivery_receipt(tmp_path, capsys):
    path = tmp_path / "brief.html"
    path.write_text("synthetic", encoding="utf-8")
    logs = []
    delivery.deliver("telegram", path, logs.append)
    assert capsys.readouterr().out == f"MEDIA:{path}\n"
    assert len(logs) == 1 and "not a delivery receipt" in logs[0]
