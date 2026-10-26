"""world-intel Module B — the on-disk knowledge base.

An llm_wiki-compatible markdown store (markdown pages + YAML-ish frontmatter)
plus a small JSON run-state. Pure standard library: a tiny frontmatter
reader/writer is implemented here rather than depending on PyYAML.

Layout (design spec §4):

    <root>/
    ├── purpose.md
    ├── schema.md
    ├── state.json
    ├── raw/
    ├── briefings/
    └── wiki/
        ├── index.md
        ├── overview.md
        ├── log.md                 (created lazily by append_log)
        ├── daily/<date>.md        (write_daily)
        ├── entities/<slug>.md
        ├── threads/<slug>.md
        └── markets/scoreboard.md

All file I/O is UTF-8; the module is import-safe (no side effects at import).
The default KB root resolves runtime.data_root at call time; callers may pass
`root` explicitly.
"""

from worldintel import config as wi_config
import json
import os
import re
from datetime import datetime, timedelta, timezone


def default_root() -> str:
    return wi_config.get().data_root

# Frontmatter key ordering for stable, readable pages.
_ENTITY_KEYS = ["slug", "name", "type", "aliases", "last_updated", "threads"]
_THREAD_KEYS = ["slug", "title", "status", "started", "last_updated", "entities"]

# wiki/markets/history.jsonl retention window (days).
HISTORY_RETENTION_DAYS = 750


# --------------------------------------------------------------------------- #
# small path / fs helpers
# --------------------------------------------------------------------------- #
def local_now() -> datetime:
    """Return an aware clock reading in the configured IANA timezone."""
    return datetime.now(wi_config.get().timezone)


def local_today() -> str:
    return local_now().date().isoformat()


def _today() -> str:
    """Today's date as YYYY-MM-DD in the configured timezone."""
    return local_today()


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _wiki(root: str, *parts: str) -> str:
    return os.path.join(root, "wiki", *parts)


# --------------------------------------------------------------------------- #
# frontmatter (tiny YAML-subset) reader / writer
# --------------------------------------------------------------------------- #
def _fm_scalar(value: str):
    """Parse a frontmatter scalar / inline list. Lists are `[a, b, c]`."""
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [item.strip() for item in inner.split(",") if item.strip()]
    return value


def _fm_render_value(value) -> str:
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(str(v) for v in value) + "]"
    return str(value)


