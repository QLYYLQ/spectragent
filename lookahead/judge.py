"""The judge model: strict-JSON requests over an OpenAI-protocol gateway, every reply kept.

An HTTP 200 does not mean the reply honours the schema (``docs/llm/dmxapi.cn.md``),
so every reply is validated with pydantic. A request is sent at most three
times: the first attempt and up to two retries, each retry telling the model
what was wrong with its previous reply. Every attempt is recorded — model,
parameters, latency, usage, the raw reply and why it was refused — in the
order the attempts finished, and the totals are reported at the end.

The transport is injected: :class:`OpenAITransport` talks to the gateway named
by ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` with the ``openai`` SDK; tests pass
a scripted one.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from spectragent.core.llm.fences import strip_code_fence
from spectragent.schemas.observation.image import image_media_type

DEFAULT_MODEL = "deepseek-v4.1-flash-1017"
DEFAULT_REASONING_EFFORT = "high"
#: Retries after the first attempt.
MAX_RETRIES = 2

M = TypeVar("M", bound=BaseModel)


class JudgeConfigurationError(RuntimeError):
    """The judge cannot be reached as configured (no gateway URL or key in the environment)."""


@dataclass(frozen=True)
class ChatReply:
    """What one chat-completion call returned, reduced to what is recorded."""

    content: str | None
    finish_reason: str | None = None
    model: str | None = None
    usage: dict[str, Any] | None = None


class ChatTransport(Protocol):
    """Sends one chat-completion request (the ``openai`` SDK's keyword arguments)."""

    async def create(self, **request: Any) -> ChatReply: ...

    async def close(self) -> None: ...


class OpenAITransport:
    """The ``openai`` SDK's async client, pointed at ``OPENAI_BASE_URL`` with ``OPENAI_API_KEY``."""

    def __init__(self, *, base_url: str, api_key: str, timeout: float = 300.0) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            base_url=base_url, api_key=api_key, max_retries=0, timeout=timeout
        )

    @classmethod
    def from_environment(cls) -> OpenAITransport:
        base_url = os.environ.get("OPENAI_BASE_URL")
        api_key = os.environ.get("OPENAI_API_KEY")
        missing = [
            name
            for name, value in (("OPENAI_BASE_URL", base_url), ("OPENAI_API_KEY", api_key))
            if not value
        ]
        if missing:
            raise JudgeConfigurationError(
                f"the judge model needs {' and '.join(missing)} in the environment"
            )
        assert base_url is not None and api_key is not None
        return cls(base_url=base_url, api_key=api_key)

    async def create(self, **request: Any) -> ChatReply:
        response = await self._client.chat.completions.create(**request)
        choice = response.choices[0] if response.choices else None
        return ChatReply(
            content=choice.message.content if choice is not None else None,
            finish_reason=choice.finish_reason if choice is not None else None,
            model=response.model,
            usage=response.usage.model_dump(mode="json") if response.usage is not None else None,
        )

    async def close(self) -> None:
        await self._client.close()


# ── requests ──────────────────────────────────────────────────────────


def image_part(data: bytes) -> dict[str, Any]:
    """An image as a base64 data URL content part."""
    media_type = image_media_type(data) or "image/png"
    url = f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"
    return {"type": "image_url", "image_url": {"url": url}}


def text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """*model*'s JSON schema for a strict ``json_schema`` response format (titles dropped)."""

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: clean(item) for key, item in value.items() if key not in {"title", "default"}
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return clean(model.model_json_schema())


@dataclass
class JudgeRequest(Generic[M]):
    """One question to the judge: messages, the reply model, and what it is about."""

    purpose: str
    item: str
    system: str
    content: list[dict[str, Any]]
    reply_model: type[M]
    #: What each image in *content* is (a run and step), recorded instead of the bytes.
    image_sources: list[str] = field(default_factory=list)


@dataclass
class JudgeOutcome(Generic[M]):
    """The validated reply (``None`` when every attempt failed) and every attempt's record."""

    value: M | None
    attempts: list[dict[str, Any]]

    @property
    def error(self) -> str | None:
        if self.value is not None or not self.attempts:
            return None
        return self.attempts[-1].get("error") or "no valid reply"


