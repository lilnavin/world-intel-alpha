"""Read-only public-export preflight; never initialize or publish a repository.

Only named source assets below are eligible. New files require review and an
allowlist edit. Reports belong outside both trees and omit matched values.
Exact caller-approved email/delivery substitutions are optional; filesystem
defaults require a separately reviewed structural change, never token scrubbing.
A clean scanner result is a preflight, not proof of publication readiness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import stat
from pathlib import Path, PurePosixPath

import scan_secrets


_SCRIPTS = """anomaly_panel backfill_pool capsule codex_engine compose compose_html
earnings_report enrich feeds fetch_sources filing_index kb metrics ownership
post_earnings_pool quality_gate reconcile run sec_filing_text sec_fundamentals
select_news source_diversity""".split()
_TESTS = """anomaly_panel capsule codex_engine compose_html earnings_report enrich
feeds fetch filing_index kb lint_plan metrics ownership post_earnings_pool
quality_gate reconcile run scan_secrets schema_guidance sec_filing_text
sec_fundamentals select source_diversity prepare_public""".split()
_FIXTURES = """aapl_10q_segments.txt amd_10q.htm amd_companyfacts.json
amzn_10q_segments.txt daily_index.idx daily_index_periodic.idx edgar.json
federal_register.json form4_buy.xml form4_derivative.xml form4_grant.xml
form4_multi_txn.xml form4_planned_sale.xml fred.json gdelt_artlist.json
gdelt_timelinetone.json guardian.json marketaux.json meta_10q_segments.txt
msft_10k_segments.txt newsdata.json rss_atom.xml rss_bbc.xml rss_dw.rdf
schedule13d_activist_amend.xml schedule13d_new.xml twelvedata_error.json
twelvedata_quote.json""".split()
ALLOWLIST = frozenset(
    [f"scripts/{name}.py" for name in _SCRIPTS]
    + [f"tests/test_{name}.py" for name in _TESTS]
    + [f"tests/fixtures/{name}" for name in _FIXTURES]
    + [f"schema/{name}.schema.json" for name in
       ("briefing", "finance", "rewrite", "world")]
    + [f"scripts/prompts/{name}.md" for name in ("analysis", "finance", "world")]
    + ["tools/scan_secrets.py", "tools/lint_plan.py", "tools/prepare_public.py"]
)
# Docs and deployment scripts contain integration-specific instructions and
# require independent review. Runtime data, private configs and caches are absent.


class PreparationError(ValueError):
    pass


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _no_links(path: Path) -> None:
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        # Python 3.11 lacks Path.is_junction. Reject all Windows reparse points.
        reparse = getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(info.st_mode) or reparse:
            raise PreparationError("Symlinks and junctions are not permitted")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def preflight(source, substitutions=None, *, allowlist=ALLOWLIST):
    """Return redacted deterministic inventory and bytes; source is never edited.

    The allowlist argument supports tests and a separately reviewed custom list.
    Missing named files are allowed so the utility works on partial source trees.
    """
    root = Path(source).absolute()
    _no_links(root)
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise PreparationError("Source must be a directory")
    replacements = substitutions or {}
    if not isinstance(replacements, dict) or any(
        not isinstance(k, str) or not k or not isinstance(v, str)
        for k, v in replacements.items()
    ):
        raise PreparationError("Substitutions must map nonempty strings to strings")
    blobs = {}
    raw = {}
    known = set()
    for relative in sorted(allowlist):
        rel = PurePosixPath(relative)
        if rel.is_absolute() or ".." in rel.parts or "\\" in relative or ":" in relative:
            raise PreparationError("Invalid allowlist path")
        path = root.joinpath(*rel.parts)
        _no_links(path)
        if not path.exists():
            continue
        if not path.is_file() or not _within(path.resolve(), root):
            raise PreparationError("Allowlist entry is not a regular source file")
        data = path.read_bytes()
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PreparationError("Allowlisted files must be UTF-8 text") from exc
        raw[relative] = data
        blobs[relative] = content
        known.update(f.text for f in scan_secrets.scan_text(relative, content)
                     if f.pattern in ("email", "telegram"))
    if not blobs:
        raise PreparationError("No allowlisted source files found")
    if set(replacements) - known:
        raise PreparationError("Substitution key is not an exact detected email/delivery value")
    files, findings, output = [], [], {}
    for relative, content in blobs.items():
        # One simultaneous replacement pass: replacement values cannot cascade.
        if replacements:
            import re
            pattern = re.compile("|".join(re.escape(k) for k in
                                         sorted(replacements, key=lambda k: (-len(k), k))))
            content = pattern.sub(lambda m: replacements[m.group()], content)
        data = content.encode("utf-8")
        output[relative] = data
        for f in scan_secrets.scan_text(relative, relative) + scan_secrets.scan_text(relative, content):
            findings.append({"path": relative, "line": f.line, "pattern": f.pattern})
        files.append({"path": relative, "source_sha256": _hash(raw[relative]),
                      "export_sha256": _hash(data), "bytes": len(data)})
    return {"format_version": 1, "status": "blocked" if findings else "preflight-clean",
            "files": files, "findings": findings}, output


def prepare(source, report, destination=None, substitutions=None, *, allowlist=ALLOWLIST):
    """Write an external report and optionally copy a clean tree to a new path."""
    root = Path(source).absolute()
    report_path = Path(report).absolute()
    _no_links(root)
    _no_links(report_path)
    root = root.resolve(strict=True)
    report_path = report_path.resolve()
    if _within(report_path, root):
        raise PreparationError("Report must be outside source")
    if report_path.exists():
        raise PreparationError("Report already exists")
    dest = None
    if destination is not None:
        dest = Path(destination).absolute()
        _no_links(dest)
        if dest.exists():
            raise PreparationError("Destination must be new")
        dest = dest.resolve()
        if _within(dest, root) or _within(root, dest):
            raise PreparationError("Source and destination overlap")
        if _within(report_path, dest):
            raise PreparationError("Report must be outside destination")
        if not dest.parent.is_dir():
            raise PreparationError("Destination parent must exist")
    inventory, output = preflight(root, substitutions, allowlist=allowlist)
    with report_path.open("x", encoding="utf-8", newline="\n") as fh:
        json.dump(inventory, fh, indent=2, sort_keys=True)
        fh.write("\n")
    if inventory["findings"]:
        raise PreparationError("Privacy preflight blocked; see redacted external report")
    if dest is not None:
        dest.mkdir()  # exclusive creation, never reuse an existing staging tree
        for relative, data in output.items():
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as fh:
                fh.write(data)
    return inventory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("--report", required=True)
    parser.add_argument("--destination")
    parser.add_argument("--substitutions", help="private JSON mapping of approved exact replacements")
    args = parser.parse_args(argv)
    try:
        mapping = None
        if args.substitutions:
            mapping = json.loads(Path(args.substitutions).read_text(encoding="utf-8"))
        prepare(args.source, args.report, args.destination, mapping)
    except (OSError, ValueError) as exc:
        print(f"BLOCKED: {exc}")
        return 1
    print("Preflight clean; publication and equivalence gates still apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
