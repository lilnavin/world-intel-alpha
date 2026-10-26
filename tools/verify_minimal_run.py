"""Attest a live RSS-only dry run; exit zero alone is never evidence of success."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

import yaml

PREFIX = "WORLD_INTEL_DRY_SUMMARY:"
ROOT = Path(__file__).resolve().parents[1]
API_KEYS = ("NEWSDATA_API_KEY", "GUARDIAN_API_KEY", "MARKETAUX_API_KEY",
            "TWELVEDATA_API_KEY", "FRED_API_KEY", "FINNHUB_API_KEY")


def validate_result(code, stdout, stderr, commit, root):
    errors = []
    if code != 0:
        errors.append("pipeline exit was nonzero")
    if stdout.strip() or "MEDIA:" in stderr:
        errors.append("unexpected stdout or delivery handoff")
    lines = [line[len(PREFIX):] for line in stderr.splitlines() if line.startswith(PREFIX)]
    if len(lines) != 1:
        return errors + ["exactly one dry summary required"]
    try:
        item = json.loads(lines[0])
        if (type(item.get("schema_version")) is not int or item["schema_version"] != 1 or item.get("dry") is not True or
                item.get("source_dirty") is not False or
                not re.fullmatch(r"[0-9a-f]{40}", item.get("source_commit", "")) or
                commit != item["source_commit"] or
                Path(item["source_file"]).resolve() != (root / "worldintel/run.py").resolve()):
            errors.append("unbound or dirty pipeline source")
        if (item.get("generation") != "rendered" or item.get("handoff") != "disabled" or
                item.get("delivery_confirmed") is not False):
            errors.append("dry rendering or delivery invariant failed")
        analysis = item["analysis"]
        if analysis["overall"] != "complete" or set(analysis["passes"]) != {"world", "finance"}:
            errors.append("incomplete analysis")
        for name in ("world", "finance"):
            record = analysis["passes"][name]
            if (record.get("success") is not True or record.get("skipped") is not False or
                    record.get("timed_out") is not False or record.get("error") is not None):
                errors.append(f"unsuccessful {name} pass")
        quality = item.get('quality', {})
        if (quality.get('overall') != 'complete' or
                type(quality.get('unresolved_coverage_events')) is not int or
                quality['unresolved_coverage_events'] != 0 or
                quality.get('scope') != 'recognized registered events; not complete major-event recall'):
            errors.append('incomplete or unattested registered-event quality')
        review = item.get('source_review', {})
        if (review.get('status') != 'accepted' or review.get('attempted') is not True or
                type(review.get('cards_reviewed')) is not int or review['cards_reviewed'] <= 0 or
                type(review.get('repair_attempted')) is not bool or
                type(review.get('review_calls')) is not int or
                type(review.get('corrected_cards')) is not int or
                review.get('review_calls') != (2 if review.get('repair_attempted') else 1) or
                (not 1 <= review['corrected_cards'] <= review['cards_reviewed']
                 if review.get('repair_attempted') else review['corrected_cards'] != 0)):
            errors.append('source review not accepted')
        evidence = item.get('model_evidence', {})
        records = evidence.get('invocations', [])
        if (evidence.get('complete') is not True or not isinstance(records, list) or not records or
                not re.fullmatch(r'[0-9a-f]{32}', evidence.get('run_id', '')) or
                not {'world', 'finance', 'source-review'} <= {r.get('pass_name') for r in records} or
                any(r.get('outcome') != 'success' for r in records)):
            errors.append('incomplete private model evidence')
        names = [r.get('pass_name') for r in records]
        required_review = ['source-review', 'source-correction', 'source-review-final'] if review.get('repair_attempted') else ['source-review']
        if ([n for n in names if n in {'source-review', 'source-correction', 'source-review-final'}] != required_review
                or names.count('world') != 1 or names.count('finance') != 1):
            errors.append('inconsistent source review invocation inventory')
        rewrite = item["rewrite"]
        if (type(rewrite["attempted"]) is not bool or rewrite["skipped"] is not False or
                rewrite["error"] is not None or type(rewrite["remaining"]) is not int or
                rewrite["remaining"] != 0):
            errors.append("degraded writing gate")
        rendered = item["rendered"]
        if (any(type(rendered.get(key)) is not int or rendered[key] <= 0
                for key in ("chars", "text_chars", "world_cards", "input_news")) or
                not re.fullmatch(r"[0-9a-f]{64}", rendered.get("sha256", ""))):
            errors.append("empty or unattested briefing")
    except (KeyError, TypeError, ValueError, AttributeError):
        errors.append("malformed dry summary")
    return errors


def validate_model_evidence(directory, summary, review=None):
    """Verify retained private bytes against the run summary and each manifest."""
    try:
        directory = Path(directory).resolve(strict=True)
        if any((parent / '.git').exists() for parent in (directory, *directory.parents)):
            return ['private model evidence is inside a repository']
        expected = set()
        records = summary['invocations']
        seen = set()
        for record in records:
            if set(record) != {'invocation_id', 'pass_name', 'outcome', 'manifest_sha256'}:
                raise ValueError
            iid = record['invocation_id']
            if not re.fullmatch(r'[0-9a-f]{32}', iid) or iid in seen:
                raise ValueError
            seen.add(iid)
            invocation = directory / iid
            path = invocation / 'manifest.json'
            expected.add(path.relative_to(directory).as_posix())
            blob = path.read_bytes()
            if hashlib.sha256(blob).hexdigest() != record['manifest_sha256']:
                raise ValueError
            manifest = json.loads(blob)
            if (set(manifest) != {'schema_version', 'invocation_id', 'pass_name', 'outcome',
                                  'returncode', 'timeout_s', 'files'} or
                    type(manifest['schema_version']) is not int or manifest['schema_version'] != 1 or
                    any(manifest[k] != record[k] for k in ('invocation_id', 'pass_name', 'outcome')) or
                    type(manifest['returncode']) is not int or manifest['returncode'] != 0 or
                    set(manifest['files']) != {'stdin.bin', 'schema.bin', 'output.bin', 'stdout.bin', 'stderr.bin'}):
                raise ValueError
            for filename, entry in manifest['files'].items():
                if set(entry) != {'sha256', 'bytes'} or type(entry['bytes']) is not int:
                    raise ValueError
                path = invocation / filename
                expected.add(path.relative_to(directory).as_posix())
                blob = path.read_bytes()
                if len(blob) != entry['bytes'] or hashlib.sha256(blob).hexdigest() != entry['sha256']:
                    raise ValueError
        paths = list(directory.rglob('*'))
        if (not records or any(p.is_symlink() for p in paths) or
                {p.relative_to(directory).as_posix() for p in paths if p.is_file()} != expected):
            raise ValueError
        if review is not None:
            _validate_review_evidence(directory, records, review)
        return []
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return ['private model evidence integrity failed']


def _validate_review_evidence(directory, records, audit):
    """Bind the captured one- or three-call chain to the actual final material."""
    from worldintel import data
    from worldintel.core import grounding_review as g
    import copy
    repair = audit['repair_attempted']
    names = ['source-review', 'source-correction', 'source-review-final'] if repair else ['source-review']
    selected = [r for r in records if r['pass_name'] in {'source-review', 'source-correction', 'source-review-final'}]
    if ([r['pass_name'] for r in selected] != names or type(repair) is not bool
            or audit['review_calls'] != (2 if repair else 1) or type(audit['review_calls']) is not int
            or type(audit['corrected_cards']) is not int or audit['status'] != 'accepted'
            or audit['attempted'] is not True or audit['scope'] != g.SCOPE or audit['reason'] is not None
            or type(audit['cards_reviewed']) is not int or audit['cards_reviewed'] <= 0):
        raise ValueError('invalid accepted review audit')
    captures = []
    for record in selected:
        folder = directory / record['invocation_id']
        manifest = json.loads((folder / 'manifest.json').read_bytes())
        timeout = manifest['timeout_s']
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise ValueError('invalid review timeout')
        name = 'source-correction' if record['pass_name'] == 'source-correction' else 'source-review'
        raw_prompt = (folder / 'stdin.bin').read_bytes()
        prompt = raw_prompt.decode('utf-8').replace('\r\n', '\n')
        material = json.loads(prompt.split('\nINPUT_JSON\n', 1)[1])
        with data.as_file(f'schema/{name}.schema.json') as schema:
            if (folder / 'schema.bin').read_bytes() != Path(schema).read_bytes():
                raise ValueError('review schema mismatch')
        expected_prompt = g._prompt(name, material)
        if raw_prompt not in (expected_prompt.encode('utf-8'), expected_prompt.replace('\n', '\r\n').encode('utf-8')):
            raise ValueError('review prompt mismatch')
        captures.append((material, json.loads((folder / 'output.bin').read_bytes()), timeout))
    material, initial, _ = captures[0]
    # Recheck hashes against actual captured prose/documents before using feedback.
    if (set(material) != {'cards', 'sources'} or len(material['cards']) != audit['cards_reviewed']
            or not 1 <= len(material['cards']) <= 24 or not 1 <= len(material['sources']) <= 48):
        raise ValueError('review material mismatch')
    original = {'briefing': {}}
    source_ids = set()
    for source in material['sources']:
        if (set(source) != {'source_id', 'sha256', 'url', 'title', 'content'} or source['source_id'] in source_ids
                or source['sha256'] != g._digest(source['content'])):
            raise ValueError('source digest mismatch')
        source_ids.add(source['source_id'])
    used_sources = set()
    for c in material['cards']:
        if (set(c) != {'card_id', 'prose_sha256', 'source_ids', *g.FIELDS}
                or c['prose_sha256'] != g._digest(json.dumps({f: c[f] for f in g.FIELDS}, ensure_ascii=False, sort_keys=True))
                or any(not isinstance(c[f], str) or not c[f].strip() for f in g.FIELDS)
                or not c['source_ids'] or len(c['source_ids']) != len(set(c['source_ids']))
                or not set(c['source_ids']) <= source_ids):
            raise ValueError('card digest mismatch')
        used_sources.update(c['source_ids'])
        section, _, index = c['card_id'].rpartition(':')
        if section == 'finance.brief':
            items = original['briefing'].setdefault('finance', {}).setdefault('brief', [])
        else:
            items = original['briefing'].setdefault(section, [])
        if str(len(items)) != index:
            raise ValueError('card order mismatch')
        items.append({f: c[f] for f in g.FIELDS})
    if used_sources != source_ids:
        raise ValueError('unused review source')
    rejected = g._validate_integrity(initial, material)
    final_material, final_response, timeout = captures[-1]
    if audit['timeout_s'] != timeout:
        raise ValueError('final review timeout mismatch')
    if repair:
        correction_input, patches, _ = captures[1]
        if (not rejected or correction_input != g._correction_material(material, initial)
                or audit['corrected_cards'] != len(rejected)):
            raise ValueError('correction input mismatch')
        candidate = g._apply_corrections(original, material, rejected, patches)
        expected = copy.deepcopy(material)
        for card in expected['cards']:
            card.update({f: g._card(candidate, card['card_id'])[f] for f in g.FIELDS})
            card['prose_sha256'] = g._digest(json.dumps({f: card[f] for f in g.FIELDS}, ensure_ascii=False, sort_keys=True))
        if final_material != expected:
            raise ValueError('final corrected material mismatch')
    elif rejected or audit['corrected_cards'] != 0:
        raise ValueError('unaccepted initial review')
    g._validate(final_response, final_material)


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def snapshot(path):
    """Detect new/deleted entries and in-place modifications, including timestamps."""
    path = Path(path)
    if not path.exists():
        return {"exists": False}
    entries = [path, *path.rglob("*")] if path.is_dir() else [path]
    return {str(entry): {"mtime_ns": entry.stat().st_mtime_ns,
                        "sha256": hashlib.sha256(entry.read_bytes()).hexdigest() if entry.is_file() else None}
            for entry in entries}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, help="new JSON report outside the checkout")
    parser.add_argument("--protect-config", action="append", default=[],
                        help="existing config whose runtime paths must remain unchanged")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args(argv)
    report_path = Path(args.report).resolve()
    if (report_path.is_relative_to(ROOT) or report_path.exists() or
            any((p / '.git').exists() for p in report_path.parents)):
        parser.error("report must be new and outside the checkout")
    sys.path.insert(0, str(ROOT))
    from worldintel import config
    report = {"schema_version": 2, "kind": "minimal-live-dry-run", "status": "FAIL", "errors": [],
              "model_evidence_root": None}
    try:
        commit = git("rev-parse", "HEAD")
        status = git("status", "--porcelain", "--untracked-files=all")
        report.update(source_commit=commit, source_dirty=bool(status), source_root=str(ROOT))
        if status:
            raise ValueError("candidate must be committed and clean before live verification")
        protected = []
        for filename in args.protect_config:
            cfg = config.load(filename)
            protected.extend([cfg.source_path, cfg.data_root, cfg.media_dir, cfg.env_file])
        before = {str(path): snapshot(path) for path in protected}
        with tempfile.TemporaryDirectory(prefix="world-intel-minimal-live-") as temporary:
            scratch = Path(temporary)
            profile = yaml.safe_load((ROOT / "worldintel/data/profiles/example.minimal.yaml").read_text(encoding="utf-8"))
            profile["runtime"].update(data_root=str(scratch / "data"), media_dir=str(scratch / "media"),
                                      env_file=str(scratch / "absent.env"))
            profile["delivery"] = {"adapter": "file"}
            profile.setdefault('analysis', {}).update(source_review_enabled=True, source_review_repair_enabled=True)
            cfg_path = scratch / "profile.yaml"
            cfg_path.write_text(yaml.safe_dump(profile), encoding="utf-8")
            private = scratch / 'data' / 'model-evidence'
            private.mkdir(parents=True)
            def isolated_snapshot():
                return {k: v for k, v in snapshot(scratch).items()
                        if not Path(k).is_relative_to(private)}
            isolated_before = isolated_snapshot()
            env = os.environ.copy()
            for key in API_KEYS:
                env.pop(key, None)
            env.update(WORLD_INTEL_CONFIG=str(cfg_path), PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
            command = [sys.executable, "-m", "worldintel.cli", "run", "--config", str(cfg_path), "--dry", "--force"]
            report["command"] = command
            try:
                result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                                        text=True, encoding="utf-8", errors="replace", timeout=args.timeout)
                report["exit_code"] = result.returncode
                report["errors"].extend(validate_result(result.returncode, result.stdout, result.stderr, commit, ROOT))
                summary = [line[len(PREFIX):] for line in result.stderr.splitlines() if line.startswith(PREFIX)]
                if len(summary) == 1:
                    try:
                        report["dry_summary"] = json.loads(summary[0])
                        model_summary = report['dry_summary']['model_evidence']
                        run_id = model_summary['run_id']
                        if not re.fullmatch(r'[0-9a-f]{32}', run_id):
                            raise ValueError('invalid evidence run ID')
                        source = private / run_id
                        report['errors'].extend(validate_model_evidence(source, model_summary))
                    except ValueError:
                        pass
                report["stderr_sha256"] = hashlib.sha256(result.stderr.encode()).hexdigest()
                if isolated_snapshot() != isolated_before:
                    report["errors"].append("isolated configured paths changed")
            finally:
                # Preserve captured failure evidence even when no summary survives.
                runs = list(private.iterdir())
                if runs:
                    if len(runs) != 1:
                        report['errors'].append('unexpected private model evidence run entries')
                    source = runs[0] if len(runs) == 1 else private
                    if ((len(runs) != 1 or re.fullmatch(r'[0-9a-f]{32}', source.name)) and source.is_dir()
                            and not source.is_symlink() and not any(p.is_symlink() for p in source.rglob('*'))):
                        target = report_path.with_name(report_path.stem + '.model-evidence')
                        shutil.copytree(source, target)
                        report['model_evidence_root'] = str(target)
                        if 'dry_summary' in report:
                            report['errors'].extend(validate_model_evidence(
                                target, report['dry_summary'].get('model_evidence', {}), report['dry_summary'].get('source_review', {})))
        if any(snapshot(path) != previous for path, previous in before.items()):
            report["errors"].append("protected configuration/runtime paths changed")
        if git("rev-parse", "HEAD") != commit or git("status", "--porcelain", "--untracked-files=all"):
            report["errors"].append("candidate changed during verification")
        report["protected_paths_unchanged"] = not any("paths changed" in e for e in report["errors"])
        if report['model_evidence_root'] is None:
            report['errors'].append('retained private model evidence unavailable')
        elif 'dry_summary' in report:
            report['errors'].extend(validate_model_evidence(
                report['model_evidence_root'], report['dry_summary'].get('model_evidence', {}), report['dry_summary'].get('source_review', {})))
        if not report["errors"]:
            report["status"] = "PASS"
    except Exception as exc:
        report["errors"].append(f"verification failed: {type(exc).__name__}: {exc}")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(f"{report['status']}: {report_path}")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
