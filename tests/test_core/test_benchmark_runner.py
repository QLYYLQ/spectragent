"""BenchmarkRunner — per-task lifecycle, identity, and isolation."""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.core.benchmark_runner import (
    PREFLIGHT_EXIT_CODE,
    TOKEN_LIMIT_EXIT_CODE,
    VETO_EXIT_CODE,
    BenchmarkRunner,
    BenchmarkRunSummary,
    PreflightError,
    TaskOutcome,
    TokenLimitReached,
    _safe_run_id,
)
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.llm.exceptions import ModelOutputTruncatedError, ModelRateLimitError
from spectragent.core.loops import ReactLoop
from spectragent.core.loops._registrar import AgentLoopRegistrar, LoopInput
from spectragent.core.loops.errors import RunAbort
from spectragent.core.negotiation import IncompatibleActionError
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.config_source import ConfigSource
from spectragent.schemas.recording import RecordingPlan
from spectragent.schemas.restoration import BranchProvenance, Checkpoint
from spectragent.schemas.task import TaskContext
from tests._helpers.assembly import make_env, make_run


class _Cap(CapabilityBase):
    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "bench_runner_cap"
    __wiring__ = {"action": ("action", ["bench_runner_cap.done"])}

    def get_action_descriptors(self, **ctx):
        return [ActionDescriptor(name="bench_runner_cap.done", description="Done")]

    async def execute_action(self, action):
        return ActionResult(success=True, is_done=True, extracted_content="answer")

    async def setup(self, *, negotiation=None, start_url=None):
        # Declaring ``start_url`` is what lets the env route the loader's
        # task context here.
        self.start_url = start_url
        await super().setup(negotiation=negotiation)


class _Env(EnvironmentBase):
    __wiring__ = {"capabilities": "capability"}
    __registry_key__ = "bench_runner_env"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.setup_kwargs: dict[str, Any] = {}
        self.torn_down = False

    async def setup(self, context=None, *, negotiation=None):
        self.setup_kwargs = {"context": context, "negotiation": negotiation}
        await super().setup(context, negotiation=negotiation)

    async def teardown(self):
        self.torn_down = True
        await super().teardown()


class _Agent:
    __execution_limit_fields__ = frozenset()
    __wiring__ = {"action": ("action", ["bench_runner_cap.done"])}

    async def build_react_prompt(self, obs, *, context):
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out):
        return [Action(name="bench_runner_cap.done")]

    def get_react_output_model(self):
        return None


class _Usage:
    prompt_tokens = 5
    completion_tokens = 5
    prompt_cached_tokens = 0
    total_tokens = 10


class _Completion:
    completion = "done"
    usage = _Usage()


class _LLM:
    inference_kind = "chat"
    model = "test-model"
    name = "test-model"

    async def aclose(self, *, timeout=5.0):
        return ()

    async def ainvoke(self, messages, **kw):
        return _Completion()


class _Memory:
    supports_protocols = frozenset({"user_assistant"})

    def __init__(self):
        self.messages: list[Any] = []

    def add_message(self, m):
        self.messages.append(m)

    def add_messages(self, ms):
        self.messages.extend(ms)

    def get_context_messages(self):
        return list(self.messages)

    def clear(self):
        self.messages.clear()


class _Task:
    def __init__(self, task_id: str, text: str):
        self.task_id = task_id
        self.task_text = text
        self.env_type = "browser"
        self.difficulty = "hard"
        self.domain = "shopping"
        self.tags = ["nav"]


class _Loader:
    """Stub TaskLoader — the runner duck-types this surface."""

    benchmark_name = "online_mind2web"
    subset = "Hard"

    def __init__(self, broken: set[str] | None = None):
        self._tasks = {
            "t-1": _Task("t-1", "find the price"),
            "t-2": _Task("t-2", "add to cart"),
        }
        self._broken = broken or set()

    def task_ids(self):
        return list(self._tasks)

    def __getitem__(self, task_id):
        if task_id in self._broken:
            raise KeyError(task_id)
        return self._tasks[task_id]

    def get_task_context(self, task_id):
        return TaskContext(env={"start_url": f"https://example.com/{task_id}"})


def _make_factory(tmp_path, envs: list[_Env], writers: list[TraceWriter]):
    """Compose a fresh stack per task, like a real benchmark sweep."""

    def compose():
        env = make_env(_Env, {"bench_runner_cap": _Cap()})
        writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
        envs.append(env)
        writers.append(writer)
        return make_run(
            agent=_Agent(),
            loop=ReactLoop(max_steps=2),
            env=env,
            llm=_LLM(),
            memory=_Memory(),
            hooks=[writer],
        )

    return compose


async def test_explicit_configuration_source_reaches_every_benchmark_trace(tmp_path):
    source = ConfigSource.from_path(tmp_path / "batch.yaml", data=b"source: fixture\n")
    runner = BenchmarkRunner(
        loader=_Loader(),
        compose=_make_factory(tmp_path, [], []),
        config_source=source,
    )
    summary = await runner.run()
    assert len(summary.outcomes) == 2
    for outcome in summary.outcomes:
        assert outcome.trace_dir is not None
        assert RunTrace(outcome.trace_dir).config.config_source == source


async def test_task_context_failure_does_not_stop_later_tasks(tmp_path):
    class ContextFailureLoader(_Loader):
        def get_task_context(self, task_id):
            if task_id == "t-2":
                raise ValueError("invalid setup data for this task")
            return super().get_task_context(task_id)

    loader = ContextFailureLoader()
    loader._tasks["t-3"] = _Task("t-3", "inspect the cart")
    envs: list[_Env] = []
    runner = BenchmarkRunner(loader=loader, compose=_make_factory(tmp_path, envs, []))

    summary = await runner.run()

    assert [outcome.task_id for outcome in summary.outcomes] == ["t-1", "t-2", "t-3"]
    assert summary.completed == 2
    assert summary.failed == 1
    assert "invalid setup data" in (summary.outcomes[1].error or "")
    assert summary.outcomes[1].trace_dir is None
    assert len(envs) == 2
    assert all(env.torn_down for env in envs)


