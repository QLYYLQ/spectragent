"""``prepare()`` refuses what setup would refuse: the environment's setup check.

``docs/architecture_design.md`` §4.2 (step 2.9), §4.3 "setup 之前的检查": whether
a configuration can run with a task is decided by configuration and task data
alone, so ``AgentRuntime.prepare()`` asks ``Environment.check_setup`` with the
task's setup context, the observation negotiation and the archivers it is
about to bind — before anything is woven or bound, before ``on_run_start``
opens a trace, before anything is launched. Through a real runtime and a real
``EnvironmentBase``:

- a setup context key no capability's ``setup()`` declares is refused;
- each capability implementing ``SetupCheck`` gets its own slice (its routed
  context keys and its archivers) and the negotiation; its refusal leaves
  nothing woven, nothing bound and no trace;
- a relative ``replay_archive`` reaches the check absolute;
- a run without an environment: a Loop that needs one is refused by name; an
  env-less Loop negotiates against an environment that provides nothing;
- an archive consumer that is not blocking is refused.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from spectragent.core import runtime as runtime_mod
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import AgentLoopRegistrar, MissingEnvironmentError, ReactLoop
from spectragent.core.negotiation import (
    IncompatibleActionError,
    IncompatibleArchiveError,
    IncompatibleFragmentError,
)
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.prompt import ReactPromptContext
from spectragent.schemas.verdict import Judgment, VerifierIdentity
from tests._helpers.archive_fakes import (
    CAPABILITY_ID,
    CONSUMER_HOOK_ID,
    STEPS_KEY,
    ArchiveConsumerHook,
    ContractArchiveAgent,
    ContractArchiveEnv,
    ContractArchiveLLM,
    ContractArchiveMemory,
    FakeArchivingCapability,
    FakeStepArchiver,
    QuietCapability,
    make_archive_run,
)
from tests._helpers.assembly import AGENT_ID, make_env, make_run

pytestmark = pytest.mark.usefixtures("archive_fakes")


@dataclass(frozen=True)
class _Check:
    context: dict[str, Any]
    negotiation: Any
    archivers: tuple[Any, ...]
    bound_before: int


class _CheckingCap(FakeArchivingCapability):
    """The contract's archiving capability, with a setup check that records what it is asked.

    Abstract: it keeps the parent's registry key (``contract_archive``), so its
    archivers route to it by prefix.
    """

    __abstract__ = True

    async def setup(
        self,
        *,
        negotiation: Any = None,
        start_url: str | None = None,
        replay_archive: str | None = None,
    ) -> None:
        await super().setup(negotiation=negotiation, start_url=start_url)

    @property
    def checks(self) -> list[_Check]:
        return self.__dict__.setdefault("_checks", [])

    def refuse(self, error: Exception) -> None:
        self.__dict__["_refusal"] = error

    def check_setup(
        self,
        context: Mapping[str, Any],
        *,
        negotiation: Any = None,
        archivers: Sequence[Any] = (),
    ) -> None:
        self.checks.append(_Check(dict(context), negotiation, tuple(archivers), self.bind_calls))
        refusal = self.__dict__.get("_refusal")
        if refusal is not None:
            raise refusal


@pytest.fixture()
def woven(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records every ``Weaver`` the runtime builds."""
    built: list[str] = []
    real = runtime_mod.Weaver

    class RecordingWeaver(real):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            built.append("weaver")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(runtime_mod, "Weaver", RecordingWeaver)
    return built


def _composite(cap: FakeArchivingCapability) -> ContractArchiveEnv:
    """*cap* beside a capability that archives nothing and has no setup check."""
    return make_env(ContractArchiveEnv, {CAPABILITY_ID: cap, "quiet": QuietCapability()})


# ── the check ─────────────────────────────────────────────────────────


