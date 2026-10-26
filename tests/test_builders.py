"""L0 exact bytes and genuinely new desk behavior, without live services."""
import copy
import importlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tools"))
from worldintel import config
from worldintel.core import schema_build
from worldintel.core import prompt_build
import check_l0


def _variant(cfg, desks):
    raw = copy.deepcopy(cfg.raw)
    raw["desks"] = desks
    return config.Config(raw, "variant.yaml")


def test_default_builds_are_exact_frozen_bytes(synthetic_config):
    for name, builder in (("world.md", prompt_build), ("world.schema.json", schema_build)):
        assert check_l0.compare(builder.build_bytes(synthetic_config), ROOT / "tests/l0_reference" / name) == []


def test_order_is_config_order_not_tab_order(synthetic_config):
    desks = copy.deepcopy(synthetic_config.raw["desks"])
    desks[2], desks[3] = desks[3], desks[2]
    cfg = _variant(synthetic_config, desks)
    schema = schema_build.build(cfg)["properties"]["briefing"]
    assert schema["required"][-2:] == ["canada", "japan"]
    assert list(schema["properties"])[-2:] == ["canada", "japan"]
    prompt = prompt_build.build(cfg)
    assert prompt.index("### Canada") < prompt.index("### Japan")


def test_zero_desks_and_new_desk(synthetic_config):
    cfg = _variant(synthetic_config, [{"id": "world", "kind": "global", "label": "World"}])
    base = schema_build.build(cfg)["properties"]["briefing"]
    assert set(base["properties"]) == {"brief", "signal", "threads", "on_radar"}
    assert base["additionalProperties"] is False
    prompt = prompt_build.build(cfg)
    assert "### Japan" not in prompt and "{{" not in prompt
    kenya = {"id": "kenya", "label": "Kenya", "kind": "national", "tab": {"colour": "#abc", "order": 2},
             "target_stories": [1, 5], "signature_patterns": [r"\bkenya"],
             "prompt_section": "### Kenya — briefing.kenya\r\nExact prose.\n\n"}
    cfg = _variant(synthetic_config, cfg.raw["desks"] + [kenya])
    schema = schema_build.build(cfg)["properties"]["briefing"]
    assert schema["required"][-1] == "kenya"
    assert schema["properties"]["kenya"]["maxItems"] == 5
    assert "region" not in schema["properties"]["kenya"]["items"]["properties"]
    assert kenya["prompt_section"] in prompt_build.build(cfg)
    assert "World, Kenya." in prompt_build.build(cfg)


def test_temp_schema_has_exact_bytes(synthetic_config):
    path = Path(schema_build.write_temp(synthetic_config))
    try:
        assert path.read_bytes() == schema_build.build_bytes(synthetic_config)
    finally:
        path.unlink()


def test_l0_catches_single_byte_and_missing_reference(tmp_path):
    path = ROOT / "tests/l0_reference/world.md"
    blob = path.read_bytes()
    assert check_l0.compare(b"X" + blob[1:], path)[0].find("offset 0") >= 0
    assert check_l0.compare(blob + b" ", path)
    assert check_l0.compare(blob.replace("–".encode(), b"-", 1), path)
    with pytest.raises(check_l0.L0Error):
        check_l0.compare(blob, tmp_path / "missing")


def test_l0_main_passes():
    assert check_l0.main([]) == 0


@pytest.mark.parametrize("mode", ["missing", "broken", "no_api", "empty", "drift"])
def test_l0_fails_closed(monkeypatch, mode):
    original = importlib.import_module
    def broken(name):
        if mode == "missing":
            raise ModuleNotFoundError(name=name)
        if mode == "broken":
            raise ModuleNotFoundError(name="inner_dependency")
        if mode == "no_api":
            return object()
        return original(name)
    if mode == "empty":
        monkeypatch.setattr(check_l0, "_REGISTRY", ())
    elif mode == "drift":
        monkeypatch.setattr(check_l0, "_REFERENCES", {"world.schema.json": "changed"})
    else:
        monkeypatch.setattr(check_l0.importlib, "import_module", broken)
    assert check_l0.main([]) == 1
