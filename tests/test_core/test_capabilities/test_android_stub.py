"""The Android-shaped stub passes the shared contract, takes the browser's params, and seats UI-TARS.

The stub (``tests/_helpers/android_stub.py``) stands in for the capability
``docs/architecture_design.md`` §11.2 designs until the real one exists. Its
three fragments come from one screen read, and its capture state is a
phone's, so running it through the contract shows the contract and
``Environment.observe`` fit a capability that is not a browser. UI-TARS runs
on it through the real ``AgentRuntime``, and its first prompt states
``android``.

The contract's classes are subclassed rather than copied, so every clause
they hold, now or later, runs here too. The stub is kept out of
``HARNESS_FACTORIES``: that map is for shipped capabilities, and a stub
answering for ``android`` there would let the real capability pass the
completeness check without running the contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

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
from tests._helpers.android_stub import (
    ACTIONS,
    ANDROID_STUB,
    StubAndroidCapability,
    android_stub_harness,
)
from tests._helpers.assembly import AGENT_ID, make_env, make_run
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


class _NoModel:
    inference_kind = "chat"
    model = name = "no-model"

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        raise AssertionError("assembly calls no model")

    async def aclose(self, *, timeout: float = 5.0) -> tuple[()]:
        return ()


def test_ui_tars_assembles_against_the_stub_with_its_wiring_unchanged() -> None:
    """A relative name is a param contract (§4.9), so UI-TARS's demands bind to Android's actions."""
    env = make_env(EnvironmentBase, {"android#0": StubAndroidCapability()})
    run = make_run(
        agent=UITarsAgent(), loop=ReactLoop(), env=env, llm=_NoModel(), memory=SimpleMemory()
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
