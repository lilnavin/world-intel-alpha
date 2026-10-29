import json
import sys
from pathlib import Path

import pytest

from worldintel.core import codex_engine


def test_dispatch_default_and_invalid():
    from worldintel.core import analysis_engine
    assert analysis_engine.select({}).engine_id == 'codex'
    with pytest.raises(codex_engine.CodexError, match='Unsupported analysis engine'):
        analysis_engine.select({'analysis': {'engine': 'secret'}})


def test_runner_reaches_both_passes():
    calls = []
    def runner(prompt, schema, **kwargs):
        calls.append((prompt, schema, kwargs))
        return {'briefing': {'brief': []}}
    result = codex_engine.run_two_pass({}, {}, 'world', 'finance', runner=runner)
    assert result['analysis_status']['overall'] == 'complete'
    assert [c[1] for c in calls] == ['world', 'finance']


def test_command_dispatch_and_native_factory(monkeypatch):
    import types
    from worldintel.core import analysis_engine
    assert analysis_engine.select({'analysis': {'engine': 'command', 'command': ['synthetic']}}).argv == ['synthetic']
    sentinel = object()
    calls = []
    def factory(kind, cfg):
        calls.append((kind, cfg))
        return sentinel
    monkeypatch.setitem(sys.modules, 'worldintel.core.host_engines', types.SimpleNamespace(create_engine=factory))
    cfg = {'analysis': {'engine': 'hermes'}}
    assert analysis_engine.select(cfg) is sentinel
    assert calls == [('hermes', cfg)]
