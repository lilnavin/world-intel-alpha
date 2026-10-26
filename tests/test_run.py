"""Tests for scripts/run.py.

run.py is a thin wiring layer with no dedicated test scaffolding for its
try/except glue (see CONTRACT.md addendum, Task 5) -- except for the ownership
deadline derivation below, which gets one narrow regression test because a
wrong clock or a wrong budget silently empties the market-wide Form 4 scan on
every run while every other test stays green (this nearly shipped in Task 3;
see the Task 5 review).
"""

import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from worldintel.core import anomaly_panel  # noqa: E402
from worldintel.core import post_earnings_pool  # noqa: E402
from worldintel import run  # noqa: E402
from worldintel.core import sec_filing_text  # noqa: E402


def _timings_dict_node():
    """The `timings = {...}` literal in run.main, as an AST node.

    Read statically rather than by executing `main()`: the metrics record is
    written at the very end of a full run (network, Codex, KB writes), so
    there is no cheap way to observe the dict at runtime. The keys, however,
    are a literal in the source and can be pinned exactly."""
    source = open(run.__file__, encoding="utf-8").read()
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "timings" for t in node.targets)
                and isinstance(node.value, ast.Dict)):
            return node.value
    raise AssertionError("no `timings = {...}` literal found in run.py")


def _run_source() -> str:
    return open(run.__file__, encoding="utf-8").read()


def _run_tree() -> ast.AST:
    """run.py's AST with a `.parent` back-link on every node, so guard/wrap
    checks below can walk upward from a call site."""
    tree = ast.parse(_run_source())
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child.parent = node
    return tree


def _find_calls(tree: ast.AST, dotted_name: str):
    """All ast.Call nodes anywhere in `tree` whose callee is `dotted_name`
    (e.g. 'kb.append_scoreboard_history')."""
    parts = dotted_name.split(".")
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        names = []
        f = node.func
        while isinstance(f, ast.Attribute):
            names.insert(0, f.attr)
            f = f.value
        if isinstance(f, ast.Name):
            names.insert(0, f.id)
        if names == parts:
            calls.append(node)
    return calls


def _enclosing_try(node: ast.AST):
    """The nearest ast.Try ancestor of `node`, or None."""
    n = node
    while hasattr(n, "parent"):
        n = n.parent
        if isinstance(n, ast.Try):
            return n
    return None


def _dry_guarded(node: ast.AST) -> bool:
    """Whether `node` sits inside an `if not dry:` block anywhere up its
    ancestor chain -- i.e. is skipped on a --dry run, matching how
    mark_covered/record_buried/record_seen_articles/write_daily are gated."""
    n = node
    while hasattr(n, "parent"):
        n = n.parent
        if isinstance(n, ast.If):
            test = n.test
            if (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
                    and isinstance(test.operand, ast.Name)
                    and test.operand.id == "dry"):
                return True
    return False


def test_append_scoreboard_history_called_with_root_markets_and_today():
    """Task 2: run.py must call kb.append_scoreboard_history(KB_ROOT,
    bundle.get("markets") or {}, today) -- the exact args the brief specifies,
    verbatim."""
    tree = _run_tree()
    calls = _find_calls(tree, "kb.append_scoreboard_history")
    assert len(calls) == 1, "expected exactly one call site for append_scoreboard_history"
    call = calls[0]
    assert len(call.args) == 3
    assert isinstance(call.args[0], ast.Name) and call.args[0].id == "KB_ROOT"
    assert isinstance(call.args[1], ast.BoolOp) and isinstance(call.args[1].op, ast.Or), (
        "second arg must be `bundle.get(\"markets\") or {}`, matching update_scoreboard's call")
    assert isinstance(call.args[2], ast.Name) and call.args[2].id == "today"


def test_append_scoreboard_history_wrapped_in_try_except_that_logs():
    """A raising append_scoreboard_history must not abort the run: the call
    site must be inside its own try/except whose handler logs via the
    existing log() helper, matching update_scoreboard/record_buried/
    record_seen_articles."""
    tree = _run_tree()
    call = _find_calls(tree, "kb.append_scoreboard_history")[0]
    try_node = _enclosing_try(call)
    assert try_node is not None, "call must be wrapped in its own try/except"
    assert try_node.handlers, "try must have an except handler"
    log_calls = [n for n in ast.walk(try_node.handlers[0])
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "log"]
    assert log_calls, "except handler must call log(...) and continue, not re-raise"


def test_append_scoreboard_history_skipped_on_dry_run():
    """Persistence must be skipped under --dry, like mark_covered,
    record_buried, record_seen_articles and write_daily."""
    tree = _run_tree()
    call = _find_calls(tree, "kb.append_scoreboard_history")[0]
    assert _dry_guarded(call), "append_scoreboard_history must be gated behind `if not dry:`"


def test_update_scoreboard_is_dry_guarded():
    """The human-readable scoreboard is persistent state too."""
    tree = _run_tree()
    calls = _find_calls(tree, "kb.update_scoreboard")
    assert len(calls) == 1
    call = calls[0]
    assert _dry_guarded(call), "update_scoreboard must be skipped under --dry"
    try_node = _enclosing_try(call)
    assert try_node is not None and try_node.handlers


