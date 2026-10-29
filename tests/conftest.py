"""The default suite cannot use live services or spend model tokens."""
import socket
import subprocess
import ntpath
import shlex
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def synthetic_config(tmp_path):
    """Exercise configured runtime paths without reading an owner's profile.

    Installed after collection so module imports must remain configuration-free.
    Each test owns its cache and filesystem; loader tests can clear it explicitly.
    """
    from worldintel import config

    previous = config._CACHE
    template = config.load(Path(__file__).resolve().parents[1] / "config/example.full.yaml")
    raw = template.raw
    raw["runtime"]["data_root"] = str(tmp_path / "data")
    raw["runtime"]["media_dir"] = str(tmp_path / "media")
    raw["runtime"]["env_file"] = str(tmp_path / "env" / ".env")
    cfg = config.Config(raw, str(tmp_path / "synthetic.yaml"))
    config.set_for_test(cfg)
    try:
        yield cfg
    finally:
        config.set_for_test(previous)


class NetworkBlocked(BaseException):
    """Not swallowed by production fail-open Exception handlers."""


def pytest_collection_modifyitems(items):
    live = {"test_smoke_test_live", "test_analyze_live_end_to_end",
            "test_live_federal_register_smoke"}
    for item in items:
        if item.name in live:
            item.add_marker(pytest.mark.skip(reason="explicit live-service test excluded from offline suite"))


@pytest.fixture(autouse=True)
def offline_boundaries(monkeypatch):
    def blocked(*args, **kwargs):
        raise NetworkBlocked("External I/O forbidden in offline tests; use recorded fixtures")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    # Host workers run in child interpreters, beyond these socket patches.
    from worldintel.core import command_engine
    monkeypatch.setattr(command_engine, "_execute", blocked)
    from worldintel import agent_run
    monkeypatch.setattr(agent_run, "_invoke", blocked)
    # curl-backed clients bypass Python sockets.
    try:
        import curl_cffi.requests
    except ImportError:
        pass
    else:
        monkeypatch.setattr(curl_cffi.requests.Session, "request", blocked)
    original = subprocess.Popen

    def guarded_process(args, *rest, **kwargs):
        tokens = shlex.split(args, posix=False) if isinstance(args, str) else args
        if any(ntpath.basename(str(token).strip('\"')).lower() in
               {"codex", "codex.exe", "codex.cmd", "codex.bat"} for token in tokens):
            blocked()
        return original(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_process)
