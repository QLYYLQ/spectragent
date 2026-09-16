"""TraceWriter — RuntimeHook for full WAL-based trace persistence.

The commit point is ``loop.record_step``: the moment the Loop finishes a
step and surrenders its :class:`StepRecord`. Everything a step consists
of is known then — the observation the agent actually saw, the actions it
chose, what they returned, what the step cost — so the writer serializes
facts instead of reconstructing them.

It used to commit on ``llm.ainvoke`` instead, which is the wrong event
twice over: it fires before the actions exist, and it fires once per LLM
call rather than once per step.

Registered via conscribe as hook_id="trace_writer".

See docs/execution_dag.md M11 for design details.

.. note::
   No ``from __future__ import annotations`` here — ``trace_dir`` must
   stay a real ``Annotated[...]`` object so any future ``TraceWriter``
   subclass living in another module still resolves it (see the same
   note on ``hooks/_registrar.py``).
"""

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from spectragent.core.aop.advice import Intercept
from spectragent.core.hooks._registrar import HookBase
from spectragent.core.trace.models import RunStatus, TraceConfig
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas._types import AgentDoneStatus
from spectragent.schemas.observation._registrar import FragmentBase
from spectragent.schemas.observation.dynamic_model import iter_fragments

logger = logging.getLogger(__name__)

# Terminal states that mean "the agent stopped without a clean finish".
# The manifest must say so: a trace stuck at ``running`` is a trace no
# evaluator will touch, and 9 of the first 14 real traces were exactly
# that because the timeout path never reached a finalizer.
_ERROR_DONE_STATES = frozenset({AgentDoneStatus.TIMEOUT, AgentDoneStatus.ERROR})