class TestRunIdSafety:
    def test_unsafe_ids_are_sanitised_but_legible(self):
        run_id = _safe_run_id("Cross-Task/2024 #7")
        assert "/" not in run_id and " " not in run_id
        assert run_id.startswith("Cross-Task-2024-7_")

    def test_reruns_do_not_collide(self):
        assert _safe_run_id("t-1") != _safe_run_id("t-1")


class TestBenchmarkRunner:
    async def test_runs_every_task(self, tmp_path):
        envs: list[_Env] = []
        writers: list[TraceWriter] = []
        runner = BenchmarkRunner(
            loader=_Loader(),
            compose=_make_factory(tmp_path, envs, writers),
            timeout=10,
        )
        summary = await runner.run()

        assert summary.benchmark == "online_mind2web"
        assert summary.subset == "Hard"
        assert summary.total == 2
        assert summary.completed == 2
        assert summary.failed == 0
        assert summary.count_by_done() == {"done": 2}
        assert [o.task_id for o in summary.outcomes] == ["t-1", "t-2"]

    async def test_each_task_gets_a_fresh_stack_and_lifecycle(self, tmp_path):
        envs: list[_Env] = []
        writers: list[TraceWriter] = []
        runner = BenchmarkRunner(
            loader=_Loader(),
            compose=_make_factory(tmp_path, envs, writers),
            timeout=10,
        )
        await runner.run()

        assert len(envs) == 2
        assert all(e.torn_down for e in envs)
        # Task context reached the environment — the loader→capability
        # link that did not exist before.
        assert envs[0].setup_kwargs["context"] == {"start_url": "https://example.com/t-1"}
        cap = envs[0].capabilities["bench_runner_cap"]
        assert isinstance(cap, _Cap)
        assert cap.start_url == "https://example.com/t-1"
        assert envs[1].setup_kwargs["context"] == {"start_url": "https://example.com/t-2"}

    async def test_traces_are_identifiable_per_task(self, tmp_path):
        envs: list[_Env] = []
        writers: list[TraceWriter] = []
        runner = BenchmarkRunner(
            loader=_Loader(),
            compose=_make_factory(tmp_path, envs, writers),
            timeout=10,
        )
        summary = await runner.run()

        for outcome, writer in zip(summary.outcomes, writers, strict=True):
            assert outcome.trace_dir is not None
            assert outcome.trace_dir == writer.run_dir
            trace = RunTrace(outcome.trace_dir)
            assert trace.config.task_id == outcome.task_id
            assert trace.config.benchmark == "online_mind2web"
            assert trace.config.subset == "Hard"
            assert trace.config.extra["difficulty"] == "hard"
            assert outcome.trace_dir.name.startswith(outcome.task_id)

    async def test_limit_and_explicit_ids(self, tmp_path):
        envs: list[_Env] = []
        writers: list[TraceWriter] = []
        runner = BenchmarkRunner(
            loader=_Loader(),
            compose=_make_factory(tmp_path, envs, writers),
            timeout=10,
        )
        assert runner.select_task_ids(limit=1) == ["t-1"]
        assert runner.select_task_ids(task_ids=["t-2"]) == ["t-2"]

        summary = await runner.run(limit=1)
        assert summary.total == 1

    async def test_an_environment_that_never_came_up_is_not_a_completed_task(self, tmp_path):
        """A sweep in which no browser ever started must not exit 0.

        ``execute`` is called with ``raise_on_error=False``, so an env
        that cannot connect comes back as an ordinary ``AgentResult`` —
        no exception, ``TaskOutcome.error is None`` — carrying
        ``env_status=failed``. Reading only ``error`` reported
        ``tasks=2 completed=2 failed=0`` and returned exit code 0, so a
        CI gate saw a fully green sweep of runs that never happened.
        """

        class _DeadEnv(_Env):
            async def setup(self, context=None, *, negotiation=None):
                raise ConnectionError("cannot connect to CDP at ws://127.0.0.1:9222")

        envs: list[_Env] = []
        writers: list[TraceWriter] = []

        def compose():
            env = make_env(_DeadEnv, {"bench_runner_cap": _Cap()})
            writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
            envs.append(env)
            writers.append(writer)
            return make_run(
                agent=_Agent(),
                loop=ReactLoop(max_steps=2),
                env=env,
                llm=_LLM(),
                memory=_Memory(),
                hooks=[writer],
            )

        summary = await BenchmarkRunner(loader=_Loader(), compose=compose, timeout=10).run()

        assert summary.total == 2
        assert summary.completed == 0
        assert summary.failed == 2
        assert all(not o.ok for o in summary.outcomes)
        # The real message reaches the table it belongs in, instead of the
        # bare status being the only thing printed.
        outcome = summary.outcomes[0]
        assert outcome.error is None  # execute did not raise...
        assert outcome.infrastructure_error is not None  # ...but it failed
        assert "cannot connect to CDP" in outcome.infrastructure_error
        assert "cannot connect to CDP" in summary.format_table()

    async def test_an_agent_that_ran_out_of_steps_is_still_a_completed_task(self, tmp_path):
        """The infra check must not swallow ordinary agent failure.

        ``max_steps`` in a healthy browser is a real attempt that scored
        badly — that is the Evaluator's business, not the runner's, and it
        must keep counting as completed.
        """

        class _Stubborn(_Agent):
            def parse_react_output(self, out):
                return []  # never calls done → the loop exhausts max_steps

        envs: list[_Env] = []
        writers: list[TraceWriter] = []

        def compose():
            env = make_env(_Env, {"bench_runner_cap": _Cap()})
            writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
            envs.append(env)
            writers.append(writer)
            return make_run(
                agent=_Stubborn(),
                loop=ReactLoop(max_steps=2),
                env=env,
                llm=_LLM(),
                memory=_Memory(),
                hooks=[writer],
            )

        summary = await BenchmarkRunner(loader=_Loader(), compose=compose, timeout=10).run()

        assert summary.count_by_done() == {"max_steps": 2}
        assert summary.completed == 2
        assert summary.failed == 0

    async def test_one_bad_task_does_not_kill_the_sweep(self, tmp_path):
        envs: list[_Env] = []
        writers: list[TraceWriter] = []
        runner = BenchmarkRunner(
            loader=_Loader(broken={"t-1"}),
            compose=_make_factory(tmp_path, envs, writers),
            timeout=10,
        )
        summary = await runner.run()

        assert summary.total == 2
        assert summary.failed == 1
        assert summary.outcomes[0].error is not None
        assert summary.outcomes[0].agent_done == "not_started"
        assert summary.outcomes[1].ok
        # 297 of 300 tasks must not be lost because task 3 was malformed.
        assert "t-1" in summary.format_table()