class TestPrepareRunsTheSetupCheck:
    def test_each_capability_gets_its_slice_the_negotiation_and_its_archivers(self) -> None:
        cap = _CheckingCap()
        steps = FakeStepArchiver()
        run = make_archive_run(hooks=[ArchiveConsumerHook([steps])], env=_composite(cap))
        runtime = AgentRuntime.for_agent(run, AGENT_ID)

        runtime.prepare(env_context={"start_url": "https://a.example"})

        (check,) = cap.checks
        assert check.context == {"start_url": "https://a.example"}
        assert check.negotiation is runtime.negotiated_observation
        assert check.archivers == (steps,)
        assert check.bound_before == 0, "checked before anything was bound"
        assert cap.bind_calls == 1

    def test_without_a_context_the_check_still_runs(self) -> None:
        cap = _CheckingCap()
        AgentRuntime.for_agent(make_archive_run(hooks=[], capability=cap), AGENT_ID).prepare()
        (check,) = cap.checks
        assert check.context == {} and check.archivers == ()

    def test_it_runs_after_archive_negotiation_and_before_weaving(
        self, monkeypatch: pytest.MonkeyPatch, woven: list[str]
    ) -> None:
        log: list[str] = []
        real_archive = runtime_mod.negotiate_archive

        def logged_archive(*args: Any, **kwargs: Any) -> Any:
            log.append("archive")
            return real_archive(*args, **kwargs)

        monkeypatch.setattr(runtime_mod, "negotiate_archive", logged_archive)
        cap = _CheckingCap()
        hooks: list[Any] = [ArchiveConsumerHook([FakeStepArchiver()])]
        runtime = AgentRuntime.for_agent(make_archive_run(hooks=hooks, capability=cap), AGENT_ID)
        cap.refuse(RuntimeError("refused"))
        with pytest.raises(RuntimeError, match="refused"):
            runtime.prepare()
        assert log == ["archive"] and len(cap.checks) == 1
        assert woven == [], "nothing is woven once the check refused"

    def test_a_refusal_leaves_nothing_bound(self, woven: list[str]) -> None:
        cap = _CheckingCap()
        cap.refuse(RuntimeError("replayability: pinned ↔ backend provider_id='cloud'"))
        run = make_archive_run(hooks=[ArchiveConsumerHook([FakeStepArchiver()])], capability=cap)
        with pytest.raises(RuntimeError, match="pinned ↔ backend"):
            AgentRuntime.for_agent(run, AGENT_ID).prepare()
        assert cap.bind_calls == 0 and woven == []

    @pytest.mark.parametrize("raise_on_error", [True, False])
    async def test_execute_raises_it_before_the_trace_is_opened(
        self, tmp_path: Path, raise_on_error: bool
    ) -> None:
        cap = _CheckingCap()
        cap.refuse(RuntimeError("refused"))
        writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
        run = make_archive_run(hooks=[writer], capability=cap)
        with pytest.raises(RuntimeError, match="refused"):
            await AgentRuntime.for_agent(run, AGENT_ID).execute(
                "t", timeout=10, raise_on_error=raise_on_error
            )
        assert not (tmp_path / "traces").exists(), "a trace was opened for a run never assembled"
        assert cap.fake.setup_calls == 0

    def test_unprepared_other_domain_path_reaches_the_check_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        cap = _CheckingCap()
        run = make_archive_run(hooks=[], capability=cap)
        AgentRuntime.for_agent(run, AGENT_ID).prepare(env_context={"replay_archive": "runs/src"})
        (check,) = cap.checks
        assert check.context == {"replay_archive": "runs/src"}


class TestUnknownSetupKeys:
    def test_prepare_refuses_a_key_no_capability_declares(self) -> None:
        run = make_archive_run(hooks=[])
        with pytest.raises(ValueError, match=r"\['bogus'\] are accepted by none") as info:
            AgentRuntime.for_agent(run, AGENT_ID).prepare(
                env_context={"start_url": "https://a.example", "bogus": 1}
            )
        assert "'start_url'" in str(info.value), "the message names the keys setup() declares"

    @pytest.mark.parametrize("raise_on_error", [True, False])
    async def test_execute_raises_it_before_the_trace_is_opened(
        self, tmp_path: Path, raise_on_error: bool
    ) -> None:
        cap = FakeArchivingCapability()
        writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
        run = make_archive_run(hooks=[writer], capability=cap)
        with pytest.raises(ValueError, match="bogus"):
            await AgentRuntime.for_agent(run, AGENT_ID).execute(
                "t", timeout=10, env_context={"bogus": 1}, raise_on_error=raise_on_error
            )
        assert not (tmp_path / "traces").exists()
        assert cap.fake.setup_calls == 0


# ── no environment ────────────────────────────────────────────────────


class _EnvlessLoop(ReactLoop):
    """A ReAct loop that declares it runs without an environment."""

    __registry_key__ = "test_setup_check_envless_loop"
    __requires_env__ = False


def teardown_module() -> None:
    # Idempotent: pytest tears a module down once per contiguous run of its
    # tests, so a selection that interleaves files tears this one down twice.
    if AgentLoopRegistrar.get_or_none("test_setup_check_envless_loop") is not None:
        AgentLoopRegistrar.unregister("test_setup_check_envless_loop")


