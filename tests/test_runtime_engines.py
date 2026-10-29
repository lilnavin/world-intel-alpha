import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from test_dry_isolation import offline, TODAY
from worldintel import cli, config, run
from worldintel.core import analysis_engine


def cfg_with(synthetic_config, analysis):
    raw = synthetic_config.raw.copy()
    raw['analysis'] = analysis
    return config.Config(raw, synthetic_config.source_path)


@pytest.mark.parametrize('analysis', [
    {'engine': 'command', 'command': ['trusted-host', 'arg with spaces']},
    {'engine': 'openclaw', 'openclaw': {'gateway_url': 'http://127.0.0.1:18789'}},
    {'engine': 'hermes', 'hermes': {'python': sys.executable, 'source_root': '/trusted/host'}},
])
def test_config_accepts_explicit_host_settings(synthetic_config, analysis):
    cfg = cfg_with(synthetic_config, analysis)
    assert not cfg.warnings
    assert cfg.raw['analysis']['engine'] == analysis['engine']


@pytest.mark.parametrize('analysis, key', [
    ({'engine':'command'}, 'analysis.command'),
    ({'engine':'command','command':[]}, 'analysis.command'),
    ({'engine':'command','command':['ok', 1]}, 'analysis.command'),
    ({'engine':'command','command':['']}, 'analysis.command'),
    ({'engine':'hermes'}, 'analysis.hermes.python'),
    ({'engine':'hermes','hermes':{'python':'ok','model':'secret'}}, 'analysis.hermes.model'),
    ({'engine':'openclaw'}, 'analysis.openclaw.gateway_url'),
    ({'engine':'openclaw','openclaw':{'gateway_url':'https://example.com','token_env':'bad-name'}}, 'analysis.openclaw.token_env'),
    ({'engine':'openclaw','openclaw':{'gateway_url':'http://remote.example'}}, 'analysis.openclaw.gateway_url'),
    ({'engine':'codex','api_key':'secret'}, 'analysis.api_key'),
])
def test_config_rejects_invalid_or_unknown_engine_settings(synthetic_config, analysis, key):
    with pytest.raises(config.ConfigError, match=key):
        cfg_with(synthetic_config, analysis)


def test_doctor_checks_selected_engine_without_codex_or_model(synthetic_config, tmp_path, monkeypatch, capsys):
    raw = synthetic_config.raw.copy()
    raw['analysis'] = {'engine':'command','command':['synthetic-host']}
    profile = tmp_path / 'profile.yaml'
    profile.write_text(yaml.safe_dump(raw), encoding='utf-8')
    engine = SimpleNamespace(engine_id='command', local_status=lambda: {'available':True,'detail':'local configuration ready'}, smoke_test=lambda **k: pytest.fail('model probe forbidden'))
    monkeypatch.setattr(analysis_engine, 'select', lambda cfg: engine)
    monkeypatch.setattr(cli.importlib, 'import_module', lambda n: SimpleNamespace(__version__='synthetic'))
    monkeypatch.setattr(cli.shutil, 'which', lambda n: pytest.fail('Codex probe forbidden'))
    assert cli.main(['doctor','--config',str(profile)]) == 0
    assert 'command' in capsys.readouterr().out


@pytest.mark.parametrize('nonce', ['a'*32, '', 'A'*32, 'bad'])
def test_runtime_copies_only_valid_helper_nonce(offline, monkeypatch, nonce):
    root, *_ = offline
    monkeypatch.setenv('WORLD_INTEL_AGENT_INVOCATION_ID', nonce)
    monkeypatch.setattr(sys, 'argv', ['run.py','--force'])
    assert run.main() == 0
    receipt = json.loads((root / 'run-status.json').read_text())
    if nonce == 'a'*32:
        assert receipt['agent_invocation_id'] == nonce
    else:
        assert 'agent_invocation_id' not in receipt

REAL_TWO_PASS = run.codex_engine.run_two_pass


def use_fake_engine(monkeypatch, kind='hermes', healthy=True):
    cfg = config.get()
    analysis = {'engine':kind, 'source_review_enabled':True, 'source_review_repair_enabled':True}
    analysis.update({'hermes':{'python':sys.executable}} if kind == 'hermes' else
                    {'openclaw':{'gateway_url':'http://127.0.0.1:18789'}} if kind == 'openclaw' else {'command':['fake']})
    config.set_for_test(cfg_with(cfg, analysis))
    calls = []
    def model(prompt, schema, **kwargs):
        calls.append(kwargs)
        if kwargs['pass_name'] == 'coverage-repair':
            return {'cards':[]}
        return {'briefing':{}}
    engine = SimpleNamespace(engine_id=kind, run_schema=model, smoke_test=lambda **kw: healthy)
    monkeypatch.setattr(analysis_engine, 'select', lambda cfg: engine)
    monkeypatch.setattr(run.codex_engine, 'run_two_pass', REAL_TWO_PASS)
    for name in ('smoke_test','run_schema','_available'):
        if hasattr(run.codex_engine, name):
            monkeypatch.setattr(run.codex_engine,name,lambda *a, **k: pytest.fail('unexpected Codex call'))
    return calls


