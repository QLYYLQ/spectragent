"""Synchronous method advice in ``AOPProxy`` — before, after *and* around.

``around`` on a sync method is not an edge case: it is the framework's
only documented around use case (``agent.build_react_prompt``, a plain
``def`` on every shipped agent — docs/architecture_design.md §4.1).
Refusing it made the documented plug point structurally impossible.
"""

from __future__ import annotations

import pytest

from spectragent.core.aop.advice import AdviceType, Intercept
from spectragent.core.aop.errors import UnsupportedAdviceError
from spectragent.core.aop.proxy import _build_wrapped_method


class _Blocking:
    blocking = True
    hook_id = "blocking_sync"


class _NonBlocking:
    blocking = False
    hook_id = "nonblocking_sync"


def _sync_target(x: int, y: int = 0) -> int:
    return x + y


async def _async_handler(result, *, args, kwargs, **ctx):  # pragma: no cover
    return result


class TestSyncAdvice:
    """Sync target + sync handlers: before/after run inline."""

    def test_sync_before_and_after_execute(self) -> None:
        calls: list[str] = []

        def before(*, args, kwargs, **ctx):
            calls.append(f"before:{args}")

        def after(result, *, args, kwargs, **ctx):
            calls.append(f"after:{result}")

        wrapped = _build_wrapped_method(
            _sync_target,
            interceptors=[
                (_Blocking(), AdviceType.BEFORE, before),
                (_Blocking(), AdviceType.AFTER, after),
            ],
            method_name="_sync_target",
        )

        result = wrapped(3, y=4)

        assert result == 7
        assert calls == ["before:(3,)", "after:7"]

    def test_sync_handlers_receive_target_role_method(self) -> None:
        """Handlers must be able to attribute a call to its target."""
        seen: list[dict] = []

        def after(result, *, args, kwargs, **ctx):
            seen.append(ctx)

        target = object()
        wrapped = _build_wrapped_method(
            _sync_target,
            interceptors=[(_Blocking(), AdviceType.AFTER, after)],
            method_name="_sync_target",
            target=target,
            role="env",
        )
        wrapped(1)

        assert seen == [{"target": target, "role": "env", "method": "_sync_target"}]

    def test_sync_handler_exception_blocking_propagates(self) -> None:
        def boom(*, args, kwargs, **ctx):
            raise RuntimeError("nope")

        wrapped = _build_wrapped_method(
            _sync_target,
            interceptors=[(_Blocking(), AdviceType.BEFORE, boom)],
            method_name="_sync_target",
        )

        with pytest.raises(RuntimeError, match="nope"):
            wrapped(1)


