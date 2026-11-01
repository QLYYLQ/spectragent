"""The input platform as each agent's prompt states it, and which platforms each prompt can state.

``agents/rendering/platform.py``. The cross-platform computer-use rendering
(UI-TARS) states all four input platforms, Android with a phone's touch
keyboard rules. The browser-specific rendering (BrowserUse, Skyvern) states a
desktop browser's three: a mobile browser is not designed
(``docs/architecture_design.md`` §11.2). Each agent declares the set its prompt
renders (``__input_platforms__``), which ``AgentRuntime.prepare()`` holds
against the run's platform fragment (``core/negotiation/platform.py``). These
tests pin each declaration to what the prompt renders, and keep the renderer's
refusal as the backstop for every platform an agent does not declare.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, get_args

import pytest
from conscribe import discover

from spectragent.agents._registrar import AgentRegistrar
from spectragent.agents.rendering.platform import (
    BROWSER_PLATFORMS,
    COMPUTER_USE_PLATFORMS,
    render_browser_platform,
    render_computer_use_platform,
)
from spectragent.core.capabilities.browser.observations import DOMObs, ScreenshotObs
from spectragent.core.llm.messages import ContentPartTextParam
from spectragent.core.negotiation import extract_wiring
from spectragent.schemas.observation.fragments import TaskObs
from spectragent.schemas.observation.platform import InputPlatform
from spectragent.schemas.prompt import ReactPromptContext

_ANDROID_RULES = (
    "On Android, text goes in through an input method's on-screen keyboard, which can cover "
    "part of the screen while a text field has focus. There are no Control or Meta "
    "modifiers, so no Control+A or Meta+A selects all."
)


def _platform(operating_system: Any, key: str = "android.platform") -> Any:
    return SimpleNamespace(
        platform=SimpleNamespace(operating_system=operating_system, fragment_id=key)
    )


class TestTheRenderings:
    def test_android_is_stated_with_a_phones_keyboard_rules(self) -> None:
        assert render_computer_use_platform(_platform("android")) == (
            "<input_platform>\n"
            "Computer-use agent; environment: android.\n"
            "Actual input platform: Android (android).\n"
            f"{_ANDROID_RULES}\n"
            "</input_platform>"
        )

    def test_the_desktop_rules_are_not_stated_for_a_phone(self) -> None:
        text = render_computer_use_platform(_platform("android"))
        assert "Ctrl/Control sends Control" not in text
        assert "use Control+A for Select All" not in text

    def test_the_browser_rendering_states_a_desktop_browser_only(self) -> None:
        assert {"macos", "windows", "linux"} == BROWSER_PLATFORMS
        with pytest.raises(ValueError, match="platform observation is missing or invalid") as info:
            render_browser_platform(_platform("android", key="browser.platform"))
        assert "must be one of macos, windows, linux." in str(info.value)

    def test_the_computer_use_rendering_states_every_input_platform(self) -> None:
        assert set(get_args(InputPlatform)) == COMPUTER_USE_PLATFORMS
        with pytest.raises(ValueError) as info:
            render_computer_use_platform(_platform("ios"))
        assert "must be one of macos, windows, linux, android." in str(info.value)

    @pytest.mark.parametrize("render", [render_browser_platform, render_computer_use_platform])
    def test_the_refusal_names_no_browser(self, render: Any) -> None:
        with pytest.raises(ValueError) as info:
            render(_platform(None))
        assert "rowser" not in str(info.value)


# ── Each agent renders exactly what it declares ───────────────────


def _platform_agents() -> list[type]:
    """Every registered agent whose observation wiring names a platform fragment.

    By the local name (``platform``) or a full key that pins one
    (``browser.platform``).
    """
    discover("spectragent.agents")
    agents = [
        cls
        for cls in AgentRegistrar.get_all().values()
        if cls.__module__.startswith("spectragent.")
        and any(
            key.rpartition(".")[2] == "platform"
            for key in set().union(*extract_wiring(cls, "observation"))
        )
    ]
    return sorted(agents, key=lambda cls: cls.__registry_key__)


def _observation(operating_system: str) -> Any:
    """What every shipped agent's prompt reads, on the platform *operating_system*."""
    environment = "android" if operating_system == "android" else "browser"
    return SimpleNamespace(
        platform=SimpleNamespace(
            operating_system=operating_system, fragment_id=f"{environment}.platform"
        ),
        task=TaskObs(user_request="Open the settings."),
        history=SimpleNamespace(items=[]),
        screenshot=ScreenshotObs(
            image_bytes=b"\x89PNG\r\n\x1a\n",
            image_width=1080,
            image_height=2400,
            viewport_width=1080,
            viewport_height=2400,
        ),
        dom=DOMObs(element_tree="<input />"),
    )


def _text(messages: list[Any]) -> str:
    texts: list[str] = []
    for message in messages:
        if isinstance(message.content, str):
            texts.append(message.content)
        else:
            texts.extend(
                part.text for part in message.content if isinstance(part, ContentPartTextParam)
            )
    return "\n".join(texts)


def test_every_agent_that_renders_the_platform_declares_which() -> None:
    agents = _platform_agents()
    assert len(agents) >= 5, [cls.__registry_key__ for cls in agents]
    for cls in agents:
        declared = getattr(cls, "__input_platforms__", None)
        assert isinstance(declared, frozenset), cls.__registry_key__
        assert declared and declared <= set(get_args(InputPlatform)), cls.__registry_key__


@pytest.mark.parametrize("operating_system", get_args(InputPlatform))
@pytest.mark.parametrize("agent_class", _platform_agents(), ids=lambda cls: cls.__registry_key__)
async def test_each_agent_renders_exactly_the_platforms_it_declares(
    agent_class: Any, operating_system: str
) -> None:
    building = agent_class().build_react_prompt(
        _observation(operating_system), context=ReactPromptContext(step=0, phase="policy")
    )
    if operating_system in agent_class.__input_platforms__:
        assert f"({operating_system})" in _text(await building)
    else:
        with pytest.raises(ValueError, match="platform observation is missing or invalid"):
            await building
