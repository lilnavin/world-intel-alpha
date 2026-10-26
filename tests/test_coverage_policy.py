import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
from worldintel.core import coverage_policy as policy


def item(source="alpha", **kw):
    return dict(title="Passenger attempts to seize aircraft cockpit in international flight",
                summary="Police investigate attempted hijacking", tier=1, source=source,
                url=f"https://{source}.com/report", desk="world", **kw)


def test_defaults_hash_and_isolation():
    a = policy.resolve_profile()
    assert a == policy.resolve_profile({})
    assert policy.profile_hash(a) == policy.profile_hash(dict(reversed(list(a.items()))))
    a["topics"].clear()
    assert policy.resolve_profile()["topics"]


@pytest.mark.parametrize("raw", [{"unknown": 1}, {"version": True},
    {"major_events": {"max_events": 4}}, {"major_events": {"min_publishers": 1}},
    {"topics": [{"terms": ["attack"], "weight": float("nan")}]},
    {"topics": [{"terms": [""], "weight": 1}]},
    {"major_events": {"enabled": 1}}])
def test_invalid_profiles(raw):
    with pytest.raises(ValueError):
        policy.resolve_profile(raw)


def test_generic_aviation_and_bounded_literal_terms():
    assert policy.route_security(item())
    assert not policy.route_security({"title": "Airline shares attack record highs"})
    assert not policy.route_security({"title": "Aircraft cockpit design unveiled"})
    assert policy.route_security({"title": "Pilot stabbed aboard aircraft during international flight"})
    p = policy.resolve_profile({"topics": [{"terms": ["terror"], "weight": 2}]})
    assert policy.score_bonus({"title": "terrorism investigation"}, p) == 2
    assert policy.score_bonus({"title": "arterror shares"}, p) == 0
    assert policy.score_bonus(item(), policy.resolve_profile()) <= 3


def test_protection_capacity_alternate_and_independent_evidence():
    a, b = item(), item("beta")
    old = {"world": [{"title": "Budget debate", "url": "https://other.com/a"}]}
    selected, events = policy.protect([a, b], old, policy.resolve_profile(),
                                     lambda x, y: True, lambda x: 5)
    assert selected["world"] == [a]
    assert len(events) == 1
    assert len(events[0]["evidence"]) == 2
    assert events[0]["selected_urls"] == [a["url"]]
    assert old["world"][0]["title"] == "Budget debate"
    selected, events = policy.protect([a, item("alpha",)], old,
        policy.resolve_profile(), lambda x,y: True, lambda x: 5)
    assert not events


def test_quality_duplicates_and_no_matcher():
    a, b = item(), item("beta")
    b["tier"] = 3
    old = {"world": [a, b]}
    for matcher in [None, lambda x,y: False]:
        assert not policy.protect([a,b], old, policy.resolve_profile(), matcher, lambda x: 1)[1]
    assert not policy.protect([a,b], old, policy.resolve_profile(), lambda x,y: True, lambda x: 1)[1]


def test_syndicated_wire_is_one_publisher():
    a, b = item(), item("beta")
    a["wire_source"] = "Reuters"
    b["wire_source"] = "Reuters"
    assert not policy.protect([a,b], {"world": [a,b]}, policy.resolve_profile(),
                              lambda x,y: True, lambda x: 5)[1]


def test_bbc_aliases_video_and_existing_alternate():
    a,b = item(), item("beta")
    a["url"],b["url"] = "https://bbc.com/news/a", "https://bbc.co.uk/news/b"
    assert not policy.protect([a,b], {"world": [a,b]}, policy.resolve_profile(),lambda x,y: True,lambda x: 5)[1]
    a,b = item(),item("beta")
    b["url"] = "https://beta.com/video/report"
    assert not policy.protect([a,b], {"world": [a,b]}, policy.resolve_profile(),lambda x,y: True,lambda x: 5)[1]
    b["url"] = "https://beta.com/news/report"
    selected,events = policy.protect([a,b], {"world": [a,b]},policy.resolve_profile(),lambda x,y: True,lambda x: 5)
    assert len({i["url"] for i in selected["world"]}) == 2


