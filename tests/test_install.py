"""Build real wheels offline; test their resources outside a source checkout.

This is not a fresh dependency-install proof. That release gate is separate.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_synthetic_assets_have_only_example_editorial_links():
    text = "\n".join(path.read_text(encoding="utf-8") for path in
                     (ROOT / "worldintel/data/examples").glob("*.json"))
    for host in re.findall(r'https?://([^/"\s]+)', text):
        assert re.fullmatch(r"(?:[^.]+\.)*example\.(?:com|net|org)", host)
    for outlet in ("reuters", "bloomberg", "guardian", "nikkei", "nytimes", "bbc"):
        assert outlet not in text.lower()
    briefing = json.loads((ROOT / "worldintel/data/examples/sample-analysis.json").read_text())["briefing"]
    assert all(briefing[key] for key in ("brief", "harbor", "signal", "threads", "on_radar", "finance", "post_earnings"))


def test_wheel_resources_and_demo_outside_checkout(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "requirements.txt"):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / "worldintel", source / "worldintel", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    script = "import setuptools.build_meta; setuptools.build_meta.build_wheel(" + repr(str(wheels)) + ")"
    built = subprocess.run([sys.executable, "-c", script], cwd=source, capture_output=True, text=True)
    assert built.returncode == 0, built.stderr[-3000:]
    wheel, = wheels.glob("*.whl")
    installed = tmp_path / "wheel-content"
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for suffix in ("profiles/example.minimal.yaml", "profiles/example.full.yaml", "prompts/world.base.md",
                       "schema/world.base.template", "schema/national.next.template", "examples/sample-analysis.json"):
            assert "worldintel/data/" + suffix in names
        entry = next(name for name in names if name.endswith("entry_points.txt"))
        assert "world-intel = worldintel.cli:main" in archive.read(entry).decode()
        archive.extractall(installed)
    # Only extracted wheel content is importable through PYTHONPATH. Deny
    # Python sockets, curl-backed requests, and every child process (including
    # a model CLI), and point config at a missing file. No quiet fail-open lane
    # can hide a boundary attempt because the sentinel inherits BaseException.
    outside = tmp_path / "outside"
    outside.mkdir()
    code = '''
import socket, subprocess, sys
class OfflineViolation(BaseException): pass
def deny(*a, **kw): raise OfflineViolation('demo attempted external I/O')
socket.socket.connect = deny
socket.socket.connect_ex = deny
socket.create_connection = deny
socket.getaddrinfo = deny
class GuardedPopen(subprocess.Popen):
    def __init__(self, *a, **kw): deny()
subprocess.Popen = GuardedPopen
try:
    import curl_cffi.requests
except ImportError:
    pass
else:
    curl_cffi.requests.Session.request = deny
from worldintel import cli
assert cli.main(['demo', '--out', '.']) == 0
'''
    env = dict(os.environ, PYTHONPATH=str(installed), WORLD_INTEL_CONFIG=str(outside / "missing.yaml"), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-c", code], cwd=outside, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (outside / "world-intel-demo.html").is_file()
