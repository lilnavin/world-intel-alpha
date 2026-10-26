"""Synthetic regressions for the bounded live-quality correction."""

import pytest

from worldintel.core import coverage_gate, coverage_policy, enrich, fetch_sources


TEXT = 'Authorities described the incident and confirmed the public safety response in detail. ' * 16


def report(n, title, summary='', **extra):
    return dict(url=f'https://wire{n}.example.org/news/report', source=f'Wire {n}',
                tier=1, desk='world', title=title, summary=summary, **extra)


def card(item, body=TEXT):
    return dict(headline=item['title'], why_it_matters='Public safety response', body=body,
                sources=[dict(outlet=item['source'], url=item['url'])], tags=[], region='GLOBAL')


def protect(rows):
    return coverage_policy.protect(rows, {'world': rows[:6]}, coverage_policy.resolve_profile(),
                                   fetch_sources.story_matcher(rows, min_ratio=0), lambda i: 5)[1]


def test_dense_current_airport_reports_join_without_rare_headline_tokens():
    a = report(1, 'Twelve killed and hundreds injured in attack at Riverton airport',
               'Authorities confirm the missile attack on Saturday.')
    b = report(2, 'Missile strike on airport in capital Riverton kills 12 people',
               'Earlier this week a bombing killed four people at another site.')
    # Every incident title anchor is common in the whole pool; filler is not acute.
    filler = [report(n + 10, 'Riverton airport missile attack safety training',
                     'Routine security exercise, no injuries.') for n in range(30)]
    events = protect([a, b, *filler])
    assert any(set(e['urls']) == {a['url'], b['url']} for e in events)


@pytest.mark.parametrize('second', [
    'Missile strike on Riverton airport kills four people on Thursday',
    'Missile strike on Westhaven airport kills twelve people',
    'Bombing at Riverton market kills twelve people',
])
def test_dense_identity_keeps_earlier_other_location_and_mechanism_separate(second):
    a = report(1, 'Missile attack at Riverton airport kills twelve people on Saturday')
    b = report(2, second)
    assert not protect([a, b])


def test_same_city_named_venues_are_not_identity_even_with_equal_casualties():
    a = report(1, 'Mass shooting kills eight people at Amber Mall', 'Police report deaths in Riverton.')
    b = report(2, 'Mass shooting kills eight people at Cobalt Mall', 'Police report deaths in Riverton.')
    assert not protect([a, b])


@pytest.mark.parametrize('title', ['Investment conference opens in Riverton', 'Economic summit studies airport expansion'])
def test_unrelated_reporting_cannot_borrow_acute_eligibility_from_background(title):
    item = report(1, title, 'The airport attack on Thursday killed four people. Delegates discussed business confidence.')
    assert not coverage_policy.recognition_reason(item)


def test_material_investigation_and_updated_current_title_survive_old_url_slug():
    investigation = report(1, 'ExampleAir co-pilot planned airport attack, prosecutors say',
                           'Investigators announced new findings after last week\'s attempted hijacking.')
    current = report(2, 'Twelve killed in Riverton airport attack', 'Authorities confirm casualties.')
    current['url'] = 'https://wire.example.org/old-border-campaign'
    assert coverage_policy.recognition_reason(investigation) == 'aviation_security'
    assert coverage_policy.recognition_reason(current) == 'aviation_security'


def test_failed_slot_recovers_despite_usable_guardian_and_prefers_wire():
    good = report(1, 'Airport attack', content=TEXT)
    good['source'] = 'The Guardian'
    failed = report(2, 'Airport attack', content='')
    guardian = report(3, 'Airport attack', raw_body=TEXT)
    guardian['source'] = 'The Guardian'
    wire = report(4, 'Airport attack', raw_body=TEXT)
    selected = {'world': [good, failed]}
    event = dict(urls=[i['url'] for i in [good, failed, guardian, wire]],
                 selected_urls=[good['url'], failed['url']])
    enrich.enrich_protected_alternates([good, failed, guardian, wire], selected, [event])
    assert selected['world'] == [good, wire]
    assert event['selected_urls'] == [good['url'], wire['url']]
    assert wire['content'] == TEXT.strip()


def test_leading_structural_controls_are_removed_without_censoring_reporting():
    controls = 'Advertisement. Cookie settings. Accept all cookies. Reject all cookies. Manage consent preferences. '
    assert enrich.clean_reporting_text(controls + TEXT) == TEXT.strip()
    journalism = 'The regulator said "Accept all cookies" buttons broke the law. ' + TEXT
    assert enrich.clean_reporting_text(journalism) == journalism.strip()
    assert not enrich.substantive_content(controls * 30)


