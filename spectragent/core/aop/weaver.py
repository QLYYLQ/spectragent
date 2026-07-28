"""Weaver — runtime AOP weaving engine.

Collects active hooks' ``__intercepts__`` declarations, builds an index
of (role, method) → interceptors, and wraps target objects' methods.

Usage in AgentRuntime (M08)::

    weaver = Weaver(active_hooks)
    wrapped_env = weaver.wrap(env, role="env")
    wrapped_agent = weaver.wrap(agent, role="agent")
    wrapped_llm = weaver.wrap(llm, role="llm")

    # Loop uses wrapped objects — hooks fire automatically
    await loop.run(wrapped_agent, wrapped_env, task, llm=wrapped_llm)

See docs/architecture_design.md §4.6 and docs/execution_dag.md M04.
"""

from __future__ import annotations

import logging
from typing import Any

from spectragent.core.aop.advice import AdviceType, Intercept
from spectragent.core.aop.errors import UnclaimedRoleError
from spectragent.core.aop.proxy import AOPProxy, InterceptorEntry

logger = logging.getLogger(__name__)


class Weaver:
    """Runtime AOP weaving engine.

    Builds an interception index from active hooks and wraps target objects.
    Only methods with registered interceptors are wrapped; all other
    attribute access is delegated transparently to the original object.
    """

    def __init__(self, active_hooks: list[Any]) -> None:
        self._hooks = active_hooks
        self._index: dict[tuple[str, str], list[InterceptorEntry]] = {}
        self._claimed_roles: set[str] = set()
        self._build_index()

    def _build_index(self) -> None:
        """Build {(role, method): [(hook, advice, handler)]} from active hooks."""
        for hook in self._hooks:
            intercepts: list[Intercept] = getattr(hook, "__intercepts__", [])
            for intercept in intercepts:
                handler_name = intercept.handler_name
                handler = getattr(hook, handler_name, None)
                if handler is None:
                    logger.warning(
                        "Hook '%s' declares intercept %s.%s(%s) but has no method '%s'",
                        getattr(hook, "hook_id", type(hook).__name__),
                        intercept.role,
                        intercept.method,
                        intercept.advice,
                        handler_name,
                    )
                    continue

                advice = (
                    intercept.advice
                    if isinstance(intercept.advice, AdviceType)
                    else AdviceType(intercept.advice)
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

        method_interceptors: dict[str, list[InterceptorEntry]] = {}
        for (r, method), interceptors in self._index.items():
            if r == role:
                method_interceptors[method] = interceptors

        if target is None or not method_interceptors:
            return target

        # Whether the target can serve a declared method is *not* decided
        # here: the proxy resolves lazily, and ``Loop.weave()`` installs
        # ``agent.run`` after wrapping. The proxy warns at resolution
        # time, when the answer is actually known.
        return AOPProxy(target, method_interceptors, role=role)

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
            hook_ids = [getattr(h, "hook_id", type(h).__name__) for h, _, _ in interceptors]
            unclaimed.setdefault(role, []).extend(hook_ids)

        if unclaimed:
            raise UnclaimedRoleError(
                unclaimed=unclaimed,
                claimed=sorted(self._claimed_roles),
            )

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
