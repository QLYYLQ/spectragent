"""Shared fakes for the archive axis: one archiving capability and its world.

One fake capability that provides archivers (``contract_archive``), its
archivers, a fragment and two actions, an environment class, a minimal
archive consumer hook, and :func:`make_archive_run` to drive the lot through
a real ``AgentRuntime``. Used by every M28 test area — the archive package,
environment routing, negotiation, the runtime, the trace hook and the shared
contract harness (``tests/_helpers/archive_contract.py``) — so its
behaviour is the reference shape of an archiving capability, nothing more.

Beside it, for environment-level tests: :class:`QuietCapability` (a
``CapabilityBase`` that archives nothing), :class:`StructuralCapability` (a
``BaseCapability`` by shape, without the archive surface) and
:class:`PeerArchivingCapability` (an unregistered, steerable archiving
capability for routing, concurrency and failure-isolation tests),
:class:`SurfacelessEnv` (an environment with no archive surface at all, for
the zero-overhead path of a run nobody archives), and :class:`CollectSpy`
(the raw environment as the trace hook sees it, every collection recorded).

Registrations are captured with conscribe's ``isolated_registries`` and
activated by the ``archive_fakes`` fixture only for tests that need them.
Other tests therefore build their schemas from production registrations.

The fake capability follows the archiving contract
(``docs/architecture_design.md`` §4.9) on purpose:

- ``bind_archivers`` replaces the binding and acquires nothing;
- what it captures is attributable without a step number: a step file per
  announced observation cycle (``cycle_NNN.json``, when
  ``contract_archive.steps`` is bound) and a content-addressed response
  body per action (``response.bin``, when ``contract_archive.blobs`` is
  bound — the payload repeats every other action, so deduplication has
  something to deduplicate);
- the first collection after a non-empty bind carries the capability's run
  record (JSON, ``scope="run"``, owned by the bare capability id);
  :meth:`FakeArchivingCapability.change_run_record` makes the next
  collection carry a changed one;
- ``collect_archive`` swaps its buffer out in one synchronous statement and
  forgets it; :attr:`FakeArchivingCapability.broken` makes it raise.

No custom ``__init__`` on the capability (``CapabilityBase.id`` must stay
visible to conscribe, ``test_no_dropped_annotated_fields``): per-instance
state lives in a holder created lazily in the instance ``__dict__``.
"""

import asyncio
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Literal

from conscribe.testing import isolated_registries
from pydantic import BaseModel, ConfigDict
from pydantic import Field as PydanticField

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.archive import (
    ArchiveBatch,
    ArchiveFile,
    ArchiveProvenance,
    ArchiverBase,
)
from spectragent.core.archive.privacy import MARKER_REDACTED
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks._registrar import HookBase
from spectragent.core.loops import ReactLoop
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.prompt import ReactPromptContext
from tests._helpers.assembly import make_env, make_run

#: The fake capability's registry key — the prefix of every key it provides.
CAPABILITY_ID = "contract_archive"
#: Step-scoped evidence, one file per announced observation cycle.
STEPS_KEY = "contract_archive.steps"
#: Content-addressed evidence, one response body per action.
BLOBS_KEY = "contract_archive.blobs"
#: Registered, prefixed like the capability, and *not* provided by it.
UNPROVIDED_KEY = "contract_archive.unprovided"
#: Registered under a prefix no capability carries.
ORPHAN_KEY = "contract_nowhere.x"

#: Two more archiving capabilities, by id, and the one archiver each provides
#: (:class:`PeerArchivingCapability`, :class:`PeerAArchiver` / :class:`PeerBArchiver`).
PEER_A = "contract_archive_peer_a"
PEER_B = "contract_archive_peer_b"
PEER_A_KEY = f"{PEER_A}.events"
PEER_B_KEY = f"{PEER_B}.events"

ENV_TYPE = "contract_archive_env"
CONSUMER_HOOK_ID = "contract_archive_consumer"
PAGE_KEY = "contract_archive.page"
DONE_KEY = "contract_archive.done"
KEEP_GOING_KEY = "contract_archive.keep_going"

#: Name of the run record the fake capability emits.
RUN_RECORD_NAME = "fingerprint.json"