def _parse_page(text: str):
    """Split a markdown page into (frontmatter_dict, body_str).

    A page with no leading `---` block yields ({}, full_text).
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    # lines[0] == '---'; find the closing '---'
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    meta = {}
    for line in lines[1:end]:
        if not line.strip() or ":" not in line:
            continue
        key, _, raw = line.partition(":")
        meta[key.strip()] = _fm_scalar(raw)
    body = "\n".join(lines[end + 1:])
    if body.startswith("\n"):
        body = body[1:]
    return meta, body


def _render_page(meta: dict, body: str, key_order=None) -> str:
    keys = list(key_order or [])
    for k in meta:
        if k not in keys:
            keys.append(k)
    out = ["---"]
    for k in keys:
        if k in meta:
            out.append(f"{k}: {_fm_render_value(meta[k])}")
    out.append("---")
    out.append("")
    text = "\n".join(out) + body
    if not text.endswith("\n"):
        text += "\n"
    return text


def _read_page(path: str):
    """Read a markdown page file into (meta, body). Missing file -> ({}, '')."""
    if not os.path.isfile(path):
        return {}, ""
    return _parse_page(_read_text(path))


# --------------------------------------------------------------------------- #
# body section helpers (shared shape: leading summary + **Marker:** sections)
# --------------------------------------------------------------------------- #
def _split_sections(body: str, markers: dict):
    """Split a body into {'summary': str, <name>: [lines], ...}.

    `markers` maps the literal `**Marker:**` prefix to a section name. Text on
    the same line as the marker is kept as the first line of that section.
    """
    sections = {"summary": []}
    current = "summary"
    for line in body.splitlines():
        stripped = line.strip()
        hit = None
        for marker, name in markers.items():
            if stripped.startswith(marker):
                rest = stripped[len(marker):].strip()
                sections[name] = [rest] if rest else []
                current = name
                hit = True
                break
        if hit:
            continue
        sections.setdefault(current, []).append(line)
    sections["summary"] = "\n".join(sections["summary"]).strip()
    return sections


def _bullets(lines):
    """Extract bullet payloads (`- text`), dropping placeholders like `(none)`."""
    out = []
    for line in lines:
        s = line.strip()
        if s.startswith("- "):
            payload = s[2:].strip()
            if payload and not (payload.startswith("(") and payload.endswith(")")):
                out.append(payload)
    return out


# --------------------------------------------------------------------------- #
# entity page render / parse / merge
# --------------------------------------------------------------------------- #
_ENTITY_MARKERS = {"**Key facts:**": "facts", "**Source refs:**": "refs"}


def _render_entity_body(summary, facts, refs) -> str:
    parts = [summary.strip() if summary else "", "", "**Key facts:**"]
    parts += [f"- {f}" for f in facts] if facts else ["- (none yet)"]
    parts += ["", "**Source refs:**"]
    parts += [f"- {r}" for r in refs] if refs else ["- (none yet)"]
    return "\n".join(parts).rstrip() + "\n"


def _parse_entity_body(body):
    sec = _split_sections(body, _ENTITY_MARKERS)
    return {
        "summary": sec["summary"],
        "facts": _bullets(sec.get("facts", [])),
        "refs": _bullets(sec.get("refs", [])),
    }


def _upsert_entity(root: str, item: dict) -> str:
    slug = str(item["slug"]).strip()
    if not slug:
        raise ValueError("entity slug is empty")
    path = _wiki(root, "entities", f"{slug}.md")
    meta, body = _read_page(path)
    old = _parse_entity_body(body) if body else {"summary": "", "facts": [], "refs": []}

    meta.setdefault("slug", slug)
    meta["slug"] = slug
    if item.get("type"):
        meta["type"] = item["type"]
    meta.setdefault("type", meta.get("type", "unknown"))
    meta.setdefault("name", meta.get("name") or slug)
    meta.setdefault("aliases", meta.get("aliases", []))

    # merge threads (union, order preserved)
    threads = list(meta.get("threads") or [])
    for t in (item.get("threads") or []):
        if t not in threads:
            threads.append(t)
    meta["threads"] = threads
    meta["last_updated"] = _today()

    summary = item.get("summary") or old["summary"]
    facts = list(old["facts"])
    for f in (item.get("facts") or []):
        if f not in facts:
            facts.append(f)

    _write_text(path, _render_page(meta, _render_entity_body(summary, facts, old["refs"]),
                                   key_order=_ENTITY_KEYS))
    return path


# --------------------------------------------------------------------------- #
# thread page render / parse / merge
# --------------------------------------------------------------------------- #
_THREAD_MARKERS = {
    "**Timeline:**": "timeline",
    "**Watch:**": "watch",
    "**Expectations:**": "expectations",
}


def _render_thread_body(summary, timeline, watch, expectations) -> str:
    parts = [summary.strip() if summary else "", "", "**Timeline:**"]
    if timeline:
        parts += [f"- {d} — {note}" for d, note in timeline]
    else:
        parts.append("- (none yet)")
    parts += ["", f"**Watch:** {watch.strip() if watch else '(nothing flagged)'}"]
    parts += ["", "**Expectations:**"]
    parts += [f"- {e}" for e in expectations] if expectations else ["- (none)"]
    return "\n".join(parts).rstrip() + "\n"


def _parse_thread_body(body):
    sec = _split_sections(body, _THREAD_MARKERS)
    timeline = []
    for payload in _bullets(sec.get("timeline", [])):
        if " — " in payload:
            date, note = payload.split(" — ", 1)
            timeline.append((date.strip(), note.strip()))
        else:
            timeline.append(("", payload))
    return {
        "summary": sec["summary"],
        "timeline": timeline,
        "watch": "\n".join(sec.get("watch", [])).strip(),
        "expectations": _bullets(sec.get("expectations", [])),
    }


def _upsert_thread(root: str, item: dict) -> str:
    slug = str(item["slug"]).strip()
    if not slug:
        raise ValueError("thread slug is empty")
    path = _wiki(root, "threads", f"{slug}.md")
    meta, body = _read_page(path)
    old = (_parse_thread_body(body) if body
           else {"summary": "", "timeline": [], "watch": "", "expectations": []})

    meta["slug"] = slug
    if item.get("status"):
        meta["status"] = item["status"]
    meta.setdefault("status", "active")
    meta.setdefault("title", meta.get("title") or slug)
    meta.setdefault("entities", meta.get("entities", []))
    meta.setdefault("started", meta.get("started") or _today())
    meta["last_updated"] = _today()

    summary = item.get("summary") or old["summary"]
    watch = item.get("watch") or old["watch"]

    # merge timeline (union of (date, note)), newest first
    timeline = list(old["timeline"])
    for entry in (item.get("timeline_add") or []):
        try:
            pair = (str(entry.get("date", "")).strip(), str(entry.get("note", "")).strip())
        except AttributeError:
            continue
        if pair not in timeline:
            timeline.append(pair)
    timeline.sort(key=lambda p: p[0], reverse=True)

    # human-readable copy of expectations on the page (authoritative copy is state.json)
    exp_lines = list(old["expectations"])
    for e in (item.get("expectations_add") or []):
        if isinstance(e, dict):
            line = "{id} — {metric}: {consensus} (due {release_date})".format(
                id=e.get("id", "?"), metric=e.get("metric", "?"),
                consensus=e.get("consensus", "?"), release_date=e.get("release_date", "?"))
            if line not in exp_lines:
                exp_lines.append(line)

    _write_text(path, _render_page(
        meta, _render_thread_body(summary, timeline, watch, exp_lines),
        key_order=_THREAD_KEYS))
    return path


# --------------------------------------------------------------------------- #
# state.json
# --------------------------------------------------------------------------- #
def _state_path(root: str) -> str:
    return os.path.join(root, "state.json")


def _empty_state() -> dict:
    return {
        "last_run_date": None,
        "expectations": [],
        "seen_story_hashes": [],
        "source_health": {},
        "codex": {},
    }


def read_state(root: str) -> dict:
    path = _state_path(root)
    if not os.path.isfile(path):
        return _empty_state()
    try:
        data = json.loads(_read_text(path) or "{}")
    except (ValueError, OSError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    base = _empty_state()
    base.update(data)
    return base


def write_state(root: str, state: dict) -> None:
    _write_text(_state_path(root), json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def record_buried(root: str, stories: list, date: str) -> None:
    """Remember today's buried-signal stories so the novelty gate has memory.

    Codex has no memory of what it already surfaced, so the same buried items
    repeated day after day. This appends {"story", "date"} entries to a
    `recent_buried` list in state.json (skipping blanks/non-strings and exact
    duplicate (story, date) pairs), then prunes entries older than 7 days
    before `date` — malformed old entries are dropped in the prune. Best
    effort: on any failure the day's dedup memory is simply lost; never raises.
    """
    try:
        state = read_state(root)
        entries = [e for e in _as_list(state.get("recent_buried")) if isinstance(e, dict)]
        seen = {(e.get("story"), e.get("date")) for e in entries}
        for story in _as_list(stories):
            if not isinstance(story, str) or not story.strip():
                continue
            if (story, date) in seen:
                continue
            entries.append({"story": story, "date": date})
            seen.add((story, date))
        cutoff = datetime.strptime(date, "%Y-%m-%d") - timedelta(
            days=wi_config.get().tuning.get("retention_days", {}).get("buried", 7))
        kept = []
        for e in entries:
            try:
                if datetime.strptime(str(e.get("date")), "%Y-%m-%d") >= cutoff:
                    kept.append(e)
            except ValueError:
                continue  # unparseable date -> drop the entry
        state["recent_buried"] = kept
        write_state(root, state)
    except wi_config.ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 — resilience is the contract
        print(f"[kb] record_buried failed (novelty dedup lost for {date}): {exc}")


def record_seen_articles(root: str, articles: list, date: str) -> None:
    """Remember which articles reached a desk, for the cross-day novelty gate.

    Fills `state['seen_story_hashes']` with {"hash", "url", "title", "date"}
    entries and prunes anything older than 14 days before `date`. Best effort:
    on any failure the day's memory is lost rather than raising.
    """
    try:
        from worldintel.core.select_news import article_hash
    except wi_config.ConfigError:
        raise
    except Exception:
        return
    try:
        state = read_state(root)
        entries = [e for e in _as_list(state.get("seen_story_hashes"))
                   if isinstance(e, dict)]
        known = {e.get("hash") for e in entries}
        for art in _as_list(articles):
            if not isinstance(art, dict) or not art.get("title"):
                continue
            h = article_hash(art)
            if not h or h in known:
                continue
            entries.append({
                "hash": h,
                "url": (art.get("url") or "")[:300],
                "title": (art.get("title") or "")[:200],
                "date": date,
            })
            known.add(h)
        cutoff = datetime.strptime(date, "%Y-%m-%d") - timedelta(
            days=wi_config.get().tuning.get("retention_days", {}).get("seen", 14))
        kept = []
        for e in entries:
            try:
                if datetime.strptime(str(e.get("date")), "%Y-%m-%d") >= cutoff:
                    kept.append(e)
            except ValueError:
                continue  # unparseable date -> drop
        state["seen_story_hashes"] = kept
        write_state(root, state)
    except wi_config.ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 — resilience is the contract
        print(f"[kb] record_seen_articles failed (novelty memory lost "
              f"for {date}): {exc}")


def load_seen(root: str, date: str, days: int | None = None) -> set:
    """Return the set of article hashes seen within `days` before `date`.

    Never raises; an unreadable or absent state yields an empty set.
    """
    if days is None:
        days = wi_config.get().tuning.get("retention_days", {}).get("seen", 14)
    try:
        cutoff = datetime.strptime(date, "%Y-%m-%d") - timedelta(days=days)
    except wi_config.ConfigError:
        raise
    except Exception:
        return set()
    out = set()
    try:
        for e in _as_list(read_state(root).get("seen_story_hashes")):
            if not isinstance(e, dict) or not e.get("hash"):
                continue
            try:
                if datetime.strptime(str(e.get("date")), "%Y-%m-%d") >= cutoff:
                    out.add(e["hash"])
            except ValueError:
                continue
    except wi_config.ConfigError:
        raise
    except Exception:
        return set()
    return out


# --------------------------------------------------------------------------- #
# ensure_kb
# --------------------------------------------------------------------------- #
_PURPOSE_MD = """\
# Purpose

