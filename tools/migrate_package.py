"""Move the committed module inventory using the exact import-only transform."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path

import prove_move_only


def migrate(root, baseline):
    root = Path(root).resolve(strict=True)
    baseline = Path(baseline).resolve(strict=True)
    snapshot = json.loads(baseline.read_bytes())
    if snapshot.get("version") != 1 or not snapshot.get("commit") or not snapshot.get("files"):
        raise ValueError("a committed, nonempty baseline is required")
    package = root / "worldintel"
    if package.exists():
        raise FileExistsError("package destination already exists")
    modules = {Path(name).stem for name in snapshot["files"]}
    prepared = []
    for name, entry in sorted(snapshot["files"].items()):
        source = root / name
        destination = root / prove_move_only.target(name)
        if (not source.is_file() or source.is_symlink()
                or source.resolve().parent != root / "scripts"
                or not destination.resolve().is_relative_to(package.resolve())):
            raise ValueError("unsafe source or destination")
        blob = base64.b64decode(entry["source_base64"], validate=True)
        if hashlib.sha256(blob).hexdigest() != entry["sha256"]:
            raise ValueError("baseline hash mismatch")
        # Git checkout conversion may alter CRLF; no unrelated source edits
        # may be discarded by migration. Output always uses committed bytes.
        if source.read_bytes().replace(b"\r\n", b"\n") != blob.replace(b"\r\n", b"\n"):
            raise ValueError(f"source has uncommitted changes: {name}")
        prepared.append((source, destination, prove_move_only.transform(blob, modules)))
    actual = {"scripts/" + path.name for path in (root / "scripts").glob("*.py")}
    if actual != set(snapshot["files"]):
        raise ValueError("working inventory differs from baseline")
    for _, destination, blob in prepared:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(blob)
    for name, blob in prove_move_only.INITIALIZERS.items():
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(blob)
    result = prove_move_only.verify(snapshot, package)
    if not result["move_only"]:
        raise ValueError("verification failed; original modules retained")
    # Every target was resolved and checked above; only the explicit source
    # inventory is removed, after the replacement files pass byte verification.
    for source, _, _ in prepared:
        source.unlink()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(migrate(args.root, args.baseline), indent=2))


if __name__ == "__main__":
    main()