def test_append_scoreboard_history_placed_after_update_scoreboard():
    """Per the brief's placement decision: the history append sits immediately
    after the existing update_scoreboard call, in the same region of run.py."""
    tree = _run_tree()
    us_call = _find_calls(tree, "kb.update_scoreboard")[0]
    ash_call = _find_calls(tree, "kb.append_scoreboard_history")[0]
    assert us_call.lineno < ash_call.lineno


def test_ownership_lane_has_no_call_site():
    """The ownership lane was removed 2026-08-30 (roadmap Phase 0): ~55s of a
    600s ceiling spent on a block whose own prose said it carried no signal.

    `scripts/ownership.py` deliberately SURVIVES the removal — `filing_index`
    imports `parse_daily_index` and `_daily_index_url` from it, and the filing
    reconciliation lane is built on that parser. So "the module still exists"
    proves nothing here; only the absence of the orchestrator's call site does.
    This is the `_find_calls` wiring guard run in reverse."""
    tree = _run_tree()
    assert not _find_calls(tree, "ownership.build_ownership"), (
        "ownership.build_ownership is called again -- the lane costs ~55s and "
        "was removed deliberately")


def test_guard_elapsed_is_logged_outside_the_dry_guard():
    """--dry writes no metrics row, and every baseline run in Plan 0 is a dry
    run. If this line ever moves inside `if not dry`, the timing evidence for
    the whole plan silently disappears."""
    tree = _run_tree()
    logs = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "log" and "GUARD-ELAPSED" in ast.dump(n)]
    assert logs, "no GUARD-ELAPSED log line found"
    assert not _dry_guarded(logs[0])


def test_elapsed_at_guard_is_initialised_before_the_filing_lane():
    """The filing lane is wrapped in `except Exception`. If the name is only
    bound inside that try, a lane failure turns the metrics row into a
    NameError and takes the whole timings block with it."""
    source = _run_source()
    init = source.index("_elapsed_at_guard = None")
    assign = source.index("_elapsed_at_guard = round(")
    assert init < assign, "_elapsed_at_guard must be initialised first"


def test_timings_no_longer_carry_the_ownership_lane():
    """The timings dict is the only production visibility into lane cost, so a
    retired lane must leave it rather than linger as a permanent 0.0."""
    node = _timings_dict_node()
    keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
    assert "ownership" not in keys


# --------------------------------------------------------------------------- #
# Task 5 Part A -- kb.record_company_quarter wired into the persist block
# --------------------------------------------------------------------------- #
def test_record_company_quarter_wired_into_persist_block():
    tree = _run_tree()
    calls = _find_calls(tree, "kb.record_company_quarter")
    assert calls, "kb.record_company_quarter must be called in run.py's persist block"


def test_record_company_quarter_wrapped_in_try_except_that_logs():
    """A raising record_company_quarter must not abort the run: the call
    site must be inside its own try/except whose handler logs via the
    existing log() helper, matching mark_covered/record_buried."""
    tree = _run_tree()
    call = _find_calls(tree, "kb.record_company_quarter")[0]
    try_node = _enclosing_try(call)
    assert try_node is not None, "call must be wrapped in its own try/except"
    assert try_node.handlers, "try must have an except handler"
    log_calls = [n for n in ast.walk(try_node.handlers[0])
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "log"]
    assert log_calls, "except handler must call log(...) and continue, not re-raise"


def test_record_company_quarter_skipped_on_dry_run():
    tree = _run_tree()
    call = _find_calls(tree, "kb.record_company_quarter")[0]
    assert _dry_guarded(call), "record_company_quarter must be gated behind `if not dry:`"


# --------------------------------------------------------------------------- #
# Task 5 Part B -- writeup_tickers wired into the reconciliation pool. run.py
# is the only place that knows which tickers Codex actually wrote up; if it
# does not hand them to post_earnings_pool.record, every entry is recorded as
# also-reported and the card tier stops existing.
# --------------------------------------------------------------------------- #
def test_writeup_tickers_extracts_tickers_from_post_earnings_writeups():
    result = {"briefing": {"post_earnings": {"writeups": [
        {"ticker": "NFLX", "guidance_struct": None},
        {"ticker": "ZM", "guidance_struct": {"metric": "eps_q", "low": 1.0, "high": 1.2}},
        {"no_ticker": True},
    ]}}}
    assert run._writeup_tickers(result) == {"NFLX", "ZM"}


def test_writeup_tickers_degrades_to_empty_set_on_absent_or_malformed():
    assert run._writeup_tickers({}) == set()
    assert run._writeup_tickers(None) == set()
    assert run._writeup_tickers({"briefing": {}}) == set()
    assert run._writeup_tickers(
        {"briefing": {"post_earnings": {"writeups": "not a list"}}}) == set()
    assert run._writeup_tickers(
        {"briefing": {"post_earnings": {"writeups": [None, 5, "x"]}}}) == set()


