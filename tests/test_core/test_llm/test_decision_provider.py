"""Both decision routes preserve native meaning, errors and resource ownership."""

from __future__ import annotations

import asyncio
import base64
import io
import json
from copy import deepcopy
from typing import Any

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from spectragent.core.llm._registrar import LLMRegistrar
from spectragent.core.llm.base import BaseDecisionModel, BaseInferenceModel
from spectragent.core.llm.decision import DecisionRequest
from spectragent.core.llm.decision_errors import (
    DecisionHTTPError,
    DecisionInputError,
    DecisionLimitError,
    DecisionResponseError,
    DecisionTimeoutError,
)
from spectragent.core.llm.decision_registrar import DecisionModelRegistrar
from spectragent.core.llm.lifecycle import model_session
from spectragent.core.llm.providers.typesafe import TypeSafeDirect, TypeSafeOpenRouter


def image_asset(size: tuple[int, int] = (640, 320)) -> dict[str, Any]:
    file = io.BytesIO()
    Image.new("RGB", size, "red").save(file, format="PNG")
    return {
        "type": "image",
        "format": "png",
        "data_base64": base64.b64encode(file.getvalue()).decode(),
    }


def request_dict() -> dict[str, Any]:
    return {
        "state": {"image": "reference-image", "ticket": ["urgent", {"count": 3}]},
        "assets": {"reference-image": image_asset()},
        "questions": {
            "team": {
                "type": "choice",
                "instructions": {"question": "Which team?"},
                "criteria": {"help": {"meaning": "support"}, "other": None},
            },
            "severity": {
                "type": "score",
                "instructions": ["How urgent?"],
                "criteria": [{"level": "low"}, {"level": "high"}],
            },
            "urgent": {
                "type": "noul",
                "instructions": "Is it urgent?",
                "criteria": {"true": {"deadline": "now"}, "false": ["later"]},
            },
            "target": {"type": "box", "instructions": "The red area in image"},
        },
    }


def response_dict() -> dict[str, Any]:
    return {
        "model": "jev-1.14.0",
        "answers": {
            "team": {
                "type": "choice",
                "choice": "help",
                "probabilities": {"help": 0.9, "other": 0.1},
                "confidence": 0.81,
            },
            "severity": {
                "type": "score",
                "score": 0.9,
                "legend": {"0": {"level": "low"}, "1": {"level": "high"}},
                "probabilities": {"0": 0.1, "1": 0.9},
                "confidence": 0.82,
            },
            "urgent": {"type": "noul", "noul": 0.93},
            "target": {
                "type": "box",
                "box": {"top": 0.0, "bottom": 1.0, "left": 0.0, "right": 0.5},
                "confidence": 0.83,
            },
        },
        "usage": {"input_tokens": 123, "output_tokens": 42, "cost": 0.001},
        "id": "test-reply",
        "provider": "TypeSafe",
    }


ROUTES = (TypeSafeDirect, TypeSafeOpenRouter)


def test_decision_registry_coverage_and_no_chat_membership():
    assert set(DecisionModelRegistrar.get_all()) == {route.__registry_key__ for route in ROUTES}
    assert not set(DecisionModelRegistrar.get_all()) & set(LLMRegistrar.get_all())
    for route in ROUTES:
        model = route()
        assert isinstance(model, BaseInferenceModel)
        assert isinstance(model, BaseDecisionModel)
        assert model.inference_kind == "decision"


