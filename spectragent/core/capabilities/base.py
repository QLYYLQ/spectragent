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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.restoration import RestorationEvidence, Retarget

if TYPE_CHECKING:
    from spectragent.core.archive._registrar import ArchiverBase


@runtime_checkable
class BaseCapability(Protocol):
    """Contract for all environment capabilities.

    Attributes:
        capability_id: Registry discriminator — matches ``__registry_key__``.
        id: Instance identity. Two capabilities of the same class can
            coexist in one run when wired into separate environments;
            each environment routes at most one instance per capability_id.
    """

    @property
    def capability_id(self) -> str:
        """Registry identity, read from the implementation's registered key."""
        ...

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


@runtime_checkable
class RestorationSupport(Protocol):
    """Optional: how a recorded run is resumed in this capability's world.

    A branch run (``BranchLoop``, ``docs/architecture_design.md`` §4.1)
    re-executes a recorded prefix in a freshly started environment. Some
    recorded actions name things that are new in every environment — the
    browser's element index is Chrome's ``backendNodeId``, a tab id is a
    fresh CDP targetId — and only the capability that issued them knows how
    to find the same thing again, or how to tell whether the restored world
    looks like the recorded one. A capability that knows implements this;
    one that does not writes nothing: ``EnvironmentBase.retarget`` /
    ``verify_restoration`` supply the identity / "nothing to check"
    defaults (§4.3 "检查点恢复的路由"). It is not part of
    :class:`BaseCapability` and ``CapabilityBase`` gives no default.

    Both methods read the two observations they are handed — each a whole
    ``NegotiatedObservation`` of this run; the capability reads its own
    fragments by their local names — and never execute anything, change the
    environment, or know a step number. ``retarget`` reads nothing else.
    ``verify_restoration`` may also report a count no observation carries,
    as the increase since its previous call (the browser's replay-log
    document misses: the capability keeps the total it last reported). Such
    a measure makes the call order-dependent, not idempotent: a restorer
    calls it exactly once per restored observation, in order.
    """

    async def retarget(self, action: Action, recorded: Any, restored: Any) -> Retarget:
        """How the recorded *action* should run against the *restored* observation.

        Computes, never acts: the restorer executes the returned action
        through ``guarded_act``. A ``Retarget`` with a ``failure`` says no
        single target was found (``not_found`` / ``ambiguous``); what that
        costs is the restorer's policy.
        """
        ...

    async def verify_restoration(self, recorded: Any, restored: Any) -> RestorationEvidence:
        """How far the *restored* observation is from the *recorded* one.

        Measures, never judges: thresholds and ``strict | warn`` are the
        restorer's. A measure this capability does not take, or could not
        take this time, is ``None``. A count since the previous call (see
        the class docstring) is reported once: called again, it reads 0.
        """
        ...


@runtime_checkable
class RestorationAlignment(Protocol):
    """Optional: put the next restored observation's state where the recording had it.

    Some environment state moves on its own clock rather than with the
    agent's actions — a video keeps playing while the model thinks — so
    re-executing a recorded prefix reaches the right page with the player at
    another position (``docs/architecture_design.md`` §4.14 "恢复：重定位与校验").
    A capability that can put such state back implements this: the restorer
    (``media: align``) calls it before each restored observation, and the
    capability aligns inside that observation's own capture — after its
    settle wait, right before its screenshot — and reports what it did with
    the next ``RestorationSupport.verify_restoration``.

    The request belongs to the next observation only, never a later one, and
    this method changes nothing by itself. Separate from
    :class:`RestorationSupport`: a capability that can retarget and verify
    need not have anything to align. Not part of :class:`BaseCapability`;
    ``CapabilityBase`` gives no default; the environment's fan-out skips a
    capability without it.
    """

    async def align_next_observation(self, recorded: Any, *, media_seek_timeout_s: float) -> None:
        """Align the next observation's media to *recorded* (a whole observation of this run).

        ``media_seek_timeout_s`` bounds each seek (and the play after it) the
        alignment makes; one that does not complete in time is undone and
        reported as failed.
        """
        ...