def test_writeup_tickers_reach_the_pool_as_the_card_tier(tmp_path):
    """The Part B regression, end to end: run.py must actually pass the tickers
    it extracts, in the right slot, and they must land as `origin == "card"`.

    `record(root, candidates, writeup_tickers, also_tickers, today)` takes the
    two ticker sets back to back and both are plain sets of strings, so handing
    them over the wrong way round type-checks, runs clean, and silently demotes
    every card to `also` -- losing the 2.0 origin bonus that decides whose
    filing gets read first."""
    tree = _run_tree()
    call = _find_calls(tree, "post_earnings_pool.record")[0]
    assert len(call.args) == 5, "record takes five positional args"
    for pos, name in ((2, "_writeup_tickers"), (3, "_also_reported_tickers")):
        arg = call.args[pos]
        assert isinstance(arg, ast.Call), f"positional {pos} must be computed, not a literal"
        assert isinstance(arg.func, ast.Name) and arg.func.id == name, (
            f"positional {pos} must come from run.{name}(result)")

    result = {"briefing": {"post_earnings": {
        "writeups": [{"ticker": "NFLX"}],
        "also_reported": [{"ticker": "ZM"}]}}}
    candidates = [{"ticker": "NFLX", "report_date": "2026-08-21"},
                  {"ticker": "ZM", "report_date": "2026-08-21"}]
    post_earnings_pool.record(str(tmp_path), candidates, run._writeup_tickers(result),
                              run._also_reported_tickers(result), "2026-08-22")
    pool = post_earnings_pool.read_pool(str(tmp_path))
    assert pool["NFLX:2026-08-21"]["origin"] == "card"
    assert pool["ZM:2026-08-21"]["origin"] == "also"


# --------------------------------------------------------------------------- #
# Task 5 Part C -- guidance_struct merged onto the candidate before the pool
# freezes its snapshot (carried over from Task 4: nothing populated
# guidance_struct, so the recorded snapshot always shipped None).
# --------------------------------------------------------------------------- #
def test_merge_guidance_struct_onto_matching_candidate():
    cand = {"ticker": "NFLX"}
    result = {"briefing": {"post_earnings": {"writeups": [
        {"ticker": "NFLX", "guidance_struct": {"metric": "eps_q", "low": 1.0, "high": 1.2}},
    ]}}}
    run._merge_guidance_struct([cand], result)
    assert cand["guidance_struct"] == {"metric": "eps_q", "low": 1.0, "high": 1.2}


def test_merge_guidance_struct_skips_writeup_with_no_matching_candidate():
    cand = {"ticker": "NFLX"}
    result = {"briefing": {"post_earnings": {"writeups": [
        {"ticker": "ZM", "guidance_struct": {"metric": "eps_q", "low": 1.0, "high": 1.2}},
    ]}}}
    run._merge_guidance_struct([cand], result)
    assert "guidance_struct" not in cand


def test_merge_guidance_struct_skips_writeup_with_no_guidance_struct():
    cand = {"ticker": "NFLX"}
    result = {"briefing": {"post_earnings": {"writeups": [{"ticker": "NFLX",
                                                            "guidance_struct": None}]}}}
    run._merge_guidance_struct([cand], result)
    assert "guidance_struct" not in cand


def test_merge_guidance_struct_never_raises_on_garbage():
    run._merge_guidance_struct(None, None)
    run._merge_guidance_struct("not a list", {"briefing": "not a dict"})
    run._merge_guidance_struct(
        [{"ticker": "NFLX"}],
        {"briefing": {"post_earnings": {"writeups": "not a list"}}})


def test_merge_guidance_struct_reaches_the_recorded_pool_snapshot(tmp_path):
    """This is the Part C regression, proven end-to-end through the real pool
    writer: without the merge, guidance_struct always ships as None on the
    snapshot the filing block later recalls, even though the write-up carried
    a real value (design Sec.3.2 'the beat-1 numeric core')."""
    cand = {"ticker": "NFLX", "report_date": "2026-08-11", "source": "us",
            "name": "Netflix"}
    result = {"briefing": {"post_earnings": {"writeups": [
        {"ticker": "NFLX", "guidance_struct": {"metric": "eps_q", "low": 1.0, "high": 1.2}},
    ]}}}
    run._merge_guidance_struct([cand], result)

    post_earnings_pool.record(str(tmp_path), [cand], {"NFLX"}, set(), "2026-08-12")
    snapshot = post_earnings_pool.read_pool(str(tmp_path))["NFLX:2026-08-11"]["snapshot"]
    assert snapshot["guidance_struct"] == {"metric": "eps_q", "low": 1.0, "high": 1.2}


def test_guidance_struct_merged_before_the_pool_records_the_snapshot():
    """The merge must run before post_earnings_pool.record, which freezes an
    entry's snapshot on first sight and never rewrites it -- a merge ordered
    after would persist None for the whole 30-day window. It must itself be
    skipped on --dry, like the persistence it feeds."""
    tree = _run_tree()
    merge_calls = _find_calls(tree, "_merge_guidance_struct")
    assert merge_calls, "run.py must call _merge_guidance_struct before the pool record"
    rec_call = _find_calls(tree, "post_earnings_pool.record")[0]
    assert merge_calls[0].lineno < rec_call.lineno
    assert _dry_guarded(merge_calls[0]), "guidance_struct merge must be gated behind `if not dry:`"


