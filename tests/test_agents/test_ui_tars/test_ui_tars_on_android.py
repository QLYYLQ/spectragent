"""UI-TARS on the Android capability, through the real ``AgentRuntime``, over the fake ADB server.

The capability, its bridge, its transport and its device-side clients are the
real ones; only the phone is scripted (``tests/_helpers/android_device.py``),
and it keeps every input its device side received. The model is a script too,
so a reply goes the whole way: the codec reads it, the Loop moves its points
onto the screen, the run's bindings route it to Android's action, and the
phone receives a stroke, a key or text.

- The first prompt states ``android``. The platform renderer once knew only
  the desktop systems, and refused the first step after setup and a screenshot.
- The mobile verbs are taught, and ``press_home()`` and ``long_press(...)``
  reach the phone as a key press and as a stroke held where it went down.
- ``type(content=...)`` without a point, as UI-TARS's own prompt writes it,
  types into the field that has focus; where none has, nothing reaches the
  phone and the next prompt carries the refusal.
- A verb whose points its action lacks is refused in ``prepare()``, before
  anything reaches the phone.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.capabilities.android.actions import AndroidTypeCoordinateAction
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.capabilities.android.environment import AndroidEnvironment
from spectragent.core.llm.messages import ContentPartTextParam, UserMessage
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import ActionDescriptor
from spectragent.schemas.agent_result import AgentResult
from tests._helpers.android_device import (
    ScriptedPhone,
    configured,
    fake_service_apk,
    phone_server,
)
from tests._helpers.android_runs import ScriptedModel
from tests._helpers.assembly import AGENT_ID, NoModel, StartSpy, make_env, make_run
from tests._helpers.fake_adb import FakeAdbServer

#: A verb the prompt's Available Actions teaches: ``- click(point='(x, y)')``.
_TAUGHT_VERB = re.compile(r"^- (\w+)\(", re.MULTILINE)


@pytest.fixture
def apk(tmp_path: Path) -> Path:
    return fake_service_apk(tmp_path)


def _android(adb: FakeAdbServer, apk: Path) -> AndroidCapability:
    """The capability for *adb*'s phone, which the run sets up; a capture waits for no still screen.

    Typing reads whether a text field has focus once, without waiting for one.
    """
    capability = AndroidCapability(service_apk=str(apk), settle_max_wait_s=0)
    configured(capability, adb)
    capability.actions = [
        AndroidTypeCoordinateAction(focus_wait_ms=0)
        if isinstance(action, AndroidTypeCoordinateAction)
        else action
        for action in capability.actions
    ]
    return capability


async def _run(
    agent: UITarsAgent, model: ScriptedModel, adb: FakeAdbServer, apk: Path
) -> AgentResult:
    env = make_env(AndroidEnvironment, {"android#0": _android(adb, apk)})
    run = make_run(agent=agent, loop=ReactLoop(), env=env, llm=model, memory=SimpleMemory())
    return await AgentRuntime.for_agent(run, AGENT_ID).execute("open the clock app", timeout=30)


async def test_its_first_prompt_states_android(apk: Path) -> None:
    """Past ``prepare()``, setup and the first screenshot, where the renderer once refused android."""
    phone = ScriptedPhone()
    model = ScriptedModel("Action: finished(content='opened')")
    async with phone_server(phone) as adb:
        result = await _run(UITarsAgent(prompt_template="mobile_use"), model, adb, apk)

    assert result.agent_done == "done", result.error
    assert phone.helper_calls("screenshot") == 2, "setup's probe, then the first observation"
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


async def test_available_actions_teach_the_mobile_verbs(apk: Path) -> None:
    """Every verb whose action Android offers, in its order; no ``hotkey``, as it offers no chords."""
    model = ScriptedModel("Action: finished()")
    async with phone_server(ScriptedPhone()) as adb:
        result = await _run(UITarsAgent(prompt_template="mobile_use"), model, adb, apk)

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


async def test_press_home_presses_the_home_key_on_the_phone(apk: Path) -> None:
    """``press_home()`` runs as ``android.press_key`` with ``key='home'``, Android's home key."""
    phone = ScriptedPhone()
    model = ScriptedModel("Action: press_home()", "Action: finished()")
    async with phone_server(phone) as adb:
        result = await _run(UITarsAgent(prompt_template="mobile_use"), model, adb, apk)

    assert result.agent_done == "done", result.error
    assert phone.inputs[1:] == ["helper key KEYCODE_HOME"], "after setup's test tap"


async def test_long_press_holds_where_its_point_lands_on_the_screen(apk: Path) -> None:
    """The point is written on the 0-1000 grid and held at the screen pixel it names."""
    phone = ScriptedPhone()  # 1080 x 2400
    model = ScriptedModel("Action: long_press(start_box='(250, 750)')", "Action: finished()")
    agent = UITarsAgent(prompt_template="mobile_use", coordinate_space="normalized_1000")
    async with phone_server(phone) as adb:
        result = await _run(agent, model, adb, apk)

    assert result.agent_done == "done", result.error
    # (250/1000 * 1080, 750/1000 * 2400), held there for the action's 1000 ms.
    assert phone.inputs[1:] == ["helper stroke (270,1800)@0 (270,1800)@1000"]


async def test_type_without_a_point_types_into_the_field_that_has_focus(apk: Path) -> None:
    phone = ScriptedPhone(text_field=True)
    model = ScriptedModel("Action: type(content='hello')", "Action: finished()")
    async with phone_server(phone) as adb:
        result = await _run(UITarsAgent(prompt_template="mobile_use"), model, adb, apk)

    assert result.agent_done == "done", result.error
    assert phone.inputs[1:] == ["service set_text 'hello' replace"]


async def test_type_without_a_point_where_no_text_field_has_focus_reaches_nothing(
    apk: Path,
) -> None:
    phone = ScriptedPhone()
    model = ScriptedModel("Action: type(content='hello')", "Action: finished()")
    async with phone_server(phone) as adb:
        result = await _run(UITarsAgent(prompt_template="mobile_use"), model, adb, apk)

    assert result.agent_done == "done", result.error
    assert phone.inputs[1:] == []
    step = [m for m in model.calls[1] if isinstance(m, UserMessage)][-1]
    assert isinstance(step.content, list) and isinstance(step.content[0], ContentPartTextParam)
    assert "No text field has focus: focus is in " in step.content[0].text


async def test_a_verb_whose_points_its_action_lacks_is_refused_before_the_run_starts(
    monkeypatch: pytest.MonkeyPatch, apk: Path
) -> None:
    """Refused in ``prepare()``, which a preflight runs alone, and not first by ``seed_messages``.

    ``execute`` seeds before ``on_run_start``, so it refused before the run
    started even when only the codec built in ``seed_messages`` checked; the
    bench preflight and ``spectragent config check`` call ``prepare()`` and
    nothing else, and passed it.
    """
    phone = ScriptedPhone()
    async with phone_server(phone) as adb:
        capability = _android(adb, apk)
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
    # Setup installs the APK and reads the device's facts: the device got nothing.
    assert adb.requests == [] and phone.inputs == []