def test_textual_wire_and_unrelated_aviation_attacks():
    a,b = item(), item("beta")
    a["summary"],b["summary"] = "Reuters reports an attempted hijacking", "Reuters: police investigate"
    assert not policy.protect([a,b], {"world": [a,b]},policy.resolve_profile(),lambda x,y: True,lambda x: 5)[1]


def test_wire_attribution_overrides_publisher_and_location_is_not_identity():
    a,b = item(),item("beta")
    a.update(publisher="Outlet A", wire_source="Reuters")
    b.update(publisher="Outlet B", wire_source="Reuters")
    assert not policy.protect([a,b], {"world": [a,b]},policy.resolve_profile(),lambda x,y: True,lambda x: 5)[1]
    a,b = item(),item("beta")
    a.update(title="Paris airport attack kills guard outside terminal", summary="")
    b.update(title="Paris airport attack on aircraft injures passenger", summary="")
    assert not policy.protect([a,b], {"world": [a,b]},policy.resolve_profile(),lambda x,y: True,lambda x: 5)[1]


def test_named_carrier_identity_with_diverse_headlines():
    a,b = item(),item("beta")
    a.update(title="ExampleAir pilot stabbed on flight to Oslo", summary="")
    b.update(title="Passenger tried to crash ExampleAir plane in Oslo, officials say", summary="")
    assert policy.protect([a,b], {"world": [a,b]},policy.resolve_profile(),lambda x,y: True,lambda x: 5)[1]


def test_background_disaster_and_same_carrier_fragments():
    a,b = item(),item("beta")
    a["title"] = b["title"] = "Imperial visit to quake-hit district postponed due to weather"
    a["summary"] = b["summary"] = "Earthquake recovery continues"
    assert not policy.protect([a,b], {"world": [a,b]},policy.resolve_profile(),lambda x,y: True,lambda x: 5)[1]
    pool = [item(source) for source in ["alpha", "beta", "gamma", "delta"]]
    for n,row in enumerate(pool):
        row.update(title=("ExampleAir flight pilot stabbed in Oslo" if n < 2 else
                          "Passenger tried to crash ExampleAir plane in Oslo"), summary="")
    selected,events = policy.protect(pool, {"world": pool},policy.resolve_profile(),
                                    lambda x,y: x["title"] == y["title"],lambda x: 5)
    assert len(events) == 1
    assert len(selected["world"]) <= 2


def test_lowercase_carrier_bridge_merges_existing_groups():
    pool = [item(source) for source in ["alpha", "beta", "gamma", "delta", "zeta"]]
    titles = ["exampleair flight pilot stabbed", "exampleair aircraft pilot stabbed",
              "Passenger tried to crash plane says exampleair", "Passenger tried to crash aircraft says exampleair",
              "exampleair plane passenger tried to crash plane pilot stabbed"]
    for row,title in zip(pool,titles):
        row.update(title=title + " in Oslo", summary="")
    selected,events = policy.protect(pool, {"world": pool},policy.resolve_profile(),
        lambda a,b: (("stabbed" in a["title"]) == ("stabbed" in b["title"]))
                    or ("stabbed" in a["title"] and "tried" in a["title"])
                    or ("stabbed" in b["title"] and "tried" in b["title"]),lambda x: 5)
    assert len(events) == 1
    assert len(events[0]["urls"]) == 5
    assert len(selected["world"]) == 2