class _GreedyAgent(_Agent):
    """Requires an action the capability does not provide: no task of the sweep can run."""

    __wiring__ = {"action": ("action", ["bench_runner_cap.done", "bench_runner_cap.fly"])}


class _BogusKeyLoader(_Loader):
    """Hands the first task a setup key no capability declares."""

    def get_task_context(self, task_id):
        return TaskContext(env={"start_url": f"https://example.com/{task_id}", "bogus": 1})


def _counting_factory(tmp_path, envs, writers, *, agent_cls=_Agent):
    def compose():
        env = make_env(_Env, {"bench_runner_cap": _Cap()})
        writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
        envs.append(env)
        writers.append(writer)
        return make_run(
            agent=agent_cls(),
            loop=ReactLoop(max_steps=2),
            env=env,
            llm=_LLM(),
            memory=_Memory(),
            hooks=[writer],
        )

    return compose


class TestPreflight:
    """``prepare()`` once, for the first task, before the sweep starts (arch §4.2).

    A configuration that cannot run refuses every task alike, so the sweep
    fails up front — no task runs, no trace is opened — instead of recording
    the same refusal once per task.
    """

    async def test_a_refused_configuration_runs_no_task(self, tmp_path):
        envs: list[_Env] = []
        writers: list[TraceWriter] = []
        runner = BenchmarkRunner(
            loader=_Loader(),
            compose=_counting_factory(tmp_path, envs, writers, agent_cls=_GreedyAgent),
            timeout=10,
        )
        with pytest.raises(PreflightError) as info:
            await runner.run()

        error = info.value
        assert error.task_id == "t-1"
        assert isinstance(error.error, IncompatibleActionError)
        assert error.__cause__ is error.error
        message = str(error)
        assert "refused the configuration for task 't-1', so no task was run" in message
        # Both sides: what the agent requires and what the environment provides.
        assert "bench_runner_cap.fly" in message and "bench_runner_cap.done" in message
        assert len(envs) == 1, "only the pre-flight composed a stack"
        assert envs[0].setup_kwargs == {}, "nothing was set up"
        assert not (tmp_path / "traces").exists(), "no trace was opened"

    async def test_it_checks_the_first_task_s_context(self, tmp_path):
        envs: list[_Env] = []
        writers: list[TraceWriter] = []
        runner = BenchmarkRunner(
            loader=_BogusKeyLoader(),
            compose=_counting_factory(tmp_path, envs, writers),
            timeout=10,
        )
        with pytest.raises(PreflightError, match=r"\['bogus'\] are accepted by none"):
            await runner.run()
        assert not (tmp_path / "traces").exists()

    async def test_the_first_task_runs_on_the_pre_flight_stack(self, tmp_path):
        envs: list[_Env] = []
        writers: list[TraceWriter] = []
        runner = BenchmarkRunner(
            loader=_Loader(), compose=_counting_factory(tmp_path, envs, writers), timeout=10
        )
        summary = await runner.run()
        assert summary.completed == 2
        assert len(envs) == 2, "one stack per task, the pre-flight's included"
        assert [o.trace_dir for o in summary.outcomes] == [w.run_dir for w in writers]

    async def test_an_empty_selection_checks_nothing(self, tmp_path):
        envs: list[_Env] = []
        writers: list[TraceWriter] = []
        runner = BenchmarkRunner(
            loader=_Loader(),
            compose=_counting_factory(tmp_path, envs, writers, agent_cls=_GreedyAgent),
            timeout=10,
        )
        summary = await runner.run(task_ids=[])
        assert summary.total == 0 and envs == []

    def test_the_cli_exits_2_and_says_why(self, tmp_path, monkeypatch, capsys):
        from spectragent.core.benchmark_runner import main
        from tests._helpers.branch_sources import write_source

        def refuse(self, task_id):
            raise PreflightError(task_id, ValueError("replayability: pinned ↔ provider"))

        executed: list[str] = []
        monkeypatch.setattr(BenchmarkRunner, "preflight", refuse)
        monkeypatch.setattr(
            BenchmarkRunner, "run_task", lambda self, task_id, **kw: executed.append(task_id)
        )
        write_source(tmp_path / "recorded", "run-a", steps=2)

        code = main(
            [
                "--session-dir",
                str(tmp_path / ".sessions"),
                "--config",
                str(_branch_sweep_config(tmp_path)),
                "--no-eval",
            ]
        )

        assert code == PREFLIGHT_EXIT_CODE == 2
        assert executed == []
        err = capsys.readouterr().err
        assert "spectragent bench: the pre-flight check refused the configuration" in err
        assert "replayability: pinned ↔ provider" in err


class _VetoingAgent(_Agent):
    """An agent whose guard inspects the model's reply and refuses to go on.

    The veto lives in ``parse_react_output`` because that is where a real one
    lives: the parser is an agent method, so it is woven, and it is the first
    place the agent sees what the model actually wrote.
    """

    def __init__(self, *, vetoes: bool) -> None:
        self._vetoes = vetoes

    async def build_react_prompt(self, obs, *, context):
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out):
        if self._vetoes:
            raise RunAbort("risk-control page detected")
        return [Action(name="bench_runner_cap.done")]


def _vetoing_factory(tmp_path, envs, writers, *, veto_on_task: int):
    """Like ``_make_factory``, but the agent for task *veto_on_task* vetoes.

    Indexed by compose order rather than by task text: the runner composes a
    fresh stack per task, in order, and the task itself reaches the loop as a
    ``run()`` argument — it is never set on the agent, so an agent cannot
    recognise which task it is on.
    """
    composed = 0

    def compose():
        nonlocal composed
        env = make_env(_Env, {"bench_runner_cap": _Cap()})
        writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
        envs.append(env)
        writers.append(writer)
        agent = _VetoingAgent(vetoes=composed == veto_on_task)
        composed += 1
        return make_run(
            agent=agent,
            loop=ReactLoop(max_steps=2),
            env=env,
            llm=_LLM(),
            memory=_Memory(),
            hooks=[writer],
        )

    return compose


