"""Import-safe, fail-loud configuration shared by pipeline entry points."""
from __future__ import annotations

import copy
import difflib
import os
import re
import zoneinfo
from urllib.parse import urlsplit

import yaml

SUPPORTED_VERSIONS = (1,)
_ENV_VAR = "WORLD_INTEL_CONFIG"
_SCHEMA = {
    "version": int,
    "identity": {"contact_email": str, "user_agent_product": str},
    "runtime": {"timezone": str, "data_root": str, "media_dir": str, "env_file": str},
    "analysis": {"engine": str, "reasoning_effort": str, "concurrent_passes": bool, "source_review_enabled": bool,
                 "source_review_repair_enabled": bool,
                 "smoke_test_timeout_s": int, "world_timeout_s": int,
                 "finance_timeout_s": int, "pipeline_timeout_s": int,
                 "finish_reserve_s": int, "rewrite_timeout_s": int},
    "desks": list,  # Entry shapes are validated by _load_desks.
    "ranking": dict,  # The coverage policy validates its nested profile.
    "sources": {
        "feeds": list,
        "markets": {key: list for key in ("indices", "fx", "rates", "commodities", "risk")},
        "macro": {"fred_series": list}, "events": {"gdelt_themes": list},
        "filings": {"enabled": bool, "edgar_forms": list},
        "earnings": {"enabled": bool, "window_days": int, "watchlist": list}},
    "tuning": {"freshness_hours": int, "per_desk_slots": int, "per_outlet_cap": int,
               "region_tokens": list,
               "enrichment": {key: int for key in ("max_items", "concurrency", "min_chars", "max_chars")},
               "deadlines_s": {key: int for key in ("anomaly", "filing_text", "filing_index")},
               "retention_days": {key: int for key in ("seen", "buried", "post_earnings", "filing_index")}},
    "delivery": {"adapter": str},
}


class ConfigError(Exception):
    """A configuration failure naming the setting or file to repair."""


def _need(value, expected, path):
    if type(value) is not expected:
        label = {dict: "mapping", list: "list", int: "whole number", str: "string", bool: "boolean"}[expected]
        raise ConfigError(f"`{path}` must be a {label}, not {type(value).__name__}")
    return value


def _text(value, path):
    _need(value, str, path)
    if not value.strip() or any(ord(c) < 32 for c in value):
        raise ConfigError(f"`{path}` must be a non-empty string without control characters")
    return value.strip()


def _integer(value, path, lo, hi):
    _need(value, int, path)
    if not lo <= value <= hi:
        raise ConfigError(f"`{path}` must be between {lo} and {hi}")
    return value


def _need_list(value, dotted):
    return _need(value, list, dotted)


def _need_mapping(value, dotted):
    return _need(value, dict, dotted)


def _need_int(value, dotted, lo=None, hi=None):
    _need(value, int, dotted)
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise ConfigError(f"`{dotted}` must be between {lo} and {hi}")
    return value


def _walk(node, schema, prefix, cfg):
    _need(node, dict, prefix or "configuration")
    for key, value in node.items():
        _need(key, str, prefix or "configuration key")
        path = f"{prefix}.{key}" if prefix else key
        if key not in schema:
            near = difflib.get_close_matches(key, list(schema), n=1)
            hint = f" (did you mean `{near[0]}`?)" if near else ""
            cfg.warnings.append(f"unknown key `{path}`{hint}")
            cfg._ignored.append(path)
            continue
        cfg._recognized.append(path)
        expected = schema[key]
        if isinstance(expected, dict):
            _walk(value, expected, path, cfg)
        else:
            _need(value, expected, path)


def _path(value, key):
    return os.path.abspath(os.path.expanduser(_text(value, key)))


def _rows(value, path, width):
    result = []
    for n, row in enumerate(_need(value, list, path)):
        key = f"{path}[{n}]"
        _need(row, list, key)
        if len(row) != width:
            raise ConfigError(f"`{key}` must have {width} entries")
        result.append(tuple(_text(v, f"{key}[{i}]") for i, v in enumerate(row)))
    return result


_DESK_KINDS = ("global", "national", "finance")
_RESERVED_COLOUR_IDS = frozenset(("us", "americas", "asia", "mena", "europe", "global"))
_RESERVED_SECTION_IDS = frozenset(("brief", "signal", "threads", "on_radar", "finance", "post_earnings", "kb_deltas", "source_notes"))
_DESK_KEYS = {"id": str, "kind": str, "label": str, "tab": {"colour": str, "order": int},
              "target_stories": list, "prompt_section": str, "signature_patterns": list}


