"""Mechanically catch the drift that three review rounds caught by hand.

ABSOLUTE RULE: a plan is executable documentation. Every name a code block uses
must be defined somewhere a reader can reach -- in that block, elsewhere in the
plan, in the live tree, or in the standard library. Prose that promises an
interface the code block does not implement is a defect, not a style issue.

This exists because the same failure recurred three rounds running: an
interface was revised in prose, the code block underneath it was not, and a
test written against the new interface referenced helpers nobody had written.
Reviewers found it by reading. Reading does not scale and did not catch it the
first two times.

Checks, in order of how often they have actually bitten:
  1. free names in a python block that nothing defines anywhere
  2. `module.attr` where the live tree has the module but not the attr
  3. a module block that USES a module it never imports
  4. `Task N` / `Step N` cross-references that do not resolve
  5. shell `VAR=x cmd` followed by `$VAR` in a LATER command (scoping)
  6. a literal backslash-n in a shell block (a broken line continuation)
  7. a block that tags or pushes without `set -euo pipefail`
  8. `Expected: N passed` disagreeing with the tests actually written
  9. near-miss spellings of the same constant ACROSS plans (corpus check)

What it does NOT check: that the plan is correct, that the tests are good
tests, or that the design holds. It catches drift between parts of a document
that are supposed to describe one revision. Hence the closing message: "static
drift checks passed", never "the plans are consistent".

Exit 1 on any finding.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import importlib
import io
import os
import re
import sys

_SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_SKILL, "scripts")
_TOOLS = os.path.join(_SKILL, "tools")

# Names a fragment may reference without defining: pytest/test idiom, and the
# conventional placeholders plans use for elided context.
_ASSUMED = {
    "pytest", "monkeypatch", "tmp_path", "capsys", "self", "cls",
    "__file__", "__name__", "_", "argv",
}


def _blocks(text: str, lang: str):
    """(index, body) for each fenced block of `lang`."""
    out = []
    for i, m in enumerate(re.finditer(r"```(\w+)\n(.*?)```", text, re.S)):
        if m.group(1) == lang:
            out.append((i, m.group(2), text[:m.start()].count("\n") + 1))
    return out


def _parse(src: str):
    """Parse a fragment, tolerating indentation and bare statements."""
    import textwrap
    for candidate in (src, textwrap.dedent(src), "def _f():\n" + src,
                      "class _C:\n" + src):
        try:
            return ast.parse(candidate)
        except SyntaxError:
            continue
    return None


def _defined(tree) -> set:
    """Every name this fragment binds."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
            for a in (list(node.args.args) + list(node.args.kwonlyargs)
                      + list(node.args.posonlyargs)):
                names.add(a.arg)
            if node.args.vararg:
                names.add(node.args.vararg.arg)
            if node.args.kwarg:
                names.add(node.args.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            tgt = node.target
            for n in ast.walk(tgt):
                if isinstance(n, ast.Name):
                    names.add(n.id)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            for n in ast.walk(node.optional_vars):
                if isinstance(n, ast.Name):
                    names.add(n.id)
        elif isinstance(node, ast.Global):
            names.update(node.names)
    return names


def _used(tree) -> set:
    return {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _imports(tree) -> set:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module.split(".")[0])
    return out


def _live_module(name: str):
    for root in (_SKILL, _TOOLS):
        if root not in sys.path:
            sys.path.insert(0, root)
    # Older plan snippets use the pre-package local module names. Resolve only
    # names backed by an actual module file; nonexistent modules stay unknown.
    if os.path.isfile(os.path.join(_SKILL, "worldintel", name + ".py")):
        name = "worldintel." + name
    elif os.path.isfile(os.path.join(_SKILL, "worldintel", "core", name + ".py")):
        name = "worldintel.core." + name
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001
        return None


def _live_test_names() -> set:
    """Module-level names the existing test files already define.

    A plan fragment may legitimately use `_BUNDLE` because the test file it is
    appended to already has one. Only names that exist NOWHERE are defects.
    """
    names = set()
    tests = os.path.join(_SKILL, "tests")
    for base, _dirs, files in os.walk(tests):
        if "__pycache__" in base:
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            try:
                tree = ast.parse(io.open(os.path.join(base, fn),
                                         encoding="utf-8").read())
            except (OSError, SyntaxError):
                continue
            names |= _defined(tree)
    return names


def _promised(text: str) -> dict:
    """Identifiers the plan's own `Interfaces:` blocks promise, by line.

    This is the check that matters most. Three rounds running, an interface was
    revised in prose while the code block beneath it was not -- `sec_sites`
    promised Site/ordinal/context/direct_imports and still returned
    (lineno, dotted) tuples, so every test written against the promise failed
    on the first line. Prose and code must be checked against each other, and
    the plan already states the contract in a parseable place.
    """
    out = {}
    in_iface = False
    in_produces = False
    for n, line in enumerate(text.split(chr(10)), 1):
        if re.match(r"^\*\*Interfaces:\*\*", line):
            in_iface = True
            continue
        if in_iface and (line.startswith("- [ ]") or line.startswith("###")
                         or line.startswith("**Files")):
            in_iface = False
        if not in_iface:
            continue
        # Only what the task PRODUCES. A `Consumes:` entry names something
        # another plan or the live tree provides -- demanding this plan
        # implement it is wrong, and was reporting Plan 1's delivery.deliver.
        if re.match(r"^\s*-\s*Consumes", line):
            in_produces = False
            continue
        if re.match(r"^\s*-\s*Produces", line):
            in_produces = True
        if not in_produces:
            continue
        # Only CALLABLES and CLASSES -- `name(` or `Name` in a type position.
        # An earlier version harvested every backticked token, so prose words
        # like `national`, `tuning` and `from` were reported as unimplemented
        # promises. A gate that cries wolf is a gate that gets ignored, which
        # is worse than not having one.
        for tok in re.findall(r"`([A-Za-z_][A-Za-z0-9_.]*)\s*\(", line):
            out.setdefault(tok.split(".")[-1], n)
        # Attributes promised on a live module -- `run.paths()`,
        # `kb.local_today()`. The module.attr check must not report these as
        # absent from the live tree: absent is the point, the task adds them.
        for tok in re.findall(r"`([a-z_]+)\.([A-Za-z_][A-Za-z0-9_]*)", line):
            out.setdefault(".".join(tok), n)
    return out


def _created_files(text: str) -> set:
    """Module stems the plan's `Files:` blocks say it creates.

    A plan that promises `sec_sites.call_sites()` and creates
    `tools/sec_sites.py` has kept its promise; the module does not need to be
    importable from the live tree yet.
    """
    stems = set()
    for m in re.finditer(r"^- (?:Create|Modify):\s*`([^`]+)`", text, re.M):
        base = os.path.basename(m.group(1))
        if base.endswith(".py"):
            stems.add(base[:-3])
    return stems


def _host_of(body: str):
    """The file a block declares itself part of, from a leading comment.

    `# in tools/check_l0.py`, `# tests/test_config.py`. Returns the path, which
    is the SCOPE key: names defined in a block belonging to one file are not in
    scope in a block belonging to another. The plan-wide union of definitions
    was a false-green generator -- `import importlib` appearing in the test
    file made check_l0.py's use of it look satisfied.
    """
    first = body.strip().split(chr(10))[0] if body.strip() else ""
    m = re.match(r"#\s*(?:in\s+)?([\w./-]+\.py)", first)
    return m.group(1) if m else None


def _is_module_block(tree, body: str) -> bool:
    """Does this block define a whole FILE rather than a fragment of one?

    A module block is self-contained: what it does not import, it does not
    have. A fragment is appended to a file that already has imports, so
    demanding they be repeated would be noise. The signal is a module
    docstring or `from __future__` at the top -- both are things you write once
    per file and never in a snippet.
    """
    if not isinstance(tree, ast.Module) or not tree.body:
        return False
    if any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
           for n in tree.body):
        return True
    head = tree.body[0]
    return (isinstance(head, ast.Expr)
            and isinstance(head.value, ast.Constant)
            and isinstance(head.value.value, str)
            and body.lstrip().startswith(('"""', "'''")))


