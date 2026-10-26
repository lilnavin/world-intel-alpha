"""Local delivery policies; scheduling and transport belong outside the core."""
from pathlib import Path
from worldintel.adapters import hermes


class DeliveryError(ValueError):
    """A requested delivery policy cannot be fulfilled."""


def deliver(adapter, html_path, log):
    if adapter not in {"stdout", "file", "hermes-cron", "telegram"}:
        raise DeliveryError(f"unsupported delivery adapter: {adapter}")
    path = Path(html_path)
    if not path.is_file():
        raise DeliveryError("brief file is missing; no delivery attempted")
    if any(c in str(path) for c in "\r\n"):
        raise DeliveryError("brief path must not contain line breaks")
    if adapter in {"hermes-cron", "telegram"}:
        print(hermes.media_line(path))
        log("brief handed to Hermes; this is not a delivery receipt")
    elif adapter == "stdout":
        print(path)
    else:
        log("brief saved; file-only delivery selected")
    return {"handoff": "saved" if adapter == "file" else "handed_off",
            "delivery_confirmed": False}
