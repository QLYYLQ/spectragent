"""The actions a Loop provides and executes itself: ``loop.done``.

``docs/architecture_design.md`` §4.5 "Loop 提供的动作": ``done`` ends the run
with the agent's answer and its claim and never directs the environment, so
the Loop provides it. A Loop declares its actions in ``__loop_actions__``
(``AgentLoopBase``: ``loop.done``), describes the active ones and executes
them; the guard hands it every ``loop.*`` action and none reaches ``env.act``.
The action's params and result are what ``browser.done``'s were: its
``success`` says it executed, and the agent's claim about the task is
``reported_success``, ``None`` when the agent made none (§4.1 "agent 的任务结论").
"""

from __future__ import annotations

import pytest

from spectragent.core.actions._registrar import is_loop_action, registered_params
from spectragent.core.actions.params import claim_params
from spectragent.core.loops import AgentLoopBase, BranchLoop, ReactLoop
from spectragent.core.loops.actions import LOOP_ACTION_KEYS, DoneAction, loop_action
from spectragent.core.negotiation.action import loop_actions
from spectragent.schemas.action import Action

# ── the action ────────────────────────────────────────────────────────


class TestDoneAction:
    def test_it_is_the_loops_and_registered(self) -> None:
        assert DoneAction().action_id == "loop.done"
        assert is_loop_action("loop.done") and not is_loop_action("browser.wait")
        assert registered_params("loop.done") is DoneAction.Params
        assert DoneAction.Effect is None

    def test_its_descriptor_takes_the_answer_and_the_claim(self) -> None:
        descriptor = DoneAction().descriptor()
        assert descriptor.name == "loop.done"
        assert set(descriptor.params) == {"extracted_content", "success"}
        assert claim_params(DoneAction.Params) == ("success",)
        assert DoneAction.Params.model_fields["extracted_content"].is_required()

    def test_the_success_param_says_what_it_means(self) -> None:
        """A model that sees params is told the param is a claim, and that it may leave it unset."""
        text = DoneAction().descriptor().params["success"]
        assert "whether the task was accomplished" in text
        assert "leave it unset if you cannot tell" in text

    async def test_it_ends_the_run_with_the_answer(self) -> None:
        result = await DoneAction().execute({"extracted_content": "42"})
        assert result.is_done is True
        assert result.success is True
        assert result.extracted_content == "42"
        assert result.effect is None

    @pytest.mark.parametrize("claim", [True, False, None])
    async def test_the_claim_is_carried_apart_from_execution(self, claim: bool | None) -> None:
        """An executed done always succeeded; what the agent said about the task rides beside it."""
        params: dict[str, object] = {"extracted_content": "answer"}
        if claim is not None:
            params["success"] = claim
        result = await DoneAction().execute(params)
        assert result.success is True
        assert result.reported_success is claim

    def test_only_a_registered_loop_action_is_built(self) -> None:
        assert isinstance(loop_action("loop.done"), DoneAction)
        with pytest.raises(TypeError, match="not a registered loop action"):
            loop_action("browser.wait")
        with pytest.raises(TypeError, match="not a registered loop action"):
            loop_action("loop.nothing")


# ── the declaration ───────────────────────────────────────────────────


class _NoActionsLoop(ReactLoop):
    """A ReAct loop that provides no action of its own."""

    __abstract__ = True
    # A plain set, as a subclass body may write it: frozen when the class is created.
    __loop_actions__ = set()  # type: ignore[assignment]


class TestTheDeclaration:
    def test_every_shipped_loop_provides_done(self) -> None:
        assert frozenset({"loop.done"}) == LOOP_ACTION_KEYS
        assert AgentLoopBase.__loop_actions__ == LOOP_ACTION_KEYS
        assert ReactLoop.__loop_actions__ == BranchLoop.__loop_actions__ == LOOP_ACTION_KEYS

    def test_a_declared_set_is_frozen(self) -> None:
        assert _NoActionsLoop.__loop_actions__ == frozenset()
        assert isinstance(_NoActionsLoop.__loop_actions__, frozenset)

    def test_a_bare_string_is_refused_where_it_is_written(self) -> None:
        with pytest.raises(TypeError, match="__loop_actions__ must be a set of 'loop.\\*'"):

            class _Typo(AgentLoopBase):
                __abstract__ = True
                __loop_actions__ = "loop.done"  # type: ignore[assignment]

    def test_a_key_the_guard_would_not_route_to_the_loop_is_refused(self) -> None:
        with pytest.raises(TypeError, match="__loop_actions__ must be a set of 'loop.\\*'"):

            class _Misrouted(AgentLoopBase):
                __abstract__ = True
                __loop_actions__ = {"browser.done"}  # type: ignore[assignment]

    def test_an_object_that_declares_nothing_provides_nothing(self) -> None:
        class _Structural:
            __registry_key__ = "structural_loop"

        assert loop_actions(_Structural()) == frozenset()
        assert loop_actions(ReactLoop()) == LOOP_ACTION_KEYS


# ── what the Loop does with them ──────────────────────────────────────


class TestTheLoopExecutesThem:
    def test_it_describes_the_active_ones_only(self) -> None:
        loop = ReactLoop()
        assert [d.name for d in loop.get_action_space(active_actions={"loop.done"})] == [
            "loop.done"
        ]
        assert loop.get_action_space(active_actions={"browser.click"}) == []

    def test_a_loop_that_provides_none_describes_none(self) -> None:
        class _Bare(_NoActionsLoop):
            __registry_key__ = "test_loop_actions_bare"

        try:
            assert _Bare().get_action_space(active_actions={"loop.done"}) == []
        finally:
            from spectragent.core.loops import AgentLoopRegistrar

            AgentLoopRegistrar.unregister("test_loop_actions_bare")

    async def test_it_executes_done(self) -> None:
        result = await ReactLoop().execute_action(
            Action(name="loop.done", params={"extracted_content": "a", "success": False})
        )
        assert result.is_done is True
        assert result.extracted_content == "a"
        assert result.reported_success is False

    async def test_a_name_it_does_not_provide_is_refused(self) -> None:
        result = await ReactLoop().execute_action(Action(name="browser.click", params={}))
        assert result.success is False
        assert result.error == (
            "loop 'react' does not execute 'browser.click' (its actions: ['loop.done'])"
        )
