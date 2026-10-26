import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
from worldintel.core import coverage_gate as cg

URL = "https://example.org/report"
TEXT = "Authorities alleged the suspect attempted to interfere with the aircraft. " * 15


def inputs(content=TEXT, tier=1):
    return ({"briefing": {"brief": []}, "source_notes": []},
            {"news": [{"url": URL, "source": "Reuters", "tier": tier, "content": content}]},
            [{"id": "incident", "title": "Aircraft incident", "evidence": [], "selected_urls": [URL]}])


def card(url=URL, body=TEXT):
    return dict(headline="Aircraft incident", why_it_matters="Public safety", body=body,
                sources=[dict(outlet="Reuters", url=url)], tags=["security"], region="GLOBAL")


def test_exact_valid_card_across_country_sections():
    for section in ("finance", "japan", "canada"):
        result, bundle, events = inputs()
        result["briefing"][section] = {"brief": [card()]}
        assert cg.enforce(result, bundle, events)["events"][0]["status"] == "covered"


def test_short_mentions_do_not_count_and_exhausted_budget_skips_repair():
    result, bundle, events = inputs()
    result["briefing"]["brief"] = [card(body="Incident reported.")]
    def forbidden(*args):
        raise AssertionError("repair must not run")
    audit = cg.enforce(result, bundle, events, repair_fn=forbidden, deadline=10, clock=lambda: 10)
    assert not audit["repair_attempted"]
    assert audit["events"][0]["status"] == "excerpt_fallback"


def test_invalid_returned_url_falls_back():
    result, bundle, events = inputs()
    audit = cg.enforce(result, bundle, events, repair_fn=lambda p, t: [card(url=URL + "?invented")])
    assert audit["events"][0]["status"] == "excerpt_fallback"
    assert result["briefing"]["brief"][0]["sources"][0]["url"] == URL


def test_combined_repair_and_remaining_timeout():
    result, bundle, events = inputs()
    events.append(dict(events[0], id="second"))
    calls = []
    def repair(prompt, timeout):
        calls.append((prompt, timeout))
        return [card()]
    audit = cg.enforce(result, bundle, events, repair_fn=repair, deadline=35, clock=lambda: 10)
    assert len(calls) == 1 and calls[0][1] == 25
    assert all(event["status"] == "repaired" for event in audit["events"])


def test_no_content_and_low_tier_emit_visible_gap():
    for content, tier in (("", 1), (TEXT, 3), ("short report", 1)):
        result, bundle, events = inputs(content, tier)
        audit = cg.enforce(result, bundle, events)
        assert audit["events"][0]["status"] == "omitted"
        assert audit["events"][0]["reason"]
        assert "Coverage gap" in result["source_notes"][0]
        assert not result["briefing"]["brief"]


def test_html_cleanup_and_allegations_preserved():
    result, bundle, events = inputs("<script>invented terrorism</script><p>" + TEXT + "</p>")
    cg.enforce(result, bundle, events)
    story = result["briefing"]["brief"][0]
    assert "Source excerpt" in story["headline"]
    assert "Authorities alleged" in story["body"]
    assert "<" not in story["body"] and "terrorism" not in story["body"]
    assert 100 <= len(story["body"].split()) <= 200


def test_repair_timeout_is_capped_and_failure_uses_excerpt():
    result, bundle, events = inputs()
    calls = []
    def repair(prompt, timeout):
        calls.append(timeout)
        raise TimeoutError("model timed out")
    audit = cg.enforce(result, bundle, events, repair_fn=repair, deadline=500, clock=lambda: 1)
    assert calls == [60]
    assert audit["events"][0]["status"] == "excerpt_fallback"
    assert "TimeoutError" in audit["events"][0]["reason"]


def test_repair_rejects_short_body_and_wrong_schema():
    for candidate in (card(body="Only a headline."), dict(card(), invented_field="unsafe")):
        result, bundle, events = inputs()
        audit = cg.enforce(result, bundle, events, repair_fn=lambda p, t: [candidate])
        assert audit["events"][0]["status"] == "excerpt_fallback"


def test_long_single_word_content_does_not_relax_excerpt_word_floor():
    result, bundle, events = inputs("verylongword" * 100)
    audit = cg.enforce(result, bundle, events)
    assert audit["events"][0]["status"] == "omitted"
    assert "fewer than 100" in audit["events"][0]["reason"]


def test_protected_event_limit_bounds_added_cards():
    result, bundle, events = inputs()
    events.extend([dict(events[0], id="second"), dict(events[0], id="third")])
    audit = cg.enforce(result, bundle, events)
    assert len(audit["events"]) == 2
    assert len(result["briefing"]["brief"]) == 2


def test_country_list_cards_are_covered():
    for section in ("japan", "canada", "australia"):
        result, bundle, events = inputs()
        result["briefing"][section] = [card()]
        assert cg.enforce(result, bundle, events)["events"][0]["status"] == "covered"


def test_repair_rejects_extra_invented_source_and_wrong_outlet():
    for sources in ([dict(outlet="Reuters", url=URL), dict(outlet="Fake", url="https://fake.org")],
                    [dict(outlet="Invented publisher", url=URL)]):
        result, bundle, events = inputs()
        candidate = card()
        candidate["sources"] = sources
        assert cg.enforce(result, bundle, events, repair_fn=lambda p, t: [candidate])["events"][0]["status"] == "excerpt_fallback"

def test_excerpt_headline_uses_the_chosen_sources_title():
    result, bundle, events = inputs()
    bundle['news'][0]['title'] = 'Chosen source incident report'
    cg.enforce(result, bundle, events)
    story = result['briefing']['brief'][0]
    assert story['headline'] == 'Source excerpt: Chosen source incident report'
    assert 'Chosen source incident report' in story['why_it_matters']


def test_three_selected_events_share_one_repair_call():
    result = {"briefing": {"brief": []}}
    events = [{"id": str(n), "selected_urls": [f"https://example.org/{n}"], "max_events": 3} for n in range(3)]
    bundle = {"news": [{"url": e["selected_urls"][0], "source": "Example", "tier": 1, "content": TEXT} for e in events]}
    calls = []
    audit = cg.enforce(result, bundle, events, repair_fn=lambda p,t: calls.append(p) or [])
    assert len(calls) == 1
    assert len(audit["events"]) == 3
    assert all(e["status"] == "excerpt_fallback" for e in audit["events"])


def test_legacy_gate_default_and_explicit_zero():
    result,bundle,events = inputs()
    events *= 3
    assert len(cg.enforce(result,bundle,events)["events"]) == 2
    assert not cg.enforce({},bundle,events,max_events=0)["events"]
