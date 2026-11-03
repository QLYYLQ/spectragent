"""Online-Mind2Web WebJudge — and the image path that used to be dead.

``RunTrace.screenshots`` globbed ``*_screenshot.png`` while the writer
only ever produced ``*_screenshot.json`` with the PNG base64'd inside, so
the list was empty for every trace ever recorded. WebJudge silently took
its ``webjudge_user_no_images`` branch and emitted a weaker verdict with
no error. These tests pin both halves: that images now reach the judge,
and that a screenshot-less trace announces its degradation instead of
hiding it.

WebJudge reads no reference row (its ``__needs__`` has none), so every
test hands it ``None``, as the runner does.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from spectragent.benchmarks.online_mind2web import verifier as webjudge
from spectragent.benchmarks.online_mind2web.verifier import (
    OnlineMind2WebVerifier,
    _parse_image_judgement,
    _parse_key_points,
    extract_failure_category,
    extract_predication,
    extract_reasoning,
)
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.prompt import TemplatePrompt, TextPrompt
from spectragent.schemas.verdict import JudgeUsage
from spectragent.verifiers.errors import TraceIncompleteError
from spectragent.verifiers.runner import VerificationRunner
from tests.test_verifiers.conftest import FakeJudge, png_bytes, write_trace

_KEY_POINTS = "**Key Points**:\n1. Open the store locator\n2. Set a home store"
_IMAGE_PASS = "**Reasoning**: The store list is visible.\n\n**Score**: 5"
_IMAGE_FAIL = "**Reasoning**: Blank loading page.\n\n**Score**: 1"
_SUCCESS = "Thoughts: All key points met.\nStatus: success"
_FAILURE = "Thoughts: The filter was never applied.\nStatus: failure"


@pytest.fixture
def traces_dir(tmp_path: Path) -> Path:
    return tmp_path / "traces"


def _verifier(judge: FakeJudge, **kwargs) -> OnlineMind2WebVerifier:
    verifier = OnlineMind2WebVerifier(**kwargs)
    verifier.llm = judge  # type: ignore[assignment]
    return verifier


def _three_frames() -> list[bytes]:
    return [png_bytes((10, 20, 30)), png_bytes((200, 10, 10)), png_bytes((5, 200, 5))]


# ── The image path ──────────────────────────────────────────────────


async def test_screenshots_reach_the_image_judge(traces_dir):
    """Regression for the silently-dead image path."""
    trace = RunTrace(write_trace(traces_dir, steps=3, screenshots=_three_frames()))
    assert len(trace.screenshots) == 3, "fixture must produce real PNGs on disk"

    judge = FakeJudge(replies=[_KEY_POINTS, _IMAGE_PASS, _IMAGE_PASS, _IMAGE_PASS, _SUCCESS])
    judgment = await _verifier(judge).verify(trace, None)

    # 1 key-points call + 3 image calls + 1 final judgement.
    assert len(judge.calls) == 5
    image_calls = [c for c in judge.calls if c.image_parts]
    assert len(image_calls) == 4  # three per-frame judgements + the final call
    for call in judge.calls[1:4]:
        assert len(call.image_parts) == 1
        assert call.image_parts[0].image_url.url.startswith("data:image/png;base64,")

    evidence = judgment.evidence
    assert evidence["screenshots_found"] == 3
    assert evidence["images_forwarded"] == 3
    assert evidence["image_judge_skipped"] is False
    assert judgment.label == 1


async def test_low_scoring_frames_are_filtered_out_of_the_final_call(traces_dir):
    """Only frames at or above ``score_threshold`` become evidence."""
    trace = RunTrace(write_trace(traces_dir, steps=3, screenshots=_three_frames()))
    judge = FakeJudge(replies=[_KEY_POINTS, _IMAGE_PASS, _IMAGE_FAIL, _IMAGE_FAIL, _SUCCESS])

    judgment = await _verifier(judge).verify(trace, None)

    assert judgment.evidence["images_forwarded"] == 1
    assert len(judge.calls[-1].image_parts) == 1
    records = judgment.evidence["image_judge_record"]
    assert isinstance(records, list)
    assert [r["Score"] for r in records if isinstance(r, dict)] == [5, 1, 1]


async def test_final_call_uses_the_image_template_when_frames_survive(traces_dir):
    trace = RunTrace(write_trace(traces_dir, steps=1, screenshots=[png_bytes()]))
    judge = FakeJudge(replies=[_KEY_POINTS, _IMAGE_PASS, _SUCCESS])

    judgment = await _verifier(judge).verify(trace, None)

    snapshot = TemplatePrompt.model_validate(judgment.evidence["user_prompt"])
    assert snapshot.ref.path == "verifiers/online_mind2web/webjudge_user.txt"
    # The image template is the one that carries the per-frame reasonings.
    assert "The store list is visible." in judge.calls[-1].user_text


async def test_screenshotless_trace_records_its_degradation(traces_dir):
    """The old failure mode, now visible in the verdict."""
    trace = RunTrace(write_trace(traces_dir, screenshots=[]))
    assert trace.screenshots == []
    judge = FakeJudge(replies=[_KEY_POINTS, _SUCCESS])

    judgment = await _verifier(judge).verify(trace, None)

    evidence = judgment.evidence
    assert evidence["screenshots_found"] == 0
    assert evidence["image_judge_skipped"] is True
    snapshot = TemplatePrompt.model_validate(evidence["user_prompt"])
    assert snapshot.ref.path == "verifiers/online_mind2web/webjudge_user_no_images.txt"
    assert len(judge.calls) == 2  # key points + final; no image judgements


async def test_require_screenshots_turns_degradation_into_an_error(traces_dir):
    trace = RunTrace(write_trace(traces_dir, screenshots=[]))
    judge = FakeJudge(replies=[_KEY_POINTS])

    with pytest.raises(TraceIncompleteError, match="no screenshots"):
        await _verifier(judge, require_screenshots=True).verify(trace, None)


async def test_max_images_caps_what_reaches_the_final_call(traces_dir):
    frames = [png_bytes((i, i, i)) for i in range(6)]
    trace = RunTrace(write_trace(traces_dir, steps=6, screenshots=frames))
    judge = FakeJudge(replies=[_KEY_POINTS, *[_IMAGE_PASS] * 6, _SUCCESS])

    judgment = await _verifier(judge, max_images=2).verify(trace, None)

    assert judgment.evidence["images_forwarded"] == 2
    assert len(judge.calls[-1].image_parts) == 2


# ── Prompt payload ──────────────────────────────────────────────────


async def test_action_history_and_key_points_reach_the_final_prompt(traces_dir):
    trace = RunTrace(write_trace(traces_dir, steps=2, screenshots=[]))
    judge = FakeJudge(replies=[_KEY_POINTS, _SUCCESS])

    judgment = await _verifier(judge).verify(trace, None)

    final = judge.calls[-1].user_text
    assert "1. browser.click({'index': 0})" in final
    assert "2. browser.click({'index': 1})" in final
    assert "Open the store locator" in final
    key_points = judgment.evidence["key_points"]
    assert isinstance(key_points, str) and "Open the store locator" in key_points


async def test_preprocessing_prompts_are_snapshotted_separately(traces_dir):
    trace = RunTrace(write_trace(traces_dir, steps=1, screenshots=[png_bytes()]))
    judge = FakeJudge(replies=[_KEY_POINTS, _IMAGE_PASS, _SUCCESS])

    evidence = (await _verifier(judge).verify(trace, None)).evidence

    additional = evidence["additional_prompts"]
    assert isinstance(additional, dict)
    assert set(additional) == {
        "identify_key_points_system",
        "identify_key_points_user",
        "judge_image_system",
        "judge_image_user",
    }
    system = TextPrompt.model_validate(evidence["system_prompt"])
    assert system.ref.path == "verifiers/online_mind2web/webjudge_system.txt"


async def test_usage_accumulates_across_every_stage(traces_dir):
    trace = RunTrace(write_trace(traces_dir, steps=2, screenshots=_three_frames()[:2]))
    judge = FakeJudge(replies=[_KEY_POINTS, _IMAGE_PASS, _IMAGE_PASS, _SUCCESS])

    judgment = await _verifier(judge).verify(trace, None)

    assert judgment.usage == JudgeUsage(
        calls=4,
        reported_calls=4,
        prompt_tokens=4 * 100,
        completion_tokens=4 * 20,
        cached_tokens=4 * 10,
    )


async def test_usage_does_not_leak_between_traces(traces_dir):
    """One verifier instance judges many traces; billing must not accrue."""
    verifier = _verifier(FakeJudge(replies=[_KEY_POINTS, _SUCCESS]))
    first = RunTrace(write_trace(traces_dir, run_id="a", screenshots=[]))
    second = RunTrace(write_trace(traces_dir, run_id="b", screenshots=[]))

    one = await verifier.verify(first, None)
    two = await verifier.verify(second, None)

    assert one.usage is not None
    assert two.usage is not None
    assert one.usage.prompt_tokens == two.usage.prompt_tokens


async def test_missing_task_text_is_a_loud_error(traces_dir):
    trace = RunTrace(write_trace(traces_dir, task=""))

    with pytest.raises(TraceIncompleteError, match="task text"):
        await _verifier(FakeJudge()).verify(trace, None)


@pytest.mark.parametrize("termination", ["failure", "cancel", "drain_failure"])
async def test_image_judges_finish_before_verifier_cleanup(traces_dir, monkeypatch, termination):
    """A failed/cancelled image batch cannot outlive its owning verification.

    The runner records a failed verification in its report, never raising it,
    and releases the verifier only after ``verify`` has returned.
    """
    trace_dir = write_trace(
        traces_dir, task_id="owned-images", steps=2, screenshots=_three_frames()[:2]
    )
    verifier = OnlineMind2WebVerifier()
    all_started = asyncio.Event()
    parked = asyncio.Event()
    image_tasks: list[asyncio.Task] = []
    finished: list[int] = []
    cleanup_counts: list[int] = []
    failure = ValueError("one image judge failed")

    async def ask(*, images=None, **kwargs):
        if not images:
            return _KEY_POINTS
        current = asyncio.current_task()
        assert current is not None
        index = len(image_tasks)
        image_tasks.append(current)
        if len(image_tasks) == 2:
            all_started.set()
        try:
            await all_started.wait()
            if termination in ("failure", "drain_failure") and index == 0:
                raise failure
            await parked.wait()
            return _IMAGE_PASS
        finally:
            await asyncio.sleep(0)
            finished.append(index)
            if index == 1:
                raise RuntimeError("image request cleanup failed")

    async def close(*, timeout=5.0):
        cleanup_counts.append(len(finished))
        return ()

    monkeypatch.setattr(verifier, "ask", ask)
    monkeypatch.setattr(verifier, "aclose", close)
    if termination == "drain_failure":

        async def broken_drain(calls):
            for call in calls:
                call.cancel()
            await asyncio.gather(*calls, return_exceptions=True)
            raise RuntimeError("drain failed too")

        monkeypatch.setattr(webjudge, "_cancel_and_drain", broken_drain)
    runner = VerificationRunner(verifiers=[verifier])
    operation = asyncio.create_task(runner.run([trace_dir]))
    try:
        await asyncio.wait_for(all_started.wait(), timeout=1)
        if termination == "cancel":
            operation.cancel("stop image batch")
            with pytest.raises(asyncio.CancelledError, match="stop image batch"):
                await operation
        else:
            report = await operation
            [outcome] = report.errors
            assert outcome.error is not None
            assert "ValueError: one image judge failed" in outcome.error
            assert outcome.evidence is not None
            prompts = outcome.evidence["additional_prompts"]
            assert isinstance(prompts, dict) and "judge_image_user" in prompts
            if termination == "drain_failure":
                assert any("drain failed too" in note for note in failure.__notes__)
        assert sorted(finished) == [0, 1]
        assert cleanup_counts == [2]
        assert all(task.done() for task in image_tasks)
    finally:
        for task in [operation, *image_tasks]:
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, *image_tasks, return_exceptions=True)


@pytest.mark.parametrize(
    "termination", ["failure", "cancel", "repeated_cancel", "failure_then_cancel"]
)
async def test_stubborn_image_judge_has_bounded_drain_and_keeps_evidence(
    traces_dir, monkeypatch, caplog, termination
):
    """A request that swallows cancellation cannot hold the original outcome hostage.

    What the image judge left rides on the error either way: a failure's in
    the runner's report, a cancellation's on the cancellation itself, which
    propagates as itself.
    """
    trace_dir = write_trace(
        traces_dir, task_id="stubborn-images", steps=2, screenshots=_three_frames()[:2]
    )
    verifier = OnlineMind2WebVerifier()
    monkeypatch.setattr(webjudge, "_IMAGE_CALL_DRAIN_TIMEOUT_S", 0.02)
    started = asyncio.Event()
    first_finished = asyncio.Event()
    cleanup_started = asyncio.Event()
    release = asyncio.Event()
    image_tasks: list[asyncio.Task] = []
    cleanup_pending: list[bool] = []
    failure = ValueError("first image judge failed")

    async def ask(*, images=None, tally=None, **kwargs):
        assert tally is not None
        if not images:
            tally.add(SimpleNamespace(prompt_tokens=7, completion_tokens=2))
            return _KEY_POINTS
        current = asyncio.current_task()
        assert current is not None
        index = len(image_tasks)
        image_tasks.append(current)
        if len(image_tasks) == 2:
            started.set()
        await started.wait()
        if index == 0:
            tally.add(SimpleNamespace(prompt_tokens=3, completion_tokens=1))
            first_finished.set()
            if termination in ("failure", "failure_then_cancel"):
                raise failure
            return _IMAGE_PASS
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                continue
        tally.add(SimpleNamespace(prompt_tokens=100, completion_tokens=100))
        return _IMAGE_PASS

    async def close(*, timeout=5.0):
        cleanup_pending.append(not image_tasks[1].done())
        return ()

    monkeypatch.setattr(verifier, "ask", ask)
    monkeypatch.setattr(verifier, "aclose", close)
    runner = VerificationRunner(verifiers=[verifier])
    operation = asyncio.create_task(runner.run([trace_dir]))
    try:
        await asyncio.wait_for(first_finished.wait(), timeout=1)
        if termination in ("cancel", "repeated_cancel"):
            operation.cancel("first cancellation")
            if termination == "repeated_cancel":
                asyncio.get_running_loop().call_later(0.005, operation.cancel, "later cancellation")
            with pytest.raises(asyncio.CancelledError, match="first cancellation") as caught:
                await asyncio.wait_for(operation, timeout=1)
            kept = getattr(caught.value, "evidence", None)
            assert kept is not None and kept["image_judge_unfinished"] == [1]
            usage = getattr(caught.value, "usage", None)
            assert usage is not None and usage.prompt_tokens == 10
        elif termination == "failure_then_cancel":
            await asyncio.wait_for(cleanup_started.wait(), timeout=1)
            operation.cancel("late cancellation")
            asyncio.get_running_loop().call_later(
                0.005, operation.cancel, "second late cancellation"
            )
            with pytest.raises(asyncio.CancelledError, match="late cancellation") as caught:
                await asyncio.wait_for(operation, timeout=1)
            assert caught.value.__cause__ is failure
            kept = getattr(caught.value, "evidence", None)
            assert kept is not None and kept["image_judge_failed"] == [
                {"index": 0, "error": "ValueError: first image judge failed"}
            ]
        else:
            report = await asyncio.wait_for(operation, timeout=1)
            [outcome] = report.errors
            assert outcome.error is not None
            assert "ValueError: first image judge failed" in outcome.error
            evidence = outcome.evidence
            assert evidence is not None
            prompts = evidence["additional_prompts"]
            assert isinstance(prompts, dict) and "judge_image_user" in prompts
            assert outcome.usage is not None
            assert outcome.usage.prompt_tokens == 10
            assert evidence["key_points"] == _parse_key_points(_KEY_POINTS)
            assert evidence["image_judge_unfinished"] == [1]
            assert evidence["screenshots_found"] == 2
            assert evidence["image_judge_failed"] == [
                {"index": 0, "error": "ValueError: first image judge failed"}
            ]
        assert cleanup_pending == [True]
        assert len(verifier._image_calls) == 1
        assert "still running" in caplog.text
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*image_tasks, return_exceptions=True), timeout=1)
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
    assert not verifier._image_calls


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