def test_same_carrier_conflicting_flight_ids_do_not_merge():
    pool = [item(source) for source in ["alpha", "beta", "gamma", "delta"]]
    for n,row in enumerate(pool):
        row.update(title="exampleair flight " + ("EA123" if n < 2 else "EA456") + " pilot stabbed", summary="")
    selected,events = policy.protect(pool, {"world": pool},policy.resolve_profile(),lambda a,b: True,lambda x: 5)
    assert len(events) == 2
    a,b = item(), item("beta")
    a["title"] = "Aircraft pilot stabbed in France"
    b["title"] = "Airport bomb attack in France"
    a["summary"] = b["summary"] = ""
    assert not policy.protect([a,b], {"world": [a,b]},policy.resolve_profile(),lambda x,y: True,lambda x: 5)[1]


def test_two_events_preserve_capacity_and_seen_score():
    pool = [item(s) for s in ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]]
    for i, row in enumerate(pool):
        row["event"] = i // 2
    old = {"world": [{"url": "old1"}, {"url": "old2"}]}
    selected, events = policy.protect(pool, old, policy.resolve_profile(),
        lambda a,b: a["event"] == b["event"], lambda a: 5-a.get("event", 9))
    assert len(events) == 2
    assert len(selected["world"]) == 2
    assert len({x["event"] for x in selected["world"]}) == 2
    assert not policy.protect(pool, old, policy.resolve_profile(),
        lambda a,b: a["event"] == b["event"], lambda a: -1)[1]


@pytest.mark.parametrize("carrier", ["Acme", "Emergency"])
@pytest.mark.parametrize("second", ["hijacked", "bombed"])
def test_carrier_is_not_incident_identity(carrier, second):
    a,b = item(),item("beta")
    a.update(title=f"{carrier} flight hijacked in Paris", summary="")
    b.update(title=f"{carrier} aircraft {second} in Tokyo", summary="")
    assert not policy.protect([a,b], {"world": [a,b]},policy.resolve_profile(),lambda x,y: True,lambda x: 5)[1]

def test_generic_fbi_words_do_not_establish_mass_shooting_identity():
    a, b = item(), item('beta')
    a.update(title='US suspect conspired with Tumbler Ridge killer before and during attack, says the FBI', summary='Mass shooting investigation')
    b.update(title='FBI arrests man for plotting mass shooting at Mall of America', summary='US suspect arrested')
    assert not policy._compatible(a, b)


def test_bridge_cannot_merge_distinct_mass_shootings():
    pool = [item(s) for s in ['alpha', 'beta', 'gamma', 'delta', 'zeta']]
    titles = ['Tumbler Ridge mass shooting suspect FBI investigation',
              'FBI investigates Tumbler Ridge mass shooting suspect',
              'Mall of America mass shooting suspect FBI investigation',
              'FBI investigates Mall of America mass shooting suspect',
              'FBI mass shooting investigation: Tumbler Ridge and Mall of America suspects']
    for row, title in zip(pool, titles):
        row.update(title=title, summary='')
    _, events = policy.protect(pool, {'world': pool}, policy.resolve_profile(), lambda a,b: True, lambda a: 5)
    assert len(events) == 2
    assert not any(pool[0]['url'] in e['urls'] and pool[2]['url'] in e['urls'] for e in events)

def test_aden_airport_and_flydubai_safety_probe_are_distinct():
    a, b = item(), item('beta')
    a.update(title="Yemen's Houthis claim strikes on Aden, Riyadh airports", summary='')
    b.update(title='EXCLUSIVE: Saudi Arabia launches air safety probe into flydubai incident, sources say - Reuters', summary='')
    assert not policy._compatible(a, b)


def test_same_named_mass_shooting_paraphrases_are_preserved():
    a, b = item(), item('beta')
    a.update(title='FBI arrests man for plotting mass shooting at Mall of America', summary='')
    b.update(title='Mall of America mass shooting plot disrupted by federal authorities', summary='')
    assert policy.protect([a,b], {'world': [a,b]}, policy.resolve_profile(), lambda a,b: True, lambda a: 5)[1]

