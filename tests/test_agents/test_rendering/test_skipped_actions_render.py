"""Each agent tells its model which actions of the last step were not executed, and why.

The user's ruling of 2026-10-24 (``docs/architecture_design.md`` §4.1): the
Loop stops a step at its first failed action and records the rest as skipped
(``StepRecord.skipped_actions``). Each agent says so in its own history
format with the same reason (``agents/rendering/skipped_actions.py``), and
writes a skipped action's points on the frame of the step it belongs to, as
it writes every action it shows. A step that skipped nothing reads as before.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.agents.browser_use import BrowserUseDomAgent, BrowserUseDomVisionAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.rendering.skipped_actions import not_executed_lines, skip_reason
from spectragent.agents.skyvern import SkyvernDomAgent, SkyvernDomVisionAgent
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.browser.observations import BrowserMetaObs, ScreenshotObs
from spectragent.core.llm.messages import BaseMessage, ContentPartTextParam
from spectragent.core.loops.coordinate_frames import step_frame
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.action_space import ActionSpace
from spectragent.schemas.observation.fragments import HistoryObs, StepInfoObs, TaskObs
from spectragent.schemas.step_record import StepRecord
from tests._helpers.action_spaces import browser_descriptors, space_for
from tests._helpers.assembly import make_agent
from tests._helpers.fragment_formats import formats_for
from tests._helpers.frames import step_context

_CLICK = Action(name="browser.click", params={"index": 29})
#: The screenshot is 2000x1000 over a 1000x500 viewport: a grid of its pixels
#: doubles a viewport point.
_POINT = Action(name="browser.click_coordinate", params={"coordinate_x": 100, "coordinate_y": 50})
_SCROLL = Action(name="browser.scroll", params={"direction": "down"})


def _observation(history: list[StepRecord]) -> SimpleNamespace:
    return SimpleNamespace(
        task=TaskObs(user_request="Open"),
        platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
        history=HistoryObs(items=history),
        step_info=StepInfoObs(step=2, max_steps=10, today="2026-10-24"),
        dom=SimpleNamespace(element_tree="[29]<button>Live</button>"),
        screenshot=ScreenshotObs(
            image_bytes=b"\x89PNG\r\n\x1a\n",
            image_width=2000,
            image_height=1000,
            viewport_width=1000,
            viewport_height=500,
        ),
        browser_meta=BrowserMetaObs(url="https://f.test/", page_title="F", tabs=[]),
    )


def _prepared(agent_class: type[Any]) -> tuple[Any, ActionSpace]:
    agent = make_agent(agent_class, memory=SimpleMemory(max_messages=20))
    required, optional = agent.negotiation_demand("action")
    wanted = {"click", "click_coordinate", "scroll", "done"} & (required | optional)
    space = space_for(
        agent,
        browser_descriptors(
            *sorted(wanted), fragments=frozenset({"browser.dom", "browser.screenshot"})
        ),
    )
    agent.prepare(
        task="Open",
        action_space=space,
        execution_limits=ExecutionLimits(max_actions_per_step=3),
        **formats_for(agent),
    )
    return agent, space


def _stopped(space: ActionSpace, *, skipped: int = 2) -> StepRecord:
    """Step 0: the click failed; the point click and the scroll after it were skipped."""
    actions = [_CLICK, _POINT, _SCROLL]
    return StepRecord(
        step=0,
        llm_output="reply",
        frame=step_frame(space, _observation([])),
        actions=actions if skipped else actions[:1],
        action_results=[ActionResult(success=False, error="element 29 is covered")],
        skipped_actions=skipped,
    )


def _text(messages: list[BaseMessage]) -> str:
    texts: list[str] = []
    for message in messages:
        if message.role != "user":
            continue
        if isinstance(message.content, str):
            texts.append(message.content)
        else:
            texts.extend(p.text for p in message.content if isinstance(p, ContentPartTextParam))
    return "\n".join(texts)


async def _prompt(agent_class: type[Any], *, skipped: int = 2) -> str:
    agent, space = _prepared(agent_class)
    observation = _observation([_stopped(space, skipped=skipped)])
    messages = await agent.build_react_prompt(
        observation, context=step_context(space, observation, step=1)
    )
    return _text(messages)


def test_the_shared_sentence() -> None:
    record = _stopped(ActionSpace())
    assert skip_reason(record) == "action 1 failed"
    assert not_executed_lines(record, write=lambda action: action.name) == [
        "Action 2 (browser.click_coordinate) was not executed: action 1 failed.",
        "Action 3 (browser.scroll) was not executed: action 1 failed.",
    ]
    assert not_executed_lines(_stopped(ActionSpace(), skipped=0), write=str) == []


@pytest.mark.parametrize("member", [BrowserUseDomAgent, BrowserUseDomVisionAgent])
async def test_browser_use_names_them_after_the_results(member: type[Any]) -> None:
    text = await _prompt(member)
    assert (
        "<results>ok=False element 29 is covered</results>\n"
        "<not_executed>Action 2 (browser.click_coordinate) was not executed: action 1 failed. "
        "Action 3 (browser.scroll) was not executed: action 1 failed.</not_executed>\n"
    ) in text
    if member is BrowserUseDomVisionAgent:
        # The skipped point is on the grid of the step it belongs to.
        assert "browser.click_coordinate({'coordinate_x': 200, 'coordinate_y': 100})" in text


@pytest.mark.parametrize("member", [SkyvernDomAgent, SkyvernDomVisionAgent])
async def test_skyvern_writes_a_line_per_skipped_action(member: type[Any]) -> None:
    text = await _prompt(member)
    lines = text.split("<action_history>\n")[1].split("\n</action_history>")[0].splitlines()
    point = (
        {"coordinate_x": 200, "coordinate_y": 100}
        if member is SkyvernDomVisionAgent
        else {"coordinate_x": 100, "coordinate_y": 50}
    )
    assert [json.loads(line) for line in lines[1:]] == [
        {
            "action": {"name": "browser.click_coordinate", "params": point},
            "not_executed": "action 1 failed",
        },
        {
            "action": {"name": "browser.scroll", "params": {"direction": "down"}},
            "not_executed": "action 1 failed",
        },
    ]


async def test_ui_tars_names_them_apart_from_unreturned_actions() -> None:
    text = await _prompt(UITarsAgent)
    facts = json.loads(text.split("<action_results>\n")[1].split("\n</action_results>")[0])
    assert facts["actions_without_returned_results"] == []
    assert facts["not_executed"] == {
        "actions": ["click(point='(200, 100)')", "scroll(direction='down')"],
        "because": "action 1 failed",
    }


@pytest.mark.parametrize(
    "agent_class",
    [BrowserUseDomVisionAgent, SkyvernDomVisionAgent, UITarsAgent],
)
async def test_a_step_that_skipped_nothing_says_nothing_about_it(agent_class: type[Any]) -> None:
    text = await _prompt(agent_class, skipped=0)
    assert "not_executed" not in text and "not executed" not in text
