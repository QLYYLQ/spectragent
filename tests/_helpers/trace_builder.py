"""Build format-2 traces through the real encoder and the real ``WALWriter``.

Every reader test needs a trace whose bytes are what production writes —
a hand-written sidecar is how fixtures drifted from the writer before. So
nothing here spells a path or a JSON shape: fragments go through
``encode_fragment``, calls through ``PendingLLMCall`` + ``encode_llm_call``,
and the commit through ``WALWriter.commit_step`` / ``commit_trailing``.

:func:`build_reference_trace` writes one directory that exercises every
accessor: a DOM page, screenshots in two encodings, a fragment whose capture
failed, a step with a refused structured reply then a completed one, a
shared output schema, per-step and content-addressed extra files, and the
trailing calls and files of a step that never committed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.llm.exceptions import StructuredOutputValidationError
from spectragent.core.llm.messages import SystemMessage, UserMessage
from spectragent.core.llm.views import ChatInvokeCompletion, ChatInvokeUsage
from spectragent.core.trace.encoding import (
    EncodedFragment,
    EncodedLLMCall,
    PendingLLMCall,
    TraceFile,
    encode_fragment,
    encode_llm_call,
    encode_output_format,
    sha256_hex,
)
from spectragent.core.trace.models import RunStatus, TraceConfig
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.agent_result import AgentMetrics, AgentResult
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.observation.fragments import TaskObs
from spectragent.schemas.step_record import StepRecord, TokenUsage
from tests._helpers.fragment_samples import JPEG_BYTES, PNG_BYTES, SAMPLES


class Reply(BaseModel):
    """A reply model, so calls carry a requested output schema."""

    thought: str
    action: str


def usage(prompt: int = 10, completion: int = 5) -> ChatInvokeUsage:
    return ChatInvokeUsage(
        prompt_tokens=prompt,
        prompt_cached_tokens=0,
        prompt_cache_creation_tokens=None,
        prompt_image_tokens=None,
        completion_tokens=completion,
        total_tokens=prompt + completion,
    )


def prompt(text: str) -> list[Any]:
    return [SystemMessage(content="You operate a browser."), UserMessage(content=text)]


def completed_call(
    step: int,
    call: int,
    text: str,
    *,
    reply: Any = "done()",
    output_format: type[BaseModel] | None = None,
) -> EncodedLLMCall:
    pending = PendingLLMCall(
        prompt(text), encode_output_format(output_format) if output_format else None
    )
    pending.complete(ChatInvokeCompletion(completion=reply, usage=usage(), stop_reason="end_turn"))
    return encode_llm_call(step, call, pending)


def failed_call(
    step: int, call: int, text: str, exc: Exception, *, output_format: type[BaseModel] | None = None
) -> EncodedLLMCall:
    pending = PendingLLMCall(
        prompt(text), encode_output_format(output_format) if output_format else None
    )
    pending.fail(exc)
    return encode_llm_call(step, call, pending)


def cancelled_call(step: int, call: int, text: str) -> EncodedLLMCall:
    pending = PendingLLMCall(prompt(text))
    pending.cancel(asyncio.CancelledError())
    return encode_llm_call(step, call, pending)


def screenshot(step: int, data: bytes = PNG_BYTES) -> EncodedFragment:
    encoded = encode_fragment(step, ScreenshotObs(image_bytes=data, viewport_width=1280))
    assert encoded is not None
    return encoded


def fragment(step: int, value: Any) -> EncodedFragment:
    encoded = encode_fragment(step, value)
    assert encoded is not None
    return encoded


def failure(step: int, key: str, error: str = "TimeoutError") -> EncodedFragment:
    return fragment(
        step, FragmentFailure(fragment_id=key, capability="browser", error=error, detail="slow")
    )


def extra(path: str, data: bytes, **meta: Any) -> TraceFile:
    return TraceFile(path, data, meta=meta)


def blob(data: bytes, prefix: str = "env/blobs/sha256") -> TraceFile:
    """A content-addressed file, sharded ``<prefix>/ab/cdef…`` the way M28 lays them out."""
    digest = sha256_hex(data)
    return TraceFile(f"{prefix}/{digest[:2]}/{digest[2:]}", data, content_addressed=True)


def step_record(step: int, llm_output: str = "click(btn)") -> StepRecord:
    return StepRecord(
        step=step,
        observation_summary=f"page {step}",
        llm_output=llm_output,
        actions=[Action(name="browser.click", params={"index": step})],
        action_results=[ActionResult(success=True, extracted_content="ok")],
        timestamp=100.0 + step,
        token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


def agent_result(**overrides: Any) -> AgentResult:
    fields: dict[str, Any] = {
        "task_id": "task-001",
        "task": "find the answer",
        "timestamp": datetime.now(UTC),
        "env_status": EnvironmentStatus.SUCCESS,
        "agent_done": AgentDoneStatus.DONE,
        "agent_reported_success": True,
        "answer": "42",
        "model_id": "gpt-4o-mini",
        "metrics": AgentMetrics(end_to_end_ms=1234, steps=2, llm_calls=3),
    }
    fields.update(overrides)
    return AgentResult(**fields)


def new_wal(base: Path, run_id: str = "run-test") -> WALWriter:
    wal = WALWriter(base, run_id)
    wal.init_run(TraceConfig(agent_id="test_agent", loop_id="react", task="find the answer"))
    return wal


@dataclass
class ReferenceTrace:
    """What :func:`build_reference_trace` committed, for assertions."""

    wal: WALWriter
    #: The schema sha256 every structured call of the run references.
    schema_sha: str
    #: The content-addressed blob both steps reference.
    shared_blob: TraceFile
    #: Extra files by step (``-1`` for the trailing commit).
    extras: dict[int, list[TraceFile]] = field(default_factory=dict)

    @property
    def run_dir(self) -> Path:
        return self.wal.run_dir


def build_reference_trace(base: Path, run_id: str = "run-test") -> ReferenceTrace:
    wal = new_wal(base, run_id)
    shared = blob(b'{"status": 200, "body": "shared response"}')

    step0_extra = [
        extra("env/steps/000/browser.network/response.json", b'{"url": "https://a"}', kind="net"),
        shared,
    ]
    wal.commit_step(
        0,
        step_record(0),
        fragments=[
            fragment(0, SAMPLES["browser.dom"]["page"]()),
            screenshot(0, PNG_BYTES),
            failure(0, "browser.browser_meta"),
            fragment(0, TaskObs(user_request="find the answer")),
        ],
        llm_calls=[
            failed_call(
                0,
                0,
                "what do you see?",
                StructuredOutputValidationError(
                    "schema mismatch", raw_output='{"thought": 1}', usage=usage()
                ),
                output_format=Reply,
            ),
            completed_call(
                0,
                1,
                "what do you see? (again)",
                reply=Reply(thought="a button", action="click"),
                output_format=Reply,
            ),
        ],
        extra_files=step0_extra,
    )

    step1_extra = [shared, extra("env/steps/001/x.y/name", b"step one evidence")]
    wal.commit_step(
        1,
        step_record(1, "done()"),
        fragments=[screenshot(1, JPEG_BYTES), fragment(1, TaskObs(user_request="find it"))],
        llm_calls=[
            completed_call(
                1, 0, "and now?", reply=Reply(thought="done", action="done"), output_format=Reply
            )
        ],
        extra_files=step1_extra,
    )

    trailing_extra = [extra("env/final/summary.txt", b"run ended mid-step")]
    wal.commit_trailing(
        llm_calls=[cancelled_call(2, 0, "step two, never answered")],
        extra_files=trailing_extra,
    )
    wal.write_result(
        agent_result(
            agent_done=AgentDoneStatus.TIMEOUT,
            agent_reported_success=None,
            error="Execution timed out after 1s",
        )
    )
    wal.finalize(RunStatus.ERROR)

    return ReferenceTrace(
        wal=wal,
        schema_sha=encode_output_format(Reply).ref.sha256,
        shared_blob=shared,
        extras={0: step0_extra, 1: step1_extra, -1: trailing_extra},
    )