@pytest.mark.parametrize('shared', ['kills four', '100', 'several people', 'four people dead', 'police launch probe'])
def test_real_matcher_does_not_group_distinct_named_mall_incidents(shared):
    from worldintel.core import fetch_sources
    a, b = item(), item('beta')
    a.update(title=f'Mass shooting {shared} at Mall of America', summary='')
    b.update(title=f'Mass shooting {shared} at Westfield Mall', summary='')
    pool = [a,b]
    _, events = policy.protect(pool, {'world': pool}, policy.resolve_profile(),
                              fetch_sources.story_matcher(pool, min_ratio=0), lambda a: 5)
    assert not events


@pytest.mark.parametrize("title", ["Named-city mass shooting kills eight people", "Named-city shooter killed eight people", "Named-city gunman killed 8 people"])
def test_shooting_consequence_recognition(title):
    assert policy._serious(dict(title=title, summary="", url="https://example.org/news/a"))

@pytest.mark.parametrize("title", [
    "River movement protests: nationwide school closures and blockades",
    "River movement protests: mass arrests and serious injuries",
    "Government announces national emergency response to River movement protests",
])
def test_material_unrest_recognition(title):
    assert policy._serious(dict(title=title, summary="", url="https://example.org/news/a"))

@pytest.mark.parametrize("title", ["Small routine demonstration in France", "Peaceful protest march in France", "Opinion: nationwide protest school closures", "Historical retrospective of mass arrests during protests"])
def test_unrest_negatives(title):
    assert not policy._serious(dict(title=title, summary="", url="https://example.org/news/a"))

@pytest.mark.parametrize("limit", [0, 2, 3])
def test_capacity_and_excess_audit(limit):
    pool = [item(s) for s in ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]]
    for n, row in enumerate(pool):
        row["event"] = n // 2
    audit = []
    selected, events = policy.protect(pool, {"world": [{"url": f"old{n}"} for n in range(4)]},
        policy.resolve_profile({"major_events": {"max_events": limit}}),
        lambda a,b: a["event"] == b["event"], lambda a: 9-a.get("event", 9), audit=audit)
    assert len(events) == limit
    assert len(selected["world"]) == 4
    assert sum(row["reason"] == "excluded_by_capacity" for row in audit) == 4-limit
    assert all(e["max_events"] == limit and e["recognition_reasons"] for e in events)


def test_unrelated_movements_country_is_not_identity():
    a,b = item(),item("beta")
    a.update(title="France River movement protests: nationwide school closures", summary="")
    b.update(title="France Mountain movement protests: nationwide school closures", summary="")
    assert not policy._compatible(a,b)


@pytest.mark.parametrize("limit", [-1, 4, True, 2.0, "3"])
def test_invalid_event_limits(limit):
    with pytest.raises(ValueError):
        policy.bounded_events([],limit)


def test_disabled_protection_and_default_two():
    a,b=item(),item('beta')
    assert policy.resolve_profile()['major_events']['max_events'] == 2
    assert not policy.protect([a,b],{'world':[a,b]},policy.resolve_profile({'major_events':{'enabled':False}}),lambda a,b:True,lambda a:5)[1]


@pytest.mark.parametrize("title", ["River movement protests: nationwide school closures and blockades", "River movement protests: mass arrests and serious injuries", "Government announces national emergency response to River movement protests"])
def test_unrest_requires_two_independent_publishers(title):
    a,b=item(),item('beta')
    for row in [a,b]:
        row.update(title=title,summary='')
    assert policy.protect([a,b],{'world':[a,b]},policy.resolve_profile(),lambda a,b:True,lambda a:5)[1]
    b['wire_source']=a['wire_source']='Same Wire'
    assert not policy.protect([a,b],{'world':[a,b]},policy.resolve_profile(),lambda a,b:True,lambda a:5)[1]


def test_country_and_escalation_words_cannot_merge_unrelated_unrest():
    a,b=item(),item('beta')
    a.update(title='France education protests: nationwide school closures and blockades',summary='')
    b.update(title='France farming protests: nationwide school closures and blockades',summary='')
    assert not policy._compatible(a,b)


