"""BrowserEnvironment — single-capability browser environment.

Delegates everything to the injected ``BrowserCapability``. Serves as the
canonical single-cap env for browser benchmarks and the fixture that
populates ``EnvironmentRegistrar`` for composed-config generation.
"""

from __future__ import annotations

from typing import Any, ClassVar

from spectragent.core.environments._registrar import EnvironmentBase


class BrowserEnvironment(EnvironmentBase):
    """Browser env — wires exactly one browser capability."""

    __registry_key__: ClassVar[str] = "browser"
    __wiring__: ClassVar[dict[str, Any]] = {
        "capabilities": ("capability", ["browser"]),
    }
