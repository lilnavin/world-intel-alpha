"""Non-default configuration reaches the runtime, not just its validator."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from worldintel import config
from worldintel.core import compose_html
from worldintel.core import codex_engine
from worldintel.core import earnings_report
from worldintel.core import filing_index
from worldintel.core import kb
from worldintel import run
from test_dry_isolation import offline  # noqa: F401


def test_timezone_applies_to_header_and_footer():
    html = compose_html.render_html({}, {}, "2026-10-01", timezone_label="UTC")
    assert "(JST)" not in html and "&middot; JST" not in html
    assert "(UTC)" in html and "&middot; UTC" in html


def configure(synthetic_config, *, nationals=True, tuning=None):
    raw = copy.deepcopy(synthetic_config.raw)
    raw["desks"] = [d for d in raw["desks"] if d["kind"] != "national"]
    if nationals:
        raw["desks"].append({"id": "kenya", "kind": "national", "label": "Kenya",
            "signature_patterns": [r"\bNairobi\b"], "target_stories": [1, 4],
            "tab": {"order": 0, "colour": "#123456"},
            "prompt_section": "## Kenya\nWrite briefing.kenya with grounded reporting.\n\n"})
    if tuning:
        raw["tuning"] = tuning
    cfg = config.Config(raw, synthetic_config.source_path)
    config.set_for_test(cfg)
    return cfg


@pytest.mark.parametrize("nationals", [False, True])
def test_dynamic_merge_and_degraded_shape(synthetic_config, nationals):
    cfg = configure(synthetic_config, nationals=nationals)
    fallback = run._degraded_world({})
    assert not {"japan", "canada"} & fallback.keys()
    assert ("kenya" in fallback) == nationals
    world = {"briefing": {"kenya": [{"headline": "Nairobi"}], "japan": [{}]}}
    merged = run._merge_passes({"world": world}, {})["briefing"]
    assert not {"japan", "canada"} & merged.keys()
    if nationals:
        assert merged["kenya"] == world["briefing"]["kenya"]
    assert set(run._world_sections()) == {"brief", "signal", "threads", "on_radar", *cfg.local_desk_ids}


def test_analysis_uses_generated_prompt(synthetic_config, monkeypatch):
    configure(synthetic_config)
    captured = {}
    def model(prompt, schema, **kwargs):
        captured.update(prompt=prompt, schema=schema)
        return {}
    monkeypatch.setattr(codex_engine, "run_schema", model)
    codex_engine.analyze_world({}, {}, "synthetic-schema")
    assert "briefing.kenya" in captured["prompt"]
    assert "briefing.japan" not in captured["prompt"]
    assert "briefing.canada" not in captured["prompt"]
    assert captured["schema"] == "synthetic-schema"


@pytest.mark.parametrize("crash", [False, True])
def test_generated_schema_is_used_and_removed(offline, synthetic_config, monkeypatch, crash):
    configure(config.get())
    captured = []
    def analyze(bundle, context, world_schema, finance_schema, **kwargs):
        captured.append(Path(world_schema))
        schema = json.loads(captured[0].read_bytes())
        props = schema["properties"]["briefing"]["properties"]
        assert "kenya" in props and "japan" not in props
        assert props["kenya"]["maxItems"] == 4
        if crash:
            raise RuntimeError("injected analysis failure")
        return {"world": {"briefing": {}}, "finance": {"briefing": {}}, "errors": {}}
    monkeypatch.setattr(run.codex_engine, "run_two_pass", analyze)
    monkeypatch.setattr(sys, "argv", ["run.py", "--dry", "--force"])
    if crash:
        with pytest.raises(RuntimeError, match="injected"):
            run.main()
    else:
        assert run.main() == 0
        assert offline[4].call_args.kwargs["desks"] == config.get().desks
    assert captured and not captured[0].exists()


def test_nondefault_retention_and_deadline_settings(synthetic_config, monkeypatch, tmp_path):
    configure(synthetic_config, tuning={"retention_days": {
        "seen": 2, "buried": 2, "post_earnings": 2, "filing_index": 2},
        "deadlines_s": {"anomaly": 7, "filing_text": 9, "filing_index": 11}})
    monkeypatch.setattr(run.anomaly_panel, "_now", lambda: 100)
    monkeypatch.setattr(run.sec_filing_text, "_now", lambda: 100)
    assert run._anomaly_deadline() == 107
    assert run._filing_text_deadline() == 109
    assert filing_index._window_days("2026-10-01") == ["2026-10-01", "2026-09-30"]
    root = str(tmp_path / "kb")
    kb.ensure_kb(root)
    kb.write_state(root, {"recent_buried": [{"story": "old", "date": "2026-09-25"}],
                          "seen_story_hashes": [{"hash": "old", "date": "2026-09-25"}]})
    assert kb.load_seen(root, "2026-10-01") == set()
    assert kb.load_seen(root, "2026-10-01", days=14) == {"old"}
    kb.record_buried(root, ["new"], "2026-10-01")
    kb.record_seen_articles(root, [], "2026-10-01")
    assert kb.read_state(root)["recent_buried"] == [{"story": "new", "date": "2026-10-01"}]
    assert kb.read_state(root)["seen_story_hashes"] == []
    earnings_report.write_pipeline(root, {"old": {"status": "covered", "last_seen": "2026-09-25"},
                                         "pending": {"status": "pending", "last_seen": "2026-09-25"}})
    earnings_report.mark_covered(root, [], "2026-10-01")
    assert set(earnings_report.read_pipeline(root)) == {"pending"}


def test_filing_index_deadline_is_configured(synthetic_config, monkeypatch, tmp_path):
    configure(synthetic_config, tuning={"deadlines_s": {"filing_index": 11}})
    monkeypatch.setattr(filing_index, "_now", lambda: 100)
    monkeypatch.setattr(filing_index, "_window_days", lambda today: [today])
    seen = []
    def fetch(day, session, health, deadline):
        seen.append(deadline)
        return {}
    monkeypatch.setattr(filing_index, "fetch_day", fetch)
    filing_index.load_window(str(tmp_path), "2026-10-01", None, {})
    assert seen == [111]
