"""Run-owned private sidecars, separate from media and the v1 replay capsule."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


class Collector:
    def __init__(self, data_root, run_id, *, media_dir=None):
        self.root = Path(data_root) / "model-evidence" / run_id
        self.media_dir = Path(media_dir).resolve() if media_dir is not None else None
        self.invocations = []
        self.failed = False

    def __call__(self, record):
        try:
            resolved = self.root.resolve()
            if (any((p / '.git').exists() for p in (resolved, *resolved.parents)) or
                    (self.media_dir is not None and resolved.is_relative_to(self.media_dir))):
                self.failed = True
                return
            target = self.root / record["invocation_id"]
            target.mkdir(parents=True, exist_ok=False)
            files = {}
            for key, filename in (("stdin", "stdin.bin"), ("schema", "schema.bin"),
                                  ("raw_output", "output.bin"), ("stdout", "stdout.bin"),
                                  ("stderr", "stderr.bin")):
                payload = record.get(key)
                if payload is None:
                    continue
                (target / filename).write_bytes(payload)
                files[filename] = {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
            manifest = {k: record.get(k) for k in ("invocation_id", "pass_name", "outcome", "returncode", "timeout_s")}
            manifest.update(schema_version=1, files=files)
            raw = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
            (target / "manifest.json").write_bytes(raw)
            self.invocations.append({**{k: manifest[k] for k in ("invocation_id", "pass_name", "outcome")},
                                     "manifest_sha256": hashlib.sha256(raw).hexdigest()})
            if record.get("capture_error"):
                self.failed = True
        except (OSError, ValueError, TypeError, KeyError):
            self.failed = True

    def summary(self):
        return {"run_id": self.root.name, "complete": bool(self.invocations) and not self.failed,
                "invocations": list(self.invocations)}