@pytest.mark.parametrize("route", ROUTES)
async def test_complete_native_contract_and_exact_route_body(route):
    observed = []

    def reply(request):
        observed.append(request)
        return httpx.Response(200, json=response_dict())

    client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
    model = route(api_key="test", http_client=client)
    request = DecisionRequest.model_validate(request_dict())
    async with model_session(model):
        first = await model.ainvoke(request)
        second = await model.ainvoke(request)
    assert first == second
    assert first.model == "jev-1.14.0" and first.requested_model == model.model
    assert first.model_dump()["answers"] == response_dict()["answers"]
    assert first.native_usage == response_dict()["usage"]
    assert first.usage is not None and first.usage.completion_tokens == 42
    assert first.metadata == {"id": "test-reply", "provider": "TypeSafe"}
    assert not client.is_closed
    sent = json.loads(observed[0].content)
    assert sent == model.serialize(request)
    if route is TypeSafeDirect:
        assert str(observed[0].url) == "https://api.typesafe.ai/v1/systemone"
        assert observed[0].headers["TypeSafe-Danger-Zone-Images"] == "true"
        assert sent["assets"]["reference-image"] == request_dict()["assets"]["reference-image"]
    else:
        assert str(observed[0].url) == "https://openrouter.ai/api/alpha/decisions"
        assert sent["state"]["image"]["type"] == "image_url"
        assert "assets" not in sent
    await client.aclose()


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "extra",
        "type",
        "options",
        "nan",
        "bool",
        "negative",
        "sum",
        "argmax",
        "legend",
        "score",
        "box",
        "noul_confidence",
    ],
)
async def test_native_reply_validation_is_not_chat_retry(route, fault):
    raw = response_dict()
    answers = raw["answers"]
    if fault == "missing":
        answers.pop("team")
    elif fault == "extra":
        answers["extra"] = answers["urgent"]
    elif fault == "type":
        answers["urgent"] = deepcopy(answers["team"])
    elif fault == "options":
        answers["team"]["probabilities"]["missing"] = 0.0
    elif fault == "nan":
        answers["urgent"]["noul"] = float("nan")
    elif fault == "bool":
        answers["urgent"]["noul"] = True
    elif fault == "negative":
        answers["urgent"]["noul"] = -0.1
    elif fault == "sum":
        answers["team"]["probabilities"]["help"] = 0.8
    elif fault == "argmax":
        answers["team"]["choice"] = "other"
    elif fault == "legend":
        answers["severity"]["legend"]["1"] = "wrong"
    elif fault == "score":
        answers["severity"]["score"] = 0.1
    elif fault == "box":
        answers["target"]["box"]["right"] = 0.0
    elif fault == "noul_confidence":
        answers["urgent"]["confidence"] = 0.99
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps(raw)))
    ) as client:
        model = route(api_key="test", http_client=client)
        with pytest.raises(DecisionResponseError) as error:
            await model.ainvoke(DecisionRequest.model_validate(request_dict()))
        assert error.value.usage is not None
        assert error.value.usage.completion_tokens == 42
        assert error.value.raw_output
        await model.aclose()


@pytest.mark.parametrize("route", ROUTES)
async def test_rounded_probability_distribution_is_retained(route):
    raw = response_dict()
    raw["answers"]["team"]["probabilities"]["help"] = 0.89
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw))
    ) as client:
        model = route(api_key="test", http_client=client)
        result = await model.ainvoke(DecisionRequest.model_validate(request_dict()))
        assert result.model_dump()["answers"]["team"]["probabilities"]["help"] == 0.89
        await model.aclose()


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("status", [400, 413, 429, 500])
async def test_http_error_once_and_known_failure_usage(route, status):
    requests = []

    def reply(request):
        requests.append(request)
        return httpx.Response(
            status, json={"error": "failure", "usage": {"input_tokens": 7, "output_tokens": 0}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        model = route(api_key="test", http_client=client)
        with pytest.raises(DecisionLimitError if status == 413 else DecisionHTTPError) as error:
            await model.ainvoke(DecisionRequest.model_validate(request_dict()))
        assert len(requests) == 1
        assert error.value.usage is not None and error.value.usage.prompt_tokens == 7
        await model.aclose()


@pytest.mark.parametrize("route", ROUTES)
async def test_timeout_and_external_cancellation_propagate(route):
    async def timeout(request):
        raise httpx.ReadTimeout("no response", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        model = route(api_key="test", http_client=client)
        with pytest.raises(DecisionTimeoutError) as error:
            await model.ainvoke(DecisionRequest.model_validate(request_dict()))
        assert error.value.usage is None
        await model.aclose()
    started = asyncio.Event()

    async def blocked(request):
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("request should have been cancelled")

    async with httpx.AsyncClient(transport=httpx.MockTransport(blocked)) as client:
        model = route(api_key="test", http_client=client)
        task = asyncio.create_task(model.ainvoke(DecisionRequest.model_validate(request_dict())))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await model.aclose() == ()
        assert not client.is_closed


@pytest.mark.parametrize("route", ROUTES)
async def test_unsupported_images_fail_before_network(route):
    def forbidden(_):
        raise AssertionError("request must fail before HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        for model_id in ("jev-latest", "jev-1.13.0"):
            model = route(model=model_id, api_key="test", http_client=client)
            with pytest.raises(DecisionInputError):
                await model.ainvoke(DecisionRequest.model_validate(request_dict()))
        raw = request_dict()
        raw["assets"]["reference-image"] = image_asset((128, 128))
        with pytest.raises(DecisionLimitError):
            await route(api_key="test", http_client=client).ainvoke(
                DecisionRequest.model_validate(raw)
            )


@pytest.mark.parametrize("field", ["state", "instructions", "criteria"])
async def test_openrouter_images_in_all_logical_positions_and_score_legend(field):
    raw = request_dict()
    if field == "instructions":
        raw["questions"]["team"]["instructions"] = "compare reference-image now"
    if field == "criteria":
        raw["questions"]["severity"]["criteria"][0] = {"example": "reference-image"}
    request = DecisionRequest.model_validate(raw)
    model = TypeSafeOpenRouter(api_key="test")
    body = model.serialize(request)
    reply = response_dict()
    reply["answers"]["severity"]["legend"] = {
        str(i): v for i, v in enumerate(body["questions"]["severity"]["criteria"])
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=reply))
    ) as client:
        model.http_client = client
        result = await model.ainvoke(request)
        assert (
            result.model_dump()["answers"]["severity"]["legend"]
            == reply["answers"]["severity"]["legend"]
        )
        await model.aclose()


def test_structured_nulls_unreferenced_assets_and_unknown_fields():
    for value in ("text", {"record": [1, None]}, ["text", {"x": False}]):
        raw = request_dict()
        raw["state"] = {"data": value, "image": "reference-image"}
        assert DecisionRequest.model_validate(raw).state == raw["state"]
    raw = request_dict()
    raw["questions"]["severity"]["criteria"][0] = None
    assert DecisionRequest.model_validate(raw).questions["severity"].type == "score"
    for mutate in (
        lambda raw: raw.update(state={"reference-image": "only a key"}),
        lambda raw: raw.update(extra="unsupported"),
        lambda raw: raw["questions"]["target"].update(criteria=[]),
        lambda raw: raw.update(assets={}),
        lambda raw: raw.update(state={"image": "reference-image", "bad": float("inf")}),
    ):
        raw = request_dict()
        mutate(raw)
        with pytest.raises(ValidationError):
            DecisionRequest.model_validate(raw)


def test_service_root_convention_and_lazy_construction(monkeypatch):
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: (_ for _ in ()).throw(AssertionError("eager client"))
    )
    model = TypeSafeDirect(base_url="https://www.dmxapi.cn/typesafe", api_key_env="OPENAI_API_KEY")
    assert model.base_url is not None
    assert model.base_url + model.endpoint_path == "https://www.dmxapi.cn/typesafe/v1/systemone"
    for root in (
        "https://api.typesafe.ai/v1",
        "https://api.typesafe.ai/v1/systemone",
        "https://secret@example.test",
    ):
        with pytest.raises(ValueError):
            TypeSafeDirect(base_url=root)


@pytest.mark.parametrize("route", ROUTES)
async def test_owned_client_is_reused_closed_and_recreated(route, monkeypatch):
    factory = httpx.AsyncClient
    clients = []

    def make_client():
        client = factory(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response_dict()))
        )
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    model = route(api_key="test")
    request = DecisionRequest.model_validate(request_dict())
    async with model_session(model):
        await model.ainvoke(request)
        await model.ainvoke(request)
    assert len(clients) == 1 and clients[0].is_closed
    async with model_session(model):
        await model.ainvoke(request)
    assert len(clients) == 2 and clients[1].is_closed


