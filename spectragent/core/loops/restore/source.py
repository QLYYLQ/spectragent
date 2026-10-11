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
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from pydantic import TypeAdapter

from spectragent.core.loops.validation import IncompatibleCheckpointError
from spectragent.core.trace.encoding import snapshot
from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import (
    RecordedLLMCall,
    RecordedMessage,
    TraceConfig,
    TraceManifest,
)
from spectragent.core.trace.reader import RunTrace
from spectragent.schemas.observation._registrar import fragment_field_name
from spectragent.schemas.observation.dynamic_model import build_observation_model_from_result
from spectragent.schemas.observation.fragments import StepInfoObs
from spectragent.schemas.restoration import Checkpoint
from spectragent.schemas.step_record import StepRecord

#: The ``loop.step_info`` key, read to pin a branch's step budget.
STEP_INFO_KEY = "loop.step_info"

T = TypeVar("T")

_MESSAGE: TypeAdapter[Any] = TypeAdapter(RecordedMessage)


def recorded_form(messages: Sequence[Any]) -> list[Any]:
    """*messages* as a trace records them: the form a live context is compared in.

    A live message takes the recorder's own path — its JSON snapshot, read
    back as the recorded message type — so a live context and a recorded
    prompt compare equal exactly when the recorder would have written the
    same thing for both.
    """
    return [
        _MESSAGE.validate_python(snapshot(message)).model_dump(mode="json") for message in messages
    ]


def first_difference(live: Sequence[Any], recorded: Sequence[Any]) -> str:
    """Where two contexts in :func:`recorded_form` part, for an error message."""
    for index, (mine, theirs) in enumerate(zip(live, recorded, strict=False)):
        if mine != theirs:
            return (
                f"message {index} differs ({mine.get('role')!r}: {_excerpt(mine)} vs "
                f"recorded {theirs.get('role')!r}: {_excerpt(theirs)})"
            )
    return f"{len(live)} messages vs {len(recorded)} recorded"


def _excerpt(message: Any) -> str:
    text = str(message.get("content", ""))
    return repr(text[:80] + ("…" if len(text) > 80 else ""))


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
        record = self._read(lambda: self._trace.step(step), f"step {step}")
        if record is None:
            raise IncompatibleCheckpointError(self.name, [f"step {step} is not committed"])
        return record

    def calls(self, step: int) -> list[RecordedLLMCall]:
        """Every recorded LLM call of *step*, in start order."""
        return self._read(lambda: self._trace.llm_messages(step), f"the calls of step {step}")

    def observation(self, step: int, negotiated: Any) -> Any:
        """The observation *step* recorded, as this run's ``NegotiatedObservation``.

        Every fragment the negotiation activates — env-source and loop-source
        alike — is read back typed (``FragmentBase.from_trace``); a capture
        that failed comes back as its ``FragmentFailure``. A loop-source
        fragment the step did not record is ``None``.

        Raises:
            IncompatibleCheckpointError: An env-source fragment the
                negotiation activates was not recorded at *step*, or a file
                does not read back.
        """
        model = build_observation_model_from_result(negotiated)
        env_sourced = frozenset(negotiated.env_sourced)
        values: dict[str, Any] = {}
        missing: list[str] = []
        for key in sorted(negotiated.active_fragments):
            fragment = self._read(
                lambda key=key: self._trace.observation_fragment(step, key),
                f"fragment {key!r} of step {step}",
            )
            if fragment is None and key in env_sourced:
                missing.append(key)
            values[fragment_field_name(key)] = fragment
        if missing:
            raise IncompatibleCheckpointError(
                self.name,
                [
                    f"step {step} recorded no {', '.join(missing)} — the observation cannot be rebuilt"
                ],
            )
        return model(**values)

    def step_info(self, step: int) -> StepInfoObs | None:
        """The ``loop.step_info`` fragment *step* recorded, or ``None``."""
        fragment = self._read(
            lambda: self._trace.observation_fragment(step, STEP_INFO_KEY),
            f"fragment {STEP_INFO_KEY!r} of step {step}",
        )
        return fragment if isinstance(fragment, StepInfoObs) else None

    # ── internals ────────────────────────────────────────────

    def _read(self, read: Callable[[], T], what: str) -> T:
        try:
            return read()
        except TraceSchemaError as exc:
            raise IncompatibleCheckpointError(
                self.name, [f"{what} does not read back: {exc}"]
            ) from exc
