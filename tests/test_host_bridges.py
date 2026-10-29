import importlib
import json
from types import SimpleNamespace
import pytest


def bridge(name):
    return importlib.import_module('worldintel.host_bridges.' + name)

@pytest.mark.parametrize('url', ['http://example.com', 'https://u:p@host', 'https://host/?token=x', 'https://host/#x'])
def test_openclaw_refuses_unsafe_urls(url):
    with pytest.raises(ValueError):
        bridge('openclaw').endpoint(url)

def test_openclaw_keeps_exact_tool_text():
    text = '{ "answer": 1 }\n'
    raw = json.dumps({'ok': True, 'result': {'content': [{'type': 'text', 'text': text}], 'details': {'json': {'answer': 1}}}}).encode()
    assert bridge('openclaw').extract(raw) == text

@pytest.mark.parametrize('result', [{}, {'content': []}, {'content': [{'type':'text','text':'{}'}, {'type':'text','text':'{}'}]}, {'content':[{'type':'text','text':'{}'}], 'details':{'json':{'x':1}}}])
def test_openclaw_ambiguous_or_conflicting_result_fails(result):
    with pytest.raises(ValueError):
        bridge('openclaw').extract(json.dumps({'ok':True,'result':result}).encode())

@pytest.mark.parametrize('mode',['chat_completions','codex_responses','anthropic_messages'])
@pytest.mark.parametrize('schema',[{'type':'object','required':['world'], 'properties':{'world':{'const':'\u03a9'}}},{'type':'object','required':['review'], 'properties':{'review':{'enum':['accepted','held']}}}])
def test_hermes_preserves_configured_provider_and_model(mode,schema):
    m = bridge('hermes')
    calls = []
    prompt = 'literal \u03a9\r\n' * 30000
    def runtime(*, requested=None, explicit_api_key=None, explicit_base_url=None, target_model=None):
        calls.append((requested,target_model))
        return {'provider':'chosen','api_mode':mode,'api_key':'private','base_url':'https://host'}
    def resolve(provider, model=None, async_mode=False, raw_codex=False, explicit_base_url=None, explicit_api_key=None, api_mode=None, main_runtime=None, is_vision=False, task=None):
        assert provider == 'chosen' and model == 'model'
        def create(*, model, messages, stream, timeout, max_tokens):
            assert messages == [
                {'role':'system','content':'Return only one JSON object conforming to the following JSON Schema. Do not include Markdown fences or any additional text.\n' + json.dumps(schema,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)},
                {'role':'user','content':prompt}]
            assert messages[1]['content'].encode('utf-8') == prompt.encode('utf-8')
            calls.append('completion')
            return SimpleNamespace(model=model, choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='{ "x": 1 }',tool_calls=None,function_call=None))])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),close=lambda:None), model
    assert m.complete(prompt, schema, 4, lambda:{'model':{'default':'chosen/model'}}, lambda value:('model','chosen'), runtime, resolve) == '{ "x": 1 }'
    assert calls == [('chosen','model'),'completion']

def test_hermes_signature_drift_prevents_completion():
    m = bridge('hermes')
    with pytest.raises(ValueError):
        m.complete('p',{},1,lambda:{'model':{'default':'x'}},lambda x:('x',''),lambda: {},lambda:None)

def test_hermes_rejects_normalized_config_provider_substitution_before_client():
    m=bridge('hermes')
    def runtime(*,requested=None,explicit_api_key=None,explicit_base_url=None,target_model=None):
        return {'provider':'wrong','api_mode':'chat_completions'}
    def resolve(provider, model=None, explicit_base_url=None, explicit_api_key=None, api_mode=None, main_runtime=None):
        pytest.fail('provider substitution reached client')
    with pytest.raises(ValueError):
        m.complete('p',{},1,lambda:{'model':{'default':'x','provider':'chosen'}},lambda x:('x',''),runtime,resolve)

