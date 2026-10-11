"""WebJudge on the traces M31 adds: a terminal frame, and a branch run's restored prefix.

``docs/architecture_design.md`` §4.11: ``TraceView.screenshots`` keeps every
committed frame — a restored step's and the terminal observation's (the
state after the last action) included — and the final judgement lists the
restored prefix, labelled ``[restored]``, before the actions the run's own
agent took. A trace with neither reads exactly as before
(``test_online_mind2web.py`` pins that byte for byte).
"""

from __future__ import annotations

import base64

from spectragent.core.trace.reader import RunTrace
from spectragent.evaluator import OnlineMind2WebEvaluator
from spectragent.schemas.eval_reference import EvalReference
from tests.test_evaluator.conftest import FakeJudge, png_bytes, write_trace

_KEY_POINTS = "**Key Points**:\n1. Open the store locator\n2. Set a home store"
_IMAGE_PASS = "**Reasoning**: The store list is visible.\n\n**Score**: 5"
_SUCCESS = "Thoughts: All key points met.\nStatus: success"


def _evaluator(judge: FakeJudge) -> OnlineMind2WebEvaluator:
    evaluator = OnlineMind2WebEvaluator()
    evaluator.llm = judge  # type: ignore[assignment]
    return evaluator


def _reference(task_id: str) -> EvalReference:
    return EvalReference(task_id=task_id, benchmark="online_mind2web")


def _image_payloads(judge: FakeJudge) -> list[bytes]:
    """The PNG bytes of every per-frame image call, in no particular order."""
    payloads = []
    for call in judge.calls:
        if len(call.image_parts) != 1:
            continue
        url = call.image_parts[0].image_url.url
        payloads.append(base64.b64decode(url.split(",", 1)[1]))
    return payloads


async def test_terminal_frame_is_judged(traces_dir):
    frames = [png_bytes((10, 20, 30)), png_bytes((200, 10, 10)), png_bytes((5, 200, 5))]
    trace = RunTrace(
        write_trace(
            traces_dir,
            task_id="t-terminal",
            steps=3,
            screenshots=frames,
            origins=["policy", "policy", "terminal"],
        )
    )
    judge = FakeJudge(replies=[_KEY_POINTS, _IMAGE_PASS, _IMAGE_PASS, _IMAGE_PASS, _SUCCESS])
    result = await _evaluator(judge).evaluate(trace, _reference("t-terminal"))

    # The frame after the last action is one of the judged frames, and it
    # reaches the final call.
    assert frames[2] in _image_payloads(judge)
    final = judge.calls[-1]
    assert len(final.image_parts) == 3
    details = result.evaluation_details.benchmark_details
    assert details["screenshots_found"] == 3
    # Its step took no action, so the action list is the policy steps'.
    assert "1. browser.click({'index': 0})\n2. browser.click({'index': 1})" in final.user_text
    assert "browser.click({'index': 2})" not in final.user_text
    assert "[restored]" not in final.user_text


async def test_branch_trace_labels_restored_actions(traces_dir):
    frames = [png_bytes((10 * i + 1, 20 + i, 30)) for i in range(4)]
    trace = RunTrace(
        write_trace(
            traces_dir,
            task_id="src/s2/b0",
            run_id="branch-run",
            steps=4,
            screenshots=frames,
            origins=["restored", "restored", "policy", "terminal"],
        )
    )
    judge = FakeJudge(replies=[_KEY_POINTS, *([_IMAGE_PASS] * 4), _SUCCESS])
    await _evaluator(judge).evaluate(trace, _reference("src/s2/b0"))

    final = judge.calls[-1]
    assert (
        "1. [restored] browser.click({'index': 0})\n"
        "2. [restored] browser.click({'index': 1})\n"
        "3. browser.click({'index': 2})"
    ) in final.user_text
    # Every frame is judged: the restored steps', the policy step's and the
    # terminal one.
    assert sorted(_image_payloads(judge)) == sorted(frames)
    assert len(final.image_parts) == 4
