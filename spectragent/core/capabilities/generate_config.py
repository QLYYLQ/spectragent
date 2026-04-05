"""Auto-generate typed Capability config from registered implementations.

Run this script to produce the discriminated-union config class at
``spectragent/generated/capability_config.py``.

Usage::

    python -m spectragent.core.capabilities.generate_config
"""

from __future__ import annotations

from pathlib import Path

from conscribe import discover, generate_layer_config_source

from spectragent.core.capabilities._registrar import CapabilityRegistrar

_GENERATED_DIR = Path(__file__).resolve().parents[2] / "generated"


def main() -> None:
    # Trigger auto-registration of all Capability subclasses
    discover("spectragent.core.capabilities")

    source = generate_layer_config_source(CapabilityRegistrar)
    out = _GENERATED_DIR / "capability_config.py"
    out.write_text(source, encoding="utf-8")
    print(f"Generated {out}")


if __name__ == "__main__":
    main()