This wiki is the accumulating memory of the **world-intel** briefing system. It
exists to help its reader genuinely *understand* world events: not a headline
feed, but a brain that connects today to prior storylines so the pieces of the
puzzle fit together.

Coverage follows the configured news desks and market sources.
It is updated by the world-intel
pipeline; the analysis engine reads only the bounded context (active threads,
overview, market scoreboard, expectations, plus the dossiers of entities
mentioned in that day's news).
"""

_SCHEMA_MD = """\
# Schema & conventions

Pages are markdown with a small YAML-style frontmatter block delimited by `---`.
Frontmatter values are either scalars (`key: value`) or inline lists
(`key: [a, b, c]`). Dates are `YYYY-MM-DD` in the configured runtime timezone.

## entities/<slug>.md
    slug, name, type (country|institution|person|company), aliases (list),
    last_updated, threads (list)
Body: summary paragraph, then `**Key facts:**` bullets, then `**Source refs:**`.
Pages whose name/aliases appear in today's news are loaded into engine context.

## threads/<slug>.md
    slug, title, status (active|dormant|resolved), started, last_updated,
    entities (list)
Body: summary, then `**Timeline:**` (dated, newest first), `**Watch:**`,
`**Expectations:**`. Only `status: active` threads are loaded into engine context.

## state.json
Run state: last_run_date, expectations[], seen_story_hashes[], source_health{},
codex{}. Expectations are the authoritative surprise-vs-expectation store.
"""

_OVERVIEW_MD = """\
# State of the world

_No overview yet. This page is rewritten each run._
"""

_INDEX_MD = """\
# Index

- [Overview](overview.md)
- [Operation log](log.md)
- Entities: `entities/`
- Threads: `threads/`
- Markets scoreboard: `markets/scoreboard.md`
- Daily briefs: `daily/`
"""


def ensure_kb(root: str) -> None:
    """Create the dir tree and seed files if missing. Idempotent; never clobbers."""
    for d in (
        root,
        os.path.join(root, "raw"),
        os.path.join(root, "briefings"),
        _wiki(root),
        _wiki(root, "entities"),
        _wiki(root, "threads"),
        _wiki(root, "markets"),
        _wiki(root, "daily"),
    ):
        os.makedirs(d, exist_ok=True)

    seeds = {
        os.path.join(root, "purpose.md"): _PURPOSE_MD,
        os.path.join(root, "schema.md"): _SCHEMA_MD,
        _wiki(root, "overview.md"): _OVERVIEW_MD,
        _wiki(root, "index.md"): _INDEX_MD,
    }
    for path, content in seeds.items():
        if not os.path.exists(path):
            _write_text(path, content)

    if not os.path.exists(_state_path(root)):
        write_state(root, _empty_state())


# --------------------------------------------------------------------------- #
# load_context — the bounded memory handed to the analysis engine
# --------------------------------------------------------------------------- #
# Entity-dossier caps: the context stays bounded no matter how the wiki grows.
_MAX_ENTITIES = 8
_MAX_ENTITY_FACTS = 8


def _entity_terms(meta: dict) -> list:
    """Matchable names for an entity page: frontmatter name + aliases. The
    pipeline defaults `name` to the slug (lowercase-hyphenated), so each
    hyphenated term also yields a spaced variant ("fast-retailing" ->
    "fast retailing") to match natural headline text."""
    terms = []
    for value in [meta.get("name")] + list(meta.get("aliases") or []):
        term = str(value or "").strip()
        for t in (term, term.replace("-", " ").strip()):
            if t and t not in terms:
                terms.append(t)
    return terms


def _term_pattern(term: str):
    """Whole-word pattern for a term. All-caps acronyms (`US`, `BOJ`) match
    exactly so `US` never matches the word "us". Other short single words
    (`iran`, `fed`) must appear capitalized in the text ("Iran" yes, "fed the
    dog" no). Longer/multi-word terms match case-insensitively."""
    if term.isupper():
        return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)")
    if len(term) <= 4 and " " not in term:
        head, rest = term[0].upper(), term[1:]
        return re.compile(
            r"(?<!\w)" + re.escape(head) + "(?i:" + re.escape(rest) + r")(?!\w)")
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)