def _load_desks(raw, cfg):
    """Validate desk data without sorting or rewriting verbatim prompt sections."""
    desks = _need(raw.get("desks", []), list, "desks")
    out, ids, orders = [], set(), set()
    for i, desk in enumerate(desks):
        key = f"desks[{i}]"
        _walk(desk, _DESK_KEYS, key, cfg)
        did = _need(desk.get("id", ""), str, key + ".id")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", did):
            raise ConfigError(f"`{key}.id` must be a lowercase ASCII identifier")
        if did in ids:
            raise ConfigError(f"duplicate desk id {did!r}")
        ids.add(did)
        kind = desk.get("kind")
        if kind not in _DESK_KINDS:
            raise ConfigError(f"`{key}.kind` must be one of {_DESK_KINDS}; got {kind!r}")
        if kind == "global" and did not in ("world", "us"):
            raise ConfigError(f"`{key}`: global desks use the fixed ids world or us")
        if kind == "finance" and did != "finance":
            raise ConfigError(f"`{key}`: the finance pipeline requires id finance")
        if did in ("world", "us") and kind != "global":
            raise ConfigError(f"desk id {did!r} is reserved for kind global")
        label = _text(desk.get("label", ""), key + ".label")
        entry = copy.deepcopy(desk)
        entry.update(id=did, kind=kind, label=label)
        if kind == "national":
            if did in _RESERVED_COLOUR_IDS or did in _RESERVED_SECTION_IDS:
                raise ConfigError(f"desk id {did!r} collides with a reserved schema section or region colour")
            section = desk.get("prompt_section")
            if type(section) is not str or not section.strip():
                raise ConfigError(f"national desk {did!r} requires `prompt_section`")
            if "{{" in section:
                raise ConfigError(f"`{key}.prompt_section` cannot contain template markers")
            references = set(re.findall(r"\bbriefing\.([a-z][a-z0-9_]*)\b", section))
            if did not in references:
                raise ConfigError(f"`{key}.prompt_section` must reference briefing.{did}")
            if any(ord(c) < 32 and c not in "\r\n\t" for c in section):
                raise ConfigError(f"`{key}.prompt_section` contains control characters")
            targets = _need(desk.get("target_stories"), list, key + ".target_stories")
            if len(targets) != 2 or any(type(v) is not int for v in targets):
                raise ConfigError(f"`{key}.target_stories` must contain [min, max] integers")
            if targets[0] < 0 or targets[1] < 1 or targets[0] > targets[1]:
                raise ConfigError(f"`{key}.target_stories` requires 0 <= min <= max and max >= 1")
            tab = _need(desk.get("tab"), dict, key + ".tab")
            order = _need(tab.get("order"), int, key + ".tab.order")
            if order < 0 or order in orders:
                raise ConfigError(f"`{key}.tab.order` must be non-negative and unique")
            orders.add(order)
            colour = _text(tab.get("colour", ""), key + ".tab.colour")
            # One CSS colour value only; no URLs, declarations, comments, HTML,
            # or arbitrary function names can cross the style-element boundary.
            colour_pattern = r"(?:#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})|[a-zA-Z]+|(?:oklch|oklab|rgb|rgba|hsl|hsla|lch|lab)\([0-9.%+\-/, ]+\))"
            if not re.fullmatch(colour_pattern, colour):
                raise ConfigError(f"`{key}.tab.colour` must be a safe CSS colour")
            entry["tab"]["colour"] = colour
            patterns = _need(desk.get("signature_patterns", []), list, key + ".signature_patterns")
            for n, pattern in enumerate(patterns):
                _text(pattern, f"{key}.signature_patterns[{n}]")
                try:
                    re.compile(pattern, re.IGNORECASE)
                except re.error as exc:
                    raise ConfigError(f"`{key}.signature_patterns[{n}]`: {exc}") from exc
            try:
                entry["_signature_re"] = re.compile("|".join(patterns) if patterns else r"(?!)", re.IGNORECASE)
            except re.error as exc:
                raise ConfigError(f"`{key}.signature_patterns`: combined regex is invalid: {exc}") from exc
            entry["signature_patterns"] = list(patterns)
        out.append(entry)
    # The content router falls back to world for an unknown source or a national
    # story outside its mandate. A declared desk set must retain that destination.
    # Empty/absent desks remain valid for non-news configuration and loader use.
    if out and "world" not in ids:
        raise ConfigError("a non-empty desks list requires the global world desk")
    # Check all national ids, including desks declared later in the file.
    # Core references (briefing.brief etc.) remain legal in section rules.
    for entry in out:
        if entry["kind"] == "national":
            refs = set(re.findall(r"\bbriefing\.([a-z][a-z0-9_]*)\b", entry["prompt_section"]))
            foreign = refs - {entry["id"]} - _RESERVED_SECTION_IDS
            if foreign:
                raise ConfigError(f"prompt_section for {entry['id']!r} references another desk: briefing.{sorted(foreign)[0]}")
    return out