@pytest.mark.parametrize("right", ["farming", "transport"])
def test_shared_government_response_does_not_corroborate_distinct_protests(right):
    from worldintel.core import fetch_sources
    a,b=item(),item('beta')
    a.update(title='President imposes nationwide curfew after France education protests',summary='')
    b.update(title=f'President imposes nationwide curfew after France {right} protests',summary='')
    assert policy._serious(a) and policy._serious(b)
    assert not policy._compatible(a,b)
    assert not policy.protect([a,b],{'world':[a,b]},policy.resolve_profile(),fetch_sources.story_matcher([a,b]),lambda a:5)[1]


def test_same_movement_government_response_paraphrases_corroborate():
    from worldintel.core import fetch_sources
    a,b=item(),item('beta')
    a.update(title='President imposes nationwide curfew after France education protests',summary='')
    b.update(title='France education protests prompt president to announce nationwide curfew',summary='')
    assert policy._compatible(a,b)
    assert policy.protect([a,b],{'world':[a,b]},policy.resolve_profile(),fetch_sources.story_matcher([a,b]),lambda a:5)[1]


@pytest.mark.parametrize('title', [
    'President dissolves national government in response to France River movement protests',
    'National government resigns following France River movement protests',
    'Prime minister resigns after France River movement protests',
    'France River movement protests force national government to resign',
])
def test_linked_material_government_consequence(title):
    a,b=item(),item('beta')
    for row in [a,b]:
        row.update(title=title,summary='')
    assert policy.recognition_reason(a) == 'unrest_national_government_response'
    assert policy.protect([a,b],{'world':[a,b]},policy.resolve_profile(),lambda a,b:True,lambda a:5)[1]


@pytest.mark.parametrize('title', [
    'President comments on France River movement protests',
    'National government promises talks after France River movement protests',
    'National government resigns; France River movement protests continue',
    'National government resigns after election; France River movement protests continue',
    'Local council resigns in response to France River movement protests',
])
def test_routine_or_unlinked_government_response_is_not_escalation(title):
    assert not policy._serious(dict(title=title,summary=''))



def test_explicit_movement_conflict_overrides_shared_country_protest_qualifier():
    from worldintel.core import fetch_sources
    a,b=item(),item('beta')
    a.update(title='President imposes nationwide curfew after CountryAlpha protests by River movement',summary='')
    b.update(title='President imposes nationwide curfew after CountryAlpha protests by Mountain movement',summary='')
    assert not policy._compatible(a,b)
    assert not policy.protect([a,b],{'world':[a,b]},policy.resolve_profile(),fetch_sources.story_matcher([a,b]),lambda a:5)[1]


@pytest.mark.parametrize('wording', [
    '400 schools across CountryAlpha will be totally or partly closed after River movement protests; police report serious injuries',
    'Hundreds of schools across CountryAlpha shut as River movement education protests rock the country; schools burned and ransacked',
    '500 schools throughout CountryAlpha shuttered during River movement protests; police report serious injuries',
    'Classes at 400 schools across CountryAlpha suspended after River movement protests; 24 schools burned',
])
def test_quantified_countrywide_school_disruption(wording):
    a,b=item(),item('beta')
    for row in [a,b]:
        row.update(title=wording,summary='')
    assert policy.recognition_reason(a) == 'unrest_national_disruption'
    assert policy.protect([a,b],{'world':[a,b]},policy.resolve_profile(),lambda a,b:True,lambda a:5)[1]


@pytest.mark.parametrize('wording', [
    'Four schools across CountryAlpha partly closed after routine protests',
    '400 schools across CountryAlpha close for scheduled holiday during peaceful protests',
    'Hundreds of schools in Metrocity shut during River movement protests; police report serious injuries',
    'One school across CountryAlpha shut during protests; police report serious injuries',
])
def test_quantified_disruption_requires_countrywide_serious_context(wording):
    assert not policy._serious(dict(title=wording,summary=''))


