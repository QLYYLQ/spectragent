"""The benchmark acceptance path, run offline.

    spectragent bench --benchmark ... --config ...
    -> one traces/ directory per task
    -> each offline verifier's verdict in each run's verdicts/
    -> each verifier's counts in the printed summary

Everything here is real except the browser and the model: a real
``BenchmarkRunner``, a real ``AgentRuntime``, a real ``ReactLoop``, a real
``TraceWriter`` writing a real WAL, and a real ``VerificationRunner``
reading it back. No network, no Chrome, no API key.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from spectragent.benchmarks import BenchmarkBase
from spectragent.core.benchmark_runner import BenchmarkRunner
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import ReactLoop
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.task import TaskContext
from spectragent.schemas.verdict import Verdict, VerifierIdentity
from spectragent.verifiers.exact_match import ExactMatchVerifier
from tests._helpers.assembly import make_env, make_run

_ANSWERS = {"t-1": "42", "t-2": "sixty"}


def _read_verdict(trace_dir) -> Verdict:
    """The one verdict in a run's store."""
    verdicts = RunTrace(trace_dir).verdicts()
    assert len(verdicts) == 1, f"expected one verdict, found {sorted(verdicts)}"
    return next(iter(verdicts.values()))


class _Cap(CapabilityBase):
    __abstract__ = True  # Directly assembled fake; never a YAML implementation.
    __registry_key__ = "eval_e2e_cap"
    __wiring__ = {"action": ("action", ["eval_e2e_cap.done"])}

    def get_action_descriptors(self, **ctx):
        return [ActionDescriptor(name="eval_e2e_cap.done", description="Done")]

    async def execute_action(self, action):
        return ActionResult(
            success=True, is_done=True, extracted_content=action.params.get("text", "")
        )


class _Env(EnvironmentBase):
    __wiring__ = {"capabilities": "capability"}
    __registry_key__ = "eval_e2e_env"


class _Agent:
    """Answers each task with whatever the fixture scripted for it."""

    __wiring__ = {
        "action": ("action", ["eval_e2e_cap.done"]),
        # Required so negotiation activates the fragment the Loop uses to
        # hand the task text to the agent.
        "observation": ("observation", ["loop.task"]),
    }

    def __init__(self) -> None:
        self.answer = ""

    __execution_limit_fields__ = frozenset()

    async def build_react_prompt(self, obs, *, context):
        # The Loop injects the task through the ``loop.task`` fragment;
        # the last word of the fixture's task text is the task id.
        task = getattr(getattr(obs, "task", None), "user_request", "") or ""
        self.answer = _ANSWERS.get(task.rsplit(" ", 1)[-1], "")
        return [{"role": "user", "content": task}]

    def parse_react_output(self, out):
        return [Action(name="eval_e2e_cap.done", params={"text": self.answer})]

    def get_react_output_model(self):
        return None


class _Usage:
    prompt_tokens = 10
    completion_tokens = 4
    prompt_cached_tokens = 0
    total_tokens = 14


class _Completion:
    completion = "answering"
    usage = _Usage()
    thinking = None
    tool_calls: list[Any] = []


class _LLM:
    inference_kind = "chat"
    model = "offline-model"
    name = "offline-model"

    async def aclose(self, *, timeout=5.0):
        return ()

    async def ainvoke(self, messages, output_format=None, **kw):
        return _Completion()


class _Memory:
    supports_protocols = frozenset({"user_assistant"})

    def __init__(self) -> None:
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
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.task_text = f"produce the answer for {task_id}"
        self.env_type = "browser"
        self.difficulty = "easy"
        self.domain = "qa"
        self.tags: list[str] = []


class _TaskLoader:
    benchmark_name = "offline_e2e"
    subset = "test"

    def task_ids(self):
        return list(_ANSWERS)

    def __getitem__(self, task_id):
        return _Task(task_id)

    def get_task_context(self, task_id):
        return TaskContext()


class _EvalLoader:
    benchmark_name = "offline_e2e"
    subset = "test"
    #: t-1's expected answer matches what the agent produces; t-2's does not.
    _refs = {
        "t-1": EvalReference(task_id="t-1", benchmark="offline_e2e", expected_answer="42"),
        "t-2": EvalReference(task_id="t-2", benchmark="offline_e2e", expected_answer="ninety"),
    }

    def task_ids(self):
        return list(self._refs)

    def get(self, task_id):
        return self._refs.get(task_id)

    def __getitem__(self, task_id):
        return self._refs[task_id]