# --------------------------------------------------------------------------- #
# Task 12 -- the anomaly_panel and sec_filing_text lanes wired into run.py.
#
# Tasks 3 and 6 each stopped at their own module's file boundary, so until this
# task neither module had a production call site anywhere: every panel and
# filing-text feature merged so far was inert, and no test caught it because
# each module's own suite passed in isolation.
# --------------------------------------------------------------------------- #
def test_anomaly_deadline_uses_own_clock_and_own_budget():
    """Same failure mode as `_ownership_deadline` (see the top of this file):
    a `time.time()` base reads as already-passed against `anomaly_panel._now()`
    and returns `partial` for every reporter without one request; borrowing
    ownership's 90s budget doubles a lane sized at 45s. Both pass silently."""
    expected = anomaly_panel._now() + anomaly_panel.LANE_DEADLINE_S
    actual = run._anomaly_deadline()
    assert abs(actual - expected) < 1.0


def test_filing_text_deadline_uses_own_clock_and_own_budget():
    expected = sec_filing_text._now() + sec_filing_text.LANE_DEADLINE_S
    actual = run._filing_text_deadline()
    assert abs(actual - expected) < 1.0


def test_the_two_deadlines_do_not_share_a_budget():
    """45s and 60s are separate numbers deliberately. A single shared helper
    would make one of the two lanes silently wrong."""
    assert anomaly_panel.LANE_DEADLINE_S != sec_filing_text.LANE_DEADLINE_S
    assert abs(run._anomaly_deadline() - run._filing_text_deadline()) > 5.0


def test_both_new_lanes_have_a_production_call_site():
    """The regression this task exists to fix."""
    tree = _run_tree()
    assert _find_calls(tree, "anomaly_panel.build_panel"), (
        "anomaly_panel.build_panel has no call site in run.py -- the panel is inert")
    assert _find_calls(tree, "sec_filing_text.fetch_filing_text"), (
        "sec_filing_text.fetch_filing_text has no call site in run.py -- the lane is inert")


def test_both_new_lanes_run_after_the_earnings_work():
    """Spec §6.3 ordering. A slow scan may never delay higher-value work, so
    both new lanes sit after the earnings block. This asserted ordering against
    `ownership.build_ownership` until that lane was removed (Phase 0); the
    surviving constraint is the same one, anchored to the earnings lane the
    ownership block used to follow."""
    tree = _run_tree()
    earn = _find_calls(tree, "earnings_report.build_post_earnings")[0]
    panel = _find_calls(tree, "anomaly_panel.build_panel")[0]
    text = _find_calls(tree, "sec_filing_text.fetch_filing_text")[0]
    assert earn.lineno < panel.lineno, "anomaly_panel must run after earnings"
    assert panel.lineno < text.lineno, "sec_filing_text must run after anomaly_panel"


def test_both_new_lanes_run_before_codex_reads_the_bundle():
    """Both lanes decorate `bundle`, and the two Codex passes are handed that
    same bundle. Running them after the analysis would put the work in the
    process without putting it in front of the model."""
    tree = _run_tree()
    text = _find_calls(tree, "sec_filing_text.fetch_filing_text")[0]
    codex = _find_calls(tree, "codex_engine.run_two_pass")[0]
    assert text.lineno < codex.lineno


def test_anomaly_panel_lane_wrapped_in_try_except_that_logs():
    tree = _run_tree()
    call = _find_calls(tree, "anomaly_panel.build_panel")[0]
    try_node = _enclosing_try(call)
    assert try_node is not None, "lane must be wrapped in its own try/except"
    assert try_node.handlers
    log_calls = [n for n in ast.walk(try_node.handlers[0])
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "log"]
    assert log_calls, "except handler must call log(...) and continue, not re-raise"


def test_filing_text_lane_wrapped_in_try_except_that_logs():
    tree = _run_tree()
    call = _find_calls(tree, "sec_filing_text.fetch_filing_text")[0]
    try_node = _enclosing_try(call)
    assert try_node is not None, "lane must be wrapped in its own try/except"
    assert try_node.handlers
    log_calls = [n for n in ast.walk(try_node.handlers[0])
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == "log"]
    assert log_calls, "except handler must call log(...) and continue, not re-raise"


def test_neither_lane_is_dry_guarded():
    """Neither lane persists anything -- they decorate the bundle the brief
    renders from. Gating them on `not dry` would make --dry stop exercising
    the very pipeline it exists to test. Only the persistence they FEED
    (record_company_quarter) is dry-guarded."""
    tree = _run_tree()
    assert not _dry_guarded(_find_calls(tree, "anomaly_panel.build_panel")[0])
    assert not _dry_guarded(_find_calls(tree, "sec_filing_text.fetch_filing_text")[0])