def test_school_disruption_does_not_merge_conflicting_movements():
    a,b=item(),item('beta')
    a.update(title='400 schools across CountryAlpha partly closed after River movement protests; police report serious injuries',summary='')
    b.update(title='400 schools across CountryAlpha partly closed after Mountain movement protests; police report serious injuries',summary='')
    assert not policy._compatible(a,b)



def test_quantified_school_closure_paraphrases_corroborate_real_matcher():
    from worldintel.core import fetch_sources
    a,b=item(),item('beta')
    a.update(title='Hundreds of schools shut as River movement education protests rock CountryAlpha',summary='400 schools across CountryAlpha partly closed; schools burned and ransacked')
    b.update(title='CountryAlpha River movement unrest disrupts 400 schools',summary='Schools throughout CountryAlpha shuttered after protests; authorities report serious injuries')
    assert policy._serious(a) and policy._serious(b)
    assert policy.protect([a,b],{'world':[a,b]},policy.resolve_profile(),fetch_sources.story_matcher([a,b]),lambda a:5)[1]



@pytest.mark.parametrize('title', [
    'More than 400 schools in CountryAlpha to be partly closed after student protests',
    'CountryAlpha to shutter up to 500 schools after pupil protests',
    '400-500 schools suspended after school protests in CountryAlpha',
])
def test_extensive_violent_school_disruption_with_national_institution(title):
    row=dict(title=title,summary='Education minister says 24 schools burned or ransacked and 78 staff seriously injured')
    assert policy.recognition_reason(row) == 'unrest_extensive_school_disruption'


def test_school_issue_participant_paraphrases_corroborate_real_matcher():
    from worldintel.core import fetch_sources
    a,b=item(),item('beta')
    a.update(title='More than 400 schools in CountryAlpha to be closed after student protests',summary='Education minister says 24 schools burned or ransacked and 78 staff injured')
    b.update(title='Hundreds of schools to shut as education protests rock CountryAlpha',summary='Education minister says up to 500 schools will be partly closed and 24 schools burned or ransacked')
    assert policy._serious(a) and policy._serious(b)
    assert policy._compatible(a,b)
    assert policy.protect([a,b],{'world':[a,b]},policy.resolve_profile(),fetch_sources.story_matcher([a,b]),lambda a:5)[1]


@pytest.mark.parametrize('title', [
    'Four schools partly closed after student protests in CountryAlpha',
    '400 schools closed after peaceful student protests in CountryAlpha',
    '400 schools across the city partly closed after student protests',
    '400 schools across the town shut after student protests',
    '400 schools across the district shuttered after student protests',
    '400 schools across the campus suspended after student protests',
])
def test_extensive_school_rule_excludes_small_peaceful_or_explicit_local_scope(title):
    summary = 'Education minister announces routine talks' if 'peaceful' in title else 'Education minister says schools burned or ransacked'
    assert not policy._serious(dict(title=title,summary=summary))


@pytest.mark.parametrize('qualifier', ['farming','transport'])
def test_extensive_school_reports_cannot_join_unrelated_issue(qualifier):
    a,b=item(),item('beta')
    a.update(title='400 schools partly closed after student protests in CountryAlpha',summary='Education minister says 24 schools burned or ransacked')
    b.update(title=f'400 schools partly closed after {qualifier} protests in CountryAlpha',summary='Education minister says 24 schools burned or ransacked')
    assert not policy._compatible(a,b)



def test_school_issue_close_infinitive_and_shut_paraphrases_are_compatible():
    a,b=item(),item('beta')
    a.update(title='Hundreds of schools to shut as student protests rock CountryAlpha',summary='Education minister says 24 schools burned and 78 staff injured')
    b.update(title='Hundreds of schools to close as education protests rock CountryAlpha',summary='Education minister says 24 schools burned and 78 staff injured')
    assert policy._compatible(a,b)



