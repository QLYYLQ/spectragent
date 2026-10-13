"""The benchmark acceptance path, run offline.

    spectragent-bench --benchmark ... --subset ... --config ...
    -> one traces/ directory per task
    -> an EvalResult under evals/
    -> a printed EvalSummary

Everything here is real except the browser and the model: a real
``BenchmarkRunner``, a real ``AgentRuntime``, a real ``ReactLoop``, a real
``TraceWriter`` writing a real WAL, and a real ``EvaluationRunner``
reading it back. No network, no Chrome, no API key.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from spectragent.core.benchmark_runner import BenchmarkRunner
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.hooks.trace_writer import TraceWriter
from spectragent.core.loops import ReactLoop
from spectragent.evaluator import ExactMatchEvaluator
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.task import TaskContext
from tests._helpers.assembly import make_env, make_run

_ANSWERS = {"t-1": "42", "t-2": "sixty"}


def _read_verdict(trace_dir) -> dict[str, Any]:
    """The one verdict under a trace's ``evals/``.

    Globbed rather than named: the filename carries the evaluator's config
    digest, so hardcoding ``exact_match.json`` here would re-freeze the
    very collision that digest exists to prevent.
    """
    written = sorted((trace_dir / "evals").glob("*.json"))
    assert len(written) == 1, f"expected one verdict, found {written}"
    return json.loads(written[0].read_text("utf-8"))


class _Cap(CapabilityBase):
    __registry_key__ = "eval_e2e_cap"
    __wiring__ = {"action": ("action", ["eval_e2e_cap.done"])}

    def get_action_descriptors(self, **ctx):
        return [ActionDescriptor(name="eval_e2e_cap.done", description="Done")]

    async def execute_action(self, action):
        return ActionResult(
            success=True, is_done=True, extracted_content=action.params.get("text", "")
        )


class _Env(EnvironmentBase):
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

    def build_react_prompt(self, obs):
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
    model = "offline-model"

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
    def build(**kwargs):
        def compose():
            writer = TraceWriter(trace_dir=str(tmp_path / "traces"))
            return make_run(
                agent=_Agent(),
                loop=ReactLoop(max_steps=2),
                env=make_env(_Env, {"eval_e2e_cap": _Cap()}),
                llm=_LLM(),
                memory=_Memory(),
                hooks=[writer],
            )

        return BenchmarkRunner(loader=_TaskLoader(), compose=compose, timeout=10, **kwargs)

    return build


async def test_a_sweep_without_an_evaluator_still_works(runner_factory):
    """Scoring is optional; a config that only records traces is valid."""
    summary = await runner_factory().run()

    assert summary.total == 2
    assert summary.eval_summary is None
    assert "evaluation:" not in summary.format_table()


async def test_acceptance_traces_evals_and_a_printed_summary(runner_factory, tmp_path):
    evals_dir = tmp_path / "evals"
    runner = runner_factory(
        evaluator=ExactMatchEvaluator(),
        eval_loader=_EvalLoader(),
        evals_dir=evals_dir,
    )

    summary = await runner.run()

    # 1. One traces/ directory per task.
    trace_dirs = sorted((tmp_path / "traces").iterdir())
    assert len(trace_dirs) == 2
    assert {o.trace_dir for o in summary.outcomes} == set(trace_dirs)

    # 2. An EvalResult under evals/ — both next to the trace and flat.
    slug = ExactMatchEvaluator().verdict_slug
    for trace_dir in trace_dirs:
        assert (trace_dir / "evals" / f"{slug}.json").is_file()
    flat = sorted(p.name for p in evals_dir.iterdir())
    assert f"{slug}_summary.json" in flat
    assert len([n for n in flat if n.endswith(f"__{slug}.json")]) == 2

    # 3. A printed EvalSummary, with real verdicts in it.
    assert summary.eval_summary is not None
    stats = summary.eval_summary.overall_statistics
    assert stats.total_tasks == 2
    assert stats.successful_tasks == 1  # t-1 matches, t-2 does not
    assert stats.failed_tasks == 1
    assert stats.success_rate == 50.0

    table = summary.format_table()
    assert "evaluation:" in table
    assert "success_rate=50.00%" in table


async def test_the_verdict_joins_back_to_the_dataset(runner_factory, tmp_path):
    runner = runner_factory(evaluator=ExactMatchEvaluator(), eval_loader=_EvalLoader())

    await runner.run()

    verdicts = {}
    for trace_dir in sorted((tmp_path / "traces").iterdir()):
        payload = _read_verdict(trace_dir)
        verdicts[payload["task_id"]] = payload

    assert set(verdicts) == {"t-1", "t-2"}
    assert verdicts["t-1"]["predicted_label"] == 1
    assert verdicts["t-2"]["predicted_label"] == 0
    # Provenance: which benchmark, which subset, which run, which grader.
    assert verdicts["t-1"]["benchmark"] == "offline_e2e"
    assert verdicts["t-1"]["subset"] == "test"
    assert verdicts["t-1"]["evaluator_id"] == "exact_match"
    assert verdicts["t-1"]["run_id"]
    assert verdicts["t-1"]["agent_result_ref"]["result_dir"]


async def test_the_agents_self_report_is_not_the_verdict(runner_factory, tmp_path):
    """``agent_reported_success`` and ``predicted_label`` are different facts."""
    await runner_factory(evaluator=ExactMatchEvaluator(), eval_loader=_EvalLoader()).run()

    for trace_dir in sorted((tmp_path / "traces").iterdir()):
        result = json.loads((trace_dir / "result.json").read_text("utf-8"))
        verdict = _read_verdict(trace_dir)
        if result["task_id"] == "t-2":
            # The agent finished cleanly; the answer is still wrong.
            assert result["agent_done"] == "done"
            assert verdict["predicted_label"] == 0


async def test_evaluation_failure_does_not_lose_the_sweep(runner_factory):
    """A broken evaluator must not destroy a sweep's worth of compute.

    The stub is duck-typed rather than a subclass, which also pins that
    the runner drives the ``BaseEvaluator`` *Protocol* and never reaches
    for a concrete base class.
    """

    class _Exploding:
        evaluator_id = "exploding"

        async def evaluate(self, trace, reference):
            raise RuntimeError("judge exploded")

    summary = await runner_factory(evaluator=_Exploding(), eval_loader=_EvalLoader()).run()

    assert summary.total == 2
    assert summary.completed == 2  # the runs themselves survived
    assert len(summary.eval_errors) == 2
    assert "judge exploded" in summary.eval_errors[0]


def test_an_evaluator_without_reference_data_is_rejected_at_construction():
    with pytest.raises(ValueError, match="eval_loader"):
        BenchmarkRunner(
            loader=_TaskLoader(),
            compose=lambda: None,  # type: ignore[arg-type, return-value]
            evaluator=ExactMatchEvaluator(),
        )
