"""UI-TARS memory receives stable context and complete execution feedback."""

import json
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.llm.messages import AssistantMessage, ContentPartTextParam, SystemMessage
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation.fragments import HistoryObs
from spectragent.schemas.prompt import ReactPromptContext
from spectragent.schemas.step_record import StepRecord
from tests._helpers.branch_browser import TASK, FakeBrowserCap, drive, source_loop


def text(messages: list[Any]) -> str:
    parts = []
    for message in messages:
        if isinstance(message.content, str):
            parts.append(message.content)
        else:
            parts.extend(p.text for p in message.content if isinstance(p, ContentPartTextParam))
    return "\n".join(parts)


def feedback(messages: list[Any]) -> dict[str, Any]:
    value = text(messages).split("<action_results>\n")[-1].split("\n</action_results>")[0]
    return json.loads(value)


def observation(*, platform: str = "macos", history: list[StepRecord] | None = None) -> Any:
    return SimpleNamespace(
        platform=SimpleNamespace(operating_system=platform, fragment_id="browser.platform"),
        history=HistoryObs(items=history or []),
        screenshot=ScreenshotObs(
            image_bytes=b"\x89PNG\r\n\x1a\n",
            image_width=1280,
            image_height=720,
            viewport_width=1280,
            viewport_height=720,
        ),
    )


def seed(agent: UITarsAgent, task: str = TASK):
    return agent.prepare(
        task=task, action_descriptors=[], execution_limits=ExecutionLimits(max_actions_per_step=4)
    )


async def test_runtime_feeds_all_four_results_to_the_next_decision(tmp_path, monkeypatch) -> None:
    original = FakeBrowserCap.execute_action

    async def mixed_results(self: FakeBrowserCap, action: Action) -> ActionResult:
        result = await original(self, action)
        if action.name == "browser.click_coordinate":
            if self.clicks == 2:
                return ActionResult(success=False, error="second target rejected the click")
            if self.clicks == 3:
                return ActionResult(
                    success=True, extracted_content="third result", typed_into_sensitive_field=True
                )
        return result

    monkeypatch.setattr(FakeBrowserCap, "execute_action", mixed_results)
    loop = source_loop()
    loop.max_actions_per_step = 4
    source = await drive(
        tmp_path,
        kind="ui_tars",
        memory="simple",
        loop=loop,
        replies=[
            "No Action line: retry this observation.",
            "\n".join(f"Action: click(point='({x},20)')" for x in [10, 20, 30, 40]),
            "Action: finished(content='finished after reading all four results')",
        ],
        run_id="feedback",
    )
    assert source.result is not None and source.result.error is None
    assert source.run_dir is not None
    trace = RunTrace(source.run_dir)
    assert len(source.teacher.prompts) == 3
    assert len(trace.step_records) == 2  # The retry reuses the first observation.
    assert trace.config.negotiation is not None
    assert "loop.history" in trace.config.negotiation.required_fragments
    assert "browser.dom" not in trace.config.negotiation.active_fragments
    for prompt in source.teacher.prompts:
        assert text(prompt).count(TASK) == 1
        assert text(prompt).count("<input_platform>") == 1
        assert "typed_into_sensitive_field" not in text(prompt)
    actual = feedback(source.teacher.prompts[-1])
    returned = actual["returned_results"]
    assert len(returned) == 4
    assert [entry["result"]["success"] for entry in returned] == [True, False, True, True]
    assert returned[1]["result"]["error"] == "second target rejected the click"
    assert returned[2]["result"]["extracted_content"] == "third result"
    recorded = trace.step(0)
    assert recorded is not None
    # The trace keeps viewport units (the 64x36 screenshot depicts 1280x720);
    # the feedback writes each action as the call the model wrote, on its grid.
    assert [(a.params["coordinate_x"], a.params["coordinate_y"]) for a in recorded.actions] == [
        (200, 400),
        (400, 400),
        (600, 400),
        (800, 400),
    ]
    assert [entry["action"] for entry in returned] == [
        f"click(point='({x}, 20)')" for x in [10, 20, 30, 40]
    ]
    assert recorded.action_results[2].typed_into_sensitive_field is True


async def test_first_observation_declares_platform_even_at_nonzero_source_step() -> None:
    agent = UITarsAgent()
    memory = SimpleMemory(max_messages=4)
    memory.add_messages(seed(agent))
    first = await agent.build_react_prompt(
        observation(), context=ReactPromptContext(step=7, phase="history")
    )
    assert isinstance(first[0], SystemMessage)
    memory.add_messages(first)
    memory.add_message(AssistantMessage(content="Action: wait()"))
    second = await agent.build_react_prompt(
        observation(), context=ReactPromptContext(step=8, phase="policy")
    )
    assert not any(isinstance(message, SystemMessage) for message in second)
    memory.add_messages(second)
    retained = text(memory.get_context_messages())
    assert retained.count(TASK) == 1
    assert retained.count("<input_platform>") == 1
    assert "Step: 9" in retained
    assert "Task:" not in text(second)


async def test_prepare_resets_platform_but_one_execution_cannot_change_platform() -> None:
    agent = UITarsAgent()
    seed(agent)
    await agent.build_react_prompt(
        observation(), context=ReactPromptContext(step=0, phase="policy")
    )
    with pytest.raises(ValueError, match="platform changed within an execution"):
        await agent.build_react_prompt(
            observation(platform="windows"), context=ReactPromptContext(step=1, phase="policy")
        )
    seed(agent, "a new task")
    messages = await agent.build_react_prompt(
        observation(platform="windows"), context=ReactPromptContext(step=0, phase="policy")
    )
    assert "Windows (windows)" in text(messages)
    assert "macOS" not in text(messages)


async def test_partial_batch_does_not_claim_missing_results_or_repeat_old_batches() -> None:
    before = StepRecord(step=2, error_phase="parse", error="older error already in memory")
    actions = [
        Action(name="browser.click_coordinate", params={"coordinate_x": x, "coordinate_y": 9})
        for x in [1, 2, 3]
    ]
    latest = StepRecord(
        step=3,
        actions=actions,
        action_results=[ActionResult(success=True)],
        truncated_actions=2,
        error_phase="act",
        error="second action raised before returning",
    )
    agent = UITarsAgent()
    # Step 3's prompt is built first: its batch is written on that step's grid.
    await agent.build_react_prompt(
        observation(history=[before]), context=ReactPromptContext(step=3, phase="policy")
    )
    messages = await agent.build_react_prompt(
        observation(history=[before, latest]), context=ReactPromptContext(step=4, phase="policy")
    )
    actual = feedback(messages)
    assert len(actual["returned_results"]) == 1
    assert actual["actions_without_returned_results"] == [
        "click(point='(2, 9)')",
        "click(point='(3, 9)')",
    ]
    assert actual["discarded_action_count"] == 2
    assert actual["error"] == latest.error
    assert "older error" not in text(messages)


async def test_feedback_refuses_results_without_corresponding_actions() -> None:
    invalid = StepRecord(step=0, action_results=[ActionResult(success=True)])
    with pytest.raises(ValueError, match="more results than actions"):
        await UITarsAgent().build_react_prompt(
            observation(history=[invalid]), context=ReactPromptContext(step=1, phase="policy")
        )
