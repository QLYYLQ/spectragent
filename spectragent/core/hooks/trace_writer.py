"""TraceWriter — RuntimeHook for full WAL-based trace persistence.

Two intercepts, one job each:

- ``around`` on ``llm.ainvoke`` is the **only** place an LLM call is
  recorded. The call is registered *before* ``proceed()`` — prompt,
  requested reply schema, start time — and its outcome filled in after:
  ``completed``, ``failed`` (an ``Exception``) or ``cancelled`` (a
  ``BaseException`` such as ``CancelledError``). ``after`` advice never
  sees a call that raised, and registering only in ``except Exception``
  would lose the prompt of exactly the call a timeout or a Ctrl-C cut off.
- ``after`` on ``loop.record_step`` is the **commit point**: the moment the
  Loop surrenders a finished :class:`StepRecord`. Everything the step
  consists of is known then — the observation the agent actually saw, every
  LLM call it made, the actions and what they returned — and it goes to
  disk in one ``WALWriter.commit_step``. Commit on ``llm.ainvoke`` would be
  the wrong event twice over: it fires before the actions exist, and once
  per call rather than once per step.

A run that ends between a call and the step (timeout, cancellation, crash)
never reaches that commit point, so run end commits whatever calls are still
registered as *trailing* calls instead of dropping them.

Registered via conscribe as hook_id="trace_writer". See
``docs/architecture_design.md`` §4.6 and §5.4.

.. note::
   No ``from __future__ import annotations`` here — ``trace_dir`` must
   stay a real ``Annotated[...]`` object so any future ``TraceWriter``
   subclass living in another module still resolves it (see the same
   note on ``hooks/_registrar.py``).
"""

import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from spectragent.core.aop.advice import Intercept
from spectragent.core.hooks._registrar import HookBase
from spectragent.core.trace.encoding import (
    EncodedOutputFormat,
    PendingLLMCall,
    encode_llm_calls,
    encode_observation,
    encode_output_format,
)
from spectragent.core.trace.models import RunStatus, TraceConfig, TraceNegotiation
from spectragent.core.trace.redaction import redact_config, trace_safe
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.step_record import StepRecord

logger = logging.getLogger(__name__)

# Terminal states that mean "the agent stopped without a clean finish".
# The manifest must say so: a trace stuck at ``running`` is a trace no
# evaluator will touch, and 9 of the first 14 real traces were exactly
# that because the timeout path never reached a finalizer.
_ERROR_DONE_STATES = frozenset({AgentDoneStatus.TIMEOUT, AgentDoneStatus.ERROR})


