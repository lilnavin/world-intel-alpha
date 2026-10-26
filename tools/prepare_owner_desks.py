"""Prepare a separate desk-enabled profile; never overwrite or print owner data."""
from __future__ import annotations

import argparse
import copy
import os
from pathlib import Path
import sys

import yaml

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
from worldintel import config  # noqa: E402


def prepare(input_path, desks_from, output_path):
    """Add only absent desks and exclusively create a validated external file."""
    output = Path(output_path).resolve()
    if output.is_relative_to(_ROOT) or any((parent / ".git").exists() for parent in output.parents):
        raise ValueError("output must be outside repository trees")
    if output.exists():
        raise FileExistsError("output already exists")
    source = config.load(input_path)
    if "desks" in source.raw:
        raise ValueError("input already declares desks")
    template = config.load(desks_from)
    if not template.desks:
        raise ValueError("desk template has no desks")
    merged = copy.deepcopy(source.raw)
    merged["desks"] = copy.deepcopy(template.raw["desks"])
    config.Config(merged, str(output))
    blob = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True, width=100).encode("utf-8")
    # Ensure serialization did not change any non-desk value (including paths,
    # identity, sources, tuning, and credentials if the input contains them).
    roundtrip = yaml.safe_load(blob)
    restored = dict(roundtrip)
    restored.pop("desks")
    if restored != source.raw:
        raise ValueError("serialization changed non-desk configuration")
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--desks-from", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        prepare(args.input, args.desks_from, args.output)
    except Exception as exc:
        # Parser/validation errors can embed private values or paths. Never
        # render their messages, source YAML, or tracebacks to the terminal.
        print(f"Preparation failed ({type(exc).__name__}); existing files were not overwritten.", file=sys.stderr)
        return 1
    print("Prepared a new validated profile; the input is unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