def _count_mentions(terms: list, texts: list) -> int:
    """Number of news texts mentioning any term."""
    patterns = [_term_pattern(t) for t in terms]
    return sum(1 for text in texts if any(p.search(text) for p in patterns))


def _relevant_entities(root: str, news) -> list:
    """Entity dossiers mentioned in today's news: top _MAX_ENTITIES by mention
    count, each with summary + the newest _MAX_ENTITY_FACTS facts. Malformed
    pages/news items are skipped; never raises."""
    texts = []
    for item in (news if isinstance(news, (list, tuple)) else []):
        if isinstance(item, dict):
            text = f"{item.get('title') or ''} {item.get('summary') or ''}".strip()
            if text:
                texts.append(text)
    ent_dir = _wiki(root, "entities")
    if not texts or not os.path.isdir(ent_dir):
        return []

    scored = []
    for fname in sorted(os.listdir(ent_dir)):
        if not fname.endswith(".md"):
            continue
        try:
            meta, body = _read_page(os.path.join(ent_dir, fname))
            count = _count_mentions(_entity_terms(meta), texts)
            if not count:
                continue
            parsed = _parse_entity_body(body)
            slug = meta.get("slug") or fname[:-3]
            scored.append((count, str(slug), {
                "slug": slug,
                "name": meta.get("name") or slug,
                "type": meta.get("type") or "unknown",
                "summary": parsed["summary"],
                "facts": parsed["facts"][-_MAX_ENTITY_FACTS:],
            }))
        except wi_config.ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 — resilience is the contract
            print(f"[kb] skipped entity page {fname}: {exc}")
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [entity for _, _, entity in scored[:_MAX_ENTITIES]]


