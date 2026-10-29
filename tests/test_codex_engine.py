"""Tests for Module C: codex_engine.

Mocked tests MUST pass everywhere. Live tests (real codex CLI) are skipped when
codex is not on PATH (or when offline / the model call fails).
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

# Make scripts/ importable when run from anywhere.
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from worldintel.core import codex_engine  # noqa: E402
from worldintel.core.codex_engine import CodexError, analyze, smoke_test  # noqa: E402


HAS_CODEX = shutil.which("codex") is not None

STUB_SCHEMA = {
    "type": "object",
    "required": ["ok"],
    "additionalProperties": False,
    "properties": {"ok": {"type": "boolean"}},
}


def _out_path_from_args(args):
    """Extract the value following '-o' in a codex arg list."""
    return args[args.index("-o") + 1]


# --------------------------------------------------------------------------- #
# Mocked analyze() tests (must pass without codex / network)
# --------------------------------------------------------------------------- #


def test_analyze_parses_canned_outfile(monkeypatch, tmp_path):
    """analyze() writes the prompt, invokes codex (mocked), and returns the dict
    parsed from the -o out file."""
    canned = {"ok": True, "headline": "hello"}

    def fake_run(args, **kwargs):
        out_file = _out_path_from_args(args)
        with open(out_file, "w", encoding="utf-8") as fh:
            json.dump(canned, fh)
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    # Make the test independent of codex being installed.
    monkeypatch.setattr(codex_engine.shutil, "which", lambda _name: "codex.CMD")
    monkeypatch.setattr(codex_engine, "_execute", fake_run)

    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(STUB_SCHEMA), encoding="utf-8")

    result = analyze(
        {"date": "2026-06-28", "news": []},
        {"overview": "ctx"},
        str(schema),
        reasoning_effort="high",
        timeout=30,
    )
    assert result == canned


def test_analyze_nonzero_exit_raises(monkeypatch, tmp_path):
    """A non-zero codex exit raises CodexError (and includes stderr)."""

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 2, stdout=b"", stderr=b"boom: model unavailable"
        )

    monkeypatch.setattr(codex_engine.shutil, "which", lambda _name: "codex.CMD")
    monkeypatch.setattr(codex_engine, "_execute", fake_run)

    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(STUB_SCHEMA), encoding="utf-8")

    with pytest.raises(CodexError) as exc:
        analyze({"x": 1}, {"y": 2}, str(schema))
    assert "exited 2" in str(exc.value)


def test_analyze_unparseable_output_raises(monkeypatch, tmp_path):
    """Non-JSON in the out file raises CodexError."""

    def fake_run(args, **kwargs):
        out_file = _out_path_from_args(args)
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write("not json at all")
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(codex_engine.shutil, "which", lambda _name: "codex.CMD")
    monkeypatch.setattr(codex_engine, "_execute", fake_run)

    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(STUB_SCHEMA), encoding="utf-8")

    with pytest.raises(CodexError) as exc:
        analyze({"x": 1}, {"y": 2}, str(schema))
    assert "not valid JSON" in str(exc.value)


def test_analyze_timeout_raises(monkeypatch, tmp_path):
    """A subprocess timeout surfaces as CodexError."""

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(codex_engine.shutil, "which", lambda _name: "codex.CMD")
    monkeypatch.setattr(codex_engine, "_execute", fake_run)

    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(STUB_SCHEMA), encoding="utf-8")

    with pytest.raises(CodexError) as exc:
        analyze({"x": 1}, {"y": 2}, str(schema), timeout=1)
    assert "timed out" in str(exc.value)


def test_build_prompt_substitutes_placeholders():
    """The stub template uses {bundle}/{context}; both are replaced with JSON."""
    prompt = codex_engine._build_prompt({"a": 1}, {"b": 2})
    assert '{"a": 1}' in prompt
    assert '{"b": 2}' in prompt
    assert "{bundle}" not in prompt
    assert "{context}" not in prompt


def test_missing_codex_raises_in_run(monkeypatch):
    """When codex is absent, the internal runner raises CodexError."""
    monkeypatch.setattr(codex_engine.shutil, "which", lambda _name: None)
    with pytest.raises(CodexError):
        codex_engine._run_codex(
            "hi", "out.txt", schema_path=None, reasoning_effort="low", timeout=5
        )


def test_smoke_test_returns_false_without_codex(monkeypatch):
    """smoke_test never raises; missing codex -> False."""
    monkeypatch.setattr(codex_engine.shutil, "which", lambda _name: None)
    assert smoke_test(timeout=5) is False


# --------------------------------------------------------------------------- #
# Live tests (real codex CLI) — skipped when codex is absent / offline
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not HAS_CODEX, reason="codex CLI not on PATH")
def test_smoke_test_live():
    """Live: smoke_test should return True against a reachable codex install."""
    if smoke_test(timeout=90) is not True:
        pytest.skip("codex unavailable/offline")


@pytest.mark.skipif(not HAS_CODEX, reason="codex CLI not on PATH")
def test_analyze_live_end_to_end(tmp_path):
    """Live: tiny bundle + stub schema -> schema-valid JSON dict.

    Skipped if codex is offline / the model call fails (raises CodexError)."""
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(STUB_SCHEMA), encoding="utf-8")
    try:
        result = analyze(
            {"date": "2026-06-28", "note": "return ok=true"},
            {"overview": "smoke"},
            str(schema),
            reasoning_effort="low",
            timeout=120,
        )
    except CodexError as exc:
        pytest.skip(f"codex unavailable/offline: {exc}")
    assert isinstance(result, dict)
    assert set(result.keys()) == {"ok"}
    assert isinstance(result["ok"], bool)


def test_analysis_prompt_style_contract():
    text = codex_engine._load_template()
    # placeholders must survive the edit
    assert "{bundle}" in text and "{context}" in text
    # style contract landed
    assert "Writing style" in text
    assert "inverted pyramid" in text.lower()
    # hard ban on pipeline vocabulary in output
    assert "never mention" in text.lower() or "never reference" in text.lower()
    # eligibility rule: narrative bodies only for enriched stories
    assert "must carry `content`" in text
    # selection rule: digests must not displace consequential hard news
    assert "news roundups, liveblogs" in text
    assert "concrete hard-news candidate" in text
    assert "before final slot allocation" in text.lower()


# --------------------------------------------------------------------------- #
# Task 9 -- the prompt/schema split.
#
# `prompts/world.md` is `analysis.md` with the finance and post-earnings
# sections removed; `prompts/finance.md` carries the finance desk,
# post-earnings. The style contract, hard bans, exemplar body,
# grounding rule, tier gate, novelty gate, gov cap and Guardian cap are lifted
# BYTE-FOR-BYTE, not paraphrased -- these are the rules that months of measured
# failures produced, and a "cleaned up" restatement of one is how the register
# silently drifts between the two tabs. Every block asserted below is sliced
# out of `analysis.md` at test time, so a paraphrase fails here.
# --------------------------------------------------------------------------- #

_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "worldintel/data/schema"
_PROMPT_DIR = Path(__file__).resolve().parent.parent / "worldintel/data/prompts"

# Global Constraint 10: analysis.md stays live and byte-for-byte unmodified
# until the two passes are proven. This is its digest at the split.
_ANALYSIS_SHA256 = "5e44483262dac11241639dee7d92f15a8ef958337d2caff815fe5ebfe8c9774f"


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def _analysis_md() -> str:
    return _read(_PROMPT_DIR / "analysis.md")


def _world_md() -> str:
    return _read(_PROMPT_DIR / "world.md")


def _finance_md() -> str:
    return _read(_PROMPT_DIR / "finance.md")


def _block(text: str, start: str, end: str) -> str:
    """The substring of `text` from `start` up to (not including) `end`."""
    i = text.index(start)
    j = text.index(end, i)
    assert j > i
    return text[i:j]


def _lifted_blocks() -> dict:
    """The blocks the plan names as verbatim lifts, sliced out of analysis.md."""
    a = _analysis_md()
    return {
        # style + hard bans + exemplar body, in one contiguous run
        "style": _block(a, "## Writing style", "## What to produce"),
        "grounding": _block(a, "- **Eligibility: a Brief story must carry `content`.**",
                            "- Lead with significant NEWS events."),
        "gov_cap": _block(a, "- **Federal Register / `gov` items are BURIED candidates",
                          "- **Source quality gate:**"),
        "tier_gate": _block(a, "- **Source quality gate:**",
                            "- **Guardian source-diversity soft cap:**"),
        "guardian_cap": _block(a, "- **Guardian source-diversity soft cap:**",
                               "- After dedupe and before final slot allocation:"),
        "novelty_gate": _block(a, "- **Novelty gate (avoid re-leading stale stories):**",
                               "- Rank by real-world impact"),
        "signal_rules": _block(a, "**Rules for the Signal section",
                               "### Threads (memory)"),
    }


def test_analysis_md_is_unmodified():
    """Global Constraint 10, pinned. `analysis.md` is the live single-pass
    prompt and stays byte-for-byte unmodified until the two passes are proven;
    Task 9 DERIVES the two new prompts from it by copying and never edits it."""
    import hashlib
    digest = hashlib.sha256((_PROMPT_DIR / "analysis.md").read_bytes()).hexdigest()
    assert digest == _ANALYSIS_SHA256, (
        "analysis.md was modified -- Global Constraint 10 forbids it until the "
        "two passes are proven")


def test_both_new_prompts_exist_and_keep_the_placeholders():
    for text in (_world_md(), _finance_md()):
        assert "{bundle}" in text and "{context}" in text


def test_world_prompt_lifts_the_named_blocks_byte_for_byte():
    """The whole point of the split rule: these blocks are COPIED, not
    rewritten. Each is sliced out of analysis.md above, so a paraphrase --
    however slight -- fails."""
    world = _world_md()
    for name, block in _lifted_blocks().items():
        assert block in world, f"{name} block is not lifted byte-for-byte into world.md"


def test_finance_prompt_lifts_the_shared_selection_blocks_byte_for_byte():
    """finance.brief carries the SAME shape, depth, grounding, tier and novelty
    gates as The Brief (analysis.md's own words), and the wire-service register
    must not diverge between the two tabs. The gov cap and the Signal rules are
    world-only and are deliberately not asserted here."""
    finance = _finance_md()
    blocks = _lifted_blocks()
    for name in ("style", "grounding", "tier_gate", "guardian_cap", "novelty_gate"):
        assert blocks[name] in finance, (
            f"{name} block is not lifted byte-for-byte into finance.md")


def test_world_prompt_drops_the_finance_and_post_earnings_sections():
    world = _world_md()
    assert "### Finance " not in world
    assert "### Post-Earnings " not in world
    # ...and keeps the sections the World News tab renders
    assert "### The Brief " in world
    assert "### Signal " in world
    assert "### Threads (memory) " in world
    assert "### On the Radar " in world


# --------------------------------------------------------------------------- #
# The Japan desk. It rides the EXISTING world pass rather than a third Codex
# call: the run already sits at ~451s against a 600s cron ceiling, and a third
# pass would spend the whole margin on a section that shares the world pass's
# inputs anyway.
# --------------------------------------------------------------------------- #
def test_world_prompt_carries_the_japan_section():
    world = _world_md()
    assert "### Japan " in world
    assert "`briefing.japan`" in world


def test_japan_prompt_repeats_the_brief_gates_rather_than_relaxing_them():
    """The Japan desk is a THIRD tab, not a lower bar. Padding a 100-200 word
    body off a bare headline is the measured failure mode this product already
    paid for once; the section that would most tempt it (a quiet domestic day)
    is exactly the one that must be allowed to return nothing."""
    japan = _block(_world_md(), "### Japan ", "### Signal ")
    assert "content" in japan            # the grounding gate
    assert "tier" in japan               # the source-quality gate
    assert "single-mention" in japan.lower()
    assert "[]" in japan                 # an empty day is a correct day


def test_finance_prompt_does_not_grow_a_japan_desk():
    """One pass owns the section, or two passes both write it and the last
    writer wins at random -- the same trap `overview_replace` documents."""
    assert "### Japan " not in _finance_md()


def test_finance_prompt_carries_finance_and_post_earnings():
    finance = _finance_md()
    assert "### Finance " in finance
    assert "### Post-Earnings " in finance
    # ...and none of the World News sections
    assert "### Signal " not in finance
    assert "### On the Radar " not in finance


def test_finance_prompt_receives_the_world_passs_headlines_for_scoreboard_read():
    """`scoreboard_read` CITES Brief stories by name under the single-mention
    rule, and the Brief is selected by the OTHER pass. The finance prompt must
    name the field that carries them AND the section that cites them, or the
    rule has nothing to bind to."""
    finance = _finance_md()
    assert "world_headlines" in finance
    assert "scoreboard_read" in finance
    assert "single-mention rule" in finance


def test_finance_invocation_receives_factual_premise_and_attribution_contract(monkeypatch):
    """Catch lost finance guidance at the callback boundary, not model entailment."""
    calls = []
    def callback(prompt, schema_path, **kwargs):
        calls.append((prompt, schema_path, kwargs))
        return {"briefing": {}}
    monkeypatch.setattr(codex_engine, "run_schema", callback)
    bundle = {"news": [{"desk": "finance", "title": "Larch Works plans notes",
                        "url": "https://example.test/larch", "content": "Fictional reporting."}]}
    context = {"overview": "Fictional context sentinel"}
    codex_engine.analyze_finance(bundle, context, "finance.schema.json",
                                 world_headlines=["Fictional world sentinel"], timeout=37)
    assert len(calls) == 1
    prompt, schema, kwargs = calls[0]
    assert schema == "finance.schema.json" and kwargs["timeout"] == 37
    assert "{bundle}" not in prompt and "{context}" not in prompt
    assert json.dumps(context, ensure_ascii=False) in prompt
    material = json.loads(prompt.split("## TODAY (BUNDLE)\n", 1)[1])
    assert material["news"] == bundle["news"]
    assert material["world_headlines"] == ["Fictional world sentinel"]
    assert "world_source_budget" in material
    blocks = _lifted_blocks()
    for name in ("style", "grounding", "tier_gate", "guardian_cap", "novelty_gate"):
        assert blocks[name] in prompt
    for guidance in ("factual premises", "use of borrowing proceeds", "causal links",
                     "speaker attribution", "comparative rankings", "Conditional analysis",
                     "observable watch conditions", "Omit unsupported padding"):
        assert guidance in prompt


# --------------------------------------------------------------------------- #
# Task 9 -- schemas. Global Constraint 3: every property is required-AND-
# nullable. Strict `--output-schema` rejects any property absent from
# `required`; this already caused a live `invalid_json_schema` degradation.
# --------------------------------------------------------------------------- #


def _load_schema(name: str) -> dict:
    return json.loads(_read(_SCHEMA_DIR / name))


def _required_violations(node, path="$"):
    """Every `properties` key that is missing from its sibling `required`."""
    bad = []
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            required = set(node.get("required") or [])
            for key in props:
                if key not in required:
                    bad.append(f"{path}.{key}")
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                for k, v in value.items():
                    bad += _required_violations(v, f"{path}.{k}")
            elif isinstance(value, (dict, list)):
                bad += _required_violations(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            bad += _required_violations(item, f"{path}[{i}]")
    return bad


@pytest.mark.parametrize("name", ["world.schema.json", "finance.schema.json"])
def test_new_schemas_load_and_are_json_schema_objects(name):
    schema = _load_schema(name)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"briefing", "kb_deltas", "source_notes"}


@pytest.mark.parametrize("name", ["world.schema.json", "finance.schema.json"])
def test_every_property_appears_in_its_required_array(name):
    """Global Constraint 3. A property absent from `required` is rejected at
    runtime by strict --output-schema, which degrades the whole pass."""
    violations = _required_violations(_load_schema(name))
    assert violations == [], f"{name}: properties missing from `required`: {violations}"


def test_the_shipped_briefing_schema_also_satisfies_the_required_rule():
    """The rule is checked against the schema already in production too, so the
    helper above is proven to find nothing where nothing is wrong."""
    assert _required_violations(_load_schema("briefing.schema.json")) == []


def test_the_two_schemas_partition_the_briefing():
    """Neither pass may emit the other's sections, and together they must
    reconstruct exactly the briefing shape compose_html already consumes."""
    world = _load_schema("world.schema.json")["properties"]["briefing"]
    finance = _load_schema("finance.schema.json")["properties"]["briefing"]
    shipped = _load_schema("briefing.schema.json")["properties"]["briefing"]

    world_keys = set(world["required"])
    finance_keys = set(finance["required"])
    assert world_keys == {"brief", "signal", "threads", "on_radar", "japan",
                          "canada"}
    assert finance_keys == {"finance", "post_earnings"}
    assert world_keys & finance_keys == set()
    # Country desks are split-pass additions, so the two halves reconstruct the
    # shipped briefing PLUS them. briefing.schema.json is the single-pass prompt's schema
    # and stays frozen alongside analysis.md (Global Constraint 10).
    assert world_keys | finance_keys == set(shipped["required"]) | {"japan", "canada"}


def test_the_canada_section_reuses_the_country_story_shape():
    props = (_load_schema("world.schema.json")["properties"]["briefing"]
             ["properties"])
    assert props["canada"]["items"] == props["japan"]["items"]
    assert props["canada"]["maxItems"] == 3


def test_the_world_half_reuses_the_shipped_field_shapes_exactly():
    """The split is a partition, not a redesign. The World News sections are
    untouched by this spec, so they must be byte-identical to the shipped
    definitions the renderer already consumes."""
    shipped = _load_schema("briefing.schema.json")["properties"]["briefing"]["properties"]
    world = _load_schema("world.schema.json")["properties"]["briefing"]["properties"]
    for key in ("brief", "signal", "threads", "on_radar"):
        assert world[key] == shipped[key], f"world.{key} drifted from the shipped shape"


def test_the_japan_section_reuses_the_brief_story_shape_minus_region():
    """A Japan story IS a Brief story -- same fields, same renderer card. The
    one field it drops is `region`: every item in the section is Japan, so
    asking the model for it would be asking it to restate the section name."""
    shipped = _load_schema("briefing.schema.json")["properties"]["briefing"]["properties"]
    japan = (_load_schema("world.schema.json")["properties"]["briefing"]
             ["properties"]["japan"])
    assert japan["type"] == "array"
    item = japan["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {"headline", "why_it_matters", "body",
                                     "sources", "tags"}
    brief_item = shipped["brief"]["items"]
    for field in ("headline", "why_it_matters", "body", "sources", "tags"):
        assert item["properties"][field] == brief_item["properties"][field], (
            f"japan.{field} drifted from the Brief's shape")
    assert "region" not in item["properties"]


def test_the_brief_region_enum_gains_no_japan_value():
    """The enum is consumed downstream (the renderer's colour buckets, the
    metrics region mix). Japan stories carry no `region` at all, so widening
    the enum would buy nothing and break every consumer that switches on it."""
    for name in ("world.schema.json", "briefing.schema.json"):
        enum = (_load_schema(name)["properties"]["briefing"]["properties"]
                ["brief"]["items"]["properties"]["region"]["enum"])
        assert enum == ["US", "AMERICAS", "ASIA", "MENA", "EUROPE", "GLOBAL"]


# Fields the shipped schema still carries but the live split schema has
# deliberately retired. `bridge` went on 2026-08-16: it, `scoreboard_note` and
# `scoreboard_read.contradiction` stated the same observation three times over,
# and the world->markets linkage it carried moved into `scoreboard_read`, which
# now cites published stories.
_RETIRED_FINANCE_FIELDS = frozenset({"bridge"})


def test_the_finance_half_keeps_every_shipped_field():
    """The finance half is EXTENDED by the rubric task (scoreboard_read,
    segment_read, panel_read, quotes), so it is checked for additive change
    only: no shipped field may be dropped or fall out of `required`, and
    anything added still answers to the required-AND-nullable rule above.

    A field may still be RETIRED, and when it is, this is where that is
    recorded. briefing.schema.json is the single-pass prompt's schema and stays
    frozen alongside analysis.md (Global Constraint 10), so it cannot move when
    the live split schema drops a field -- the two would then disagree forever
    with nothing to say why. `_RETIRED_FINANCE_FIELDS` above is that statement:
    an exemption has to be written down and justified, so a retirement is a
    decision someone made rather than a test quietly loosened."""
    shipped = _load_schema("briefing.schema.json")["properties"]["briefing"]["properties"]
    finance = _load_schema("finance.schema.json")["properties"]["briefing"]["properties"]
    for section in ("finance", "post_earnings"):
        for field in shipped[section]["properties"]:
            if field in _RETIRED_FINANCE_FIELDS:
                continue
            assert field in finance[section]["properties"], (
                f"finance.{section}.{field} was dropped from the split schema")
            assert field in finance[section]["required"], (
                f"finance.{section}.{field} fell out of `required`")


def test_guidance_struct_keeps_the_required_and_nullable_pattern():
    """`guidance_struct` is THE pattern Global Constraint 3 names. It must stay
    nullable at the type level and carry every property in `required`."""
    writeups = (_load_schema("finance.schema.json")["properties"]["briefing"]
                ["properties"]["post_earnings"]["properties"]["writeups"])
    gs = writeups["items"]["properties"]["guidance_struct"]
    assert gs["type"] == ["object", "null"]
    assert set(gs["required"]) == set(gs["properties"])
    assert gs["properties"]["low"]["type"] == ["number", "null"]


def test_the_world_pass_owns_the_overview_and_finance_does_not():
    """`overview_replace` is a single string; two passes both writing it would
    make the last writer win at random. The world pass owns the state-of-the-
    world overview and the expectations ledger; the finance pass writes only
    entity dossiers and threads, which `apply_deltas` merges additively."""
    world = _load_schema("world.schema.json")["properties"]["kb_deltas"]
    finance = _load_schema("finance.schema.json")["properties"]["kb_deltas"]
    assert set(world["required"]) == {"entities_upsert", "threads_upsert",
                                      "overview_replace", "expectations_resolved"}
    assert set(finance["required"]) == {"entities_upsert", "threads_upsert"}


# --------------------------------------------------------------------------- #
# Task 10 -- two-pass orchestration.
#
# Two passes, World and Finance, each with its OWN prompt and its OWN schema.
# `run_two_pass` never raises: one pass failing must still ship the other tab,
# because a dead finance pass costing the reader The Brief as well would be a
# strictly worse outcome than today's single pass.
# --------------------------------------------------------------------------- #

WORLD_RESULT = {
    "briefing": {"brief": [{"headline": "Tankers struck in Hormuz"},
                           {"headline": "Diet passes stimulus"}],
                 "signal": {"top": {"story": "", "reason": "", "url": ""},
                            "buried": [], "noise_line": ""},
                 "threads": [], "on_radar": []},
    "kb_deltas": {"overview_replace": "world state"},
    "source_notes": [],
}
FINANCE_RESULT = {
    "briefing": {"finance": {"brief": [], "scoreboard_note": "",
                             "filings_note": "", "earnings_note": "",
                             "ownership_note": ""},
                 "post_earnings": {"writeups": [], "also_reported": [],
                                   "followups": None}},
    "kb_deltas": {"entities_upsert": []},
    "source_notes": [],
}


def _install_fake_codex(monkeypatch, handler):
    """Patch subprocess.run with `handler(schema_path, prompt_text) ->
    (returncode, out_text|None)`. The prompt is read back off the stdin file
    handle, which is the only place it exists -- `_run_codex` feeds it from a
    file and passes NO prompt argument (see the module docstring)."""

    def fake_run(args, **kwargs):
        schema = (args[args.index("--output-schema") + 1]
                  if "--output-schema" in args else None)
        prompt = kwargs["stdin"].read().decode("utf-8")
        rc, text = handler(schema, prompt)
        if text is not None:
            with open(_out_path_from_args(args), "w", encoding="utf-8") as fh:
                fh.write(text)
        return subprocess.CompletedProcess(args, rc, stdout=b"", stderr=b"contention")

    monkeypatch.setattr(codex_engine.shutil, "which", lambda _n: "codex.CMD")
    monkeypatch.setattr(codex_engine, "_execute", fake_run)


def _schemas(tmp_path):
    world = tmp_path / "world.json"
    finance = tmp_path / "finance.json"
    for p in (world, finance):
        p.write_text(json.dumps(STUB_SCHEMA), encoding="utf-8")
    return str(world), str(finance)


def _is_world(schema_path: str) -> bool:
    return os.path.basename(schema_path).startswith("world")


def test_run_two_pass_gives_each_pass_its_own_prompt_and_schema(monkeypatch, tmp_path):
    """The whole point of the split. If both passes ran the same prompt the
    rubric would dilute the world-affairs selection rules exactly as it does
    today, and the split would have bought nothing."""
    seen = {}

    def handler(schema, prompt):
        key = "world" if _is_world(schema) else "finance"
        seen[key] = prompt
        return 0, json.dumps(WORLD_RESULT if key == "world" else FINANCE_RESULT)

    _install_fake_codex(monkeypatch, handler)
    ws, fs = _schemas(tmp_path)
    out = codex_engine.run_two_pass({"news": []}, {}, ws, fs)

    assert set(seen) == {"world", "finance"}
    assert "(World News pass)" in seen["world"]
    assert "(Finance pass)" in seen["finance"]
    assert "### Post-Earnings" not in seen["world"]
    assert "### Signal" not in seen["finance"]
    assert out["world"] == WORLD_RESULT
    assert out["finance"] == FINANCE_RESULT
    assert out["errors"] == {"world": None, "finance": None}


def test_sequential_is_the_default_and_feeds_finance_the_world_headlines(
        monkeypatch, tmp_path):
    """`scoreboard_read` cites stories the OTHER pass selected, under the
    single-mention rule. Sequencing world -> finance is what makes that list
    exist; a citation of a story The Brief never ran fails the renderer's
    grounding check and the operator item is dropped, with no post-hoc repair."""
    order, seen = [], {}

    def handler(schema, prompt):
        key = "world" if _is_world(schema) else "finance"
        order.append(key)
        seen[key] = prompt
        return 0, json.dumps(WORLD_RESULT if key == "world" else FINANCE_RESULT)

    _install_fake_codex(monkeypatch, handler)
    ws, fs = _schemas(tmp_path)
    out = codex_engine.run_two_pass({"news": []}, {}, ws, fs)

    assert order == ["world", "finance"], "world must run first so finance can cite it"
    assert out["mode"] == "sequential"
    assert "Tankers struck in Hormuz" in seen["finance"]
    assert "world_headlines" in seen["finance"]


def test_headlines_helper_degrades_to_empty_on_garbage():
    assert codex_engine._headlines(WORLD_RESULT) == [
        "Tankers struck in Hormuz", "Diet passes stimulus"]
    for junk in (None, {}, {"briefing": None}, {"briefing": {"brief": "nope"}},
                 {"briefing": {"brief": [None, {"headline": ""}]}}):
        assert codex_engine._headlines(junk) == []


def test_legacy_concurrent_option_runs_sequentially(monkeypatch, tmp_path):
    """A retained legacy flag cannot opt out of sequential reliability."""
    import threading
    lock = threading.Lock()
    state = {"live": 0, "peak": 0}

    def handler(schema, prompt):
        with lock:
            state["live"] += 1
            state["peak"] = max(state["peak"], state["live"])
        time.sleep(0.15)
        with lock:
            state["live"] -= 1
        return 0, json.dumps(WORLD_RESULT if _is_world(schema) else FINANCE_RESULT)

    _install_fake_codex(monkeypatch, handler)
    ws, fs = _schemas(tmp_path)
    out = codex_engine.run_two_pass({"news": []}, {}, ws, fs, concurrent=True)

    assert state["peak"] == 1, "the reliability path must remain sequential"
    assert out["mode"] == "sequential"
    assert out["world"] and out["finance"]


def test_failed_pass_is_not_retried_and_legacy_option_is_logged(monkeypatch, tmp_path):
    """Legacy concurrency requests are logged, but failures are not retried."""
    calls = []
    logged = []

    def handler(schema, prompt):
        key = "world" if _is_world(schema) else "finance"
        calls.append(key)
        if key == "finance" and calls.count("finance") == 1:
            return 1, None          # contention on the concurrent attempt
        return 0, json.dumps(WORLD_RESULT if key == "world" else FINANCE_RESULT)

    _install_fake_codex(monkeypatch, handler)
    ws, fs = _schemas(tmp_path)
    out = codex_engine.run_two_pass({"news": []}, {}, ws, fs, concurrent=True,
                                    log=logged.append)

    assert calls == ["world", "finance"], "neither pass may be retried"
    assert out["mode"] == "sequential"
    assert out["finance"] is None
    assert "exited 1" in out["errors"]["finance"]
    assert any("sequential" in m for m in logged), logged


def test_legacy_concurrent_option_still_feeds_finance_the_world_headlines(
        monkeypatch, tmp_path):
    """Finance gets world headlines even when an old launcher requests concurrency."""
    seen = {}
    calls = []

    def handler(schema, prompt):
        key = "world" if _is_world(schema) else "finance"
        calls.append(key)
        seen[key] = prompt
        if key == "finance" and calls.count("finance") == 1:
            return 1, None
        return 0, json.dumps(WORLD_RESULT if key == "world" else FINANCE_RESULT)

    _install_fake_codex(monkeypatch, handler)
    ws, fs = _schemas(tmp_path)
    codex_engine.run_two_pass({"news": []}, {}, ws, fs, concurrent=True)
    assert "Tankers struck in Hormuz" in seen["finance"]


def test_one_pass_failing_still_ships_the_other(monkeypatch, tmp_path):
    """The requirement that makes the two-pass split safe to ship."""
    def handler(schema, prompt):
        if _is_world(schema):
            return 1, None
        return 0, json.dumps(FINANCE_RESULT)

    _install_fake_codex(monkeypatch, handler)
    ws, fs = _schemas(tmp_path)
    out = codex_engine.run_two_pass({"news": []}, {}, ws, fs, log=lambda _m: None)

    assert out["world"] is None
    assert out["errors"]["world"] and "exited 1" in out["errors"]["world"]
    assert out["finance"] == FINANCE_RESULT
    assert out["errors"]["finance"] is None


def test_run_two_pass_never_raises_when_both_passes_die(monkeypatch, tmp_path):
    """House rule 1: nothing raises out of a module."""
    _install_fake_codex(monkeypatch, lambda schema, prompt: (1, None))
    ws, fs = _schemas(tmp_path)
    out = codex_engine.run_two_pass({"news": []}, {}, ws, fs)
    assert out["world"] is None and out["finance"] is None
    assert out["errors"]["world"] and out["errors"]["finance"]


def test_run_two_pass_never_raises_on_an_unserialisable_bundle(monkeypatch, tmp_path):
    """A malformed bundle must degrade, not abort the run -- the failure the
    `except CodexError` in the old single-pass call site would NOT have caught."""
    _install_fake_codex(monkeypatch, lambda schema, prompt: (0, json.dumps(WORLD_RESULT)))
    ws, fs = _schemas(tmp_path)
    out = codex_engine.run_two_pass({"news": object()}, {}, ws, fs)
    assert out["world"] is None and out["finance"] is None


# --------------------------------------------------------------------------- #
# Task 10 -- the input split (spec 6.5's Input column).
# --------------------------------------------------------------------------- #

_FULL_BUNDLE = {
    "date": "2026-08-12",
    "news": [{"title": "war", "desk": "world"}, {"title": "cpi", "desk": "finance"}],
    "gov": [{"title": "notice"}],
    "gdelt": {"themes": []},
    "filings": [{"company": "X"}],
    "markets": {"indices": {}},
    "macro": {"cpi": 1},
    "earnings_calendar": [{"ticker": "AMD"}],
    "post_earnings_candidates": [{"ticker": "AMD"}],
    "post_earnings_followups": [{"ticker": "MSFT"}],
    "source_health": {},
}


def test_world_bundle_drops_the_finance_only_material():
    out = codex_engine._world_bundle(_FULL_BUNDLE)
    for key in ("markets", "macro", "earnings_calendar", "post_earnings_candidates",
                "post_earnings_followups"):
        assert key not in out, f"{key} is finance material and must not reach the world pass"
    # Signal hunts filings and gov; both stay, and so does the whole news pool.
    for key in ("news", "gov", "gdelt", "filings", "source_health", "date"):
        assert key in out
    assert len(out["news"]) == 2


def test_finance_bundle_drops_the_world_only_material_and_keeps_the_finance_desk():
    out = codex_engine._finance_bundle(_FULL_BUNDLE, ["Tankers struck in Hormuz"])
    assert "gov" not in out and "gdelt" not in out
    assert [n["title"] for n in out["news"]] == ["cpi"]
    for key in ("markets", "macro", "filings", "post_earnings_candidates",
                "post_earnings_followups"):
        assert key in out
    assert out["world_headlines"] == ["Tankers struck in Hormuz"]


def test_finance_bundle_keeps_every_story_when_no_item_carries_a_desk():
    """`desk` is set by the feed registry and survives selection, but a degraded
    selection path can drop it. Filtering to nothing would silently empty
    `finance.brief` on exactly the day the pipeline was already limping."""
    bundle = {"news": [{"title": "cpi"}, {"title": "war"}]}
    assert len(codex_engine._finance_bundle(bundle)["news"]) == 2


def test_neither_split_mutates_the_caller_s_bundle():
    before = json.dumps(_FULL_BUNDLE, sort_keys=True)
    codex_engine._world_bundle(_FULL_BUNDLE)
    codex_engine._finance_bundle(_FULL_BUNDLE, ["x"])
    assert json.dumps(_FULL_BUNDLE, sort_keys=True) == before
