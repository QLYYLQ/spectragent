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
        id: Instance identity. Two capabilities of the same class can
            coexist in one run as long as their ``id`` differs.
    """

    capability_id: str
    id: str | None

    # ── Action group ──────────────────────────────────────────

    def get_action_descriptors(self, **context: Any) -> list[ActionDescriptor]:
        """Declare the actions this capability provides.

        The ``**context`` parameter supports dynamic action spaces: for
        ToolSandbox, each task defines different available tools, so the
        descriptor list is generated per-task. For browser, the list is
        static.
        """
        ...

    async def execute_action(self, action: Action) -> ActionResult:
        """Execute one action this capability owns.

        The Environment routes by the action name's leading segment and
        passes the action through unchanged; the capability looks it up
        by its full registry key.

        No routing handle is passed. Sub-context selection (tab,
        container, window) is this capability's own state, changed by one
        of its own actions.
        """
        ...

    # ── Observation resolver ──────────────────────────────────

    @property
    def provided_fragments(self) -> frozenset[str]:
        """Observation fragment keys this capability will resolve.

        Before ``setup()`` this is the capability's full resolver set —
        that is what the negotiation engine (M02) consumes to decide
        which fragments are available. Once ``setup(negotiation=...)``
        has run it narrows to the negotiated active set, so per-step
        resolver cost tracks what the agent and loop actually asked for.
        """
        ...

    async def resolve_fragment(self, fragment_key: str) -> Any:
        """Resolve one observation fragment on demand.

        Called by Environment.observe() for env-sourced fragments whose
        key is in the negotiation result's active set.

        Returns an ObservationFragment instance (typed as Any here to
        avoid circular imports — the negotiation engine handles typing).
        """
        ...

    # ── Lifecycle ─────────────────────────────────────────────

    async def setup(self, *, negotiation: Any = None) -> None:
        """Initialize the capability (e.g. launch browser, connect Docker).

        ``negotiation`` is the observation negotiation result for this
        run. Capabilities use it to skip work for fragments nobody
        requested — without it a vision-only agent still pays for a full
        DOM extraction on every step.

        **The rest of the signature is the capability's public setup
        vocabulary.** Every task-context option a capability accepts must
        be a named keyword-only parameter here, because that signature is
        exactly what :meth:`Environment.setup` reads to decide which keys
        it may route to this capability — and a key no capability names
        is reported as an error instead of vanishing. A ``**kwargs``
        catch-all opts out of that check and re-opens the silent-typo
        hole; concrete capabilities should not have one.
        """
        ...

    async def teardown(self) -> None:
        """Tear down the capability (e.g. close browser, disconnect)."""
        ...

    @property
    def is_alive(self) -> bool:
        """Whether this capability is still operational."""
        ...
