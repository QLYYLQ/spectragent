"""The runtime hands an agent the format descriptions its prompt quotes, or refuses before setup.

``docs/architecture_design.md`` §4.2 step 2b and §4.4 "观测格式由提供方描述": the
descriptions come from the environment, are read once in ``prepare()`` and
reach the agent through its existing ``prepare`` seed slot. An agent that
quotes an active fragment its provider does not describe is refused before
anything is set up.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.loops import ReactLoop
from spectragent.core.negotiation import FragmentFormatError
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import ActionDescriptor, ActionResult
from tests._helpers.assembly import AGENT_ID, make_env
from tests.test_core.test_runtime.test_agent_runtime import SimpleAgent, SimpleEnv, _mk


class _Page(CapabilityBase):
    """A browser stand-in that provides ``browser.dom`` and describes nothing."""

    __abstract__ = True
    __wiring__ = {"action": ("action", ["browser.done"])}

    setups: int

    @property
    def capability_id(self) -> str:
        return "browser"

    @property
    def declared_fragments(self) -> frozenset[str]:
        return frozenset({"browser.dom"})

    def get_action_descriptors(self, **ctx: Any) -> list[ActionDescriptor]:
        return [ActionDescriptor(name="browser.done", description="Done")]

    async def execute_action(self, action: Any, **kw: Any) -> ActionResult:
        return ActionResult(success=True, is_done=True, extracted_content="answer")

    async def setup(self, *, negotiation: Any = None) -> None:
        self.setups += 1


class _DescribedPage(_Page):
    __abstract__ = True

    def fragment_formats(self) -> dict[str, str]:
        return {"browser.dom": "`[42]<button />` is element 42"}


class _Quoting(SimpleAgent):
    __wiring__ = {
        "observation": ("observation", ["dom"], []),
        "action": ("action", ["browser.done"]),
    }
    __fragment_formats__ = frozenset({"dom"})

    def prepare(self, task: str, action_space: Any, fragment_formats: Mapping[str, str]):
        return [dict(fragment_formats)]


def _runtime(page: _Page) -> AgentRuntime:
    page.setups = 0
    env = make_env(SimpleEnv, {"browser": page})
    return AgentRuntime.for_agent(_mk(_Quoting(), ReactLoop(), env), AGENT_ID)


def test_the_provider_s_description_reaches_the_seed_slot() -> None:
    runtime = _runtime(_DescribedPage())
    runtime.prepare()
    assert runtime.seed_messages("task") == [{"dom": "`[42]<button />` is element 42"}]


async def test_an_undescribed_quoted_fragment_is_refused_before_setup() -> None:
    page = _Page()
    runtime = _runtime(page)
    with pytest.raises(FragmentFormatError, match="'dom'.*'browser.dom'.*describes no format"):
        await runtime.execute("task")
    assert page.setups == 0
