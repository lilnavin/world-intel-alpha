"""Packaged, read-only templates and example profiles."""
from importlib import resources
from pathlib import PurePosixPath


def _resource(relative):
    path = PurePosixPath(relative)
    if (path.is_absolute() or not path.parts or any(part in ("..", ".") for part in path.parts)
            or "\\" in str(relative) or ":" in str(relative)):
        raise ValueError("resource name must be a package-relative path")
    return resources.files(__package__).joinpath(*path.parts)


def read_bytes(relative):
    return _resource(relative).read_bytes()


def read_text(relative):
    return _resource(relative).read_text(encoding="utf-8")


def as_file(relative):
    """Keep the context open for the complete lifetime of an external reader."""
    return resources.as_file(_resource(relative))
