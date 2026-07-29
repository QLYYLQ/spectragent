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
        """One step record by index, or ``None`` if it was never written."""
        step_path = self._trace_dir / f"steps/{idx:03d}.json"
        if not step_path.exists():
            return None
        return _load_step(step_path)

    def get_observation(self, step: int, fragment_key: str) -> Any | None:
        """Load a single observation fragment for a given step.

        Tries .json first, then .png (binary).

        Untyped, unlike its neighbours: fragment files are named by the
        *negotiated field name* (``000_dom.json``), and the map back to
        the registry key (``browser.dom``) that would identify the class
        is not persisted anywhere. Typing this needs that map written
        into the manifest first.
        """
        json_path = self._trace_dir / f"observations/{step:03d}_{fragment_key}.json"
        if json_path.exists():
            return json.loads(json_path.read_text("utf-8"))

        png_path = self._trace_dir / f"observations/{step:03d}_{fragment_key}.png"
        if png_path.exists():
            return png_path.read_bytes()

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
        """All screenshot file paths — globbed on first access."""
        obs_dir = self._trace_dir / "observations"
        if not obs_dir.exists():
            return []
        return sorted(obs_dir.glob("*_screenshot.png"))


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
