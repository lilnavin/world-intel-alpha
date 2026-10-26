"""Stage a verified package into a NEW destination; never overwrite a live tree.

There is deliberately no live default destination. --scratch permits a dirty
source only for installation testing and records that fact in provenance.
Owner cutover/rollback and scheduler changes are separate approval-gated work.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import stat


ROOT = Path(__file__).resolve().parents[1]


def provenance(root):
    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    return git("rev-parse", "HEAD"), bool(git("status", "--porcelain"))


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True).stdout


def _runtime_path(name):
    path = Path(name)
    if name == "deploy/world_intel_brief.py":
        return True
    if not path.parts or path.parts[0] != "worldintel" or any(part.startswith(".") for part in path.parts):
        return False
    return (path.suffix == ".py" and "__pycache__" not in path.parts) or (
        len(path.parts) > 2 and path.parts[1] == "data" and
        path.suffix in (".json", ".yaml", ".md", ".template"))


def _inventory(root, commit, scratch):
    """Freeze tracked runtime blobs; scratch may add nonignored runtime files."""
    tracked = {}
    listing = _git(root, "ls-tree", "-rz", "--full-tree", commit, "--", "worldintel", "deploy/world_intel_brief.py")
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        meta, name = entry.split(b"\t", 1)
        mode, kind, oid = meta.split()
        name = name.decode("utf-8")
        if _runtime_path(name):
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                raise ValueError("runtime inventory contains a nonregular Git entry")
            tracked[name] = oid.decode("ascii")
    if scratch:
        untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", "worldintel", "deploy/world_intel_brief.py")
        for name in untracked.decode("utf-8").split("\0"):
            if name and _runtime_path(name):
                tracked.setdefault(name, None)
    if not {"worldintel/run.py", "deploy/world_intel_brief.py"} <= tracked.keys():
        raise ValueError("package or launcher is missing from runtime inventory")
    frozen, changed = {}, False
    for name, oid in sorted(tracked.items()):
        source = root / name
        if any(parent.is_symlink() for parent in [source, *source.parents] if parent != root):
            raise ValueError("runtime symlink traversal is forbidden")
        if not stat.S_ISREG(source.stat().st_mode):
            raise ValueError("runtime source is not a regular file")
        blob = source.read_bytes()
        altered = oid is None or blob != _git(root, "cat-file", "blob", oid)
        if altered and not scratch:
            raise ValueError("runtime worktree bytes differ from the committed source")
        changed |= altered
        target = "world_intel_brief.py" if name.startswith("deploy/") else name
        frozen[target] = blob
    return frozen, changed


def deploy(root, destination, *, scratch=False):
    root = Path(root).resolve(strict=True)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("destination must be new; existing installation was not touched")
    destination = destination.resolve()
    if destination == Path(destination.anchor) or destination == Path.home().resolve():
        raise ValueError("unsafe deployment destination")
    package = root / "worldintel"
    if destination == root or destination.is_relative_to(package):
        raise ValueError("deployment would overlap source")
    commit, dirty = provenance(root)
    if dirty and not scratch:
        raise ValueError("deployment requires committed clean source; use --scratch only for testing")
    files, changed = _inventory(root, commit, scratch)
    dirty = dirty or changed
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wi-stage-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "installation"
        stage.mkdir()
        hashes = {}
        for relative, blob in files.items():
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            expected = hashlib.sha256(blob).hexdigest()
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ValueError("copied package failed verification")
            hashes[relative] = expected
        stamp = {"commit": commit, "dirty": dirty, "scratch": scratch,
                 "deployed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 "files": hashes}
        # mkdir is an exclusive reservation on both Windows and POSIX. rename
        # can replace an empty directory on POSIX, even after an exists check.
        destination.mkdir(exist_ok=False)
        marker = destination / ".deployment-incomplete"
        marker.write_text("Installation incomplete; do not execute this directory.\n", encoding="utf-8")
        for relative, expected in hashes.items():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write((stage / relative).read_bytes())
            if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise ValueError("installed package failed verification")
        # Mark completion only after every executable and asset has verified.
        with (destination / "worldintel/.provenance.json").open("x", encoding="utf-8") as fh:
            json.dump(stamp, fh, indent=2)
        marker.unlink()
    return stamp


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--scratch", action="store_true")
    args = parser.parse_args(argv)
    try:
        stamp = deploy(ROOT, args.dest, scratch=args.scratch)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Deployment failed; no existing destination was replaced")
        if (args.dest / ".deployment-incomplete").exists():
            print("The new destination is marked incomplete and must not be executed")
        return 1
    print(f"New installation verified: {len(stamp['files'])} files; scratch={stamp['scratch']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
