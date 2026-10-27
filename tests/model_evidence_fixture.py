"""Synthetic private model bytes for verifier tests; never release evidence."""
import hashlib
import json
from pathlib import Path


def model_evidence(root=None, *, repaired=False, cleanup=False):
    summary = {"run_id": "f" * 32, "complete": True, "invocations": []}
    from worldintel import data
    from worldintel.core import grounding_review as g
    material = {"cards": [{"card_id": "brief:0", "headline": "Bridge opens", "why_it_matters": "Traffic can cross.",
                          "body": "Officials opened a bridge on Monday.", "source_ids": ["source:0"]}],
                "sources": [{"source_id": "source:0", "url": "https://example.test/bridge", "title": "Opening",
                             "content": "Officials opened a bridge on Monday. Traffic can cross."}]}
    def digest_cards(value):
        for c in value["cards"]:
            c["prose_sha256"] = g._digest(json.dumps({f: c[f] for f in g.FIELDS}, ensure_ascii=False, sort_keys=True))
    digest_cards(material)
    material["sources"][0]["sha256"] = g._digest(material["sources"][0]["content"])
    def response(value, verdict="supported"):
        c, source = value["cards"][0], value["sources"][0]
        return {"cards": [{"card_id": c["card_id"], "prose_sha256": c["prose_sha256"], "verdict": verdict,
                           "reason": "Source reports the opening.", "claims": [{"field": "body", "text": c["body"],
                            "source_id": source["source_id"], "source_sha256": source["sha256"],
                            "quote": "Officials opened a bridge on Monday."}]}]}
    generation, bundle = None, None
    if cleanup:
        import copy
        generation = {'briefing': {'brief': [{**{f: material['cards'][0][f] for f in g.FIELDS},
                       'sources': [{'url': 'https://example.test/bridge'}, {'url': 'https://example.test/empty'}]}]}}
        bundle = {'news': [{'url': 'https://example.test/bridge', 'title': 'Opening', 'tier': 1,
                           'content': material['sources'][0]['content'] + ' reporting' * 150},
                          {'url': 'https://example.test/empty', 'tier': 1, 'content': '',
                           'enrichment': {'reason': 'below_content_threshold'}}]}
        staged, preflight = g._preflight(generation, bundle)
        material = g._material(staged, bundle)
        material['citation_preflight'] = preflight
    initial = response(material, "uncertain" if repaired else "supported")
    captures = {"source-review": ("source-review", material, initial)}
    if repaired:
        import copy
        c = material["cards"][0]
        patch = {"cards": [{"card_id": c["card_id"], "prose_sha256": c["prose_sha256"], "headline": "Bridge opened",
                           "why_it_matters": "Traffic can cross.", "body": " ".join([c["body"]] * 18)}]}
        final = copy.deepcopy(material)
        final["cards"][0].update({f: patch["cards"][0][f] for f in g.FIELDS})
        digest_cards(final)
        captures["source-correction"] = ("source-correction", g._correction_material(material, initial), patch)
        captures["source-review-final"] = ("source-review", final, response(final))
    for index, name in enumerate(("world", "finance", *captures)):

        iid = str(index) * 32
        files = {}
        blobs = {"stdin.bin": b"synthetic prompt", "schema.bin": b"{}", "output.bin": b"{}",
                 "stdout.bin": b"", "stderr.bin": b""}
        if cleanup and name == 'world':
            blobs['stdin.bin'] = ('synthetic world context\n## TODAY (BUNDLE)\n' + json.dumps(bundle)).encode()
            blobs['output.bin'] = json.dumps(generation).encode()
        elif cleanup and name == 'finance':
            blobs['output.bin'] = json.dumps({'briefing': {'finance': {'brief': []}}}).encode()
        if name in captures:
            template, value, output = captures[name]
            blobs["stdin.bin"] = g._prompt(template, value).encode("utf-8")
            with data.as_file(f"schema/{template}.schema.json") as schema:
                blobs["schema.bin"] = Path(schema).read_bytes()
            blobs["output.bin"] = json.dumps(output).encode("utf-8")
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
