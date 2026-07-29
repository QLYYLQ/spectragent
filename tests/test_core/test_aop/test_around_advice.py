"""Async ``around`` advice — the chain, the nesting, the short-circuit.

The async around path shipped with zero tests. It turned out to be
correct, which is exactly why it needed pinning: nothing was stopping
the next refactor from breaking the ordering, the ``proceed()``
short-circuit, or the closure binding that gives each link in the chain
its own handler.
"""

from __future__ import annotations

import pytest

from spectragent.core.aop.advice import AdviceType, Intercept
from spectragent.core.aop.proxy import _build_wrapped_method
from spectragent.core.aop.weaver import Weaver


class _Blocking:
    blocking = True
    hook_id = "blocking_async"


async def _target(x: int, y: int = 0) -> int:
    return x + y


class TestAsyncAroundChain:
    async def test_single_around_sees_args_ctx_and_result(self) -> None:
        calls: list[str] = []

        async def around(*, proceed, args, kwargs, **ctx):
            calls.append(f"enter args={args} kwargs={kwargs} ctx={sorted(ctx)}")
            result = await proceed()
            calls.append(f"exit result={result}")
            return result * 10

        wrapped = _build_wrapped_method(
            _target,
            interceptors=[(_Blocking(), AdviceType.AROUND, around)],
            method_name="_target",
        )

        assert await wrapped(3, y=4) == 70
        assert calls == [
            "enter args=(3,) kwargs={'y': 4} ctx=['method', 'role', 'target']",
            "exit result=7",
        ]

    async def test_two_arounds_nest_in_declaration_order(self) -> None:
        calls: list[str] = []

        async def outer(*, proceed, args, kwargs, **ctx):
            calls.append("outer:enter")
            result = await proceed()
            calls.append("outer:exit")
            return result

        async def inner(*, proceed, args, kwargs, **ctx):
            calls.append("inner:enter")
            result = await proceed()
            calls.append("inner:exit")
            return result

        wrapped = _build_wrapped_method(
            _target,
            interceptors=[
                (_Blocking(), AdviceType.AROUND, outer),
                (_Blocking(), AdviceType.AROUND, inner),
            ],
            method_name="_target",
        )

        assert await wrapped(10, y=10) == 20
        assert calls == ["outer:enter", "inner:enter", "inner:exit", "outer:exit"]

    async def test_each_link_keeps_its_own_handler(self) -> None:
        """Three arounds: a closure over the loop variable would run one thrice."""
        calls: list[str] = []

        def make(name: str):
            async def around(*, proceed, args, kwargs, **ctx):
                calls.append(name)
                return await proceed()

            return around

        wrapped = _build_wrapped_method(
            _target,
            interceptors=[
                (_Blocking(), AdviceType.AROUND, make("a")),
                (_Blocking(), AdviceType.AROUND, make("b")),
                (_Blocking(), AdviceType.AROUND, make("c")),
            ],
            method_name="_target",
        )

        await wrapped(1)
        assert calls == ["a", "b", "c"]

    async def test_before_and_after_run_inside_the_innermost_around(self) -> None:
        calls: list[str] = []

        async def around(*, proceed, args, kwargs, **ctx):
            calls.append("around:enter")
            result = await proceed()
            calls.append("around:exit")
            return result

        async def before(*, args, kwargs, **ctx):
            calls.append("before")

        async def after(result, *, args, kwargs, **ctx):
            calls.append("after")

        wrapped = _build_wrapped_method(
            _target,
            interceptors=[
                (_Blocking(), AdviceType.AROUND, around),
                (_Blocking(), AdviceType.BEFORE, before),
                (_Blocking(), AdviceType.AFTER, after),
            ],
            method_name="_target",
        )

        assert await wrapped(10, y=10) == 20
        assert calls == ["around:enter", "before", "after", "around:exit"]

    async def test_not_calling_proceed_skips_target_and_inner_advice(self) -> None:
        ran: list[str] = []

        async def target(x: int) -> int:
            ran.append("target")
            return x

        async def before(*, args, kwargs, **ctx):
            ran.append("before")

        async def cache_hit(*, proceed, args, kwargs, **ctx):
            ran.append("cache_hit")
            return 999

        wrapped = _build_wrapped_method(
            target,
            interceptors=[
                (_Blocking(), AdviceType.AROUND, cache_hit),
                (_Blocking(), AdviceType.BEFORE, before),
            ],
            method_name="target",
        )

        assert await wrapped(1) == 999
        assert ran == ["cache_hit"]

    async def test_target_exception_surfaces_at_proceed(self) -> None:
        async def target() -> int:
            raise ValueError("boom")

        async def recover(*, proceed, args, kwargs, **ctx):
            try:
                return await proceed()
            except ValueError:
                return "recovered"

        wrapped = _build_wrapped_method(
            target,
            interceptors=[(_Blocking(), AdviceType.AROUND, recover)],
            method_name="target",
        )

        assert await wrapped() == "recovered"

    async def test_around_handler_exception_propagates(self) -> None:
        async def around(*, proceed, args, kwargs, **ctx):
            raise RuntimeError("around boom")

        wrapped = _build_wrapped_method(
            _target,
            interceptors=[(_Blocking(), AdviceType.AROUND, around)],
            method_name="_target",
        )

        with pytest.raises(RuntimeError, match="around boom"):
            await wrapped(1)


class TestAroundThroughTheWeaver:
    """The full path a hook actually takes: Intercept → Weaver → proxy."""

    async def test_around_ainvoke(self) -> None:
        class _LLM:
            async def ainvoke(self, messages, **kwargs):
                return "completion"

        class _AuditHook:
            hook_id = "audit"
            blocking = True
            __intercepts__ = [Intercept("ainvoke", role="llm", advice="around")]

            def __init__(self) -> None:
                self.seen: list = []

            async def around_ainvoke(self, *, proceed, args, kwargs, **ctx):
                self.seen.append((args, ctx["role"], ctx["method"]))
                return await proceed()

        hook = _AuditHook()
        wrapped = Weaver([hook]).wrap(_LLM(), role="llm")

        assert await wrapped.ainvoke(["m"]) == "completion"
        assert hook.seen == [((["m"],), "llm", "ainvoke")]
