"""Offline replay worker and strict capsule validation. Never emits article data."""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import sys
import traceback

BOUNDARIES = {
    "selection-input.json": "before select_news.select_candidates",
    "render-input.json": "before compose_html.render_html",
}
_STAGE = "startup"
_LEGACY_SETTINGS = {"desks", "local_desks", "fresh_hours", "per_desk", "per_feed_cap"}
_RANKING_SETTINGS = {"ranking_profile", "ranking_profile_hash"}


def ranking_policy():
    """Validate recordings with this tool's policy, independently of replay root."""
    root = Path(__file__).resolve().parents[1]
    path = root / "worldintel/core/coverage_policy.py"
    if not path.is_file():
        path = root / "scripts/coverage_policy.py"
    require(path.is_file(), "ranking policy unavailable")
    spec = importlib.util.spec_from_file_location("_replay_ranking_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReplayError(ValueError):
    """A safe diagnostic containing no captured content."""


def require(condition, message):
    if not condition:
        raise ReplayError(message)


def digest(blob):
    return hashlib.sha256(blob).hexdigest()


def module_layout(root):
    """Prefer the explicit package when present; never mix both layouts."""
    root = Path(root)
    packaged = (root / "worldintel").is_dir()
    origins = {name: root / ("worldintel/core" if packaged else "scripts") / (name + ".py")
               for name in ("select_news", "compose_html", "fetch_sources")}
    return packaged, origins


def load_capsule(path, expected_commit):
    path = Path(path)
    require(path.is_dir() and not path.is_symlink(), "invalid capsule directory")
    require(not (path / "manifest.json").is_symlink(), "manifest symlink")
    manifest = json.loads((path / "manifest.json").read_bytes())
    require(isinstance(manifest, dict), "invalid manifest")
    if manifest.get("mode") in ("forced", "synthetic"):
        return None
    require(manifest.get("mode") == "scheduled", "invalid capture mode")
    require(type(manifest.get("format_version")) is int and manifest["format_version"] == 1,
            "invalid format version")
    require(manifest.get("capsule_id") == path.name, "capsule id mismatch")
    require(manifest.get("source_commit") == expected_commit, "commit mismatch")
    require(manifest.get("source_dirty") is False, "dirty or missing provenance")
    require(manifest.get("errors") == [], "capture errors")
    started = dt.datetime.fromisoformat(manifest["started_at"])
    require(started.utcoffset() is not None, "missing timestamp timezone")
    require(re.fullmatch(re.escape(started.strftime("%Y-%m-%dT%H%M%S%z")) + r"-[0-9a-f]{8}", path.name),
            "timestamp mismatch")
    require(manifest.get("boundaries") == BOUNDARIES, "boundary mismatch")
    files = manifest.get("files")
    require(isinstance(files, dict) and set(files) == set(BOUNDARIES), "missing or unexpected inputs")
    payloads = {}
    for name in BOUNDARIES:
        file = path / name
        require(file.is_file() and not file.is_symlink(), "missing or linked input")
        blob = file.read_bytes()
        entry = files[name]
        require(isinstance(entry, dict) and type(entry.get("bytes")) is int
                and entry["bytes"] == len(blob) and entry.get("sha256") == digest(blob),
                "input integrity mismatch")
        payloads[name] = json.loads(blob)
        require(isinstance(payloads[name], dict), "invalid input object")
    selection, render = (payloads[name] for name in BOUNDARIES)
    date = started.date().isoformat()
    require(selection.get("today") == render.get("date") == date, "recorded date mismatch")
    require(isinstance(selection.get("bundle"), dict) and isinstance(render.get("bundle"), dict)
            and isinstance(render.get("analysis"), dict), "missing boundary payload")
    require(isinstance(selection.get("seen"), list)
            and all(isinstance(item, str) for item in selection["seen"]), "invalid seen set")
    require(selection.get("as_of") == selection["bundle"].get("as_of"), "as_of mismatch")
    settings = selection.get("selection_settings")
    require(isinstance(settings, dict) and set(settings) in
            (_LEGACY_SETTINGS, _LEGACY_SETTINGS | _RANKING_SETTINGS), "missing selection settings")
    if "ranking_profile" in settings:
        policy = ranking_policy()
        require(isinstance(settings["ranking_profile"], dict), "invalid ranking profile")
        try:
            resolved = policy.resolve_profile(settings["ranking_profile"])
        except ValueError as exc:
            raise ReplayError("invalid ranking profile") from exc
        require(resolved == settings["ranking_profile"], "ranking profile is not resolved")
        require(settings["ranking_profile_hash"] == policy.profile_hash(resolved), "ranking profile hash mismatch")
    for key in ("desks", "local_desks"):
        require(isinstance(settings[key], list) and all(isinstance(v, str) for v in settings[key])
                and len(set(settings[key])) == len(settings[key]), "invalid desk settings")
    require(bool(settings["desks"]) and set(settings["local_desks"]) <= set(settings["desks"]), "invalid desks")
    for key in ("fresh_hours", "per_desk", "per_feed_cap"):
        require(type(settings[key]) is int and settings[key] > 0, "invalid numeric selection setting")
    clock = render.get("clock")
    require(isinstance(clock, dict) and clock.get("local_date") == date, "missing or inconsistent render clock")
    generated = dt.datetime.fromisoformat(clock["generated_at"])
    require(generated.utcoffset() is not None and generated.date().isoformat() == date
            and generated.strftime("%z") == clock["utc_offset"].replace(":", ""), "invalid render clock")
    return {"date": date, "selection": selection, "render": render}


def collect(store, expected_commit):
    require(bool(expected_commit), "expected commit required")
    result = []
    for path in sorted(Path(store).iterdir()):
        require(not path.name.startswith(".partial-"), "partial capsule present")
        if path.is_dir() or path.is_symlink():
            capsule = load_capsule(path, expected_commit)
            if capsule is not None:
                result.append((path.resolve(), capsule["date"]))
    dates = [date for _, date in result]
    require(len(dates) == len(set(dates)), "duplicate scheduled date")
    require(len(dates) >= 5, "five distinct scheduled dates required")
    return sorted(result, key=lambda row: row[1])


def worker(root, path, commit):
    global _STAGE
    _STAGE = "import"
    root = Path(root).resolve(strict=True)
    packaged, origins = module_layout(root)
    sys.path.insert(0, str(root if packaged else root / "scripts"))
    # Fail on network attempts even when pipeline code catches the exception.
    network_attempts = []
    def offline(event, args):
        # urllib3.util.connection._has_ipv6 binds ::1:0 during import to
        # detect local IPv6 support. An ephemeral loopback bind performs no
        # external I/O; listening remains explicitly prohibited below.
        if event == "socket.bind" and len(args) == 2 and args[1] in (("::1", 0), ("127.0.0.1", 0)):
            return
        if event in {"socket.connect", "socket.getaddrinfo", "socket.bind", "socket.sendto", "socket.listen"}:
            # Only code locations: never hostnames, socket arguments, source
            # lines, paths, or exception text from private inputs.
            frames = traceback.extract_stack(limit=7)[:-1]
            trace = ">".join(Path(frame.filename).name + ":" + frame.name for frame in frames)
            network_attempts.append(f"{_STAGE}/{event}/{trace}")
            raise ReplayError("network disabled")
    sys.addaudithook(offline)
    import socket
    def no_listen(self, *args, **kwargs):
        offline("socket.listen", ())
    socket.socket.listen = no_listen
    modules = {}
    for name in ("select_news", "compose_html", "fetch_sources"):
        import_name = "worldintel.core." + name if packaged else name
        module = importlib.import_module(import_name)
        require(Path(module.__file__).resolve() == origins[name].resolve()
                and module.__spec__.name == import_name, "wrong module origin")
        modules[name] = module
    _STAGE = "load"
    capsule = load_capsule(path, commit)
    require(capsule is not None, "worker requires scheduled capsule")
    _STAGE = "settings"
    selection = modules["select_news"]
    settings = capsule["selection"]["selection_settings"]
    policy_path = root / ("worldintel/core" if packaged else "scripts") / "coverage_policy.py"
    if "ranking_profile" in settings or policy_path.is_file():
        import_name = "worldintel.config" if packaged else "config"
        config = importlib.import_module(import_name)
        config_path = root / ("worldintel" if packaged else "scripts") / "config.py"
        require(Path(config.__file__).resolve() == config_path.resolve()
                and config.__spec__.name == import_name, "wrong config module origin")
        cfg = config.get()
        require(hasattr(cfg, "ranking"), "ranking configuration unavailable")
        policy = ranking_policy()
        cfg.ranking = policy.resolve_profile(settings["ranking_profile"] if "ranking_profile" in settings else
                                            {"topics": [], "major_events": {"enabled": False}})
    # Verify defaults as well as passing the recorded call argument: mutating
    # candidate constants to match the recording would hide a config regression.
    effective = selection.selection_settings() if hasattr(selection, "selection_settings") else {
        "desks": list(selection.desk_ids() if hasattr(selection, "desk_ids") else selection.DESKS),
        "local_desks": list(selection.local_desk_ids() if hasattr(selection, "local_desk_ids") else selection._LOCAL_DESKS),
        "per_feed_cap": selection.PER_FEED_CAP,
        "fresh_hours": inspect.signature(selection._drop_stale).parameters["hours"].default,
        "per_desk": inspect.signature(selection.select_candidates).parameters["per_desk"].default,
    }
    if "ranking_profile" not in settings:
        effective = {key: value for key, value in effective.items() if key in _LEGACY_SETTINGS}
    require(effective == settings, "recorded selection settings differ from code configuration")
    payload = capsule["selection"]
    _STAGE = "select"
    selected = selection.select_candidates(payload["bundle"], set(payload["seen"]), per_desk=settings["per_desk"])
    require(isinstance(selected, dict) and set(selected) == set(settings["desks"])
            and all(isinstance(v, list) for v in selected.values()), "invalid selection output")
    payload = capsule["render"]
    _STAGE = "render"
    render_fn = modules["compose_html"].render_html
    kwargs = {}
    if "desks" in inspect.signature(render_fn).parameters:
        import_name = "worldintel.config" if packaged else "config"
        config = importlib.import_module(import_name)
        config_path = root / ("worldintel" if packaged else "scripts") / "config.py"
        require(Path(config.__file__).resolve() == config_path.resolve()
                and config.__spec__.name == import_name, "wrong config module origin")
        cfg = config.get()
        recorded = dt.datetime.fromisoformat(payload["clock"]["generated_at"])
        local = recorded.astimezone(cfg.timezone)
        require(local.date().isoformat() == payload["clock"]["local_date"]
                and local.utcoffset() == recorded.utcoffset(), "configured timezone differs from recorded clock")
        kwargs = {"desks": cfg.desks, "timezone_label": local.tzname()}
    html = render_fn(payload["analysis"], payload["bundle"], payload["date"], **kwargs)
    require(isinstance(html, str) and bool(html), "invalid render output")
    require(not network_attempts, "network attempted during offline replay: " + "; ".join(network_attempts[:3]))
    return {"date": capsule["date"], "root": str(root), "selection_settings": effective,
            "origins": {name: str(Path(module.__file__).resolve()) for name, module in modules.items()},
            "selection_sha256": digest(json.dumps(selected, sort_keys=True, ensure_ascii=False,
                                                  separators=(",", ":"), allow_nan=False).encode("utf-8")),
            "counts": {key: len(value) for key, value in selected.items()},
            "html_sha256": digest(html.encode("utf-8")), "html_bytes": len(html.encode("utf-8"))}


if __name__ == "__main__":
    try:
        # Imported code cannot leak article data through stdout/stderr.
        import contextlib
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = worker(*sys.argv[1:])
        print(json.dumps(result))
    except Exception as exc:
        print(json.dumps({"error": "offline replay worker failed", "stage": _STAGE,
                          "detail": str(exc) if isinstance(exc, ReplayError) else type(exc).__name__}))
        raise SystemExit(2)
