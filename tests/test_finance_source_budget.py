from worldintel.core import codex_engine as engine, source_diversity as sd


def story():
    return {"headline": "Central bank decision", "sources": [{"outlet": "Guardian"}]}


def candidates():
    return {"news": [{"title": "Central bank decision " + str(i), "source": "Reuters", "url": "https://reuters.com/" + str(i), "tier": 1, "content": "Full reporting", "desk": "finance"} for i in range(2)]}


def test_sequential_finance_prompt_has_exhausted_shared_guardian_budget(monkeypatch):
    prompts = []
    monkeypatch.setattr(engine, "analyze_world", lambda *a, **k: {"briefing": {"brief": [story(), story()]}})
    def model(prompt, *args, **kwargs):
        prompts.append(prompt)
        return {"briefing": {"finance": {"brief": []}}}
    monkeypatch.setattr(engine, "run_schema", model)
    result = engine.run_two_pass(candidates(), {}, "world", "finance")
    assert '"world_guardian_picks": 2' in prompts[0]
    assert '"finance_guardian_remaining": 0' in prompts[0]
    assert '"scarcity_exception_allowed": false' in prompts[0]
    assert result["analysis_status"]["overall"] == "complete"
    assert result["finance"]["briefing"]["finance"]["brief"] == []


def test_gate_diagnostics_explain_finance_removal_without_story_body():
    result = {"briefing": {"brief": [story(), story()], "finance": {"brief": [story()]}}}
    debug = sd.enforce_guardian_soft_cap(candidates(), result)
    assert debug["section_counts_before"] == {"world": 2, "finance": 1}
    assert debug["section_counts_after"] == {"world": 2, "finance": 0}
    assert debug["removed_by_section"] == {"world": 0, "finance": 1}
    assert debug["removals"] == [{"section": "finance", "index": 0, "reason": "shared_guardian_cap_exceeded",
                                  "headline": "Central bank decision", "source_urls": [""]}]
    assert all("body" not in row and "content" not in row for row in debug["removals"])


def test_finance_budget_uses_whole_enriched_pool_for_scarcity():
    bundle = candidates()
    bundle["news"][0]["desk"] = "world"
    result = {"briefing": {"brief": [story(), story()]}}
    budget = sd.finance_guardian_budget(bundle, result)
    assert budget["eligible_non_guardian_candidates"] == 2
    assert budget["scarcity_exception_allowed"] is False
    bundle["news"][0]["content"] = ""
    assert sd.finance_guardian_budget(bundle, result)["scarcity_exception_allowed"] is True


def test_empty_finance_remains_empty_with_section_diagnostics():
    result = {"briefing": {"brief": [], "finance": {"brief": []}}}
    debug = sd.enforce_guardian_soft_cap(candidates(), result)
    assert debug["section_counts_before"] == debug["section_counts_after"] == {"world": 0, "finance": 0}
    assert debug["removals"] == []
