"""Proxy creation — wraps target object methods with AOP advice.

The proxy delegates all attribute access to the original object, but
replaces intercepted methods with wrapped versions that execute
before/after/around hooks.

Two properties make the proxy transparent rather than merely
convenient:

* **Writes pass through.** ``proxy.run = fn`` sets the attribute on the
  *target*, not on the proxy. A proxy with its own ``__dict__`` entry
  would permanently shadow the wrapped method — which is exactly how
  ``Loop.weave()``'s ``agent.run = run`` used to make
  ``Intercept("run", role="agent")`` unreachable.
* **Method resolution is lazy.** The wrapper is built on first access and
  re-built whenever the underlying attribute changes, so a method
  installed *after* wrapping is still intercepted.

Because resolution is lazy, so is validation: an intercepted method the
target does not have is reported when someone asks for it, not when the
proxy is built. At build time the answer is not yet knowable — ``run`` is
absent from every agent until ``Loop.weave()`` installs it — and a check
that runs then calls the framework's own flagship case dead.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from typing import Any

from spectragent.core.aop.advice import AdviceType
from spectragent.core.aop.errors import UnsupportedAdviceError

logger = logging.getLogger(__name__)

# Type for an interceptor entry: (hook_instance, advice_type, handler_fn)
#
# The handler is deliberately ``Callable[..., Any]``, not
# ``Callable[..., Coroutine]``: a handler intercepting a *synchronous*
# target method must be a plain ``def``, and the proxy enforces that
# pairing at build time with ``UnsupportedAdviceError``. Declaring it
# always-async made every legitimate sync interception a type error.
InterceptorEntry = tuple[Any, AdviceType, Callable[..., Any]]


class AOPProxy:
    """Transparent proxy that intercepts specific methods with AOP advice.

    All attribute access is delegated to the wrapped target, except for
    methods that have registered interceptors.
    """

    def __init__(
        self,
        target: Any,
        method_interceptors: dict[str, list[InterceptorEntry]],
        role: str = "",
    ) -> None:
        # Use object.__setattr__ — our own __setattr__ forwards to the target.
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_role", role)
        object.__setattr__(self, "_method_interceptors", dict(method_interceptors))
        # {method_name: (resolved_original, wrapper)} — the resolved original
        # is kept so a later rebind of the attribute invalidates the wrapper.
        object.__setattr__(self, "_wrapper_cache", {})
        # Methods already reported as unservable — warn once, not per access.
        object.__setattr__(self, "_reported_missing", set())

    def __getattr__(self, name: str) -> Any:
        target = object.__getattribute__(self, "_target")
        interceptors = object.__getattribute__(self, "_method_interceptors").get(name)
        if not interceptors:
            return getattr(target, name)

        try:
            original = getattr(target, name)
        except AttributeError:
            # Someone asked for an intercepted method the target does not
            # have. ``getattr(obj, name, default)`` swallows the
            # AttributeError, so without this the hook is dead and nothing
            # says why.
            reported = object.__getattribute__(self, "_reported_missing")
            if name not in reported:
                reported.add(name)
                logger.warning(
                    "Hook(s) %s intercept %s.%s but %s has no such attribute — "
                    "this access cannot be advised.",
                    sorted({getattr(h, "hook_id", type(h).__name__) for h, _, _ in interceptors}),
                    object.__getattribute__(self, "_role"),
                    name,
                    type(target).__name__,
                )
            raise

        cache = object.__getattribute__(self, "_wrapper_cache")
        cached = cache.get(name)
        # Bound methods are recreated per attribute access, so compare with
        # ``==`` (same function + same instance), never ``is``.
        if cached is not None and cached[0] == original:
            return cached[1]

        wrapped = _build_wrapped_method(
            original,
            interceptors,
            name,
            target=target,
            role=object.__getattribute__(self, "_role"),
        )
        cache[name] = (original, wrapped)
        return wrapped

    def __setattr__(self, name: str, value: Any) -> None:
        """Forward writes to the target so the proxy never shadows it."""
        setattr(object.__getattribute__(self, "_target"), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(object.__getattribute__(self, "_target"), name)

    def __repr__(self) -> str:
        target = object.__getattribute__(self, "_target")
        methods = object.__getattribute__(self, "_method_interceptors")
        return f"AOPProxy({target!r}, intercepted={sorted(methods)})"


def _build_wrapped_method(
    original: Callable[..., Any],
    interceptors: list[InterceptorEntry],
    method_name: str,
    *,
    target: Any = None,
    role: str = "",
) -> Callable[..., Any]:
    """Build a wrapped async method with before/after/around advice.

    Every handler receives ``target``/``role``/``method`` alongside
    ``args``/``kwargs``. Without the target reference a handler cannot
    attribute anything to the object it fired on — which is why
    ``AgentUsage.by_model`` was structurally unfillable: the model name
    lives on the LLM instance, not in the call's kwargs.
    """

    before_hooks = [(h, fn) for h, adv, fn in interceptors if adv == AdviceType.BEFORE]
    after_hooks = [(h, fn) for h, adv, fn in interceptors if adv == AdviceType.AFTER]
    around_hooks = [(h, fn) for h, adv, fn in interceptors if adv == AdviceType.AROUND]

    ctx: dict[str, Any] = {"target": target, "role": role, "method": method_name}

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
                    ctx,
                )

            # Before hooks
            for hook, handler in before_hooks:
                if hook.blocking:
                    await handler(args=args, kwargs=kwargs, **ctx)
                else:
                    asyncio.create_task(
                        _safe_call(
                            handler, hook, method_name, "before", args=args, kwargs=kwargs, **ctx
                        )
                    )

            # Original call
            result = await original(*args, **kwargs)

            # After hooks
            for hook, handler in after_hooks:
                if hook.blocking:
                    await handler(result, args=args, kwargs=kwargs, **ctx)
                else:
                    asyncio.create_task(
                        _safe_call(
                            handler,
                            hook,
                            method_name,
                            "after",
                            result,
                            args=args,
                            kwargs=kwargs,
                            **ctx,
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
                    handler(args=args, kwargs=kwargs, **ctx)
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
                    handler(result, args=args, kwargs=kwargs, **ctx)
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
    ctx: dict[str, Any],
) -> Any:
    """Apply around hooks as a chain: each can call ``proceed()``."""

    async def innermost() -> Any:
        # Before hooks
        for hook, handler in before_hooks:
            if hook.blocking:
                await handler(args=args, kwargs=kwargs, **ctx)
            else:
                asyncio.create_task(
                    _safe_call(
                        handler, hook, method_name, "before", args=args, kwargs=kwargs, **ctx
                    )
                )

        result = await original(*args, **kwargs)

        # After hooks
        for hook, handler in after_hooks:
            if hook.blocking:
                await handler(result, args=args, kwargs=kwargs, **ctx)
            else:
                asyncio.create_task(
                    _safe_call(
                        handler,
                        hook,
                        method_name,
                        "after",
                        result,
                        args=args,
                        kwargs=kwargs,
                        **ctx,
                    )
                )

        return result

    # Build the around chain from innermost outward
    proceed = innermost
    for hook, handler in reversed(around_hooks):
        current_proceed = proceed

        async def make_around(h: Any, fn: Any, p: Any) -> Any:
            return await fn(proceed=p, args=args, kwargs=kwargs, **ctx)

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
