"""BrowseComp evaluator — real encrypted reference data, scripted judge.

The reference rows come from the shipped
``benchmark_data/browser/browsecomp/tasks.jsonl`` through the real
``BrowseCompEvalLoader``, so the decryption path is exercised against
production data rather than a fixture someone wrote to match the code.
"""

from __future__ import annotations

import pytest

from spectragent.core.data_loader.eval.browsecomp import BrowseCompEvalLoader
from spectragent.core.trace.reader import RunTrace
from spectragent.evaluator import BrowseCompEvaluator
from spectragent.evaluator.browsecomp import decrypt, derive_key
from spectragent.evaluator.errors import ReferenceMismatchError
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.task import TaskReference
from tests.test_evaluator.conftest import FakeJudge, write_trace


@pytest.fixture(scope="module")
def loader() -> BrowseCompEvalLoader:
    return BrowseCompEvalLoader()


@pytest.fixture
def reference(loader: BrowseCompEvalLoader) -> EvalReference:
    return loader[loader.task_ids()[0]]


def _judge(evaluator: BrowseCompEvaluator, judge: FakeJudge) -> BrowseCompEvaluator:
    evaluator.llm = judge  # type: ignore[assignment]
    return evaluator


def test_shipped_data_decrypts_to_readable_text(reference):
    """The canary is the key; the plaintext never lands on disk."""
    assert reference.reference is not None
    canary = reference.metadata["canary"]

    question = decrypt(reference.reference.encrypted_question or "", canary)
    answer = decrypt(reference.reference.encrypted_answer or "", canary)

    assert question.isprintable()
    assert len(question) > 50
    assert answer.strip()


def test_derive_key_is_the_canary_hash_repeated():
    key = derive_key("password", 100)
    assert len(key) == 100
    assert key[:32] == key[32:64]


async def test_correct_verdict_produces_label_one(traces_dir, reference):
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id))
    judge = FakeJudge(replies=["reasoning here\ncorrect: yes"])

    result = await _judge(BrowseCompEvaluator(), judge).evaluate(trace, reference)

    assert result.predicted_label == 1
    assert result.evaluation_details.is_correct is True
    assert result.failure_category is None
    assert result.evaluator_id == "browsecomp"


async def test_incorrect_verdict_produces_label_zero(traces_dir, reference):
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id))
    judge = FakeJudge(replies=["Explanation.\ncorrect: no"])

    result = await _judge(BrowseCompEvaluator(), judge).evaluate(trace, reference)

    assert result.predicted_label == 0
    assert result.failure_category == "wrong_answer"
    assert result.evaluation_details.reasoning == "Explanation."


async def test_unparseable_grader_reply_defaults_to_incorrect(traces_dir, reference):
    """Preserves the ported behaviour: no verdict line means not correct."""
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id))
    judge = FakeJudge(replies=["I am not sure about this one."])

    result = await _judge(BrowseCompEvaluator(), judge).evaluate(trace, reference)

    assert result.predicted_label == 0
    assert result.evaluation_details.benchmark_details["grader_matched_pattern"] is False


async def test_the_grader_prompt_carries_decrypted_question_and_key(
    traces_dir, reference
):
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id, answer="my answer"))
    judge = FakeJudge(replies=["correct: yes"])

    result = await _judge(BrowseCompEvaluator(), judge).evaluate(trace, reference)

    assert len(judge.calls) == 1
    call = judge.calls[0]
    # BrowseComp's grader is a single user turn with no system prompt —
    # injecting one would change verdicts against a tuned prompt.
    assert call.system_text == ""
    assert "my answer" in call.user_text
    assert result.correct_answer is not None
    assert result.correct_answer in call.user_text
    assert result.task and result.task in call.user_text


async def test_prompt_snapshot_pins_the_template_and_substitutions(
    traces_dir, reference
):
    """A verdict must be re-derivable: which prompt text, which values."""
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id))
    judge = FakeJudge(replies=["correct: yes"])

    details = (
        await _judge(BrowseCompEvaluator(), judge).evaluate(trace, reference)
    ).evaluation_details

    snapshot = details.user_prompt
    assert snapshot is not None
    assert snapshot.kind == "template"
    assert snapshot.ref.path == "evaluators/browsecomp/grader_user.txt"
    assert len(snapshot.ref.content_hash) == 64
    assert set(snapshot.params) == {"question", "correct_answer", "response"}


async def test_judge_token_usage_is_recorded(traces_dir, reference):
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id))
    judge = FakeJudge(replies=["correct: yes"])

    details = (
        await _judge(BrowseCompEvaluator(), judge).evaluate(trace, reference)
    ).evaluation_details

    assert details.eval_usage is not None
    assert details.eval_usage.prompt_tokens == 100
    assert details.eval_usage.completion_tokens == 20
    assert details.eval_usage.cached_tokens == 10
    assert details.eval_usage.non_cached_prompt == 90


async def test_missing_canary_is_a_loud_error(traces_dir, reference):
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id))
    stripped = reference.model_copy(update={"metadata": {}})

    with pytest.raises(ReferenceMismatchError, match="canary"):
        await _judge(BrowseCompEvaluator(), FakeJudge()).evaluate(trace, stripped)


async def test_missing_ciphertext_is_a_loud_error(traces_dir):
    trace = RunTrace(write_trace(traces_dir))
    bare = EvalReference(
        task_id="task-0",
        benchmark="browsecomp",
        reference=TaskReference(),
        metadata={"canary": "x"},
    )

    with pytest.raises(ReferenceMismatchError, match="encrypted_question"):
        await _judge(BrowseCompEvaluator(), FakeJudge()).evaluate(trace, bare)


async def test_judge_transport_failure_is_not_scored_as_wrong(traces_dir, reference):
    """The ported client swallowed API errors into ``correct: no``.

    Turning an outage into a benchmark score is worse than crashing.
    """

    class _Broken:
        model = "broken"

        async def ainvoke(self, messages, output_format=None, **kwargs):
            raise ConnectionError("judge API is down")

    evaluator = BrowseCompEvaluator()
    evaluator.llm = _Broken()  # type: ignore[assignment]
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id))

    with pytest.raises(ConnectionError):
        await evaluator.evaluate(trace, reference)
