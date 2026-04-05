"""Generate typed config models and JSON schema from the memory registry.

Usage:
    python -m spectragent.agents.memory.generate_config
"""

import json
import sys

from conscribe import discover, generate_layer_config_source, generate_layer_json_schema

from spectragent import GENERATED_DIR
from spectragent.agents.memory._registrar import MemoryRegistrar

EXPECTED_MEMORY_TYPES = 4


def main() -> None:
    # 1. Discover all memory modules to trigger registration
    imported = discover("spectragent.agents.memory")
    print(f"Discovered {len(imported)} modules under spectragent.agents.memory")

    # 2. Build discriminated-union config
    result = MemoryRegistrar.build_config()
    keys = MemoryRegistrar.keys()
    print(f"Registered {len(keys)} memory types: {keys}")

    # 3. Generate Python source
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    py_source = generate_layer_config_source(result)
    py_path = GENERATED_DIR / "memory_config.py"
    py_path.write_text(py_source)
    print(f"Wrote {py_path}")

    # 4. Generate JSON Schema
    schema = generate_layer_json_schema(result)
    schema_path = GENERATED_DIR / "memory_config.schema.json"
    schema_path.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"Wrote {schema_path}")

    # 5. Verify
    if len(keys) != EXPECTED_MEMORY_TYPES:
        print(
            f"WARNING: expected {EXPECTED_MEMORY_TYPES} memory types, got {len(keys)}",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()