@pytest.mark.parametrize('tool_error',[False,True])
def test_openclaw_request_uses_exact_stdin_schema_header_and_raw_capture(monkeypatch,tmp_path, capsys,tool_error):
    import io
    m=bridge('openclaw')
    prompt=('large O\r\n'*30000)
    schema={'type':'object'}
    schema_path=tmp_path/'schema.json'; schema_path.write_text(json.dumps(schema))
    output=tmp_path/'output.json'
    for key,value in {'WORLD_INTEL_TIMEOUT_S':'2','WORLD_INTEL_SCHEMA_PATH':str(schema_path),'WORLD_INTEL_OUTPUT_PATH':str(output),'WORLD_INTEL_OPENCLAW_GATEWAY_URL':'http://127.0.0.1:1234','OPENCLAW_GATEWAY_TOKEN':'fake-secret','WORLD_INTEL_OPENCLAW_AGENT_ID':'owner'}.items():
        monkeypatch.setenv(key,value)
    raw=json.dumps({'ok':True,'result':{'isError':tool_error,'content':[{'type':'text','text':'{ "x": 1 }'}]}}).encode()
    class Response(io.BytesIO):
        pass
    class Opener:
        def open(self,request,timeout):
            body=json.loads(request.data)
            assert body=={'tool':'llm-task','agentId':'owner','args':{'prompt':prompt,'input':{},'schema':schema,'timeoutMs':2000}}
            assert request.get_header('Authorization')=='Bearer fake-secret'
            assert timeout==2
            return Response(raw)
    monkeypatch.setattr(m,'build_opener',lambda *handlers: Opener())
    monkeypatch.setattr(m.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(prompt.encode())))
    capture=io.BytesIO(); monkeypatch.setattr(m.sys,'stdout',SimpleNamespace(buffer=capture))
    assert m.main()==(1 if tool_error else 0)
    if tool_error:
        assert not output.exists()
    else:
        assert output.read_bytes()==b'{ "x": 1 }'
    assert capture.getvalue()==raw


def test_openclaw_failure_never_exposes_sdk_secret(monkeypatch,tmp_path,capsys):
    import io
    m=bridge('openclaw')
    schema=tmp_path/'schema';schema.write_text('{}')
    for key,value in {'WORLD_INTEL_TIMEOUT_S':'1','WORLD_INTEL_SCHEMA_PATH':str(schema),'WORLD_INTEL_OUTPUT_PATH':str(tmp_path/'out'),'WORLD_INTEL_OPENCLAW_GATEWAY_URL':'https://host','OPENCLAW_GATEWAY_TOKEN':'private-key'}.items():
        monkeypatch.setenv(key,value)
    class Opener:
        def open(self,*a,**kw):
            raise RuntimeError('SDK leaked private-key')
    monkeypatch.setattr(m,'build_opener',lambda *a:Opener())
    monkeypatch.setattr(m.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(b'p')))
    assert m.main()==1
    assert 'private-key' not in capsys.readouterr().err
    assert not (tmp_path/'out').exists()

def test_openclaw_http_error_body_is_private_capture(monkeypatch,tmp_path):
    import io
    from urllib.error import HTTPError
    m=bridge('openclaw')
    schema=tmp_path/'schema'; schema.write_text('{}')
    for key,value in {'WORLD_INTEL_TIMEOUT_S':'1','WORLD_INTEL_SCHEMA_PATH':str(schema),'WORLD_INTEL_OUTPUT_PATH':str(tmp_path/'out'),'WORLD_INTEL_OPENCLAW_GATEWAY_URL':'https://host','OPENCLAW_GATEWAY_TOKEN':'fake'}.items():
        monkeypatch.setenv(key,value)
    class Opener:
        def open(self,*a,**kw):
            raise HTTPError('https://host',403,'forbidden',{},io.BytesIO(b'actual failure body'))
    monkeypatch.setattr(m,'build_opener',lambda *a:Opener())
    monkeypatch.setattr(m.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(b'p')))
    captured=io.BytesIO();monkeypatch.setattr(m.sys,'stdout',SimpleNamespace(buffer=captured))
    assert m.main()==1
    assert captured.getvalue()==b'actual failure body'

