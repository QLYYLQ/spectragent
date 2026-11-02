"""An action whose required param addresses a fragment is on offer only in a run that negotiated it.

``docs/architecture_design.md`` §4.5 "参数的含义": a required param that needs
fragment F makes the whole action need F. ``android.click``'s ``index`` names
an element of ``android.ui_tree``, so in a run without the tree there is
nothing for it to name: negotiation leaves the action out when the agent
declared it optional, and refuses the run, naming the fragment, when the agent
requires it. ``AgentRuntime.prepare()`` hands negotiation the fragments the
observation negotiation activated; the refusal comes before setup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

import spectragent.core.capabilities.android.actions  # noqa: F401 — registers them
import spectragent.core.capabilities.browser.actions  # noqa: F401 — registers them
from spectragent.agents.memory.simple import SimpleMemory
from spectragent.core.capabilities.android.capability import AndroidCapability
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.loops import ReactLoop
from spectragent.core.negotiation import ActionFragmentError, IncompatibleActionError
from spectragent.core.negotiation.action import negotiate_action
from spectragent.core.runtime import AgentRuntime
from tests._helpers.android_device import fake_service_apk
from tests._helpers.assembly import AGENT_ID, NoModel, make_env, make_run

#: What the Android capability offers, as a run's environment.
ANDROID = (
    "android.click",
    "android.click_coordinate",
    "android.scroll",
    "android.type_coordinate",
)
SCREENSHOT_ONLY = frozenset({"android.screenshot", "android.platform"})
WITH_TREE = SCREENSHOT_ONLY | {"android.ui_tree"}


class _Env:
    def __init__(self, *actions: str) -> None:
        self.provided_actions = frozenset(actions)


class _MaybeClicks:
    """Taps points, and taps elements when the run offers it."""

    __registry_key__ = "maybe_clicks"
    __wiring__: ClassVar[dict[str, Any]] = {
        "action": ("action", ["click_coordinate", "done"], ["click", "scroll"]),
    }


class _Clicks:
    """Needs element indexes."""

    __registry_key__ = "clicks"
    __wiring__: ClassVar[dict[str, Any]] = {"action": ("action", ["click", "done"])}


def test_an_optional_action_whose_fragment_the_run_lacks_is_left_out() -> None:
    result = negotiate_action(
        _MaybeClicks(), ReactLoop(), _Env(*ANDROID), fragments=SCREENSHOT_ONLY
    )

    assert "android.click" not in result.active_actions and "click" not in result.action_bindings
    assert "android.scroll" in result.active_actions, "an optional param's fragment drops the param"


def test_an_action_whose_fragment_the_run_negotiated_is_on_offer() -> None:
    result = negotiate_action(_MaybeClicks(), ReactLoop(), _Env(*ANDROID), fragments=WITH_TREE)

    assert result.action_bindings["click"] == "android.click"


def test_a_required_action_whose_fragment_the_run_lacks_is_refused_naming_the_fragment() -> None:
    with pytest.raises(ActionFragmentError) as refused:
        negotiate_action(_Clicks(), ReactLoop(), _Env(*ANDROID), fragments=SCREENSHOT_ONLY)

    error = refused.value
    assert isinstance(error, IncompatibleActionError)
    assert error.missing == {"click"}
    assert error.fragments == {"click": frozenset({"android.ui_tree"})}
    assert error.requested_by == {"click": ("clicks",)}
    assert str(error).startswith(
        "Required actions address fragments this run does not have: 'click' (android.click) is "
        "required by clicks and needs fragment(s) ['android.ui_tree'], which this run's "
        "observation negotiation did not activate."
    )


def test_without_an_observation_negotiation_every_action_is_on_offer() -> None:
    result = negotiate_action(_Clicks(), ReactLoop(), _Env(*ANDROID))

    assert result.action_bindings["click"] == "android.click"


class _TapsElementsFromAScreenshot:
    """Reads the screenshot alone, and requires element indexes: a wiring mistake."""

    __registry_key__ = "taps_elements_from_a_screenshot"
    __wiring__: ClassVar[dict[str, Any]] = {
        "observation": ("observation", ["screenshot"]),
        "action": ("action", ["click", "done"]),
    }
    __execution_limit_fields__: ClassVar[frozenset[str]] = frozenset()

    async def build_react_prompt(self, obs: Any, *, context: Any) -> list[dict[str, str]]:
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out: Any) -> list[Any]:
        return []


def test_prepare_refuses_a_required_element_index_with_no_tree_before_setup(tmp_path: Path) -> None:
    capability = AndroidCapability(service_apk=str(fake_service_apk(tmp_path)))
    env = make_env(EnvironmentBase, {"android#0": capability})
    run = make_run(
        agent=_TapsElementsFromAScreenshot(),
        loop=ReactLoop(),
        env=env,
        llm=NoModel(),
        memory=SimpleMemory(),
    )

    with pytest.raises(ActionFragmentError, match=r"'click' \(android\.click\)"):
        AgentRuntime.for_agent(run, AGENT_ID).prepare(task_id="assembly")
    assert not capability.is_alive
