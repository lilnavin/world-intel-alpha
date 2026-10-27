import copy
import json
import pytest
from test_grounding_review import fixture, response
from worldintel.core import grounding_review as g


def mixed():
    result, bundle = fixture()
    bundle['news'][0].update(tier=1, content=bundle['news'][0]['content'] + ' x' * 450)
    bundle['news'].append({'url': 'https://example.test/empty', 'content': '', 'tier': 1,
                           'enrichment': {'reason': 'below_content_threshold'}})
    result['briefing']['brief'][0]['sources'].append({'url': bundle['news'][1]['url']})
    return result, bundle


def test_preflight_stages_cleanup_and_captures_exact_audit():
    result, bundle = mixed()
    before = copy.deepcopy(result)
    seen = []
    def model(prompt, *args):
        assert result == before
        seen.append(json.loads(prompt.split('\nINPUT_JSON\n')[1]))
        return response(prompt)
    audit = g.review(result, bundle, review_fn=model, deadline=500, clock=lambda: 100)
    assert audit['status'] == 'accepted'
    assert result['briefing']['brief'][0]['sources'] == before['briefing']['brief'][0]['sources'][:1]
    cleanup = seen[0]['citation_preflight']
    assert cleanup['cards'][0]['original_sources'] == before['briefing']['brief'][0]['sources']
    assert cleanup['cards'][0]['retained_sources'] == result['briefing']['brief'][0]['sources']
    assert cleanup['cards'][0]['removed'][0]['source_id'] == 'source:1'
    assert cleanup['sha256'] == g._digest(json.dumps(cleanup['cards'], ensure_ascii=False, sort_keys=True))


@pytest.mark.parametrize('failure', ['reject', 'timeout', 'no-anchor', 'tier3', 'unknown', 'ambiguous', 'nonempty'])
def test_preflight_failure_is_atomic(failure):
    result, bundle = mixed()
    if failure == 'no-anchor': bundle['news'][0]['content'] = 'short'
    elif failure == 'tier3': bundle['news'][0]['tier'] = 3
    elif failure == 'unknown': bundle['news'].pop()
    elif failure == 'ambiguous': bundle['news'].append(copy.deepcopy(bundle['news'][1]))
    elif failure == 'nonempty': bundle['news'][1]['content'] = 'insufficient but present'
    before = copy.deepcopy(result)
    calls = []
    def model(prompt, *args):
        calls.append(prompt)
        if failure == 'timeout': raise TimeoutError()
        out = response(prompt)
        out['cards'][0]['verdict'] = 'unsupported'
        return out
    audit = g.review(result, bundle, review_fn=model, deadline=500, clock=lambda: 100)
    assert audit['status'] == 'hold' and result == before
    if failure in ('no-anchor', 'tier3', 'unknown', 'ambiguous'): assert not calls


def test_cleanup_requires_substantive_retained_extras_and_cleaned_anchor():
    for content in ('headline only', '<p>' + 'x' * 850 + '</p>'):
        result, bundle = mixed()
        bundle['news'].append({'url': 'https://example.test/partial', 'content': content, 'tier': 2})
        result['briefing']['brief'][0]['sources'].append({'url': bundle['news'][2]['url']})
        before = copy.deepcopy(result)
        audit = g.review(result, bundle, review_fn=lambda *a: pytest.fail('must hold before review'),
                         deadline=500, clock=lambda: 100)
        assert audit['status'] == 'hold' and result == before


@pytest.mark.parametrize('content', [0, [], {}])
def test_malformed_selected_content_is_never_pruned(content):
    result, bundle = mixed()
    bundle['news'][1]['content'] = content
    before = copy.deepcopy(result)
    audit = g.review(result, bundle, review_fn=lambda *a: pytest.fail('must hold before review'),
                     deadline=500, clock=lambda: 100)
    assert audit['status'] == 'hold' and result == before


def test_cleanup_correction_preserves_audit_citations_and_reviews_all_cards():
    from test_grounding_correction import BODY
    result, bundle = mixed()
    original_sources = copy.deepcopy(result['briefing']['brief'][0]['sources'])
    result['briefing']['brief'].append(copy.deepcopy(result['briefing']['brief'][0]))
    result['briefing']['brief'][0]['sources'].insert(0, {'url': bundle['news'][0]['url'], 'label': 'exact metadata'})
    before = copy.deepcopy(result)
    seen = []
    def reviewer(prompt, *args):
        assert result == before
        material = json.loads(prompt.split('\nINPUT_JSON\n')[1])
        seen.append(material)
        out = response(prompt)
        if len(seen) == 1: out['cards'][1]['verdict'] = 'unsupported'
        return out
    def repair(prompt, *args):
        c = json.loads(prompt.split('\nINPUT_JSON\n')[1])['cards'][0]
        return {'cards': [{'card_id': c['card_id'], 'prose_sha256': c['prose_sha256'],
                          'headline': 'Corrected', 'why_it_matters': 'Traffic can cross.', 'body': BODY}]}
    audit = g.review(result, bundle, review_fn=reviewer, repair_fn=repair, deadline=500, clock=lambda: 100)
    assert audit['status'] == 'accepted' and len(seen) == 2
    assert len(seen[1]['cards']) == 2
    assert seen[0]['citation_preflight'] == seen[1]['citation_preflight']
    assert result['briefing']['brief'][0]['sources'] == before['briefing']['brief'][0]['sources'][:2]
    assert result['briefing']['brief'][1]['sources'] == original_sources[:1]


@pytest.mark.parametrize('tier', [True, 1.9, 2.9, '1'])
def test_cleanup_anchor_requires_actual_integer_tier(tier):
    result, bundle = mixed()
    bundle['news'][0]['tier'] = tier
    before = copy.deepcopy(result)
    audit = g.review(result, bundle, review_fn=lambda *a: pytest.fail('invalid tier must hold before review'),
                     deadline=500, clock=lambda: 100)
    assert audit['status'] == 'hold' and result == before


@pytest.mark.parametrize('tier', [1, 2])
def test_cleanup_accepts_actual_integer_anchor_tiers(tier):
    result, bundle = mixed()
    bundle['news'][0]['tier'] = tier
    audit = g.review(result, bundle, review_fn=lambda prompt, *a: response(prompt),
                     deadline=500, clock=lambda: 100)
    assert audit['status'] == 'accepted'
