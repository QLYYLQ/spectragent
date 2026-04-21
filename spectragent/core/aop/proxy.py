"""Proxy creation — wraps target object methods with AOP advice.

The proxy delegates all attribute access to the original object, but
replaces intercepted methods with wrapped versions that execute
before/after/around hooks.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from spectragent.core.aop.advice import AdviceType
from spectragent.core.aop.errors import UnsupportedAdviceError

logger = logging.getLogger(__name__)

# Type for an interceptor entry: (hook_instance, advice_type, handler_fn)
InterceptorEntry = tuple[Any, AdviceType, Callable[..., Coroutine[Any, Any, Any]]]


class AOPProxy:
    """Transparent proxy that intercepts specific methods with AOP advice.

    All attribute access is delegated to the wrapped target, except for
    methods that have registered interceptors.
    """

    def __init__(
        self,
        target: Any,
        method_interceptors: dict[str, list[InterceptorEntry]],
    ) -> None:
        # Use object.__setattr__ to avoid triggering __setattr__ on the proxy
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_wrapped_methods", {})

        # Pre-build wrapped methods
        wrapped: dict[str, Any] = {}
        for method_name, interceptors in method_interceptors.items():
            original = getattr(target, method_name, None)
            if original is not None:
                wrapped[method_name] = _build_wrapped_method(original, interceptors, method_name)
        object.__setattr__(self, "_wrapped_methods", wrapped)

    def __getattr__(self, name: str) -> Any:
        wrapped = object.__getattribute__(self, "_wrapped_methods")
        if name in wrapped:
            return wrapped[name]
        target = object.__getattribute__(self, "_target")
        return getattr(target, name)

    def __repr__(self) -> str:
        target = object.__getattribute__(self, "_target")
        wrapped = object.__getattribute__(self, "_wrapped_methods")
        return f"AOPProxy({target!r}, intercepted={list(wrapped.keys())})"


def _build_wrapped_method(
    original: Callable[..., Any],
    interceptors: list[InterceptorEntry],
    method_name: str,
) -> Callable[..., Any]:
    """Build a wrapped async method with before/after/around advice."""

    before_hooks = [(h, fn) for h, adv, fn in interceptors if adv == AdviceType.BEFORE]
    after_hooks = [(h, fn) for h, adv, fn in interceptors if adv == AdviceType.AFTER]
    around_hooks = [(h, fn) for h, adv, fn in interceptors if adv == AdviceType.AROUND]

    if asyncio.iscoroutinefunction(original):

        @functools.wraps(original)
        async def wrapped_async(*args: Any, **kwargs: Any) -> Any:
            # Around hooks wrap the entire chain
            if around_hooks:
                return await _apply_around_chain(
                    around_hooks,
                    original,
                    before_hooks,
                    after_hooks,
                    method_name,
                    args,
                    kwargs,
                )

            # Before hooks
            for hook, handler in before_hooks:
                if hook.blocking:
                    await handler(args=args, kwargs=kwargs)
                else:
                    asyncio.create_task(
                        _safe_call(handler, hook, method_name, "before", args=args, kwargs=kwargs)
                    )

            # Original call
            result = await original(*args, **kwargs)

            # After hooks
            for hook, handler in after_hooks:
                if hook.blocking:
                    await handler(result, args=args, kwargs=kwargs)
                else:
                    asyncio.create_task(
                        _safe_call(
                            handler, hook, method_name, "after", result, args=args, kwargs=kwargs
                        )
                    )

            return result

        return wrapped_async

    else:
        # Synchronous target method — all handlers must be plain callables.
        # Nested ``asyncio.run`` on an already-running loop would deadlock,
        # and ``around`` advice is also ill-defined on sync methods.
        if around_hooks:
            raise UnsupportedAdviceError(
                f"around advice is not supported on synchronous method "
                f"{method_name!r} (around chains must be async)."
            )
        for _hook, fn in before_hooks + after_hooks:
            if asyncio.iscoroutinefunction(fn):
                raise UnsupportedAdviceError(
                    f"Async handler {fn.__qualname__!r} cannot intercept "
                    f"synchronous method {method_name!r}. Make the handler "
                    f"a plain ``def`` or the target method ``async def``."
                )

        @functools.wraps(original)
        def wrapped_sync(*args: Any, **kwargs: Any) -> Any:
            for hook, handler in before_hooks:
                try:
                    handler(args=args, kwargs=kwargs)
                except Exception:
                    if hook.blocking:
                        raise
                    hook_id = getattr(hook, "hook_id", type(hook).__name__)
                    logger.exception(
                        "Non-blocking before hook '%s' failed on sync method '%s'",
                        hook_id,
                        method_name,
                    )

            result = original(*args, **kwargs)

            for hook, handler in after_hooks:
                try:
                    handler(result, args=args, kwargs=kwargs)
                except Exception:
                    if hook.blocking:
                        raise
                    hook_id = getattr(hook, "hook_id", type(hook).__name__)
                    logger.exception(
                        "Non-blocking after hook '%s' failed on sync method '%s'",
                        hook_id,
                        method_name,
                    )

            return result

        return wrapped_sync


async def _apply_around_chain(
    around_hooks: list[tuple[Any, Callable[..., Any]]],
    original: Callable[..., Any],
    before_hooks: list[tuple[Any, Callable[..., Any]]],
    after_hooks: list[tuple[Any, Callable[..., Any]]],
    method_name: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """Apply around hooks as a chain: each can call ``proceed()``."""

    async def innermost() -> Any:
        # Before hooks
        for hook, handler in before_hooks:
            if hook.blocking:
                await handler(args=args, kwargs=kwargs)
            else:
                asyncio.create_task(
                    _safe_call(handler, hook, method_name, "before", args=args, kwargs=kwargs)
                )

        result = await original(*args, **kwargs)

        # After hooks
        for hook, handler in after_hooks:
            if hook.blocking:
                await handler(result, args=args, kwargs=kwargs)
            else:
                asyncio.create_task(
                    _safe_call(
                        handler, hook, method_name, "after", result, args=args, kwargs=kwargs
                    )
                )

        return result

    # Build the around chain from innermost outward
    proceed = innermost
    for hook, handler in reversed(around_hooks):
        current_proceed = proceed

        async def make_around(h: Any, fn: Any, p: Any) -> Any:
            return await fn(proceed=p, args=args, kwargs=kwargs)

        proceed = lambda _h=hook, _fn=handler, _p=current_proceed: make_around(_h, _fn, _p)  # noqa: E731

    return await proceed()


async def _safe_call(
    handler: Callable[..., Any],
    hook: Any,
    method_name: str,
    advice_type: str,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Call a non-blocking hook handler, catching and logging exceptions."""
    try:
        await handler(*args, **kwargs)
    except Exception:
        hook_id = getattr(hook, "hook_id", type(hook).__name__)
        logger.exception(
            "Non-blocking %s hook '%s' failed on method '%s'",
            advice_type,
            hook_id,
            method_name,
        )
