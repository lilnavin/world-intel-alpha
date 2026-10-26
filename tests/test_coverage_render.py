import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
from worldintel.core.compose_html import render_html
from worldintel.core.coverage_gate import enforce


def test_gate_gap_is_visible_and_render_does_not_mutate():
    result = {"briefing": {"brief": []}, "source_notes": []}
    events = [{"id": "event", "title": "Aircraft incident", "selected_urls": ["https://example.org/a"]}]
    result["coverage_audit"] = enforce(result, {"news": []}, events)
    before = copy.deepcopy(result)
    rendered = render_html(result, {}, "2026-10-03")
    assert "Coverage gap: Aircraft incident" in rendered
    assert "A substantive report from a reliable selected source was unavailable." in rendered
    assert rendered.index("Coverage gap:") < rendered.index('id="brief"')
    assert result == before


def test_covered_events_do_not_show_gap():
    result = {"coverage_audit": {"events": [{"title": "Aircraft incident", "status": "covered"}]}}
    assert "Coverage gap:" not in render_html(result, {}, "2026-10-03")


def test_notice_escapes_title_reason_and_is_bounded():
    event = {"title": '<script>alert("title")</script>', "status": "omitted", "reason": '<img src=x onerror="bad">'}
    result = {"coverage_audit": {"events": [event] * 8}}
    rendered = render_html(result, {}, "2026-10-03")
    assert "<script>" not in rendered and "<img src=x" not in rendered
    assert "&lt;script&gt;" in rendered and "&lt;img" in rendered
    assert rendered.count("Coverage gap:") == 2
