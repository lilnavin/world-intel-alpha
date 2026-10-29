"""Native adapters share the generic owned subprocess/evidence boundary."""
from pathlib import Path
import sys

from .command_engine import CommandEngine
from .codex_engine import CodexError


def create_engine(kind, cfg):
    raw = cfg.raw if hasattr(cfg, 'raw') else cfg
    settings = raw.get('analysis', {}).get(kind, {})
    bridge = Path(__file__).resolve().parents[1] / 'host_bridges' / (kind + '.py')
    if kind == 'openclaw':
        try:
            url = settings['gateway_url']
            if not isinstance(url, str) or not url.strip():
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise CodexError('Native host configuration is unsupported') from None
        env = {'WORLD_INTEL_OPENCLAW_GATEWAY_URL':url, 'WORLD_INTEL_OPENCLAW_TOKEN_ENV':settings.get('token_env','OPENCLAW_GATEWAY_TOKEN')}
        if settings.get('agent_id'):
            env['WORLD_INTEL_OPENCLAW_AGENT_ID'] = settings['agent_id']
        python = sys.executable
    elif kind == 'hermes':
        python = settings.get('python')
        if not isinstance(python,str) or not python.strip():
            raise CodexError('Native host configuration is unsupported')
        env = {}
        if settings.get('source_root'):
            env['WORLD_INTEL_HERMES_SOURCE_ROOT'] = settings['source_root']
    else:
        raise CodexError('Native host engine is unsupported')
    return CommandEngine([python, '-I', str(bridge)], engine_id=kind, env=env)
