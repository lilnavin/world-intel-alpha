import json

from worldintel.core import enrich, select_news, coverage_gate


BODY = ('Officials confirmed that the aircraft landed safely after passengers restrained the attacker. ' * 15)


def test_reporting_beats_picture_and_video_formats():
    article = {'title': 'Earthquake strikes capital', 'tier': 1, 'url': 'https://example.org/a'}
    for title in ('Earthquake strikes capital: week in pictures', 'Earthquake strikes capital: video programme'):
        other = dict(article, title=title)
        assert select_news._score(article, {}, set()) > select_news._score(other, {}, set())


def test_player_and_programme_metadata_are_not_article():
    noise = 'To play this video please enable JavaScript and upgrade your browser. '
    assert not enrich._extract_text('<p>' + noise * 30 + '</p>')
    metadata = 'In this programme: Canada elections; Japan markets; France protests. '
    assert not enrich._extract_text('<p>' + metadata * 30 + '</p>')


def test_complete_transcript_survives_cleaning():
    assert enrich._extract_text('<article><p>Transcript</p><p>' + BODY + '</p></article>') == BODY.strip()


def test_cookie_navigation_removed_without_losing_article():
    html = '<nav><p>' + BODY + '</p></nav><p>Accept all cookies to continue.</p><p>' + BODY + '</p>'
    assert enrich._extract_text(html) == BODY.strip()


def test_journalism_about_cookie_policy_is_preserved():
    text = 'Officials said users must accept all cookies under the disputed policy. The policy faces legal challenges.'
    assert enrich.clean_reporting_text(text) == text


def test_unpunctuated_player_controls_are_not_substantive():
    control = 'To play this video please enable JavaScript and upgrade your browser'
    assert not enrich._extract_text(('<p>' + control + '</p>') * 30)
    assert not enrich.substantive_content((control + ' ') * 30)


def test_short_standalone_player_control_variants_are_not_substantive():
    for control in ('Please enable JavaScript.', 'Please enable JavaScript',
                    'Please enable JavaScript to play this video.',
                    'Please enable JavaScript and upgrade your browser'):
        text = (control + ' ') * 50
        assert not enrich.clean_reporting_text(text)
        assert not enrich._extract_text('<p>' + text + '</p>')
        assert not enrich.substantive_content(text)


def test_embedded_controls_in_journalism_are_preserved():
    for text in (
        'Officials said users can manage their accounts and manage cookie preferences under the new policy.',
        'Officials said the message "Please enable JavaScript." confused customers.',
        'Please enable JavaScript is the instruction officials said caused confusion.',
    ):
        assert enrich.clean_reporting_text(text) == text
        assert enrich._extract_text('<p>' + text + '</p>') == text


def test_sentence_start_cookie_journalism_is_preserved():
    text = 'Accept all cookies is the requirement imposed by the disputed policy. Officials said the policy faces legal challenges.'
    assert enrich.clean_reporting_text(text) == text
    assert enrich._extract_text('<p>' + text + '</p>') == text


def test_repair_prompt_excludes_selected_source_below_word_floor():
    items = [{'url': 'https://example.org/' + name, 'title': name, 'source': 'Example', 'tier': 1, 'content': content}
             for name, content in [('article', BODY), ('shortwords', 'longword' * 110)]]
    events = [{'id': 'event', 'selected_urls': [i['url'] for i in items]}]
    prompts = []
    coverage_gate.enforce({'briefing': {'brief': []}}, {'news': items}, events,
                          repair_fn=lambda prompt, timeout: prompts.append(prompt) or [])
    assert len(prompts) == 1
    payload = json.loads(prompts[0].split('source text: ', 1)[1])
    assert [source['url'] for source in payload[0]['sources']] == [items[0]['url']]


def test_raw_player_instructions_cannot_reappear_as_fallback():
    item = {'raw_body': '<p>' + 'To play this video please enable JavaScript. ' * 40 + '</p>'}
    assert not enrich._content_for(item, None, 8)


def test_no_vetted_alternate_records_reason_offline():
    failed = {'url': 'https://example.org/a', 'desk': 'world', 'tier': 1}
    event = {'urls': [failed['url']], 'selected_urls': [failed['url']]}
    enrich.enrich_protected_alternates([], {'world': [failed]}, [event])
    assert event['enrichment_stop_reason'] == 'no_vetted_alternate'


def test_word_floor_applies_to_enrichment():
    item = {'raw_body': 'longword' * 110, 'url': 'https://example.org/a'}
    enrich.enrich_news({'news': [item]}, candidates={'world': [item]})
    assert 'content' not in item


def test_alternate_deadline_records_failure_without_fetch():
    failed = {'url': 'https://example.org/a', 'desk': 'world', 'tier': 1}
    alternate = dict(failed, url='https://example.org/b')
    event = {'urls': [failed['url'], alternate['url']], 'selected_urls': [failed['url']]}
    class Session:
        def get(self, *args, **kwargs):
            raise AssertionError('network forbidden')
    selected = {'world': [failed]}
    enrich.enrich_protected_alternates([alternate], selected, [event], session=Session(), deadline=10, clock=lambda: 10)
    assert selected['world'] == [failed]
    assert event['enrichment_attempts'][-1]['reason'] == 'enrichment_budget_exhausted'
