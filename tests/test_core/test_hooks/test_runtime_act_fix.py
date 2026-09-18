"""Tests for runtime.py act_fn AOP wrapping order fix — M11.

Verifies that act_fn uses wrapped_env.act so AOP intercepts fire
when actions are executed through the guarded_act closure.
"""

import pytest

from spectragent.core.aop.advice import Intercept
from spectragent.core.aop.weaver import Weaver
from spectragent.core.hooks._registrar import HookBase


class _ActTracker(HookBase):
    """Hook that tracks after_act calls."""

    __registry_key__ = "_test_act_tracker"
    __intercepts__ = [
        Intercept("act", role="env", advice="after"),
    ]

    calls: list

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = []

    @property
    def blocking(self) -> bool:
        return True

    async def after_act(self, result, *, args, kwargs, **ctx):
        self.calls.append({"result": result, "args": args})


class _FakeEnv:
    """Minimal env with an async act method."""

    async def act(self, action, **kwargs):
        return f"acted:{action}"


class TestActFnAOPWrapping:
    @pytest.mark.asyncio
    async def test_wrapped_act_triggers_intercept(self):
        """After runtime fix, AOP intercepts on env.act should fire."""
        tracker = _ActTracker()
        weaver = Weaver([tracker])

        env = _FakeEnv()
        wrapped_env = weaver.wrap(env, role="env")

        # Simulate what runtime does after the fix:
        # act_fn = build_guarded_act(wrapped_env.act, ...)
        # For this test, just call wrapped_env.act directly
        result = await wrapped_env.act("click")
        assert result == "acted:click"
        assert len(tracker.calls) == 1
        assert tracker.calls[0]["result"] == "acted:click"

    @pytest.mark.asyncio
    async def test_unwrapped_act_does_not_trigger(self):
        """Before the fix: using unwrapped env.act misses AOP intercepts."""
        tracker = _ActTracker()
        weaver = Weaver([tracker])

        env = _FakeEnv()
        # Wrap env (this is what runtime does)
        _wrapped = weaver.wrap(env, role="env")

        # But if we call the original env.act (the old bug), tracker doesn't fire
        result = await env.act("click")
        assert result == "acted:click"
        assert len(tracker.calls) == 0  # No interception!

    @pytest.mark.asyncio
    async def test_guarded_act_with_wrapped_env(self):
        """Full simulation: build_guarded_act with wrapped_env.act."""
        from spectragent.core.action_guard import build_guarded_act
        from spectragent.schemas.action import Action, ActionDescriptor, ActionResult

        tracker = _ActTracker()
        weaver = Weaver([tracker])

        class _FullEnv:
            async def act(self, action, **kwargs):
                return ActionResult(
                    success=True,
                    is_done=False,
                    extracted_content=f"did:{action.name}",
                )

        env = _FullEnv()
        wrapped_env = weaver.wrap(env, role="env")

        descriptors = [
            ActionDescriptor(name="click", description="Click"),
        ]
        active_descs, act_fn = build_guarded_act(wrapped_env.act, descriptors, None)

        action = Action(name="click", params={"selector": "#btn"})
        result = await act_fn(action)

        assert result.extracted_content == "did:click"
        assert len(tracker.calls) == 1  # AOP fired!
