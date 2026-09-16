"""RunTrace — lazy-loading reader for WAL-persisted trace directories.

Different consumers need different depths of data:
- exact_match evaluator: only result.json (a few KB)
- llm_judge: full action history + screenshots
- debugging: step-by-step messages

The reader uses cached_property so data is loaded on first access only.

Everything the writer put on disk as a typed model comes back as that
model: ``result`` is an :class:`AgentResult`, ``step_records`` are
:class:`StepRecord` objects. The reader used to return bare dicts for
those, which meant an evaluator author had to learn the on-disk key
names by opening a JSON file, and a wrong guess (``rec["step_index"]``)
survived every type check and failed at runtime.

See docs/architecture_design.md §5.5 and docs/execution_dag.md M10.
"""

from __future__ import annotations

import json
from functools import cached_property
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from spectragent.core.trace.errors import TraceSchemaError
from spectragent.core.trace.models import TraceConfig, TraceManifest
from spectragent.schemas.agent_result import AgentResult
from spectragent.schemas.step_record import StepRecord

#: Extensions an observation fragment's binary payload is written under,
#: most-likely first. A fragment names its own via ``trace_binary()``
#: (``ScreenshotObs`` takes it from the capture's header, so a JPEG
#: observation lands as ``.jpg``), which leaves the reader needing to
#: recognise the whole set rather than assume PNG.
_BINARY_SUFFIXES: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp", ".gif")


class RunTrace:
    """Lazy-loading reader for a single trace directory.

    Only the manifest is loaded eagerly (a few hundred bytes).
    All other data is loaded on first property access via cached_property.
    """

    def __init__(self, trace_dir: Path) -> None:
        self._trace_dir = trace_dir
        # Manifest is tiny — load eagerly
        manifest_path = trace_dir / "manifest.json"
        raw = json.loads(manifest_path.read_text("utf-8"))
        self._manifest = TraceManifest.model_validate(raw)

    @property
    def trace_dir(self) -> Path:
        return self._trace_dir

    @property
    def manifest(self) -> TraceManifest:
        return self._manifest

    @property
    def run_id(self) -> str:
        return self._manifest.run_id

    @property
    def step_count(self) -> int:
        """Number of completed steps — from manifest, no I/O."""
        return self._manifest.last_step_completed + 1

    @cached_property
    def config(self) -> TraceConfig:
        """Frozen config snapshot — loaded on first access."""
        config_path = self._trace_dir / "config.json"
        raw = json.loads(config_path.read_text("utf-8"))
        return TraceConfig.model_validate(raw)

    @cached_property
    def result(self) -> AgentResult | None:
        """Final result — loaded on first access.

        ``None`` means exactly one thing: ``result.json`` is absent, so
        the run died before anything terminal was recorded. A file that
        exists but does not parse raises :class:`TraceSchemaError`
        instead — "written by an older schema" and "never finished" are
        different facts about a run, and returning ``None`` for both
        hides the first one from every evaluator downstream.
        """
        result_path = self._trace_dir / "result.json"
        if not result_path.exists():
            return None
        raw = json.loads(result_path.read_text("utf-8"))
        try:
            return AgentResult.model_validate(raw)
        except ValidationError as exc:
            raise TraceSchemaError(_result_schema_message(result_path, raw, exc)) from exc

    @cached_property
    def step_records(self) -> list[StepRecord]:
        """Every committed step, in manifest order — loaded on first access."""
        records: list[StepRecord] = []
        for entry in self._manifest.steps:
            step_path = self._trace_dir / entry.step_file
            if step_path.exists():
                records.append(_load_step(step_path))
        return records

    def step(self, idx: int) -> StepRecord | None:
        """One committed step record by index, or ``None``.

        Resolved through the manifest, like :attr:`step_records` and
        :attr:`step_count`, because the manifest is the commit point. The
        WAL writes ``steps/NNN.json`` atomically and *then* renames the
        manifest, so a crash in between leaves a complete, parseable step
        file for a step that never committed — and probing the filename
        would hand it back as a valid record while the two accessors next
        to it correctly refuse to. One reader, one answer to "did this
        step happen".
        """
        for entry in self._manifest.steps:
            if entry.step == idx:
                step_path = self._trace_dir / entry.step_file
                return _load_step(step_path) if step_path.exists() else None
        return None

    def get_observation(self, step: int, fragment_key: str) -> Any | None:
        """Load a single observation fragment for a given step.

        *fragment_key* is the fully-qualified registry key
        (``browser.dom``), which is what the filename carries. It used to
        be the negotiated local name (``dom``) — a per-run artefact of
        which capability won negotiation, so the same file name meant a
        different thing in a browser run and a desktop run and no reader
        could tell.

        Tries .json first, then each binary extension a fragment can be
        written under (:data:`_BINARY_SUFFIXES`) — a screenshot captured
        as JPEG lands as ``.jpg``. Still untyped: the key now identifies
        the fragment class, and the payload repeats it in
        ``fragment_id``, so returning a real ``FragmentBase`` is a
        lookup away — it waits for an evaluator that needs one, since
        that also fixes the return type of a public accessor.
        """
        json_path = self._trace_dir / f"observations/{step:03d}_{fragment_key}.json"
        if json_path.exists():
            return json.loads(json_path.read_text("utf-8"))

        for suffix in _BINARY_SUFFIXES:
            path = self._trace_dir / f"observations/{step:03d}_{fragment_key}{suffix}"
            if path.exists():
                return path.read_bytes()

        return None

    def get_messages(self, step: int) -> tuple[Any | None, Any | None]:
        """Load LLM input and output messages for a step.

        Returns (input_data, output_data). Either may be None if file missing.
        """
        input_path = self._trace_dir / f"messages/{step:03d}_input.json"
        output_path = self._trace_dir / f"messages/{step:03d}_output.json"

        input_data = (
            json.loads(input_path.read_text("utf-8"))
            if input_path.exists()
            else None
        )
        output_data = (
            json.loads(output_path.read_text("utf-8"))
            if output_path.exists()
            else None
        )
        return input_data, output_data

    @cached_property
    def unfinished_message_files(self) -> list[Path]:
        """Messages from a step the Loop never surrendered.

        Non-empty only for a run that died — timeout, crash — after the
        LLM answered and before the step was recorded. Reading them is
        how you see what the agent was doing when the budget ran out.
        """
        return [self._trace_dir / rel for rel in self._manifest.unfinished_message_files]

    @cached_property
    def screenshots(self) -> list[Path]:
        """All screenshot file paths — globbed on first access.

        Matches any capability's ``screenshot`` fragment
        (``000_browser.screenshot.png``, ``000_desktop.screenshot.png``),
        because a vision judge wants the frames whatever produced them.
        Which one it was stays readable off the path.

        Every image extension, not only ``.png``: a run configured for
        JPEG observations (``screenshot_format``) writes ``.jpg``, and a
        judge that silently found no frames would score the run text-only.
        Sorted by name within one extension *and across* them, so the
        list stays in step order for a run whose encoding changed
        mid-flight (a resumed trace, a re-run into the same directory).
        """
        obs_dir = self._trace_dir / "observations"
        if not obs_dir.exists():
            return []
        return sorted(
            path
            for suffix in _BINARY_SUFFIXES
            for path in obs_dir.glob(f"*.screenshot{suffix}")
        )


