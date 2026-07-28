"""Online-Mind2Web WebJudge — and the image path that used to be dead.

``RunTrace.screenshots`` globbed ``*_screenshot.png`` while the writer
only ever produced ``*_screenshot.json`` with the PNG base64'd inside, so
the list was empty for every trace ever recorded. WebJudge silently took
its ``webjudge_user_no_images`` branch and emitted a weaker verdict with
no error. These tests pin both halves: that images now reach the judge,
and that a screenshot-less trace announces its degradation instead of
hiding it.
"""

from __future__ import annotations

import pytest

from spectragent.core.data_loader.eval.online_mind2web import OnlineMind2WebEvalLoader
from spectragent.core.trace.reader import RunTrace
from spectragent.evaluator import OnlineMind2WebEvaluator
from spectragent.evaluator.errors import TraceIncompleteError
from spectragent.evaluator.online_mind2web import (
    _parse_image_judgement,
    _parse_key_points,
    extract_failure_category,
    extract_predication,
    extract_reasoning,
)
from tests.test_evaluator.conftest import FakeJudge, png_bytes, write_trace

_KEY_POINTS = "**Key Points**:\n1. Open the store locator\n2. Set a home store"
_IMAGE_PASS = "**Reasoning**: The store list is visible.\n\n**Score**: 5"
_IMAGE_FAIL = "**Reasoning**: Blank loading page.\n\n**Score**: 1"
_SUCCESS = "Thoughts: All key points met.\nStatus: success"
_FAILURE = "Thoughts: The filter was never applied.\nStatus: failure"


@pytest.fixture(scope="module")
def loader() -> OnlineMind2WebEvalLoader:
    return OnlineMind2WebEvalLoader()


@pytest.fixture
def reference(loader: OnlineMind2WebEvalLoader):
    return loader[loader.task_ids()[0]]


def _evaluator(judge: FakeJudge, **kwargs) -> OnlineMind2WebEvaluator:
    evaluator = OnlineMind2WebEvaluator(**kwargs)
    evaluator.llm = judge  # type: ignore[assignment]
    return evaluator


def _three_frames() -> list[bytes]:
    return [png_bytes((10, 20, 30)), png_bytes((200, 10, 10)), png_bytes((5, 200, 5))]


# ── The image path ──────────────────────────────────────────────────


async def test_screenshots_reach_the_image_judge(traces_dir, reference):
    """Regression for the silently-dead image path."""
    trace = RunTrace(
        write_trace(
            traces_dir,
            task_id=reference.task_id,
            steps=3,
            screenshots=_three_frames(),
        )
    )
    assert len(trace.screenshots) == 3, "fixture must produce real PNGs on disk"

    judge = FakeJudge(replies=[_KEY_POINTS, _IMAGE_PASS, _IMAGE_PASS, _IMAGE_PASS, _SUCCESS])
    result = await _evaluator(judge).evaluate(trace, reference)

    # 1 key-points call + 3 image calls + 1 final judgement.
    assert len(judge.calls) == 5
    image_calls = [c for c in judge.calls if c.image_parts]
    assert len(image_calls) == 4  # three per-frame judgements + the final call
    for call in judge.calls[1:4]:
        assert len(call.image_parts) == 1
        assert call.image_parts[0].image_url.url.startswith("data:image/png;base64,")

    details = result.evaluation_details.benchmark_details
    assert details["screenshots_found"] == 3
    assert details["images_forwarded"] == 3
    assert details["image_judge_skipped"] is False
    assert result.predicted_label == 1


async def test_low_scoring_frames_are_filtered_out_of_the_final_call(
    traces_dir, reference
):
    """Only frames at or above ``score_threshold`` become evidence."""
    trace = RunTrace(
        write_trace(
            traces_dir,
            task_id=reference.task_id,
            steps=3,
            screenshots=_three_frames(),
        )
    )
    judge = FakeJudge(replies=[_KEY_POINTS, _IMAGE_PASS, _IMAGE_FAIL, _IMAGE_FAIL, _SUCCESS])

    result = await _evaluator(judge).evaluate(trace, reference)

    assert result.evaluation_details.benchmark_details["images_forwarded"] == 1
    assert len(judge.calls[-1].image_parts) == 1
    scores = [
        r["Score"] for r in result.evaluation_details.benchmark_details["image_judge_record"]
    ]
    assert scores == [5, 1, 1]


async def test_final_call_uses_the_image_template_when_frames_survive(
    traces_dir, reference
):
    trace = RunTrace(
        write_trace(traces_dir, task_id=reference.task_id, steps=1, screenshots=[png_bytes()])
    )
    judge = FakeJudge(replies=[_KEY_POINTS, _IMAGE_PASS, _SUCCESS])

    result = await _evaluator(judge).evaluate(trace, reference)

    snapshot = result.evaluation_details.user_prompt
    assert snapshot is not None
    assert snapshot.ref.path == "evaluators/online_mind2web/webjudge_user.txt"
    # The image template is the one that carries the per-frame reasonings.
    assert "The store list is visible." in judge.calls[-1].user_text


async def test_screenshotless_trace_records_its_degradation(traces_dir, reference):
    """The old failure mode, now visible in the verdict."""
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id, screenshots=[]))
    assert trace.screenshots == []
    judge = FakeJudge(replies=[_KEY_POINTS, _SUCCESS])

    result = await _evaluator(judge).evaluate(trace, reference)

    details = result.evaluation_details.benchmark_details
    assert details["screenshots_found"] == 0
    assert details["image_judge_skipped"] is True
    snapshot = result.evaluation_details.user_prompt
    assert snapshot is not None
    assert snapshot.ref.path == "evaluators/online_mind2web/webjudge_user_no_images.txt"
    assert len(judge.calls) == 2  # key points + final; no image judgements


