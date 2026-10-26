"""Offline filing-text regressions using reconstructed parser fixtures.

The HTML and four segment examples contain synthetic prose around retained
numeric regression tables. Names identify historical table shapes, not live
filing captures. See tests/fixtures/README.md for provenance and limitations.
The suite retains heading/cross-reference collisions, long sections, glued
commitment rows, four column layouts, reconciliation and deliberate refusal.
"""

import os
import re
import sys

import pytest

# Make scripts/ importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(os.path.dirname(_HERE), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from worldintel.core import sec_filing_text as sft  # noqa: E402

_FIX = os.path.join(_HERE, "fixtures")

AMD_CIK = "0000002488"
AMD_ACCESSION = "0000002488-26-000123"
AMD_DOC = "amd-20260627.htm"
AMD_URL = (
    "https://www.sec.gov/Archives/edgar/data/2488/"
    "000000248826000123/amd-20260627.htm"
)


def load_html():
    with open(os.path.join(_FIX, "amd_10q.htm"), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def amd_html():
    return load_html()


@pytest.fixture(scope="module")
def amd_text(amd_html):
    return sft._extract_text(amd_html)


def load_segment_fixture(name):
    """A reconstructed table-layout example; see fixtures/README.md."""
    with open(os.path.join(_FIX, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def msft_text():
    return load_segment_fixture("msft_10k_segments.txt")


@pytest.fixture(scope="module")
def meta_text():
    return load_segment_fixture("meta_10q_segments.txt")


@pytest.fixture(scope="module")
def amzn_text():
    return load_segment_fixture("amzn_10q_segments.txt")


@pytest.fixture(scope="module")
def aapl_text():
    return load_segment_fixture("aapl_10q_segments.txt")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakeResponse:
    def __init__(self, text="", status=200, headers=None):
        self.text = text
        self.status_code = status
        self.headers = headers or {"Content-Type": "text/html"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("{0} Client Error".format(self.status_code))


class _FakeSession:
    """Records every GET so URL / header / timeout discipline is assertable."""

    def __init__(self, response=None, exc=None):
        self.calls = []
        self._response = response
        self._exc = exc

    def get(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        if self._exc is not None:
            raise self._exc
        return self._response


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch):
    monkeypatch.setattr(sft, "_sleep", lambda _s: None)


# --------------------------------------------------------------------------- #
# Constants and lane budget
# --------------------------------------------------------------------------- #
def test_module_constants_are_this_lanes_own_budget():
    assert sft.LANE_DEADLINE_S == 60.0
    assert sft.DOC_MAX_BYTES == 12_000_000
    assert sft.SECTION_MAX_CHARS == 20_000
    assert sft.QUOTE_MAX_CHARS == 300


# Measured on the deployed copy, MSFT's FY2026 10-K primary document:
# 8,585,501 chars. The old 4 MB cap — calibrated on AMD's 1,348,602-char 10-Q —
# refused it outright, so a bellwether's ANNUAL report was never read. Every
# 10-K is in this size class.
MSFT_10K_CHARS = 8_585_501


def test_the_cap_admits_a_megacap_annual_report_with_headroom():
    """A 10-K is several times the size of the 10-Q the cap was calibrated on.
    The cap exists to refuse a PATHOLOGICAL document, not a large ordinary one;
    the measured cost of admitting MSFT's is 1.5s of a 60s lane budget."""
    assert sft.DOC_MAX_BYTES > MSFT_10K_CHARS
    assert sft.DOC_MAX_BYTES - MSFT_10K_CHARS >= 3_000_000


def test_the_sec_contact_user_agent_matches_the_sibling_lanes():
    from worldintel.core import sec_fundamentals as sf

    assert sft._sec_ua() == sf._sec_ua()
    # Copied, not imported: the SEC modules stay independently loadable.
    assert sft._now is not sf._now


# --------------------------------------------------------------------------- #
# `_extract_text` — parser choice must not change the result
# --------------------------------------------------------------------------- #
def test_both_parsers_produce_byte_identical_text(amd_html):
    """The lxml/html.parser fallback is a speed choice, never a result choice."""
    from bs4 import BeautifulSoup  # noqa: F401

    assert sft._PARSERS == ("lxml", "html.parser")
    with_lxml = sft._extract_text(amd_html, parsers=("lxml",))
    with_builtin = sft._extract_text(amd_html, parsers=("html.parser",))
    assert with_lxml == with_builtin
    assert len(with_lxml) > 20_000


def test_extraction_falls_back_when_the_preferred_parser_is_unavailable(amd_html):
    """A runtime without lxml still parses — same text, via html.parser."""
    expected = sft._extract_text(amd_html, parsers=("html.parser",))
    got = sft._extract_text(amd_html, parsers=("no-such-parser", "html.parser"))
    assert got == expected
    assert "Results of Continuing Operations" in got


def test_extraction_returns_empty_when_every_parser_fails(amd_html):
    assert sft._extract_text(amd_html, parsers=("no-such-parser",)) == ""


def test_extraction_never_raises_on_junk():
    for junk in ("", None, 0, [], {}, b"bytes", "<p>unclosed"):
        assert isinstance(sft._extract_text(junk), str)


# --------------------------------------------------------------------------- #
# `fetch_filing_text`
# --------------------------------------------------------------------------- #
def test_fetch_builds_the_archives_url_and_carries_the_contact_user_agent(amd_html):
    health = {}
    session = _FakeSession(_FakeResponse(amd_html))
    text = sft.fetch_filing_text(
        AMD_CIK, AMD_ACCESSION, AMD_DOC, session, health, deadline_at=1e18
    )
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == AMD_URL
    assert call["headers"].get("User-Agent") == sft._sec_ua()
    assert call["timeout"] == sft._HTTP_TIMEOUT
    assert "Results of Continuing Operations" in text
    assert health == {}


def test_a_404_returns_empty_text_plus_a_health_entry():
    health = {}
    session = _FakeSession(_FakeResponse("", status=404))
    text = sft.fetch_filing_text(
        AMD_CIK, AMD_ACCESSION, AMD_DOC, session, health, deadline_at=1e18
    )
    assert text == ""
    assert health["filing_text"]["ok"] is False
    assert "404" in health["filing_text"]["note"]


def test_a_network_error_returns_empty_text_plus_a_health_entry():
    health = {}
    session = _FakeSession(exc=TimeoutError("read timed out"))
    text = sft.fetch_filing_text(
        AMD_CIK, AMD_ACCESSION, AMD_DOC, session, health, deadline_at=1e18
    )
    assert text == ""
    assert health["filing_text"]["ok"] is False
    assert "read timed out" in health["filing_text"]["note"]


def test_a_pathological_document_is_refused_rather_than_parsed(monkeypatch):
    """The cap is exercised against a MONKEYPATCHED small value, not by
    building a 12 MB string: the behaviour under test is the comparison, and a
    real-size string would only make the suite slow."""
    monkeypatch.setattr(sft, "DOC_MAX_BYTES", 500)
    health = {}
    huge = "<p>" + ("x" * sft.DOC_MAX_BYTES) + "</p>"
    assert len(huge) > sft.DOC_MAX_BYTES
    session = _FakeSession(_FakeResponse(huge))
    text = sft.fetch_filing_text(
        AMD_CIK, AMD_ACCESSION, AMD_DOC, session, health, deadline_at=1e18
    )
    assert text == ""
    assert health["filing_text"]["ok"] is False
    assert "too large" in health["filing_text"]["note"]


def test_a_document_just_under_the_cap_is_processed(monkeypatch):
    """The other side of the same boundary: at the cap the document is read
    normally and no health note is raised."""
    monkeypatch.setattr(sft, "DOC_MAX_BYTES", 500)
    health = {}
    body = "x" * (sft.DOC_MAX_BYTES - len("<p></p>"))
    html = "<p>" + body + "</p>"
    assert len(html) == sft.DOC_MAX_BYTES
    session = _FakeSession(_FakeResponse(html))
    text = sft.fetch_filing_text(
        AMD_CIK, AMD_ACCESSION, AMD_DOC, session, health, deadline_at=1e18
    )
    assert text == body
    assert health == {}


def test_an_exhausted_deadline_returns_empty_and_never_touches_the_network():
    health = {}
    session = _FakeSession(_FakeResponse("<p>never read</p>"))
    text = sft.fetch_filing_text(
        AMD_CIK, AMD_ACCESSION, AMD_DOC, session, health, deadline_at=0.0
    )
    assert text == ""
    assert session.calls == []
    assert health["filing_text"]["ok"] is False


def test_fetch_paces_its_request_like_the_sibling_sec_lanes(monkeypatch):
    slept = []
    monkeypatch.setattr(sft, "_sleep", lambda s: slept.append(s))
    session = _FakeSession(_FakeResponse("<p>body</p>"))
    sft.fetch_filing_text(
        AMD_CIK, AMD_ACCESSION, AMD_DOC, session, {}, deadline_at=1e18
    )
    assert slept == [sft.REQUEST_PACING_S]


def test_fetch_never_raises_on_missing_or_junk_arguments():
    for args in (
        (None, None, None),
        ("", "", ""),
        (AMD_CIK, AMD_ACCESSION, ""),
        ("not-a-cik", AMD_ACCESSION, AMD_DOC),
    ):
        health = {}
        assert sft.fetch_filing_text(
            args[0], args[1], args[2], _FakeSession(_FakeResponse("<p>x</p>")),
            health, deadline_at=1e18
        ) in ("", "x")


def test_a_missing_or_junk_session_degrades_instead_of_going_around_the_pool():
    """The lane's shared session is not optional: five SEC lanes share one IP
    and one rate budget, so a bare `requests.get` fallback is refused."""
    health = {}
    for session in (None, object(), 7, "session"):
        assert sft.fetch_filing_text(
            AMD_CIK, AMD_ACCESSION, AMD_DOC, session, health, deadline_at=1e18
        ) == ""
    assert health["filing_text"]["ok"] is False
    assert len(health["filing_text"]["notes"]) == 4


def test_a_failure_preserves_the_note_of_an_earlier_company_in_the_run():
    health = {"filing_text": {"ok": False, "note": "earlier", "notes": ["earlier"]}}
    session = _FakeSession(_FakeResponse("", status=404))
    sft.fetch_filing_text(
        AMD_CIK, AMD_ACCESSION, AMD_DOC, session, health, deadline_at=1e18
    )
    assert "earlier" in health["filing_text"]["notes"]


# --------------------------------------------------------------------------- #
# `slice_sections` — the last-occurrence rule
# --------------------------------------------------------------------------- #
def test_mdna_selects_the_last_occurrence_not_the_table_of_contents(amd_text):
    """AMD's document names MD&A four times; only the last opens the body."""
    named = re.compile(r"Management[’']s Discussion and Analysis", re.I)
    assert len(named.findall(amd_text)) == 4
    # A fifth, lowercase "Our discussion and analysis of our financial
    # condition ..." is prose, not the section's name, and is not a candidate.
    assert len(re.findall(r"discussion and analysis", amd_text, re.I)) == 5
    sections = sft.slice_sections(amd_text, "10-Q")
    mdna = sections["mdna"]

    # The body: results of operations and the segment table.
    assert "Results of Continuing Operations" in mdna
    assert "Total net revenue" in mdna

    # NOT the table of contents, which lists page numbers.
    assert "Condensed Consolidated Balance Sheets" not in mdna
    assert "Page No." not in mdna

    # NOT the ITEM 2 heading occurrence either — the last one is the last one.
    assert "The statements in this report include forward-looking" not in mdna

    # The slice really starts at the LAST of the four: three are behind it.
    start = amd_text.index(mdna)
    assert start > amd_text.index("Page No.")
    assert len(named.findall(amd_text, 0, start)) == 3
    assert len(named.findall(amd_text, start)) == 1


def test_mdna_is_a_verbatim_contiguous_slice_of_the_document(amd_text):
    mdna = sft.slice_sections(amd_text, "10-Q")["mdna"]
    assert mdna in amd_text


def test_sections_are_capped_at_section_max_chars():
    filler = "word " * 12_000
    text = "Management's Discussion and Analysis of Operations " + filler
    mdna = sft.slice_sections(text, "10-Q")["mdna"]
    assert len(mdna) <= sft.SECTION_MAX_CHARS
    assert len(mdna) > sft.SECTION_MAX_CHARS - 100


def test_a_section_stops_at_the_next_heading_it_knows(amd_text):
    sections = sft.slice_sections(amd_text, "10-Q")
    # Critical estimates is 1.3 KB of its own text, not 20 KB of MD&A body.
    assert "ITEM 1A" not in sections["mdna"]
    assert "Results of Continuing Operations" not in sections["critical_estimates"]
    assert len(sections["critical_estimates"]) < 2_000


def test_critical_estimates_takes_the_heading_not_the_prose_mention(amd_text):
    """AMD says "critical accounting estimates" in running prose AFTER the
    heading. A bare last-occurrence rule would start the section mid-sentence."""
    assert amd_text.lower().count("critical accounting") == 2
    section = sft.slice_sections(amd_text, "10-Q")["critical_estimates"]
    assert section.startswith("Critical Accounting Policies and Estimates")
    assert "Our discussion and analysis of our financial condition" in section


def test_subsequent_events_is_absent_when_the_prose_mention_is_not_a_note(amd_text):
    """AMD's 10-Q has no subsequent-events note; a risk factor says "subsequent
    events may arise". That is prose, not a heading, and yields an ABSENT key."""
    assert "subsequent events may arise" in amd_text
    assert "subsequent_events" not in sft.slice_sections(amd_text, "10-Q")


def test_the_body_opener_rule_is_what_selects_amds_mdna(amd_text):
    """AMD's anchor is chosen because the results narrative follows it by 88
    chars, not because it happens to be last."""
    pattern = dict(sft._SECTION_PATTERNS)["mdna"]
    matches = [m for m in pattern.finditer(amd_text) if sft._is_heading_case(m.group(0))]
    openers = [m for m in matches if sft._opens_the_body(amd_text, m)]
    assert len(matches) == 4
    assert len(openers) == 1
    assert openers[0] is matches[-1]
    assert amd_text.index(sft.slice_sections(amd_text, "10-Q")["mdna"]) == \
        openers[0].start()


def _without_the_10k_cross_reference(text):
    """The fixture minus the single phrase that makes plain last-occurrence
    work for AMD: the cross-reference to the prior 10-K's MD&A that closes
    Critical Accounting Estimates."""
    needle = (
        "in the Management’s Discussion and Analysis of Financial Condition "
        "and Results of Operations section of our Annual Report"
    )
    assert text.count(needle) == 1
    return text.replace(needle, "in that Annual Report")


def test_mdna_survives_a_filer_that_omits_the_10k_cross_reference(amd_text):
    """The case that exposed the coincidence: with that one phrase gone, plain
    last-occurrence collapses to 869 chars of forward-looking boilerplate."""
    variant = _without_the_10k_cross_reference(amd_text)
    pattern = dict(sft._SECTION_PATTERNS)["mdna"]
    matches = [m for m in pattern.finditer(variant) if sft._is_heading_case(m.group(0))]
    assert len(matches) == 3
    assert not [m for m in matches if sft._opens_the_body(variant, m)]

    mdna = sft.slice_sections(variant, "10-Q")["mdna"]
    # The real ITEM 2 heading, not the quoted forward-looking cross-reference.
    assert mdna.startswith("MANAGEMENT’S DISCUSSION AND ANALYSIS")
    assert mdna in variant
    assert "Results of Continuing Operations" in mdna
    assert len(mdna) > 5_000
    assert sft.parse_segments(mdna) == sft.parse_segments(amd_text)


def test_a_container_section_is_not_truncated_by_its_own_subsection(amd_text):
    """Critical Accounting Estimates is part of Item 2. When the MD&A body
    opens BEFORE it, it must not cut the MD&A short of the results narrative."""
    variant = _without_the_10k_cross_reference(amd_text)
    sections = sft.slice_sections(variant, "10-Q")
    assert "Critical Accounting Policies and Estimates" in sections["mdna"]
    assert sft._NESTED_IN.get("critical_estimates") == "mdna"


def test_the_subsection_still_stops_at_the_body_heading_in_the_fallback_path(amd_text):
    """The fallback arrangement — MD&A heading BEFORE Critical Accounting
    Estimates — is the normal one, so this is the usual production case. With
    no marker between them, `critical_estimates` ran to Item 1A: 5,343 chars
    of results narrative published under the critical-estimates label."""
    variant = _without_the_10k_cross_reference(amd_text)
    sections = sft.slice_sections(variant, "10-Q")
    critical = sections["critical_estimates"]
    assert "Results of Continuing Operations" not in critical
    assert "Total net revenue" not in critical
    assert len(critical) < 2_000
    # The boundary is the MD&A body heading, which is never published itself.
    assert "_mdna_body" not in sections
    assert sft._mdna_body_offsets(variant) == [
        variant.index("Results of Continuing Operations")]


_ALL_CAPS_FILING = (
    "PART I, ITEM 2. MANAGEMENT'S DISCUSSION AND ANALYSIS OF FINANCIAL "
    "CONDITION AND RESULTS OF OPERATIONS RESULTS OF OPERATIONS The following "
    "table provides a summary of net revenue and operating income (loss) by "
    "segment: (In millions) Net revenue: Data Center $ 100 $ 90 $ 200 $ 180 "
    "Total net revenue $ 100 $ 90 $ 200 $ 180 Operating income (loss): "
    "Data Center $ 10 $ 9 $ 20 $ 18 Total operating income (loss) $ 10 $ 9"
)


def test_an_all_caps_body_heading_still_opens_the_body():
    """The body-opener rule must not be case-sensitive. Filings that typeset
    the heading as RESULTS OF OPERATIONS are common, and for them a
    case-sensitive rule produces no opener at all and silently falls through."""
    pattern = dict(sft._SECTION_PATTERNS)["mdna"]
    m = pattern.search(_ALL_CAPS_FILING)
    assert sft._opens_the_body(_ALL_CAPS_FILING, m) is True

    mdna = sft.slice_sections(_ALL_CAPS_FILING, "10-Q")["mdna"]
    assert mdna.startswith("MANAGEMENT'S DISCUSSION AND ANALYSIS")
    assert [s["segment"] for s in sft.parse_segments(mdna)] == ["Data Center"]


def test_the_title_case_form_of_the_same_document_behaves_identically():
    title_case = _ALL_CAPS_FILING.replace(
        "MANAGEMENT'S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION AND "
        "RESULTS OF OPERATIONS RESULTS OF OPERATIONS",
        "Management's Discussion and Analysis of Financial Condition and "
        "Results of Operations Results of Operations")
    pattern = dict(sft._SECTION_PATTERNS)["mdna"]
    assert sft._opens_the_body(title_case, pattern.search(title_case)) is True


def test_the_prose_phrase_results_of_operations_never_opens_a_body():
    """`re.I` is what makes the all-caps case work; `_is_heading_case` is what
    stops it admitting prose. Both are required, so both are tested."""
    prose = (
        "Management's Discussion and Analysis of Financial Condition and "
        "Results of Operations The statements in this report may differ from "
        "our actual results of operations for any future period."
    )
    pattern = dict(sft._SECTION_PATTERNS)["mdna"]
    assert sft._opens_the_body(prose, pattern.search(prose)) is False


def test_a_heading_punctuated_with_a_comma_is_not_a_cross_reference():
    """`Part II, Item 1A.` and `Note 14, Subsequent Events` are heading styles.
    Rejecting them loses the section outright — change assertion and all."""
    risks = (
        "PART II, ITEM 1A. RISK FACTORS There have been material changes to "
        "the risk factors previously disclosed. Tariffs could raise our costs."
    )
    section = sft.slice_sections(risks, "10-Q")["risk_changes"]
    assert section.startswith("ITEM 1A. RISK FACTORS")
    assert "Tariffs could raise our costs" in section

    note = "NOTE 14, SUBSEQUENT EVENTS On July 30, 2026, the Company acquired a business."
    assert "acquired a business" in sft.slice_sections(note, "10-Q")["subsequent_events"]


def test_a_referred_designator_is_still_a_cross_reference():
    """The comma exemption must not swallow references that merely lack a
    ", of the Notes" tail: `see Note 14, Subsequent Events in this report`
    published the fragment "Subsequent Events in this report."."""
    both = (
        "NOTE 14, SUBSEQUENT EVENTS On July 30, 2026, the Company acquired a "
        "business. For further detail see Note 14, Subsequent Events in this "
        "report."
    )
    section = sft.slice_sections(both, "10-Q")["subsequent_events"]
    assert section.startswith("SUBSEQUENT EVENTS On July 30")
    assert both.rindex("Subsequent Events") > both.index(section)

    only_the_reference = "For further detail see Note 14, Subsequent Events in this report."
    assert "subsequent_events" not in sft.slice_sections(only_the_reference, "10-Q")


def test_the_heading_style_comma_test_is_about_the_designator_not_the_comma():
    assert sft._is_heading_style_comma("PART II, ") is True
    assert sft._is_heading_style_comma("NOTE 14, ") is True
    assert sft._is_heading_style_comma("see Note 14, ") is False
    assert sft._is_heading_style_comma("described in Part II, ") is False
    assert sft._is_heading_style_comma("our obligations, ") is False


def test_a_quoted_section_name_is_a_cross_reference_not_a_heading(amd_text):
    """AMD's third MD&A occurrence sits inside “Part I, Item 2-...”."""
    pattern = dict(sft._SECTION_PATTERNS)["mdna"]
    matches = [m for m in pattern.finditer(amd_text) if sft._is_heading_case(m.group(0))]
    assert sft._is_cross_reference(amd_text, matches[2]) is True
    assert sft._is_cross_reference(amd_text, matches[1]) is False


def test_a_title_case_cross_reference_never_outranks_the_real_note():
    """In a 10-Q the notes precede MD&A, so a reference back to a note is
    systematically LATER than the note. Last-occurrence alone picks the
    reference."""
    text = (
        "Note 14 Subsequent Events On July 30, 2026, the Company completed an "
        "acquisition. Item 2 Management Discussion. For further detail see "
        "Note 14, Subsequent Events, of the Notes to Financial Statements."
    )
    section = sft.slice_sections(text, "10-Q")["subsequent_events"]
    assert section.startswith("Subsequent Events On July 30")
    assert "completed an acquisition" in section
    # The reference is the LAST occurrence and was still not chosen.
    assert text.rindex("Subsequent Events") > text.index(section)


def test_an_all_caps_disclaimer_is_not_a_subsequent_events_note():
    text = (
        "WE UNDERTAKE NO OBLIGATION TO UPDATE ANY FORWARD-LOOKING STATEMENT "
        "TO REFLECT EVENTS OR CIRCUMSTANCES AFTER THE DATE HEREOF OR FOR "
        "SUBSEQUENT EVENTS OR ANY OTHER MATTER."
    )
    assert "subsequent_events" not in sft.slice_sections(text, "10-Q")


def test_a_real_subsequent_events_note_is_found():
    text = (
        "Note 14 Subsequent Events On July 30, 2026, the Company completed the "
        "acquisition of a business for $1.0 billion in cash."
    )
    section = sft.slice_sections(text, "10-Q")["subsequent_events"]
    assert section.startswith("Subsequent Events")
    assert "completed the acquisition" in section


# --------------------------------------------------------------------------- #
# `slice_sections` — Item 1A
# --------------------------------------------------------------------------- #
_ITEM_1A_NO_CHANGE = (
    "ITEM 1A. RISK FACTORS There have been no material changes to the risk "
    "factors previously disclosed in our Annual Report on Form 10-K for the "
    "fiscal year ended December 27, 2025."
)


def test_item_1a_boilerplate_yields_an_absent_key_not_an_empty_string():
    sections = sft.slice_sections(_ITEM_1A_NO_CHANGE, "10-Q")
    assert "risk_changes" not in sections
    assert sections.get("risk_changes", "SENTINEL") == "SENTINEL"


def test_item_1a_is_published_when_the_section_asserts_a_change():
    text = (
        "ITEM 1A. RISK FACTORS The following risk factor has been updated since "
        "our Annual Report on Form 10-K. Export controls on our data center "
        "products could materially harm our business."
    )
    section = sft.slice_sections(text, "10-Q")["risk_changes"]
    assert "Export controls" in section


def test_item_1a_is_published_when_boilerplate_carries_an_exception():
    text = (
        "ITEM 1A. RISK FACTORS There have been no material changes to the risk "
        "factors previously disclosed, except as set forth below. A new tariff "
        "regime could increase our cost of sales."
    )
    assert "A new tariff regime" in sft.slice_sections(text, "10-Q")["risk_changes"]


def test_a_quarterly_restatement_of_unchanged_risks_is_not_a_change(amd_text):
    """AMD restates its full risk factors every quarter without asserting that
    any changed. Publishing 20 KB of unchanged boilerplate is exactly the noise
    the absent-key rule exists to prevent."""
    assert "ITEM 1A. RISK FACTORS" in amd_text
    assert "risk_changes" not in sft.slice_sections(amd_text, "10-Q")


def test_the_change_gate_applies_to_the_annual_form_too(amd_text):
    """The key is named `risk_changes`. Publishing all of a 10-K's Item 1A
    under it would report "risks changed" for every 10-K ever filed."""
    for form in ("10-K", "10-K/A", "10-Q", None):
        assert "risk_changes" not in sft.slice_sections(amd_text, form)


def test_the_annual_form_still_publishes_a_real_change_assertion():
    text = (
        "ITEM 1A. RISK FACTORS The following risk factor has been added since "
        "our last Annual Report. Export controls could harm our business."
    )
    for form in ("10-K", "10-Q"):
        assert "Export controls" in sft.slice_sections(text, form)["risk_changes"]


# --------------------------------------------------------------------------- #
# `slice_sections` — degradation and purity
# --------------------------------------------------------------------------- #
def test_slice_sections_returns_an_empty_dict_when_nothing_is_found():
    assert sft.slice_sections("The quick brown fox jumped.", "10-Q") == {}


def test_slice_sections_never_raises_on_junk():
    for junk in ("", None, 0, [], {}, 3.5):
        assert sft.slice_sections(junk, "10-Q") == {}
    assert isinstance(sft.slice_sections("Subsequent Events happened.", None), dict)


def test_slice_sections_is_pure_and_deterministic(amd_text):
    first = sft.slice_sections(amd_text, "10-Q")
    second = sft.slice_sections(amd_text, "10-Q")
    assert first == second
    assert set(first) <= {"mdna", "risk_changes", "subsequent_events",
                          "critical_estimates"}


# --------------------------------------------------------------------------- #
# `parse_segments`
# --------------------------------------------------------------------------- #
def test_amd_segment_table_reproduces_the_filed_quarter(amd_text):
    segments = sft.parse_segments(amd_text)
    by_name = {s["segment"]: s for s in segments}
    assert set(by_name) == {"Data Center", "Client and Gaming", "Embedded"}

    dc = by_name["Data Center"]
    assert dc["revenue"] == 6718.0
    assert dc["revenue_prior"] == 3240.0
    assert dc["op_income"] == 2103.0
    assert dc["op_income_prior"] == -155.0

    cg = by_name["Client and Gaming"]
    assert cg["revenue"] == 3841.0
    assert cg["revenue_prior"] == 3621.0
    assert cg["op_income"] == 582.0
    assert cg["op_income_prior"] == 767.0

    emb = by_name["Embedded"]
    assert emb["revenue"] == 977.0
    assert emb["revenue_prior"] == 824.0
    assert emb["op_income"] == 386.0
    assert emb["op_income_prior"] == 275.0


def test_segments_read_the_three_month_columns_not_the_six_month_columns(amd_text):
    """AMD's table carries four numeric columns per row; the six-month figures
    (12,493 / 6,914 for Data Center) must never be published as the quarter."""
    values = set()
    for seg in sft.parse_segments(amd_text):
        values.update(v for v in seg.values() if isinstance(v, float))
    assert 12493.0 not in values
    assert 6914.0 not in values
    assert 7446.0 not in values


def test_segments_parse_from_the_mdna_slice_as_well_as_the_whole_document(amd_text):
    mdna = sft.slice_sections(amd_text, "10-Q")["mdna"]
    assert sft.parse_segments(mdna) == sft.parse_segments(amd_text)


def test_reconciling_rows_are_not_published_as_segments(amd_text):
    """"All other" and the "Total" rows are reconciling items, not segments."""
    names = [s["segment"] for s in sft.parse_segments(amd_text)]
    assert not [n for n in names if n.lower().startswith("total")]
    assert not [n for n in names if n.lower().startswith("all other")]


def test_every_published_segment_carries_the_full_entry_shape(amd_text):
    for seg in sft.parse_segments(amd_text):
        assert set(seg) == {"segment", "revenue", "revenue_prior",
                            "op_income", "op_income_prior"}
        assert isinstance(seg["segment"], str) and seg["segment"]
        assert isinstance(seg["revenue"], float)
        assert isinstance(seg["revenue_prior"], float)


def test_an_unparseable_table_returns_empty_rather_than_a_guess():
    unparseable = (
        "The following table provides a summary of net revenue and operating "
        "income (loss) by segment: Three Months Ended Net revenue: Data Center "
        "n/a n/a Embedded n/a n/a"
    )
    assert sft.parse_segments(unparseable) == []


def test_a_segment_without_a_matching_revenue_row_refuses_the_whole_table():
    """Partial is not an option: the table is published whole or not at all."""
    text = (
        "The following table provides a summary of net revenue and operating "
        "income (loss) by segment: Net revenue: Data Center $ 100 $ 90 $ 200 "
        "$ 180 Total net revenue $ 100 $ 90 $ 200 $ 180 "
        "Operating income (loss): Data Center $ 10 $ 9 $ 20 $ 18 "
        "Ghost Segment $ 5 $ 4 $ 10 $ 8 "
        "Total operating income (loss) $ 15 $ 13 $ 30 $ 26"
    )
    assert sft.parse_segments(text) == []


def test_parse_segments_returns_empty_when_the_anchor_is_absent(amd_text):
    assert sft.parse_segments("Revenue rose sharply this quarter.") == []
    assert sft.parse_segments(amd_text[:2000]) == []


def test_parse_segments_never_raises_on_junk():
    for junk in ("", None, 0, [], {}, 3.5, "$$$ , , ("):
        assert sft.parse_segments(junk) == []


def test_parse_segments_is_pure_and_deterministic(amd_text):
    assert sft.parse_segments(amd_text) == sft.parse_segments(amd_text)


# --------------------------------------------------------------------------- #
# `parse_segments` — the other four table shapes
#
# Every number below is the cell the filing itself prints, in the unit its own
# caption states (all four say "in millions"). Nothing is scaled.
# --------------------------------------------------------------------------- #
def test_msft_10k_segment_table_reproduces_the_filed_year(msft_text):
    """Per-segment VERTICAL blocks: a segment name, then its own Revenue /
    Cost of revenue / Operating expenses / Operating income rows. Two value
    columns (FY2026, FY2025) plus a percentage-change column that must not be
    read as a value."""
    by_name = {s["segment"]: s for s in sft.parse_segments(msft_text)}
    assert set(by_name) == {"Productivity and Business Processes",
                            "Intelligent Cloud", "More Personal Computing"}

    pbp = by_name["Productivity and Business Processes"]
    assert (pbp["revenue"], pbp["revenue_prior"]) == (139996.0, 120810.0)
    assert (pbp["op_income"], pbp["op_income_prior"]) == (83879.0, 69773.0)

    ic = by_name["Intelligent Cloud"]
    assert (ic["revenue"], ic["revenue_prior"]) == (137791.0, 106265.0)
    assert (ic["op_income"], ic["op_income_prior"]) == (56972.0, 44589.0)

    mpc = by_name["More Personal Computing"]
    assert (mpc["revenue"], mpc["revenue_prior"]) == (54052.0, 54649.0)
    assert (mpc["op_income"], mpc["op_income_prior"]) == (14386.0, 14166.0)


def test_msft_percentage_change_column_is_never_published_as_a_figure(msft_text):
    """MSFT prints "16%", "(1)%" and "20%" alongside the two value columns. A
    percentage read as a value is a wrong number, which is the one failure
    mode this module refuses outright."""
    values = set()
    for seg in sft.parse_segments(msft_text):
        values.update(v for v in seg.values() if isinstance(v, float))
    assert not [v for v in values if abs(v) < 1000]


def test_meta_10q_segment_table_reproduces_the_filed_quarter(meta_text):
    """META prints its revenue and its operating income in TWO SEPARATE flat
    tables, thousands of characters apart, with "% change" columns between the
    value columns and sub-lines (Advertising, Other revenue) that roll up into
    Family of Apps and must not be published as segments."""
    segments = sft.parse_segments(meta_text)
    by_name = {s["segment"]: s for s in segments}
    assert set(by_name) == {"Family of Apps", "Reality Labs"}

    foa = by_name["Family of Apps"]
    assert (foa["revenue"], foa["revenue_prior"]) == (60370.0, 47146.0)
    assert (foa["op_income"], foa["op_income_prior"]) == (23394.0, 24971.0)

    rl = by_name["Reality Labs"]
    assert (rl["revenue"], rl["revenue_prior"]) == (431.0, 370.0)
    # Reality Labs loses money; the parentheses are a minus sign.
    assert (rl["op_income"], rl["op_income_prior"]) == (-4619.0, -4530.0)


def test_meta_revenue_sublines_are_not_published_as_segments(meta_text):
    names = [s["segment"] for s in sft.parse_segments(meta_text)]
    assert "Advertising" not in names
    assert "Other revenue" not in names


def test_amzn_10q_segment_table_reproduces_the_filed_quarter(amzn_text):
    by_name = {s["segment"]: s for s in sft.parse_segments(amzn_text)}
    assert set(by_name) == {"North America", "International", "AWS"}

    na = by_name["North America"]
    assert (na["revenue"], na["revenue_prior"]) == (116177.0, 100068.0)
    assert (na["op_income"], na["op_income_prior"]) == (9123.0, 7517.0)

    intl = by_name["International"]
    assert (intl["revenue"], intl["revenue_prior"]) == (42197.0, 36761.0)
    assert (intl["op_income"], intl["op_income_prior"]) == (1717.0, 1494.0)

    aws = by_name["AWS"]
    assert (aws["revenue"], aws["revenue_prior"]) == (42232.0, 30873.0)
    assert (aws["op_income"], aws["op_income_prior"]) == (16621.0, 10160.0)


def test_amzn_prints_the_prior_year_column_first_and_is_not_read_reversed(amzn_text):
    """AMZN's columns run 2025, 2026 — the OPPOSITE of AMD, MSFT and META. A
    parser that assumes current-first publishes every figure backwards, and
    every one of them is a plausible-looking number. The column is chosen from
    the table's own year header, never from position."""
    na = {s["segment"]: s for s in sft.parse_segments(amzn_text)}["North America"]
    assert na["revenue"] == 116177.0      # 2026, the later year
    assert na["revenue_prior"] == 100068.0  # 2025
    assert na["revenue"] > na["revenue_prior"]


def test_apple_geographic_segments_are_deliberately_not_published(aapl_text):
    """AAPL is a REFUSAL, and a considered one.

    Apple's Note 10 table is TRANSPOSED — the segments are columns
    (Americas / Europe / Greater China / Japan / Rest of Asia Pacific /
    Corporate / Total) and the line items are rows — so no row in it is a
    segment. The one row-shaped table Apple offers, MD&A's "net sales by
    reportable segment", carries NET SALES ONLY: there is no operating income
    to establish a roster from. And two pages later Apple prints "net sales by
    category" (iPhone / Mac / iPad / Wearables / Services) in the byte-identical
    shape, summing to the same Total net sales. A reader that accepted a
    revenue-only table on shape alone would publish "iPhone" as an operating
    segment with equal confidence. [] is the correct answer, not a gap.
    """
    assert sft.parse_segments(aapl_text) == []
    # Both tables really are in the fixture; the refusal is not a missing input.
    assert "net sales by reportable segment" in aapl_text
    assert "Greater China" in aapl_text
    assert "net sales by category" in aapl_text


# A minimal vertical-block table, in both column orders. Same facts, opposite
# printing convention: the published result must be identical.
_VERTICAL_CURRENT_FIRST = (
    "Information on reportable segments and reconciliation to consolidated net "
    "income is as follows (in millions): Three Months Ended June 30, 2026 2025 "
    "Alpha Net sales $ 100 $ 90 Operating expenses 90 81 Operating income $ 10 $ 9 "
    "Beta Net sales $ 50 $ 40 Operating expenses 45 36 Operating income $ 5 $ 4 "
    "Consolidated Net sales $ 150 $ 130 Operating expenses 135 117 "
    "Operating income $ 15 $ 13"
)
_VERTICAL_PRIOR_FIRST = (
    "Information on reportable segments and reconciliation to consolidated net "
    "income is as follows (in millions): Three Months Ended June 30, 2025 2026 "
    "Alpha Net sales $ 90 $ 100 Operating expenses 81 90 Operating income $ 9 $ 10 "
    "Beta Net sales $ 40 $ 50 Operating expenses 36 45 Operating income $ 4 $ 5 "
    "Consolidated Net sales $ 130 $ 150 Operating expenses 117 135 "
    "Operating income $ 13 $ 15"
)


def test_the_year_header_decides_which_column_is_the_current_period():
    current_first = sft.parse_segments(_VERTICAL_CURRENT_FIRST)
    prior_first = sft.parse_segments(_VERTICAL_PRIOR_FIRST)
    assert current_first == prior_first
    assert [s["segment"] for s in current_first] == ["Alpha", "Beta"]
    assert current_first[0]["revenue"] == 100.0
    assert current_first[0]["revenue_prior"] == 90.0
    assert current_first[0]["op_income"] == 10.0
    assert current_first[0]["op_income_prior"] == 9.0


def test_a_table_with_no_year_header_is_refused_rather_than_assumed():
    """Without the years there is nothing that says which column is current,
    and guessing would publish the wrong period under the right label."""
    no_years = _VERTICAL_CURRENT_FIRST.replace(
        "Three Months Ended June 30, 2026 2025 ", "Three Months Ended ")
    assert sft.parse_segments(no_years) == []


def test_a_table_that_does_not_add_up_to_its_own_total_is_refused():
    """The segments plus the reconciling rows must equal the table's own total
    row. When they do not, either a row was missed or the wrong column was
    read — both of which would publish a share-of-total that is wrong."""
    broken = _VERTICAL_CURRENT_FIRST.replace(
        "Consolidated Net sales $ 150 $ 130", "Consolidated Net sales $ 900 $ 130")
    assert sft.parse_segments(broken) == []


def test_a_vertical_table_without_a_total_row_is_refused():
    """No total row means no arithmetic check, and this parser does not publish
    a table it could not verify."""
    headless = _VERTICAL_CURRENT_FIRST.replace(
        "Consolidated Net sales", "Gamma Net sales")
    assert sft.parse_segments(headless) == []


def test_a_lone_income_statement_is_not_mistaken_for_a_segment_table():
    """"Revenue ... Operating income" is also the shape of the income
    statement. One block is not a table of segments."""
    income_statement = (
        "Condensed Consolidated Statements of Operations (In millions) "
        "Three Months Ended June 30, 2026 2025 "
        "Revenue $ 150 $ 130 Cost of revenue 60 55 Operating expenses 75 62 "
        "Operating income $ 15 $ 13"
    )
    assert sft.parse_segments(income_statement) == []


def test_the_new_readers_never_raise_on_junk():
    for junk in ("", None, 0, [], {}, 3.5, "Net sales $ Operating income $",
                 "Alpha Net sales $ 1 Operating income $ 2"):
        assert sft.parse_segments(junk) == []


def test_every_issuer_publishes_the_full_entry_shape(msft_text, meta_text, amzn_text):
    for text in (msft_text, meta_text, amzn_text):
        segments = sft.parse_segments(text)
        assert segments
        for seg in segments:
            assert set(seg) == {"segment", "revenue", "revenue_prior",
                                "op_income", "op_income_prior"}
            assert isinstance(seg["segment"], str) and seg["segment"]
            for key in ("revenue", "revenue_prior", "op_income",
                        "op_income_prior"):
                assert isinstance(seg[key], float)


def test_no_reconciling_row_reaches_the_published_roster(msft_text, meta_text,
                                                        amzn_text):
    for text in (msft_text, meta_text, amzn_text):
        for seg in sft.parse_segments(text):
            low = seg["segment"].lower()
            assert not low.startswith(sft._RECONCILING)


# --------------------------------------------------------------------------- #
# `find_disclosures`
# --------------------------------------------------------------------------- #
_MDNA_LEASE_QUOTE = (
    "Synthetic leases already active total $1.2 billion and synthetic leases "
    "not yet commenced total $4.5 billion."
)
# The synthetic note deliberately glues an unscaled table row to a sentence
# with no figure; only the following sentence supplies a stated magnitude.
_NOTE_LEASE_TAIL = "Synthetic laboratory leases have not yet commenced."
_PURCHASE_OBLIGATIONS_QUOTE = (
    "Synthetic purchase obligations depend on future orders and do not state "
    "a monetary amount in this sentence."
)
_LETTERS_OF_CREDIT_QUOTE = (
    "The synthetic facility allows $ 250 million for letters of credit."
)


def test_amd_disclosures_are_the_families_the_filing_actually_carries(amd_text):
    found = sft.find_disclosures(amd_text)
    terms = [d["term"] for d in found]
    assert terms == ["letters of credit", "not yet commenced",
                     "not yet commenced", "purchase obligations"]
    # Families the spec scans for that AMD simply does not disclose.
    assert "residual value guarantee" not in terms
    assert "variable interest entity" not in terms


def test_every_quote_is_a_verbatim_substring_of_the_filing(amd_text):
    for d in sft.find_disclosures(amd_text):
        assert d["quote"] in amd_text
        assert len(d["quote"]) <= sft.QUOTE_MAX_CHARS
        assert d["term"].split()[0].lower() in d["quote"].lower()


def test_a_sentence_carrying_no_figure_yields_amount_none(amd_text):
    """Note 10's "not yet commenced" sentence states no amount — the $4.5
    billion sits in the NEXT sentence. The module must not reach for it."""
    found = sft.find_disclosures(amd_text)
    note_hit = [d for d in found if d["quote"].endswith(_NOTE_LEASE_TAIL)]
    assert len(note_hit) == 1
    assert note_hit[0]["amount"] is None
    # The quote carries the commitments row on its front, verbatim and visible.
    # Those cells ($ 17,386 ... $ 30,276) are millions with no magnitude word,
    # and NONE of them may reach `amount` — that is the whole narrowing.
    assert "$ 30,276" in note_hit[0]["quote"]
    assert note_hit[0]["quote"] in amd_text
    assert len(note_hit[0]["quote"]) <= sft.QUOTE_MAX_CHARS

    po = [d for d in found if d["term"] == "purchase obligations"]
    assert po[0]["quote"] == _PURCHASE_OBLIGATIONS_QUOTE
    assert po[0]["amount"] is None


def test_the_figure_the_sentence_attaches_to_the_term_is_reported(amd_text):
    """The MD&A sentence carries TWO figures; the one that belongs to
    "not yet commenced" is the $4.5 billion that follows it, not the $1.2
    billion of leases that HAVE commenced."""
    found = sft.find_disclosures(amd_text)
    hit = [d for d in found if d["quote"] == _MDNA_LEASE_QUOTE]
    assert len(hit) == 1
    assert hit[0]["amount"] == 4_500_000_000.0


def test_a_sole_figure_ahead_of_the_term_is_still_that_sentences_figure(amd_text):
    hit = [d for d in sft.find_disclosures(amd_text)
           if d["term"] == "letters of credit"]
    assert len(hit) == 1
    assert hit[0]["quote"] == _LETTERS_OF_CREDIT_QUOTE
    assert hit[0]["amount"] == 250_000_000.0


def test_an_ambiguous_sentence_reports_no_amount_rather_than_picking_one():
    text = (
        "Our residual value guarantees of $3.0 billion and $2.0 billion were "
        "outstanding."
    )
    found = sft.find_disclosures(text)
    assert len(found) == 1
    assert found[0]["term"] == "residual value guarantee"
    assert found[0]["amount"] is None


def test_a_bare_figure_is_never_published_however_it_is_punctuated():
    """The nine sentences that defeated three successive lexical proxies: the
    three I-1 residuals, and the six refusals that round 2's binder guard turned
    into published figures by suppressing the split that had protected them.
    Every one states $17.386bn as a millions-scale table cell. Under the
    narrowed contract all nine refuse for one reason — the figure is bare."""
    sentences = [
        # I-1 residuals: the split cannot see these boundaries.
        "Fiscal Year 2027 Total unconditional purchase obligations $ 17,386 (1) "
        "amounts shown exclude obligations under our wafer supply agreement.",
        "Total unconditional purchase obligations $ 17,386 excludes amounts "
        "payable under our wafer supply agreement.",
        "Fiscal Year Remainder of 2026 2027 2028 Total purchase obligations "
        "$ 17,386 5,012 3,001 as of the balance sheet date.",
        # Round-2 regressions: a label ending in a binder or an -ed/-ing word
        # blocked the split, leaving one figure in a glued sentence.
        "amounts recognized $ 17,386 The Company has purchase obligations "
        "extending beyond one year.",
        "Purchase obligations outstanding $ 17,386 The Company expects to "
        "settle these in the ordinary course.",
        "Total $ 17,386 The Company has purchase obligations extending beyond "
        "one year.",
        "Purchase obligations included $ 17,386 The Company reports these "
        "under contractual commitments.",
        "Leases not yet commenced $ 17,386 The Company expects these to begin "
        "in the second half.",
        "Purchase obligations of $ 17,386 The Company has disclosed these in "
        "its commitments note.",
    ]
    for sentence in sentences:
        found = sft.find_disclosures(sentence)
        assert found, sentence
        assert found[0]["amount"] is None, sentence
        # The quote still carries whatever the filing said, verbatim.
        assert found[0]["quote"] in sentence


def test_a_bare_figure_refuses_even_with_no_table_material_in_sight():
    text = "Our purchase obligations were $ 1,200 at the balance sheet date."
    found = sft.find_disclosures(text)
    assert found[0]["amount"] is None
    assert found[0]["quote"] == text


def test_a_figure_that_states_its_own_magnitude_is_published():
    """The other half of the rule: the magnitude word IS the figure's scale, so
    a stated magnitude is publishable wherever it sits."""
    text = (
        "The following table sets out our commitments (in millions). "
        "Our purchase obligations were $1.2 billion at quarter end."
    )
    found = sft.find_disclosures(text)
    assert len(found) == 1
    assert found[0]["amount"] == 1_200_000_000.0


def test_the_guards_leave_an_ordinary_stated_figure_alone():
    text = "Our residual value guarantees totalled $2.5 billion at quarter end."
    assert sft.find_disclosures(text)[0]["amount"] == 2_500_000_000.0


def test_a_named_instrument_after_a_figure_keeps_the_sentence_whole():
    """A standing guard against reintroducing a table-cell split. When one
    existed, this sentence was cut after "$3.0 billion" — turning a correct
    refusal into a confident wrong amount under a quote that read as a complete
    statement of the total."""
    text = (
        "Our residual value guarantees of $3.0 billion Senior Notes due 2030 "
        "and $2.0 billion of other obligations were outstanding."
    )
    found = sft.find_disclosures(text)
    assert len(found) == 1
    assert found[0]["amount"] is None
    assert found[0]["quote"] == text


def test_the_module_never_totals_across_disclosure_families(amd_text):
    found = sft.find_disclosures(amd_text)
    assert isinstance(found, list)
    for d in found:
        assert set(d) == {"term", "quote", "amount"}
    # No aggregate row smuggled in under a family name.
    assert len({id(d) for d in found}) == len(found)
    assert not [d for d in found if "total" in d["term"].lower()]


def test_every_named_family_is_scanned_for():
    canonical = [t for t, _p in sft.DISCLOSURE_TERMS]
    for family in ("residual value guarantee", "variable interest entity",
                   "primary beneficiary", "not yet commenced",
                   "purchase obligations", "unconsolidated joint venture",
                   "keepwell", "letters of credit", "off-balance sheet"):
        assert family in canonical


def test_a_long_sentence_is_cut_to_a_verbatim_window_around_the_term():
    filler = "the quarter was uneventful and nothing else happened at all "
    text = filler * 8 + "we had a keepwell agreement in place. " + filler * 8
    found = sft.find_disclosures(text)
    assert len(found) == 1
    quote = found[0]["quote"]
    assert len(quote) <= sft.QUOTE_MAX_CHARS
    assert quote in text
    assert "keepwell" in quote


def test_the_same_term_twice_in_one_sentence_reports_once():
    text = ("Our purchase obligations are material and our purchase "
            "obligations continue to grow.")
    assert len(sft.find_disclosures(text)) == 1


def test_find_disclosures_never_raises_on_junk():
    for junk in ("", None, 0, [], {}, 3.5):
        assert sft.find_disclosures(junk) == []


def test_find_disclosures_is_pure_and_deterministic(amd_text):
    assert sft.find_disclosures(amd_text) == sft.find_disclosures(amd_text)


# --------------------------------------------------------------------------- #
# End to end against the recorded fixture
# --------------------------------------------------------------------------- #
def test_the_lane_runs_end_to_end_from_one_recorded_response(amd_html):
    health = {}
    session = _FakeSession(_FakeResponse(amd_html))
    text = sft.fetch_filing_text(
        AMD_CIK, AMD_ACCESSION, AMD_DOC, session, health, deadline_at=1e18
    )
    sections = sft.slice_sections(text, "10-Q")
    segments = sft.parse_segments(sections["mdna"])
    disclosures = sft.find_disclosures(text)

    assert health == {}
    assert set(sections) == {"mdna", "critical_estimates"}
    assert [s["segment"] for s in segments] == [
        "Data Center", "Client and Gaming", "Embedded"]
    assert [d["amount"] for d in disclosures] == [
        250_000_000.0, None, 4_500_000_000.0, None]
