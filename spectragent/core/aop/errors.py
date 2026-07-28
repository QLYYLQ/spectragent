"""AOP-layer exceptions.

Raised at class-creation time (``MissingHandlerError``) or weave time
(``UnsupportedAdviceError``) so configuration mistakes fail fast rather
than silently degrading into warnings.
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

    Currently: ``around`` advice on synchronous methods, or async
    handlers attached to synchronous methods (which would need nested
    event-loop execution).
    """


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
            f"role={role!r} declared by {sorted(set(hooks))}" for role, hooks in sorted(unclaimed.items())
        )
        super().__init__(
            f"Hook interception declared for role(s) nothing wraps: {detail}. "
            f"Roles wrapped in this run: {sorted(claimed)}. Either fix the "
            f"role name or wrap that object in AgentRuntime."
        )