def source_budget_case():
    guardian = [report(n, f'Protected airport event {n}', content=TEXT) for n in range(3)]
    for item in guardian:
        item.update(source='The Guardian', url=f'https://theguardian.com/event/{item["title"][-1]}')
    wire = report(4, 'Earthquake response confirmed by wire', content=TEXT)
    other = report(5, 'Government earthquake response', content=TEXT)
    events = [dict(id=str(n), title=item['title'], urls=[item['url']],
                   selected_urls=[item['url']], max_events=3) for n, item in enumerate(guardian)]
    events[1]['urls'].append(wire['url'])
    events[1]['selected_urls'].append(wire['url'])
    bundle = dict(news=[*guardian, wire, other, report(6, 'Government earthquake recovery', content=TEXT)])
    result = dict(briefing=dict(brief=[card(guardian[0]), card(guardian[1], 'Guardian exclusive detail. ' + TEXT)]))
    return result, bundle, events, wire


def test_source_cap_rewrites_covered_card_with_grounded_wire_excerpt():
    result, bundle, events, wire = source_budget_case()
    audit = coverage_gate.enforce(result, bundle, events)
    assert audit['unresolved'] == 0
    cards = result['briefing']['brief']
    replacement = next(c for c in cards if c['sources'] == [dict(outlet=wire['source'], url=wire['url'])])
    assert replacement['body'] == TEXT.strip()
    assert 'Guardian exclusive' not in replacement['body']
    assert sum(any(s['outlet'] == 'The Guardian' for s in c['sources']) for c in cards) == 2
    assert len(cards) == 3


def test_single_repair_prompt_restricts_cap_rewrite_to_wire_facts():
    result, bundle, events, wire = source_budget_case()
    calls = []
    def repair(prompt, timeout):
        calls.append(prompt)
        return [card(wire)]
    audit = coverage_gate.enforce(result, bundle, events, repair_fn=repair)
    assert len(calls) == 1
    assert wire['url'] in calls[0]
    assert audit['unresolved'] == 0
    assert audit['events'][1]['status'] == 'repaired'


def test_unavailable_non_guardian_never_waives_real_source_conflict():
    result, bundle, events, wire = source_budget_case()
    wire['content'] = ''
    audit = coverage_gate.enforce(result, bundle, events)
    assert audit['unresolved'] == 1
    assert result['quality_status']['overall'] == 'incomplete'



def test_leading_prose_consent_panel_does_not_enter_grounded_excerpt():
    controls = ('Please disable the ad blocker. To access this site, please disable the ad blocker and reload the page. '
                'This site uses cookies to store preferences and analyze visits. '
                'By clicking "Accept all", you consent to these cookies. '
                'Users accessing this site cannot view this site without consent. '
                'We apologize for the inconvenience. ')
    assert enrich.clean_reporting_text(controls + TEXT) == TEXT.strip()
    assert not enrich.substantive_content(controls * 12)
    journalism = 'This site uses cookies to spy on readers, investigators alleged. ' + TEXT
    assert enrich.clean_reporting_text(journalism) == journalism.strip()


def test_dense_signature_cannot_override_distinct_flight_identifiers():
    a = report(1, 'Attack at Riverton airport kills twelve people on flight EA123')
    b = report(2, 'Attack at Riverton airport kills twelve people on flight EA456')
    assert not protect([a, b])


def test_short_wire_content_cannot_hide_needed_source_budget_rewrite():
    result, bundle, events, wire = source_budget_case()
    short = report(7, 'Earthquake attack update', content='longword' * 150)
    bundle['news'].append(short)
    events[2]['selected_urls'].append(short['url'])
    events[2]['urls'].append(short['url'])
    audit = coverage_gate.enforce(result, bundle, events)
    assert audit['unresolved'] == 0
    assert any(c['sources'] == [dict(outlet=wire['source'], url=wire['url'])]
               for c in result['briefing']['brief'])


def test_conference_title_with_background_attack_is_not_an_acute_incident():
    item = report(1, 'Investment conferences proceed despite deadly airport attack',
                  'Organizers confirmed the economic meeting. Four died in earlier attacks.')
    assert not coverage_policy.recognition_reason(item)


def test_shooting_victim_count_and_total_including_perpetrator_remain_one_event():
    a = report(1, 'Riverton police say shooter killed eight people then himself', 'Police investigate the mass shooting at a home.')
    b = report(2, 'Nine dead in Riverton mass shooting, police say', 'Eight victims and the shooter died at a home.')
    assert len(protect([a, b])) == 1


def test_evolving_airport_casualties_on_same_explicit_day_remain_one_event():
    a = report(1, 'Missile attack at Riverton airport kills twelve people on Saturday')
    b = report(2, 'Riverton airport missile strike kills 13 people on Saturday')
    assert len(protect([a, b])) == 1


def test_untimed_different_fatalities_and_city_are_insufficient_identity():
    a = report(1, 'Missile attack at Riverton airport kills four people')
    b = report(2, 'Riverton airport missile strike kills twelve people')
    assert not protect([a, b])


def test_airport_missile_strike_with_fatalities_is_direct_security_reporting():
    assert coverage_policy.recognition_reason(report(1, 'Riverton airport missile strike kills 13 people')) == 'aviation_security'