def test_generic_protest_movement_references_are_not_conflicting_names():
    a,b=item(),item('beta')
    a.update(title='Hundreds of schools to shut as student protests rock CountryAlpha',summary='Education minister says 24 schools burned; the protest movement continues')
    b.update(title='Hundreds of schools to close as education protests rock CountryAlpha',summary='Education minister says 24 schools burned; the movement continues')
    assert policy._compatible(a,b)


def test_shooting_report_with_recovered_video_evidence_is_recognized():
    row = dict(title='9 dead, including relatives, in shooting in Sampletown',
               summary='Police recovered video and audio evidence of the gunman at the home.',
               url='https://example.org/news/report')
    assert policy.recognition_reason(row) == 'shooting_multiple_fatalities'


@pytest.mark.parametrize('title,summary', [
    ('Base shooter to be executed by firing squad, officials say', 'The gunman killed 13 people in a 2009 shooting.'),
    ('President orders firing squad for gunman who killed 13 people at Samplebase', ''),
    ('Court sentences gunman for mass shooting at Samplebase', 'The attack happened years ago.'),
])
def test_legal_followup_does_not_protect_historical_shooting(title, summary):
    assert not policy._serious(dict(title=title, summary=summary))


@pytest.mark.parametrize('title,summary', [
    ('Gunman killed eight people today near court as execution debate continues', ''),
    ('Court debates execution policy after mass shooting today', 'A gunman killed eight people today.'),
    ('Eight dead in new shooting outside courthouse', 'An execution hearing was under way.'),
])
def test_current_shooting_survives_incidental_legal_context(title, summary):
    assert policy._serious(dict(title=title, summary=summary))


def test_protection_reserves_unique_vetted_urls_before_alternates():
    a, duplicate, b, low = item(), item(), item('beta'), item('low')
    duplicate['source'] = 'alpha-world'
    low['tier'] = 3
    low['url'] = a['url']
    selected, events = policy.protect([a, duplicate, b, low], {'world': [low, duplicate]},
                                     policy.resolve_profile(), lambda a,b: True,
                                     lambda row: 10 if row['source'].startswith('alpha') else 5)
    assert events[0]['selected_urls'] == [a['url'], b['url']]
    assert len({row['url'] for row in selected['world']}) == 2
    assert all(row['tier'] in (1, 2) for row in selected['world'])


def test_execution_today_does_not_refresh_background_shooting():
    assert not policy._serious(dict(
        title='Execution today for gunman who killed 13 people',
        summary='The gunman who killed 13 people faces execution today.'))


def test_new_shooting_now_survives_trial_background():
    assert policy._serious(dict(title='Suspect awaiting trial kills eight people outside courthouse',
                               summary='A gunman killed eight people now; police are responding.'))


def test_selected_alternate_replaced_with_unique_vetted_membership():
    a, b = item(), item('beta')
    low = dict(b, tier=3)
    selected, events = policy.protect([a,b], {'world':[a,low,dict(low)]},
                                     policy.resolve_profile(), lambda a,b:True,
                                     lambda row:10 if row['source']=='alpha' else 5)
    assert events[0]['selected_urls'] == [a['url'],b['url']]
    assert selected['world'] == [a,b]


@pytest.mark.parametrize('cue', ['opinion', 'newsletter', 'historical', 'retrospective'])
def test_summary_editorial_or_historical_cues_remain_excluded(cue):
    assert not policy._serious(dict(title='Gunman killed eight people in Sampletown',
                                   summary=f'This {cue} describes the shooting.'))


