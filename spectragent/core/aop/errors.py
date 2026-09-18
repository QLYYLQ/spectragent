"""AOP-layer exceptions.

Raised at class-creation time (``MissingHandlerError``), weave time
(``UnsupportedAdviceError``) or assembly time (``UnclaimedRoleError``,
``DeadInterceptError``) so configuration mistakes fail fast rather than
silently degrading into warnings.
"""

from __future__ import annotations

from conscribe.exceptions import RegistryError


class MissingHandlerError(RegistryError):
    """A Hook declares ``__intercepts__`` but the matching handler is missing.

    Raised by ``HookBase.__init_subclass__`` at class creation time so
    typos like ``after_obseve`` instead of ``after_observe`` surface
    immediately, not when the Weaver runs.
    """

    def __init__(
        self,
        *,
        hook_cls: str,
        method: str,
        advice: str,
        handler_name: str,
    ) -> None:
        self.hook_cls = hook_cls
        self.method = method
        self.advice = advice
        self.handler_name = handler_name
        super().__init__(
            f"Hook '{hook_cls}' declares Intercept(method={method!r}, "
            f"advice={advice!r}) but handler '{handler_name}' is missing "
            f"or not callable."
        )


class UnsupportedAdviceError(Exception):
    """A combination of advice + target method shape is not supported.

    The three the proxy cannot honour, all decided by
    ``proxy.validate_advice_shape``:

    * a handler whose sync/async shape does not match its target's — an
      ``async def`` handler on a plain ``def`` method would need a nested
      event loop, a plain ``def`` handler on an ``async def`` method
      returns something the proxy cannot await;
    * ``blocking=False`` on a synchronous target — there is no running
      call to hand the handler to;
    * ``blocking=False`` on ``around`` advice — an around handler *is*
      the call, so nothing can continue without it.
    """


class UnsupportedProxyProtocolError(TypeError):
    """Something used a Python protocol the ``AOPProxy`` cannot forward.

    ``AOPProxy`` forwards *named* attribute access. It cannot forward
    implicit special-method lookup, because CPython resolves ``async
    with obj`` / ``iter(obj)`` against ``type(obj)`` and never consults
    ``__getattr__``. Left alone that surfaces as a bare
    ``TypeError: 'AOPProxy' object is not iterable``, which names the
    proxy but not the reason or the way out.

    A ``TypeError`` subclass on purpose: duck-typing code legitimately
    writes ``try: iter(x) except TypeError``, and making the message
    legible must not also make it uncatchable.
    """


class DeadInterceptError(RegistryError):
    """A Hook declares an intercept that cannot fire in this run.

    ``UnclaimedRoleError``'s sibling. That one catches a role nothing
    wraps; this one catches an intercept whose role *is* wrapped but
    whose method can still never run the handler — the object for that
    role is absent from this run (an ``env``-role intercept in an
    env-less run), the target has no such attribute (``env.obsserve``),
    or the advice does not fit the method's shape.

    Raised from ``AgentRuntime.prepare()``, after ``Loop.weave()``: that
    is the first moment the answer is knowable, since no agent has
    ``run`` until weaving installs it.
    """

    def __init__(self, *, dead: list[tuple[str, list[str], str]]) -> None:
        self.dead = dead
        detail = "; ".join(
            f"{point} declared by {hook_ids}: {reason}" for point, hook_ids, reason in dead
        )
        super().__init__(
            f"Hook interception(s) that can never fire: {detail}. Either fix the "
            f"declaration or drop the hook from this run's config."
        )


class UnclaimedRoleError(RegistryError):
    """A Hook intercepts a role that nothing in the run ever wraps.

    ``Intercept.role`` is a free-form string, so ``role="capability"`` or
    a plain typo used to register cleanly, show up in
    ``Weaver.intercepted_methods``, and then never fire — with no log and
    no exception. Raising here turns a silent no-op into a config error.
    """

    def __init__(self, *, unclaimed: dict[str, list[str]], claimed: list[str]) -> None:
        self.unclaimed = unclaimed
        self.claimed = claimed
        detail = "; ".join(
            f"role={role!r} declared by {sorted(set(hooks))}"
            for role, hooks in sorted(unclaimed.items())
        )
        super().__init__(
            f"Hook interception declared for role(s) nothing wraps: {detail}. "
            f"Roles wrapped in this run: {sorted(claimed)}. Either fix the "
            f"role name or wrap that object in AgentRuntime."
        )
