"""UI-TARS on the Android capability, through the real ``AgentRuntime``, over the fake ADB server.

The capability, its bridge and its transport are the real ones; only the phone
is scripted (``tests/_helpers/android_device.py``), and so is the model.

- The first prompt states ``android``. The platform renderer once knew only
  the desktop systems, and refused the first step after setup and a screenshot.
- A verb whose points its action lacks is refused in ``prepare()``, before
  anything reaches the phone.
"""

from __future__ import annotations

from typing import Any

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import ActionDescriptor
from spectragent.schemas.agent_result import AgentResult
from tests._helpers.android_device import SERIAL, FakeServerProvider, ScriptedPhone
from tests._helpers.assembly import AGENT_ID, NoModel, StartSpy, make_env, make_run
from tests._helpers.fake_adb import FakeAdbServer


class _ScriptedModel:
    """Answers each call with the next reply of its script, and keeps the messages of every call."""

    inference_kind = "chat"
    model = name = "scripted"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[Any]] = []

    async def ainvoke(self, messages: Any, output_format: Any = None) -> ChatInvokeCompletion:
        self.calls.append(list(messages))
        if not self.replies:
            raise AssertionError("the script has no reply left")
        return ChatInvokeCompletion(completion=self.replies.pop(0), usage=None)

    async def aclose(self, *, timeout: float = 5.0) -> tuple[()]:
        return ()


def _android(adb: FakeAdbServer) -> AndroidCapability:
    """The capability for *adb*'s phone, which the run sets up; a capture waits for no still screen."""
    capability = AndroidCapability(settle_max_wait_s=0)
    capability.backend = FakeServerProvider(adb.host, adb.port)
    return capability


async def _run(agent: UITarsAgent, model: _ScriptedModel, adb: FakeAdbServer) -> AgentResult:
    env = make_env(AndroidEnvironment, {"android#0": _android(adb)})
    run = make_run(agent=agent, loop=ReactLoop(), env=env, llm=model, memory=SimpleMemory())
    return await AgentRuntime.for_agent(run, AGENT_ID).execute("open the clock app", timeout=30)


async def test_its_first_prompt_states_android() -> None:
    """Past ``prepare()``, setup and the first screenshot, where the renderer once refused android."""
    model = _ScriptedModel("Action: finished(content='opened')")
    async with FakeAdbServer({SERIAL: ScriptedPhone()}) as adb:
        result = await _run(UITarsAgent(prompt_template="mobile_use"), model, adb)

    assert result.agent_done == "done", result.error
    assert [r.service for r in adb.requests].count("shell,v2:screencap -p") == 1
    (call,) = model.calls
    stated = [m.content for m in call if "<input_platform>" in str(m.content)]
    assert stated == [
        "<input_platform>\n"
        "Computer-use agent; environment: android.\n"
        "Actual input platform: Android (android).\n"
        "On Android, text goes in through an input method's on-screen keyboard, which can "
        "cover part of the screen while a text field has focus. There are no Control or Meta "
        "modifiers, so no Control+A or Meta+A selects all.\n"
        "</input_platform>"
    ]


async def test_a_verb_whose_points_its_action_lacks_is_refused_before_the_run_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused in ``prepare()``, which a preflight runs alone, and not first by ``seed_messages``.

    ``execute`` seeds before ``on_run_start``, so it refused before the run
    started even when only the codec built in ``seed_messages`` checked; the
    bench preflight and ``spectragent config check`` call ``prepare()`` and
    nothing else, and passed it.
    """
    async with FakeAdbServer({SERIAL: ScriptedPhone()}) as adb:
        capability = _android(adb)
        described = capability.get_action_descriptors

        def without_the_taps_point(**context: Any) -> list[ActionDescriptor]:
            return [
                d.model_copy(update={"point_params": ()})
                if d.name == "android.click_coordinate"
                else d
                for d in described(**context)
            ]

        monkeypatch.setattr(capability, "get_action_descriptors", without_the_taps_point)
        spy = StartSpy()
        run = make_run(
            agent=UITarsAgent(),
            loop=ReactLoop(),
            env=make_env(AndroidEnvironment, {"android#0": capability}),
            llm=NoModel(),
            memory=SimpleMemory(),
            hooks=[spy],
        )
        runtime = AgentRuntime.for_agent(run, AGENT_ID)

        with pytest.raises(ValueError, match=r"click\(\) writes 1 point\(s\)") as excinfo:
            runtime.prepare()
        assert "android.click_coordinate" in str(excinfo.value)
        with pytest.raises(ValueError, match=r"click\(\) writes 1 point\(s\)"):
            await runtime.execute("open the clock app")

    assert spy.starts == []
    # Setup reads the device's facts and a screenshot is a screencap: the device got neither.
    assert adb.requests == []
