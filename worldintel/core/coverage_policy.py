"""Portable, offline ranking configuration and bounded event coverage policy."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from urllib.parse import urlparse
from html import unescape

_DEFAULT = {"version": 1, "topics": [
    {"terms": ["hijack", "cockpit", "mass shooting", "mass casualty"], "weight": 0.8}],
    "major_events": {"enabled": True, "max_events": 2, "min_publishers": 2}}


def resolve_profile(raw=None):
    """Resolve optional overrides, rejecting explicit invalid configuration."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - set(_DEFAULT):
        raise ValueError("invalid ranking profile keys")
    result = copy.deepcopy(_DEFAULT)
    if "version" in raw and (type(raw["version"]) is not int or raw["version"] != 1):
        raise ValueError("ranking version must be 1")
    if "topics" in raw:
        topics = raw["topics"]
        if not isinstance(topics, list) or len(topics) > 20:
            raise ValueError("topics must be a list of at most 20 topics")
        for topic in topics:
            if not isinstance(topic, dict) or set(topic) != {"terms", "weight"}:
                raise ValueError("topic requires terms and weight")
            terms, weight = topic["terms"], topic["weight"]
            if (not isinstance(terms, list) or not 1 <= len(terms) <= 30 or
                    any(not isinstance(t, str) or not t.strip() or len(t) > 80 for t in terms)):
                raise ValueError("invalid topic terms")
            if type(weight) not in (int, float) or not math.isfinite(weight) or not 0 <= weight <= 3:
                raise ValueError("topic weight must be finite and between 0 and 3")
        result["topics"] = [{"terms": [t.strip().lower() for t in topic["terms"]],
                             "weight": float(topic["weight"])} for topic in topics]
    if "major_events" in raw:
        settings = raw["major_events"]
        if not isinstance(settings, dict) or set(settings) - set(result["major_events"]):
            raise ValueError("invalid major_events keys")
        result["major_events"].update(settings)
    settings = result["major_events"]
    if type(settings["enabled"]) is not bool:
        raise ValueError("enabled must be boolean")
    for key, lower, upper in [("max_events", 0, 3), ("min_publishers", 2, 10)]:
        if type(settings[key]) is not int or not lower <= settings[key] <= upper:
            raise ValueError(f"invalid {key}")
    return result


