"""browser_use and UI-TARS place a point read off a screenshot identically.

Both agents let a model click a point on the screenshot it was shown, and
the framework has one answer to where that point is executed:
``spectragent.schemas.observation.geometry`` — scale by ``viewport / image``,
land the far edge on the last viewport pixel, clamp a point off the grid.
UI-TARS converts through it. browser_use still carries its own copy of the
same conversion, pending the move that also gives it a ``coordinate_space``
knob. Until that lands, this is what keeps two copies one policy: the same
point on the same screenshot has to reach ``browser.click_coordinate`` as
the same viewport point from either agent, and as the point the shared
grid computes.

Driven through each agent's public slots (``prepare`` →
``build_react_prompt`` → ``parse_react_output``), not their private
helpers, so renaming a helper does not break it — changing either policy
does. Delete it once browser_use converts through ``point_grid`` too.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.environments.browser import BrowserEnvironment
from spectragent.core.negotiation import extract_wiring
from spectragent.core.negotiation.action import _action_rules
from spectragent.schemas.action import ActionDescriptor
from spectragent.schemas.observation.geometry import point_grid
from tests._helpers.assembly import make_agent, make_env

#: ``(image, viewport, point)``: DPR 1 and 2, on screen, the far edge, and
#: off the grid in each direction.
_CASES: list[tuple[tuple[int, int], tuple[int, int], tuple[int, int]]] = [
    ((1280, 720), (1280, 720), (61, 377)),
    ((2560, 1440), (1280, 720), (1199, 747)),
    ((2560, 1440), (1280, 720), (101, 401)),
    ((2560, 1440), (1280, 720), (2560, 1440)),
    ((2560, 1440), (1280, 720), (2600, 300)),
    ((1280, 720), (1280, 720), (1287, 727)),
    ((1280, 720), (1280, 720), (-3, 5)),
]


def _observation(image: tuple[int, int], viewport: tuple[int, int]) -> Any:
    return SimpleNamespace(
        task=SimpleNamespace(user_request="tick the checkbox", follow_ups=[]),
        screenshot=ScreenshotObs(
            image_bytes=b"\x89PNG\r\n\x1a\n",
            viewport_width=viewport[0],
            viewport_height=viewport[1],
            image_width=image[0],
            image_height=image[1],
        ),
    )


def _negotiated_for(agent_cls: type) -> list[ActionDescriptor]:
    capability = BrowserCapability()
    env = make_env(BrowserEnvironment, {"browser": capability})
    negotiated = _action_rules(
        agent=extract_wiring(agent_cls, "action"),
        env_provides=set(capability.provided_actions),
    )
    return env.get_action_space(active_actions=negotiated.active_actions)


@pytest.fixture(scope="module")
def descriptors() -> dict[str, list[ActionDescriptor]]:
    return {
        "browser_use": _negotiated_for(BrowserUseAgent),
        "ui_tars": _negotiated_for(UITarsAgent),
    }


def _browser_use_point(
    descriptors: list[ActionDescriptor], observation: Any, point: tuple[int, int]
) -> tuple[int, int]:
    agent = make_agent(BrowserUseAgent, memory=SimpleMemory(max_messages=10), use_vision=True)
    agent.prepare(task="t", action_descriptors=descriptors)
    agent.build_react_prompt(observation)
    reply = json.dumps(
        {
            "memory": "m",
            "action": [{"click_coordinate": {"coordinate_x": point[0], "coordinate_y": point[1]}}],
        }
    )
    (action,) = agent.parse_react_output(reply)
    return action.params["coordinate_x"], action.params["coordinate_y"]


def _ui_tars_point(
    descriptors: list[ActionDescriptor], observation: Any, point: tuple[int, int]
) -> tuple[int, int]:
    agent = make_agent(UITarsAgent, memory=SimpleMemory(max_messages=10))
    agent.prepare(task="t", action_descriptors=descriptors)
    agent.build_react_prompt(observation)
    (action,) = agent.parse_react_output(f"Action: click(point='({point[0]}, {point[1]})')")
    return action.params["coordinate_x"], action.params["coordinate_y"]


@pytest.mark.parametrize(("image", "viewport", "point"), _CASES)
def test_both_agents_execute_a_screenshot_point_where_the_shared_grid_puts_it(
    descriptors: dict[str, list[ActionDescriptor]],
    image: tuple[int, int],
    viewport: tuple[int, int],
    point: tuple[int, int],
) -> None:
    observation = _observation(image, viewport)
    grid = point_grid("screenshot_pixels", observation.screenshot)
    assert grid is not None
    expected = grid.to_viewport(*point)

    assert _ui_tars_point(descriptors["ui_tars"], observation, point) == expected
    assert _browser_use_point(descriptors["browser_use"], observation, point) == expected