async def test_require_screenshots_turns_degradation_into_an_error(
    traces_dir, reference
):
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id, screenshots=[]))
    judge = FakeJudge(replies=[_KEY_POINTS])

    with pytest.raises(TraceIncompleteError, match="no screenshots"):
        await _evaluator(judge, require_screenshots=True).evaluate(trace, reference)


async def test_max_images_caps_what_reaches_the_final_call(traces_dir, reference):
    frames = [png_bytes((i, i, i)) for i in range(6)]
    trace = RunTrace(
        write_trace(traces_dir, task_id=reference.task_id, steps=6, screenshots=frames)
    )
    judge = FakeJudge(replies=[_KEY_POINTS, *[_IMAGE_PASS] * 6, _SUCCESS])

    result = await _evaluator(judge, max_images=2).evaluate(trace, reference)

    assert result.evaluation_details.benchmark_details["images_forwarded"] == 2
    assert len(judge.calls[-1].image_parts) == 2


# ── Prompt payload ──────────────────────────────────────────────────


async def test_action_history_and_key_points_reach_the_final_prompt(
    traces_dir, reference
):
    trace = RunTrace(
        write_trace(traces_dir, task_id=reference.task_id, steps=2, screenshots=[])
    )
    judge = FakeJudge(replies=[_KEY_POINTS, _SUCCESS])

    result = await _evaluator(judge).evaluate(trace, reference)

    final = judge.calls[-1].user_text
    assert "1. browser.click({'index': 0})" in final
    assert "2. browser.click({'index': 1})" in final
    assert "Open the store locator" in final
    assert "Open the store locator" in result.evaluation_details.benchmark_details["key_points"]


async def test_preprocessing_prompts_are_snapshotted_separately(traces_dir, reference):
    trace = RunTrace(
        write_trace(traces_dir, task_id=reference.task_id, steps=1, screenshots=[png_bytes()])
    )
    judge = FakeJudge(replies=[_KEY_POINTS, _IMAGE_PASS, _SUCCESS])

    details = (await _evaluator(judge).evaluate(trace, reference)).evaluation_details

    assert details.additional_prompts is not None
    assert set(details.additional_prompts) == {
        "identify_key_points_system",
        "identify_key_points_user",
        "judge_image_system",
        "judge_image_user",
    }
    assert details.system_prompt is not None
    assert details.system_prompt.ref.path == "evaluators/online_mind2web/webjudge_system.txt"


async def test_usage_accumulates_across_every_stage(traces_dir, reference):
    trace = RunTrace(
        write_trace(traces_dir, task_id=reference.task_id, steps=2, screenshots=_three_frames()[:2])
    )
    judge = FakeJudge(replies=[_KEY_POINTS, _IMAGE_PASS, _IMAGE_PASS, _SUCCESS])

    details = (await _evaluator(judge).evaluate(trace, reference)).evaluation_details

    assert details.eval_usage is not None
    assert details.eval_usage.prompt_tokens == 4 * 100
    assert details.eval_usage.completion_tokens == 4 * 20


async def test_usage_does_not_leak_between_traces(traces_dir, reference):
    """One evaluator instance scores many traces; billing must not accrue."""
    evaluator = _evaluator(FakeJudge(replies=[_KEY_POINTS, _SUCCESS]))
    first = RunTrace(write_trace(traces_dir, run_id="a", task_id=reference.task_id, screenshots=[]))
    second = RunTrace(write_trace(traces_dir, run_id="b", task_id=reference.task_id, screenshots=[]))

    one = await evaluator.evaluate(first, reference)
    two = await evaluator.evaluate(second, reference)

    assert one.evaluation_details.eval_usage is not None
    assert two.evaluation_details.eval_usage is not None
    assert (
        one.evaluation_details.eval_usage.prompt_tokens
        == two.evaluation_details.eval_usage.prompt_tokens
    )


async def test_missing_task_text_is_a_loud_error(traces_dir, reference):
    trace = RunTrace(write_trace(traces_dir, task_id=reference.task_id, task=""))

    with pytest.raises(TraceIncompleteError, match="task text"):
        await _evaluator(FakeJudge()).evaluate(trace, reference)


# ── Parsers (verbatim ports) ────────────────────────────────────────


@pytest.mark.parametrize(
    "response,expected",
    [
        ("Thoughts: ok\nStatus: success", 1),
        ("Thoughts: no\nStatus: failure", 0),
        ("STATUS: SUCCESS", 1),
        ("no status line at all", 0),
        ("", 0),
    ],
)
def test_extract_predication(response, expected):
    assert extract_predication(response) == expected


def test_extract_failure_category():
    assert extract_failure_category("Failure Category: A2\nStatus: failure") == "A2"
    assert extract_failure_category("Status: failure") is None


def test_extract_reasoning():
    assert extract_reasoning("Thoughts: it never filtered\nStatus: failure") == (
        "it never filtered"
    )
    assert extract_reasoning("nothing here") is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("**Key Points**:\n1. a\n2. b", "\n1. a\n2. b"),
        ("Key Points:\n  1. a", "\n1. a"),
    ],
)
def test_parse_key_points(raw, expected):
    assert _parse_key_points(raw) == expected


def test_unparseable_image_judgement_scores_zero_not_the_threshold():
    """A malformed judgement can only exclude a frame, never include one."""
    score, thought = _parse_image_judgement("the model rambled without a score")
    assert score == 0
    assert thought == ""
