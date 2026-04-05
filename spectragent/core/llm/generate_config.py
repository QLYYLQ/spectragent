"""Generate typed config models and JSON schema from the LLM provider registry.

Usage:
    python -m spectragent.core.llm.generate_config
"""

import json
import sys

from conscribe import discover, generate_layer_config_source, generate_layer_json_schema

from spectragent import GENERATED_DIR
from spectragent.core.llm._registrar import LLMRegistrar

EXPECTED_MODEL_TYPES = 15


def main() -> None:
    # 1. Discover all provider modules to trigger @register decorators
    imported = discover("spectragent.core.llm")
    print(f"Discovered {len(imported)} modules under spectragent.core.llm")

    # 2. Build discriminated-union config
    result = LLMRegistrar.build_config()
    keys = LLMRegistrar.keys()
    print(f"Registered {len(keys)} model_types: {keys}")

    # 3. Generate Python source
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    py_source = generate_layer_config_source(result)
    py_path = GENERATED_DIR / "llm_config.py"
    py_path.write_text(py_source)
    print(f"Wrote {py_path}")

    # 4. Generate JSON Schema
    schema = generate_layer_json_schema(result)
    schema_path = GENERATED_DIR / "llm_config.schema.json"
    schema_path.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"Wrote {schema_path}")

    # 5. Verify
    if len(keys) != EXPECTED_MODEL_TYPES:
        print(
            f"WARNING: expected {EXPECTED_MODEL_TYPES} model_types, got {len(keys)}",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()
