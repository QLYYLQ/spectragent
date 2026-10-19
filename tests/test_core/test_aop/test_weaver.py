"""Tests for the AOP Weaver and method interception."""

import asyncio

import pytest

from spectragent.core.aop.advice import Intercept
from spectragent.core.aop.errors import MissingHandlerError
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

    async def observe(self, negotiated=None):
        return {"dom": "tree", "negotiated": negotiated}

    async def act(self, action):
        return {"success": True}


class _MockLLM:
    inference_kind = "chat"

    async def ainvoke(self, messages, **kwargs):
        return {"completion": "hello", "usage": {"tokens": 42}}


class TestWeaverIndex:
    """Weaver builds intercept index from hooks."""

    def test_empty_hooks(self) -> None:
        weaver = Weaver([])
        assert weaver.intercepted_methods == {}

    def test_index_built(self) -> None:
        hook = _MockHook(
            "h1",
            [
                Intercept("observe", role="env", advice="after"),
            ],
        )
        weaver = Weaver([hook])
        assert "env" in weaver.intercepted_methods
        assert "observe" in weaver.intercepted_methods["env"]

    @pytest.mark.parametrize("handler", [None, 3])
    def test_missing_or_noncallable_handler_is_refused(self, handler) -> None:
        """Hook declares intercept but has no matching handler method."""
        hook = _MockHook(
            "h1",
            [
                Intercept("nonexistent_method", role="env", advice="after"),
            ],
        )
        if handler is not None:
            hook.__dict__["after_nonexistent_method"] = handler
        with pytest.raises(MissingHandlerError, match="after_nonexistent_method"):
            Weaver([hook])


class TestWeaverWrap:
    """Weaver.wrap() creates proxied objects."""

    def test_no_intercepts_returns_original(self) -> None:
        weaver = Weaver([])
        env = _MockEnv()
        wrapped = weaver.wrap(env, role="env")
        assert wrapped is env  # No wrapping needed

    def test_wrapped_preserves_non_intercepted(self) -> None:
        hook = _MockHook(
            "h1",
            [
                Intercept("observe", role="env", advice="after"),
            ],
        )
        weaver = Weaver([hook])
        env = _MockEnv()
        wrapped = weaver.wrap(env, role="env")
        # act() is not intercepted, should still work
        result = asyncio.run(wrapped.act({"name": "click"}))
        assert result["success"] is True


class TestAfterAdvice:
    """After advice fires after the original method."""

    def test_after_observe(self) -> None:
        hook = _MockHook(
            "h1",
            [
                Intercept("observe", role="env", advice="after"),
            ],
        )
        weaver = Weaver([hook])
        env = _MockEnv()
        wrapped = weaver.wrap(env, role="env")

        result = asyncio.run(wrapped.observe(negotiated="neg"))

        # Original result preserved
        assert result["dom"] == "tree"
        assert result["negotiated"] == "neg"

        # Hook was called
        assert len(hook.calls) == 1
        assert hook.calls[0][0] == "after_observe"

    def test_after_ainvoke(self) -> None:
        hook = _MockHook(
            "h1",
            [
                Intercept("ainvoke", role="llm", advice="after"),
            ],
        )
        weaver = Weaver([hook])
        llm = _MockLLM()
        wrapped = weaver.wrap(llm, role="llm")

        result = asyncio.run(wrapped.ainvoke([{"role": "user", "content": "hi"}]))

        assert result["completion"] == "hello"
        assert len(hook.calls) == 1
        assert hook.calls[0][0] == "after_ainvoke"


class TestBeforeAdvice:
    """Before advice fires before the original method."""

    def test_before_observe(self) -> None:
        hook = _MockHook(
            "h1",
            [
                Intercept("observe", role="env", advice="before"),
            ],
        )
        weaver = Weaver([hook])
        env = _MockEnv()
        wrapped = weaver.wrap(env, role="env")

        result = asyncio.run(wrapped.observe())

        assert result["dom"] == "tree"
        assert len(hook.calls) == 1
        assert hook.calls[0][0] == "before_observe"


class TestMultipleHooks:
    """Multiple hooks on the same method."""

    def test_two_after_hooks(self) -> None:
        hook1 = _MockHook(
            "h1",
            [
                Intercept("observe", role="env", advice="after"),
            ],
        )
        hook2 = _MockHook(
            "h2",
            [
                Intercept("observe", role="env", advice="after"),
            ],
        )
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


class TestRoleWithMultipleTargets:
    async def test_loop_phase_and_step_sink_are_audited_together(self):
        calls = []

        class Loop:
            async def execute_step(self):
                calls.append("execute")

        class Sink:
            async def record_step(self):
                calls.append("record")

        class Hook:
            hook_id = "step_boundaries"
            blocking = True
            __intercepts__ = [
                Intercept("execute_step", role="loop", advice="before"),
                Intercept("record_step", role="loop", advice="before"),
            ]

            async def before_execute_step(self, **ctx):
                calls.append("before_execute")

            async def before_record_step(self, **ctx):
                calls.append("before_record")

        weaver = Weaver([Hook()])
        loop = weaver.wrap(Loop(), role="loop")
        sink = weaver.wrap(Sink(), role="loop")
        weaver.raise_for_unclaimed_roles()
        weaver.audit_intercepts()
        await loop.execute_step()
        await sink.record_step()
        assert calls == ["before_execute", "execute", "before_record", "record"]

    def test_one_invalid_target_cannot_hide_behind_a_valid_target(self):
        from spectragent.core.aop.errors import DeadInterceptError

        class SynchronousModel:
            inference_kind = "chat"

            def ainvoke(self, messages):
                return "wrong shape"

        hook = _MockHook("observer", [Intercept("ainvoke", role="llm", advice="after")])
        weaver = Weaver([hook])
        weaver.wrap(SynchronousModel(), role="llm")
        weaver.wrap(_MockLLM(), role="llm")
        with pytest.raises(DeadInterceptError):
            weaver.audit_intercepts()
