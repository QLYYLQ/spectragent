"""The judge: strict JSON, pydantic validation, bounded retries, every call recorded."""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from lookahead.judge import (
    MAX_TRANSIENT_RETRIES,
    Judge,
    JudgeRequest,
    JudgeRouteError,
    TransportError,
    image_part,
    text_part,
    wire_schema,
)
from tests.test_lookahead.conftest import BASE
from tests.test_lookahead.fake_judge import ScriptedTransport, images, prompt_text


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Literal["yes", "no"]
    reason: str = Field(min_length=1)


def _request(item: str = "run@1") -> JudgeRequest[Verdict]:
    return JudgeRequest(
        purpose="test",
        item=item,
        system="You judge.",
        content=[text_part("Is it?"), image_part(BASE)],
        reply_model=Verdict,
        image_sources=["run@1"],
    )


class Pauses:
    """Records the pauses the judge asks for instead of sleeping."""

    def __init__(self) -> None:
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)


def _judge(
    transport: ScriptedTransport, *, concurrency: int = 4, log_path: Path | None = None
) -> tuple[Judge, Pauses]:
    pauses = Pauses()
    judge = Judge(
        transport,
        sleep=pauses,
        rng=random.Random(0),
        concurrency=concurrency,
        log_path=log_path,
    )
    return judge, pauses


def _ask(judge: Judge, request: JudgeRequest[Verdict]):
    return asyncio.run(judge.ask(request))


GOOD = '{"answer": "yes", "reason": "it is."}'


