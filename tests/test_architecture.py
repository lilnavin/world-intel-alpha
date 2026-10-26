"""The core cannot statically or dynamically import delivery integrations."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import check_architecture


@pytest.mark.parametrize("source", [
    "import worldintel.adapters.hermes",
    "from worldintel import adapters",
    "from ..adapters import hermes",
    "from .. import delivery",
    "def f():\n    from worldintel.adapters import hermes",
    "try:\n    import worldintel.run\nexcept ImportError:\n    pass",
    "import importlib as il\nil.import_module('worldintel.adapters.hermes')",
    "from importlib import import_module as im\nim('worldintel.delivery')",
    "__import__('worldintel.adapters')",
    "__import__(computed_name)",
])
def test_disallowed_import_forms(source):
    assert check_architecture.violations(source, "worldintel.core.sample")


def test_core_and_configuration_are_allowed():
    source = "from worldintel import config\nfrom . import select_news\nimport requests"
    assert not check_architecture.violations(source, "worldintel.core.sample")


def test_empty_core_is_not_a_success(tmp_path):
    assert check_architecture.main(["--root", str(tmp_path)]) == 1
