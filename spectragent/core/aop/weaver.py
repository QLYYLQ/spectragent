"""Weaver — runtime AOP weaving engine.

Collects active hooks' ``__intercepts__`` declarations, builds an index
of (role, method) → interceptors, and wraps target objects' methods.

Usage in AgentRuntime (M08)::

    weaver = Weaver(active_hooks, background=run_tasks)
    wrapped_env = weaver.wrap(env, role="env")
    wrapped_agent = weaver.wrap(agent, role="agent")
    wrapped_llm = weaver.wrap(llm, role="llm")
    weaver.raise_for_unclaimed_roles()

    loop.weave(wrapped_agent, env=wrapped_env, llm=wrapped_llm, ...)
    weaver.audit_intercepts()   # after weave: ``agent.run`` exists now

    # Loop uses wrapped objects — hooks fire automatically
    await wrapped_agent.run(task)
    await run_tasks.drain()     # non-blocking handlers finish here

See docs/architecture_design.md §4.6 and docs/execution_dag.md M04.
"""

from __future__ import annotations

import inspect
from typing import Any

from spectragent.core.aop.advice import AdviceType, Intercept
from spectragent.core.aop.errors import (
    DeadInterceptError,
    MissingHandlerError,
    UnclaimedRoleError,
    UnsupportedAdviceError,
)
from spectragent.core.aop.proxy import (
    MISSING,
    AOPProxy,
    InterceptorEntry,
    hook_id_of,
    static_target_method,
    validate_advice_shape,
)
from spectragent.core.aop.tasks import BackgroundTasks


