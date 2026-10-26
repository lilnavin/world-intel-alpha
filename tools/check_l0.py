"""L0 succeeds only when both generated artifacts match frozen committed bytes."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_REFERENCES = {
    "world.schema.json": "ef543955788f9797688747a6ddd48468ac72fe6fe50a4516fd77e549fa6399eb",
    "world.md": "840d8c1d7955c22b1a8808f26dc7468af8c1668e6e99229fb5cb1a4cb03855ca",
}
_REGISTRY = (("world.schema.json", "schema_build"), ("world.md", "prompt_build"))


class L0Error(Exception):
    """Missing, corrupted, or unbuildable evidence is always a failed gate."""


def compare(generated: bytes, reference_path: str) -> list[str]:
    try:
        reference = Path(reference_path).read_bytes()
    except OSError as exc:
        raise L0Error(f"cannot read L0 reference {reference_path}: {exc}") from exc
    if generated == reference:
        return []
    offset = next((i for i, (a, b) in enumerate(zip(generated, reference)) if a != b),
                  min(len(generated), len(reference)))
    diff = [f"{Path(reference_path).name}: first difference at offset {offset}; "
            f"generated {len(generated)} bytes, reference {len(reference)} bytes"]
    diff.extend(list(difflib.unified_diff(
        reference.decode("utf-8", "replace").splitlines(),
        generated.decode("utf-8", "replace").splitlines(),
        fromfile="reference", tofile="generated", n=1, lineterm=""))[:60])
    return diff


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(_ROOT / "config/example.full.yaml"))
    args = parser.parse_args(argv)
    try:
        from worldintel import config
        cfg = config.load(args.config)
        if not _REGISTRY:
            raise L0Error("no artifacts registered; nothing was compared")
        failed = False
        for name, module in _REGISTRY:
            path = _ROOT / "tests/l0_reference" / name
            if hashlib.sha256(path.read_bytes()).hexdigest() != _REFERENCES[name]:
                raise L0Error(f"frozen c84fd11 reference changed: {name}")
            builder = importlib.import_module("worldintel.core." + module)
            if Path(builder.__file__).resolve() != (_ROOT / "worldintel/core" / (module + ".py")).resolve():
                raise L0Error("builder imported from unexpected code root")
            diff = compare(builder.build_bytes(cfg), str(path))
            if diff:
                failed = True
                print("\n".join(diff), file=sys.stderr)
            else:
                print(f"L0 ok {name}")
        return int(failed)
    except Exception as exc:
        print(f"L0 FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
