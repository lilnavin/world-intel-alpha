"""world-intel Module D — orchestrator / pipeline entrypoint.

Run by the world-intel cron job. Ties the modules together:
  fetch_all -> update_scoreboard -> load_context -> codex analyze
  -> apply_deltas -> render_html -> persist (daily/log/state).

Writes a self-contained HTML briefing to the media dir and prints a single
`MEDIA:<path>` line to stdout; the cron no_agent runner turns that into a native
Telegram document attachment (attachment-only, no message body). The only other
thing ever printed is a short plain-text notice when Codex is unavailable. All
diagnostics go to stderr. Exit code is always 0 for a normal or gracefully-
degraded run so the cron never reports infra failure for a content gap.

Usage:
  python run.py            # normal daily run (dedups on last_run_date)
  python run.py --force    # ignore the once-per-day guard (for testing)
  python run.py --dry      # skip persisting state/daily (test the pipeline)
  python run.py --concurrent-passes
                           # legacy option: logged and ignored; analysis is
                           # sequential with no automatic retries
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import sys
import tempfile
import time
import unicodedata
import uuid
from urllib.parse import urlsplit
from collections import namedtuple

import requests  # noqa: E402
from bs4 import BeautifulSoup
from worldintel import config as wi_config  # noqa: E402
from worldintel import delivery  # noqa: E402
from worldintel import data  # noqa: E402

from worldintel.core import anomaly_panel  # noqa: E402
from worldintel.core import capsule  # noqa: E402
from worldintel.core import feeds  # noqa: E402
from worldintel.core import fetch_sources  # noqa: E402
from worldintel.core import earnings_report  # noqa: E402
from worldintel.core import filing_index  # noqa: E402
from worldintel.core import post_earnings_pool  # noqa: E402
from worldintel.core import reconcile  # noqa: E402
from worldintel.core import sec_filing_text  # noqa: E402
from worldintel.core import sec_fundamentals  # noqa: E402
from worldintel.core import enrich  # noqa: E402
from worldintel.core import select_news  # noqa: E402
from worldintel.core import kb  # noqa: E402
from worldintel.core import codex_engine  # noqa: E402
from worldintel.core import compose_html  # noqa: E402
from worldintel.core import metrics  # noqa: E402
from worldintel.core import quality_gate  # noqa: E402
from worldintel.core import grounding_review, model_evidence  # noqa: E402
from worldintel.core import source_diversity  # noqa: E402
from worldintel.core import coverage_policy  # noqa: E402
from worldintel.core import coverage_gate  # noqa: E402
from worldintel.core import schema_build  # noqa: E402

Paths = namedtuple("Paths", "kb_root media_dir env_file")


def paths() -> Paths:
    cfg = wi_config.get()
    return Paths(cfg.data_root, cfg.media_dir, cfg.env_file)


FINANCE_SCHEMA_PATH = "schema/finance.schema.json"


def capsule_store() -> str:
    """Where capsules live. A FUNCTION, not a constant, because Plan 1 Task 8
    replaces KB_ROOT with config.get().data_root -- a module-level constant here
    would be evaluated at import against a name that no longer exists."""
    return os.path.join(wi_config.get().data_root, "capsules")


CAPSULE_KEEP = 20          # 20 x ~4.87MB measured; see the plan's "Measured"


def _source_provenance() -> tuple:
    """(full commit, dirty) for the code that is ACTUALLY running.

    Resolved at import so no subprocess runs inside the timed window.

    Two homes, two answers. In the checkout, git knows. In the deployed copy at
    a standalone deployed copy there is no .git, so the
    deploy leaves `.provenance.json` beside these scripts and we read that.

    Dirtiness is scoped to the SKILL, not the repository: `git status
    --porcelain` with no pathspec counts untracked files anywhere, and the repo
    root carries scratch directories, so an unscoped check reports dirty on a
    fully committed tree, permanently, and no capsule could ever count.

    Unknown provenance reports dirty=True. A capsule wrongly marked clean is
    trusted as a replay baseline and silently poisons the equivalence diff; one
    wrongly marked dirty is merely re-collected.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    skill_root = os.path.dirname(here)  # checkout contains worldintel/ and .git
    try:
        # Never attribute an installed package to an unrelated ancestor repo.
        if not os.path.exists(os.path.join(skill_root, ".git")):
            raise OSError("installed package has no checkout provenance")
        import subprocess
        head = subprocess.run(["git", "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=5,
                              cwd=skill_root)
        if head.returncode == 0 and head.stdout.strip():
            status = subprocess.run(["git", "status", "--porcelain", "--", "."],
                                    capture_output=True, text=True, timeout=5,
                                    cwd=skill_root)
            return (head.stdout.strip(),
                    bool(status.stdout.strip()) or status.returncode != 0)
    except Exception:  # noqa: BLE001
        pass

    try:
        import hashlib
        from pathlib import Path, PurePosixPath
        import stat

        with open(os.path.join(here, ".provenance.json"),
                  encoding="utf-8") as fh:
            stamp = json.load(fh)
        commit = str(stamp.get("commit") or "")
        root = Path(skill_root).resolve()
        hashes = stamp.get("files")
        if (len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit) or
                type(stamp.get("dirty")) is not bool or type(stamp.get("scratch")) is not bool or
                not isinstance(hashes, dict) or not hashes or
                not {"worldintel/run.py", "world_intel_brief.py"} <= hashes.keys() or
                (root / ".deployment-incomplete").exists()):
            return commit, True
        def allowed(name):
            p = PurePosixPath(name)
            if (p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name or
                    name != p.as_posix()):
                return False
            if name == "world_intel_brief.py":
                return True
            if not p.parts or p.parts[0] != "worldintel" or any(part.startswith(".") for part in p.parts):
                return False
            return (p.suffix == ".py" and "__pycache__" not in p.parts) or (
                len(p.parts) > 2 and p.parts[1] == "data" and
                p.suffix in (".json", ".yaml", ".md", ".template"))
        for name, expected in hashes.items():
            if not isinstance(name, str) or not allowed(name) or not isinstance(expected, str):
                return commit, True
            path = root / name
            if (any(parent.is_symlink() for parent in [path, *path.parents] if parent != root) or
                    not stat.S_ISREG(path.stat().st_mode) or
                    hashlib.sha256(path.read_bytes()).hexdigest() != expected):
                return commit, True
        actual = set()
        package = root / "worldintel"
        for path in package.rglob("*"):
            if path.is_symlink():
                return commit, True
            name = path.relative_to(root).as_posix()
            if path.is_file() and allowed(name):
                actual.add(name)
        if (root / "world_intel_brief.py").is_file():
            actual.add("world_intel_brief.py")
        if actual != set(hashes):
            return commit, True
        return commit, bool(stamp.get("dirty", True)) or bool(stamp.get("scratch", False))
    except Exception:  # noqa: BLE001
        return "", True


SOURCE_COMMIT, SOURCE_DIRTY = _source_provenance()


