"""AndroidEnvironment — the single-capability Android environment.

Delegates everything to the injected ``AndroidCapability``. It lives in the
Android package, as ``BrowserEnvironment`` lives in the browser's: a framework
class wired to the ``android`` capability key would break the catalog of every
run that names no Android (``docs/architecture_design.md`` §4.2.1, §11.2).
"""

from __future__ import annotations

from typing import Any, ClassVar

from spectragent.core.environments._registrar import EnvironmentBase


class AndroidEnvironment(EnvironmentBase):
    """Android env — wires exactly one Android capability."""

    __registry_key__: ClassVar[str] = "android"
    __wiring__: ClassVar[dict[str, Any]] = {
        "capabilities": ("capability", ["android"]),
    }