def test_panel_receives_the_year_ago_end_not_the_previous_quarter():
    """The NAMING TRAP. `anomaly_panel`'s fourth positional argument is the
    YEAR-AGO quarter end; `sec_fundamentals`' anchor calls the PREVIOUS quarter
    `prior_end`. Passing `fundamentals["prior_end"]` would type-check, run
    clean, and empty every YoY metric, because `_build_panel` matches on an
    exact date. It must be fed `py_end`."""
    tree = _run_tree()
    call = _find_calls(tree, "anomaly_panel.build_panel")[0]
    source = _run_source()
    fourth = ast.get_source_segment(source, call.args[3])
    assert "py_end" in fourth, (
        f"build_panel's prior_end argument must come from py_end, got: {fourth!r}")
    assert "prior_end" not in fourth, (
        f"that is sec_fundamentals' PREVIOUS-quarter key, not the year-ago one: {fourth!r}")


def test_record_company_quarter_is_fed_the_panel_not_the_fundamentals_standin():
    """Task 5 wired this to `cand["fundamentals"]` as an explicit interim
    stand-in until the panel had a call site. The companies/ KB wing is what
    PRECEDENT/REGIME reads across quarters, and fundamentals carries neither."""
    tree = _run_tree()
    call = _find_calls(tree, "kb.record_company_quarter")[0]
    source = _run_source()
    third = ast.get_source_segment(source, call.args[2])
    assert "fundamentals" not in third, (
        f"record_company_quarter is still fed the Task 5 stand-in: {third!r}")
    assert "panel" in third, (
        f"record_company_quarter must be fed the panel result: {third!r}")


def test_timings_record_the_two_new_lanes():
    """`metrics` is the only production visibility into lane cost, and these
    are the two newest budgets we most need to watch."""
    node = _timings_dict_node()
    keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
    assert keys == ["fetch", "select", "enrich", "earnings",
                    "anomaly", "filing_text", "codex", "elapsed_at_guard",
                    "total"]
    assigned = {
        t.id
        for n in ast.walk(ast.parse(_run_source())) if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    }
    for lane in ("anomaly", "filing_text"):
        value = node.values[keys.index(lane)]
        assert isinstance(value, ast.Name), f"timings[{lane!r}] is not a variable"
        assert value.id in assigned


# --------------------------------------------------------------------------- #
# Task 10 -- two-pass orchestration wired into the run.
#
# run.py now calls codex_engine.run_two_pass and merges the two halves back
# into the single {"briefing", "kb_deltas", "source_notes"} shape that
# quality_gate, source_diversity, metrics, kb.apply_deltas and
# compose_html.render_html all already consume. The merge is where "one pass
# failing still ships the other tab" actually happens.
# --------------------------------------------------------------------------- #
def test_run_two_pass_has_a_production_call_site():
    tree = _run_tree()
    assert _find_calls(tree, "codex_engine.run_two_pass"), (
        "codex_engine.run_two_pass has no call site in run.py -- the split is inert")


def test_the_single_pass_call_site_is_gone():
    """`analyze` runs analysis.md, which still contains the finance and
    post-earnings sections. Leaving it wired alongside the two passes would
    pay for three Codex runs and then render whichever half won."""
    tree = _run_tree()
    assert not _find_calls(tree, "codex_engine.analyze"), (
        "run.py still calls the single-pass codex_engine.analyze")


def test_the_two_passes_use_the_split_schemas():
    """Each pass gets its OWN schema file. Handing both passes
    briefing.schema.json would make every response fail `required`, since
    neither pass emits the other's half."""
    assert os.path.basename(run.FINANCE_SCHEMA_PATH) == "finance.schema.json"
    with run.data.as_file(run.FINANCE_SCHEMA_PATH) as schema_path:
        assert schema_path.is_file()

    tree = _run_tree()
    call = _find_calls(tree, "codex_engine.run_two_pass")[0]
    args = [ast.get_source_segment(_run_source(), a) for a in call.args]
    assert "world_schema" in args and "str(finance_schema)" in args
    assert len(_find_calls(tree, "schema_build.write_temp")) == 1


def test_run_two_pass_is_given_the_log_helper():
    """The sequential fallback is only observable if it is logged (spec 8.2)."""
    tree = _run_tree()
    call = _find_calls(tree, "codex_engine.run_two_pass")[0]
    kwargs = {kw.arg: ast.get_source_segment(_run_source(), kw.value)
              for kw in call.keywords}
    assert kwargs.get("log") == "log"


def _passes(world=None, finance=None, **kw):
    out = {"world": world, "finance": finance,
           "errors": {"world": None if world else "boom",
                      "finance": None if finance else "boom"},
           "mode": "sequential"}
    out.update(kw)
    return out


_WORLD = {"briefing": {"brief": [{"headline": "Hormuz"}],
                       "signal": {"top": {"story": "s", "reason": "r", "url": ""},
                                  "buried": [], "noise_line": ""},
                       "threads": [{"slug": "t", "today": "x", "since": "y"}],
                       "on_radar": ["watch this"],
                       "japan": [{"headline": "Diet passes the stimulus"}]},
          "kb_deltas": {"entities_upsert": [{"slug": "iran"}],
                        "threads_upsert": [{"slug": "hormuz"}],
                        "overview_replace": "the world today",
                        "expectations_resolved": ["e1"]},
          "source_notes": ["world note"]}