class _TaskOnlyAgent:
    """Asks an environment for nothing: only the task, which the Loop builds."""

    __execution_limit_fields__ = frozenset()

    __wiring__ = {"observation": ("observation", ["loop.task"])}

    async def build_react_prompt(
        self, obs: Any, *, context: ReactPromptContext
    ) -> list[dict[str, str]]:
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out: Any) -> list[Any]:
        return []


class _ClickingAgent(_TaskOnlyAgent):
    """Needs an action only an environment provides."""

    __wiring__ = {"observation": ("observation", ["loop.task"]), "action": ("action", ["click"])}


class _FinishingAgent(_TaskOnlyAgent):
    """Needs only ``done``, which the Loop provides (``loop.done``)."""

    __wiring__ = {"observation": ("observation", ["loop.task"]), "action": ("action", ["done"])}


def _envless(agent: Any, loop: Any, hooks: list[Any] | None = None) -> Any:
    return make_run(
        agent=agent,
        loop=loop,
        env=None,
        llm=ContractArchiveLLM(),
        memory=ContractArchiveMemory(),
        hooks=hooks,
    )


class TestNoEnvironment:
    def test_a_loop_that_needs_one_is_refused_by_name(self, woven: list[str]) -> None:
        run = _envless(_TaskOnlyAgent(), ReactLoop(max_steps=1))
        with pytest.raises(MissingEnvironmentError) as info:
            AgentRuntime.for_agent(run, AGENT_ID).prepare()
        assert info.value.loop_id == "react"
        assert "requires an environment" in str(info.value)
        assert woven == []

    async def test_execute_raises_it_before_the_trace_is_opened(self, tmp_path: Path) -> None:
        writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
        run = _envless(_TaskOnlyAgent(), ReactLoop(max_steps=1), hooks=[writer])
        with pytest.raises(MissingEnvironmentError):
            await AgentRuntime.for_agent(run, AGENT_ID).execute(
                "t", timeout=10, raise_on_error=False
            )
        assert not (tmp_path / "traces").exists()

    def test_an_env_less_loop_negotiates_against_nothing(self) -> None:
        runtime = AgentRuntime.for_agent(_envless(_TaskOnlyAgent(), _EnvlessLoop()), AGENT_ID)
        prepared = runtime.prepare()
        assert callable(prepared.run)
        observation, actions = runtime.negotiated_observation, runtime.negotiated_actions
        assert observation is not None and actions is not None
        assert observation.active_fragments == frozenset({"loop.task"})
        assert observation.env_sourced == frozenset()
        assert actions.active_actions == frozenset()
        assert runtime.active_descriptors == [], "an env-less run advertises no action"

    def test_an_agent_that_needs_an_environment_fragment_is_refused_by_negotiation(self) -> None:
        runtime = AgentRuntime.for_agent(_envless(ContractArchiveAgent(), _EnvlessLoop()), AGENT_ID)
        with pytest.raises(IncompatibleFragmentError) as info:
            runtime.prepare()
        assert info.value.missing == {"page"}
        assert info.value.provided == set() and info.value.provider == "environment"

    def test_an_agent_that_needs_an_environment_action_is_refused_by_negotiation(self) -> None:
        runtime = AgentRuntime.for_agent(_envless(_ClickingAgent(), _EnvlessLoop()), AGENT_ID)
        with pytest.raises(IncompatibleActionError) as info:
            runtime.prepare()
        assert info.value.missing == {"click"} and info.value.provided == set()
        assert info.value.provider == "environment"

    def test_the_loops_own_actions_are_its_whole_action_space(self) -> None:
        """``done`` needs no environment: the Loop provides and executes ``loop.done``."""
        runtime = AgentRuntime.for_agent(_envless(_FinishingAgent(), _EnvlessLoop()), AGENT_ID)
        runtime.prepare()
        assert [d.name for d in runtime.active_descriptors] == ["loop.done"]
        assert runtime.action_space.action_bindings == {"done": "loop.done"}

    def test_setup_keys_without_an_environment_are_refused(self) -> None:
        runtime = AgentRuntime.for_agent(_envless(_TaskOnlyAgent(), _EnvlessLoop()), AGENT_ID)
        with pytest.raises(ValueError, match=r"\['start_url'\] .* no environment"):
            runtime.prepare(env_context={"start_url": "https://a.example"})


