"""UI-TARS on the Android capability, through the real ``AgentRuntime``, over the fake ADB server.

The capability, its bridge and its transport are the real ones; only the phone
is scripted (``tests/_helpers/android_device.py``), and it keeps every
``input`` command it received. The model is a script too, so a reply goes the
whole way: the codec reads it, the Loop moves its points onto the screen, the
run's bindings route it to Android's action, and the phone receives a command.

- The first prompt states ``android``. The platform renderer once knew only
  the desktop systems, and refused the first step after setup and a screenshot.
- The mobile verbs are taught, and ``press_home()`` and ``long_press(...)``
  reach the phone as a key press and as a swipe that does not move.
- ``type(content=...)`` without a point, as UI-TARS's own prompt writes it,
  types into the field that has focus; where none has, nothing reaches the
  phone and the next prompt carries the refusal.
- A verb whose points its action lacks is refused in ``prepare()``, before
  anything reaches the phone.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.android.actions import AndroidTypeCoordinateAction
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.llm.messages import BaseMessage, ContentPartTextParam, UserMessage
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import ActionDescriptor
from spectragent.schemas.agent_result import AgentResult
from tests._helpers.android_device import SERIAL, FakeServerProvider, ScriptedPhone
from tests._helpers.assembly import AGENT_ID, NoModel, StartSpy, make_env, make_run
from tests._helpers.fake_adb import FakeAdbServer

#: A verb the prompt's Available Actions teaches: ``- click(point='(x, y)')``.
_TAUGHT_VERB = re.compile(r"^- (\w+)\(", re.MULTILINE)


class _ScriptedModel:
    """Answers each call with the next reply of its script, and keeps the messages of every call."""

    inference_kind = "chat"
    model = name = "scripted"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[BaseMessage]] = []

    async def ainvoke(
        self, messages: Sequence[BaseMessage], output_format: object = None
    ) -> ChatInvokeCompletion:
        self.calls.append(list(messages))
        if not self.replies:
            raise AssertionError("the script has no reply left")
        return ChatInvokeCompletion(completion=self.replies.pop(0), usage=None)

    async def aclose(self, *, timeout: float = 5.0) -> tuple[()]:
        return ()


def _android(adb: FakeAdbServer) -> AndroidCapability:
    """The capability for *adb*'s phone, which the run sets up; a capture waits for no still screen.

    Typing reads whether a text field has focus once, without waiting for one.
    """
    capability = AndroidCapability(settle_max_wait_s=0)
    capability.backend = FakeServerProvider(adb.host, adb.port)
    capability.actions = [
        AndroidTypeCoordinateAction(focus_wait_ms=0)
        if isinstance(action, AndroidTypeCoordinateAction)
        else action
        for action in capability.actions
    ]
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


async def test_available_actions_teach_the_mobile_verbs() -> None:
    """Every verb whose action Android offers, in its order; no ``hotkey``, as it offers no chords."""
    model = _ScriptedModel("Action: finished()")
    async with FakeAdbServer({SERIAL: ScriptedPhone()}) as adb:
        result = await _run(UITarsAgent(prompt_template="mobile_use"), model, adb)

    assert result.agent_done == "done", result.error
    system = str(model.calls[0][0].content)
    section = system.split("# Available Actions\n", 1)[1].split("\n# ", 1)[0]
    assert _TAUGHT_VERB.findall(section) == [
        "click",
        "long_press",
        "drag",
        "scroll",
        "type",
        "press_home",
        "press_back",
        "open_app",
        "wait",
        "finished",
    ]


async def test_press_home_presses_the_home_key_on_the_phone() -> None:
    """``press_home()`` runs as ``android.press_key`` with ``key='home'``, Android's home key."""
    phone = ScriptedPhone()
    model = _ScriptedModel("Action: press_home()", "Action: finished()")
    async with FakeAdbServer({SERIAL: phone}) as adb:
        result = await _run(UITarsAgent(prompt_template="mobile_use"), model, adb)

    assert result.agent_done == "done", result.error
    assert phone.inputs == ["input keyevent KEYCODE_HOME"]


async def test_long_press_holds_where_its_point_lands_on_the_screen() -> None:
    """The point is written on the 0-1000 grid and held at the screen pixel it names."""
    phone = ScriptedPhone()  # 1080 x 2400
    model = _ScriptedModel("Action: long_press(start_box='(250, 750)')", "Action: finished()")
    agent = UITarsAgent(prompt_template="mobile_use", coordinate_space="normalized_1000")
    async with FakeAdbServer({SERIAL: phone}) as adb:
        result = await _run(agent, model, adb)

    assert result.agent_done == "done", result.error
    # (250/1000 * 1080, 750/1000 * 2400); a long press is a swipe that does not move.
    assert phone.inputs == ["input swipe 270 1800 270 1800 1000"]


async def test_type_without_a_point_types_into_the_field_that_has_focus() -> None:
    phone = ScriptedPhone(text_field=True)
    model = _ScriptedModel("Action: type(content='hello')", "Action: finished()")
    async with FakeAdbServer({SERIAL: phone}) as adb:
        result = await _run(UITarsAgent(prompt_template="mobile_use"), model, adb)

    assert result.agent_done == "done", result.error
    assert phone.inputs == [
        "input keycombination KEYCODE_CTRL_LEFT KEYCODE_A && input keyevent KEYCODE_DEL",
        "input text hello",
    ]


async def test_type_without_a_point_where_no_text_field_has_focus_reaches_nothing() -> None:
    phone = ScriptedPhone()
    model = _ScriptedModel("Action: type(content='hello')", "Action: finished()")
    async with FakeAdbServer({SERIAL: phone}) as adb:
        result = await _run(UITarsAgent(prompt_template="mobile_use"), model, adb)

    assert result.agent_done == "done", result.error
    assert phone.inputs == []
    step = [m for m in model.calls[1] if isinstance(m, UserMessage)][-1]
    assert isinstance(step.content, list) and isinstance(step.content[0], ContentPartTextParam)
    assert "No text field has focus: focus is in " in step.content[0].text


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

        def without_the_taps_point(**context: object) -> list[ActionDescriptor]:
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
