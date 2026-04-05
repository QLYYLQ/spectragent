"""Generate typed config models and JSON schema from the browser provider registry.

Usage:
    python spectragent/core/providers/browser/generate_config.py
"""

import json
import sys

from conscribe import discover, generate_layer_config_source, generate_layer_json_schema

from spectragent import GENERATED_DIR
from spectragent.core.providers.browser._registrar import BrowserProviderRegistrar

_EXPECTED_PROVIDERS = 7  # lexmount, browser_use, hyperbrowser, agentbay, airtop, skyvern, local_chrome


def main() -> None:
    # 1. Discover all provider modules to trigger registration
    imported = discover("spectragent.core.providers.browser")
    print(f"Discovered {len(imported)} modules under core.providers.browser")

    # 2. Build discriminated-union config
    result = BrowserProviderRegistrar.build_config()
    keys = BrowserProviderRegistrar.keys()
    print(f"Registered {len(keys)} providers: {keys}")

    # 3. Generate Python source
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    py_source = generate_layer_config_source(result)
    py_path = GENERATED_DIR / "browser_provider_config.py"
    py_path.write_text(py_source)
    print(f"Wrote {py_path}")

    # 4. Generate JSON Schema
    schema = generate_layer_json_schema(result)
    schema_path = GENERATED_DIR / "browser_provider_config.schema.json"
    schema_path.write_text(json.dumps(schema, indent=2) + "\n")
    print(f"Wrote {schema_path}")

    # 5. Verify
    if len(keys) != _EXPECTED_PROVIDERS:
        print(
            f"WARNING: expected {_EXPECTED_PROVIDERS} providers, got {len(keys)}",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Done.")


if __name__ == "__main__":
    main()
