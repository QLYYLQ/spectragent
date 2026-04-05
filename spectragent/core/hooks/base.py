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
    """

    hook_id: str

    @property
    def blocking(self) -> bool:
        """Whether this hook must complete before agent continues."""
        ...

    # ── Lifecycle callbacks (AgentRuntime calls directly) ──

    async def on_run_start(
        self, *, agent: Any, env: Any, task: str, config: dict[str, Any]
    ) -> None:
        """Called when AgentRuntime.execute() begins."""
        ...

    async def on_run_end(self, *, result: Any) -> None:
        """Called when AgentRuntime.execute() completes successfully."""
        ...

    async def on_run_error(
        self, *, error: BaseException, partial_steps: list[Any]
    ) -> None:
        """Called when AgentRuntime.execute() encounters an exception."""
        ...
