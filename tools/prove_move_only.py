"""Prove a package migration by comparing an exact mechanical source transform.

Only local import statement spans may change. Every other byte, including
indentation, comments, blank lines, bootstrap code and resource paths, survives.
Snapshots can be read directly from a committed Git tree with --commit.
No module is imported or executed by this tool.
"""
from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import json
from pathlib import Path
import subprocess


TOP = frozenset({"run", "config", "delivery"})
INITIALIZERS = {
    "worldintel/__init__.py": b'"""World Intelligence package."""\n',
    "worldintel/core/__init__.py": b'"""World Intelligence domain modules."""\n',
    "worldintel/adapters/__init__.py": b'"""World Intelligence delivery adapters."""\n',
}


def target(path):
    path = Path(path)
    if path.parent.as_posix() != "scripts" or path.suffix != ".py" or path.name == "__init__.py":
        raise ValueError("baseline requires flat scripts/*.py module paths")
    return "worldintel/" + ("" if path.stem in TOP else "core/") + path.name


def canonical_module(name, modules):
    if name in modules:
        return "worldintel." + ("" if name in TOP else "core.") + name
    return name


def transform(blob, modules):
    """Replace only exact AST import spans, preserving all intervening bytes."""
    text = blob.decode("utf-8")
    tree = ast.parse(text)
    lines = blob.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    replacements = []
    for node in ast.walk(tree):
        replacement = None
        if isinstance(node, ast.Import) and any(alias.name in modules for alias in node.names):
            statements = []
            for alias in node.names:
                suffix = " as " + alias.asname if alias.asname else ""
                if alias.name in modules:
                    namespace, name = canonical_module(alias.name, modules).rsplit(".", 1)
                    statements.append(f"from {namespace} import {name}{suffix}")
                else:
                    statements.append(f"import {alias.name}{suffix}")
            replacement = "; ".join(statements)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in modules:
            names = ", ".join(alias.name + (" as " + alias.asname if alias.asname else "")
                              for alias in node.names)
            replacement = f"from {canonical_module(node.module, modules)} import {names}"
        if replacement is not None:
            start = offsets[node.lineno - 1] + node.col_offset
            end = offsets[node.end_lineno - 1] + node.end_col_offset
            original = blob[start:end]
            # Comments inside a multiline import are nonimport bytes. Refuse
            # to silently discard them; choose a reviewed manual extension.
            if b"#" in original:
                raise ValueError("comment inside rewritten import requires explicit transform support")
            replacements.append((start, end, replacement.encode("utf-8")))
    for start, end, replacement in sorted(replacements, reverse=True):
        blob = blob[:start] + replacement + blob[end:]
    ast.parse(blob.decode("utf-8"))
    return blob


def import_manifest(blob):
    """AST field/index chains distinguish try body, handlers and import order."""
    rows = []
    def walk(node, chain):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            rows.append({"position": chain, "statement": ast.dump(node, include_attributes=False)})
        for field, value in ast.iter_fields(node):
            if isinstance(value, list):
                for index, child in enumerate(value):
                    if isinstance(child, ast.AST):
                        walk(child, chain + [f"{type(node).__name__}.{field}[{index}]"])
            elif isinstance(value, ast.AST):
                walk(value, chain + [f"{type(node).__name__}.{field}"])
    walk(ast.parse(blob.decode("utf-8")), [])
    return rows


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def record(scripts, commit=None):
    if Path(scripts).is_symlink():
        raise ValueError("record requires a regular scripts directory")
    scripts = Path(scripts).resolve(strict=True)
    if scripts.name != "scripts" or scripts.is_symlink():
        raise ValueError("record requires the scripts directory")
    files = {}
    revision = None
    if commit is not None:
        root = _git(scripts, "rev-parse", "--show-toplevel").decode().strip()
        revision = _git(root, "rev-parse", "--verify", commit + "^{commit}").decode().strip()
        records = _git(root, "ls-tree", "-r", "-z", revision, "--", "scripts").split(b"\0")
        for record in records:
            if not record:
                continue
            metadata, name = record.split(b"\t", 1)
            path = name.decode("utf-8")
            if path.endswith(".py"):
                mode, kind, oid = metadata.split()
                if mode not in (b"100644", b"100755") or kind != b"blob":
                    raise ValueError("baseline contains a nonregular module")
                target(path)
                files[path] = _git(root, "cat-file", "blob", oid.decode("ascii"))
    else:
        for path in sorted(scripts.rglob("*.py")):
            if path.is_symlink() or not path.is_file():
                raise ValueError("baseline contains a nonregular module")
            rel = "scripts/" + path.relative_to(scripts).as_posix()
            target(rel)
            files[rel] = path.read_bytes()
    if not files:
        raise ValueError("empty baseline inventory")
    modules = {Path(path).stem for path in files}
    for blob in files.values():
        transform(blob, modules)
    return {"version": 1, "commit": revision, "files": {
        path: {"sha256": hashlib.sha256(blob).hexdigest(),
               "source_base64": base64.b64encode(blob).decode("ascii")}
        for path, blob in sorted(files.items())}}


