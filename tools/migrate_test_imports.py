"""Mechanically qualify existing test imports using the module move inventory."""
import json
from pathlib import Path
import prove_move_only

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {"test_cli.py", "test_install.py", "test_adapter_hermes.py", "test_architecture.py", "test_replay.py"}


def main():
    baseline = json.loads((ROOT / "move-baseline.local.json").read_bytes())
    modules = {Path(name).stem for name in baseline["files"]}
    changed = 0
    for path in sorted((ROOT / "tests").glob("*.py")):
        if path.name in EXCLUDED:
            continue
        original = path.read_bytes()
        transformed = prove_move_only.transform(original, modules)
        if transformed != original:
            path.write_bytes(transformed)
            changed += 1
    print(f"Qualified imports in {changed} test modules")


if __name__ == "__main__":
    main()
