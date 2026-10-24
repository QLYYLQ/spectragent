"""An action whose params do not fit its ``Params`` goes back to the model, never to the page.

The user's ruling of 2026-10-23: ReAct validates every action a reply would
run against the ``Params`` of the action the guard executes it as
(``core/action_guard.py::params_refusal``); since 2026-10-24 a name the run
does not execute is refused the same way. A misfit is a parse error with a
reason the model can act on, so it takes the existing parse retries
(``parse_retries_per_step``) and, when they are spent, is recorded as the
step's parse failure. The action is not executed and is not turned into a
failed ``ActionResult``.
"""

from __future__ import annotations

import json
from typing import Any

from spectragent.core.action_guard import build_guarded_act
from spectragent.core.llm.messages import UserMessage
from spectragent.core.loops.react import ReactLoop
from spectragent.core.step_recorder import StepRecorder
from spectragent.schemas.action import Action, ActionResult
from tests._helpers.action_spaces import browser_descriptors, space_of


class _Completion:
    def __init__(self, text: str) -> None:
        self.completion = text
        self.usage = None


class _LLM:
    inference_kind = "chat"
    model = "test-model"

    def __init__(self, replies: list[list[dict[str, Any]]]) -> None:
        self._replies = [json.dumps(reply) for reply in replies]
        self.calls = 0

    async def ainvoke(self, messages: Any, **kw: Any) -> Any:
        text = self._replies[min(self.calls, len(self._replies) - 1)]
        self.calls += 1
        return _Completion(text)


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


class _Agent:
    """Reads a reply as ``[{"name": ..., "params": {...}}, ...]``, params as written."""

    __execution_limit_fields__ = frozenset()

    async def build_react_prompt(self, obs: Any, *, context: Any) -> list:
        return [UserMessage(content="the page")]

    def parse_react_output(self, out: Any) -> list[Action]:
        return [Action(name=item["name"], params=item["params"]) for item in json.loads(out)]

    def get_react_output_model(self) -> None:
        return None


class _Env:
    async def observe(self, negotiated: Any) -> Any:
        return object()


class _Negotiated:
    loop_sourced = frozenset()


async def _run(
    replies: list[list[dict[str, Any]]], *, retries: int, max_actions: int = 3
) -> tuple[Any, list[Action], _Memory, StepRecorder]:
    ran: list[Action] = []

    async def execute(action: Action, **_kw: Any) -> ActionResult:
        ran.append(action)
        done = action.name == "browser.done"
        return ActionResult(success=True, is_done=done, extracted_content="42" if done else None)

    descriptors = browser_descriptors("click", "type", "done")
    space, act_fn = build_guarded_act(
        execute, descriptors, bindings=space_of(descriptors).action_bindings
    )
    memory, recorder = _Memory(), StepRecorder()
    loop = ReactLoop(
        max_steps=2,
        max_actions_per_step=max_actions,
        parse_retries_per_step=retries,
        max_consecutive_parse_errors=1,
    )
    result = await loop._execute(
        _Agent(),
        "t",
        ctx={
            "env": _Env(),
            "llm": _LLM(replies),
            "memory": memory,
            "act_fn": act_fn,
            "action_space": space,
            "negotiated_obs": _Negotiated(),
            "step_recorder": recorder,
            "task_id": "t-1",
        },
    )
    return result, ran, memory, recorder


_DONE = {"name": "browser.done", "params": {"extracted_content": "42"}}


def _feedback(memory: _Memory) -> list[str]:
    return [
        str(m.content)
        for m in memory.messages
        if isinstance(m, UserMessage) and "could not be parsed" in str(m.content)
    ]


async def test_a_misfit_is_rewritten_by_the_model_and_never_executed() -> None:
    result, ran, memory, _ = await _run(
        [
            [{"name": "browser.click", "params": {}}],
            [{"name": "browser.click", "params": {"index": 3}}, _DONE],
        ],
        retries=1,
    )
    assert result.agent_done == "done"
    assert [(a.name, a.params) for a in ran] == [
        ("browser.click", {"index": 3}),
        ("browser.done", {"extracted_content": "42"}),
    ]
    (said,) = _feedback(memory)
    assert "action 1, browser.click: index: Field required" in said
    assert "browser.click takes index: int" in said, "the reason says what the action takes"


async def test_a_param_the_action_does_not_declare_is_a_misfit() -> None:
    _, ran, memory, _ = await _run(
        [
            [{"name": "click", "params": {"index": 3, "reasoning": "the cart"}}],
            [_DONE],
        ],
        retries=1,
    )
    assert [a.name for a in ran] == ["browser.done"]
    (said,) = _feedback(memory)
    assert "click: reasoning: Extra inputs are not permitted" in said


async def test_spent_retries_record_a_parse_failure_and_run_nothing() -> None:
    result, ran, _, recorder = await _run(
        [[{"name": "browser.type", "params": {"index": "the box", "text": "x"}}]], retries=1
    )
    assert ran == []
    assert result.agent_done == "error"
    (record,) = recorder.records
    assert record.error_phase == "parse" and record.actions == [] and record.action_results == []
    assert "browser.type: index: Input should be a valid integer" in (record.error or "")


async def test_an_action_past_the_step_budget_is_not_judged() -> None:
    """It would not run: the Loop drops it, and says so, instead of re-asking."""
    _, ran, memory, _ = await _run(
        [[_DONE, {"name": "browser.click", "params": {}}]], retries=0, max_actions=1
    )
    assert [a.name for a in ran] == ["browser.done"]
    assert _feedback(memory) == []


async def test_a_name_this_run_does_not_execute_is_rewritten_by_the_model() -> None:
    """Nothing in the reply runs: the done after the unknown name would assume it happened."""
    result, ran, memory, _ = await _run(
        [[{"name": "browser.fly", "params": {"high": True}}, _DONE], [_DONE]], retries=1
    )
    assert result.agent_done == "done"
    assert ran == [Action(name="browser.done", params={"extracted_content": "42"})]
    (said,) = _feedback(memory)
    assert "action 1, browser.fly is not an action this run executes" in said
    assert "browser.click, browser.done, browser.type" in said, "the reason lists the run's actions"


async def test_an_unknown_name_with_spent_retries_runs_nothing() -> None:
    result, ran, _, recorder = await _run([[_DONE, {"name": "fly", "params": {}}]], retries=0)
    assert ran == []
    (record,) = recorder.records
    assert record.error_phase == "parse" and record.actions == [] and record.action_results == []
    assert "action 2, fly is not an action this run executes" in (record.error or "")
