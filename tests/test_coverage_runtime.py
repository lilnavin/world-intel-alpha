"""Run the candidate entry point offline through real selection, gates and HTML."""

import copy
from pathlib import Path
import sys

import pytest

from worldintel import run
from worldintel.core import coverage_policy


@pytest.mark.parametrize("has_content", [True, False])
def test_main_enforces_protected_event_in_published_html(monkeypatch, synthetic_config, has_content):
    today = run.jst_today()
    published = run.local_now().isoformat()
    title = "Fictional Meridian airliner pilot stabbed aboard aircraft during international flight"
    content = ("Authorities alleged that a passenger injured the pilot aboard the aircraft. "
               "Police said their investigation remained ongoing and the aircraft landed safely. ") * 12
    articles = [dict(title=title, summary="Police investigate alleged attempted hijacking aboard aircraft.",
                     url=f"https://{outlet}.example/report", source=outlet, tier=1,
                     desk=desk, published=published, content=content if has_content else "")
                for outlet, desk in (("alpha", "world"), ("beta", "us"))]
    bundle = {"news": articles, "markets": {}, "source_health": {}}
    monkeypatch.setattr(sys, "argv", ["run.py", "--force"])
    monkeypatch.setattr(run, "load_env", lambda _: {})
    monkeypatch.setattr(run.codex_engine, "smoke_test", lambda **kw: True)
    monkeypatch.setattr(run.fetch_sources, "fetch_all", lambda *args: copy.deepcopy(bundle))
    monkeypatch.setattr(run.enrich, "enrich_news", lambda *args, **kw: None)
    monkeypatch.setattr(run.earnings_report, "build_post_earnings", lambda *args: [])
    monkeypatch.setattr(run.earnings_report, "correct_forward_calendar", lambda *args: [])
    monkeypatch.setattr(run.earnings_report, "_load_cik_map", lambda *args: {})
    monkeypatch.setattr(run.filing_index, "load_window", lambda *args: {})
    monkeypatch.setattr(run.codex_engine, "run_two_pass", lambda *args, **kw:
                        {"world": {"briefing": {"brief": []}}, "finance": {"briefing": {}}, "errors": {}})
    repair_calls = []
    def invalid_repair(*args, **kw):
        repair_calls.append(kw["timeout"])
        return {"cards": [{"headline": "Invalid ungrounded repair"}]}
    monkeypatch.setattr(run.codex_engine, "run_schema", invalid_repair)
    captured = {}
    renderer = run.compose_html.render_html
    def render(analysis, selected_bundle, date, **kw):
        captured.update(analysis=copy.deepcopy(analysis), bundle=copy.deepcopy(selected_bundle))
        return renderer(analysis, selected_bundle, date, **kw)
    monkeypatch.setattr(run.compose_html, "render_html", render)
    delivered = []
    monkeypatch.setattr(run.delivery, "deliver", lambda adapter, path, log: delivered.append(path))

    assert run.main() == 0
    html_path = Path(synthetic_config.media_dir) / f"brief-{today}.html"
    rendered = html_path.read_text(encoding="utf-8")
    audit = captured["analysis"]["coverage_audit"]
    assert len(audit["events"]) == 1
    assert captured["bundle"]["ranking_profile_hash"] == coverage_policy.profile_hash(synthetic_config.ranking)
    assert delivered == [str(html_path)]
    if has_content:
        assert audit["events"][0]["status"] == "excerpt_fallback"
        assert "Source excerpt:" in rendered
        assert "Authorities alleged" in rendered
        assert len(repair_calls) == 1 and 0 < repair_calls[0] <= 60
    else:
        assert audit["events"][0]["status"] == "omitted"
        assert "Coverage gap:" in rendered and "Fictional Meridian" in rendered
        assert not repair_calls