@pytest.mark.parametrize("second_name,expected", [("Mountain Alliance", 0), ("River Alliance", 1)])
def test_multiword_named_movements_use_real_matcher(second_name, expected):
    from worldintel.core import fetch_sources
    rows = [dict(title=f"400 schools closed after CountryAlpha {name} movement protests",
        summary=f"Education minister reports schools burned and ransacked during {name} demonstrations in {city}.",
        source=source, tier=1, url=f"https://{source}.example/report", desk="world")
        for name, city, source in [("River Alliance", "Northville", "wirea"),
                                  (second_name, "Southville", "wireb")]]
    rows[1]["title"] = f"CountryAlpha {second_name} movement protests close 400 schools"
    matcher = fetch_sources.story_matcher(rows, min_ratio=0)
    _, events = policy.protect(rows, {"world": rows}, policy.resolve_profile({}), matcher, lambda i: 5)
    assert len(events) == expected


@pytest.mark.parametrize("prefix,country", [("Norland ", "Norland"), ("France ", "France"), ("The ", "Norland")])
def test_multiword_movement_boundaries_preserve_same_real_match(prefix, country):
    from worldintel.core import fetch_sources
    rows = [dict(title=f"400 schools closed after {prefix}River Alliance movement protests",
                 summary="Education minister reports schools burned and ransacked.",
                 source="wirea", tier=1, url="https://wirea.example/report", desk="world"),
            dict(title=f"River Alliance movement protests close 400 schools in {country}",
                 summary="Education minister reports schools burned and ransacked.",
                 source="wireb", tier=1, url="https://wireb.example/report", desk="world")]
    matcher = fetch_sources.story_matcher(rows, min_ratio=0)
    assert matcher(*rows)
    assert policy._compatible(*rows)
    _, events = policy.protect(rows, {"world": rows}, policy.resolve_profile({}), matcher, lambda i: 5)
    assert len(events) == 1


@pytest.mark.parametrize("left,right,expected", [
    ('"United River Alliance"', '"River Alliance"', False),
    ('"The River Alliance"', '"River Alliance"', False),
    ('"United River Alliance"', '"United River Alliance"', True),
    ('River Alliance', 'Mountain Alliance', False),
    ('The River Alliance', 'River Alliance', True),
])
def test_quoted_movement_prefixes_and_distinct_names_remain_meaningful(left, right, expected):
    from worldintel.core import fetch_sources
    rows = [dict(title=f"400 schools closed after {name} movement protests in Norland",
                 summary="Education minister reports schools burned and ransacked.",
                 source=source, tier=1, url=f"https://{source}.example/report", desk="world")
            for name, source in [(left,"wirea"),(right,"wireb")]]
    matcher = fetch_sources.story_matcher(rows, min_ratio=0)
    assert matcher(*rows)
    assert policy._compatible(*rows) is expected
    _, events = policy.protect(rows, {"world": rows}, policy.resolve_profile({}), matcher, lambda i: 5)
    assert len(events) == int(expected)


def test_title_case_prose_is_not_an_explicit_movement_name():
    left = dict(title="Norland National School movement closes 400 schools",
                summary="Education minister reports schools burned and ransacked.")
    right = dict(title="400 schools closed during school movement protests in Norland",
                 summary="Education minister reports schools burned and ransacked.")
    assert policy._compatible(left,right)


@pytest.mark.parametrize("second_name,expected", [("River Alliance", False), ("United River Alliance", True)])
def test_curly_quoted_full_movement_names_use_explicit_boundaries(second_name, expected):
    LEFT, RIGHT = chr(0x201c), chr(0x201d)
    rows = [dict(title=f"400 schools closed after {LEFT}{name}{RIGHT} movement protests in Norland",
                 summary="Education minister reports schools burned and ransacked.",
                 source=source, tier=1, url=f"https://{source}.example/report", desk="world")
            for name, source in [("United River Alliance", "wirea"), (second_name, "wireb")]]
    from worldintel.core import fetch_sources
    matcher = fetch_sources.story_matcher(rows, min_ratio=0)
    assert matcher(*rows)
    assert policy._compatible(*rows) is expected
    _, events = policy.protect(rows, {"world": rows}, policy.resolve_profile({}), matcher, lambda i: 5)
    assert len(events) == int(expected)
