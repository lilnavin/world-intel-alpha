"""Fail closed on core imports crossing the delivery/adapter boundary."""
from __future__ import annotations

import argparse
import ast
from importlib.util import resolve_name
from pathlib import Path


def violations(source, module):
    findings = []
    aliases = {}
    tree = ast.parse(source)
    package = module.rsplit(".", 1)[0]
    def check(name, line):
        if name == "worldintel" or name.startswith("worldintel."):
            permitted = ("worldintel.core", "worldintel.config", "worldintel.data", "worldintel.resources")
            if not any(name == item or name.startswith(item + ".") for item in permitted):
                findings.append((line, name))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                check(item.name, node.lineno)
                aliases[item.asname or item.name] = item.name
        elif isinstance(node, ast.ImportFrom):
            base = ("." * node.level) + (node.module or "")
            base = resolve_name(base, package) if node.level else base
            for item in node.names:
                name = base + "." + item.name
                check(name, node.lineno)
                aliases[item.asname or item.name] = name
    def dotted(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return dotted(node.value) + "." + node.attr
        return ""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and dotted(node.func) in ("__import__", "importlib.import_module"):
            if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                findings.append((node.lineno, "computed dynamic import"))
            else:
                name = node.args[0].value
                if name.startswith("."):
                    if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
                        findings.append((node.lineno, "unresolved relative dynamic import"))
                        continue
                    name = resolve_name(name, node.args[1].value)
                check(name, node.lineno)
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    core = args.root / "worldintel/core"
    modules = list(core.rglob("*.py"))
    if not modules:
        print("BLOCKED: no core modules found")
        return 1
    failed = False
    for path in modules:
        if path.is_symlink():
            print("BLOCKED: core symlink")
            return 1
        module = ".".join(path.relative_to(args.root).with_suffix("").parts)
        for line, name in violations(path.read_text(encoding="utf-8"), module):
            print(f"BLOCKED: {module}:{line}: {name}")
            failed = True
    if not failed:
        print(f"Core import boundary passed: {len(modules)} modules")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