@dataclass
class TraceWriter(HookBase):
    """Full-trace WAL persistence hook.

    At run start it writes the snapshot of *how the run was configured* —
    identity, the redacted resolved config, the negotiation result — and
    at each step it commits what the run *did*. The configuration half is
    handed to it as values by ``AgentRuntime``: this hook derives nothing
    about how the run was set up, it only chooses what of it to withhold.

    Each LLM call is registered in ``around_ainvoke`` before it goes out
    and committed with its step — every call of the step, in start order —
    when the Loop hands over its ``StepRecord`` (``after_record_step``).
    The ``around`` advice is also the only vantage point that sees a call's
    start, its end and its failure, so it is where a step's model wait is
    measured (:meth:`_stamp_timings`).

    A run that dies before surrendering a step (timeout, cancellation,
    crash) commits the calls it still holds as *trailing* calls at run end
    (``WALWriter.commit_trailing``) instead of dropping them.
    """

    __registry_key__ = "trace_writer"
    __intercepts__ = [
        # The one recording point for LLM calls — see :meth:`around_ainvoke`.
        Intercept("ainvoke", role="llm", advice="around"),
        Intercept("record_step", role="loop", advice="after"),
    ]

    # Serializing must happen after every enricher has finished mutating
    # the result.
    priority = HookBase.PRIORITY_PERSIST

    # ── conscribe config-annotated fields ─────────────────────
    trace_dir: Annotated[
        str,
        trace_safe(
            "output location — where this trace already is, not a credential",
            default="traces",
            description=(
                "Base directory for trace output. Each run lands in "
                "``<trace_dir>/<run_id>``. Relative paths resolve against "
                "the process working directory."
            ),
        ),
    ] = "traces"

    record_secrets: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Record the resolved configuration verbatim, including "
                "api keys, CDP URLs carrying a token, session tokens and "
                "proxy credentials. Off by default: only fields explicitly "
                "marked trace-safe are recorded, everything else lands as "
                '``"<omitted>"``. Turn it on for a local debugging run '
                "whose trace directory will not be shared — the trace "
                "records that you did (``config.secrets_recorded``)."
            ),
        ),
    ] = False

    # ── internal state (not config) ───────────────────────────
    _wal: WALWriter | None = field(default=None, init=False, repr=False)
    _steps_written: int = field(default=0, init=False, repr=False)
    # The LLM calls registered since the last commit, in start order.
    _pending_calls: list[PendingLLMCall] = field(default_factory=list, init=False, repr=False)
    # Calls of the step in progress already committed as trailing calls,
    # so a later commit of that step numbers its calls after them.
    _calls_flushed: int = field(default=0, init=False, repr=False)
    # Requested reply schemas, encoded once per class per run; ``None`` for
    # a class whose schema could not be generated.
    _output_formats: dict[Any, EncodedOutputFormat | None] = field(
        default_factory=dict, init=False, repr=False
    )
    # ``perf_counter`` at the last step boundary — run start, then every
    # commit. ``None`` before the run starts, which is what makes an
    # unmeasured step record ``duration_ms=None`` rather than 0.
    _step_started: float | None = field(default=None, init=False, repr=False)
    # Model wait accumulated since the last commit, in milliseconds.
    _pending_llm_ms: float = field(default=0.0, init=False, repr=False)

    @property
    def blocking(self) -> bool:
        return True

    @property
    def run_dir(self) -> Path | None:
        """Directory this run is being written to — the evaluator seam."""
        return self._wal.run_dir if self._wal is not None else None

    # ── lifecycle hooks ───────────────────────────────────────

    async def on_run_start(
        self,
        *,
        task: str = "",
        task_id: str = "",
        benchmark: str = "",
        subset: str = "",
        run_id: str | None = None,
        config: Any = None,
        agent_id: str = "",
        loop_id: str = "",
        env_type: str = "",
        llm_model: str = "",
        resolved_config: Any = None,
        observation_negotiation: Any = None,
        action_negotiation: Any = None,
        **kwargs: Any,
    ) -> None:
        """Open the trace: directory, config snapshot, initial manifest.

        Everything here arrives as a value. This hook used to reconstruct
        the run's identity by ``getattr``-ing ``agent_id`` / ``loop_id`` /
        ``env_type`` off whatever objects it was handed, which made a
        serializer responsible for *discovering* facts and left the
        configuration itself — the thing the run was actually set up as —
        recorded nowhere. ``AgentRuntime`` is the one component holding
        the composed config and both negotiation results, so it resolves
        them and passes them in; see
        :meth:`spectragent.core.runtime.AgentRuntime.execute`.

        The duck-typing did not vanish, it moved: identity is still read
        off the live objects, by ``AgentRuntime._component_id``, once, in
        the component that knows the run. That is why a run assembled
        without the Composer still reports an ``agent_id`` while its
        ``resolved_config`` is ``None``.

        Args:
            task / task_id / benchmark / subset: Dataset identity — the
                key an Evaluator joins the trace back to ground truth by.
            run_id: Trace directory name. Generated when omitted.
            config: Caller-curated extra context, recorded verbatim (see
                :attr:`~spectragent.core.trace.models.TraceConfig.extra`).
            agent_id / loop_id / env_type / llm_model: Component identity.
            resolved_config: The composed, validated configuration —
                ``ComposedRun.top_level_cfg``. Passed through field-level
                redaction on the way to disk unless
                :attr:`record_secrets` is set. The ``config_hash`` in the
                manifest is computed from it *before* that redaction; the
                recorded view cannot carry a sweep.
            observation_negotiation / action_negotiation: The two
                negotiation results, recorded as
                :class:`~spectragent.core.trace.models.TraceNegotiation`.
        """
        resolved_run_id = run_id or str(uuid.uuid4())[:8]
        base_dir = Path(self.trace_dir)

        self._wal = WALWriter(base_dir, resolved_run_id)
        self._steps_written = 0
        self._pending_calls = []
        self._calls_flushed = 0
        self._output_formats = {}
        self._step_started = time.perf_counter()
        self._pending_llm_ms = 0.0

        # Two views of the same object, and they are not interchangeable.
        # ``resolved_full`` never reaches disk: it exists so the digest
        # can cover ``max_steps``, ``temperature`` and every other
        # unmarked knob a sweep varies, which the recorded view collapses
        # into one ``"<omitted>"``. See ``configuration_digest``.
        resolved_full = redact_config(resolved_config, record_secrets=True)
        recorded = resolved_full if self.record_secrets else redact_config(resolved_config)

        trace_config = TraceConfig(
            agent_id=agent_id,
            loop_id=loop_id,
            env_type=env_type,
            llm_model=llm_model,
            benchmark=benchmark,
            subset=subset,
            task=task,
            task_id=task_id,
            extra=config if isinstance(config, dict) else {},
            resolved=recorded,
            negotiation=TraceNegotiation.from_results(
                observation=observation_negotiation, action=action_negotiation
            ),
            secrets_recorded=bool(self.record_secrets),
        )
        self._wal.init_run(
            trace_config,
            config_hash=trace_config.configuration_digest(resolved=resolved_full),
        )
        if self.record_secrets:
            logger.warning(
                "TraceWriter: record_secrets is on — %s contains credentials "
                "verbatim and must not be shared.",
                self._wal.run_dir,
            )
        logger.info("TraceWriter: initialized run %s at %s", resolved_run_id, self._wal.run_dir)

    async def on_run_end(self, *, result: Any = None, env: Any = None, **kwargs: Any) -> None:
        """Commit trailing calls, write result.json, finalize — on every termination path."""
        if self._wal is None:
            return
        try:
            self._flush_trailing()
        finally:
            # The terminal state lands even if the trailing commit failed: a
            # trace stuck at ``running`` is a trace no evaluator will touch.
            # No result means no result.json. ``RunTrace.result`` returns
            # ``None`` for exactly that state; a file containing ``null``
            # would instead read as a result that fails to parse.
            status = _status_for(result)
            try:
                if result is not None:
                    self._wal.write_result(result)
            except BaseException:
                # Nor if result.json could not be written — but then the
                # trace is no clean record of the run, whatever the run did.
                status = RunStatus.ERROR
                raise
            finally:
                self._wal.finalize(status)
        logger.info(
            "TraceWriter: finalized run at %s (%d steps)",
            self._wal.run_dir,
            self._steps_written,
        )

    async def on_run_error(
        self,
        *,
        error: BaseException | None = None,
        partial_steps: Any = None,
        env: Any = None,
        **kwargs: Any,
    ) -> None:
        if self._wal is None:
            return
        # Idempotent: ``on_run_end`` normally commits the trailing calls
        # first, but a driver that only dispatches ``on_run_error`` must not
        # lose them.
        try:
            self._flush_trailing()
        finally:
            self._wal.finalize(RunStatus.ERROR)
        logger.warning("TraceWriter: run errored after %d steps: %s", self._steps_written, error)

    # ── AOP intercept handlers ────────────────────────────────

    async def around_ainvoke(self, *, proceed: Any, args: tuple, kwargs: dict, **ctx: Any) -> Any:
        """Record one LLM call: registered before it goes out, settled when it ends.

        Registration comes first so that the prompt is on record whatever
        happens next. The outcome is one of three:

        - **completed** — the reply, as ``RecordedCompletion``.
        - **failed** — any ``Exception``, recorded with what it carried and
          nothing invented: ``raw_output`` for a structured reply the
          validator refused (the run *continues* past that one: the Loop
          hands the model its own text back), ``partial_output`` for a
          truncation, ``stop_reason`` and ``usage`` because a failed call is
          billed. The prompt that caused a 429 or a timeout is worth having
          too, so every exception is recorded, not only the recoverable one.
        - **cancelled** — a ``BaseException``: ``CancelledError`` from the
          run's timeout or from outside, which neither ``after`` advice nor
          ``except Exception`` ever sees. Without this the prompt of the
          call in flight when a run was cut off was lost.

        The exception always propagates; this hook observes, it does not
        swallow. One advice records the call from start to end, so nothing
        has to correlate a ``before`` with an ``after`` — which would break
        the day a Loop issues calls concurrently.

        This is also the only place that can measure what the step spent
        waiting on the model; the accumulator is in a ``finally`` because a
        step whose LLM time went on a refused reply spent it all the same.
        """
        call = self._register_call(args, kwargs)
        started = time.perf_counter()
        try:
            result = await proceed()
        except Exception as exc:
            call.fail(exc)
            raise
        except BaseException as exc:
            call.cancel(exc)
            raise
        finally:
            self._pending_llm_ms += (time.perf_counter() - started) * 1000
        call.complete(result)
        return result

    async def after_record_step(
        self, result: Any, *, args: tuple, kwargs: dict, **ctx: Any
    ) -> None:
        """Commit point: the observation, every call of the step, the step record."""
        if self._wal is None:
            return

        record = args[0] if args else kwargs.get("record")
        if record is None:
            return
        observation = kwargs.get("observation")
        step = int(getattr(record, "step", self._steps_written))

        self._stamp_timings(record)

        pending = list(self._pending_calls)
        self._wal.commit_step(
            step,
            record,
            fragments=encode_observation(step, observation),
            llm_calls=encode_llm_calls(step, pending, first_call=self._calls_flushed),
        )
        # Only once the commit succeeded: a call whose step failed to commit
        # is still owed to the trace, and run end commits it as trailing.
        del self._pending_calls[: len(pending)]
        self._calls_flushed = 0

        self._steps_written += 1
        logger.debug("TraceWriter: committed step %d (%d LLM calls)", step, len(pending))

    # ── internals ─────────────────────────────────────────────

    def _stamp_timings(self, record: Any) -> None:
        """Write the step's wall clock and model wait onto its record.

        Measured here rather than in the Loop on purpose: timing is a
        cross-cutting concern, and a Loop that timed itself would have to
        be re-instrumented once per Loop. The two clocks are the only
        ones a hook can see — the boundary between commits, and the
        ``around`` advice on ``ainvoke`` — and both are reset here so the
        next step measures itself, not the run so far.

        A record that already carries a value keeps it: the Loop is
        allowed to know better, and nothing here should overwrite a
        measurement made closer to the work.

        Two things about this being a *mutation*, both honest limits
        rather than guarantees:

        - The object is the Loop's own ``StepRecord``, which is also the
          payload of ``HistoryObs`` and therefore reachable from prompt
          building. No shipped agent renders these fields (they enumerate
          what they render), and a test pins that; an agent that dumps a
          record wholesale would make its prompt depend on whether
          ``trace_writer`` is in the ``hook:`` list, which is exactly the
          non-transparency AOP exists to prevent.
        - Nothing orders intercept handlers. ``priority`` orders
          *lifecycle* dispatch only (``hooks/base.py``); the Weaver
          indexes interceptors in hook-list order and ``core/aop/`` never
          reads ``priority``. ``TraceWriter`` is today the only hook
          intercepting ``record_step``, so the question does not arise —
          but a second one reading ``record.duration_ms`` would see
          ``None`` or a value purely by YAML order, silently. Treat these
          fields as readable only after the commit point.
        """
        now = time.perf_counter()
        if isinstance(record, StepRecord):
            if record.duration_ms is None and self._step_started is not None:
                record.duration_ms = int((now - self._step_started) * 1000)
            if record.llm_latency_ms is None and self._pending_llm_ms:
                record.llm_latency_ms = int(self._pending_llm_ms)
        self._step_started = now
        self._pending_llm_ms = 0.0

    def _register_call(self, args: tuple, kwargs: dict) -> PendingLLMCall:
        """Put a call on record before it goes out.

        The prompt is ``ainvoke``'s first argument; the requested reply
        model arrives as ``output_format`` (ReactLoop calls
        ``llm.ainvoke(context, output_format=output_model)``). Neither is
        validated — see :mod:`spectragent.core.trace.encoding`.
        """
        messages = args[0] if args else kwargs.get("messages")
        output_format = kwargs.get("output_format", args[1] if len(args) > 1 else None)
        call = PendingLLMCall(messages, self._output_format(output_format))
        self._pending_calls.append(call)
        return call

    def _output_format(self, output_format: Any) -> EncodedOutputFormat | None:
        """The requested schema, generated once per reply model per run.

        A model whose schema cannot be generated is recorded as no schema,
        with a warning, rather than failing the call over the trace: the
        provider builds its request from the same schema and reports that
        failure itself.
        """
        if output_format is None:
            return None
        try:
            return self._output_formats[output_format]
        except KeyError:
            pass
        except TypeError:  # unhashable — not a class, so not worth caching
            return _encode_output_format(output_format)
        encoded = self._output_formats[output_format] = _encode_output_format(output_format)
        return encoded

    def _flush_trailing(self) -> None:
        """Commit the calls of a step the Loop never surrendered.

        ``after_record_step`` is the only *commit* point, but it is not the
        only *termination* point: a run can be cut off by the timeout or a
        cancellation mid-call, or crash in parsing or action execution after
        the reply came back. Dropping the registered calls there discards
        precisely the exchange that says what the agent was doing when the
        run died — and abnormal termination is the common case, not the
        exception, in this project's real traces.

        They are recorded under the step index that *would* have been next
        and listed in ``manifest.trailing``, not as a step:
        ``RunTrace.trailing_calls`` reads them back.
        """
        if self._wal is None or not self._pending_calls:
            return

        # The step index the Loop was working on. Derived from the manifest
        # rather than a local counter so it stays right for a Loop that
        # numbers its steps from something other than 0.
        manifest = self._wal.manifest
        step = manifest.last_step_completed + 1 if manifest is not None else self._steps_written

        pending = list(self._pending_calls)
        self._wal.commit_trailing(
            llm_calls=encode_llm_calls(step, pending, first_call=self._calls_flushed)
        )
        del self._pending_calls[: len(pending)]
        self._calls_flushed += len(pending)
        # No StepRecord exists for these, so the accumulated model wait has
        # nowhere to land. Cleared rather than carried, so a driver that
        # drains and keeps going does not attribute it to a later step.
        self._pending_llm_ms = 0.0
        logger.info(
            "TraceWriter: committed %d LLM call(s) of unfinished step %d as trailing",
            len(pending),
            step,
        )


def _encode_output_format(output_format: Any) -> EncodedOutputFormat | None:
    try:
        return encode_output_format(output_format)
    except Exception:  # the trace must never be what fails a call
        logger.warning(
            "TraceWriter: could not encode the JSON schema of output_format %r; "
            "its calls are recorded without one",
            output_format,
            exc_info=True,
        )
        return None


def _status_for(result: Any) -> RunStatus:
    """Map a terminal AgentResult onto a manifest status."""
    done = getattr(result, "agent_done", None)
    if done is not None and done in _ERROR_DONE_STATES:
        return RunStatus.ERROR
    if getattr(result, "error", None):
        return RunStatus.ERROR
    return RunStatus.COMPLETED
