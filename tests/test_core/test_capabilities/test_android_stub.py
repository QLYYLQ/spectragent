"""The Android-shaped stub passes the shared contract, takes the browser's params, and seats UI-TARS.

The stub (``tests/_helpers/android_stub.py``) stands in for the capability
``docs/architecture_design.md`` §11.2 designs until the real one exists. Its
three fragments come from one screen read, and its capture state is a
phone's, so running it through the contract shows the contract and
``Environment.observe`` fit a capability that is not a browser. UI-TARS runs
on it through the real ``AgentRuntime``: its first prompt states ``android``,
and a verb whose points the run's action does not declare is refused in
``prepare()``, before the run starts.

The contract's classes are subclassed rather than copied, so every clause
they hold, now or later, runs here too. The stub is kept out of
``HARNESS_FACTORIES``: that map is for shipped capabilities, and a stub
answering for ``android`` there would let the real capability pass the
completeness check without running the contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, get_args

import pytest
from pydantic import BaseModel

from spectragent.agents.memory.simple import SimpleMemory
from spectragent.agents.ui_tars import UITarsAgent
from spectragent.core.actions._registrar import ActionRegistrar
from spectragent.core.actions.params import point_params
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.llm.views import ChatInvokeCompletion
from spectragent.core.loops import ReactLoop
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import ActionDescriptor
from tests._helpers.android_stub import (
    ACTIONS,
    ANDROID_STUB,
    MOBILE_ACTIONS,
    StubAndroidCapability,
    android_stub_harness,
)
from tests._helpers.assembly import AGENT_ID, NoModel, StartSpy, make_env, make_run
from tests._helpers.capability_contract import CaptureHarness
from tests.test_core.test_capabilities import test_capability_contract as contract


@pytest.fixture(autouse=True)
def _android_stub() -> Iterator[None]:
    with ANDROID_STUB.activate():
        yield


@pytest.fixture
def harness() -> CaptureHarness:
    return android_stub_harness()


class TestStubObservationCycle(contract.TestObservationCycleContract):
    """Every clause of the shared contract, on the stub."""


class TestStubSurvivesAPeersFailure(contract.TestRealCapabilitySurvivesAPeersFailure):
    """A peer's failure leaves the stub's capture whole."""


class TestStubFailedObservation(contract.TestAFailedObservationDoesNotScaleWithFragmentCount):
    """A failed screen read costs one read, whatever the agent negotiated."""


def test_ui_tars_assembles_against_the_stub_with_its_wiring_unchanged() -> None:
    """A relative name is a param contract (§4.9), so UI-TARS's demands bind to Android's actions."""
    env = make_env(EnvironmentBase, {"android#0": StubAndroidCapability()})
    run = make_run(
        agent=UITarsAgent(), loop=ReactLoop(), env=env, llm=NoModel(), memory=SimpleMemory()
    )
    runtime = AgentRuntime.for_agent(run, AGENT_ID)

    runtime.prepare(task_id="assembly")

    actions, observation = runtime.negotiated_actions, runtime.negotiated_observation
    assert actions is not None and observation is not None
    assert actions.action_bindings == {
        "click_coordinate": "android.click_coordinate",
        "type_coordinate": "android.type_coordinate",
        "scroll": "android.scroll",
        "wait": "android.wait",
        "done": "loop.done",
    }
    fragments = observation.fragment_bindings
    assert {name: fragments[name] for name in ("screenshot", "capture_state", "platform")} == {
        "screenshot": "android.screenshot",
        "capture_state": "android.capture_state",
        "platform": "android.platform",
    }
    scroll = next(d for d in runtime.action_space.descriptors if d.name == "android.scroll")
    assert "index" not in scroll.params, "no android.ui_tree in this run, so no element to name"
    # The codec binds each verb's points to its action's here, after prepare().
    assert runtime.seed_messages("open the settings app")


class _ScriptedModel:
    """Answers every call with one reply and keeps every message it was shown."""

    inference_kind = "chat"
    model = name = "scripted"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.seen: list[Any] = []

    async def ainvoke(self, messages: Any, output_format: Any = None) -> ChatInvokeCompletion:
        self.seen.extend(messages)
        return ChatInvokeCompletion(completion=self.reply, usage=None)

    async def aclose(self, *, timeout: float = 5.0) -> tuple[()]:
        return ()


async def test_ui_tars_runs_on_the_stub_and_its_first_prompt_states_android() -> None:
    """Where the stub first stopped UI-TARS: past ``prepare()``, the first prompt refused android."""
    model = _ScriptedModel("Action: finished(content='opened')")
    capability = StubAndroidCapability()
    env = make_env(EnvironmentBase, {"android#0": capability})
    run = make_run(
        agent=UITarsAgent(prompt_template="mobile_use"),
        loop=ReactLoop(),
        env=env,
        llm=model,
        memory=SimpleMemory(),
    )

    result = await AgentRuntime.for_agent(run, AGENT_ID).execute("open the clock app", timeout=30)

    assert result.agent_done == "done", result.error
    assert capability.device.captures == 1
    stated = [m.content for m in model.seen if "<input_platform>" in str(m.content)]
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
    capability = StubAndroidCapability()
    described = capability.get_action_descriptors

    def without_the_taps_point(**context: Any) -> list[ActionDescriptor]:
        return [
            d.model_copy(update={"point_params": ()}) if d.name == "android.click_coordinate" else d
            for d in described(**context)
        ]

    monkeypatch.setattr(capability, "get_action_descriptors", without_the_taps_point)
    env = make_env(EnvironmentBase, {"android#0": capability})
    setups: list[Any] = []

    async def setup(*args: Any, **kwargs: Any) -> None:
        setups.append((args, kwargs))

    monkeypatch.setattr(env, "setup", setup)
    spy = StartSpy()
    run = make_run(
        agent=UITarsAgent(),
        loop=ReactLoop(),
        env=env,
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
    assert setups == []
    assert capability.device.captures == 0


def _schema_without_descriptions(params: type[BaseModel]) -> object:
    def strip(node: object) -> object:
        if isinstance(node, dict):
            # A description is a string; a param named "description" is a schema.
            return {
                key: strip(value)
                for key, value in node.items()
                if not (key == "description" and isinstance(value, str))
            }
        if isinstance(node, list):
            return [strip(value) for value in node]
        return node

    return strip(params.model_json_schema())


@pytest.mark.parametrize("key", ACTIONS)
def test_each_stub_action_takes_the_params_of_the_browser_action_of_its_name(key: str) -> None:
    """Negotiation binds names, not params, so one relative name is one param contract (AGENTS.md §2.5)."""
    stub = ActionRegistrar.get(key)
    browser = ActionRegistrar.get(key.replace("android.", "browser.", 1))

    assert _schema_without_descriptions(stub.Params) == _schema_without_descriptions(browser.Params)
    assert point_params(stub.Params) == point_params(browser.Params)


def test_the_android_only_actions_take_what_the_mobile_verbs_fill() -> None:
    """``long_press`` takes a tap's point, ``open_app`` an app's name, ``press_key`` a closed key set."""
    long_press, open_app, press_key = (ActionRegistrar.get(key) for key in MOBILE_ACTIONS)
    tap = ActionRegistrar.get("android.click_coordinate")

    assert _schema_without_descriptions(long_press.Params) == _schema_without_descriptions(
        tap.Params
    )
    assert point_params(long_press.Params) == point_params(tap.Params)
    (app_name,) = open_app.Params.model_fields.values()
    assert list(open_app.Params.model_fields) == ["app_name"]
    assert app_name.annotation is str and app_name.is_required()
    key = press_key.Params.model_fields["key"]
    assert get_args(key.annotation) == ("back", "home", "enter")
