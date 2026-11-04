"""A sampling value the configuration leaves out is never sent.

User ruling 2026-11-04 (``docs/architecture_design.md`` §4.7): no provider
sends a temperature, penalty or other sampling value the configuration did not
write, so the model's own default applies. "Not sent" means the key is absent
from the request body, not present as ``null``: an SDK sends an argument given
as ``None`` as JSON ``null``, which some endpoints refuse and others read as a
value.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import oci
import pytest

from spectragent.core.composer import ConfigComposer
from spectragent.core.llm.messages import UserMessage
from spectragent.core.llm.providers.mistral import ChatMistral
from spectragent.core.llm.providers.oci import ChatOCI
from spectragent.core.llm.providers.openai import ChatOpenAI
from spectragent.core.llm.providers.openai_groq import ChatGroq

#: The settings that change how a reply is sampled, on any provider that has them.
SAMPLING = ("temperature", "frequency_penalty", "presence_penalty", "top_p", "top_k", "seed")

_PROMPT = [UserMessage(content="Say hi.")]
_REPLY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 0,
    "model": "stub",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
}


class _Wire:
    """Answers every request with one chat completion and keeps each request body."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_REPLY)

    @property
    def body(self) -> dict[str, Any]:
        (body,) = self.bodies
        return body


def test_no_provider_defaults_a_sampling_value() -> None:
    """Every chat provider's sampling fields default to "not sent", across the whole catalog."""
    catalog = ConfigComposer.for_every_package().catalog
    defaults = {
        (key, name): info.default
        for key, definition in catalog.components["llm"].items()
        for name, info in definition.config_model.model_fields.items()
        if name in SAMPLING
    }
    assert defaults, "the catalog holds providers with sampling fields"
    assert {setting: value for setting, value in defaults.items() if value is not None} == {}


@pytest.mark.parametrize(
    "provider",
    [
        lambda wire: ChatOpenAI(
            model="gpt-4o",
            api_key="stub",
            base_url="https://gateway.test/v1",
            http_client=wire.client(),
        ),
        lambda wire: ChatGroq(
            model="llama-3.3-70b-versatile", api_key="stub", http_client=wire.client()
        ),
        lambda wire: ChatMistral(
            model="mistral-medium-latest", api_key="stub", http_client=wire.client()
        ),
    ],
    ids=["openai", "groq", "mistral"],
)
async def test_an_unset_value_is_absent_from_the_request_body(provider) -> None:
    wire = _Wire()
    reply = await provider(wire).ainvoke(_PROMPT)
    assert reply.completion == "hi"
    body = wire.body
    assert not set(SAMPLING) & set(body), body
    assert None not in body.values(), "an unset setting is left out, never sent as null"


async def test_a_written_value_is_sent_as_written() -> None:
    wire = _Wire()
    chat = ChatOpenAI(
        model="gpt-4o",
        api_key="stub",
        base_url="https://gateway.test/v1",
        temperature=0.0,
        frequency_penalty=0.3,
        http_client=wire.client(),
    )
    await chat.ainvoke(_PROMPT)
    assert (wire.body["temperature"], wire.body["frequency_penalty"]) == (0.0, 0.3)


@pytest.mark.parametrize("oci_provider", ["meta", "cohere", "xai"])
async def test_oci_leaves_an_unset_value_out_of_the_body_its_sdk_writes(
    oci_provider: str, monkeypatch
) -> None:
    """OCI's SDK writes the body from the request object and drops an attribute that is None."""
    sent: list[Any] = []

    class _Captured(Exception):
        pass

    class _Client:
        def chat(self, details: Any) -> Any:
            sent.append(details)
            raise _Captured

    chat = ChatOCI(
        model_id="ocid1.generativeaimodel.oc1..stub",
        service_endpoint="https://oci.test",
        compartment_id="ocid1.compartment.oc1..stub",
        oci_provider=oci_provider,
    )
    monkeypatch.setattr(chat, "_get_oci_client", lambda _clients: _Client())
    with pytest.raises(Exception):  # noqa: B017 - the stub stops the call once it has the request
        await chat.ainvoke(_PROMPT)
    (details,) = sent
    serializer = oci.generative_ai_inference.GenerativeAiInferenceClient(
        {},
        signer=oci.auth.signers.SecurityTokenSigner.__new__(oci.auth.signers.SecurityTokenSigner),
        service_endpoint="https://oci.test",
    ).base_client
    body = serializer.sanitize_for_serialization(details)["chatRequest"]
    wire_names = {"temperature", "frequencyPenalty", "presencePenalty", "topP", "topK", "seed"}
    assert not wire_names & set(body), body