class TestSyncAround:
    """``around`` on a sync target: ``proceed()`` returns a value."""

    def test_single_around_wraps_the_call(self) -> None:
        calls: list[str] = []

        def around(*, proceed, args, kwargs, **ctx):
            calls.append(f"enter:{args}:{ctx['method']}")
            result = proceed()
            calls.append(f"exit:{result}")
            return result * 10

        wrapped = _build_wrapped_method(
            _sync_target,
            interceptors=[(_Blocking(), AdviceType.AROUND, around)],
            method_name="_sync_target",
        )

        assert wrapped(3, y=4) == 70
        assert calls == ["enter:(3,):_sync_target", "exit:7"]

    def test_two_arounds_nest_outermost_first(self) -> None:
        calls: list[str] = []

        def outer(*, proceed, args, kwargs, **ctx):
            calls.append("outer:enter")
            result = proceed()
            calls.append("outer:exit")
            return result

        def inner(*, proceed, args, kwargs, **ctx):
            calls.append("inner:enter")
            result = proceed()
            calls.append("inner:exit")
            return result

        wrapped = _build_wrapped_method(
            _sync_target,
            interceptors=[
                (_Blocking(), AdviceType.AROUND, outer),
                (_Blocking(), AdviceType.AROUND, inner),
            ],
            method_name="_sync_target",
        )

        assert wrapped(1, y=1) == 2
        assert calls == ["outer:enter", "inner:enter", "inner:exit", "outer:exit"]

    def test_before_and_after_nest_inside_the_innermost_around(self) -> None:
        calls: list[str] = []

        def around(*, proceed, args, kwargs, **ctx):
            calls.append("around:enter")
            result = proceed()
            calls.append("around:exit")
            return result

        def before(*, args, kwargs, **ctx):
            calls.append("before")

        def after(result, *, args, kwargs, **ctx):
            calls.append("after")

        wrapped = _build_wrapped_method(
            _sync_target,
            interceptors=[
                (_Blocking(), AdviceType.AROUND, around),
                (_Blocking(), AdviceType.BEFORE, before),
                (_Blocking(), AdviceType.AFTER, after),
            ],
            method_name="_sync_target",
        )

        assert wrapped(2, y=3) == 5
        assert calls == ["around:enter", "before", "after", "around:exit"]

    def test_not_calling_proceed_short_circuits_the_target(self) -> None:
        ran: list[str] = []

        def target(x: int) -> int:
            ran.append("target")
            return x

        def cache_hit(*, proceed, args, kwargs, **ctx):
            return 999

        wrapped = _build_wrapped_method(
            target,
            interceptors=[(_Blocking(), AdviceType.AROUND, cache_hit)],
            method_name="target",
        )

        assert wrapped(1) == 999
        assert ran == []

    def test_synchronous_prompt_builder_can_be_audited(self) -> None:
        """Generic synchronous builders support around advice; ReAct uses an async builder."""

        class _Agent:
            def build_prompt(self, obs):
                return [{"role": "user", "content": obs}]

        audited: list[list] = []

        def around_build_prompt(*, proceed, args, kwargs, **ctx):
            messages = proceed()
            audited.append(messages)
            return messages

        agent = _Agent()
        wrapped = _build_wrapped_method(
            agent.build_prompt,
            interceptors=[(_Blocking(), AdviceType.AROUND, around_build_prompt)],
            method_name="build_prompt",
            target=agent,
            role="agent",
        )

        assert wrapped("dom") == [{"role": "user", "content": "dom"}]
        assert audited == [[{"role": "user", "content": "dom"}]]


class TestSyncContractViolations:
    """Weaving sync targets with incompatible advice must fail fast."""

    def test_async_handler_on_sync_target_raises(self) -> None:
        with pytest.raises(UnsupportedAdviceError, match="synchronous method"):
            _build_wrapped_method(
                _sync_target,
                interceptors=[(_Blocking(), AdviceType.AFTER, _async_handler)],
                method_name="_sync_target",
            )

    def test_nonblocking_hook_on_sync_target_raises(self) -> None:
        """``blocking`` means concurrency, and a sync call has none to offer.

        Running the handler inline anyway — the old behaviour — made
        ``blocking`` mean "error isolation" here and "concurrency" on the
        async path, so a hook author could not know what they had asked
        for.
        """

        def before(*, args, kwargs, **ctx):
            pass

        with pytest.raises(UnsupportedAdviceError, match="non-blocking"):
            _build_wrapped_method(
                _sync_target,
                interceptors=[(_NonBlocking(), AdviceType.BEFORE, before)],
                method_name="_sync_target",
            )

    def test_nonblocking_around_raises_on_async_target_too(self) -> None:
        """An around handler *is* the call; nothing can proceed without it."""

        async def target():  # pragma: no cover - never called
            return 1

        async def around(*, proceed, args, kwargs, **ctx):  # pragma: no cover
            return await proceed()

        with pytest.raises(UnsupportedAdviceError, match="around advice"):
            _build_wrapped_method(
                target,
                interceptors=[(_NonBlocking(), AdviceType.AROUND, around)],
                method_name="target",
            )

    def test_sync_handler_on_async_target_raises(self) -> None:
        """The async branch had no shape guard at all — it died at call time."""

        async def target():  # pragma: no cover - never called
            return 1

        def handler(result, *, args, kwargs, **ctx):  # pragma: no cover
            return result

        with pytest.raises(UnsupportedAdviceError, match="plain ``def``"):
            _build_wrapped_method(
                target,
                interceptors=[(_Blocking(), AdviceType.AFTER, handler)],
                method_name="target",
            )


# Sanity: ensure Intercept still round-trips advice for sync contracts too.
def test_intercept_handler_name_stable_for_sync() -> None:
    ic = Intercept("observe", role="env", advice="before")
    assert ic.handler_name == "before_observe"