@pytest.mark.parametrize('kind',['command','hermes','openclaw'])
def test_all_model_phases_use_one_selected_engine(offline, monkeypatch, kind):
    root, _, _, _, renderer, _ = offline
    calls = use_fake_engine(monkeypatch, kind)
    old = {'last_ok':'old-date', 'status':{'overall':'old'}}
    run.kb.ensure_kb(str(root))
    run.kb.write_state(str(root), {'codex':old})
    monkeypatch.setattr(run.quality_gate,'scan',lambda result: [1])
    def rewrite(result, flagged, model, schema):
        model('rewrite',schema)
        return 1
    monkeypatch.setattr(run.quality_gate,'rewrite',rewrite)
    def coverage(result, bundle, events, *, repair_fn, deadline):
        assert deadline > run.time.monotonic()
        repair_fn('coverage',5)
        result['quality_status'] = {'overall':'complete'}
        return {'events':[]}
    monkeypatch.setattr(run.coverage_gate,'enforce',coverage)
    def review(result,bundle,*,review_fn,repair_fn,deadline):
        review_fn('initial','schema',4)
        repair_fn('correction','schema',3)
        review_fn('final','schema',2)
        return {'status':'accepted'}
    monkeypatch.setattr(run.grounding_review,'review',review)
    monkeypatch.setattr(sys,'argv',['run.py','--force'])
    assert run.main() == 0
    assert [c['pass_name'] for c in calls] == ['world','finance','writing-rewrite','coverage-repair','source-review','source-correction','source-review-final']
    assert all(c['evidence_sink'] is calls[0]['evidence_sink'] for c in calls)
    assert renderer.call_count == 1
    receipt=json.loads((root/'run-status.json').read_text())
    assert receipt['analysis_engine'] == kind
    assert receipt['analysis']['overall'] == 'complete'
    assert receipt['analysis']['passes']['finance']['success'] is True
    state=run.kb.read_state(str(root))
    assert state['codex'] == old
    assert state['analysis_engine']['engine_id'] == kind
    metrics=[json.loads(line) for line in (root/'metrics'/'runs.jsonl').read_text().splitlines()]
    assert metrics[-1]['analysis_engine'] == kind
    assert metrics[-1]['codex_ok'] is None


@pytest.mark.parametrize('kind',['command','hermes','openclaw'])
def test_selected_engine_unavailable_message_and_no_analysis(offline,monkeypatch,capsys,kind):
    root, _, fetch, _, renderer, _ = offline
    calls=use_fake_engine(monkeypatch,kind,healthy=False)
    monkeypatch.setattr(sys,'argv',['run.py','--force'])
    assert run.main() == 0
    output=capsys.readouterr().out
    assert kind in output and 'Fix:' in output
    assert 'codex login' not in output
    assert not calls and not fetch.called and not renderer.called
    receipt=json.loads((root/'run-status.json').read_text())
    assert receipt['generation']=='failed' and receipt['artifact'] is None


@pytest.mark.parametrize('dry',[False,True])
def test_selected_host_late_hold_blocks_kb_render_handoff(offline,monkeypatch,capsys,dry):
    root, media, _, _, renderer, _ = offline
    use_fake_engine(monkeypatch)
    writers=[]
    for name in ('apply_deltas','update_scoreboard','append_scoreboard_history','write_daily','write_state'):
        writer=Mock(wraps=getattr(run.kb,name));monkeypatch.setattr(run.kb,name,writer);writers.append(writer)
    def review(*a,review_fn,repair_fn,**k):
        review_fn('initial','schema',4)
        repair_fn('correction','schema',3)
        review_fn('final','schema',2)
        return {'status':'hold'}
    monkeypatch.setattr(run.grounding_review,'review',review)
    monkeypatch.setattr(sys,'argv',['run.py','--force']+(['--dry'] if dry else []))
    assert run.main() == (1 if dry else 0)
    assert not renderer.called and not media.exists()
    assert not any(w.called for w in writers)
    assert 'MEDIA:' not in capsys.readouterr().out
    if not dry:
        assert json.loads((root/'run-status.json').read_text())['generation']=='held'


