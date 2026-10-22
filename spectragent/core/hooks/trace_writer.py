"""One run-owned fact ledger with bounded, execution-independent persistence.

Snapshots and segment handoff are blocking AOP work. WAL commits and retries
belong to the recorder task; no failed write controls the Agent's next step.
"""

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, NamedTuple

from pydantic import Field

from spectragent.core.aop.advice import Intercept
from spectragent.core.archive import (
    ArchiveBatch,
    ArchiveContractError,
    ArchiveFile,
    ArchiverBase,
    segment_problem,
    text_problem,
)
from spectragent.core.archive.privacy import RedactionLedger, redact_value
from spectragent.core.hooks._registrar import HookBase
from spectragent.core.llm.call_context import RunCallContext
from spectragent.core.llm.views import ModelInvokeUsage
from spectragent.core.trace.encoding import (
    EncodedOutputFormat,
    PendingLLMCall,
    TraceFile,
    archive_run_path,
    archive_step_path,
    check_relative_path,
    encode_archive_file,
    encode_call_metrics,
    encode_llm_calls,
    encode_observation,
    encode_output_format,
    fold_path,
    sha256_hex,
    snapshot,
)
from spectragent.core.trace.models import (
    TaskSetupRecord,
    TraceConfig,
    TraceNegotiation,
    TraceStatus,
    recorded_prefix,
)
from spectragent.core.trace.redaction import redact_config, trace_safe
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas.agent_result import AgentUsage
from spectragent.schemas.config_source import ConfigSource
from spectragent.schemas.recording import (
    ExecutionError,
    RecordingHealth,
    RecordingPlan,
    RecordingProblem,
    UsageCoverage,
)
from spectragent.schemas.restoration import BranchProvenance
from spectragent.schemas.step_record import ActionSpan, StepRecord, TokenUsage

logger = logging.getLogger(__name__)
_RunVersions = dict[str, tuple[int, str, int]]

#: Seconds the close-out archive collection may take before it is abandoned.
#: A capability bounds its own collection (the browser alone may wait 5 s for
#: its event channel, then for in-flight bodies and storage reads), so this is
#: the backstop for one that does not: long enough that a slow capability
#: reports its own failure, and spent on its own clock, so the recording
#: worker's drain (``close_timeout``) is never what a hung environment uses up.
CLOSEOUT_COLLECTION_TIMEOUT_S = 10.0


class _Collected(NamedTuple):
    number: int
    files: tuple[ArchiveFile, ...]


@dataclass
class RecordingSegment:
    """A sealed execution segment; retries keep all original identities."""

    step: int | None
    record: StepRecord | None
    fragments: list[Any]
    calls: tuple[PendingLLMCall, ...]
    archive: _Collected | None
    capture_ms: float = 0.0
    attempts: int = 0
    content_committed: bool = False
    timing_committed: bool = False
    trailing_index: int | None = None
    timing_index: int | None = None
    work_ms: float = 0.0
    archive_versions: _RunVersions | None = None
    segment_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class RecordingStorage:
    """One run's single-writer state, retained by an in-flight daemon I/O job.

    A later execution owns a different instance. A timed-out system call can
    therefore never discover or mutate the next execution's state.
    """

    wal: WALWriter | None
    config: TraceConfig | None
    config_hash: str
    plan: RecordingPlan
    stopped: threading.Event
    run_versions: _RunVersions = field(default_factory=dict)
    unconfirmed_archive: list[tuple[int | None, str, _RunVersions]] = field(default_factory=list)


class _TraceWriterReceptors:
    archive: list[ArchiverBase]


