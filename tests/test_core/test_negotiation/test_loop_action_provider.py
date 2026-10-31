"""The Loop provides the actions it executes itself — and only those are on offer beside the environment's.

``docs/architecture_design.md`` §4.5 "Loop 提供的动作": the action universe is
the environment's actions plus the ``loop.*`` actions the selected Loop declares
(``__loop_actions__``; ``AgentLoopBase`` declares ``loop.done``). Relative names
bind and collide exactly as before: an agent's ``done`` binds to ``loop.done``,
and a capability that also offers a ``done`` makes the relative name ambiguous.
An agent that requires a registered loop action the Loop does not provide is
refused at ``prepare()``, naming the action and the Loop — the mirror of a
loop-source fragment the Loop does not build (§4.4).
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from spectragent.core.loops import AgentLoopRegistrar, ReactLoop
from spectragent.core.negotiation import IncompatibleActionError
from spectragent.core.negotiation.action import (
    AmbiguousActionError,
    _action_rules,
    negotiate_action,
)
from spectragent.core.runtime import AgentRuntime
from tests._helpers.archive_fakes import (
    ContractArchiveLLM,
    ContractArchiveMemory,
    make_archive_env,
)
from tests._helpers.assembly import AGENT_ID, make_run

pytestmark = pytest.mark.usefixtures("archive_fakes")


class _NoActionsLoop(ReactLoop):
    """A ReAct loop that provides no action of its own."""

    __registry_key__ = "test_loop_actions_none"
    __loop_actions__ = frozenset()


def teardown_module() -> None:
    # Idempotent: pytest tears a module down once per contiguous run of its
    # tests, so a selection that interleaves files tears this one down twice.
    if AgentLoopRegistrar.get_or_none("test_loop_actions_none") is not None:
        AgentLoopRegistrar.unregister("test_loop_actions_none")


class _ClickAgent:
    """Clicks, and ends the run with ``done``."""

    __registry_key__ = "click_agent"
    __wiring__: ClassVar[dict[str, Any]] = {"action": ("action", ["click", "done"])}

    __execution_limit_fields__ = frozenset()

    async def build_react_prompt(self, obs: Any, *, context) -> list[dict[str, str]]:
        return [{"role": "user", "content": "go"}]

    def parse_react_output(self, out: Any) -> list[Any]:
        return []


class _DoneOnlyAgent(_ClickAgent):
    """Only ends the run."""

    __registry_key__ = "done_only_agent"
    __wiring__: ClassVar[dict[str, Any]] = {"action": ("action", ["done"])}


class _Env:
    def __init__(self, *actions: str) -> None:
        self.provided_actions = frozenset(actions)


# ── negotiation ───────────────────────────────────────────────────────


class TestNegotiation:
    def test_done_binds_to_the_loops(self) -> None:
        result = negotiate_action(_ClickAgent(), ReactLoop(), _Env("browser.click", "browser.wait"))
        assert result.active_actions == frozenset({"browser.click", "loop.done"})
        assert dict(result.action_bindings) == {"click": "browser.click", "done": "loop.done"}

    def test_a_capability_offering_done_too_makes_the_relative_name_ambiguous(self) -> None:
        with pytest.raises(AmbiguousActionError) as info:
            negotiate_action(_ClickAgent(), ReactLoop(), _Env("browser.click", "desktop.done"))
        assert info.value.declared == "done"
        assert info.value.candidates == ("desktop.done", "loop.done")

    def test_pinning_the_loops_done_resolves_it(self) -> None:
        class _PinnedAgent(_ClickAgent):
            __wiring__: ClassVar[dict[str, Any]] = {
                "action": ("action", ["click", "loop.done"]),
            }

        result = negotiate_action(
            _PinnedAgent(), ReactLoop(), _Env("browser.click", "desktop.done")
        )
        assert result.active_actions == frozenset({"browser.click", "loop.done"})

    def test_a_required_action_the_loop_does_not_provide_names_the_loop(self) -> None:
        with pytest.raises(IncompatibleActionError) as info:
            negotiate_action(_ClickAgent(), _NoActionsLoop(), _Env("browser.click"))
        error = info.value
        assert error.missing == {"done"}
        assert error.provider == "loop 'test_loop_actions_none'"
        assert error.provided == set()
        assert error.requested_by == {"done": ("click_agent",)}
        assert str(error) == (
            "Required actions not provided by loop 'test_loop_actions_none'. Missing: ['done']. "
            "Requested by: done ← click_agent. Loop 'test_loop_actions_none' provides: []."
        )

    def test_a_required_action_no_one_provides_is_the_environments(self) -> None:
        class _FlyingAgent(_ClickAgent):
            __wiring__: ClassVar[dict[str, Any]] = {"action": ("action", ["fly", "done"])}

        with pytest.raises(IncompatibleActionError) as info:
            negotiate_action(_FlyingAgent(), ReactLoop(), _Env("browser.click"))
        assert info.value.missing == {"fly"}
        assert info.value.provider == "environment"
        assert info.value.provided == {"browser.click"}
        assert "ablation" in str(info.value)

    def test_an_optional_one_it_does_not_provide_is_left_out(self) -> None:
        class _MaybeDoneAgent(_ClickAgent):
            __wiring__: ClassVar[dict[str, Any]] = {
                "action": ("action", ["click"], ["done"]),
            }

        result = negotiate_action(_MaybeDoneAgent(), _NoActionsLoop(), _Env("browser.click"))
        assert result.active_actions == frozenset({"browser.click"})

    def test_an_env_less_run_offers_the_loops_actions_alone(self) -> None:
        result = negotiate_action(_DoneOnlyAgent(), ReactLoop(), None)
        assert result.active_actions == frozenset({"loop.done"})

    def test_the_bare_rules_without_a_loop_offer_every_registered_one(self) -> None:
        result = _action_rules(agent=({"done"}, set()), env_provides=set())
        assert result.active_actions == frozenset({"loop.done"})


class TestPrepare:
    def test_prepare_refuses_it_before_anything_runs(self) -> None:
        run = make_run(
            agent=_DoneOnlyAgent(),
            loop=_NoActionsLoop(max_steps=1),
            env=make_archive_env(),
            llm=ContractArchiveLLM(),
            memory=ContractArchiveMemory(),
        )
        with pytest.raises(IncompatibleActionError, match="not provided by loop"):
            AgentRuntime.for_agent(run, AGENT_ID).prepare()
