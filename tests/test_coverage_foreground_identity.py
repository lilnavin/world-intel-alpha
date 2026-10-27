import pytest

from worldintel.core import coverage_policy as policy


def report(title, source="alpha", summary="Zephyr regional reporting."):
    return dict(title=title, summary=summary, source=source, tier=1,
                url=f"https://{source}.example/report", desk="world")


@pytest.mark.parametrize("reverse", [
    "Vardans attack Norian airport",
    "Vardans say they attacked Norian airport",
    "Vardans say they hit Norian airport",
    "Vardans said that they attacked Norian airport",
    "Vardan says it attacked Norian airport",
    "Vardan's forces strike Norian airport",
])
def test_reciprocal_foreground_veto_precedes_shared_anchors(reverse):
    forward = report("Norian-led forces strike Vardan missile facilities after airport attack",
                     summary="Vardans attacked Norian airport in the earlier fighting.")
    backward = report(reverse, "beta")
    assert not policy._compatible(forward, backward)
    assert not policy._compatible(backward, forward)


def test_inverse_reports_do_not_establish_independent_corroboration():
    forward = report("Norian-led forces strike Vardan missile facilities after airport attack")
    backward = report("Vardans say they attacked Norian airport as fighting intensifies", "beta")
    audit = []
    _, events = policy.protect([forward, backward], {"world": [forward]},
                              policy.resolve_profile(), lambda a, b: True,
                              lambda item: 5, audit=audit)
    assert not events
    assert audit
    assert all(row["reason"] == "lack_independent_corroboration" for row in audit)


@pytest.mark.parametrize("title", [
    "Norian forces attack Vardan airport",
    "Norian forces attack Vardan airport after Vardans attacked Norian bases",
    "Norian forces attack Vardan airport amid Vardan strikes on Norian bases",
])
def test_same_direction_still_groups_despite_inverse_background(title):
    a = report("Norian-led forces strike Vardan airport",
               summary="Vardans attacked Norian bases earlier.")
    b = report(title, "beta")
    assert policy._compatible(a, b)
    assert len(policy.protect([a, b], {"world": [a]}, policy.resolve_profile(),
                             lambda a, b: True, lambda item: 5)[1]) == 1


@pytest.mark.parametrize("title", [
    "Vardans plan to attack Norian airport",
    "Vardans deny they attacked Norian airport",
    "Vardans did not attack Norian airport",
    "Vardans may attack Norian airport",
    "Vardans could have attacked Norian airport",
    "Norian airport attacked by Vardans",
    "Officials say Vardans attacked Norian airport",
    "Vardans say Norian forces attacked Vardan bases",
    'Vardans say "they attacked Norian airport"',
    "Vardans reportedly attacked Norian airport",
    "After Norian strikes, Vardans attack Norian airport",
    "Vardan and Norian forces attack Norian airport",
    "Vardans attack airport near Norian bases",
])
def test_uncertain_or_nonreciprocal_grammar_does_not_add_a_veto(title):
    assert policy._compatible(report("Norian forces attack Vardan airport"), report(title))


@pytest.mark.parametrize("title", [
    "Otherian forces attack Vardan airport",
    "Vardans attack Otherian airport",
    "Vardan Norian forces attack Norian airport",
    "Forces attack Norian airport",
])
def test_actor_difference_alone_is_not_a_contradiction(title):
    assert policy._compatible(report("Norian forces attack Vardan airport"), report(title))


@pytest.mark.parametrize("title", [
    "Norian says Vardan forces attacked Norian airport",
    "Vardan says they attacked Norian airport",
    "Vardans say it attacked Norian airport",
    "Vardan-backed Norian forces attack Norian airport",
    "Vardan-based Norian forces attack Norian airport",
    "Vardan and Otherian forces attack Norian airport",
    "Vardans attack Norian airport?",
])
def test_ambiguous_attribution_and_compound_actors_abstain(title):
    assert policy._compatible(report("Norian forces attack Vardan airport"), report(title))


def test_action_word_inside_named_actor_is_not_the_action():
    assert not policy._compatible(report("Norian forces attack Strike Force airport"),
                                  report("Strike Force attacks Norian airport"))


def test_simple_plural_in_is_matches_reciprocal_named_target():
    assert not policy._compatible(report("Norian-led forces strike Vardi missile facilities"),
                                  report("Vardis say they attacked Norian airport"))


@pytest.mark.parametrize("title", [
    "Vardans attack Norian airport after talks fail?",
    'Vardans attack Norian airport after officials say "talks fail"',
    "Vardans attack Norian airport after talks may fail",
    "Vardans attack Norian airport, officials say",
    "Vardans attack Norian airport, officials claim",
    "Vardans attack Norian airport: officials",
    "Vardans attack Norian airport; according to officials",
    "Vardans attack Norian airport after talks fail, officials claim",
    "Vardans attack Norian airport after talks fail: officials",
    "Vardans attack Norian airport after talks fail; according to officials",
    "Vardans attacked Norian airport, Norian forces say they attacked Vardan airport",
    "Vardans attack Norian airport in exercise",
])
def test_full_title_uncertainty_and_suffix_attribution_abstain(title):
    assert policy._compatible(report("Norian forces attack Vardan airport"), report(title))


@pytest.mark.parametrize("title", [
    "Vardi forces attack Norian airport",
    "Vardi attacks Norian airport",
    "Vardi said it attacked Norian airport",
])
def test_terminal_s_proper_names_are_not_merged_with_other_names(title):
    assert policy._compatible(report("Norian forces attack Vardis bases"), report(title))


def test_singular_proper_name_ending_s_preserves_self_report_identity():
    assert not policy._compatible(report("Norian forces attack Vardis airport"),
                                  report("Vardis says it attacked Norian airport"))


@pytest.mark.parametrize("title", [
    "Vardis forces attack Norian airport",
    "Vardis attacks Norian airport",
    "Vardis attacked Norian airport",
    "Vardis hit Norian airport",
])
def test_actor_terminal_s_requires_bare_explicit_plural_agreement(title):
    assert policy._compatible(report("Norian forces attack Vardi airport"), report(title))


def test_comma_separated_targets_abstain():
    assert policy._compatible(report("Norian-led forces strike Vardi missile facilities"),
                              report("Vardis say they attacked Norian oil pumping stations, Bellport airport"))


def test_compound_proper_actor_does_not_lose_capitalized_role_word():
    assert policy._compatible(report("Norian forces attack Strike airport"),
                              report("Strike Force attacks Norian airport"))


@pytest.mark.parametrize("background", [
    "after Bellport airport attack", "following Bellport airport attack",
    "amid Bellport airport attacks",
])
def test_nonaviation_foreground_cannot_borrow_background_airport(background):
    item = report("Norian-led forces strike Vardi missile facilities " + background,
                  summary="Vardis attacked Norian airport and killed two people.")
    assert policy.recognition_reason(item) == ""


@pytest.mark.parametrize("title, summary, expected", [
    ("Vardis say they attacked Norian airport", "", "aviation_security"),
    ("Flights resume at Bellport airport following attacks", "", "aviation_security"),
    ("Military tensions rise", "Attack at Bellport airport", "aviation_security"),
    ("Norian forces attack Vardi bases after airport attack", "Earthquake kills ten people", "acute_disaster"),
    ("Norian forces attack Vardi bases after airport attack", "Mass shooting reported", "explicit_mass_casualty"),
])
def test_aviation_guard_preserves_direct_unknown_and_other_categories(title, summary, expected):
    assert policy.recognition_reason(report(title, summary=summary)) == expected
