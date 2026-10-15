"""Archive binding errors surface where a run is assembled: ``AgentRuntime.prepare()``.

Through a real runtime, with real consumers — the trace hook itself, and the
test consumer for the cases that need a second hook — and a real environment
(``docs/architecture_design.md`` §4.2 "归档：先校验，最后绑定", §4.5). Every
case pins:

- ``prepare()`` raises ``IncompatibleArchiveError`` whose lines name the
  parties — the requesting hook and what the environment and its capability
  provide, or the two hooks that collide — verbatim, in the wording §4.5
  pins, before anything is woven;
- ``execute()`` raises it before ``on_run_start`` — no trace directory, no
  consumer told the run started — and before setup: the capability was never
  set up and nothing was bound. Whatever ``raise_on_error`` says: a binding
  error is a configuration error, not a failed task.

The negotiation rules themselves, message by message, are
``tests/test_core/test_negotiation/test_archive.py``; this file is about the
runtime raising them at the right moment.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from spectragent.core import runtime as runtime_mod
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import AgentLoopRegistrar, MissingEnvironmentError, ReactLoop
from spectragent.core.negotiation import IncompatibleArchiveError
from spectragent.core.runtime import AgentRuntime
from tests._helpers.archive_fakes import (
    BLOBS_KEY,
    CAPABILITY_ID,
    CONSUMER_HOOK_ID,
    ENV_TYPE,
    ORPHAN_KEY,
    STEPS_KEY,
    UNPROVIDED_KEY,
    ArchiveConsumerHook,
    ContractArchiveEnv,
    ContractArchiveLLM,
    ContractArchiveMemory,
    FakeArchivingCapability,
    FakeBlobArchiver,
    FakeOrphanArchiver,
    FakeStepArchiver,
    FakeUnprovidedArchiver,
    StructuralCapability,
    StructuralCapabilityArchiver,
    make_archive_run,
)
from tests._helpers.assembly import AGENT_ID, make_env, make_run

pytestmark = pytest.mark.usefixtures("archive_fakes")

_TW = "trace_writer"
_BOTH = sorted([BLOBS_KEY, STEPS_KEY])


@dataclass
class Bench:
    """One mis-configured run, and everything that must stay untouched by it."""

    run: Any
    cap: FakeArchivingCapability
    trace_root: Path
    consumers: list[ArchiveConsumerHook] = field(default_factory=list)

    def runtime(self) -> AgentRuntime:
        return AgentRuntime.for_agent(self.run, AGENT_ID)


def _writer(trace_root: Path, *archivers: Any) -> TraceWriter:
    return TraceWriter(trace_dir=str(trace_root / "traces"), archive=list(archivers))


def _bench(
    trace_root: Path,
    hooks: Callable[[Path], list[Any]],
    *,
    with_structural_peer: bool = False,
) -> Bench:
    cap = FakeArchivingCapability()
    capabilities: dict[str, Any] = {CAPABILITY_ID: cap}
    if with_structural_peer:
        # A capability without the archive surface: it provides no archiver.
        capabilities["structural#0"] = StructuralCapability()
    built = hooks(trace_root)
    env = make_env(ContractArchiveEnv, capabilities)
    return Bench(
        run=make_archive_run(hooks=built, env=env),
        cap=cap,
        trace_root=trace_root,
        consumers=[hook for hook in built if isinstance(hook, ArchiveConsumerHook)],
    )


@dataclass(frozen=True)
class Case:
    hooks: Callable[[Path], list[Any]]
    lines: list[str]
    with_structural_peer: bool = False

    def bench(self, trace_root: Path) -> Bench:
        return _bench(trace_root, self.hooks, with_structural_peer=self.with_structural_peer)


CASES: dict[str, Case] = {
    "no capability for the prefix": Case(
        hooks=lambda root: [_writer(root, FakeOrphanArchiver())],
        lines=[
            f"{_TW} requests archiver {ORPHAN_KEY!r}, but environment {ENV_TYPE!r} has no "
            f"capability 'contract_nowhere' (its capabilities: [{CAPABILITY_ID!r}])."
        ],
    ),
    "the capability does not provide the key": Case(
        hooks=lambda root: [_writer(root, FakeUnprovidedArchiver())],
        lines=[
            f"{_TW} requests archiver {UNPROVIDED_KEY!r}, but environment {ENV_TYPE!r} "
            f"provides archivers: {_BOTH} (capability {CAPABILITY_ID!r} declares {_BOTH})."
        ],
    ),
    "a capability that declares none": Case(
        hooks=lambda root: [_writer(root, StructuralCapabilityArchiver())],
        lines=[
            f"{_TW} requests archiver 'contract_archive_structural.x', but environment "
            f"{ENV_TYPE!r} provides archivers: {_BOTH} (capability "
            f"'contract_archive_structural' declares none)."
        ],
        with_structural_peer=True,
    ),
    "the same key twice in one list": Case(
        hooks=lambda root: [_writer(root, FakeStepArchiver(), FakeStepArchiver(redact=False))],
        lines=[
            f"{_TW} lists archiver {STEPS_KEY!r} more than once; an archive list names each "
            f"archiver at most once."
        ],
    ),
    "the same key in two consumers": Case(
        hooks=lambda root: [
            ArchiveConsumerHook([FakeStepArchiver()]),
            _writer(root, FakeStepArchiver()),
        ],
        lines=[
            f"archiver {STEPS_KEY!r} is requested by more than one hook ({CONSUMER_HOOK_ID}, "
            f"{_TW}); a run binds each archiver once.",
            f"more than one hook requests archivers ({CONSUMER_HOOK_ID}: [{STEPS_KEY!r}]; "
            f"{_TW}: [{STEPS_KEY!r}]); a run binds the archive of at most one hook.",
        ],
    ),
    "two consumers with disjoint archives": Case(
        hooks=lambda root: [
            ArchiveConsumerHook([FakeBlobArchiver()]),
            _writer(root, FakeStepArchiver()),
        ],
        lines=[
            f"more than one hook requests archivers ({CONSUMER_HOOK_ID}: [{BLOBS_KEY!r}]; "
            f"{_TW}: [{STEPS_KEY!r}]); a run binds the archive of at most one hook."
        ],
    ),
}


@pytest.fixture()
def woven(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records every ``Weaver`` the runtime builds — none may be, for a binding error."""
    built: list[str] = []
    real = runtime_mod.Weaver

    class RecordingWeaver(real):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            built.append("weaver")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(runtime_mod, "Weaver", RecordingWeaver)
    return built