class TestGuardVeto:
    """A guard's veto stops the sweep; a plain failure does not.

    ``raise_on_error=False`` exists so one bad task cannot end a 300-task
    sweep. A veto is the opposite instruction — a guard looked at this run and
    said *stop* — so it has to be able to travel past that flag, through
    ``AgentRuntime.run`` and ``BenchmarkRunner.run_task``, both of which
    otherwise collapse every exception into a recorded failure.
    """

    async def test_a_veto_stops_the_sweep_and_keeps_what_ran(self, tmp_path):
        envs: list[_Env] = []
        writers: list[TraceWriter] = []
        runner = BenchmarkRunner(
            loader=_Loader(),
            compose=_vetoing_factory(tmp_path, envs, writers, veto_on_task=1),
            timeout=10,
        )
        summary = await runner.run()

        assert summary.was_aborted
        assert summary.aborted is not None
        assert "t-2" in summary.aborted
        assert "risk-control page detected" in summary.aborted
        # The guard said stop, not destroy the evidence: t-1 finished before
        # the veto and its outcome is still here to be scored — and the vetoed
        # task is shown, as what stopped the sweep.
        first, vetoed = summary.outcomes
        assert (first.task_id, first.ok) == ("t-1", True)
        assert vetoed.task_id == "t-2" and vetoed.agent_done == "vetoed"
        assert vetoed.vetoed is not None and "risk-control page detected" in vetoed.vetoed
        assert vetoed.result is not None and vetoed.result.agent_done == AgentDoneStatus.VETOED
        assert not vetoed.ok and vetoed.infrastructure_error is None  # a veto, not a failure
        assert summary.failed == 0
        table = summary.format_table()
        assert "vetoed=1" in table.splitlines()[0]
        assert "  t-2: vetoed (" in table and "vetoed: RunAbort" in table
        assert "vetoed: a guard stopped the sweep at t-2" in table

    async def test_an_ordinary_failure_still_does_not_stop_the_sweep(self, tmp_path):
        """The veto path must not have widened into "any exception stops"."""
        envs: list[_Env] = []
        writers: list[TraceWriter] = []
        runner = BenchmarkRunner(
            loader=_Loader(broken={"t-1"}),
            compose=_make_factory(tmp_path, envs, writers),
            timeout=10,
        )
        summary = await runner.run()

        assert not summary.was_aborted
        assert summary.aborted is None
        assert summary.total == 2

    async def test_the_veto_is_in_the_trace_before_it_propagates(self, tmp_path):
        """``_finish`` runs first, so the evidence outlives the exception."""
        envs: list[_Env] = []
        writers: list[TraceWriter] = []
        runner = BenchmarkRunner(
            loader=_Loader(),
            compose=_vetoing_factory(tmp_path, envs, writers, veto_on_task=0),
            timeout=10,
        )
        summary = await runner.run()

        assert summary.was_aborted
        assert [o.agent_done for o in summary.outcomes] == ["vetoed"]  # the very first task
        # A trace was still opened and closed out for the vetoed run.
        trace_dirs = sorted((tmp_path / "traces").iterdir())
        assert trace_dirs, "the vetoed run left no trace at all"
        trace = RunTrace(trace_dirs[0])
        assert trace.result is not None
        assert trace.result.agent_done == AgentDoneStatus.VETOED
        assert "RunAbort" in (getattr(trace.result, "error", "") or "")
        assert "risk-control page detected" in (getattr(trace.result, "error", "") or "")
        assert summary.outcomes[0].trace_status == "complete"


class _RaisingLLM:
    inference_kind = "chat"
    model = "test-model"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def ainvoke(self, messages, **kw):
        raise self._exc


def _factory_with(tmp_path, *, llm: Any):
    def compose():
        return make_run(
            agent=_Agent(),
            loop=ReactLoop(max_steps=2),
            env=make_env(_Env, {"bench_runner_cap": _Cap()}),
            llm=llm,
            memory=_Memory(),
            hooks=[TraceWriter(trace_dir=str(tmp_path / "traces"))],
        )

    return compose