def load_context(root: str, news=None) -> dict:
    """Return only active threads + overview + scoreboard + expectations
    + recently-shown buried stories (the Signal novelty gate's memory)
    + dossiers for entities that today's `news` items mention."""
    active = []
    threads_dir = _wiki(root, "threads")
    if os.path.isdir(threads_dir):
        for name in sorted(os.listdir(threads_dir)):
            if not name.endswith(".md"):
                continue
            meta, body = _read_page(os.path.join(threads_dir, name))
            if str(meta.get("status", "")).strip() != "active":
                continue
            parsed = _parse_thread_body(body)
            active.append({
                "slug": meta.get("slug", name[:-3]),
                "title": meta.get("title", meta.get("slug", name[:-3])),
                "summary": parsed["summary"],
                "watch": parsed["watch"],
            })

    overview_path = _wiki(root, "overview.md")
    overview = _read_text(overview_path) if os.path.isfile(overview_path) else ""

    scoreboard_path = _wiki(root, "markets", "scoreboard.md")
    scoreboard = _read_text(scoreboard_path) if os.path.isfile(scoreboard_path) else ""

    state = read_state(root)
    return {
        "active_threads": active,
        "overview": overview,
        "scoreboard": scoreboard,
        "expectations": state.get("expectations", []),
        # Already pruned by record_buried; read-only here (no pruning on load).
        "recent_buried": _as_list(state.get("recent_buried")),
        "entities": _relevant_entities(root, news),
    }


