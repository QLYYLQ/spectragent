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

from typing import Any, ClassVar, Protocol, runtime_checkable

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

    __invalidated_by_foreign_actions__: ClassVar[bool]
    """Does an action executed by **another** capability invalidate this
    one's current capture?

    Default ``False``: capabilities are independent worlds. A
    ``browser.click`` does not change what an Android emulator is showing,
    so it must not cost an ADB round trip — and the environment must not
    guess otherwise on a capability's behalf.

    Set it to ``True`` when two capabilities genuinely share one surface
    (a desktop VM whose screen also hosts the browser; two views of one
    container's filesystem). The environment then opens a fresh
    observation cycle on this capability after every foreign action, which
    is exactly as expensive as re-capturing says it is.
    """

    # ── Action group ──────────────────────────────────────────

    @property
    def provided_actions(self) -> frozenset[str]:
        """Fully-qualified action keys this capability will execute.

        The action-axis twin of :attr:`provided_fragments`, and declared
        here for the same reason: :attr:`Environment.provided_actions`
        unions this across capabilities and hands the result to action
        negotiation, so it is part of the contract a capability owes the
        environment — not an implementation detail of the concrete base.

        It was missing from this Protocol while ``CapabilityBase``
        implemented it, which left the two axes asymmetric (AGENTS.md
        §2.5) and made a *structural* implementer — the usage
        Protocol-First exists to allow — type-check clean and then fail
        at negotiation.
        """
        ...

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

    async def begin_observation(self, cycle: int) -> None:
        """Open observation cycle *cycle*; discard the previous capture.

        **This is the observation cycle contract, and it is the caller's
        statement, not the capability's inference.**
        :meth:`Environment.observe` owns a monotonically increasing cycle
        counter, increments it once per observation, and calls this on
        every capability taking part *before* any fragment of that cycle
        is resolved. Between two of these calls, every
        :meth:`resolve_fragment` belongs to one observation:

        - A capability that captures many fragments in one round trip
          (browser: one ``BrowserStateRequestEvent`` carries DOM +
          screenshot + tab list) captures **once** per cycle and serves
          the rest from that capture, however many times each key is
          asked for.
        - The next cycle must re-capture. A fragment served across a cycle
          boundary from the previous capture is stale data presented as
          current, and the trace cannot tell the difference afterwards.

        Default is a no-op: a capability that captures per fragment holds
        no cycle state and needs nothing from this. Capabilities must not
        infer the boundary from their own history — "have I served this
        key before" is a guess about a fact the caller already had, and it
        is wrong for any caller that reads one fragment twice.

        Raising here fails this capability's whole slice of the
        observation, and is treated exactly like a failed resolve.
        """
        ...

    async def resolve_fragment(self, fragment_key: str) -> Any:
        """Resolve one observation fragment on demand.

        Called by Environment.observe() for env-sourced fragments whose
        key is in the negotiation result's active set, after
        :meth:`begin_observation` has opened the cycle. All of one
        capability's fragments for one cycle are resolved in sequence —
        they share one capture — while different capabilities run
        concurrently.

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