class TestTheSummaryCountsThreeLedgers:
    """How the agent ended, whether the environment held, whether the trace is complete."""

    async def test_the_table_has_the_three_counters(self, tmp_path):
        runner = BenchmarkRunner(
            loader=_Loader(), compose=_make_factory(tmp_path, [], []), timeout=10
        )
        summary = await runner.run()

        assert summary.count_by_done() == {"done": 2}
        assert summary.count_by_environment() == {"success": 2}
        assert summary.count_by_trace() == {"complete": 2}
        lines = summary.format_table().splitlines()
        assert lines[1:4] == [
            "  agent outcome: done=2",
            "  environment: success=2",
            "  trace: complete=2",
        ]
        assert all(o.trace_status == "complete" and o.ok for o in summary.outcomes)

    async def test_a_model_unavailable_after_retries_is_an_infrastructure_failure(self, tmp_path):
        """The agent's ledger says error; the summary says what it really was."""
        llm = _RaisingLLM(ModelRateLimitError("rate limit exceeded", model="test-model"))
        runner = BenchmarkRunner(
            loader=_Loader(), compose=_factory_with(tmp_path, llm=llm), timeout=10
        )
        summary = await runner.run(task_ids=["t-1"])

        (outcome,) = summary.outcomes
        assert (outcome.agent_done, outcome.env_status) == ("error", "success")
        assert outcome.model_unavailable == "ModelRateLimitError: rate limit exceeded"
        assert outcome.infrastructure_error == (
            "the model call failed after the provider's retries: "
            "ModelRateLimitError: rate limit exceeded"
        )
        assert outcome.trace_status == "complete"
        assert summary.failed == 1 and summary.completed == 0
        assert "infrastructure failure: the model call failed" in summary.format_table()

    async def test_a_truncated_reply_is_the_runs_own_ceiling_not_an_outage(self, tmp_path):
        llm = _RaisingLLM(
            ModelOutputTruncatedError(
                model="test-model",
                stop_reason="length",
                ceiling_field="max_completion_tokens",
                ceiling=16,
            )
        )
        runner = BenchmarkRunner(
            loader=_Loader(), compose=_factory_with(tmp_path, llm=llm), timeout=10
        )
        summary = await runner.run(task_ids=["t-1"])

        (outcome,) = summary.outcomes
        assert outcome.agent_done == "error"
        assert outcome.model_unavailable is None
        assert outcome.ok and summary.failed == 0

    async def test_an_incomplete_trace_is_an_infrastructure_failure(self, tmp_path, monkeypatch):
        def refuse(self, result):
            raise OSError("disk full")

        monkeypatch.setattr(WALWriter, "write_result", refuse)
        runner = BenchmarkRunner(
            loader=_Loader(), compose=_make_factory(tmp_path, [], []), timeout=10
        )
        summary = await runner.run(task_ids=["t-1"])

        (outcome,) = summary.outcomes
        assert outcome.agent_done == "done"  # the agent's ledger is unharmed
        assert outcome.trace_status == "incomplete"
        assert outcome.trace_note == "OSError: disk full"
        assert outcome.infrastructure_error == ("the trace is incomplete: OSError: disk full")
        assert summary.count_by_trace() == {"incomplete": 1}
        assert summary.failed == 1

    def test_the_cli_exits_3_on_a_veto(self, tmp_path, monkeypatch, capsys):
        from spectragent.core.benchmark_runner import main
        from tests._helpers.branch_sources import write_source

        monkeypatch.setattr(BenchmarkRunner, "preflight", lambda self, task_id: None)

        async def vetoed(self, task_id, **kw):
            return TaskOutcome(task_id=task_id, vetoed="RunAbort: guard: risk-control page")

        monkeypatch.setattr(BenchmarkRunner, "run_task", vetoed)
        write_source(tmp_path / "recorded", "run-a", steps=2)
        write_source(tmp_path / "recorded", "run-b", steps=2)

        code = main(
            [
                "--session-dir",
                str(tmp_path / ".sessions"),
                "--config",
                str(_branch_sweep_config(tmp_path)),
                "--no-eval",
            ]
        )

        assert code == VETO_EXIT_CODE == 3
        captured = capsys.readouterr()
        assert "vetoed: a guard stopped the sweep at" in captured.out
        assert "spectragent bench: the sweep was vetoed at" in captured.err


# ── The task context's three parts (M31) ──────────────────────────────


class _CheckpointedLoop(ReactLoop):
    """A ReAct loop that declares one loop input and reports what it was handed."""

    __registry_key__ = "test_bench_runner_checkpointed"
    __loop_inputs__ = {"checkpoint": LoopInput(Checkpoint)}

    async def _execute(self, agent, task, *, ctx, **kwargs):
        _LOOP_INPUTS_SEEN.append(dict(ctx["loop_inputs"]))
        return await super()._execute(agent, task, ctx=ctx, **kwargs)


_LOOP_INPUTS_SEEN: list[dict[str, Any]] = []


def teardown_module():
    # Idempotent: pytest tears a module down once per contiguous run of its
    # tests, so a selection that interleaves files tears this one down twice.
    if AgentLoopRegistrar.get_or_none("test_bench_runner_checkpointed") is not None:
        AgentLoopRegistrar.unregister("test_bench_runner_checkpointed")


class _BranchLoader(_Loader):
    """Hands every task all three parts, as the ``trace_branches`` loader does."""

    def __init__(self, source: str):
        super().__init__()
        self._source = source

    def get_task_context(self, task_id):
        return TaskContext(
            env={"start_url": f"https://example.com/{task_id}"},
            loop={"checkpoint": Checkpoint(trace_dir=self._source, step=1)},
            branch=BranchProvenance(
                source_run_id="src",
                source_trace="src",
                checkpoint_step=1,
                branch_index=int(task_id.rsplit("-", 1)[-1]),
                branch_group_id="src/s1",
            ),
        )


class TestTaskContextRouting:
    async def test_three_parts_land_in_three_places(self, tmp_path):
        _LOOP_INPUTS_SEEN.clear()
        source = str(tmp_path / "source-run")
        envs: list[_Env] = []
        writers: list[TraceWriter] = []

        def compose():
            env = make_env(_Env, {"bench_runner_cap": _Cap()})
            writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
            envs.append(env)
            writers.append(writer)
            return make_run(
                agent=_Agent(),
                loop=_CheckpointedLoop(max_steps=2),
                env=env,
                llm=_LLM(),
                memory=_Memory(),
                hooks=[writer],
            )

        summary = await BenchmarkRunner(
            loader=_BranchLoader(source), compose=compose, timeout=10
        ).run()
        assert summary.completed == 2

        # env -> the capabilities' setup, and nothing else rides along.
        assert envs[0].setup_kwargs["context"] == {"start_url": "https://example.com/t-1"}
        # loop -> the Loop's ctx["loop_inputs"].
        assert [seen["checkpoint"].trace_dir for seen in _LOOP_INPUTS_SEEN] == [source, source]
        # branch -> the trace, and only the trace.
        for outcome, index in zip(summary.outcomes, (1, 2), strict=True):
            assert outcome.trace_dir is not None
            config = RunTrace(outcome.trace_dir).config
            assert config.branch is not None
            assert config.branch.branch_index == index
            assert config.task_setup is not None
            assert "checkpoint" not in config.task_setup.context

    async def test_a_plain_task_hands_the_loop_nothing(self, tmp_path):
        envs: list[_Env] = []
        writers: list[TraceWriter] = []
        summary = await BenchmarkRunner(
            loader=_Loader(), compose=_make_factory(tmp_path, envs, writers), timeout=10
        ).run()
        assert summary.completed == 2
        for outcome in summary.outcomes:
            assert outcome.trace_dir is not None
            assert RunTrace(outcome.trace_dir).config.branch is None


# ── A trace_branches sweep (M31) ──────────────────────────────────────


def _branch_sweep_config(tmp_path, **extra: Any) -> Any:
    """A ``spectragent bench`` config whose tasks come from ``trace_branches``."""
    import yaml

    config = {
        "task_loader": [
            {
                "benchmark_name": "trace_branches",
                "data_dir": str(tmp_path / "recorded"),
                "step_selector": {"selector_id": "explicit", "steps": [1]},
            }
        ],
        **extra,
    }
    path = tmp_path / "branch.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


