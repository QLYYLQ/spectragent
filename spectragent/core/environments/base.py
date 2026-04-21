"""Environment Protocol — contract for all environment implementations.

An Environment is a Capability combiner and router. It aggregates
action spaces from injected Capabilities, routes actions via prefix
dispatch, and delegates observation resolution to Capability resolvers.

See docs/architecture_design.md §4.3 and docs/execution_dag.md M07.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from spectragent.schemas.action import Action, ActionDescriptor, ActionResult


@runtime_checkable
class Environment(Protocol):
    """Contract for all environment implementations.

    Attributes:
        env_type: Registry discriminator — matches ``__registry_key__``.
    """

    env_type: str

    # ── Capability discovery ──────────────────────────────────

    def capabilities(self) -> frozenset[str]:
        """Return capability IDs aggregated from injected capabilities."""
        ...

    def has_capability(self, cap_id: str) -> bool:
        """Check if a specific capability is available."""
        ...

    @property
    def provided_fragments(self) -> frozenset[str]:
        """Observation fragment keys provided by all capabilities combined.

        Used by AgentRuntime for three-party observation negotiation.
        """
        ...

    # ── Observation ───────────────────────────────────────────

    async def observe(
        self,
        negotiated: Any,
        session_id: str | None = None,
    ) -> Any:
        """Return the current observation, delegating to Capability resolvers.

        Args:
            negotiated: NegotiationResult from the three-party negotiation.
                Typed as Any to avoid circular imports.
            session_id: Optional session/tab routing.

        Returns:
            A dynamically-built NegotiatedObservation Pydantic model instance.
        """
        ...

    # ── Action execution ──────────────────────────────────────

    async def act(
        self,
        action: Action,
        session_id: str | None = None,
    ) -> ActionResult:
        """Execute an action, routing via prefix for composite environments."""
        ...

    def get_action_space(
        self,
        *,
        active_actions: frozenset[str] | None = None,
        **context: Any,
    ) -> list[ActionDescriptor]:
        """Aggregate action descriptors from all capabilities.

        Multi-capability environments auto-prefix descriptors.

        Args:
            active_actions: Optional set of unprefixed action names
                (from two-party action negotiation). Descriptors are
                filtered by unprefixed name *before* any prefix is
                applied, keeping the contract symmetric with
                :attr:`provided_actions`.
            **context: Forwarded to each capability.
        """
        ...

    # ── Lifecycle ─────────────────────────────────────────────

    async def setup(self, **kwargs: Any) -> None:
        """Initialize the environment and its capabilities."""
        ...

    async def teardown(self) -> None:
        """Tear down the environment and its capabilities."""
        ...

    @property
    def is_alive(self) -> bool:
        """Whether the environment is still operational."""
        ...
