"""Synthetic private model bytes for verifier tests; never release evidence."""
import hashlib
import json
from pathlib import Path


def model_evidence(root=None):
    summary = {"run_id": "f" * 32, "complete": True, "invocations": []}
    for index, name in enumerate(("world", "finance", "source-review")):
        iid = str(index) * 32
        files = {}
        blobs = {"stdin.bin": b"synthetic prompt", "schema.bin": b"{}", "output.bin": b"{}",
                 "stdout.bin": b"", "stderr.bin": b""}
        for filename, blob in blobs.items():
            files[filename] = {"sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
        manifest = {"schema_version": 1, "invocation_id": iid, "pass_name": name,
                    "outcome": "success", "returncode": 0, "timeout_s": 100, "files": files}
        raw = json.dumps(manifest).encode()
        summary["invocations"].append({"invocation_id": iid, "pass_name": name, "outcome": "success",
                                       "manifest_sha256": hashlib.sha256(raw).hexdigest()})
        if root is not None:
            target = Path(root) / summary["run_id"] / iid
            target.mkdir(parents=True)
            (target / "manifest.json").write_bytes(raw)
            for filename, blob in blobs.items():
                (target / filename).write_bytes(blob)
    return summary