def _missing_imports(tree, body: str) -> set:
    """Importable modules a self-contained module block uses but never imports.

    check_l0.py called importlib.import_module() with no `import importlib`;
    the name resolved because the SEPARATE test block imported it and because
    the free-name check exempts anything importable. Both exemptions are right
    on their own and wrong together.
    """
    if not _is_module_block(tree, body):
        return set()
    free = _used(tree) - _defined(tree) - set(dir(builtins)) - _ASSUMED
    return {n for n in free
            if _live_module(n) is not None and n not in _imports(tree)}


def _expected_counts(text: str, py) -> list:
    """`Expected: N passed` against the tests actually written above it.

    Task 1 said five; eight had been written. The number is the only thing a
    reader has to tell them a test silently failed to collect, so a wrong one
    is worse than none.
    """
    findings = []
    starts = {line: (body, idx) for idx, body, line in py}
    for m in re.finditer(r"^Expected:\s*(\d+)\s+passed", text, re.M):
        ln = text[:m.start()].count(chr(10)) + 1
        # Everything since the previous Expected-count, so consecutive steps
        # each account for their own tests rather than re-counting the task's.
        prev = [p.start() for p in
                re.finditer(r"^Expected:\s*\d+\s+passed", text, re.M)
                if p.start() < m.start()]
        floor = text[:prev[-1]].count(chr(10)) + 1 if prev else 0
        written = 0
        for line, (body, _idx) in starts.items():
            if floor < line < ln:
                written += len(re.findall(r"^\s*def (test_\w+)", body, re.M))
        if written and written != int(m.group(1)):
            findings.append(
                f"{ln}: says {m.group(1)} tests pass; {written} are written "
                f"above it")
    return findings


