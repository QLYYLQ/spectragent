"""The judge: a chat model from core/llm, refused replies asked again, every call recorded."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from lookahead.judge import (
    CallRecord,
    Judge,
    JudgeModel,
    JudgeOutcome,
    JudgeRequest,
    JudgeRouteError,
    image_part,
    load_judge_model,
    text_part,
)
from lookahead.runs import LookaheadInputError
from spectragent.core.llm.exceptions import (
    ModelGenerationInterruptedError,
    ModelOutputTruncatedError,
    ModelProviderError,
    ModelRateLimitError,
)
from spectragent.core.llm.messages import (
    AssistantMessage,
    BaseMessage,
    SystemMessage,
    UserMessage,
)
from spectragent.core.llm.providers.openai_deepseek import ChatDeepSeek
from spectragent.core.llm.views import ModelInvokeUsage
from tests.test_lookahead.conftest import BASE
from tests.test_lookahead.fake_judge import (
    THINKING,
    USAGE,
    ScriptedChatModel,
    images,
    judge_model,
    prompt_text,
)

SHIPPED = Path(__file__).resolve().parents[2] / "configs" / "lookahead" / "judge_deepseek.yaml"
#: The smallest JPEG header ``image_media_type`` recognises.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Literal["yes", "no"]
    reason: str = Field(min_length=1)


def _request(item: str = "run@1", text: str = "Is it?") -> JudgeRequest[Verdict]:
    return JudgeRequest(
        purpose="test",
        item=item,
        system="You judge.",
        content=[text_part(text), image_part(BASE)],
        reply_model=Verdict,
        image_sources=["run@1"],
    )


def _judge(llm: ScriptedChatModel, *, concurrency: int = 4, log: Path | None = None) -> Judge:
    return Judge(judge_model(llm), concurrency=concurrency, log_path=log)


def _ask(judge: Judge, request: JudgeRequest[Verdict]) -> JudgeOutcome[Verdict]:
    return asyncio.run(judge.ask(request))


GOOD = '{"answer": "yes", "reason": "it is."}'


def _truncated() -> ModelOutputTruncatedError:
    return ModelOutputTruncatedError(
        model="fake-judge",
        stop_reason="length",
        ceiling_field="max_completion_tokens",
        ceiling=16384,
        usage=USAGE,
        partial_output='{"answer": "ye',
    )


def test_a_valid_reply_asks_with_the_reply_model_and_records_the_call(tmp_path: Path) -> None:
    llm = ScriptedChatModel(lambda messages: GOOD)
    log = tmp_path / "calls.jsonl"
    outcome = _ask(_judge(llm, log=log), _request())
    assert outcome.value == Verdict(answer="yes", reason="it is.")
    assert llm.formats == [Verdict]
    (messages,) = llm.calls
    system, user = messages
    assert isinstance(system, SystemMessage) and system.content == "You judge."
    assert isinstance(user, UserMessage) and prompt_text([user]) == "Is it?"
    (image,) = images(messages)
    assert image.image_url.media_type == "image/png"
    assert image.image_url.url.startswith("data:image/png;base64,")
    (record,) = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert record["call_id"] == "test-00001" and record["attempt"] == 0 and record["valid"]
    assert record["model"] == "fake-judge" and record["config"]["max_completion_tokens"] == 16384
    assert record["reply"] == {"answer": "yes", "reason": "it is."}
    assert record["thinking"] == THINKING and record["stop_reason"] == "stop"
    assert record["usage"]["completion_reasoning_tokens"] == 5
    assert record["prompt_text"] == "You judge.\n\nIs it?" and len(record["prompt_sha256"]) == 64
    assert record["images"] == ["run@1"] and isinstance(record["latency_ms"], int)
    assert record["error_kind"] is None and record["raw_output"] is None


def test_an_image_is_labelled_with_its_own_media_type() -> None:
    part = image_part(JPEG)
    assert part.image_url.media_type == "image/jpeg"
    assert part.image_url.url.startswith("data:image/jpeg;base64,")


def test_a_refused_reply_is_asked_again_with_the_reason() -> None:
    replies = iter(['```json\n{"answer": "maybe", "reason": "x"}\n```', GOOD])
    llm = ScriptedChatModel(lambda messages: next(replies))
    outcome = _ask(_judge(llm), _request())
    assert outcome.value is not None
    first, second = outcome.attempts
    assert first.valid is False and first.error_kind == "validation"
    assert first.error is not None and first.error.startswith("it does not match the schema (")
    assert "answer: Input should be 'yes' or 'no'" in first.error
    assert "errors.pydantic.dev" not in first.error and "input_value" not in first.error
    assert first.raw_output == '{"answer": "maybe", "reason": "x"}'  # as the validator saw it
    assert first.usage == USAGE and first.thinking is None
    again = llm.calls[1]
    assert again[:2] == llm.calls[0]
    assistant, refusal = again[-2:]
    assert isinstance(assistant, AssistantMessage) and assistant.content == first.raw_output
    assert isinstance(refusal, UserMessage)
    assert refusal.content == (
        f"That reply was refused: {first.error}. Reply again with only the JSON object the "
        "schema describes, nothing else."
    )
    assert second.valid is True and second.attempt == 1 and second.prompt_text is None


def test_three_refusals_give_no_value_and_keep_every_reply() -> None:
    llm = ScriptedChatModel(lambda messages: "not json")
    judge = _judge(llm)
    outcome = _ask(judge, _request())
    assert outcome.value is None and len(outcome.attempts) == 3
    assert outcome.error is not None and "Invalid JSON" in outcome.error
    assert [attempt.raw_output for attempt in outcome.attempts] == ["not json"] * 3
    assert judge.totals.unanswered_requests == 1 and judge.totals.invalid_replies == 3


def test_an_empty_reply_is_named_as_such() -> None:
    replies = iter(["", GOOD])
    outcome = _ask(_judge(ScriptedChatModel(lambda messages: next(replies))), _request())
    assert outcome.attempts[0].error == "the reply is empty"


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (_truncated(), "truncated"),
        (ModelGenerationInterruptedError(stop_reason="aborted", usage=USAGE), "interrupted"),
    ],
)
def test_a_cut_reply_fails_only_its_request_without_a_re_ask(
    error: ModelOutputTruncatedError | ModelGenerationInterruptedError, kind: str
) -> None:
    def script(messages: list[BaseMessage]) -> str | Exception:
        return error if "first" in prompt_text(messages) else GOOD

    llm = ScriptedChatModel(script)
    judge = _judge(llm)
    # The cut reply is the probe: it is no route refusal, so the batch goes on.
    outcomes = asyncio.run(judge.ask_all([_request("a", "first"), _request("b", "second")]))
    assert outcomes[0].value is None and outcomes[0].refused is None
    assert outcomes[1].value is not None
    (record,) = outcomes[0].attempts
    assert record.error_kind == kind and record.error_type == type(error).__name__
    assert record.stop_reason == error.stop_reason and record.status is None
    assert record.raw_output == error.partial_output and record.usage == USAGE
    assert record.error == error.message
    assert len(llm.calls) == 2  # asked once each
    assert judge.totals.failed_calls == 1
    assert (judge.totals.truncated_calls, judge.totals.interrupted_calls) == (
        (1, 0) if kind == "truncated" else (0, 1)
    )


@pytest.mark.parametrize(
    "error",
    [
        ModelRateLimitError("slow down"),
        ModelProviderError("bad gateway", status_code=502),
        ModelProviderError("Connection error.", status_code=502),
        ModelProviderError("unavailable", status_code=503),
    ],
    ids=["429", "502", "connection", "503"],
)
def test_a_failure_left_after_the_sdk_retries_fails_only_its_request(
    error: ModelProviderError,
) -> None:
    def script(messages: list[BaseMessage]) -> str | Exception:
        return error if "first" in prompt_text(messages) else GOOD

    judge = _judge(ScriptedChatModel(script))
    outcomes = asyncio.run(judge.ask_all([_request("a", "first"), _request("b", "second")]))
    assert outcomes[0].value is None and outcomes[0].refused is None
    assert outcomes[1].value is not None
    (record,) = outcomes[0].attempts
    assert record.error_kind == "call" and record.status == error.status_code
    assert record.error == error.message and record.error_type == type(error).__name__


@pytest.mark.parametrize("status", [400, 401, 404])
def test_the_first_request_probes_the_route_and_a_refusal_stops_the_batch(status: int) -> None:
    llm = ScriptedChatModel(
        lambda messages: ModelProviderError("Model Not Exist", status_code=status)
    )
    judge = Judge(judge_model(llm, Path("configs/judge.yaml")))
    with pytest.raises(JudgeRouteError) as raised:
        asyncio.run(judge.ask_all([_request(f"run@{index}") for index in range(5)]))
    message = str(raised.value)
    assert f"status {status}" in message and "Model Not Exist" in message
    assert "configs/judge.yaml" in message and "No other request was sent" in message
    assert len(llm.calls) == 1


def test_a_later_refusal_fails_only_its_request() -> None:
    def script(messages: list[BaseMessage]) -> str | Exception:
        if "second" in prompt_text(messages):
            return ModelProviderError("too big", status_code=413)
        return GOOD

    judge = _judge(ScriptedChatModel(script))
    outcomes = asyncio.run(judge.ask_all([_request("a"), _request("b", "second")]))
    assert outcomes[0].value is not None
    assert outcomes[1].value is None and outcomes[1].refused is not None
    assert outcomes[1].attempts[0].status == 413


def test_every_call_is_logged_with_the_redacted_configuration(tmp_path: Path) -> None:
    config = tmp_path / "judge.yaml"
    config.write_text(
        SHIPPED.read_text(encoding="utf-8").replace(
            "    max_retries: 5\n", "    max_retries: 5\n    api_key: sk-not-for-the-log\n"
        ),
        encoding="utf-8",
    )
    composed = load_judge_model(config)
    assert composed.config["api_key"] == "<omitted>"
    replies = iter(["{}", GOOD])
    llm = ScriptedChatModel(lambda messages: next(replies))
    log = tmp_path / "calls.jsonl"
    judge = Judge(
        JudgeModel(llm=judge_model(llm).llm, source=config, config=composed.config), log_path=log
    )
    _ask(judge, _request())
    text = log.read_text(encoding="utf-8")
    assert "sk-not-for-the-log" not in text
    records = [json.loads(line) for line in text.splitlines()]
    assert [record["attempt"] for record in records] == [0, 1]
    for record in records:
        assert record["config"] == composed.config
        assert record["config"]["max_completion_tokens"] == 16384
        assert record["config"]["reasoning_effort"] == "high"
        assert record["usage"]["completion_reasoning_tokens"] == 5
        assert record["images"] == ["run@1"]
    assert records[0]["prompt_text"].startswith("You judge.")
    assert records[1]["prompt_text"] is None
    totals = judge.totals
    assert (totals.calls, totals.prompt_tokens, totals.reasoning_tokens) == (2, 200, 10)
    assert "2 call(s)" in totals.summary()


def test_totals_count_calls_whose_usage_states_no_reasoning() -> None:
    judge = _judge(ScriptedChatModel(lambda messages: GOOD))
    _ask(judge, _request())
    usage = ModelInvokeUsage(
        prompt_tokens=7,
        prompt_cached_tokens=None,
        prompt_cache_creation_tokens=None,
        prompt_image_tokens=None,
        completion_tokens=3,
        total_tokens=10,
    )
    judge.totals.add(
        CallRecord(
            call_id="other-00001",
            purpose="other",
            item="run@2",
            attempt=0,
            model="other",
            config={},
            prompt_sha256="0" * 64,
            prompt_text=None,
            images=[],
            started_at=0.0,
            usage=usage,
            valid=True,
        )
    )
    totals = judge.totals
    assert totals.reasoning_tokens == 5 and totals.calls_without_reasoning_count == 1
    assert totals.completion_tokens == 23
    assert "1 call(s) without a reasoning count" in totals.summary()


def test_concurrency_is_bounded() -> None:
    llm = ScriptedChatModel(lambda messages: GOOD, delay=0.01)
    judge = _judge(llm, concurrency=2)
    outcomes = asyncio.run(judge.ask_all([_request(f"run@{index}") for index in range(7)]))
    assert all(outcome.value is not None for outcome in outcomes)
    assert llm.most_in_flight == 2
    assert judge.totals.requests == 7


# ── composing the model from YAML ─────────────────────────────────────


def test_the_shipped_configuration_composes_deepseek_on_its_official_api() -> None:
    composed = load_judge_model(SHIPPED)
    assert isinstance(composed.llm, ChatDeepSeek)
    assert composed.llm.model == "deepseek-flash" and composed.llm.provider == "deepseek"
    assert composed.llm.reasoning_effort == "high"
    assert composed.llm.max_completion_tokens == 16384
    assert composed.source == SHIPPED
    assert composed.config["max_completion_tokens"] == 16384
    assert composed.config["api_key"] is None  # left out: the client reads OPENAI_API_KEY


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        (
            "llm:\n  - {model_type: openai, provider: {name: deepseek}, model: m}\n"
            "agent_loop:\n  - {loop_id: react}\n",
            "holds layers ['agent_loop']",
        ),
        (
            "llm:\n  - {model_type: openai, provider: {name: deepseek}, model: a}\n"
            "  - {model_type: openai, provider: {name: deepseek}, model: b}\n",
            "expected exactly 1 instance",
        ),
        ("llm:\n  - {model_type: openai, provider: {name: nowhere}, model: m}\n", "nowhere"),
        ("- just a list\n", "YAML mapping"),
        ("model_type: openai\n", "YAML mapping"),
    ],
    ids=["another-layer", "two-models", "unknown-provider", "a-list", "no-llm-layer"],
)
def test_a_configuration_that_is_not_one_llm_layer_is_refused(
    tmp_path: Path, text: str, complaint: str
) -> None:
    config = tmp_path / "judge.yaml"
    config.write_text(text, encoding="utf-8")
    with pytest.raises(LookaheadInputError, match="--judge-config") as raised:
        load_judge_model(config)
    assert complaint in str(raised.value)


def test_a_missing_configuration_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LookaheadInputError, match="cannot be read"):
        load_judge_model(tmp_path / "absent.yaml")
