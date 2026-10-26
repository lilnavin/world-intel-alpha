"""The plan linter must catch the drift classes that cost three review rounds.

Testing the linter rather than the plans: the plans are expected to fail until
they are fixed, but a linter that silently stops detecting a class is worse
than no linter, because the next round's green is meaningless.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))

import lint_plan  # noqa: E402


def _write(tmp_path, body):
    p = tmp_path / "plan.md"
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_an_undefined_helper_in_a_test_block_is_caught(tmp_path):
    """The exact failure of rounds 5, 6 and 7: a test calling a helper that
    was described in prose and never written."""
    body = ("### Task 1: x\n\n```python\n"
            "def test_thing():\n    assert _never_defined() == 1\n```\n")
    found = lint_plan.lint(_write(tmp_path, body))
    assert any("_never_defined" in f for f in found), found


def test_an_interface_promise_with_no_implementation_is_caught(tmp_path):
    """sec_sites promised Site/ordinal/direct_imports while its code block
    still returned tuples. Prose and code describing different revisions."""
    body = ("### Task 1: x\n\n**Interfaces:**\n"
            "- Produces:\n  - `mymod.brand_new_thing()` does something\n\n"
            "```python\ndef unrelated():\n    return 1\n```\n")
    found = lint_plan.lint(_write(tmp_path, body))
    assert any("brand_new_thing" in f for f in found), found


def test_a_shell_env_prefix_that_does_not_persist_is_caught(tmp_path):
    body = ("### Task 1: x\n\n```bash\nMYVAR=$(mktemp -d) python x.py\n"
            "ls \"$MYVAR\"\n```\n")
    found = lint_plan.lint(_write(tmp_path, body))
    assert any("MYVAR" in f for f in found), found


def test_a_dangling_task_reference_is_caught(tmp_path):
    body = "### Task 1: x\n\nSee Task 9 for details.\n"
    found = lint_plan.lint(_write(tmp_path, body))
    assert any("Task 9" in f for f in found), found


def test_a_call_to_a_function_the_live_tree_lacks_is_caught(tmp_path):
    """feeds._item_from_entry and enrich.select_for_enrichment -- both invented
    by me in a patch written to stop inventing names."""
    body = ("### Task 1: x\n\n```python\nimport feeds\n"
            "def test_x():\n    feeds._item_from_entry({})\n```\n")
    found = lint_plan.lint(_write(tmp_path, body))
    assert any("_item_from_entry" in f for f in found), found


def test_a_clean_fragment_produces_nothing(tmp_path):
    body = ("### Task 1: x\n\n```python\n"
            "def helper():\n    return 1\n\n\n"
            "def test_helper():\n    assert helper() == 1\n```\n")
    assert lint_plan.lint(_write(tmp_path, body)) == []


def test_a_module_block_that_never_imports_what_it_uses_is_caught(tmp_path):
    """The false-green the plan-wide union created: check_l0.py called
    importlib.import_module() with no `import importlib`, and the name
    resolved because the SEPARATE test block imported it."""
    body = ('### Task 1: x\n\n```python\n'
            '"""A whole module."""\n\nimport os\n\n\n'
            'def f():\n    return importlib.import_module(os.sep)\n```\n')
    found = lint_plan.lint(_write(tmp_path, body))
    assert any("importlib" in f and "without importing" in f
               for f in found), found


def test_definitions_do_not_leak_between_declared_host_files(tmp_path):
    """A helper written in one file is not in scope in another."""
    body = ("### Task 1: x\n\n```python\n# tools/a.py\n"
            "def helper():\n    return 1\n```\n\n"
            "```python\n# tools/b.py\n"
            "def g():\n    return helper()\n```\n")
    found = lint_plan.lint(_write(tmp_path, body))
    assert any("helper" in f for f in found), found


def test_two_spellings_of_one_constant_across_plans_are_caught(tmp_path):
    """WORLDINTEL_CONFIG set by one plan, WORLD_INTEL_CONFIG read by another:
    `--config` was accepted and silently ignored."""
    a = tmp_path / "a.md"
    a.write_text("_ENV = \"WORLD_INTEL_CONFIG\"\n", encoding="utf-8")
    b = tmp_path / "b.md"
    b.write_text("os.environ[\"WORLDINTEL_CONFIG\"] = p\n", encoding="utf-8")
    found = lint_plan.corpus_findings([str(a), str(b)])
    assert any("WORLDINTEL_CONFIG" in f for f in found), found


def test_a_literal_backslash_n_in_a_shell_block_is_caught(tmp_path):
    body = ("### Task 1: x\n\n```bash\npython gates.py --a 1 " + chr(92) + "n  --b 2\n```\n")
    found = lint_plan.lint(_write(tmp_path, body))
    assert any("backslash-n" in f for f in found), found


def test_a_tag_after_an_unguarded_pipe_is_caught(tmp_path):
    """`pytest | tail` reports tail's status; the tag lands over a red suite."""
    body = ("### Task 1: x\n\n```bash\npython -m pytest | tail -3\n"
            "git tag -a candidate -m x\n```\n")
    found = lint_plan.lint(_write(tmp_path, body))
    assert any("exit status" in f for f in found), found
    assert any("git tag" in f for f in found), found


def test_a_guarded_block_is_not_flagged(tmp_path):
    body = ("### Task 1: x\n\n```bash\nset -euo pipefail\n"
            "python -m pytest | tail -3\ngit tag -a candidate -m x\n```\n")
    assert lint_plan.lint(_write(tmp_path, body)) == []


def test_a_wrong_expected_test_count_is_caught(tmp_path):
    body = ("### Task 1: x\n\n```python\ndef test_a():\n    assert 1\n\n\n"
            "def test_b():\n    assert 1\n```\n\nExpected: 5 passed.\n")
    found = lint_plan.lint(_write(tmp_path, body))
    assert any("5 tests pass" in f and "2 are written" in f
               for f in found), found
