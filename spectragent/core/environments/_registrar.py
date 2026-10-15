"""Environment registrar — conscribe-driven auto-registration.

EnvironmentBase implements prefix routing for action dispatch and
observation delegation. Concrete subclasses declare capabilities
via ``__wiring__`` and set ``__registry_key__``.

Registration is import-driven. Use ``discover("spectragent.core.environments")``
to scan and register all concrete Environment implementations.

See docs/execution_dag.md M07 for the registrar design.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, ClassVar, cast

from conscribe import create_registrar
from pydantic import Field

from spectragent.core.archive._registrar import ArchiverBase
from spectragent.core.archive.protocols import ArchivingCapability
from spectragent.core.archive.types import ArchiveBatch, ArchiveContractError
from spectragent.core.capabilities.base import (
    BaseCapability,
    CaptureCheck,
    CaptureComparison,
    PreparedSetupContext,
    RestorationSupport,
    SetupCheck,
    SetupContextPreparation,
)
from spectragent.core.environments.base import Environment
from spectragent.core.environments.errors import (
    ArchiveCollectionError,
    ObservationFailedError,
    UnroutableArchiverError,
    UnroutableFragmentError,
)
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.observation._registrar import fragment_field_name
from spectragent.schemas.observation.dynamic_model import (
    build_observation_model_from_result,
)
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.restoration import RestorationEvidence, Retarget

logger = logging.getLogger(__name__)

EnvironmentRegistrar = create_registrar(
    "environment",
    Environment,
    discriminator_field="env_type",
)

#: Framework-owned ``setup()`` parameters — never task-context keys.
_RESERVED_SETUP_PARAMS = frozenset({"self", "negotiation"})


def _setup_keys(capability: BaseCapability) -> frozenset[str]:
    """Task-context keys a capability's ``setup()`` declares by name.

    A ``**kwargs`` catch-all contributes nothing: it names no option, so
    it claims none. That is the whole point — the pre-F8 base and browser
    capabilities both ended in ``**kwargs``, which is why a typo'd key
    was accepted by every capability and acted on by none.
    """
    try:
        sig = inspect.signature(capability.setup)
    except (TypeError, ValueError):  # pragma: no cover — exotic callables
        return frozenset()
    return frozenset(
        name
        for name, param in sig.parameters.items()
        if name not in _RESERVED_SETUP_PARAMS
        and param.kind in (param.KEYWORD_ONLY, param.POSITIONAL_OR_KEYWORD)
    )


class EnvironmentBase(metaclass=EnvironmentRegistrar.Meta):
    """Concrete base for all environment implementations.

    Action and observation keys are fully qualified at the registry level
    (``source.action_id`` / ``source.fragment_id``) so the environment
    doesn't apply any runtime prefixing. ``act()`` still routes by
    splitting the leading segment of the action name to pick the owning
    capability, but the action itself is passed through unchanged.

    Subclasses must set:
    - ``__registry_key__``: conscribe key (e.g. ``"browser"``)
    - ``__wiring__``: capability requirements

    Example::

        class BrowserEnvironment(EnvironmentBase):
            __registry_key__ = "browser"
            __wiring__ = {
                "capability": ("capability", ["browser"]),
            }
    """

    __abstract__ = True
    __config_annotated_only__ = True
    __registry_key__: ClassVar[str]
    __wiring__: ClassVar[dict[str, Any]] = {}

    # dict-form annotation tells the Composer this wired receptor is multi-
    # instance. Keys are capability **instance ids** (``browser#0``); the
    # values must have *distinct* ``capability_id``s, because every routing
    # key the environment exposes — action prefixes (``browser.click``) and
    # observation fragment keys (``browser.dom``) — is scoped to the
    # capability *class*, not the instance. See :meth:`_by_capability_id`.
    capabilities: dict[str, BaseCapability]

    def __init__(
        self,
        id: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Instance identity. Unique across the whole config; "
                    "defaults to '<env_type>#<ordinal>' when omitted."
                ),
            ),
        ] = None,
        capabilities: Annotated[
            dict[str, BaseCapability] | None,
            Field(
                default=None,
                description=(
                    "Capabilities this environment owns. Omit to adopt every "
                    "compatible capability the rest of the config builds; "
                    "declare inline to bind them explicitly (required once a "
                    "run holds more than one environment)."
                ),
            ),
        ] = None,
    ) -> None:
        self.id = id
        self._fragment_router: dict[str, BaseCapability] | None = None
        self._capability_router: dict[str, BaseCapability] | None = None
        #: Observation cycle counter — see :meth:`observe`. Per instance,
        #: never module state: two environments in one run each count
        #: their own observations, and a fresh run starts at zero.
        self._observation_cycle: int = 0
        #: Capabilities that received a non-empty archiver group in the
        #: last :meth:`bind_archivers`, in capability order — exactly the
        #: ones :meth:`collect_archive` pulls from. Replaced whole by
        #: every bind.
        self._archiving: list[BaseCapability] = []
        if capabilities is not None:
            self.capabilities = capabilities
            # Reject a colliding capability set at construction rather
            # than at the first ``act()``, which is several minutes and
            # one launched browser too late.
            self._by_capability_id()

    # ── Registry property ─────────────────────────────────────

    @property
    def env_type(self) -> str:
        return self.__registry_key__

    # ── Capability discovery ──────────────────────────────────

    def _by_capability_id(self) -> dict[str, BaseCapability]:
        """Reverse index ``capability_id -> capability`` for prefix routing.

        ``self.capabilities`` is keyed by *instance* id; action names are
        fully qualified with the *class* key (``browser.click``), so
        routing needs this projection. Built lazily and cached.

        Two instances of the same capability class are rejected here.
        The projection used to be a last-wins comprehension, which
        composed and ran without a word while sending every action to
        the last instance, resolving fragments from whichever instance
        happened to win each key, and advertising every action to the
        model twice — with the losing instance's Chrome fully launched,
        held for the whole run and unreachable. Nothing in the routing
        vocabulary can tell two ``browser`` instances apart, so the
        honest answer is to refuse the config.

        Raises:
            ValueError: Two capability instances share a ``capability_id``.
        """
        if self._capability_router is None:
            by_cap_id: dict[str, list[str]] = {}
            for inst_id, cap in self.capabilities.items():
                by_cap_id.setdefault(cap.capability_id, []).append(inst_id)
            collisions = {k: v for k, v in by_cap_id.items() if len(v) > 1}
            if collisions:
                where = getattr(type(self), "__name__", "Environment")
                detail = "; ".join(
                    f"{cap_id!r} claimed by {sorted(inst_ids)}"
                    for cap_id, inst_ids in sorted(collisions.items())
                )
                raise ValueError(
                    f"{where}: capability instances collide on capability_id "
                    f"({detail}). Action names ('browser.click') and fragment "
                    f"keys ('browser.dom') are scoped to the capability class, "
                    f"so two instances of one class cannot be addressed apart "
                    f"— the extra instance would be started and then never "
                    f"reached. Use one instance per capability class, or give "
                    f"each environment its own."
                )
            self._capability_router = {cap.capability_id: cap for cap in self.capabilities.values()}
        return self._capability_router

    def capability_ids(self) -> frozenset[str]:
        return frozenset(self._by_capability_id())

    def has_capability(self, cap_id: str) -> bool:
        return cap_id in self._by_capability_id()

    @property
    def provided_fragments(self) -> frozenset[str]:
        """Union of all capabilities' provided_fragments."""
        fragments: set[str] = set()
        for cap in self.capabilities.values():
            fragments |= cap.provided_fragments
        return frozenset(fragments)

    @property
    def provided_actions(self) -> frozenset[str]:
        """Union of all capabilities' provided_actions.

        Keys are fully qualified (``source.action_id``) and symmetric
        to :attr:`provided_fragments`.
        """
        actions: set[str] = set()
        for cap in self.capabilities.values():
            actions |= cap.provided_actions
        return frozenset(actions)

    @property
    def provided_archivers(self) -> frozenset[str]:
        """Union of the archiving capabilities' provided_archivers.

        The third provides axis, aggregated like the other two. Only a
        capability implementing
        :class:`~spectragent.core.archive.protocols.ArchivingCapability`
        contributes — the archive surface is optional, and a structural
        capability without it simply provides no archivers.
        """
        archivers: set[str] = set()
        for cap in self.capabilities.values():
            if isinstance(cap, ArchivingCapability):
                archivers |= cap.provided_archivers
        return frozenset(archivers)

    # ── Action space aggregation ──────────────────────────────

    def get_action_space(
        self,
        *,
        active_actions: frozenset[str] | None = None,
        **context: Any,
    ) -> list[ActionDescriptor]:
        """Aggregate descriptors from all capabilities, narrowed by the
        negotiation result.

        Descriptors carry the fully-qualified action name from the
        registry (``source.action_id``); no runtime re-prefixing.

        Args:
            active_actions: Optional set of fully-qualified action names
                produced by :func:`negotiate_action`. When provided,
                descriptors whose name is not in the set are filtered
                out. ``None`` disables narrowing.
            **context: Forwarded to each capability's
                ``get_action_descriptors`` call.
        """
        descriptors: list[ActionDescriptor] = []
        for cap in self.capabilities.values():
            for desc in cap.get_action_descriptors(**context):
                if active_actions is not None and desc.name not in active_actions:
                    continue
                descriptors.append(desc)
        return descriptors

    # ── Action execution (prefix routing) ─────────────────────

    async def act(self, action: Action) -> ActionResult:
        """Route a fully-qualified action to the owning Capability.

        The capability is identified by the leading segment of
        ``action.name`` (``browser.click`` → ``browser``). The action
        itself is passed through unchanged — capabilities look it up by
        its full registry key.

        Nothing else is passed. This used to forward a session/tab handle
        to whichever capability matched, which meant a browser tab id
        could be handed to a tool sandbox and a run-scoped id could be
        handed to the browser (where it fails every action as an unknown
        session). Sub-context is capability state.
        """
        prefix, sep, _rest = action.name.partition(".")
        if not sep:
            return ActionResult(
                success=False,
                error=(
                    f"Action name must be fully qualified ('source.action_id'), got '{action.name}'"
                ),
            )
        cap = self._by_capability_id().get(prefix)
        if cap is None:
            return ActionResult(
                success=False,
                error=(
                    f"No capability handles prefix '{prefix}'. "
                    f"Available: {sorted(self.capability_ids())}"
                ),
            )
        result = await cap.execute_action(action)
        await self._invalidate_foreign_captures(cap)
        return result

    async def _invalidate_foreign_captures(self, actor: BaseCapability) -> None:
        """Open a fresh cycle on capabilities this action may have changed.

        The policy is the capability's own, declared as
        ``__invalidated_by_foreign_actions__`` and **False by default**:
        capabilities are independent worlds, so a ``browser.click`` costs
        an Android capability nothing. A capability that shares a surface
        with its peers (a desktop VM whose screen also hosts the browser)
        sets it to ``True`` and pays for a re-capture per foreign action.

        The acting capability is not in scope: invalidating its own
        capture after its own action is its business, and it is the one
        that knows whether the action changed anything.

        What this buys, precisely, so nobody mistakes it for more:
        ``observe()`` opens a new cycle on every participating capability
        anyway, so for a plain observe → act → observe loop this changes
        nothing. It is the guarantee for everything that reads a fragment
        *between* two observes — an action implementation resolving one,
        a loop taking several actions per step — which is exactly where a
        stale capture is invisible.

        A failure to open the cycle here is logged, not raised: the action
        already happened, its result is owed to the caller, and the next
        ``observe()`` opens a strictly newer cycle on the same capability,
        which invalidates it again.
        """
        affected = [
            cap
            for cap in self.capabilities.values()
            if cap is not actor and getattr(type(cap), "__invalidated_by_foreign_actions__", False)
        ]
        if not affected:
            return
        self._observation_cycle += 1
        cycle = self._observation_cycle
        errors = await asyncio.gather(*(self._begin_cycle(cap, cycle) for cap in affected))
        for cap, exc in zip(affected, errors, strict=True):
            if exc is not None:
                logger.warning(
                    "Capability %r failed to open cycle %d after a foreign "
                    "action (%s: %s) — its next observation opens a newer one",
                    cap.capability_id,
                    cycle,
                    type(exc).__name__,
                    exc,
                )

    # ── Checkpoint restoration: routed like ``act``, read-only ──

    async def retarget(self, action: Action, recorded: Any, restored: Any) -> Retarget:
        """Route a recorded action's retarget to the capability that owns it.

        The same prefix routing as :meth:`act` (``browser.click`` →
        ``browser``). A capability implementing
        :class:`~spectragent.core.capabilities.base.RestorationSupport`
        answers; for anything else — a capability without the protocol, a
        bare name, a prefix no capability owns — the action runs as
        recorded (``method="identity"``, a deep copy): a replay reproduces
        the source run's execution, including the actions ``guarded_act``
        refused there, which it then refuses again. Nothing is executed and
        nothing is cached.
        """
        prefix, sep, _rest = action.name.partition(".")
        cap = self._by_capability_id().get(prefix) if sep else None
        if isinstance(cap, RestorationSupport):
            return await cap.retarget(action, recorded, restored)
        return Retarget(action=action.model_copy(deep=True), method="identity")

    async def verify_restoration(self, recorded: Any, restored: Any) -> list[RestorationEvidence]:
        """Each restoring capability's measurement, in capability order.

        Fans out to every capability implementing ``RestorationSupport``,
        one at a time; each answers with its own ``capability_id``. No
        implementer means nothing to check: an empty list. Thresholds and
        verdicts are the restorer's (``docs/architecture_design.md`` §4.3).
        """
        evidence: list[RestorationEvidence] = []
        for cap in self.capabilities.values():
            if isinstance(cap, RestorationSupport):
                evidence.append(await cap.verify_restoration(recorded, restored))
        return evidence

    # ── Recorded capture vs this configuration (training export) ─

    def compare_capture(self, recorded: Mapping[str, Any]) -> CaptureComparison:
        """Whether this environment, as configured, would capture *recorded*.

        *recorded* is one step's recorded env-source fragments, keyed fully
        qualified. Each key goes to the capability its prefix names (the
        routing :meth:`act` uses); every capability implementing
        :class:`~spectragent.core.capabilities.base.CaptureCheck` compares
        its own group, in capability order, and the mismatches and unchecked
        settings are concatenated in that order. A capability without the
        protocol, or a key no capability owns, contributes nothing — there
        is nothing to compare. Reads configuration and the fragments only:
        no setup, no backend, no cache (``docs/architecture_design.md`` §4.3).
        """
        routing = self._by_capability_id()
        groups: dict[str, dict[str, Any]] = {}
        for key, fragment in recorded.items():
            prefix, sep, _rest = key.partition(".")
            if sep and prefix in routing:
                groups.setdefault(prefix, {})[key] = fragment
        mismatches: list[str] = []
        unchecked: list[str] = []
        for cap in self.capabilities.values():
            group = groups.get(cap.capability_id)
            if group is None or not isinstance(cap, CaptureCheck):
                continue
            comparison = cap.compare_capture(group)
            mismatches.extend(comparison.mismatches)
            unchecked.extend(comparison.unchecked)
        return CaptureComparison(mismatches=tuple(mismatches), unchecked=tuple(unchecked))

    # ── Archive: routing the binding, collecting the evidence ─

    def _archiver_groups(self, archivers: Sequence[ArchiverBase]) -> dict[str, list[ArchiverBase]]:
        """Each archiver under the ``capability_id`` its key's prefix names, in the order given.

        The routing :meth:`bind_archivers` binds by and :meth:`check_setup`
        checks by: the prefix must name a capability here (the routing
        :meth:`act` uses), that capability must implement the archiving
        surface, and it must provide the key. Every problem is collected
        before anything is returned.

        Raises:
            UnroutableArchiverError: Naming every key that cannot be bound
                and why.
        """
        by_cap = self._by_capability_id()
        groups: dict[str, list[ArchiverBase]] = {}
        problems: dict[str, str] = {}
        for archiver in archivers:
            key = archiver.archiver_id
            prefix = key.partition(".")[0]
            cap = by_cap.get(prefix)
            if cap is None:
                problems[key] = f"no capability {prefix!r} in this environment"
            elif not isinstance(cap, ArchivingCapability):
                problems[key] = (
                    f"capability {prefix!r} does not implement the archiving surface "
                    f"(ArchivingCapability)"
                )
            elif key not in cap.provided_archivers:
                problems[key] = (
                    f"capability {prefix!r} does not provide it "
                    f"(it provides {sorted(cap.provided_archivers)})"
                )
            else:
                groups.setdefault(prefix, []).append(archiver)
        if problems:
            raise UnroutableArchiverError(problems, capability_ids=frozenset(by_cap))
        return groups

    def bind_archivers(self, archivers: Sequence[ArchiverBase]) -> None:
        """Route each archiver to its capability by key prefix, then bind.

        Validate first, bind second. Every archiver is checked before any
        capability is called — its key's leading segment must name a
        capability here (the same routing :meth:`act` uses), that
        capability must implement the archiving surface, and it must
        provide the key — and every problem is reported in one
        :class:`UnroutableArchiverError`, with nothing bound. Only then
        does **every** archiving capability receive its group, in the
        order given: the ones that get none receive an empty list, which
        clears whatever an earlier call bound.

        Binding acquires nothing. ``AgentRuntime.prepare`` makes this its
        last statement — on a dry run that never sets up, too — and a
        capability reads the binding in ``setup()`` to decide whether to
        build its capture channel.

        Raises:
            UnroutableArchiverError: Naming every key that cannot be bound
                and why.
        """
        groups = self._archiver_groups(archivers)

        # Reset first, record as each bind succeeds: should a capability's
        # own bind raise half way, collection only ever reaches the
        # capabilities that really hold a binding.
        self._archiving = []
        for cap in self.capabilities.values():
            if not isinstance(cap, ArchivingCapability):
                continue
            group = groups.get(cap.capability_id, [])
            cap.bind_archivers(group)
            if group:
                self._archiving.append(cap)

    async def collect_archive(self) -> ArchiveBatch:
        """Pull every bound capability's evidence; merge in capability order.

        Nothing bound → an empty batch, and no capability is touched: the
        zero-overhead path of a run that records no evidence.

        Otherwise one task per bound capability, run **concurrently**
        (independent worlds — the cost is the slowest, not the sum), and
        every task is waited for before the outcome is decided, so no task
        outlives the call. Each task returns its failure rather than
        raising it: ``gather(..., return_exceptions=True)`` would also
        swallow a ``CancelledError``, and a cancelled run must stay
        cancelled.

        A capability fails by raising, or by handing over a file whose
        ``capability_id`` is not its own — its evidence would land under
        another capability's name, so that is a contract violation
        (:class:`~spectragent.core.archive.types.ArchiveContractError`).
        Any failure raises :class:`ArchiveCollectionError` naming every one,
        in capability order, chained from the first; the healthy batches
        are discarded with it. The archive consumer records this as a
        recording-health failure, independently of the agent's outcome.

        No argument, ever: the environment never hands a step number or a
        cycle to a capability.

        Raises:
            ArchiveCollectionError: One or more capabilities failed.
        """
        bound = list(self._archiving)
        if not bound:
            return ArchiveBatch()
        tasks = [asyncio.ensure_future(self._collect_one(cap)) for cap in bound]
        try:
            outcomes = await asyncio.gather(*tasks)
        except BaseException:
            # Only a cancellation reaches here — ``_collect_one`` returns
            # every ``Exception``. ``gather`` lets the siblings of a child
            # that raised ``CancelledError`` keep running; cancel whatever
            # is still pending and wait it out, so no collection outlives
            # this call, then let the cancellation through unchanged.
            for task in tasks:
                task.cancel()
            await asyncio.wait(tasks)
            raise

        batches: list[ArchiveBatch] = []
        failures: dict[str, BaseException] = {}
        for cap, outcome in zip(bound, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                failures[cap.capability_id] = outcome
                continue
            foreign = [f for f in outcome.files if f.capability_id != cap.capability_id]
            if foreign:
                failures[cap.capability_id] = ArchiveContractError(
                    f"capability {cap.capability_id!r} handed over evidence under "
                    f"another capability's prefix: "
                    f"{sorted({f.archiver_id for f in foreign})}"
                )
                continue
            batches.append(outcome)
        if failures:
            # ``env_type`` is the registry key; the abstract base used
            # directly (tests do) has none, so fall back to the class name.
            where = getattr(self, "env_type", None) or getattr(
                type(self), "__name__", "Environment"
            )
            raise ArchiveCollectionError(where, failures) from next(iter(failures.values()))
        return ArchiveBatch.merge(batches)

    @staticmethod
    async def _collect_one(cap: BaseCapability) -> ArchiveBatch | Exception:
        """Collect from one capability; return its failure instead of raising.

        Catches ``Exception`` only — a ``CancelledError`` (a
        ``BaseException``) propagates, the same shape as
        :meth:`_begin_cycle`. A capability that returns something other
        than an :class:`ArchiveBatch` has broken the contract, and that is
        returned as its failure too.
        """
        # Only capabilities that passed the isinstance check in
        # ``bind_archivers`` are ever collected from.
        archiving = cast(ArchivingCapability, cap)
        try:
            batch = await archiving.collect_archive()
        except Exception as exc:  # per-capability isolation
            return exc
        if not isinstance(batch, ArchiveBatch):
            return ArchiveContractError(
                f"capability {cap.capability_id!r} returned {type(batch).__name__} "
                f"from collect_archive(), not an ArchiveBatch"
            )
        return batch

    # ── Observation delegation ────────────────────────────────

    async def observe(self, negotiated: Any) -> Any:
        """Assemble one observation from the capabilities that own it.

        **The environment owns the observation cycle.** It holds a
        monotonically increasing counter, increments it here — one
        ``observe()`` is one cycle, whatever the loop did or did not do in
        between — and tells every participating capability before anything
        is resolved. That statement is what lets a capability batch: the
        browser captures DOM + screenshot + tabs in one CDP round trip and
        serves them all from it, knowing the next cycle will be announced
        rather than guessed at. Before this existed, the rule lived only
        inside ``BrowserCapability`` as "have I already served this key",
        an inference standing in for information the caller had all along
        — and one that read a fragment fetched twice as a new observation.

        Capabilities run **concurrently**, each capability's own fragments
        **sequentially**. Both halves are load-bearing:

        - Concurrent across capabilities, because they are independent
          worlds: a 0.5 s browser scrape and a 3 s Android capture cost
          3 s, not 3.5 s, and the two samples are taken as close together
          in time as the machine allows.
        - Sequential within a capability, because its fragments share one
          capture. Firing them at once would either duplicate the capture
          or race for it.

        Failures are isolated per fragment, and what a failure *means* is
        what the agent and loop declared: a fragment in
        ``required_fragments`` raises :class:`ObservationFailedError`
        (after every capability has finished, so the message reports them
        all); an optional one lands in the observation as a
        :class:`FragmentFailure` and the rest of the observation survives.
        Nothing is ever fabricated to fill a slot — that is the same
        discipline the LLM layer applies to a failed completion.

        Args:
            negotiated: NegotiationResult from three-party negotiation.

        Returns:
            A NegotiatedObservation Pydantic model instance.
        """
        if self._fragment_router is None:
            self._build_fragment_router()

        slices = self._observation_slices(frozenset(negotiated.env_sourced))
        # Read directly, never through ``getattr(..., frozenset())``. A
        # result without the field would silently mean "nothing is
        # required", which turns a failed *required* DOM into a recorded
        # FragmentFailure and lets the run continue on a slot the reader
        # believes is real data. This invariant must fail closed and loud.
        required = frozenset(negotiated.required_fragments)

        self._observation_cycle += 1
        cycle = self._observation_cycle

        # Phase 1 — every participating capability is told the cycle
        # before any fragment of it is resolved, so a capability can rely
        # on that ordering when it decides what to throw away.
        begin_errors = await asyncio.gather(
            *(self._begin_cycle(cap, cycle) for cap, _keys in slices)
        )
        # Phase 2 — one task per capability; within a task, one fragment
        # at a time.
        harvested = await asyncio.gather(
            *(
                self._resolve_slice(cap, keys, begin_error)
                for (cap, keys), begin_error in zip(slices, begin_errors, strict=True)
            )
        )

        field_values: dict[str, Any] = {}
        failures: dict[str, BaseException] = {}
        owners: dict[str, str] = {}
        for (cap, _keys), (captured, failed) in zip(slices, harvested, strict=True):
            for frag_key, fragment in captured.items():
                # Dynamic-model attribute names use the local segment of
                # the fully-qualified key (``browser.dom`` → ``dom``), so
                # agents keep their ``obs.dom`` access pattern.
                field_values[fragment_field_name(frag_key)] = fragment
            for frag_key, exc in failed.items():
                failures[frag_key] = exc
                owners[frag_key] = cap.capability_id

        if failures:
            fatal = sorted(set(failures) & required)
            if fatal:
                raise ObservationFailedError(cycle, failures, owners, required) from failures[
                    fatal[0]
                ]
            for frag_key, exc in failures.items():
                logger.warning(
                    "observe cycle %d: optional fragment %r from capability %r "
                    "failed (%s: %s) — recorded as a failure, observation "
                    "continues",
                    cycle,
                    frag_key,
                    owners[frag_key],
                    type(exc).__name__,
                    exc,
                )
                field_values[fragment_field_name(frag_key)] = FragmentFailure.from_exception(
                    frag_key, owners[frag_key], exc
                )

        # Which slots may carry a failure is derived by the model builder
        # from the same single field this method read above
        # (``required_fragments``): it widens exactly ``env_sourced -
        # required``. The two are exact complements by construction rather
        # than by agreement — there is one source, read twice, not two
        # computations that have to be kept in step.
        obs_model = build_observation_model_from_result(negotiated)
        return obs_model(**field_values)

    def _observation_slices(
        self, env_sourced: frozenset[str]
    ) -> list[tuple[BaseCapability, list[str]]]:
        """Group the cycle's fragments into one slice per capability.

        Grouping *is* the concurrency plan: one slice is one task, and a
        slice's keys share one capture. Sorted so a run resolves its
        fragments in a stable order — a log of two runs of one config
        should differ where the pages differ, not where a set iterated.

        Raises:
            UnroutableFragmentError: a negotiated fragment no capability
                here owns.
        """
        router = self._fragment_router or {}
        unroutable = env_sourced - router.keys()
        if unroutable:
            raise UnroutableFragmentError(frozenset(unroutable), frozenset(router))
        # Keyed by ``id()`` because a capability is genuinely unhashable:
        # ``CapabilityBase`` is a ``@dataclass`` with the default
        # ``eq=True``, which sets ``__hash__ = None``, so
        # ``{cap: keys}`` raises ``TypeError``. Identity is the right key
        # anyway — two entries of ``capabilities`` are two capabilities
        # even if their fields happen to match.
        by_instance: dict[int, tuple[BaseCapability, list[str]]] = {}
        for frag_key in sorted(env_sourced):
            cap = router[frag_key]
            slice_ = by_instance.get(id(cap))
            if slice_ is None:
                slice_ = (cap, [])
                by_instance[id(cap)] = slice_
            slice_[1].append(frag_key)
        return list(by_instance.values())

    @staticmethod
    async def _begin_cycle(cap: BaseCapability, cycle: int) -> Exception | None:
        """Open *cycle* on one capability; return its failure, if any.

        Returning rather than raising keeps ``gather`` free of
        ``return_exceptions=True``, which would also swallow a
        ``CancelledError`` — a cancelled run must stay cancelled.
        """
        try:
            await cap.begin_observation(cycle)
        except Exception as exc:  # per-capability isolation
            return exc
        return None

    @staticmethod
    async def _resolve_slice(
        cap: BaseCapability,
        keys: list[str],
        begin_error: Exception | None,
    ) -> tuple[dict[str, Any], dict[str, BaseException]]:
        """Resolve one capability's fragments, in order, isolating failures.

        A capability that could not open the cycle resolves nothing: its
        fragments would come from the previous capture, which is the stale
        data this whole contract exists to prevent. They are reported as
        failed, carrying the reason the cycle could not be opened.

        Isolation is per *fragment*, and stays that way on purpose: a
        capability that genuinely captures one fragment at a time has
        independent failures, and short-circuiting its slice would throw
        away data it did capture. The cost of that choice falls on the
        capability that **batches**, and the contract puts it there: a
        shared capture that fails must be recorded for the cycle, so the
        next key of the slice is refused with the same exception rather
        than re-entering the capture. ``BrowserCapability`` does this with
        ``_ObserveCycleCache.error``; before it did, one failed
        three-fragment observation cost three full retry budgets. See
        ``assert_a_failed_cycle_costs_one_capture`` in
        ``tests/_helpers/capability_contract.py`` — every capability is
        run through it.
        """
        captured: dict[str, Any] = {}
        failed: dict[str, BaseException] = {}
        for frag_key in keys:
            if begin_error is not None:
                failed[frag_key] = begin_error
                continue
            try:
                captured[frag_key] = await cap.resolve_fragment(frag_key)
            except Exception as exc:  # per-fragment isolation
                failed[frag_key] = exc
        return captured, failed

    def _build_fragment_router(self) -> None:
        """Build reverse index: fragment_key → Capability."""
        router: dict[str, BaseCapability] = {}
        for cap in self.capabilities.values():
            for frag_key in cap.provided_fragments:
                router[frag_key] = cap
        self._fragment_router = router

    # ── Lifecycle ─────────────────────────────────────────────

    def prepare_setup_context(
        self, context: Mapping[str, Any] | None = None
    ) -> PreparedSetupContext:
        """Prepare only after the same owner routing validation setup uses.

        Flat shared keys still broadcast. If an owner changes or omits such a
        key, only that key is expanded into individual instance namespaces;
        neighboring owners retain their own values. Existing namespace aliases
        are preserved, with effective routed overrides already applied.
        """
        self._by_capability_id()
        original = context or {}
        routed = self.route_setup_context(original)
        prepared = {
            inst_id: (
                cap.prepare_setup_context(routed[inst_id])
                if isinstance(cap, SetupContextPreparation)
                else PreparedSetupContext(dict(routed[inst_id]), dict(routed[inst_id]))
            )
            for inst_id, cap in self.capabilities.items()
        }
        return PreparedSetupContext(
            self._project_setup_context(
                original, routed, {i: p.execution for i, p in prepared.items()}
            ),
            self._project_setup_context(
                original, routed, {i: p.recordable for i, p in prepared.items()}
            ),
        )

    def _setup_namespace(self, key: str, value: Any) -> list[str] | None:
        if not isinstance(value, Mapping):
            return None
        if key in self.capabilities:
            return [key]
        matches = [i for i, cap in self.capabilities.items() if cap.capability_id == key]
        return matches or None

    def _project_setup_context(
        self,
        original: Mapping[str, Any],
        routed: Mapping[str, Mapping[str, Any]],
        projection: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        scoped: dict[str, dict[str, Any]] = {}
        aliases: dict[str, str] = {}
        for key, value in original.items():
            namespace = self._setup_namespace(key, value)
            if namespace is not None:
                for owner in namespace:
                    aliases[owner] = key
                    scoped[owner] = dict(projection[owner])
                continue
            owners = [i for i, values in routed.items() if key in values]
            if len(owners) == 1 and key in projection[owners[0]]:
                result[key] = projection[owners[0]][key]
            elif all(key in projection[i] and projection[i][key] is value for i in owners):
                result[key] = value
            else:
                for owner in owners:
                    if key in projection[owner]:
                        scoped.setdefault(owner, {})[key] = projection[owner][key]
        # Namespaces follow flat values, so their effective routed values win.
        result.update({aliases.get(owner, owner): values for owner, values in scoped.items()})
        return result

    def check_setup(
        self,
        context: Mapping[str, Any] | None = None,
        *,
        negotiation: Any = None,
        archivers: Sequence[ArchiverBase] = (),
    ) -> None:
        """Refuse now, acquiring nothing, what :meth:`setup` would refuse for this task.

        ``AgentRuntime.prepare()`` calls this with the task's setup context,
        the observation negotiation result and the archivers it is about to
        bind (``docs/architecture_design.md`` §4.3 "setup 之前的检查"), so a
        configuration that cannot run fails before the trace is opened and
        before anything is launched — on a dry run too. In order, each step
        raising on its own:

        1. the capability set must route (:meth:`_by_capability_id`);
        2. every context key must be one some capability's ``setup()``
           declares (:meth:`route_setup_context`, the routing setup uses);
        3. every archiver must route to an archiving capability that
           provides it (:meth:`_archiver_groups`, the routing
           :meth:`bind_archivers` uses);
        4. every capability implementing
           :class:`~spectragent.core.capabilities.base.SetupCheck` checks
           its own slice — its routed context and its archivers — in
           capability order. A capability without the protocol is checked
           for routing only.

        Changes nothing: no capability is set up, bound or narrowed.

        Raises:
            ValueError: A colliding capability set, or a context key no
                capability accepts.
            UnroutableArchiverError: An archiver that cannot be bound here.
            Exception: A capability's own refusal (browser:
                ``BrowserCooperationError``, naming both parties of every
                problem).
        """
        self._by_capability_id()
        routed = self.route_setup_context(context or {})
        groups = self._archiver_groups(archivers)
        for inst_id, cap in self.capabilities.items():
            if isinstance(cap, SetupCheck):
                cap.check_setup(
                    routed[inst_id],
                    negotiation=negotiation,
                    archivers=groups.get(cap.capability_id, []),
                )

    async def setup(
        self,
        context: Mapping[str, Any] | None = None,
        *,
        negotiation: Any = None,
    ) -> None:
        """Set up every capability with *its own* slice of the task context.

        ``context`` is normally ``TaskLoader.get_task_context(task_id)``.
        It used to be splatted verbatim into every capability, which meant
        two capabilities could not be configured independently and a
        misspelled key was dropped by all of them without a word. See
        :meth:`route_setup_context` for the routing rules.
        """
        # Before anything is acquired: a capability set that cannot be
        # routed is a config error, not a runtime surprise. ``prepare()``
        # refused it already (:meth:`check_setup` routes the same way); this
        # is the same routing again, for a caller that sets up directly.
        self._by_capability_id()
        routed = self.route_setup_context(context or {})
        for inst_id, cap in self.capabilities.items():
            await cap.setup(negotiation=negotiation, **routed[inst_id])

    def route_setup_context(self, context: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        """Split a task context into one keyword mapping per capability.

        Two forms, which compose:

        - **Flat** (``{"start_url": ...}``) — what ``TaskSetup.to_context()``
          emits. Each key goes to every capability whose ``setup()``
          *names* it, and to no others. A key no capability names raises.
        - **Namespaced** (``{"browser": {"timeout": 5}, "docker":
          {"timeout": 600}}``) — a mapping under a capability instance id
          or ``capability_id`` is private to that capability. This is how
          two capabilities take the same option name independently.

        Accepted keys are read off each capability's ``setup()``
        signature, so the contract cannot drift from the implementation.
        A ``**kwargs`` catch-all declares nothing and therefore receives
        nothing — being a sink is no longer a way to opt out of the
        check.

        Raises:
            ValueError: A key nobody accepts, or a namespaced block
                naming a key its target capability does not accept.
        """
        # ``type(self).__name__`` through the conscribe metaclass is not
        # visible to the type checker; the value is always there.
        where = getattr(type(self), "__name__", "Environment")
        accepted = {inst_id: _setup_keys(cap) for inst_id, cap in self.capabilities.items()}
        routed: dict[str, dict[str, Any]] = {inst_id: {} for inst_id in self.capabilities}
        unclaimed: list[str] = []
        for key, value in context.items():
            targets = self._setup_namespace(key, value)
            if targets is not None:
                for inst_id in targets:
                    unknown = sorted(set(value) - accepted[inst_id])
                    if unknown:
                        raise ValueError(
                            f"{where}: capability "
                            f"{inst_id!r} does not accept setup key(s) "
                            f"{unknown} — its setup() declares "
                            f"{sorted(accepted[inst_id])}."
                        )
                    routed[inst_id].update(value)
                continue

            claimed = [i for i, keys in accepted.items() if key in keys]
            if not claimed:
                unclaimed.append(key)
                continue
            for inst_id in claimed:
                routed[inst_id][key] = value

        if unclaimed:
            known = sorted({k for keys in accepted.values() for k in keys})
            raise ValueError(
                f"{where}: setup key(s) "
                f"{sorted(unclaimed)} are accepted by none of its "
                f"capabilities {sorted(self.capabilities)}. Keys their "
                f"setup() methods declare: {known}. Nest a mapping under a "
                f"capability id to target one capability explicitly."
            )
        return routed

    async def teardown(self) -> None:
        """Tear every capability down, even if one of them raises.

        Two guarantees, both learned from leaked Chrome processes:

        - Safe after a setup that failed part way, and after no setup at
          all. ``AgentRuntime`` calls this whenever it *attempted* setup,
          because the capabilities that came up before the failure are
          exactly the ones holding OS processes and paid cloud sessions.
        - One capability's teardown blowing up must not strand the
          capabilities after it in the loop. The first failure is
          re-raised once every capability has had its turn; the rest are
          logged.
        """
        errors: list[Exception] = []
        for inst_id, cap in self.capabilities.items():
            try:
                await cap.teardown()
            except Exception as exc:  # noqa: PERF203 — per-capability isolation
                logger.exception(
                    "Environment teardown: capability %r failed to tear down",
                    inst_id,
                )
                errors.append(exc)
        if errors:
            raise errors[0]

    @property
    def is_alive(self) -> bool:
        return all(cap.is_alive for cap in self.capabilities.values())
