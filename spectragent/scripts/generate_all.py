"""Generate the composed config + per-layer stubs in one pass.

Replaces the nine per-layer ``generate_config.py`` scripts. Run it whenever
a registrar gains or loses a registered class::

    uv run spectragent config generate          # via pyproject [project.scripts]
    uv run python -m spectragent.scripts.generate_all

Outputs (under ``spectragent/generated/``):

- ``_composed.py`` — a single ``ComposedConfig`` Pydantic model with a
  ``list[layer_union]`` field per registrar and wired fields inlined as
  nested discriminated unions.
- ``_composed.schema.json`` — JSON Schema for the same.
- ``.fingerprint.json`` — content fingerprint used by CI to assert the
  generated file is in sync with the current registry state.

Only injected attributes absent from source declarations need ``.pyi`` stubs.
Obsolete conscribe-generated stubs are pruned; all current receptors are typed
in the source, so the current build needs none.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from conscribe import (
    build_composed_config,
    compute_registry_fingerprint,
    discover,
    generate_composed_config_source,
    generate_composed_json_schema,
)
from conscribe.stubs import write_layer_stubs

from spectragent import GENERATED_DIR, PACKAGE_ROOT
from spectragent.core.registries import collect_registrars


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spectragent config generate",
        description="Regenerate composed configuration, schema and registry fingerprints.",
    )
    parser.parse_args(argv)
    discover("spectragent", exclude=["spectragent.generated", "spectragent.scripts"])
    registrars = collect_registrars()
    # Snapshot declarations before rendering: pydantic can normalize numeric
    # constraint metadata (0 to 0.0) while building the composed schema.
    fingerprints: dict[str, str] = {
        layer: compute_registry_fingerprint(reg) for layer, reg in registrars.items()
    }
    fingerprint_path = GENERATED_DIR / ".fingerprint.json"
    fingerprint = json.dumps(fingerprints, indent=2, sort_keys=True) + "\n"

    result = build_composed_config(registrars, inline_wiring=True, top_level_extra="forbid")

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    source_path = GENERATED_DIR / "_composed.py"
    source = generate_composed_config_source(result)

    schema_path = GENERATED_DIR / "_composed.schema.json"
    schema = json.dumps(generate_composed_json_schema(result), indent=2) + "\n"

    # Only undeclared injected attributes need stubs in conscribe 2.0.
    # Prune obsolete generated stubs so they cannot shadow typed source.
    # ``write_layer_stubs`` takes one registrar at a time; omit
    # ``output_dir`` so stubs land next to the source files.
    stub_paths: list[Path] = []
    for reg in registrars.values():
        stub_paths.extend(write_layer_stubs(reg, prune=True))

    for path, content in (
        (source_path, source),
        (schema_path, schema),
        (fingerprint_path, fingerprint),
    ):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content)
        temporary.replace(path)

    print(f"Wrote {source_path}")
    print(f"Wrote {schema_path}")
    print(f"Wrote {fingerprint_path}")
    print(f"Stubs ({len(stub_paths)}) written under {PACKAGE_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
