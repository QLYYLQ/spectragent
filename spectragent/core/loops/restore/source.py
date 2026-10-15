"""The source run of a checkpoint, read typed and only as far as it was committed.

``SourceRun`` is the one door through which branching reads a recorded run
(``docs/architecture_design.md`` §10 "分叉相关的 import 方向"): ``BranchLoop``
checks and replays from it, and the restorer reads the recorded observations
and actions from it. It reads through :class:`RunTrace` — the manifest decides
what exists, so an orphan step file is invisible — and turns every way a
checkpoint cannot be read into an :class:`IncompatibleCheckpointError` naming
the source.

The import direction is one way: ``core/loops`` reads ``core/trace``;
``core/trace`` never imports ``core/loops``.

Shared observation and message-context reconstruction belongs to
``core.trace.reconstruction``; this wrapper adds checkpoint-specific errors.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from spectragent.core.loops.validation import IncompatibleCheckpointError
from spectragent.core.trace import reconstruction
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import (
    RecordedLLMCall,
    TraceConfig,
    TraceManifest,
)
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.observation.fragments import StepInfoObs
from spectragent.schemas.restoration import Checkpoint
from spectragent.schemas.step_record import StepRecord

T = TypeVar("T")


class SourceRun:
    """Typed, read-only access to a checkpoint's source run.

    Built with :meth:`open`. Every accessor reads committed data only, and
    raises :class:`IncompatibleCheckpointError` — never a bare
    ``TraceSchemaError`` — when what it needs is missing or unreadable.
    """

    def __init__(self, trace: RunTrace, checkpoint: Checkpoint) -> None:
        self._trace = trace
        self._checkpoint = checkpoint

    @classmethod
    def open(cls, checkpoint: Checkpoint) -> SourceRun:
        """Open the run directory *checkpoint* names.

        Raises:
            IncompatibleCheckpointError: There is no readable trace there.
        """
        path = Path(checkpoint.trace_dir)
        try:
            trace = RunTrace(path)
            trace.require_recording("trajectory", "observations", consumer="branch restoration")
        except (TraceSchemaError, OSError) as exc:
            raise IncompatibleCheckpointError(
                str(path), [f"no readable trace at {path}: {type(exc).__name__}: {exc}"]
            ) from exc
        return cls(trace, checkpoint)

    # ── identity ─────────────────────────────────────────────

    @property
    def checkpoint(self) -> Checkpoint:
        return self._checkpoint

    @property
    def trace_dir(self) -> Path:
        return self._trace.trace_dir

    @property
    def run_id(self) -> str:
        return self._trace.run_id

    @property
    def name(self) -> str:
        """How errors name the source: its run id and directory."""
        return f"run {self._trace.run_id!r} ({self._trace.trace_dir})"

    @property
    def manifest(self) -> TraceManifest:
        return self._trace.manifest

    @property
    def config(self) -> TraceConfig:
        return self._read(lambda: self._trace.config, "config.json")

    # ── committed steps ──────────────────────────────────────

    def committed_steps(self) -> frozenset[int]:
        """The step numbers the manifest lists as committed."""
        return frozenset(entry.step for entry in self._trace.manifest.steps)

    def record(self, step: int) -> StepRecord:
        """The committed record of *step*.

        Raises:
            IncompatibleCheckpointError: The step never committed, or its file
                is unreadable.
        """
        if (
            self._trace.manifest.complete_through is None
            or step > self._trace.manifest.complete_through
        ):
            raise IncompatibleCheckpointError(
                self.name, [f"step {step} is beyond the complete source prefix"]
            )
        record = self._read(lambda: self._trace.step(step), f"step {step}")
        if record is None:
            raise IncompatibleCheckpointError(self.name, [f"step {step} is not committed"])
        return record

    def calls(self, step: int) -> list[RecordedLLMCall]:
        """Recorded Agent decisions and parse retries, excluding helper calls."""
        return self._read(
            lambda: [
                call for call in self._trace.llm_messages(step) if call.purpose == "agent_decision"
            ],
            f"the decision calls of step {step}",
        )

    def observation(self, step: int, negotiated: Any) -> Any:
        """The observation *step* recorded, as this run's ``NegotiatedObservation``.

        Every fragment the negotiation activates — env-source and loop-source
        alike — is read back typed (:func:`recorded_observation`); a capture
        that failed comes back as its ``FragmentFailure``. A loop-source
        fragment the step did not record is ``None``.

        Raises:
            IncompatibleCheckpointError: An env-source fragment the
                negotiation activates was not recorded at *step*, or a file
                does not read back.
        """
        observation, missing = self._read(
            lambda: reconstruction.recorded_observation(self._trace, step, negotiated),
            f"the observation of step {step}",
        )
        if missing:
            raise IncompatibleCheckpointError(
                self.name,
                [
                    f"step {step} recorded no {', '.join(missing)} — the observation cannot be rebuilt"
                ],
            )
        return observation

    def step_info(self, step: int) -> StepInfoObs | None:
        """The ``loop.step_info`` fragment *step* recorded, or ``None``."""
        return self._read(
            lambda: reconstruction.recorded_step_info(self._trace, step),
            f"fragment {reconstruction.STEP_INFO_KEY!r} of step {step}",
        )

    # ── internals ────────────────────────────────────────────

    def _read(self, read: Callable[[], T], what: str) -> T:
        try:
            return read()
        except TraceSchemaError as exc:
            raise IncompatibleCheckpointError(
                self.name, [f"{what} does not read back: {exc}"]
            ) from exc
