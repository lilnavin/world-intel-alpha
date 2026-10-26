"""Build national schema properties without changing the frozen wire bytes.

The committed schema uses mixed line endings and nonstandard indentation.
Literal structural templates preserve those bytes; first and following property
slots differ only in whitespace. Desk identity and maximum always come from cfg.
"""
from __future__ import annotations

import json
import os
from worldintel import data
import tempfile

from worldintel import config

_SCHEMA = "schema/"


def _template(name):
    return data.read_bytes(_SCHEMA + name)


def build_bytes(cfg=None) -> bytes:
    cfg = config.get() if cfg is None else cfg
    base = _template("world.base.template")
    properties, required = [], []
    for i, desk in enumerate(cfg.national_desks):
        did = json.dumps(desk["id"]).encode("utf-8")
        required_template = _template("national.required.first.template" if i == 0 else "national.required.next.template")
        fragment = _template("national.first.template" if i == 0 else "national.next.template")
        properties.append(fragment.replace(b"{{DESK_ID}}", did).replace(
            b"{{MAX_ITEMS}}", str(desk["target_stories"][1]).encode("ascii")))
        required.append(required_template.replace(b"{{DESK_ID}}", did))
    for marker, content in ((b"{{NATIONAL_PROPERTIES}}", b"".join(properties)),
                            (b"{{NATIONAL_REQUIRED}}", b"".join(required))):
        if base.count(marker) != 1:
            raise ValueError(f"schema template requires one {marker!r}")
        base = base.replace(marker, content)
    if b"{{" in base:
        raise ValueError("unresolved schema template marker")
    json.loads(base)  # Never pass a malformed structural template to the engine.
    return base


def build(cfg=None) -> dict:
    return json.loads(build_bytes(cfg))


def write_temp(cfg=None) -> str:
    """Return a temporary schema path; the caller must unlink it in finally."""
    blob = build_bytes(cfg)
    fd, path = tempfile.mkstemp(prefix="world-schema-", suffix=".json")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
    except BaseException:
        os.unlink(path)
        raise
    return path
