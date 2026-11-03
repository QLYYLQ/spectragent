"""TraceView over a typed RunTrace.

``RunTrace`` hands back real models now, so the projection every
verifier reads through is doing attribute access, not dict digging.
These are the guarantees that change gives the verifier layer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import TraceConfig
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.eval_reference import EvalReference
from spectragent.schemas.task import SetupTaskText
from spectragent.verifiers.errors import TraceIncompleteError
from spectragent.verifiers.exact_match import ExactMatchVerifier
from spectragent.verifiers.runner import VerificationRunner
from spectragent.verifiers.trace_view import TraceView
from tests._helpers.export_traces import ExportStep
from tests._helpers.export_traces import write_run as write_export_run
from tests._helpers.trace_builder import agent_result, step_record
from tests.test_verifiers.conftest import png_bytes, write_trace


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
                "metrics": {"execution_ms": 1, "steps": 1},
            }
        ),
        "utf-8",
    )
    return trace_dir


class TestTheProjectionReadsModels:
    def test_the_four_things_come_off_the_typed_result(self, traces_dir):
        view = TraceView(RunTrace(write_trace(traces_dir, task="Find it", answer="42", steps=2)))

        assert view.task_text == "Find it"
        assert view.answer == "42"
        assert view.agent_done == "done"
        assert view.action_history == [
            "browser.click({'index': 0})",
            "browser.click({'index': 1})",
        ]
        assert view.step_count == 2

    def test_the_task_is_the_text_the_runs_setup_completed(self, traces_dir):
        """Not the loader's half: what the agent was given (§4.10)."""
        made = SetupTaskText(capability_id="android", text="Find it: the blue kettle")
        wal = WALWriter(traces_dir, "run-made")
        wal.init_run(TraceConfig(task="Find it", task_id="task-0", task_text_setter="android"))
        wal.commit_task_text(made)
        wal.commit_step(0, step_record(0))
        wal.write_result(agent_result(task_id="task-0", task=made.text))

        assert TraceView(RunTrace(wal.run_dir)).task_text == made.text

    def test_metrics_are_never_absent(self, traces_dir):
        """``AgentMetrics`` is required on ``AgentResult``, so this is total.

        It used to be ``AgentMetrics | None`` because the projection was
        rebuilding it from a dict that might not have carried the key.
        """
        view = TraceView(RunTrace(write_trace(traces_dir, execution_ms=777)))

        assert view.metrics.execution_ms == 777
        assert view.execution_ms == 777

    def test_metrics_cannot_be_edited_through_the_view(self, traces_dir):
        """Returning the trace's own object would let one verifier's
        bookkeeping show up in the next reader's numbers.
        """
        trace = RunTrace(write_trace(traces_dir, total_tokens=90))
        view = TraceView(trace)

        assert view.metrics.usage is not None
        view.metrics.usage.total_tokens = 999_999

        assert trace.result is not None
        assert trace.result.metrics.usage is not None
        assert trace.result.metrics.usage.total_tokens == 90


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

    async def test_a_legacy_trace_costs_the_pass_one_entry_not_the_run(self, traces_dir):
        """The runner must survive it, and say what it survived.

        A schema failure and a judge outage both leave the task unjudged,
        but only one of them can be fixed by running the judging pass again
        — so the recorded reason has to name which happened.
        """
        good = write_trace(traces_dir, run_id="good", task_id="t-1", answer="42")
        legacy = _legacy_trace(traces_dir, task_id="t-old")

        report = await VerificationRunner(
            verifiers=[ExactMatchVerifier()],
            reference_loader=_Loader({"t-1": "42", "t-old": "42"}),  # type: ignore[arg-type]
        ).run([good, legacy])

        judged = {o.task_id for o in report.outcomes if o.verdict is not None}
        assert judged == {"t-1"}

        assert [o.trace_dir for o in report.errors] == [legacy]
        reason = report.errors[0].error or ""
        assert "TraceSchemaError" in reason
        assert "predates AgentResult schema" in reason


class TestOrigins:
    """A branch trace's restored prefix and a terminal frame, kept apart (§4.11)."""

    def _branch_trace(self, traces_dir: Path) -> TraceView:
        frames = [png_bytes((10 * i, 20, 30)) for i in range(4)]
        return TraceView(
            RunTrace(
                write_trace(
                    traces_dir,
                    run_id="branch",
                    steps=4,
                    screenshots=frames,
                    origins=["restored", "restored", "policy", "terminal"],
                )
            )
        )

    def test_action_history_is_the_runs_own_agent(self, traces_dir):
        view = self._branch_trace(traces_dir)
        assert view.action_history == ["browser.click({'index': 2})"]

    def test_restored_actions_are_listed_apart_and_labelled(self, traces_dir):
        view = self._branch_trace(traces_dir)
        assert view.restored_action_history == [
            "[restored] browser.click({'index': 0})",
            "[restored] browser.click({'index': 1})",
        ]

    def test_screenshots_include_every_origin(self, traces_dir):
        view = self._branch_trace(traces_dir)
        assert [path.name for path in view.screenshots] == [
            f"{step:03d}_browser.screenshot.png" for step in range(4)
        ]

    def test_step_count_is_policy_steps(self, traces_dir):
        assert self._branch_trace(traces_dir).step_count == 1

    def test_a_plain_trace_has_no_restored_history(self, traces_dir):
        view = TraceView(RunTrace(write_trace(traces_dir, steps=2)))
        assert view.restored_action_history == []
        assert view.action_history == [
            "browser.click({'index': 0})",
            "browser.click({'index': 1})",
        ]

    def test_a_terminal_step_adds_no_action(self, traces_dir):
        view = TraceView(
            RunTrace(write_trace(traces_dir, steps=3, origins=["policy", "policy", "terminal"]))
        )
        assert view.action_history == [
            "browser.click({'index': 0})",
            "browser.click({'index': 1})",
        ]
        assert view.step_count == 2


def test_an_action_the_step_skipped_is_not_in_the_action_history(tmp_path: Path) -> None:
    """The Loop stopped the step at the failed click; the judge reads only what ran."""

    def click(index: int) -> Action:
        return Action(name="browser.click", params={"index": index})

    run = write_export_run(
        tmp_path,
        "stopped",
        [
            ExportStep(
                actions=[click(1), click(2)],
                results=[ActionResult(success=False, error="covered")],
                skipped_actions=1,
            ),
            ExportStep(actions=[click(3)]),
        ],
    )
    assert TraceView(RunTrace(run)).action_history == [
        "browser.click({'index': 1})",
        "browser.click({'index': 3})",
    ]
