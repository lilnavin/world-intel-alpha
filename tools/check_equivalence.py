"""Compare two explicit code trees using verified private offline capsules."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import replay


def run_worker(root, capsule, commit, config=None):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("WORLD_INTEL_") and key not in {"PYTHONPATH", "PYTHONHOME"}}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if config:
        env["WORLD_INTEL_CONFIG"] = str(Path(config).resolve(strict=True))
    process = subprocess.run([sys.executable, "-I", "-B", str(Path(replay.__file__).resolve()),
                              str(root), str(capsule), commit], cwd=root, env=env,
                             capture_output=True, text=True, encoding="utf-8", timeout=120)
    if process.returncode:
        try:
            error = json.loads(process.stdout)
        except (ValueError, TypeError):
            error = {}
        stage = error.get("stage")
        detail = error.get("detail")
        # Only our worker emits this object; stdout from replayed functions is
        # redirected. Never forward raw stderr, tracebacks, or captured data.
        if error.get("error") == "offline replay worker failed" and stage in {
                "startup", "import", "load", "settings", "select", "render"} and isinstance(detail, str):
            raise replay.ReplayError(f"replay subprocess failed ({stage}: {detail})")
        raise replay.ReplayError("replay subprocess failed (no safe diagnostic)")
    result = json.loads(process.stdout)
    replay.require(result.get("root") == str(root), "wrong replay root")
    expected = {name: str(path.resolve()) for name, path in replay.module_layout(root)[1].items()}
    replay.require(result.get("origins") == expected, "wrong imported module origins")
    for key in ("selection_sha256", "html_sha256"):
        replay.require(isinstance(result.get(key), str) and len(result[key]) == 64, "missing replay digest")
    return result


def check(store, baseline_root, candidate_root, expected_commit, candidate_config=None, baseline_config=None):
    baseline = Path(baseline_root).resolve(strict=True)
    candidate = Path(candidate_root).resolve(strict=True)
    replay.require(baseline != candidate and not baseline.samefile(candidate), "code roots must be distinct")
    rows = []
    for capsule, date in replay.collect(store, expected_commit):
        try:
            before = run_worker(baseline, capsule, expected_commit, baseline_config)
        except replay.ReplayError as exc:
            raise replay.ReplayError(f"baseline: {exc}") from None
        try:
            after = run_worker(candidate, capsule, expected_commit, candidate_config)
        except replay.ReplayError as exc:
            raise replay.ReplayError(f"candidate: {exc}") from None
        replay.require(before["date"] == after["date"] == date, "replay date mismatch")
        rows.append({"date": date, "baseline": before, "candidate": after,
                     "equivalent": all(before[k] == after[k] for k in
                                       ("selection_sha256", "html_sha256", "counts", "html_bytes"))})
    return {"equivalent": all(row["equivalent"] for row in rows), "days": len(rows), "results": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("store", "baseline-root", "candidate-root", "expected-commit"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--candidate-config")
    parser.add_argument("--baseline-config")
    parser.add_argument("--report", type=Path, help="Atomically write a shareable report containing dates and hashes only")
    args = parser.parse_args(argv)
    report = args.report
    del args.report
    try:
        result = check(**vars(args))
        if report is not None:
            write_report(report, result)
    except Exception as exc:
        detail = str(exc) if isinstance(exc, replay.ReplayError) else type(exc).__name__
        print(json.dumps({"equivalent": False, "error": "capsule validation or offline replay failed",
                          "detail": detail}))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["equivalent"] else 1


def write_report(path, result):
    """No profile, code roots, captured values, or fixture paths leave the gate."""
    safe = {"equivalent": result["equivalent"], "days": result["days"], "results": [
        {"date": row["date"], "equivalent": row["equivalent"],
         **{side: {key: row[side][key] for key in ("selection_sha256", "html_sha256")}
            for side in ("baseline", "candidate")}} for row in result["results"]]}
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".equivalence-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(safe, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
