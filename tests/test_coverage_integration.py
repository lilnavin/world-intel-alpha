import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from worldintel import run
from worldintel.core import select_news, codex_engine


def article(source, url):
    return {'title': 'Airliner pilot stabbed in cockpit attack on international flight',
            'summary': 'Passengers prevented an attempted crash of the aircraft.',
            'source': source, 'url': url, 'tier': 1, 'desk': 'us',
            'published': '2026-10-01T00:00:00+00:00'}


def test_corroborated_attack_protected_and_routed_within_capacity():
    news = [article('bbc-world', 'https://bbc.co.uk/news/attack'),
            article('france24', 'https://france24.com/attack')]
    for index in range(15):
        news.append({'title': f'President cabinet election sanctions energy budget ruling {index}',
                     'summary': 'Government announces major policy changes.',
                     'url': f'https://outlet{index}.example/policy', 'source': f'outlet{index}',
                     'desk': 'world', 'tier': 1, 'published': '2026-10-01T00:00:00+00:00'})
    bundle = {'news': news, 'date': '2026-10-01', 'as_of': '2026-10-01T07:00:00+09:00'}
    selected = select_news.select_candidates(bundle, set(), per_desk=3)
    assert any('cockpit attack' in x['title'] for x in selected['world'])
    assert len(selected['world']) <= 3
    assert bundle['protected_events']
    assert bundle['ranking_profile_hash']
    assert bundle['selection_audit']


def test_entrypoint_calls_final_coverage_after_diversity():
    text = Path(run.__file__).read_text(encoding='utf-8')
    assert text.index('coverage_gate.enforce(') > text.index('source_diversity.enforce_guardian_soft_cap(')
    assert 'coverage_audit' in text


def test_private_selection_audit_not_sent_to_models():
    bundle = {'news': [], 'selection_audit': [{'url': 'private'}],
              'ranking_profile': {'topics': []}, 'ranking_profile_hash': 'hash',
              'protected_events': [{'id': 'e', 'selected_urls': ['https://bbc.co.uk/a']}]}
    world = codex_engine._world_bundle(bundle)
    finance = codex_engine._finance_bundle(bundle)
    for view in (world, finance):
        assert 'selection_audit' not in view
        assert 'ranking_profile' not in view
    assert 'coverage_requirements' in world


def test_real_repair_adapter_passes_timeout_and_schema(monkeypatch):
    calls = []
    def model(prompt, schema, **kwargs):
        calls.append((prompt, schema, kwargs))
        return {'cards': [{'headline': 'test'}]}
    monkeypatch.setattr(codex_engine, 'run_schema', model)
    assert run._coverage_repair('source evidence', 12) == [{'headline': 'test'}]
    assert calls[0][2]['timeout'] == 12
    schema = json.loads(Path(calls[0][1]).read_text(encoding='utf-8'))
    assert schema['properties']['cards']['maxItems'] == 3
