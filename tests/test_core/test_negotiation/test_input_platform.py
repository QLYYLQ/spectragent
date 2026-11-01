"""An agent paired with a platform its prompt cannot render is refused at ``prepare()``.

``core/negotiation/platform.py``. The agent declares the input platforms its
prompt renders (``__input_platforms__``), the capability's platform fragment
the ones it may report (a closed ``Literal`` on ``operating_system``), and
``AgentRuntime.prepare()`` compares the two. Before this check an Android run
of a desktop-only renderer passed ``prepare()``, set the environment up, took a
screenshot and only then failed rendering the first prompt
(``docs/architecture_design.md`` §11.2, ``android.platform``).
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal, get_args

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.rendering.platform import BROWSER_PLATFORMS, render_browser_platform
from spectragent.core.environments._registrar import EnvironmentBase
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
from tests._helpers.android_stub import (
    ANDROID_STUB,
    PLATFORM,
    AndroidPlatformObs,
    StubAndroidCapability,
)
from tests._helpers.assembly import AGENT_ID, NoModel, StartSpy, make_env, make_run


class _DesktopAgent:
    """A vision agent whose prompt renders a desktop browser's platform only."""

    __input_platforms__: ClassVar[frozenset[InputPlatform]] = BROWSER_PLATFORMS
    __execution_limit_fields__: ClassVar[frozenset[str]] = frozenset()
    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["screenshot", "platform"]),
        "action": ("action", ["click_coordinate"]),
    }

    async def build_react_prompt(self, observation: Any, *, context: Any) -> list[Any]:
        return [UserMessage(content=render_browser_platform(observation))]

    def parse_react_output(self, llm_output: Any) -> list[Action]:
        return []


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
        with ANDROID_STUB.activate(), pytest.raises(IncompatiblePlatformError) as excinfo:
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

    def test_nothing_is_checked_without_a_declaration_or_a_platform(self) -> None:
        check_input_platform(object(), {PLATFORM_FIELD: "nowhere.platform"}, name="silent")
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
        assert "browser.platform" in self._shipped()

    def test_each_reports_a_closed_literal_of_input_platforms(self) -> None:
        with ANDROID_STUB.activate():
            fragments = {**self._shipped(), PLATFORM: ObservationRegistrar.get(PLATFORM)}
        for key, cls in fragments.items():
            reported = reported_platforms(cls)
            assert reported is not None, f"{key} ({cls.__name__}): operating_system is no Literal"
            assert set(reported) <= set(get_args(InputPlatform)), (key, reported)

    def test_the_browser_reports_the_desktop_systems_and_the_stub_android(self) -> None:
        assert reported_platforms(ObservationRegistrar.get("browser.platform")) == (
            "macos",
            "windows",
            "linux",
        )
        assert reported_platforms(AndroidPlatformObs) == ("android",)


async def test_a_desktop_only_prompt_on_android_is_refused_before_the_run_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused in ``prepare()``: no trace opened, nothing set up, no screen read."""
    setups: list[Any] = []

    async def setup(*args: Any, **kwargs: Any) -> None:
        setups.append((args, kwargs))

    with ANDROID_STUB.activate():
        capability = StubAndroidCapability()
        env = make_env(EnvironmentBase, {"android#0": capability})
        monkeypatch.setattr(env, "setup", setup)
        spy = StartSpy()
        run = make_run(
            agent=_DesktopAgent(),
            loop=ReactLoop(),
            env=env,
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
    assert setups == []
    assert capability.device.captures == 0
