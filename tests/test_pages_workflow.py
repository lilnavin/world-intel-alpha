"""Security and artifact contracts for the fixture-only Pages workflow."""
from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]


def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8"))


def test_events_and_deployment_boundary():
    data = workflow()
    events = data.get("on", data.get(True))
    assert set(events) == {"push", "pull_request", "workflow_dispatch"}
    assert events["push"]["branches"] == ["main"]
    deploy = data["jobs"]["deploy"]
    guard = deploy["if"]
    for condition in (
        "github.repository == 'lilnavin/world-intel-alpha'",
        "github.event.repository.private == false",
        "github.ref == 'refs/heads/main'",
        "github.event_name == 'push'",
        "github.event_name == 'workflow_dispatch'",
    ):
        assert condition in guard
    assert "pull_request" not in guard
    assert deploy["needs"] == "build"
    assert deploy["environment"]["name"] == "github-pages"
    assert deploy["concurrency"]["group"] == "pages"


def test_permissions_and_pinned_official_actions():
    data = workflow()
    assert data["permissions"] == {}
    assert data["jobs"]["build"]["permissions"] == {"contents": "read"}
    assert data["jobs"]["deploy"]["permissions"] == {
        "contents": "read", "pages": "write", "id-token": "write",
    }
    for job in data["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", step["uses"])
    checkout = data["jobs"]["build"]["steps"][0]
    assert checkout["with"]["persist-credentials"] is False


def test_only_validated_fresh_staging_is_uploaded():
    steps = workflow()["jobs"]["build"]["steps"]
    scripts = "\n".join(step.get("run", "") for step in steps)
    assert "pip install --require-hashes -r requirements.lock" in scripts
    assert "pip install --no-deps --no-build-isolation ." in scripts
    build = next(step for step in steps if step.get("id") == "stage")
    assert build["shell"] == "python"
    for required in ("tempfile.mkdtemp", '"world-intel", "demo", "--out"',
                     '"index.html"', "scan_secrets.py", "is_symlink", "rglob"):
        assert required in build["run"]
    upload = steps[-1]
    assert upload["uses"].startswith("actions/upload-pages-artifact@")
    assert upload["with"]["path"] == "${{ steps.stage.outputs.path }}"
    assert "if" not in upload
    assert "world-intel-agent" not in scripts
