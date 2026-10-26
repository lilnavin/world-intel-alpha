"""Desk declarations fail at configuration load, before model or render work."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from worldintel import config


def _raw(cfg):
    return copy.deepcopy(cfg.raw)


def _load(raw):
    return config.Config(raw, "synthetic.yaml")


def test_views_preserve_declaration_order(synthetic_config):
    raw = _raw(synthetic_config)
    raw["desks"][2]["tab"]["order"] = 9
    cfg = _load(raw)
    assert cfg.desk_ids == ("world", "us", "japan", "canada", "finance")
    assert cfg.local_desk_ids == ("japan", "canada")
    assert cfg.national_desks[0]["_signature_re"].search("Tokyo budget")
    assert not cfg.national_desks[0]["_signature_re"].search("Ottawa budget")


@pytest.mark.parametrize("bad", [None, False, {}, "desks"])
def test_desks_require_exact_list(bad, synthetic_config):
    raw = _raw(synthetic_config)
    raw["desks"] = bad
    with pytest.raises(config.ConfigError, match="desks"):
        _load(raw)


@pytest.mark.parametrize("bad", ["", "Japan", "my desk", "my-desk", " japan ", 3, True])
def test_invalid_id(bad, synthetic_config):
    raw = _raw(synthetic_config)
    raw["desks"][2]["id"] = bad
    with pytest.raises(config.ConfigError):
        _load(raw)


@pytest.mark.parametrize("key,value", [
    ("kind", "regional"), ("kind", True), ("label", ""),
    ("target_stories", [0, 0]), ("target_stories", [-1, 3]),
    ("target_stories", [4, 3]), ("target_stories", [True, 3]),
    ("target_stories", [1, 3.0]), ("target_stories", [3]),
    ("prompt_section", "### Japan stories\n"),
    ("prompt_section", "briefing.japan_extra"),
    ("prompt_section", "briefing.japan and briefing.canada"),
    ("prompt_section", "briefing.japan and briefing.kenya"),
    ("prompt_section", "briefing.japan {{INJECT}}"),
    ("signature_patterns", ["["]), ("signature_patterns", [False]),
    ("signature_patterns", "japan"),
])
def test_invalid_national_contract(key, value, synthetic_config):
    raw = _raw(synthetic_config)
    raw["desks"][2][key] = value
    with pytest.raises(config.ConfigError):
        _load(raw)


@pytest.mark.parametrize("did", ["brief", "signal", "on_radar", "post_earnings", "asia", "global", "us"])
def test_reserved_national_ids(did, synthetic_config):
    raw = _raw(synthetic_config)
    raw["desks"] = [raw["desks"][2]]
    raw["desks"][0]["id"] = did
    with pytest.raises(config.ConfigError, match="reserved"):
        _load(raw)


@pytest.mark.parametrize("key,value", [("order", -1), ("order", True), ("order", 4),
    ("colour", "red;body{display:none}"), ("colour", "</style><script>"),
    ("colour", "url(https://example.org)"), ("colour", "red/*x*/"),
    ("colour", "#12345"), ("colour", "var(--anything)"), ("colour", None)])
def test_unsafe_tab_contract(key, value, synthetic_config):
    raw = _raw(synthetic_config)
    raw["desks"][2]["tab"][key] = value
    with pytest.raises(config.ConfigError):
        _load(raw)


def test_duplicate_id(synthetic_config):
    raw = _raw(synthetic_config)
    raw["desks"].append(copy.deepcopy(raw["desks"][0]))
    with pytest.raises(config.ConfigError, match="duplicate desk"):
        _load(raw)


def test_absent_and_zero_national_desks(synthetic_config):
    raw = _raw(synthetic_config)
    raw.pop("desks")
    assert _load(raw).desk_ids == ()
    raw["desks"] = [{"id": "world", "kind": "global", "label": "World"}]
    assert _load(raw).local_desk_ids == ()


def test_empty_signatures_never_match_and_prompt_is_verbatim(synthetic_config):
    raw = _raw(synthetic_config)
    raw["desks"][2]["signature_patterns"] = []
    raw["desks"][2]["prompt_section"] = "briefing.japan\r\n\n"
    desk = _load(raw).national_desks[0]
    assert desk["prompt_section"] == "briefing.japan\r\n\n"
    assert desk["_signature_re"].search("anything") is None


@pytest.mark.parametrize("did,kind", [("earth", "global"), ("markets", "finance"),
                                      ("world", "finance"), ("world", "national"),
                                      ("us", "national"), ("finance", "global")])
def test_core_pipeline_ids_cannot_be_renamed_or_retyped(did, kind, synthetic_config):
    raw = _raw(synthetic_config)
    raw["desks"] = [{"id": did, "kind": kind, "label": "Example"}]
    with pytest.raises(config.ConfigError):
        _load(raw)


def test_nonempty_desk_set_requires_world_fallback(synthetic_config):
    raw = _raw(synthetic_config)
    raw["desks"] = [desk for desk in raw["desks"] if desk["id"] != "world"]
    with pytest.raises(config.ConfigError, match="requires the global world"):
        _load(raw)
    raw["desks"] = []
    assert _load(raw).desk_ids == ()
