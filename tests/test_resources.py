"""Resource packaging preserves reference bytes and usable file lifetimes."""
import json
from pathlib import Path

import pytest

from worldintel import data
from worldintel.core import codex_engine


@pytest.mark.parametrize("name", ["example.full.yaml", "example.world.yaml", "example.minimal.yaml"])
def test_packaged_profile_matches_editable_example(name):
    reference = Path(__file__).resolve().parents[1] / "config" / name
    assert data.read_bytes("profiles/" + name) == reference.read_bytes()


@pytest.mark.parametrize("name", ["finance.schema.json", "rewrite.schema.json"])
def test_schema_resource_path_is_usable_inside_context(name):
    with data.as_file("schema/" + name) as path:
        assert json.loads(path.read_bytes()) == json.loads(data.read_bytes("schema/" + name))


def test_prompt_loader_uses_resources_outside_checkout(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert codex_engine._load_template() == data.read_text("prompts/analysis.md")
    assert codex_engine._load_template(codex_engine.FINANCE_PROMPT_TEMPLATE) == data.read_text("prompts/finance.md")


@pytest.mark.parametrize("name", ["../secret", "/secret", "C:/secret", "..\\secret"])
def test_resource_names_cannot_escape_package(name):
    with pytest.raises(ValueError):
        data.read_bytes(name)
