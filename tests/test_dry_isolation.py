"""Execute the orchestrator with offline lanes and real filesystem persistence."""

from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from worldintel import run


TODAY = "2026-10-01"
HTML = "<html>offline rendered briefing</html>"


@pytest.fixture
def offline(monkeypatch, tmp_path, synthetic_config):
    root = tmp_path / "data"
    media = tmp_path / "media"
    raw = synthetic_config.raw
    raw["runtime"].update(data_root=str(root), media_dir=str(media), env_file=str(tmp_path / "absent.env"))
    raw["delivery"] = {"adapter": "hermes-cron"}
    run.wi_config.set_for_test(run.wi_config.Config(raw, str(tmp_path / "test.yaml")))
    monkeypatch.setattr(run.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(run, "jst_today", lambda: TODAY)
    monkeypatch.setattr(run, "load_env", lambda _: {})
    monkeypatch.setattr(run.codex_engine, "smoke_test", lambda **k: True)

    def fetch(config, raw_dir, today):
        Path(raw_dir, "fetch.json").write_text("fresh cache", encoding="utf-8")
        return {"news": [], "markets": {}, "source_health": {}}

    fetch_mock = Mock(side_effect=fetch)
    monkeypatch.setattr(run.fetch_sources, "fetch_all", fetch_mock)
    monkeypatch.setattr(run.enrich, "enrich_news", lambda *a, **k: None)
    def earnings(config, raw_dir, health, today, earnings_root):
        run.earnings_report.write_pipeline(earnings_root, {"fresh": {"status": "pending"}})
        return []

    monkeypatch.setattr(run.earnings_report, "build_post_earnings", earnings)
    monkeypatch.setattr(run.earnings_report, "correct_forward_calendar", lambda *a: [])
    monkeypatch.setattr(run.earnings_report, "_load_cik_map", lambda *a: {})
    monkeypatch.setattr(run.requests.Session, "request",
                        Mock(side_effect=AssertionError("network forbidden")))
    index_roots = []

    def load_window(index_root, *args):
        index_roots.append(Path(index_root))
        run.filing_index.write_index(index_root, {TODAY: {}})
        return {}

    monkeypatch.setattr(run.filing_index, "load_window", load_window)
    analysis = Mock(return_value={"world": {"briefing": {},
                        "kb_deltas": {"overview_replace": "new overview"}},
                                  "finance": {"briefing": {}}, "errors": {}})
    monkeypatch.setattr(run.codex_engine, "run_two_pass", analysis)
    render = Mock(return_value=HTML)
    monkeypatch.setattr(run.compose_html, "render_html", render)
    # Delivery is the MEDIA stdout handoff, captured by each test. No external
    # delivery transport is called by this version of the orchestrator.
    return root, media, fetch_mock, analysis, render, index_roots


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("existing", [False, True])
def test_dry_main_isolates_every_write(offline, tmp_path, monkeypatch, capsys, existing):
    root, media, fetch, analysis, render, index_roots = offline
    if existing:
        run.kb.ensure_kb(str(root))
        run.kb.write_state(str(root), {"last_run_date": TODAY})
        run.filing_index.write_index(str(root), {"2026-09-30": {}})
        run.earnings_report.write_pipeline(str(root), {"old": {"status": "covered"}})
        media.mkdir()
        (media / f"brief-{TODAY}.html").write_text("existing brief", encoding="utf-8")
    before = snapshot(tmp_path)
    dirs_before = {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_dir()}
    monkeypatch.setattr(sys, "argv", ["run.py", "--dry", "--force"])
    assert run.main() == 0
    assert fetch.call_count == analysis.call_count == render.call_count == 1
    assert "MEDIA:" not in capsys.readouterr().out
    assert snapshot(tmp_path) == before
    assert {str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_dir()} == dirs_before
    assert index_roots and index_roots[0] != root
    assert not index_roots[0].exists()  # scratch is removed even after success
    if not existing:
        assert not root.exists()
        assert not media.exists()


def test_normal_main_writes_and_hands_off_real_file(offline, monkeypatch, capsys):
    root, media, fetch, analysis, render, index_roots = offline
    monkeypatch.setattr(sys, "argv", ["run.py", "--force"])
    assert run.main() == 0
    assert fetch.call_count == analysis.call_count == render.call_count == 1
    path = media / f"brief-{TODAY}.html"
    assert path.read_text(encoding="utf-8") == HTML
    assert capsys.readouterr().out.strip() == f"MEDIA:{path}"
    assert run.kb.read_state(str(root))["last_run_date"] == TODAY
    assert (root / "wiki" / "daily" / f"{TODAY}.md").read_text(encoding="utf-8") == HTML
    assert (root / "raw" / TODAY / "fetch.json").exists()
    assert index_roots == [root]


def test_dry_populated_earnings_and_followups_do_not_persist(offline, tmp_path, monkeypatch):
    root, media, _, _, render, _ = offline
    run.kb.ensure_kb(str(root))
    before = snapshot(tmp_path)
    fundamentals = {"period_end": "2026-06-30", "py_end": "2025-06-30",
                    "accession": "abc", "primary_doc": "filing.htm", "form": "10-Q"}
    candidate = {"ticker": "TEST", "fundamentals": fundamentals}
    followup = {"key": "TEST:2026-06-30", "ticker": "TEST", "cik": "0000000001",
                "filed": TODAY}
    monkeypatch.setattr(run.earnings_report, "build_post_earnings", lambda *a: [candidate])
    monkeypatch.setattr(run.earnings_report, "_load_cik_map", lambda *a: {"TEST": "0000000001"})
    monkeypatch.setattr(run.reconcile, "match", lambda *a: [followup])
    monkeypatch.setattr(run.reconcile, "rank", lambda *a: [followup])
    monkeypatch.setattr(run.reconcile, "expired_unread", lambda *a: [])
    monkeypatch.setattr(run.reconcile, "select", lambda *a, **k: ([followup], []))
    monkeypatch.setattr(run.sec_fundamentals, "build_fundamentals", lambda *a: fundamentals)
    monkeypatch.setattr(run.earnings_report, "_has_resolved_metric", lambda *a: True)
    monkeypatch.setattr(run.sec_filing_text, "fetch_filing_text", lambda *a: "filing body")
    for name in ("slice_sections", "parse_segments", "find_disclosures"):
        monkeypatch.setattr(run.sec_filing_text, name, lambda *a: {})
    panel = Mock(return_value={"period_end": "2026-06-30"})
    monkeypatch.setattr(run.anomaly_panel, "build_panel", panel)
    # Call-count assertions catch swallowed persistence errors as well as writes.
    writers = []
    for module, name in ((run.kb, "record_company_quarter"), (run.earnings_report, "mark_covered"),
                         (run.post_earnings_pool, "record"), (run.post_earnings_pool, "mark_shown")):
        writer = Mock(wraps=getattr(module, name))
        monkeypatch.setattr(module, name, writer)
        writers.append(writer)
    monkeypatch.setattr(sys, "argv", ["run.py", "--dry", "--force"])
    assert run.main() == 0
    assert panel.call_count == 2
    bundle = render.call_args.args[1]
    assert bundle["post_earnings_candidates"][0]["panel"]
    assert bundle["post_earnings_followups"][0]["panel"]
    assert all(writer.call_count == 0 for writer in writers)
    assert snapshot(tmp_path) == before
    assert not media.exists()


def test_dry_scratch_removed_when_pipeline_raises(offline, tmp_path, monkeypatch):
    before = snapshot(tmp_path)
    scratch_paths = []
    def fail(config, raw_dir, today):
        scratch_paths.append(Path(raw_dir).parents[1])
        Path(raw_dir, "partial.json").write_text("partial")
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(run.fetch_sources, "fetch_all", fail)
    monkeypatch.setattr(sys, "argv", ["run.py", "--dry", "--force"])
    with pytest.raises(RuntimeError, match="synthetic failure"):
        run.main()
    assert scratch_paths and not scratch_paths[0].exists()
    assert snapshot(tmp_path) == before