# ── archivers ──────────────────────────────────────────────────────


FAKES = isolated_registries()

with FAKES:

    class FakeStepArchiver(ArchiverBase):
        """``contract_archive.steps`` — one JSON file per observation cycle.

        Its one knob shows the shape every archiver's knobs take (an
        ``Annotated`` constructor parameter) and how a privacy declaration is
        *derived* from configuration rather than hard-coded.
        """

        __registry_key__ = STEPS_KEY

        def __init__(
            self,
            redact: Annotated[
                bool,
                PydanticField(description="Apply the shared privacy rules to what it captures."),
            ] = True,
        ) -> None:
            self.redact = redact

        @property
        def privacy_markers(self) -> frozenset[str]:
            return frozenset({MARKER_REDACTED}) if self.redact else frozenset()


with FAKES:

    class FakeBlobArchiver(ArchiverBase):
        """``contract_archive.blobs`` — a content-addressed body per action.

        ``payload_bytes`` is read by the capability off the *bound instance*:
        the knobs travel with the archivers the trace hook holds.
        """

        __registry_key__ = BLOBS_KEY

        def __init__(
            self,
            payload_bytes: Annotated[
                int,
                PydanticField(ge=8, description="Size of each captured response body."),
            ] = 32,
        ) -> None:
            self.payload_bytes = payload_bytes


with FAKES:

    class FakeUnprovidedArchiver(ArchiverBase):
        """Prefixed like the capability, missing from its provides."""

        __registry_key__ = UNPROVIDED_KEY


with FAKES:

    class FakeOrphanArchiver(ArchiverBase):
        """Prefixed by a capability no environment here has."""

        __registry_key__ = ORPHAN_KEY


with FAKES:

    class StructuralCapabilityArchiver(ArchiverBase):
        """Prefixed by :class:`StructuralCapability`, which cannot archive."""

        __registry_key__ = "contract_archive_structural.x"


with FAKES:

    class PeerAArchiver(ArchiverBase):
        """Provided by the first :class:`PeerArchivingCapability`."""

        __registry_key__ = PEER_A_KEY


with FAKES:

    class PeerBArchiver(ArchiverBase):
        """Provided by the second :class:`PeerArchivingCapability`."""

        __registry_key__ = PEER_B_KEY


# ── the fragment and the actions the capability serves ─────────────


with FAKES:

    class ContractArchiveObs(FragmentBase):
        """``contract_archive.page`` — says which cycle it was captured in."""

        __registry_key__ = PAGE_KEY
        __source__: ClassVar[Literal["env", "loop"]] = "env"
        cycle: int = 0


class _NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


with FAKES:

    class ContractArchiveDoneAction(ActionBase):
        """``contract_archive.done`` — ends the run."""

        __registry_key__ = DONE_KEY
        Params = _NoParams

        def descriptor(self, **context: Any) -> ActionDescriptor:
            return self.describe("Finish the task.", **context)

        async def execute(self, params: dict[str, Any], *, capability: Any) -> ActionResult:
            return await capability.execute_action(Action(name=self.action_id, params=dict(params)))


with FAKES:

    class ContractArchiveKeepGoingAction(ActionBase):
        """``contract_archive.keep_going`` — a step that does not end the run."""

        __registry_key__ = KEEP_GOING_KEY
        Params = _NoParams

        def descriptor(self, **context: Any) -> ActionDescriptor:
            return self.describe("Take one more step.", **context)

        async def execute(self, params: dict[str, Any], *, capability: Any) -> ActionResult:
            return await capability.execute_action(Action(name=self.action_id, params=dict(params)))


# ── the capability ─────────────────────────────────────────────────


@dataclass
class FakeArchiveState:
    """Everything one :class:`FakeArchivingCapability` instance remembers."""

    bound: tuple[ArchiverBase, ...] = ()
    buffer: list[ArchiveFile] = field(default_factory=list)
    bind_calls: int = 0
    collect_calls: int = 0
    setup_calls: int = 0
    broken: bool = False
    cycle: int = 0
    actions: int = 0
    run_record_due: bool = False
    run_generation: int = 0
    #: What was bound when ``setup()`` ran — the moment a real capability
    #: decides whether to build its capture channel.
    bound_at_setup: tuple[str, ...] | None = None
    #: The ``start_url`` task-context key ``setup()`` received.
    start_url: str | None = None
    #: Optional shared event log (``"bind"``, ``"setup"``, ``"collect"``),
    #: for tests that pin an order across components.
    log: list[str] | None = None


