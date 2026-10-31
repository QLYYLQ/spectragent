"""BrowserEnvironment — single-capability browser environment.

Delegates everything to the injected ``BrowserCapability``: the canonical
environment of browser benchmarks. It lives in the browser package, not in
the framework's ``core/environments/``: a framework class wired to the
``browser`` capability key would break the catalog of every run that names
no browser, since a run imports only the capability packages its
configuration names (``docs/architecture_design.md`` §4.2.1, §4.3).
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
