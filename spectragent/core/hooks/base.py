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
        blocking: Whether the call the hook fired on **waits** for the
            handler. See :meth:`blocking` for the full contract; it means
            concurrency, and error isolation follows from it.
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
        """Whether the intercepted call waits for this hook's handler.

        **The one definition: concurrency.** ``blocking=True`` awaits the
        handler inline, so the intercepted method does not return until
        the handler is done. ``blocking=False`` hands the handler to a
        background task and lets the call continue immediately.

        Error isolation is a *consequence*, not a second meaning. A
        blocking handler's exception propagates into the call it fired on
        — the call is still on the stack, and a ``TraceWriter`` that
        cannot write is a run that must stop. A non-blocking handler's
        cannot: by the time it raises, the call it decorated has already
        returned, so there is nothing to propagate to and the exception
        is logged.

        Three shapes are impossible and are rejected during assembly with
        ``UnsupportedAdviceError`` rather than degrading into "blocking
        means something else here": ``blocking=False`` on a synchronous
        target (no running call to defer to), ``blocking=False`` on
        ``around`` advice (the handler *is* the call — the return value
        comes back through its ``proceed()``), and any handler whose
        sync/async shape does not match its target's.

        **``blocking=False`` cannot contribute to the trace.** A
        background handler finishes after the WAL commit point — after
        the step record, possibly after ``on_run_end`` — so anything it
        computes reaches no artifact. That is why both shipped hooks are
        blocking: ``trace_writer`` and ``metrics`` produce the run's
        output. ``blocking=False`` is for genuine side channels that
        nothing downstream reads, of which the designed one is
        ``live_monitor`` pushing state over a WebSocket. Its handlers are
        still owned and drained at run end (``BackgroundTasks``), so
        "fire and forget" does not mean "fire and never run".
        """
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
