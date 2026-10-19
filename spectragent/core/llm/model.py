"""Shared provider resources; invocation contracts belong to the two model branches."""

import math
from typing import TYPE_CHECKING

from spectragent.core.llm.client_resources import ClientResources
from spectragent.core.llm.lifecycle import ClientCleanupDiagnostic


class ModelBase:
    """Implementation reuse for providers, independent of either registrar."""

    if TYPE_CHECKING:
        model: str

    @property
    def provider(self) -> str:
        """The registrar's route component, also for providers with a default route."""
        return str(getattr(type(self), "__registry_key__", "")).partition(".")[2]

    @property
    def client_resources(self) -> ClientResources:
        resources = getattr(self, "_client_resources", None)
        if resources is None:
            resources = ClientResources(str(self.model))
            self._client_resources = resources
            self._client_cleanup_diagnostics = ()
        return resources

    async def aclose(self, *, timeout: float = 5.0) -> tuple[ClientCleanupDiagnostic, ...]:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Model cleanup timeout must be finite and positive")
        resources = getattr(self, "_client_resources", None)
        if resources is None:
            return getattr(self, "_client_cleanup_diagnostics", ())
        try:
            diagnostics = await resources.aclose(timeout=timeout)
            self._client_cleanup_diagnostics = diagnostics
            return diagnostics
        finally:
            if self._client_resources is resources:
                self._client_resources = None
