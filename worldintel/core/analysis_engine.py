"""Select a configured analysis transport without launching a host."""
from worldintel.core import codex_engine


class CodexEngine:
    engine_id = 'codex'

    def local_status(self):
        import shutil
        available = shutil.which('codex') is not None
        return {'available': available, 'detail': 'Codex CLI available' if available else 'Codex CLI unavailable'}

    def smoke_test(self, timeout=60):
        return codex_engine.smoke_test(timeout=timeout)

    def run_schema(self, prompt_text, schema_path, *, reasoning_effort='high', timeout=360, evidence_sink=None, pass_name=None):
        return codex_engine.run_schema(prompt_text, schema_path, reasoning_effort=reasoning_effort, timeout=timeout,
                                      **({'evidence_sink': evidence_sink, 'pass_name': pass_name} if evidence_sink is not None else {}))


def select(cfg):
    raw = cfg.raw if hasattr(cfg, 'raw') else cfg
    analysis = raw.get('analysis', {})
    kind = analysis.get('engine', 'codex')
    if kind == 'codex':
        return CodexEngine()
    if kind == 'command':
        from .command_engine import CommandEngine
        return CommandEngine(analysis.get('command'))
    if kind in {'openclaw', 'hermes'}:
        try:
            from .host_engines import create_engine
            return create_engine(kind, cfg)
        except ImportError:
            raise codex_engine.CodexError('Analysis host adapter unavailable') from None
    raise codex_engine.CodexError('Unsupported analysis engine')
