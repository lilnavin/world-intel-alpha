"""SEC filing-document text layer — what the press release never carried.

`sec_fundamentals.py` and `anomaly_panel.py` read the XBRL APIs on
`data.sec.gov`; neither ever fetches a filing DOCUMENT. This module does. When a
company files its 10-Q or 10-K days after reporting earnings, the filing carries
things the earnings release did not: segment revenue *and operating income*, the
MD&A narrative, whether Item 1A changed, and the off-balance-sheet disclosures
that never reach a tagged fact. That second beat is what this module supplies.

ABSOLUTE RULE (same house rule as fetch_sources.py / earnings_report.py /
sec_fundamentals.py / anomaly_panel.py): nothing here may raise. A 404, a
timeout, malformed HTML, a missing section, a segment table this parser has
never seen — all degrade to empty output ("" / [] / {}) plus a health entry
under `health["filing_text"]`. There is no exception path out of this module.

FOUR PUBLIC FUNCTIONS, ONE OF WHICH TOUCHES THE NETWORK:

    fetch_filing_text(...)   the single HTTP seam
    slice_sections(text, form)   pure
    find_disclosures(text)       pure
    parse_segments(text)         pure

`_sec_ua()` resolves shared configuration at call time. `REQUEST_PACING_S`
remains local (house rule: the SEC modules stay independently loadable).
They must stay in step: earnings_report, sec_fundamentals, ownership,
anomaly_panel and this module all hit SEC from the same IP, and a block takes
all five down at once.

THIS MODULE REPORTS; IT NEVER ESTIMATES. `find_disclosures` returns the
filing's own sentences and the figure those sentences state. Where a sentence
carries no figure — or carries more than one and does not say which belongs to
the term — `amount` is None and the caller simply does not compute the ratio.
It never totals across disclosure families. `parse_segments` returns [] rather
than a partial or inferred table.

WHAT THIS MODULE PROMISES, AND WHAT IT DOES NOT
-----------------------------------------------
`_extract_text` flattens the document with `get_text(" ")`, which destroys the
table/prose boundary before any rule in this module can see it. A filing's
table cells and the prose after them arrive as one undifferentiated string.
Everything below follows from that, and two things are calibrated to it:

* **`amount` is published only when the filing states the magnitude** —
  "$4.5 billion" yes, "$ 17,386" never, because the latter's scale lives in an
  "(In millions)" caption the flattened text can no longer attach to it. A bare
  figure yields `amount: None` with the verbatim quote intact. Three successive
  lexical proxies for the boundary were tried and retired; do not add a fourth.

* **A section anchor is a best effort, not an exact one.** `slice_sections` may
  select any valid occurrence of a heading and may occasionally return a
  cross-reference fragment instead of the body. Its consumer is a model reading
  prose, so a wrong anchor costs RELEVANCE, not correctness — the wrong
  paragraph, never a wrong number. That asymmetry is why heuristics are
  acceptable here and were removed from the amount path.
"""

from __future__ import annotations

from worldintel import config as wi_config
import re
import time

# --------------------------------------------------------------------------- #
# Endpoints and tuning constants (fixed by the design spec, §4)
# --------------------------------------------------------------------------- #
SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"

LANE_DEADLINE_S = 60.0         # this lane's own budget; nothing shares it

# THE CAP REFUSES A PATHOLOGICAL DOCUMENT, NOT A LARGE ORDINARY ONE.
#
# 4_000_000 was calibrated on AMD's 10-Q (1,348,602 chars) and was wrong for
# the whole 10-K class: MSFT's FY2026 annual report is 8,585,501 chars, so a
# bellwether's ANNUAL filing — the one that carries the segment table, the full
# MD&A and Item 1A — was refused outright every year, with only a health note
# to show for it. Every megacap 10-K sits in that size class.
#
# The cap was guarding against a cost that is not there. Measured on the
# deployed copy against MSFT's 10-K: fetch 0.1s, `_extract_text` 1.4s,
# slice_sections + parse_segments + find_disclosures 0.0s — 1.5s against
# LANE_DEADLINE_S of 60.0. 12 MB is 8.59 MB plus ~40% headroom, which covers
# growth and the largest 10-Ks filed, while still refusing a document an order
# of magnitude past anything a megacap files. Raise it only with a fresh
# measurement; the guard must not be removed.
DOC_MAX_BYTES = 12_000_000
SECTION_MAX_CHARS = 20_000     # per section handed to the model
QUOTE_MAX_CHARS = 300
REQUEST_PACING_S = 0.12

_HTTP_TIMEOUT = 25
# SEC returns HTTP 403 without a descriptive, contactable User-Agent.
def _sec_ua() -> str:
    return wi_config.get().sec_user_agent()

# Parsers tried in order. lxml is roughly twice as fast as the builtin on a
# 1.35 MB primary document, but this repo pins no requirements file, so lxml
# lives only in whatever Python happens to be installed and a reinstall could
# drop it silently. Both are therefore guarded, and `_extract_text` is pinned by
# a test asserting the two produce BYTE-IDENTICAL text on the recorded AMD
# document: the parser choice is a speed decision and must never become a result
# decision. (enrich.py's comment saying lxml is not installed is stale; leave it
# alone, it is another task's file.)
_PARSERS = ("lxml", "html.parser")

# Inline-XBRL documents are littered with these; collapsing them first means
# every downstream regex sees ordinary single spaces.
_SPACE_CHARS = (" ", " ", " ", " ", " ")


def _short(exc: Exception) -> str:
    """A short, single-line reason string for health notes."""
    msg = str(exc).split("\n")[0].strip()
    return f"{type(exc).__name__}: {msg}"[:120] if msg else type(exc).__name__


def _health(health: dict, note: str) -> None:
    """Record a failure under `health["filing_text"]` WITHOUT clobbering the
    notes of any other company the lane processed this morning: `ok`/`note`
    carry the latest reason, `notes` accumulates."""
    if not isinstance(health, dict):
        return
    entry = health.get("filing_text")
    if not isinstance(entry, dict):
        entry = {}
    notes = entry.get("notes")
    if not isinstance(notes, list):
        notes = []
    notes.append(note)
    entry["ok"] = False
    entry["note"] = note
    entry["notes"] = notes
    health["filing_text"] = entry


# --------------------------------------------------------------------------- #
# Network seams
# --------------------------------------------------------------------------- #
def _now() -> float:
    """Monotonic wall clock for deadline checks. A seam so deadline behaviour is
    testable without sleeping."""
    return time.monotonic()


def _sleep(seconds: float) -> None:
    """Request pacing. A seam so the offline suite never actually waits."""
    time.sleep(seconds)


