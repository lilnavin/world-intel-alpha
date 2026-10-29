import sys
from types import SimpleNamespace


def test_native_factory_uses_owned_standalone_process(monkeypatch):
    from worldintel.core import host_engines
    calls=[]
    monkeypatch.setattr(host_engines,'CommandEngine',lambda argv,**kw: calls.append((argv,kw)) or SimpleNamespace())
    cfg={'analysis':{'openclaw':{'gateway_url':'http://127.0.0.1:18789','agent_id':'owner'},'hermes':{'python':'host-python','source_root':'trusted-root'}}}
    host_engines.create_engine('openclaw',cfg)
    host_engines.create_engine('hermes',cfg)
    assert calls[0][0][:2] == [sys.executable,'-I']
    assert calls[1][0][:2] == ['host-python','-I']
    assert calls[0][1]['env']['WORLD_INTEL_OPENCLAW_AGENT_ID'] == 'owner'
    assert calls[1][1]['env']['WORLD_INTEL_HERMES_SOURCE_ROOT'] == 'trusted-root'
    assert calls[0][1]['engine_id']=='openclaw'