@dataclass(frozen=True)
class CaptureComparison:
    """How a recorded capture compares with what this configuration would capture.

    Attributes:
        mismatches: One line per difference: the fragment, the setting and
            both values. Any mismatch means the recorded observation is not
            what this configuration would show an agent.
        unchecked: Settings that cannot be compared from configuration alone,
            each with the reason — not differences.
    """

    mismatches: tuple[str, ...] = ()
    unchecked: tuple[str, ...] = ()


@runtime_checkable
class CaptureCheck(Protocol):
    """Optional: would this capability, configured as it is, capture what was recorded?

    The training export renders samples with a student's run configuration
    from observations another configuration recorded (arch §4.15). A
    screenshot pipeline that differs — format, viewport, device scale factor,
    image size — trains the student on images it will never see when it
    serves, and only the capability knows what its configuration captures.
    A capability that can tell implements this; one that cannot writes
    nothing: ``EnvironmentBase.compare_capture`` routes by key prefix and a
    capability without the protocol contributes nothing (§4.3). Not part of
    :class:`BaseCapability`; ``CapabilityBase`` gives no default.
    """

    def compare_capture(self, recorded: Mapping[str, FragmentBase]) -> CaptureComparison:
        """Compare this capability's fragments in *recorded* with its configuration.

        *recorded* holds one step's recorded env-source fragments, keyed
        fully qualified; the capability reads the keys under its own prefix.
        Reads configuration and the fragments only: no setup, no backend, no
        cache.
        """
        ...


@runtime_checkable
class SetupCheck(Protocol):
    """Optional: refuse now, acquiring nothing, what this capability's setup would refuse.

    Whether the configured parts can cooperate — with each other, with the
    task's setup context, with the run's observation negotiation and with the
    archivers about to be bound — is decided by configuration and task data
    alone, so it is refused at ``AgentRuntime.prepare()``: before the trace
    is opened, before anything is launched, on a dry run too
    (``docs/architecture_design.md`` §4.2, §4.3). ``Environment.check_setup``
    routes each capability its slice and calls this. A capability that can
    tell implements it; one that cannot writes nothing and is checked for
    context routing only. ``setup()`` may call it again as a defensive
    re-check, with the archivers bound by then. Not part of
    :class:`BaseCapability`; ``CapabilityBase`` gives no default.
    """

    def check_setup(
        self,
        context: Mapping[str, Any],
        *,
        negotiation: Any = None,
        archivers: Sequence[ArchiverBase] = (),
    ) -> None:
        """Raise, naming both parties of every problem, if setup would refuse this run.

        Args:
            context: This capability's routed slice of the task's setup
                context — only keys its ``setup()`` declares.
            negotiation: The run's observation negotiation result.
            archivers: The archivers about to be bound to this capability.

        Reads configuration and its arguments only: no backend, no connection,
        no state written.
        """
        ...


@dataclass(frozen=True)
class PreparedSetupContext:
    """Execution inputs and recordable provenance after their owner prepared them.

    Fields are fixed; opaque domain values retain their own mutability semantics.
    A capability that needs a durable snapshot must freeze its own input values.
    """

    execution: Mapping[str, Any]
    recordable: Mapping[str, Any]


@runtime_checkable
class SetupContextPreparation(Protocol):
    """Optional owner-specific preparation, after environment setup-key routing.

    The environment calls once per prepare. This operation may read task input
    files but never acquires backend resources. Ordinary capabilities need no
    implementation: their routed values are passed through in both projections.
    """

    def prepare_setup_context(self, context: Mapping[str, Any]) -> PreparedSetupContext:
        """Prepare owned execution input and the provenance a recorder may persist."""
        ...
