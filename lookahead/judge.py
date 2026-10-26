"""The judge model: a chat model from ``core/llm``, every call kept, refused replies asked again.

The model is composed from a YAML file holding one ``llm`` layer
(:func:`load_judge_model`), the one construction path every model in the
framework takes (``docs/architecture_design.md`` §4.7), and asked through the
public chat Protocol with the reply model as ``output_format``. The provider
sends the schema the way its route accepts it, removes a code fence,
validates the reply and raises ``StructuredOutputValidationError`` when it
does not fit; an HTTP 200 is no proof the schema was honoured. Such a reply
is asked again at most twice, each time telling the model what was wrong.
That re-asking is the judge's own: ``core/llm`` reports, the consumer decides.

Calls that return no reply are retried by the provider's SDK (its
``max_retries``): rate limits, server errors and lost connections. What still
fails after that fails only its request, as does a reply cut at the token
ceiling (``ModelOutputTruncatedError``) or stopped by the server
(``ModelGenerationInterruptedError``): asking again would not change the
budget or the server. The first request of a batch is sent alone, as a
probe: when the route refuses it (a 4xx other than 429), the batch stops
there with a message naming the status, instead of sending every request
into the same refusal.

Every call is recorded — the model, its redacted configuration, latency,
usage, the reply or the refused text, the model's reasoning, and why a call
failed or a reply was refused — in the order the calls finished, and the
totals are reported at the end.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from lookahead.runs import LookaheadInputError
from spectragent.core.composer import ComposeError, ConfigComposer
from spectragent.core.llm.base import BaseChatModel, require_inference_protocol
from spectragent.core.llm.exceptions import (
    ModelGenerationInterruptedError,
    ModelOutputTruncatedError,
    ModelProviderError,
    StructuredOutputValidationError,
)
from spectragent.core.llm.messages import (
    AssistantMessage,
    BaseMessage,
    ContentPartImageParam,
    ContentPartTextParam,
    ImageURL,
    SystemMessage,
    UserMessage,
)
from spectragent.core.llm.views import ModelInvokeUsage
from spectragent.core.registries import collect_registrars
from spectragent.core.trace.redaction import redact_config
from spectragent.schemas.observation.image import image_media_type

#: Re-asks of a request whose reply the validator refused.
MAX_RETRIES = 2
#: The one layer a judge configuration file may hold.
JUDGE_LAYER = "llm"
#: How much of a provider's message a refusal reason keeps when pydantic gives none.
_REASON_CHARS = 300

M = TypeVar("M", bound=BaseModel)
ContentPart = ContentPartTextParam | ContentPartImageParam
CallErrorKind = Literal["validation", "truncated", "interrupted", "call"]


class JudgeRouteError(RuntimeError):
    """The judge model's route refused the probe request: no other request was sent."""


@dataclass(frozen=True)
class JudgeModel:
    """The judge's chat model and the configuration it was composed from."""

    llm: BaseChatModel
    #: The YAML file it was composed from.
    source: Path
    #: The composed ``llm`` entry, redacted (``redact_config``): never a key.
    config: dict[str, JsonValue]


def load_judge_model(path: Path) -> JudgeModel:
    """The chat model a YAML file holding exactly one ``llm`` layer composes.

    Composed by the ordinary ``ConfigComposer`` outside an agent run, as
    ``spectragent eval`` composes its judge (``compose_evaluation``).
    Composing opens no connection: the client is created at the first
    request.

    Raises:
        LookaheadInputError: The file cannot be read, holds another layer or
            more than one model, or does not compose into a chat model.
    """
    where = f"--judge-config {path}"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LookaheadInputError(f"{where} cannot be read: {exc}") from exc
    if not isinstance(raw, dict) or JUDGE_LAYER not in raw:
        raise LookaheadInputError(f"{where} must be a YAML mapping with one {JUDGE_LAYER!r} layer")
    others = sorted(str(layer) for layer in raw if layer != JUDGE_LAYER)
    if others:
        raise LookaheadInputError(
            f"{where} holds layers {others}; the judge configuration holds only {JUDGE_LAYER!r}"
        )
    try:
        composed = ConfigComposer(registrars=collect_registrars()).compose(raw)
        llm = composed.single(JUDGE_LAYER)
        require_inference_protocol(llm, "chat", consumer="the lookahead judge")
    except (ComposeError, ValidationError, TypeError) as exc:
        raise LookaheadInputError(f"{where} does not compose a judge model: {exc}") from exc
    config: dict[str, JsonValue] = redact_config(composed.top_level_cfg)[JUDGE_LAYER][0]
    return JudgeModel(llm=llm, source=path, config=config)


# ── requests ──────────────────────────────────────────────────────────


def image_part(data: bytes) -> ContentPartImageParam:
    """An image as a base64 data URL content part, labelled with its own media type."""
    media_type = image_media_type(data) or "image/png"
    url = f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"
    return ContentPartImageParam(image_url=ImageURL(url=url, media_type=media_type))