with FAKES:

    class FakeArchivingCapability(CapabilityBase):
        """``contract_archive`` — a capability that provides two archivers."""

        __registry_key__ = CAPABILITY_ID
        __wiring__ = {
            "action": ("action", [DONE_KEY, KEEP_GOING_KEY]),
            "observation": ("observation", [PAGE_KEY]),
            "archiver": ("archiver", [STEPS_KEY, BLOBS_KEY]),
        }

        # ── per-instance state, created on first use ──

        @property
        def fake(self) -> FakeArchiveState:
            state = self.__dict__.get("_fake_state")
            if state is None:
                state = FakeArchiveState()
                self.__dict__["_fake_state"] = state
            return state

        @property
        def bind_calls(self) -> int:
            return self.fake.bind_calls

        @property
        def collect_calls(self) -> int:
            return self.fake.collect_calls

        @property
        def broken(self) -> bool:
            return self.fake.broken

        @broken.setter
        def broken(self, value: bool) -> None:
            self.fake.broken = value

        @property
        def bound_keys(self) -> tuple[str, ...]:
            return tuple(a.archiver_id for a in self.fake.bound)

        def _note(self, event: str) -> None:
            if self.fake.log is not None:
                self.fake.log.append(event)

        def _bound(self, key: str) -> ArchiverBase | None:
            return next((a for a in self.fake.bound if a.archiver_id == key), None)

        def _provenance(self, **ids: str) -> ArchiveProvenance:
            return ArchiveProvenance(
                captured_at=time.time(),
                cycle=self.fake.cycle or None,
                ids={"capability": CAPABILITY_ID, **ids},
            )

        # ── the archiving surface ──

        def bind_archivers(self, archivers: Sequence[ArchiverBase]) -> None:
            unknown = sorted(
                a.archiver_id for a in archivers if a.archiver_id not in self.provided_archivers
            )
            if unknown:
                raise ValueError(f"{CAPABILITY_ID} does not provide {unknown}")
            self.fake.bind_calls += 1
            self.fake.bound = tuple(archivers)
            self.fake.run_record_due = bool(archivers)
            self._note("bind")

        async def collect_archive(self) -> ArchiveBatch:
            self.fake.collect_calls += 1
            self._note("collect")
            if self.fake.broken:
                raise RuntimeError(f"{CAPABILITY_ID}: collection broken on purpose")
            if self.fake.run_record_due:
                self.fake.run_record_due = False
                self.fake.buffer.append(
                    ArchiveFile(
                        archiver_id=CAPABILITY_ID,
                        name=RUN_RECORD_NAME,
                        media_type="application/json",
                        data=json.dumps(
                            {"capability": CAPABILITY_ID, "generation": self.fake.run_generation},
                            sort_keys=True,
                        ).encode("utf-8"),
                        meta=self._provenance(),
                        scope="run",
                    )
                )
            # The hand-over: one synchronous statement, so nothing a producer
            # appends can fall between "read" and "clear".
            files, self.fake.buffer = self.fake.buffer, []
            return ArchiveBatch(tuple(files))

        # ── helpers for tests and the contract harness ──

        async def produce(self, step: int) -> None:
            """Capture what one step of activity would: one cycle, one action.

            *step* is the caller's bookkeeping only — the capability never
            learns step numbers; it numbers the cycle itself.
            """
            await self.begin_observation(self.fake.cycle + 1)
            await self.execute_action(Action(name=KEEP_GOING_KEY, params={}))

        def change_run_record(self) -> None:
            """Make the next collection carry a run record with new content."""
            self.fake.run_generation += 1
            self.fake.run_record_due = True

        # ── capability behaviour ──

        def get_action_descriptors(self, **context: Any) -> list[ActionDescriptor]:
            return [
                ContractArchiveDoneAction().descriptor(**context),
                ContractArchiveKeepGoingAction().descriptor(**context),
            ]

        async def begin_observation(self, cycle: int) -> None:
            self.fake.cycle = cycle
            if self._bound(STEPS_KEY) is not None:
                self.fake.buffer.append(
                    ArchiveFile(
                        archiver_id=STEPS_KEY,
                        name=f"cycle_{cycle:03d}.json",
                        media_type="application/json",
                        data=json.dumps({"cycle": cycle}).encode("utf-8"),
                        meta=self._provenance(frame="main"),
                    )
                )

        async def resolve_fragment(self, fragment_key: str) -> Any:
            if fragment_key != PAGE_KEY:
                raise KeyError(fragment_key)
            return ContractArchiveObs(cycle=self.fake.cycle)

        async def execute_action(self, action: Any) -> ActionResult:
            if action.name not in self.provided_actions:
                return ActionResult(
                    success=False, error=f"{CAPABILITY_ID} cannot run {action.name!r}"
                )
            self.fake.actions += 1
            blobs = self._bound(BLOBS_KEY)
            if isinstance(blobs, FakeBlobArchiver):
                seed = f"response #{(self.fake.actions - 1) % 2}|".encode()
                payload = (seed * (blobs.payload_bytes // len(seed) + 1))[: blobs.payload_bytes]
                self.fake.buffer.append(
                    ArchiveFile(
                        archiver_id=BLOBS_KEY,
                        name="response.bin",
                        media_type="application/octet-stream",
                        data=payload,
                        meta=self._provenance(request=f"r{self.fake.actions}"),
                        content_addressed=True,
                    )
                )
            done = action.name == DONE_KEY
            return ActionResult(
                success=True, is_done=done, extracted_content="42" if done else None
            )

        async def setup(self, *, negotiation: Any = None, start_url: str | None = None) -> None:
            await super().setup(negotiation=negotiation)
            self.fake.setup_calls += 1
            self.fake.start_url = start_url
            self.fake.bound_at_setup = self.bound_keys
            self._note("setup")


with FAKES:

    class QuietCapability(CapabilityBase):
        """``contract_archive_quiet`` — a ``CapabilityBase`` that archives nothing.

        Satisfies ``ArchivingCapability`` through the inert defaults: provides
        no archivers, accepts an empty binding, collects an empty batch.
        """

        __registry_key__ = "contract_archive_quiet"


class StructuralCapability:
    """``contract_archive_structural`` — a ``BaseCapability`` by shape only.

    Implements the capability Protocol without inheriting
    ``CapabilityBase`` — and without the optional archive surface, so it is
    *not* an ``ArchivingCapability``. Not registered (it has no registrar
    base): it stands for the structural implementer Protocol-First allows.
    """

    capability_id = "contract_archive_structural"
    id: str | None = None
    __invalidated_by_foreign_actions__: ClassVar[bool] = False

    @property
    def provided_actions(self) -> frozenset[str]:
        return frozenset()

    @property
    def provided_fragments(self) -> frozenset[str]:
        return frozenset()

    def get_action_descriptors(self, **context: Any) -> list[ActionDescriptor]:
        return []

    async def execute_action(self, action: Any) -> ActionResult:
        return ActionResult(success=False, error="structural capability runs nothing")

    async def begin_observation(self, cycle: int) -> None:
        return None

    async def resolve_fragment(self, fragment_key: str) -> Any:
        raise KeyError(fragment_key)

    async def setup(self, *, negotiation: Any = None) -> None:
        return None

    async def teardown(self) -> None:
        return None

    @property
    def is_alive(self) -> bool:
        return True


class PeerArchivingCapability(StructuralCapability):
    """An archiving capability by shape — unregistered, steerable.

    For environment-level tests that need more than one archiving
    capability: routing across prefixes, concurrent collection, failure
    isolation. Each collection sets :attr:`entered`, then (optionally)
    waits for :attr:`wait_for`, then raises :attr:`fail_with` or hands over
    whatever :attr:`files` holds and forgets it. :attr:`finished` records
    that a collection ran to its end.
    """

    def __init__(self, capability_id: str, provides: Sequence[str]) -> None:
        self.capability_id = capability_id
        self._provides = frozenset(provides)
        self.bound: tuple[ArchiverBase, ...] = ()
        self.bind_calls = 0
        self.collect_calls = 0
        self.entered = asyncio.Event()
        self.wait_for: asyncio.Event | None = None
        self.yields_after_wait = 0
        self.fail_with: BaseException | None = None
        self.files: list[ArchiveFile] = []
        self.finished = False
        self.was_cancelled = False

    @property
    def provided_archivers(self) -> frozenset[str]:
        return self._provides

    def bind_archivers(self, archivers: Sequence[ArchiverBase]) -> None:
        self.bind_calls += 1
        self.bound = tuple(archivers)

    async def collect_archive(self) -> ArchiveBatch:
        self.collect_calls += 1
        self.entered.set()
        try:
            if self.wait_for is not None:
                await self.wait_for.wait()
            for _ in range(self.yields_after_wait):
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            self.was_cancelled = True
            raise
        if self.fail_with is not None:
            raise self.fail_with
        files, self.files = self.files, []
        self.finished = True
        return ArchiveBatch(tuple(files))

    def evidence(self, key: str, name: str = "evidence.json") -> ArchiveFile:
        """One step file under *key* — queue it with ``files.append``."""
        return ArchiveFile(
            archiver_id=key,
            name=name,
            media_type="application/json",
            data=b"{}",
            meta=ArchiveProvenance(captured_at=time.time()),
        )

    def run_record(self, data: bytes, name: str = "fingerprint.json") -> ArchiveFile:
        """This capability's run record (owned by its bare id) — queue it with ``files.append``."""
        return ArchiveFile(
            archiver_id=self.capability_id,
            name=name,
            media_type="application/json",
            data=data,
            meta=ArchiveProvenance(captured_at=time.time()),
            scope="run",
        )


with FAKES:

    class ContractArchiveEnv(EnvironmentBase):
        """The environment the fake capability lives in."""

        __wiring__ = {"capabilities": "capability"}

        __registry_key__ = ENV_TYPE


class SurfacelessEnv:
    """An environment with no archive surface at all.

    Everything a run needs, forwarded to a real environment — and nothing
    of the archive axis: touching ``bind_archivers``, ``collect_archive``
    or ``provided_archivers`` raises ``AttributeError``. Stands for an
    environment written before the axis existed, which must keep working
    as long as no hook archives.
    """

    env_type = "contract_archive_surfaceless"

    def __init__(self, inner: ContractArchiveEnv) -> None:
        self._inner = inner
        self.capabilities = inner.capabilities

    @property
    def provided_fragments(self) -> frozenset[str]:
        return self._inner.provided_fragments

    @property
    def provided_actions(self) -> frozenset[str]:
        return self._inner.provided_actions

    def get_action_space(self, **kwargs: Any) -> Any:
        return self._inner.get_action_space(**kwargs)

    def prepare_setup_context(self, context: Mapping[str, Any] | None = None) -> Any:
        return self._inner.prepare_setup_context(context)

    def check_setup(
        self,
        context: Mapping[str, Any] | None = None,
        *,
        negotiation: Any = None,
        archivers: Any = (),
    ) -> None:
        # Every run calls it (``prepare()``'s setup check), archiving or not.
        self._inner.check_setup(context, negotiation=negotiation, archivers=archivers)

    async def observe(self, negotiated: Any) -> Any:
        return await self._inner.observe(negotiated)

    async def act(self, action: Any) -> Any:
        return await self._inner.act(action)

    async def setup(
        self, context: Mapping[str, Any] | None = None, *, negotiation: Any = None
    ) -> None:
        await self._inner.setup(context, negotiation=negotiation)

    async def teardown(self) -> None:
        await self._inner.teardown()

    @property
    def is_alive(self) -> bool:
        return True


class CollectSpy:
    """The raw environment as the trace hook sees it: ``collect_archive``, recorded.

    Wraps a real environment; every batch it hands over is kept, and
    *on_collect* runs before each collection. ``TraceWriter`` only ever
    calls ``collect_archive()`` on the environment ``on_run_start`` handed
    it, so this is all of the environment it needs.
    """

    def __init__(self, env: Any, on_collect: Callable[[], None] | None = None) -> None:
        self.env = env
        self.batches: list[ArchiveBatch] = []
        self.calls = 0
        self.on_collect = on_collect

    async def collect_archive(self) -> ArchiveBatch:
        self.calls += 1
        if self.on_collect is not None:
            self.on_collect()
        batch = await self.env.collect_archive()
        self.batches.append(batch)
        return batch


# ── a consumer that is not the trace hook ──────────────────────────


with FAKES:

    class ArchiveConsumerHook(HookBase):
        """A minimal ``ArchiveConsumer``: an ``archive`` list, nothing else.

        For tests that need a consumer without the trace hook (the runtime
        knows only the Protocol). Records the keywords ``on_run_start``
        received, and appends lifecycle events to an optional shared log.
        """

        __registry_key__ = CONSUMER_HOOK_ID

        def __init__(
            self, archive: list[ArchiverBase] | None = None, *, log: list[str] | None = None
        ) -> None:
            super().__init__()
            self.archive: list[ArchiverBase] = list(archive or [])
            self.log = log
            self.run_start: dict[str, Any] | None = None

        async def on_run_start(self, **kwargs: Any) -> None:
            self.run_start = dict(kwargs)
            if self.log is not None:
                self.log.append("on_run_start")


# ── a whole run ────────────────────────────────────────────────────


class ContractArchiveAgent:
    """Takes ``keep_going_steps`` steps of ``keep_going``, then ``done``."""

    __execution_limit_fields__: ClassVar[frozenset[str]] = frozenset()

    __wiring__ = {
        "action": ("action", ["done"], ["keep_going"]),
        "observation": ("observation", ["page", "loop.task"]),
    }

    def __init__(self, keep_going_steps: int = 0) -> None:
        self.keep_going_steps = keep_going_steps
        self.parsed = 0

    async def build_react_prompt(
        self, obs: Any, *, context: ReactPromptContext
    ) -> list[dict[str, str]]:
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out: Any) -> list[Action]:
        self.parsed += 1
        name = "keep_going" if self.parsed <= self.keep_going_steps else "done"
        return [Action(name=name, params={})]

    def get_react_output_model(self) -> None:
        return None


class _Completion:
    completion = '{"action": "done"}'
    usage = None


class ContractArchiveLLM:
    model = "contract-archive-llm"

    async def ainvoke(self, messages: Any, **kwargs: Any) -> _Completion:
        return _Completion()

    name = "test-model"

    async def aclose(self, *, timeout=5.0):
        return ()


class ContractArchiveMemory:
    supports_protocols = frozenset({"user_assistant"})

    def __init__(self) -> None:
        self.messages: list[Any] = []

    def add_message(self, message: Any) -> None:
        self.messages.append(message)

    def add_messages(self, messages: Any) -> None:
        self.messages.extend(messages)

    def get_context_messages(self) -> list[Any]:
        return list(self.messages)

    def clear(self) -> None:
        self.messages.clear()


def make_archive_env(capability: FakeArchivingCapability | None = None) -> ContractArchiveEnv:
    """A :class:`ContractArchiveEnv` around *capability* (a fresh one by default)."""
    return make_env(ContractArchiveEnv, {CAPABILITY_ID: capability or FakeArchivingCapability()})


def make_archive_run(
    *,
    hooks: list[Any],
    capability: FakeArchivingCapability | None = None,
    env: Any | None = None,
    steps: int = 1,
    max_steps: int = 10,
) -> Any:
    """A ``ComposedRun`` that drives the fake capability through a real Loop.

    The agent takes ``steps - 1`` ``keep_going`` steps and then ``done``, so
    the run commits *steps* steps. Pass *env* to use another environment
    (a stub without the archive surface, say); otherwise the fake
    capability (*capability*, or a fresh one) in a
    :class:`ContractArchiveEnv`.

    Drive it with ``AgentRuntime.for_agent(run, tests._helpers.assembly.AGENT_ID)``.
    """
    return make_run(
        agent=ContractArchiveAgent(keep_going_steps=max(steps - 1, 0)),
        loop=ReactLoop(max_steps=max_steps),
        env=env if env is not None else make_archive_env(capability),
        llm=ContractArchiveLLM(),
        memory=ContractArchiveMemory(),
        hooks=hooks,
    )
