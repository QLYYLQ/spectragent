"""Shared fixtures: synthetic-but-real traces and recording judge stubs.

Traces are written through the real :class:`WALWriter`, not hand-rolled
JSON. If the on-disk format changes under an evaluator, these tests break
— which is the point. Nothing here touches a network, a browser or an
LLM provider.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.trace.models import RunStatus, TraceConfig
from spectragent.core.trace.reader import RunTrace
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.agent_result import AgentMetrics, AgentResult, AgentUsage
from spectragent.schemas.step_record import StepRecord, TokenUsage


def png_bytes(seed: tuple[int, int, int] = (12, 34, 56), size: int = 8) -> bytes:
    """A real, *textured* PNG — i.e. one a screenshot filter keeps.

    Deliberately not a flat fill: ``clean_screenshots`` drops frames whose
    greyscale standard deviation is below 5, which a solid colour always
    is. A flat fixture would make every screenshot test pass for the wrong
    reason. ``seed`` shifts the pattern so two frames differ.
    """
    from PIL import Image

    image = Image.new("RGB", (size, size))
    pixels = image.load()
    assert pixels is not None
    for y in range(size):
        for x in range(size):
            pixels[x, y] = (
                (seed[0] + x * 31) % 256,
                (seed[1] + y * 53) % 256,
                (seed[2] + (x * y) * 17) % 256,
            )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def blank_png_bytes(size: int = 8) -> bytes:
    """A uniform frame — the kind the blank filter exists to drop."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (size, size), (255, 255, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def write_trace(
    base_dir: Path,
    *,
    run_id: str = "run-0",
    task_id: str = "task-0",
    benchmark: str = "exact_match_bench",
    subset: str = "All",
    task: str = "Find the answer",
    answer: str = "42",
    agent_done: AgentDoneStatus = AgentDoneStatus.DONE,
    agent_reported_success: bool | None = True,
    steps: int = 2,
    screenshots: list[bytes] | None = None,
    model_id: str = "test-model",
    end_to_end_ms: int = 1234,
    total_tokens: int = 90,
) -> Path:
    """Write one complete trace and return its directory.

    ``screenshots`` are persisted the way ``ScreenshotObs`` does it — a
    real ``NNN_screenshot.png`` plus a JSON sidecar — so
    ``RunTrace.screenshots`` finds them exactly as it would in production.
    """
    wal = WALWriter(base_dir, run_id)
    wal.init_run(
        TraceConfig(
            agent_id="test_agent",
            loop_id="react",
            env_type="browser",
            llm_model=model_id,
            benchmark=benchmark,
            subset=subset,
            task=task,
            task_id=task_id,
        )
    )

    frames = screenshots or []
    for step in range(steps):
        obs_files: list[str] = []
        if step < len(frames):
            data = frames[step]
            obs_files.append(
                wal.write_observation(step, "screenshot", data, suffix=".png")
            )
            sidecar = ScreenshotObs(
                viewport_width=1280, viewport_height=720
            ).model_dump(mode="json", exclude={"image_bytes"})
            sidecar["artifact_file"] = f"{step:03d}_screenshot.png"
            obs_files.append(wal.write_observation(step, "screenshot", sidecar))

        record = StepRecord(
            step=step,
            observation_summary=f"page {step}",
            llm_output=f"thinking about step {step}",
            actions=[Action(name="browser.click", params={"index": step})],
            action_results=[ActionResult(success=True)],
            timestamp=float(step),
            token_usage=TokenUsage(
                prompt_tokens=30, completion_tokens=15, total_tokens=45
            ),
        )
        step_file = wal.write_step_record(step, record)
        wal.commit_step(step=step, step_file=step_file, observation_files=obs_files)

    wal.write_result(
        AgentResult(
            task_id=task_id,
            task=task,
            timestamp=datetime.now(UTC),
            env_status=EnvironmentStatus.SUCCESS,
            agent_done=agent_done,
            agent_reported_success=agent_reported_success,
            answer=answer,
            model_id=model_id,
            action_history=[f"browser.click({{'index': {i}}})" for i in range(steps)],
            metrics=AgentMetrics(
                end_to_end_ms=end_to_end_ms,
                steps=steps,
                llm_calls=steps,
                usage=AgentUsage(
                    total_prompt_tokens=60,
                    total_completion_tokens=30,
                    total_tokens=total_tokens,
                    entry_count=steps,
                ),
            ),
        )
    )
    wal.finalize(RunStatus.COMPLETED)
    return wal.run_dir


@dataclass
class RecordedCall:
    """One judge invocation, as the evaluator issued it."""

    messages: list[Any]

    @property
    def system_text(self) -> str:
        for message in self.messages:
            if getattr(message, "role", "") == "system":
                return message.text
        return ""

    @property
    def user_text(self) -> str:
        for message in self.messages:
            if getattr(message, "role", "") == "user":
                return message.text
        return ""

    @property
    def image_parts(self) -> list[Any]:
        parts: list[Any] = []
        for message in self.messages:
            content = getattr(message, "content", None)
            if isinstance(content, list):
                parts.extend(p for p in content if getattr(p, "type", "") == "image_url")
        return parts


class _Usage:
    prompt_tokens = 100
    completion_tokens = 20
    prompt_cached_tokens = 10
    total_tokens = 120


@dataclass
class _Completion:
    completion: str
    usage: Any = field(default_factory=_Usage)
    thinking: None = None
    tool_calls: list[Any] = field(default_factory=list)


@dataclass
class FakeJudge:
    """Scripted judge model. Records every call it received.

    ``replies`` is consumed in order; the last entry repeats once
    exhausted, so a test only has to script the turns it cares about.
    """

    replies: list[str] = field(default_factory=lambda: ["Status: success"])
    model: str = "fake-judge"
    calls: list[RecordedCall] = field(default_factory=list)

    async def ainvoke(self, messages, output_format=None, **kwargs: Any) -> Any:
        self.calls.append(RecordedCall(messages=list(messages)))
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        return _Completion(completion=self.replies[index])


@pytest.fixture
def traces_dir(tmp_path: Path) -> Path:
    return tmp_path / "traces"


@pytest.fixture
def simple_trace(traces_dir: Path) -> RunTrace:
    return RunTrace(write_trace(traces_dir))