# --------------------------------------------------------------------------- #
# HTML -> text
# --------------------------------------------------------------------------- #
def _extract_text(html, parsers=None) -> str:
    """Tag-stripped text of a filing document. Returns "" on anything at all.

    `parsers` exists so the fallback itself is testable; production callers use
    the module default. bs4 is imported lazily and guarded — if it is missing
    entirely the module degrades to "" rather than failing at import.
    """
    if not html or not isinstance(html, str):
        return ""
    try:
        from bs4 import BeautifulSoup
    except wi_config.ConfigError:
        raise
    except Exception:
        return ""

    # Filing primary documents are XHTML; parsing them as HTML is deliberate
    # (we want the tag soup flattened, not a namespace-correct tree) and bs4
    # warns about it. Suppressed locally, never globally.
    try:
        import warnings

        from bs4 import XMLParsedAsHTMLWarning
    except wi_config.ConfigError:
        raise
    except Exception:
        warnings = None
        XMLParsedAsHTMLWarning = None

    soup = None
    for parser in (parsers or _PARSERS):
        try:
            if warnings is not None and XMLParsedAsHTMLWarning is not None:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
                    soup = BeautifulSoup(html, parser)
            else:
                soup = BeautifulSoup(html, parser)
            break
        except wi_config.ConfigError:
            raise
        except Exception:
            continue
    if soup is None:
        return ""

    try:
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
    except wi_config.ConfigError:
        raise
    except Exception:
        return ""

    for ch in _SPACE_CHARS:
        text = text.replace(ch, " ")
    return re.sub(r"\s+", " ", text).strip()


def _archive_url(cik, accession, primary_doc) -> str:
    """The EDGAR Archives URL of a filing's primary document, or "" when any
    part is missing or malformed. The path segment is the CIK with leading
    zeros stripped and the accession with its dashes removed — EDGAR serves
    neither the zero-padded nor the dashed form."""
    try:
        cik_num = str(int(str(cik).strip()))
    except wi_config.ConfigError:
        raise
    except Exception:
        return ""
    acc = re.sub(r"[^0-9]", "", str(accession or ""))
    doc = str(primary_doc or "").strip()
    if not acc or not doc:
        return ""
    return SEC_ARCHIVES_URL.format(cik=cik_num, accession=acc, doc=doc)


