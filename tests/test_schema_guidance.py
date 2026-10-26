import json
import os

import pytest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(SKILL_DIR, "worldintel", "data", "schema", "briefing.schema.json")

_TYPE_MAP = {
    "object": dict, "array": list, "string": str,
    "number": (int, float), "integer": int, "boolean": bool,
    "null": type(None),
}


def _writeup_schema():
    schema = json.load(open(SCHEMA_PATH, encoding="utf-8"))
    return (schema["properties"]["briefing"]["properties"]["post_earnings"]
            ["properties"]["writeups"]["items"])


def _validate(node, value):
    """Minimal JSON-schema-subset validator (type/enum/required/
    additionalProperties/properties/items) — just enough for this
    schema's shapes; raises AssertionError on any violation."""
    types = node.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        ok = any(
            value is None if t == "null" else
            (value is not None and isinstance(value, _TYPE_MAP[t]))
            for t in allowed
        )
        if not ok:
            raise AssertionError(f"type mismatch: {value!r} not in {allowed}")
    if value is None:
        return
    if "enum" in node and value not in node["enum"]:
        raise AssertionError(f"{value!r} not in enum {node['enum']}")
    if isinstance(value, dict) and "properties" in node:
        for key in node.get("required", []):
            if key not in value:
                raise AssertionError(f"missing required key {key!r}")
        if node.get("additionalProperties") is False:
            extra = set(value) - set(node["properties"])
            if extra:
                raise AssertionError(f"unexpected keys {extra}")
        for key, sub in node["properties"].items():
            if key in value:
                _validate(sub, value[key])
    if isinstance(value, list) and "items" in node:
        for item in value:
            _validate(node["items"], item)


BASE_WRITEUP = {
    "ticker": "WKC", "headline": "h", "company": "c", "guidance": "g",
    "read": "r", "read_through": "rt", "tags": ["x"],
}


def test_writeup_with_guidance_struct_validates():
    writeup = dict(BASE_WRITEUP,
                    guidance_struct={"metric": "eps_fy", "low": 3.2, "high": 3.4})
    _validate(_writeup_schema(), writeup)


def test_writeup_with_null_guidance_struct_validates():
    writeup = dict(BASE_WRITEUP, guidance_struct=None)
    _validate(_writeup_schema(), writeup)


def test_writeup_missing_guidance_struct_fails():
    with pytest.raises(AssertionError):
        _validate(_writeup_schema(), dict(BASE_WRITEUP))


def test_writeup_bad_enum_metric_fails():
    writeup = dict(BASE_WRITEUP,
                    guidance_struct={"metric": "ebitda", "low": 1, "high": 1})
    with pytest.raises(AssertionError):
        _validate(_writeup_schema(), writeup)


# --------------------------------------------------------------------------- #
# post_earnings.followups (Spec C D7, Task 9). Codex strict structured output
# rejects any property not listed in `required` (invalid_json_schema degraded a
# live run on 2026-07-12), so the new array is required AND nullable, exactly
# like guidance_struct.
# --------------------------------------------------------------------------- #
def _post_earnings_schema():
    schema = json.load(open(SCHEMA_PATH, encoding="utf-8"))
    return schema["properties"]["briefing"]["properties"]["post_earnings"]


BASE_POST_EARNINGS = {
    "writeups": [], "also_reported": [], "followups": None,
}


def test_followups_is_required_and_nullable():
    node = _post_earnings_schema()
    assert "followups" in node["required"]
    followups = node["properties"]["followups"]
    assert set(followups["type"]) == {"array", "null"}


def test_post_earnings_with_null_followups_validates():
    _validate(_post_earnings_schema(), dict(BASE_POST_EARNINGS))


def test_post_earnings_with_populated_followups_validates():
    payload = dict(BASE_POST_EARNINGS, followups=[
        {"ticker": "META", "headline": "The filing shows the capex bill",
         "body": "Operating cash flow covered the build."}])
    _validate(_post_earnings_schema(), payload)


def test_post_earnings_missing_followups_fails():
    payload = dict(BASE_POST_EARNINGS)
    payload.pop("followups")
    with pytest.raises(AssertionError):
        _validate(_post_earnings_schema(), payload)


def test_followup_entry_missing_body_fails():
    payload = dict(BASE_POST_EARNINGS, followups=[
        {"ticker": "META", "headline": "h"}])
    with pytest.raises(AssertionError):
        _validate(_post_earnings_schema(), payload)


def test_followup_entry_with_extra_key_fails():
    payload = dict(BASE_POST_EARNINGS, followups=[
        {"ticker": "META", "headline": "h", "body": "b", "eps_act": 6.18}])
    with pytest.raises(AssertionError):
        _validate(_post_earnings_schema(), payload)


# --------------------------------------------------------------------------- #
# finance.ownership_note (Spec E, Task 5). Codex strict structured output
# rejects any `finance` response missing a property listed in `required`
# ("additionalProperties": false), so the new field must be BOTH required AND
# listed in `properties` -- one without the other breaks every response.
# --------------------------------------------------------------------------- #
def _finance_schema():
    schema = json.load(open(SCHEMA_PATH, encoding="utf-8"))
    return schema["properties"]["briefing"]["properties"]["finance"]


BASE_FINANCE = {
    "bridge": "", "brief": [], "scoreboard_note": "",
    "filings_note": "", "earnings_note": "",
}


def test_ownership_note_is_gone_from_the_schema():
    """The ownership lane was removed 2026-08-30 (roadmap Phase 0). Codex
    strict structured output uses `"additionalProperties": false`, so a
    `required` entry the prompt no longer describes is not a harmless leftover
    — it forces the model to invent a note about a bundle key that never
    arrives. Property and requirement must go together."""
    node = _finance_schema()
    assert "ownership_note" not in node["required"]
    assert "ownership_note" not in node["properties"]


def test_finance_without_ownership_note_validates():
    _validate(_finance_schema(), dict(BASE_FINANCE))


def test_finance_with_a_stray_ownership_note_fails():
    """`additionalProperties: false` must reject the retired key rather than
    silently tolerate a model that still emits it."""
    payload = dict(BASE_FINANCE)
    payload["ownership_note"] = "Insiders keep buying the dip."
    with pytest.raises(AssertionError):
        _validate(_finance_schema(), payload)


PROMPT_PATH = os.path.join(SKILL_DIR, "worldintel", "data", "prompts", "analysis.md")


def test_ownership_is_gone_from_the_live_prompts():
    """A prompt paragraph that outlives its bundle key is worse than dead text:
    it tells the model to reason about data that no longer arrives, and with
    `additionalProperties: false` on the schema the response is then rejected
    outright. Code and prompt must be removed in the same change.

    Only `world.md` and `finance.md` are asserted: they are the two templates
    `codex_engine` actually loads. `analysis.md` is the retired single-pass
    prompt, kept frozen byte-for-byte under Global Constraint 10 (pinned by
    test_codex_engine.test_analysis_md_is_unmodified) as the reference the
    two-pass lift tests slice their blocks from. It reaches no production call
    path, so its ownership text is inert -- and unfreezing it is a deliberate
    decision, not a side effect of removing a lane."""
    for name in ("world.md", "finance.md"):
        path = os.path.join(SKILL_DIR, "worldintel", "data", "prompts", name)
        text = open(path, encoding="utf-8").read()
        assert "ownership" not in text.lower(), f"{name} still mentions ownership"
