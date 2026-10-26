"""Tests for deterministic selection source-diversity guards."""

import os
import sys

# Make scripts/ importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from worldintel.core import source_diversity as sd  # noqa: E402


def _candidate(title, source, url, tier=1, content="full article"):
    return {
        "title": title,
        "source": source,
        "url": url,
        "tier": tier,
        "content": content,
    }


def _story(headline, outlet, url):
    return {
        "headline": headline,
        "why_it_matters": "stakes",
        "body": "body",
        "sources": [{"outlet": outlet, "url": url}],
        "tags": ["US"],
        "region": "US",
    }


def test_guardian_cap_removes_excess_across_world_and_finance():
    bundle = {"news": [
        _candidate("Reuters hard news", "reuters", "https://reuters.com/a"),
        _candidate("BBC hard news", "bbc", "https://bbc.com/b", tier=2),
        _candidate("Guardian 1", "guardian", "https://theguardian.com/1"),
        _candidate("Guardian 2", "guardian", "https://theguardian.com/2"),
        _candidate("Guardian 3", "guardian", "https://theguardian.com/3"),
    ]}
    result = {"briefing": {
        "brief": [
            _story("Guardian lead", "Guardian", "https://theguardian.com/1"),
            _story("Reuters lead", "Reuters", "https://reuters.com/a"),
            _story("Guardian second", "Guardian", "https://theguardian.com/2"),
        ],
        "finance": {
            "brief": [
                _story("Guardian finance", "Guardian", "https://theguardian.com/3"),
            ],
        },
    }}

    debug = sd.enforce_guardian_soft_cap(bundle, result)

    world = result["briefing"]["brief"]
    finance = result["briefing"]["finance"]["brief"]
    assert debug["guardian_cap_overridden"] is False
    assert debug["guardian_cap_removed"] == 1
    assert debug["guardian_picks_before"] == 3
    assert debug["guardian_picks_after"] == 2
    assert len(world) == 3
    assert finance == []


def test_guardian_cap_override_records_scarcity_reason():
    bundle = {"news": [
        _candidate("Reuters hard news", "reuters", "https://reuters.com/a"),
        # Not eligible: no content, so there is only one non-Guardian alternative.
        _candidate("BBC headline only", "bbc", "https://bbc.com/b", content=""),
        _candidate("Guardian 1", "guardian", "https://theguardian.com/1"),
        _candidate("Guardian 2", "guardian", "https://theguardian.com/2"),
        _candidate("Guardian 3", "guardian", "https://theguardian.com/3"),
    ]}
    result = {"briefing": {
        "brief": [
            _story("Guardian lead", "Guardian", "https://theguardian.com/1"),
            _story("Guardian second", "Guardian", "https://theguardian.com/2"),
            _story("Guardian third", "Guardian", "https://theguardian.com/3"),
        ],
        "finance": {"brief": []},
    }}

    debug = sd.enforce_guardian_soft_cap(bundle, result)

    assert debug["guardian_cap_overridden"] is True
    assert "fewer than 2 non-Guardian" in debug["guardian_cap_override_reason"]
    assert debug["guardian_picks_after"] == 3
    assert len(result["briefing"]["brief"]) == 3
    assert result["_debug"]["guardian_selection"] == debug


def test_guardian_candidate_dedupe_for_override_decision():
    bundle = {"news": [
        _candidate("Reuters hard news", "reuters", "https://reuters.com/a"),
        _candidate("Reuters duplicate", "reuters", "https://reuters.com/a"),
        _candidate("Guardian 1", "guardian", "https://theguardian.com/1"),
        _candidate("Guardian 2", "guardian", "https://theguardian.com/2"),
        _candidate("Guardian 3", "guardian", "https://theguardian.com/3"),
    ]}
    result = {"briefing": {
        "brief": [
            _story("Guardian lead", "Guardian", "https://theguardian.com/1"),
            _story("Guardian second", "Guardian", "https://theguardian.com/2"),
            _story("Guardian third", "Guardian", "https://theguardian.com/3"),
        ],
        "finance": {"brief": []},
    }}

    debug = sd.enforce_guardian_soft_cap(bundle, result)

    assert debug["eligible_non_guardian_candidates"] == 1
    assert debug["guardian_cap_overridden"] is True
