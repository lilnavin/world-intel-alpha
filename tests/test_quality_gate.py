"""Unit tests for the post-analysis writing-quality gate (pure, no Codex)."""
import os
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from worldintel.core import quality_gate


def _result(world_bodies, finance_bodies=()):
    return {"briefing": {
        "brief": [{"body": b} for b in world_bodies],
        "finance": {"brief": [{"body": b} for b in finance_bodies]},
    }}


CLEAN = "Iran struck two tankers on Monday, tripling war-risk premiums."
DIRTY_BUNDLE = "The bundle only gives a headline for this story."
DIRTY_LIMITED = "Limited detail is available about the vote."
DIRTY_UNCLEAR = "It is unclear from the reporting who ordered the strike."


def test_scan_flags_meta_language_in_both_sections():
    res = _result([CLEAN, DIRTY_BUNDLE], [DIRTY_LIMITED])
    flagged = quality_gate.scan(res)
    assert [(s, i) for s, i, _ in flagged] == [("brief", 1), ("finance", 0)]


def test_scan_clean_result_and_garbage():
    assert quality_gate.scan(_result([CLEAN])) == []
    assert quality_gate.scan(None) == []
    assert quality_gate.scan({"briefing": {"brief": [None, {"body": 3}]}}) == []


def test_meta_re_patterns():
    for bad in (DIRTY_BUNDLE, DIRTY_LIMITED, DIRTY_UNCLEAR,
                "Based on the headline alone, this matters.",
                "No further information was provided.",
                "The data provided stops at the headline level.",
                "This bundle covers only two outlets."):
        assert quality_gate.META_RE.search(bad), bad
    assert not quality_gate.META_RE.search(CLEAN)


def test_meta_re_negative_on_finance_prose():
    for clean in (
        "A bundle of mortgage loans rallied after the Fed decision.",
        "The available liquidity supported the rally.",
        "The company withdrew the provided guidance for 2027.",
        "Aid arrived as a bundle of reforms and loans.",
    ):
        assert not quality_gate.META_RE.search(clean), clean


def test_rewrite_patches_in_place():
    res = _result([DIRTY_BUNDLE], [DIRTY_LIMITED])
    flagged = quality_gate.scan(res)

    def fake_run(prompt, schema_path):
        assert DIRTY_BUNDLE in prompt and DIRTY_LIMITED in prompt
        return {"bodies": ["Clean world body.", "Clean finance body."]}

    n = quality_gate.rewrite(res, flagged, fake_run, "schema.json")
    assert n == 2
    assert res["briefing"]["brief"][0]["body"] == "Clean world body."
    assert res["briefing"]["finance"]["brief"][0]["body"] == "Clean finance body."


def test_rewrite_fails_open():
    res = _result([DIRTY_BUNDLE])
    flagged = quality_gate.scan(res)
    # engine raised
    assert quality_gate.rewrite(res, flagged, lambda p, s: (_ for _ in ()).throw(RuntimeError()), "x") == 0
    # wrong count
    assert quality_gate.rewrite(res, flagged, lambda p, s: {"bodies": []}, "x") == 0
    # rewrite still dirty -> not applied
    assert quality_gate.rewrite(res, flagged, lambda p, s: {"bodies": [DIRTY_LIMITED]}, "x") == 0
    assert res["briefing"]["brief"][0]["body"] == DIRTY_BUNDLE
    # empty flagged is a no-op
    assert quality_gate.rewrite(res, [], lambda p, s: {}, "x") == 0


# --------------------------------------------------------------------------- #
# Japan section. Its bodies come from the same pass, under the same style
# contract, and its prompt is the one that warns hardest about padding — so it
# earns the same scrutiny as The Brief and the finance brief. Shipped unscanned,
# the one section nothing measures would be the one most at risk.
# --------------------------------------------------------------------------- #
REWRITTEN = "Tokyo confirmed the toll rose to eight on Friday."


def _japan_result(world_bodies, japan_bodies, finance_bodies=()):
    return {"briefing": {
        "brief": [{"body": b} for b in world_bodies],
        "japan": [{"body": b} for b in japan_bodies],
        "finance": {"brief": [{"body": b} for b in finance_bodies]},
    }}


def test_scan_flags_meta_language_in_the_japan_section():
    flagged = quality_gate.scan(_japan_result([CLEAN], [CLEAN, DIRTY_UNCLEAR]))
    assert [(s, i) for s, i, _ in flagged] == [("japan", 1)]