class Weaver:
    """Runtime AOP weaving engine.

    Builds an interception index from active hooks and wraps target objects.
    Only methods with registered interceptors are wrapped; all other
    attribute access is delegated transparently to the original object.

    Args:
        active_hooks: Hook instances whose ``__intercepts__`` to weave.
        background: Owner of ``blocking=False`` handler tasks. Pass the
            run's own so it can be drained at run end; a Weaver that
            makes its own keeps the tasks alive but nobody can wait for
            them.
    """

    def __init__(
        self,
        active_hooks: list[Any],
        *,
        background: BackgroundTasks | None = None,
    ) -> None:
        self._hooks = active_hooks
        self._index: dict[tuple[str, str], list[InterceptorEntry]] = {}
        self._claimed_roles: set[str] = set()
        # A role may be served by several objects (Loop phases and its step
        # sink, or main and helper models). Audit their combined surface.
        self._wrapped_targets: dict[str, list[Any]] = {}
        self._background = background if background is not None else BackgroundTasks()
        self._build_index()

    def _build_index(self) -> None:
        """Build {(role, method): [(hook, advice, handler)]} from active hooks."""
        for hook in self._hooks:
            intercepts: list[Intercept] = getattr(hook, "__intercepts__", [])
            for intercept in intercepts:
                handler_name = intercept.handler_name
                handler = getattr(hook, handler_name, None)
                advice = (
                    intercept.advice
                    if isinstance(intercept.advice, AdviceType)
                    else AdviceType(intercept.advice)
                )
                if not callable(handler):
                    raise MissingHandlerError(
                        hook_cls=type(hook).__name__,
                        method=intercept.method,
                        advice=advice.value,
                        handler_name=handler_name,
                    )
                key = (intercept.role, intercept.method)
                self._index.setdefault(key, []).append((hook, advice, handler))

    def wrap(self, target: Any, role: str) -> Any:
        """Wrap a target object's methods with registered interceptors.

        Only methods that have interceptors for the given role are wrapped.
        If no interceptors apply, returns the original target unchanged.

        Calling this marks *role* as claimed, whether or not anything was
        actually wrapped — see :meth:`raise_for_unclaimed_roles`.

        Args:
            target: The object to wrap (agent, env, llm, loop). ``None``
                is allowed (env-less runs): the role still counts as
                claimed and nothing is wrapped.
            role: The role identifier (``"env"``, ``"agent"``, ``"llm"``,
                ``"loop"``).

        Returns:
            An AOPProxy wrapping the target, or the original target if
            no interceptors apply.
        """
        self._claimed_roles.add(role)
        self._wrapped_targets.setdefault(role, []).append(target)

        method_interceptors: dict[str, list[InterceptorEntry]] = {}
        for (r, method), interceptors in self._index.items():
            if r == role:
                method_interceptors[method] = interceptors

        if target is None or not method_interceptors:
            return target

        # Whether the target can serve a declared method is *not* decided
        # here: the proxy resolves lazily, and ``Loop.weave()`` installs
        # ``agent.run`` after wrapping. :meth:`audit_intercepts` asks once
        # weaving is done, when the answer is actually known.
        return AOPProxy(target, method_interceptors, role=role, background=self._background)

    def raise_for_unclaimed_roles(self) -> None:
        """Fail if any declared role was never handed to :meth:`wrap`.

        Roles are a closed set decided by whoever drives the weaving
        (AgentRuntime wraps agent/env/llm/loop). A hook that declares any
        other role is either a typo or a request for a plug point that
        does not exist; both deserve an exception, not silence.
        """
        unclaimed: dict[str, list[str]] = {}
        for (role, _method), interceptors in self._index.items():
            if role in self._claimed_roles:
                continue
            unclaimed.setdefault(role, []).extend(hook_id_of(h) for h, _, _ in interceptors)

        if unclaimed:
            raise UnclaimedRoleError(
                unclaimed=unclaimed,
                claimed=sorted(self._claimed_roles),
            )

    def audit_intercepts(self) -> None:
        """Fail if a declared intercept can never fire on its target.

        The complement of :meth:`raise_for_unclaimed_roles`, which only
        catches a role nothing wraps. This catches the three ways an
        intercept on a *wrapped* role still dies quietly:

        * the run has no object for that role (an ``env``-role intercept
          in an env-less run — ``wrap(None, role="env")`` claims the role,
          so the unclaimed check passes);
        * the target has no such attribute (``env.obsserve``);
        * the advice does not fit the method's shape (an ``async def``
          handler on a plain ``def`` method).

        **Call this after ``Loop.weave()``.** No agent has ``run`` until
        weaving installs it, so the same audit run before weaving reports
        the framework's flagship intercept dead on every single run —
        which is precisely why the old wrap-time check was deleted.

        It raises rather than warns, deliberately. This is the same class
        of mistake as ``UnclaimedRoleError`` and a hook wired to nothing
        is worth exactly as much as a hook not wired at all; the house
        rule is that config errors surface during assembly, not at the
        first LLM call. A hook that wants to observe whichever objects a
        run happens to have should be split into per-role hooks and
        selected in YAML, not left to guess.

        Raises:
            DeadInterceptError: listing every dead intercept at once, so
                one prepare() reports all of them instead of one per fix.
        """
        dead: list[tuple[str, list[str], str]] = []

        for (role, method), interceptors in sorted(self._index.items()):
            if role not in self._claimed_roles:
                continue  # raise_for_unclaimed_roles owns this case
            point = f"{role}.{method}"
            hook_ids = sorted({hook_id_of(h) for h, _, _ in interceptors})

            targets = [
                target for target in self._wrapped_targets.get(role, []) if target is not None
            ]
            if not targets:
                dead.append((point, hook_ids, f"this run has no {role!r} object to intercept"))
                continue

            methods = [static_target_method(target, method) for target in targets]
            originals = [original for original in methods if original is not MISSING]
            if not originals:
                names = ", ".join(type(target).__name__ for target in targets)
                dead.append((point, hook_ids, f"{names} has no attribute {method!r}"))
                continue

            # Every target that exposes this point must support its advice.
            # Objects serving other methods of the role do not make it dead.
            for original in originals:
                if not (inspect.isfunction(original) or inspect.ismethod(original)):
                    # Descriptors may resolve to methods; do not invoke them
                    # during assembly. A plain non-callable value cannot.
                    if not callable(original) and not hasattr(type(original), "__get__"):
                        dead.append((point, hook_ids, f"attribute {method!r} is not callable"))
                    continue
                try:
                    validate_advice_shape(original, interceptors, method)
                except UnsupportedAdviceError as exc:
                    dead.append((point, hook_ids, str(exc)))

        if dead:
            raise DeadInterceptError(dead=dead)

    @property
    def claimed_roles(self) -> frozenset[str]:
        """Roles handed to :meth:`wrap` so far."""
        return frozenset(self._claimed_roles)

    @property
    def declared_roles(self) -> frozenset[str]:
        """Roles any active hook declares an interception for."""
        return frozenset(role for role, _ in self._index)

    @property
    def intercepted_methods(self) -> dict[str, list[str]]:
        """Return {role: [method_names]} for introspection."""
        result: dict[str, list[str]] = {}
        for role, method in self._index:
            result.setdefault(role, []).append(method)
        return {k: sorted(v) for k, v in result.items()}
