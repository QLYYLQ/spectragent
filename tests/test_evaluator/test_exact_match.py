"""ExactMatchEvaluator — the fully offline acceptance path.

No network, no browser, no LLM: a synthetic ``RunTrace`` on disk, a real
``EvalReference``, and an ``EvalResult`` out. This is the shape of every
evaluation in this framework; the LLM judges only differ in how they
decide.
"""

from __future__ import annotations

import pytest

from spectragent.core.trace.reader import RunTrace
from spectragent.evaluator import ExactMatchEvaluator
from spectragent.evaluator.errors import ReferenceMismatchError, TraceIncompleteError
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.eval_result import EvalResult
from tests.test_evaluator.conftest import write_trace


def _reference(expected: str, task_id: str = "task-0") -> EvalReference:
    return EvalReference(
        task_id=task_id, benchmark="exact_match_bench", expected_answer=expected
    )


async def test_correct_answer_scores_one(traces_dir):
    trace = RunTrace(write_trace(traces_dir, answer="42"))

    result = await ExactMatchEvaluator().evaluate(trace, _reference("42"))

    assert isinstance(result, EvalResult)
    assert result.predicted_label == 1
    assert result.evaluation_details.is_correct is True
    assert result.correct_answer == "42"
    assert result.agent_response == "42"
    assert result.failure_category is None


async def test_wrong_answer_scores_zero_and_is_categorised(traces_dir):
    trace = RunTrace(write_trace(traces_dir, answer="43"))

    result = await ExactMatchEvaluator().evaluate(trace, _reference("42"))

    assert result.predicted_label == 0
    assert result.failure_category == "wrong_answer"


async def test_the_verdict_carries_full_provenance(traces_dir):
    """A verdict must say which grader, benchmark, subset and run made it."""
    trace_dir = write_trace(
        traces_dir, run_id="run-7", task_id="t-7", benchmark="bench-x", subset="Hard"
    )

    result = await ExactMatchEvaluator().evaluate(RunTrace(trace_dir), _reference("42", "t-7"))

    assert result.schema_version == "2.1"
    assert result.evaluator_id == "exact_match"
    assert result.benchmark == "bench-x"
    assert result.subset == "Hard"
    assert result.run_id == "run-7"
    assert result.task_id == "t-7"
    assert result.agent_result_ref.result_dir == str(trace_dir)
    assert result.agent_result_ref.model_id == "test-model"
    # A grader read the trace, so the verdict is a measurement.
    assert result.verdict_source == "graded"
    # ...produced by *these* settings. "exact_match" alone is not identity:
    # allow_substring on and off are different scorers under one name.
    assert result.evaluator_config == {
        "case_sensitive": False,
        "strip_punctuation": True,
        "strip_articles": True,
        "allow_substring": False,
    }


async def test_the_verdict_names_the_settings_that_produced_it(traces_dir):
    """Two configurations of one evaluator must be tellable apart."""
    trace = RunTrace(write_trace(traces_dir, answer="paris"))
    reference = _reference("Paris")

    strict = await ExactMatchEvaluator(case_sensitive=True).evaluate(trace, reference)
    lenient = await ExactMatchEvaluator(allow_substring=True).evaluate(trace, reference)

    assert strict.predicted_label == 0
    assert lenient.predicted_label == 1
    assert strict.evaluator_config["case_sensitive"] is True
    assert lenient.evaluator_config["allow_substring"] is True
    assert strict.evaluator_config != lenient.evaluator_config


async def test_agent_metrics_are_nested_not_flat_merged(traces_dir):
    """The agent's usage must not overwrite the evaluator's."""
    trace = RunTrace(write_trace(traces_dir, end_to_end_ms=999, steps=3))

    details = (await ExactMatchEvaluator().evaluate(trace, _reference("42"))).evaluation_details

    assert details.agent_metrics is not None
    assert details.agent_metrics.end_to_end_ms == 999
    assert details.agent_metrics.steps == 3
    assert details.agent_metrics.usage is not None
    assert details.agent_metrics.usage.total_tokens == 90


@pytest.mark.parametrize(
    "expected,answer,matched",
    [
        ("Paris", "paris", True),
        ("Paris", "  PARIS  ", True),
        ("the Eiffel Tower", "Eiffel Tower", True),
        ("42", "42.", True),
        ("42", "the answer is 42", False),
        ("Paris", "Lyon", False),
    ],
)
async def test_normalization_rules(traces_dir, expected, answer, matched):
    trace = RunTrace(write_trace(traces_dir, run_id=f"r-{answer!r}", answer=answer))

    result = await ExactMatchEvaluator().evaluate(trace, _reference(expected))

    assert bool(result.predicted_label) is matched


async def test_substring_is_off_by_default_and_opt_in(traces_dir):
    trace = RunTrace(write_trace(traces_dir, answer="I believe the answer is 42."))
    reference = _reference("42")

    assert (await ExactMatchEvaluator().evaluate(trace, reference)).predicted_label == 0
    lenient = ExactMatchEvaluator(allow_substring=True)
    assert (await lenient.evaluate(trace, reference)).predicted_label == 1


async def test_case_sensitivity_is_configurable(traces_dir):
    trace = RunTrace(write_trace(traces_dir, answer="paris"))

    strict = ExactMatchEvaluator(case_sensitive=True)
    assert (await strict.evaluate(trace, _reference("Paris"))).predicted_label == 0


async def test_missing_expected_answer_is_a_loud_error(traces_dir):
    """Rubric benchmarks must not be silently graded as 'answer != None'."""
    trace = RunTrace(write_trace(traces_dir))

    with pytest.raises(ReferenceMismatchError, match="expected_answer"):
        await ExactMatchEvaluator().evaluate(
            trace, EvalReference(task_id="task-0", benchmark="b")
        )


async def test_an_agent_that_never_finished_is_still_scorable(traces_dir):
    """A timeout scores 0 — it does not crash the sweep or get skipped."""
    trace = RunTrace(
        write_trace(
            traces_dir,
            answer="partial",
            agent_done=AgentDoneStatus.TIMEOUT,
            agent_reported_success=None,
        )
    )

    result = await ExactMatchEvaluator().evaluate(trace, _reference("42"))

    assert result.predicted_label == 0
    assert result.evaluation_details.benchmark_details["agent_done"] == "timeout"


async def test_trace_without_result_json_says_so(tmp_path):
    from spectragent.core.trace.models import RunStatus, TraceConfig
    from spectragent.core.trace.wal import WALWriter

    wal = WALWriter(tmp_path / "traces", "crashed")
    wal.init_run(TraceConfig(task_id="task-0", benchmark="b"))
    wal.finalize(RunStatus.ERROR)

    with pytest.raises(TraceIncompleteError, match="result.json"):
        await ExactMatchEvaluator().evaluate(RunTrace(wal.run_dir), _reference("42"))