def verify(snapshot, package):
    if snapshot.get("version") != 1 or not isinstance(snapshot.get("files"), dict) or not snapshot["files"]:
        raise ValueError("invalid baseline")
    package = Path(package)
    if package.name != "worldintel" or not package.is_dir() or package.is_symlink():
        raise ValueError("verify requires the worldintel package directory")
    sources = {}
    for path, entry in snapshot["files"].items():
        destination = target(path)
        if destination in sources:
            raise ValueError("duplicate destination")
        blob = base64.b64decode(entry["source_base64"], validate=True)
        if hashlib.sha256(blob).hexdigest() != entry["sha256"]:
            raise ValueError("baseline integrity mismatch")
        sources[destination] = blob
    modules = {Path(path).stem for path in snapshot["files"]}
    expected = {path: transform(blob, modules) for path, blob in sources.items()}
    actual = {}
    for path in package.rglob("*.py"):
        if path.is_symlink() or not path.is_file() or any(p.is_symlink() for p in path.parents if p != package.parent):
            raise ValueError("package contains a nonregular module")
        actual["worldintel/" + path.relative_to(package).as_posix()] = path.read_bytes()
    issues = []
    for path in sorted(set(expected) - set(actual)):
        issues.append({"path": path, "error": "missing module"})
    for path in sorted(set(actual) - set(expected)):
        if path not in INITIALIZERS or actual[path] not in (b"", INITIALIZERS[path]):
            issues.append({"path": path, "error": "unexpected module or initializer contents"})
    for path in sorted(set(expected) & set(actual)):
        if expected[path] != actual[path]:
            try:
                imports_match = import_manifest(expected[path]) == import_manifest(actual[path])
            except (SyntaxError, UnicodeError):
                imports_match = False
            issues.append({"path": path, "error": "nonimport bytes changed" if imports_match else "import identity, scope or position changed"})
    return {"move_only": not issues, "modules": len(expected), "baseline_commit": snapshot.get("commit"), "issues": issues}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--record", type=Path)
    action.add_argument("--verify", type=Path)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--commit", help="Read record inputs from this immutable commit, not the working tree")
    args = parser.parse_args(argv)
    try:
        if args.record:
            snapshot = record(args.record, args.commit)
            # Refuse accidental replacement of the evidence being verified.
            with args.baseline.open("x", encoding="utf-8") as stream:
                json.dump(snapshot, stream, indent=2)
            print(json.dumps({"recorded": len(snapshot["files"]), "commit": snapshot["commit"]}))
            return 0
        if args.commit:
            raise ValueError("--commit requires --record")
        result = verify(json.loads(args.baseline.read_bytes()), args.verify)
        print(json.dumps(result, indent=2))
        return 0 if result["move_only"] else 1
    except (OSError, ValueError, KeyError, TypeError, SyntaxError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"move_only": False, "error": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
