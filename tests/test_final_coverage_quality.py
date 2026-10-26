from worldintel.core import coverage_gate, source_diversity

TEXT = 'Authorities reported violence and described the response in detail. ' * 18


def story(url, outlet='The Guardian', headline='National unrest'):
    return dict(headline=headline, why_it_matters='Public safety', body=TEXT,
                sources=[dict(outlet=outlet, url=url)], tags=[], region='GLOBAL')


def test_cap_removal_has_private_identity():
    cards = [story('https://theguardian.com/' + str(n)) for n in range(3)]
    result = {'briefing': {'brief': cards}}
    audit = source_diversity.enforce_guardian_soft_cap(pool(), result)
    assert audit['removals'][0]['headline'] == 'National unrest'
    assert audit['removals'][0]['source_urls'] == ['https://theguardian.com/2']
    assert len(audit['before']) == 3 and len(audit['after']) == 2


def pool():
    return {'news': [dict(url=f'https://wire{n}.example/report', source='Wire',
                         tier=1, title='Government election', content=TEXT) for n in range(2)]}


def test_repair_cannot_bypass_shared_cap_and_gap_is_final():
    url = 'https://theguardian.com/unrest'
    bundle = pool()
    bundle['news'].append(dict(url=url, source='The Guardian', tier=1, content=TEXT))
    result = {'briefing': {'brief': [story('https://theguardian.com/first'),
                                    story('https://theguardian.com/second')],
                          'finance': {'brief': []}}}
    events = [dict(id='unrest', title='National unrest', selected_urls=[url])]
    audit = coverage_gate.enforce(result, bundle, events, repair_fn=lambda p,t: [story(url)])
    assert audit['events'][0]['status'] == 'omitted'
    assert audit['events'][0]['reason'] == 'source_policy_conflict'
    assert audit['events'][0]['stage'] == 'source_policy'
    assert result['quality_status']['overall'] == 'incomplete'
    assert len(result['briefing']['brief']) + len(result['briefing']['finance']['brief']) == 2
    assert 'Coverage gap' in result['source_notes'][-1]


def test_missing_text_records_stage_and_model_omission():
    url = 'https://wire.example/report'
    result = {'briefing': {'brief': []}}
    audit = coverage_gate.enforce(result, {'news': [dict(url=url,tier=1,content='')]},
                                  [dict(id='event', selected_urls=[url])])
    assert audit['events'][0]['stage'] == 'enrichment'
    assert audit['events'][0]['initial_reason'] == 'model_omission'
    assert result['quality_status']['overall'] == 'incomplete'


def test_capacity_is_a_visible_quality_gap():
    result = {'briefing': {'brief': []}}
    bundle = {'news': [], 'coverage_eligibility_audit': [
        dict(title='Third event', urls=['https://wire.example/third'], eligible=True,
             stage='selection', reason='excluded_by_capacity')]}
    audit = coverage_gate.enforce(result,bundle,[])
    assert audit['events'][0]['reason'] == 'excluded_by_capacity'
    assert result['quality_status']['overall'] == 'incomplete'


def test_stale_and_seen_reporting_have_selection_decisions():
    item = dict(url='https://wire.example/unrest', title='Mass shooting kills eight people', tier=1)
    for outcome, seen, reason in [('stale',False,'stale_material'), ('outside_shortlist',True,'seen_article')]:
        result = {'briefing': {'brief': []}}
        bundle = {'news': [item], 'selection_audit': [dict(url=item['url'],outcome=outcome,seen=seen)]}
        audit = coverage_gate.enforce(result,bundle,[])
        assert audit['events'][0]['reason'] == reason
        assert audit['events'][0]['stage'] == 'selection'
        assert result['quality_status']['overall'] == 'complete'


def test_capacity_exclusion_can_be_covered_by_final_ordinary_card():
    url = 'https://wire.example/fourth'
    result = {'briefing': {'brief': [story(url,'Wire')]}}
    bundle = {'news': [dict(url=url,source='Wire',tier=1,content=TEXT)],
              'coverage_eligibility_audit': [dict(title='Fourth event',urls=[url],eligible=True,
                  stage='selection',reason='excluded_by_capacity')]}
    audit = coverage_gate.enforce(result,bundle,[])
    assert audit['events'][0]['status'] == 'covered'
    assert audit['events'][0]['selection_reason'] == 'excluded_by_capacity'
    assert result['quality_status']['overall'] == 'complete'