def _load_step(path: Path) -> StepRecord:
    raw = json.loads(path.read_text("utf-8"))
    try:
        return StepRecord.model_validate(raw)
    except ValidationError as exc:
        raise TraceSchemaError(
            f"{path} does not parse as a StepRecord: {exc}"
        ) from exc


def _current_result_schema() -> str:
    """The one ``AgentResult.schema_version`` this build can read.

    Read off the model rather than restated here, so bumping the schema
    cannot leave a stale version number in an error message.
    """
    return str(AgentResult.model_fields["schema_version"].default)


def _result_schema_message(path: Path, raw: Any, exc: ValidationError) -> str:
    """Say *why* a result.json is unreadable, in that file's own terms.

    The field-level pydantic errors are only useful when the versions
    agree. When they do not, they are actively misleading: a 2.0 trace
    fails on ``agent_success`` being an extra key, which reads as a typo
    rather than as a schema this reader retired.
    """
    current = _current_result_schema()
    found = raw.get("schema_version") if isinstance(raw, dict) else None
    if found is not None and found != current:
        return (
            f"{path} predates AgentResult schema {current}: it declares "
            f"schema_version {found!r}, whose fields this reader cannot map. "
            f"The trace is not readable and re-running the evaluation pass "
            f"will not change that — re-run the task to record a current "
            f"trace, or read the JSON by hand."
        )
    return f"{path} does not parse as an AgentResult (schema {current}): {exc}"