def test_rewrite_patches_a_japan_body_in_place():
    res = _japan_result([CLEAN], [DIRTY_BUNDLE])
    flagged = quality_gate.scan(res)
    patched = quality_gate.rewrite(
        res, flagged, lambda p, s: {"bodies": [REWRITTEN]}, "unused.json")
    assert patched == 1
    assert res["briefing"]["japan"][0]["body"] == REWRITTEN


def test_rewrite_never_writes_a_japan_body_into_the_finance_brief():
    """`rewrite` branched on `brief` vs EVERYTHING ELSE, so the first japan hit
    would have been patched into `finance.brief[idx]` — silently overwriting a
    different section's story with another section's prose. Pinned."""
    res = _japan_result([CLEAN], [DIRTY_BUNDLE], [CLEAN])
    quality_gate.rewrite(res, quality_gate.scan(res),
                         lambda p, s: {"bodies": [REWRITTEN]}, "unused.json")
    assert res["briefing"]["finance"]["brief"][0]["body"] == CLEAN
    assert res["briefing"]["japan"][0]["body"] == REWRITTEN


def test_scan_survives_a_malformed_japan_section():
    for bad in (None, "not a list", [None], [{"body": 7}]):
        assert quality_gate.scan(
            {"briefing": {"brief": [], "japan": bad}}) == []


def test_country_grounding_keeps_only_full_text_tier12_cards():
    body = "word " * 120
    result = {"briefing": {
        "japan": [{"body": body, "sources": [{"url": "https://good/jp"}]}],
        "canada": [
            {"body": body, "sources": [{"url": "https://good/ca"}]},
            {"body": body, "sources": [{"url": "https://bad/ca"}]},
        ],
    }}
    bundle = {"news": [
        {"url": "https://good/jp", "tier": 1, "content": "x" * 800},
        {"url": "https://good/ca", "tier": 2, "content": "x" * 900},
        {"url": "https://bad/ca", "tier": 3, "content": "x" * 900},
    ]}
    audit = quality_gate.enforce_country_grounding(result, bundle)
    assert audit == {"japan_removed": 0, "canada_removed": 1}
    assert len(result["briefing"]["japan"]) == 1
    assert len(result["briefing"]["canada"]) == 1


def test_country_grounding_rejects_padding_and_unknown_urls():
    result = {"briefing": {
        "japan": [{"body": "too short", "sources": [{"url": "https://good"}]}],
        "canada": [{"body": "word " * 120,
                    "sources": [{"url": "https://invented"}]}],
    }}
    bundle = {"news": [{"url": "https://good", "tier": 1,
                        "content": "x" * 800}]}
    audit = quality_gate.enforce_country_grounding(result, bundle)
    assert audit == {"japan_removed": 1, "canada_removed": 1}
    assert result["briefing"]["japan"] == []
    assert result["briefing"]["canada"] == []


def test_country_grounding_matches_scheme_and_host_case_insensitively():
    story = {"body": "word " * 120,
             "sources": [{"url": "HTTPS://News.Example/Report?id=AbC"}]}
    result = {"briefing": {"japan": [story]}}
    bundle = {"news": [{"url": "https://news.example/Report?id=AbC",
                        "tier": 1, "content": "x" * 800}]}

    audit = quality_gate.enforce_country_grounding(result, bundle)

    assert audit["japan_removed"] == 0
    assert result["briefing"]["japan"] == [story]


def test_country_grounding_rejects_path_case_mismatch():
    result = {"briefing": {"japan": [
        {"body": "word " * 120,
         "sources": [{"url": "https://news.example/REPORT"}]},
    ]}}
    bundle = {"news": [{"url": "https://news.example/report",
                        "tier": 1, "content": "x" * 800}]}

    audit = quality_gate.enforce_country_grounding(result, bundle)

    assert audit["japan_removed"] == 1
    assert result["briefing"]["japan"] == []


def test_country_grounding_rejects_query_case_mismatch():
    result = {"briefing": {"japan": [
        {"body": "word " * 120,
         "sources": [{"url": "https://news.example/report?id=AbC"}]},
    ]}}
    bundle = {"news": [{"url": "https://news.example/report?id=abc",
                        "tier": 1, "content": "x" * 800}]}

    audit = quality_gate.enforce_country_grounding(result, bundle)

    assert audit["japan_removed"] == 1
    assert result["briefing"]["japan"] == []
