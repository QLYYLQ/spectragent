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


class _NoReferenceLoader:
    """An eval loader a task variant must never reach: its rows describe the source tasks."""

    benchmark_name = "online_mind2web"
    subset = "All"

    def get(self, task_id: str) -> EvalReference:
        raise AssertionError(f"the source reference {task_id!r} was looked up for a task variant")


async def test_a_task_variant_is_judged_on_its_own_task_without_a_reference(traces_dir):
    """WebJudge needs no reference (``__needs_reference__ = False``): it judges the variant."""
    import json

    from spectragent.evaluator.base import needs_reference
    from spectragent.evaluator.runner import EvaluationRunner

    variant_task = "Find how long the kettle's warranty lasts"
    source_task = "Put the blue kettle in the cart"
    frames = [png_bytes((40, 40, 40)), png_bytes((90, 10, 10))]
    path = write_trace(
        traces_dir,
        task_id="src/s1/vwarranty/b0",
        run_id="variant-run",
        benchmark="trace_branches",
        task=variant_task,
        steps=2,
        screenshots=frames,
        origins=["restored", "policy"],
    )
    config = path / "config.json"
    raw = json.loads(config.read_text())
    raw["branch"] = {
        "source_run_id": "src",
        "source_trace": "src",
        "source_benchmark": "online_mind2web",
        "source_subset": "All",
        "source_task_id": "task-7",
        "checkpoint_step": 1,
        "branch_index": 0,
        "branch_group_id": "src/s1/vwarranty",
        "task_variant": {
            "variant_id": "warranty",
            "task_text": variant_task,
            "source_task_text": source_task,
            "note": "written by a model",
        },
    }
    config.write_text(json.dumps(raw))
    judge = FakeJudge(replies=[_KEY_POINTS, _IMAGE_PASS, _IMAGE_PASS, _SUCCESS])
    evaluator = _evaluator(judge)
    assert not needs_reference(evaluator)

    report = await EvaluationRunner(evaluator=evaluator, eval_loader=_NoReferenceLoader()).run(
        [path]
    )
    (result,) = report.graded
    assert report.errors == []
    assert variant_task in judge.calls[0].user_text, "the key points come from the variant"
    assert not any(source_task in call.user_text for call in judge.calls)
    assert result.task == variant_task and result.task_id == "src/s1/vwarranty/b0"
    assert result.evaluation_details.benchmark_details["task_variant"] == {
        "variant_id": "warranty",
        "task_text": variant_task,
        "source_task_text": source_task,
        "note": "written by a model",
        "source_task": {"benchmark": "online_mind2web", "subset": "All", "task_id": "task-7"},
        "reference": None,
    }
    assert "source_reference" not in result.evaluation_details.benchmark_details
    assert len(RunTrace(path).evals()) == 1


def test_only_reference_free_judges_declare_it():
    """Undeclared means a reference is needed: the safe reading for a grader."""
    from spectragent.evaluator._registrar import EvaluatorRegistrar
    from spectragent.evaluator.base import needs_reference

    class _Structural:
        evaluator_id = "structural"

    assert needs_reference(_Structural())
    assert not needs_reference(OnlineMind2WebEvaluator())
    for key in ("exact_match", "browsecomp", "lexbench_browser"):
        assert getattr(EvaluatorRegistrar.get(key), "__needs_reference__", True), key
