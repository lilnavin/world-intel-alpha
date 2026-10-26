"""Assemble one structural prompt template; desk prose stays byte-verbatim."""
from worldintel import data

from worldintel import config

_BASE = "prompts/world.base.md"


def build(cfg=None) -> str:
    cfg = config.get() if cfg is None else cfg
    # read_bytes deliberately avoids universal-newline translation. The frozen
    # instructions contain CRLF and LF, including within the national sections.
    text = data.read_bytes(_BASE).decode("utf-8")
    sections = "".join(desk["prompt_section"] for desk in cfg.national_desks)
    geographies = ", ".join(desk["label"] for desk in cfg.desks if desk["kind"] != "finance")
    for marker, content in (("{{DESK_SECTIONS}}", sections), ("{{GEOGRAPHIES}}", geographies)):
        if text.count(marker) != 1:
            raise ValueError(f"prompt template requires one {marker}")
        text = text.replace(marker, content)
    if "{{" in text:
        raise ValueError("unresolved prompt template marker")
    return text


def build_bytes(cfg=None) -> bytes:
    return build(cfg).encode("utf-8")