_FINANCE = {"briefing": {"finance": {"brief": [],
                                     "scoreboard_note": "n", "filings_note": "",
                                     "earnings_note": "", "ownership_note": ""},
                         "post_earnings": {"writeups": [{"ticker": "AMD"}],
                                           "also_reported": [], "followups": None}},
            "kb_deltas": {"entities_upsert": [{"slug": "amd"}],
                          "threads_upsert": [{"slug": "ai-capex"}]},
            "source_notes": ["finance note"]}

_BUNDLE = {"news": [{"title": "Tankers struck", "url": "http://x", "source": "Reuters",
                     "summary": "s"}]}


def test_merge_passes_reconstructs_the_shape_render_html_consumes():
    """compose_html.render_html(analysis, bundle, date) reads
    analysis["briefing"] with all six sections. The merge is what keeps that
    contract unchanged across the split."""
    result = run._merge_passes(_passes(_WORLD, _FINANCE), _BUNDLE)
    assert set(result) == {"briefing", "kb_deltas", "source_notes", "analysis_status"}
    assert set(result["briefing"]) == {"brief", "finance", "post_earnings",
                                       "signal", "threads", "on_radar", "japan",
                                       "canada"}
    assert result["briefing"]["brief"][0]["headline"] == "Hormuz"
    assert result["briefing"]["finance"]["scoreboard_note"] == "n"
    assert result["briefing"]["post_earnings"]["writeups"][0]["ticker"] == "AMD"
    assert result["briefing"]["on_radar"] == ["watch this"]


def test_the_japan_section_survives_the_merge():
    """`_WORLD_SECTIONS` is the allow-list `_half` copies out of the model's
    briefing. A section missing from it is dropped SILENTLY -- the pass writes
    the stories, the tab renders empty, and nothing anywhere logs why."""
    assert "japan" in run._world_sections()
    result = run._merge_passes(_passes(_WORLD, _FINANCE), _BUNDLE)
    assert result["briefing"]["japan"][0]["headline"] == "Diet passes the stimulus"


def test_the_degraded_world_still_carries_a_japan_key():
    """Raw headlines carry no country, and the renderer is pure -- it may not
    guess which of them are Japanese. So the degraded shape supplies the key
    EMPTY: the tab renders its empty state instead of KeyError-ing its way
    through a run that is already having a bad day."""
    for bundle in (None, {}, _BUNDLE):
        assert run._degraded_world(bundle)["japan"] == []


def test_a_dead_finance_pass_still_ships_the_world_tab():
    result = run._merge_passes(_passes(_WORLD, None), _BUNDLE)
    briefing = result["briefing"]
    assert briefing["brief"][0]["headline"] == "Hormuz"
    # ...and the finance tab renders its empty state rather than vanishing.
    # `scoreboard_read` is None rather than absent: the degraded shape is the
    # one the renderer reads on the pipeline's worst mornings, so every key it
    # promises has to actually be there.
    assert briefing["finance"]["scoreboard_note"] == ""
    assert briefing["finance"]["scoreboard_read"] is None
    assert briefing["post_earnings"]["followups"] is None
    assert any("finance" in n for n in result["source_notes"])


def test_a_dead_world_pass_still_ships_the_finance_tab():
    result = run._merge_passes(_passes(None, _FINANCE), _BUNDLE)
    briefing = result["briefing"]
    assert briefing["finance"]["scoreboard_note"] == "n"
    # The world tab degrades to raw headlines, exactly as the single pass did.
    assert briefing["brief"][0]["headline"] == "Tankers struck"
    assert any("world" in n for n in result["source_notes"])


def test_only_a_total_failure_claims_the_engine_is_unavailable():
    """run.py skips the writing gate when source_notes says "analysis engine
    unavailable". With one pass alive there ARE real bodies to scan, so that
    phrase must be reserved for a double failure or the gate silently stops
    running on half-good days."""
    half = run._merge_passes(_passes(_WORLD, None), _BUNDLE)
    assert not any("analysis engine unavailable" in n for n in half["source_notes"])
    both = run._merge_passes(_passes(None, None), _BUNDLE)
    assert any("analysis engine unavailable" in n for n in both["source_notes"])


def test_merge_passes_merges_kb_deltas_additively_and_world_owns_the_overview():
    """`apply_deltas` merges entity and thread upserts, so concatenating the
    two passes' lists is safe. `overview_replace` is a single string and comes
    from the world pass alone -- see the finance schema, which has no such
    property to merge."""
    deltas = run._merge_passes(_passes(_WORLD, _FINANCE), _BUNDLE)["kb_deltas"]
    assert [e["slug"] for e in deltas["entities_upsert"]] == ["iran", "amd"]
    assert [t["slug"] for t in deltas["threads_upsert"]] == ["hormuz", "ai-capex"]
    assert deltas["overview_replace"] == "the world today"
    assert deltas["expectations_resolved"] == ["e1"]