def fetch_filing_text(cik: str, accession: str, primary_doc: str, session,
                      health: dict, deadline_at: float) -> str:
    """GET the filing's primary document and return tag-stripped text.

    Never raises; returns "" on any failure plus `health["filing_text"]`.
    `deadline_at` is a `_now()`-comparable monotonic instant shared by the whole
    lane — an exhausted deadline returns "" WITHOUT touching the network, so a
    late run costs nothing rather than costing a request.
    """
    try:
        if _now() >= deadline_at:
            _health(health, "deadline reached before filing document fetch")
            return ""

        url = _archive_url(cik, accession, primary_doc)
        if not url:
            _health(health, "cik={0} acc={1}: incomplete document reference".format(
                cik, accession))
            return ""

        # The lane's shared session is not optional. Falling back to a bare
        # `requests.get` would put an unpooled, unpaced request on the same IP
        # the other four SEC lanes depend on, so a missing session is a
        # configuration failure that degrades like any other.
        _sleep(REQUEST_PACING_S)
        resp = session.get(url, headers={"User-Agent": _sec_ua()},
                           timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        html = resp.text or ""

        if len(html) > DOC_MAX_BYTES:
            _health(health, "{0}: document too large ({1} chars)".format(
                primary_doc, len(html)))
            return ""

        text = _extract_text(html)
        if not text:
            _health(health, "{0}: no text extracted".format(primary_doc))
        return text
    except wi_config.ConfigError:
        raise
    except Exception as exc:
        _health(health, "{0}: {1}".format(primary_doc, _short(exc)))
        return ""


# --------------------------------------------------------------------------- #
# Section location (pure)
#
# A filing names its own sections several times before it reaches them, so a
# heading match needs three filters. They run in this order, and the order
# matters.
#
# 1. THE HEADING-CASE FILTER. A filing mentions its sections in running prose.
#    AMD writes "critical accounting estimates" mid-sentence AFTER the "Critical
#    Accounting Policies and Estimates" heading, and a risk factor says
#    "subsequent events may arise" where no subsequent-events note exists at
#    all. A heading is typeset in title or upper case; running prose is not.
#
# 2. THE CROSS-REFERENCE FILTER. Case alone still admits prose, because filings
#    also refer to their sections BY NAME in title case: `see Note 14,
#    Subsequent Events, of the Notes to Financial Statements` and the all-caps
#    `...FOR SUBSEQUENT EVENTS OR ANY OTHER MATTER`. In a 10-Q the notes precede
#    MD&A, so a cross-reference from MD&A back to a note is systematically
#    LATER in the document than the note itself — meaning a bare
#    last-occurrence rule picks the reference over the section every time. A
#    match is a cross-reference when it is quoted, when a referring word
#    ("see", "in", "under", "for", "of", ...) or a comma sits immediately in
#    front of it, or when ", of the Notes" follows it.
#
# 3. THE BODY-OPENER RULE (mdna only) — A DELIBERATE DEVIATION FROM SPEC §4.
#    §4 mandates plain last-occurrence and justifies it with a sentence that is
#    factually wrong: it calls AMD's second offset a table-of-contents entry,
#    when it is the real all-caps `ITEM 2. MANAGEMENT'S DISCUSSION AND ANALYSIS`
#    heading. Last-occurrence lands correctly for AMD ONLY because AMD happens
#    to close Critical Accounting Estimates with a cross-reference to the prior
#    10-K's MD&A that sits 88 characters ahead of "Results of Continuing
#    Operations". Delete that one phrase and last-occurrence collapses to the
#    forward-looking-statements cross-reference: 869 chars of boilerplate, no
#    results narrative, and `parse_segments` returns [].
#
#    The rule's INTENT is unambiguous — select the MD&A body — so the intent is
#    what is implemented. An MD&A match that is followed within
#    `_BODY_OPENER_WINDOW` chars by the body's own first heading ("Results of
#    Operations") opens the body, wherever it sits and whatever words introduce
#    it. AMD's fourth offset qualifies at 88 chars and the nearest other is
#    2,133 away, so AMD's selection is UNCHANGED. When nothing qualifies, the
#    rule falls back to last-occurrence among non-cross-references, which finds
#    the real ITEM 2 heading. Both paths are pinned by tests.
#
#    WHAT THE ORDERING DOES AND DOES NOT DO. The opener rule runs before the
#    cross-reference filter, because AMD's selected anchor is itself phrased as
#    a cross-reference. Running the filter first is not "broken": it yields
#    offset 5038 — the real all-caps ITEM 2 heading, 9,908 chars, all three
#    segments parsing. The ordering therefore decides WHICH VALID ANCHOR is
#    chosen, not correct versus broken. It is ordered this way because the task
#    requires AMD's published selection to be unchanged, and because the opener
#    test is positional evidence about where the narrative actually begins,
#    which is stronger than an inference from the words in front of it.
# --------------------------------------------------------------------------- #
_SECTION_PATTERNS = (
    # The optional title tail is part of the match so that `m.end()` clears the
    # section's own name: "... and Results of Operations" inside the TITLE must
    # never be mistaken for the body heading of the same name.
    ("mdna", re.compile(
        r"Management(?:'|’)s Discussion and Analysis"
        r"(?: of Financial Condition and Results of Operations)?", re.I)),
    ("risk_changes", re.compile(r"Item\s*1A[^A-Za-z]{0,4}Risk Factors", re.I)),
    ("subsequent_events", re.compile(r"Subsequent Events?", re.I)),
    ("critical_estimates",
     re.compile(r"Critical Accounting (?:Policies and )?Estimates", re.I)),
)

# Words a heading may legitimately leave lowercase.
_STOP_WORDS = frozenset(
    ("a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or",
     "the", "to", "with")
)

# Cross-reference cues, tested against the 60 characters immediately before a
# match and the 30 immediately after it.
_XREF_WORD_BEFORE = re.compile(
    r"(?:see|refer(?:s|red|ring)? to|set forth in|described in|disclosed in|"
    r"included in|contained in|in|under|for|of|to|within|per|from|about|"
    r"regarding|concerning)\s+(?:the\s+|our\s+|its\s+|these\s+)?$", re.I)
_XREF_QUOTE_BEFORE = re.compile(r"[“\"][^”\"]{0,60}$")
_XREF_COMMA_BEFORE = re.compile(r",\s+$")
# ... but a comma is ALSO how several filers punctuate a real heading:
# "PART II, ITEM 1A. RISK FACTORS" and "NOTE 14, SUBSEQUENT EVENTS". Rejecting
# those loses the whole section, change assertion and all, so a comma preceded
# by a part/note/item designator is exempt from the comma rule — UNLESS THE
# DESIGNATOR IS ITSELF INTRODUCED BY A REFERRING WORD. "see Note 14, Subsequent
# Events in this report" is a reference whose designator carries the same comma
# as the heading style, and without that second test it published the fragment
# "Subsequent Events in this report."
#
# This is a lexical proxy, and this module has now retired three of those. It is
# kept because of an asymmetry: getting a SECTION anchor wrong costs relevance —
# a model reads a fragment of prose instead of the right prose — while getting
# an AMOUNT wrong publishes a false number. A proxy that fails to absence is
# acceptable; one that fails to a wrong figure is not.
_XREF_DESIGNATOR_COMMA = re.compile(
    r"(?:Part\s+[IVX]+|Note\s+\d+|Item\s+\d+[A-Z]?)\s*,\s+$", re.I)
_XREF_REFERRING_WORD = re.compile(
    r"(?:see|refer(?:s|red|ring)? to|set forth in|described in|disclosed in|"
    r"included in|contained in|in|under|for|of|to|within|per|from|about|"
    r"regarding|concerning)\s+$", re.I)
_XREF_AFTER = re.compile(
    r"^\s*(?:,\s*of\s+(?:the|our)|of\s+the\s+Notes)", re.I)

_XREF_LOOKBACK = 60
_XREF_LOOKAHEAD = 30

# How far past a section title the body's own first heading may sit for that
# title to count as opening the body. AMD's qualifying offset is 88 chars; the
# nearest other is 2,133, so the choice sits in a 2,045-char gap and is not
# delicate. 400 keeps "followed by" meaning "immediately introduces".
#
# `re.I` IS LOAD-BEARING. Without it this pattern only matches title case, and
# every filer that typesets the body heading as "RESULTS OF OPERATIONS" — very
# common, and AMD itself prints ITEM 2 in caps — produces no opener at all and
# silently falls through to last-occurrence.
#
# WHERE THE `_is_heading_case` GUARD IS LOAD-BEARING, precisely. AMD's document
# carries this phrase 5 times in heading case and 9 times in lowercase prose.
# The 9 prose hits matter in `_mdna_body_offsets`, where they would become
# section BOUNDARY markers and truncate a section at the first time a filer
# happens to write "results of operations" in a sentence. They do NOT change
# `_opens_the_body` on AMD: no prose hit falls inside any 400-char window, so
# dropping the guard there selects the identical opener. It is kept because
# that is luck about one document, not a property of filings, and a synthetic
# test pins the case AMD does not exhibit.
_BODY_OPENER_WINDOW = 400
_MDNA_BODY_HEADING = re.compile(
    r"Results of (?:Continuing |Discontinued )?Operations", re.I)


def _is_heading_case(matched: str) -> bool:
    """True when `matched` is typeset as a heading — every significant word
    either capitalised or all-caps. "Critical Accounting Policies and Estimates"
    passes; "critical accounting estimates" (the same words, mid-sentence) does
    not."""
    words = re.findall(r"[A-Za-z][A-Za-z'’]*", matched or "")
    if not words:
        return False
    significant = [w for w in words if w.lower() not in _STOP_WORDS]
    if not significant:
        return False
    return all(w[0].isupper() for w in significant)


def _is_heading_style_comma(before: str) -> bool:
    """True when the comma in front of a match is heading punctuation
    ("PART II, ITEM 1A.") rather than reference punctuation ("see Note 14,")."""
    m = _XREF_DESIGNATOR_COMMA.search(before)
    if m is None:
        return False
    return _XREF_REFERRING_WORD.search(before[:m.start()]) is None


def _is_cross_reference(text: str, match) -> bool:
    """True when a heading-case match is the filing REFERRING to a section
    rather than opening it. See filter 2 above for why case is not enough."""
    before = text[max(0, match.start() - _XREF_LOOKBACK):match.start()]
    after = text[match.end():match.end() + _XREF_LOOKAHEAD]
    comma = bool(_XREF_COMMA_BEFORE.search(before)) and not _is_heading_style_comma(before)
    return bool(
        _XREF_QUOTE_BEFORE.search(before)
        or _XREF_WORD_BEFORE.search(before)
        or comma
        or _XREF_AFTER.search(after)
    )


def _opens_the_body(text: str, match) -> bool:
    """True when the body's own first heading follows this title closely enough
    that the title introduces it. Heading-case is required of the body heading
    too, so the prose phrase "results of operations" does not qualify."""
    window = text[match.end():match.end() + _BODY_OPENER_WINDOW]
    for m in _MDNA_BODY_HEADING.finditer(window):
        if _is_heading_case(m.group(0)):
            return True
    return False


def _mdna_body_offsets(text: str) -> list:
    """Offsets of the MD&A body's own heading ("Results of Operations"), used as
    a BOUNDARY only — it is not a published section.

    Without it, `critical_estimates` runs from its heading all the way to Item
    1A whenever the MD&A anchor sits BEFORE it, which is the normal arrangement
    for most filers and the one the fallback path produces: 5,343 chars of
    results narrative published under the critical-estimates label. Matches
    inside an MD&A TITLE are excluded — "... and Results of Operations" is the
    section's name, not its body heading.
    """
    title = dict(_SECTION_PATTERNS)["mdna"]
    spans = [m.span() for m in title.finditer(text)]
    out = []
    for m in _MDNA_BODY_HEADING.finditer(text):
        if not _is_heading_case(m.group(0)):
            continue
        if any(lo <= m.start() < hi for lo, hi in spans):
            continue
        out.append(m.start())
    return out


def _heading_offsets(text: str) -> dict:
    """{key: start offset} for every section whose heading is found. Keys are
    absent when the section has no usable match at all — never present and
    wrong."""
    out = {}
    for key, pattern in _SECTION_PATTERNS:
        matches = [m for m in pattern.finditer(text) if _is_heading_case(m.group(0))]
        if not matches:
            continue

        if key == "mdna":
            openers = [m for m in matches if _opens_the_body(text, m)]
            if openers:
                # Positional: whatever words introduce it, this offset is the
                # one the results narrative follows. AMD's selected anchor is
                # itself phrased as a cross-reference, which is exactly why the
                # cross-reference filter must not run first here.
                out[key] = openers[-1].start()
                continue

        body = [m for m in matches if not _is_cross_reference(text, m)]
        if body:
            out[key] = body[-1].start()
    return out


# Item 1A normally reads "no material changes". `risk_changes` is published
# ONLY when the section asserts a change, so the renderer shows nothing rather
# than a line saying nothing changed. These are the assertions that count;
# anything else (including a filer who simply restates all of its risk factors
# every quarter, as AMD does, without claiming any of them moved) is not a
# change and yields an ABSENT key.
#
# THE GATE APPLIES TO EVERY FORM, including the 10-K. An earlier draft exempted
# annual reports on the reasoning that a 10-K's Item 1A IS the risk factors —
# which is true, and beside the point: the KEY IS NAMED `risk_changes`, and a
# renderer keyed on it would report "risks changed" for every 10-K ever filed,
# off 7,860 chars that assert no change at all. Spec §4 states the gate
# unconditionally and is right to.
_RISK_CHANGE_PATTERNS = (
    re.compile(r"there (?:have|has) been (?:certain |the following )?material changes",
               re.I),
    re.compile(r"following risk factors? (?:has|have) been "
               r"(?:added|updated|revised|amended|supplemented|modified)", re.I),
    re.compile(r"we (?:have |are )?(?:added|updated|revised|amended|supplement\w*)"
               r"[^.]{0,40}risk factors?", re.I),
    re.compile(r"except as (?:set forth|described|provided|disclosed) below", re.I),
    re.compile(r"the following risk factors? (?:is|are) new", re.I),
)

# Sections that sit INSIDE another section. A container is never truncated by
# its own subsection; everything else bounds everything else. Critical
# Accounting Policies and Estimates is part of Item 2, so an MD&A body that
# opens before it must be allowed to run past it to the results narrative — and
# `_mdna_body`, the boundary-only marker from `_mdna_body_offsets`, is inside
# MD&A for the same reason (it would otherwise cut `mdna` off after 173 chars).
_NESTED_IN = {"critical_estimates": "mdna", "_mdna_body": "mdna"}


def slice_sections(text: str, form: str) -> dict:
    """Pure. Locate and return the sections the finance pass reads.

    Returns a subset of `{"mdna", "risk_changes", "subsequent_events",
    "critical_estimates"}`; a section that is not found is ABSENT, never present
    and empty. Each slice is a verbatim contiguous substring of `text`, running
    from its heading to whichever comes first: the next heading this module
    knows, or `SECTION_MAX_CHARS`. Bounding on the next heading is what keeps
    `critical_estimates` from being 20 KB of the results-of-operations
    narrative that follows it — EXCEPT where one section nests inside another
    (`_NESTED_IN`): Critical Accounting Estimates is a subsection of Item 2, so
    it must not truncate the MD&A that contains it.

    `form` is part of spec §4's signature and is accepted, but no longer varies
    behaviour: the Item 1A change gate applies to every form (see the comment on
    `_RISK_CHANGE_PATTERNS`).

    BEST EFFORT, NOT EXACT. Any valid occurrence of a heading may be selected,
    and a cross-reference fragment may occasionally be returned instead of the
    body. The consumer is a model reading prose, so the cost of a wrong anchor
    is relevance — the wrong paragraph — never a wrong figure. Do not try to
    make this exact; see the module docstring.
    """
    try:
        if not text or not isinstance(text, str):
            return {}

        offsets = _heading_offsets(text)
        if not offsets:
            return {}

        # Bounds come from EVERY located heading, including ones the gates below
        # suppress and one (`_mdna_body`) that is never published at all: a
        # section's extent is a fact about the document, not about what this
        # module chose to publish.
        markers = [(pos, other) for other, pos in offsets.items()]
        markers += [(pos, "_mdna_body") for pos in _mdna_body_offsets(text)]

        out = {}
        for key, start in offsets.items():
            bounds = sorted(
                pos for pos, other in markers if _NESTED_IN.get(other) != key
            )
            end = min(len(text), start + SECTION_MAX_CHARS)
            for nxt in bounds:
                if start < nxt < end:
                    end = nxt
                    break
            section = text[start:end].strip()
            if not section:
                continue
            if key == "risk_changes":
                if not any(p.search(section) for p in _RISK_CHANGE_PATTERNS):
                    continue
            out[key] = section
        return out
    except wi_config.ConfigError:
        raise
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# Off-balance-sheet and commitment disclosures (pure)
#
# The families the design spec names. Each entry is (canonical term, pattern);
# the canonical term is what is published, so "guarantees"/"guarantee" and
# "entities"/"entity" report under one name without the module rewriting the
# filing's words — the QUOTE is always verbatim.
# --------------------------------------------------------------------------- #
DISCLOSURE_TERMS = (
    ("residual value guarantee", re.compile(r"residual value guarantees?", re.I)),
    ("variable interest entity",
     re.compile(r"variable interest entit(?:y|ies)", re.I)),
    ("primary beneficiary", re.compile(r"primary beneficiar(?:y|ies)", re.I)),
    ("not yet commenced", re.compile(r"not yet commenced", re.I)),
    ("purchase obligations", re.compile(r"purchase obligations?", re.I)),
    ("unconsolidated joint venture",
     re.compile(r"unconsolidated joint ventures?", re.I)),
    ("keepwell", re.compile(r"keepwell", re.I)),
    ("letters of credit", re.compile(r"letters? of credit", re.I)),
    ("off-balance sheet", re.compile(r"off[-\s]balance[-\s]sheet", re.I)),
)

# A sentence ends at .:;!? followed by whitespace and something that starts a
# new clause. The lookahead matters: filings are full of "$4.5 billion." and
# "6 to 11 years." where a bare "\." split would be right, but also of table
# rows glued to prose, and of decimals that must never split.
_SENTENCE_BREAK = re.compile(r"(?<=[.:;!?])\s+(?=[A-Z$(“\"])")

# A stated money figure. Group 2 — the magnitude word — is what makes a figure
# publishable at all: see `_stated_amount`. It is optional in the PATTERN so
# that a bare figure is still recognised and can be deliberately refused,
# rather than being invisible and silently mistaken for "no figure here".
_MONEY = re.compile(
    r"\$\s*(\d[\d,]*(?:\.\d+)?)(?:\s*(billion|million|thousand|trillion))?", re.I
)
_MAGNITUDE = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12}