# ── the archive consumer must block ───────────────────────────────────


class _NonBlockingConsumer(ArchiveConsumerHook):
    """An archive consumer whose handlers would run after the commit point."""

    __abstract__ = True

    @property
    def blocking(self) -> bool:
        return False


class TestArchiveConsumerMustBlock:
    def test_prepare_refuses_a_consumer_that_does_not_block(self, woven: list[str]) -> None:
        cap = FakeArchivingCapability()
        run = make_archive_run(hooks=[_NonBlockingConsumer([FakeStepArchiver()])], capability=cap)
        with pytest.raises(IncompatibleArchiveError) as info:
            AgentRuntime.for_agent(run, AGENT_ID).prepare()
        (problem,) = info.value.problems
        assert problem.startswith(
            f"{CONSUMER_HOOK_ID} requests archivers {[STEPS_KEY]} but is not blocking"
        )
        assert "an archive consumer must be blocking" in problem
        assert cap.bind_calls == 0 and woven == []


# ── the benchmark's setup component ───────────────────────────────────


@dataclass
class _Setup:
    """A benchmark setup component hosted by the contract's archiving capability."""

    setup_id: str = "contract_setup"
    capability_id: str = CAPABILITY_ID


@dataclass
class _Verifier:
    """A live verifier hosted by the contract's archiving capability."""

    verifier_id: str = "contract_judge"
    capability_id: str = CAPABILITY_ID
    needs: frozenset[str] = frozenset({"live_environment"})

    def identity(self) -> VerifierIdentity:
        return VerifierIdentity(verifier_id=self.verifier_id)


@dataclass
class _Benchmark:
    """The run's benchmark block, as far as ``prepare()`` reads it: its setup component and live verifiers."""

    setup: _Setup | None
    verifiers: list[_Verifier] = field(default_factory=list)

    def live_verifiers(self) -> list[_Verifier]:
        return list(self.verifiers)


class _HostingCap(_CheckingCap):
    """A capability that hosts setup components (``SetupHost``), recording what it is handed."""

    __abstract__ = True

    @property
    def hosted(self) -> list[tuple[str, Any]]:
        return self.__dict__.setdefault("_hosted", [])

    def check_setup(
        self,
        context: Mapping[str, Any],
        *,
        negotiation: Any = None,
        archivers: Sequence[Any] = (),
        setup: Any = None,
    ) -> None:
        self.hosted.append(("check", setup))
        super().check_setup(context, negotiation=negotiation, archivers=archivers)

    def bind_setup(self, setup: Any) -> None:
        self.hosted.append(("bind", setup))


class _JudgingCap(_HostingCap):
    """A capability that hosts live verifiers too (``LiveVerifierHost``), recording what it is handed."""

    __abstract__ = True

    def check_setup(
        self,
        context: Mapping[str, Any],
        *,
        negotiation: Any = None,
        archivers: Sequence[Any] = (),
        setup: Any = None,
        live_verifiers: Sequence[Any] = (),
    ) -> None:
        self.hosted.append(("check verifiers", tuple(live_verifiers)))
        super().check_setup(context, negotiation=negotiation, archivers=archivers, setup=setup)

    def bind_live_verifiers(self, verifiers: Sequence[Any]) -> None:
        self.hosted.append(("bind verifiers", tuple(verifiers)))

    async def verify_live(self, verifier: Any, *, answer: str | None) -> Judgment:
        return Judgment(score=1.0)


def _benchmark_run(
    cap: FakeArchivingCapability | None,
    setup: _Setup | None,
    verifiers: Sequence[_Verifier] = (),
) -> Any:
    env = None if cap is None else _composite(cap)
    return make_run(
        agent=ContractArchiveAgent(),
        loop=ReactLoop(max_steps=3),
        env=env,
        llm=ContractArchiveLLM(),
        memory=ContractArchiveMemory(),
        benchmark=_Benchmark(setup, list(verifiers)),
    )