class TestPrepareRaisesNamingTheParties:
    @pytest.mark.parametrize("name", list(CASES))
    def test_before_anything_is_woven(self, name: str, tmp_path: Path, woven: list[str]) -> None:
        case = CASES[name]
        bench = case.bench(tmp_path)
        runtime = bench.runtime()

        with pytest.raises(IncompatibleArchiveError) as excinfo:
            runtime.prepare()

        assert str(excinfo.value).split("\n") == case.lines
        assert woven == []
        assert runtime.negotiated_archive is None
        assert bench.cap.bind_calls == 0


class TestExecuteRaisesBeforeTheRunStarts:
    @pytest.mark.parametrize("raise_on_error", [True, False])
    @pytest.mark.parametrize("name", list(CASES))
    async def test_no_trace_no_setup_nothing_bound(
        self, name: str, raise_on_error: bool, tmp_path: Path
    ) -> None:
        bench = CASES[name].bench(tmp_path)

        with pytest.raises(IncompatibleArchiveError):
            await bench.runtime().execute("t", timeout=10, raise_on_error=raise_on_error)

        assert not (tmp_path / "traces").exists(), "a trace was opened for a run never assembled"
        assert all(consumer.run_start is None for consumer in bench.consumers)
        assert bench.cap.fake.setup_calls == 0
        assert bench.cap.bind_calls == 0
        assert bench.cap.collect_calls == 0


# ── no environment ────────────────────────────────────────────────────


class _EnvlessLoop(ReactLoop):
    """A ReAct loop that runs without an environment (``__requires_env__ = False``)."""

    __registry_key__ = "test_archive_errors_envless_loop"
    __requires_env__ = False


def teardown_module() -> None:
    # Idempotent: pytest tears a module down once per contiguous run of its
    # tests, so a selection that interleaves files tears this one down twice.
    if AgentLoopRegistrar.get_or_none("test_archive_errors_envless_loop") is not None:
        AgentLoopRegistrar.unregister("test_archive_errors_envless_loop")


class _EnvlessAgent:
    """Asks an environment for nothing: only the task, which the Loop builds."""

    __wiring__ = {"observation": ("observation", ["loop.task"])}

    def build_react_prompt(self, obs: Any) -> list[dict[str, str]]:
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out: Any) -> list[Any]:
        return []

    def get_react_output_model(self) -> None:
        return None


def _envless_run(trace_root: Path, *, loop: Any = None) -> Any:
    return make_run(
        agent=_EnvlessAgent(),
        loop=loop if loop is not None else _EnvlessLoop(max_steps=2),
        env=None,
        llm=ContractArchiveLLM(),
        memory=ContractArchiveMemory(),
        hooks=[_writer(trace_root, FakeStepArchiver(), FakeBlobArchiver())],
    )


class TestWithoutAnEnvironment:
    """An archive with nothing to bind it to is refused — the archive step's own rule.

    The run is a real env-less one: its Loop declares ``__requires_env__ =
    False`` and its agent needs nothing an environment provides, so the
    observation and action negotiations pass against an environment that
    provides nothing, and the archive step is reached. What is pinned is that
    step as ``prepare()`` runs it: the consumer is named, nothing is woven,
    and the bind at the end of ``prepare()`` therefore never sees an archive
    without an environment. A Loop that needs an environment is refused
    before any negotiation (the last test).
    """

    def test_prepare_refuses_it_naming_the_consumer(self, tmp_path: Path, woven: list[str]):
        with pytest.raises(IncompatibleArchiveError) as excinfo:
            AgentRuntime.for_agent(_envless_run(tmp_path), AGENT_ID).prepare()
        assert str(excinfo.value).split("\n") == [
            f"{_TW} requests archivers {_BOTH}, but this run has no environment."
        ]
        assert excinfo.value.provided == frozenset()
        assert woven == []

    async def test_execute_raises_it_before_on_run_start(self, tmp_path: Path) -> None:
        runtime = AgentRuntime.for_agent(_envless_run(tmp_path), AGENT_ID)
        with pytest.raises(IncompatibleArchiveError):
            await runtime.execute("t", timeout=10, raise_on_error=False)
        assert not (tmp_path / "traces").exists()

    def test_a_loop_that_needs_an_environment_is_refused_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ReactLoop`` declares ``__requires_env__``: refused by name, before any negotiation."""
        reached: list[Any] = []
        real = runtime_mod.negotiate_archive

        def recording(*args: Any, **kwargs: Any) -> Any:
            reached.append(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(runtime_mod, "negotiate_archive", recording)
        runtime = AgentRuntime.for_agent(
            _envless_run(tmp_path, loop=ReactLoop(max_steps=2)), AGENT_ID
        )
        with pytest.raises(MissingEnvironmentError, match="Loop 'react' requires an environment"):
            runtime.prepare()
        assert reached == []
