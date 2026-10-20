"""The typing marker reaches the trace, never a prompt (§5.6).

``ActionResult.typed_into_sensitive_field`` is recorded for dataset export.
Agents render an action result as success, failure and error (BrowserUse's
history block; the loop's ``loop.action_result`` fragment carries nothing
else), so every agent's prompt is byte-identical whatever the marker says.
"""

from __future__ import annotations

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
from spectragent.core.loops._loop_fragments import last_action_result_obs, loop_fragment_builders
from spectragent.core.loops.limits import ExecutionLimits
from spectragent.core.negotiation import negotiate_action
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.observation._registrar import fragment_field_name
from spectragent.schemas.prompt import ReactPromptContext
from spectragent.schemas.step_record import StepRecord, TokenUsage
from tests._helpers.assembly import make_agent

_AGENTS: dict[str, type[Any]] = {
    "browser_use": BrowserUseAgent,
    "skyvern": SkyvernAgent,
    "ui_tars": UITarsAgent,
}
_MARKERS = (None, False, True)


def _png(width: int = 1280, height: int = 720) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


def _typed_step(marker: bool | None) -> StepRecord:
    """A step that typed into the password field; only the marker varies."""
    return StepRecord(
        step=0,
        observation_summary="login page",
        llm_output="type the password",
        actions=[Action(name="browser.type", params={"index": 3, "text": "hunter2"})],
        action_results=[ActionResult(success=True, typed_into_sensitive_field=marker)],
        timestamp=100.0,
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def _observation(marker: bool | None) -> Any:
    step = _typed_step(marker)
    builders = loop_fragment_builders(
        task="log in",
        step=1,
        max_steps=10,
        today="2026-10-13",
        history=[step],
        prev_step=step,
    )
    return SimpleNamespace(
        platform=SimpleNamespace(operating_system="macos", fragment_id="browser.platform"),
        **{fragment_field_name(key): build() for key, build in builders.items()},
        screenshot=ScreenshotObs(
            image_bytes=_png(),
            image_width=1280,
            image_height=720,
            viewport_width=1280,
            viewport_height=720,
        ),
        dom=DOMObs(element_tree=SimpleNamespace(serialized_dom="[3]<input type=password id=pw />")),
        browser_meta=BrowserMetaObs(url="https://shop.test/login", page_title="Log in"),
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
    agent.prepare(
        task="log in",
        action_descriptors=descriptors,
        execution_limits=ExecutionLimits(max_actions_per_step=1),
    )
    return agent


async def _prompt(agent_id: str, marker: bool | None) -> list[dict[str, Any]]:
    messages = await _prepared(agent_id).build_react_prompt(
        _observation(marker), context=ReactPromptContext(step=0, phase="policy")
    )
    return [message.model_dump() for message in messages]


@pytest.mark.parametrize("agent_id", sorted(_AGENTS))
async def test_the_marker_never_changes_a_prompt(agent_id: str) -> None:
    unset, unsensitive, sensitive = [await _prompt(agent_id, marker) for marker in _MARKERS]
    assert sensitive == unsensitive == unset


async def test_the_history_the_comparison_relies_on_is_rendered() -> None:
    """BrowserUse shows the typed step and its result: the comparison above is not vacuous."""
    text = repr(await _prompt("browser_use", True))
    assert "browser.type" in text and "ok=True" in text


def test_the_loop_fragment_is_the_same_whatever_the_marker() -> None:
    shown = [
        last_action_result_obs(_typed_step(marker)).model_dump(exclude={"captured_at"})
        for marker in _MARKERS
    ]
    assert shown[0] == shown[1] == shown[2]
    assert shown[0]["last_action"] == "browser.type"