def _coverage_repair(prompt, timeout, *, evidence_sink=None):
    with data.as_file("schema/coverage.schema.json") as schema:
        response = codex_engine.run_schema(
            prompt + '\nReturn an object {"cards": [...]} matching the schema.',
            str(schema), reasoning_effort="medium", timeout=timeout,
            **({"evidence_sink": evidence_sink, "pass_name": "coverage-repair"} if evidence_sink is not None else {}))
    return response.get("cards", []) if isinstance(response, dict) else []


def _selection_settings() -> dict:
    """Resolved defaults used by the selector (no call-site overrides)."""
    return select_news.selection_settings()


def _capsule_fingerprint() -> dict:
    """Behaviour-bearing settings that influence selection and rendering.

    Personal values are excluded by capsule.config_fingerprint. Plan 1 replaces
    the bodies here as these move into config -- see its Tasks 9 and 10.
    """
    return {**_selection_settings(),
            "feed_count": len(feeds.feed_registry()),
            "timezone": str(wi_config.get().timezone),
            "utc_offset": _utc_offset(local_now())}
REWRITE_SCHEMA_PATH = "schema/rewrite.schema.json"


def log(msg: str) -> None:
    print(f"[world-intel] {msg}", file=sys.stderr, flush=True)


_ASCII_MAP = {
    "‘": "'", "’": "'", "“": '"', "”": '"',  # smart quotes
    "–": "-", "—": "-", "−": "-",                  # dashes/minus
    "•": "-", "·": "|", "…": "...",                # bullet, middot, ellipsis
    "»": ">", "«": "<", "→": "->",                # guillemets, arrow
    "▲": "+", "▼": "-", " ": " ",                  # tri arrows, nbsp
}


def _asciify(s: str) -> str:
    """Flatten a string to pure ASCII for the cron stdout path. ASCII bytes decode
    identically under utf-8/cp1252/latin-1, so the daemon can never mangle them.
    Maps common punctuation, strips diacritics (é->e), and replaces the rest with '?'."""
    for k, v in _ASCII_MAP.items():
        s = s.replace(k, v)
    # Decompose then drop combining marks so accents fold (café -> cafe, naïve -> naive)
    # instead of leaving a stray '?' where the diacritic was.
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.encode("ascii", "replace").decode("ascii")


def load_env(path: str) -> dict:
    env = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def jst_today() -> str:
    """Compatibility name; the day follows the configured local timezone."""
    return local_now().date().isoformat()


def local_now() -> datetime.datetime:
    return datetime.datetime.now(wi_config.get().timezone)


def _utc_offset(instant: datetime.datetime) -> str:
    offset = instant.strftime("%z")
    return offset[:3] + ":" + offset[3:]


def _anomaly_deadline() -> float:
    """`deadline_at` for `anomaly_panel.build_panel`: that module's OWN
    monotonic clock plus its OWN 45s budget. The failure mode this guards --
    it retired with the ownership lane in 2026-08 -- is that a `time.time()`
    base reads as already-passed
    against `anomaly_panel._now()` and every reporter returns `partial`
    without a single request being made, while the run stays green."""
    return anomaly_panel._now() + wi_config.get().tuning.get("deadlines_s", {}).get(
        "anomaly", anomaly_panel.LANE_DEADLINE_S)


def _filing_text_deadline() -> float:
    """`deadline_at` for the `sec_filing_text` lane: that module's OWN clock
    plus its OWN 60s budget. 60 is not 45 by accident -- this lane fetches
    1.35 MB filing DOCUMENTS, not XBRL facts."""
    return sec_filing_text._now() + wi_config.get().tuning.get("deadlines_s", {}).get(
        "filing_text", sec_filing_text.LANE_DEADLINE_S)


def _writeup_tickers(result: dict) -> set:
    """Tickers that earned a beat-1 write-up card this run
    (briefing.post_earnings.writeups). Degrades to an empty set on any absent
    or malformed shape; never raises."""
    try:
        writeups = (((result or {}).get("briefing") or {})
                    .get("post_earnings") or {}).get("writeups")
        return {w["ticker"] for w in (writeups or [])
                if isinstance(w, dict) and w.get("ticker")}
    except Exception:
        return set()


def _also_reported_tickers(result: dict) -> set:
    """Tickers named in the Also-reported line (briefing.post_earnings.
    also_reported). One line is a thinner mention than a card, but it is still
    a name the brief put in front of the reader, so it earns the same claim on
    a later look at the company's filing. Never raises."""
    try:
        also = (((result or {}).get("briefing") or {})
                .get("post_earnings") or {}).get("also_reported")
        return {a["ticker"] for a in (also or [])
                if isinstance(a, dict) and a.get("ticker")}
    except Exception:
        return set()


def _merge_guidance_struct(candidates: list, result: dict) -> None:
    """Merge each write-up's `guidance_struct` onto its matching candidate
    (matched by ticker) so the pool snapshot ships the beat-1 guide instead of
    always None (spec Sec.6.2). A write-up with no matching candidate, or no
    `guidance_struct`, is skipped. Never raises."""
    try:
        writeups = (((result or {}).get("briefing") or {})
                    .get("post_earnings") or {}).get("writeups")
        by_ticker = {c.get("ticker"): c for c in (candidates or [])
                     if isinstance(c, dict) and c.get("ticker")}
        for w in (writeups or []):
            if not isinstance(w, dict):
                continue
            gs = w.get("guidance_struct")
            cand = by_ticker.get(w.get("ticker"))
            if cand is not None and gs is not None:
                cand["guidance_struct"] = gs
    except Exception as exc:  # noqa: BLE001
        log(f"guidance_struct merge failed (non-fatal): {exc}")


def _plain_feed_text(value) -> str:
    if not isinstance(value, str):
        return ""
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style", "iframe", "object"]):
        tag.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


def _degraded_world(bundle: dict) -> dict:
    """The World News half of the briefing, built from raw headlines when the
    world pass is down. Still renders a usable tab."""
    news = (bundle or {}).get("news") or []
    brief = []
    for n in news[:5]:
        if not isinstance(n, dict):
            continue
        title = _plain_feed_text(n.get("title"))
        if not title:
            continue
        url = n.get("url") if isinstance(n.get("url"), str) else ""
        try:
            parts = urlsplit(url)
            if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
                url = ""
        except ValueError:
            url = ""
        outlet = _plain_feed_text(n.get("source")) or "source"
        brief.append({
            "headline": title,
            "why_it_matters": "(analysis engine unavailable — raw headline)",
            "body": _plain_feed_text(n.get("summary")),
            "sources": [{"outlet": outlet, "url": url}] if url else [],
            "tags": [],
        })
    return {
        "brief": brief,
        "signal": {"top": {"story": "", "reason": "", "url": ""},
                   "buried": [], "noise_line": ""},
        "threads": [],
        "on_radar": [],
        # Empty, never guessed: country assignment belongs to analysis.
        **{desk["id"]: [] for desk in wi_config.get().national_desks},
    }


