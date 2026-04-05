"""Generate typed config models and JSON schema from the agent registry.

Usage:
    python -m spectragent.agents.generate_config
"""

import json
import sys

from conscribe import discover, generate_layer_config_source, generate_layer_json_schema

from spectragent import GENERATED_DIR
from spectragent.agents._registrar import AgentRegistrar

EXPECTED_AGENTS = 3


def main() -> None:
    # 1. Discover all agent modules to trigger registration
    imported = discover("spectragent.agents")
    print(f"Discovered {len(imported)} modules under spectragent.agents")

    # 2. Build discriminated-union config
    result = AgentRegistrar.build_config()
    keys = AgentRegistrar.keys()
    print(f"Registered {len(keys)} agents: {keys}")

    # 3. Generate Python source
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    py_source = generate_layer_config_source(result)
    py_path = GENERATED_DIR / "agent_config.py"
    py_path.write_text(py_source)
    print(f"Wrote {py_path}")

    # 4. Generate JSON Schema
    schema = generate_layer_json_schema(result)
    schema_path = GENERATED_DIR / "agent_config.schema.json"
    schema_path.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"Wrote {schema_path}")

    # 5. Verify
    if len(keys) != EXPECTED_AGENTS:
        print(
            f"WARNING: expected {EXPECTED_AGENTS} agents, got {len(keys)}",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()
