"""Environment Protocol — contract for all environment implementations.

An Environment is a Capability combiner and router. It aggregates
action spaces from injected Capabilities, routes actions via prefix
dispatch, and delegates observation resolution to Capability resolvers.

See docs/architecture_design.md §4.3 and docs/execution_dag.md M07.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from spectragent.schemas.action import Action, ActionDescriptor, ActionResult


@runtime_checkable
class Environment(Protocol):
    """Contract for all environment implementations.

    Attributes:
        env_type: Registry discriminator — matches ``__registry_key__``.
    """

    env_type: str

    id: str | None
    """Instance identity — distinct from ``env_type`` (class identity).
    Two ``browser`` environments can coexist in one run as long as their
    ``id`` differs.
    """

    # ── Capability discovery ──────────────────────────────────

    capabilities: dict[str, Any]
    """Mapping from **capability instance id** to the wired
    :class:`BaseCapability` instance. Populated by
    :class:`ConfigComposer` via setattr at compose time — same IoC
    pattern as ``agent.llm`` and ``capability.backend``.

    The key is the *instance* id (``browser#0``), not the
    ``capability_id`` class key. The values must still have distinct
    ``capability_id``s: action prefix routing goes through
    :meth:`capability_ids`, which reads ``capability_id`` off the values,
    and observation fragment keys are class-scoped the same way — so two
    instances of one capability class have no way to be addressed apart
    and are rejected when the environment is built.
    """

    def capability_ids(self) -> frozenset[str]:
        """Return the set of registered capability IDs."""
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
    ) -> Any:
        """Return the current observation, delegating to Capability resolvers.

        Args:
            negotiated: NegotiationResult from the three-party negotiation.
                Typed as Any to avoid circular imports.

        Returns:
            A dynamically-built NegotiatedObservation Pydantic model instance.
        """
        ...

    # ── Action execution ──────────────────────────────────────

    async def act(self, action: Action) -> ActionResult:
        """Execute an action, routing via prefix to the owning Capability.

        Deliberately takes no routing handle. Which sub-context a
        capability acts in is that capability's own state; broadcasting
        one scalar to every capability would mean the environment picking
        a tenancy dimension on behalf of domains it knows nothing about.
        """
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

    async def setup(
        self,
        context: Mapping[str, Any] | None = None,
        *,
        negotiation: Any = None,
    ) -> None:
        """Initialize the environment and its capabilities.

        Args:
            context: Task context — typically
                ``TaskLoader.get_task_context(task_id)``. Routed
                **per capability**, never broadcast: a key is delivered
                only to capabilities whose ``setup()`` declares it, and a
                key nobody declares is an error. Nest a mapping under a
                capability id (``{"browser": {"timeout": 5}}``) to
                configure one capability privately when two of them want
                the same option name.
            negotiation: Observation negotiation result for this run,
                handed to every capability so it can skip resolving
                fragments nobody asked for.
        """
        ...

    async def teardown(self) -> None:
        """Tear down the environment and its capabilities.

        Must be safe to call after a :meth:`setup` that raised part way
        through, and after no setup at all — that is what lets
        ``AgentRuntime`` release the capabilities a failed setup already
        brought up instead of orphaning them.
        """
        ...

    @property
    def is_alive(self) -> bool:
        """Whether the environment is still operational."""
        ...