def _degraded_finance() -> dict:
    """The Finance half when the finance pass is down. Empty but well-formed,
    so the tab renders its empty state instead of vanishing -- the numbers in
    it render from the bundle regardless of whether the model ran."""
    return {
        "finance": {"brief": [], "scoreboard_note": "", "scoreboard_read": None,
                    "filings_note": "", "earnings_note": ""},
        "post_earnings": {"writeups": [], "also_reported": [], "followups": None},
    }


def _world_sections():
    return ("brief", "signal", "threads", "on_radar", *wi_config.get().local_desk_ids)


_FINANCE_SECTIONS = ("finance", "post_earnings")


def _merge_passes(passes: dict, bundle: dict) -> dict:
    """Fold the two Codex passes back into the single
    `{"briefing", "kb_deltas", "source_notes"}` result that quality_gate,
    source_diversity, kb.apply_deltas, metrics and compose_html.render_html
    all already consume. **This is where "one pass failing still ships the
    other tab" happens**: a missing half is replaced by its degraded shape,
    never by an absent key. Never raises."""
    passes = passes if isinstance(passes, dict) else {}
    world = passes.get("world")
    finance = passes.get("finance")
    errors = passes.get("errors") if isinstance(passes.get("errors"), dict) else {}
    if errors.get("world"):
        world = None
    if errors.get("finance"):
        finance = None

    def _half(result, sections, fallback):
        briefing = (result or {}).get("briefing") if isinstance(result, dict) else None
        if not isinstance(briefing, dict):
            return fallback, False
        out = dict(fallback)
        for key in sections:
            if briefing.get(key) is not None:
                out[key] = briefing[key]
        return out, True

    world_half, world_ok = _half(world, _world_sections(), _degraded_world(bundle))
    finance_half, finance_ok = _half(finance, _FINANCE_SECTIONS, _degraded_finance())

    # The world pass owns the overview and the expectations ledger (its schema
    # is the only one carrying them); entity and thread upserts concatenate,
    # which is what apply_deltas already expects.
    deltas = dict((world or {}).get("kb_deltas") or {}) if isinstance(world, dict) else {}
    fin_deltas = (finance or {}).get("kb_deltas") or {} if isinstance(finance, dict) else {}
    for key in ("entities_upsert", "threads_upsert"):
        merged = list(deltas.get(key) or []) + list(fin_deltas.get(key) or [])
        if merged:
            deltas[key] = merged

    notes = []
    for result in (world, finance):
        if isinstance(result, dict):
            notes += [n for n in (result.get("source_notes") or []) if n]
    if not world_ok and not finance_ok:
        # The exact phrase the writing gate below keys on. Reserved for a
        # DOUBLE failure: with one pass alive there are real bodies to scan.
        notes.append("analysis engine unavailable: "
                     f"world={errors.get('world')}; finance={errors.get('finance')}")
    else:
        if not world_ok:
            notes.append(f"world pass unavailable: {errors.get('world')}")
        if not finance_ok:
            notes.append(f"finance pass unavailable: {errors.get('finance')}")

    records = {}
    previous = (passes.get("analysis_status") or {}).get("passes") or {}
    for name, success in (("world", world_ok), ("finance", finance_ok)):
        record = dict(previous.get(name) or {})
        record.update(success=success, error=codex_engine.diagnostic(errors.get(name)) if errors.get(name) else None)
        record.setdefault("duration_s", 0)
        record.setdefault("timeout_s", 0)
        record.setdefault("timed_out", bool(record["error"] and "timed out" in record["error"]))
        record.setdefault("skipped", False)
        records[name] = record
    count = int(world_ok) + int(finance_ok)
    status = {"overall": "complete" if count == 2 else "partial" if count else "failed",
              "mode": passes.get("mode", "sequential"), "passes": records}
    return {"briefing": {**world_half, **finance_half},
            "kb_deltas": deltas,
            "source_notes": notes,
            "analysis_status": status}


def _persist_status(root, receipt, sidecar=None):
    """Replace receipts atomically; artifact identity prevents stale handoff claims."""
    os.makedirs(root, exist_ok=True)
    for path in (os.path.join(root, "run-status.json"), sidecar):
        if not path:
            continue
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(receipt, fh, ensure_ascii=False, indent=2)
        os.replace(temporary, path)


def main() -> int:
    try:
        cfg = wi_config.get()
    except wi_config.ConfigError as exc:
        log(f"configuration error: {exc}")
        print(_asciify(f"World Intelligence paused: configuration error: {exc}"))
        return 0
    for warning in cfg.warnings:
        log(f"configuration warning: {warning}")
    if "--dry" in sys.argv or cfg.raw.get("analysis", {}).get("source_review_enabled", False):
        # Stage mutating ledgers until source review accepts; dry runs discard
        # all intermediate writes, including fetch caches.
        with tempfile.TemporaryDirectory(prefix="world-intel-dry-") as scratch:
            return _run_pipeline(cfg, scratch_root=scratch)
    return _run_pipeline(cfg)


