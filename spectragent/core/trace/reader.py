"""RunTrace — lazy-loading reader for WAL-persisted trace directories.

Different consumers need different depths of data:
- exact_match evaluator: only result.json (a few KB)
- llm_judge: full action history + screenshots
- debugging: step-by-step messages

The reader uses cached_property so data is loaded on first access only.

See docs/execution_dag.md M10 for design details.
"""

from __future__ import annotations

import json
from functools import cached_property
from pathlib import Path
from typing import Any

from spectragent.core.trace.models import TraceConfig, TraceManifest


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
    def result(self) -> dict[str, Any] | None:
        """Final result — loaded on first access. None if run didn't complete."""
        result_path = self._trace_dir / "result.json"
        if not result_path.exists():
            return None
        return json.loads(result_path.read_text("utf-8"))

    @cached_property
    def step_records(self) -> list[dict[str, Any]]:
        """All step records — loaded on first access."""
        records = []
        for entry in self._manifest.steps:
            step_path = self._trace_dir / entry.step_file
            if step_path.exists():
                records.append(json.loads(step_path.read_text("utf-8")))
        return records

    def get_step_record(self, step: int) -> dict[str, Any] | None:
        """Load a single step record by step index."""
        step_path = self._trace_dir / f"steps/{step:03d}.json"
        if not step_path.exists():
            return None
        return json.loads(step_path.read_text("utf-8"))

    def get_observation(self, step: int, fragment_key: str) -> Any | None:
        """Load a single observation fragment for a given step.

        Tries .json first, then .png (binary).
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
