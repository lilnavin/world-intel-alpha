import ast
from pathlib import Path


def test_live_pipeline_recovers_protected_sources_before_model_analysis():
    tree = ast.parse((Path(__file__).parents[1] / "worldintel/run.py").read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)]
    recovery = [node for node in calls if node.func.attr == "enrich_protected_alternates"]
    analysis = [node for node in calls if node.func.attr == "run_two_pass"]
    assert len(recovery) == 1
    assert analysis and recovery[0].lineno < min(node.lineno for node in analysis)
    assert any(k.arg == 'deadline' and isinstance(k.value, ast.Name)
               and k.value.id == '_analysis_deadline' for k in recovery[0].keywords)
