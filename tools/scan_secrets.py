"""Pre-commit / CI guard: refuse to ship anything personal.

The scrub is a one-time event; the risk is not (spec section 8). This runs as a
pre-commit hook on the author's machine AND in CI, because a hook protects only
the machine it is installed on.

ABSOLUTE RULE: no forbidden value may appear in this file as a literal. The
scanner is scanned by itself (see tests/test_scan_secrets.py); one inlined
address, id or user path here would report itself as a finding forever and make
the blocking CI scan impossible to pass. Every pattern is a shape, never a value.

Exit code 1 and a printed report on any finding, so it drops straight into a
`git diff --cached --name-only | xargs python tools/scan_secrets.py` hook.
`--json` writes the same findings to stdout as a machine-readable list, so an
inventory can be turned into a checklist without transcription.
"""

from __future__ import annotations

import collections
import json
import os
import re
import sys
import subprocess

Finding = collections.namedtuple("Finding", "path line pattern text")

# Addresses that are obviously placeholders, not a real person's.
_ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net")

# Assembled, never a literal -- for the same reason as everything else here.
# Requiring a separator after the token (the first draft used App-Data[\\/])
# was a silent blind spot. The path is written as
#   os.path.join("~", "App" "Data", "Local", ...)
# in run.py:54, deploy/world_intel_brief.py, scripts/backfill_pool.py and
# tests/test_kb.py -- no separator follows the token, so four real leaks
# went unreported while the scan still exited clean. That is worse than
# having no scanner. The bare token is the shape that actually matters.
_APPDATA = "App" + "Data"
# Case-SENSITIVE deliberately. The folder is camelCase in every real path,
# while this module's own label and constant are not -- with IGNORECASE
# the scanner matches its own source and can never pass its self-scan.


_PATTERNS = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("appdata", re.compile(_APPDATA)),
    ("telegram", re.compile(r"telegram:\s*-?\d{6,}")),
    ("apikey", re.compile(r"\b(sk-[A-Za-z0-9]{16,}|AIza[A-Za-z0-9_-]{20,})\b")),
)

# Binary files only. Recorded-traffic and prose files are scanned deliberately:
# the author's address lives in a hand-written provenance comment in
# tests/fixtures/amd_10q.htm, in tests/fixtures/amd_companyfacts.json and in
# CONTRACT.md, so .htm, .json and .md must NOT be skipped.
_SKIP_SUFFIXES = (".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2")


def _is_allowed(pattern: str, text: str) -> bool:
    if pattern != "email":
        return False
    return any(text.lower().endswith("@" + d) or text.lower().endswith("." + d)
               for d in _ALLOWED_EMAIL_DOMAINS)


def scan_text(path: str, text: str) -> list:
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for name, rx in _PATTERNS:
            for match in rx.findall(line):
                hit = match if isinstance(match, str) else match[0]
                if _is_allowed(name, hit):
                    continue
                out.append(Finding(path, lineno, name, hit))
    return out


def scan_paths(paths) -> list:
    out = []
    for path in paths:
        if path.endswith(_SKIP_SUFFIXES) or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        out += scan_text(path, text)
    return out


def _walk(root: str):
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d not in ("__pycache__", ".git", ".pytest_cache")]
        for name in files:
            yield os.path.join(base, name)


def main(argv) -> int:
    args = list(argv[1:])
    as_json = "--json" in args
    tracked = "--tracked" in args
    paths = [a for a in args if a not in ("--json", "--tracked")]
    if tracked:
        if paths:
            print("BLOCKED: --tracked cannot be combined with paths", file=sys.stderr)
            return 2
        try:
            prefix = subprocess.check_output(
                ["git", "rev-parse", "--show-prefix"], stderr=subprocess.PIPE
            ).strip()
            if prefix:
                raise ValueError("run tracked scan from repository root")
            paths = subprocess.check_output(
                ["git", "ls-files", "-z"], stderr=subprocess.PIPE
            ).decode("utf-8").split("\0")
            paths = [p for p in paths if p]
            if not paths:
                raise ValueError("empty Git inventory")
            findings = []
            for path in paths:
                findings += scan_text(path, path)
                if os.path.islink(path) or not os.path.isfile(path):
                    raise ValueError("tracked path is not a regular file")
                if not path.endswith(_SKIP_SUFFIXES):
                    with open(path, encoding="utf-8") as fh:
                        findings += scan_text(path, fh.read())
        except (OSError, UnicodeError, ValueError, subprocess.CalledProcessError):
            print("BLOCKED: cannot completely scan tracked inventory", file=sys.stderr)
            return 2
    else:
        paths = paths or list(_walk("."))
        findings = scan_paths(paths)
    if as_json:
        print(json.dumps([f._asdict() for f in findings], indent=2))
    else:
        for f in findings:
            print(f"{f.path}:{f.line}: {f.pattern}: {f.text}", file=sys.stderr)
    if findings:
        # Worded so it cannot be skim-read as an all-clear: this branch
        # runs only when the scan FAILED.
        print(f"\nBLOCKED: {len(findings)} finding(s) must be scrubbed "
              f"before this tree can ship.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