@pytest.mark.parametrize('missing',['api_key','base_url'])
def test_hermes_missing_auth_or_endpoint_fails_before_client(missing):
    m=bridge('hermes')
    def runtime(*,requested=None,explicit_api_key=None,explicit_base_url=None,target_model=None):
        route={'provider':'chosen','api_mode':'chat_completions','api_key':'fake','base_url':'https://host'}
        route.pop(missing)
        return route
    def resolve(provider,model=None,explicit_base_url=None,explicit_api_key=None,api_mode=None,main_runtime=None):
        pytest.fail('incomplete route reached client')
    with pytest.raises(ValueError):
        m.complete('p',{},1,lambda:{'model':{'default':'x'}},lambda x:('x',''),runtime,resolve)

@pytest.mark.parametrize('failure',['tool_calls','length','wrong_model','empty','resolved_model','transport','sdk'])
def test_hermes_failures_never_retry_or_fallback(failure):
    m=bridge('hermes');calls=[]
    def runtime(*,requested=None,explicit_api_key=None,explicit_base_url=None,target_model=None):
        return {'provider':'chosen','api_mode':'external_process' if failure=='transport' else 'chat_completions','api_key':'fake','base_url':'https://host'}
    def resolve(provider,model=None,explicit_base_url=None,explicit_api_key=None,api_mode=None,main_runtime=None):
        def create(*,model,messages,stream,timeout,max_tokens):
            calls.append('completion')
            if failure=='sdk':
                raise RuntimeError('private sdk secret')
            return SimpleNamespace(model='wrong' if failure=='wrong_model' else model,choices=[SimpleNamespace(finish_reason='length' if failure=='length' else 'stop',message=SimpleNamespace(content='' if failure=='empty' else '{}',tool_calls=['tool'] if failure=='tool_calls' else None))])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),close=lambda:calls.append('closed')), 'wrong' if failure=='resolved_model' else model
    with pytest.raises((ValueError,RuntimeError)):
        m.complete('p',{},1,lambda:{'model':{'default':'x'}},lambda x:('x',''),runtime,resolve)
    assert calls.count('completion') <= 1
    if failure!='transport':
        assert calls[-1]=='closed'


def test_bridge_sources_have_no_agent_or_bootstrap_dependencies():
    from pathlib import Path
    source=Path(bridge('hermes').__file__).read_text()
    for forbidden in ('import run_agent','import AIAgent','import call_llm','load_hermes_dotenv','resolve_runtime_with_fallback','subprocess','jsonschema','from worldintel'):
        assert forbidden not in source