def _sentences(text: str) -> list:
    """[(start_offset, sentence)] over `text`. Every sentence is a verbatim
    contiguous substring, so a quote taken from one is verbatim too.

    Sentences are split on PUNCTUATION ONLY. Tag-stripped filings glue a table's
    last cell straight onto the prose that follows it with no punctuation
    between them, so a "sentence" here may carry a table row on its front. Two
    rounds of lexical proxies for that boundary each fixed their demonstrated
    case and broke a wider one; the boundary is destroyed upstream, by
    `_extract_text`'s `get_text(" ")`, and no regex over the result recovers it.
    The module now absorbs that where it is harmless — in the QUOTE, which stays
    verbatim and may simply contain adjacent table material — and refuses it
    where it is not, by never publishing a bare figure (`_stated_amount`).
    """
    out = []
    start = 0
    for m in _SENTENCE_BREAK.finditer(text):
        out.append((start, text[start:m.start()]))
        start = m.end()
    out.append((start, text[start:].rstrip()))
    return out


def _stated_amount(sentence: str, rel_start: int, rel_end: int):
    """The figure THIS SENTENCE attaches to the matched term, or None.

    AN AMOUNT IS PUBLISHED ONLY WHEN THE FILING ITSELF STATES THE MAGNITUDE.
    "$4.5 billion" is a complete statement of a quantity. "$ 17,386" is not: in
    a filing it is almost always a table cell whose scale lives in an "(In
    millions)" caption somewhere above it, and tag-stripped text cannot tell
    that cell from prose — the boundary is destroyed by `_extract_text` before
    any rule here can see it. Publishing it risks a figure understated by a
    factor of a million under a verbatim quote, which is worse than publishing
    nothing at all. So a bare figure yields None. Always.

    This replaced three successive lexical proxies (a table-cell split, a
    binder list, a caption lookback). Each fixed its demonstrated case and
    opened a wider one. DO NOT REINTRODUCE ONE: the answer to "when is a bare
    figure trustworthy?" is *never*, and that is the point. The quote is
    unaffected and still carries whatever the filing said, so a reader loses
    nothing but a machine-readable number that was never reliable.

    Selection runs first, and refuses to choose between candidates:

    1. Exactly one money figure follows the term in the sentence -> that one.
       AMD's "...leases that have commenced for approximately $1.2 billion and
       leases that have not yet commenced for $4.5 billion" reports $4.5bn,
       because that is the figure the clause attaches to the term.
    2. Otherwise, exactly one money figure anywhere in the sentence -> that one.
       AMD's "Up to $ 250 million of the Revolving Facility may be used for the
       issuance of letters of credit" states its figure ahead of the term.

    Anything else is None. A sentence with two figures after the term does not
    say which one belongs to it, and this module does not guess.
    """
    hits = list(_MONEY.finditer(sentence))
    if not hits:
        return None
    after = [m for m in hits if m.start() >= rel_end]
    if len(after) == 1:
        chosen = after[0]
    elif not after and len(hits) == 1:
        chosen = hits[0]
    else:
        return None

    if not chosen.group(2):
        return None
    try:
        value = float(chosen.group(1).replace(",", ""))
    except wi_config.ConfigError:
        raise
    except Exception:
        return None
    return value * _MAGNITUDE[chosen.group(2).lower()]


