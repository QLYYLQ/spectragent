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
from dataclasses import dataclass
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

    def test_a_relative_replay_archive_reaches_the_check_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        cap = _CheckingCap()
        run = make_archive_run(hooks=[], capability=cap)
        AgentRuntime.for_agent(run, AGENT_ID).prepare(env_context={"replay_archive": "runs/src"})
        (check,) = cap.checks
        assert check.context == {"replay_archive": str((tmp_path / "runs" / "src").resolve())}


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


class _ActingAgent(_TaskOnlyAgent):
    """Needs an action — something only an environment provides."""

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

    def test_an_agent_that_needs_an_action_is_refused_by_negotiation(self) -> None:
        runtime = AgentRuntime.for_agent(_envless(_ActingAgent(), _EnvlessLoop()), AGENT_ID)
        with pytest.raises(IncompatibleActionError) as info:
            runtime.prepare()
        assert info.value.missing == {"done"} and info.value.provided == set()

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