@pytest.mark.parametrize('boundary',['direct','common'])
@pytest.mark.parametrize('sdk_failure',['success','sdk','length','wrong_model','tool_calls'])
def test_hermes_standalone_entry_uses_fake_host_under_install_hold(monkeypatch,tmp_path,capsys,sdk_failure,boundary):
    import io
    import sys
    from contextlib import contextmanager
    from types import ModuleType
    m=bridge('hermes');state={'held':False,'calls':0}
    prompt = 'stdin \u03a9\r\n' * 30000
    expected_prompt = {'text':prompt}
    @contextmanager
    def held():
        state['held']=True
        try: yield
        finally: state['held']=False
    def load():
        assert state['held']
        return {'model':{'default':'configured','provider':'owner'}}
    def runtime(*,requested=None,explicit_api_key=None,explicit_base_url=None,target_model=None):
        assert state['held'] and requested=='owner' and target_model=='configured'
        return {'provider':'owner','api_mode':'chat_completions','api_key':'fake-secret','base_url':'https://host'}
    def resolve(provider,model=None,explicit_base_url=None,explicit_api_key=None,api_mode=None,main_runtime=None):
        assert state['held'] and provider=='owner'
        def create(*,model,messages,stream,timeout,max_tokens):
            state['calls']+=1
            assert messages[-1] == {'role':'user','content':expected_prompt['text']}
            assert messages[-1]['content'].encode('utf-8') == expected_prompt['text'].encode('utf-8')
            assert messages[0]['role'] == 'system'
            assert json.loads(messages[0]['content'].split('\n',1)[1]) == {}
            if sdk_failure == 'sdk': raise RuntimeError('fake-secret provider internal failure')
            return SimpleNamespace(model='wrong' if sdk_failure == 'wrong_model' else model,choices=[SimpleNamespace(finish_reason='length' if sdk_failure == 'length' else 'stop',message=SimpleNamespace(content='{ "exact": true }\r\n',tool_calls=['tool'] if sdk_failure == 'tool_calls' else None))])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),close=lambda:None),model
    modules={'hermes_cli.left_core_migration':{'installs_held':held},'hermes_cli.config':{'load_config_readonly':load,'split_model_config_default':lambda raw:(raw,'')},'hermes_cli.runtime_provider':{'resolve_runtime_provider':runtime},'agent.auxiliary_client':{'resolve_provider_client':resolve}}
    for name,attrs in modules.items():
        module=ModuleType(name);module.__dict__.update(attrs);monkeypatch.setitem(sys.modules,name,module)
    schema=tmp_path/'schema';schema.write_text('{}');output=tmp_path/'output'
    for key,value in {'WORLD_INTEL_TIMEOUT_S':'2','WORLD_INTEL_SCHEMA_PATH':str(schema),'WORLD_INTEL_OUTPUT_PATH':str(output)}.items():monkeypatch.setenv(key,value)
    monkeypatch.setattr(m.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(prompt.encode('utf-8'))))
    if boundary == 'direct':
        assert m.main()==(0 if sdk_failure == 'success' else 1)
        actual_output = output.read_bytes() if output.exists() else None
    else:
        from subprocess import CompletedProcess
        from worldintel.core import command_engine, codex_engine
        records=[]
        def execute(argv, *, stdin, timeout, env, deadline=None):
            for key,value in env.items(): monkeypatch.setenv(key,value)
            expected_prompt['text'] = stdin.read().decode('utf-8')
            stdin.seek(0)
            monkeypatch.setattr(m.sys,'stdin',SimpleNamespace(buffer=stdin))
            diagnostics=io.StringIO()
            monkeypatch.setattr(m.sys,'stderr',diagnostics)
            code=m.main()
            return CompletedProcess(argv,code,b'',diagnostics.getvalue().encode())
        monkeypatch.setattr(command_engine,'_execute',execute)
        engine=command_engine.CommandEngine(['synthetic-host'],engine_id='hermes')
        if sdk_failure == 'success':
            assert engine.run_schema(prompt,str(schema),evidence_sink=records.append)=={'exact':True}
        else:
            with pytest.raises(codex_engine.CodexError) as error:
                engine.run_schema(prompt,str(schema),evidence_sink=records.append)
            assert 'fake-secret' not in str(error.value)
            assert records[0]['returncode']==1 and records[0]['outcome']=='nonzero'
        actual_output=records[0]['raw_output']
        assert b'fake-secret' not in records[0]['stderr']
    assert state['calls']==1 and not state['held']
    assert 'fake-secret' not in capsys.readouterr().err
    if sdk_failure == 'sdk': assert actual_output is None
    else: assert actual_output==b'{ "exact": true }\r\n'


def test_openclaw_rejects_tool_level_error_with_schema_valid_json():
    raw=json.dumps({'ok':True,'result':{'isError':True,'content':[{'type':'text','text':'{}'}]}}).encode()
    with pytest.raises(ValueError):
        bridge('openclaw').extract(raw)
