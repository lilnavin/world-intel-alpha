"""Install into an empty venv and exercise the actual console script elsewhere.

Dependency installation needs network access (or a prepopulated pip cache).
No live pipeline, model call, credential read, or deployment is performed.
This proves installation/demo, NOT that a minimal live dry run completed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def verify(root=ROOT):
    completed = []
    def execute(stage, command, cwd, env):
        print(stage, flush=True)
        result = subprocess.run([str(arg) for arg in command], cwd=cwd, env=env,
                                capture_output=True, text=True, timeout=900)
        if result.returncode:
            # Do not print dependency-manager output, which can include private
            # configured index URLs. The stage and exit status locate the fault.
            raise RuntimeError(f"{stage} failed with exit {result.returncode}")
        completed.append(stage)
        return result.stdout
    with tempfile.TemporaryDirectory(prefix="world-intel-install-") as temporary:
        temp = Path(temporary)
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("WORLD_INTEL_CONFIG", None)
        env["PYTHONUTF8"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        venv = temp / "venv"
        execute("create fresh venv", [sys.executable, "-m", "venv", venv], temp, env)
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        python = bindir / ("python.exe" if os.name == "nt" else "python")
        exe = bindir / ("world-intel.exe" if os.name == "nt" else "world-intel")
        execute("install hash-locked dependencies", [python, "-m", "pip", "install", "--require-hashes",
                "-r", root / "requirements.lock"], temp, env)
        execute("install product package", [python, "-m", "pip", "install", "--no-deps",
                "--no-build-isolation", root], temp, env)
        execute("dependency compatibility", [python, "-m", "pip", "check"], temp, env)
        execute("console help", [exe, "--help"], temp, env)
        cfg = temp / "worldintel.yaml"
        execute("init minimal", [exe, "init", "--profile", "minimal", "--out", cfg], temp, env)
        execute("strict validation", [exe, "validate", "--config", cfg, "--strict"], temp, env)
        execute("explain configuration", [exe, "validate", "--config", cfg, "--explain"], temp, env)
        execute("offline demo", [exe, "demo", "--out", temp / "demo"], temp, env)
        code = (
            "from pathlib import Path; import worldintel; from worldintel import config; "
            "from worldintel.core import schema_build,prompt_build; "
            f"assert not Path(worldintel.__file__).resolve().is_relative_to(Path({str(root)!r})); "
            f"cfg=config.load({str(cfg)!r}); "
            "assert schema_build.build(cfg)['properties']; assert prompt_build.build(cfg)"
        )
        execute("installed resources and module origin", [python, "-I", "-c", code], temp, env)
    return {"passed": True, "checks": completed, "minimal_live_dry_run": "NOT VERIFIED"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        result = {"passed": False, "error": str(exc)}
    if args.report:
        args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