def _run_pipeline(cfg, scratch_root: str | None = None) -> int:
    KB_ROOT, MEDIA_DIR, ENV_PATH = cfg.data_root, cfg.media_dir, cfg.env_file
    # The Hermes cron no_agent runner captures stdout with subprocess text=True, whose
    # decode encoding (cp1252 vs utf-8) is unpredictable and has mismatched what we emit —
    # yielding empty (decode crash) or mojibake ("Â»", "â€¢") deliveries. We sidestep it
    # entirely by writing the briefing as pure ASCII (see _asciify + the final print):
    # ASCII bytes are identical under utf-8/cp1252/latin-1, so no decode mismatch is
    # possible. The knowledge base keeps full Unicode; only stdout is flattened.

    force = "--force" in sys.argv
    dry = "--dry" in sys.argv
    concurrent_passes = ("--concurrent-passes" in sys.argv
                         or cfg.raw.get("analysis", {}).get("concurrent_passes", False))
    _run_start = time.time()
    analysis_cfg = cfg.raw.get("analysis", {})
    review_enabled = analysis_cfg.get("source_review_enabled", False)
    _pipeline_start = time.monotonic()
    _analysis_deadline = (_pipeline_start + analysis_cfg.get("pipeline_timeout_s", 1500)
                          - analysis_cfg.get("finish_reserve_s", 120))
    receipt = {"schema_version": 1, "run_id": uuid.uuid4().hex,
               "started_at": local_now().isoformat(), "date": None,
               "artifact": None, "analysis": None,
               "rewrite": {"attempted": False, "skipped": False, "timeout_s": 0, "error": None},
               "generation": "pending", "handoff": "pending", "delivery_confirmed": False}

    evidence = model_evidence.Collector(KB_ROOT, receipt["run_id"], media_dir=MEDIA_DIR)
    receipt["source_review"] = {"status": "pending" if review_enabled else "disabled",
                                "scope": grounding_review.SCOPE}
    _capsule = None
    try:
        _capsule = capsule.Pending(
            started_at=local_now().isoformat(),
            mode="forced" if force else "scheduled",
            source_commit=SOURCE_COMMIT,          # resolved at import
            source_dirty=SOURCE_DIRTY,            # resolved at import
            config=_capsule_fingerprint())
    except Exception as exc:  # noqa: BLE001
        log(f"capsule init failed (non-fatal): {exc}")

    env = {**os.environ, **load_env(ENV_PATH)}
    config = {
        "newsdata_key": env.get("NEWSDATA_API_KEY"),
        "guardian_key": env.get("GUARDIAN_API_KEY"),
        "marketaux_key": env.get("MARKETAUX_API_KEY"),
        "twelvedata_key": env.get("TWELVEDATA_API_KEY"),
        "fred_key": env.get("FRED_API_KEY"),
        "finnhub_key": env.get("FINNHUB_API_KEY"),
    }
    today = jst_today()
    receipt["date"] = today

    if not dry and not review_enabled:
        kb.ensure_kb(KB_ROOT)
    state = kb.read_state(KB_ROOT)
    if state.get("last_run_date") == today and not force:
        # Empty stdout => the no_agent cron treats the run as silent (no delivery).
        log("already ran today; nothing to deliver (silent)")
        return 0

    if not dry:
        _persist_status(KB_ROOT, receipt)

    # Health gate: if Codex is unavailable, surface a clear re-auth note, don't fail.
    log("codex smoke test...")
    if not codex_engine.smoke_test(
            timeout=cfg.raw.get("analysis", {}).get("smoke_test_timeout_s", 60)):
        msg = (
            "⚠️ World Intelligence paused — Codex CLI/auth unavailable.\n"
            "Fix: run `codex login` in a terminal (subscription auth). "
            "The briefing resumes on the next run."
        )
        log("codex smoke test FAILED")
        receipt["generation"] = "failed"
        if not dry:
            _persist_status(KB_ROOT, receipt)
        print(_asciify(msg))
        return 0

    raw_dir = os.path.join((scratch_root if dry else None) or KB_ROOT, "raw", today)
    os.makedirs(raw_dir, exist_ok=True)

    log("fetching sources...")
    _t = time.time()
    bundle = fetch_sources.fetch_all(config, raw_dir, today)
    bundle["ranking_profile"] = cfg.ranking
    # Selection's freshness window must be relative to when this run actually
    # executes. Anchoring it to the end of `today` reduced a 36-hour promise to
    # ~19 hours at the 07:00 cron and discarded the prior Japanese business day.
    bundle["as_of"] = local_now().isoformat()
    _t_fetch = round(time.time() - _t, 1)

    # Deterministic selection: dedupe the ~1,200-item feed pool, drop stale,
    # noise and JP/CA-local items, score, and take the top N per desk.
    log("selecting desk candidates...")
    _t = time.time()
    try:
        seen = kb.load_seen(KB_ROOT, today)
    except Exception as exc:  # noqa: BLE001
        log(f"load_seen failed (non-fatal): {exc}")
        seen = set()
    if _capsule is not None:
        try:
            _capsule.add("selection-input", capsule.snapshot({
                "bundle": bundle,
                "seen": sorted(seen),
                "as_of": bundle.get("as_of"),
                "today": today,
                # Spec 8.2: the RESOLVED settings selection actually used, not
                # just its data. Plan 2 makes these configurable, and replay
                # must then apply the capsule's values rather than whatever the
                # replaying build defaults to -- otherwise the L1 diff silently
                # compares two different configurations.
                "selection_settings": _selection_settings(),
            }))
        except Exception as exc:  # noqa: BLE001
            _capsule.fail("L1", exc)

    try:
        desks = select_news.select_candidates(bundle, seen)
    except Exception as exc:  # noqa: BLE001
        log(f"select failed (non-fatal): {exc}")
        desks = {d: [] for d in select_news.desk_ids()}
    log("desk candidates: " + ", ".join(
        f"{d} {len(desks.get(d) or [])}" for d in select_news.desk_ids()))

    # Phase 1 keeps the single Codex pass and the single-page layout: flatten the
    # desk map back into bundle['news'] in desk order. Each item keeps its `desk`
    # field, which the current prompt simply ignores. Phase 2 consumes the map.
    bundle["news_pool_size_raw"] = len(bundle.get("news") or [])
    enrichment_pool = list(bundle.get("news") or [])
    bundle["news"] = [item for d in select_news.desk_ids()
                      for item in (desks.get(d) or [])]
    _t_select = round(time.time() - _t, 1)

    log("enriching selected stories with full text...")
    _t = time.time()
    try:
        enrich.enrich_news(bundle, candidates=desks)
        enrich.enrich_protected_alternates(
            enrichment_pool, desks, bundle.get("protected_events") or [],
            deadline=_analysis_deadline)
        bundle["news"] = [item for d in select_news.desk_ids() for item in (desks.get(d) or [])]
        selected_urls = {item.get("url") for item in bundle["news"]}
        for row in bundle.get("selection_audit") or []:
            if row.get("url") in selected_urls:
                row["outcome"] = "selected"
            elif row.get("outcome") == "selected":
                row["outcome"] = "replaced_after_enrichment"
        enriched = sum(bool(item.get("content")) for item in bundle["news"])
        parts = [f"{desk} {sum(bool(i.get('content')) for i in items)}/{len(items)}"
                 for desk, items in sorted(desks.items())]
        bundle.setdefault("source_health", {})["enrich"] = {
            "ok": enriched >= 1, "note": f"{enriched}/{len(bundle['news'])} enriched ({', '.join(parts)})"}
    except Exception as exc:  # noqa: BLE001
        log(f"enrich failed (non-fatal): {exc}")
    _t_enrich = round(time.time() - _t, 1)

    # Post-earnings scan: who reported since the last brief, pre-filtered to the
    # candidates worth Codex's attention (see earnings_report module). Never fatal.
    log("post-earnings scan...")
    _t = time.time()
    if cfg.earnings_enabled:
        try:
            earnings_root = KB_ROOT
            if dry or review_enabled:
                # The scan updates pending/expired entries before publication.
                earnings_root = scratch_root
                earnings_report.write_pipeline(
                    earnings_root, earnings_report.read_pipeline(KB_ROOT))
            bundle["post_earnings_candidates"] = earnings_report.build_post_earnings(
                config, raw_dir, bundle.setdefault("source_health", {}), today, earnings_root)
        except Exception as exc:  # noqa: BLE001
            log(f"post-earnings scan failed (non-fatal): {exc}")
            bundle["post_earnings_candidates"] = []
        log(f"post-earnings candidates: {len(bundle['post_earnings_candidates'])}")

        # The forward calendar comes from yfinance, whose next-earnings date lags a
        # report by a day; the Finnhub calendar fetched just above is authoritative,
        # so drop any name it already shows as reported before the brief announces
        # it as upcoming. Non-fatal and non-destructive.
        try:
            _cal = bundle.get("earnings") or []
            bundle["earnings"] = earnings_report.correct_forward_calendar(
                _cal, raw_dir, today)
            _dropped = len(_cal) - len(bundle["earnings"])
            if _dropped:
                log(f"forward calendar: dropped {_dropped} already-reported name(s)")
        except Exception as exc:  # noqa: BLE001
            log(f"forward-calendar correction failed (non-fatal): {exc}")
    _t_earnings = round(time.time() - _t, 1)

    # Anomaly panel: deterministic analytical priors (working capital, margin,
    # tax, accrual, funding) for each reporter whose fundamentals resolved a
    # period. Runs after post-earnings for the reason the retired ownership
    # lane did — a slow scan never delays higher-value work. One
    # companyfacts call per company; own clock, own 45s budget. Never fatal.
    #
    # Not --dry guarded: this decorates the bundle the brief renders from and
    # persists nothing itself. Only record_company_quarter, downstream, is.
    log("anomaly panel...")
    _t = time.time()
    _panels = 0
    if cfg.earnings_enabled:
        try:
            panel_session = requests.Session()
            panel_deadline_at = _anomaly_deadline()
            panel_ciks = earnings_report._load_cik_map(panel_session)
            for cand in bundle.get("post_earnings_candidates") or []:
                fundamentals = cand.get("fundamentals")
                ticker = (cand.get("ticker") or "").upper()
                if not ticker or not isinstance(fundamentals, dict):
                    continue
                period_end = fundamentals.get("period_end")
                cik = panel_ciks.get(ticker)
                if not period_end or not cik:
                    continue
                # `py_end` is the YEAR-AGO end. anomaly_panel names this argument
                # `prior_end`, but sec_fundamentals uses that name for the PREVIOUS
                # QUARTER — passing fundamentals["prior_end"] here would run clean
                # and empty every YoY metric, since the match is on an exact date.
                cand["panel"] = anomaly_panel.build_panel(
                    cik, ticker, period_end, fundamentals.get("py_end"),
                    panel_session, bundle.setdefault("source_health", {}),
                    panel_deadline_at,
                    reaction={"surprise_pct": cand.get("surprise_pct"),
                              "price_move_pct": cand.get("price_move_pct")})
                _panels += 1
        except Exception as exc:  # noqa: BLE001
            log(f"anomaly panel failed (non-fatal): {exc}")
    _t_anomaly = round(time.time() - _t, 1)
    log(f"anomaly panel: {_panels} reporter(s)")

    # ------------------------------------------------------------------ #
    # Beat 2: filing reconciliation.
    #
    # This used to poll forward -- open a watch on a covered company, then ask
    # SEC every morning whether it had filed yet. That published about one
    # company a day while 191 filings by companies we had covered went unread in
    # a single month, because only write-up cards were ever watched, because 12
    # of 22 open watches were ADRs being asked daily for a 10-Q they can never
    # file, and because a watch not serviced inside its ten days lapsed and was
    # pruned with no record that anything had been missed.
    #
    # It now observes instead: read the index of every 10-Q/10-K actually filed
    # and intersect it with everyone the brief has published. An ADR never
    # appears in that index, so the wasted requests stop existing rather than
    # needing a guard; and a match that misses today is still inside the window
    # tomorrow, so a miss means LATER rather than NEVER.
    # ------------------------------------------------------------------ #
    log("filing reconciliation...")
    _t = time.time()
    _matched = _promoted = _deferred = 0
    _elapsed_at_guard = None
    bundle["post_earnings_followups"] = []
    bundle["filing_lines"] = []
    if cfg.filings_enabled:
        try:
            rec_session = requests.Session()
            rec_health = bundle.setdefault("source_health", {})
            index_root = KB_ROOT
            if dry or review_enabled:
                index_root = scratch_root
                filing_index.write_index(index_root, filing_index.read_index(KB_ROOT))
            index = filing_index.load_window(index_root, today, rec_session, rec_health)

            # The pool stores whatever CIK the candidate carried, and candidates
            # carry none -- earnings_report resolves them on demand. Fill them here,
            # from the map this run already loads, rather than making the pool do
            # I/O or the reconciler guess.
            pool = post_earnings_pool.read_pool(KB_ROOT)
            rec_ciks = earnings_report._load_cik_map(rec_session)
            for entry in pool.values():
                if isinstance(entry, dict) and not entry.get("cik"):
                    entry["cik"] = rec_ciks.get((entry.get("ticker") or "").upper(), "")

            # The size term lives in reconcile but its universe cannot: that module
            # is stdlib-only and I/O-free, and earnings_report pulls requests at
            # import time. One copy of the list, injected from the caller that
            # already holds it.
            matches = reconcile.rank(
                reconcile.match(pool, index, today,
                                earnings_report.LARGECAP_INDEX_SET), today)
            _matched = len(matches)
            # What the 20-day window aged out before anything read it. Nothing else
            # reports this: an expired match leaves no error and no trace, so a
            # model of the backlog built on `matched` alone reads a leak as
            # successful coverage.
            bundle["filings_expired_unread"] = len(
                reconcile.expired_unread(pool, index))

            # The screen. `fetch_filing_text` needs the primary document's name and
            # the daily index carries only the full-submission path, so resolving
            # the reference means build_fundamentals -- which is also what says
            # whether the filing anchored a period at all. Bounded to SCREEN_MAX so
            # a heavy filing day cannot turn into an unbounded SEC crawl.
            screen_deadline = _filing_text_deadline()
            screened = []
            for m in matches[:reconcile.SCREEN_MAX]:
                fundamentals = sec_fundamentals.build_fundamentals(
                    m["cik"], m.get("report_date") or m.get("filed"),
                    rec_session, rec_health, screen_deadline)
                if not earnings_report._has_resolved_metric(fundamentals):
                    continue
                m["fundamentals"] = fundamentals
                text = sec_filing_text.fetch_filing_text(
                    m["cik"], fundamentals.get("accession"),
                    fundamentals.get("primary_doc"), rec_session, rec_health,
                    screen_deadline)
                if not text:
                    continue
                m["filing_sections"] = sec_filing_text.slice_sections(
                    text, fundamentals.get("form"))
                m["segments"] = sec_filing_text.parse_segments(text)
                m["disclosures"] = sec_filing_text.find_disclosures(text)
                screened.append(m)

            # Depth is decided by what the filings turned out to contain, not by
            # which tier the company came from. The elapsed guard trades depth for
            # the brief's life: overrunning the 600s cron ceiling is a hard kill
            # that loses every tab, and carry-over returns anything skipped
            # tomorrow, so deferring is always cheaper than dying.
            elapsed = time.time() - _run_start
            _elapsed_at_guard = round(elapsed, 1)
            # Stable, greppable, emitted on EVERY run including --dry, which writes
            # no metrics row (append_metrics is behind `if not dry`).
            log(f"GUARD-ELAPSED {_elapsed_at_guard}")
            promoted, _ = reconcile.select(screened, elapsed_s=elapsed)
            if elapsed > reconcile.PROMOTION_ELAPSED_GUARD_S:
                log(f"promotion guard: {round(elapsed)}s elapsed, "
                    f"promoting at most {reconcile.PROMOTION_FLOOR}")

            promoted_keys = {m["key"] for m in promoted}
            # A backfilled pool entry has no display name -- the HTML archive it was
            # seeded from does not carry one -- so the block head would read "AAP"
            # rather than "Advance Auto Parts" for the whole first window. The daily
            # index names every filer, so use that when the pool cannot.
            for m in promoted:
                if not m.get("name"):
                    m["name"] = m.get("company") or ""
            bundle["post_earnings_followups"] = promoted
            bundle["filing_lines"] = [
                {"key": m["key"], "ticker": m.get("ticker"),
                 "name": m.get("name") or m.get("company"),
                 "form": m.get("form"), "filed": m.get("filed")}
                for m in matches if m["key"] not in promoted_keys
            ]
            _promoted, _deferred = len(promoted), len(bundle["filing_lines"])

            # The panel runs on both beats (spec Sec.3), but only for the promoted:
            # its segment and off-balance-sheet families are filing-derived, and it
            # feeds the same period_end back into record_company_quarter below,
            # which is what converges beat 1's `partial` row to the complete one.
            panel_deadline = _anomaly_deadline()
            for m in promoted:
                fundamentals = m.get("fundamentals") or {}
                period_end = fundamentals.get("period_end")
                if not period_end:
                    continue
                m["panel"] = anomaly_panel.build_panel(
                    m["cik"], m.get("ticker"), period_end, fundamentals.get("py_end"),
                    rec_session, rec_health, panel_deadline,
                    reaction={"surprise_pct": m.get("surprise_pct"),
                              "price_move_pct": m.get("price_move_pct")})
        except Exception as exc:  # noqa: BLE001
            log(f"filing reconciliation failed (non-fatal): {exc}")
    _t_filing_text = round(time.time() - _t, 1)
    log(f"filing reconciliation: {_matched} matched, {_promoted} promoted, "
        f"{_deferred} listed, "
        f"{bundle.get('filings_expired_unread', 0)} expired unread")

    health = bundle.get("source_health") or {}
    ok_sources = [s for s, v in health.items() if isinstance(v, dict) and v.get("ok")]
    log(f"sources ok: {sorted(ok_sources) or 'none'}")
    _en = health.get("enrich") or {}
    log(f"enrich: {_en.get('note') or _en or 'n/a'}")

    if not dry and not review_enabled:
        try:
            kb.update_scoreboard(KB_ROOT, bundle.get("markets") or {})
        except Exception as exc:  # noqa: BLE001
            log(f"update_scoreboard failed: {exc}")

    # Machine-readable market time series, additive to the scoreboard markdown
    # above: builds the history a future analytical layer reads. Persistence,
    # so it is skipped on --dry.
    if not dry and not review_enabled:
        try:
            kb.append_scoreboard_history(KB_ROOT, bundle.get("markets") or {}, today)
        except Exception as exc:  # noqa: BLE001
            log(f"append_scoreboard_history failed (non-fatal): {exc}")

    context = kb.load_context(KB_ROOT, news=bundle.get("news"))
    log(f"entity dossiers in context: {len(context.get('entities') or [])}")

    # Two Codex passes: World News and Finance, each with its own prompt and
    # its own schema (spec Sec.6.5/8.2). Sequential by default -- the finance
    # pass's `scoreboard_read` cites the world pass's selected headlines under
    # the single-mention rule, and only sequencing can supply them.
    # Legacy concurrency requests are logged and ignored. No retries: one
    # pass failing still ships the other tab within the remaining model budget.
    log("running Codex analysis (two passes: world, finance)...")
    _t = time.time()
    world_schema = schema_build.write_temp(cfg)
    try:
        with data.as_file(FINANCE_SCHEMA_PATH) as finance_schema:
            passes = codex_engine.run_two_pass(
                bundle, context, world_schema, str(finance_schema),
                concurrent=concurrent_passes, log=log, evidence_sink=evidence,
                world_timeout=analysis_cfg.get("world_timeout_s", 600),
                finance_timeout=analysis_cfg.get("finance_timeout_s", 360),
                deadline=_analysis_deadline,
                reasoning_effort=cfg.raw.get("analysis", {}).get("reasoning_effort", "high"))
    finally:
        os.unlink(world_schema)
    for _pass in ("world", "finance"):
        _err = (passes.get("errors") or {}).get(_pass)
        log(f"{_pass} pass: " + (f"FAILED, degrading: {_err}" if _err else "ok"))
    result = _merge_passes(passes, bundle)
    receipt["analysis"] = result["analysis_status"]
    _t_codex = round(time.time() - _t, 1)

    # Writing gate: if any Brief body narrates its inputs instead of reporting
    # news, ask Codex once to rewrite just those bodies. Fail-open by design.
    if "source_notes" not in result or "analysis engine unavailable" not in str(
            result.get("source_notes")):
        try:
            flagged = quality_gate.scan(result)
            rewrite_timeout = min(analysis_cfg.get("rewrite_timeout_s", 180),
                                  max(0, _analysis_deadline - time.monotonic()))
            receipt["rewrite"]["timeout_s"] = rewrite_timeout
            if flagged and rewrite_timeout <= 0:
                receipt["rewrite"].update(skipped=True, error="analysis budget exhausted")
                log("writing gate: skipped; analysis budget exhausted")
            elif flagged:
                log(f"writing gate: {len(flagged)} meta-language body(ies), rewriting...")
                def rewrite_model(prompt, schema):
                    bounded = min(analysis_cfg.get("rewrite_timeout_s", 180),
                                  max(0, _analysis_deadline - time.monotonic()))
                    receipt["rewrite"]["timeout_s"] = bounded
                    if bounded <= 0:
                        receipt["rewrite"].update(skipped=True, error="analysis budget exhausted")
                        raise codex_engine.CodexError("analysis budget exhausted")
                    receipt["rewrite"]["attempted"] = True
                    try:
                        return codex_engine.run_schema(prompt, schema, reasoning_effort="medium", timeout=bounded,
                                                       evidence_sink=evidence, pass_name="writing-rewrite")
                    except Exception as exc:
                        receipt["rewrite"]["error"] = codex_engine.diagnostic(exc)
                        raise
                with data.as_file(REWRITE_SCHEMA_PATH) as rewrite_schema:
                    patched = quality_gate.rewrite(
                        result, flagged,
                        rewrite_model,
                        str(rewrite_schema))
                log(f"writing gate: rewrote {patched}/{len(flagged)}")
        except Exception as exc:  # noqa: BLE001
            receipt["rewrite"]["error"] = codex_engine.diagnostic(exc)
            log(f"writing gate failed (non-fatal): {receipt['rewrite']['error']}")

    try:
        country_audit = quality_gate.enforce_country_grounding(result, bundle)
        removed = sum(country_audit.values())
        if removed:
            log(f"country grounding gate: removed {removed} invalid card(s)")
    except Exception as exc:  # noqa: BLE001
        log(f"country grounding gate failed (non-fatal): {exc}")

    try:
        guard = source_diversity.enforce_guardian_soft_cap(bundle, result)
        if guard.get("guardian_cap_overridden"):
            log("guardian cap overridden: "
                f"{guard.get('guardian_cap_override_reason')}")
        elif guard.get("guardian_cap_removed"):
            log(f"guardian cap: removed {guard.get('guardian_cap_removed')} "
                "excess Guardian pick(s)")
    except Exception as exc:  # noqa: BLE001
        log(f"guardian cap failed (non-fatal): {exc}")

    coverage_audit = coverage_gate.enforce(
        result, bundle, bundle.get("protected_events", []),
        repair_fn=lambda prompt, timeout: _coverage_repair(prompt, timeout, evidence_sink=evidence),
        deadline=_analysis_deadline)
    result["coverage_audit"] = coverage_audit
    receipt["coverage_audit"] = coverage_audit
    receipt["quality"] = result["quality_status"]
    receipt["ranking_profile_hash"] = bundle.get("ranking_profile_hash")
    log("major-event coverage: " + ", ".join(
        e["status"] for e in coverage_audit["events"]))

    if review_enabled:
        def source_review_model(prompt, schema, timeout):
            return codex_engine.run_schema(prompt, schema, reasoning_effort="medium", timeout=timeout,
                                           evidence_sink=evidence, pass_name="source-review")
        receipt["source_review"] = grounding_review.review(
            result, bundle, review_fn=source_review_model, deadline=_analysis_deadline)
    receipt["model_evidence"] = evidence.summary()
    result["source_review"] = receipt["source_review"]
    if receipt["source_review"]["status"] == "hold":
        receipt.update(generation="held", handoff="held")
        if dry:
            summary = {"schema_version": 1, "dry": True,
                       "source_commit": SOURCE_COMMIT, "source_dirty": SOURCE_DIRTY,
                       "source_file": os.path.realpath(__file__),
                       "generation": "held", "handoff": "disabled", "delivery_confirmed": False,
                       "analysis": receipt["analysis"], "quality": receipt["quality"],
                       "source_review": receipt["source_review"], "model_evidence": receipt["model_evidence"]}
            print("WORLD_INTEL_DRY_SUMMARY:" + json.dumps(summary), file=sys.stderr, flush=True)
            return 1
        _persist_status(KB_ROOT, receipt)
        return 0

    if review_enabled and not dry:
        kb.ensure_kb(KB_ROOT)
        if cfg.earnings_enabled and os.path.isfile(os.path.join(scratch_root, earnings_report.PIPELINE_FILE)):
            earnings_report.write_pipeline(KB_ROOT, earnings_report.read_pipeline(scratch_root))
        if cfg.filings_enabled and os.path.isfile(os.path.join(scratch_root, filing_index.INDEX_FILE)):
            filing_index.write_index(KB_ROOT, filing_index.read_index(scratch_root))
    if review_enabled and not dry:
        try:
            kb.update_scoreboard(KB_ROOT, bundle.get("markets") or {})
        except Exception as exc:  # noqa: BLE001
            log(f"update_scoreboard failed: {exc}")

    # Machine-readable market time series, additive to the scoreboard markdown
    # above: builds the history a future analytical layer reads. Persistence,
    # so it is skipped on --dry.
    if review_enabled and not dry:
        try:
            kb.append_scoreboard_history(KB_ROOT, bundle.get("markets") or {}, today)
        except Exception as exc:  # noqa: BLE001
            log(f"append_scoreboard_history failed (non-fatal): {exc}")


    deltas = result.get("kb_deltas") or {}
    if deltas and not dry:
        try:
            changed = kb.apply_deltas(KB_ROOT, deltas)
            log(f"kb updated: {len(changed)} file(s)")
        except Exception as exc:  # noqa: BLE001
            log(f"apply_deltas failed: {exc}")

    if _capsule is not None:
        try:
            _capsule.add("render-input", capsule.snapshot({
                "analysis": result,
                "bundle": bundle,
                "date": today,
                # Spec 8.4: a recorded instant is not enough -- converting it
                # through a newer timezone database can still yield a different
                # local result. The RESOLVED date and offset travel along.
                "clock": {"local_date": today,
                          "utc_offset": _utc_offset(local_now()),
                          "generated_at": local_now().isoformat()},
            }))
        except Exception as exc:  # noqa: BLE001
            _capsule.fail("L2", exc)

    html = compose_html.render_html(
        result, bundle, today, desks=cfg.desks,
        timezone_label=local_now().tzname() or str(cfg.timezone))

    html_path = os.path.join(MEDIA_DIR, f"brief-{today}.html")
    if not dry:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        try:
            with open(html_path, "w", encoding="utf-8") as fh:
                fh.write(html)
        except OSError as exc:
            log(f"failed to write briefing html: {exc}")
            receipt["generation"] = "failed"
            _persist_status(KB_ROOT, receipt)
            return 0
        receipt["generation"] = "generated"
        with open(html_path, "rb") as artifact:
            receipt["artifact"] = {"path": html_path, "sha256": hashlib.sha256(artifact.read()).hexdigest()}
        _persist_status(KB_ROOT, receipt, html_path.removesuffix(".html") + ".status.json")
        log(f"briefing written: {html_path} ({len(html)} chars)")
    else:
        log(f"dry run: briefing rendered ({len(html)} chars); no files published")
        rendered = BeautifulSoup(html, "html.parser")
        for tag in rendered(["script", "style"]):
            tag.decompose()
        summary = {
            "schema_version": 1, "dry": True,
            "source_commit": SOURCE_COMMIT, "source_dirty": SOURCE_DIRTY,
            "source_file": os.path.realpath(__file__),
            "generation": "rendered", "handoff": "disabled", "delivery_confirmed": False,
            "analysis": receipt["analysis"],
            "quality": receipt["quality"],
            "source_review": receipt["source_review"], "model_evidence": receipt["model_evidence"],
            "rewrite": {**receipt["rewrite"], "remaining": len(quality_gate.scan(result))},
            "rendered": {"chars": len(html),
                         "text_chars": len(rendered.get_text(" ", strip=True)),
                         "sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
                         "world_cards": len(result.get("briefing", {}).get("brief") or []),
                         "input_news": len(bundle.get("news") or [])},
        }
        print("WORLD_INTEL_DRY_SUMMARY:" + json.dumps(summary), file=sys.stderr, flush=True)

    # Record which reporters this brief had the chance to cover so the same
    # (ticker, report_date) never resurfaces inside the lookback window.
    # guidance_struct is merged onto the candidate first so beat 2 can restate
    # the guide without re-fetching the 8-K (spec Sec.6.2).
    if cfg.earnings_enabled:
        if not dry:
            try:
                candidates = bundle.get("post_earnings_candidates") or []
                _merge_guidance_struct(candidates, result)
                earnings_report.mark_covered(KB_ROOT, candidates, today)
            except Exception as exc:  # noqa: BLE001
                log(f"mark_covered failed (non-fatal): {exc}")

    # Everything this brief actually PUBLISHED about a reporter goes into the
    # reconciliation pool, so a later run can notice when that company's 10-Q or
    # 10-K lands. Both tiers count: a write-up card and an also-reported line are
    # equally a promise that we were watching the name. Recorded after the HTML
    # is written and never on --dry, because the pool records what was
    # published, not what was considered.
    if cfg.earnings_enabled:
        if not dry:
            try:
                post_earnings_pool.record(
                    KB_ROOT, bundle.get("post_earnings_candidates") or [],
                    _writeup_tickers(result), _also_reported_tickers(result), today)
            except Exception as exc:  # noqa: BLE001
                log(f"pool record failed (non-fatal): {exc}")

    # Whatever the reconciler surfaced today is marked so it cannot repeat: a
    # line records that the filing has been acknowledged, a block retires it
    # from promotion for good.
    if cfg.filings_enabled:
        if not dry:
            try:
                post_earnings_pool.mark_shown(
                    KB_ROOT,
                    [f.get("key") for f in bundle.get("filing_lines") or []],
                    [f.get("key") for f in bundle.get("post_earnings_followups") or []],
                    today)
            except Exception as exc:  # noqa: BLE001
                log(f"pool mark_shown failed (non-fatal): {exc}")

    # Persist each covered reporter's quarter result into the companies/ KB
    # wing so PRECEDENT/REGIME across quarters has a series to read (spec
    # Sec.6.1/7). Fed from the anomaly panel built above — Task 5 wired this to
    # `cand["fundamentals"]` as an explicit interim stand-in, but fundamentals
    # carries neither the metric families nor the flags those two operators
    # read across quarters.
    if cfg.earnings_enabled:
        if not dry:
            try:
                for cand in bundle.get("post_earnings_candidates") or []:
                    panel = cand.get("panel")
                    ticker = cand.get("ticker")
                    if (isinstance(panel, dict) and panel.get("period_end")
                            and ticker):
                        kb.record_company_quarter(KB_ROOT, ticker, panel)
            except Exception as exc:  # noqa: BLE001
                log(f"record_company_quarter failed (non-fatal): {exc}")

    # Beat 2 upserts the SAME period_end, replacing beat 1's partial row with
    # the complete one (spec Sec.3). Kept as its own block rather than folded
    # into the loop above: the two beats have different scopes and either may
    # be empty on any given day.
    if cfg.filings_enabled:
        if not dry:
            try:
                for followup in bundle.get("post_earnings_followups") or []:
                    panel = followup.get("panel")
                    ticker = followup.get("ticker")
                    if (isinstance(panel, dict) and panel.get("period_end")
                            and ticker):
                        kb.record_company_quarter(KB_ROOT, ticker, panel)
            except Exception as exc:  # noqa: BLE001
                log(f"beat-2 record_company_quarter failed (non-fatal): {exc}")

    # Record today's buried signals so the Signal novelty gate has memory —
    # without this the same FedReg notice re-surfaces day after day.
    if not dry:
        try:
            signal = (result.get("briefing") or {}).get("signal") or {}
            stories = [(signal.get("top") or {}).get("story") or ""]
            stories += [(b or {}).get("story") or ""
                        for b in (signal.get("buried") or [])]
            kb.record_buried(KB_ROOT, [s for s in stories if s.strip()], today)
        except Exception as exc:  # noqa: BLE001
            log(f"record_buried failed (non-fatal): {exc}")

    # Article-level novelty memory: remember every article that reached a desk
    # so tomorrow's selection can demote it (14-day window).
    if not dry:
        try:
            kb.record_seen_articles(KB_ROOT, bundle.get("news") or [], today)
        except Exception as exc:  # noqa: BLE001
            log(f"record_seen_articles failed (non-fatal): {exc}")

    # Phase 0 instrumentation: append deterministic KPIs for the weekly PM review.
    timings = {"fetch": _t_fetch, "select": _t_select, "enrich": _t_enrich,
               "earnings": _t_earnings,
               "anomaly": _t_anomaly, "filing_text": _t_filing_text,
               "codex": _t_codex,
               "elapsed_at_guard": _elapsed_at_guard,
               "total": round(time.time() - _run_start, 1)}
    try:
        rec = metrics.compute_metrics(bundle, result, today, timings)
        log(f"metrics: fedreg={rec.get('fedreg_share')} "
            f"enriched={rec.get('enriched_count')}/{rec.get('news_pool_size')} "
            f"hedging={rec.get('hedging_count')} regions={rec.get('region_distribution')}")
        if not dry:
            metrics.append_metrics(KB_ROOT, rec)
    except Exception as exc:  # noqa: BLE001
        log(f"metrics failed (non-fatal): {exc}")

    if not dry:
        try:
            kb.write_daily(KB_ROOT, today, html)
        except Exception as exc:  # noqa: BLE001
            log(f"write_daily failed: {exc}")
        try:
            kb.append_log(KB_ROOT, f"{today} brief generated ({len(html)} chars, html)")
        except Exception:  # noqa: BLE001
            pass
        state["last_run_date"] = today
        sh = state.setdefault("source_health", {})
        for s, v in health.items():
            if isinstance(v, dict) and v.get("ok"):
                sh[s] = {"last_ok": today}
        codex_state = state.setdefault("codex", {})
        codex_state["status"] = result["analysis_status"]
        if result["analysis_status"]["overall"] == "complete":
            codex_state["last_ok"] = today
        try:
            kb.write_state(KB_ROOT, state)
        except Exception as exc:  # noqa: BLE001
            log(f"write_state failed: {exc}")

    # The cron no_agent runner extracts a `MEDIA:<path>` line and delivers the file
    # as a native Telegram document (attachment-only). The path is pure ASCII, so
    # the runner's cp1252 stdout capture cannot mangle it.
    if not dry:
        try:
            handoff = delivery.deliver(cfg.raw.get("delivery", {}).get("adapter", "stdout"), html_path, log)
            receipt["handoff"] = handoff["handoff"]
        except Exception as exc:  # noqa: BLE001
            receipt["handoff"] = "failed"
            log(f"brief handoff failed: {codex_engine.diagnostic(exc)}")
        _persist_status(KB_ROOT, receipt, html_path.removesuffix(".html") + ".status.json")

    # Deferred on purpose: every elapsed-time guard in this run has already been
    # evaluated by the time stdout carries the MEDIA line, so capsule I/O cannot
    # move a decision. See capsule.py's module docstring.
    if _capsule is not None and not dry:
        try:
            _capsule.note(total_s=round(time.time() - _run_start, 1))
            store = capsule_store()
            if capsule.write(_capsule, store):
                capsule.prune(store, CAPSULE_KEEP)
        except Exception as exc:  # noqa: BLE001
            log(f"capsule write failed (non-fatal): {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
