"""RuntimeHook Protocol — contract for all hooks.

Hooks have two interaction surfaces:
1. **Lifecycle callbacks** (on_run_start/end/error): Called directly by
   AgentRuntime at structural boundaries.
2. **Method interceptions** (via __intercepts__ + AOP Weaver): Declared
   handlers that fire when target Protocol methods are called.

See docs/architecture_design.md §4.6 for the full Hook design.
"""

from __future__ import annotations

from collections.abc import Mapping
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
        agent_id: str,
        loop_id: str,
        env_type: str,
        llm_model: str,
        resolved_config: Any,
        observation_negotiation: Any,
        action_negotiation: Any,
        archive_negotiation: Any,
        env_evidence_recorded: bool,
        env_evidence_privacy: list[str],
        task_setup: Mapping[str, Any] | None,
        branch: Any,
        **kwargs: Any,
    ) -> None:
        """Called when AgentRuntime.execute() begins, before env setup.

        Two groups of parameters, and the distinction matters.

        The *objects* (``agent``, ``env``, ``loop``, ``llm``) are for a
        hook that needs to talk to them. They are not the place to read
        the run's configuration from: an instance tells you what it is
        now, not what it was configured as, and every hook that tried
        ended up re-deriving a different subset by ``getattr``.

        The *values* are what the run was set up as, resolved once by
        AgentRuntime — the only component holding all of it:

        - ``agent_id`` / ``loop_id`` / ``env_type`` / ``llm_model``:
          component identity.
        - ``resolved_config``: the composed, validated configuration
          (``ComposedRun.top_level_cfg``) — every default applied and
          every wired receptor inlined inside its owner, so it is what
          *ran* rather than what the YAML said. Not a record of
          everything a run resolved: a value a component derives at
          construction (``BrowserCapability`` turning a profile name into
          a watchdog set) lives on the instance and is not in here. It is
          also where every credential in the run lives; a hook that
          persists it must redact it (see
          :mod:`spectragent.core.trace.redaction`).
        - ``observation_negotiation`` / ``action_negotiation``: the
          negotiation results — active fragments and actions, fully
          qualified, plus what each relative ``__wiring__`` declaration
          bound to in this environment.
        - ``archive_negotiation``: the archive negotiation result — the
          archivers bound for this run and the capability each binds to —
          or ``None`` when no hook declared a non-empty ``archive``.
        - ``env_evidence_recorded``: whether archivers were bound, i.e.
          whether the run may record environment evidence.
        - ``env_evidence_privacy``: the sorted union of the bound
          archivers' declared privacy markers (a declaration, not a
          guarantee).
        - ``task_setup``: the environment setup context ``execute()`` hands
          ``setup_environment`` for this task (``start_url`` …), exactly as
          given — unredacted; a hook that persists it must redact it — or
          ``None`` when there is none.
        - ``branch``: where a branch task came from — a
          :class:`~spectragent.schemas.restoration.BranchProvenance` naming
          the source run, its trace and the checkpoint step — or ``None``
          for every run that is not a branch. Per task, like ``task_setup``;
          the runtime passes it through without reading it.

        *env* is the **raw** environment, never its AOP proxy. A recorder
        that pulls environment evidence (``Environment.collect_archive``)
        does so on this object, and only at its own commit points
        (``docs/architecture_design.md`` §4.6).

        ``**kwargs`` is part of the signature, not a convention: this
        payload grows as the framework learns to record more about a run,
        and ``AgentRuntime`` dispatches every keyword to every hook.
        Declaring it here means a hook that would break on the next
        addition fails a type-check instead of a run.
        """
        ...

    async def on_run_end(self, *, result: Any, env: Any) -> None:
        """Called on **every** termination path, before env teardown.

        Every path after ``on_run_start``: done, max_steps, timeout, an
        exception, and external cancellation (``asyncio.CancelledError`` —
        Ctrl-C, a cancelled sweep), which arrives here as a *result* with
        ``agent_done=ERROR`` and ``error="Execution cancelled"`` before
        ``AgentRuntime.execute`` re-raises the cancellation. A cancellation
        that lands while the run is already being closed out does not
        dispatch it again. The one path it cannot cover is the process
        being killed outright. A run whose own timeout expired while the
        Loop was still recording the step it had already ended on — vetoed,
        or failed — arrives with that ending's result, not a timeout one.

        Called once per run. A blocking hook that raises here is not
        retried: the error propagates out of ``execute`` (a later hook in
        priority order does not get the call) — except under a
        cancellation or a guard's veto (``RunAbort``), where it is logged
        (on a veto, also attached to it as a note) and the
        ``CancelledError`` or the ``RunAbort`` is what the caller sees: a
        sweep must stop either way. A hook whose close-out awaits can be
        cancelled in the middle of it, and then the same precedence is its
        own to keep: finish what it can, log any failure that happened
        under the cancellation, and re-raise the cancellation
        (``TraceWriter``'s close-out collection is the reference).

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
        """Called when a run ends in a timeout, an exception or a cancellation.

        Always paired with :meth:`on_run_end`, which fires first with a
        result carrying ``agent_done=ERROR|TIMEOUT``. This callback is for
        the detail only: *error* is the exception itself — a
        ``TimeoutError``, whatever the run raised, or the
        ``asyncio.CancelledError`` that cancelled it — and *partial_steps*
        the records the Loop surrendered before it ended. A veto the run's
        own timeout cut off while its step was being recorded arrives as
        the ``RunAbort``; a terminal result the Loop had already built when
        that happened is closed out like any result the Loop returns,
        without this callback.
        """
        ...