class TraceWriter(_TraceWriterReceptors, HookBase):
    """Collect once, persist selected facts, report write health separately."""

    __registry_key__ = "trace_writer"
    __wiring__: ClassVar[dict[str, Any]] = {"archive": "archiver"}
    __intercepts__ = [
        Intercept("ainvoke", role="llm", advice="around"),
        Intercept("act", role="env", advice="around"),
        Intercept("execute_step", role="loop", advice="around"),
        Intercept("observe_terminal", role="loop", advice="around"),
        Intercept("record_step", role="loop", advice="around"),
    ]
    priority = HookBase.PRIORITY_PERSIST

    def __init__(
        self,
        trace_dir: Annotated[
            str, trace_safe("record output directory", default="traces")
        ] = "traces",
        record_secrets: Annotated[
            bool,
            Field(
                default=False,
                description="Persist configured secrets verbatim; defaults to redaction.",
            ),
        ] = False,
        recording: Annotated[
            RecordingPlan | None,
            Field(
                description="Facts selected before capture and persisted; omitted channels are not damaged records.",
            ),
        ] = None,
        pending_segments: Annotated[
            int,
            Field(
                default=64,
                ge=1,
                description="Maximum sealed segments awaiting persistence; overflow reports lost coverage.",
            ),
        ] = 64,
        write_retries: Annotated[
            int,
            Field(
                default=2,
                ge=0,
                description="Additional background write attempts per sealed segment.",
            ),
        ] = 2,
        retry_delay: Annotated[
            float,
            Field(
                default=0.05,
                ge=0,
                description="Seconds between background write attempts; never awaited by execution.",
            ),
        ] = 0.05,
        close_timeout: Annotated[
            float,
            Field(
                default=5.0,
                gt=0,
                description=(
                    "Maximum seconds close-out waits for the recording worker to drain and "
                    "write the result and the closed manifest; counted after the close-out "
                    "archive collection, never shared with it."
                ),
            ),
        ] = 5.0,
        closeout_collection_timeout: Annotated[
            float,
            Field(
                default=CLOSEOUT_COLLECTION_TIMEOUT_S,
                gt=0,
                description=(
                    "Maximum seconds the last archive collection at close-out may take; "
                    "an overrun is recorded as an archive problem and the trace still closes."
                ),
            ),
        ] = CLOSEOUT_COLLECTION_TIMEOUT_S,
        archive: Annotated[
            list[ArchiverBase] | None,
            Field(
                default=None,
                description="Selected environment archivers, constructed only in this receptor.",
            ),
        ] = None,
    ) -> None:
        super().__init__()
        self.trace_dir = trace_dir
        self.record_secrets = record_secrets
        self.recording = recording if recording is not None else RecordingPlan()
        self.pending_segments = pending_segments
        self.write_retries = write_retries
        self.retry_delay = retry_delay
        self.close_timeout = close_timeout
        self.closeout_collection_timeout = closeout_collection_timeout
        self.archive = list(archive or [])
        self.wal: WALWriter | None = None
        self.call_context = RunCallContext()
        self._health = RecordingHealth()
        self._reset()

    def _reset(self) -> None:
        self._stop_writes = threading.Event()
        self._pending_calls: list[PendingLLMCall] = []
        self._call_numbers: dict[int, int] = {}
        self._output_formats: dict[Any, EncodedOutputFormat | None] = {}
        self._queue: asyncio.Queue[RecordingSegment] = asyncio.Queue(self.pending_segments)
        self._worker: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False
        self._result: Any = None
        self._config: TraceConfig | None = None
        self._config_hash = ""
        self._model = ""
        self._run_id = ""
        self._next_step = 0
        self._started_steps: dict[int, float] = {}
        # Per step, a slot per ``env.act`` call in call order, filled when it ends.
        self._action_spans: dict[int, list[ActionSpan | None]] = {}
        self._capture_ms: dict[int, float] = {}
        self._usage = AgentUsage()
        self._coverage = UsageCoverage()
        self._env: Any = None
        self._archive_keys: frozenset[str] = frozenset()
        self._archive_owners: frozenset[str] = frozenset()
        self._collections = 0
        self._storage: RecordingStorage | None = None
        self._health = RecordingHealth()

    def __repr__(self) -> str:
        return (
            f"TraceWriter(trace_dir={self.trace_dir!r}, record_secrets={self.record_secrets!r}, "
            f"archive={[item.archiver_id for item in self.archive]!r})"
        )

    @property
    def blocking(self) -> bool:
        return True

    def intercept_environment(self, present: bool) -> None:
        """Time ``env.act`` only in a run that has an environment (``EnvironmentInterceptor``).

        An env-less run executes no action, so its measured steps carry
        ``action_spans == []`` without an interception that could never fire.
        """
        intercepts = [item for item in type(self).__intercepts__ if present or item.role != "env"]
        # The declaration is a ClassVar: shadow it on this instance only.
        object.__setattr__(self, "__intercepts__", intercepts)

    @property
    def health(self) -> RecordingHealth:
        """An independent snapshot of this run's recording coverage and faults."""
        return self._health.model_copy(deep=True)

    @property
    def run_dir(self) -> Path | None:
        return self.wal.run_dir if self.wal is not None else None

    @property
    def llm_calls(self) -> int | None:
        return self._coverage.calls if self.recording.metrics else None

    async def on_run_start(
        self,
        *,
        env: Any = None,
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
        archive_negotiation: Any = None,
        env_evidence_privacy: Sequence[str] | None = None,
        task_setup: Mapping[str, Any] | None = None,
        branch: BranchProvenance | None = None,
        config_source: ConfigSource | None = None,
        call_context: RunCallContext | None = None,
        action_space: Any = None,
        **kwargs: Any,
    ) -> None:
        self._reset()
        self.call_context = call_context if call_context is not None else RunCallContext()
        self._run_id = run_id or str(uuid.uuid4())
        self._model = llm_model
        self.wal = None
        try:
            self.wal = WALWriter(Path(self.trace_dir), self._run_id)
            if self.archive:
                self._refuse_an_unbound_archive(archive_negotiation, env)
                self._env = env
                self._archive_keys = frozenset(a.archiver_id for a in self.archive)
                self._archive_owners = frozenset(archive_negotiation.archive_bindings.values())
            resolved_full = (
                redact_config(resolved_config, record_secrets=True)
                if resolved_config is not None
                else {}
            )
            self._config = TraceConfig(
                recording=self.recording,
                agent_id=agent_id,
                loop_id=loop_id,
                env_type=env_type,
                llm_model=llm_model,
                benchmark=benchmark,
                subset=subset,
                task=task,
                task_id=task_id,
                extra=snapshot(config) if isinstance(config, Mapping) else {},
                resolved=redact_config(resolved_config, record_secrets=self.record_secrets)
                if resolved_config is not None
                else {},
                negotiation=TraceNegotiation.from_results(
                    observation_negotiation, action_negotiation, archive_negotiation
                ),
                secrets_recorded=self.record_secrets,
                env_evidence_privacy=sorted(set(env_evidence_privacy))
                if env_evidence_privacy is not None
                else None,
                task_setup=_task_setup_record(task_setup, record_secrets=self.record_secrets),
                branch=branch,
                config_source=config_source,
                **(
                    {"action_space": action_space.model_copy(deep=True)}
                    if action_space is not None
                    else {}
                ),
            )
            self._config_hash = self._config.configuration_digest(resolved=resolved_full)
            await self._offload(self.wal.init_run, self._config, config_hash=self._config_hash)
        except Exception as exc:
            self._problem(None, "initialization", "initialize", exc)
        self._storage = RecordingStorage(
            self.wal, self._config, self._config_hash, self.recording, self._stop_writes
        )
        self._worker = asyncio.create_task(self._persist(), name=f"recording:{self._run_id}")

    def _problem(
        self,
        step: int | None,
        channel: str,
        phase: str,
        error: BaseException,
        *,
        segment_id: str | None = None,
        call_ids: Sequence[str] = (),
    ) -> None:
        name = type(error).__name__
        self._record_problem(
            step,
            channel,
            phase,
            name,
            str(error) or name,
            segment_id=segment_id,
            call_ids=call_ids,
        )

    def _record_problem(
        self,
        step: int | None,
        channel: str,
        phase: str,
        error: str,
        detail: str,
        *,
        segment_id: str | None = None,
        call_ids: Sequence[str] = (),
    ) -> None:
        problem = RecordingProblem(
            step=step,
            segment_id=segment_id,
            call_ids=list(call_ids),
            channel=channel,
            phase=phase,
            error=error,
            detail=detail,
            occurred_at=time.time(),
        )
        self._health.problems.append(problem)
        logger.warning(
            "Recording %s failed at step %s (%s): %s: %s",
            phase,
            step,
            channel,
            problem.error,
            problem.detail,
        )

    async def around_execute_step(
        self, *, proceed: Any, args: tuple, kwargs: dict, **ctx: Any
    ) -> Any:
        scope = None
        try:
            step = int(kwargs.get("step_idx", args[1] if len(args) > 1 else self._next_step))
            self._started_steps.setdefault(step, time.perf_counter())
            attribution = self.call_context.current
            scope = self.call_context.scope(attribution.caller, attribution.purpose, step)
            scope.__enter__()
        except Exception as exc:
            scope = None
            self._problem(None, "timing", "capture", exc)
        try:
            return await proceed()
        finally:
            if scope is not None:
                try:
                    scope.__exit__(None, None, None)
                except Exception as exc:
                    self._problem(None, "attribution", "capture", exc)

    async def around_observe_terminal(
        self, *, proceed: Any, args: tuple, kwargs: dict, **ctx: Any
    ) -> Any:
        try:
            step = int(kwargs["step"])
            self._started_steps.setdefault(step, time.perf_counter())
        except Exception as exc:
            self._problem(None, "timing", "capture", exc)
        return await proceed()

    async def around_act(self, *, proceed: Any, args: tuple, kwargs: dict, **ctx: Any) -> Any:
        """One ``ActionSpan`` per ``env.act`` call, for the step executing it.

        Attributed like a model call: the step ``execute_step`` opened, else
        the next one. The action's result, exception or cancellation passes
        through unchanged; a recording fault only costs the span.
        """
        if not self.recording.metrics:
            return await proceed()
        step = self._next_step
        slot: tuple[list[ActionSpan | None], int, str] | None = None
        try:
            attributed = self.call_context.current.step
            step = step if attributed is None else attributed
            spans = self._action_spans.setdefault(step, [])
            action = args[0] if args else kwargs["action"]
            slot = (spans, len(spans), str(action.name))
            spans.append(None)
        except Exception as exc:
            self._problem(step, "timing", "capture", exc)
        outcome: Literal["returned", "raised", "cancelled"] = "cancelled"
        started = time.time()
        try:
            result = await proceed()
            outcome = "returned"
            return result
        except Exception:
            outcome = "raised"
            raise
        finally:
            if slot is not None:
                spans, index, name = slot
                try:
                    spans[index] = ActionSpan(
                        action=name, started_at=started, ended_at=time.time(), outcome=outcome
                    )
                except Exception as exc:
                    self._problem(step, "timing", "capture", exc)

    async def around_ainvoke(self, *, proceed: Any, args: tuple, kwargs: dict, **ctx: Any) -> Any:
        pending: PendingLLMCall | None = None
        capture_started = time.perf_counter()
        call_step = self._next_step
        try:
            attribution = self.call_context.current
            step = self._next_step if attribution.step is None else attribution.step
            call_step = step
            number = self._call_numbers.get(step, 0)
            self._call_numbers[step] = number + 1
            output = kwargs.get("output_format", args[1] if len(args) > 1 else None)
            target = ctx.get("target")
            model = str(target.model) if target is not None else self._model
            kind = getattr(target, "inference_kind", "chat")
            if kind not in ("chat", "decision"):
                raise ValueError(f"Unsupported inference protocol {kind!r}")
            logical_input = (
                args[0] if args else kwargs.get("request" if kind == "decision" else "messages")
            )
            pending = PendingLLMCall(
                logical_input if kind == "chat" else None,
                kind=kind,
                owner="run",
                owner_id=self._run_id,
                request=logical_input if kind == "decision" else None,
                output_format=self._output_format(output) if self.recording.trajectory else None,
                capture_trajectory=self.recording.trajectory,
                capture_metrics=self.recording.metrics,
                capture_errors=self.recording.errors,
                call_id=f"{self._run_id}:{step}:{number}",
                step=step,
                call=number,
                caller=attribution.caller,
                purpose=attribution.purpose,
                model=model,
            )
            self._pending_calls.append(pending)
        except Exception as exc:
            self._problem(self._next_step, "calls", "capture", exc)
        self._capture_ms[call_step] = (
            self._capture_ms.get(call_step, 0) + (time.perf_counter() - capture_started) * 1000
        )
        settle_started: float | None = None
        try:
            result = await proceed()
        except BaseException as exc:
            settle_started = time.perf_counter()
            if pending is not None:
                try:
                    pending.fail(exc) if isinstance(exc, Exception) else pending.cancel(exc)
                except Exception as recording_error:
                    self._problem(pending.step, "calls", "settle", recording_error)
            raise
        else:
            settle_started = time.perf_counter()
            if pending is not None:
                try:
                    pending.complete(result)
                except Exception as exc:
                    self._problem(pending.step, "calls", "settle", exc)
            return result
        finally:
            if pending is not None and self.recording.metrics:
                self._account(pending)
            if settle_started is not None:
                self._capture_ms[call_step] = (
                    self._capture_ms.get(call_step, 0)
                    + (time.perf_counter() - settle_started) * 1000
                )

    def _account(self, call: PendingLLMCall) -> None:
        try:
            self._coverage.calls += 1
            usage = ModelInvokeUsage.model_validate(call.usage) if call.usage is not None else None
            if usage is None:
                self._coverage.missing_call_ids.append(call.call_id)
            else:
                self._coverage.reported_calls += 1
            self._usage.add_llm_usage(usage, model=call.model, purpose=call.purpose)
        except Exception as exc:
            self._problem(call.step, "metrics", "account", exc)

    async def around_record_step(
        self, *, proceed: Any, args: tuple, kwargs: dict, **ctx: Any
    ) -> Any:
        try:
            record = args[0] if args else kwargs["record"]
            step = record.step
            # This edge stops execution timing before sink and recording work.
            started = self._started_steps.pop(step, None)
            spans = self._action_spans.pop(step, None) or []
            if self.recording.metrics and started is not None:
                record.duration_ms = max(
                    0, int((time.perf_counter() - started) * 1000 - self._capture_ms.get(step, 0))
                )
                # A call still running when its step is handed over has no end.
                record.action_spans = [span for span in spans if span is not None]
        except Exception as exc:
            self._problem(None, "timing", "capture", exc)
        result = await proceed()
        await self.after_record_step(result, args=args, kwargs=kwargs, **ctx)
        return result

    async def after_record_step(
        self, result: Any, *, args: tuple, kwargs: dict, **ctx: Any
    ) -> None:
        """Seal synchronously; capture failure cannot retain calls for the next step."""
        record = args[0] if args else kwargs["record"]
        step = record.step
        started = time.perf_counter()
        calls = tuple(call for call in self._pending_calls if call.step == step)
        self._pending_calls = [call for call in self._pending_calls if call.step != step]
        self._next_step = max(self._next_step, step + 1)
        self._health.expected_steps = max(self._health.expected_steps, step + 1)
        try:
            if self.recording.errors:
                errors = []
                if record.error:
                    errors.append(
                        ExecutionError(
                            phase=record.error_phase,
                            detail=record.error,
                            occurred_at=record.timestamp,
                        )
                    )
                for index, outcome in enumerate(record.action_results):
                    if outcome.error or not outcome.success:
                        errors.append(
                            ExecutionError(
                                phase="act",
                                detail=outcome.error or "action reported failure",
                                occurred_at=record.timestamp,
                                action_index=index,
                                action_name=record.actions[index].name
                                if index < len(record.actions)
                                else None,
                            )
                        )
                record.execution_errors = errors
            if self.recording.metrics:
                usages = [
                    ModelInvokeUsage.model_validate(call.usage)
                    for call in calls
                    if call.usage is not None
                ]
                record.action_count = len(record.action_results)
                record.proposed_action_count = len(record.actions) + record.truncated_actions
                record.token_usage = TokenUsage.combine(usages)
                record.usage_coverage = UsageCoverage(
                    calls=len(calls),
                    reported_calls=len(usages),
                    missing_call_ids=[call.call_id for call in calls if call.usage is None],
                )
                record.llm_latency_ms = sum(call.latency_ms or 0 for call in calls)
            sealed = (
                record.model_copy(deep=True)
                if self.recording.trajectory
                else StepRecord(
                    step=step,
                    timestamp=record.timestamp,
                    origin=record.origin,
                    duration_ms=record.duration_ms if self.recording.metrics else None,
                    llm_latency_ms=record.llm_latency_ms if self.recording.metrics else None,
                    action_spans=[span.model_copy() for span in record.action_spans]
                    if self.recording.metrics and record.action_spans is not None
                    else None,
                    token_usage=record.token_usage.model_copy(deep=True)
                    if self.recording.metrics and record.token_usage is not None
                    else None,
                    usage_coverage=record.usage_coverage.model_copy(deep=True)
                    if self.recording.metrics and record.usage_coverage is not None
                    else None,
                    action_count=record.action_count if self.recording.metrics else None,
                    proposed_action_count=record.proposed_action_count
                    if self.recording.metrics
                    else None,
                    error_phase=record.error_phase if self.recording.errors else "",
                    error=record.error if self.recording.errors else None,
                    execution_errors=[
                        error.model_copy(deep=True) for error in record.execution_errors
                    ]
                    if self.recording.errors
                    else [],
                )
            )
            if not self.recording.errors:
                sealed.error = None
                sealed.error_phase = ""
                sealed.execution_errors = []
                for outcome in sealed.action_results:
                    outcome.error = None
            if not self.recording.metrics:
                sealed.token_usage = None
                sealed.usage_coverage = None
                sealed.duration_ms = None
                sealed.llm_latency_ms = None
                sealed.action_spans = None
                sealed.action_count = None
                sealed.proposed_action_count = None
            fragments = (
                encode_observation(step, kwargs.get("observation"))
                if self.recording.observations
                else []
            )
            batch = await self._collect() if self.archive else None
            elapsed = (time.perf_counter() - started) * 1000 + self._capture_ms.pop(step, 0)
            self._enqueue(RecordingSegment(step, sealed, fragments, calls, batch, elapsed))
        except Exception as exc:
            self._problem(step, "segment", "capture", exc)
        except BaseException:
            self._problem(step, "segment", "capture", RuntimeError("capture cancelled"))
            raise

    async def _collect(self) -> _Collected:
        files = self._checked(await self._env.collect_archive())
        self._collections += 1
        return _Collected(self._collections, files)

    def _enqueue(self, segment: RecordingSegment) -> None:
        try:
            self._queue.put_nowait(segment)
        except asyncio.QueueFull as exc:
            self._problem(segment.step, "segment", "buffer", exc)

    def _output_format(self, output_format: Any) -> EncodedOutputFormat | None:
        if output_format is None:
            return None
        if output_format in self._output_formats:
            return self._output_formats[output_format]
        try:
            encoded = encode_output_format(output_format)
        except Exception as exc:
            step = self.call_context.current.step
            self._problem(
                self._next_step if step is None else step, "trajectory", "encode_schema", exc
            )
            return None
        self._output_formats[output_format] = encoded
        return encoded

    def _commit(self, segment: RecordingSegment, storage: RecordingStorage) -> None:
        stop_writes = storage.stopped
        if stop_writes.is_set():
            return
        wal = storage.wal
        if wal is None:
            raise RuntimeError("recording storage could not be constructed")
        manifest = wal.reconcile()
        if manifest is None:
            if storage.config is None:
                raise RuntimeError("recording configuration could not be captured")
            wal.init_run(storage.config, config_hash=storage.config_hash)
            manifest = wal.manifest
        if stop_writes.is_set():
            return
        assert manifest is not None
        for candidate_step, candidate_id, versions in storage.unconfirmed_archive:
            installed = (
                any(entry.step == candidate_step for entry in manifest.steps)
                if candidate_step is not None
                else any(entry.segment_id == candidate_id for entry in manifest.trailing)
            )
            if installed:
                self._accept_versions(storage, versions)
        storage.unconfirmed_archive.clear()
        if segment.step is not None:
            segment.content_committed = segment.content_committed or any(
                entry.step == segment.step for entry in manifest.steps
            )
        else:
            segment.content_committed = any(
                entry.segment_id == segment.segment_id for entry in manifest.trailing
            )
        segment.timing_committed = any(
            entry.segment_id == f"{segment.segment_id}:timing" for entry in manifest.trailing
        )
        if segment.content_committed and segment.archive_versions is not None:
            self._accept_versions(storage, segment.archive_versions)
        started = time.perf_counter()
        if not segment.content_committed:
            if segment.step is None:
                segment.trailing_index = len(manifest.trailing)
            extra, versions = self._encode_archive(
                segment.archive,
                versions_table=storage.run_versions,
                step=segment.step,
                trailing=segment.trailing_index,
            )
            segment.archive_versions = versions
            calls = []
            metrics = []
            for call in segment.calls:
                if storage.plan.trajectory:
                    calls.extend(encode_llm_calls(call.step, [call], first_call=call.call))
                elif storage.plan.metrics or storage.plan.errors:
                    metrics.append(encode_call_metrics(call.step, call.call, call))
            try:
                if segment.step is None:
                    wal.commit_trailing(
                        llm_calls=calls,
                        call_metrics=metrics,
                        extra_files=extra,
                        segment_id=segment.segment_id,
                    )
                else:
                    assert segment.record is not None
                    wal.commit_step(
                        segment.step,
                        segment.record,
                        fragments=segment.fragments,
                        llm_calls=calls,
                        call_metrics=metrics,
                        extra_files=extra,
                    )
            finally:
                current = wal.manifest
                installed = current is not None and (
                    any(entry.step == segment.step for entry in current.steps)
                    if segment.step is not None
                    else any(entry.segment_id == segment.segment_id for entry in current.trailing)
                )
                if not stop_writes.is_set():
                    if installed:
                        segment.content_committed = True
                        self._accept_versions(storage, versions)
                    elif versions:
                        storage.unconfirmed_archive.append(
                            (segment.step, segment.segment_id, versions)
                        )
                segment.work_ms += (time.perf_counter() - started) * 1000
        if stop_writes.is_set():
            return
        if storage.plan.metrics and segment.step is not None and not segment.timing_committed:
            from pydantic_core import to_json

            current = wal.manifest
            assert current is not None
            if segment.timing_index is None:
                segment.timing_index = len(current.trailing)
            measurement = TraceFile(
                f"recording/step_{segment.step:03d}.json",
                to_json(
                    {"step": segment.step, "recording_ms": segment.capture_ms + segment.work_ms}
                ),
                meta={"kind": "recording_timing", "step": segment.step},
            )
            try:
                wal.commit_trailing(
                    extra_files=[measurement], segment_id=f"{segment.segment_id}:timing"
                )
            finally:
                current = wal.manifest
                segment.timing_committed = current is not None and any(
                    entry.segment_id == f"{segment.segment_id}:timing" for entry in current.trailing
                )

    @staticmethod
    def _accept_versions(storage: RecordingStorage, versions: _RunVersions) -> None:
        for owner, version in versions.items():
            current = storage.run_versions.get(owner)
            if current is None or current[2] < version[2]:
                storage.run_versions[owner] = version

    async def _offload(self, operation: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """A single awaited daemon job, never the event loop's default executor.

        The recorder worker serializes jobs. A system call cannot be cancelled;
        closeout stops subsequent writes and reports incomplete coverage. Daemon
        ownership prevents asyncio.run() from adding an unbounded executor join.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()

        def settle(value: Any, error: BaseException | None) -> None:
            if not future.done():
                if error is None:
                    future.set_result(value)
                else:
                    future.set_exception(error)

        def work() -> None:
            try:
                value, error = operation(*args, **kwargs), None
            except BaseException as exc:
                value, error = None, exc
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(settle, value, error)

        threading.Thread(target=work, name=f"recording-io:{self._run_id}", daemon=True).start()
        return await future

    async def _persist(self) -> None:
        storage = self._storage
        assert storage is not None
        while not self._closing or not self._queue.empty():
            try:
                segment = await asyncio.wait_for(self._queue.get(), timeout=0.05)
            except TimeoutError:
                continue
            try:
                await self._offload(self._commit, segment, storage)
                initialized = storage.wal is not None and storage.wal.manifest is not None
                for problem in self._health.problems:
                    if problem.phase == "initialize" and initialized:
                        problem.recovered = True
                    if problem.segment_id == segment.segment_id and problem.phase == "commit":
                        problem.recovered = True
            except Exception as exc:
                self._problem(
                    segment.step,
                    "segment",
                    "commit",
                    exc,
                    segment_id=segment.segment_id,
                    call_ids=[call.call_id for call in segment.calls],
                )
                if segment.attempts < self.write_retries:
                    segment.attempts += 1
                    await asyncio.sleep(self.retry_delay)
                    self._enqueue(segment)
            finally:
                self._queue.task_done()
        await self._finalize()

    def _refresh_health(self) -> None:
        manifest = self.wal.manifest if self.wal is not None else None
        saved = {entry.step for entry in manifest.steps} if manifest is not None else set()
        self._health.committed_steps = len(saved)
        self._health.missing_steps = sorted(set(range(self._health.expected_steps)) - saved)
        self._health.complete_through = recorded_prefix(saved, self._health.problems)
        unresolved = [problem for problem in self._health.problems if not problem.recovered]
        self._health.status = (
            "incomplete" if self._health.missing_steps or unresolved else "complete"
        )

    def _enrich_result(self) -> None:
        if self._result is None:
            return
        if self.recording.metrics:
            self._result.metrics.usage = self._usage.model_copy(deep=True)
            self._result.metrics.llm_calls = self._coverage.calls
            self._result.metrics.usage_coverage = self._coverage.model_copy(deep=True)
        self._result.metrics.recording_health = self._health.model_copy(deep=True)

    async def _finalize(self) -> None:
        if self._stop_writes.is_set():
            return
        self._refresh_health()
        self._enrich_result()
        wal = self.wal
        if wal is None or wal.manifest is None:
            self._health.status = "incomplete"
            self._enrich_result()
            return
        try:
            if self._result is None:
                raise RuntimeError("execution closed without a result")
            persisted_result = self._result
            if not self.recording.trajectory:
                persisted_result = self._result.model_copy(
                    update={
                        "action_history": [],
                        "screenshots": [],
                        "system_prompt": None,
                        "agent_metadata": {},
                        "config": {},
                    }
                )
            if not self.recording.errors:
                persisted_result = persisted_result.model_copy(update={"error": None})
            await self._offload(wal.write_result, persisted_result)
        except Exception as exc:
            self._problem(None, "result", "closeout", exc)
        self._refresh_health()
        reason = "; ".join(
            f"{problem.channel}: {problem.error}: {problem.detail}"
            for problem in self._health.problems
            if not problem.recovered
        )
        try:
            await self._offload(
                wal.finalize,
                TraceStatus(self._health.status),
                incomplete_reason=reason or None,
                expected_steps=self._health.expected_steps,
                recording_problems=self._health.problems,
            )
        except Exception as exc:
            self._problem(None, "manifest", "closeout", exc)
            self._health.status = "incomplete"
        self._enrich_result()

    async def flush(self, timeout: float | None = None) -> None:
        """Wait boundedly for sealed segments, without ending this recording.

        Intended for explicit inspection/checkpoint callers, never required by
        a Loop's step handoff. Failures remain recording-health facts.
        """
        budget = self.close_timeout if timeout is None else timeout
        if budget <= 0:
            raise ValueError("recording flush timeout must be positive")
        try:
            await asyncio.wait_for(self._queue.join(), timeout=budget)
        except TimeoutError:
            self._problem(
                None,
                "worker",
                "flush",
                TimeoutError(f"the sealed segments were not persisted within {budget:g} s"),
            )
        self._refresh_health()

    async def on_run_end(self, *, result: Any = None, env: Any = None, **kwargs: Any) -> None:
        """Close the recording: owed calls, the last archive collection, then the drain.

        The two waits run on separate clocks. The collection gets
        ``closeout_collection_timeout``; an overrun or a failure is an archive
        problem of the close-out and nothing more. The worker then
        gets the whole ``close_timeout`` to commit what is queued, write
        ``result.json`` and close the manifest, so a hung environment can
        leave the trace ``incomplete`` but never ``writing``. Only a worker
        that itself overruns its budget (a write stuck in the kernel) stops
        all further writes and leaves the manifest as it is: a write that
        might still land must not be followed by a finalisation.
        """
        if self._closed:
            return
        self._closed = True
        self._result = result
        cancellation: BaseException | None = None
        calls, self._pending_calls = tuple(self._pending_calls), []
        if calls:
            self._enqueue(RecordingSegment(None, None, [], calls, None))
        if self.archive:
            budget = self.closeout_collection_timeout
            clock = asyncio.timeout(budget)
            try:
                async with clock:
                    batch = await self._collect()
            except TimeoutError as exc:
                if not clock.expired():
                    self._problem(None, "archive", "closeout", exc)
                else:
                    self._problem(
                        None,
                        "archive",
                        "closeout",
                        TimeoutError(
                            f"the close-out archive collection did not finish within "
                            f"{budget:g} s (closeout_collection_timeout)"
                        ),
                    )
            except Exception as exc:
                self._problem(None, "archive", "closeout", exc)
            except BaseException as exc:
                cancellation = exc
                self._problem(None, "archive", "closeout", RuntimeError("closeout cancelled"))
            else:
                if batch.files:
                    self._enqueue(RecordingSegment(None, None, [], (), batch))
        self._closing = True
        self._env = None
        if self._worker is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._worker), timeout=self.close_timeout)
            except TimeoutError:
                self._problem(
                    None,
                    "worker",
                    "closeout",
                    TimeoutError(
                        f"the recording worker did not drain within {self.close_timeout:g} s "
                        f"(close_timeout); later writes were stopped and the manifest was left "
                        f"as it was"
                    ),
                )
                self._stop_writes.set()
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
                self._refresh_health()
                self._enrich_result()
            except asyncio.CancelledError as exc:
                cancellation = cancellation or exc
                self._stop_writes.set()
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
                self._problem(None, "worker", "closeout", RuntimeError("closeout cancelled"))
                self._refresh_health()
                self._enrich_result()
            except Exception as exc:
                self._problem(None, "worker", "closeout", exc)
                self._refresh_health()
                self._enrich_result()
        else:
            self._refresh_health()
            self._enrich_result()
        if cancellation is not None:
            raise cancellation

    async def on_run_error(self, *, error: BaseException | None = None, **kwargs: Any) -> None:
        if not self._closed:
            await self.on_run_end()

    def _refuse_an_unbound_archive(self, negotiation: Any, env: Any) -> None:
        """Refuse to record an archive ``AgentRuntime.prepare`` did not negotiate and bind."""
        wanted = sorted(a.archiver_id for a in self.archive)
        if negotiation is None:
            problem = "no archive negotiation was passed (archive_negotiation=None)"
        elif set(negotiation.active_archivers) != set(wanted):
            problem = (
                f"the archive negotiation bound {sorted(negotiation.active_archivers)}, "
                f"not this hook's archive"
            )
        elif env is None:
            problem = "no environment was passed to collect from (env=None)"
        else:
            return
        raise RuntimeError(
            f"TraceWriter cannot record the archive {wanted}: {problem}. Archivers are "
            f"negotiated and bound by AgentRuntime.prepare() before on_run_start; nothing "
            f"else may stand in for that."
        )

    def _checked(self, batch: Any) -> tuple[ArchiveFile, ...]:
        """A collected batch's files — or ``ArchiveContractError`` before any is written.

        The checks are what only this hook can know — which archivers the
        run bound — plus everything that would make the commit fail no
        matter how often it were retried. Step evidence must come from a
        bound archiver, content-addressed bodies included; a run record from
        a capability that holds one, under its bare id or a bound key, at
        most one per capability; ``archiver_id`` and ``name`` must each be
        one path segment a filesystem accepts (``segment_problem``: at most
        255 bytes of UTF-8) and every string the index records must encode
        as UTF-8; no two step files may land on one path as a case- and
        normalization-insensitive filesystem sees it (``fold_path`` —
        macOS's is); every path must pass the WAL's own rule.

        ``ArchiveFile`` refuses most of this at construction; it is checked
        again here because a frozen dataclass can be forged around its own
        validation, and because a batch refused here is one that never
        becomes owed: a commit that failed for a reason like these would
        fail again, the same way, at every retry.
        """
        if not isinstance(batch, ArchiveBatch):
            raise ArchiveContractError(
                f"collect_archive() returned {type(batch).__name__}, not an ArchiveBatch"
            )
        bound = self._archive_keys
        problems: list[str] = []
        landing: dict[str, str] = {}
        run_records: dict[str, int] = {}
        for item in batch.files:
            label = f"{item.archiver_id}/{item.name}"
            unwritable = [
                f"{label!r}: {what} {problem}"
                for what, problem in (
                    ("archiver_id", segment_problem(item.archiver_id)),
                    ("name", segment_problem(item.name)),
                    ("media_type", text_problem(item.media_type)),
                    *(
                        (f"id {key!r}", text_problem(key) or text_problem(value))
                        for key, value in item.meta.ids.items()
                    ),
                )
                if problem is not None
            ]
            if unwritable:
                problems += unwritable
                continue
            if item.scope == "run":
                owner = item.capability_id
                run_records[owner] = run_records.get(owner, 0) + 1
                if owner not in self._archive_owners:
                    problems.append(
                        f"run record {label!r} belongs to capability {owner!r}, which holds "
                        f"none of this run's archivers {sorted(bound)}"
                    )
                elif item.archiver_id != owner and item.archiver_id not in bound:
                    problems.append(
                        f"run record {label!r} names {item.archiver_id!r}, which this run "
                        f"did not bind"
                    )
                path = archive_run_path(owner, 0)
            else:
                if item.archiver_id not in bound:
                    problems.append(
                        f"{label!r} comes from archiver {item.archiver_id!r}, which this run "
                        f"did not bind (bound: {sorted(bound)})"
                    )
                if item.content_addressed:
                    # Its path is its data's digest: always valid, one per content.
                    continue
                # Step 0 stands in for the commit that takes it: the number
                # changes neither validity nor collisions.
                path = archive_step_path(0, item.archiver_id, item.name)
                folded = fold_path(path)
                if folded in landing:
                    problems.append(
                        f"{label!r} and {landing[folded]!r} land on the same path "
                        f"(case- and normalization-insensitively)"
                    )
                landing.setdefault(folded, label)
            try:
                check_relative_path(path)
            except ValueError as exc:
                problems.append(f"{label!r}: {exc}")
        problems += [
            f"capability {owner!r} handed over {count} run records in one batch; a batch "
            f"carries at most one per capability"
            for owner, count in sorted(run_records.items())
            if count > 1
        ]
        if problems:
            raise ArchiveContractError(
                "the collected batch breaks the archiving contract, and none of it was "
                "written: " + "; ".join(sorted(problems))
            )
        return batch.files

    def _encode_archive(
        self,
        batch: _Collected | None,
        *,
        versions_table: _RunVersions,
        step: int | None = None,
        trailing: int | None = None,
    ) -> tuple[list[TraceFile], _RunVersions]:
        """The extra files for *batch*, and the run-record versions they leave behind.

        Run records keep capture order, whatever order the commits come in.
        One collected before the latest committed record of its capability
        is dropped — an owed batch committed at close-out, after a later
        collection's record already landed: that fingerprint is stale, and
        as the next version it would read back as the latest. One whose
        bytes equal that latest record is dropped too — re-emitting an
        unchanged fingerprint adds nothing — but its collection still counts
        as the latest capture. Any other is committed as the next version
        (the first is 0), because a committed path is immutable. The
        returned table takes effect only once the commit carrying these
        files succeeded, so the versions are recomputed for every commit,
        each trailing one included.
        """
        versions = dict(versions_table)
        extra: list[TraceFile] = []
        if batch is None:
            return extra, versions
        for item in batch.files:
            if item.scope != "run":
                extra.append(encode_archive_file(item, step=step, trailing=trailing))
                continue
            digest = sha256_hex(item.data)
            latest = versions.get(item.capability_id)
            if latest is not None and latest[2] > batch.number:
                logger.warning(
                    "TraceWriter: not committing %r from collection %d: collection %d's run "
                    "record of capability %r is newer and already committed",
                    item.name,
                    batch.number,
                    latest[2],
                    item.capability_id,
                )
                continue
            if latest is not None and latest[1] == digest:
                versions[item.capability_id] = (latest[0], digest, batch.number)
                continue
            version = 0 if latest is None else latest[0] + 1
            versions[item.capability_id] = (version, digest, batch.number)
            extra.append(encode_archive_file(item, run_version=version))
        return extra, versions


def _task_setup_record(context: Any, *, record_secrets: bool) -> TaskSetupRecord | None:
    """The owners' recordable setup projection: snapshotted, then redacted.

    The record must not be what fails a run: ``encoding.snapshot`` never
    raises and turns anything into plain JSON (stringifying keys and
    whatever JSON cannot hold), and a snapshot that is not a JSON object is
    kept under ``"value"``, so the rules below only ever walk JSON.

    The shared value-level rules
    (:func:`~spectragent.core.archive.privacy.redact_value`) replace
    credential-shaped keys and URL credentials, and the ledger records each
    replacement. ``record_secrets`` disables that exactly as it disables
    config redaction: the context is recorded verbatim with an empty
    ledger, and ``TraceConfig.secrets_recorded`` says so.
    """
    if context is None:
        return None
    taken = snapshot(context)
    document = taken if isinstance(taken, dict) else {"value": taken}
    if record_secrets:
        return TaskSetupRecord(context=document, redactions=RedactionLedger())
    redacted, entries = redact_value(document)
    return TaskSetupRecord(context=redacted, redactions=RedactionLedger(entries=entries))
