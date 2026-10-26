"""Ranking is optional, validated before fetching, and travels with profiles."""
import copy
from pathlib import Path

import pytest

from worldintel import config

ROOT = Path(__file__).resolve().parents[1]


def raw_config():
    return {"version": 1, "runtime": {"timezone": "UTC", "data_root": "~/.world-intel"},
            "sources": {"filings": {"enabled": False}, "earnings": {"enabled": False}}}


def test_missing_ranking_resolves_packaged_defaults():
    cfg = config.Config(raw_config(), "test.yaml")
    assert cfg.ranking["version"] == 1
    assert cfg.ranking["topics"]
    assert cfg.ranking["major_events"]["max_events"] <= 2
    assert cfg.ranking["major_events"]["min_publishers"] >= 2
    assert "ranking" in config.explain(cfg)["defaulted"]


def test_topic_override_and_major_events_are_resolved_without_mutating_raw():
    raw = raw_config()
    raw["ranking"] = {"version": 1, "topics": [{"terms": ["earthquake"], "weight": 3.0}],
                      "major_events": {"enabled": False, "max_events": 1, "min_publishers": 3}}
    before = copy.deepcopy(raw)
    cfg = config.Config(raw, "test.yaml")
    assert cfg.ranking["topics"] == raw["ranking"]["topics"]
    assert cfg.ranking["major_events"] == raw["ranking"]["major_events"]
    assert raw == before
    assert not cfg.warnings


@pytest.mark.parametrize("ranking", [
    {"version": 2}, {"topics": [{"terms": ["war"], "weight": True}]},
    {"topics": [{"terms": [""], "weight": 1}]},
    {"major_events": {"max_events": 4}}, {"major_events": {"min_publishers": 1}},
    {"major_events": {"enabled": "yes"}}, {"unknown": 1}, None,
    {"topics": None}, {"major_events": None},
    {"topics": [{"terms": ["war"], "weight": float("inf")}]},
])
def test_invalid_ranking_is_a_configuration_error(ranking):
    raw = raw_config()
    raw["ranking"] = ranking
    with pytest.raises(config.ConfigError, match="ranking"):
        config.Config(raw, "test.yaml")


@pytest.mark.parametrize("name", ["minimal", "world", "full"])
def test_source_and_packaged_profiles_resolve_same_ranking(name):
    source = config.load(ROOT / "config" / f"example.{name}.yaml")
    packaged = config.load(ROOT / "worldintel/data/profiles" / f"example.{name}.yaml")
    assert source.ranking == packaged.ranking