def test_merge_passes_never_raises_on_garbage():
    for passes in ({}, {"world": None, "finance": None},
                   {"world": "nope", "finance": 7},
                   {"world": {"briefing": "nope"}, "finance": {"briefing": None}}):
        result = run._merge_passes(passes, _BUNDLE)
        assert set(result["briefing"]) == {"brief", "finance", "post_earnings",
                                           "signal", "threads", "on_radar", "japan",
                                           "canada"}


def test_the_merge_happens_before_the_quality_gate_and_the_render():
    """Every downstream consumer -- the writing gate, the Guardian cap, the KB
    deltas, metrics and the renderer -- reads the MERGED result. A merge placed
    after any of them would leave that stage reading one tab only."""
    tree = _run_tree()
    merge = _find_calls(tree, "_merge_passes")[0]
    for callee in ("quality_gate.scan", "source_diversity.enforce_guardian_soft_cap",
                   "kb.apply_deltas", "compose_html.render_html",
                   "metrics.compute_metrics"):
        assert merge.lineno < _find_calls(tree, callee)[0].lineno, (
            f"{callee} runs before the two passes are merged")


# --------------------------------------------------------------------------- #
# Beat-2 panel (spec §3 "When the panel runs"): the panel runs on BOTH beats.
# Task 12 wired beat 1 (candidates) only, which left two things broken --
# the follow-up card's flag chips had no producer and were inert in
# production, and the persisted company-quarter row never converged from the
# beat-1 `partial` to the beat-2 complete version for a closed quarter.
# --------------------------------------------------------------------------- #
def test_panel_is_built_for_promoted_followups_as_well_as_candidates():
    """Spec §3 gives beat 2 its own row: scope 'the promoted follow-ups only'.
    Without a second call site the follow-up card renders a segment table and
    a risk line but never a flag chip."""
    tree = _run_tree()
    calls = _find_calls(tree, "anomaly_panel.build_panel")
    assert len(calls) == 2, (
        f"expected a beat-1 and a beat-2 build_panel call site, found {len(calls)}")


def test_beat2_panel_runs_after_the_filing_text_lane():
    """Beat 2's extra families (segment, off-balance-sheet) are filing-derived,
    so the beat-2 panel must sit downstream of the document fetch."""
    tree = _run_tree()
    text = _find_calls(tree, "sec_filing_text.fetch_filing_text")[0]
    panels = _find_calls(tree, "anomaly_panel.build_panel")
    assert panels[-1].lineno > text.lineno, (
        "the beat-2 panel must run after the filing text it is meant to read")


def test_beat2_panel_is_persisted_so_the_partial_row_converges():
    """Spec §3: beat 2 'upserts the SAME period_end key, replacing it', which
    is what stops load_company_history seeing a partial row for a closed
    quarter. Two persist sites, both dry-guarded."""
    tree = _run_tree()
    calls = _find_calls(tree, "kb.record_company_quarter")
    assert len(calls) == 2, (
        f"expected a beat-1 and a beat-2 persist site, found {len(calls)}")
    for call in calls:
        assert _dry_guarded(call), "persistence must be gated behind `if not dry:`"


def test_reconcile_match_is_given_the_largecap_universe():
    """`reconcile` may not import earnings_report (requests at import time), so
    the size term exists only if run.py injects the set. Accepted-but-not-passed
    is the built-tested-never-called shape again: every unit test would stay
    green while every filing scored as a small cap and the read order stayed
    exactly as broken as it was on 2026-08-30."""
    tree = _run_tree()
    calls = _find_calls(tree, "reconcile.match")
    assert len(calls) == 1
    call = calls[0]
    passed = ([ast.unparse(a) for a in call.args]
              + [ast.unparse(k.value) for k in call.keywords])
    assert any("LARGECAP_INDEX_SET" in p for p in passed), (
        "reconcile.match must receive earnings_report.LARGECAP_INDEX_SET")


def test_expired_unread_is_computed_and_put_on_the_bundle():
    """`metrics` reads `bundle["filings_expired_unread"]`. Computing it and not
    recording it, or recording a key metrics does not read, both leave the
    backlog leak exactly as invisible as it was."""
    tree = _run_tree()
    assert _find_calls(tree, "reconcile.expired_unread"), (
        "reconcile.expired_unread has no call site -- the counter is inert")
    source = _run_source()
    assert 'bundle["filings_expired_unread"]' in source


def test_l1_snapshot_precedes_selection():
    """The capsule must capture the bundle as selection saw it. run.py mutates
    `bundle` right after (news at :354), so a snapshot taken later captures
    something selection never received."""
    tree = _run_tree()
    snap = _find_calls(tree, "capsule.snapshot")[0]
    select = _find_calls(tree, "select_news.select_candidates")[0]
    assert snap.lineno < select.lineno


def test_l2_snapshot_precedes_the_renderer_and_follows_every_gate():
    """L2 is the state entering render_html -- AFTER the writing, country
    grounding and diversity gates. Capturing at the merge would store
    something four mutations upstream of what actually rendered."""
    tree = _run_tree()
    merge = _find_calls(tree, "_merge_passes")[0]
    grounding = _find_calls(tree, "quality_gate.enforce_country_grounding")[0]
    diversity = _find_calls(
        tree, "source_diversity.enforce_guardian_soft_cap")[0]
    render = _find_calls(tree, "compose_html.render_html")[0]
    snaps = _find_calls(tree, "capsule.snapshot")
    l2 = [s for s in snaps if s.lineno > diversity.lineno][0]
    assert merge.lineno < grounding.lineno < diversity.lineno < l2.lineno
    assert l2.lineno < render.lineno


