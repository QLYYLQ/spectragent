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
from spectragent.schemas.verdict import Judgment

if TYPE_CHECKING:
    from spectragent.benchmarks._setup_registrar import BenchmarkSetup
    from spectragent.core.archive._registrar import ArchiverBase
    from spectragent.verifiers.base import LiveVerifier


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

    The evidence is the capability's own type: a class implementing this
    names a subclass of
    :class:`~spectragent.schemas.restoration.RestorationEvidence` as its
    ``RestorationEvidence`` class attribute — the facts it measures, the
    limits its checks apply (from its own configuration) and the checks
    themselves (``failed_checks``). The trace reader finds that attribute
    through the capability registry by the evidence's ``capability_id``, as
    it finds an action's ``Effect`` (``docs/architecture_design.md`` §4.5,
    §4.9).
    """

    async def retarget(self, action: Action, recorded: Any, restored: Any) -> Retarget:
        """How the recorded *action* should run against the *restored* observation.

        Computes, never acts: the restorer executes the returned action
        through ``guarded_act``. A ``Retarget`` with a ``failure`` says no
        single target was found (``not_found`` / ``ambiguous``); what that
        costs is the restorer's policy. ``method`` is ``"identity"`` for an
        action run as recorded, else this capability's own name for its way.
        """
        ...

    async def verify_restoration(self, recorded: Any, restored: Any) -> RestorationEvidence:
        """How far the *restored* observation is from the *recorded* one, as this capability's evidence.

        Returns an instance of this class's ``RestorationEvidence`` carrying
        this capability's ``capability_id``. A measure this capability does
        not take, or could not take this time, is ``None`` and decides
        nothing. Its checks are the evidence's own
        (``RestorationEvidence.failed_checks``); ``strict | warn`` is the
        restorer's. A count since the previous call (see the class
        docstring) is reported once: called again, it reads 0.
        """
        ...


@runtime_checkable
class RestorationAlignment(Protocol):
    """Optional: put the next restored observation's state where the recording had it.

    Some environment state moves on its own clock rather than with the
    agent's actions — a video keeps playing while the model thinks, a
    device clock runs on — so re-executing a recorded prefix reaches the
    right screen with that state somewhere else (``docs/architecture_design.md``
    §4.14 "恢复：重定位与校验"). A capability that can put such state back
    implements this: the restorer calls it before each restored observation,
    and the capability aligns inside that observation's own capture (the
    browser: after its settle wait, right before its screenshot) and reports
    what it did with the next ``RestorationSupport.verify_restoration``.
    Whether it aligns at all, and every bound the alignment keeps, is the
    capability's own configuration (the browser's ``align_media``,
    ``media_seek_timeout_s``, ``media_end_lead_s``).

    The request belongs to the next observation only, never a later one, and
    this method changes nothing by itself. Separate from
    :class:`RestorationSupport`: a capability that can retarget and verify
    need not have anything to align. Not part of :class:`BaseCapability`;
    ``CapabilityBase`` gives no default; the environment's fan-out skips a
    capability without it.
    """

    async def align_next_observation(self, recorded: Any) -> None:
        """Align the next observation to *recorded* (a whole observation of this run)."""
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
class FragmentFormats(Protocol):
    """Optional: how this capability's fragments read as text, as it is configured.

    Whoever writes a fragment's model-readable text knows its format; an agent
    that described the format in its own prompt would keep a second copy that
    drifts the moment the writer changes (``docs/architecture_design.md`` §4.4
    "观测格式由提供方描述"). The browser's ``browser.dom`` text is written by
    the configured DOM serializer, which describes itself. A run's
    ``AgentRuntime.prepare()`` reads the descriptions through
    ``Environment.fragment_formats`` and hands an agent the ones it declared it
    quotes. A per-run fact, so it is read once, from configuration, and never
    rides on an observation. Not part of :class:`BaseCapability`;
    ``CapabilityBase`` gives no default: a capability without it describes
    nothing.
    """

    def fragment_formats(self) -> Mapping[str, str]:
        """Fully-qualified fragment key → a short description of its text, for a model.

        Only fragments this capability provides, keyed under its own prefix.
        Reads configuration only: no setup, no backend, before negotiation too.
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