def test_reporting_day_does_not_conflict_with_explicit_attack_day():
    a = report(1, 'Twelve killed in attack at Riverton airport',
               'The airport attack killed twelve people, authorities said early Sunday.')
    b = report(2, 'Missile attack at Riverton airport kills twelve people on Saturday')
    assert len(protect([a, b])) == 1


def test_distinct_named_airports_in_summaries_override_city_signature():
    a = report(1, 'Twelve killed in attack at Riverton airport', 'The attack struck Amber International Airport on Saturday.')
    b = report(2, 'Missile attack at Riverton airport kills twelve people', 'The attack struck Cobalt International Airport on Saturday.')
    assert not protect([a, b])


@pytest.mark.parametrize('title', [
    'Summit attacked after earlier airport bombing kills twelve people',
    'Airport attack kills twelve and threatens investment conference',
])
def test_direct_attack_does_not_lose_eligibility_for_economic_context(title):
    assert coverage_policy.recognition_reason(report(1, title))


def test_generic_international_airport_is_not_a_shared_city_anchor():
    a = report(1, 'Attack at airport in capital Riverton kills twelve people', 'The International Airport was struck on Saturday.')
    b = report(2, 'Attack at airport in capital Westhaven kills twelve people', 'The International Airport was struck on Saturday.')
    assert not protect([a, b])


def test_direct_economic_unrest_retains_national_escalation_eligibility():
    item = report(1, 'Economic crisis fuels nationwide protests',
                  'Hundreds arrested with seriously injured protesters; national government declares a national emergency.')
    assert coverage_policy.recognition_reason(item) == 'unrest_arrests_serious_harm'


def test_disaster_in_economic_capital_retains_direct_consequence_eligibility():
    item = report(1, 'Earthquake devastates economic capital', 'Magnitude 7.2 quake killed twelve people.')
    assert coverage_policy.recognition_reason(item) == 'acute_disaster'


def test_airport_geographic_adjective_does_not_veto_current_city_report():
    a = report(1, 'Strike on Eastern airport kills twelve people', 'The attack on Westhaven airport was on Saturday.')
    b = report(2, 'Attack on Westhaven airport kills twelve people', 'The attack was on Saturday.')
    c = report(3, 'Attack on Eastern airport kills twelve people', 'The strike was on Saturday.')
    assert coverage_policy._compatible(a, b)
    assert coverage_policy._compatible(b, c)


@pytest.mark.parametrize('title', [
    'Gunman kills eight people at investment conference',
    'Gunfire kills eight people at economic summit',
    'Massacre kills eight people at investment conference',
])
def test_direct_shooting_foreground_retains_eligibility(title):
    assert coverage_policy.recognition_reason(report(1, title, 'Police confirmed a mass shooting and eight fatalities at the venue.'))


@pytest.mark.parametrize('title', [
    'Investment conference proceeds despite gunman killing eight people',
    'Economic summit continues after massacre at the airport',
])
def test_shooting_in_background_does_not_make_conference_acute(title):
    assert not coverage_policy.recognition_reason(report(1, title, 'Earlier airport mass shooting killed eight people.'))


def prose_controls_without_button_marker():
    return [
        'To use this site, please disable the ad blocking feature and reload the page.',
        'This website uses cookies to collect settings and analyze visits.',
        'By clicking "Accept all," you will allow these cookies.',
        'Users accessing this site cannot view this site without consent.',
        'We apologize for any inconvenience caused.',
    ]


@pytest.mark.parametrize('path', ['cleaner', 'one_paragraph', 'split_paragraphs'])
def test_prose_control_panel_bootstraps_without_standalone_button_marker(path):
    controls = prose_controls_without_button_marker()
    text = ' '.join(controls) + ' ' + TEXT
    if path == 'cleaner':
        cleaned = enrich.clean_reporting_text(text)
    elif path == 'one_paragraph':
        cleaned = enrich._extract_text('<p>' + text + '</p>')
    else:
        cleaned = enrich._extract_text(''.join('<p>' + s + '</p>' for s in [*controls, TEXT]))
    assert cleaned == TEXT.strip()


def test_prose_control_panel_cannot_supply_missing_article_floor():
    text = (' '.join(prose_controls_without_button_marker()) + ' ') * 3 + 'Officials confirmed the public response. ' * 8
    assert not enrich.substantive_content(text)


def test_quoted_prose_control_in_journalism_remains_reporting():
    text = '"To use this site, please disable the ad blocking feature and reload the page," the regulator quoted. ' + TEXT
    assert enrich.clean_reporting_text(text) == text.strip()


def test_grounded_wire_excerpt_strips_unmarked_prose_panel():
    result, bundle, events, wire = source_budget_case()
    wire['content'] = ' '.join(prose_controls_without_button_marker()) + ' ' + TEXT
    coverage_gate.enforce(result, bundle, events)
    replacement = next(c for c in result['briefing']['brief'] if c['sources'] == [dict(outlet=wire['source'], url=wire['url'])])
    assert replacement['body'] == TEXT.strip()
