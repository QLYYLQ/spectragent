"""An agent paired with a platform its prompt cannot render is refused at ``prepare()``.

``core/negotiation/platform.py``. The agent declares the input platforms its
prompt renders (``__input_platforms__``), the capability's platform fragment
the ones it may report (a closed ``Literal`` on ``operating_system``), and
``AgentRuntime.prepare()`` compares the two; an agent whose run negotiates a
platform and that declares nothing is refused as well. Before this check an
Android run of a desktop-only renderer passed ``prepare()``, set the
environment up, took a screenshot and only then failed rendering the first
prompt (``docs/architecture_design.md`` §11.2, ``android.platform``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar, Literal, get_args

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.rendering.platform import BROWSER_PLATFORMS, render_browser_platform
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.capabilities.android.observations import PLATFORM, AndroidPlatformObs
from spectragent.core.llm.messages import UserMessage
from spectragent.core.loops import ReactLoop
from spectragent.core.negotiation.platform import (
    PLATFORM_FIELD,
    IncompatiblePlatformError,
    check_input_platform,
    reported_platforms,
)
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import Action
from spectragent.schemas.observation._registrar import FragmentBase, ObservationRegistrar
from spectragent.schemas.observation.platform import InputPlatform
from tests._helpers.android_device import (
    ScriptedPhone,
    configured,
    fake_service_apk,
    phone_server,
)
from tests._helpers.assembly import AGENT_ID, NoModel, StartSpy, make_env, make_run


class _VisionAgent:
    """A vision agent whose prompt renders a desktop browser's platform, declaring nothing."""

    __execution_limit_fields__: ClassVar[frozenset[str]] = frozenset()
    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["screenshot", "platform"]),
        "action": ("action", ["click_coordinate"]),
    }

    async def build_react_prompt(self, observation: Any, *, context: Any) -> list[Any]:
        return [UserMessage(content=render_browser_platform(observation))]

    def parse_react_output(self, llm_output: Any) -> list[Action]:
        return []


class _DesktopAgent(_VisionAgent):
    """The same agent, declaring the desktop platforms its prompt renders."""

    __input_platforms__: ClassVar[frozenset[InputPlatform]] = BROWSER_PLATFORMS


def _register_open_platform() -> str:
    """``open_check.platform``, whose ``operating_system`` is any string. Rolled back after the test."""

    class _OpenPlatform(FragmentBase):
        __registry_key__ = "open_check.platform"
        __source__: ClassVar[Literal["env", "loop"]] = "env"

        operating_system: str = "android"

    return _OpenPlatform.__registry_key__


class TestTheTwoDeclarationsAreCompared:
    def test_a_fragment_reporting_only_what_the_prompt_renders_passes(self) -> None:
        check_input_platform(_DesktopAgent(), {PLATFORM_FIELD: "browser.platform"}, name="desktop")

    def test_a_fragment_that_may_report_another_platform_is_refused_by_name(self) -> None:
        with pytest.raises(IncompatiblePlatformError) as excinfo:
            check_input_platform(_DesktopAgent(), {PLATFORM_FIELD: PLATFORM}, name="desktop")
        assert str(excinfo.value).startswith(
            "desktop's prompt renders the input platforms macos, windows, linux, but the "
            "platform fragment this run negotiates, android.platform (AndroidPlatformObs), "
            "may report android."
        )
        assert isinstance(excinfo.value, TypeError)

    def test_a_fragment_that_is_not_a_closed_literal_is_refused(self) -> None:
        key = _register_open_platform()
        with pytest.raises(IncompatiblePlatformError, match="not a closed Literal") as excinfo:
            check_input_platform(_DesktopAgent(), {PLATFORM_FIELD: key}, name="desktop")
        assert "open_check.platform (_OpenPlatform)" in str(excinfo.value)

    def test_an_unregistered_fragment_is_refused(self) -> None:
        with pytest.raises(IncompatiblePlatformError, match="no registered fragment class"):
            check_input_platform(_DesktopAgent(), {PLATFORM_FIELD: "nowhere.platform"}, name="x")

    def test_an_agent_that_declares_no_platforms_is_refused(self) -> None:
        with pytest.raises(IncompatiblePlatformError) as excinfo:
            check_input_platform(_VisionAgent(), {PLATFORM_FIELD: PLATFORM}, name="vision")
        assert str(excinfo.value).startswith(
            "vision negotiates the platform fragment android.platform (AndroidPlatformObs) but "
            "declares no __input_platforms__"
        )

    def test_nothing_is_checked_when_no_platform_is_negotiated(self) -> None:
        check_input_platform(_VisionAgent(), {"screenshot": "browser.screenshot"}, name="vision")
        check_input_platform(_DesktopAgent(), {"screenshot": "browser.screenshot"}, name="x")

    def test_a_declaration_that_is_not_a_set_of_names_is_refused(self) -> None:
        class _Misdeclared(_DesktopAgent):
            __input_platforms__ = "macos"  # type: ignore[assignment]

        with pytest.raises(IncompatiblePlatformError, match="must be a set"):
            check_input_platform(_Misdeclared(), {PLATFORM_FIELD: "browser.platform"}, name="x")


class TestEveryPlatformFragmentIsAClosedLiteral:
    """What the comparison reads: every registered platform fragment says what it may report."""

    @staticmethod
    def _shipped() -> dict[str, type]:
        return {
            key: cls
            for key, cls in ObservationRegistrar.get_all().items()
            if key.rpartition(".")[2] == PLATFORM_FIELD
            and cls.__module__.startswith("spectragent.")
        }

    def test_there_are_platform_fragments_to_check(self) -> None:
        assert {"browser.platform", PLATFORM} <= set(self._shipped())

    def test_each_reports_a_closed_literal_of_input_platforms(self) -> None:
        for key, cls in self._shipped().items():
            reported = reported_platforms(cls)
            assert reported is not None, f"{key} ({cls.__name__}): operating_system is no Literal"
            assert set(reported) <= set(get_args(InputPlatform)), (key, reported)

    def test_the_browser_reports_the_desktop_systems_and_android_its_own(self) -> None:
        assert reported_platforms(ObservationRegistrar.get("browser.platform")) == (
            "macos",
            "windows",
            "linux",
        )
        assert reported_platforms(AndroidPlatformObs) == ("android",)


@pytest.mark.parametrize("agent_class", [_DesktopAgent, _VisionAgent])
async def test_a_desktop_only_prompt_on_android_is_refused_before_the_run_starts(
    agent_class: type[_VisionAgent], tmp_path: Path
) -> None:
    """Refused in ``prepare()``, declared or not: no trace opened, nothing set up, no screen read."""
    phone = ScriptedPhone()
    async with phone_server(phone) as adb:
        capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
        configured(capability, adb)
        spy = StartSpy()
        run = make_run(
            agent=agent_class(),
            loop=ReactLoop(),
            env=make_env(AndroidEnvironment, {"android#0": capability}),
            llm=NoModel(),
            memory=SimpleMemory(),
            hooks=[spy],
        )
        runtime = AgentRuntime.for_agent(run, AGENT_ID)
        with pytest.raises(IncompatiblePlatformError, match=r"android\.platform"):
            runtime.prepare()
        with pytest.raises(IncompatiblePlatformError, match=r"android\.platform"):
            await runtime.execute("open the settings app")

    assert spy.starts == []
    # Setup installs the APK and reads the device's facts: the device got nothing.
    assert adb.requests == [] and phone.inputs == []
