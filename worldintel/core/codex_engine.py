"""codex_engine — Module C of the world-intel skill.

Thin, import-safe wrapper around the Codex CLI. Codex is invoked as a READ-ONLY
pure function: feed it a prompt on stdin (from a file), constrain its output with a
JSON schema, and read the single final assistant message back from the ``-o`` file.

Proven-working invocation (validated on this machine, exit 0, schema-valid JSON):

    codex exec -s read-only --ephemeral --skip-git-repo-check --ignore-user-config \
      -c model_reasoning_effort=<effort> \
      --output-schema <schema> -o <out_file> < <prompt_file>

Critical gotcha: stdin MUST be fed from the prompt FILE. Passing the prompt as a CLI
arg while leaving stdin an open empty pipe makes codex hang forever waiting on stdin.
We therefore always redirect stdin from a file and pass NO prompt argument.
"""

from __future__ import annotations

import json
import os
import shutil
import re
import signal
import subprocess
import tempfile
import uuid
import time
from pathlib import Path
from worldintel import data

__all__ = ["CodexError", "smoke_test", "analyze", "analyze_world",
           "analyze_finance", "run_two_pass", "SENTINEL"]

SENTINEL = "WORLDINTEL-OK"

_PROMPT_TEMPLATE = "prompts/analysis.md"

# The two-pass prompts (spec E Sec.6.5). `analysis.md` above stays live and
# unmodified until the two passes are proven; these are derived from it, and
# the style contract, hard bans, exemplar body, grounding rule, tier gate,
# novelty gate, gov cap and Guardian cap are byte-for-byte lifts, asserted in
# test_codex_engine.py against analysis.md itself.
WORLD_PROMPT_TEMPLATE = "prompts/world.md"
FINANCE_PROMPT_TEMPLATE = "prompts/finance.md"

# Which bundle material each pass sees (spec Sec.6.5's Input column). `filings`
# is in BOTH -- Signal hunts it for overlooked items and `filings_note` frames
# it -- and so is the whole `news` pool for the world pass, since Signal's
# remit explicitly includes "obscure-but-real finance items".
_WORLD_DROP = ("markets", "macro", "earnings", "earnings_calendar",
               "post_earnings_candidates", "post_earnings_followups",
               "selection_audit", "ranking_profile", "ranking_profile_hash")
_FINANCE_DROP = ("gov", "gdelt", "selection_audit", "ranking_profile",
                 "ranking_profile_hash", "protected_events")


class CodexError(Exception):
    """Raised on any Codex failure: missing CLI, non-zero exit, timeout, or
    output that is not parseable JSON."""