@runtime_checkable
class SetupHost(Protocol):
    """Optional: this capability hosts a benchmark's setup component.

    A benchmark's own setup of each task (AndroidWorld's initialize) is a
    component written under the benchmark that names the capability hosting
    it (``docs/architecture_design.md`` §4.9 "基准组件由 capability 承载").
    ``AgentRuntime.prepare()`` routes it here through the environment: this
    capability's :meth:`check_setup` gets it with the archivers about to be
    bound, and :meth:`bind_setup` is called as the last step, as archivers are
    bound. From the next setup on, the capability gives the component access to
    its device or environment, lets it start the task, asks it first at
    teardown, and answers ``TaskTextFromSetup`` and ``EpisodeEnd`` through it.
    What the device must offer beyond its plain form comes from the component,
    never from a provider setting. Not part of :class:`BaseCapability`;
    ``CapabilityBase`` gives no default.
    """

    def check_setup(
        self,
        context: Mapping[str, Any],
        *,
        negotiation: Any = None,
        archivers: Sequence[ArchiverBase] = (),
        setup: BenchmarkSetup | None = None,
    ) -> None:
        """``SetupCheck.check_setup``, with the setup component about to be bound (``None``: none).

        Refuses, among the rest, a component this capability cannot host and a
        task the component would refuse. Reads configuration and its
        arguments only.
        """
        ...

    def bind_setup(self, setup: BenchmarkSetup | None) -> None:
        """Host *setup* from the next setup on (``None``: host none). Acquires nothing.

        Raises:
            TypeError: *setup* is no component this capability can host.
        """
        ...


@runtime_checkable
class LiveVerifierHost(SetupHost, Protocol):
    """Optional: this capability hosts a benchmark's live verifiers, besides its setup component.

    Some benchmarks can only be judged against the environment itself
    (AndroidWorld reads databases, files and settings on the device), and only
    the capability reaches it. Their verifiers need the live environment and
    name the capability that hosts them (``LiveVerifier``,
    ``docs/architecture_design.md`` §4.9 "基准组件由 capability 承载").
    ``AgentRuntime.prepare()`` routes them here through the environment: this
    capability's :meth:`check_setup` gets them with the setup component and
    the archivers about to be bound, and :meth:`bind_live_verifiers` is called
    as the last step. At the scoring point (§4.2 "判分时间点"), after the Loop
    returned or raised and before the close-out and the teardown, the
    environment asks :meth:`verify_live` once for each, concurrently and each
    within the run's ``verify_timeout``, and the trace writer keeps each
    verdict in the run's verdict store (§5.3 "判定统一存放"). Not part of
    :class:`BaseCapability`; ``CapabilityBase`` gives no default.

    A verifier turns its own transport failures into ordinary exceptions (a
    ``ConnectionError``, say) and never lets a ``CancelledError`` escape: the
    framework takes any ``CancelledError`` for the run being cancelled, closes
    the run out as cancelled and records no verdict, as it does for every
    other capability call.
    """

    def check_setup(
        self,
        context: Mapping[str, Any],
        *,
        negotiation: Any = None,
        archivers: Sequence[ArchiverBase] = (),
        setup: BenchmarkSetup | None = None,
        live_verifiers: Sequence[LiveVerifier] = (),
    ) -> None:
        """``SetupHost.check_setup``, with the live verifiers about to be bound too (``()``: none).

        Refuses, among the rest, a verifier this capability cannot host and one
        whose requirements the run, as configured, cannot meet. A verifier
        only states requirements and never changes the run (§4.10.1 "capability
        配置合并"), so the refusal names the verifier and the requirement
        instead of meeting it. Reads configuration and its arguments only.
        """
        ...

    def bind_live_verifiers(self, verifiers: Sequence[LiveVerifier]) -> None:
        """Host *verifiers* from the next setup on (``()``: host none). Acquires nothing.

        Raises:
            TypeError: One is no verifier this capability can host.
        """
        ...

    async def verify_live(self, verifier: LiveVerifier, *, answer: str | None) -> Judgment:
        """Judge this task with *verifier*, one of those bound here, against the live environment.

        *answer* is the run's final answer: the text the agent handed over with
        ``loop.done``, or ``None`` when the run ended any other way or
        ``loop.done`` carried no text (§4.2). It is only the answer, never the
        agent's reasoning or history; a verifier whose benchmark judges no
        answer ignores it.

        Executes none of the run's actions and tears nothing down; handing
        *answer* to the benchmark's own judge is part of judging. The judgment
        holds only what the verifier found: its score, what it judged. The
        environment adds who judged (the verifier's ``identity()``), the
        attachment, and when it was asked and how long it took, on its own
        clock; it enforces the time limit and turns a timeout or an exception
        into a not-judged verdict.
        """
        ...