def test_real_selection_preserves_stale_recognition_after_pool_flattening():
    from worldintel.core import select_news
    item = dict(url='https://wire.example/old',title='Mass shooting kills eight people',
                source='Wire',tier=1,published='2026-09-01T00:00:00+00:00')
    bundle = {'news':[item], 'date':'2026-10-11','as_of':'2026-10-11T07:00:00+09:00'}
    desks = select_news.select_candidates(bundle,set())
    bundle['news'] = [i for values in desks.values() for i in values]
    assert not bundle['news']
    audit = coverage_gate.enforce({'briefing':{'brief':[]}},bundle,bundle['protected_events'])
    assert any(e['reason'] == 'stale_material' for e in audit['events'])


def test_seen_selected_report_does_not_claim_unchanged_development():
    item = dict(url='https://wire.example/update',title='Mass shooting kills eight people',tier=1)
    bundle = {'news':[item], 'selection_audit':[dict(url=item['url'],outcome='selected',seen=True)]}
    audit = coverage_gate.enforce({'briefing':{'brief':[]}},bundle,[])
    assert audit['events'][0]['reason'] == 'seen_article'
    assert audit['events'][0]['selection_outcome'] == 'selected'
    assert audit['events'][0]['status'] == 'observed'


def test_finance_removal_and_three_event_final_verification():
    urls = [f'https://theguardian.com/{n}' for n in range(3)]
    result = {'briefing': {'brief': [story(u) for u in urls[:2]], 'finance': {'brief': [story(urls[2])]}}}
    bundle = pool()
    bundle['news'].extend(dict(url=u,source='The Guardian',tier=1,content=TEXT) for u in urls)
    events = [dict(id=str(n), selected_urls=[u],max_events=3) for n,u in enumerate(urls)]
    audit = coverage_gate.enforce(result,bundle,events)
    assert [e['status'] for e in audit['events']] == ['covered','covered','omitted']
    assert audit['events'][2]['source_policy_removals'][0]['section'] == 'finance'


def test_scarcity_exception_survives_final_check():
    urls = [f'https://theguardian.com/{n}' for n in range(3)]
    result = {'briefing': {'brief': [story(u) for u in urls]}}
    bundle = {'news': [dict(url=u,source='The Guardian',tier=1,content=TEXT) for u in urls]}
    events = [dict(id=str(n),selected_urls=[u],max_events=3) for n,u in enumerate(urls)]
    audit = coverage_gate.enforce(result,bundle,events)
    assert audit['source_policy']['final']['guardian_cap_overridden']
    assert result['quality_status']['overall'] == 'complete'


def test_uncorroborated_event_is_audited_without_forcing_a_card():
    from worldintel.core import coverage_policy
    item = dict(url='https://wire.example/shooting', title='Mass shooting kills eight people', tier=1)
    decisions = []
    coverage_policy.protect([item], {'world': [item]}, coverage_policy.resolve_profile({}),
                            lambda a,b: True, lambda i: 5, audit=decisions)
    assert decisions[0]['reason'] == 'lack_independent_corroboration'
    result = {'briefing': {'brief': []}}
    audit = coverage_gate.enforce(result,{'news':[item], 'coverage_eligibility_audit':decisions},[])
    assert audit['events'][0]['stage'] == 'corroboration'
    assert audit['events'][0]['status'] == 'not_eligible'


def test_three_gaps_render_when_owner_capacity_is_three():
    from worldintel.core.compose_html import render_html
    events = [dict(id=str(n),title=f'Event {n}',selected_urls=[],max_events=3) for n in range(3)]
    result = {'briefing': {'brief': []}}
    result['coverage_audit'] = coverage_gate.enforce(result,{'news':[]},events)
    assert render_html(result,{},'2026-10-11').count('Coverage gap:') == 3


def test_quality_and_private_audit_are_preserved_in_metrics():
    from worldintel.core.metrics import compute_metrics
    result = {'briefing': {'brief': []}, 'quality_status': {'overall':'incomplete'},
              'coverage_audit': {'unresolved':1}, 'analysis_status': {'overall':'complete'}}
    rec = compute_metrics({},result,'2026-10-11',{})
    assert rec['quality_status']['overall'] == 'incomplete'
    assert rec['coverage_audit']['unresolved'] == 1


def test_fallback_preserves_existing_world_story_limit():
    url = 'https://wire.example/protected'
    result = {'briefing': {'brief': [story(f'https://wire.example/{n}', 'Wire') for n in range(5)]}}
    bundle = {'news': [dict(url=url,source='Wire',tier=1,content=TEXT)]}
    audit = coverage_gate.enforce(result,bundle,[dict(id='event',selected_urls=[url])])
    assert audit['events'][0]['status'] == 'excerpt_fallback'
    assert len(result['briefing']['brief']) == 5
