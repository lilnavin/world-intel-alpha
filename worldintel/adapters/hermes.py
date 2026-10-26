"""Hermes integration: a MEDIA handoff, never a claim of delivery receipt."""
import os
import shutil


def media_line(path):
    value = os.fspath(path)
    if not value or "\r" in value or "\n" in value:
        raise ValueError("media path must fit on one nonempty line")
    return "MEDIA:" + value


def install_paths(cfg):
    """Use configured paths independently; env/media need not live under data."""
    def value(name):
        return cfg[name] if isinstance(cfg, dict) else getattr(cfg, name)
    paths = {name: os.path.abspath(os.path.expanduser(os.fspath(value(name))))
             for name in ("data_root", "media_dir", "env_file")}
    paths["capsules"] = os.path.join(paths["data_root"], "capsules")
    return paths


def codex_available():
    """Executable presence only; this does not test authentication or access."""
    return shutil.which("codex") is not None