class TestTraceBranchesSweep:
    """No eval loader exists for branch tasks in v1; the CLI says so before running anything."""

    def test_an_evaluator_with_no_eval_loader_is_refused_by_name(self, tmp_path, monkeypatch):
        from spectragent.core.benchmark_runner import main
        from tests._helpers.branch_sources import write_source

        write_source(tmp_path / "recorded", "run-a", steps=2)
        path = _branch_sweep_config(tmp_path, evaluator=[{"evaluator_id": "exact_match"}])
        executed: list[str] = []
        monkeypatch.setattr(
            BenchmarkRunner, "run_task", lambda self, task_id: executed.append(task_id)
        )
        try:
            main(["--session-dir", str(tmp_path / ".sessions"), "--config", str(path)])
        except SystemExit as exc:
            message = str(exc.code)
        else:  # pragma: no cover — the assertion below explains
            message = ""
        assert "'trace_branches' has no registered eval loader" in message
        assert "--no-eval" in message and executed == []

    def test_a_sweep_without_an_evaluator_never_asks_for_one(self, tmp_path, monkeypatch, capsys):
        from spectragent.core import benchmark_runner
        from spectragent.core.benchmark_runner import main
        from tests._helpers.branch_sources import write_source

        def no_lookup(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a sweep that scores nothing resolved an eval loader")

        # The configuration catalog may read eval-loader classes as schema
        # metadata; scoring nothing must skip actual reference resolution.
        monkeypatch.setattr(benchmark_runner, "_resolve_eval_loader", no_lookup)
        write_source(tmp_path / "recorded", "run-a", steps=2)

        assert (
            main(
                [
                    "--session-dir",
                    str(tmp_path / ".sessions"),
                    "--config",
                    str(_branch_sweep_config(tmp_path)),
                    "--list",
                ]
            )
            == 0
        )
        assert capsys.readouterr().out.split() == ["run-a/s1/b0"]
        # --no-eval keeps a configured evaluator from asking, too.
        with_judge = _branch_sweep_config(tmp_path, evaluator=[{"evaluator_id": "exact_match"}])
        assert (
            main(
                [
                    "--session-dir",
                    str(tmp_path / ".sessions"),
                    "--config",
                    str(with_judge),
                    "--list",
                    "--no-eval",
                ]
            )
            == 0
        )

    def test_the_registry_answers_for_every_other_benchmark(self, monkeypatch):
        from spectragent.core.benchmark_runner import _resolve_eval_loader
        from spectragent.core.data_loader.eval._registrar import EvalLoaderRegistrar

        class _NoBlock:
            def optional_single(self, layer):
                return None

        class _Reference:
            def __init__(self, subset: str = "All") -> None:
                self.subset = subset

        with pytest.raises(SystemExit, match="'no_such_bench' has no registered eval loader"):
            _resolve_eval_loader(_NoBlock(), "no_such_bench", None)  # type: ignore[arg-type]
        monkeypatch.setattr(EvalLoaderRegistrar, "get_or_none", lambda key: _Reference)
        built = _resolve_eval_loader(_NoBlock(), "online_mind2web", "Hard")  # type: ignore[arg-type]
        assert isinstance(built, _Reference) and built.subset == "Hard"

    async def test_each_branch_task_runs_from_its_context(self, tmp_path):
        """Loader → runner → runtime → BranchLoop: the env part reaches setup, the
        checkpoint reaches the Loop, the provenance reaches the trace."""
        from spectragent.core.data_loader.task import TraceBranchesTaskLoader
        from spectragent.core.data_loader.task.step_selector import ExplicitSteps
        from spectragent.core.loops import BranchLoop
        from tests._helpers.branch_fakes import BranchFakeAgent, build_run, make_cap, record_source

        source = await record_source(
            tmp_path, ["click 100", "click 101", "done ok"], env_context={}
        )
        setup = RunTrace(source).config.task_setup
        assert setup is not None and setup.context == {}
        loader = TraceBranchesTaskLoader(data_dir=str(source.parent), require_archive=())
        loader.step_selector = ExplicitSteps(steps=[1])
        caps: list[Any] = []

        def compose():
            cap = make_cap(id_offset=500)  # a relaunched world: every element id is new
            caps.append(cap)
            run, _writer, _llm = build_run(
                tmp_path / "branches",
                loop=BranchLoop(max_steps=10, today_override="2001-02-03"),
                replies=["done branched"],
                agent=BranchFakeAgent(),
                cap=cap,
            )
            return run

        summary = await BenchmarkRunner(loader=loader, compose=compose, timeout=30).run()
        assert summary.completed == 1 and summary.failed == 0, summary.format_table()
        (outcome,) = summary.outcomes
        assert outcome.task_id == "src/s1/b0"
        assert caps[0].replay_archive == str(source), "the env part reached setup"
        assert caps[0].log == [
            "branchfake.click({'index': 500})",
            "branchfake.done({'answer': 'branched'})",
        ]

        assert outcome.trace_dir is not None
        trace = RunTrace(outcome.trace_dir)
        assert trace.config.branch == loader.get_task_context("src/s1/b0").branch
        assert trace.config.benchmark == "trace_branches"
        assert trace.config.task_id == "src/s1/b0"
        assert [record.origin for record in trace.step_records] == [
            "restored",
            "policy",
            "terminal",
        ]
        assert all(
            record.restoration is not None and record.restoration.verdict == "ok"
            for record in trace.step_records[:2]
        )


# ── The token limit ──────────────────────────────────────────────────


class _ThreeTaskLoader(_Loader):
    def __init__(self, broken: set[str] | None = None):
        super().__init__(broken)
        self._tasks["t-3"] = _Task("t-3", "check out")


class _Ticking(_Agent):
    """Never calls done: every step is one more model call, until ``max_steps``."""

    def parse_react_output(self, out):
        return []


class _NoUsage:
    completion = "done"
    usage = None


class _SilentLLM:
    """A provider that reports no usage at all."""

    inference_kind = "chat"

    model = "test-model"

    async def ainvoke(self, messages, **kw):
        return _NoUsage()


def _limited_factory(
    tmp_path, *, agent_cls: Any = _Agent, llm_cls: Any = _LLM, max_steps=2, trace=True
):
    """One stack per task; each ``_LLM`` call reports 10 tokens. Returns the factory and its writers."""
    writers: list[TraceWriter] = []

    def compose():
        writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
        writers.append(writer)
        return make_run(
            agent=agent_cls(),
            loop=ReactLoop(max_steps=max_steps),
            env=make_env(_Env, {"bench_runner_cap": _Cap()}),
            llm=llm_cls(),
            memory=_Memory(),
            hooks=[writer] if trace else [],
        )

    return compose, writers


class TestTheTokenLimit:
    """``--token-limit``: count each finished task's recorded tokens, stop once the total reaches it.

    The user's ruling of 2026-10-14 (arch §4.2 "sweep 的 token 上限"): the
    task that reaches the limit finishes, no task after it starts, and the
    sweep has its own exit code; ``unlimited`` changes nothing.
    """

    async def test_the_sweep_stops_after_the_task_that_reaches_it(self, tmp_path):
        compose, writers = _limited_factory(tmp_path)
        runner = BenchmarkRunner(
            loader=_ThreeTaskLoader(), compose=compose, timeout=10, token_limit=15
        )
        summary = await runner.run()

        # One model call of 10 tokens a task: 10 < 15 after t-1, 20 >= 15 after t-2.
        assert [outcome.task_id for outcome in summary.outcomes] == ["t-1", "t-2"]
        assert [o.usage.tokens for o in summary.outcomes if o.usage is not None] == [10, 10]
        assert summary.token_limit_reached == TokenLimitReached(
            task_id="t-2", tokens=20, limit=15, not_started=1
        )
        assert len(writers) == 2, "t-3 never started: no stack was composed for it"
        assert all(outcome.ok for outcome in summary.outcomes)
        lines = summary.format_table().splitlines()
        assert lines[0].endswith(" token_limit_reached=1")
        assert "  tokens: 20 recorded by 2 call(s); limit 15" in lines
        assert any(
            line.startswith("  t-2: done (") and line.endswith(", 10 tokens)") for line in lines
        )
        assert lines[-1] == (
            "token limit: the sweep's runs recorded 20 tokens, reaching its limit of 15 after "
            "t-2; 1 task(s) not started"
        )

    async def test_the_task_that_crosses_it_runs_to_its_end(self, tmp_path):
        """The limit is checked between tasks: the task already running is never cut off."""
        compose, _writers = _limited_factory(tmp_path, agent_cls=_Ticking, max_steps=3)
        runner = BenchmarkRunner(
            loader=_ThreeTaskLoader(), compose=compose, timeout=10, token_limit=5
        )
        summary = await runner.run()

        (outcome,) = summary.outcomes
        # Its first call already passed the limit; its other two steps ran all the same.
        assert (outcome.agent_done, outcome.trace_status) == ("max_steps", "complete")
        assert outcome.usage is not None and (outcome.usage.calls, outcome.usage.tokens) == (3, 30)
        assert outcome.trace_dir is not None
        policy = [r.step for r in RunTrace(outcome.trace_dir).step_records if r.origin == "policy"]
        assert policy == [0, 1, 2]
        assert summary.token_limit_reached == TokenLimitReached(
            task_id="t-1", tokens=30, limit=5, not_started=2
        )

    async def test_a_limit_never_reached_lets_every_task_run(self, tmp_path):
        compose, _writers = _limited_factory(tmp_path)
        summary = await BenchmarkRunner(
            loader=_ThreeTaskLoader(), compose=compose, timeout=10, token_limit=31
        ).run()
        assert summary.total == 3 and summary.token_limit_reached is None
        assert (summary.tokens, summary.calls) == (30, 3)
        assert "  tokens: 30 recorded by 3 call(s); limit 31" in summary.format_table()

    async def test_unlimited_reads_no_usage_and_prints_what_it_always_did(
        self, tmp_path, monkeypatch
    ):
        def never(trace):
            raise AssertionError("a sweep without a token limit read the usage")

        monkeypatch.setattr("spectragent.core.benchmark_runner.recorded_usage", never)
        default, _ = _limited_factory(tmp_path / "default")
        unlimited, _ = _limited_factory(tmp_path / "unlimited")
        runners = [
            BenchmarkRunner(loader=_ThreeTaskLoader(), compose=default, timeout=10),
            BenchmarkRunner(
                loader=_ThreeTaskLoader(), compose=unlimited, timeout=10, token_limit="unlimited"
            ),
        ]
        for runner in runners:
            summary = await runner.run()
            assert runner.token_limit is None and summary.token_limit is None
            assert summary.total == 3 and summary.token_limit_reached is None
            assert all(o.usage is None and o.usage_note is None for o in summary.outcomes)
            table = summary.format_table()
            assert "token" not in table, table
            assert "tasks=3 completed=3 infrastructure_failures=0" in table.splitlines()[0]

    async def test_calls_without_usage_are_counted_and_named(self, tmp_path):
        compose, _writers = _limited_factory(tmp_path, llm_cls=_SilentLLM)
        summary = await BenchmarkRunner(
            loader=_Loader(), compose=compose, timeout=10, token_limit=15
        ).run()

        assert summary.total == 2 and summary.token_limit_reached is None
        assert (summary.tokens, summary.calls) == (0, 2)
        assert summary.calls_without_usage == [
            "t-1: step 0 call 0 (completed)",
            "t-2: step 0 call 0 (completed)",
        ]
        table = summary.format_table()
        assert "  no usage reported (not counted): t-1: step 0 call 0 (completed)" in table
        assert "  tokens: 0 recorded by 2 call(s); limit 15" in table

    async def test_a_trace_whose_calls_do_not_read_back_is_named_not_zero(
        self, tmp_path, monkeypatch
    ):
        from spectragent.core.trace.errors import TraceSchemaError
        from spectragent.core.trace.usage import recorded_usage

        read: list[str] = []

        def first_unreadable(trace):
            read.append(trace.run_id)
            if len(read) == 1:
                raise TraceSchemaError("messages/000_call_00.json does not parse")
            return recorded_usage(trace)

        monkeypatch.setattr("spectragent.core.benchmark_runner.recorded_usage", first_unreadable)
        compose, _writers = _limited_factory(tmp_path)
        summary = await BenchmarkRunner(
            loader=_Loader(), compose=compose, timeout=10, token_limit=15
        ).run()

        first, second = summary.outcomes
        assert first.usage is None
        assert first.usage_note == (
            "a recorded model call does not read back: messages/000_call_00.json does not parse"
        )
        assert second.usage is not None and summary.tokens == 10
        assert summary.token_limit_reached is None, "the sweep went on past the uncounted task"
        assert "  tokens not counted: t-1: a recorded model call does not read back" in (
            summary.format_table()
        )

    async def test_a_veto_on_the_same_task_is_a_veto(self, tmp_path):
        envs: list[_Env] = []
        writers: list[TraceWriter] = []
        runner = BenchmarkRunner(
            loader=_Loader(),
            compose=_vetoing_factory(tmp_path, envs, writers, veto_on_task=0),
            timeout=10,
            token_limit=1,
        )
        summary = await runner.run()
        assert summary.was_aborted and summary.token_limit_reached is None

    async def test_the_pre_flight_refuses_a_limit_with_nothing_to_count(self, tmp_path):
        compose, _writers = _limited_factory(tmp_path, trace=False)
        runner = BenchmarkRunner(loader=_Loader(), compose=compose, timeout=10, token_limit=15)
        with pytest.raises(PreflightError, match="this configuration writes no trace"):
            await runner.run()
        # Without a limit the same configuration runs: nothing needs counting.
        compose, _writers = _limited_factory(tmp_path, trace=False)
        summary = await BenchmarkRunner(loader=_Loader(), compose=compose, timeout=10).run()
        assert summary.completed == 2

    async def test_the_preflight_requires_selected_metrics_for_a_token_limit(self, tmp_path):
        factory, writers = _limited_factory(tmp_path)

        def compose():
            run = factory()
            run.hooks[0].recording = RecordingPlan(metrics=False)
            return run

        runner = BenchmarkRunner(loader=_Loader(), compose=compose, token_limit=15)
        with pytest.raises(PreflightError, match="recording.metrics enabled"):
            await runner.run()
        assert writers[0].run_dir is None

    async def test_a_token_limit_counts_light_recording_without_model_trajectory(self, tmp_path):
        factory, _writers = _limited_factory(tmp_path)

        def compose():
            run = factory()
            run.hooks[0].recording = RecordingPlan(trajectory=False, observations=False)
            return run

        summary = await BenchmarkRunner(
            loader=_ThreeTaskLoader(), compose=compose, token_limit=15
        ).run()
        assert summary.tokens == 20 and summary.calls == 2
        assert summary.token_limit_reached is not None
        assert all(outcome.trace_status == "complete" for outcome in summary.outcomes)

    @pytest.mark.parametrize("limit", [0, -3, True, 2.5, "lots", None])
    def test_a_limit_is_a_number_of_at_least_one_or_unlimited(self, limit):
        with pytest.raises(ValueError, match="at least 1 or 'unlimited'"):
            BenchmarkRunner(loader=_Loader(), compose=lambda: None, token_limit=limit)  # type: ignore[arg-type]

    def test_the_cli_exits_4_and_says_why(self, tmp_path, monkeypatch, capsys):
        from spectragent.core.benchmark_runner import main
        from spectragent.core.trace.usage import RunUsage
        from tests._helpers.branch_sources import write_source

        monkeypatch.setattr(BenchmarkRunner, "preflight", lambda self, task_id: None)
        started: list[str] = []

        async def ten_tokens(self, task_id, **kw):
            started.append(task_id)
            return TaskOutcome(task_id=task_id, usage=RunUsage(tokens=10, calls=1))

        monkeypatch.setattr(BenchmarkRunner, "run_task", ten_tokens)
        for name in ("run-a", "run-b", "run-c"):
            write_source(tmp_path / "recorded", name, steps=2)
        config = str(_branch_sweep_config(tmp_path))

        code = main(
            [
                "--session-dir",
                str(tmp_path / ".sessions"),
                "--config",
                config,
                "--no-eval",
                "--token-limit",
                "15",
            ]
        )

        assert code == TOKEN_LIMIT_EXIT_CODE == 4
        assert started == ["run-a/s1/b0", "run-b/s1/b0"]
        captured = capsys.readouterr()
        assert "token limit: the sweep's runs recorded 20 tokens" in captured.out
        assert (
            "spectragent bench: the sweep reached its token limit after task run-b/s1/b0 "
            "(20 of 15 tokens); 1 task(s) not started"
        ) in captured.err

    @pytest.mark.parametrize("value", ["0", "lots", "-1"])
    def test_the_cli_refuses_a_limit_that_is_not_one(self, tmp_path, value, capsys):
        from spectragent.core.benchmark_runner import main

        with pytest.raises(SystemExit) as raised:
            main(
                [
                    "--session-dir",
                    str(tmp_path / ".sessions"),
                    "--config",
                    str(tmp_path / "x.yaml"),
                    "--token-limit",
                    value,
                ]
            )
        assert raised.value.code == 2
        assert "--token-limit" in capsys.readouterr().err

    def test_the_cli_default_is_unlimited(self, tmp_path, monkeypatch):
        from spectragent.core.benchmark_runner import main
        from tests._helpers.branch_sources import write_source

        limits: list[int | None] = []

        async def record_limit(self, *, task_ids=None, limit=None):
            limits.append(self.token_limit)
            return BenchmarkRunSummary(benchmark="b", subset="")

        monkeypatch.setattr(BenchmarkRunner, "run", record_limit)
        write_source(tmp_path / "recorded", "run-a", steps=2)
        config = str(_branch_sweep_config(tmp_path))
        assert (
            main(["--session-dir", str(tmp_path / ".sessions"), "--config", config, "--no-eval"])
            == 0
        )
        assert (
            main(
                [
                    "--session-dir",
                    str(tmp_path / ".sessions"),
                    "--config",
                    config,
                    "--no-eval",
                    "--token-limit",
                    "unlimited",
                ]
            )
            == 0
        )
        assert (
            main(
                [
                    "--session-dir",
                    str(tmp_path / ".sessions"),
                    "--config",
                    config,
                    "--no-eval",
                    "--token-limit",
                    "7",
                ]
            )
            == 0
        )
        assert limits == [None, None, 7]
