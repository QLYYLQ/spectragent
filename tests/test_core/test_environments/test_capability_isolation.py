"""F8 — the framework ABI carries no capability-private routing handle.

``session_id`` is a CDP tab handle. It used to be a **required**
parameter of ``BaseAction.execute`` and an optional one on
``Environment.observe/act``, ``AgentRuntime.prepare/execute`` and the
ReAct loop's ctx — four framework layers, none of which could construct
one. On the production path it was always ``None``.

The damage ran both ways, and both directions are pinned here:

- a real tab id handed to a **non-browser** capability's action, which
  has no idea what a Chrome DevTools target is;
- a run-scoped id handed to the **browser** capability, whose
  ``Unknown session_id`` guard fails every action it touches.

The companion half is §5.2.6: ``EnvironmentBase.setup`` broadcast one
undifferentiated ``**kwargs`` to every capability, so two capabilities
could not be configured independently and a typo was dropped by all of
them in silence.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any

import pytest

from spectragent.core.actions._registrar import ActionBase
from spectragent.core.actions.base import BaseAction
from spectragent.core.capabilities._registrar import CapabilityBase
from spectragent.core.environments._registrar import EnvironmentBase
from spectragent.core.environments.base import Environment
from spectragent.core.runtime import AgentRuntime
from spectragent.schemas.action import Action, ActionDescriptor, ActionResult
from tests._helpers.assembly import make_env

# ── A non-browser capability, of the shape τ-bench needs ──────────────


@dataclass
class CallToolAction(ActionBase):
    """A JSON tool call. No DOM, no tab, no screenshot."""

    __registry_key__ = "f8_sandbox.call_tool"

    #: Every keyword this action was handed beyond ``params`` and
    #: ``capability``. Must stay empty: anything else is a leak from a
    #: neighbouring domain.
    seen_extra: list[dict[str, Any]] = field(default_factory=list)

    def descriptor(self, **context: Any) -> ActionDescriptor:
        return ActionDescriptor(
            name="f8_sandbox.call_tool",
            description="Call a JSON tool.",
            params={"tool": "str"},
        )

    async def execute(
        self, params: dict[str, Any], *, capability: Any, **extra: Any
    ) -> ActionResult:
        self.seen_extra.append(extra)
        return ActionResult(
            success=True, extracted_content=f"called {params.get('tool')}"
        )


@dataclass
class ToolSandboxCapability(CapabilityBase):
    __registry_key__ = "f8_sandbox"

    def __post_init__(self) -> None:
        self.action = CallToolAction()
        self._actions_by_name = {self.action.action_id: self.action}

    def get_action_descriptors(self, **context: Any) -> list[ActionDescriptor]:
        return [self.action.descriptor(**context)]

    async def execute_action(self, action: Action) -> ActionResult:
        impl = self._actions_by_name.get(action.name)
        if impl is None:
            return ActionResult(success=False, error=f"Unknown action: {action.name}")
        return await impl.execute(action.params, capability=self)


# ── A stand-in for the browser: sub-context is its own state ──────────


@dataclass
class TabbedCapability(CapabilityBase):
    """Tracks a focused sub-context the way BrowserCapability does."""

    __registry_key__ = "f8_tabbed"

    def __post_init__(self) -> None:
        self._focused = "tab-1"
        self.acted_on: list[str] = []

    def get_action_descriptors(self, **context: Any) -> list[ActionDescriptor]:
        return [
            ActionDescriptor(name="f8_tabbed.click", description="Click"),
            ActionDescriptor(name="f8_tabbed.switch_tab", description="Switch"),
        ]

    async def execute_action(self, action: Action) -> ActionResult:
        if action.name == "f8_tabbed.switch_tab":
            self._focused = action.params["target"]
            return ActionResult(success=True)
        self.acted_on.append(self._focused)
        return ActionResult(success=True)


def _mixed_env() -> EnvironmentBase:
    return make_env(
        EnvironmentBase,
        {"f8_sandbox": ToolSandboxCapability(), "f8_tabbed": TabbedCapability()},
    )


# ── 1. The ABI itself ─────────────────────────────────────────────────


class TestNoRoutingHandleInTheABI:
    """The four layers §5.2.1 named, checked by signature."""

    def test_action_execute_takes_only_params_and_capability(self) -> None:
        params = inspect.signature(BaseAction.execute).parameters
        assert set(params) == {"self", "params", "capability"}

    @pytest.mark.parametrize("method", ["observe", "act"])
    @pytest.mark.parametrize(
        ("label", "owner"),
        [("Environment", Environment), ("EnvironmentBase", EnvironmentBase)],
    )
    def test_environment_has_no_session_param(
        self, label: str, owner: type, method: str
    ) -> None:
        params = inspect.signature(getattr(owner, method)).parameters
        assert "session_id" not in params, f"{label}.{method}"

    @pytest.mark.parametrize("method", ["prepare", "execute"])
    def test_runtime_has_no_session_param(self, method: str) -> None:
        params = inspect.signature(getattr(AgentRuntime, method)).parameters
        assert "session_id" not in params

    def test_capability_setup_declares_its_own_vocabulary(self) -> None:
        """No ``**kwargs`` sink on the base — that was the silent-drop hole."""
        params = inspect.signature(CapabilityBase.setup).parameters
        kinds = {p.kind for p in params.values()}
        assert inspect.Parameter.VAR_KEYWORD not in kinds


# ── 2. Damage direction one: browser handle into a tool sandbox ───────


class TestNonBrowserCapabilitySeesNoBrowserArgument:
    async def test_call_tool_receives_params_and_capability_only(self) -> None:
        env = _mixed_env()
        cap = env.capabilities["f8_sandbox"]
        assert isinstance(cap, ToolSandboxCapability)

        result = await env.act(
            Action(name="f8_sandbox.call_tool", params={"tool": "book_flight"})
        )

        assert result.success
        assert result.extracted_content == "called book_flight"
        # The whole point: nothing but its own two arguments arrived.
        assert cap.action.seen_extra == [{}]

    async def test_environment_act_refuses_a_routing_handle(self) -> None:
        """``env.act(action, session_id='CDP-TAB-ABC')`` is rejected at runtime.

        Built dynamically because the static form no longer type-checks —
        which is the point, and is covered by the signature test above.
        """
        env = _mixed_env()
        leaked: dict[str, Any] = {"session_id": "CDP-TAB-ABC"}
        with pytest.raises(TypeError):
            await env.act(
                Action(name="f8_sandbox.call_tool", params={"tool": "x"}),
                **leaked,
            )


# ── 3. Damage direction two: a run-scoped id into the browser ─────────


class TestSubContextIsCapabilityState:
    async def test_focus_moves_by_action_and_sticks(self) -> None:
        env = _mixed_env()
        cap = env.capabilities["f8_tabbed"]
        assert isinstance(cap, TabbedCapability)

        await env.act(Action(name="f8_tabbed.click"))
        await env.act(
            Action(name="f8_tabbed.switch_tab", params={"target": "tab-2"})
        )
        await env.act(Action(name="f8_tabbed.click"))

        # No caller ever named a tab; the capability remembered.
        assert cap.acted_on == ["tab-1", "tab-2"]

    async def test_one_capabilitys_focus_does_not_reach_the_other(self) -> None:
        """A shared scalar would have shoved 'tab-2' into CallToolAction."""
        env = _mixed_env()
        sandbox = env.capabilities["f8_sandbox"]
        assert isinstance(sandbox, ToolSandboxCapability)

        await env.act(
            Action(name="f8_tabbed.switch_tab", params={"target": "tab-2"})
        )
        await env.act(Action(name="f8_sandbox.call_tool", params={"tool": "t"}))

        assert sandbox.action.seen_extra == [{}]