class TestPrepareRoutesTheBenchmarksSetup:
    def test_the_host_checks_it_and_it_is_bound_last(self) -> None:
        cap, setup = _HostingCap(), _Setup()

        AgentRuntime.for_agent(_benchmark_run(cap, setup), AGENT_ID).prepare()

        assert cap.hosted == [("check", setup), ("bind", setup)]

    def test_a_benchmark_without_one_binds_nothing(self) -> None:
        cap = _HostingCap()

        AgentRuntime.for_agent(_benchmark_run(cap, None), AGENT_ID).prepare()

        assert cap.hosted == [("check", None)], "checked with none, and no binding call"

    def test_a_refusing_host_binds_nothing(self) -> None:
        cap, setup = _HostingCap(), _Setup()
        cap.refuse(ValueError("not this task"))

        with pytest.raises(ValueError, match="not this task"):
            AgentRuntime.for_agent(_benchmark_run(cap, setup), AGENT_ID).prepare()

        assert cap.hosted == [("check", setup)]

    def test_a_capability_that_hosts_none_is_refused_by_name(self) -> None:
        with pytest.raises(ValueError, match="hosts no setup component"):
            AgentRuntime.for_agent(_benchmark_run(_CheckingCap(), _Setup()), AGENT_ID).prepare()

    def test_a_run_without_an_environment_is_refused(self) -> None:
        run = make_run(
            agent=_TaskOnlyAgent(),
            loop=_EnvlessLoop(max_steps=3),
            env=None,
            llm=ContractArchiveLLM(),
            memory=ContractArchiveMemory(),
            benchmark=_Benchmark(_Setup()),
        )
        with pytest.raises(ValueError, match="hosted by the 'contract_archive' capability, and "):
            AgentRuntime.for_agent(run, AGENT_ID).prepare()


class TestABranchTasksTwoBenchmarkBlocks:
    """A branch task's run holds its task source and its source's benchmark (§4.10.1).

    The task source never has a setup component or a verifier of its own, so
    what is routed is the source benchmark's.
    """

    def test_the_source_benchmarks_components_are_routed(self) -> None:
        cap, setup, verifier = _JudgingCap(), _Setup(), _Verifier()
        run = _benchmark_run(cap, setup, [verifier])
        task_source = _Benchmark(None)
        run.by_layer["benchmark"] = {
            "trace_branches#0": task_source,
            "source#0": run.by_layer["benchmark"]["benchmark#0"],
        }

        AgentRuntime.for_agent(run, AGENT_ID).prepare()

        assert ("bind", setup) in cap.hosted
        assert ("bind verifiers", (verifier,)) in cap.hosted


class TestPrepareRoutesTheBenchmarksLiveVerifiers:
    """Routed as the setup component is: checked by their host with it, then bound last."""

    def test_the_host_checks_them_with_the_setup_and_they_are_bound_last(self) -> None:
        cap, setup, verifier = _JudgingCap(), _Setup(), _Verifier()

        AgentRuntime.for_agent(_benchmark_run(cap, setup, [verifier]), AGENT_ID).prepare()

        assert cap.hosted == [
            ("check verifiers", (verifier,)),
            ("check", setup),
            ("bind", setup),
            ("bind verifiers", (verifier,)),
        ]

    def test_a_benchmark_without_one_binds_none(self) -> None:
        cap = _JudgingCap()

        AgentRuntime.for_agent(_benchmark_run(cap, _Setup()), AGENT_ID).prepare()

        assert ("check verifiers", ()) in cap.hosted
        assert [event for event, _ in cap.hosted if event == "bind verifiers"] == []

    def test_a_refusing_host_binds_nothing(self) -> None:
        cap = _JudgingCap()
        cap.refuse(ValueError("cannot judge here"))

        with pytest.raises(ValueError, match="cannot judge here"):
            AgentRuntime.for_agent(_benchmark_run(cap, None, [_Verifier()]), AGENT_ID).prepare()

        assert [event for event, _ in cap.hosted if event.startswith("bind")] == []

    def test_a_capability_that_hosts_none_is_refused_by_name(self) -> None:
        run = _benchmark_run(_HostingCap(), None, [_Verifier()])
        with pytest.raises(
            ValueError,
            match="the live verifier 'contract_judge' is hosted by the 'contract_archive' "
            "capability, which hosts no live verifier",
        ):
            AgentRuntime.for_agent(run, AGENT_ID).prepare()

    def test_a_run_without_an_environment_is_refused(self) -> None:
        run = make_run(
            agent=_TaskOnlyAgent(),
            loop=_EnvlessLoop(max_steps=3),
            env=None,
            llm=ContractArchiveLLM(),
            memory=ContractArchiveMemory(),
            benchmark=_Benchmark(None, [_Verifier()]),
        )
        with pytest.raises(
            ValueError,
            match="the live verifier 'contract_judge' is hosted by the 'contract_archive' "
            "capability, and this run has no environment",
        ):
            AgentRuntime.for_agent(run, AGENT_ID).prepare()
