"""Auto-generate typed Action config from registered action implementations.

Run this script to produce the discriminated-union config class at
``spectragent/generated/action_config.py``.

Usage::

    python -m spectragent.core.actions.generate_config
"""

from __future__ import annotations

from pathlib import Path

from conscribe import discover, generate_layer_config_source

from spectragent.core.actions._registrar import ActionRegistrar

_GENERATED_DIR = Path(__file__).resolve().parents[2] / "generated"


def main() -> None:
    # Trigger auto-registration of all concrete Action subclasses.
    # discover() is only used in codegen (never in hot paths).
    discover("spectragent.core.capabilities.browser.actions")
    # Future: discover("spectragent.core.capabilities.docker.actions")
    # Future: discover("spectragent.core.capabilities.tool_sandbox.actions")

    result = ActionRegistrar.build_config()
    keys = ActionRegistrar.keys()
    print(f"Registered {len(keys)} actions: {keys}")

    _GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    source = generate_layer_config_source(result)
    out = _GENERATED_DIR / "action_config.py"
    out.write_text(source, encoding="utf-8")
    print(f"Generated {out}")


if __name__ == "__main__":
    main()