@dataclass
class TraceWriter(HookBase):
    """Full-trace WAL persistence hook.

    Buffers LLM messages (``after_ainvoke``, and ``around_ainvoke`` for a
    call that raised) and commits a complete step — observation fragments,
    messages, step record, manifest — when the Loop hands over its
    ``StepRecord`` (``after_record_step``).

    A run that dies between the completion and the step (timeout, crash)
    never reaches that commit point, so run end drains whatever is still
    buffered as *unfinished* messages instead of dropping it.
    """

    __registry_key__ = "trace_writer"
    __intercepts__ = [
        Intercept("ainvoke", role="llm", advice="after"),
        # ``after`` does not fire when the target raises, and one LLM error
        # is recoverable — the run continues past it. See
        # :meth:`around_ainvoke`.
        Intercept("ainvoke", role="llm", advice="around"),
        Intercept("record_step", role="loop", advice="after"),
    ]

    # Serializing must happen after every enricher has finished mutating
    # the result.
    priority = HookBase.PRIORITY_PERSIST

    # ── conscribe config-annotated fields ─────────────────────
    trace_dir: Annotated[
        str,
        Field(
            default="traces",
            description=(
                "Base directory for trace output. Each run lands in "
                "``<trace_dir>/<run_id>``. Relative paths resolve against "
                "the process working directory."
            ),
        ),
    ] = "traces"

    # ── internal state (not config) ───────────────────────────
    _wal: WALWriter | None = field(default=None, init=False, repr=False)
    _steps_written: int = field(default=0, init=False, repr=False)
    # [(input_msgs, output)] for the LLM calls made since the last commit
    _pending_messages: list[tuple[Any, Any]] = field(
        default_factory=list, init=False, repr=False
    )

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
        agent: Any = None,
        env: Any = None,
        loop: Any = None,
        llm: Any = None,
        task: str = "",
        task_id: str = "",
        benchmark: str = "",
        subset: str = "",
        run_id: str | None = None,
        config: Any = None,
        **kwargs: Any,
    ) -> None:
        resolved_run_id = run_id or str(uuid.uuid4())[:8]
        base_dir = Path(self.trace_dir)

        self._wal = WALWriter(base_dir, resolved_run_id)
        self._steps_written = 0
        self._pending_messages = []

        trace_config = TraceConfig(
            agent_id=getattr(agent, "agent_id", getattr(agent, "agent_name", "")),
            loop_id=getattr(loop, "loop_id", ""),
            env_type=getattr(env, "env_type", ""),
            llm_model=_model_name(llm),
            benchmark=benchmark,
            subset=subset,
            task=task,
            task_id=task_id,
            extra=config if isinstance(config, dict) else {},
        )
        self._wal.init_run(trace_config)
        logger.info(
            "TraceWriter: initialized run %s at %s", resolved_run_id, self._wal.run_dir
        )

    async def on_run_end(self, *, result: Any = None, env: Any = None, **kwargs: Any) -> None:
        """Write result.json and finalize — on every termination path."""
        if self._wal is None:
            return
        self._flush_pending_messages()
        # No result means no result.json. ``RunTrace.result`` returns
        # ``None`` for exactly that state; a file containing ``null``
        # would instead read as a result that fails to parse.
        if result is not None:
            self._wal.write_result(result)
        self._wal.finalize(_status_for(result))
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
        # Idempotent: ``on_run_end`` normally drains first, but a driver
        # that only dispatches ``on_run_error`` must not lose the buffer.
        self._flush_pending_messages()
        self._wal.finalize(RunStatus.ERROR)
        logger.warning(
            "TraceWriter: run errored after %d steps: %s", self._steps_written, error
        )

    # ── AOP intercept handlers ────────────────────────────────

    async def after_ainvoke(
        self, result: Any, *, args: tuple, kwargs: dict, **ctx: Any
    ) -> None:
        """Buffer one LLM call; it is written when the step commits."""
        # args[0] is the messages list passed to ainvoke()
        input_msgs = args[0] if args else kwargs.get("messages")
        self._pending_messages.append((input_msgs, result))

    async def around_ainvoke(
        self, *, proceed: Any, args: tuple, kwargs: dict, **ctx: Any
    ) -> Any:
        """Buffer a call whose *reply* was refused, which ``after`` misses.

        A structured reply that does not match the schema comes back as an
        exception, so no ``after`` advice fires — and yet the run does not
        end there: the Loop hands the model its own text back and takes
        another step. Without this the trace would carry a step whose
        messages file is simply absent, and "Record Once, Evaluate Many"
        would be false for exactly the steps a researcher most wants to
        read: the ones where the model got it wrong.

        The exception is buffered in place of the completion, serialized as
        what it is — the error, the model's raw text — rather than shaped
        into a completion it never was.

        Deliberately every exception, not only the recoverable one. The
        messages that *caused* a 429 or a timeout are the same messages,
        and ``on_run_end`` already drains the buffer as unfinished; a
        prompt that kills the endpoint is worth having in the trace.
        """
        input_msgs = args[0] if args else kwargs.get("messages")
        try:
            return await proceed()
        except Exception as exc:
            self._pending_messages.append((input_msgs, _failed_call(exc)))
            raise

    async def after_record_step(
        self, result: Any, *, args: tuple, kwargs: dict, **ctx: Any
    ) -> None:
        """Commit point: observation + messages + real step record."""
        if self._wal is None:
            return

        record = args[0] if args else kwargs.get("record")
        if record is None:
            return
        observation = kwargs.get("observation")
        step = int(getattr(record, "step", self._steps_written))

        obs_files = self._write_observation(step, observation)

        message_files: list[str] = []
        first_in: str | None = None
        first_out: str | None = None
        for call_idx, (input_msgs, output) in enumerate(self._pending_messages):
            in_rel, out_rel = self._wal.write_messages(
                step, input_msgs, output, call=call_idx
            )
            message_files.extend((in_rel, out_rel))
            if call_idx == 0:
                first_in, first_out = in_rel, out_rel
        self._pending_messages = []

        step_rel = self._wal.write_step_record(step, record)

        # Atomic commit
        self._wal.commit_step(
            step=step,
            step_file=step_rel,
            observation_files=obs_files,
            message_input_file=first_in,
            message_output_file=first_out,
            message_files=message_files,
        )

        self._steps_written += 1
        logger.debug("TraceWriter: committed step %d", step)

    # ── internals ─────────────────────────────────────────────

    def _flush_pending_messages(self) -> None:
        """Persist LLM calls the Loop never got to surrender as a step.

        ``after_record_step`` is the only *commit* point, but it is not
        the only *termination* point: a run can be killed by the timeout,
        or crash in parsing or action execution, after the completion has
        already come back. Dropping the buffer there discards precisely
        the exchange you need to see what the agent was doing when the
        run died — and abnormal termination is the common case, not the
        exception, in this project's real traces.

        The files are written under the step index that *would* have been
        next, so ``RunTrace.get_messages(step)`` finds them by path, and
        listed in the manifest as unfinished rather than committed.
        """
        if self._wal is None or not self._pending_messages:
            return

        # The step index the Loop was working on. Derived from the
        # manifest rather than a local counter so it stays right for a
        # Loop that numbers its steps from something other than 0.
        manifest = self._wal.manifest
        step = (
            manifest.last_step_completed + 1 if manifest is not None else self._steps_written
        )

        files: list[str] = []
        for call_idx, (input_msgs, output) in enumerate(self._pending_messages):
            in_rel, out_rel = self._wal.write_messages(
                step, input_msgs, output, call=call_idx
            )
            files.extend((in_rel, out_rel))
        pending = len(self._pending_messages)
        self._pending_messages = []

        self._wal.commit_unfinished_messages(files)
        logger.info(
            "TraceWriter: flushed %d unfinished LLM call(s) as step %d", pending, step
        )

    def _write_observation(self, step: int, observation: Any) -> list[str]:
        """Write every populated fragment; binaries land as real files.

        Files are named after the fragment's **fully-qualified** key
        (``000_browser.dom.json``), not the negotiated local name. The
        local name is a per-run artefact of which capability won
        negotiation, so a trace keyed by it cannot tell an evaluator
        which capability produced the data — and an evaluator reading a
        ``RunTrace`` is the only consumer this file has.
        """
        if self._wal is None or observation is None:
            return []

        obs_files: list[str] = []
        fragments = list(iter_fragments(observation))
        if not fragments:
            # Not a fragment model — persist it whole rather than lose it.
            return [self._wal.write_observation(step, "observation", observation)]

        for field_name, fragment in fragments:
            key = getattr(fragment, "fragment_id", "") or field_name
            payload: Any = fragment
            binary = fragment.trace_binary() if isinstance(fragment, FragmentBase) else None

            if binary is not None:
                suffix, data = binary
                obs_files.append(
                    self._wal.write_observation(step, key, data, suffix=suffix)
                )
                # JSON sidecar without the bytes, plus a pointer to them.
                payload = fragment.model_dump(
                    mode="json", exclude=fragment.trace_json_exclude()
                )
                payload["artifact_file"] = f"{step:03d}_{key}{suffix}"

            obs_files.append(self._wal.write_observation(step, key, payload))
        return obs_files


def _failed_call(exc: BaseException) -> dict[str, Any]:
    """What goes in the output file when the call raised instead of returning.

    A plain dict, not a stand-in ``ChatInvokeCompletion``: whoever reads
    the trace has to be able to tell a reply the model wrote from one the
    framework invented, and a completion-shaped object with an error in it
    reads as the former. ``raw_output`` appears only when the provider had
    text to hand over — a schema mismatch does, a dead socket does not.
    """
    record: dict[str, Any] = {"error": type(exc).__name__, "detail": str(exc)}
    raw = getattr(exc, "raw_output", None)
    if raw is not None:
        record["raw_output"] = raw
    return record


def _status_for(result: Any) -> RunStatus:
    """Map a terminal AgentResult onto a manifest status."""
    done = getattr(result, "agent_done", None)
    if done is not None and done in _ERROR_DONE_STATES:
        return RunStatus.ERROR
    if getattr(result, "error", None):
        return RunStatus.ERROR
    return RunStatus.COMPLETED


def _model_name(llm: Any) -> str:
    """Best-effort model identifier for the config snapshot."""
    if llm is None:
        return ""
    for attr in ("model", "model_name", "name"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""
