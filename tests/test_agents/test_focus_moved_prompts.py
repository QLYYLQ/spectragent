"""A focus move reaches the trace, never a prompt (§4.14 "页面新开的标签页").

When the browser follows a tab the page opened, every fragment of the next
capture carries ``focus_moved``. The agents render what the page shows — the
screenshot, the URL, the tab list, the DOM — and nothing of that record, so a
prompt changes only because the observation shows another page, never because
of the record itself. UI-TARS's no-move baseline also includes the required
measured platform fact, pinned below by its digest.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from spectragent.agents.browser_use import BrowserUseAgent
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.skyvern import SkyvernAgent
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.observations import (
    BrowserMetaObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.core.capabilities.browser.tabs import FocusMove
from spectragent.core.loops._loop_fragments import loop_fragment_builders
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.negotiation import negotiate_action
from spectragent.schemas.observation._registrar import fragment_field_name
from tests._helpers.action_spaces import space_for
from tests._helpers.assembly import make_agent
from tests._helpers.fragment_formats import formats_for
from tests._helpers.frames import step_context

_AGENTS: dict[str, type[Any]] = {
    "browser_use": BrowserUseAgent,
    "skyvern": SkyvernAgent,
    "ui_tars": UITarsAgent,
}
_MOVE = FocusMove(reason="new_tab", from_tab="OPENER", to_tab="FOLLOWED")
#: sha256 of UI-TARS's messages (system prompt, then this step's) for :func:`_observation`
#: with no move and the measured macOS platform. A deliberate change to
#: UI-TARS's prompt updates it; nothing in this package may.
#: Updated 2026-10-20: the platform block names a computer-use agent and its environment.
#: Updated 2026-10-22: the system prompt says feedback points are on the model's own grid
#: (the only difference: the old digest comes back with the old sentence put back).
#: Updated 2026-10-23: ``finished``'s ``executes:`` line no longer lists ``success``, which
#: ``finished()`` cannot set (the only difference: ``, success: bool (optional)`` removed).
UI_TARS_PROMPT_SHA256 = "8a843a077b3faeccdb70df6b0a4858ac97cb27690015672ab0b340f0f7891181"


def _png(width: int = 1280, height: int = 720) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


def _observation(move: FocusMove | None) -> Any:
    """One step's observation of a tab, as the capture serves it; only the move varies."""
    builders = loop_fragment_builders(
        task="read the article",
        step=1,
        max_steps=10,
        today="2026-10-14",
        history=[],
        prev_step=None,
    )
    tabs = [
        {"tab_id": "OPENER", "title": "News", "url": "https://news.test/", "is_active": False},
        {
            "tab_id": "FOLLOWED",
            "title": "Article",
            "url": "https://news.test/a/1",
            "is_active": True,
        },
    ]
    return SimpleNamespace(
        platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
        **{fragment_field_name(key): build() for key, build in builders.items()},
        screenshot=ScreenshotObs(
            image_bytes=_png(),
            image_width=1280,
            image_height=720,
            viewport_width=1280,
            viewport_height=720,
            focus_moved=move,
        ),
        dom=DOMObs(
            element_tree=SimpleNamespace(serialized_dom="[7]<a href=/a/2 >Next article</a>"),
            focus_moved=move,
        ),
        browser_meta=BrowserMetaObs(
            url="https://news.test/a/1", page_title="Article", tabs=tabs, focus_moved=move
        ),
    )


class _ActionsOnlyEnv:
    def __init__(self, capability: Any) -> None:
        self.provided_actions = capability.provided_actions


def _prepared(agent_id: str) -> Any:
    """A fresh agent, prepared with the actions a standard browser negotiates for it."""
    agent = make_agent(_AGENTS[agent_id], memory=SimpleMemory(max_messages=10))
    capability = BrowserCapability()
    negotiated = negotiate_action(agent, _ActionsOnlyEnv(capability))
    descriptors = [
        a.descriptor() for a in capability.actions if a.action_id in negotiated.active_actions
    ]
    space = space_for(agent, descriptors)
    seed = agent.prepare(
        task="read the article",
        action_space=space,
        execution_limits=ExecutionLimits(max_actions_per_step=1),
        **formats_for(agent),
    )
    return agent, seed, space


async def _messages(agent_id: str, move: FocusMove | None) -> list[dict[str, Any]]:
    agent, seed, space = _prepared(agent_id)
    observation = _observation(move)
    step = await agent.build_react_prompt(observation, context=step_context(space, observation))
    return [message.model_dump() for message in [*seed, *step]]


@pytest.mark.parametrize("agent_id", sorted(_AGENTS))
async def test_a_focus_move_never_changes_a_prompt(agent_id: str) -> None:
    assert (await _messages(agent_id, _MOVE)) == (await _messages(agent_id, None))


async def test_the_tab_list_the_comparison_relies_on_is_rendered() -> None:
    """BrowserUse shows the URL and the tabs: the comparison above is not vacuous."""
    text = repr(await _messages("browser_use", _MOVE))
    assert "https://news.test/a/1" in text and "FOLLOWED" in text


async def test_ui_tars_no_focus_move_prompt_is_stable() -> None:
    encoded = json.dumps((await _messages("ui_tars", None)), sort_keys=True, default=repr).encode()
    assert hashlib.sha256(encoded).hexdigest() == UI_TARS_PROMPT_SHA256