def text_part(text: str) -> ContentPartTextParam:
    return ContentPartTextParam(text=text)


@dataclass
class JudgeRequest(Generic[M]):
    """One question to the judge: messages, the reply model, and what it is about."""

    purpose: str
    item: str
    system: str
    content: list[ContentPart]
    reply_model: type[M]
    #: What each image in *content* is (a run and step), recorded instead of the bytes.
    image_sources: list[str] = field(default_factory=list)


class CallRecord(BaseModel):
    """One ``ainvoke`` of the judge model, as ``calls.jsonl`` keeps it."""

    model_config = ConfigDict(extra="forbid")

    call_id: str
    purpose: str
    item: str
    #: 0 for the first ask, then one per re-ask after a refused reply.
    attempt: int
    model: str
    config: dict[str, JsonValue]
    #: SHA-256 of the judge's own messages (system and user, images included).
    prompt_sha256: str
    #: The text of those messages, on the first attempt only.
    prompt_text: str | None
    images: list[str]
    started_at: float
    latency_ms: int = 0
    usage: ModelInvokeUsage | None = None
    stop_reason: str | None = None
    #: The validated reply.
    reply: dict[str, JsonValue] | None = None
    #: The reply as the validator saw it (refused), or the partial text of a cut reply.
    raw_output: str | None = None
    thinking: str | None = None
    error_kind: CallErrorKind | None = None
    error_type: str | None = None
    #: The provider error's status code, on a failed call.
    status: int | None = None
    error: str | None = None
    valid: bool = False


@dataclass
class JudgeOutcome(Generic[M]):
    """The validated reply (``None`` when there is none) and every call's record."""

    value: M | None
    attempts: list[CallRecord]
    #: The route's refusal (a 4xx other than 429) that ended the request, if one did.
    refused: ModelProviderError | None = None

    @property
    def error(self) -> str | None:
        if self.value is not None or not self.attempts:
            return None
        return self.attempts[-1].error or "no valid reply"


@dataclass
class Totals:
    calls: int = 0
    failed_calls: int = 0
    truncated_calls: int = 0
    interrupted_calls: int = 0
    invalid_replies: int = 0
    valid_replies: int = 0
    requests: int = 0
    unanswered_requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    #: Calls whose usage states no reasoning count: not zero, not reported.
    calls_without_reasoning_count: int = 0
    calls_without_usage: int = 0
    latency_ms: int = 0
    wall_s: float = 0.0

    def add(self, record: CallRecord) -> None:
        self.calls += 1
        self.latency_ms += record.latency_ms
        if record.valid:
            self.valid_replies += 1
        elif record.error_kind == "validation":
            self.invalid_replies += 1
        else:
            self.failed_calls += 1
            self.truncated_calls += record.error_kind == "truncated"
            self.interrupted_calls += record.error_kind == "interrupted"
        usage = record.usage
        if usage is None:
            self.calls_without_usage += 1
            return
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        if usage.completion_reasoning_tokens is None:
            self.calls_without_reasoning_count += 1
        else:
            self.reasoning_tokens += usage.completion_reasoning_tokens

    def summary(self) -> str:
        return (
            f"judge: {self.requests} request(s), {self.unanswered_requests} without a valid reply; "
            f"{self.calls} call(s): {self.valid_replies} valid, {self.invalid_replies} refused by "
            f"validation, {self.failed_calls} failed ({self.truncated_calls} cut at the token "
            f"ceiling, {self.interrupted_calls} stopped by the server); tokens: "
            f"{self.prompt_tokens} prompt, {self.completion_tokens} completion "
            f"({self.reasoning_tokens} reasoning, {self.calls_without_reasoning_count} call(s) "
            f"without a reasoning count), {self.calls_without_usage} call(s) without usage; "
            f"summed latency {self.latency_ms / 1000:.1f} s, wall time {self.wall_s:.1f} s"
        )


def is_route_refusal(error: ModelProviderError) -> bool:
    """A 4xx other than 429: the same request would be refused again."""
    return 400 <= error.status_code < 500 and error.status_code != 429


def refusal_reason(raw_output: str, reply_model: type[BaseModel]) -> str | None:
    """Why *raw_output* is not a *reply_model*: pydantic's errors, located and short.

    ``None`` when it validates after all (a provider that normalised the text
    before validating it).
    """
    if not raw_output.strip():
        return "the reply is empty"
    try:
        reply_model.model_validate_json(raw_output)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'reply'}: {error['msg']}"
            for error in exc.errors(include_url=False, include_input=False)[:5]
        )
        return f"it does not match the schema ({problems})"
    return None


