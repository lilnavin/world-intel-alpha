"""Configuration is lazy, observable at request boundaries, and fail-loud."""
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from worldintel import config
from worldintel.core import feeds
from worldintel.core import fetch_sources
from worldintel.core import earnings_report
from worldintel.core import kb


MODULES = ("feeds", "fetch_sources", "kb", "enrich", "sec_filing_text",
           "earnings_report", "filing_index", "ownership", "backfill_pool",
           "anomaly_panel", "sec_fundamentals")


def test_imports_never_load_configuration(monkeypatch):
    def fail():
        raise AssertionError("import read configuration")
    monkeypatch.setattr(config, "get", fail)
    for name in MODULES:
        importlib.reload(importlib.import_module("worldintel.core." + name))


def test_source_tables_follow_replaced_config(synthetic_config):
    raw = synthetic_config.raw
    raw["sources"].update({
        "feeds": [{"name": "custom", "desk": "world", "url": "https://example.org/rss", "tier": 3}],
        "markets": {"fx": [["custom", "A/B", "AB=X"]]},
        "macro": {"fred_series": [["CUSTOM", "Custom series"]]},
        "events": {"gdelt_themes": [["custom", "custom query"]]},
        "filings": {"enabled": True, "edgar_forms": ["10-K"]},
        "earnings": {"enabled": True, "window_days": 3, "watchlist": [["ZZZ", "Synthetic"]]},
    })
    config.set_for_test(config.Config(raw, synthetic_config.source_path))
    assert feeds.feed_registry()[0]["name"] == "custom"
    assert fetch_sources._empty_markets().keys() == {"fx"}
    assert fetch_sources.market_spec() == {"fx": [("custom", "A/B", "AB=X")]}
    assert fetch_sources.fred_series() == [("CUSTOM", "Custom series")]
    assert fetch_sources.gdelt_themes() == [("custom", "custom query")]
    assert fetch_sources.edgar_forms() == ["10-K"]
    assert fetch_sources.earnings_window_days() == 3
    assert earnings_report.bellwether_tickers() == {"ZZZ"}
    assert kb.default_root() == synthetic_config.data_root


def test_kb_clock_uses_configured_timezone(synthetic_config, monkeypatch):
    from datetime import datetime, timezone
    raw = synthetic_config.raw
    raw["runtime"]["timezone"] = "America/Toronto"
    cfg = config.Config(raw, synthetic_config.source_path)
    config.set_for_test(cfg)
    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2026, 1, 1, 2, tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(kb, "datetime", Clock)
    assert kb.local_now().tzinfo == cfg.timezone
    assert kb.local_today() == kb._today() == "2025-12-31"


@pytest.mark.parametrize("name", ["anomaly_panel", "sec_fundamentals", "ownership",
                                      "filing_index", "sec_filing_text", "earnings_report", "fetch_sources"])
def test_sec_identity_follows_config(name, synthetic_config):
    assert importlib.import_module("worldintel.core." + name)._sec_ua() == synthetic_config.sec_user_agent()


@pytest.mark.parametrize("name", ["feeds", "enrich"])
def test_browser_identity_follows_config(name, synthetic_config):
    assert importlib.import_module("worldintel.core." + name)._browser_ua() == synthetic_config.browser_user_agent()


@pytest.mark.parametrize("name,note", [("fetch_sources", "resilient-fetch"),
                                          ("earnings_report", "post-earnings"), ("backfill_pool", "backfill")])
def test_plain_identity_follows_config(name, note, synthetic_config):
    assert importlib.import_module("worldintel.core." + name)._plain_ua() == synthetic_config.plain_user_agent(note)


def test_config_error_escapes_request_fail_open_handlers(monkeypatch, tmp_path):
    def fail():
        raise config.ConfigError("invalid synthetic configuration")
    monkeypatch.setattr(config, "get", fail)
    class Session:
        def get(self, *args, **kwargs):
            pytest.fail("request ran before configuration was checked")
    with pytest.raises(config.ConfigError):
        fetch_sources.fetch_edgar({}, Session(), str(tmp_path), {})
    from worldintel.core import enrich
    with pytest.raises(config.ConfigError):
        enrich._fetch_html(Session(), "https://example.org/article", 1)
    with pytest.raises(config.ConfigError):
        feeds._resolve_gnews(Session(), "https://news.google.com/example")


def test_disabled_sec_sources_do_not_call_fetchers(monkeypatch, synthetic_config, tmp_path):
    raw = synthetic_config.raw
    raw["sources"]["filings"]["enabled"] = False
    raw["sources"]["earnings"]["enabled"] = False
    config.set_for_test(config.Config(raw, synthetic_config.source_path))
    monkeypatch.setattr(feeds, "fetch_feeds", lambda *a, **kw: [])
    for name in ("fetch_markets", "fetch_jgb", "fetch_fred", "fetch_gdelt", "fetch_gov"):
        monkeypatch.setattr(fetch_sources, name, lambda *a, **kw: {})
    def forbidden(*a, **kw):
        pytest.fail("disabled SEC source ran")
    monkeypatch.setattr(fetch_sources, "fetch_edgar", forbidden)
    monkeypatch.setattr(fetch_sources, "fetch_earnings", forbidden)
    out = fetch_sources.fetch_all({}, str(tmp_path), "2026-09-01")
    assert out["filings"] == out["earnings_calendar"] == []
    assert out["source_health"]["edgar"]["note"] == "disabled by configuration"
    assert out["source_health"]["earnings"]["note"] == "disabled by configuration"
