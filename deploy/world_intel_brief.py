"""Hermes launcher template; deployment supplies this file beside worldintel/."""
from pathlib import Path
import os
import sys


def main():
    code_root = Path(os.environ.get("WORLD_INTEL_CODE_ROOT", Path(__file__).parent)).resolve()
    if (code_root / ".deployment-incomplete").exists():
        raise SystemExit("Installation is incomplete; the briefing was not started")
    if not (code_root / "worldintel/run.py").is_file():
        raise SystemExit("Configured code root has no World Intel package")
    if not os.environ.get("WORLD_INTEL_CONFIG"):
        raise SystemExit("Set WORLD_INTEL_CONFIG to the private owner profile")
    sys.path.insert(0, str(code_root))
    from worldintel import config, run
    if Path(run.__file__).resolve() != code_root / "worldintel/run.py":
        raise SystemExit("Wrong World Intel package imported")
    config.load(os.environ["WORLD_INTEL_CONFIG"])
    if "--check" in sys.argv:
        print("Launcher package and configuration check passed; no briefing executed")
        return 0
    return run.main()


if __name__ == "__main__":
    raise SystemExit(main())