def _shell_hazards(text: str) -> list:
    """Two ways a shell block lies about having succeeded."""
    findings = []
    for _idx, body, line in _blocks(text, "bash"):
        lines = body.split(chr(10))
        guarded = any(re.match(r"^\s*set -[a-z]*e[a-z]*o? ?", l)
                      and "pipefail" in l for l in lines)
        # Heredoc bodies are not shell. Every one of these blocks embeds a
        # python script via `python - <<'PY'`, where a backslash-n is a string
        # escape doing exactly what it should; scanning them as shell reported
        # twenty-two lines of correct code.
        closer = None
        for i, l in enumerate(lines):
            if closer is not None:
                if l.strip() == closer:
                    closer = None
                continue
            here = re.search(r"<<-?\s*'?([A-Za-z_][A-Za-z0-9_]*)'?", l)
            if here:
                closer = here.group(1)
                continue
            # A literal backslash-n in a shell block is never what was meant;
            # it is a python string that got written into a bash fence, and
            # the shell passes it through as two characters of argument.
            if "\\n" in l and not l.lstrip().startswith("#"):
                findings.append(
                    f"{line + i}: literal backslash-n in a shell command; "
                    f"a continuation needs a real newline")
            if guarded:
                continue
            # `pytest ... | tail` reports tail's status, and `scan ...; echo`
            # turns any exit code into 0. Both have been used immediately
            # before `git tag`, which is the point of no return.
            if re.search(r"\b(pytest|scan_secrets|check_l0|check_equivalence"
                         r"|publication_gates)\b[^|]*\|", l):
                findings.append(
                    f"{line + i}: a pipe discards the exit status of the "
                    f"command on its left; this block has no `set -euo "
                    f"pipefail`")
            if re.search(r"^\s*git (tag|push)\b", l):
                findings.append(
                    f"{line + i}: `{l.strip()[:40]}` runs in a block with no "
                    f"`set -euo pipefail`, so it runs even if a check above "
                    f"it failed")
    return findings


def corpus_findings(paths) -> list:
    """Near-miss spellings of one constant, across the whole plan set.

    `WORLDINTEL_CONFIG` and `WORLD_INTEL_CONFIG` are two names for one
    contract: Plan 3's CLI set the first, Plan 1's loader reads the second, so
    `--config` was silently ignored and the check ran against whatever
    configuration happened to be installed. No single-file check can see this.
    """
    seen = {}
    for path in paths:
        text = io.open(path, encoding="utf-8").read()
        for tok in re.findall(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,})\b", text):
            seen.setdefault(tok.replace("_", ""), {}).setdefault(
                tok, set()).add(os.path.basename(path))
    out = []
    for _key, spellings in sorted(seen.items()):
        if len(spellings) < 2:
            continue
        detail = "; ".join(f"{k} in {', '.join(sorted(v))}"
                           for k, v in sorted(spellings.items()))
        out.append(f"two spellings of one constant -- {detail}")
    return out


