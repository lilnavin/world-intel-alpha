"""Scan exact changed index blobs; no staged changes is explicitly a success.

Unmerged entries, symlinks, submodules, binary/undecodable blobs, and Git errors
fail closed. Deletions have no outgoing blob. Renames scan their destination.
"""
from __future__ import annotations

import subprocess
import sys

import scan_secrets


def git(*args):
    return subprocess.run(["git", *args], check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE).stdout


def scan():
    if git("rev-parse", "--show-prefix").strip():
        raise ValueError("run from repository root")
    records = git("ls-files", "--stage", "-z").split(b"\0")
    index = {}
    for record in records:
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, oid, stage = metadata.split()
        if stage != b"0":
            raise ValueError("unmerged index")
        index[name] = (mode, oid)
    paths = git("diff", "--cached", "--no-renames", "--name-only", "--diff-filter=ACMT", "-z").split(b"\0")
    findings = []
    count = 0
    for path in paths:
        if not path:
            continue
        name = path.decode("utf-8")
        mode, oid = index[path]
        if mode not in (b"100644", b"100755"):
            raise ValueError("nonregular staged entry")
        blob = git("cat-file", "blob", oid.decode("ascii"))
        if b"\0" in blob:
            raise ValueError("binary staged entry")
        content = blob.decode("utf-8")
        findings.extend(scan_secrets.scan_text(name, name))
        findings.extend(scan_secrets.scan_text(name, content))
        count += 1
    return count, findings


def main():
    try:
        count, findings = scan()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
        print("BLOCKED: unable to completely scan staged blobs", file=sys.stderr)
        return 2
    if findings:
        # Report categories and count, not secrets or sensitive filenames.
        patterns = ", ".join(sorted({item.pattern for item in findings}))
        print(f"BLOCKED: {len(findings)} staged finding(s): {patterns}", file=sys.stderr)
        return 1
    print(f"Staged scan passed: {count} blob(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