@pytest.mark.parametrize("route", ROUTES)
async def test_route_specific_null_rejections_happen_before_http(route):
    def forbidden(_):
        raise AssertionError("unsupported request reached network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        model = route(api_key="test", http_client=client)
        raw = request_dict()
        raw["questions"]["severity"]["criteria"][0] = None
        with pytest.raises(DecisionInputError, match="Score"):
            await model.ainvoke(DecisionRequest.model_validate(raw))
        raw = request_dict()
        raw["questions"]["urgent"]["criteria"]["true"] = None
        with pytest.raises(DecisionInputError, match="Noul"):
            await model.ainvoke(DecisionRequest.model_validate(raw))
        if route is TypeSafeOpenRouter:
            raw = request_dict()
            raw["questions"]["team"]["instructions"] = None
            with pytest.raises(DecisionInputError, match="instructions"):
                await model.ainvoke(DecisionRequest.model_validate(raw))


async def test_typed_questions_keep_discriminators_and_omitted_criteria_on_record_roundtrip():
    from spectragent.core.llm.decision import NoulCriteria, NoulQuestion
    from spectragent.core.trace.encoding import PendingLLMCall, encode_llm_call
    from tests._helpers.trace_builder import read_back

    question = NoulQuestion(
        instructions="Is the answer positive?",
        criteria=NoulCriteria.model_validate({"true": "Positive"}),
    )
    request = DecisionRequest(state="positive", questions={"positive": question})
    pending = PendingLLMCall(None, kind="decision", request=request)
    pending.fail(RuntimeError("failed before a response"))
    restored = read_back(encode_llm_call(0, 0, pending))
    assert restored.kind == "decision"
    assert restored.request.logical_dump() == request.logical_dump()
    assert restored.request.logical_dump()["questions"]["positive"]["criteria"] == {
        "true": "Positive"
    }
    raw = {"model": "jev-1.14.0", "answers": {"positive": {"type": "noul", "noul": 0.99}}}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw))
    ) as client:
        model = TypeSafeDirect(api_key="test", http_client=client)
        answer = await model.ainvoke(restored.request)
        assert answer.answers["positive"].type == "noul"
