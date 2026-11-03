"""WebJudge on a terminal frame and on a branch run's restored prefix.

``docs/architecture_design.md`` §4.11: ``TraceView.screenshots`` keeps every
committed frame — a restored step's and the terminal observation's (the
state after the last action) included — and the final judgement lists the
restored prefix, labelled ``[restored]``, before the actions the run's own
agent took. A trace with neither reads exactly as before
(``test_online_mind2web_verifier.py`` pins that byte for byte).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from spectragent.benchmarks.browsecomp import BrowseCompVerifier
from spectragent.benchmarks.lexbench_browser import LexBenchBrowserVerifier
from spectragent.benchmarks.online_mind2web import OnlineMind2WebVerifier
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.eval_reference import EvalReference
from spectragent.verifiers import VerifierBase
from spectragent.verifiers.exact_match import ExactMatchVerifier
from spectragent.verifiers.runner import VerificationRunner
from tests.test_verifiers.conftest import FakeJudge, png_bytes, write_trace

_KEY_POINTS = "**Key Points**:\n1. Open the store locator\n2. Set a home store"
_IMAGE_PASS = "**Reasoning**: The store list is visible.\n\n**Score**: 5"
_SUCCESS = "Thoughts: All key points met.\nStatus: success"


@pytest.fixture
def traces_dir(tmp_path: Path) -> Path:
    return tmp_path / "traces"


def _verifier(judge: FakeJudge) -> OnlineMind2WebVerifier:
    verifier = OnlineMind2WebVerifier()
    verifier.llm = judge  # type: ignore[assignment]
    return verifier


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
    judgment = await _verifier(judge).verify(trace, None)

    # The frame after the last action is one of the judged frames, and it
    # reaches the final call.
    assert frames[2] in _image_payloads(judge)
    final = judge.calls[-1]
    assert len(final.image_parts) == 3
    assert judgment.evidence["screenshots_found"] == 3
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
    await _verifier(judge).verify(trace, None)

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
    """WebJudge needs no reference (``__needs__`` names none): it judges the variant."""
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
    verifier = _verifier(judge)
    assert "reference" not in verifier.needs

    report = await VerificationRunner(
        verifiers=[verifier],
        reference_loader=_NoReferenceLoader(),  # type: ignore[arg-type]
    ).run([path])
    assert report.errors == []
    (outcome,) = report.outcomes
    assert outcome.task_id == "src/s1/vwarranty/b0"
    assert variant_task in judge.calls[0].user_text, "the key points come from the variant"
    assert not any(source_task in call.user_text for call in judge.calls)
    verdicts = RunTrace(path).verdicts()
    assert list(verdicts) == [verifier.identity().slug]
    verdict = verdicts[verifier.identity().slug]
    assert verdict.evidence["task"] == variant_task
    assert verdict.evidence["task_variant"] == {
        "variant_id": "warranty",
        "task_text": variant_task,
        "source_task_text": source_task,
        "note": "written by a model",
        "source_task": {"benchmark": "online_mind2web", "subset": "All", "task_id": "task-7"},
        "reference": None,
    }
    assert "source_reference" not in verdict.evidence


def test_only_reference_free_verifiers_declare_it():
    """Undeclared means a reference is needed: the safe reading for a verifier."""
    assert "reference" in VerifierBase.__needs__
    assert "reference" not in OnlineMind2WebVerifier().needs
    for cls in (ExactMatchVerifier, BrowseCompVerifier, LexBenchBrowserVerifier):
        assert "reference" in cls.__needs__, cls.__registry_key__
