"""TraceView over a typed RunTrace.

``RunTrace`` hands back real models now, so the projection every
evaluator reads through is doing attribute access, not dict digging.
These are the guarantees that change gives the evaluator layer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.reader import RunTrace
from spectragent.evaluator import EvaluationRunner, ExactMatchEvaluator
from spectragent.evaluator.errors import TraceIncompleteError
from spectragent.evaluator.trace_view import TraceView
from spectragent.schemas.eval_reference import EvalReference
from tests.test_evaluator.conftest import write_trace


class _Loader:
    benchmark_name = "offline_bench"
    subset = "All"

    def __init__(self, answers: dict[str, str]):
        self._refs = {
            task_id: EvalReference(
                task_id=task_id, benchmark="offline_bench", expected_answer=answer
            )
            for task_id, answer in answers.items()
        }

    def get(self, task_id: str) -> EvalReference | None:
        return self._refs.get(task_id)


def _legacy_trace(traces_dir: Path, task_id: str = "t-old") -> Path:
    """A trace whose result.json was written by the retired 2.0 schema."""
    trace_dir = write_trace(traces_dir, run_id="legacy", task_id=task_id)
    (trace_dir / "result.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "task_id": task_id,
                "timestamp": "2025-01-01T00:00:00+00:00",
                "env_status": "success",
                "agent_done": "done",
                "agent_success": True,
                "answer": "42",
                "metrics": {"end_to_end_ms": 1, "steps": 1},
            }
        ),
        "utf-8",
    )
    return trace_dir


class TestTheProjectionReadsModels:
    def test_the_four_things_come_off_the_typed_result(self, traces_dir):
        view = TraceView(
            RunTrace(write_trace(traces_dir, task="Find it", answer="42", steps=2))
        )

        assert view.task_text == "Find it"
        assert view.answer == "42"
        assert view.agent_done == "done"
        assert view.action_history == [
            "browser.click({'index': 0})",
            "browser.click({'index': 1})",
        ]
        assert view.step_count == 2

    def test_metrics_are_never_absent(self, traces_dir):
        """``AgentMetrics`` is required on ``AgentResult``, so this is total.

        It used to be ``AgentMetrics | None`` because the projection was
        rebuilding it from a dict that might not have carried the key.
        """
        view = TraceView(RunTrace(write_trace(traces_dir, end_to_end_ms=777)))

        assert view.metrics.end_to_end_ms == 777
        assert view.end_to_end_ms == 777

    def test_metrics_cannot_be_edited_through_the_view(self, traces_dir):
        """``build_result`` hands these to a mutable ``EvalDetails``.

        Returning the trace's own object would let one evaluator's
        bookkeeping show up in the next reader's numbers.
        """
        trace = RunTrace(write_trace(traces_dir, total_tokens=90))
        view = TraceView(trace)

        assert view.metrics.usage is not None
        view.metrics.usage.total_tokens = 999_999

        assert trace.result is not None
        assert trace.result.metrics.usage is not None
        assert trace.result.metrics.usage.total_tokens == 90

    def test_timestamp_needs_no_fallback(self, traces_dir):
        view = TraceView(RunTrace(write_trace(traces_dir)))

        assert view.timestamp.tzinfo is not None
        assert view.result_ref().timestamp == view.timestamp


class TestUnreadableIsNotUnfinished:
    def test_a_missing_result_is_still_the_incomplete_error(self, traces_dir):
        trace_dir = write_trace(traces_dir)
        (trace_dir / "result.json").unlink()

        with pytest.raises(TraceIncompleteError, match="no result.json"):
            _ = TraceView(RunTrace(trace_dir)).answer

    def test_a_legacy_result_is_a_schema_error_not_an_incomplete_run(self, traces_dir):
        view = TraceView(RunTrace(_legacy_trace(traces_dir)))

        with pytest.raises(TraceSchemaError):
            _ = view.answer

    async def test_a_legacy_trace_costs_the_sweep_one_entry_not_the_run(
        self, traces_dir
    ):
        """The runner must survive it, and say what it survived.

        A schema failure and a judge outage both leave the task unscored,
        but only one of them can be fixed by re-running the scoring pass
        — so the recorded reason has to name which happened.
        """
        good = write_trace(traces_dir, run_id="good", task_id="t-1", answer="42")
        legacy = _legacy_trace(traces_dir, task_id="t-old")

        report = await EvaluationRunner(
            evaluator=ExactMatchEvaluator(),
            eval_loader=_Loader({"t-1": "42", "t-old": "42"}),
        ).run([good, legacy], expected_task_ids=["t-1", "t-old"])

        graded = {r.task_id for r in report.graded}
        assert graded == {"t-1"}

        assert [o.trace_dir for o in report.errors] == [legacy]
        assert "TraceSchemaError" in (report.errors[0].error or "")

        unscored = next(r for r in report.results if r.task_id == "t-old")
        reason = unscored.evaluation_details.benchmark_details["evaluation_error"]
        assert "predates AgentResult schema" in reason
