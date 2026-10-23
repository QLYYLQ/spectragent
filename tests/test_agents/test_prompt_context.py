"""Public prompt/history contracts preserve actual restoration and external provenance."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.agents.browser_use import BrowserUseDomAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.skyvern import SkyvernDomAgent, SkyvernDomVisionAgent
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.llm.messages import BaseMessage, SystemMessage, UserMessage
from spectragent.core.loops.coordinate_frames import step_frame
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.prompt import PromptContribution, ReactPromptContext, RestoredHistory
from spectragent.schemas.restoration import RestoredAction
from spectragent.schemas.step_record import StepRecord
from tests._helpers.action_spaces import space_for
from tests._helpers.assembly import make_agent
from tests._helpers.fragment_formats import formats_for

AGENT_CLASSES = (BrowserUseDomAgent, SkyvernDomAgent, SkyvernDomVisionAgent, UITarsAgent)

# Captured from the original builders before adding the explicit prompt context.
# The complete serialized messages include image bytes, roles and cache markers.
ORIGINAL_PROMPT_DIGESTS = {
    BrowserUseDomAgent: "d89b121aa6319adcc31f2032e14b19a73d0a021459d3ea8248820d06d12a8451",
    # Skyvern split into its two members (2026-10-23): ``dom`` writes the same
    # message as before; ``dom_vision`` attaches the screenshot and states its size.
    SkyvernDomAgent: "bf357a0c812a20adaef9d691207e1a2720d6df5310bfbd7b1df7e26c1a577eb1",
    SkyvernDomVisionAgent: "aed5036b0cea64543d3c401201f50f96938da0fa034978ffbe0a782ddc3e9ead",
    # UI-TARS's platform block now names it a computer-use agent operating a
    # browser environment (the user's 2026-10-20 decision); the rest is unchanged.
    UITarsAgent: "8adf8cbf432c4c669d8a4cded683557fcfcf9b2959954810b0c31d0ed4b9bdec",
}


def observation() -> Any:
    return SimpleNamespace(
        task=SimpleNamespace(user_request="Open the live result", follow_ups=[]),
        platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
        history=SimpleNamespace(items=[]),
        dom=SimpleNamespace(element_tree="[29]<button>Live result</button>"),
        screenshot=ScreenshotObs(
            image_bytes=b"\x89PNG\r\n\x1a\n",
            image_width=2000,
            image_height=1000,
            viewport_width=1000,
            viewport_height=500,
        ),
        browser_meta=SimpleNamespace(url="https://fixture.test/live", page_title="Live"),
    )


def _space(agent: Any) -> ActionSpace:
    keys = {"browser.click", "browser.click_coordinate", "browser.wait", "browser.done"}
    descriptors = [
        action.descriptor(fragments=frozenset({"browser.dom", "browser.screenshot"}))
        for action in BrowserCapability().actions
        if action.action_id in keys
    ]
    return space_for(agent, descriptors)


def prepared(agent_class: type[Any]) -> Any:
    agent = make_agent(agent_class, memory=SimpleMemory(max_messages=20))
    agent.prepare(
        task="Open the live result",
        action_space=_space(agent),
        execution_limits=ExecutionLimits(max_actions_per_step=1),
        **formats_for(agent),
    )
    return agent


def context(agent_class: type[Any], **fields: Any) -> ReactPromptContext:
    """The context a Loop hands an *agent_class* agent for a step on :func:`observation`.

    Its frame included: the frame of the run's framed space over the observation's screenshot.
    """
    space = _space(make_agent(agent_class))
    return ReactPromptContext(frame=step_frame(space, observation()), **fields)


def restored_history(*, coordinate: bool = False, marker: bool | None = None) -> RestoredHistory:
    if coordinate:
        source_action = Action(
            name="browser.click_coordinate", params={"coordinate_x": 2, "coordinate_y": 4}
        )
        actual_action = Action(
            name="browser.click_coordinate", params={"coordinate_x": 14, "coordinate_y": 28}
        )
    else:
        source_action = Action(name="browser.click", params={"index": 3})
        actual_action = Action(name="browser.click", params={"index": 29})
    result = ActionResult(
        success=False,
        error="The live button was disabled",
        extracted_content="actual result text",
        typed_into_sensitive_field=marker,
    )
    return RestoredHistory(
        source_run_id="source-run",
        source_step=4,
        source_reply="old source thought; stale action index 3; this is not valid Agent JSON",
        source_actions=[source_action],
        actions=[actual_action],
        results=[result],
        executions=[
            RestoredAction(
                recorded=source_action,
                executed=actual_action,
                method="identity" if coordinate else "geometry_signature",
                recorded_success=True,
                live_success=False,
                result=result,
            )
        ],
    )


def serialized(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    return [message.model_dump(mode="json") for message in messages]


@pytest.mark.parametrize("agent_class", AGENT_CLASSES)
def test_prompt_and_restored_history_are_explicit_async_slots(agent_class: type[Any]) -> None:
    for name in ("build_react_prompt", "build_restored_history"):
        method = getattr(agent_class, name)
        assert inspect.iscoroutinefunction(method)
        context = inspect.signature(method).parameters["context"]
        assert context.kind is inspect.Parameter.KEYWORD_ONLY
        assert context.default is inspect.Parameter.empty


@pytest.mark.parametrize("agent_class", AGENT_CLASSES)
async def test_no_contribution_preserves_every_message_byte_across_phases(
    agent_class: type[Any],
) -> None:
    agent = prepared(agent_class)
    policy = await agent.build_react_prompt(
        observation(), context=context(agent_class, step=4, phase="policy")
    )
    history = await prepared(agent_class).build_react_prompt(
        observation(), context=context(agent_class, step=4, phase="history", is_branch=True)
    )
    assert serialized(policy) == serialized(history)
    payload = json.dumps(serialized(policy), sort_keys=True, ensure_ascii=False).encode()
    assert hashlib.sha256(payload).hexdigest() == ORIGINAL_PROMPT_DIGESTS[agent_class]
    if agent_class is UITarsAgent:
        assert len(policy) == 2 and isinstance(policy[0], SystemMessage)
    else:
        assert len(policy) == 1
    assert isinstance(policy[-1], UserMessage)


@pytest.mark.parametrize("agent_class", AGENT_CLASSES)
async def test_agent_places_system_and_turn_contributions_without_changing_observation(
    agent_class: type[Any],
) -> None:
    agent = prepared(agent_class)
    plain = await agent.build_react_prompt(
        observation(), context=context(agent_class, step=4, phase="policy")
    )
    contributions = [
        PromptContribution(text="Compare the available options", placement="system"),
        PromptContribution(text="Check the live result first", placement="turn"),
    ]
    turn = context(agent_class, step=4, phase="policy", contributions=contributions)
    shown = await prepared(agent_class).build_react_prompt(observation(), context=turn)
    assert serialized(shown[1:-1]) == serialized(plain)
    assert isinstance(shown[0], SystemMessage)
    assert shown[0].text == "External prompt contribution:\nCompare the available options"
    assert isinstance(shown[-1], UserMessage)
    assert shown[-1].text == "External prompt contribution:\nCheck the live result first"
    assert turn.contributions == contributions


@pytest.mark.parametrize("agent_class", AGENT_CLASSES)
async def test_restored_history_shows_actual_execution_and_labels_source_reply(
    agent_class: type[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = prepared(agent_class)
    history = restored_history(coordinate=agent_class is UITarsAgent)

    def refuse_source_parsing(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("restoration must not parse or invent an Agent reply")

    monkeypatch.setattr(agent, "parse_react_output", refuse_source_parsing)
    messages = await agent.build_restored_history(
        observation(), history=history, context=context(agent_class, step=4, phase="history")
    )
    if agent_class is UITarsAgent:
        assert isinstance(messages[0], SystemMessage)
        assert "<input_platform>" in messages[0].text
        assert all(message.role == "user" for message in messages[1:])
    else:
        assert all(message.role == "user" for message in messages)
    observation_text = next(message.text for message in messages if message.role == "user")
    assert "Live result" in observation_text or "Screenshot: 2000x1000 pixels." in observation_text
    assert "not a new reply or thought" in messages[-1].text
    assert "viewport pixels" not in messages[-1].text
    facts = json.loads(messages[-1].text.split("\n", 1)[1])
    assert facts["source_material"]["run_id"] == "source-run"
    assert facts["source_material"]["recorded_reply"] == history.source_reply
    executed = facts["actual_restored_execution"]
    if agent_class is UITarsAgent:
        # On the restored observation's grid: its 2000x1000 screenshot's pixels
        # over a 1000x500 viewport, so the viewport's (14, 28) is (28, 56).
        assert executed[0]["action"] == "click(point='(28, 56)')"
        assert facts["source_material"]["recorded_actions"] == ["click(point='(4, 8)')"]
    else:
        assert executed[0]["action"] == history.actions[0].model_dump()
    assert executed[0]["result"]["success"] is False
    assert executed[0]["result"]["error"] == "The live button was disabled"
    assert executed[0]["result"]["extracted_content"] == "actual result text"


@pytest.mark.parametrize("agent_class", AGENT_CLASSES)
async def test_restored_history_rejects_missing_or_mismatched_results(
    agent_class: type[Any],
) -> None:
    for update in (
        {"results": []},
        {"actions": [Action(name="browser.click", params={"index": 40})]},
        {"results": [ActionResult(success=True)]},
    ):
        agent = prepared(agent_class)
        history = restored_history(coordinate=agent_class is UITarsAgent).model_copy(update=update)
        with pytest.raises(ValueError, match="restored history"):
            await agent.build_restored_history(
                observation(),
                history=history,
                context=context(agent_class, step=4, phase="history"),
            )


@pytest.mark.parametrize("agent_class", AGENT_CLASSES)
async def test_restored_result_sensitivity_marker_stays_out_of_model_input(
    agent_class: type[Any],
) -> None:
    shown = []
    for marker in (None, False, True):
        messages = await prepared(agent_class).build_restored_history(
            observation(),
            history=restored_history(coordinate=agent_class is UITarsAgent, marker=marker),
            context=context(agent_class, step=4, phase="history"),
        )
        shown.append(serialized(messages))
    assert shown[0] == shown[1] == shown[2]


async def test_skyvern_next_prompt_remembers_actual_retargeted_actions() -> None:
    """The window after a restored step shows what restoration actually ran.

    Skyvern's window is rendered from the Loop's ``history`` fragment, whose
    restored record carries the actual retargeted actions and results
    (``BranchLoop._restored_record``); the source's actions and reply are
    material on ``source_history`` and never enter it.
    """
    agent = prepared(SkyvernDomAgent)
    history = restored_history()
    await agent.build_restored_history(
        observation(), history=history, context=context(SkyvernDomAgent, step=4, phase="history")
    )
    restored = StepRecord(
        step=4,
        origin="restored",
        source_history=history,
        actions=history.actions,
        action_results=history.results,
    )
    next_observation = observation()
    next_observation.history = SimpleNamespace(items=[restored])
    messages = await agent.build_react_prompt(
        next_observation, context=context(SkyvernDomAgent, step=5, phase="policy")
    )
    window = messages[0].text.split("<action_history>\n")[1].split("\n</action_history>")[0]
    (entry,) = [json.loads(line) for line in window.splitlines()]
    assert entry["action"] == {"name": "browser.click", "params": {"index": 29}}
    assert entry["result"]["error"] == "The live button was disabled"
    assert entry["result"]["extracted_content"] == "actual result text"
    assert '"index": 3}' not in messages[0].text
    assert "old source thought" not in messages[0].text


async def test_ui_tars_restored_step_records_and_shows_the_live_coordinate_frame() -> None:
    """The restored execution is written on the restored observation's frame, as recorded.

    The frame is the framework's (``BranchLoop`` records it on the restored
    step); the next step reads it off that record, so UI-TARS keeps no grid.
    """
    agent = prepared(UITarsAgent)
    restored = context(UITarsAgent, step=4, phase="history")
    assert restored.frame is not None and restored.frame.grid is not None
    assert (restored.frame.grid.width, restored.frame.grid.viewport_width) == (2000, 1000)
    messages = await agent.build_restored_history(
        observation(), history=restored_history(coordinate=True), context=restored
    )
    facts = json.loads(messages[-1].text.split("\n", 1)[1])
    assert facts["actual_restored_execution"][0]["action"] == "click(point='(28, 56)')"
    assert not any("grid" in name for name in vars(agent))


@pytest.mark.parametrize("agent_class", AGENT_CLASSES)
async def test_concurrent_runs_keep_contributions_in_their_own_context(
    agent_class: type[Any],
) -> None:
    async def build(label: str) -> list[BaseMessage]:
        turn = context(
            agent_class, step=4, phase="policy", contributions=[PromptContribution(text=label)]
        )
        return await prepared(agent_class).build_react_prompt(observation(), context=turn)

    left, right = await asyncio.gather(
        build("left run instruction"), build("right run instruction")
    )
    assert "left run instruction" in left[-1].text
    assert "right run instruction" not in left[-1].text
    assert "right run instruction" in right[-1].text
    assert "left run instruction" not in right[-1].text
