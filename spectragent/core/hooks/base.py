"""RuntimeHook Protocol — contract for all hooks.

Hooks have two interaction surfaces:
1. **Lifecycle callbacks** (on_run_start/end/error): Called directly by
   AgentRuntime at structural boundaries.
2. **Method interceptions** (via __intercepts__ + AOP Weaver): Declared
   handlers that fire when target Protocol methods are called.

See docs/architecture_design.md §4.6 for the full Hook design.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class RuntimeHook(Protocol):
    """Contract for all runtime hooks.

    Attributes:
        hook_id: Registry discriminator — matches ``__registry_key__``.
        blocking: If True, the hook must complete before execution continues.
            Blocking hook failures propagate (re-raise). Non-blocking hook
            failures are logged but do not interrupt execution.
        priority: Lifecycle dispatch order, ascending — lower runs first.
            Load-bearing whenever one hook *enriches* the result and
            another *serializes* it: with ``hook: [trace_writer, metrics]``
            in YAML and no priority, every ``result.json`` on disk records
            ``total_tokens: 0``, silently.
    """

    hook_id: str
    priority: int

    @property
    def blocking(self) -> bool:
        """Whether this hook must complete before agent continues."""
        ...

    # ── Lifecycle callbacks (AgentRuntime calls directly) ──

    async def on_run_start(
        self,
        *,
        agent: Any,
        env: Any,
        loop: Any,
        llm: Any,
        task: str,
        task_id: str,
        benchmark: str,
        subset: str,
        run_id: str | None,
        config: dict[str, Any],
    ) -> None:
        """Called when AgentRuntime.execute() begins, before env setup."""
        ...

    async def on_run_end(self, *, result: Any, env: Any) -> None:
        """Called on **every** termination path, before env teardown.

        *env* is live here on purpose: env-state scoring (SWE-bench
        running ``pytest FAIL_TO_PASS`` in the container, OSWorld
        evaluating predicates against the VM) has no other moment where
        the environment is both still alive and reachable by a
        framework-aware component.
        """
        ...

    async def on_run_error(
        self, *, error: BaseException, partial_steps: list[Any], env: Any
    ) -> None:
        """Called when AgentRuntime.execute() encounters an exception.

        Always paired with :meth:`on_run_end`, which fires first with a
        result carrying ``agent_done=ERROR|TIMEOUT``. This callback is for
        exception detail only.
        """
        ...
