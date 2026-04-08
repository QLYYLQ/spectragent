"""Tests for the AOP Weaver and method interception."""

import asyncio

import pytest

from spectragent.core.aop.advice import AdviceType, Intercept
from spectragent.core.aop.weaver import Weaver


class _MockHook:
    """Minimal mock hook for testing."""

    def __init__(self, hook_id: str, intercepts: list, blocking: bool = True):
        self.hook_id = hook_id
        self.__intercepts__ = intercepts
        self.blocking = blocking
        self.calls: list[tuple[str, ...]] = []

    async def after_observe(self, result, *, args, kwargs, **ctx):
        self.calls.append(("after_observe", str(result)))

    async def before_observe(self, *, args, kwargs, **ctx):
        self.calls.append(("before_observe",))

    async def after_ainvoke(self, result, *, args, kwargs, **ctx):
        self.calls.append(("after_ainvoke", str(result)))


class _MockEnv:
    """Mock environment for testing AOP wrapping."""

    async def observe(self, session_id=None):
        return {"dom": "tree", "session": session_id}

    async def act(self, action, session_id=None):
        return {"success": True}


class _MockLLM:
    async def ainvoke(self, messages, **kwargs):
        return {"completion": "hello", "usage": {"tokens": 42}}


class TestWeaverIndex:
    """Weaver builds intercept index from hooks."""

    def test_empty_hooks(self) -> None:
        weaver = Weaver([])
        assert weaver.intercepted_methods == {}

    def test_index_built(self) -> None:
        hook = _MockHook("h1", [
            Intercept("observe", role="env", advice="after"),
        ])
        weaver = Weaver([hook])
        assert "env" in weaver.intercepted_methods
        assert "observe" in weaver.intercepted_methods["env"]

    def test_missing_handler_warned(self) -> None:
        """Hook declares intercept but has no matching handler method."""
        hook = _MockHook("h1", [
            Intercept("nonexistent_method", role="env", advice="after"),
        ])
        weaver = Weaver([hook])
        # Should not crash, just warn (logged)
        assert weaver.intercepted_methods == {}


class TestWeaverWrap:
    """Weaver.wrap() creates proxied objects."""

    def test_no_intercepts_returns_original(self) -> None:
        weaver = Weaver([])
        env = _MockEnv()
        wrapped = weaver.wrap(env, role="env")
        assert wrapped is env  # No wrapping needed

    def test_wrapped_preserves_non_intercepted(self) -> None:
        hook = _MockHook("h1", [
            Intercept("observe", role="env", advice="after"),
        ])
        weaver = Weaver([hook])
        env = _MockEnv()
        wrapped = weaver.wrap(env, role="env")
        # act() is not intercepted, should still work
        result = asyncio.run(
            wrapped.act({"name": "click"})
        )
        assert result["success"] is True


class TestAfterAdvice:
    """After advice fires after the original method."""

    def test_after_observe(self) -> None:
        hook = _MockHook("h1", [
            Intercept("observe", role="env", advice="after"),
        ])
        weaver = Weaver([hook])
        env = _MockEnv()
        wrapped = weaver.wrap(env, role="env")

        result = asyncio.run(
            wrapped.observe(session_id="tab1")
        )

        # Original result preserved
        assert result["dom"] == "tree"
        assert result["session"] == "tab1"

        # Hook was called
        assert len(hook.calls) == 1
        assert hook.calls[0][0] == "after_observe"

    def test_after_ainvoke(self) -> None:
        hook = _MockHook("h1", [
            Intercept("ainvoke", role="llm", advice="after"),
        ])
        weaver = Weaver([hook])
        llm = _MockLLM()
        wrapped = weaver.wrap(llm, role="llm")

        result = asyncio.run(
            wrapped.ainvoke([{"role": "user", "content": "hi"}])
        )

        assert result["completion"] == "hello"
        assert len(hook.calls) == 1
        assert hook.calls[0][0] == "after_ainvoke"


class TestBeforeAdvice:
    """Before advice fires before the original method."""

    def test_before_observe(self) -> None:
        hook = _MockHook("h1", [
            Intercept("observe", role="env", advice="before"),
        ])
        weaver = Weaver([hook])
        env = _MockEnv()
        wrapped = weaver.wrap(env, role="env")

        result = asyncio.run(
            wrapped.observe()
        )

        assert result["dom"] == "tree"
        assert len(hook.calls) == 1
        assert hook.calls[0][0] == "before_observe"


class TestMultipleHooks:
    """Multiple hooks on the same method."""

    def test_two_after_hooks(self) -> None:
        hook1 = _MockHook("h1", [
            Intercept("observe", role="env", advice="after"),
        ])
        hook2 = _MockHook("h2", [
            Intercept("observe", role="env", advice="after"),
        ])
        weaver = Weaver([hook1, hook2])
        env = _MockEnv()
        wrapped = weaver.wrap(env, role="env")

        asyncio.run(wrapped.observe())

        assert len(hook1.calls) == 1
        assert len(hook2.calls) == 1


class TestBlockingVsNonBlocking:
    """Blocking hooks are awaited; non-blocking use create_task."""

    def test_blocking_error_propagates(self) -> None:
        class ErrorHook:
            hook_id = "error_hook"
            blocking = True
            __intercepts__ = [
                Intercept("observe", role="env", advice="after"),
            ]

            async def after_observe(self, result, *, args, kwargs, **ctx):
                raise ValueError("hook failed")

        weaver = Weaver([ErrorHook()])
        env = _MockEnv()
        wrapped = weaver.wrap(env, role="env")

        with pytest.raises(ValueError, match="hook failed"):
            asyncio.run(wrapped.observe())