# --------------------------------------------------------------------------- #
# apply_deltas
# --------------------------------------------------------------------------- #
def apply_deltas(root: str, deltas: dict) -> list:
    """Apply a kb_deltas object; return the list of changed file paths.

    Merges entity/thread pages (never duplicates). A single malformed delta item
    is skipped (a note is printed) while the rest still apply. Never raises.
    """
    changed = []
    notes = []
    if not isinstance(deltas, dict):
        return changed

    state = read_state(root)
    state_dirty = False

    # entities
    for item in _as_list(deltas.get("entities_upsert")):
        try:
            if not isinstance(item, dict) or not str(item.get("slug", "")).strip():
                raise ValueError("entity missing slug")
            changed.append(_upsert_entity(root, item))
        except wi_config.ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 — resilience is the contract
            notes.append(f"skipped entity delta ({_brief(item)}): {exc}")

    # threads (+ expectations_add side-effect into state)
    for item in _as_list(deltas.get("threads_upsert")):
        try:
            if not isinstance(item, dict) or not str(item.get("slug", "")).strip():
                raise ValueError("thread missing slug")
            changed.append(_upsert_thread(root, item))
            for exp in _as_list(item.get("expectations_add")):
                if isinstance(exp, dict) and exp.get("id"):
                    if _upsert_expectation(state, exp):
                        state_dirty = True
        except wi_config.ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001
            notes.append(f"skipped thread delta ({_brief(item)}): {exc}")

    # overview
    if "overview_replace" in deltas:
        try:
            text = deltas["overview_replace"]
            if not isinstance(text, str):
                raise ValueError("overview_replace must be a string")
            path = _wiki(root, "overview.md")
            _write_text(path, text)
            changed.append(path)
        except wi_config.ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001
            notes.append(f"skipped overview_replace: {exc}")

    # expectations_resolved
    resolved = deltas.get("expectations_resolved")
    if isinstance(resolved, (list, tuple)):
        ids = {str(r) for r in resolved}
        before = len(state["expectations"])
        state["expectations"] = [e for e in state["expectations"]
                                 if str(e.get("id")) not in ids]
        if len(state["expectations"]) != before:
            state_dirty = True

    if state_dirty:
        write_state(root, state)
        changed.append(_state_path(root))

    for n in notes:
        print(f"[kb] {n}")

    # dedupe preserving order
    seen, out = set(), []
    for p in changed:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _as_list(value):
    return value if isinstance(value, (list, tuple)) else []


def _brief(item) -> str:
    if isinstance(item, dict):
        return str(item.get("slug") or item.get("id") or "?")
    return repr(item)[:40]


def _upsert_expectation(state: dict, exp: dict) -> bool:
    """Insert/replace an expectation by id. Returns True if state changed."""
    exps = state.setdefault("expectations", [])
    for i, existing in enumerate(exps):
        if str(existing.get("id")) == str(exp.get("id")):
            if existing == exp:
                return False
            exps[i] = exp
            return True
    exps.append(exp)
    return True


# --------------------------------------------------------------------------- #
# daily brief / log / scoreboard writers
# --------------------------------------------------------------------------- #
def write_daily(root: str, date: str, content: str) -> None:
    """Write the rendered daily brief to wiki/daily/<date>.md."""
    _write_text(_wiki(root, "daily", f"{date}.md"), content)