def _quote(sentence: str, rel_start: int, rel_end: int) -> str:
    """The sentence, or — when it is longer than QUOTE_MAX_CHARS — a verbatim
    window centred on the term. Never an ellipsis and never a rewrite: the
    result is always a contiguous substring of the filing."""
    if len(sentence) <= QUOTE_MAX_CHARS:
        return sentence
    term = sentence[rel_start:rel_end]
    pad = max(0, (QUOTE_MAX_CHARS - (rel_end - rel_start)) // 2)
    lo = max(0, rel_start - pad)
    hi = min(len(sentence), lo + QUOTE_MAX_CHARS)
    lo = max(0, hi - QUOTE_MAX_CHARS)
    chunk = sentence[lo:hi]
    if lo > 0:
        cut = chunk.find(" ")
        if 0 <= cut < 40 and term.lower() in chunk[cut + 1:].lower():
            chunk = chunk[cut + 1:]
    if hi < len(sentence):
        cut = chunk.rfind(" ")
        if cut > 0 and term.lower() in chunk[:cut].lower():
            chunk = chunk[:cut]
    return chunk.strip()


def find_disclosures(text: str) -> list:
    """Pure. Scan for off-balance-sheet disclosure language and return
    [{"term": str, "quote": str, "amount": float | None}], in document order.

    AN AMOUNT IS PUBLISHED ONLY WHEN THE FILING ITSELF STATES THE MAGNITUDE.
    "$4.5 billion" is published as `4_500_000_000.0`; a BARE figure such as
    "$ 17,386" yields `amount: None` with the verbatim quote intact, because
    tag-stripped text cannot distinguish a table cell from prose and that cell's
    scale lives in a caption the flattening destroyed. This is a deliberate
    narrowing after three failed lexical proxies — see `_stated_amount`.

    Quote is verbatim and <= QUOTE_MAX_CHARS, and for the same reason it may
    carry adjacent table material on its front; that is noise a reader can see,
    not a number they cannot check. Never infers an amount that is not in the
    sentence, and NEVER totals across families: a filing's residual
    value guarantees, its purchase obligations and its letters of credit are
    different obligations under different terms, and adding them would produce
    a number the filing does not state.
    """
    try:
        if not text or not isinstance(text, str):
            return []

        sentences = _sentences(text)
        starts = [s for s, _ in sentences]

        def _sentence_index(offset: int) -> int:
            lo, hi = 0, len(starts) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if starts[mid] <= offset:
                    lo = mid
                else:
                    hi = mid - 1
            return lo

        found = []
        seen = set()
        for term, pattern in DISCLOSURE_TERMS:
            for m in pattern.finditer(text):
                idx = _sentence_index(m.start())
                key = (term, idx)
                if key in seen:
                    continue
                seen.add(key)
                sent_start, sentence = sentences[idx]
                rel_start = m.start() - sent_start
                rel_end = m.end() - sent_start
                if rel_start < 0 or rel_end > len(sentence):
                    continue
                found.append({
                    "term": term,
                    "quote": _quote(sentence, rel_start, rel_end),
                    "amount": _stated_amount(sentence, rel_start, rel_end),
                    "_at": m.start(),
                })
        found.sort(key=lambda d: d["_at"])
        for d in found:
            d.pop("_at", None)
        return found
    except wi_config.ConfigError:
        raise
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Segment table (pure)
#
# Segment tables vary enormously by filer — the roster, the column count, the
# ORDER OF THE YEAR COLUMNS, the order of the revenue and operating-income
# blocks, and even whether operating income is disclosed at all. SILENT ABSENCE
# IS THE CORRECT FAILURE MODE: a partial or invented segment table is worse
# than none, because the card would publish it.
#
# THREE READERS, TRIED IN ORDER, EACH VALIDATED AGAINST A REAL FILING. The
# first that returns a table wins; a reader that cannot parse its shape returns
# [] and the next is tried. They were separated rather than generalised into
# one because AMD's published output is pinned byte-for-byte and the cost of a
# shared regex drifting is a wrong number, not a missing one.
#
#   1. `_segments_from_amd_block`      AMD 10-Q. One table, a "Net revenue:"
#      block and an "Operating income (loss):" block, four value columns
#      (current quarter, prior quarter, current YTD, prior YTD). UNCHANGED.
#
#   2. `_segments_from_vertical_blocks`   MSFT 10-K, AMZN 10-Q. A segment NAME,
#      then that segment's own Revenue / Cost of revenue / Operating expenses /
#      Operating income rows, repeated per segment and closed by a Total or
#      Consolidated block. MSFT prints two value columns plus a percentage
#      change; AMZN prints four — AND PRINTS THE PRIOR YEAR FIRST.
#
#   3. `_segments_from_paired_tables`     META 10-Q. Revenue and operating
#      income in TWO SEPARATE flat tables thousands of characters apart, each
#      row `<segment> <cells>`, with "% change" columns interleaved between the
#      value columns and roll-up sub-lines (Advertising, Other revenue) above
#      the segment they belong to.
#
# WHAT MAKES READERS 2 AND 3 SAFE — three checks, not a pattern list:
#
#   * THE COLUMN IS CHOSEN FROM THE TABLE'S OWN YEAR HEADER, NEVER FROM
#     POSITION. AMD, MSFT and META print the current year first; AMZN prints it
#     SECOND. A parser that assumed position publishes every AMZN figure
#     backwards, and each one looks perfectly plausible. When the header does
#     not yield two adjacent years, the table is refused.
#
#   * A PERCENTAGE CELL IS NOT A VALUE CELL. "16%", "(1)%" and "28 %" are
#     dropped, and the surviving value count must equal the number of years in
#     the header. That single equality catches a misread row.
#
#   * THE TABLE MUST ADD UP TO ITS OWN TOTAL ROW. The published segments plus
#     any reconciling rows must equal the Total/Consolidated row, in both the
#     current and the prior column, for revenue and for operating income. A
#     table that does not reconcile is refused — the roster is incomplete, or a
#     column was misread, and either would corrupt the share-of-total the
#     renderer computes.
#
# AAPL IS A DELIBERATE REFUSAL, NOT A GAP. Apple's Note 10 segment table is
# TRANSPOSED: the segments are COLUMNS (Americas / Europe / Greater China /
# Japan / Rest of Asia Pacific / Corporate / Total) and the line items are
# rows, so no row in it is a segment and no reader here can read one. The only
# row-shaped table Apple offers, MD&A's "net sales by reportable segment",
# carries NET SALES ONLY — there is no operating income to take a roster from —
# and two pages later Apple prints "net sales by category" (iPhone / Mac /
# iPad / Wearables / Services) in the byte-identical shape, summing to the same
# Total net sales. Accepting a revenue-only table on shape alone would publish
# "iPhone" as an operating segment with exactly the same confidence. Apple
# therefore yields [], and a test pins that it is [] on purpose.
#
# Figures are published AS PRINTED, in whatever unit the table's own caption
# states (millions, for all five filers read). No scaling is applied, because
# the scale lives in a caption outside the row and applying it would be
# inference.
# --------------------------------------------------------------------------- #
_SEGMENT_ANCHOR = re.compile(
    r"net revenue and operating income[^:]{0,60}by segment", re.I
)
_REVENUE_BLOCK = re.compile(r"Net revenue\s*:", re.I)
_REVENUE_BLOCK_END = re.compile(
    r"Cost of sales[^:]{0,60}:|Operating income(?:\s*\(loss\))?\s*:", re.I
)
_OPINCOME_BLOCK = re.compile(r"Operating income(?:\s*\(loss\))?\s*:", re.I)
_OPINCOME_BLOCK_END = re.compile(r"Total operating income", re.I)

_N = r"(\$?\s*\(?\d[\d,]*(?:\.\d+)?\)?)"
_SEGMENT_ROW = re.compile(
    r"([A-Za-z][A-Za-z&/'’\-. ]*?)\s*"
    + _N + r"\s+" + _N + r"\s+" + _N + r"\s+" + _N
)

# Rows that reconcile the table rather than name a segment. "consolidated" is
# how AMZN labels its grand total where AMD and MSFT write "Total"; it is never
# a segment name.
_RECONCILING = ("total", "all other", "corporate", "eliminations",
                "unallocated", "intersegment", "consolidated")

# The subset of the above that names the table's own GRAND TOTAL row — the row
# the arithmetic check reconciles against, as distinct from a reconciling row
# ("All other") that is one of the addends.
_TOTAL_ROW = ("total", "consolidated")

_SEGMENT_REGION_CHARS = 20_000

# Named so the unit rule is greppable and appears in the module's public
# surface, not only in a comment above a function. See `parse_segments`.
SEGMENT_VALUES_ARE_AS_PRINTED = True


def _to_number(raw: str):
    """"$ 6,718" -> 6718.0, "(155)" -> -155.0, junk -> None."""
    try:
        cleaned = raw.replace("$", "").replace(",", "").strip()
        negative = cleaned.startswith("(") and cleaned.endswith(")")
        cleaned = cleaned.strip("()").strip()
        value = float(cleaned)
        return -value if negative else value
    except wi_config.ConfigError:
        raise
    except Exception:
        return None


def _is_reconciling(label: str) -> bool:
    return any(label.startswith(word) for word in _RECONCILING)


def _rows(block: str) -> dict:
    """{normalised label: (current, prior)} for a table block, or None when a
    label repeats — a repeated label is an ambiguity this parser will not
    resolve, and the whole table is refused rather than half-read."""
    out = {}
    for m in _SEGMENT_ROW.finditer(block):
        label = re.sub(r"\s+", " ", m.group(1)).strip().lower()
        if not label:
            continue
        current = _to_number(m.group(2))
        prior = _to_number(m.group(3))
        if current is None or prior is None:
            continue
        if label in out:
            return None
        out[label] = (current, prior)
    return out


def _segments_from_amd_block(text: str) -> list:
    """Reader 1. AMD's shape: one table, a "Net revenue:" block and an
    "Operating income (loss):" block.

    The SEGMENT ROSTER is read off the operating-income block, which lists each
    segment exactly once. The revenue block does not: AMD breaks Client and
    Gaming into two sub-lines before totalling them, so its revenue rows are
    "Client and Gaming Client", "Gaming" and "Total Client and Gaming". A
    segment's revenue is therefore its exact row if one exists, else its
    "Total <segment>" row, and nothing else. A roster segment with neither
    refuses the WHOLE table: partial is not an option.
    """
    try:
        anchor = _SEGMENT_ANCHOR.search(text)
        if anchor is None:
            return []
        region = text[anchor.end():anchor.end() + _SEGMENT_REGION_CHARS]

        rev_open = _REVENUE_BLOCK.search(region)
        op_open = _OPINCOME_BLOCK.search(region)
        if rev_open is None or op_open is None:
            return []

        rev_close = _REVENUE_BLOCK_END.search(region, rev_open.end())
        if rev_close is None:
            return []
        op_close = _OPINCOME_BLOCK_END.search(region, op_open.end())
        if op_close is None:
            return []

        revenue_rows = _rows(region[rev_open.end():rev_close.start()])
        opincome_rows = _rows(region[op_open.end():op_close.start()])
        if not revenue_rows or not opincome_rows:
            return []

        segments = []
        for label, (op_current, op_prior) in opincome_rows.items():
            if _is_reconciling(label):
                continue
            values = revenue_rows.get(label) or revenue_rows.get("total " + label)
            if values is None:
                # A segment the revenue block does not name. Publishing the
                # rest would ship a table that does not add up.
                return []
            segments.append({
                "segment": _titled(label, region),
                "revenue": values[0],
                "revenue_prior": values[1],
                "op_income": op_current,
                "op_income_prior": op_prior,
            })
        return segments
    except wi_config.ConfigError:
        raise
    except Exception:
        return []


def _titled(label: str, region: str) -> str:
    """The segment's name as the filing prints it. The lookup key is
    normalised to lowercase for matching; this recovers the filing's own
    casing rather than inventing a title-case rendering."""
    pattern = r"\s+".join(re.escape(word) for word in label.split())
    m = re.search(pattern, region, re.I) if pattern else None
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else label


# --------------------------------------------------------------------------- #
# Shared machinery for readers 2 and 3
# --------------------------------------------------------------------------- #
# One printed cell: an optional "$", optional accounting parentheses (which are
# the minus sign), the figure, and an optional trailing "%". A cell that ends in
# "%" is a PERCENTAGE COLUMN — MSFT prints "16%" and "(1)%", META and AAPL print
# "28 %" and "11%" — and is dropped rather than counted as a value. Reading one
# as a value would publish a segment whose revenue is 16.
_CELL = re.compile(r"\s*(?:\$\s*)?(\(\s*)?(\d[\d,]*(?:\.\d+)?)\s*(\))?\s*(%)?")

# How far back a segment's own name may sit in front of its first data row.
_LABEL_LOOKBACK = 220
_LABEL_MAX_WORDS = 6
# A label is what follows the LAST piece of table furniture in front of the row:
# any digit (the year header, or the previous row's last cell), a colon or a
# closing parenthesis (a caption such as "(In millions, except percentages)"),
# a percent sign, or the column caption "Change". "Change" is the only LEXICAL
# item here and it is load-bearing exactly once: MSFT's header ends
# "2026 2025 Percentage Change" immediately before "Productivity and Business
# Processes", so cutting on punctuation and digits alone would name that
# segment "Percentage Change Productivity and Business Processes". Its failure
# mode is a wrong NAME under right numbers, never a wrong number — the same
# asymmetry that makes the section-anchor proxies acceptable.
_LABEL_CUT = re.compile(r"[0-9:%)]|\bchange\b", re.I)
_LABEL_OK = re.compile(r"[A-Z][A-Za-z&/'’.\- ]{1,58}\Z")

# The year header. 320 chars is enough to clear a table caption and its period
# captions ("Three Months Ended June 30, Six Months Ended June 30,") without
# reaching back into the prose above the table.
_YEAR_WINDOW = 320
_YEAR = re.compile(r"\b(19\d\d|20\d\d)\b")

# The word that must appear shortly before a table for it to be a candidate at
# all. Cheap, and it keeps the structural scan off the income statement.
_SEGMENT_CONTEXT_CHARS = 700


def _cells_at(text: str, pos: int):
    """(values, end_offset) for the run of printed cells starting at `pos`.

    Percentage cells are consumed but NOT returned. Accounting parentheses are
    read as a minus sign, and a half-open parenthesis ends the run rather than
    being guessed at.
    """
    values = []
    i = pos
    while True:
        m = _CELL.match(text, i)
        if m is None:
            break
        opened, digits, closed, percent = m.groups()
        if bool(opened) != bool(closed):
            break
        try:
            value = float(digits.replace(",", ""))
        except wi_config.ConfigError:
            raise
        except Exception:
            break
        if opened:
            value = -value
        if not percent:
            values.append(value)
        i = m.end()
    return values, i


def _label_before(text: str, start: int, floor: int):
    """The segment name printed immediately in front of a data row, or None.

    None means "this row does not open a segment" — a header fragment, a
    transposed table's line item, or a run of prose — and the caller drops the
    group rather than inventing a name for it.
    """
    candidate = text[max(floor, start - _LABEL_LOOKBACK):start]
    cut = None
    for m in _LABEL_CUT.finditer(candidate):
        cut = m
    if cut is not None:
        candidate = candidate[cut.end():]
    candidate = re.sub(r"\s+", " ", candidate).strip()
    if not candidate or _LABEL_OK.match(candidate) is None:
        return None
    if len(candidate.split()) > _LABEL_MAX_WORDS:
        return None
    return candidate


def _column_order(text: str, first_row_start: int, columns: int):
    """(current_index, prior_index) read off the table's OWN year header, or
    None when the header does not settle it.

    This is the whole defence against publishing a filing backwards. AMD, MSFT
    and META print the current period first; AMZN prints it second, and every
    figure in its table is a plausible number in either order. The two leading
    years must be ADJACENT — a comparative table compares consecutive periods —
    and there must be one year per value column.
    """
    window = text[max(0, first_row_start - _YEAR_WINDOW):first_row_start]
    years = [int(y) for y in _YEAR.findall(window)]
    if len(years) < columns or columns < 2:
        return None
    years = years[-columns:]
    if abs(years[0] - years[1]) != 1:
        return None
    return (0, 1) if years[0] > years[1] else (1, 0)


def _is_total_row(label: str) -> bool:
    return label.lower().startswith(_TOTAL_ROW)


def _reconciles(rows, total, index, count) -> bool:
    """True when the addend rows sum to the total row in column `index`.

    Printed cells are rounded to the caption's unit, so `count` half-units of
    slack is the most the arithmetic may legitimately drift.
    """
    return abs(sum(r[index] for r in rows) - total[index]) <= 0.5 * count + 1e-9


# --------------------------------------------------------------------------- #
# Reader 2 — per-segment vertical blocks (MSFT 10-K, AMZN 10-Q)
# --------------------------------------------------------------------------- #
# A row is a known line item followed IMMEDIATELY by a figure. The lookahead is
# what keeps "Net revenue: Data Center $ 6,718" (AMD's block caption, whose next
# token is a segment name) from being read as a row.
_VERTICAL_ROW = re.compile(
    r"\b(?:(?P<revenue>Net sales|Net revenues?|Revenues?)"
    r"|(?P<opincome>Operating income(?:\s*/?\s*\(loss\))?|Operating loss)"
    r"|(?:Cost of revenues?|Cost of sales|Operating expenses))"
    r"\s*:?\s*(?=[\$(\d])", re.I)

# The most text that may sit between one segment's last figure and the next
# segment's first row for the two to belong to the same table: the segment name
# and nothing else. Beyond it the parser is looking at a different table.
_VERTICAL_GAP_CHARS = 100
_VERTICAL_MIN_GROUPS = 3   # at least two segments plus the total row


def _vertical_groups(text: str) -> list:
    """[{label, start, end, revenue, op_income}] for every segment-shaped block
    in `text`, in document order. Blocks without a name, or without both a
    revenue and an operating-income row, are not groups."""
    rows = []
    for m in _VERTICAL_ROW.finditer(text):
        values, end = _cells_at(text, m.end())
        if not values:
            continue
        if m.group("revenue"):
            kind = "revenue"
        elif m.group("opincome"):
            kind = "op_income"
        else:
            kind = "other"
        rows.append((kind, m.start(), end, values))

    groups = []
    for i, (kind, start, _end, values) in enumerate(rows):
        if kind != "revenue":
            continue
        op = None
        for j in range(i + 1, len(rows)):
            if rows[j][0] == "revenue":
                break
            if rows[j][0] == "op_income":
                op = rows[j]
                break
        if op is None:
            continue
        label = _label_before(text, start, rows[i - 1][2] if i else 0)
        if label is None:
            continue
        groups.append({"label": label, "start": start, "end": op[2],
                       "revenue": values, "op_income": op[3]})
    return groups


def _vertical_tables(groups: list) -> list:
    """`groups` split into contiguous tables. A change of column count, or more
    than a segment name's worth of text between two groups, is a table
    boundary."""
    tables = []
    current = []
    for group in groups:
        if current and (group["start"] - current[-1]["end"] > _VERTICAL_GAP_CHARS
                        or len(group["revenue"]) != len(current[-1]["revenue"])):
            tables.append(current)
            current = []
        current.append(group)
    if current:
        tables.append(current)
    return tables


def _read_vertical_table(text: str, table: list) -> list:
    """One candidate table -> published rows, or [] when any check fails."""
    if len(table) < _VERTICAL_MIN_GROUPS:
        return []
    columns = len(table[0]["revenue"])
    for group in table:
        if len(group["revenue"]) != columns or len(group["op_income"]) != columns:
            return []

    totals = [g for g in table if _is_total_row(g["label"])]
    if len(totals) != 1 or totals[0] is not table[-1]:
        return []
    total = totals[0]
    segments = [g for g in table if not g["label"].lower().startswith(_RECONCILING)]
    if len(segments) < 2:
        return []
    # "All other", "Corporate" and friends: excluded from the roster, but they
    # are addends of the total and the arithmetic check needs them.
    others = [g for g in table if g is not total and g not in segments]

    start = table[0]["start"]
    if "segment" not in text[max(0, start - _SEGMENT_CONTEXT_CHARS):start].lower():
        return []

    order = _column_order(text, start, columns)
    if order is None:
        return []
    current, prior = order

    addends = segments + others
    for key in ("revenue", "op_income"):
        rows = [g[key] for g in addends]
        for index in (current, prior):
            if not _reconciles(rows, total[key], index, len(table)):
                return []

    return [{"segment": g["label"],
             "revenue": g["revenue"][current],
             "revenue_prior": g["revenue"][prior],
             "op_income": g["op_income"][current],
             "op_income_prior": g["op_income"][prior]} for g in segments]


def _segments_from_vertical_blocks(text: str) -> list:
    """Reader 2. The first candidate table that passes every check wins."""
    try:
        for table in _vertical_tables(_vertical_groups(text)):
            rows = _read_vertical_table(text, table)
            if rows:
                return rows
        return []
    except wi_config.ConfigError:
        raise
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Reader 3 — two separate flat tables (META 10-Q)
# --------------------------------------------------------------------------- #
_FLAT_REVENUE_ANCHOR = re.compile(
    r"(?:net (?:sales|revenues?)|revenues?)[^.:;]{0,40}"
    r"\bby (?:reportable |operating )?segment", re.I)
_FLAT_OPINCOME_ANCHOR = re.compile(
    r"(?:income|loss)[^.:;]{0,40}from operations "
    r"by (?:reportable |operating )?segment"
    r"|operating income(?:\s*\(loss\))?[^.:;]{0,40}"
    r"\bby (?:reportable |operating )?segment", re.I)
_FLAT_ROW_LABEL = re.compile(r"[A-Z][A-Za-z&/'’.\- ]{1,58}?(?=\s*[\$(\d])")
_FLAT_REGION_CHARS = 2_500
_FLAT_MAX_ANCHORS = 6


def _flat_rows(text: str, anchor_end: int):
    """([(label, cells, start)], first_row_start) for the flat table that
    follows a caption, or ([], None).

    The scan runs to the table's own total row and then walks BACKWARDS from
    it: the period captions above the body ("Three Months Ended June 30,") look
    like rows with one cell, and the run of rows sharing the total row's column
    count is the body. This is what keeps a header fragment out of the roster.
    """
    region_end = min(len(text), anchor_end + _FLAT_REGION_CHARS)
    rows = []
    i = anchor_end
    while i < region_end:
        m = _FLAT_ROW_LABEL.search(text, i, region_end)
        if m is None:
            break
        values, end = _cells_at(text, m.end())
        if not values:
            i = m.end() + 1
            continue
        label = re.sub(r"\s+", " ", m.group(0)).strip()
        rows.append((label, values, m.start()))
        i = end
        if _is_total_row(label):
            break

    if not rows or not _is_total_row(rows[-1][0]):
        return [], None
    columns = len(rows[-1][1])
    body = [rows[-1]]
    for row in reversed(rows[:-1]):
        if len(row[1]) != columns:
            break
        body.insert(0, row)
    if len(body) < 3:            # at least two segments plus the total row
        return [], None
    return body, body[0][2]


def _read_flat_pair(text: str, revenue_rows, revenue_start,
                    opincome_rows, opincome_start) -> list:
    columns = len(revenue_rows[-1][1])
    if len(opincome_rows[-1][1]) != columns:
        return []

    revenue_order = _column_order(text, revenue_start, columns)
    opincome_order = _column_order(text, opincome_start, columns)
    if revenue_order is None or revenue_order != opincome_order:
        return []
    current, prior = revenue_order

    by_label = {label.lower(): values for label, values, _ in revenue_rows}
    published = []
    revenue_addends = []
    for label, values, _ in opincome_rows[:-1]:
        low = label.lower()
        # The revenue table may print a segment as its own "Total <segment>"
        # roll-up, above the sub-lines that make it up.
        revenue = by_label.get(low) or by_label.get("total " + low)
        if revenue is None:
            # A segment the revenue table does not name. Publishing the rest
            # would ship a table that does not add up.
            return []
        revenue_addends.append(revenue)
        if low.startswith(_RECONCILING):
            continue
        published.append({"segment": label,
                          "revenue": revenue[current],
                          "revenue_prior": revenue[prior],
                          "op_income": values[current],
                          "op_income_prior": values[prior]})
    if not published:
        return []

    opincome_addends = [values for _, values, _ in opincome_rows[:-1]]
    count = len(opincome_rows)
    for index in (current, prior):
        if not _reconciles(revenue_addends, revenue_rows[-1][1], index, count):
            return []
        if not _reconciles(opincome_addends, opincome_rows[-1][1], index, count):
            return []
    return published


def _segments_from_paired_tables(text: str) -> list:
    """Reader 3. Every (revenue caption, operating-income caption) pair is
    tried; the first that reconciles wins. Both captions are required — a
    filing that prints only a revenue-by-segment table yields [], because a
    revenue-only table is indistinguishable by shape from the by-product and
    by-geography tables printed beside it."""
    try:
        cap = _FLAT_MAX_ANCHORS
        revenue_anchors = list(_FLAT_REVENUE_ANCHOR.finditer(text))[:cap]
        opincome_anchors = list(_FLAT_OPINCOME_ANCHOR.finditer(text))[:cap]
        if not revenue_anchors or not opincome_anchors:
            return []
        for revenue_anchor in revenue_anchors:
            revenue_rows, revenue_start = _flat_rows(text, revenue_anchor.end())
            if not revenue_rows:
                continue
            for opincome_anchor in opincome_anchors:
                opincome_rows, opincome_start = _flat_rows(
                    text, opincome_anchor.end())
                if not opincome_rows:
                    continue
                rows = _read_flat_pair(text, revenue_rows, revenue_start,
                                       opincome_rows, opincome_start)
                if rows:
                    return rows
        return []
    except wi_config.ConfigError:
        raise
    except Exception:
        return []


_SEGMENT_READERS = (_segments_from_amd_block,
                    _segments_from_vertical_blocks,
                    _segments_from_paired_tables)


def parse_segments(text: str) -> list:
    """Pure. Extract the segment revenue / operating income table.

    Returns [{"segment": str, "revenue": float, "revenue_prior": float,
              "op_income": float | None, "op_income_prior": float | None}].
    Returns [] when the table cannot be parsed unambiguously — never guesses.

    UNITS ARE THE TABLE'S OWN, NOT DOLLARS (`SEGMENT_VALUES_ARE_AS_PRINTED`).
    Every figure is the cell as printed, under whatever caption the table
    carries: AMD's table says "(In millions)", so `revenue: 6718.0` means
    $6.718bn and is NOT comparable with `sec_fundamentals`' `revenue_q` of
    11_536_000_000 without scaling. The caption sits outside the row, and
    applying it here would be this module inferring a number the row does not
    state. A caller that needs absolute dollars must read the caption itself.

    Three readers are tried in order — see the block comment above for what
    each one reads, why they are separate, and why AAPL yields [] on purpose.
    """
    try:
        if not text or not isinstance(text, str):
            return []
        for reader in _SEGMENT_READERS:
            rows = reader(text)
            if rows:
                return rows
        return []
    except wi_config.ConfigError:
        raise
    except Exception:
        return []
