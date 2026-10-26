"""Read-only, fail-closed publication preflight. This tool NEVER publishes.

Evidence is an external JSON attestation, not independently authenticated truth.
Reports are hashed and bound to HEAD; humans must verify their provenance.
Minimal-profile dry-run reports are validated but remain attestations.
Init/demo cannot satisfy that gate.

External manifest version 1 contract (unknown keys are rejected):
  version: 1; head: full SHA; candidate: full SHA
  runs: exactly seven ordered objects with date (YYYY-MM-DD), candidate, mode
    (scheduled), status (success), owner_accepted (true), artifact and sha256.
  evidence: individual EVIDENCE gate names mapped to head, artifact, sha256,
    exit_code (integer zero), command (the actual argv list). Install entries
    additionally declare platform (windows or linux); this remains attested.
  approvals: individual MANUAL names mapped to head, approved (true),
    approved_by and statement. Omitted gates stay pending.
Artifact paths are absolute or relative to the manifest, with SHA-256 of their
nonempty bytes. Keep this manifest and private logs outside repository trees.
Tests contain synthetic complete examples; none is real acceptance evidence.
Version 2 adds an explicit attributed two-day acceptance policy and a validated
coverage regression artifact; see docs/coverage-validation.md. It retains the
original strict report as historical evidence and never implies byte equivalence.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tomllib

import yaml

import scan_secrets
import verify_minimal_run
import check_coverage_history

ROOT = Path(__file__).resolve().parents[1]
MATRIX = {f"{os} / Python {version}" for os in ("ubuntu-latest", "windows-latest") for version in ("3.11", "3.12")}
MANUAL = {"fresh-clone", "install", "cutover", "owner-authorised"}
EVIDENCE = {"tests", "install-windows", "install-linux", "demo-offline", "minimal-dry-run", "architecture", "l0", "replay"}
REQUIRED_FILES = {"LICENSE", "SECURITY.md", "CONTRIBUTING.md", "README.md", "docs/architecture.md",
                  "pyproject.toml", "requirements.txt", "tools/check_publication.py", "tools/scan_secrets.py"}


class GateError(ValueError):
    """Controlled diagnostic, never constructed from private external values."""


def need(condition, message):
    if not condition:
        raise GateError(message)


def command(root, *args):
    return subprocess.run(list(args), cwd=root, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=120).stdout


def git(root, *args):
    return command(root, "git", "--no-replace-objects", *args)


def full_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def github_repo(value):
    # Exact identity, never substring matching an attacker-controlled URL.
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?", value)
    need(match is not None, "expected a GitHub repository identity")
    return match[1].lower()


def repository_state(root, expected_remote):
    need(not git(root, "status", "--porcelain=v1", "--untracked-files=all").strip(), "working tree is not clean")
    need(git(root, "rev-parse", "--is-shallow-repository").strip() == b"false", "shallow history")
    need(not git(root, "for-each-ref", "--format=%(refname)", "refs/replace").strip(), "replacement refs present")
    head = git(root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    need(full_sha(head), "HEAD is not a full commit")
    branch = git(root, "symbolic-ref", "--short", "HEAD").decode().strip()
    need(git(root, "config", "--get", f"branch.{branch}.remote").strip() == b"origin", "HEAD does not track origin")
    ref = git(root, "config", "--get", f"branch.{branch}.merge").decode().strip()
    need(ref.startswith("refs/heads/"), "invalid upstream branch")
    repo = github_repo(expected_remote)
    for args in (("remote", "get-url", "origin"), ("remote", "get-url", "--push", "origin")):
        need(github_repo(git(root, *args).decode().strip()) == repo, "origin identity mismatch")
    need(git(root, "rev-parse", "@{upstream}").decode().strip() == head, "local upstream differs from HEAD")
    remote = git(root, "ls-remote", "--exit-code", "origin", ref).decode().splitlines()
    need(remote == [head + "\t" + ref], "remote branch is not exact HEAD")
    for name in REQUIRED_FILES:
        committed = git(root, "show", f"{head}:{name}")
        need(committed == (root / name).read_bytes(), "required publication artifact is not committed at HEAD")
    return head, repo


def api(root, endpoint):
    pages = json.loads(command(root, "gh", "api", "--paginate", "--slurp", endpoint))
    need(isinstance(pages, list) and bool(pages) and all(isinstance(p, dict) for p in pages), "invalid GitHub API pages")
    return pages


def ci_state(root, repo, head):
    pages = api(root, f"repos/{repo}/actions/runs?head_sha={head}&per_page=100")
    runs = [run for page in pages for run in page["workflow_runs"]]
    need(bool(runs), "no CI runs for HEAD")
    need(len(runs) == pages[0]["total_count"], "incomplete workflow run inventory")
    ci_runs = 0
    for run in runs:
        need(run["head_sha"] == head and run["status"] == "completed" and run["conclusion"] == "success",
             "CI has wrong-commit, unfinished or unsuccessful runs")
        job_pages = api(root, f"repos/{repo}/actions/runs/{int(run['id'])}/jobs?filter=latest&per_page=100")
        jobs = [job for page in job_pages for job in page["jobs"]]
        need(bool(jobs) and len(jobs) == job_pages[0]["total_count"], "incomplete CI job inventory")
        need(all(job["status"] == "completed" and job["conclusion"] == "success" for job in jobs), "CI job is not successful")
        if run["name"] == "CI":
            ci_runs += 1
            names = [job["name"] for job in jobs]
            need(MATRIX <= set(names) and len(names) == len(set(names)), "expected CI matrix job missing or duplicated")
    need(ci_runs > 0, "required CI workflow missing")


def history_scan(root):
    objects = git(root, "rev-list", "--objects", "--all", "--no-object-names").splitlines()
    need(bool(objects), "empty history inventory")
    findings, blobs = 0, 0
    for oid in sorted(set(objects)):
        sha = oid.decode("ascii")
        kind = git(root, "cat-file", "-t", sha).strip()
        if kind in (b"blob", b"commit", b"tag"):
            data = git(root, "cat-file", kind.decode(), sha).decode("utf-8", "replace")
            findings += len(scan_secrets.scan_text("reachable " + kind.decode(), data))
            blobs += int(kind == b"blob")
        elif kind == b"tree":
            # NUL-delimited tree names preserve tabs, newlines and quotes.
            for row in git(root, "ls-tree", "-z", sha).split(b"\0"):
                if row:
                    metadata, path = row.split(b"\t", 1)
                    need(not metadata.startswith(b"160000 "), "submodule history requires separate audit")
                    findings += len(scan_secrets.scan_text("historical pathname", path.decode("utf-8", "replace")))
        else:
            raise GateError("unknown reachable object type")
    refs = git(root, "for-each-ref", "--format=%(refname)").decode("utf-8", "replace")
    findings += len(scan_secrets.scan_text("reference names", refs))
    need(blobs > 0, "no history blobs scanned")
    need(findings == 0, "sensitive-shaped data detected in reachable history")
    return len(set(objects))


def prose(path):
    return path in {"README.md", "CONTRIBUTING.md", "CHANGELOG.md", "SECURITY.md", "LICENSE"} or (
        path.startswith("docs/") and Path(path).suffix in {".md", ".rst", ".txt"})


def inventory(root, revision):
    out = {}
    for row in git(root, "ls-tree", "-r", "-z", revision).split(b"\0"):
        if row:
            metadata, raw_path = row.split(b"\t", 1)
            path = raw_path.decode("utf-8")
            if not prose(path):
                out[path] = metadata.decode("ascii")
    return out


def runtime_equal(root, head, candidate):
    need(full_sha(candidate), "candidate must be a full SHA")
    need(git(root, "rev-parse", "--verify", candidate + "^{commit}").decode().strip() == candidate, "candidate commit unavailable")
    git(root, "merge-base", "--is-ancestor", candidate, head)
    expected, actual = inventory(root, candidate), inventory(root, head)
    need(bool(expected) and expected == actual, "non-prose byte inventory differs from observed candidate")
    need(any(path.startswith("worldintel/") for path in actual)
         and any(path.startswith("deploy/") for path in actual)
         and {"pyproject.toml", "requirements.txt"} <= set(actual), "incomplete runtime inventory")


def workflow_policy(root):
    workflows = sorted((root / ".github/workflows").glob("*.y*ml"))
    need(bool(workflows), "CI workflows missing")
    actions = 0
    for path in workflows:
        workflow = yaml.safe_load(path.read_bytes())
        need(isinstance(workflow, dict) and workflow.get("permissions") == {"contents": "read"}, "workflow permissions must be contents read only")
        jobs = workflow.get("jobs")
        need(isinstance(jobs, dict) and bool(jobs), "workflow jobs missing")
        for job in jobs.values():
            need(isinstance(job, dict) and job.get("permissions", {"contents": "read"}) == {"contents": "read"}, "job permissions broaden access")
            for step in [job, *job.get("steps", [])]:
                if "uses" in step:
                    uses = step["uses"]
                    need(isinstance(uses, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}", uses), "action is not pinned to a full SHA")
                    actions += 1
                    if uses.startswith("actions/checkout@"):
                        need(step.get("with", {}).get("persist-credentials") is False, "checkout persists credentials")
    need(actions > 0, "no pinned actions checked")


def requirements(blob, hashes=False):
    logical = []
    current = ""
    for raw in blob.decode("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        current += " " + line.removesuffix("\\").strip()
        if not line.endswith("\\"):
            logical.append(current.strip())
            current = ""
    need(not current and bool(logical), "empty or incomplete dependency inventory")
    result = set()
    for line in logical:
        found = re.findall(r"--hash=sha256:[0-9a-f]{64}(?=\s|$)", line)
        if hashes:
            need(bool(found), "dependency lacks SHA-256 hashes")
        cleaned = re.sub(r"\s+--hash=sha256:[0-9a-f]{64}(?=\s|$)", "", line).strip()
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9.!+_-]+)(?:\s*;[^\r\n]+)?", cleaned)
        need(match is not None, "dependency is not exactly pinned or contains unsupported options")
        result.add((re.sub(r"[-_.]+", "-", match[1]).lower(), match[2]))
    return result


def dependency_policy(root):
    locked = requirements((root / "requirements.lock").read_bytes(), hashes=True)
    files = sorted(root.glob("requirements*.txt"))
    need(bool(files), "direct dependencies missing")
    direct = set()
    for path in files:
        direct.update(requirements(path.read_bytes()))
    need(direct <= locked, "lock omits a direct dependency or its pinned version")
    build = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["build-system"]
    need(isinstance(build.get("requires"), list) and bool(build["requires"]), "build backend requirements missing")
    backend = requirements("\n".join(build["requires"]).encode())
    need(backend <= locked, "build backend pin absent from hash lock")


def documentation_policy(root):
    for name in ("README.md", "CONTRIBUTING.md", "SECURITY.md", "docs/architecture.md", "LICENSE"):
        need(bool((root / name).read_text(encoding="utf-8").strip()), "required documentation is empty")
    license_text = (root / "LICENSE").read_text(encoding="utf-8")
    need(all(marker in license_text for marker in ("MIT License", "Permission is hereby granted, free of charge", "THE SOFTWARE IS PROVIDED")), "MIT license text missing")
    examples = root / "worldintel/data/examples"
    need(any(examples.glob("*.json")), "redistributable example data missing")
    provenance = (examples / "README.md").read_text(encoding="utf-8").lower()
    need("invented" in provenance and "demonstration" in provenance and "not a captured" in provenance, "example provenance declaration missing")


def keys(value, expected):
    need(isinstance(value, dict) and set(value) == set(expected), "invalid evidence schema")


def artifact(entry, directory):
    need(isinstance(entry["artifact"], str) and bool(entry["artifact"]), "missing evidence artifact")
    path = Path(entry["artifact"])
    if not path.is_absolute():
        path = directory / path
    need(path.is_file() and not path.is_symlink(), "evidence artifact unavailable")
    need(not any((parent / ".git").exists() for parent in path.resolve().parents), "private evidence artifact is inside a repository")
    blob = path.read_bytes()
    need(bool(blob) and hashlib.sha256(blob).hexdigest() == entry["sha256"], "evidence artifact integrity mismatch")
    return path.resolve()


def load_manifest(path, root, head):
    path = Path(path).resolve(strict=True)
    need(not path.is_relative_to(root.resolve()) and not any((parent / ".git").exists() for parent in path.parents),
         "owner evidence must remain outside repository")
    data = json.loads(path.read_bytes())
    version = data.get('version')
    keys(data, {"version", "head", "candidate", "runs", "evidence", "approvals"} |
         ({'acceptance_policy', 'coverage_regression'} if version == 2 else set()))
    need(type(version) is int and version in (1, 2) and data["head"] == head, "evidence is not bound to HEAD")
    need(full_sha(data["candidate"]), "candidate SHA is incomplete")
    need(isinstance(data["evidence"], dict) and set(data["evidence"]) <= EVIDENCE, "unknown automated evidence gate")
    need(isinstance(data["approvals"], dict) and set(data["approvals"]) <= MANUAL, "unknown manual gate")
    if version == 2:
        acceptance_policy(data)
    return data, path.parent


def acceptance_policy(data):
    policy = data['acceptance_policy']
    keys(policy, {'kind', 'days', 'candidate', 'profile_sha256', 'frozen_at', 'approved', 'approved_by', 'statement'})
    need(policy['kind'] == 'two-consecutive-scheduled-days' and type(policy['days']) is int
         and policy['days'] == 2 and policy['candidate'] == data['candidate'], 'invalid two-day acceptance policy')
    need(policy['approved'] is True and all(isinstance(policy[k], str) and policy[k].strip()
         for k in ('approved_by', 'statement')), 'explicit attributed owner policy required')
    need(check_coverage_history.sha(policy['profile_sha256']), 'frozen profile hash required')
    frozen = dt.datetime.fromisoformat(policy['frozen_at'])
    need(frozen.utcoffset() is not None, 'freeze timestamp needs timezone')
    return frozen


def scheduled_runs(data, directory):
    runs = data["runs"]
    v2 = data.get('version') == 2
    frozen = acceptance_policy(data) if v2 else None
    days = 2 if v2 else 7
    need(isinstance(runs, list) and len(runs) == days, f"exactly {days} acceptance runs required")
    dates, paths, hashes = [], [], []
    for run in runs:
        keys(run, {"date", "candidate", "mode", "status", "owner_accepted", "artifact", "sha256"} |
             ({'profile_sha256', 'quality_complete', 'transport_confirmed'} if v2 else set()))
        date = dt.date.fromisoformat(run["date"])
        need(run["date"] == date.isoformat(), "invalid acceptance date")
        need(run["candidate"] == data["candidate"] and run["mode"] == "scheduled"
             and run["status"] == "success" and run["owner_accepted"] is True, "acceptance run is not eligible")
        if v2:
            need(date > frozen.date() and date <= dt.date.today()
                 and run['profile_sha256'] == data['acceptance_policy']['profile_sha256']
                 and run['quality_complete'] is True and run['transport_confirmed'] is True,
                 'run predates freeze, is future, has wrong profile or incomplete quality/transport')
        dates.append(date)
        paths.append(artifact(run, directory))
        hashes.append(run["sha256"])
    need(len(set(paths)) == days and len(set(dates)) == days and len(set(hashes)) == days, "duplicate acceptance evidence")
    need(all(dates[i] - dates[i - 1] == dt.timedelta(days=1) for i in range(1, days)), "acceptance dates not consecutive in order")


def coverage_regression(data, directory, head, root=ROOT):
    entry = data['coverage_regression']
    keys(entry, {'head', 'artifact', 'sha256', 'exit_code', 'command'})
    need(entry['head'] == head and type(entry['exit_code']) is int and entry['exit_code'] == 0,
         'coverage evaluation not successful at HEAD')
    cmd = entry['command']
    need(isinstance(cmd, list) and len(cmd) > 2 and all(isinstance(v, str) and v for v in cmd)
         and re.fullmatch(r'python(?:3(?:\.\d+)?)?(?:\.exe)?', Path(cmd[0]).name.lower()) is not None
         and Path(cmd[1]).name == 'check_coverage_history.py', 'coverage command mismatch')
    path = artifact(entry, directory)
    report = json.loads(path.read_bytes())
    options = cmd[2:]
    required = {'--pin-path', '--baseline-root', '--baseline-commit', '--baseline-profile',
                '--candidate-root', '--candidate-commit', '--candidate-profile', '--expectations',
                '--strict-equivalence-report', '--report'}
    need(len(options) % 2 == 0, 'invalid coverage command options')
    pairs = list(zip(options[::2], options[1::2]))
    arguments = dict(pairs)
    need(len(arguments) == len(pairs) and required <= set(arguments)
         and set(arguments) <= required | {'--dispositions'}, 'coverage command options missing or duplicated')
    need(arguments['--candidate-commit'] == data['candidate']
         and arguments['--baseline-commit'] == report.get('baseline_commit')
         and Path(arguments['--report']).resolve() == path
         and Path(arguments['--candidate-root']).resolve() == Path(root).resolve(), 'coverage command binding mismatch')
    for option, field in [('--pin-path', 'input_pin_sha256'), ('--candidate-profile', 'candidate_profile_sha256'),
                          ('--baseline-profile', 'baseline_profile_sha256'), ('--expectations', 'expectations_sha256')]:
        source = Path(arguments[option])
        need(source.is_file() and not source.is_symlink()
             and check_coverage_history.digest(source.read_bytes()) == report.get(field), 'coverage command input mismatch')
    pin = json.loads(Path(arguments['--pin-path']).read_bytes())
    captures = [check_coverage_history.load_capture(e) for e in pin['capsules']]
    need([(c['case_id'], c['date'], c['mode']) for c in captures] ==
         [(c['case_id'], c['date'], c['mode']) for c in report['cases']], 'coverage captured cases mismatch')
    for capture, case, pinned in zip(captures, report['cases'], pin['capsules']):
        need(case['input_hashes'] == pinned['files'] and case['capture_source_commit'] == pinned['expected_source_commit'],
             'coverage captured provenance mismatch')
        settings = capture['selection']['selection_settings']
        for side in ('baseline', 'candidate'):
            selected = case[side]['selection']
            need(set(selected) == set(settings['desks']) and all(len(v) <= settings['per_desk']
                 and len(set(v)) == len(v) for v in selected.values()), 'coverage desk capacity mismatch')
        bodies = {i['url']: i.get('content', '') for i in capture['render']['bundle'].get('news', [])
                  if isinstance(i, dict) and isinstance(i.get('url'), str)}
        urls = {u for v in case['candidate']['selection'].values() for u in v}
        need(case['body_availability'] == check_coverage_history.body_availability(sorted(urls), bodies),
             'coverage archived body availability mismatch')
    need(Path(arguments['--strict-equivalence-report']).resolve() ==
         Path(report['strict_equivalence']['artifact']).resolve(), 'coverage strict evidence mismatch')
    dispositions = None
    if report.get('dispositions_sha256') is not None:
        need('--dispositions' in arguments, 'coverage disposition command missing')
        source = Path(arguments['--dispositions'])
        need(source.is_file() and check_coverage_history.digest(source.read_bytes()) == report['dispositions_sha256'],
             'coverage disposition input mismatch')
        dispositions = json.loads(source.read_bytes())
    elif '--dispositions' in arguments:
        raise GateError('unexpected unhashed disposition input')
    try:
        check_coverage_history.validate_report(report, data['candidate'], data['acceptance_policy']['profile_sha256'],
            expectations=json.loads(Path(arguments['--expectations']).read_bytes()), dispositions=dispositions)
    except (ValueError, KeyError, TypeError):
        raise GateError('coverage report invalid or unreviewed') from None


def minimal_report(entry, directory, head, root, path):
    cmd = entry['command']
    need(len(cmd) >= 4 and cmd[1] == 'tools/verify_minimal_run.py',
         'UNSATISFIED: minimal evidence must invoke the live dry-run verifier')
    try:
        options = cmd[2:]
        report_arg = None
        while options:
            flag, value, *options = options
            need(flag in {'--report', '--protect-config', '--timeout'}, 'invalid verifier command')
            if flag == '--report':
                need(report_arg is None, 'duplicate verifier report option')
                report_arg = Path(value)
            if flag == '--timeout':
                need(value.isdigit() and int(value) > 0, 'invalid verifier timeout')
        need(report_arg is not None, 'missing verifier report option')
        if not report_arg.is_absolute():
            report_arg = root / report_arg
        need(report_arg.resolve() == path, 'verifier command report differs from hashed artifact')
        report = json.loads(path.read_bytes())
        keys(report, {'schema_version', 'kind', 'status', 'errors', 'source_commit',
                      'source_dirty', 'source_root', 'protected_paths_unchanged',
                      'command', 'exit_code', 'dry_summary', 'stderr_sha256', 'model_evidence_root'})
        need(type(report['schema_version']) is int and report['schema_version'] == 2
             and report['kind'] == 'minimal-live-dry-run' and report['status'] == 'PASS'
             and report['errors'] == [] and type(report['errors']) is list,
             'minimal report is not successful')
        need(report['source_commit'] == head and report['source_dirty'] is False
             and report['protected_paths_unchanged'] is True
             and isinstance(report['source_root'], str)
             and Path(report['source_root']).resolve() == root.resolve(), 'minimal report source or protection mismatch')
        actual = report['command']
        need(isinstance(actual, list) and len(actual) == 8
             and all(isinstance(arg, str) and arg for arg in actual)
             and re.fullmatch(r'python(?:3(?:\.\d+)?)?(?:\.exe)?', Path(actual[0]).name.lower()) is not None
             and actual[1:5] == ['-m', 'worldintel.cli', 'run', '--config']
             and actual[6:] == ['--dry', '--force'], 'minimal report did not execute the forced dry pipeline')
        need(type(report['exit_code']) is int and report['exit_code'] == 0
             and isinstance(report['stderr_sha256'], str)
             and re.fullmatch(r'[0-9a-f]{64}', report['stderr_sha256']) is not None,
             'minimal report exit or stderr attestation invalid')
        need(isinstance(report['dry_summary'], dict)
             and type(report['dry_summary'].get('schema_version')) is int,
             'minimal report dry summary schema invalid')
        need(not verify_minimal_run.validate_result(report['exit_code'], '',
             verify_minimal_run.PREFIX + json.dumps(report['dry_summary']), head, root),
             'minimal report dry summary invalid')
        need(isinstance(report['model_evidence_root'], str)
             and Path(report['model_evidence_root']).is_absolute()
             and not Path(report['model_evidence_root']).resolve().is_relative_to(root.resolve())
             and not verify_minimal_run.validate_model_evidence(
                 report['model_evidence_root'], report['dry_summary']['model_evidence']),
             'minimal report private model evidence invalid')
    except (ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, GateError):
            raise
        raise GateError('invalid minimal verifier report') from None


def evidence_gate(name, data, directory, head, root=ROOT):
    entry = data["evidence"].get(name)
    need(entry is not None, "evidence pending")
    keys(entry, {"head", "artifact", "sha256", "exit_code", "command"} |
         ({"platform"} if name.startswith("install-") else set()))
    need(entry["head"] == head and type(entry["exit_code"]) is int and entry["exit_code"] == 0, "command evidence is not successful at HEAD")
    need(isinstance(entry["command"], list) and entry["command"]
         and all(isinstance(arg, str) and arg for arg in entry["command"]), "missing executed command")
    cmd = entry["command"]
    need(re.fullmatch(r"python(?:3(?:\.\d+)?)?(?:\.exe)?", Path(cmd[0]).name.lower()) is not None
         or (name == "demo-offline" and Path(cmd[0]).stem == "world-intel"), "unrecognized evidence command executable")
    if name == "tests":
        need(cmd[1:3] == ["-m", "pytest"] and cmd[3:] in ([], ["-q"], ["tests", "-q"]), "test evidence command is not an allowed complete-suite invocation")
    elif name.startswith("install-"):
        need(entry["platform"] == name.removeprefix("install-"), "install platform attestation mismatch")
        options = cmd[4:]
        if options[:2] == ['--no-deps', '--no-build-isolation']:
            options = options[2:]
        need(cmd[1:4] == ["-m", "pip", "install"] and len(options) == 1
             and (options[0] == "." or (not options[0].startswith("-") and options[0].endswith(".whl"))), "install evidence command is not an allowed project installation")
    elif name == "demo-offline":
        need(cmd[1:4] == ["-m", "worldintel.cli", "demo"] or (Path(cmd[0]).stem == "world-intel" and cmd[1:2] == ["demo"]), "demo evidence command mismatch")
    elif name in {"architecture", "l0", "replay"}:
        script = {"architecture": "check_architecture.py", "l0": "check_l0.py", "replay": "check_equivalence.py"}[name]
        need(len(cmd) > 1 and Path(cmd[1]).name == script, "gate evidence command mismatch")
    path = artifact(entry, directory)
    if name == "minimal-dry-run":
        minimal_report(entry, directory, head, Path(root), path)


def manual_gate(name, data, head):
    entry = data["approvals"].get(name)
    need(entry is not None, "MANUAL pending")
    keys(entry, {"head", "approved", "approved_by", "statement"})
    need(entry["head"] == head and entry["approved"] is True, "MANUAL approval not bound to HEAD")
    need(all(isinstance(entry[key], str) and entry[key].strip() for key in ("approved_by", "statement")), "MANUAL approval lacks attribution")


def check(root, expected_remote, evidence_path):
    rows = []
    def gate(name, fn, status="PASS"):
        try:
            result = fn()
            rows.append({"gate": name, "status": status})
            return result
        except Exception as exc:
            # Avoid command stderr, private file paths, captured logs or findings.
            reason = str(exc) if isinstance(exc, GateError) else type(exc).__name__
            rows.append({"gate": name, "status": "PENDING" if name in MANUAL else "FAIL", "reason": reason})
    state = gate("committed-remote-head", lambda: repository_state(root, expected_remote))
    gate("reachable-history", lambda: history_scan(root))
    gate("workflow-policy", lambda: workflow_policy(root))
    gate("dependency-policy", lambda: dependency_policy(root))
    gate("documentation-policy", lambda: documentation_policy(root))
    if state:
        head, repo = state
        gate("exact-head-ci", lambda: ci_state(root, repo, head))
        loaded = gate("evidence-manifest", lambda: load_manifest(evidence_path, root, head))
        if loaded:
            data, directory = loaded
            gate("candidate-runtime", lambda: runtime_equal(root, head, data["candidate"]))
            scheduled_gate = 'two-scheduled-runs' if data['version'] == 2 else 'seven-scheduled-runs'
            gate(scheduled_gate, lambda: scheduled_runs(data, directory), "ATTESTED")
            evidence_names = EVIDENCE - {'replay'} if data['version'] == 2 else EVIDENCE
            if data['version'] == 2:
                gate('coverage-regression', lambda: coverage_regression(data, directory, head, root), 'ATTESTED')
            for name in sorted(evidence_names):
                gate(name, lambda name=name: evidence_gate(name, data, directory, head, root), "ATTESTED")
            for name in sorted(MANUAL):
                gate(name, lambda name=name: manual_gate(name, data, head), "ATTESTED")
    represented = {row["gate"] for row in rows}
    v2 = any(row['gate'] in {'two-scheduled-runs', 'coverage-regression'} for row in rows)
    expected = (EVIDENCE - {'replay'} | {'coverage-regression', 'two-scheduled-runs'}) if v2 else EVIDENCE | {'seven-scheduled-runs'}
    for name in sorted(expected | MANUAL | {"candidate-runtime", "exact-head-ci", "evidence-manifest"}):
        if name not in represented:
            rows.append({"gate": name, "status": "PENDING", "reason": "prerequisite pending"})
    return {"publication_ready": all(row["status"] == "PASS" for row in rows), "gates": rows,
            "limitation": "ATTESTED gates are not verified PASS and keep publication blocked. External logs, declared platforms and human approvals are not independently authenticated. Executable verifiers and an owner review are still required. No publication action is performed."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-remote", required=True, help="Exact GitHub owner/repository or origin URL")
    parser.add_argument("--evidence", required=True, type=Path, help="External owner-controlled evidence manifest")
    args = parser.parse_args(argv)
    result = check(ROOT, args.expect_remote, args.evidence)
    print(json.dumps(result, indent=2))
    return 0 if result["publication_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
