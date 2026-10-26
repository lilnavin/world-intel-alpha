"""Runtime coverage of configuration entry and both SEC lane switches."""

import ast
import datetime
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from test_dry_isolation import offline  # noqa: F401 (shared offline lane fixture)
from worldintel import run


class ForbiddenLane(BaseException):
    pass


def install(monkeypatch, filings, earnings):
    raw = run.wi_config.get().raw
    raw["sources"]["filings"]["enabled"] = filings
    raw["sources"]["earnings"]["enabled"] = earnings
    run.wi_config.set_for_test(run.wi_config.Config(raw, run.wi_config.get().source_path))
    monkeypatch.setattr(sys, "argv", ["run.py", "--force"])


def test_config_failure_stops_before_any_work(monkeypatch, capsys):
    monkeypatch.setattr(run.wi_config, "get", Mock(side_effect=run.wi_config.ConfigError("bad zone")))
    blocked = Mock(side_effect=ForbiddenLane("work happened before config validation"))
    monkeypatch.setattr(run.kb, "ensure_kb", blocked)
    monkeypatch.setattr(run.codex_engine, "smoke_test", blocked)
    monkeypatch.setattr(run.tempfile, "TemporaryDirectory", blocked)
    monkeypatch.setattr(sys, "argv", ["run.py", "--dry"])
    assert run.main() == 0
    assert "bad zone" in capsys.readouterr().out
    blocked.assert_not_called()


def test_unexpected_config_bug_is_not_swallowed(monkeypatch):
    monkeypatch.setattr(run.wi_config, "get", Mock(side_effect=RuntimeError("bug")))
    with pytest.raises(RuntimeError, match="bug"):
        run.main()


def test_unsupported_delivery_stops_before_smoke(offline, monkeypatch, capsys):
    cfg = run.wi_config.get()
    cfg.raw["delivery"]["adapter"] = "unknown-adapter"
    def invalid_config():
        return run.wi_config.Config(cfg.raw, cfg.source_path)
    monkeypatch.setattr(run.wi_config, "get", invalid_config)
    smoke = Mock(side_effect=ForbiddenLane())
    monkeypatch.setattr(run.codex_engine, "smoke_test", smoke)
    assert run.main() == 0
    assert "delivery.adapter" in capsys.readouterr().out
    smoke.assert_not_called()


def test_paths_and_zone_resolve_at_call_time(synthetic_config):
    raw = synthetic_config.raw
    raw["runtime"]["timezone"] = "America/Toronto"
    cfg = run.wi_config.Config(raw, synthetic_config.source_path)
    run.wi_config.set_for_test(cfg)
    assert run.paths() == (cfg.data_root, cfg.media_dir, cfg.env_file)
    assert run.capsule_store() == str(Path(cfg.data_root) / "capsules")
    assert run.local_now().utcoffset() != datetime.timedelta(hours=9)
    assert run.jst_today() == run.local_now().date().isoformat()


def test_both_disabled_block_every_actual_sec_site(offline, monkeypatch):
    install(monkeypatch, False, False)
    modules = {"earnings_report", "filing_index", "post_earnings_pool", "reconcile",
               "sec_fundamentals", "sec_filing_text", "anomaly_panel"}
    source = Path(run.__file__).read_text(encoding="utf-8")
    names = {(n.func.value.id, n.func.attr) for n in ast.walk(ast.parse(source))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id in modules}
    blocked = Mock(side_effect=ForbiddenLane("disabled SEC site executed"))
    for module, name in names:
        monkeypatch.setattr(getattr(run, module), name, blocked)
    assert run.main() == 0
    blocked.assert_not_called()
    offline[4].assert_called_once()


@pytest.mark.parametrize("earnings,filings", [(True, False), (False, True), (True, True)])
def test_each_enabled_lane_executes_complete_stage(offline, monkeypatch, earnings, filings):
    install(monkeypatch, filings, earnings)
    fundamentals = {"period_end": "2026-06-30", "py_end": "2025-06-30",
                    "accession": "abc", "primary_doc": "filing.htm", "form": "10-Q"}
    candidate = {"ticker": "TEST", "fundamentals": fundamentals}
    match = {"key": "TEST:2026-06-30", "ticker": "TEST", "cik": "0000000001",
             "filed": "2026-10-01"}
    mocks = {}

    def patch(module, name, value, enabled):
        mock = Mock(return_value=value) if enabled else Mock(side_effect=ForbiddenLane(name))
        monkeypatch.setattr(module, name, mock)
        mocks[name] = mock
        return mock

    patch(run.earnings_report, "build_post_earnings", [candidate], earnings)
    patch(run.earnings_report, "correct_forward_calendar", [], earnings)
    patch(run.earnings_report, "mark_covered", None, earnings)
    patch(run.post_earnings_pool, "record", None, earnings)
    patch(run.filing_index, "load_window", {}, filings)
    patch(run.post_earnings_pool, "read_pool", {}, filings)
    patch(run.post_earnings_pool, "mark_shown", None, filings)
    patch(run.reconcile, "match", [match], filings)
    patch(run.reconcile, "rank", [match], filings)
    patch(run.reconcile, "expired_unread", [], filings)
    patch(run.reconcile, "select", ([match], []), filings)
    patch(run.sec_fundamentals, "build_fundamentals", fundamentals, filings)
    patch(run.earnings_report, "_has_resolved_metric", True, filings)
    patch(run.sec_filing_text, "fetch_filing_text", "filing body", filings)
    patch(run.sec_filing_text, "slice_sections", {}, filings)
    patch(run.sec_filing_text, "parse_segments", [], filings)
    patch(run.sec_filing_text, "find_disclosures", [], filings)
    cik = patch(run.earnings_report, "_load_cik_map", {"TEST": "0000000001"}, True)
    panel = patch(run.anomaly_panel, "build_panel", {"period_end": "2026-06-30"}, True)
    assert run.main() == 0
    assert mocks["build_post_earnings"].call_count == int(earnings)
    assert mocks["mark_covered"].call_count == int(earnings)
    assert mocks["record"].call_count == int(earnings)
    assert mocks["load_window"].call_count == int(filings)
    assert mocks["fetch_filing_text"].call_count == int(filings)
    assert mocks["mark_shown"].call_count == int(filings)
    assert cik.call_count == panel.call_count == int(earnings) + int(filings)
    offline[4].assert_called_once()
