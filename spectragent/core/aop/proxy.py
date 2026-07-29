"""Proxy creation — wraps target object methods with AOP advice.

The proxy delegates all attribute access to the original object, but
replaces intercepted methods with wrapped versions that execute
before/after/around hooks.

Two properties make *named attribute access* through the proxy behave
like the target:

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
that runs then calls the framework's own flagship case dead. The
fail-fast check lives in ``Weaver.audit_intercepts()``, which
``AgentRuntime.prepare()`` runs *after* weaving; the lazy warning below
is the last-ditch diagnostic for a proxy built outside that path.

**Where transparency stops, and what it says when it does.** Attribute
forwarding covers named access only: CPython resolves ``async with obj``
/ ``iter(obj)`` / ``bool(obj)`` against ``type(obj)`` and never consults
``__getattr__``, so a dunder the proxy's class does not declare cannot
reach the target however the target defines it. Nine are declared, in
two groups:

* ``__bool__``, ``__eq__``, ``__hash__`` forward outright. These were
  the dangerous ones — they answered *for the proxy*, silently, so a
  target whose ``__bool__`` said False read as True.
* ``__enter__``/``__exit__``, ``__aenter__``/``__aexit__``,
  ``__iter__``/``__aiter__`` forward when the target implements them and
  otherwise raise :class:`UnsupportedProxyProtocolError` (a
  ``TypeError``) naming the target — one protocol per resource-holding
  or streaming shape that actually occurs here.

The list is fixed rather than generated per target. A dynamic subclass
carrying exactly the target's dunders would be more complete, but it
needs a class cache to avoid minting a type per wrap — a module-level
mutable, which this framework does not do — and nothing in the run has
ever needed a protocol outside the list.

Anything outside it (``__call__``, ``__len__``, ``__getitem__``, …) is
absent on purpose: declaring them would make ``callable(proxy)`` and
``len(proxy)`` answer True for every target, trading a loud failure for
a quiet lie.

``__class__`` is deliberately *not* forwarded either: spoofing it while
``type(proxy)`` still says ``AOPProxy`` swaps one inconsistency for
another, and this framework's consumers are ``Protocol``-typed. A
``@runtime_checkable`` Protocol check is structural — it asks
``hasattr``, which the proxy answers correctly — so the only thing that
sees through the proxy is a nominal ``isinstance`` against a concrete
class, which Protocol-First says nothing should be doing.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from spectragent.core.aop.advice import AdviceType
from spectragent.core.aop.errors import (
    UnsupportedAdviceError,
    UnsupportedProxyProtocolError,
)
from spectragent.core.aop.tasks import BackgroundTasks

logger = logging.getLogger(__name__)

# Type for an interceptor entry: (hook_instance, advice_type, handler_fn)
#
# The handler is deliberately ``Callable[..., Any]``, not
# ``Callable[..., Coroutine]``: a handler intercepting a *synchronous*
# target method must be a plain ``def``, and ``validate_advice_shape``
# enforces that pairing. Declaring it always-async made every legitimate
# sync interception a type error.
InterceptorEntry = tuple[Any, AdviceType, Callable[..., Any]]

_PROTOCOL_HINTS = {
    "__enter__": "``with obj:``",
    "__exit__": "``with obj:``",
    "__aenter__": "``async with obj:``",
    "__aexit__": "``async with obj:``",
    "__iter__": "``for x in obj:``",
    "__aiter__": "``async for x in obj:``",
}


def hook_id_of(hook: Any) -> str:
    return str(getattr(hook, "hook_id", type(hook).__name__))


def _handler_id(handler: Callable[..., Any]) -> str:
    return str(getattr(handler, "__qualname__", repr(handler)))


def validate_advice_shape(
    original: Any,
    interceptors: list[InterceptorEntry],
    method_name: str,
) -> None:
    """Reject advice the proxy provably cannot honour on *original*.

    Called from two places on purpose: ``_build_wrapped_method`` (so a
    proxy built by hand still refuses to produce a wrapper that would
    die at call time) and ``Weaver.audit_intercepts`` (so a real run
    fails in ``prepare()``, before an environment has been paid for).

    Raises:
        UnsupportedAdviceError: with the offending hook, handler and the
            shape mismatch.
    """
    target_is_async = asyncio.iscoroutinefunction(original)

    for hook, advice, handler in interceptors:
        blocking = bool(getattr(hook, "blocking", True))
        handler_is_async = asyncio.iscoroutinefunction(handler)

        if advice is AdviceType.AROUND and not blocking:
            raise UnsupportedAdviceError(
                f"Hook {hook_id_of(hook)!r} declares around advice on "
                f"{method_name!r} with blocking=False. An around handler *is* "
                f"the call — the return value comes back through its "
                f"proceed() — so nothing can continue without it. Use "
                f"blocking=True, or before/after advice."
            )

        if target_is_async and not handler_is_async:
            raise UnsupportedAdviceError(
                f"Handler {_handler_id(handler)!r} of hook {hook_id_of(hook)!r} is "
                f"a plain ``def`` but intercepts asynchronous method "
                f"{method_name!r}. Handlers on an async target are awaited; a "
                f"plain ``def`` returns something unawaitable. Make the handler "
                f"``async def``."
            )

        if not target_is_async:
            if handler_is_async:
                raise UnsupportedAdviceError(
                    f"Async handler {_handler_id(handler)!r} cannot intercept "
                    f"synchronous method {method_name!r}: running it would need a "
                    f"nested event loop. Make the handler a plain ``def`` or the "
                    f"target method ``async def``."
                )
            if not blocking:
                raise UnsupportedAdviceError(
                    f"Hook {hook_id_of(hook)!r} is non-blocking but intercepts "
                    f"synchronous method {method_name!r}. blocking=False means "
                    f"the call does not wait for the handler, which needs a "
                    f"running event loop to hand it to — a synchronous target has "
                    f"nowhere to put it. Use blocking=True, or intercept an "
                    f"``async def`` method."
                )


class AOPProxy:
    """Proxy that intercepts specific methods with AOP advice.

    All *named* attribute access is delegated to the wrapped target,
    except for methods that have registered interceptors. See the module
    docstring for the protocols that are not forwarded.
    """

    def __init__(
        self,
        target: Any,
        method_interceptors: dict[str, list[InterceptorEntry]],
        role: str = "",
        background: BackgroundTasks | None = None,
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
        # Owner of non-blocking handler tasks. A private one when nobody
        # supplied one keeps the tasks from being garbage-collected
        # mid-flight; only a caller-supplied one can be *drained*.
        object.__setattr__(
            self, "_background", background if background is not None else BackgroundTasks()
        )

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
                    sorted({hook_id_of(h) for h, _, _ in interceptors}),
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
            background=object.__getattribute__(self, "_background"),
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

    # ── forwarded protocols ───────────────────────────────────
    #
    # These three answered for the *proxy* instead of the target, and did
    # it silently: a target whose ``__bool__`` said False read as True,
    # and ``proxy == target`` was False however the target defined
    # equality.

    def __bool__(self) -> bool:
        return bool(object.__getattribute__(self, "_target"))

    def __eq__(self, other: object) -> bool:
        target = object.__getattribute__(self, "_target")
        if isinstance(other, AOPProxy):
            other = object.__getattribute__(other, "_target")
        return bool(target == other)

    def __hash__(self) -> int:
        # Paired with __eq__ on purpose: a proxy and its target compare
        # equal, so they must hash alike or a dict keyed by one would
        # miss the other.
        return hash(object.__getattribute__(self, "_target"))

    # ── curated protocols: forward, or explain ────────────────
    #
    # Declaring these *on the class* is the only way an implicit lookup
    # can reach the target at all. The set is deliberately short — one
    # entry per protocol a real target in this framework implements —
    # rather than a generated per-target subclass: a fixed class is
    # greppable, has one identity, and needs no cache to avoid building
    # a new type per wrap.

    def _protocol_method(self, dunder: str) -> Callable[..., Any]:
        target = object.__getattribute__(self, "_target")
        method = getattr(type(target), dunder, None)
        if method is None:
            role = object.__getattribute__(self, "_role")
            raise UnsupportedProxyProtocolError(
                f"AOPProxy(role={role!r}) was asked for {_PROTOCOL_HINTS[dunder]} "
                f"but its target {type(target).__name__} does not implement "
                f"{dunder}. The proxy declares the protocol so it can forward it; "
                f"it cannot invent it."
            )
        return functools.partial(method, target)

    def _reproxy(self, result: Any) -> Any:
        """Keep ``as x`` woven when the target returns itself."""
        return self if result is object.__getattribute__(self, "_target") else result

    def __enter__(self) -> Any:
        return self._reproxy(self._protocol_method("__enter__")())

    def __exit__(self, *exc: Any) -> Any:
        return self._protocol_method("__exit__")(*exc)

    async def __aenter__(self) -> Any:
        return self._reproxy(await self._protocol_method("__aenter__")())

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._protocol_method("__aexit__")(*exc)

    def __iter__(self) -> Any:
        return self._protocol_method("__iter__")()

    def __aiter__(self) -> Any:
        return self._protocol_method("__aiter__")()


def _task_label(hook: Any, ctx: dict[str, Any], method_name: str, advice: str) -> str:
    role = ctx.get("role") or "?"
    return f"aop:{role}.{method_name}:{advice}:{hook_id_of(hook)}"


async def _run_before_async(
    before_hooks: list[tuple[Any, Callable[..., Any]]],
    *,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    ctx: dict[str, Any],
    method_name: str,
    background: BackgroundTasks,
) -> None:
    for hook, handler in before_hooks:
        if hook.blocking:
            await handler(args=args, kwargs=kwargs, **ctx)
        else:
            background.spawn(
                _safe_call(handler, hook, method_name, "before", args=args, kwargs=kwargs, **ctx),
                label=_task_label(hook, ctx, method_name, "before"),
            )


async def _run_after_async(
    after_hooks: list[tuple[Any, Callable[..., Any]]],
    result: Any,
    *,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    ctx: dict[str, Any],
    method_name: str,
    background: BackgroundTasks,
) -> None:
    for hook, handler in after_hooks:
        if hook.blocking:
            await handler(result, args=args, kwargs=kwargs, **ctx)
        else:
            background.spawn(
                _safe_call(
                    handler,
                    hook,
                    method_name,
                    "after",
                    result,
                    args=args,
                    kwargs=kwargs,
                    **ctx,
                ),
                label=_task_label(hook, ctx, method_name, "after"),
            )


def _build_wrapped_method(
    original: Callable[..., Any],
    interceptors: list[InterceptorEntry],
    method_name: str,
    *,
    target: Any = None,
    role: str = "",
    background: BackgroundTasks | None = None,
) -> Callable[..., Any]:
    """Build a wrapped method with before/after/around advice.

    Every handler receives ``target``/``role``/``method`` alongside
    ``args``/``kwargs``. Without the target reference a handler cannot
    attribute anything to the object it fired on — which is why
    ``AgentUsage.by_model`` was structurally unfillable: the model name
    lives on the LLM instance, not in the call's kwargs.

    Sync and async targets support the same three advice types. On a sync
    target every handler is a plain ``def`` and every hook is blocking
    (``validate_advice_shape`` rejects anything else), so the sync chain
    is the async one with the awaits removed.
    """
    validate_advice_shape(original, interceptors, method_name)

    before_hooks = [(h, fn) for h, adv, fn in interceptors if adv == AdviceType.BEFORE]
    after_hooks = [(h, fn) for h, adv, fn in interceptors if adv == AdviceType.AFTER]
    around_hooks = [(h, fn) for h, adv, fn in interceptors if adv == AdviceType.AROUND]

    ctx: dict[str, Any] = {"target": target, "role": role, "method": method_name}
    tasks = background if background is not None else BackgroundTasks()

    if asyncio.iscoroutinefunction(original):

        @functools.wraps(original)
        async def wrapped_async(*args: Any, **kwargs: Any) -> Any:
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
                    tasks,
                )

            await _run_before_async(
                before_hooks,
                args=args,
                kwargs=kwargs,
                ctx=ctx,
                method_name=method_name,
                background=tasks,
            )
            result = await original(*args, **kwargs)
            await _run_after_async(
                after_hooks,
                result,
                args=args,
                kwargs=kwargs,
                ctx=ctx,
                method_name=method_name,
                background=tasks,
            )
            return result

        return wrapped_async

    @functools.wraps(original)
    def wrapped_sync(*args: Any, **kwargs: Any) -> Any:
        if around_hooks:
            return _apply_around_chain_sync(
                around_hooks,
                original,
                before_hooks,
                after_hooks,
                args,
                kwargs,
                ctx,
            )

        for _hook, handler in before_hooks:
            handler(args=args, kwargs=kwargs, **ctx)

        result = original(*args, **kwargs)

        for _hook, handler in after_hooks:
            handler(result, args=args, kwargs=kwargs, **ctx)

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
    background: BackgroundTasks,
) -> Any:
    """Apply around hooks as a chain: each can call ``proceed()``.

    The innermost link runs before/after advice around the target, so a
    hook that never calls ``proceed()`` short-circuits all of it.
    """

    async def innermost() -> Any:
        await _run_before_async(
            before_hooks,
            args=args,
            kwargs=kwargs,
            ctx=ctx,
            method_name=method_name,
            background=background,
        )
        result = await original(*args, **kwargs)
        await _run_after_async(
            after_hooks,
            result,
            args=args,
            kwargs=kwargs,
            ctx=ctx,
            method_name=method_name,
            background=background,
        )
        return result

    # Built innermost outward. ``functools.partial`` binds this
    # iteration's handler and proceed by value — a closure over the loop
    # variable would give every link the last hook in the list.
    proceed: Callable[[], Awaitable[Any]] = innermost
    for _hook, handler in reversed(around_hooks):
        proceed = functools.partial(handler, proceed=proceed, args=args, kwargs=kwargs, **ctx)

    return await proceed()


def _apply_around_chain_sync(
    around_hooks: list[tuple[Any, Callable[..., Any]]],
    original: Callable[..., Any],
    before_hooks: list[tuple[Any, Callable[..., Any]]],
    after_hooks: list[tuple[Any, Callable[..., Any]]],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    ctx: dict[str, Any],
) -> Any:
    """The synchronous twin of :func:`_apply_around_chain`.

    ``around`` on a sync method is not a curiosity: it is the framework's
    only documented around use case (``agent.build_react_prompt``, a
    plain ``def`` on every shipped agent — see
    docs/architecture_design.md §4.1). Handlers here call ``proceed()``
    and get a value rather than an awaitable.
    """

    def innermost() -> Any:
        for _hook, handler in before_hooks:
            handler(args=args, kwargs=kwargs, **ctx)
        result = original(*args, **kwargs)
        for _hook, handler in after_hooks:
            handler(result, args=args, kwargs=kwargs, **ctx)
        return result

    proceed: Callable[[], Any] = innermost
    for _hook, handler in reversed(around_hooks):
        proceed = functools.partial(handler, proceed=proceed, args=args, kwargs=kwargs, **ctx)

    return proceed()


async def _safe_call(
    handler: Callable[..., Any],
    hook: Any,
    method_name: str,
    advice_type: str,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Call a non-blocking hook handler, catching and logging exceptions.

    A non-blocking handler runs after the call it fired on has already
    returned, so there is no longer anything to propagate an exception
    *to*.
    """
    try:
        await handler(*args, **kwargs)
    except Exception:
        logger.exception(
            "Non-blocking %s hook '%s' failed on method '%s'",
            advice_type,
            hook_id_of(hook),
            method_name,
        )


MISSING = object()
"""Sentinel for "the target has no such attribute" — ``None`` is a value."""


def static_target_method(target: Any, method: str) -> Any:
    """Look *method* up on *target* without running any descriptor.

    ``inspect.getattr_static`` on purpose: ``getattr`` would evaluate a
    property, and an audit has no business doing that. It also skips
    ``__getattr__``, so an object that serves the method dynamically
    (a Mock, a router) gets a real lookup before being declared dead —
    reachable only when the static lookup already found nothing, so no
    property is evaluated on the way.

    Returns:
        The unresolved attribute, or :data:`MISSING`.
    """
    try:
        attr = inspect.getattr_static(target, method)
    except AttributeError:
        attr = getattr(target, method, MISSING)
    if isinstance(attr, staticmethod | classmethod):
        return attr.__func__
    return attr
