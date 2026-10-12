"""BenchmarkRunner — per-task lifecycle, identity, and isolation (F5)."""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.core.benchmark_runner import (
    PREFLIGHT_EXIT_CODE,
    BenchmarkRunner,
    PreflightError,
    _safe_run_id,
)
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import ReactLoop
from spectragent.core.loops._registrar import AgentLoopRegistrar, LoopInput
from spectragent.core.loops.errors import RunAbort
from spectragent.core.negotiation import IncompatibleActionError
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.restoration import BranchProvenance, Checkpoint
from spectragent.schemas.task import TaskContext
from tests._helpers.assembly import make_env, make_run


class _Cap(CapabilityBase):
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
    __wiring__ = {"action": ("action", ["bench_runner_cap.done"])}

    def build_react_prompt(self, obs):
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
    model = "test-model"

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

        code = main(["--config", str(_branch_sweep_config(tmp_path)), "--no-eval"])

        assert code == PREFLIGHT_EXIT_CODE == 2
        assert executed == []
        err = capsys.readouterr().err
        assert "spectragent-bench: the pre-flight check refused the configuration" in err
        assert "replayability: pinned ↔ provider" in err


class _VetoingAgent(_Agent):
    """An agent whose guard inspects the model's reply and refuses to go on.

    The veto lives in ``parse_react_output`` because that is where a real one
    lives: the parser is an agent method, so it is woven, and it is the first
    place the agent sees what the model actually wrote.
    """

    def __init__(self, *, vetoes: bool) -> None:
        self._vetoes = vetoes

    def build_react_prompt(self, obs):
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
        assert "t-2" in summary.aborted
        assert "risk-control page detected" in summary.aborted
        # The guard said stop, not destroy the evidence: t-1 finished before
        # the veto and its outcome is still here to be scored.
        assert summary.total == 1
        assert summary.outcomes[0].task_id == "t-1"
        assert summary.outcomes[0].ok

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
        assert summary.total == 0  # vetoed on the very first task
        # A trace was still opened and closed out for the vetoed run.
        trace_dirs = sorted((tmp_path / "traces").iterdir())
        assert trace_dirs, "the vetoed run left no trace at all"
        trace = RunTrace(trace_dirs[0])
        assert trace.result is not None
        assert "RunAbort" in (getattr(trace.result, "error", "") or "")
        assert "risk-control page detected" in (getattr(trace.result, "error", "") or "")


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
    """A ``spectragent-bench`` config whose tasks come from ``trace_branches``."""
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
            main(["--config", str(path)])
        except SystemExit as exc:
            message = str(exc.code)
        else:  # pragma: no cover — the assertion below explains
            message = ""
        assert "'trace_branches' has no registered eval loader" in message
        assert "--no-eval" in message and executed == []

    def test_a_sweep_without_an_evaluator_never_asks_for_one(self, tmp_path, monkeypatch, capsys):
        from spectragent.core.benchmark_runner import main
        from spectragent.core.data_loader.eval._registrar import EvalLoaderRegistrar
        from tests._helpers.branch_sources import write_source

        def no_lookup(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a sweep that scores nothing resolved an eval loader")

        monkeypatch.setattr(EvalLoaderRegistrar, "get", no_lookup)
        monkeypatch.setattr(EvalLoaderRegistrar, "get_or_none", no_lookup)
        write_source(tmp_path / "recorded", "run-a", steps=2)

        assert main(["--config", str(_branch_sweep_config(tmp_path)), "--list"]) == 0
        assert capsys.readouterr().out.split() == ["run-a/s1/b0"]
        # --no-eval keeps a configured evaluator from asking, too.
        with_judge = _branch_sweep_config(tmp_path, evaluator=[{"evaluator_id": "exact_match"}])
        assert main(["--config", str(with_judge), "--list", "--no-eval"]) == 0

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
        loader = TraceBranchesTaskLoader(data_dir=str(source.parent), require_archive=[])
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
