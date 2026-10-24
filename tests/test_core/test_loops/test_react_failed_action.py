"""A failed action stops its step: the actions after it are not executed.

The user's ruling of 2026-10-24 (``docs/architecture_design.md`` §4.1): the
later actions of a step were planned assuming the earlier ones worked, so
when one fails (its ``ActionResult.success`` is False, the guard's refusal of
a name outside the run's actions included) ``ReactLoop`` executes none of the
rest. The step is handed over as an ordinary step — no error, the run goes on
— and records the reply it accepted whole: ``actions`` keeps every action,
``action_results`` holds only those that returned, and ``skipped_actions``
counts the rest. Raising still ends the run in failure, and ``done`` still
ends it.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from spectragent.core.action_guard import build_guarded_act
from spectragent.core.loops.react import ReactLoop
from spectragent.core.step_recorder import StepRecorder
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.step_record import StepRecord
from tests._helpers.action_spaces import browser_descriptors, space_of


class _Completion:
    completion = "reply"
    usage = None


class _LLM:
    inference_kind = "chat"
    model = "test-model"

    async def ainvoke(self, messages: Any, **kw: Any) -> Any:
        return _Completion()


class _Memory:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def add_message(self, m: Any) -> None:
        self.messages.append(m)

    def add_messages(self, ms: Any) -> None:
        self.messages.extend(ms)

    def get_context_messages(self) -> list[Any]:
        return list(self.messages)

    def clear(self) -> None:
        self.messages.clear()


class _Env:
    async def observe(self, negotiated: Any) -> Any:
        return object()


class _Negotiated:
    loop_sourced = frozenset()


class _Agent:
    """Replies with the next planned batch; the last one repeats."""

    __execution_limit_fields__ = frozenset()

    def __init__(self, plan: list[list[Action]]) -> None:
        self._plan = plan
        self._i = 0

    async def build_react_prompt(self, obs: Any, *, context: Any) -> list[Any]:
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out: Any) -> list[Action]:
        actions = self._plan[min(self._i, len(self._plan) - 1)]
        self._i += 1
        return list(actions)

    def get_react_output_model(self) -> None:
        return None


def _click(index: int) -> Action:
    return Action(name="browser.click", params={"index": index})


_DONE = Action(name="browser.done", params={"extracted_content": "the answer"})


class _Page:
    """Runs actions; an index in *failing* fails, one in *raising* raises."""

    def __init__(
        self, *, failing: frozenset[int] = frozenset(), raising: frozenset[int] = frozenset()
    ):
        self.failing = failing
        self.raising = raising
        self.ran: list[Action] = []

    async def act(self, action: Action) -> ActionResult:
        self.ran.append(action)
        index = action.params.get("index")
        if index in self.raising:
            raise RuntimeError(f"element {index} vanished mid-click")
        if index in self.failing:
            return ActionResult(success=False, error=f"element {index} is covered")
        if action.name == "browser.done":
            return ActionResult(
                success=True, is_done=True, extracted_content=action.params["extracted_content"]
            )
        return ActionResult(success=True)


async def _run(
    plan: list[list[Action]],
    page: _Page,
    *,
    max_steps: int = 2,
    max_actions: int = 4,
    act_fn: Any = None,
    action_space: ActionSpace | None = None,
) -> tuple[Any, StepRecorder]:
    recorder = StepRecorder()
    loop = ReactLoop(max_steps=max_steps, max_actions_per_step=max_actions)
    result = await loop._execute(
        _Agent(plan),
        "t",
        ctx={
            "env": _Env(),
            "llm": _LLM(),
            "memory": _Memory(),
            "act_fn": act_fn or page.act,
            "action_space": action_space or ActionSpace(),
            "negotiated_obs": _Negotiated(),
            "step_recorder": recorder,
            "task_id": "t-1",
        },
    )
    return result, recorder


async def test_a_failed_action_stops_the_step_and_the_rest_are_not_executed() -> None:
    page = _Page(failing=frozenset({2}))
    result, recorder = await _run([[_click(1), _click(2), _click(3), _click(4)], [_DONE]], page)

    first = recorder.records[0]
    # The page saw the first two; the two after the failed one never reached it.
    assert page.ran[:2] == [_click(1), _click(2)]
    assert _click(3) not in page.ran and _click(4) not in page.ran
    # The reply the step accepted stays whole; only real results are kept.
    assert first.actions == [_click(1), _click(2), _click(3), _click(4)]
    assert [r.success for r in first.action_results] == [True, False]
    assert first.skipped_actions == 2
    assert first.skipped() == [_click(3), _click(4)]
    # Not an error: the step is handed over and the run goes on.
    assert first.error_phase == "" and first.error is None
    assert result.agent_done.value == "done"
    assert len(recorder.records) == 2


async def test_a_failed_last_action_skips_nothing() -> None:
    page = _Page(failing=frozenset({2}))
    _, recorder = await _run([[_click(1), _click(2)], [_DONE]], page)
    first = recorder.records[0]
    assert [r.success for r in first.action_results] == [True, False]
    assert first.skipped_actions == 0 and first.skipped() == []


async def test_a_done_after_a_failed_action_does_not_end_the_run() -> None:
    page = _Page(failing=frozenset({1}))
    result, recorder = await _run([[_click(1), _DONE], [_DONE]], page)
    first, second = recorder.records
    assert first.skipped() == [_DONE]
    assert [a.name for a in page.ran] == ["browser.click", "browser.done"], (
        "the skipped done never ran; the next step's done did"
    )
    assert second.action_results[0].is_done
    assert result.agent_done.value == "done"


async def test_done_still_ends_the_run_and_nothing_after_it_runs() -> None:
    page = _Page()
    result, recorder = await _run([[_click(1), _DONE, _click(2)]], page)
    (record,) = recorder.records
    assert page.ran == [_click(1), _DONE]
    assert result.agent_done.value == "done"
    # Not skipped: an action after ``done`` is not a step the run goes on from.
    assert record.skipped_actions == 0


async def test_an_action_that_raises_still_ends_the_run_in_failure() -> None:
    page = _Page(raising=frozenset({2}))
    result, recorder = await _run([[_click(1), _click(2), _click(3)]], page)
    (record,) = recorder.records
    assert page.ran == [_click(1), _click(2)]
    assert record.error_phase == "act"
    assert record.skipped_actions == 0
    assert result.agent_done.value == "error"


async def test_a_name_the_guard_refuses_counts_as_a_failed_action() -> None:
    """The guard answers a name outside the run's actions with a failed result."""
    page = _Page()
    descriptors = browser_descriptors("click", "done")

    async def execute(action: Action, **_kw: Any) -> ActionResult:
        return await page.act(action)

    space, act_fn = build_guarded_act(
        execute, descriptors, bindings=space_of(descriptors).action_bindings
    )
    fly = Action(name="browser.fly", params={})
    _, recorder = await _run([[fly, _click(1)], [_DONE]], page, act_fn=act_fn, action_space=space)
    first = recorder.records[0]
    assert page.ran[0] == _DONE, "neither the refused name nor the click after it ran"
    assert first.action_results[0].success is False
    assert first.skipped() == [_click(1)]


class TestTheRecordShape:
    def test_skipped_actions_need_a_failed_last_result(self) -> None:
        with pytest.raises(ValidationError, match="need a failed action before them"):
            StepRecord(
                step=0,
                actions=[_click(1), _click(2)],
                action_results=[ActionResult(success=True)],
                skipped_actions=1,
            )

    def test_skipped_actions_are_exactly_the_actions_after_the_last_result(self) -> None:
        with pytest.raises(ValidationError, match="exactly the actions after the last result"):
            StepRecord(
                step=0,
                actions=[_click(1), _click(2), _click(3)],
                action_results=[ActionResult(success=False)],
                skipped_actions=1,
            )

    def test_a_record_without_the_field_skipped_nothing(self) -> None:
        """A step recorded before the ruling reads back as skipping nothing — truthfully."""
        old = {
            "step": 0,
            "actions": [_click(1).model_dump(), _click(2).model_dump()],
            "action_results": [{"success": False}, {"success": True}],
        }
        record = StepRecord.model_validate(old)
        assert record.skipped_actions == 0 and record.skipped() == []