@runtime_checkable
class TaskTextFromSetup(Protocol):
    """Optional: this capability's setup completes the run's task text.

    Some benchmarks make a task's text in the environment as it starts
    (AndroidWorld's MiniWoB++ pages write their instruction then); the
    loader's text is only the part known before. ``AgentRuntime.prepare()``
    asks, through ``Environment.task_text_setter``, whether this task's setup
    completes it, and after setup reads the text through
    ``Environment.task_text_from_setup``, records it and hands it to the
    agent as the run's task (``docs/architecture_design.md`` §4.2, §4.10).
    The environment adds this capability's id. Not part of
    :class:`BaseCapability`; ``CapabilityBase`` gives no default.

    Transport failures become ordinary exceptions; a ``CancelledError`` of
    its own never escapes (the ``LiveVerifierHost`` convention).
    """

    def sets_task_text(
        self, context: Mapping[str, Any], *, setup: BenchmarkSetup | None = None
    ) -> bool:
        """Whether setup, with *context*, completes the run's task text.

        *context* is this capability's routed slice of the task's setup
        context; *setup* is the benchmark setup component about to be bound
        to it (``SetupHost``), ``None`` when it hosts none. ``prepare()`` asks
        before it binds anything, so a host answers for *setup*, never for
        what an earlier run bound. Reads configuration and its arguments only,
        like ``check_setup``: no backend, no state written. It must agree with
        setup: a ``True`` here and no text from :meth:`task_text_from_setup`
        after setup fails the run's setup.
        """
        ...

    async def task_text_from_setup(self) -> str | None:
        """After setup: the task text it completed, exactly as the agent gets it; ``None`` when it completed none."""
        ...


@runtime_checkable
class EpisodeEnd(Protocol):
    """Optional: whether the task's episode is over, as the benchmark decides.

    Some benchmarks end an episode themselves: AndroidWorld's runner asks a
    MiniWoB++ page after every step whether it reported a reward, and ends the
    episode when it has. The Loop asks, through ``Environment.episode_over``,
    after every step of the agent's that did not end the run, and ends the run
    as ``episode_over`` when it is (``docs/architecture_design.md`` §4.1).
    Only whether it is over: the run is still judged at the scoring point
    (:class:`LiveVerifierHost`). Not part of :class:`BaseCapability`;
    ``CapabilityBase`` gives no default.

    Transport failures become ordinary exceptions; a ``CancelledError`` of
    its own never escapes (the ``LiveVerifierHost`` convention).
    """

    async def episode_over(self) -> bool:
        """Whether this task's episode is over; ``False`` for a task its benchmark never ends.

        Asked after every step, so a task whose benchmark ends no episode
        answers without touching the environment. Reads only: changes nothing.
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
