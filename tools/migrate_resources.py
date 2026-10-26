"""Relocate an explicit asset inventory while preserving every byte."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS = ("analysis.md", "finance.md", "world.md", "world.base.md")
SCHEMAS = ("briefing.schema.json", "finance.schema.json", "rewrite.schema.json", "world.schema.json",
           "world.base.template", "national.first.template", "national.next.template",
           "national.required.first.template", "national.required.next.template")
PROFILES = ("example.full.yaml", "example.world.yaml", "example.minimal.yaml")


def main():
    rows = ([(ROOT / "scripts/prompts" / name, "prompts/" + name, True) for name in PROMPTS]
            + [(ROOT / "schema" / name, "schema/" + name, True) for name in SCHEMAS]
            + [(ROOT / "config" / name, "profiles/" + name, False) for name in PROFILES])
    prepared = []
    for source, relative, move in rows:
        destination = ROOT / "worldintel/data" / relative
        if not source.is_file() or source.is_symlink() or destination.exists():
            raise ValueError(f"resource source/destination check failed: {relative}")
        if not source.resolve().is_relative_to(ROOT) or not destination.resolve().is_relative_to(ROOT / "worldintel/data"):
            raise ValueError("resource path escapes checkout")
        prepared.append((source, destination, source.read_bytes(), move))
    for source, destination, blob, move in prepared:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as stream:
            stream.write(blob)
        if destination.read_bytes() != blob:
            raise ValueError("resource byte verification failed")
        if move:
            source.unlink()
    print(f"Verified {len(prepared)} byte-preserving resource relocations/copies")


if __name__ == "__main__":
    main()
