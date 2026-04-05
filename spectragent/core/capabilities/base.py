"""BaseCapability Protocol — contract for all capabilities.

A Capability is the atomic unit of environment ability. It unifies:
- **ActionGroup**: declares and executes actions within its scope
- **Resolver**: provides observation fragments on demand

Capabilities are flat peers (browser, docker, tool_sandbox, gpu_profiling,
desktop_vm, android) — no inheritance hierarchy. Each manages its own
backend lifecycle independently.

See docs/architecture_design.md §4.6 and docs/execution_dag.md M06.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from spectragent.schemas.action import Action, ActionDescriptor, ActionResult


@runtime_checkable
class BaseCapability(Protocol):
    """Contract for all environment capabilities.

    Attributes:
        capability_id: Registry discriminator — matches ``__registry_key__``.
    """

    capability_id: str

    # ── Action group ──────────────────────────────────────────

    def get_action_descriptors(self, **context: Any) -> list[ActionDescriptor]:
        """Declare the actions this capability provides.

        The ``**context`` parameter supports dynamic action spaces: for
        ToolSandbox, each task defines different available tools, so the
        descriptor list is generated per-task. For browser, the list is
        static.
        """
        ...

    async def execute_action(
        self, action: Action, **kwargs: Any
    ) -> ActionResult:
        """Execute a single **unprefixed** action.

        Environment handles prefix stripping before calling this method.
        The action name is always local to this capability's scope.
        """
        ...

    # ── Observation resolver ──────────────────────────────────

    @property
    def provided_fragments(self) -> frozenset[str]:
        """Observation fragment keys this capability can resolve.

        Derived from ``__wiring__["observation"]`` on CapabilityBase
        subclasses. Used by the negotiation engine (M02) to determine
        which fragments are available.
        """
        ...

    async def resolve_fragment(
        self, fragment_key: str, **kwargs: Any
    ) -> Any:
        """Resolve one observation fragment on demand.

        Called by Environment.observe() for env-sourced fragments whose
        key is in the negotiation result's active set.

        Returns an ObservationFragment instance (typed as Any here to
        avoid circular imports — the negotiation engine handles typing).
        """
        ...

    # ── Lifecycle ─────────────────────────────────────────────

    async def setup(self, config: dict[str, Any] | None = None, **kwargs: Any) -> None:
        """Initialize the capability (e.g. launch browser, connect Docker)."""
        ...

    async def teardown(self) -> None:
        """Tear down the capability (e.g. close browser, disconnect)."""
        ...

    @property
    def is_alive(self) -> bool:
        """Whether this capability is still operational."""
        ...