def _delivery_point(tree):
    """The line at which the brief is handed off for delivery.

    Today that is a literal `print("MEDIA:...")`; after Plan 1's delivery seam
    it is `delivery.deliver(...)`. Accepting either keeps this invariant green
    across that refactor instead of turning it into a scheduled failure -- the
    property being asserted (capsule I/O comes last) is unchanged by which
    mechanism delivers.
    """
    calls = _find_calls(tree, "delivery.deliver")
    if calls:
        return calls[0].lineno
    media = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "print" and "MEDIA:" in ast.dump(n)]
    assert media, "no delivery point found: neither delivery.deliver nor MEDIA:"
    return media[0].lineno


def test_capsule_write_follows_delivery():
    """The guard that matters. Every elapsed-time decision is evaluated before
    the brief is handed off, so writing after that cannot move one. See
    reconcile.PROMOTION_ELAPSED_GUARD_S and run.py:516."""
    tree = _run_tree()
    write = _find_calls(tree, "capsule.write")[0]
    assert write.lineno > _delivery_point(tree)


def test_no_capsule_write_before_the_guard_is_read():
    """Belt and braces: not one capsule.write may precede the elapsed read at
    :516, regardless of how the code is later rearranged."""
    tree = _run_tree()
    guard = [n for n in ast.walk(tree)
             if isinstance(n, ast.Compare)
             and "PROMOTION_ELAPSED_GUARD_S" in ast.dump(n)]
    assert guard, "promotion guard comparison not found"
    for call in _find_calls(tree, "capsule.write"):
        assert call.lineno > guard[0].lineno


def test_every_capsule_call_is_wrapped_in_try_except_that_logs():
    """Fail-open by contract: a capture failure must never cost a brief."""
    tree = _run_tree()
    calls = (_find_calls(tree, "capsule.snapshot")
             + _find_calls(tree, "capsule.write")
             + _find_calls(tree, "capsule.Pending"))
    assert len(calls) >= 4, "expected both snapshots, the write and the Pending"
    for call in calls:
        try_node = _enclosing_try(call)
        assert try_node is not None, f"unwrapped capsule call at line {call.lineno}"
        assert try_node.handlers
        logs = [n for n in ast.walk(try_node.handlers[0])
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "log"]
        assert logs or any(
            isinstance(n, ast.Attribute) and n.attr == "fail"
            for n in ast.walk(try_node.handlers[0])), (
            f"handler at line {call.lineno} must log or record, not re-raise")


def test_capsule_capture_is_not_dry_guarded():
    """--dry exists to exercise the pipeline without side effects. Capsules are
    how the pipeline is observed, so gating them on `not dry` would make the
    forced captures in Task 1 and Task 4 impossible."""
    tree = _run_tree()
    for call in _find_calls(tree, "capsule.snapshot"):
        assert not _dry_guarded(call)


def test_selection_settings_resolve_against_the_real_code():
    """Names drift. A capsule whose settings block raised would be dropped by
    the fail-open handler, and the sample would quietly be empty."""
    from worldintel import run
    s = run._selection_settings()
    assert s["fresh_hours"] == 36
    assert s["per_desk"] == 10
    assert s["per_feed_cap"] == 3
    assert "japan" in s["local_desks"]


def test_selection_is_called_without_overriding_its_defaults():
    """_selection_settings records DECLARED defaults, which inspect can see.
    It cannot see an argument passed at a call site. That is only honest while
    run.py passes none -- so pin it here rather than in a comment."""
    tree = _run_tree()
    call = _find_calls(tree, "select_news.select_candidates")[0]
    assert not call.keywords, (
        "select_candidates now takes an override; _selection_settings must "
        "record the passed value, not the signature default")


def test_legacy_deploy_stamp_without_hashes_remains_dirty(tmp_path, monkeypatch):
    """An old stamp cannot attest to the bytes of a deployed package. Preserve
    its recorded commit for diagnostics, but never classify it as clean.

    Relocate run.py by its `__file__`, not by patching os.path.dirname --
    os.path.abspath and open() both route through dirname, so patching it
    globally breaks the very call under test.
    """
    import json as _json
    from worldintel import run
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / ".provenance.json").write_text(
        _json.dumps({"commit": "deadbee", "dirty": False}), encoding="utf-8")
    monkeypatch.setattr(run, "__file__", str(scripts / "run.py"))
    assert run._source_provenance() == ("deadbee", True)


def test_provenance_is_dirty_when_nothing_can_answer(tmp_path, monkeypatch):
    """No git, no stamp: say so. Guessing clean here would hand Plan 1 a
    baseline that nothing produced."""
    from worldintel import run
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    monkeypatch.setattr(run, "__file__", str(scripts / "run.py"))
    assert run._source_provenance() == ("", True)
