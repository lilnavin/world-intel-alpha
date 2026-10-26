# Contributing

Use Python 3.11 or 3.12. In an isolated environment:

```text
python -m pip install --require-hashes -r requirements.lock
python -m pip install --no-deps --no-build-isolation .
python -m pytest -q
python tools/check_architecture.py
python tools/check_l0.py
python tools/scan_secrets.py --tracked
```

The lock includes runtime, development and build dependencies for the supported
Windows/Linux matrix. Regenerate it deliberately with `uv pip compile
requirements-dev.txt requirements.txt requirements-build.txt --universal
--python-version 3.11 --generate-hashes --no-header --output-file requirements.lock`.
Review changed pins and rerun clean installation checks.

Install the optional staged-content guard with `git config core.hooksPath tools`.
It scans index blobs, not just working files, and fails closed on unsupported
binary/symlink entries. CI also scans tracked files. Neither replaces history
review. Never stage private profiles, credentials or replay captures.

Behavior changes require tests with non-default profiles, not only the reference
profile. Preserve exact prompt/schema bytes unless normalization is explicitly
approved. Private replay input stays outside Git. The core may import core/config
and data helpers, never scheduling or delivery adapters.

The full runtime has deliberately graceful failure paths. A successful process
exit alone does not prove the brief succeeded: inspect its recorded outcome and
evidence. Tests must exercise the lane rather than bypass the already-ran guard.