def test_request_parameters_ask_for_strict_json_with_a_completion_budget() -> None:
    transport = ScriptedTransport(lambda request: GOOD)
    judge, _ = _judge(transport)
    outcome = _ask(judge, _request())
    assert outcome.value == Verdict(answer="yes", reason="it is.")
    (request,) = transport.requests
    assert request["model"] == "deepseek-v4.1-flash-1017"
    assert request["reasoning_effort"] == "high"
    assert request["max_completion_tokens"] == 8192
    response_format = request["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert "minLength" not in json.dumps(schema) and "title" not in json.dumps(schema)
    assert images(request)[0].startswith("data:image/png;base64,")
    assert request["messages"][0] == {"role": "system", "content": "You judge."}


def test_the_sent_schema_drops_strict_mode_keywords_but_keeps_property_names() -> None:
    class Inner(BaseModel):
        model_config = ConfigDict(extra="forbid")
        title: str = Field(min_length=3, max_length=9, pattern="^[a-z]+$")
        tags: list[str] = Field(min_length=1)

    class Outer(BaseModel):
        model_config = ConfigDict(extra="forbid")
        inner: Inner
        score: int = Field(ge=0, le=10)

    schema = wire_schema(Outer)
    inner = schema["$defs"]["Inner"]
    assert set(inner["properties"]) == {"title", "tags"}  # a field named title stays
    assert inner["properties"]["title"] == {"type": "string"}
    assert inner["properties"]["tags"] == {"items": {"type": "string"}, "type": "array"}
    assert schema["properties"]["score"] == {"type": "integer"}
    assert inner["additionalProperties"] is False
    # the pydantic model still enforces them
    assert Outer.model_json_schema()["$defs"]["Inner"]["properties"]["title"]["minLength"] == 3


def test_a_refused_reply_is_retried_with_the_reason() -> None:
    replies = iter(['```json\n{"answer": "maybe", "reason": "x"}\n```', GOOD])
    transport = ScriptedTransport(lambda request: next(replies))
    judge, _ = _judge(transport)
    outcome = _ask(judge, _request())
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
    judge, _ = _judge(transport)
    outcome = _ask(judge, _request())
    assert outcome.value is None and len(outcome.attempts) == 3
    assert outcome.error is not None and "schema" in outcome.error
    assert [attempt["raw_reply"] for attempt in outcome.attempts] == ["not json"] * 3
    assert judge.totals.unanswered_requests == 1 and judge.totals.invalid_replies == 3


@pytest.mark.parametrize("status", [429, 500, 503, None])
def test_rate_limits_server_errors_and_lost_connections_back_off(status: int | None) -> None:
    results: list[str | Exception] = [
        TransportError("busy", status=status),
        TransportError("busy", status=status),
        GOOD,
    ]
    transport = ScriptedTransport(lambda request: results.pop(0))
    judge, pauses = _judge(transport)
    outcome = _ask(judge, _request())
    assert outcome.value is not None
    assert [attempt["status"] for attempt in outcome.attempts[:2]] == [status, status]
    assert all(attempt["retried"] for attempt in outcome.attempts[:2])
    assert len(pauses.seconds) == 2
    assert 1.0 <= pauses.seconds[0] <= 2.0 and 2.0 <= pauses.seconds[1] <= 4.0  # jittered, doubling
    assert judge.totals.failed_calls == 2 and judge.totals.retried_calls == 2


def test_backoff_stops_after_its_retries() -> None:
    transport = ScriptedTransport(lambda request: TransportError("down", status=502))
    judge, pauses = _judge(transport)
    outcome = _ask(judge, _request())
    assert outcome.value is None and outcome.refused is None
    assert len(outcome.attempts) == MAX_TRANSIENT_RETRIES + 1
    assert len(pauses.seconds) == MAX_TRANSIENT_RETRIES
    assert max(pauses.seconds) <= 60.0


def test_other_client_errors_are_not_retried() -> None:
    transport = ScriptedTransport(
        lambda request: TransportError("bad request", status=400, body='{"error": "bad schema"}')
    )
    judge, pauses = _judge(transport)
    outcome = _ask(judge, _request())
    assert outcome.value is None and len(outcome.attempts) == 1
    assert outcome.refused is not None and outcome.refused.status == 400
    assert outcome.attempts[0]["body"] == '{"error": "bad schema"}'
    assert pauses.seconds == []


def test_the_first_request_probes_the_route_and_a_refusal_stops_the_batch() -> None:
    transport = ScriptedTransport(
        lambda request: TransportError("nope", status=404, body="no model")
    )
    judge, _ = _judge(transport)
    with pytest.raises(JudgeRouteError, match="HTTP 404.*no model.*No other request"):
        asyncio.run(judge.ask_all([_request(f"run@{index}") for index in range(5)]))
    assert len(transport.requests) == 1


def test_a_later_refusal_fails_only_its_request() -> None:
    def script(request: dict) -> str | Exception:
        return TransportError("too big", status=413) if "second" in prompt_text(request) else GOOD

    requests = [_request("run@1"), _request("run@2")]
    requests[1].content = [text_part("second"), image_part(BASE)]
    judge, _ = _judge(ScriptedTransport(script))
    outcomes = asyncio.run(judge.ask_all(requests))
    assert outcomes[0].value is not None
    assert outcomes[1].value is None and outcomes[1].refused is not None


def test_every_call_is_logged_with_model_parameters_latency_and_usage(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"
    replies = iter(["{}", GOOD])
    judge, _ = _judge(ScriptedTransport(lambda request: next(replies)), log_path=log)
    _ask(judge, _request())
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    for record in records:
        assert record["model"] == "deepseek-v4.1-flash-1017"
        assert record["parameters"]["reasoning_effort"] == "high"
        assert record["parameters"]["max_completion_tokens"] == 8192
        assert isinstance(record["latency_ms"], int)
        assert record["usage"]["completion_tokens_details"]["reasoning_tokens"] == 5
        assert record["images"] == ["run@1"]
    assert records[0]["prompt_text"].startswith("You judge.")
    totals = judge.totals
    assert (totals.calls, totals.prompt_tokens, totals.reasoning_tokens) == (2, 200, 10)
    assert "2 call(s)" in totals.summary()


def test_concurrency_is_bounded() -> None:
    transport = ScriptedTransport(lambda request: GOOD, delay=0.01)
    judge, _ = _judge(transport, concurrency=2)
    outcomes = asyncio.run(judge.ask_all([_request(f"run@{index}") for index in range(7)]))
    assert all(outcome.value is not None for outcome in outcomes)
    assert transport.most_in_flight == 2
    assert judge.totals.requests == 7
