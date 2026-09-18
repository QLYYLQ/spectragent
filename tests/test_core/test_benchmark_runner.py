"""BenchmarkRunner — per-task lifecycle, identity, and isolation (F5)."""

from __future__ import annotations

from typing import Any

from spectragent.core.benchmark_runner import BenchmarkRunner, _safe_run_id
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import ReactLoop
from spectragent.core.loops.errors import RunAbort
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
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
        return {"start_url": f"https://example.com/{task_id}"}


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
        assert envs[0].setup_kwargs["context"] == {
            "start_url": "https://example.com/t-1"
        }
        cap = envs[0].capabilities["bench_runner_cap"]
        assert isinstance(cap, _Cap)
        assert cap.start_url == "https://example.com/t-1"
        assert envs[1].setup_kwargs["context"] == {
            "start_url": "https://example.com/t-2"
        }

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

    async def test_an_environment_that_never_came_up_is_not_a_completed_task(
        self, tmp_path
    ):
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

        summary = await BenchmarkRunner(
            loader=_Loader(), compose=compose, timeout=10
        ).run()

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

    async def test_an_agent_that_ran_out_of_steps_is_still_a_completed_task(
        self, tmp_path
    ):
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

        summary = await BenchmarkRunner(
            loader=_Loader(), compose=compose, timeout=10
        ).run()

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
