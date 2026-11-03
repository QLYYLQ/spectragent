"""ExactMatchVerifier — the fully offline acceptance path.

No network, no browser, no LLM: a synthetic ``RunTrace`` on disk, a real
``EvalReference``, and a ``Judgment`` out. This is the shape of every offline
verifier in this framework; the LLM judges only differ in how they decide.
"""

from __future__ import annotations

import pytest

from spectragent.core.trace.reader import RunTrace
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.verdict import Judgment
from spectragent.verifiers.errors import ReferenceMismatchError, TraceIncompleteError
from spectragent.verifiers.exact_match import ExactMatchVerifier
from tests.test_verifiers.conftest import write_trace


def _reference(expected: str, task_id: str = "task-0") -> EvalReference:
    return EvalReference(task_id=task_id, benchmark="exact_match_bench", expected_answer=expected)


async def test_correct_answer_passes(traces_dir):
    trace = RunTrace(write_trace(traces_dir, answer="42"))

    judgment = await ExactMatchVerifier().verify(trace, _reference("42"))

    assert isinstance(judgment, Judgment)
    assert judgment.label == 1
    assert judgment.score is None
    assert judgment.evidence["expected"] == "42"
    assert judgment.evidence["answer"] == "42"


async def test_wrong_answer_fails(traces_dir):
    trace = RunTrace(write_trace(traces_dir, answer="43"))

    judgment = await ExactMatchVerifier().verify(trace, _reference("42"))

    assert judgment.label == 0
    assert "!=" in (judgment.rationale or "")


def test_its_identity_names_the_settings_that_judge():
    """``exact_match`` alone is not identity: allow_substring on and off are different judges."""
    identity = ExactMatchVerifier().identity()

    assert identity.verifier_id == "exact_match"
    assert identity.model is None
    assert identity.config_signature == {
        "case_sensitive": False,
        "strip_punctuation": True,
        "strip_articles": True,
        "allow_substring": False,
    }
    lenient = ExactMatchVerifier(allow_substring=True).identity()
    assert lenient.config_signature["allow_substring"] is True
    assert lenient.slug != identity.slug
    assert identity.slug == ExactMatchVerifier().identity().slug


async def test_two_settings_of_one_kind_judge_differently(traces_dir):
    trace = RunTrace(write_trace(traces_dir, answer="paris"))
    reference = _reference("Paris")

    strict = await ExactMatchVerifier(case_sensitive=True).verify(trace, reference)
    lenient = await ExactMatchVerifier(allow_substring=True).verify(trace, reference)

    assert strict.label == 0
    assert lenient.label == 1


def test_it_judges_offline_against_a_reference_row():
    assert ExactMatchVerifier().needs == frozenset({"trace", "reference"})


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

    judgment = await ExactMatchVerifier().verify(trace, _reference(expected))

    assert bool(judgment.label) is matched


async def test_substring_is_off_by_default_and_opt_in(traces_dir):
    trace = RunTrace(write_trace(traces_dir, answer="I believe the answer is 42."))
    reference = _reference("42")

    assert (await ExactMatchVerifier().verify(trace, reference)).label == 0
    lenient = ExactMatchVerifier(allow_substring=True)
    assert (await lenient.verify(trace, reference)).label == 1


async def test_missing_expected_answer_is_a_loud_error(traces_dir):
    """Rubric benchmarks must not be silently judged as 'answer != None'."""
    trace = RunTrace(write_trace(traces_dir))

    with pytest.raises(ReferenceMismatchError, match="expected_answer"):
        await ExactMatchVerifier().verify(trace, EvalReference(task_id="task-0", benchmark="b"))


async def test_no_reference_is_a_loud_error(traces_dir):
    trace = RunTrace(write_trace(traces_dir))

    with pytest.raises(ReferenceMismatchError, match="reference row"):
        await ExactMatchVerifier().verify(trace, None)


async def test_an_agent_that_never_finished_is_still_judged(traces_dir):
    """A timeout fails — it does not crash the pass or get skipped."""
    trace = RunTrace(
        write_trace(
            traces_dir,
            answer="partial",
            agent_done=AgentDoneStatus.TIMEOUT,
            agent_reported_success=None,
        )
    )

    judgment = await ExactMatchVerifier().verify(trace, _reference("42"))

    assert judgment.label == 0
    assert judgment.evidence["agent_done"] == "timeout"


async def test_trace_without_result_json_says_so(tmp_path):
    from spectragent.core.trace.models import TraceConfig, TraceStatus
    from spectragent.core.trace.wal import WALWriter

    wal = WALWriter(tmp_path / "traces", "crashed")
    wal.init_run(TraceConfig(task_id="task-0", benchmark="b"))
    wal.finalize(TraceStatus.INCOMPLETE, incomplete_reason="result.json could not be written")

    with pytest.raises(TraceIncompleteError, match="result.json"):
        await ExactMatchVerifier().verify(RunTrace(wal.run_dir), _reference("42"))