@pytest.fixture
def runner_factory(tmp_path):
    def build(*, hooks: tuple[Any, ...] = (), **kwargs):
        def compose():
            writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
            return make_run(
                agent=_Agent(),
                loop=ReactLoop(max_steps=2),
                env=make_env(_Env, {"eval_e2e_cap": _Cap()}),
                llm=_LLM(),
                memory=_Memory(),
                hooks=[writer, *hooks],
            )

        return BenchmarkRunner(loader=_TaskLoader(), compose=compose, timeout=10, **kwargs)

    return build


async def test_a_sweep_without_a_verifier_still_works(runner_factory, tmp_path):
    """Judging is optional; a benchmark whose block lists no verifier only records traces."""
    summary = await runner_factory().run()

    assert summary.total == 2
    assert summary.verification == []
    assert "verifiers:" not in summary.format_table()
    assert all(not (path / "verdicts").exists() for path in (tmp_path / "traces").iterdir())


async def test_acceptance_traces_verdicts_and_a_printed_summary(runner_factory, tmp_path):
    verifier = ExactMatchVerifier()
    runner = runner_factory(verifiers=[verifier], reference_loader=_EvalLoader())

    summary = await runner.run()

    # 1. One traces/ directory per task.
    trace_dirs = sorted((tmp_path / "traces").iterdir())
    assert len(trace_dirs) == 2
    assert {o.trace_dir for o in summary.outcomes} == set(trace_dirs)

    # 2. A verdict in each run's store, and nowhere else.
    slug = verifier.identity().slug
    for trace_dir in trace_dirs:
        assert (trace_dir / "verdicts" / f"{slug}.json").is_file()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["traces"]

    # 3. The verifier's counts in the printed summary.
    [tally] = summary.verification
    assert (tally.provided, tally.judged, tally.passed, tally.failed) == (2, 2, 1, 1)
    assert tally.success_rate == 0.5

    table = summary.format_table()
    assert "verifiers:" in table
    assert f"{slug}: judged 2 of 2" in table
    assert "success rate 50.0%" in table
    assert summary.as_dict()["verification"][0]["passed"] == 1


async def test_the_verdict_says_who_judged(runner_factory, tmp_path):
    await runner_factory(verifiers=[ExactMatchVerifier()], reference_loader=_EvalLoader()).run()

    verdicts = {}
    for trace_dir in sorted((tmp_path / "traces").iterdir()):
        verdicts[RunTrace(trace_dir).config.task_id] = _read_verdict(trace_dir)

    assert set(verdicts) == {"t-1", "t-2"}
    assert verdicts["t-1"].label == 1
    assert verdicts["t-2"].label == 0
    assert verdicts["t-1"].verifier == ExactMatchVerifier().identity()
    assert verdicts["t-1"].attachment == "offline"


async def test_the_agents_self_report_is_not_the_verdict(runner_factory, tmp_path):
    """``agent_reported_success`` and a verdict's label are different facts."""
    await runner_factory(verifiers=[ExactMatchVerifier()], reference_loader=_EvalLoader()).run()

    for trace_dir in sorted((tmp_path / "traces").iterdir()):
        result = json.loads((trace_dir / "result.json").read_text("utf-8"))
        verdict = _read_verdict(trace_dir)
        if result["task_id"] == "t-2":
            # The agent finished cleanly; the answer is still wrong.
            assert result["agent_done"] == "done"
            assert verdict.label == 0


async def test_a_failing_verifier_does_not_lose_the_sweep(runner_factory):
    """A broken verifier must not destroy a sweep's worth of compute.

    The stub is written to the Protocol only, which also pins that the
    runner drives the ``OfflineVerifier`` Protocol and never reaches for a
    concrete base class.
    """

    class _Exploding:
        verifier_id = "exploding"
        needs = frozenset({"trace"})

        def identity(self):
            return VerifierIdentity(verifier_id=self.verifier_id)

        async def verify(self, trace, reference):
            raise RuntimeError("judge exploded")

    summary = await runner_factory(verifiers=[_Exploding()]).run()

    assert summary.total == 2
    assert summary.completed == 2  # the runs themselves survived
    assert len(summary.verification_errors) == 2
    assert "judge exploded" in summary.verification_errors[0]