def lint(path: str) -> list:
    text = io.open(path, encoding="utf-8").read()
    findings = []
    py = _blocks(text, "python")

    trees = {}
    for idx, body, line in py:
        tree = _parse(body)
        if tree is None:
            findings.append(f"{line}: block {idx} does not parse")
            continue
        trees[idx] = (tree, body, line)

    # Definitions are scoped BY FILE. A block that declares its host sees only
    # its own file's blocks plus the unhosted ones (fixtures, snippets whose
    # home the plan leaves implicit). The plan-wide union is kept only for
    # blocks that declare no host, where there is nothing better to use.
    plan_defines = set()
    by_host, unhosted = {}, set()
    for tree, body, _line in trees.values():
        names = _defined(tree)
        plan_defines |= names
        host = _host_of(body)
        if host:
            by_host.setdefault(host, set()).update(names)
        else:
            unhosted |= names

    promised = _promised(text)
    created = _created_files(text)
    known = (set(dir(builtins)) | _ASSUMED | plan_defines
             | _live_test_names() | set(promised) | created)

    for idx, (tree, body, line) in trees.items():
        # A block may declare the module it edits with a first-line comment,
        # e.g. `# in scripts/compose_html.py`. Its private helpers are then in
        # scope -- _as_dict and _story_card are real names inside that file
        # and reporting them is noise.
        host_path = _host_of(body)
        host = None
        if host_path:
            stem = os.path.basename(host_path)[:-3]
            host = _live_module(stem)
        scope = (known - plan_defines) | (
            (by_host.get(host_path, set()) | unhosted) if host_path
            else plan_defines)
        free = _used(tree) - _defined(tree) - scope
        for name in sorted(free):
            if _live_module(name) is not None:
                continue
            if host is not None and hasattr(host, name):
                continue
            findings.append(
                f"{line}: block {idx} uses undefined name {name!r}")

        for name in sorted(_missing_imports(tree, body)):
            findings.append(
                f"{line}: block {idx} is a whole module and uses {name!r} "
                f"without importing it")

        # module.attr against the live tree. A name BOUND in this block is a
        # local, not a module -- `html = render_html(...)` shadows the stdlib
        # `html`, and reporting html.count would be pure noise.
        imported = _imports(tree)
        # Names bound by ASSIGNMENT only. _defined() also counts imports, and
        # skipping those would disable this check entirely -- which it did,
        # caught by test_a_call_to_a_function_the_live_tree_lacks_is_caught.
        local = {n.id for n in ast.walk(tree)
                 if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)):
                continue
            if node.value.id in local or node.value.id not in imported:
                continue
            mod = _live_module(node.value.id)
            if mod is None:
                continue
            if f"{node.value.id}.{node.attr}" in promised:
                continue      # this plan's Interfaces undertake to add it
            if not hasattr(mod, node.attr):
                findings.append(
                    f"{line}: block {idx} calls "
                    f"{node.value.id}.{node.attr}, which does not exist in "
                    f"the live tree")

    # Promised but never implemented: the interface/code drift itself.
    for name, line in sorted(promised.items(), key=lambda kv: kv[1]):
        if "." in name:
            continue          # a module.attr promise, checked above instead
        if (name in plan_defines or name in created
                or _live_module(name) is not None):
            continue
        if any(hasattr(m, name) for m in
               (_live_module(x) for x in ("run", "select_news", "compose_html",
                                          "enrich", "metrics", "quality_gate",
                                          "feeds", "capsule"))
               if m is not None):
            continue
        findings.append(
            f"{line}: Interfaces promise {name!r}, which no code block in "
            f"this plan defines")

    # Task / Step cross-references
    tasks = {int(m) for m in re.findall(r"^### Task (\d+)", text, re.M)}
    for m in re.finditer(r"(Plan \d+ )?Task (\d+)(?:\s+Step (\d+))?", text):
        if m.group(1):
            continue          # `Plan 3 Task 12` refers to another document
        t = int(m.group(2))
        if t not in tasks:
            ln = text[:m.start()].count("\n") + 1
            findings.append(f"{ln}: reference to Task {t}, which does not exist")
    for m in re.finditer(r"Task 6a|Task \d+[a-z]", text):
        ln = text[:m.start()].count("\n") + 1
        findings.append(f"{ln}: reference to {m.group(0)!r}, not a real task")

    findings += _expected_counts(text, py)
    findings += _shell_hazards(text)

    # Shell: VAR=x cmd ... then $VAR later
    for idx, body, line in _blocks(text, "bash"):
        lines = body.split("\n")
        for i, l in enumerate(lines):
            # The value may be quoted and may contain spaces. `LIVE="/c/.../VS
            # Code Projects/..."` is a plain assignment that DOES persist; a
            # naive \S+ read "Code" as the command and reported it.
            m = re.match(
                r"^([A-Z_][A-Z0-9_]*)=(?:\"[^\"]*\"|'[^']*'|[^\s\"'])+\s+\S",
                l)
            if not m:
                continue
            var = m.group(1)
            later = "\n".join(lines[i + 1:])
            if re.search(rf"\${var}\b|\$\{{{var}\}}", later):
                findings.append(
                    f"{line + i}: `{var}=... cmd` does not export {var}; "
                    f"a later command in the same block reads $ {var}"
                    .replace("$ ", "$"))
    return findings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("paths", nargs="+")
    args = ap.parse_args(argv)
    total = 0
    for path in args.paths:
        found = lint(path)
        total += len(found)
        print(f"== {os.path.basename(path)}: {len(found)} finding(s)")
        for f in found:
            print(f"   {f}")
    cross = corpus_findings(args.paths) if len(args.paths) > 1 else []
    if cross:
        print(f"== across {len(args.paths)} plans: {len(cross)} finding(s)")
        for f in cross:
            print(f"   {f}")
        total += len(cross)
    print()
    if total:
        print(f"BLOCKED: {total} finding(s).", file=sys.stderr)
        return 1
    # NOT "the plans are consistent". This tool checks that the parts of a
    # document describe one revision of the design. Whether that design is
    # right is a question for a reader, and saying more than was checked is
    # how a green gate starts standing in for a review.
    print("Static drift checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