def append_log(root: str, line: str) -> None:
    """Append a line to wiki/log.md (created if missing)."""
    path = _wiki(root, "log.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


_SCOREBOARD_SECTIONS = [
    ("indices", "Indices"),
    ("fx", "FX"),
    ("rates", "Rates"),
    ("commodities", "Commodities"),
    ("risk", "Risk"),
]


def _horizon_note(q: dict, section: str) -> str:
    """Compact long-horizon context for a scoreboard line, per section:
    rates in bp, risk (VIX) as 1y percentile, everything else in %.
    Empty string when the quote carries no horizon fields.

    FX takes the % horizons but is ranked, not measured off its high — the same
    call `compose_html._ctx_line` makes, and it has to be the same call: the
    prose is written against this file and read against that table, so a stat
    only one of them carries is a figure the reader cannot check."""
    parts = []
    if section == "rates":
        for label, key in (("YTD", "ytd_abs"), ("90d", "d90_abs")):
            v = q.get(key)
            if isinstance(v, (int, float)):
                parts.append(f"{label} {round(v * 100):+d}bp")
    elif section == "risk":
        p = q.get("pctl_1y")
        if isinstance(p, (int, float)):
            parts.append(f"1y pctile {round(p)}")
    else:
        for label, key in (("YTD", "ytd_pct"), ("90d", "d90_pct")):
            v = q.get(key)
            if isinstance(v, (int, float)):
                parts.append(f"{label} {v:+.1f}%")
        if section == "fx":
            p = q.get("pctl_1y")
            if isinstance(p, (int, float)):
                parts.append(f"1y pctile {round(p)}")
        else:
            off = q.get("off_high_pct")
            if isinstance(off, (int, float)):
                parts.append("at 52w high" if off <= 0.05
                             else f"{off:.1f}% off high")
    return ", ".join(parts)


def _fmt_quote(q, section: str = "") -> str:
    if not isinstance(q, dict):
        return "n/a"
    level = q.get("level")
    change = q.get("change_pct")
    level_s = "n/a" if level is None else str(level)
    if change is None:
        base = level_s
    else:
        arrow = "▲" if change > 0 else ("▼" if change < 0 else "→")
        base = f"{level_s} ({arrow}{abs(change)}%)"
    extra = _horizon_note(q, section)
    return f"{base} | {extra}" if extra else base


def update_scoreboard(root: str, markets: dict) -> None:
    """Rewrite wiki/markets/scoreboard.md from a BUNDLE `markets` dict."""
    markets = markets if isinstance(markets, dict) else {}
    asof = ""
    for section, _ in _SCOREBOARD_SECTIONS:
        for q in (markets.get(section) or {}).values():
            if isinstance(q, dict) and q.get("asof"):
                asof = q["asof"]
                break
        if asof:
            break

    lines = ["# Market scoreboard", "", f"_Updated: {asof or _today()}_", ""]
    for section, title in _SCOREBOARD_SECTIONS:
        data = markets.get(section)
        if not isinstance(data, dict) or not data:
            continue
        lines.append(f"## {title}")
        for symbol, quote in data.items():
            lines.append(f"- {symbol}: {_fmt_quote(quote, section)}")
        lines.append("")
    _write_text(_wiki(root, "markets", "scoreboard.md"), "\n".join(lines).rstrip() + "\n")


# --------------------------------------------------------------------------- #
# scoreboard history — persisted daily market series (additive, independent
# of update_scoreboard/scoreboard.md; that markdown stays the prompt's memory)
# --------------------------------------------------------------------------- #
_HISTORY_QUOTE_KEYS = ("level", "change_pct", "change_abs", "asof")


def _history_path(root: str) -> str:
    return _wiki(root, "markets", "history.jsonl")


def _flatten_instruments(markets: dict) -> dict:
    """Flatten indices/fx/rates/commodities/risk into one {symbol: quote} map,
    carrying only the keys actually present (never writing nulls)."""
    instruments = {}
    for section, _ in _SCOREBOARD_SECTIONS:
        data = markets.get(section)
        if not isinstance(data, dict):
            continue
        for symbol, quote in data.items():
            if not isinstance(quote, dict):
                continue
            row = {k: quote[k] for k in _HISTORY_QUOTE_KEYS
                   if quote.get(k) is not None}
            instruments[symbol] = row
    return instruments


def _read_history_rows(path: str) -> list:
    """Read wiki/markets/history.jsonl into a list of row dicts. A malformed
    line is skipped rather than raising; missing file -> []."""
    rows = []
    if not os.path.isfile(path):
        return rows
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("date"):
                    rows.append(row)
    except OSError:
        return []
    return rows


def append_scoreboard_history(root: str, markets: dict, date: str) -> None:
    """Append one JSON line for `date` to wiki/markets/history.jsonl.

    Idempotent on `date`: replaces that day's row rather than duplicating it
    (a --force re-run for the same date wins). Prunes rows older than
    HISTORY_RETENTION_DAYS *relative to `date`* (assumes `date` is "today" —
    the normal once-daily cron call pattern; a backfill/replay call with a
    non-"today" `date` will prune relative to that date, not wall-clock time).
    `markets` is flattened into one `instruments` map (see
    `_flatten_instruments`). Never raises; best effort."""
    try:
        markets = markets if isinstance(markets, dict) else {}
        path = _history_path(root)
        rows = [r for r in _read_history_rows(path) if r.get("date") != date]
        rows.append({"date": date, "instruments": _flatten_instruments(markets)})

        try:
            cutoff = datetime.strptime(date, "%Y-%m-%d") - timedelta(days=HISTORY_RETENTION_DAYS)
        except (ValueError, TypeError):
            cutoff = None
        if cutoff is not None:
            kept = []
            for r in rows:
                try:
                    if datetime.strptime(str(r.get("date")), "%Y-%m-%d") >= cutoff:
                        kept.append(r)
                except ValueError:
                    continue  # unparseable date -> drop the row
            rows = kept

        rows.sort(key=lambda r: str(r.get("date") or ""))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    except wi_config.ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 — resilience is the contract
        print(f"[kb] append_scoreboard_history failed for {date}: {exc}")


def load_scoreboard_history(root: str, days: int = 400) -> list:
    """Return the persisted history rows, oldest first, windowed to the last
    `days` entries. Never raises; [] when the file is absent or unreadable."""
    try:
        rows = _read_history_rows(_history_path(root))
        rows.sort(key=lambda r: str(r.get("date") or ""))
        if isinstance(days, int) and days >= 0:
            rows = rows[-days:] if days else []
        return rows
    except wi_config.ConfigError:
        raise
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# companies/ — per-quarter structured panel results (additive KB wing; the
# entity markdown stays parsed-by-_ENTITY_MARKERS text and never carries this
# data directly — see the module docstring's wiki/companies/ wing)
# --------------------------------------------------------------------------- #
def _company_path(root: str, ticker: str) -> str:
    return _wiki(root, "companies", f"{ticker}.json")


def _read_company_rows(path: str) -> list:
    """Read a companies/<TICKER>.json sidecar into a list of quarter row
    dicts. Malformed JSON, a non-list body, or a row missing `period_end`
    degrades that row (or the whole file) out rather than raising; missing
    file -> []."""
    if not os.path.isfile(path):
        return []
    try:
        data = json.loads(_read_text(path) or "[]")
    except (ValueError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict) and r.get("period_end")]


