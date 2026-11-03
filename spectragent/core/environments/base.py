"""Environment Protocol — contract for all environment implementations.

An Environment is a Capability combiner and router. It aggregates
action spaces from injected Capabilities, routes actions via prefix
dispatch, and delegates observation resolution to Capability resolvers.

See docs/architecture_design.md §4.3 and docs/execution_dag.md M07.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from spectragent.schemas.action import Action, ActionDescriptor, ActionResult

if TYPE_CHECKING:
    from spectragent.benchmarks._setup_registrar import BenchmarkSetup
    from spectragent.core.archive._registrar import ArchiverBase
    from spectragent.core.archive.types import ArchiveBatch
    from spectragent.core.capabilities.base import PreparedSetupContext
    from spectragent.schemas.restoration import RestorationEvidence, Retarget
    from spectragent.schemas.task import SetupTaskText
    from spectragent.schemas.verdict import Verdict


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

    # ── The three provides axes ───────────────────────────────
    #
    # Declared alike, all three, and all fully qualified. ``provided_actions``
    # used to live only on ``EnvironmentBase`` while ``negotiate_action``
    # read it off whatever environment it was handed — so a structural
    # implementer of this Protocol type-checked clean and then failed at
    # negotiation. The archive axis is declared complete from its first day.

    @property
    def provided_fragments(self) -> frozenset[str]:
        """Observation fragment keys provided by all capabilities combined.

        Used by AgentRuntime for three-party observation negotiation.
        """
        ...

    @property
    def provided_actions(self) -> frozenset[str]:
        """Action keys executable by all capabilities combined.

        What ``negotiate_action`` resolves the agent's declared actions
        against.
        """
        ...

    @property
    def provided_archivers(self) -> frozenset[str]:
        """Archiver keys provided by the capabilities that archive.

        The union over capabilities implementing
        :class:`~spectragent.core.archive.protocols.ArchivingCapability`;
        a capability that does not contributes nothing.
        """
        ...

    # ── Archive (environment evidence) ────────────────────────

    def bind_archivers(self, archivers: Sequence[ArchiverBase]) -> None:
        """Route each archiver to its capability by key prefix, and bind.

        The same routing as :meth:`act`: the leading segment of
        ``archiver_id`` names the capability. Validates every archiver
        before binding any — an unroutable or unprovided key raises with
        nothing bound — then hands every archiving capability its group,
        an empty one to those that get none (which clears an earlier
        binding). Acquires no resource: ``AgentRuntime.prepare`` calls it
        as its last step, on a dry run too, before setup.

        Raises:
            UnroutableArchiverError: A key whose prefix names no
                capability, whose capability does not archive, or that
                the capability does not provide.
        """
        ...

    async def collect_archive(self) -> ArchiveBatch:
        """Pull everything the bound capabilities captured since the last pull.

        Takes nothing — no step number or cycle ever reaches a capability;
        "what step t's commit takes belongs to step t" is the consumer's
        rule. With nothing bound, returns an empty batch without touching
        any capability. Otherwise collects from every bound capability
        concurrently, waits for all of them, and merges their batches in
        capability order. A capability that failed (raised, or handed over
        another capability's file or gap) contributes one ``ArchiveGap``
        under its id instead of evidence; the healthy batches are kept.
        """
        ...

    # ── Observation ───────────────────────────────────────────

    async def observe(
        self,
        negotiated: Any,
    ) -> Any:
        """Return the current observation, delegating to Capability resolvers.

        **One call is one observation cycle.** The environment owns a
        monotonically increasing cycle counter, increments it here, and
        announces it to every participating capability
        (:meth:`BaseCapability.begin_observation`) before resolving
        anything — which is what makes "a capability may capture once and
        serve many fragments from it" a contract rather than a convention
        one capability happens to follow. A caller that wants fresh data
        calls ``observe()`` again; nothing else opens a cycle.

        Capabilities are resolved concurrently and each capability's own
        fragments in sequence, so the wall-clock cost of an observation is
        its slowest capability rather than the sum, and each fragment
        carries the time it was captured (``FragmentBase.captured_at``) so
        the skew between capabilities is visible in the run and in the
        trace.

        A capability that raises while owing a **required** fragment fails
        the observation; one that raises while owing only optional
        fragments has those slots recorded as
        :class:`~spectragent.schemas.observation.failure.FragmentFailure`
        and everything its peers captured survives. Which is which comes
        from ``negotiated.required_fragments`` — the environment decides
        nothing here.

        Args:
            negotiated: NegotiationResult from the three-party negotiation.
                Typed as Any to avoid circular imports.

        Returns:
            A dynamically-built NegotiatedObservation Pydantic model instance.

        Raises:
            ObservationFailedError: A required fragment could not be
                captured.
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

    # ── Checkpoint restoration (only a restorer calls these) ──

    async def retarget(self, action: Action, recorded: Any, restored: Any) -> Retarget:
        """How a recorded *action* should run in the restored environment.

        Routed by the action's prefix exactly like :meth:`act`, to a
        capability implementing ``RestorationSupport``
        (``core/capabilities/base.py``). An action no such capability owns —
        a capability without the protocol, a bare name, an unknown prefix —
        runs as recorded: ``Retarget(action=<a copy>, method="identity")``,
        so ``guarded_act`` refuses it again exactly as it did in the source
        run. Never executes anything.
        """
        ...

    async def verify_restoration(self, recorded: Any, restored: Any) -> list[RestorationEvidence]:
        """What each ``RestorationSupport`` capability measured, in capability order.

        *recorded* and *restored* are whole observations of this run. Each
        entry is its capability's own evidence type, carrying its checks.
        Empty when no capability implements the protocol: there is nothing
        to check. ``strict | warn`` is the restorer's.
        """
        ...

    async def align_next_observation(self, recorded: Any) -> None:
        """Ask every ``RestorationAlignment`` capability to align the next observation to *recorded*.

        Fans out in capability order; nothing happens without an implementer.
        What to align is each capability's own configuration. The alignment
        runs inside the next observation's capture and is reported by the
        ``verify_restoration`` after it; the request never reaches a later
        observation.
        """
        ...

    def get_action_space(
        self,
        *,
        active_actions: frozenset[str] | None = None,
        **context: Any,
    ) -> list[ActionDescriptor]:
        """Aggregate action descriptors from all capabilities.

        Descriptors already carry fully-qualified registry keys; the
        environment preserves their names when aggregating them.

        Args:
            active_actions: Optional set of fully-qualified action names
                from action negotiation. Only descriptors in that set are
                included (the Loop's own ``loop.*`` actions are never an
                environment's); ``None`` disables narrowing.
            **context: Forwarded to each capability.
        """
        ...

    def fragment_formats(self) -> dict[str, str]:
        """How each described fragment reads as text: fully-qualified key → description.

        The union over capabilities implementing
        :class:`~spectragent.core.capabilities.base.FragmentFormats`; a
        capability without it describes nothing. ``AgentRuntime.prepare()``
        reads it once and hands an agent the descriptions of the fragments
        its prompt quotes. Reads configuration only: no setup, no backend.

        Raises:
            ValueError: A capability described a key outside its own prefix.
        """
        ...

    # ── The scoring point: after the Loop, before the close-out ─

    async def verify_live(self, *, timeout_s: float, answer: str | None) -> list[Verdict]:
        """Every live verdict on this task, in capability order; ``[]`` when nothing judges it live.

        ``AgentRuntime`` calls it once on every run that reaches the scoring
        point (after the Loop returned or raised, before ``on_run_end`` and
        teardown), so a structural implementation needs it too. Asks every
        capability implementing ``TaskOracle`` (``core/capabilities/base.py``)
        whose oracle judges the run, concurrently and each within
        *timeout_s*, handing each the run's final *answer* (``loop.done``'s
        text; ``None`` when the run ended without one). A judgment comes back
        as a ``judged`` live verdict under the oracle's identity, with the
        start time and duration measured around the call, and a timeout or an
        ``Exception`` as a ``not_judged`` one. Never raises ``Exception``. A
        cancellation cancels the oracles still running, waits for them, and
        then propagates.
        """
        ...

    # ── The task text a setup completes; the episode the environment ends ──

    def task_text_setter(
        self,
        context: Mapping[str, Any] | None = None,
        *,
        setups: Sequence[BenchmarkSetup] = (),
    ) -> str | None:
        """The capability whose setup completes this task's text; ``None`` when none does.

        ``AgentRuntime.prepare()`` asks it after :meth:`check_setup`, with the
        same context and the same benchmark setup components, before it binds
        them (``docs/architecture_design.md`` §4.2, §4.10). The context is
        routed as :meth:`setup` routes it and the components as
        :meth:`check_setup` routes them, and every capability implementing
        ``TaskTextFromSetup`` (``core/capabilities/base.py``) answers for its
        own slice and the component it is about to host. Reads configuration
        and task data only, so a structural implementation needs it too:
        Runtime asks on every run.

        Raises:
            ValueError: A context key no capability accepts, a setup component
                no capability here hosts, or more than one capability would
                complete the text: a run has one task text.
        """
        ...

    async def task_text_from_setup(self) -> SetupTaskText | None:
        """After setup: the task text a capability's setup completed, with its id; ``None`` when none did.

        Asks every ``TaskTextFromSetup`` capability in capability order. Public
        so the trace hook records it, as it records :meth:`verify_live`.

        Raises:
            ValueError: More than one capability completed a text, or one
                completed a blank one.
        """
        ...

    async def episode_over(self) -> bool:
        """Whether the task's episode is over: some ``EpisodeEnd`` capability says so.

        The Loop asks after every step of the agent's that did not end the run
        (``docs/architecture_design.md`` §4.1), so a structural implementation
        needs it too. Asks in capability order, one at a time, and stops at the
        first ``True``; with no such capability it touches none and answers
        ``False``. A capability's exception and a cancellation propagate.
        """
        ...

    # ── Lifecycle ─────────────────────────────────────────────

    def prepare_setup_context(
        self, context: Mapping[str, Any] | None = None
    ) -> PreparedSetupContext:
        """Route setup keys to their owners before preparing execution and provenance.

        Calls each capability's optional SetupContextPreparation operation on
        its effective slice. Runtime reuses the execution result for validation
        and setup, and sends only the recordable result to lifecycle recording.
        """
        ...

    def check_setup(
        self,
        context: Mapping[str, Any] | None = None,
        *,
        negotiation: Any = None,
        archivers: Sequence[ArchiverBase] = (),
        setups: Sequence[BenchmarkSetup] = (),
    ) -> None:
        """Refuse now, acquiring nothing, what :meth:`setup` would refuse for this task.

        ``AgentRuntime.prepare()`` calls it — before the trace is opened and
        before anything is launched, on a dry run too — with the task's setup
        context, the observation negotiation result, and the archivers and
        benchmark setup components it is about to bind. The context is routed
        exactly as :meth:`setup` routes it (a key no capability's ``setup()``
        declares is refused), each archiver goes to the capability its prefix
        names, each setup component to the capability it names, and every
        capability implementing ``SetupCheck`` or ``SetupHost``
        (``core/capabilities/base.py``) checks its own slice.

        Raises:
            ValueError: A colliding capability set, a context key no
                capability accepts, or a setup component no capability here
                hosts.
            UnroutableArchiverError: An archiver that cannot be bound here.
            Exception: A capability's own refusal, naming both parties
                (browser: ``BrowserCooperationError``).
        """
        ...

    def bind_setups(self, setups: Sequence[BenchmarkSetup]) -> None:
        """Route each benchmark setup component to the capability it names, and bind.

        Validates every component before binding any, as :meth:`bind_archivers`
        does, then hands every hosting capability its component, ``None`` to
        those that get none. Acquires no resource: ``AgentRuntime.prepare``
        calls it as its last step, with the archivers, only when the run's
        benchmark has a setup component.

        Raises:
            ValueError: A component whose capability is not here, does not
                host setup components, or is named by two components.
        """
        ...

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