class Config:
    def __init__(self, raw, source_path):
        _need(raw, dict, "configuration")
        self.source_path = _path(source_path, "source_path")
        self.raw = copy.deepcopy(raw)
        self.warnings, self._recognized, self._ignored = [], [], []
        version = raw.get("version")
        if type(version) is not int or version not in SUPPORTED_VERSIONS:
            raise ConfigError(f"unsupported `version`: {version!r}; supported: {SUPPORTED_VERSIONS}")
        _walk(raw, _SCHEMA, "", self)
        from worldintel.core.coverage_policy import resolve_profile
        try:
            self.ranking = resolve_profile(raw.get("ranking", {}))
        except ValueError as exc:
            raise ConfigError(f"`ranking`: {exc}") from exc
        self.desks = _load_desks(raw, self)
        sources = raw.get("sources", {})
        filings, earnings = sources.get("filings", {}), sources.get("earnings", {})
        self.filings_enabled = filings.get("enabled", True)
        self.earnings_enabled = earnings.get("enabled", True)
        identity = raw.get("identity", {})
        self.contact_email = identity.get("contact_email", "").strip()
        self.user_agent_product = _text(identity.get("user_agent_product", "world-intel/1.0"), "identity.user_agent_product")
        if self.contact_email and not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", self.contact_email):
            raise ConfigError("`identity.contact_email` must be a contact email address")
        if self.filings_enabled or self.earnings_enabled:
            if self.contact_email.lower() in {"", "changeme", "you@example.com", "your.email@example.com"}:
                raise ConfigError("`identity.contact_email` must be your own address when SEC lanes are enabled")
        runtime = raw.get("runtime", {})
        for key in ("data_root", "timezone"):
            if key not in runtime:
                raise ConfigError(f"missing required setting `runtime.{key}`")
        self.data_root = _path(runtime["data_root"], "runtime.data_root")
        self.media_dir = _path(runtime.get("media_dir", os.path.join(self.data_root, "media")), "runtime.media_dir")
        self.env_file = _path(runtime.get("env_file", os.path.join(self.data_root, ".env")), "runtime.env_file")
        tz = _text(runtime["timezone"], "runtime.timezone")
        try:
            self.timezone = zoneinfo.ZoneInfo(tz)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(f"`runtime.timezone` is not a known IANA zone: {tz!r}; Windows needs tzdata") from exc
        self.feeds = []
        names = set()
        for n, feed in enumerate(sources.get("feeds", [])):
            key = f"sources.feeds[{n}]"
            _walk(feed, {"name": str, "desk": str, "tier": int, "url": str}, key, self)
            name = _text(feed.get("name", ""), f"{key}.name")
            desk = _text(feed.get("desk", ""), f"{key}.desk ({name})")
            url = _text(feed.get("url", ""), f"{key}.url ({name})")
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme in ("http", "https") and bool(parsed.hostname) and not any(c.isspace() for c in url)
                parsed.port
            except ValueError:
                valid = False
            if not valid:
                raise ConfigError(f"`{key}.url` ({name}) must be an http(s) URL with a host")
            if name in names:
                raise ConfigError(f"duplicate feed name {name!r} at `{key}`")
            names.add(name)
            tier = _integer(feed.get("tier", 2), f"{key}.tier", 1, 3)
            self.feeds.append(dict(name=name, desk=desk, tier=tier, url=url))
        self.market_spec = {k: _rows(v, f"sources.markets.{k}", 3) for k, v in sources.get("markets", {}).items() if k in _SCHEMA["sources"]["markets"]}
        self.fred_series = _rows(sources.get("macro", {}).get("fred_series", []), "sources.macro.fred_series", 2)
        self.gdelt_themes = _rows(sources.get("events", {}).get("gdelt_themes", []), "sources.events.gdelt_themes", 2)
        self.edgar_forms = [_text(v, f"sources.filings.edgar_forms[{n}]") for n, v in enumerate(filings.get("edgar_forms", []))]
        self.earnings_window_days = _integer(earnings.get("window_days", 10), "sources.earnings.window_days", 1, 90)
        self.earnings_watchlist = _rows(earnings.get("watchlist", []), "sources.earnings.watchlist", 2)
        self.tuning = copy.deepcopy(raw.get("tuning", {}))
        for key, bounds in {"freshness_hours": (1, 168), "per_desk_slots": (1, 100), "per_outlet_cap": (1, 50)}.items():
            if key in self.tuning:
                _integer(self.tuning[key], f"tuning.{key}", *bounds)
        for group in ("enrichment", "deadlines_s", "retention_days"):
            for key, value in self.tuning.get(group, {}).items():
                if key in _SCHEMA["tuning"][group]:
                    _integer(value, f"tuning.{group}.{key}", 1, 100000)
        enrichment = self.tuning.get("enrichment", {})
        if "min_chars" in enrichment and "max_chars" in enrichment and enrichment["min_chars"] > enrichment["max_chars"]:
            raise ConfigError("`tuning.enrichment.min_chars` must not exceed max_chars")
        if "region_tokens" in self.tuning:
            for n, token in enumerate(self.tuning["region_tokens"]):
                _text(token, f"tuning.region_tokens[{n}]")
        analysis = raw.get("analysis", {})
        if analysis.get("source_review_repair_enabled", False) and not analysis.get("source_review_enabled", False):
            raise ConfigError("`analysis.source_review_repair_enabled` requires source_review_enabled")
        if analysis.get("engine", "codex") != "codex":
            raise ConfigError("`analysis.engine` must be codex")
        if "reasoning_effort" in analysis:
            _text(analysis["reasoning_effort"], "analysis.reasoning_effort")
        if "smoke_test_timeout_s" in analysis:
            _integer(analysis["smoke_test_timeout_s"], "analysis.smoke_test_timeout_s", 1, 3600)
        for key in ("world_timeout_s", "finance_timeout_s", "pipeline_timeout_s", "finish_reserve_s", "rewrite_timeout_s"):
            if key in analysis:
                _integer(analysis[key], f"analysis.{key}", 1, 3600)
        if analysis.get("finish_reserve_s", 120) >= analysis.get("pipeline_timeout_s", 1500):
            raise ConfigError("`analysis.finish_reserve_s` must be less than analysis.pipeline_timeout_s")
        if raw.get("delivery", {}).get("adapter", "stdout") not in ("stdout", "file", "telegram", "hermes-cron"):
            raise ConfigError("`delivery.adapter` is unknown")

    def sec_user_agent(self):
        return f"{self.user_agent_product} {self.contact_email}".rstrip()

    @property
    def national_desks(self):
        return [desk for desk in self.desks if desk["kind"] == "national"]

    @property
    def desk_ids(self):
        return tuple(desk["id"] for desk in self.desks)

    @property
    def local_desk_ids(self):
        return tuple(desk["id"] for desk in self.national_desks)

    def browser_user_agent(self):
        contact = f"; +{self.contact_email}" if self.contact_email else ""
        return f"Mozilla/5.0 (compatible; {self.user_agent_product}{contact})"

    def plain_user_agent(self, note):
        return f"{self.user_agent_product} (+{_text(note, 'user agent note')})"


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if type(key) is not str:
            raise ConfigError(f"YAML mapping keys must be strings at {key_node.start_mark}")
        if key in result:
            raise ConfigError(f"duplicate YAML key `{key}` at {key_node.start_mark}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)
_CACHE = None


def load(path=None):
    """Read one YAML without caching, reading env files, or creating directories."""
    if path is not None:
        if not isinstance(path, (str, os.PathLike)):
            raise ConfigError("`config path` must be a string or path-like object")
        candidates = [_path(os.fspath(path), "config path")]
    elif _ENV_VAR in os.environ:
        candidates = [_path(os.environ[_ENV_VAR], _ENV_VAR)]
    else:
        candidates = [os.path.abspath("worldintel.yaml"), os.path.expanduser("~/.world-intel/worldintel.yaml")]
    chosen = next((p for p in candidates if os.path.isfile(p)), None)
    if chosen is None:
        raise ConfigError(f"no configuration file found; looked at {candidates}; set {_ENV_VAR}")
    try:
        with open(chosen, encoding="utf-8") as fh:
            raw = yaml.load(fh, Loader=_UniqueLoader)
        return Config(raw, chosen)
    except (OSError, UnicodeError, yaml.YAMLError, ConfigError) as exc:
        raise ConfigError(f"`{chosen}`: {exc}") from exc


def get():
    global _CACHE
    if _CACHE is None:
        _CACHE = load()
    return _CACHE


def set_for_test(cfg):
    global _CACHE
    if cfg is not None and not isinstance(cfg, Config):
        raise ConfigError("test configuration must be Config or None")
    _CACHE = cfg


def explain(cfg):
    """Stable categories; invalid configurations raise before a Config exists."""
    defaults = ("identity.contact_email", "identity.user_agent_product", "runtime.media_dir", "runtime.env_file",
                "sources.filings.enabled", "sources.earnings.enabled", "sources.filings.edgar_forms",
                "sources.earnings.window_days", "sources.earnings.watchlist", "sources.feeds", "sources.markets",
                "sources.macro.fred_series", "sources.events.gdelt_themes", "ranking", "tuning", "delivery.adapter")
    return {"recognized": list(cfg._recognized), "defaulted": [k for k in defaults if k not in cfg._recognized],
            "deprecated": [], "ignored": list(cfg._ignored), "invalid": []}