def test_helper_verifies_receipt_from_real_runtime(offline,monkeypatch,tmp_path,capsys):
    from worldintel import agent_run
    cfg=config.get()
    profile=tmp_path/'profile.yaml'
    profile.write_text(yaml.safe_dump(cfg.raw),encoding='utf-8')
    original=profile.read_bytes()
    # Source collection and model are offline fixtures; pipeline/receipts/artifact
    # and helper verification are real, rather than hand-written fake receipts.
    def coverage(result,*a,**k):
        result['quality_status']={'overall':'complete'}
        return {'events':[]}
    monkeypatch.setattr(run.coverage_gate,'enforce',coverage)
    def invoke(private,force,env,diagnostics):
        with monkeypatch.context() as context:
            context.setenv('WORLD_INTEL_AGENT_INVOCATION_ID',env['WORLD_INTEL_AGENT_INVOCATION_ID'])
            context.setattr(sys,'argv',['run.py','--force'])
            context.setattr(config,'_CACHE',config.load(private))
            return run.main()
    monkeypatch.setattr(agent_run,'_invoke',invoke)
    assert agent_run.main(['--config',str(profile),'--force']) == 0
    result=json.loads(capsys.readouterr().out)
    assert result['outcome']=='generated' and result['delivery_confirmed'] is False
    assert Path(result['artifact']['path']).is_file()
    root=Path(cfg.data_root)
    receipt=json.loads((root/'run-status.json').read_text())
    assert len(receipt['agent_invocation_id'])==32
    side=json.loads(Path(result['artifact']['path']).with_suffix('.status.json').read_text())
    assert side['agent_invocation_id']==receipt['agent_invocation_id']
    assert profile.read_bytes()==original


@pytest.mark.parametrize('kind',['codex','command','hermes','openclaw'])
def test_metrics_preserve_codex_meaning(kind):
    status={'overall':'complete','passes':{n:{'success':True} for n in ('world','finance')}}
    rec=run.metrics.compute_metrics({}, {'analysis_engine':kind,'analysis_status':status}, TODAY)
    assert rec['codex_ok'] is (True if kind=='codex' else None)
    if kind != 'codex':
        assert rec['analysis_engine']==kind

@pytest.mark.parametrize('mutation',['other_nonce','stale','hold'])
def test_helper_refuses_other_stale_or_held_real_runtime(offline,monkeypatch,tmp_path,capsys,mutation):
    from worldintel import agent_run
    cfg=config.get()
    profile=tmp_path/'profile.yaml'
    profile.write_text(yaml.safe_dump(cfg.raw),encoding='utf-8')
    def coverage(result,*a,**k):
        result['quality_status']={'overall':'complete'}
        return {'events':[]}
    monkeypatch.setattr(run.coverage_gate,'enforce',coverage)
    if mutation=='hold':
        cfg.raw.setdefault('analysis',{})['source_review_enabled']=True
        profile.write_text(yaml.safe_dump(cfg.raw),encoding='utf-8')
        monkeypatch.setattr(run.grounding_review,'review',lambda *a,**k:{'status':'hold'})
    def invoke(private,force,env,diagnostics):
        with monkeypatch.context() as context:
            nonce='b'*32 if mutation=='other_nonce' else env['WORLD_INTEL_AGENT_INVOCATION_ID']
            context.setenv('WORLD_INTEL_AGENT_INVOCATION_ID',nonce)
            context.setattr(sys,'argv',['run.py','--force'])
            context.setattr(config,'_CACHE',config.load(private))
            if mutation=='stale':
                context.setattr(run,'local_now',lambda: run.datetime.datetime(2000,1,1,tzinfo=run.datetime.timezone.utc))
            return run.main()
    monkeypatch.setattr(agent_run,'_invoke',invoke)
    assert agent_run.main(['--config',str(profile),'--force','--emit-media']) == 1
    output=capsys.readouterr().out
    assert 'MEDIA:' not in output
    assert 'artifact' not in json.loads(output)


def test_host_budget_exhaustion_skips_finance_and_holds_before_persistence(offline,monkeypatch):
    root, media, _, _, renderer, _ = offline
    calls=use_fake_engine(monkeypatch)
    clock=[100.0]
    monkeypatch.setattr(run.time,'monotonic',lambda:clock[0])
    engine=analysis_engine.select(config.get())
    original=engine.run_schema
    def model(*args,**kwargs):
        response=original(*args,**kwargs)
        if kwargs['pass_name']=='world':
            clock[0]+=2000
        return response
    engine.run_schema=model
    monkeypatch.setattr(run.grounding_review,'review',lambda *a,**k:{'status':'hold'})
    monkeypatch.setattr(sys,'argv',['run.py','--force'])
    assert run.main()==0
    assert [call['pass_name'] for call in calls]==['world']
    receipt=json.loads((root/'run-status.json').read_text())
    assert receipt['analysis']['overall']=='partial'
    assert receipt['analysis']['passes']['finance']['skipped'] is True
    assert receipt['generation']=='held' and receipt['artifact'] is None
    assert not renderer.called and not media.exists()
    assert not (root/'state.json').exists()
