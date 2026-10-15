"""Terminal controls own the task and wait for cancellation cleanup."""

import asyncio
import logging

import pytest
from textual.widgets import DataTable, RichLog, Static, TabbedContent

from spectragent.monitoring import ProgressState
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.prompt import RestoredHistory
from spectragent.schemas.restoration import BranchProvenance, RestorationCheck, RestoredAction
from spectragent.schemas.step_record import StepRecord
from spectragent.tui.app import MonitorApp


async def test_progress_is_rendered_and_return_value_survives_closure():
    state = ProgressState()

    async def operation():
        state.set_tasks(["one"])
        state.start_task("one")
        state.run_started(task_id="one", agent_id="demo", llm_model="test")
        state.finish_task("one", {"agent_done": "done", "trace_status": "complete"})
        return 17

    app = MonitorApp(operation, state)
    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        assert app.query_one("#tasks", DataTable).row_count == 1
        assert "FINISHED" in str(app.query_one("#summary", Static).render())
        await pilot.press("q")
    assert app.operation_result() == 17


@pytest.mark.parametrize("close", ["keyboard", "programmatic"])
async def test_close_cancels_once_and_waits_for_environment_cleanup(close):
    started, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))

    async def operation():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            cleaned.set()

    app = MonitorApp(operation, ProgressState())
    async with app.run_test(size=(100, 35)) as pilot:
        await started.wait()
        if close == "keyboard":
            await pilot.press("ctrl+c")
        else:
            app.exit()
        await cleaning.wait()
        assert not cleaned.is_set()
        assert app.operation is not None
        assert not app.operation.done()
        # A second key must not interrupt the runtime's close-out.
        await pilot.press("ctrl+c")
        release.set()
        await cleaned.wait()
        await pilot.pause()
    assert app.operation is not None and app.operation.done()
    with pytest.raises(asyncio.CancelledError):
        app.operation_result()


async def test_operation_failure_is_not_changed_by_display():
    failure = ValueError("bad configuration")

    async def operation():
        raise failure

    app = MonitorApp(operation, ProgressState())
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q")
    with pytest.raises(ValueError) as caught:
        app.operation_result()
    assert caught.value is failure


async def test_selected_step_shows_action_result_and_log_handler_is_removed():
    state = ProgressState()
    original_handlers = list(logging.getLogger().handlers)

    async def operation():
        state.configure(max_steps=9)
        state.recorded(
            StepRecord(
                step=0,
                actions=[Action(name="browser.click", params={"index": 5})],
                action_results=[ActionResult(success=False, error="element disappeared")],
                error="action failed",
            )
        )
        logging.getLogger("experiment.example").warning("direct logging is visible")

    app = MonitorApp(operation, state)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        assert "policy steps 1 · configured ceiling 9" in str(
            app.query_one("#summary", Static).render()
        )
        app.query_one("#steps", DataTable).focus()
        await pilot.press("enter")
        await pilot.pause()
        assert app.query_one("#views", TabbedContent).active == "detail_view"
        text = "\n".join(line.text for line in app.query_one("#details", RichLog).lines)
        assert "element disappeared" in text
        assert '"index": 5' in text
        # Activate the pane so RichLog has a width and flushes deferred lines.
        await pilot.click(app.query_one("#views", TabbedContent).get_tab("log_view"))
        await pilot.pause()
        text = "\n".join(line.text for line in app.query_one("#events", RichLog).lines)
        assert "direct logging is visible" in text
        await pilot.press("q")
    assert logging.getLogger().handlers == original_handlers


async def test_inspect_trace_opens_only_the_committed_result(tmp_path):
    from tests._helpers.trace_builder import build_reference_trace

    reference = build_reference_trace(tmp_path)
    state = ProgressState()

    async def operation():
        state.set_trace(str(reference.run_dir))

    app = MonitorApp(operation, state)
    async with app.run_test(size=(110, 40)) as pilot:
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        text = "\n".join(line.text for line in app.query_one("#details", RichLog).lines)
        assert '"trace_status": "complete"' in text
        assert '"agent_done": "timeout"' in text
        await pilot.press("q")


@pytest.mark.parametrize("reason", ["token_limit", "vetoed", "completed_with_failures"])
async def test_ui_preserves_scheduler_stop_reason_and_duplicate_task_rows(reason):
    state = ProgressState()

    async def operation():
        state.set_tasks(["same", "same"])
        state.start_task("same")
        state.finish_task("same", {"agent_done": "done"})
        state.finish(reason)
        return 4

    app = MonitorApp(operation, state)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert state.snapshot()["status"] == reason
        assert app.query_one("#tasks", DataTable).row_count == 2
        await pilot.press("q")
    assert app.operation_result() == 4


async def test_branch_ceiling_and_source_failures_are_explicit():
    state = ProgressState()
    check = RestorationCheck(
        step=0,
        verdict="warn",
        reason="action_failed",
        reasons=["action_failed"],
        actions=[
            RestoredAction(
                recorded=Action(name="browser.click", params={"index": 1}),
                executed=Action(name="browser.click", params={"index": 9}),
                method="geometry_signature",
                recorded_success=True,
                live_success=None,
                live_error="restored element disappeared",
            )
        ],
    )

    async def operation():
        state.run_started(
            loop_id="branch",
            max_steps=30,
            continue_steps=0,
            branch=BranchProvenance(
                source_run_id="source-run",
                source_trace="source",
                checkpoint_step=1,
                branch_index=0,
                branch_group_id="source-run/s1",
            ),
        )
        state.recorded(
            StepRecord(
                step=0,
                origin="restored",
                actions=[],
                action_results=[],
                source_history=RestoredHistory(
                    source_run_id="source-run",
                    source_step=0,
                    source_reply="source reply",
                    source_actions=[Action(name="browser.click", params={"index": 1})],
                ),
                restoration=check,
            )
        )
        state.recorded(StepRecord(step=1, origin="policy"))
        state.recorded(StepRecord(step=2, origin="terminal"))

    app = MonitorApp(operation, state)
    async with app.run_test(size=(125, 40)) as pilot:
        await pilot.pause()
        text = str(app.query_one("#summary", Static).render())
        assert "policy steps 1 · configured ceiling 30 · continue_steps 0" in text
        assert "1/30" not in text
        assert "source-run · checkpoint 1" in text
        table = app.query_one("#steps", DataTable)
        assert "source: browser.click; actual: browser.click (failed)" in str(
            table.get_row_at(0)[2]
        )
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
        text = "\n".join(line.text for line in app.query_one("#details", RichLog).lines)
        assert '"actual_action_results"' in text
        assert '"source_reply": "source reply"' in text
        assert '"actual_restoration_actions"' in text
        assert '"live_success": null' in text
        assert "restored element disappeared" in text
        await pilot.press("q")
