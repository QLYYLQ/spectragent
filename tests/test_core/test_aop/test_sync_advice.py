"""Tests for synchronous method advice support in ``AOPProxy``."""

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

    def test_sync_handler_exception_nonblocking_logged(self, caplog) -> None:
        def boom(*, args, kwargs, **ctx):
            raise RuntimeError("silent")

        wrapped = _build_wrapped_method(
            _sync_target,
            interceptors=[(_NonBlocking(), AdviceType.BEFORE, boom)],
            method_name="_sync_target",
        )

        with caplog.at_level("ERROR"):
            # Should not raise — non-blocking swallows and logs.
            assert wrapped(1, y=2) == 3
        assert any("nonblocking_sync" in rec.message for rec in caplog.records)


class TestSyncContractViolations:
    """Weaving sync targets with incompatible advice must fail fast."""

    def test_async_handler_on_sync_target_raises(self) -> None:
        with pytest.raises(UnsupportedAdviceError, match="synchronous method"):
            _build_wrapped_method(
                _sync_target,
                interceptors=[(_Blocking(), AdviceType.AFTER, _async_handler)],
                method_name="_sync_target",
            )

    def test_around_on_sync_target_raises(self) -> None:
        def around_sync(*, proceed, args, kwargs, **ctx):
            return proceed()

        with pytest.raises(UnsupportedAdviceError, match="around advice"):
            _build_wrapped_method(
                _sync_target,
                interceptors=[(_Blocking(), AdviceType.AROUND, around_sync)],
                method_name="_sync_target",
            )


# Sanity: ensure Intercept still round-trips advice for sync contracts too.
def test_intercept_handler_name_stable_for_sync() -> None:
    ic = Intercept("observe", role="env", advice="before")
    assert ic.handler_name == "before_observe"
