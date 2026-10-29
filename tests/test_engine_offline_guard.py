"""The new model-process boundary must be offline unless explicitly simulated."""
import sys
from types import SimpleNamespace

import pytest

from conftest import NetworkBlocked
from worldintel.core.command_engine import CommandEngine
from worldintel import agent_run


def test_command_analysis_cannot_launch_by_default(tmp_path):
    schema = tmp_path / "schema.json"
    schema.write_text('{"type": "object"}', encoding="utf-8")
    harmless_child = [sys.executable, "-c", "raise SystemExit(98)"]
    with pytest.raises(NetworkBlocked):
        CommandEngine(harmless_child).run_schema("synthetic", str(schema), timeout=2)


def test_agent_pipeline_child_cannot_launch_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_run.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    with (tmp_path / "diagnostics").open("wb") as diagnostics:
        with pytest.raises(NetworkBlocked):
            agent_run._invoke(tmp_path / "synthetic.yaml", False, {}, diagnostics)