def diagnostic(value, limit=700):
    """Bounded local diagnostics, never provider URLs or credentials."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    value = str(value or "")
    value = re.sub(r"https?://\S+", "[redacted URL]", value)
    value = re.sub(r"(?i)\b(Bearer|Basic)\s+\S+", r"\1 [redacted]", value)
    value = re.sub(r'''(?i)(["']?[\w-]*(?:key|token|secret|password)[\w-]*["']?\s*[=:]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}]+)''',
                   r"\1[redacted]", value)
    value = re.sub(r"\bsk-[\w-]+", "[redacted]", value)
    return value[:limit]


def _execute(args, *, stdin, capture_output, timeout):
    """Own the process so timeout cleanup includes the Windows shim's children."""
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(args, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            proc.kill()
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            stdout, stderr = proc.communicate(timeout=5)
            exc.output = stdout if stdout is not None else exc.output
            exc.stderr = stderr if stderr is not None else exc.stderr
        except subprocess.TimeoutExpired as cleanup:
            exc.output = cleanup.output if cleanup.output is not None else exc.output
            exc.stderr = cleanup.stderr if cleanup.stderr is not None else exc.stderr
        except (OSError, subprocess.SubprocessError):
            pass
        raise


def _resolve_codex() -> str:
    """Return the absolute path to the codex CLI (a ``.CMD``/``.ps1`` shim on
    Windows). Raise CodexError if it is not on PATH."""
    codex = shutil.which("codex")
    if not codex:
        raise CodexError("codex CLI not found on PATH")
    return codex


def _new_temp(prefix: str, suffix: str) -> str:
    """Return a fresh, unique path under the system temp dir (file not created)."""
    name = f"worldintel-{prefix}-{uuid.uuid4().hex}{suffix}"
    return os.path.join(tempfile.gettempdir(), name)


def _run_codex(
    prompt_text: str,
    out_file: str,
    *,
    schema_path: str | None,
    reasoning_effort: str,
    timeout: int,
    evidence_sink=None,
    pass_name=None,
) -> subprocess.CompletedProcess:
    """Write ``prompt_text`` to a temp file, invoke codex with the proven flags,
    feeding stdin from that file. Returns the CompletedProcess. The caller owns
    reading ``out_file``. Raises CodexError on timeout."""
    codex = _resolve_codex()
    record = {"invocation_id": uuid.uuid4().hex, "pass_name": pass_name,
              "timeout_s": timeout, "returncode": None, "outcome": "exception",
              "stdout": b"", "stderr": b""}
    prompt_file = _new_temp("prompt", ".txt")

    args = [
        codex,
        "exec",
        "-s",
        "read-only",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "-c",
        f"model_reasoning_effort={reasoning_effort}",
    ]
    if schema_path:
        args += ["--output-schema", str(schema_path)]
    args += ["-o", str(out_file)]

    try:
        with open(prompt_file, "w", encoding="utf-8") as fh:
            fh.write(prompt_text)
        if evidence_sink is not None:
            record["stdin"] = Path(prompt_file).read_bytes()
            record["schema"] = Path(schema_path).read_bytes() if schema_path else None
        # Remove any stale out file so a hang/failure cannot return prior content.
        if os.path.exists(out_file):
            os.remove(out_file)
        with open(prompt_file, "rb") as stdin_fh:
            proc = _execute(
                args,
                stdin=stdin_fh,
                capture_output=True,
                timeout=timeout,
            )
        record.update(returncode=proc.returncode, stdout=proc.stdout or b"", stderr=proc.stderr or b"",
                      outcome="nonzero" if proc.returncode else "success")
        return proc
    except subprocess.TimeoutExpired as exc:
        record.update(outcome="timeout", stdout=exc.output or b"", stderr=exc.stderr or b"")
        if evidence_sink is not None:
            raise CodexError(f"codex timed out after {timeout}s") from exc
        raise CodexError(f"codex timed out after {timeout}s; "
                         f"stdout={diagnostic(exc.output)}; stderr={diagnostic(exc.stderr)}") from exc
    finally:
        try:
            if evidence_sink is not None:
                if "stdin" not in record or (schema_path and "schema" not in record):
                    record["capture_error"] = True
                try:
                    record["raw_output"] = Path(out_file).read_bytes() if Path(out_file).is_file() else None
                    if record["outcome"] == "success":
                        if record["raw_output"] is None:
                            record["outcome"] = "missing"
                        elif schema_path:
                            try:
                                json.loads(record["raw_output"])
                            except (ValueError, UnicodeError):
                                record["outcome"] = "malformed"
                except OSError:
                    record["capture_error"] = True
                for key in ("stdout", "stderr"):
                    if isinstance(record[key], str):
                        record[key] = record[key].encode("utf-8")
                try:
                    evidence_sink(record)
                except Exception as exc:
                    raise CodexError("private model evidence capture failed") from exc
        finally:
            try:
                os.remove(prompt_file)
            except OSError:
                pass



def smoke_test(timeout: int = 60) -> bool:
    """Run codex on a trivial prompt (stdin fed from a file, so it cannot hang)
    and return True iff exit code is 0 and the output contains the sentinel.

    Never raises — this is the pipeline health gate, so any failure (codex
    missing, non-zero exit, timeout, unreadable output) returns False.
    """
    out_file = _new_temp("smoke-out", ".txt")
    prompt = (
        "Output exactly the following token and nothing else, no quotes, no "
        f"punctuation:\n{SENTINEL}\n"
    )
    try:
        proc = _run_codex(
            prompt,
            out_file,
            schema_path=None,
            reasoning_effort="low",
            timeout=timeout,
        )
        if proc.returncode != 0:
            return False
        if not os.path.exists(out_file):
            return False
        with open(out_file, "r", encoding="utf-8", errors="replace") as fh:
            return SENTINEL in fh.read()
    except CodexError:
        return False
    finally:
        try:
            os.remove(out_file)
        except OSError:
            pass


def _load_template(template_path=None) -> str:
    """Load a prompt template (the single-pass ``analysis.md`` by default).
    Raise CodexError if it is missing."""
    path = template_path or _PROMPT_TEMPLATE
    try:
        if str(path) in {"prompts/analysis.md", "prompts/world.md", "prompts/finance.md"}:
            return data.read_text(str(path))
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CodexError(f"prompt template not found: {path}") from exc


def _build_prompt(bundle: dict, context: dict, template_path=None, *, template_text=None) -> str:
    """Fill the template with the bundle + context JSON.

    If the template contains ``{bundle}`` / ``{context}`` placeholders they are
    substituted in place (plain ``str.replace``, so literal braces in prose are
    safe). Otherwise the JSON is appended under labelled headings.
    """
    template = _load_template(template_path) if template_text is None else template_text
    bundle_json = json.dumps(bundle, ensure_ascii=False)
    context_json = json.dumps(context, ensure_ascii=False)

    if "{bundle}" in template or "{context}" in template:
        return template.replace("{bundle}", bundle_json).replace(
            "{context}", context_json
        )
    return (
        f"{template}\n\n## BUNDLE\n{bundle_json}\n\n## CONTEXT\n{context_json}\n"
    )


def run_schema(
    prompt_text: str,
    schema_path: str,
    *,
    reasoning_effort: str = "high",
    timeout: int = 360,
    evidence_sink=None,
    pass_name=None,
) -> dict:
    """Run codex on an ALREADY-BUILT prompt, constrained by the schema at
    ``schema_path``, and return the parsed JSON dict. Generic sibling of
    ``analyze`` for callers (e.g. the self-improvement PM/ingest flows) that build
    their own prompt. Raises CodexError on non-zero exit, timeout, or bad JSON."""
    out_file = _new_temp("schema-out", ".txt")
    try:
        proc = _run_codex(
            prompt_text,
            out_file,
            schema_path=schema_path,
            reasoning_effort=reasoning_effort,
            timeout=timeout,
            **({"evidence_sink": evidence_sink, "pass_name": pass_name} if evidence_sink is not None else {}),
        )
        if proc.returncode != 0:
            if evidence_sink is not None:
                raise CodexError(f"codex exited {proc.returncode}")
            stderr = proc.stderr or b""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            raise CodexError(f"codex exited {proc.returncode}: {diagnostic(stderr)}")
        if not os.path.exists(out_file):
            raise CodexError("codex produced no output file")
        with open(out_file, "r", encoding="utf-8") as fh:
            raw = fh.read()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            if evidence_sink is not None:
                raise CodexError("codex output is not valid JSON") from exc
            raise CodexError(
                f"codex output is not valid JSON: {exc}; raw={diagnostic(raw)!r}"
            ) from exc
    finally:
        try:
            os.remove(out_file)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Two-pass orchestration (spec E Sec.8.2)
# --------------------------------------------------------------------------- #


def _world_bundle(bundle: dict) -> dict:
    """The World News pass's view of the bundle: everything except the finance
    desk's own material. A shallow copy -- the caller's bundle is also handed
    to the finance pass and to compose_html, and must not be mutated."""
    bundle = bundle if isinstance(bundle, dict) else {}
    out = {k: v for k, v in bundle.items() if k not in _WORLD_DROP}
    if out.get("protected_events"):
        out["coverage_requirements"] = (
            "Include each protected event with eligible full-text reporting as a substantive "
            "100-200 word card, using an exact selected event URL. Preserve attribution and "
            "allegations; protection is a coverage requirement, never proof of terrorism. "
            "If source content is insufficient, do not invent facts.")
    return out


def _finance_bundle(bundle: dict, world_headlines=None) -> dict:
    """The Finance pass's view: the finance desk's stories plus markets,
    filings, post-earnings and follow-ups, and the headlines the
    World News pass led with (see `world_headlines` in prompts/finance.md).

    `news` is narrowed to the finance desk -- but only when that leaves
    something. `desk` is set by the feed registry and survives selection, yet a
    degraded selection path can drop it, and filtering to nothing would empty
    `finance.brief` on exactly the morning the pipeline was already limping."""
    bundle = bundle if isinstance(bundle, dict) else {}
    out = {k: v for k, v in bundle.items() if k not in _FINANCE_DROP}
    try:
        desk = [n for n in (bundle.get("news") or [])
                if isinstance(n, dict) and n.get("desk") == "finance"]
        if desk:
            out["news"] = desk
    except Exception:  # noqa: BLE001
        pass
    out["world_headlines"] = list(world_headlines or [])
    if "world_source_budget" not in out:
        from worldintel.core import source_diversity
        out["world_source_budget"] = source_diversity.finance_guardian_budget(bundle)
    return out


def _headlines(result) -> list:
    """The headlines the World News pass selected for The Brief. The Finance
    pass's `scoreboard_read` cites them under the single-mention rule, so this
    is the one piece of the world result the finance pass needs. Never raises;
    degrades to [] on any shape."""
    try:
        brief = ((result or {}).get("briefing") or {}).get("brief") or []
        return [h for h in ((b or {}).get("headline") for b in brief
                            if isinstance(b, dict)) if h]
    except Exception:  # noqa: BLE001
        return []


def _log(log, msg: str) -> None:
    """Call the caller's logger if it gave us one, and never let logging be the
    thing that breaks the run."""
    if callable(log):
        try:
            log(msg)
        except Exception:  # noqa: BLE001
            pass


def analyze_world(
    bundle: dict,
    context: dict,
    schema_path: str,
    *,
    reasoning_effort: str = "high",
    timeout: int = 360,
    evidence_sink=None,
    pass_name=None,
    runner=None,
) -> dict:
    """Run the World News pass: `prompts/world.md` against
    `schema/world.schema.json`. Raises CodexError like ``analyze``."""
    from worldintel.core import prompt_build
    # Match the historical Path.read_text universal-newline behavior at the
    # model-input boundary. The builder's separate byte API remains exact.
    template = prompt_build.build().replace("\r\n", "\n").replace("\r", "\n")
    prompt = _build_prompt(_world_bundle(bundle), context, template_text=template)
    return (runner or run_schema)(prompt, schema_path,
                      reasoning_effort=reasoning_effort, timeout=timeout,
                      **({"evidence_sink": evidence_sink, "pass_name": pass_name} if evidence_sink is not None else {}))


def analyze_finance(
    bundle: dict,
    context: dict,
    schema_path: str,
    *,
    world_headlines=None,
    reasoning_effort: str = "high",
    timeout: int = 360,
    evidence_sink=None,
    pass_name=None,
    runner=None,
) -> dict:
    """Run the Finance pass: `prompts/finance.md` against
    `schema/finance.schema.json`. `world_headlines` is what `scoreboard_read`'s
    story citations bind to. Raises CodexError like ``analyze``."""
    prompt = _build_prompt(_finance_bundle(bundle, world_headlines), context,
                           FINANCE_PROMPT_TEMPLATE)
    return (runner or run_schema)(prompt, schema_path,
                      reasoning_effort=reasoning_effort, timeout=timeout,
                      **({"evidence_sink": evidence_sink, "pass_name": pass_name} if evidence_sink is not None else {}))


def _settle(fn, *args, **kwargs):
    """Run one pass and return ``(result, error_string)``. Catches Exception,
    not just CodexError: a bundle that will not serialise raises TypeError out
    of json.dumps, and house rule 1 says nothing raises out of a module."""
    try:
        return fn(*args, **kwargs), None
    except Exception as exc:  # noqa: BLE001
        return None, f"{exc}"


def run_two_pass(
    bundle: dict, context: dict, world_schema: str, finance_schema: str, *,
    concurrent: bool = False, reasoning_effort: str = "high", timeout=None,
    world_timeout: int = 600, finance_timeout: int = 360, deadline=None, log=None,
    evidence_sink=None, pass_name=None, runner=None,
) -> dict:
    """Sequential world then finance, with no automatic retries.

    ``concurrent`` remains accepted for old launchers but is intentionally ignored.
    ``deadline`` is an absolute monotonic analysis deadline (finishing reserve
    already subtracted by the orchestrator). One failure still ships the other.
    """
    if timeout is not None:
        world_timeout = finance_timeout = timeout
    if concurrent:
        _log(log, "two-pass: sequential required; concurrent option ignored")
    results, errors, records = {}, {}, {}
    for name, fn, schema, configured in (
            ("world", analyze_world, world_schema, world_timeout),
            ("finance", analyze_finance, finance_schema, finance_timeout)):
        start = time.monotonic()
        available = configured if deadline is None else max(0, deadline - start)
        bounded = min(configured, available)
        result, error = None, None
        skipped = bounded <= 0
        if skipped:
            error = "analysis budget exhausted"
        else:
            kwargs = {"reasoning_effort": reasoning_effort, "timeout": bounded}
            if runner is not None:
                kwargs["runner"] = runner
            if evidence_sink is not None:
                kwargs.update(evidence_sink=evidence_sink, pass_name=f"{pass_name}.{name}" if pass_name else name)
            if name == "finance":
                kwargs["world_headlines"] = _headlines(results.get("world"))
                from worldintel.core import source_diversity
                pass_bundle = {**bundle, "world_source_budget":
                               source_diversity.finance_guardian_budget(bundle, results.get("world"))}
            else:
                pass_bundle = bundle
            result, error = _settle(fn, pass_bundle, context, schema, **kwargs)
            if not error and not (isinstance(result, dict) and isinstance(result.get("briefing"), dict)):
                result, error = None, "invalid analysis result: briefing missing"
        error = diagnostic(error, limit=1600) if error else None
        results[name], errors[name] = result, error
        records[name] = {"success": error is None, "duration_s": round(time.monotonic() - start, 3),
                         "timeout_s": bounded, "timed_out": bool(error and "timed out" in error),
                         "skipped": skipped, "error": error}
    successes = sum(record["success"] for record in records.values())
    status = {"overall": "complete" if successes == 2 else "partial" if successes else "failed",
              "mode": "sequential", "passes": records}
    return {**results, "errors": errors, "mode": "sequential", "analysis_status": status}


def analyze(
    bundle: dict,
    context: dict,
    schema_path: str,
    *,
    reasoning_effort: str = "high",
    timeout: int = 360,
    evidence_sink=None,
    pass_name=None,
) -> dict:
    """Compose the analysis prompt from ``bundle`` + ``context``, run codex with
    the schema at ``schema_path``, and return the parsed JSON dict.

    Raises CodexError on non-zero exit, timeout, or unparseable output.
    """
    prompt = _build_prompt(bundle, context)
    out_file = _new_temp("analysis-out", ".txt")
    try:
        proc = _run_codex(
            prompt,
            out_file,
            schema_path=schema_path,
            reasoning_effort=reasoning_effort,
            timeout=timeout,
            **({"evidence_sink": evidence_sink, "pass_name": pass_name} if evidence_sink is not None else {}),
        )
        if proc.returncode != 0:
            if evidence_sink is not None:
                raise CodexError(f"codex exited {proc.returncode}")
            stderr = (proc.stderr or b"")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            raise CodexError(
                f"codex exited {proc.returncode}: {diagnostic(stderr)}"
            )
        if not os.path.exists(out_file):
            raise CodexError("codex produced no output file")
        with open(out_file, "r", encoding="utf-8") as fh:
            raw = fh.read()
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            if evidence_sink is not None:
                raise CodexError("codex output is not valid JSON") from exc
            raise CodexError(
                f"codex output is not valid JSON: {exc}; raw={diagnostic(raw)!r}"
            ) from exc
    finally:
        try:
            os.remove(out_file)
        except OSError:
            pass
