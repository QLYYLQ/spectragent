"""BrowseComp's verifier — real encrypted reference data, scripted judge.

The reference rows come from the shipped
``benchmark_data/browser/browsecomp/tasks.jsonl`` through the real
``BrowseCompEvalLoader``, so the decryption path is exercised against
production data rather than a fixture someone wrote to match the code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spectragent.benchmarks.browsecomp.verifier import BrowseCompVerifier, decrypt, derive_key
from spectragent.core.data_loader.eval.browsecomp import BrowseCompEvalLoader
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.prompt import TemplatePrompt
from spectragent.schemas.task import TaskReference
from spectragent.schemas.verdict import JudgeUsage
from spectragent.verifiers.errors import ReferenceMismatchError
from tests.conftest import require_benchmark_data
from tests.test_verifiers.conftest import FakeJudge, write_trace


@pytest.fixture(scope="module")
def loader() -> BrowseCompEvalLoader:
    require_benchmark_data("browser/browsecomp")
    return BrowseCompEvalLoader()


@pytest.fixture
def reference(loader: BrowseCompEvalLoader) -> EvalReference:
    return loader[loader.task_ids()[0]]


@pytest.fixture
def traces_dir(tmp_path: Path) -> Path:
    return tmp_path / "traces"


def _judge(verifier: BrowseCompVerifier, judge: FakeJudge) -> BrowseCompVerifier:
    verifier.llm = judge  # type: ignore[assignment]
    return verifier


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
    verifier = _judge(BrowseCompVerifier(), judge)

    judgment = await verifier.verify(trace, reference)

    assert judgment.label == 1
    assert verifier.identity().verifier_id == "browsecomp"


async def test_incorrect_verdict_produces_label_zero(traces_dir, reference):
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id))
    judge = FakeJudge(replies=["Explanation.\ncorrect: no"])

    judgment = await _judge(BrowseCompVerifier(), judge).verify(trace, reference)

    assert judgment.label == 0
    assert judgment.rationale == "Explanation."


async def test_unparseable_grader_reply_defaults_to_incorrect(traces_dir, reference):
    """Preserves the ported behaviour: no verdict line means not correct."""
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id))
    judge = FakeJudge(replies=["I am not sure about this one."])

    judgment = await _judge(BrowseCompVerifier(), judge).verify(trace, reference)

    assert judgment.label == 0
    assert judgment.evidence["grader_matched_pattern"] is False


async def test_the_grader_prompt_carries_decrypted_question_and_key(traces_dir, reference):
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id, answer="my answer"))
    judge = FakeJudge(replies=["correct: yes"])

    judgment = await _judge(BrowseCompVerifier(), judge).verify(trace, reference)

    assert len(judge.calls) == 1
    call = judge.calls[0]
    # BrowseComp's grader is a single user turn with no system prompt —
    # injecting one would change verdicts against a tuned prompt.
    assert call.system_text == ""
    assert "my answer" in call.user_text
    correct_answer = judgment.evidence["correct_answer"]
    assert isinstance(correct_answer, str) and correct_answer in call.user_text
    question = judgment.evidence["question"]
    assert isinstance(question, str) and question and question in call.user_text


async def test_prompt_snapshot_pins_the_template_and_substitutions(traces_dir, reference):
    """A verdict must be re-derivable: which prompt text, which values."""
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id))
    judge = FakeJudge(replies=["correct: yes"])

    judgment = await _judge(BrowseCompVerifier(), judge).verify(trace, reference)

    snapshot = TemplatePrompt.model_validate(judgment.evidence["user_prompt"])
    assert snapshot.ref.path == "verifiers/browsecomp/grader_user.txt"
    assert len(snapshot.ref.content_hash) == 64
    assert set(snapshot.params) == {"question", "correct_answer", "response"}


async def test_judge_token_usage_is_recorded(traces_dir, reference):
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id))
    judge = FakeJudge(replies=["correct: yes"])

    judgment = await _judge(BrowseCompVerifier(), judge).verify(trace, reference)

    assert judgment.usage == JudgeUsage(
        calls=1, reported_calls=1, prompt_tokens=100, completion_tokens=20, cached_tokens=10
    )


async def test_missing_canary_is_a_loud_error(traces_dir, reference):
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id))
    stripped = reference.model_copy(update={"metadata": {}})

    with pytest.raises(ReferenceMismatchError, match="canary"):
        await _judge(BrowseCompVerifier(), FakeJudge()).verify(trace, stripped)


async def test_missing_ciphertext_is_a_loud_error(traces_dir):
    trace = RunTrace(write_trace(traces_dir))
    bare = EvalReference(
        task_id="task-0",
        benchmark="browsecomp",
        reference=TaskReference(),
        metadata={"canary": "x"},
    )

    with pytest.raises(ReferenceMismatchError, match="encrypted_question"):
        await _judge(BrowseCompVerifier(), FakeJudge()).verify(trace, bare)


async def test_judge_transport_failure_is_not_judged_wrong(traces_dir, reference):
    """The ported client swallowed API errors into ``correct: no``.

    Turning an outage into a benchmark score is worse than crashing.
    """

    class _Broken:
        inference_kind = "chat"
        model = "broken"

        async def ainvoke(self, messages, output_format=None, **kwargs):
            raise ConnectionError("judge API is down")

    verifier = BrowseCompVerifier()
    verifier.llm = _Broken()  # type: ignore[assignment]
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id))

    with pytest.raises(ConnectionError):
        await verifier.verify(trace, reference)
