"""Standalone commands. main(argv) accepts arguments without a program name."""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import shutil
import sys

VERBS = ("init", "validate", "demo", "doctor", "run")
_RUNTIME_DEPS = ("requests", "yaml", "bs4", "yfinance", "lxml")


def _parser():
    parser = argparse.ArgumentParser(prog="world-intel", description="A configurable daily intelligence briefing.")
    sub = parser.add_subparsers(dest="verb")
    init = sub.add_parser("init", help="copy a starter profile outside repositories")
    init.add_argument("--profile", choices=("minimal", "world", "full"), default="minimal")
    init.add_argument("--out", default="worldintel.yaml", help="new configuration file (never overwritten)")
    validate = sub.add_parser("validate", help="validate configuration without side effects")
    validate.add_argument("--config")
    validate.add_argument("--strict", action="store_true", help="treat unknown-key warnings as errors")
    validate.add_argument("--explain", action="store_true", help="show recognized, defaulted and ignored key names")
    demo = sub.add_parser("demo", help="render invented sample data fully offline")
    demo.add_argument("--out", default=".", help="output directory")
    doctor = sub.add_parser("doctor", help="check dependencies and local configuration")
    doctor.add_argument("--config")
    run = sub.add_parser("run", help="run the live pipeline using the selected analysis engine")
    run.add_argument("--config")
    run.add_argument("--dry", action="store_true", help="temporary pipeline outputs; still uses network and the selected analysis engine")
    run.add_argument("--force", action="store_true")
    run.add_argument("--concurrent-passes", action="store_true")
    run.add_argument("--offline", action="store_true", help="unsupported for a live run; use demo instead")
    return parser


def _init(args):
    from worldintel import data
    dest = Path(args.out).absolute()
    if any((parent / ".git").exists() for parent in dest.resolve().parents):
        raise ValueError("configuration belongs outside Git repositories; choose --out outside the checkout")
    blob = data.read_bytes(f"profiles/example.{args.profile}.yaml")
    # O_EXCL closes the exists-check race and protects even dangling symlinks.
    with dest.open("xb") as fh:
        fh.write(blob)
    print(f"Created {dest}")
    if args.profile == "full":
        print("Replace the synthetic SEC contact with your own before a live run.")
    print(f'Next: world-intel validate --config "{dest}" --strict')
    return 0


def _validate(args):
    from worldintel import config
    cfg = config.load(args.config)
    print(f"Valid configuration: {cfg.source_path}")
    for warning in cfg.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if args.explain:
        print(json.dumps(config.explain(cfg), indent=2))
    return 1 if args.strict and cfg.warnings else 0


def _demo(args):
    from worldintel import data
    from worldintel.core import compose_html
    analysis = json.loads(data.read_text("examples/sample-analysis.json"))
    bundle = json.loads(data.read_text("examples/sample-bundle.json"))
    profile = json.loads(data.read_text("examples/demo-profile.json"))
    html = compose_html.render_html(analysis, bundle, bundle["date"],
                                    desks=profile["desks"], timezone_label="UTC")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    dest = out / "world-intel-demo.html"
    with dest.open("x", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    print(f"Created {dest}")
    print("All sample stories and figures are invented; this is not a real briefing.")
    return 0


def _doctor(args):
    ok = sys.version_info >= (3, 11)
    print(f"python: {sys.version.split()[0]} ({'ok' if ok else 'requires 3.11 or newer'})")
    deps = _RUNTIME_DEPS + (("tzdata",) if sys.platform == "win32" else ())
    for name in deps:
        try:
            mod = importlib.import_module(name)
            print(f"dependency {name}: available ({getattr(mod, '__version__', 'version unknown')})")
        except Exception:
            print(f"dependency {name}: MISSING or cannot import; reinstall world-intel")
            ok = False
    try:
        from worldintel import config
        cfg = config.load(args.config)
        print(f"configuration: valid ({cfg.source_path})")
        from worldintel.core import analysis_engine
        engine = analysis_engine.select(cfg)
        status = engine.local_status()
        print(f"Analysis engine {engine.engine_id}: {status['detail']}"
              + ("" if status["available"] else " (NOT FOUND or unavailable)"))
        print("Engine authentication: not checked (doctor makes no model call)")
        ok = ok and status["available"]
        for warning in cfg.warnings:
            print(f"configuration warning: {warning}")
    except Exception as exc:
        print(f"configuration: unavailable ({type(exc).__name__})")
        ok = False
    print("Local checks passed; live authentication remains unchecked." if ok else "One or more local checks failed.")
    return 0 if ok else 1


def _run(args):
    if args.offline:
        raise ValueError("run --offline is not supported: live analysis needs network and the selected analysis engine; use world-intel demo")
    from worldintel import config
    from worldintel import run
    cfg = config.load(args.config)
    argv = ["world-intel-run"]
    for name in ("dry", "force", "concurrent_passes"):
        if getattr(args, name):
            argv.append("--" + name.replace("_", "-"))
    old_argv, old_cache = sys.argv, config._CACHE
    old_env = os.environ.get("WORLD_INTEL_CONFIG")
    try:
        sys.argv = argv
        os.environ["WORLD_INTEL_CONFIG"] = cfg.source_path
        config.set_for_test(cfg)
        return run.main()
    finally:
        sys.argv = old_argv
        config.set_for_test(old_cache)
        if old_env is None:
            os.environ.pop("WORLD_INTEL_CONFIG", None)
        else:
            os.environ["WORLD_INTEL_CONFIG"] = old_env


def main(argv=None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if args.verb is None:
        parser.print_usage(sys.stderr)
        return 2
    try:
        return {"init": _init, "validate": _validate, "demo": _demo,
                "doctor": _doctor, "run": _run}[args.verb](args)
    except Exception as exc:
        print(f"{args.verb}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