def profile_hash(profile):
    return hashlib.sha256(json.dumps(resolve_profile(profile), sort_keys=True,
                                    separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _text(item):
    return f"{item.get('title', '')} {item.get('summary', '')}".lower()


def score_bonus(item, profile):
    text = _text(item)
    return min(3.0, sum(topic["weight"] for topic in profile["topics"]
                        if any(re.search(r"\b" + re.escape(term), text)
                               for term in topic["terms"])))


def route_security(item):
    text = _text(item)
    return bool(re.search(r"\b(?:aircraft|airliner|airplane|plane|flight|cockpit|airport)\b", text)
                and re.search(r"\b(?:hijack\w*|terror\w*|bomb\w*|seiz\w*|stab\w*|attack\w*|crash attempt|(?:attempt\w*|tried|tries) to crash)\b", text))


def recognition_reason(item):
    """Return the conservative action/consequence rule matched by metadata."""
    text = _text(item)
    if re.search(r"\b(?:opinion|newsletter|roundup|video|liveblog|live updates|historical|retrospective)\b", str(item.get("title", "")).lower()):
        return ""
    if re.search(r"\b(?:opinion|newsletter|roundup|liveblog|live updates|historical|retrospective)\b", text):
        return ""
    if re.search(r"/(?:video|videos|opinion|newsletter|live)(?:/|$)", str(item.get("url", ""))):
        return ""
    # Attack reporting must concern an incident, not borrow it from background
    # in an unrelated conference, market or infrastructure article.
    title = str(item.get("title", "")).lower()
    background_topic = re.search(r"\b(?:conferences?|summits?|investment|economic|economy|business confidence|shares|expansion|training|exercise)\b", title)
    direct_action = re.search(r"\b(?:attack\w*|strike\w*|bomb\w*|hijack\w*|stab\w*|shoot\w*|gunm[ae]n|gunfire|massacre)\b", title)
    background_action = re.search(
        r"\b(?:despite|amid|after|following|since)\b.{0,80}\b(?:attack\w*|strike\w*|bomb\w*|hijack\w*|stab\w*|shoot\w*|gunm[ae]n|gunfire|massacre)\b", title)
    foreground_action = direct_action and (not background_action or direct_action.start() < background_action.start())
    unrelated_security_context = background_topic and not foreground_action
    if re.search(r"\b(?:training|exercise|simulation)\b", title) and re.search(r"\b(?:routine|exercise|simulation|no injuries)\b", text):
        return ""
    direct_missile_harm = (re.search(r"\bairport\b", title)
                           and re.search(r"\b(?:missile|drone)\w*\b", title)
                           and re.search(r"\b(?:kill\w*|dead|death\w*|injur\w*)\b", title))
    foreground_aviation = (not _foreground_action(item) or re.search(
        r"\b(?:aircraft|airliner|airplane|plane|flight|cockpit|airport)\b",
        _foreground_title(item), re.IGNORECASE))
    if ((route_security(item) or direct_missile_harm) and not unrelated_security_context
            and foreground_aviation):
        return "aviation_security"
    if (re.search(r"\b(?:earthquake|quake|tsunami)\b", text)
            and re.search(r"\b(?:kill\w*|dead|death\w*|injur\w*|evacuat\w*|destroy\w*)\b|\bmagnitude\s+[6-9](?:\.\d)?", text)):
        return "acute_disaster"
    # A newly published legal update does not make its background attack fresh.
    title = str(item.get("title", "")).lower()
    legal_followup = re.search(r"\b(?:execut(?:e|ed|ion|ions)|firing squad|sentenc(?:e|es|ed|ing)|trial|convict(?:ed|ion))\b", title)
    fresh_attack = re.search(
        r"\b(?:shoot\w*|gunm[ae]n|gunfire|attack\w*)\b\s+(?:(?!(?:who|execut\w*|sentenc\w*|trial|convict\w*|firing|\d{4})\b)[\w,-]+\s+){0,12}(?:today|yesterday|overnight|now)\b|"
        r"\b(?:today|yesterday|overnight|new|fresh)\s+(?:mass\s+)?(?:shooting|attack)\b", text)
    if legal_followup and not fresh_attack:
        return ""
    if not unrelated_security_context and re.search(r"\b(?:mass shooting|mass casualty|massacre|terror\w* attack)\b", text):
        return "explicit_mass_casualty"
    number = r"(?:[2-9]|[1-9]\d+|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozens)"
    if (not unrelated_security_context
            and re.search(r"\b(?:shoot\w*|gunm[ae]n|gunfire)\b", text)
            and re.search(r"\b(?:kill(?:ed|s)?\s+(?:at least\s+)?" + number + r"\s+(?:people|victims|persons)|" + number + r"\s+(?:people\s+)?(?:dead|killed))\b", text)):
        return "shooting_multiple_fatalities"
    if re.search(r"\b(?:protest\w*|unrest|demonstrat\w*|movement)\b", text):
        if (re.search(r"\b(?:nationwide|national|across the country)\b", text)
                and re.search(r"\b(?:school closures|school blockades|schools? (?:closed|blocked)|blockades)\b", text)):
            return "unrest_national_disruption"
        quantified_schools = re.search(r"\b(?:[1-9]\d{2,}|hundreds(?: of)?|thousands(?: of)?)\s+schools\b", text)
        broad_disruption = (re.search(r"\b(?:across|throughout)\s+(?!(?:the\s+)?(?:city|town|district|campus|capital)\b)[a-z][a-z-]*\b", text)
                            and re.search(r"\b(?:closed|clos(?:e|es|ing|ure|ures)|shut|shutter(?:ed|s)?|suspended)\b", text))
        serious_context = re.search(r"\b(?:burn(?:ed|t)?|ransack\w*|serious(?:ly)? injur\w*|critical(?:ly)? injur\w*|kill(?:ed|s)?|deaths?)\b", text)
        if quantified_schools and broad_disruption and serious_context:
            return "unrest_national_disruption"
        national_institution = re.search(r"\b(?:education minister|national government|national cabinet|prime minister|president)\b", text)
        local_scope = re.search(r"\b(?:across|throughout|in|within)\s+(?:the\s+)?(?:city|town|district|campus|capital)\b", text)
        closures = re.search(r"\b(?:closed|clos(?:e|es|ing|ure|ures)|shut|shutter(?:ed|s)?|suspended)\b", text)
        if quantified_schools and closures and serious_context and national_institution and not local_scope:
            return "unrest_extensive_school_disruption"
        if (re.search(r"\bmass arrests\b|\b(?:hundreds|thousands)\s+(?:arrested|of arrests)\b", text)
                and re.search(r"\b(?:serious(?:ly)? injur\w*|critical(?:ly)? injur\w*|deaths?|killed)\b", text)):
            return "unrest_arrests_serious_harm"
        if (re.search(r"\b(?:government|cabinet|prime minister|president)\b", text)
                and re.search(r"\b(?:national emergency|state of emergency|nationwide curfew|deploy\w* troops)\b", text)):
            return "unrest_national_government_response"
        institution = r"(?:national government|national cabinet|prime minister|president)"
        movement = r"(?:(?!(?:election|vote|while|but|meanwhile)\b)[\w-]+\s+){0,6}(?:protests?|unrest|demonstrations?)\b"
        linked_consequence = (
            r"\b(?:" + institution + r"\s+(?:resigns?|resigned|steps? down)|"
            r"president\s+dissolv(?:es|ed)\s+(?:the\s+)?national (?:government|cabinet))"
            r"\s+(?:in response to|after|following)\s+" + movement)
        forced_consequence = (
            r"\b(?:protests?|unrest|demonstrations?)\s+(?:force[sd]?|prompt(?:s|ed)?)\s+"
            r"(?:the\s+)?" + institution + r"\s+to\s+(?:resign|step down|dissolve)\b")
        if re.search(linked_consequence, text) or re.search(forced_consequence, text):
            return "unrest_national_government_response"
    return ""


def _serious(item):
    return bool(recognition_reason(item))


def bounded_events(events, max_events=None):
    """Honor the selection limit; old callers without metadata retain two."""
    limits = {event["max_events"] for event in events if "max_events" in event}
    if len(limits) > 1:
        raise ValueError("inconsistent protected event limits")
    limit = max_events if max_events is not None else next(iter(limits), 2)
    resolve_profile({"major_events": {"max_events": limit}})
    return events[:limit]


def _publisher(item):
    # Explicit wire attribution prevents syndicated URLs counting independently.
    explicit = item.get("wire_source") or item.get("publisher")
    if explicit:
        return re.sub(r"[^a-z0-9]", "", str(explicit).lower())
    text = _text(item)
    if re.search(r"\breuters\b", text):
        return "reuters"
    if re.search(r"\b(?:associated press|ap reports|ap news)\b", text):
        return "associatedpress"
    host = urlparse(str(item.get("url", ""))).hostname or ""
    host = host.removeprefix("www.")
    for domain, publisher in [("bbc.com", "bbc"), ("bbc.co.uk", "bbc"),
                              ("reuters.com", "reuters"), ("apnews.com", "associatedpress")]:
        if host == domain or host.endswith("." + domain):
            return publisher
    if host and host not in {"news.google.com", "google.com"}:
        return host
    return re.sub(r"(?:[-_](?:world|business|politics|canada|us|japan|gnews))+$", "",
                  str(item.get("source", "")).lower())



def _current_item(item):
    """Exclude explicitly introduced background from incident comparison."""
    summary = unescape(re.sub(r"<[^>]+>", " ", str(item.get("summary", ""))))
    units = re.split(r"(?<=[.!?])\s+", summary)
    summary = " ".join(unit for unit in units if not re.match(
        r"\s*(?:earlier|previously|last (?:week|month|year)|in \d{4})\b", unit, re.I))
    return dict(item, summary=summary)


def _incident_details(item):
    """Small affirmative signature; shared city or country alone is insufficient."""
    item = _current_item(item)
    raw = str(item.get("title", "")) + ". " + str(item.get("summary", ""))
    text = raw.lower()
    numbers = dict(zip("one two three four five six seven eight nine ten eleven twelve".split(), range(1, 13)))
    number = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
    fatalities = set()
    for pattern in (r"\b(" + number + r")\s+(?:people\s+)?(?:were\s+)?(?:killed|dead)\b",
                    r"\bkill(?:s|ed)?\s+(?:at least\s+)?(" + number + r")\b"):
        for value in re.findall(pattern, text):
            fatalities.add(numbers[value] if value in numbers else int(value))
    # Only explicit proper-name location syntax; no country-name registry.
    locations = set()
    for pattern in (r"\b([A-Z][a-z]+)(?:['?]s)?\s+(?:international\s+)?airport\b",
                    r"\bcapital\s+([A-Z][a-z]+)\b",
                    r"\b(?:in|at|near)\s+([A-Z][a-z]+)(?=[.,;:]|$)"):
        locations.update(value.lower() for value in re.findall(pattern, raw))
    locations -= {"international", "national", "regional", "domestic", "the"}
    venues = {value.lower() for value in re.findall(
        r"\b((?:[A-Z][a-z]+\s+){1,3}(?:Mall|School|Church|Hotel|Stadium|Airport))\b", raw)}
    day = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|yesterday|overnight)"
    times = set(re.findall(r"\b" + day + r"\b", str(item.get("title", "")).lower()))
    # Date of an official's statement is not the incident's date. In summaries,
    # accept only a bounded incident clause with no reporting-subject transition.
    times.update(re.findall(
        r"\b(?:attack|strike|struck|bombing|shooting)\s+"
        r"(?:(?!(?:officials?|authorities|said|say|reported|confirmed|earlier)\b)[^\s.?!,;]+\s+){0,10}"
        r"(?:on|late on|early on)\s+(" + day + r")\b", str(item.get("summary", "")).lower()))
    aviation = bool(re.search(r"\bairport\b", text) and re.search(r"\b(?:attack\w*|strike\w*|missile\w*)\b", text))
    return locations, fatalities, times, venues, aviation


def _structured_same_incident(a, b):
    left, right = _incident_details(a), _incident_details(b)
    # A shared airport location needs equal multiple-fatality evidence or an
    # explicit same-day fatal incident. Counts may evolve within that incident.
    equal_harm = bool(left[1] & right[1] and max(left[1] & right[1]) >= 2)
    timed_harm = bool(left[2] & right[2] and left[1] and right[1]
                      and max(left[1]) >= 2 and max(right[1]) >= 2)
    return bool(left[4] and right[4] and left[0] & right[0]
                and (equal_harm or timed_harm)
                and (not left[2] or not right[2] or left[2] & right[2]))


def _foreground_title(item):
    """Return the title prefix before an explicit background transition."""
    return re.split(r"\b(?:after|following|amid|as|while|during|because|before|since)\b",
                    unescape(str(item.get("title", ""))), maxsplit=1,
                    flags=re.IGNORECASE)[0].strip()


def _foreground_action(item):
    """Extract only confident active named roles; absence is not identity evidence."""
    full_title = unescape(str(item.get("title", ""))).replace("\u2019", "'")
    # Uncertainty anywhere in a headline survives background truncation.
    if re.search(r'["?,;:\u201c\u201d\u2018]|\b(?:not|never|no|deny|denies|denied|may|might|could|would|'
                 r'will|plan|plans|planned|reportedly|allegedly|exercise|simulation|training)\b',
                 full_title, re.IGNORECASE):
        return None
    title = _foreground_title(dict(item, title=full_title))
    action = re.fullmatch(
        r"(?P<actor>.+?)\s+(?:(?P<report>say|says|said)\s+(?:that\s+)?(?P<pronoun>it|they)\s+)?"
        r"(?P<verb>attack|attacks|attacked|strike|strikes|struck|bomb|bombs|bombed|"
        r"shell|shells|shelled|hit|hits)\s+(?P<target>.+)", title)
    if not action:
        return None
    reports = re.findall(r"\b(?:say|says|said)\b", full_title)
    if len(reports) != bool(action["report"]) or re.search(
            r"\b(?:attack|attacks|attacked|strike|strikes|struck|bomb|bombs|bombed|shell|shells|shelled|hit|hits)\b",
            action["target"]):
        return None
    roles = set("the force forces army armies navy military rebel rebels militant militants"
                " coalition troop troops government".split())
    objects = roles | set("a an missile missiles facility facilities oil pumping station stations"
                          " airport airports base bases port ports depot depots airfield airfields"
                          " target targets site sites position positions".split())

    def names(phrase, allowed):
        phrase = re.sub(r"-led\b", "", phrase)
        phrase = re.sub(r"'s\b|(?<=s)'", "", phrase)
        words = phrase.split()
        if not words or any(not re.fullmatch(r"[A-Za-z]+", word) for word in words):
            return set()
        anchors = []
        finished_name = False
        for word in words:
            lower = word.lower()
            if lower in allowed and (word.islower() or not anchors):
                finished_name = bool(anchors)
                continue
            if finished_name or not word[0].isupper():
                return set()
            anchors.append(lower)
        return {" ".join(anchors)} if anchors else set()

    actor = names(action["actor"], roles)
    if action["pronoun"]:
        # Bare proper names ending in s may be singular. Reporting agreement,
        # rather than their spelling alone, establishes the supported number.
        subject_head = action["actor"].rstrip("'").split()[-1].lower()
        plural = subject_head.endswith("s") and not subject_head.endswith("ss")
        if ((action["pronoun"] == "they" and (not plural or action["report"] == "says"))
                or (action["pronoun"] == "it" and action["report"] == "say")):
            return None
    # Only a bare actor with explicit plural agreement may lose terminal s.
    # Qualifiers (Vardis forces) and target names preserve their identity.
    if (re.fullmatch(r"[A-Z][a-z]+s", action["actor"])
            and not action["actor"].endswith("ss")
            and (action["pronoun"] == "they" or (not action["report"]
                 and action["verb"] in {"attack", "strike", "bomb", "shell"}))):
        actor = {name[:-1] for name in actor}
    # Only the leading target phrase is used, never a location or second clause.
    target_phrase = re.split(r"[,;:.]|\s+-\s+|\b(?:in|at|near|on|with|by|and)\b",
                             action["target"], maxsplit=1)[0].strip()
    target = names(target_phrase, objects)
    if not actor or not target:
        return None
    return actor, target


def _compatible(a, b):
    left_action, right_action = _foreground_action(a), _foreground_action(b)
    if left_action and right_action:
        left_actor, left_target = left_action
        right_actor, right_target = right_action
        if (left_actor.isdisjoint(right_actor) and left_actor & right_target
                and right_actor & left_target):
            return False
    a, b = _current_item(a), _current_item(b)
    left_details, right_details = _incident_details(a), _incident_details(b)
    # Airport adjectives can name a country rather than its incident city.
    # Only conflicting explicit capital names make this location veto definite.
    capitals = [{v.lower() for v in re.findall(r"\bcapital\s+([A-Z][a-z]+)\b",
                str(item.get("title", "")) + ". " + str(item.get("summary", "")))} for item in (a, b)]
    if (left_details[4] and right_details[4] and all(capitals)
            and not capitals[0] & capitals[1]):
        return False
    for index in (2, 3):
        if left_details[index] and right_details[index] and not left_details[index] & right_details[index]:
            return False
    # Explicit movement names veto a match based only on shared country words.
    def movement(item):
        qualifiers = set(re.findall(r"\b([a-z]+) (?:movement|protests?|demonstrations?)\b", _text(item)))
        return qualifiers - {"movement", "mass", "nationwide", "national", "peaceful",
                             "violent", "ongoing", "the", "these", "those", "of"}
    left_movement, right_movement = movement(a), movement(b)
    school_context = all(re.search(r"\bschools?\b", _text(item))
                         and re.search(r"\b(?:clos(?:e|es|ed|ing|ure|ures)|shut|shutter\w*|suspended)\b", _text(item))
                         for item in (a, b))
    school_terms = {"student", "students", "pupil", "pupils", "school", "schools", "education"}
    if school_context:
        left_movement = {"school_issue" if value in school_terms else value for value in left_movement}
        right_movement = {"school_issue" if value in school_terms else value for value in right_movement}
    # Infer geographic prefixes only from explicit location phrasing in this
    # pair; do not maintain a country registry or trim arbitrary proper names.
    context = ". ".join(str(item.get(key, "")) for item in (a, b)
                        for key in ("title", "summary"))
    locations = {match.lower() for match in re.findall(
        r"\b(?:in|across|throughout|from|near)\s+((?:[A-Z][a-z]+[- ]+)*[A-Z][a-z]+)(?=[.,;:]|$)", context)}

    def explicit_movement(item):
        generic = school_terms | {"the", "a", "this", "that", "protest", "protests",
                                  "mass", "national", "nationwide", "peaceful", "violent"}
        boundaries = generic | {"after", "during", "amid", "in", "across", "from", "following"}
        raw = str(item.get("title", "")) + ". " + str(item.get("summary", ""))
        # Quotation marks establish explicit name boundaries. Their meaningful
        # prefixes (including an article that belongs to the name) remain intact.
        names = {match.lower() for match in re.findall(
            r'["\u201c]((?:[A-Z][a-z]+[- ]+)*[A-Z][a-z]+)["\u201d] movement\b', raw)}
        for match in re.finditer(r"\b((?:[A-Z][a-z]+[- ]+)*[A-Z][a-z]+) movement\b", raw):
            words = match.group(1).lower().split()
            boundary = next((i for i in range(len(words) - 1, -1, -1)
                             if words[i] in boundaries), -1)
            name = " ".join(words[boundary + 1:])
            for location in locations:
                if name.startswith(location + " "):
                    name = name[len(location) + 1:]
            if name:
                names.add(name)
        # Lowercase single-word references retain the prior recognition contract.
        if not names:
            names = set(re.findall(r"\b([a-z]+) movement\b", _text(item))) - generic
        return names
    left_explicit, right_explicit = explicit_movement(a), explicit_movement(b)
    if left_explicit and right_explicit and not left_explicit & right_explicit:
        return False
    unrest = recognition_reason(a).startswith("unrest_") or recognition_reason(b).startswith("unrest_")
    if unrest and left_movement and right_movement and not left_movement & right_movement:
        return False
    if _flight_conflict(a, b):
        return False
    # A rare geographical anchor alone can join unrelated aviation incidents.
    # Conflicting concrete incident mechanisms veto that weak match.
    mechanisms = (r"\bstab\w*", r"\bbomb\w*", r"\bhijack\w*", r"\bearthquake\b", r"\btsunami\b")
    left = {n for n, pattern in enumerate(mechanisms) if re.search(pattern, _text(a))}
    right = {n for n, pattern in enumerate(mechanisms) if re.search(pattern, _text(b))}
    if left and right and not left & right:
        return False
    if _structured_same_incident(a, b):
        return True
    if str(a.get("title", "")).lower() == str(b.get("title", "")).lower():
        return True
    flight_ids = lambda item: set(re.findall(r"\b[A-Z]{2}\s?\d{2,4}\b", str(item.get("title", ""))))
    if flight_ids(a) & flight_ids(b):
        return True
    # Keep named carrier/flight/person/event anchors, while stripping ordinary
    # aviation/security vocabulary and geographical phrases. This adds identity
    # to the caller's rare-anchor matcher without requiring headline similarity.
    common = set("a an the on in at to from of for and with by after amid outside"
                 " airport airports terminal aircraft airliner airplane plane flight"
                 " international passenger passengers pilot pilots guard cockpit"
                 " attack attacks attacked attempted attempt attempts tried tries missile missiles"
                 " crash crashes crashed hijack hijacking hijacked stabbing stabbed"
                 " bomb bombing kills killed injures injured seize seizure police"
                 " officials official say says report reports reporting investigation"
                 " investigate security terror terrorist terrorism aboard during"
                 " earthquake tsunami mass shooting casualty casualties massacre"
                 " fbi suspect suspects suspected killer killers conspired conspiracy"
                 " plot plots plotting plotted arrests arrested arrest man men before"
                 " probe safety us federal authorities alleged alleges allegedly"
                 " mall malls school schools shopping centre center station church"
                 " hospital hotel stadium university venue victims victim people"
                 " dead death deaths wounded casualties gunman gunmen gunfire shooter"
                 " shooters launch launches launched investigating investigates"
                 " one two three four five six seven eight nine ten eleven twelve"
                 " thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
                 " twenty thirty forty fifty sixty seventy eighty ninety hundred"
                 " thousand million dozen dozens several many multiple more least"
                 " protest protests protesting protesters demonstration demonstrations"
                 " unrest movement nationwide national closures closure closed blockades"
                 " blockade blocked government cabinet president minister prime emergency"
                 " response serious seriously injuries injury critical critically harm"
                 " announces announced".split())
    def anchors(item):
        text = _text(item)
        text = re.sub(r"\b(?:in|at|to|from|near|over)\s+[a-z]+(?:\s+[a-z]+)?(?=\s+(?:airport|city|country|province|region)\b)", " ", text)
        text = re.sub(r"\b[a-z]+\s+(?=airport\b)", " ", text)
        return {token for token in re.findall(r"[a-z0-9]+", text)
                if token not in common and not token.isdigit() and len(token) > 2}
    # Carrier equality supports a match but cannot establish incident identity.
    # Require an additional incident anchor, such as a shared place or person.
    shared = anchors(a) & anchors(b)
    shared -= _carriers(a) | _carriers(b)
    if unrest:
        shared -= set("announce imposes imposed impose curfew troops deploy deploys deployed"
                      " deploying declaration declares declared following because prompted"
                      " prompts prompt force forces forced resign resigns resigned resignation"
                      " dissolves dissolved dissolution steps step down state into across"
                      " country also continue continues".split())
        # One shared country/city is insufficient. A named movement is an
        # explicit identity anchor; otherwise require two non-generic anchors.
        if left_explicit or right_explicit:
            return bool(left_explicit & right_explicit) or len(shared - left_movement - right_movement) >= 2
        if "school_issue" in left_movement & right_movement:
            # Generic participants/topics agree only in school-disruption
            # context and still need the caller's matcher plus concrete anchors.
            return len(shared - school_terms) >= 2
        return bool(left_movement & right_movement) or len(shared) >= 2
    return bool(shared)


def _carriers(item):
    title = str(item.get("title", "")) + " " + str(item.get("summary", ""))
    # Context identifies a carrier without a registry or airline-specific rule.
    matches = re.findall(r"(?<![\w-])([a-z][a-z0-9]+)\s+(?:flight|plane|aircraft|airliner|pilot|airlines?)\b", title, re.IGNORECASE)
    matches += re.findall(r"\b([A-Z][a-z]+[A-Z][A-Za-z0-9]+)\b", title)
    return {m.lower() for m in matches} - set(
        "passenger international domestic commercial private charter cargo military"
        " french british american indian israeli russian chinese japanese canadian"
        " australian german italian spanish ukrainian iranian pakistani turkish"
        " civilian the a an on aboard of to from in for at with by said says"
        " this that another other first second third his her their our earlier later"
        " diverted rerouted stricken scheduled replacement incoming outgoing"
        " pilot airport aircraft airliner airplane plane flight cockpit hijacked"
        " crashed attack attacks attacked stabbing stabbed attempted attempts"
        " seize seizes seized hijack hijacking hijacker crash crashes bombing bomb"
        " investigate investigation police security terror terrorist terrorists".split())


def _flight_conflict(a, b):
    left_ids = set(re.findall(r"\b[A-Z]{2}\s?\d{2,4}\b", str(a.get("title", ""))))
    right_ids = set(re.findall(r"\b[A-Z]{2}\s?\d{2,4}\b", str(b.get("title", ""))))
    return bool(left_ids and right_ids and not left_ids & right_ids)


def _same_carrier(a, b):
    return not _flight_conflict(a, b) and bool(_carriers(a) & _carriers(b))


def protect(pool, selected, profile, same_story, score_fn, *, audit=None):
    """Protect corroborated filtered events without expanding desk capacity.

    Caller supplies the existing story matcher and novelty-aware scoring. Pool
    must already pass freshness/noise/local gates and exclude unchanged seen
    articles; nonpositive-scoring representatives are additionally ineligible.
    """
    result = {desk: list(items) for desk, items in selected.items()}
    settings = profile["major_events"]
    if not settings["enabled"] or not callable(same_story):
        return result, []
    capacities = {desk: len(items) for desk, items in result.items()}
    groups = []
    for item in sorted(pool, key=lambda i: str(i.get("url", ""))):
        if not _serious(item) or not item.get("url"):
            continue
        matches = [g for g in groups if all(_compatible(item, previous) for previous in g)
                   and any(_compatible(item, previous)
                           and (same_story(item, previous) or _same_carrier(item, previous)
                                or _structured_same_incident(item, previous))
                           for previous in g)]
        if not matches:
            groups.append([item])
        else:
            group = matches[0]
            for other in matches[1:]:
                # A bridge must not merge groups lacking shared incident identity.
                if not all(_compatible(a, b) for a in group for b in other):
                    continue
                group.extend(other)
                groups.remove(other)
            group.append(item)
    eligible = []
    for group in groups:
        quality = [i for i in group if i.get("tier") in (1, 2)]
        publishers = {_publisher(i) for i in quality} - {""}
        if len(publishers) < settings["min_publishers"]:
            if audit is not None:
                audit.append({"title": group[0].get("title", ""),
                              "urls": sorted({str(i["url"]) for i in group}),
                              "eligible": False, "stage": "corroboration",
                              "reason": "lack_independent_corroboration"})
            continue
        ranked = sorted(quality, key=lambda i: (-score_fn(i), str(i["url"])))
        if ranked and score_fn(ranked[0]) > 0:
            eligible.append((ranked, group))
    eligible.sort(key=lambda pair: (-score_fn(pair[0][0]), str(pair[0][0]["url"])))
    events, locked = [], set()
    for ranked, group in eligible:
        record = {"title": ranked[0].get("title", ""),
                  "urls": sorted({str(i["url"]) for i in group}),
                  "recognition_reasons": sorted({recognition_reason(i) for i in group}),
                  "eligible": True, "stage": "selection", "reason": "protected"}
        if audit is not None:
            audit.append(record)
        if len(events) >= settings["max_events"]:
            record["reason"] = "excluded_by_capacity"
            continue
        lead = ranked[0]
        desk = lead.get("desk", "world")
        capacity = len(result.get(desk, []))
        if not capacity:
            record["reason"] = "excluded_by_desk_capacity"
            continue
        # Reserve distinct events before adding alternate reports.
        chosen = [i for i in ranked if i.get("desk", "world") == desk][:1]
        current = result[desk]
        for candidate in chosen:
            if any(i.get("url") == candidate["url"] for i in current):
                # Keep the vetted membership and only one slot for this article.
                position = next(n for n, i in enumerate(current) if i.get("url") == candidate["url"])
                current[:] = [i for n, i in enumerate(current)
                              if i.get("url") != candidate["url"] or n == position]
                current[position] = candidate
                locked.add(candidate["url"])
                continue
            removable = [i for i in current if i.get("url") not in locked]
            if not removable:
                break
            victim = min(removable, key=lambda i: (score_fn(i), str(i.get("url", ""))))
            current.remove(victim)
            current.append(candidate)
            locked.add(candidate["url"])
        selected_urls = list(dict.fromkeys(i["url"] for i in ranked if i["url"] in locked))
        if not selected_urls:
            record["reason"] = "excluded_by_desk_capacity"
            continue
        urls = sorted({str(i["url"]) for i in group})
        events.append({"id": hashlib.sha256("\n".join(urls).encode()).hexdigest()[:16],
                       "title": lead.get("title", ""), "urls": urls,
                       "selected_urls": selected_urls,
                       "max_events": settings["max_events"],
                       "recognition_reasons": record["recognition_reasons"],
                       "evidence": [{"publisher": _publisher(i), "source": i.get("source", ""),
                                     "tier": i.get("tier"), "url": i["url"]} for i in group]})
    for event in events:
        if len(event["selected_urls"]) >= 2:
            continue
        alternates = sorted([i for i in pool if i.get("url") in event["urls"]
                             and i.get("tier") in (1, 2) and i.get("url") not in locked],
                            key=lambda i: (-score_fn(i), str(i["url"])))
        for alternate in alternates:
            current = result.get(alternate.get("desk", "world"), [])
            if any(i.get("url") == alternate["url"] for i in current):
                position = next(n for n, i in enumerate(current) if i.get("url") == alternate["url"])
                current[:] = [i for n, i in enumerate(current)
                              if i.get("url") != alternate["url"] or n == position]
                current[position] = alternate
                locked.add(alternate["url"])
                event["selected_urls"].append(alternate["url"])
                break
            removable = [i for i in current if i.get("url") not in locked]
            if len(current) >= capacities.get(alternate.get("desk", "world"), 0):
                if not removable:
                    continue
                victim = min(removable, key=lambda i: (score_fn(i), str(i.get("url", ""))))
                current.remove(victim)
            current.append(alternate)
            locked.add(alternate["url"])
            event["selected_urls"].append(alternate["url"])
            break
    # Protection must not undo the existing same-story cap. Extra selected
    # reports from a protected event are replaced with unrelated filtered pool
    # candidates where available, otherwise the desk remains below capacity.
    for event in events:
        urls = set(event["urls"])
        for desk, current in result.items():
            extras = [i for i in current if i.get("url") in urls
                      and i.get("url") not in event["selected_urls"]]
            for extra in extras:
                current.remove(extra)
                replacements = [i for i in pool if i.get("desk", "world") == desk
                                and i.get("url") not in urls
                                and not any(j.get("url") == i.get("url") for j in current)]
                if replacements:
                    current.append(max(replacements, key=lambda i: score_fn(i)))
    return result, events