@dataclass
class Totals:
    calls: int = 0
    failed_calls: int = 0
    invalid_replies: int = 0
    valid_replies: int = 0
    requests: int = 0
    unanswered_requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    calls_without_usage: int = 0
    latency_ms: int = 0
    wall_s: float = 0.0

    def add(self, attempt: dict[str, Any]) -> None:
        self.calls += 1
        self.latency_ms += int(attempt.get("latency_ms") or 0)
        if attempt.get("error_kind") == "call":
            self.failed_calls += 1
        elif attempt.get("valid"):
            self.valid_replies += 1
        else:
            self.invalid_replies += 1
        usage = attempt.get("usage")
        if not usage:
            self.calls_without_usage += 1
            return
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        details = usage.get("completion_tokens_details") or {}
        self.reasoning_tokens += int(details.get("reasoning_tokens") or 0)

    def summary(self) -> str:
        return (
            f"judge: {self.requests} request(s), {self.unanswered_requests} without a valid reply; "
            f"{self.calls} call(s): {self.valid_replies} valid, {self.invalid_replies} refused by "
            f"validation, {self.failed_calls} failed; tokens: {self.prompt_tokens} prompt, "
            f"{self.completion_tokens} completion ({self.reasoning_tokens} reasoning), "
            f"{self.calls_without_usage} call(s) without usage; "
            f"summed latency {self.latency_ms / 1000:.1f} s, wall time {self.wall_s:.1f} s"
        )

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class Judge:
    """Sends judge requests with bounded concurrency; keeps every attempt's record."""

    def __init__(
        self,
        transport: ChatTransport,
        *,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
        concurrency: int = 4,
        log_path: Path | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self.transport = transport
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._semaphore = asyncio.Semaphore(concurrency)
        self.totals = Totals()
        self.log_path = log_path
        self._counter = 0
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text("", encoding="utf-8")

    def parameters(self, reply_model: type[BaseModel]) -> dict[str, Any]:
        """Every request parameter except the messages."""
        params: dict[str, Any] = {
            "model": self.model,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": reply_model.__name__,
                    "strict": True,
                    "schema": strict_schema(reply_model),
                },
            },
        }
        if self.reasoning_effort is not None:
            params["reasoning_effort"] = self.reasoning_effort
        return params

    async def ask(self, request: JudgeRequest[M]) -> JudgeOutcome[M]:
        async with self._semaphore:
            outcome = await self._ask(request)
        self.totals.requests += 1
        if outcome.value is None:
            self.totals.unanswered_requests += 1
        return outcome

    async def ask_all(self, requests: Sequence[JudgeRequest[M]]) -> list[JudgeOutcome[M]]:
        started = time.perf_counter()
        outcomes = await asyncio.gather(*(self.ask(request) for request in requests))
        self.totals.wall_s += time.perf_counter() - started
        return list(outcomes)

    async def _ask(self, request: JudgeRequest[M]) -> JudgeOutcome[M]:
        params = self.parameters(request.reply_model)
        user = {"role": "user", "content": request.content}
        messages: list[dict[str, Any]] = [{"role": "system", "content": request.system}, user]
        prompt_digest = hashlib.sha256(
            json.dumps(messages, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        prompt_text = "\n\n".join(
            [request.system]
            + [part["text"] for part in request.content if part.get("type") == "text"]
        )
        attempts: list[dict[str, Any]] = []
        for attempt in range(MAX_RETRIES + 1):
            self._counter += 1
            record: dict[str, Any] = {
                "call_id": f"{request.purpose}-{self._counter:05d}",
                "purpose": request.purpose,
                "item": request.item,
                "attempt": attempt,
                "model": self.model,
                "parameters": dict(params),
                "prompt_sha256": prompt_digest,
                "prompt_text": prompt_text if attempt == 0 else None,
                "images": request.image_sources,
                "started_at": time.time(),
            }
            started = time.perf_counter()
            try:
                reply = await self.transport.create(messages=messages, **params)
            except Exception as exc:  # the gateway's or the SDK's error: recorded, then retried
                record.update(
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    error_kind="call",
                    error=f"{type(exc).__name__}: {exc}",
                    valid=False,
                )
                self._record(record, attempts)
                continue
            record.update(
                latency_ms=round((time.perf_counter() - started) * 1000),
                response_model=reply.model,
                finish_reason=reply.finish_reason,
                usage=reply.usage,
                raw_reply=reply.content,
            )
            value, problem, unfenced = _validate(reply.content, request.reply_model)
            record["unfenced"] = unfenced
            if value is not None:
                record["valid"] = True
                self._record(record, attempts)
                return JudgeOutcome(value=value, attempts=attempts)
            record.update(valid=False, error_kind="validation", error=problem)
            self._record(record, attempts)
            messages = [
                *messages,
                {"role": "assistant", "content": reply.content or ""},
                {
                    "role": "user",
                    "content": (
                        f"That reply was refused: {problem}. Reply again with only the JSON "
                        f"object the schema describes, nothing else."
                    ),
                },
            ]
        return JudgeOutcome(value=None, attempts=attempts)

    def _record(self, record: dict[str, Any], attempts: list[dict[str, Any]]) -> None:
        attempts.append(record)
        self.totals.add(record)
        if self.log_path is not None:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _validate(content: str | None, model: type[M]) -> tuple[M | None, str | None, bool]:
    """The reply validated as *model*; else why not. The flag: a code fence had to be removed."""
    if not content or not content.strip():
        return None, "the reply is empty", False
    text = content.strip()
    unfenced = strip_code_fence(text)
    fenced = unfenced != text
    try:
        return model.model_validate_json(unfenced), None, fenced
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'reply'}: {error['msg']}"
            for error in exc.errors()[:5]
        )
        return None, f"it does not match the schema ({problems})", fenced
