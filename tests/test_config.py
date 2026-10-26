"""Offline configuration contracts using isolated module imports and temp files."""
import copy
import importlib.util
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def config():
    path = Path(__file__).resolve().parents[1] / "worldintel" / "config.py"
    spec = importlib.util.spec_from_file_location("world_intel_config_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def raw(tmp_path):
    return {"version": 1, "runtime": {"timezone": "UTC", "data_root": str(tmp_path / "state")},
            "sources": {"filings": {"enabled": False}, "earnings": {"enabled": False}}}


def load(config, tmp_path, raw):
    path = tmp_path / "worldintel.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return config.load(path)


def assign(raw, dotted, value):
    parts = dotted.split(".")
    node = raw
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def test_analysis_engine_and_effort_are_validated(config, raw, tmp_path):
    raw["analysis"] = {"engine": "invented"}
    with pytest.raises(config.ConfigError, match="analysis.engine"):
        load(config, tmp_path, raw)
    raw["analysis"] = {"reasoning_effort": " "}
    with pytest.raises(config.ConfigError, match="analysis.reasoning_effort"):
        load(config, tmp_path, raw)


def test_shared_shape_guards_reject_coercion(config):
    assert config._need_list([], "items") == []
    assert config._need_mapping({}, "settings") == {}
    assert config._need_int(0, "order", 0, 5) == 0
    for value in (True, "2", 2.5, -1, 6):
        with pytest.raises(config.ConfigError):
            config._need_int(value, "order", 0, 5)


@pytest.mark.parametrize("version", [None, True, False, "1", 1.0, 0, 2, [], {}])
def test_invalid_version(config, raw, tmp_path, version):
    raw["version"] = version
    with pytest.raises(config.ConfigError, match="version"):
        load(config, tmp_path, raw)


@pytest.mark.parametrize("key", ["identity", "runtime", "analysis", "sources", "tuning", "delivery",
                                     "sources.markets", "sources.macro", "sources.events",
                                     "sources.filings", "sources.earnings", "tuning.enrichment"])
@pytest.mark.parametrize("value", [None, False, 0, "", [], "nope"])
def test_sections_require_mappings(config, raw, tmp_path, key, value):
    assign(raw, key, value)
    with pytest.raises(config.ConfigError, match=key):
        load(config, tmp_path, raw)


@pytest.mark.parametrize("key", ["desks", "sources.feeds", "sources.macro.fred_series",
                                     "sources.events.gdelt_themes", "sources.filings.edgar_forms",
                                     "sources.earnings.watchlist", "sources.markets.indices"])
@pytest.mark.parametrize("value", [None, False, 0, "", {}, "10-K"])
def test_falsey_malformed_lists_are_not_defaults(config, raw, tmp_path, key, value):
    assign(raw, key, value)
    with pytest.raises(config.ConfigError, match=key):
        load(config, tmp_path, raw)


@pytest.mark.parametrize("key", ["sources.filings.enabled", "sources.earnings.enabled"])
@pytest.mark.parametrize("value", [1, "false", None])
def test_boolean_flags(config, raw, tmp_path, key, value):
    assign(raw, key, value)
    with pytest.raises(config.ConfigError, match=key):
        load(config, tmp_path, raw)


@pytest.mark.parametrize("key", ["runtime.data_root", "runtime.media_dir", "runtime.env_file", "runtime.timezone"])
@pytest.mark.parametrize("value", [None, False, 0, "", [], {}])
def test_invalid_runtime_values(config, raw, tmp_path, key, value):
    assign(raw, key, value)
    with pytest.raises(config.ConfigError, match=key):
        load(config, tmp_path, raw)


@pytest.mark.parametrize("path", [False, 0, [], {}, b"invalid", ""])
def test_invalid_load_paths(config, path):
    with pytest.raises(config.ConfigError):
        config.load(path)


def test_constructor_and_defaults_have_no_writes(config, raw, tmp_path):
    cfg = config.Config(raw, str(tmp_path / "not-created.yaml"))
    assert not Path(cfg.data_root).exists()
    assert cfg.env_file == str(Path(cfg.data_root) / ".env")
    assert cfg.media_dir == str(Path(cfg.data_root) / "media")
    assert cfg.contact_email == ""
    assert cfg.browser_user_agent() == "Mozilla/5.0 (compatible; world-intel/1.0)"
    raw["runtime"]["timezone"] = "bad"
    assert cfg.raw["runtime"]["timezone"] == "UTC"


def test_paths_timezone_and_ua(config, raw, tmp_path):
    raw["identity"] = {"contact_email": "tester@example.org", "user_agent_product": "test-product/9.9"}
    raw["sources"]["filings"]["enabled"] = True
    raw["runtime"].update(timezone="Asia/Tokyo", env_file=str(tmp_path / "keys" / ".env"))
    cfg = load(config, tmp_path, raw)
    assert cfg.env_file == str(tmp_path / "keys" / ".env")
    assert str(cfg.timezone) == "Asia/Tokyo"
    assert cfg.sec_user_agent() == "test-product/9.9 tester@example.org"
    assert cfg.browser_user_agent() == "Mozilla/5.0 (compatible; test-product/9.9; +tester@example.org)"
    assert cfg.plain_user_agent("resilient-fetch") == "test-product/9.9 (+resilient-fetch)"


@pytest.mark.parametrize("lane", ["filings", "earnings"])
@pytest.mark.parametrize("email", ["", "you@example.com", "your.email@example.com", "CHANGEME", "bad", "a@b", "a\nb@example.org"])
def test_sec_identity(config, raw, tmp_path, lane, email):
    raw["sources"][lane]["enabled"] = True
    raw["identity"] = {"contact_email": email}
    with pytest.raises(config.ConfigError, match="identity.contact_email"):
        load(config, tmp_path, raw)


@pytest.mark.parametrize("value", [True, 0, 91, "10", None])
def test_window_days(config, raw, tmp_path, value):
    raw["sources"]["earnings"]["window_days"] = value
    with pytest.raises(config.ConfigError, match="window_days"):
        load(config, tmp_path, raw)


def test_source_tables_and_feeds(config, raw, tmp_path):
    raw["sources"].update(feeds=[dict(name="test", desk="world", url="https://feed.example.org/rss", tier=1)],
                          markets={"indices": [["index", "IDX", "^IDX"]]},
                          macro={"fred_series": [["SERIES", "Series label"]]},
                          events={"gdelt_themes": [["THEME", "Theme label"]]})
    raw["sources"]["earnings"]["watchlist"] = [["TEST", "Test Company"]]
    raw["sources"]["filings"]["edgar_forms"] = ["10-K"]
    cfg = load(config, tmp_path, raw)
    assert cfg.market_spec["indices"] == [("index", "IDX", "^IDX")]
    assert cfg.fred_series == [("SERIES", "Series label")]
    assert cfg.gdelt_themes == [("THEME", "Theme label")]
    assert cfg.earnings_watchlist == [("TEST", "Test Company")]
    assert cfg.edgar_forms == ["10-K"]
    assert cfg.feeds[0]["tier"] == 1


@pytest.mark.parametrize("change", [{"tier": True}, {"tier": 4}, {"url": "http:///x"}, {"url": "file:///x"},
                                      {"url": 1}, {"name": ""}, {"desk": []}])
def test_invalid_feed(config, raw, tmp_path, change):
    feed = dict(name="test", desk="world", tier=1, url="https://example.org/rss")
    feed.update(change)
    raw["sources"]["feeds"] = [feed]
    with pytest.raises(config.ConfigError, match="sources.feeds"):
        load(config, tmp_path, raw)


def test_duplicate_feed_names(config, raw, tmp_path):
    raw["sources"]["feeds"] = [dict(name="dup", desk="world", url="https://example.org/rss")] * 2
    with pytest.raises(config.ConfigError, match="duplicate feed"):
        load(config, tmp_path, raw)


@pytest.mark.parametrize("row", ["abc", ["one"], ["one", "two", "three"], [True, "two"], ["", "two"]])
def test_invalid_rows(config, raw, tmp_path, row):
    raw["sources"]["macro"] = {"fred_series": [row]}
    with pytest.raises(config.ConfigError, match="fred_series"):
        load(config, tmp_path, raw)


def test_unknown_keys_and_explain(config, raw, tmp_path):
    raw["runttime"] = {"unknown": 1}
    raw["sources"]["filigns"] = {"enabled": False}
    raw["sources"]["markets"] = {"future": False}
    cfg = load(config, tmp_path, raw)
    assert any("runttime" in w and "runtime" in w for w in cfg.warnings)
    assert any("filigns" in w and "filings" in w for w in cfg.warnings)
    report = config.explain(cfg)
    assert set(report) == {"recognized", "defaulted", "deprecated", "ignored", "invalid"}
    assert "runtime.timezone" in report["recognized"]
    assert "runtime.media_dir" in report["defaulted"]
    assert "sources.markets.future" in report["ignored"]
    assert "sources.markets.future" not in cfg.market_spec


@pytest.mark.parametrize("text", ["version: 1\nversion: 1\n", "version: 1\nsources: {filings: {}, filings: {}}",
                                    "version: [", "- a\n- b", "", "1: value"])
def test_yaml_failures_name_file(config, tmp_path, text):
    path = tmp_path / "invalid.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(config.ConfigError, match="invalid.yaml"):
        config.load(path)


def test_env_resolution_and_cache(config, raw, tmp_path, monkeypatch):
    cfg = load(config, tmp_path, raw)
    monkeypatch.setenv("WORLD_INTEL_CONFIG", cfg.source_path)
    assert config.get().source_path == cfg.source_path
    config.set_for_test(cfg)
    assert config.get() is cfg
    config.set_for_test(None)
    monkeypatch.setenv("WORLD_INTEL_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(config.ConfigError, match="missing.yaml"):
        config.get()
    assert config.load(cfg.source_path).source_path == cfg.source_path


@pytest.mark.parametrize("key,value", [("freshness_hours", True), ("freshness_hours", 100000),
                                         ("per_desk_slots", 0), ("per_outlet_cap", "3")])
def test_tuning_ranges(config, raw, tmp_path, key, value):
    raw["tuning"] = {key: value}
    with pytest.raises(config.ConfigError, match=key):
        load(config, tmp_path, raw)


def test_future_desks_are_preserved(config, raw, tmp_path):
    raw["desks"] = [{"id": "world", "kind": "global", "label": "World",
                     "future_field": {"opaque": True}}]
    cfg = load(config, tmp_path, raw)
    assert cfg.raw["desks"] == raw["desks"]
    assert any("desks[0].future_field" in warning for warning in cfg.warnings)


def test_unknown_timezone(config, raw, tmp_path):
    raw["runtime"]["timezone"] = "Mars/Olympus"
    with pytest.raises(config.ConfigError, match="runtime.timezone"):
        load(config, tmp_path, raw)
