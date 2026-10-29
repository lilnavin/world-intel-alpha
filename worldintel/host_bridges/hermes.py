"""Feature-guarded Hermes direct client bridge. No agent construction or bootstrap."""
import inspect
import json
import os
from pathlib import Path
import sys


def accepts(function, names):
    signature = inspect.signature(function)
    for name in names:
        param = signature.parameters.get(name)
        if param is None or param.kind not in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY):
            raise ValueError('unsupported host capability')


def complete(prompt, schema, timeout, load, split, runtime_resolver, client_resolver, *, capture_text=None):
    accepts(runtime_resolver, ['requested','explicit_api_key','explicit_base_url','target_model'])
    accepts(client_resolver, ['provider','model','explicit_base_url','explicit_api_key','api_mode','main_runtime'])
    config = load()
    if type(config).__name__ == 'FailedConfigRead' or not isinstance(config, dict):
        raise ValueError('unsupported host configuration')
    raw = config.get('model', {})
    if not isinstance(raw, dict) or not raw.get('default'):
        raise ValueError('missing host model')
    model, requested = split(raw['default'])
    requested = raw.get('provider') or requested
    if requested == 'auto':
        requested = ''
    if not isinstance(model,str) or not model.strip():
        raise ValueError('missing host model')
    runtime = runtime_resolver(requested=requested or None, target_model=model)
    provider = runtime.get('provider')
    if runtime.get('auth_error') or not provider or provider in ('auto','moa') or (requested and provider != requested):
        raise ValueError('unsupported host provider')
    if not runtime.get('api_key') or not runtime.get('base_url'):
        raise ValueError('incomplete host route')
    mode = runtime.get('api_mode')
    if mode not in ('chat_completions','codex_responses','anthropic_messages'):
        raise ValueError('unsupported host transport')
    client, resolved_model = client_resolver(provider, model=model, explicit_base_url=runtime.get('base_url'), explicit_api_key=runtime.get('api_key'), api_mode=mode, main_runtime={**runtime,'model':model})
    try:
        if resolved_model != model:
            raise ValueError('host model mismatch')
        create = client.chat.completions.create
        # Explicit keywords are required: broad **kwargs alone is not a capability proof.
        accepts(create, ['model','messages','stream','timeout','max_tokens'])
        format_contract = ('Return only one JSON object conforming to the following JSON Schema. '
                           'Do not include Markdown fences or any additional text.\n'
                           + json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False))
        kwargs = dict(model=model, messages=[{'role':'system','content':format_contract},
                                            {'role':'user','content':prompt}],
                      stream=False, timeout=timeout, max_tokens=16384)
        # Unknown provider schema capabilities: omit the hint before the sole call.
        # The parent always validates the call-specific schema.
        response = create(**kwargs)
        # Preserve SDK-decoded actual message text, not reconstructed provider HTTP.
        choices = getattr(response, 'choices', None)
        if choices:
            received_text = getattr(getattr(choices[0], 'message', None), 'content', None)
            if isinstance(received_text, str) and capture_text is not None:
                capture_text(received_text)
        if getattr(response,'model',None) != model or len(response.choices) != 1:
            raise ValueError('host response mismatch')
        choice = response.choices[0]
        message = choice.message
        if choice.finish_reason not in ('stop','end_turn') or getattr(message,'tool_calls',None) or getattr(message,'function_call',None):
            raise ValueError('unsupported host response')
        text = message.content
        if not isinstance(text,str) or not text.strip():
            raise ValueError('empty host response')
        return text
    finally:
        close = getattr(client,'close',None)
        if callable(close):
            close()


def main():
    try:
        source = os.environ.get('WORLD_INTEL_HERMES_SOURCE_ROOT')
        if source:
            sys.path.insert(0, source)
        from hermes_cli.left_core_migration import installs_held
        timeout = float(os.environ['WORLD_INTEL_TIMEOUT_S'])
        if timeout <= 0:
            raise ValueError()
        schema = json.loads(Path(os.environ['WORLD_INTEL_SCHEMA_PATH']).read_text(encoding='utf-8'))
        prompt = sys.stdin.buffer.read().decode('utf-8')
        with installs_held():
            from hermes_cli.config import load_config_readonly, split_model_config_default
            from hermes_cli.runtime_provider import resolve_runtime_provider
            from agent.auxiliary_client import resolve_provider_client
            output = Path(os.environ['WORLD_INTEL_OUTPUT_PATH'])
            complete(prompt, schema, timeout, load_config_readonly, split_model_config_default, resolve_runtime_provider, resolve_provider_client, capture_text=lambda text: output.write_bytes(text.encode('utf-8')))
        return 0
    except Exception:
        sys.stderr.write('Native host completion failed or is unsupported.\n')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
