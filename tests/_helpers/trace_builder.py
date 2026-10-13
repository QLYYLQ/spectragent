"""Build traces through the real encoder and the real ``WALWriter``.

Every reader test needs a trace whose bytes are what production writes —
a hand-written sidecar is how fixtures drifted from the writer before. So
nothing here spells a path or a JSON shape: fragments go through
``encode_fragment``, calls through ``PendingLLMCall`` + ``encode_llm_call``,
and the commit through ``WALWriter.commit_step`` / ``commit_trailing``.

:func:`build_reference_trace` writes one directory that exercises every
accessor: a DOM page, screenshots in two encodings, a fragment whose capture
failed, a step with a refused structured reply then a completed one, a
shared output schema, prompts that send a growing history of screenshots
inline (each image stored once), per-step and content-addressed extra files,
and the trailing calls and files of a step that never committed.

:func:`build_archive_trace` writes one that exercises the environment
evidence accessors: step evidence from several archivers, a
content-addressed body two steps share, a capability's run record in two
versions, trailing evidence, and an ``env/`` extra file that is *not* an
archive file. Archive files go through :func:`archive_file` →
``encode_archive_file``, the function ``TraceWriter`` commits with.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from spectragent.core.archive import ArchiveFile, ArchiveProvenance, ArchiveScope
from spectragent.core.capabilities.browser.observations import ScreenshotObs
from spectragent.core.llm.exceptions import StructuredOutputValidationError
from spectragent.core.llm.messages import (
    ContentPartImageParam,
    ContentPartTextParam,
    ImageURL,
    SystemMessage,
    UserMessage,
)
from spectragent.core.llm.views import ChatInvokeCompletion, ChatInvokeUsage
from spectragent.core.trace.encoding import (
    EncodedFragment,
    EncodedLLMCall,
    PendingLLMCall,
    TraceFile,
    encode_archive_file,
    encode_fragment,
    encode_llm_call,
    encode_output_format,
    prompt_image_text,
    restore_prompt_images,
    sha256_hex,
)
from spectragent.core.trace.fsio import atomic_write
from spectragent.core.trace.models import RecordedLLMCall, TraceConfig, TraceStatus
from spectragent.core.trace.wal import WALWriter
from spectragent.schemas._types import AgentDoneStatus, EnvironmentStatus
from spectragent.schemas.action import Action, ActionResult
from spectragent.schemas.agent_result import AgentMetrics, AgentResult
from spectragent.schemas.eval_result import AgentResultRef, EvalDetails, EvalResult
from spectragent.schemas.observation.failure import FragmentFailure
from spectragent.schemas.observation.fragments import TaskObs
from spectragent.schemas.observation.image import image_media_type
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


def data_url(data: bytes) -> str:
    """*data* as a prompt sends an image inline: a ``data:`` URL in standard base64."""
    media_type = image_media_type(data) or "application/octet-stream"
    return f"data:{media_type};base64,{base64.b64encode(data).decode()}"


def prompt(text: str, images: Sequence[str] = ()) -> list[Any]:
    """A system message, then a user message: *text* and each image URL of *images* after it."""
    system = SystemMessage(content="You operate a browser.")
    if not images:
        return [system, UserMessage(content=text)]
    parts: list[Any] = [ContentPartTextParam(text=text)]
    parts += [ContentPartImageParam(image_url=ImageURL(url=url)) for url in images]
    return [system, UserMessage(content=parts)]


def completed_call(
    step: int,
    call: int,
    text: str,
    *,
    reply: Any = "done()",
    output_format: type[BaseModel] | None = None,
    images: Sequence[str] = (),
) -> EncodedLLMCall:
    pending = PendingLLMCall(
        prompt(text, images), encode_output_format(output_format) if output_format else None
    )
    pending.complete(ChatInvokeCompletion(completion=reply, usage=usage(), stop_reason="end_turn"))
    return encode_llm_call(step, call, pending)


def failed_call(
    step: int,
    call: int,
    text: str,
    exc: Exception,
    *,
    output_format: type[BaseModel] | None = None,
    images: Sequence[str] = (),
) -> EncodedLLMCall:
    pending = PendingLLMCall(
        prompt(text, images), encode_output_format(output_format) if output_format else None
    )
    pending.fail(exc)
    return encode_llm_call(step, call, pending)


def cancelled_call(
    step: int, call: int, text: str, *, images: Sequence[str] = ()
) -> EncodedLLMCall:
    pending = PendingLLMCall(prompt(text, images))
    pending.cancel(asyncio.CancelledError())
    return encode_llm_call(step, call, pending)


def read_back(encoded: EncodedLLMCall) -> RecordedLLMCall:
    """An encoded call record read back the way ``RunTrace`` reads one, without a commit.

    Its stored prompt images come from the encoded call's own files, so a
    test of the encoding alone sees the prompt exactly as ``llm_messages``
    would hand it back.
    """
    record = json.loads(encoded.file.data)
    files = {image.rel_path: image.data for image in encoded.images}
    restore_prompt_images(record, lambda ref: prompt_image_text(ref, files[ref.stored_image]))
    return RecordedLLMCall.model_validate(record)


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


def eval_result(**overrides: Any) -> EvalResult:
    """A verdict with every required field filled; *overrides* replace any of them."""
    now = datetime.now(UTC)
    fields: dict[str, Any] = {
        "task_id": "task-001",
        "evaluator_id": "exact_match",
        "benchmark": "test",
        "run_id": "run-test",
        "timestamp": now,
        "agent_result_ref": AgentResultRef(task_id="task-001", timestamp=now, result_dir="."),
        "predicted_label": 1,
        "evaluation_details": EvalDetails(),
    }
    fields.update(overrides)
    return EvalResult(**fields)


def write_verdict(run_dir: Path, slug: str, verdict: EvalResult) -> Path:
    """Write *verdict* the way ``EvaluationRunner._persist`` does: ``evals/<slug>.json``, atomically."""
    path = run_dir / "evals" / f"{slug}.json"
    payload = json.dumps(verdict.model_dump(mode="json"), ensure_ascii=False, indent=2)
    atomic_write(path, payload.encode("utf-8"))
    return path


def new_wal(base: Path, run_id: str = "run-test") -> WALWriter:
    wal = WALWriter(base, run_id)
    wal.init_run(TraceConfig(agent_id="test_agent", loop_id="react", task="find the answer"))
    return wal


#: An image URL whose base64 is not the standard encoding of what it decodes to
#: (the last character's low bits are set): the trace stores its own text.
NONSTANDARD_IMAGE_URL = "data:image/png;base64,QR=="


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
    #: The stored prompt images, in the order the commits first carried them:
    #: step 0's PNG, step 1's JPEG, the trailing call's text.
    images: list[TraceFile] = field(default_factory=list)

    @property
    def run_dir(self) -> Path:
        return self.wal.run_dir


def build_reference_trace(base: Path, run_id: str = "run-test") -> ReferenceTrace:
    wal = new_wal(base, run_id)
    shared = blob(b'{"status": 200, "body": "shared response"}')
    # The history of screenshots each prompt sends inline grows by one a step.
    step0_images = [data_url(PNG_BYTES)]
    step1_images = [*step0_images, data_url(JPEG_BYTES)]
    trailing_images = [*step1_images, NONSTANDARD_IMAGE_URL]
    calls: dict[int, list[EncodedLLMCall]] = {
        0: [
            failed_call(
                0,
                0,
                "what do you see?",
                StructuredOutputValidationError(
                    "schema mismatch", raw_output='{"thought": 1}', usage=usage()
                ),
                output_format=Reply,
                images=step0_images,
            ),
            completed_call(
                0,
                1,
                "what do you see? (again)",
                reply=Reply(thought="a button", action="click"),
                output_format=Reply,
                images=step0_images,
            ),
        ],
        1: [
            completed_call(
                1,
                0,
                "and now?",
                reply=Reply(thought="done", action="done"),
                output_format=Reply,
                images=step1_images,
            )
        ],
        -1: [cancelled_call(2, 0, "step two, never answered", images=trailing_images)],
    }

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
        llm_calls=calls[0],
        extra_files=step0_extra,
    )

    step1_extra = [shared, extra("env/steps/001/x.y/name", b"step one evidence")]
    wal.commit_step(
        1,
        step_record(1, "done()"),
        fragments=[screenshot(1, JPEG_BYTES), fragment(1, TaskObs(user_request="find it"))],
        llm_calls=calls[1],
        extra_files=step1_extra,
    )

    trailing_extra = [extra("env/final/summary.txt", b"run ended mid-step")]
    wal.commit_trailing(llm_calls=calls[-1], extra_files=trailing_extra)
    wal.write_result(
        agent_result(
            agent_done=AgentDoneStatus.TIMEOUT,
            agent_reported_success=None,
            error="Execution timed out after 1s",
        )
    )
    # The agent timed out; the trace recorded all of it, the cut-off step's
    # call included (trailing) — the two ledgers say different things.
    wal.finalize(TraceStatus.COMPLETE)

    first: dict[str, TraceFile] = {}
    for call in (*calls[0], *calls[1], *calls[-1]):
        for image in call.images:
            first.setdefault(image.rel_path, image)
    return ReferenceTrace(
        wal=wal,
        schema_sha=encode_output_format(Reply).ref.sha256,
        shared_blob=shared,
        extras={0: step0_extra, 1: step1_extra, -1: trailing_extra},
        images=list(first.values()),
    )


# ── environment evidence ──────────────────────────────────────────

#: The fixed capture time every archive fixture carries unless told otherwise.
CAPTURED_AT = 1_760_000_000.0


def archive_item(
    archiver_id: str,
    name: str,
    data: bytes,
    *,
    media_type: str = "application/json",
    content_addressed: bool = False,
    scope: ArchiveScope = "step",
    captured_at: float = CAPTURED_AT,
    cycle: int | None = None,
    ids: dict[str, str] | None = None,
) -> ArchiveFile:
    """An ``ArchiveFile`` as a capability would hand it over."""
    return ArchiveFile(
        archiver_id=archiver_id,
        name=name,
        media_type=media_type,
        data=data,
        meta=ArchiveProvenance(captured_at=captured_at, cycle=cycle, ids=ids or {}),
        content_addressed=content_addressed,
        scope=scope,
    )


def archive_file(
    archiver_id: str,
    name: str,
    data: bytes,
    *,
    step: int | None = None,
    trailing: int | None = None,
    run_version: int | None = None,
    **fields: Any,
) -> TraceFile:
    """An archive file as ``TraceWriter`` commits it.

    Through ``encode_archive_file`` — never a spelled path — so a fixture
    cannot drift from the layout production writes (§5.3).
    """
    item = archive_item(archiver_id, name, data, **fields)
    return encode_archive_file(item, step=step, trailing=trailing, run_version=run_version)


#: The fictional capability :func:`build_archive_trace` records, and its archivers.
ARCHIVE_CAPABILITY = "builder"
STEPS_ARCHIVER = "builder.steps"
BLOBS_ARCHIVER = "builder.blobs"
STORAGE_ARCHIVER = "builder.storage"


@dataclass
class ArchiveTrace:
    """What :func:`build_archive_trace` committed, for assertions."""

    wal: WALWriter
    #: The content-addressed body steps 0 and 1 both reference.
    shared_blob: TraceFile
    #: Run-record versions 0 and 1 of :data:`ARCHIVE_CAPABILITY`.
    run_records: list[TraceFile]
    #: Archive files by the step that committed them (``-1``: the trailing commit).
    evidence: dict[int, list[TraceFile]] = field(default_factory=dict)
    #: An ``env/`` extra file with no archive metadata — not an archive file.
    plain_extra: TraceFile | None = None

    @property
    def run_dir(self) -> Path:
        return self.wal.run_dir


def build_archive_trace(base: Path, run_id: str = "run-archive") -> ArchiveTrace:
    """Three committed steps and one trailing commit, carrying environment evidence.

    - step 0: a step file of ``builder.steps``, the shared body
      (``builder.blobs``, content-addressed), run record version 0, and an
      ``env/`` extra file that is no archive file;
    - step 1: a step file of ``builder.steps`` and one of
      ``builder.storage``, the shared body again (deduplicated);
    - step 2: a step file of ``builder.steps`` and run record version 1;
    - trailing: a step file of ``builder.steps`` and a body of its own, with
      the cancelled call of step 3.
    """
    wal = new_wal(base, run_id)
    shared = archive_file(
        BLOBS_ARCHIVER,
        "response.bin",
        b"HTTP/1.1 200 OK\r\n\r\nshared body",
        step=0,
        media_type="application/octet-stream",
        content_addressed=True,
        cycle=1,
        ids={"request": "r1"},
    )
    run_v0 = archive_file(
        ARCHIVE_CAPABILITY,
        "fingerprint.json",
        b'{"generation": 0}',
        scope="run",
        run_version=0,
    )
    run_v1 = archive_file(
        ARCHIVE_CAPABILITY,
        "fingerprint.json",
        b'{"generation": 1}',
        scope="run",
        run_version=1,
    )
    plain = extra("env/notes/plain.txt", b"not evidence", kind="note")

    evidence: dict[int, list[TraceFile]] = {
        0: [
            archive_file(
                STEPS_ARCHIVER,
                "cycle_001.json",
                b'{"cycle": 1}',
                step=0,
                cycle=1,
                ids={"frame": "main"},
            ),
            shared,
            run_v0,
        ],
        1: [
            archive_file(STEPS_ARCHIVER, "cycle_002.json", b'{"cycle": 2}', step=1, cycle=2),
            archive_file(STORAGE_ARCHIVER, "storage.json", b'{"cookies": 3}', step=1, cycle=2),
            shared,
        ],
        2: [
            archive_file(STEPS_ARCHIVER, "cycle_003.json", b'{"cycle": 3}', step=2, cycle=3),
            run_v1,
        ],
        -1: [
            archive_file(STEPS_ARCHIVER, "tail.json", b'{"tail": true}', trailing=0),
            archive_file(
                BLOBS_ARCHIVER,
                "response.bin",
                b"HTTP/1.1 204 No Content\r\n\r\n",
                trailing=0,
                media_type="application/octet-stream",
                content_addressed=True,
            ),
        ],
    }
    for step in (0, 1, 2):
        files = evidence[step] + ([plain] if step == 0 else [])
        wal.commit_step(
            step,
            step_record(step),
            llm_calls=[completed_call(step, 0, f"step {step}")],
            extra_files=files,
        )
    wal.commit_trailing(llm_calls=[cancelled_call(3, 0, "cut off")], extra_files=evidence[-1])
    wal.write_result(agent_result())
    wal.finalize(TraceStatus.COMPLETE)
    return ArchiveTrace(
        wal=wal,
        shared_blob=shared,
        run_records=[run_v0, run_v1],
        evidence=evidence,
        plain_extra=plain,
    )