def _link_company_panel(root: str, ticker: str) -> None:
    """Cross-link wiki/entities/<ticker>.md's frontmatter to the new sidecar,
    touching only the `panel` key. The entity page's body and every other
    frontmatter key are read back and rewritten untouched -- structured data
    must never reach the markdown that `_ENTITY_MARKERS`/`apply_deltas` own.

    Entity slugs are company names, not tickers (`advanced-micro-devices`, not
    `amd`), and no ticker->slug mapping exists. `ticker.lower()` is therefore
    only a guess at the page's location, and a miss must never fabricate an
    orphan stub beside the real dossier -- so the write is gated on the guess
    already existing. A ticker with no matching page simply gets no cross-link;
    the sidecar (keyed on the ticker itself) is unaffected and remains the
    load-bearing artifact."""
    slug = ticker.lower()
    path = _wiki(root, "entities", f"{slug}.md")
    if not os.path.isfile(path):
        return  # no matching entity page; the sidecar is the source of truth
    meta, body = _read_page(path)
    meta.setdefault("slug", slug)
    meta["panel"] = f"companies/{ticker}.json"
    _write_text(path, _render_page(meta, body, key_order=_ENTITY_KEYS))


def record_company_quarter(root: str, ticker: str, record: dict) -> None:
    """Upsert one quarter's panel result into wiki/companies/<TICKER>.json,
    keyed on `record["period_end"]` (replaces any existing row for that
    period rather than duplicating it). Also cross-links the ticker's entity
    page via a `panel:` frontmatter key. Never raises."""
    try:
        ticker = str(ticker or "").strip().upper()
        if not ticker or not isinstance(record, dict) or not record.get("period_end"):
            return
        path = _company_path(root, ticker)
        rows = [r for r in _read_company_rows(path) if r.get("period_end") != record["period_end"]]
        rows.append(record)
        rows.sort(key=lambda r: str(r.get("period_end") or ""))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        _link_company_panel(root, ticker)
    except wi_config.ConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 — resilience is the contract
        print(f"[kb] record_company_quarter failed for {ticker}: {exc}")


def load_company_history(root: str, ticker: str, quarters: int = 12) -> list:
    """Return that company's persisted quarters, oldest first, windowed to
    the last `quarters` entries. Never raises; [] for an unknown ticker or an
    unreadable sidecar."""
    try:
        ticker = str(ticker or "").strip().upper()
        if not ticker:
            return []
        rows = _read_company_rows(_company_path(root, ticker))
        rows.sort(key=lambda r: str(r.get("period_end") or ""))
        if isinstance(quarters, int) and quarters >= 0:
            rows = rows[-quarters:] if quarters else []
        return rows
    except wi_config.ConfigError:
        raise
    except Exception:
        return []