def test_a_verifier_without_reference_data_is_rejected_at_construction():
    with pytest.raises(ValueError, match="no reference data"):
        BenchmarkRunner(
            loader=_TaskLoader(),
            compose=lambda: None,  # type: ignore[arg-type, return-value]
            verifiers=[ExactMatchVerifier()],
        )


class _ExactMatchBenchmark(BenchmarkBase):
    """A benchmark judged by exact matching, its primary verdict."""

    __abstract__ = True  # Directly built test benchmark; never a YAML implementation.
    __registry_key__ = "offline_e2e_benchmark"
    __primary_verifier__ = "exact_match"


async def test_the_summary_reports_the_primary_verdict_and_the_success_rate(
    runner_factory, tmp_path
):
    """After the three ledgers: each task's primary verdict and the success rate over them."""
    verifier = ExactMatchVerifier()
    benchmark = _ExactMatchBenchmark()
    benchmark.verifiers = [verifier]
    runner = runner_factory(
        verifiers=[verifier], reference_loader=_EvalLoader(), primary_rule=benchmark
    )

    summary = await runner.run()

    assert summary.primary_verifier == "exact_match"
    assert [(o.task_id, o.success) for o in summary.outcomes] == [("t-1", 1.0), ("t-2", 0.0)]
    assert all(
        o.primary is not None and o.primary.slug == verifier.identity().slug
        for o in summary.outcomes
    )
    assert (summary.judged, summary.success_rate) == (2, 0.5)
    table = summary.format_table()
    lines = table.splitlines()
    ledger = next(i for i, line in enumerate(lines) if line.startswith("  trace:"))
    assert lines[ledger + 1] == (
        "  primary verdict (exact_match): success rate 50.0% over the 2 of 2 task(s) it judged"
    )
    assert "primary label 1, success 1" in table and "primary label 0, success 0" in table
    assert summary.as_dict()["primary"] == {
        "verifier_id": "exact_match",
        "judged": 2,
        "success_rate": 0.5,
    }


async def test_a_task_its_primary_verifier_could_not_judge_is_left_out_of_the_rate(
    runner_factory,
):
    class _Partial(_EvalLoader):
        _refs = {"t-1": _EvalLoader._refs["t-1"]}

    verifier = ExactMatchVerifier()
    benchmark = _ExactMatchBenchmark()
    benchmark.verifiers = [verifier]
    runner = runner_factory(
        verifiers=[verifier], reference_loader=_Partial(), primary_rule=benchmark
    )

    summary = await runner.run()

    assert [(o.task_id, o.success) for o in summary.outcomes] == [("t-1", 1.0), ("t-2", None)]
    assert (summary.judged, summary.success_rate) == (1, 1.0)
    assert "over the 1 of 2 task(s) it judged" in summary.format_table()
    assert "no primary verdict" in summary.format_table()


async def test_a_run_whose_close_out_raised_is_counted_by_its_recorded_ending(runner_factory):
    """``execute`` raised after the trace writer closed the run: the ending is in result.json.

    Without it, a benchmark whose success depends on how the run ended
    (AndroidWorld's "declared done") would count a done run as failed.
    """
    from spectragent.core.hooks._registrar import HookBase

    class FailsAfterTheRecord(HookBase):
        priority = HookBase.PRIORITY_PERSIST + 50

        async def on_run_end(self, **kwargs):
            raise RuntimeError("a hook failed after the close-out")

    class CountsTheEnding(_ExactMatchBenchmark):
        __abstract__ = True
        __registry_key__ = "offline_e2e_ending"

        def success(self, verdict, result):
            if result is None:
                return None
            return float(verdict.label or 0) if result.agent_done == "done" else 0.0

    verifier = ExactMatchVerifier()
    benchmark = CountsTheEnding()
    benchmark.verifiers = [verifier]
    runner = runner_factory(
        hooks=(FailsAfterTheRecord(),),
        verifiers=[verifier],
        reference_loader=_EvalLoader(),
        primary_rule=benchmark,
    )

    summary = await runner.run()

    assert all(o.result is None and o.error for o in summary.outcomes)
    assert [(o.task_id, o.success) for o in summary.outcomes] == [("t-1", 1.0), ("t-2", 0.0)]
