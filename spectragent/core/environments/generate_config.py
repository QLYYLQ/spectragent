"""Auto-generate typed Environment config from registered implementations.

Standalone script — NOT imported by __init__.py. Run manually to
produce the discriminated-union config class.

Usage::

    python -m spectragent.core.environments.generate_config
"""

from __future__ import annotations

from pathlib import Path

from conscribe import discover, generate_layer_config_source

from spectragent.core.environments._registrar import EnvironmentRegistrar

_GENERATED_DIR = Path(__file__).resolve().parents[2] / "generated"


def main() -> None:
    discover("spectragent.core.environments")
    source = generate_layer_config_source(EnvironmentRegistrar)
    out = _GENERATED_DIR / "environment_config.py"
    out.write_text(source, encoding="utf-8")
    print(f"Generated {out}")


if __name__ == "__main__":
    main()
