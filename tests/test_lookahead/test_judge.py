"""The judge: strict JSON, pydantic validation, at most two retries, every attempt recorded."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from lookahead.judge import Judge, JudgeRequest, image_part, strict_schema, text_part
from tests.test_lookahead.conftest import BASE
from tests.test_lookahead.fake_judge import ScriptedTransport, images, prompt_text


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Literal["yes", "no"]
    reason: str


def _request(item: str = "run@1") -> JudgeRequest[Verdict]:
    return JudgeRequest(
        purpose="test",
        item=item,
        system="You judge.",
        content=[text_part("Is it?"), image_part(BASE)],
        reply_model=Verdict,
        image_sources=["run@1"],
    )


def _ask(judge: Judge, request: JudgeRequest[Verdict]):
    return asyncio.run(judge.ask(request))


GOOD = '{"answer": "yes", "reason": "it is."}'


def test_request_parameters_ask_for_strict_json_with_high_reasoning() -> None:
    transport = ScriptedTransport(lambda request: GOOD)
    outcome = _ask(Judge(transport), _request())
    assert outcome.value == Verdict(answer="yes", reason="it is.")
    (request,) = transport.requests
    assert request["model"] == "deepseek-v4.1-flash-1017"
    assert request["reasoning_effort"] == "high"
    response_format = request["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["additionalProperties"] is False and "title" not in json.dumps(schema)
    assert images(request)[0].startswith("data:image/png;base64,")
    assert request["messages"][0] == {"role": "system", "content": "You judge."}


def test_a_refused_reply_is_retried_with_the_reason() -> None:
    replies = iter(['```json\n{"answer": "maybe", "reason": "x"}\n```', GOOD])
    transport = ScriptedTransport(lambda request: next(replies))
    outcome = _ask(Judge(transport), _request())
    assert outcome.value is not None
    first, second = outcome.attempts
    assert first["valid"] is False and first["error_kind"] == "validation"
    assert first["unfenced"] is True and "answer" in first["error"]
    assert first["raw_reply"].startswith("```json")
    retry = transport.requests[1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": first["raw_reply"]}
    assert "refused" in retry[-1]["content"]
    assert second["valid"] is True and second["attempt"] == 1


def test_three_refusals_give_no_value_and_keep_every_reply() -> None:
    transport = ScriptedTransport(lambda request: "not json")
    judge = Judge(transport)
    outcome = _ask(judge, _request())
    assert outcome.value is None and len(outcome.attempts) == 3
    assert outcome.error is not None and "schema" in outcome.error
    assert [attempt["raw_reply"] for attempt in outcome.attempts] == ["not json"] * 3
    assert judge.totals.unanswered_requests == 1 and judge.totals.invalid_replies == 3


def test_a_failed_call_is_recorded_and_retried() -> None:
    results: list[str | Exception] = [ConnectionError("gateway reset"), GOOD]
    transport = ScriptedTransport(lambda request: results.pop(0))
    judge = Judge(transport)
    outcome = _ask(judge, _request())
    assert outcome.value is not None
    assert outcome.attempts[0]["error_kind"] == "call"
    assert outcome.attempts[0]["error"] == "ConnectionError: gateway reset"
    assert judge.totals.failed_calls == 1 and judge.totals.valid_replies == 1


def test_every_attempt_is_logged_with_model_parameters_latency_and_usage(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    replies = iter(["{}", GOOD])
    judge = Judge(ScriptedTransport(lambda request: next(replies)), log_path=log)
    _ask(judge, _request())
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    for record in records:
        assert record["model"] == "deepseek-v4.1-flash-1017"
        assert record["parameters"]["reasoning_effort"] == "high"
        assert isinstance(record["latency_ms"], int)
        assert record["usage"]["completion_tokens_details"]["reasoning_tokens"] == 5
        assert record["images"] == ["run@1"]
    assert records[0]["prompt_text"].startswith("You judge.")
    totals = judge.totals
    assert (totals.calls, totals.prompt_tokens, totals.reasoning_tokens) == (2, 200, 10)
    assert "2 call(s)" in totals.summary()


def test_concurrency_is_bounded() -> None:
    transport = ScriptedTransport(lambda request: GOOD, delay=0.01)
    judge = Judge(transport, concurrency=2)
    outcomes = asyncio.run(judge.ask_all([_request(f"run@{index}") for index in range(7)]))
    assert all(outcome.value is not None for outcome in outcomes)
    assert transport.most_in_flight == 2
    assert judge.totals.requests == 7


def test_strict_schema_keeps_definitions_and_drops_titles() -> None:
    class Inner(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: int

    class Outer(BaseModel):
        model_config = ConfigDict(extra="forbid")
        inner: Inner

    schema = strict_schema(Outer)
    assert schema["$defs"]["Inner"]["additionalProperties"] is False
    assert "title" not in json.dumps(schema)
    assert "Is it?" in prompt_text({"messages": [{"role": "user", "content": _request().content}]})
