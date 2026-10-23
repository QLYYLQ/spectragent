"""A DOM agent's prompt teaches the format its run's observation is actually written in.

``docs/architecture_design.md`` §4.4 "观测格式由提供方描述": the DOM serializer
describes its own text and BrowserUse and Skyvern quote that description,
delivered by ``AgentRuntime.prepare()`` from the configured capability. The
defect this pins: Skyvern's prompt taught ``[42]<button …>`` while its shipped
configuration writes ``data-idx="42"``, and its tests fed the agent
hand-written ``[1]<button>`` text, so nothing compared the two.

Every case here composes a shipped YAML with the serializer swapped, runs the
real runtime assembly, and feeds the agent a real ``DOMState`` from the
configured cleaner through the configured serializer.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from spectragent.core.benchmark_runner import build_composer
from spectragent.core.capabilities.browser.capability import BrowserCapability
from spectragent.core.capabilities.browser.dom.budget import DOMBudget, DOMBudgetPass
from spectragent.core.capabilities.browser.dom.models import DOMState
from spectragent.core.capabilities.browser.observations import (
    BrowserMetaObs,
    DOMObs,
    ScreenshotObs,
)
from spectragent.core.llm.messages import BaseMessage
from spectragent.core.loops._loop_fragments import loop_fragment_builders
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.observation._registrar import fragment_field_name
from tests._helpers.dom_captures import load_capture
from tests._helpers.dom_payloads import reference_page
from tests._helpers.frames import step_context

_CONFIGS = Path(__file__).resolve().parents[2] / "configs"

#: The shipped configuration each agent is composed from.
_CONFIG = {"browser_use": "browser_use_local.yaml", "skyvern": "skyvern_agent_local.yaml"}

#: Each agent configuration whose prompt describes the DOM.
_CASES: list[tuple[str, dict[str, Any]]] = [
    ("browser_use", {"agent_id": "browser_use.dom"}),
    ("browser_use", {"agent_id": "browser_use.dom_vision"}),
    ("browser_use", {"agent_id": "browser_use.dom", "flash_mode": True}),
    ("browser_use", {"agent_id": "browser_use.dom", "is_browser_use_model": True}),
    ("skyvern", {}),
]
_IDS = [f"{agent}-{sorted(overrides.items())}" for agent, overrides in _CASES]

#: How each serializer writes an element's index (as in
#: ``test_browser_dom_serializer_format.py``).
_MARKER = {"text": "[{index}]", "html": 'data-idx="{index}"'}


def _png(width: int = 1280, height: int = 720) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
    )


def _compose(agent_id: str, overrides: dict[str, Any], serializer: str) -> Any:
    """The shipped YAML for *agent_id*, with *serializer* configured and no trace written."""
    raw = yaml.safe_load((_CONFIGS / _CONFIG[agent_id]).read_text())
    raw.pop("hook", None)
    raw["agent"][0].update(overrides)
    raw["agent"][0]["llm"] = {
        "model_type": "openai",
        "provider": {"name": "direct"},
        "model": "offline-stub",
    }
    raw["environment"][0]["capabilities"][0]["dom_serializer"] = {"serializer_type": serializer}
    return build_composer().compose(raw)


def _capability(run: Any) -> BrowserCapability:
    env = run.instance("environment", run.ids("environment")[0])
    (capability,) = env.capabilities.values()
    assert isinstance(capability, BrowserCapability)
    return capability


async def _observed_dom(capability: BrowserCapability) -> DOMState:
    """A real ``DOMState``: the configured cleaner on a capture it can read, then the serializer.

    The BrowserUse cleaner reads a synthetic CDP payload with frames and a
    shadow root; the Skyvern cleaner reads a committed real-Chrome capture.
    Serialized through the DOM budget, as the DOM resolver does.
    """
    cleaner = capability.dom_cleaner
    capture = (
        load_capture("skyvern_sticky")
        if cleaner.cleaner_type.startswith("skyvern")  # type: ignore[attr-defined]
        else reference_page()
    )
    state = await cleaner.clean(capture, (1280, 720))
    DOMBudgetPass(DOMBudget()).apply(state, capability.dom_serializer.serialize)
    return state


def _observation(state: DOMState) -> Any:
    builders = loop_fragment_builders(
        task="Find the result",
        step=1,
        max_steps=10,
        today="2026-10-23",
        history=[],
        prev_step=None,
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
        dom=DOMObs(element_tree=state, selector_map=dict(state.selector_map)),
        browser_meta=BrowserMetaObs(url="https://shop.test/", page_title="Shop"),
    )


def _text(messages: list[BaseMessage]) -> str:
    return "\n".join(message.text for message in messages)


async def _prompts(
    agent_id: str, overrides: dict[str, Any], serializer: str
) -> tuple[str, str, BrowserCapability, DOMState]:
    """The run's system prompt, one step's message, the capability and the DOM shown."""
    run = _compose(agent_id, overrides, serializer)
    runtime = AgentRuntime.for_agent(run, "main")
    runtime.prepare()
    system = _text(runtime.seed_messages("Find the result"))
    capability = _capability(run)
    state = await _observed_dom(capability)
    observation = _observation(state)
    agent = run.instance("agent", "main")
    step = await agent.build_react_prompt(
        observation, context=step_context(runtime.action_space, observation)
    )
    return system, _text(step), capability, state


@pytest.mark.parametrize("serializer", sorted(_MARKER))
@pytest.mark.parametrize("agent_id,overrides", _CASES, ids=_IDS)
async def test_the_prompt_quotes_the_serializer_the_observation_was_written_by(
    agent_id: str, overrides: dict[str, Any], serializer: str
) -> None:
    system, step, capability, state = await _prompts(agent_id, overrides, serializer)
    assert capability.dom_serializer.serializer_type == serializer  # type: ignore[attr-defined]
    assert capability.dom_serializer.describe_format() in system
    # The marker the prompt teaches is the one every shown element carries.
    marker = _MARKER[serializer]
    assert marker.format(index=42) in system
    assert state.serialized_dom in step
    shown = [k for k, node in state.selector_map.items() if node.is_interactive]
    assert shown
    for index in shown:
        assert marker.format(index=index) in step, f"element {index} is not shown as taught"
    # And no other serializer's marker is taught.
    for other, other_marker in _MARKER.items():
        if other != serializer:
            assert other_marker.format(index=42) not in system, other
            assert other_marker.format(index=shown[0]) not in step, other


@pytest.mark.parametrize("agent_id,overrides", _CASES, ids=_IDS)
async def test_no_prompt_keeps_a_format_of_its_own(
    agent_id: str, overrides: dict[str, Any]
) -> None:
    """Swap the serializer and the prompt changes exactly where the description sits."""
    text_system, _step, text_capability, _state = await _prompts(agent_id, overrides, "text")
    html_system, _step, html_capability, _state = await _prompts(agent_id, overrides, "html")
    text_format = text_capability.dom_serializer.describe_format()
    html_format = html_capability.dom_serializer.describe_format()
    assert text_system != html_system
    assert text_system.replace(text_format, "") == html_system.replace(html_format, "")


async def test_the_shipped_skyvern_configuration_teaches_data_idx() -> None:
    """The defect itself: ``configs/skyvern_agent_local.yaml`` serializes to ``html``."""
    raw = yaml.safe_load((_CONFIGS / _CONFIG["skyvern"]).read_text())
    assert raw["environment"][0]["capabilities"][0]["dom_serializer"] == {"serializer_type": "html"}
    system, step, _capability_, _state = await _prompts("skyvern", {}, "html")
    assert 'data-idx="42"' in system and 'data-idx="4"' in step
    assert "[index]" not in system and "square brackets" not in system