def _first_line(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= _REASON_CHARS else line[: _REASON_CHARS - 3] + "..."


class Judge:
    """Sends judge requests with bounded concurrency; keeps every call's record."""

    def __init__(
        self, judge_model: JudgeModel, *, concurrency: int = 4, log_path: Path | None = None
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        require_inference_protocol(judge_model.llm, "chat", consumer="the lookahead judge")
        self.judge_model = judge_model
        self.llm = judge_model.llm
        self.model = judge_model.llm.model
        self._semaphore = asyncio.Semaphore(concurrency)
        self.totals = Totals()
        self.log_path = log_path
        self._counter = 0
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")

    async def ask(self, request: JudgeRequest[M]) -> JudgeOutcome[M]:
        async with self._semaphore:
            outcome = await self._ask(request)
        self.totals.requests += 1
        if outcome.value is None:
            self.totals.unanswered_requests += 1
        return outcome

    async def ask_all(self, requests: Sequence[JudgeRequest[Any]]) -> list[JudgeOutcome[Any]]:
        """Every request's outcome, in order; the first is sent alone as a probe.

        Raises:
            JudgeRouteError: The route refused the probe (a 4xx other than
                429); no other request was sent.
        """
        if not requests:
            return []
        started = time.perf_counter()
        try:
            probe = await self.ask(requests[0])
            if probe.refused is not None:
                refused = probe.refused
                raise JudgeRouteError(
                    f"the judge model refused the first request (status "
                    f"{refused.status_code}): {refused.message}. Model {self.model!r} from "
                    f"{self.judge_model.source}; check its model id, provider, base_url and API "
                    f"key. No other request was sent."
                )
            rest = await asyncio.gather(*(self.ask(request) for request in requests[1:]))
        finally:
            self.totals.wall_s += time.perf_counter() - started
        return [probe, *rest]

    async def _ask(self, request: JudgeRequest[M]) -> JudgeOutcome[M]:
        messages: list[BaseMessage] = [
            SystemMessage(content=request.system),
            UserMessage(content=list(request.content)),
        ]
        prompt_digest = hashlib.sha256(
            json.dumps(
                [message.model_dump(mode="json") for message in messages],
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        prompt_text = "\n\n".join(
            [request.system]
            + [part.text for part in request.content if isinstance(part, ContentPartTextParam)]
        )
        attempts: list[CallRecord] = []
        for attempt in range(MAX_RETRIES + 1):
            self._counter += 1
            record = CallRecord(
                call_id=f"{request.purpose}-{self._counter:05d}",
                purpose=request.purpose,
                item=request.item,
                attempt=attempt,
                model=self.model,
                config=self.judge_model.config,
                prompt_sha256=prompt_digest,
                prompt_text=prompt_text if attempt == 0 else None,
                images=request.image_sources,
                started_at=time.time(),
            )
            started = time.perf_counter()
            try:
                completion = await self.llm.ainvoke(messages, output_format=request.reply_model)
            except ModelProviderError as exc:
                record.latency_ms = _elapsed_ms(started)
                record.error_type = type(exc).__name__
                if isinstance(exc, StructuredOutputValidationError):
                    problem = refusal_reason(exc.raw_output, request.reply_model) or (
                        f"it does not match the schema ({_first_line(exc.message)})"
                    )
                    record.usage = exc.usage
                    record.raw_output = exc.raw_output
                    record.error_kind = "validation"
                    record.error = problem
                    self._record(record, attempts)
                    messages = [
                        *messages,
                        AssistantMessage(content=exc.raw_output),
                        UserMessage(
                            content=(
                                f"That reply was refused: {problem}. Reply again with only the "
                                f"JSON object the schema describes, nothing else."
                            )
                        ),
                    ]
                    continue
                if isinstance(exc, ModelOutputTruncatedError | ModelGenerationInterruptedError):
                    # The budget and the server are unchanged: asking again would end the same way.
                    record.usage = exc.usage
                    record.raw_output = exc.partial_output
                    record.stop_reason = exc.stop_reason
                    record.error_kind = (
                        "truncated" if isinstance(exc, ModelOutputTruncatedError) else "interrupted"
                    )
                    record.error = exc.message
                    self._record(record, attempts)
                    return JudgeOutcome(value=None, attempts=attempts)
                record.error_kind = "call"
                record.status = exc.status_code
                record.error = exc.message
                self._record(record, attempts)
                refused = exc if is_route_refusal(exc) else None
                return JudgeOutcome(value=None, attempts=attempts, refused=refused)
            record.latency_ms = _elapsed_ms(started)
            record.usage = completion.usage
            record.stop_reason = completion.stop_reason
            record.reply = completion.completion.model_dump(mode="json")
            record.thinking = completion.thinking
            record.valid = True
            self._record(record, attempts)
            return JudgeOutcome(value=completion.completion, attempts=attempts)
        return JudgeOutcome(value=None, attempts=attempts)

    def _record(self, record: CallRecord, attempts: list[CallRecord]) -> None:
        attempts.append(record)
        self.totals.add(record)
        if self.log_path is not None:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(record.model_dump_json() + "\n")


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
